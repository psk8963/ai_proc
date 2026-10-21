import io
import json
import zipfile

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("langgraph")
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import source_repository  # noqa: E402

client = TestClient(app_module.app)


def test_status_counts_samples():
    r = client.get("/api/status")
    assert r.status_code == 200
    a = r.json()["assets"]
    assert a["programs"] == 2 and a["headers"] == 1 and a["scripts"] == 2
    assert r.json()["db"]["configured"] is False


def test_upload_zip_and_reject_paths():
    source_repository.clear_assets("source")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("src/../../evil.pc", "int main(){ EXEC SQL SELECT 1 INTO :x FROM DUAL; return 0; }")
        z.writestr("src/readme.md", "skip")
    r = client.post("/api/upload/source", files=[("files", ("all.zip", buf.getvalue(), "application/zip"))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["saved"] == 1 and body["skipped_count"] == 1
    assert [p["program"] for p in client.get("/api/programs").json()["programs"]] == ["evil"]


def test_programs_and_detail():
    progs = client.get("/api/programs").json()["programs"]
    ord010 = next(p for p in progs if p["program"] == "ORD010")
    assert {"name": "TB_ORDER", "access": "RW"} in ord010["tables"]
    d = client.get("/api/programs/ord010").json()
    fn = next(f for f in d["functions"] if f["name"] == "fetch_orders")
    assert fn["called_by"] == ["main"] and any(s["kind"] == "fetch" for s in fn["statements"])


def test_source_text_and_batch():
    r = client.get("/api/source/ORD010.pc", params={"start": 57, "end": 57})
    assert "OPEN c_ord" in r.json()["lines"][0]
    b = client.get("/api/batches/daily_ord.sh").json()
    steps = [s for s in b["steps"] if s.get("program")]
    assert [s["program"] for s in steps] == ["ORD010", "ORD020"]
    assert "TB_ORDER_HIST" in steps[0]["writes"]


def test_tables_detail_uses_snapshot():
    t = client.get("/api/tables/TB_CUST").json()
    cust_nm = next(c for c in t["columns"] if c["name"] == "CUST_NM")
    assert cust_nm["type_text"] == "VARCHAR2(20)"
    assert "ORD010.fetch_orders" in cust_nm["used_by"]


def test_impact_endpoint():
    r = client.post("/api/impact", json={"table": "tb_cust", "column": "cust_nm", "change": "length", "new_length": 50})
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["error"] >= 2
    bad = client.post("/api/impact", json={"table": "TB_CUST", "change": "length"})
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_SCENARIO"


def test_impact_explain_fallback(fake_models):
    fake_models.responses = [ConnectionError("Failed to connect to Ollama")]
    r = client.post("/api/impact/explain", json={"table": "TB_CUST", "column": "CUST_NM", "change": "nullable", "nullable": True})
    assert r.status_code == 200
    assert r.json()["decided_by"] == "fallback" and "ORA-01405" in r.json()["report"]


def test_db_endpoints_without_config():
    r = client.post("/api/db/query", json={"sql": "SELECT 1 FROM DUAL"})
    assert r.status_code == 503 and r.json()["code"] == "DB_NOT_CONFIGURED"
    r = client.post("/api/db/query", json={"sql": "DELETE FROM T"})
    assert r.status_code == 400 and r.json()["code"] == "NOT_READONLY"


def test_chat_impact_and_history(fake_models):
    fake_models.responses = ["## 요약\n보고서"]
    r = client.post("/api/chat", data={"query": "TB_ORDER.QTY 컬럼을 삭제하면?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["intent"] == "impact" and body["impact"]["summary"]["error"] == 1
    r = client.post("/api/chat", data={"query": "TB_ORDER", "history": "{bad"})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_HISTORY"


def test_chat_no_entity(fake_models):
    r = client.post("/api/chat", data={"query": "안녕"})
    assert r.json()["status"] == "INSUFFICIENT_EVIDENCE" and fake_models.calls == []


def test_error_does_not_leak(monkeypatch):
    class Boom:
        def invoke(self, _):
            raise RuntimeError("secret internal detail")

    monkeypatch.setattr(app_module, "build_agent_graph", lambda: Boom())
    r = client.post("/api/chat", data={"query": "TB_ORDER"})
    assert r.status_code == 500 and "secret" not in r.text


def test_ui_served():
    r = client.get("/ui/")
    assert r.status_code == 200 and "Pro*C" in r.text
    assert json.loads(client.get("/api/batches").content)["scripts"]


def test_settings_api_and_app_control(tmp_path, monkeypatch):
    import settings_store
    monkeypatch.setattr(settings_store.config, "DATA_DIR", tmp_path)
    r = client.put("/api/settings", json={"values": {"ORACLE_USER": "app"}})
    assert r.status_code == 200 and r.json()["changed"] == ["ORACLE_USER"]
    assert client.get("/api/settings").json()["values"]["ORACLE_USER"] == "app"
    r = client.post("/api/app/restart")
    assert r.status_code == 409 and r.json()["code"] == "NOT_DESKTOP"     # 개발 모드
