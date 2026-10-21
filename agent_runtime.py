"""[M05] Pro*C 영향도 분석 에이전트 그래프.

    START → rewrite_query → understand ─┬─ (대상 없음) ─────────────────────────→ END
                                        ├─ impact → [extract_llm] → run_impact → explain → END
                                        └─ lookup → gather_facts → answer ──────────→ END

- 질문 이해(테이블·컬럼·프로그램 이름 찾기, 변경 종류 판단)는 규칙(코드)으로 한다.
  규칙으로 시나리오를 다 못 채웠을 때만 LLM 에게 JSON 으로 뽑게 한다.
- 영향도 판단(어느 함수에서 어떤 ORA 오류)은 impact_engine 규칙이 하고, LLM 은 설명만 쓴다.
- LLM(Ollama)에 연결할 수 없으면 규칙 결과로 만든 보고서를 그대로 돌려준다(decided_by=fallback).
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from langgraph.graph import END, START, StateGraph

import prompts
from config import (
    LLM_MODEL,
    LLM_NUM_CTX,
    MAX_CODE_CHARS,
    MAX_CONTEXT_CHARS,
    MAX_FINDINGS_IN_PROMPT,
    MAX_HISTORY_CHARS,
    MAX_HISTORY_TURNS,
    OLLAMA_HOST,
)
from db_runtime import SchemaMeta, column_type_text
from impact_engine import ScenarioError, analyze_impact_live, normalize_scenario
from schemas import AgentState, ConversationTurn
from source_repository import get_index, read_source_text

logger = logging.getLogger(__name__)

INSUFFICIENT_MESSAGE = (
    "질문에서 등록된 테이블·컬럼·프로그램 이름을 찾지 못했습니다.\n\n"
    "- `TB_ORDER.STATUS 값을 '01'에서 '05'로 바꾸면?` 처럼 테이블.컬럼 이름을 넣어 질문하시거나\n"
    "- Pro*C 소스와 배치 스크립트를 먼저 등록하고, DB 메타데이터를 가져와 주세요.\n"
    "- 정확한 조건으로 보려면 **영향도 분석** 탭의 입력 양식을 쓰세요."
)
SCENARIO_HELP = (
    "어떤 변경인지 정확히 알 수 없습니다. 예시처럼 다시 질문해 주세요.\n\n"
    "- `TB_CUST.CUST_NM 길이를 50으로 늘리면?`\n"
    "- `TB_ORDER.AMT 를 NUMBER(20,2)로 바꾸면?`\n"
    "- `TB_CUST.GRADE 를 NULL 허용으로 바꾸면?`\n"
    "- `TB_ORDER.QTY 컬럼을 삭제하면?`\n"
    "- `TB_ORDER.STATUS 값을 '01'에서 '05'로 바꾸면?`\n\n"
    "또는 **영향도 분석** 탭에서 직접 선택할 수 있습니다."
)


# ---------- 공용 ----------

@lru_cache(maxsize=1)
def get_llm():
    return ChatOllama(base_url=OLLAMA_HOST, model=LLM_MODEL, temperature=0, num_ctx=LLM_NUM_CTX, keep_alive="10m")


def is_llm_unavailable(exc: Exception) -> bool:
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & {"ConnectError", "ConnectionError", "ConnectTimeout"}) or "Failed to connect to Ollama" in str(exc)


def bound_history(history, max_turns: int = MAX_HISTORY_TURNS, max_chars: int = MAX_HISTORY_CHARS) -> List[ConversationTurn]:
    """클라이언트가 보낸 대화 기록을 정리하고, 턴 수·글자 수 한도 안으로 자른다(오래된 것부터 제거)."""
    if not history or max_turns <= 0 or max_chars <= 0:
        return []
    turns: List[ConversationTurn] = []
    for raw in history:
        if not isinstance(raw, dict):
            continue
        q = str(raw.get("question") or "").strip()
        a = str(raw.get("answer") or "").strip()
        if not q:
            continue
        if len(a) > 600:
            a = a[:600] + " …(생략)"
        turns.append({"question": q[:500], "answer": a})
    turns = turns[-max_turns:]
    while turns and sum(len(t["question"]) + len(t["answer"]) for t in turns) > max_chars:
        turns.pop(0)
    return turns


# ---------- 질문 이해 (규칙) ----------

_IDENT_RE = re.compile(r"[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?")
_QUOTED_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"|‘([^’]*)’|“([^”]*)”")
_TYPE_RE = re.compile(r"\b(VARCHAR2|NVARCHAR2|VARCHAR|CHAR|NCHAR|NUMBER|INTEGER|DATE|TIMESTAMP|CLOB)\b\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+))?\s*(?:CHAR|BYTE)?\s*\))?", re.I)


def known_names(meta: SchemaMeta) -> Dict:
    idx = get_index()
    tables = set(idx.table_usage) | {t["name"] for t in meta.tables.values()} | set(meta.synonyms)
    columns: Dict[str, set] = {}
    for st in idx.statements.values():
        for c in (st.get("columns") or []) + (st.get("bindings") or []) + (st.get("literals") or []):
            if not c.get("column"):
                continue
            for t in ([c["table"]] if c.get("table") else c.get("candidates") or []):
                columns.setdefault(t, set()).add(c["column"].upper())
    for t in meta.tables.values():
        columns.setdefault(t["name"], set()).update(c["name"] for c in t["columns"])
    return {"tables": tables, "columns": columns, "programs": {p.lower(): p for p in idx.programs},
            "functions": set(idx.func_defs)}


def find_entities(text: str, meta: SchemaMeta) -> Dict:
    names = known_names(meta)
    tables, cols, progs, funcs = [], [], [], []
    tokens = _IDENT_RE.findall(text)
    for tok in tokens:
        up = tok.upper()
        if "." in up:
            t, c = up.split(".", 1)
            if t in names["tables"]:
                tables.append(t)
                cols.append((t, c))
                continue
            if t.lower() in names["programs"]:
                progs.append(names["programs"][t.lower()])
                if c.lower() in {f.lower() for f in names["functions"]}:
                    funcs.append(next(f for f in names["functions"] if f.lower() == c.lower()))
                continue
        if up in names["tables"]:
            tables.append(up)
        elif tok.lower() in names["programs"]:
            progs.append(names["programs"][tok.lower()])
        elif tok in names["functions"] and len(tok) > 3:
            funcs.append(tok)
    tables = list(dict.fromkeys(tables))
    # 테이블 없이 컬럼만 말한 경우: 그 컬럼을 가진 테이블이 하나뿐이거나, 이미 찾은 테이블의 컬럼일 때
    for tok in tokens:
        up = tok.upper()
        if "." in up or up in names["tables"] or len(up) < 2:
            continue
        owners = [t for t, cs in names["columns"].items() if up in cs]
        in_found = [t for t in tables if t in owners]
        if in_found:
            cols.append((in_found[0], up))
        elif len(owners) == 1 and not tables:
            tables.append(owners[0])
            cols.append((owners[0], up))
    cols = list(dict.fromkeys(cols))
    return {"tables": tables, "columns": [f"{t}.{c}" for t, c in cols], "programs": list(dict.fromkeys(progs)),
            "functions": list(dict.fromkeys(funcs))}


_CHANGE_WORDS = re.compile(r"(바꾸|바뀌|바꿔|변경|늘리|늘어|줄이|줄어|삭제|지우|제거|없애|drop|추가|add|alter|rename|수정|"
                           r"손보|건드리|고치|변환|update|delete|insert|넣|들어오|들어가|되면|하면|어떻게\s*돼|NULL|널|타입|길이|사이즈|크기)", re.I)
_LOOKUP_WORDS = re.compile(r"(어떤 프로그램|쓰는 곳|쓰는 프로그램|사용처|목록|설명해|알려줘|보여줘)")


def parse_scenario(text: str, ent: Dict, meta: SchemaMeta) -> Optional[Dict]:
    """규칙으로 시나리오를 만든다. 판단할 수 없으면 None."""
    if not ent["tables"]:
        return None
    col_pairs = [c.split(".", 1) for c in ent["columns"]]
    table = col_pairs[0][0] if col_pairs else ent["tables"][0]
    column = col_pairs[0][1] if col_pairs else None
    low = text.lower()
    quoted = [next(g for g in m.groups() if g is not None) for m in _QUOTED_RE.finditer(text)]
    s: Dict = {"table": table}
    if column:
        s["column"] = column
    cur = meta.column(table, column) if column else None

    is_data = bool(quoted) or re.search(r"(데이터|값을|값이|값\s|행을|행이|건을|레코드|row)", text) is not None
    tm = _TYPE_RE.search(text)

    if column is None and re.search(r"(삭제|drop|제거|없애|없어지)", low):
        if is_data and re.search(r"(데이터|행|건|레코드)", text):
            s.update(change="data", operation="delete")
        else:
            s["change"] = "drop_table"
        return s
    if column is None and re.search(r"(이름|명칭|rename)", low):
        new = _new_name(text, table, None)
        s.update(change="rename_table", new_name=new)
        return s
    if column is None and re.search(r"(추가|insert|넣)", low) and is_data:
        s.update(change="data", operation="insert")
        return s
    if column is None:
        return None

    where = re.search(r"\bWHERE\b\s+(.+)$", text, re.I)
    if where:
        s["condition"] = where.group(1).strip().rstrip("?")

    if re.search(r"NULL\s*허용|널\s*허용|nullable|NULL\s*가능|NULL\s*로\s*허용", text, re.I):
        s.update(change="nullable", nullable=True)
        return s
    if re.search(r"NOT\s*NULL|필수\s*(값|컬럼)?\s*(로|으로)", text, re.I):
        s.update(change="nullable", nullable=False)
        return s
    if re.search(r"(NULL|널)\s*(이|값이|로|으로|을|가)?\s*(들어|되면|바뀌|넣|변경|update)", text, re.I):
        s.update(change="data", operation="set_null")
        return s
    if re.search(r"(컬럼|column).{0,10}(삭제|drop|제거|없애)|(삭제|drop|제거)\s*(하면|시)", low) and not is_data:
        s["change"] = "drop_column"
        return s
    if re.search(r"(컬럼|column).{0,10}(추가|add)", low) or (re.search(r"추가", low) and cur is None and not is_data):
        s["change"] = "add_column"
        if re.search(r"NOT\s*NULL", text, re.I):
            s["nullable"] = False
        return s
    if re.search(r"(이름|명칭|컬럼명|rename)", low) and not quoted:
        s.update(change="rename_column", new_name=_new_name(text, table, column))
        return s
    if re.search(r"(길이|사이즈|크기|size|자리|바이트|byte)", low) or (tm and tm.group(2) and cur and
                                                                     tm.group(1).upper() == cur.get("type") and cur.get("type") != "NUMBER"):
        n = None
        if tm and tm.group(2):
            n = int(tm.group(2))
        else:
            nums = [int(x) for x in re.findall(r"(\d+)\s*(?:자리|자|바이트|byte|으로|로|까지)", text, re.I)] or \
                   [int(x) for x in re.findall(r"\d+", text)]
            n = nums[-1] if nums else None
        if n:
            s.update(change="length", new_length=n)
            return s
    if tm and re.search(r"(타입|형식|자료형|type|으로|로)", low):
        s.update(change="type", new_type=tm.group(1).upper())
        if tm.group(2):
            s["new_precision"] = int(tm.group(2))
        if tm.group(3):
            s["new_scale"] = int(tm.group(3))
        return s
    if is_data:
        op = "update"
        if re.search(r"(삭제|delete|지우)", low):
            op = "delete"
        elif re.search(r"(추가|insert|신규)", low):
            op = "insert"
        s.update(change="data", operation=op)
        if op == "update" and len(quoted) >= 2:
            s["old_value"], s["new_value"] = quoted[0], quoted[1]
        elif quoted:
            m = re.search(r"['\"‘“]([^'\"’”]*)['\"’”]\s*(?:으로|로|를|을)?\s*(?:바꾸|변경|수정|되면|하면|update|넣)", text)
            if op == "update":
                s["new_value"] = m.group(1) if m else quoted[-1]
            else:
                s["old_value" if op == "delete" else "new_value"] = quoted[0]
        return s
    return None


def _new_name(text: str, table: str, column: Optional[str]) -> str:
    skip = {table, column or ""}
    cands = [t.upper() for t in re.findall(r"([A-Za-z_][\w$#]*)\s*(?:으로|로|to)\b", text)]
    cands += [t.upper() for t in re.findall(r"(?:→|->)\s*([A-Za-z_][\w$#]*)", text)]
    for c in reversed(cands):
        if c not in skip and c.upper() not in ("NULL",):
            return c
    return ""


def _scenario_complete(s: Optional[Dict]) -> bool:
    if not s or not s.get("change"):
        return False
    try:
        normalize_scenario(s)
        return True
    except ScenarioError:
        return False


# ---------- 결과 → 문자열 ----------

SEV_KO = {"ERROR": "에러 예상", "WARN": "주의", "INFO": "참고"}


def impact_facts(result: Dict) -> str:
    lines = [f"[변경 시나리오]\n{result['description']}"]
    cm = result.get("column_meta")
    if cm:
        lines.append(f"[현재 컬럼 정보] {column_type_text(cm)}, {'NULL 허용' if cm.get('nullable') else 'NOT NULL'}"
                     + (f", 설명: {cm['comment']}" if cm.get("comment") else ""))
    sm = result["summary"]
    lines.append(f"[요약 수치] 에러 예상 {sm['error']}건, 주의 {sm['warn']}건, 참고 {sm['info']}건, "
                 f"영향 프로그램: {', '.join(sm['programs']) or '없음'}")
    lines.append("[분석 결과]")
    for i, f in enumerate(result["findings"][:MAX_FINDINGS_IN_PROMPT], 1):
        batch = ", ".join(f"{b['script']} {b['order']}번째" for b in f.get("batch") or [] if b.get("order"))
        path = " → ".join(f["call_paths"][0]) if f.get("call_paths") else f["function"]
        lines.append(
            f"{i}. [{f['severity']}] {f['program']}.{f['function']}({f['file_name']}:{f['line']}) {f['code']} {f['title']}"
            + (" (확실도 낮음)" if not f.get("certain", True) else "")
            + (" (현재도 존재하는 문제)" if f.get("existing") else "")
            + f"\n   이유: {f['message']}"
            + (f"\n   변수: {f['host']}" if f.get("host") else "")
            + f"\n   SQL: {f['sql'][:200]}"
            + (f"\n   배치: {batch}" if batch else "")
            + f"\n   호출 경로: {path}")
    if len(result["findings"]) > MAX_FINDINGS_IN_PROMPT:
        lines.append(f"... 외 {len(result['findings']) - MAX_FINDINGS_IN_PROMPT}건")
    if result.get("downstream"):
        lines.append("[뒤 단계 영향]")
        for d in result["downstream"][:10]:
            readers = ", ".join(f"{r['program']}.{r['function']}" + (" (배치상 뒤)" if r.get("after") else "")
                                for r in d["readers"][:6])
            lines.append(f"- {d['from_program']}.{d['from_function']} 이(가) {d['table']} 에 씀 → 읽는 곳: {readers}")
    if result.get("db_objects"):
        lines.append("[DB 안의 사용처 (프로시저·패키지·트리거·뷰)]")
        for o in result["db_objects"][:15]:
            hit = f" — 컬럼 사용 {len(o['lines'])}줄 (예: {o['lines'][0]['line']}줄 {o['lines'][0]['text'].strip()[:80]})" if o.get("lines") else ""
            lines.append(f"- {o['type']} {o['owner']}.{o['name']}{hit}")
    if result.get("db_checks"):
        rows = []
        for c in result["db_checks"]:
            if c.get("result"):
                r = c["result"]
                preview = "; ".join(", ".join(f"{k}={v}" for k, v in zip(r["columns"], row)) for row in r["rows"][:5])
                rows.append(f"- {c['title']}: {preview}")
            elif c.get("error"):
                rows.append(f"- {c['title']}: 조회 실패 ({c['error']})")
        if rows:
            lines.append("[DB 확인 결과]\n" + "\n".join(rows))
    if result.get("notes"):
        lines.append("[참고]\n" + "\n".join(f"- {n}" for n in result["notes"]))
    text = "\n".join(lines)
    return text[:MAX_CONTEXT_CHARS + 2000]


def fallback_report(result: Dict) -> str:
    """LLM 없이 규칙 결과로 만드는 보고서."""
    sm = result["summary"]
    out = [f"## 요약\n{result['description']}\n\n에러 예상 **{sm['error']}건**, 주의 **{sm['warn']}건**, 참고 {sm['info']}건 · "
           f"영향 프로그램: {', '.join(sm['programs']) or '없음'}"]
    rows = [f for f in result["findings"] if f["severity"] in ("ERROR", "WARN")]
    out.append("## 에러 예상 지점")
    if rows:
        out.append("| 심각도 | 위치 | 예상 오류 | 이유 |\n|---|---|---|---|")
        for f in rows[:40]:
            msg = f["message"].replace("|", "\\|").replace("\n", " ")
            out.append(f"| {SEV_KO[f['severity']]} | {f['program']}.{f['function']} ({f['file_name']}:{f['line']}) | {f['code']} | {msg} |")
    else:
        out.append("에러가 예상되는 지점을 찾지 못했습니다.")
    infos = [f for f in result["findings"] if f["severity"] == "INFO"]
    if infos:
        out.append("## 참고")
        out += [f"- {f['program']}.{f['function']} ({f['file_name']}:{f['line']}) {f['title']}: {f['message']}" for f in infos[:20]]
    if result.get("downstream"):
        out.append("## 배치 흐름 영향")
        for d in result["downstream"][:10]:
            readers = ", ".join(f"{r['program']}.{r['function']}" for r in d["readers"][:6])
            out.append(f"- {d['from_program']}.{d['from_function']} → {d['table']} → {readers}")
    if result.get("db_objects"):
        out.append("## DB 안의 사용처 (프로시저·트리거·뷰)")
        for o in result["db_objects"][:20]:
            hit = f" — 컬럼 사용 {len(o['lines'])}줄" if o.get("lines") else ""
            out.append(f"- {o['type']} {o['owner']}.{o['name']}{hit}")
    if result.get("notes"):
        out.append("## 참고 사항\n" + "\n".join(f"- {n}" for n in result["notes"]))
    return "\n\n".join(out)


def impact_evidence(result: Dict) -> List[Dict]:
    return [{"source": f"{f['file_name']}:{f['line']} {f['function']}() [{f['code']}]", "snippet": f["sql"][:160]}
            for f in result["findings"][:20]]


def explain_impact(result: Dict, question: str = "", history: Optional[List] = None) -> Dict:
    """영향도 결과를 LLM 으로 설명한다. LLM 연결이 안 되면 규칙 보고서."""
    if not result["findings"] and not result.get("db_checks"):
        return {"report": fallback_report(result), "decided_by": "rule"}
    human = impact_facts(result)
    hb = prompts.build_history_block(history or [])
    if hb:
        human += "\n\n" + hb
    human += f"\n\n[질문]\n{question or result['description'] + ' 영향도를 분석해 주세요.'}"
    try:
        msg = get_llm().invoke([SystemMessage(content=prompts.IMPACT_SYSTEM), HumanMessage(content=human)])
        text = (msg.content or "").strip()
        if not text:
            raise ValueError("empty")
        return {"report": text, "decided_by": "llm"}
    except Exception as e:
        if not is_llm_unavailable(e) and not isinstance(e, ValueError):
            raise
        logger.warning("llm unavailable; using rule report")
        return {"report": fallback_report(result), "decided_by": "fallback",
                "note": "LLM(Ollama)에 연결할 수 없어 규칙 분석 결과만 표시했습니다."}


# ---------- 조회(lookup) 사실 모으기 ----------

def lookup_facts(ent: Dict, meta: SchemaMeta):
    idx = get_index()
    parts, evidence = [], []
    opened = {(s["program"], s.get("cursor_stmt")) for s in idx.statements.values() if s["kind"] == "open"}
    for t in ent["tables"][:3]:
        tm = meta.table(t)
        usage = [u for u in idx.table_usage.get(t, [])]
        head = f"[테이블 {t}]" + (f" {tm.get('comment')}" if tm and tm.get("comment") else "")
        if tm:
            head += f" (스키마 {tm.get('owner')}, 약 {tm.get('num_rows')}행, PK: {', '.join(tm.get('pk') or []) or '없음'})"
        parts.append(head)
        wanted = [c.split(".", 1)[1] for c in ent["columns"] if c.startswith(t + ".")]
        if tm:
            cols = [c for c in tm["columns"] if not wanted or c["name"] in wanted]
            parts.append("컬럼: " + ", ".join(f"{c['name']} {column_type_text(c)}{'' if c['nullable'] else ' NOT NULL'}"
                                             + (f"({c['comment']})" if c.get("comment") else "") for c in cols[:40]))
        by_prog: Dict[str, List[str]] = {}
        for u in usage:
            st = idx.statements.get(u["stmt"]) or {}
            if st.get("kind") == "cursor" and (u["program"], st.get("id")) in opened:
                continue  # OPEN 문장에서 같이 보여준다
            col_txt = ""
            if wanted:
                roles = sorted({c.get("role", "") for c in st.get("columns") or [] if c.get("column") in wanted})
                if not roles:
                    continue
                col_txt = f" 컬럼용도={'/'.join(roles)}"
            by_prog.setdefault(u["program"], []).append(
                f"{u['function']}():{u['line']} {u['kind'].upper()} {u['access']}{col_txt}")
            evidence.append({"source": f"{st.get('file_name', u['program'])}:{u['line']} {u['function']}()",
                             "snippet": (st.get("sql") or "")[:160]})
        if by_prog:
            parts.append("사용하는 곳:")
            for p, items in list(by_prog.items())[:30]:
                runs = idx.program_runs.get(p) or []
                batch = ", ".join(f"{r['script']} {r['order']}번째" for r in runs if r.get("order"))
                parts.append(f"- {p}{' [배치: ' + batch + ']' if batch else ''}: " + "; ".join(items[:8]))
        else:
            parts.append("사용하는 곳: 등록된 Pro*C 소스에서 찾지 못함")
    for p in ent["programs"][:2]:
        prog = idx.programs.get(p)
        if not prog:
            continue
        runs = idx.program_runs.get(p) or []
        parts.append(f"[프로그램 {p}] 파일 {prog['file_name']}, {prog['line_count']}줄, main {'있음' if prog['has_main'] else '없음'}"
                     + (", 배치: " + ", ".join(f"{r['script']} {r['order']}번째" for r in runs if r.get("order")) if runs else ""))
        for fn in prog["functions"][:30]:
            sts = [idx.statements.get(f"{p}:{sid.split(':')[-1]}") for sid in fn["statements"]]
            sql = "; ".join(f"{s['kind'].upper()} " + ",".join(f"{t['name']}({t['access']})" for t in s.get("tables") or [])
                            for s in sts if s and s.get("tables"))
            parts.append(f"- {fn['name']}() {fn['line']}~{fn['end_line']}줄, 호출: {', '.join(fn['calls'][:8]) or '-'}"
                         + (f", SQL: {sql}" if sql else ""))
        evidence.append({"source": prog["file_name"], "snippet": f"함수 {len(prog['functions'])}개"})
    for fname in ent["functions"][:2]:
        for key in idx.func_defs.get(fname, [])[:2]:
            fn = idx.functions[key]
            src = read_source_text(fn["file_name"]) or ""
            code = "\n".join(src.splitlines()[fn["line"] - 1:fn["end_line"]])
            parts.append(f"[함수 {key}] {fn['file_name']} {fn['line']}~{fn['end_line']}줄\n```c\n{code[:MAX_CODE_CHARS]}\n```")
            evidence.append({"source": f"{fn['file_name']}:{fn['line']} {fn['name']}()", "snippet": code[:160]})
    return "\n".join(parts)[:MAX_CONTEXT_CHARS], evidence


# ---------- 노드 ----------

def node_rewrite_query(state: AgentState) -> dict:
    query = state["query"]
    history = state.get("history") or []
    if not history:
        return {"search_query": query}
    try:
        msg = get_llm().invoke([
            SystemMessage(content=prompts.REWRITE_SYSTEM),
            HumanMessage(content=f"{prompts.build_history_block(history)}\n\n[마지막 질문]\n{query}"),
        ])
        rewritten = (msg.content or "").strip().splitlines()[0].strip(" \"'`") if msg.content else ""
    except Exception:
        logger.warning("trace_id=%s query rewrite failed; using original", state.get("trace_id"))
        rewritten = ""
    if not rewritten or len(rewritten) > 300:
        rewritten = query
    return {"search_query": rewritten}


def node_understand(state: AgentState) -> dict:
    text = state.get("search_query") or state["query"]
    meta = SchemaMeta()
    ent = find_entities(text, meta)
    # 재작성 질의에서 못 찾으면 원문으로 한 번 더
    if not any(ent.values()) and text != state["query"]:
        ent = find_entities(state["query"], meta)
    notes = list(state.get("notes") or [])
    if not any(ent.values()):
        return {"entities": ent, "intent": "none", "status": "INSUFFICIENT_EVIDENCE", "decided_by": "rule",
                "result_text": INSUFFICIENT_MESSAGE, "notes": notes}
    if ent["tables"] and (_CHANGE_WORDS.search(text) or _QUOTED_RE.search(text)):
        scenario = parse_scenario(text, ent, meta)
        if _scenario_complete(scenario) or not _LOOKUP_WORDS.search(text):
            return {"entities": ent, "intent": "impact", "scenario": scenario, "notes": notes}
    return {"entities": ent, "intent": "lookup", "notes": notes}


def route_after_understand(state: AgentState) -> str:
    if state.get("intent") == "none":
        return "end"
    if state.get("intent") == "impact":
        return "run" if _scenario_complete(state.get("scenario")) else "extract"
    return "lookup"


def node_extract_llm(state: AgentState) -> dict:
    """규칙으로 못 채운 시나리오를 LLM 에게 JSON 으로 뽑게 한다."""
    ent = state.get("entities") or {}
    meta = SchemaMeta()
    known = []
    for t in ent.get("tables", [])[:3]:
        tm = meta.table(t)
        cols = [c["name"] for c in tm["columns"]][:60] if tm else []
        known.append(f"{t}: {', '.join(cols)}" if cols else t)
    human = "[알려진 테이블/컬럼]\n" + "\n".join(known) + f"\n\n[문장]\n{state.get('search_query') or state['query']}"
    scenario = None
    try:
        msg = get_llm().invoke([SystemMessage(content=prompts.EXTRACT_SYSTEM), HumanMessage(content=human)])
        m = re.search(r"\{.*\}", msg.content or "", re.S)
        if m:
            data = json.loads(m.group())
            data = {k: v for k, v in data.items() if v not in (None, "", "null")}
            base = state.get("scenario") or {}
            scenario = {**base, **data}
    except Exception as e:
        logger.warning("trace_id=%s scenario extract failed: %s", state.get("trace_id"), type(e).__name__)
    if _scenario_complete(scenario):
        notes = list(state.get("notes") or [])
        notes.append("질문을 다음 시나리오로 해석했습니다(LLM). 다르면 영향도 분석 탭에서 직접 지정해 주세요.")
        return {"scenario": scenario, "notes": notes}
    return {"scenario": None, "status": "UNCERTAIN", "decided_by": "rule", "result_text": SCENARIO_HELP}


def route_after_extract(state: AgentState) -> str:
    return "run" if _scenario_complete(state.get("scenario")) else "end"


def node_run_impact(state: AgentState) -> dict:
    result = analyze_impact_live(get_index(), state["scenario"])
    return {"impact": result, "scenario": result["scenario"]}


def node_explain(state: AgentState) -> dict:
    result = state["impact"]
    out = explain_impact(result, state.get("search_query") or state["query"], state.get("history"))
    notes = list(state.get("notes") or []) + list(result.get("notes") or [])
    if out.get("note"):
        notes.append(out["note"])
    status = "SUCCESS" if result["findings"] else "UNCERTAIN"
    return {"result_text": out["report"], "decided_by": out["decided_by"], "status": status,
            "evidence": impact_evidence(result), "notes": list(dict.fromkeys(notes))}


def node_gather_facts(state: AgentState) -> dict:
    facts, evidence = lookup_facts(state["entities"], SchemaMeta())
    return {"facts": facts, "evidence": evidence}


def node_answer(state: AgentState) -> dict:
    hb = prompts.build_history_block(state.get("history") or [])
    human = f"[분석 사실]\n{state.get('facts') or '(없음)'}\n\n" + (f"{hb}\n\n" if hb else "") + f"[질문]\n{state['query']}"
    try:
        msg = get_llm().invoke([SystemMessage(content=prompts.LOOKUP_SYSTEM), HumanMessage(content=human)])
        return {"result_text": msg.content, "decided_by": "llm", "status": "SUCCESS"}
    except Exception as e:
        if not is_llm_unavailable(e):
            raise
        notes = list(state.get("notes") or []) + ["LLM(Ollama)에 연결할 수 없어 분석 사실만 표시했습니다."]
        return {"result_text": "```\n" + (state.get("facts") or "") + "\n```", "decided_by": "fallback",
                "status": "UNCERTAIN", "notes": notes}


# ---------- 그래프 ----------

@lru_cache(maxsize=1)
def build_agent_graph():
    g = StateGraph(AgentState)
    g.add_node("rewrite_query", node_rewrite_query)
    g.add_node("understand", node_understand)
    g.add_node("extract_llm", node_extract_llm)
    g.add_node("run_impact", node_run_impact)
    g.add_node("explain", node_explain)
    g.add_node("gather_facts", node_gather_facts)
    g.add_node("answer", node_answer)

    g.add_edge(START, "rewrite_query")
    g.add_edge("rewrite_query", "understand")
    g.add_conditional_edges("understand", route_after_understand,
                            {"end": END, "run": "run_impact", "extract": "extract_llm", "lookup": "gather_facts"})
    g.add_conditional_edges("extract_llm", route_after_extract, {"run": "run_impact", "end": END})
    g.add_edge("run_impact", "explain")
    g.add_edge("explain", END)
    g.add_edge("gather_facts", "answer")
    g.add_edge("answer", END)
    return g.compile()
