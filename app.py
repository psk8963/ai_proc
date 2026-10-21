import json
import logging
import threading
import uuid
from typing import List, Optional

from fastapi import FastAPI, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import config
import db_runtime
from agent_runtime import bound_history, build_agent_graph, explain_impact, is_llm_unavailable
from config import BASE_DIR, DATA_DIR, MAX_UPLOAD_BYTES
from version import __version__
from db_runtime import DbError, SchemaMeta, column_type_text
from impact_engine import CHANGE_TYPES, DATA_OPS, ScenarioError, analyze_impact_live
import settings_store
from schemas import ChatResponse, ErrorEnvelope, Evidence, FolderImportRequest, ImpactRequest, SettingsRequest, SqlRequest
from source_repository import (
    AssetError,
    clear_assets,
    delete_asset,
    get_index,
    import_folder,
    read_batch_text,
    read_source_text,
    save_assets,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("app")

app = FastAPI(title="Pro*C Impact Agent - Local PoC", version=__version__)
app.mount("/ui", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")


class RequestError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def error_response(status: int, code: str, message: str, trace_id: str) -> JSONResponse:
    # 내부 예외 메시지(str(e))는 서버 로그에만 남기고 클라이언트에는 보내지 않는다.
    return JSONResponse(status_code=status, content=ErrorEnvelope(code=code, message=message, trace_id=trace_id).model_dump())


def _new_trace() -> str:
    return str(uuid.uuid4())


def _guard(trace_id: str, fn):
    """공통 예외 처리."""
    try:
        return fn()
    except (RequestError, AssetError, DbError) as e:
        return error_response(e.status, e.code, e.message, trace_id)
    except ScenarioError as e:
        return error_response(400, "INVALID_SCENARIO", e.message, trace_id)
    except Exception:
        logger.exception("trace_id=%s request failed", trace_id)
        return error_response(500, "INTERNAL_ERROR", "처리 중 오류가 발생했습니다.", trace_id)


def _read_uploads(files: List[UploadFile]):
    out, total = [], 0
    for f in files:
        if not f or not f.filename:
            continue
        data = bytearray()
        while chunk := f.file.read(1024 * 1024):
            data += chunk
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise RequestError("FILE_TOO_LARGE", f"파일이 너무 큽니다. (최대 {MAX_UPLOAD_BYTES // 1024 // 1024}MB)", 413)
        out.append((f.filename, bytes(data)))
    if not out:
        raise RequestError("EMPTY_FILE", "파일을 선택해 주세요.")
    return out


@app.get("/")
def root():
    return RedirectResponse("/ui/")


# ---------- 상태 ----------

@app.get("/api/status")
def status():
    idx = get_index()
    snap = db_runtime.load_snapshot()
    return {
        "version": __version__,
        "data_dir": str(DATA_DIR),
        "desktop": bool(getattr(app.state, "desktop", None)),
        "assets": idx.stats(),
        "db": {"configured": config.db_configured(), "dsn": config.ORACLE_DSN, "user": config.ORACLE_USER,
               "snapshot_taken_at": snap.get("taken_at"), "snapshot_tables": len(snap.get("tables") or {}),
               "snapshot_source": snap.get("source")},
        "llm": {"model": config.LLM_MODEL, "host": config.OLLAMA_HOST},
        "change_types": CHANGE_TYPES,
        "data_ops": DATA_OPS,
    }


# ---------- 설정 · 앱 제어 ----------

@app.get("/api/settings")
def get_settings():
    return settings_store.read_settings()


@app.put("/api/settings")
def put_settings(req: SettingsRequest):
    trace_id = _new_trace()
    try:
        r = settings_store.write_settings(req.values)
    except ValueError as e:
        return error_response(400, "INVALID_SETTING", str(e), trace_id)
    logger.info("trace_id=%s settings changed=%s", trace_id, r["changed"])
    return dict(r, trace_id=trace_id)


def _desktop_action(name: str):
    trace_id = _new_trace()
    ctl = getattr(app.state, "desktop", None)
    if not ctl:
        return error_response(409, "NOT_DESKTOP", "앱(설치판)으로 실행할 때만 쓸 수 있습니다. 개발 모드에서는 터미널에서 서버를 다시 시작하세요.", trace_id)
    threading.Timer(0.5, ctl[name]).start()
    return {"ok": True, "action": name, "trace_id": trace_id}


@app.post("/api/app/restart")
def app_restart():
    return _desktop_action("restart")


@app.post("/api/app/quit")
def app_quit():
    return _desktop_action("quit")


# ---------- 자산 업로드 ----------

@app.post("/api/upload/source")
def upload_source(files: List[UploadFile] = File(...)):
    """[A04] Pro*C 소스(.pc/.c) · 헤더(.h) · zip 업로드"""
    trace_id = _new_trace()

    def run():
        r = save_assets(_read_uploads(files), "source")
        logger.info("trace_id=%s source saved=%d skipped=%d", trace_id, r["saved"], r["skipped_count"])
        return dict(r, stats=get_index().stats(), trace_id=trace_id)
    return _guard(trace_id, run)


@app.post("/api/upload/batch")
def upload_batch(files: List[UploadFile] = File(...)):
    """[A04] 배치 스크립트(.sh/.ksh/.bat ...) · zip 업로드"""
    trace_id = _new_trace()

    def run():
        r = save_assets(_read_uploads(files), "batch")
        return dict(r, stats=get_index().stats(), trace_id=trace_id)
    return _guard(trace_id, run)


@app.post("/api/upload/folder")
def upload_folder(req: FolderImportRequest):
    """이 PC 의 폴더에서 바로 가져오기 (수천 개 파일을 업로드하지 않아도 되게)"""
    trace_id = _new_trace()

    def run():
        if req.kind not in ("source", "batch"):
            raise RequestError("INVALID_KIND", "kind 는 source 또는 batch 입니다.")
        r = import_folder(req.path.strip().strip('"'), req.kind)
        return dict(r, stats=get_index().stats(), trace_id=trace_id)
    return _guard(trace_id, run)


@app.delete("/api/assets/{kind}")
def clear_kind(kind: str):
    trace_id = _new_trace()
    if kind not in ("source", "batch"):
        return error_response(400, "INVALID_KIND", "kind 는 source 또는 batch 입니다.", trace_id)
    return {"deleted": clear_assets(kind), "trace_id": trace_id}


@app.delete("/api/assets/{kind}/{name}")
def remove_asset(kind: str, name: str):
    trace_id = _new_trace()
    if kind not in ("source", "batch"):
        return error_response(400, "INVALID_KIND", "kind 는 source 또는 batch 입니다.", trace_id)
    if not delete_asset(kind, name):
        return error_response(404, "ASSET_NOT_FOUND", "파일을 찾을 수 없습니다.", trace_id)
    return {"deleted": name, "trace_id": trace_id}


# ---------- 소스 · 배치 탐색 ----------

@app.get("/api/programs")
def list_programs():
    idx = get_index()
    out = []
    for name, p in sorted(idx.programs.items()):
        tables = {}
        for st in idx.statements.values():
            if st["program"] == name:
                for t in st.get("tables") or []:
                    prev = tables.get(t["name"], "")
                    tables[t["name"]] = "".join(sorted(set(prev + t["access"].replace("?", ""))))
        runs = idx.program_runs.get(name) or []
        out.append({"program": name, "file_name": p["file_name"], "lines": p["line_count"], "has_main": p["has_main"],
                    "functions": len(p["functions"]),
                    "sql": sum(1 for s in p["statements"] if s.get("tables")),
                    "tables": [{"name": k, "access": v} for k, v in sorted(tables.items())],
                    "batch": [{"script": r["script"], "order": r["order"]} for r in runs]})
    headers = [{"file_name": n, "defines": len(h.get("defines") or {}), "typedefs": list((h.get("typedefs") or {}).keys())}
               for n, h in sorted(idx.headers.items())]
    return {"programs": out, "headers": headers, "warnings": idx.warnings[:50]}


@app.get("/api/programs/{program}")
def program_detail(program: str):
    trace_id = _new_trace()
    idx = get_index()
    name = idx.program_name(program)
    if not name:
        return error_response(404, "PROGRAM_NOT_FOUND", "프로그램을 찾을 수 없습니다.", trace_id)
    p = idx.programs[name]
    funcs = []
    for fn in p["functions"]:
        key = f"{name}.{fn['name']}"
        sts = []
        for sid in fn["statements"]:
            st = idx.statements.get(f"{name}:{sid.split(':')[-1]}")
            if st and st["kind"] not in ("whenever", "declare_section", "include"):
                sts.append(_stmt_view(st))
        funcs.append({"name": fn["name"], "line": fn["line"], "end_line": fn["end_line"], "calls": fn["calls"],
                      "called_by": [c.split(".", 1)[1] if c.startswith(name + ".") else c for c in idx.callers.get(key, [])],
                      "statements": sts})
    return {"program": name, "file_name": p["file_name"], "lines": p["line_count"], "includes": p["includes"],
            "functions": funcs, "batch": idx.program_runs.get(name) or []}


def _stmt_view(st):
    return {"line": st["line"], "end_line": st["end_line"], "kind": st["kind"], "cursor": st.get("cursor") or "",
            "tables": st.get("tables") or [], "sql": st.get("sql", "")[:1500], "dynamic": st.get("dynamic", False),
            "guessed_tables": st.get("guessed_tables") or [],
            "dynamic_sql": (st.get("dynamic_sql") or "")[:3000], "dynamic_partial": bool(st.get("dynamic_partial")),
            "bindings": [{"column": b.get("column"), "table": b.get("table"), "host": b.get("host"),
                          "indicator": b.get("indicator"), "direction": b.get("direction"),
                          "host_decl": b.get("hv")} for b in st.get("bindings") or []],
            "whenever": st.get("whenever") or {}, "checks": st.get("checks") or {},
            "into_mismatch": st.get("into_mismatch")}


@app.get("/api/source/{file_name}")
def source_text(file_name: str, start: int = Query(1, ge=1), end: int = Query(0, ge=0)):
    trace_id = _new_trace()
    text = read_source_text(file_name)
    if text is None:
        return error_response(404, "SOURCE_NOT_FOUND", "소스 파일을 찾을 수 없습니다.", trace_id)
    lines = text.splitlines()
    end = end or len(lines)
    end = min(end, len(lines), start + 3000)
    return {"file_name": file_name, "start": start, "end": end, "total": len(lines), "lines": lines[start - 1:end]}


@app.get("/api/batches")
def list_batches():
    idx = get_index()
    return {"scripts": [{"name": n, "root": n in idx.root_scripts, "lines": s["line_count"],
                         "steps": sum(1 for x in idx.batch_steps.get(n, []) if x.get("program"))}
                        for n, s in sorted(idx.scripts.items())]}


@app.get("/api/batches/{name}")
def batch_detail(name: str):
    trace_id = _new_trace()
    idx = get_index()
    if name not in idx.scripts:
        return error_response(404, "BATCH_NOT_FOUND", "배치 스크립트를 찾을 수 없습니다.", trace_id)
    steps = []
    for s in idx.batch_steps.get(name, []):
        item = dict(s)
        prog = s.get("program")
        if prog:
            rw = {}
            for st in idx.statements.values():
                if st["program"] == prog:
                    for t in st.get("tables") or []:
                        rw[t["name"]] = "".join(sorted(set(rw.get(t["name"], "") + t["access"].replace("?", ""))))
            item["reads"] = sorted(k for k, v in rw.items() if "R" in v)
            item["writes"] = sorted(k for k, v in rw.items() if "W" in v)
        steps.append(item)
    return {"name": name, "steps": steps, "text": (read_batch_text(name) or "")[:50000]}


@app.get("/api/tables")
def list_tables():
    idx = get_index()
    meta = SchemaMeta()
    names = set(idx.table_usage) | {t["name"] for t in meta.tables.values()}
    out = []
    for n in sorted(names):
        tm = meta.table(n)
        usage = [u for u in idx.table_usage.get(n, []) if u["kind"] != "cursor"]
        out.append({"name": n, "comment": (tm or {}).get("comment"), "owner": (tm or {}).get("owner"),
                    "num_rows": (tm or {}).get("num_rows"), "columns": len((tm or {}).get("columns") or []),
                    "in_meta": bool(tm), "programs": len({u["program"] for u in usage}),
                    "reads": sum(1 for u in usage if "R" in u["access"]),
                    "writes": sum(1 for u in usage if "W" in u["access"])})
    return {"tables": out, "snapshot_taken_at": meta.snap.get("taken_at")}


@app.get("/api/tables/{name}")
def table_detail(name: str):
    idx = get_index()
    meta = SchemaMeta()
    n = name.upper()
    tm = meta.table(n)
    col_usage = {}
    for st in idx.statements.values():
        if not any(t["name"] == n for t in st.get("tables") or []):
            continue
        if st["kind"] == "cursor":
            continue
        for c in st.get("columns") or []:
            if c.get("table") == n or (not c.get("table") and n in (c.get("candidates") or [])):
                col_usage.setdefault(c["column"], set()).add(f"{st['program']}.{st['function']}")
    columns = []
    for c in (tm or {}).get("columns") or []:
        columns.append(dict(c, type_text=column_type_text(c), used_by=sorted(col_usage.get(c["name"], set()))))
    extra = [{"name": k, "type_text": "?", "used_by": sorted(v), "not_in_meta": True}
             for k, v in col_usage.items() if not tm or not any(c["name"] == k for c in tm["columns"])]
    usage = [u for u in idx.table_usage.get(n, [])]
    return {"name": n, "meta": {k: tm.get(k) for k in ("owner", "comment", "num_rows", "pk", "unique", "fks")} if tm else None,
            "columns": columns + extra, "usage": usage}


# ---------- DB ----------

@app.get("/api/db/status")
def db_status():
    trace_id = _new_trace()
    return _guard(trace_id, lambda: dict(db_runtime.test_connection(), trace_id=trace_id))


@app.post("/api/db/refresh")
def db_refresh():
    """DB 메타데이터(컬럼·제약조건·코멘트)를 다시 읽어 스냅샷으로 저장"""
    trace_id = _new_trace()

    def run():
        r = db_runtime.refresh_snapshot(list(get_index().table_usage.keys()))
        logger.info("trace_id=%s snapshot tables=%d", trace_id, r["tables"])
        return dict(r, trace_id=trace_id)
    return _guard(trace_id, run)


@app.get("/api/db/owners")
def db_owners():
    trace_id = _new_trace()
    return _guard(trace_id, lambda: dict(db_runtime.list_owners(), trace_id=trace_id))


@app.get("/api/db/objects")
def db_objects(owner: str = Query(""), refresh: bool = Query(False)):
    """Toad 처럼 스키마의 오브젝트 목록(이름·종류·상태)만 가볍게 읽는다."""
    trace_id = _new_trace()
    return _guard(trace_id, lambda: dict(db_runtime.list_objects(owner or None, refresh), trace_id=trace_id))


@app.get("/api/db/object")
def db_object(owner: str = Query(...), name: str = Query(...), type: str = Query(...)):
    """오브젝트 하나의 상세: 테이블이면 컬럼·제약·인덱스·참조 오브젝트, 프로시저류면 소스."""
    trace_id = _new_trace()

    def run():
        d = db_runtime.object_detail(owner, name, type)
        if d["type"] in ("TABLE", "VIEW", "MATERIALIZED VIEW", "SYNONYM"):
            idx = get_index()
            d["proc_usage"] = [u for u in idx.table_usage.get(d["name"], []) if u["kind"] != "cursor"]
            col_usage = {}
            for st in idx.statements.values():
                if st["kind"] == "cursor" or not any(t["name"] == d["name"] for t in st.get("tables") or []):
                    continue
                for c in st.get("columns") or []:
                    if c.get("table") == d["name"] or (not c.get("table") and d["name"] in (c.get("candidates") or [])):
                        col_usage.setdefault(c["column"], set()).add(f"{st['program']}.{st['function']}")
            d["column_usage"] = {k: sorted(v) for k, v in col_usage.items()}
        return dict(d, trace_id=trace_id)
    return _guard(trace_id, run)


@app.get("/api/db/tables/{name}/rows")
def db_rows(name: str, limit: int = Query(50, ge=1, le=1000), where: str = Query("")):
    trace_id = _new_trace()
    return _guard(trace_id, lambda: dict(db_runtime.preview_rows(name, limit, where), trace_id=trace_id))


@app.post("/api/db/query")
def db_query(req: SqlRequest):
    trace_id = _new_trace()

    def run():
        r = db_runtime.run_readonly(req.sql, max_rows=req.max_rows)
        logger.info("trace_id=%s query rows=%d", trace_id, r["row_count"])
        return dict(r, trace_id=trace_id)
    return _guard(trace_id, run)


# ---------- 영향도 ----------

def _run_impact(req: ImpactRequest) -> dict:
    scenario = req.model_dump(exclude={"run_db_checks"}, exclude_none=True)
    result = analyze_impact_live(get_index(), scenario)
    if req.run_db_checks and config.db_configured():
        for chk in result["db_checks"]:
            try:
                chk["result"] = db_runtime.run_readonly(chk["sql"], max_rows=50)
            except DbError as e:
                chk["error"] = e.message
    return result


@app.post("/api/impact")
def impact(req: ImpactRequest):
    """[A05] 규칙 기반 영향도 분석 (LLM 미사용)"""
    trace_id = _new_trace()

    def run():
        result = _run_impact(req)
        logger.info("trace_id=%s impact %s findings=%d", trace_id, result["description"], len(result["findings"]))
        return dict(result, trace_id=trace_id)
    return _guard(trace_id, run)


@app.post("/api/impact/explain")
def impact_explain(req: ImpactRequest):
    """영향도 분석 + LLM 설명 보고서"""
    trace_id = _new_trace()

    def run():
        result = _run_impact(req)
        out = explain_impact(result)
        notes = list(result.get("notes") or []) + ([out["note"]] if out.get("note") else [])
        return dict(result, report=out["report"], decided_by=out["decided_by"], notes=notes, trace_id=trace_id)
    return _guard(trace_id, run)


# ---------- Chat ----------

def parse_history(raw: Optional[str]) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RequestError("INVALID_HISTORY", "대화 기록 형식이 올바르지 않습니다.")
    return bound_history(data if isinstance(data, list) else [])


@app.post("/api/chat", response_model=ChatResponse)
def chat_endpoint(query: str = Form(""), history: Optional[str] = Form(None)):
    """[M05] 자연어 질문 → 영향도 분석 / 연관 관계 조회.

    async가 아닌 def로 둬서 FastAPI가 스레드풀에서 실행한다.
    (동기 LLM 호출이 이벤트 루프를 막아 다른 요청까지 멈추는 문제 방지)
    """
    trace_id = _new_trace()
    try:
        turns = parse_history(history)
        query = query.strip()
        if not query:
            raise RequestError("EMPTY_QUERY", "질문을 입력해 주세요.")
        if len(query) > 2000:
            raise RequestError("QUERY_TOO_LONG", "질문은 2,000자 이내로 입력해 주세요.")
    except RequestError as e:
        return error_response(e.status, e.code, e.message, trace_id)

    logger.info("trace_id=%s chat start query_chars=%d history_turns=%d", trace_id, len(query), len(turns))
    try:
        result = build_agent_graph().invoke({
            "trace_id": trace_id, "query": query, "history": turns, "entities": {}, "scenario": None,
            "impact": None, "facts": "", "evidence": [], "result_text": "", "decided_by": "", "status": "",
            "notes": [],
        })
    except ScenarioError as e:
        return error_response(400, "INVALID_SCENARIO", e.message, trace_id)
    except Exception as e:
        logger.exception("trace_id=%s agent failed", trace_id)
        if is_llm_unavailable(e):
            return error_response(503, "LLM_UNAVAILABLE", "LLM(Ollama)에 연결할 수 없습니다. Ollama 실행 상태를 확인해 주세요.", trace_id)
        return error_response(500, "AGENT_RUNTIME_ERROR", "답변 생성 오류", trace_id)

    evidence = [Evidence(source=e["source"], snippet=e.get("snippet", "")) for e in result.get("evidence") or []]
    impact_result = result.get("impact")
    logger.info("trace_id=%s chat done intent=%s status=%s decided_by=%s evidence=%d",
                trace_id, result.get("intent"), result.get("status"), result.get("decided_by"), len(evidence))
    return ChatResponse(
        report=result.get("result_text", ""),
        decided_by=result.get("decided_by", ""),
        status=result.get("status", ""),
        intent=result.get("intent", "none"),
        search_query=result.get("search_query", query),
        evidence=evidence,
        notes=result.get("notes", []),
        trace_id=trace_id,
        scenario=result.get("scenario"),
        impact=impact_result,
    )
