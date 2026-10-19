import pytest

pytest.importorskip("langgraph")

import agent_runtime  # noqa: E402
from db_runtime import SchemaMeta  # noqa: E402


def _initial(query, **kw):
    state = {"trace_id": "t", "query": query, "history": [], "entities": {}, "scenario": None, "impact": None,
             "facts": "", "evidence": [], "result_text": "", "decided_by": "", "status": "", "notes": []}
    state.update(kw)
    return state


def _run(query, **kw):
    return agent_runtime.build_agent_graph().invoke(_initial(query, **kw))


def _scenario(text):
    meta = SchemaMeta()
    return agent_runtime.parse_scenario(text, agent_runtime.find_entities(text, meta), meta)


# ---------- 질문 이해 (규칙) ----------

@pytest.mark.parametrize("text, expected", [
    ("TB_CUST.CUST_NM 길이를 50으로 늘리면?", {"change": "length", "new_length": 50, "column": "CUST_NM"}),
    ("TB_CUST.CUST_NM 을 VARCHAR2(100)으로 바꾸면", {"change": "length", "new_length": 100}),
    ("TB_ORDER.AMT 를 NUMBER(20,2)로 바꾸면?", {"change": "type", "new_type": "NUMBER", "new_precision": 20, "new_scale": 2}),
    ("TB_CUST.GRADE 를 NULL 허용으로 바꾸면", {"change": "nullable", "nullable": True}),
    ("TB_CUST.GRADE 에 NULL 이 들어오면?", {"change": "data", "operation": "set_null"}),
    ("TB_ORDER.QTY 컬럼을 삭제하면?", {"change": "drop_column"}),
    ("TB_ORDER.STATUS 값을 '01'에서 '05'로 바꾸면?", {"change": "data", "operation": "update", "old_value": "01", "new_value": "05"}),
    ("TB_ORDER_HIST 테이블을 삭제하면", {"change": "drop_table", "table": "TB_ORDER_HIST"}),
    ("GRADE 길이를 5로", {"change": "length", "table": "TB_CUST", "new_length": 5}),
])
def test_parse_scenario(text, expected):
    s = _scenario(text)
    assert s is not None
    for k, v in expected.items():
        assert s.get(k) == v, (k, s)


def test_entities_programs_and_functions():
    ent = agent_runtime.find_entities("ORD010 의 fetch_orders 함수 설명해줘", SchemaMeta())
    assert ent["programs"] == ["ORD010"] and ent["functions"] == ["fetch_orders"]


# ---------- 그래프 ----------

def test_impact_flow_uses_llm_for_explanation(fake_models):
    fake_models.responses = ["## 요약\n보고서"]
    r = _run("TB_CUST.CUST_NM 길이를 50으로 늘리면 어디서 에러가 나?")
    assert r["intent"] == "impact" and r["decided_by"] == "llm"
    assert r["impact"]["summary"]["error"] >= 2
    human = fake_models.calls[-1][1].content
    assert "fetch_orders" in human and "ORA-01406" in human     # 규칙 결과가 근거로 들어간다
    assert r["evidence"]


def test_impact_falls_back_when_llm_down(fake_models):
    fake_models.responses = [ConnectionError("Failed to connect to Ollama")]
    r = _run("TB_CUST.CUST_NM 길이를 50으로 늘리면?")
    assert r["decided_by"] == "fallback"
    assert "ORA-01406" in r["result_text"] and any("LLM" in n for n in r["notes"])


def test_unknown_entities_skip_llm(fake_models):
    r = _run("점심 메뉴 추천해줘")
    assert r["status"] == "INSUFFICIENT_EVIDENCE" and fake_models.calls == []


def test_lookup_flow(fake_models):
    fake_models.responses = ["TB_ORDER 는 ORD010, ORD020 에서 씁니다."]
    r = _run("TB_ORDER 테이블을 쓰는 프로그램 알려줘")
    assert r["intent"] == "lookup"
    facts = fake_models.calls[-1][1].content
    assert "ORD010" in facts and "ORD020" in facts and "daily_ord.sh" in facts


def test_function_code_in_lookup(fake_models):
    fake_models.responses = ["설명"]
    _run("ORD010 의 confirm_order 함수 설명해줘")
    assert "INSERT INTO TB_ORDER_HIST" in fake_models.calls[-1][1].content


def test_llm_extracts_when_rules_cannot(fake_models):
    fake_models.responses = ['{"table": "TB_ORDER", "column": "STATUS", "change": "data", "operation": "update", "new_value": "09"}',
                             "보고서"]
    r = _run("TB_ORDER.STATUS 를 손보면 어떻게 돼?")
    assert r["scenario"]["new_value"] == "09" and r["decided_by"] == "llm"


def test_history_rewrites_query(fake_models):
    fake_models.responses = ["TB_CUST.CUST_NM 길이를 50으로 늘리면?", "보고서"]
    r = _run("그 컬럼 길이를 50으로 늘리면?", history=[{"question": "TB_CUST.CUST_NM 쓰는 곳", "answer": "ORD010"}])
    assert r["search_query"].startswith("TB_CUST.CUST_NM") and r["intent"] == "impact"


def test_bound_history_limits():
    h = [{"question": f"q{i}", "answer": "a" * 1000} for i in range(10)]
    out = agent_runtime.bound_history(h, max_turns=4, max_chars=2000)
    assert len(out) <= 4 and all(len(t["answer"]) <= 610 for t in out)
