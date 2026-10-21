"""[D01] Oracle 연결 (python-oracledb). 읽기 전용으로만 쓴다.

- thin 모드가 기본(Instant Client 설치 불필요, DB 서버 12.1 이상).
  DB 서버가 11.2 라면 ORACLE_CLIENT_LIB_DIR 에 Instant Client(19 이상, 64bit) 폴더를 지정해 thick 모드로 붙는다.
  (python-oracledb thick 모드는 클라이언트 19 이상만 지원 → PC 에 깔린 11g 클라이언트/Toad 용 클라이언트는 쓸 수 없다)
- ORACLE_TNS_ADMIN 에 tnsnames.ora 폴더를 주면 ORACLE_DSN 에 TNS 별칭을 쓸 수 있다.
- 모든 조회는 SET TRANSACTION READ ONLY 안에서 실행하고 끝나면 ROLLBACK 한다.
  SQL 창에서는 SELECT / WITH 로 시작하는 한 문장만 받는다. (DB 계정도 조회 전용을 권장)
- 메타데이터(컬럼 타입·길이·NULL·PK/FK·코멘트)는 스냅샷 JSON 으로 저장해 두고,
  DB 에 붙을 수 없는 곳(집 PC)에서도 영향도 분석에 쓴다.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

_pool = None
_pool_lock = threading.Lock()
_thick_done = False


class DbError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _oracledb():
    try:
        import oracledb  # noqa: WPS433 (설치 안 된 PC 에서도 나머지 기능은 돌게 늦게 import)
    except ImportError:
        raise DbError("DRIVER_MISSING", "oracledb 패키지가 설치되어 있지 않습니다. (pip install -r requirements.txt)", 503)
    return oracledb


def _get_pool():
    global _pool, _thick_done
    if not config.db_configured():
        raise DbError("DB_NOT_CONFIGURED", "Oracle 접속 정보가 없습니다. .env 의 ORACLE_USER / ORACLE_PASSWORD / ORACLE_DSN 을 설정해 주세요.", 503)
    with _pool_lock:
        if _pool is None:
            oracledb = _oracledb()
            extra = {}
            if config.ORACLE_CLIENT_LIB_DIR and not _thick_done:
                kw = {"lib_dir": config.ORACLE_CLIENT_LIB_DIR}
                if config.ORACLE_TNS_ADMIN:
                    kw["config_dir"] = config.ORACLE_TNS_ADMIN
                try:
                    oracledb.init_oracle_client(**kw)
                except Exception as e:
                    raise DbError("CLIENT_INIT_FAILED",
                                  "Instant Client 를 불러오지 못했습니다. ORACLE_CLIENT_LIB_DIR 이 19 이상 64bit Instant Client 폴더인지 확인해 주세요. "
                                  f"({str(e).splitlines()[0][:150]})", 503)
                _thick_done = True
            elif not config.ORACLE_CLIENT_LIB_DIR and config.ORACLE_TNS_ADMIN:
                extra["config_dir"] = config.ORACLE_TNS_ADMIN  # thin 모드에서 tnsnames.ora 별칭 사용
            _pool = oracledb.create_pool(
                user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN,
                min=0, max=config.DB_POOL_MAX, increment=1, getmode=oracledb.POOL_GETMODE_TIMEDWAIT, wait_timeout=10000,
                **extra,
            )
        return _pool


def reset_pool():
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close(force=True)
            except Exception:
                pass
        _pool = None


def _wrap_db_error(e: Exception) -> DbError:
    msg = str(e).splitlines()[0] if str(e) else type(e).__name__
    code = re.search(r"(ORA-\d{5}|DPY-\d{4}|DPI-\d{4})", msg)
    logger.warning("db error: %s", msg)
    if code and code.group(1) in ("DPY-6005", "DPY-6000", "ORA-12541", "ORA-12514", "ORA-12170", "DPY-4011"):
        return DbError("DB_UNAVAILABLE", f"DB 에 연결할 수 없습니다. ({code.group(1)}) 접속 정보와 네트워크를 확인해 주세요.", 503)
    if code and code.group(1) == "DPY-3010":
        return DbError("DB_VERSION", "DB 서버가 11g 라 thin 모드로 붙을 수 없습니다(DPY-3010). Instant Client 19 이상(64bit)을 받아 ORACLE_CLIENT_LIB_DIR 에 지정해 주세요. (Toad 가 쓰는 11g 클라이언트는 사용할 수 없습니다)", 503)
    if code and code.group(1) in ("DPY-4024", "DPI-1067", "ORA-03136"):
        return DbError("DB_TIMEOUT", f"DB 조회 시간이 제한을 넘었습니다({code.group(1)}). .env 의 DB_CALL_TIMEOUT_MS / META_CALL_TIMEOUT_MS 를 늘려 주세요.", 504)
    if code and code.group(1) == "ORA-12154":
        return DbError("DB_TNS", "접속 이름(TNS 별칭)을 찾지 못했습니다(ORA-12154). ORACLE_DSN 에 IP:포트/서비스명 을 직접 넣어 보세요.", 503)
    if code and code.group(1) == "ORA-01017":
        return DbError("DB_LOGIN_FAILED", "DB 계정 또는 비밀번호가 올바르지 않습니다. (ORA-01017)", 503)
    # 조회 SQL 오류(ORA-00942 등)는 사용자가 고칠 수 있게 메시지를 그대로 보여준다(비밀번호는 포함되지 않음)
    return DbError("DB_QUERY_FAILED", msg[:300], 400)


def _to_json(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() and abs(v) < 2 ** 53 else str(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat(sep=" ") if isinstance(v, _dt.datetime) else v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v[:64]).hex() + ("…" if len(v) > 64 else "")
    if hasattr(v, "read"):
        try:
            data = v.read(1, 4000)
            return _to_json(data)
        except Exception:
            return "(LOB)"
    return str(v)


# ---------- 읽기 전용 실행 ----------

_SQL_COMMENT_RE = re.compile(r"/\*.*?\*/|--[^\n]*", re.S)
_STRING_RE = re.compile(r"'(?:[^']|'')*'")


def validate_readonly_sql(sql: str) -> str:
    text = (sql or "").strip()
    if not text:
        raise DbError("EMPTY_SQL", "SQL 을 입력해 주세요.")
    if len(text) > 20000:
        raise DbError("SQL_TOO_LONG", "SQL 이 너무 깁니다.")
    body = _SQL_COMMENT_RE.sub(" ", text)
    bare = _STRING_RE.sub("''", body).strip()
    bare = bare.rstrip().rstrip(";").rstrip()
    if ";" in bare:
        raise DbError("MULTI_STATEMENT", "한 번에 한 문장만 실행할 수 있습니다.")
    if not re.match(r"(?is)^\(?\s*(SELECT|WITH)\b", bare):
        raise DbError("NOT_READONLY", "조회(SELECT / WITH) 문만 실행할 수 있습니다.")
    if re.search(r"(?is)\bFOR\s+UPDATE\b", bare) or re.search(r"(?is)^\s*WITH\s+(FUNCTION|PROCEDURE)\b", bare):
        raise DbError("NOT_READONLY", "FOR UPDATE / WITH FUNCTION 은 사용할 수 없습니다.")
    # 원문에서 마지막 세미콜론만 뗀다(문자열 안 세미콜론은 유지)
    return re.sub(r";\s*$", "", body.strip())


def run_readonly(sql: str, params: Optional[Dict] = None, max_rows: Optional[int] = None, validate: bool = True) -> Dict:
    stmt = validate_readonly_sql(sql) if validate else sql
    limit = max(1, min(max_rows or config.DB_MAX_ROWS, 5000))
    pool = _get_pool()
    try:
        with pool.acquire() as conn:
            conn.call_timeout = config.DB_CALL_TIMEOUT_MS
            cur = conn.cursor()
            try:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.arraysize = min(limit, 500)
                cur.execute(stmt, params or {})
                cols = [d[0] for d in cur.description or []]
                rows = cur.fetchmany(limit + 1)
                truncated = len(rows) > limit
                rows = [[_to_json(v) for v in r] for r in rows[:limit]]
                types = [getattr(d[1], "name", str(d[1])) for d in cur.description or []]
            finally:
                try:
                    conn.rollback()
                except Exception:
                    pass
                cur.close()
        return {"columns": cols, "types": types, "rows": rows, "row_count": len(rows), "truncated": truncated}
    except DbError:
        raise
    except Exception as e:
        raise _wrap_db_error(e)


def test_connection() -> Dict:
    r = run_readonly(
        "SELECT USER AS DB_USER, SYS_CONTEXT('USERENV','DB_NAME') AS DB_NAME, "
        "(SELECT VERSION FROM PRODUCT_COMPONENT_VERSION WHERE PRODUCT LIKE 'Oracle%' AND ROWNUM = 1) AS VERSION FROM DUAL",
        validate=False)
    row = dict(zip(r["columns"], r["rows"][0])) if r["rows"] else {}
    return {"connected": True, "user": row.get("DB_USER"), "db_name": row.get("DB_NAME"), "version": row.get("VERSION")}


# ---------- 메타데이터 스냅샷 ----------

_snapshot_cache: Dict[str, Any] = {"mtime": None, "data": None}


def load_snapshot() -> Dict:
    path = config.SCHEMA_SNAPSHOT_PATH
    if not os.path.exists(path):
        return {"tables": {}, "synonyms": {}, "taken_at": None}
    mtime = os.path.getmtime(path)
    if _snapshot_cache["mtime"] != mtime:
        with open(path, encoding="utf-8") as f:
            _snapshot_cache["data"] = json.load(f)
        _snapshot_cache["mtime"] = mtime
    return _snapshot_cache["data"]


def save_snapshot(snap: Dict):
    path = config.SCHEMA_SNAPSHOT_PATH
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    os.replace(tmp, path)
    _snapshot_cache["mtime"] = None


def _in_chunks(names: List[str], size: int = 900):
    for i in range(0, len(names), size):
        yield names[i:i + size]


def _bind_list(prefix: str, names: List[str]):
    keys = [f"{prefix}{i}" for i in range(len(names))]
    return ", ".join(":" + k for k in keys), dict(zip(keys, names))


_user_cache: Dict[str, str] = {}


def current_user() -> str:
    key = f"{config.ORACLE_USER}@{config.ORACLE_DSN}"
    if key not in _user_cache:
        _user_cache[key] = (test_connection().get("user") or config.ORACLE_USER).upper()
    return _user_cache[key]


def default_owners() -> List[str]:
    return list(config.ORACLE_SCHEMAS) or [current_user()]


def _resolve_synonyms(names: List[str]) -> Dict[str, Dict]:
    """내 스키마/PUBLIC 시노님 → 실제 테이블. 내 스키마 시노님이 PUBLIC 보다 우선."""
    user = current_user()
    synonyms: Dict[str, Dict] = {}
    for clause, binds in _name_filter("SYNONYM_NAME", names, required=True):
        for syn, towner, tname, owner in _meta_query(
                "SELECT SYNONYM_NAME, TABLE_OWNER, TABLE_NAME, OWNER FROM ALL_SYNONYMS "
                f"WHERE OWNER IN (:usr, 'PUBLIC') AND DB_LINK IS NULL{clause}", dict(binds, usr=user)):
            if syn not in synonyms or owner != "PUBLIC":
                synonyms[syn] = {"owner": towner, "table": tname}
    return synonyms


def _collect_tables(owners: List[str], scope_names: Optional[List[str]]) -> Dict[str, Dict]:
    """owners 의 테이블/뷰 메타데이터(컬럼·코멘트·PK/UK/FK). scope_names 가 있으면 그 이름만."""
    tables: Dict[str, Dict] = {}
    for owner in owners:
        logger.info("metadata owner=%s names=%s", owner, len(scope_names) if scope_names else "all")
        found = []
        for clause, binds in _name_filter("TABLE_NAME", scope_names):
            found += [(n, r, "TABLE") for n, r in _meta_query(
                f"SELECT TABLE_NAME, NUM_ROWS FROM ALL_TABLES WHERE OWNER = :o{clause}", dict(binds, o=owner))]
        for clause, binds in _name_filter("VIEW_NAME", scope_names):
            found += [(n, None, "VIEW") for (n,) in _meta_query(
                f"SELECT VIEW_NAME FROM ALL_VIEWS WHERE OWNER = :o{clause}", dict(binds, o=owner))]
        for name, num_rows, kind in found:
            key = name if name not in tables else f"{owner}.{name}"
            tables[key] = {"owner": owner, "name": name, "kind": kind, "comment": None, "num_rows": num_rows,
                           "columns": [], "pk": [], "unique": [], "fks": []}
        if not found:
            continue
        for clause, binds in _name_filter("TABLE_NAME", scope_names):
            for tname, comment in _meta_query(
                    f"SELECT TABLE_NAME, COMMENTS FROM ALL_TAB_COMMENTS WHERE OWNER = :o AND COMMENTS IS NOT NULL{clause}",
                    dict(binds, o=owner)):
                key = _table_key(tables, owner, tname)
                if key:
                    tables[key]["comment"] = comment
        _fetch_columns(owner, tables, scope_names)
        _fetch_constraints(owner, tables, scope_names)
    return tables


def refresh_snapshot(referenced_tables: Optional[List[str]] = None) -> Dict:
    """DB 에서 메타데이터를 읽어 스냅샷을 새로 저장한다(집 PC 로 가져가 오프라인 분석할 때).

    대상 스키마: ORACLE_SCHEMAS(없으면 접속 계정) + 소스가 쓰는 테이블의 시노님이 가리키는 스키마.
    ORACLE_META_SCOPE=referenced(기본)이면 등록된 소스가 쓰는 테이블만 읽는다.
    11g 의 ALL_* 뷰는 조인하면 매우 느려서, 뷰마다 따로 조회한 뒤 파이썬에서 합친다.
    """
    owners = default_owners()
    refs = sorted({t.upper() for t in (referenced_tables or [])})
    synonyms = _resolve_synonyms(refs) if refs else {}
    for sy in synonyms.values():
        if sy["owner"] not in owners:
            owners.append(sy["owner"])
    scope_names = None
    if config.ORACLE_META_SCOPE != "all" and refs:
        scope_names = sorted(set(refs) | {sy["table"] for sy in synonyms.values()})
    tables = _collect_tables(owners, scope_names)
    snap = {"taken_at": _dt.datetime.now().isoformat(timespec="seconds"), "source": config.ORACLE_DSN,
            "owners": owners, "scope": "referenced" if scope_names else "all", "tables": tables, "synonyms": synonyms}
    save_snapshot(snap)
    return {"tables": len(tables), "owners": owners, "synonyms": len(synonyms), "taken_at": snap["taken_at"],
            "scope": snap["scope"]}


_ensure_lock = threading.Lock()
NOT_FOUND_RETRY_SEC = 3600


def ensure_tables(names: List[str]) -> Dict:
    """분석에 필요한 테이블 메타데이터만 그때그때 DB 에서 읽어 스냅샷(캐시)에 더한다.

    이미 캐시에 있으면 DB 에 가지 않는다. DB 에 없는 이름은 1시간 동안 다시 찾지 않는다.
    반환: {"fetched": [...], "not_found": [...]}
    """
    want = sorted({n.upper() for n in names if n})
    if not want or not config.db_configured():
        return {"fetched": [], "not_found": []}
    with _ensure_lock:
        snap = load_snapshot()
        snap = {**snap, "tables": dict(snap.get("tables") or {}), "synonyms": dict(snap.get("synonyms") or {}),
                "not_found": dict(snap.get("not_found") or {})}
        meta = SchemaMeta(snap)
        now = _dt.datetime.now()
        missing = []
        for n in want:
            if meta.table(n):
                continue
            nf = snap["not_found"].get(n)
            if nf and (now - _dt.datetime.fromisoformat(nf)).total_seconds() < NOT_FOUND_RETRY_SEC:
                continue
            missing.append(n)
        if not missing:
            return {"fetched": [], "not_found": []}
        synonyms = _resolve_synonyms(missing)
        owners = default_owners()
        for sy in synonyms.values():
            if sy["owner"] not in owners:
                owners.append(sy["owner"])
        scope = sorted(set(missing) | {sy["table"] for sy in synonyms.values()})
        found = _collect_tables(owners, scope)
        for key, t in found.items():
            k = t["name"] if t["name"] not in snap["tables"] or snap["tables"][t["name"]].get("owner") == t["owner"] \
                else f"{t['owner']}.{t['name']}"
            snap["tables"][k] = t
        snap["synonyms"].update(synonyms)
        meta = SchemaMeta(snap)
        fetched = [n for n in missing if meta.table(n)]
        not_found = [n for n in missing if not meta.table(n)]
        for n in not_found:
            snap["not_found"][n] = now.isoformat(timespec="seconds")
        snap.setdefault("taken_at", now.isoformat(timespec="seconds"))
        snap["source"] = snap.get("source") or config.ORACLE_DSN
        snap["updated_at"] = now.isoformat(timespec="seconds")
        save_snapshot(snap)
        logger.info("ensure_tables fetched=%d not_found=%d", len(fetched), len(not_found))
        return {"fetched": fetched, "not_found": not_found}


# ---------- 오브젝트 브라우저 (Toad 처럼) ----------

OBJECT_TYPES = ["TABLE", "VIEW", "MATERIALIZED VIEW", "PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY",
                "TRIGGER", "SEQUENCE", "SYNONYM", "TYPE", "TYPE BODY"]
CODE_TYPES = ("PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY", "TRIGGER", "TYPE", "TYPE BODY")
_objects_cache: Dict[str, Dict] = {}
OBJECTS_CACHE_SEC = 600


def list_owners() -> Dict:
    """선택할 수 있는 스키마: 설정한 스키마 + 접속 계정 + 오브젝트를 가진 스키마(조회 권한 있는 것)."""
    base = default_owners()
    rows = _meta_query("SELECT DISTINCT OWNER FROM ALL_OBJECTS WHERE OBJECT_TYPE IN ('TABLE','PROCEDURE','PACKAGE')", {})
    others = sorted({r[0] for r in rows} - set(base))
    return {"default": base, "owners": base + others}


def list_objects(owner: Optional[str] = None, refresh: bool = False) -> Dict:
    """한 스키마의 오브젝트 목록(이름·종류·상태·최종 DDL). 상세는 object_detail 에서 따로 읽는다."""
    owner = (owner or default_owners()[0]).upper()
    cached = _objects_cache.get(owner)
    if cached and not refresh and (_dt.datetime.now() - cached["at"]).total_seconds() < OBJECTS_CACHE_SEC:
        return cached["data"]
    ph, binds = _bind_list("t", OBJECT_TYPES)
    rows = _meta_query(
        "SELECT OBJECT_NAME, OBJECT_TYPE, STATUS, LAST_DDL_TIME FROM ALL_OBJECTS "
        f"WHERE OWNER = :o AND OBJECT_TYPE IN ({ph}) AND GENERATED = 'N' AND OBJECT_NAME NOT LIKE 'BIN$%'",
        dict(binds, o=owner))
    objs = [{"name": n, "type": t, "status": st, "last_ddl": _to_json(d)} for n, t, st, d in rows]
    objs.sort(key=lambda o: (OBJECT_TYPES.index(o["type"]) if o["type"] in OBJECT_TYPES else 99, o["name"]))
    counts: Dict[str, int] = {}
    for o in objs:
        counts[o["type"]] = counts.get(o["type"], 0) + 1
    data = {"owner": owner, "objects": objs, "counts": counts,
            "loaded_at": _dt.datetime.now().isoformat(timespec="seconds")}
    _objects_cache[owner] = {"at": _dt.datetime.now(), "data": data}
    return data


def object_detail(owner: str, name: str, otype: str) -> Dict:
    owner, name, otype = owner.upper(), name.upper(), otype.upper()
    out: Dict[str, Any] = {"owner": owner, "name": name, "type": otype}
    if otype in ("TABLE", "VIEW", "MATERIALIZED VIEW"):
        t = _collect_tables([owner], [name]).get(name)
        if t:
            out["table"] = t
            _cache_table(t)
        out["indexes"] = _indexes(owner, name)
        if otype == "VIEW":
            rows = _meta_query("SELECT TEXT FROM ALL_VIEWS WHERE OWNER = :o AND VIEW_NAME = :n", {"o": owner, "n": name})
            out["source"] = str(rows[0][0]) if rows and rows[0][0] is not None else ""
        out["used_by"] = [{"owner": o, "name": n, "type": t} for o, n, t in _meta_query(
            "SELECT OWNER, NAME, TYPE FROM ALL_DEPENDENCIES WHERE REFERENCED_OWNER = :o AND REFERENCED_NAME = :n "
            "AND OWNER NOT IN ('SYS','SYSTEM','PUBLIC') ORDER BY TYPE, OWNER, NAME", {"o": owner, "n": name})]
    elif otype in CODE_TYPES:
        rows = _meta_query("SELECT LINE, TEXT FROM ALL_SOURCE WHERE OWNER = :o AND NAME = :n AND TYPE = :t ORDER BY LINE",
                           {"o": owner, "n": name, "t": otype})
        out["source"] = "".join(str(t or "") for _, t in rows[:30000])
        out["lines"] = len(rows)
        out["references"] = [{"owner": o, "name": n, "type": t} for o, n, t in _meta_query(
            "SELECT REFERENCED_OWNER, REFERENCED_NAME, REFERENCED_TYPE FROM ALL_DEPENDENCIES "
            "WHERE OWNER = :o AND NAME = :n AND TYPE = :t AND REFERENCED_OWNER NOT IN ('SYS','PUBLIC') "
            "ORDER BY REFERENCED_TYPE, REFERENCED_NAME", {"o": owner, "n": name, "t": otype})]
        if otype == "TRIGGER":
            rows = _meta_query("SELECT TABLE_OWNER, TABLE_NAME, TRIGGERING_EVENT, TRIGGER_TYPE, STATUS FROM ALL_TRIGGERS "
                               "WHERE OWNER = :o AND TRIGGER_NAME = :n", {"o": owner, "n": name})
            if rows:
                out["trigger"] = dict(zip(["table_owner", "table_name", "event", "timing", "status"], rows[0]))
    elif otype == "SEQUENCE":
        rows = _meta_query("SELECT MIN_VALUE, MAX_VALUE, INCREMENT_BY, CYCLE_FLAG, CACHE_SIZE, LAST_NUMBER "
                           "FROM ALL_SEQUENCES WHERE SEQUENCE_OWNER = :o AND SEQUENCE_NAME = :n", {"o": owner, "n": name})
        if rows:
            out["sequence"] = dict(zip(["min", "max", "increment", "cycle", "cache", "last_number"],
                                       [_to_json(v) for v in rows[0]]))
    elif otype == "SYNONYM":
        rows = _meta_query("SELECT TABLE_OWNER, TABLE_NAME, DB_LINK FROM ALL_SYNONYMS WHERE OWNER = :o AND SYNONYM_NAME = :n",
                           {"o": owner, "n": name})
        if rows:
            out["synonym"] = dict(zip(["table_owner", "table_name", "db_link"], rows[0]))
    return out


def _indexes(owner: str, name: str) -> List[Dict]:
    idx: Dict[str, Dict] = {}
    for iname, uniq in _meta_query("SELECT INDEX_NAME, UNIQUENESS FROM ALL_INDEXES WHERE TABLE_OWNER = :o AND TABLE_NAME = :n",
                                   {"o": owner, "n": name}):
        idx[iname] = {"name": iname, "unique": uniq == "UNIQUE", "columns": []}
    for iname, col, pos in _meta_query(
            "SELECT INDEX_NAME, COLUMN_NAME, COLUMN_POSITION FROM ALL_IND_COLUMNS WHERE TABLE_OWNER = :o AND TABLE_NAME = :n",
            {"o": owner, "n": name}):
        if iname in idx:
            idx[iname]["columns"].append((pos, col))
    for i in idx.values():
        i["columns"] = [c for _, c in sorted(i["columns"])]
    return sorted(idx.values(), key=lambda i: (not i["unique"], i["name"]))


def _cache_table(t: Dict):
    """상세 조회한 테이블은 스냅샷 캐시에도 넣어 영향도 분석이 바로 쓰게 한다."""
    with _ensure_lock:
        snap = load_snapshot()
        snap = {**snap, "tables": dict(snap.get("tables") or {})}
        cur = snap["tables"].get(t["name"])
        key = t["name"] if not cur or cur.get("owner") == t["owner"] else f"{t['owner']}.{t['name']}"
        snap["tables"][key] = t
        snap["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        snap.setdefault("source", config.ORACLE_DSN)
        save_snapshot(snap)


def db_dependents(table: str, column: Optional[str] = None, limit: int = 60) -> List[Dict]:
    """DB 안에서 이 테이블을 쓰는 프로시저·패키지·트리거·뷰. column 이 있으면 그 이름이 나오는 소스 줄도."""
    meta = SchemaMeta()
    t = meta.table(table)
    owner = (t or {}).get("owner") or default_owners()[0]
    tname = (t or {}).get("name") or table.upper()
    deps = _meta_query(
        "SELECT OWNER, NAME, TYPE FROM ALL_DEPENDENCIES WHERE REFERENCED_OWNER = :o AND REFERENCED_NAME = :n "
        "AND OWNER NOT IN ('SYS','SYSTEM','PUBLIC') ORDER BY TYPE, OWNER, NAME", {"o": owner, "n": tname})[:limit]
    out = [{"owner": o, "name": n, "type": ty, "lines": []} for o, n, ty in deps]
    if column and out:
        col = column.upper()
        word = re.compile(r"(?<![A-Z0-9_$#])" + re.escape(col) + r"(?![A-Z0-9_$#])")
        by_owner: Dict[str, List[Dict]] = {}
        for d in out:
            if d["type"] in CODE_TYPES:
                by_owner.setdefault(d["owner"], []).append(d)
        for o, items in by_owner.items():
            names = sorted({d["name"] for d in items})
            for clause, binds in _name_filter("NAME", names):
                for name, ty, line, text in _meta_query(
                        "SELECT NAME, TYPE, LINE, TEXT FROM ALL_SOURCE WHERE OWNER = :o AND INSTR(UPPER(TEXT), :c) > 0"
                        f"{clause} ORDER BY NAME, TYPE, LINE", dict(binds, o=o, c=col)):
                    if not word.search(str(text or "").upper()):
                        continue
                    for d in items:
                        if d["name"] == name and d["type"] == ty and len(d["lines"]) < 20:
                            d["lines"].append({"line": line, "text": str(text).rstrip()[:200]})
        for d in out:
            d["uses_column"] = bool(d["lines"]) if d["type"] in CODE_TYPES else None
    return out


def _name_filter(column: str, names: Optional[List[str]], required: bool = False):
    """names 가 있으면 ' AND column IN (...)' 조각을 900개씩 나눠 돌려준다. 없으면 조건 없이 한 번."""
    if not names:
        if required:
            return []
        return [("", {})]
    out = []
    for chunk in _in_chunks(names):
        ph, binds = _bind_list("n", chunk)
        out.append((f" AND {column} IN ({ph})", binds))
    return out


def _table_key(tables: Dict, owner: str, name: str) -> Optional[str]:
    if name in tables and tables[name]["owner"] == owner:
        return name
    k = f"{owner}.{name}"
    return k if k in tables else None


def _fetch_columns(owner: str, tables: Dict, names: Optional[List[str]] = None):
    for clause, binds in _name_filter("TABLE_NAME", names):
        rows = _meta_query(
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, DATA_LENGTH, CHAR_LENGTH, CHAR_USED, "
            "DATA_PRECISION, DATA_SCALE, NULLABLE, COLUMN_ID FROM ALL_TAB_COLUMNS "
            f"WHERE OWNER = :o{clause}", dict(binds, o=owner))
        for tname, col, dtype, dlen, clen, cused, prec, scale, nullable, cid in rows:
            key = _table_key(tables, owner, tname)
            if not key:
                continue
            length = clen if cused == "C" and clen else dlen
            tables[key]["columns"].append({
                "name": col, "type": re.sub(r"\(.*\)", "", dtype or ""), "length": length, "char_used": cused,
                "byte_length": dlen, "precision": prec, "scale": scale, "nullable": nullable == "Y", "id": cid,
                "comment": None,
            })
        for tname, col, comment in _meta_query(
                "SELECT TABLE_NAME, COLUMN_NAME, COMMENTS FROM ALL_COL_COMMENTS "
                f"WHERE OWNER = :o AND COMMENTS IS NOT NULL{clause}", dict(binds, o=owner)):
            key = _table_key(tables, owner, tname)
            if key:
                for c in tables[key]["columns"]:
                    if c["name"] == col:
                        c["comment"] = comment
                        break
    for t in tables.values():
        if t["owner"] == owner:
            t["columns"].sort(key=lambda c: c.get("id") or 0)


def _fetch_constraints(owner: str, tables: Dict, names: Optional[List[str]] = None):
    cons: Dict[tuple, Dict] = {}
    for clause, binds in _name_filter("TABLE_NAME", names):
        for tname, cname, ctype, rowner, rcname in _meta_query(
                "SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE, R_OWNER, R_CONSTRAINT_NAME FROM ALL_CONSTRAINTS "
                f"WHERE OWNER = :o AND CONSTRAINT_TYPE IN ('P','U','R'){clause}", dict(binds, o=owner)):
            cons[(owner, cname)] = {"table": tname, "type": ctype, "r": (rowner, rcname) if rcname else None, "cols": []}
        for tname, cname, col, pos in _meta_query(
                "SELECT TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME, POSITION FROM ALL_CONS_COLUMNS "
                f"WHERE OWNER = :o{clause}", dict(binds, o=owner)):
            c = cons.get((owner, cname))
            if c:
                c["cols"].append((pos or 0, col))
    for c in cons.values():
        c["cols"] = [col for _, col in sorted(c["cols"])]
    # FK 가 가리키는 PK/UK 이름 → 테이블·컬럼 (같은 스키마 안에서 찾을 수 있는 것만)
    for c in cons.values():
        key = _table_key(tables, owner, c["table"])
        if not key:
            continue
        t = tables[key]
        if c["type"] == "P":
            t["pk"] = c["cols"]
        elif c["type"] == "U":
            t["unique"].append(c["cols"])
        else:
            ref = cons.get(c["r"]) if c["r"] else None
            t["fks"].append({"columns": c["cols"], "ref_table": ref["table"] if ref else None,
                             "ref_columns": ref["cols"] if ref else []})


def _meta_query(sql: str, params: Dict) -> List[List]:
    """메타데이터 조회용: 행 수 제한 없이 전부 읽는다. 제한 시간은 META_CALL_TIMEOUT_MS."""
    pool = _get_pool()
    try:
        with pool.acquire() as conn:
            conn.call_timeout = config.META_CALL_TIMEOUT_MS
            cur = conn.cursor()
            cur.arraysize = 5000
            cur.prefetchrows = 5000
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sql, params)
            rows = [list(r) for r in cur.fetchall()]
            conn.rollback()
            return rows
    except Exception as e:
        raise _wrap_db_error(e)


# ---------- 메타데이터 조회 도우미 ----------

class SchemaMeta:
    """스냅샷 위의 조회 도우미. 이름은 대문자, 시노님은 실제 테이블로 바꿔서 찾는다."""

    def __init__(self, snap: Optional[Dict] = None):
        self.snap = snap if snap is not None else load_snapshot()
        self.tables = self.snap.get("tables") or {}
        self.synonyms = self.snap.get("synonyms") or {}
        for key, t in self.tables.items():
            t.setdefault("name", key.split(".")[-1])
            t.setdefault("owner", "")

    @property
    def available(self) -> bool:
        return bool(self.tables)

    def table(self, name: Optional[str]) -> Optional[Dict]:
        if not name:
            return None
        n = name.upper()
        if n in self.tables:
            return self.tables[n]
        syn = self.synonyms.get(n)
        if syn:
            return self.tables.get(syn["table"]) or self.tables.get(f"{syn['owner']}.{syn['table']}")
        return None

    def column(self, table: Optional[str], column: str) -> Optional[Dict]:
        t = self.table(table)
        if not t:
            return None
        c = column.upper()
        return next((x for x in t["columns"] if x["name"] == c), None)

    def has_column(self, table: str, column: str) -> Optional[bool]:
        """True/False, 테이블 정보가 없으면 None."""
        t = self.table(table)
        if not t:
            return None
        return any(x["name"] == column.upper() for x in t["columns"])

    def qualified(self, table: str) -> str:
        t = self.table(table)
        if not t:
            raise DbError("UNKNOWN_TABLE", f"메타데이터에 없는 테이블입니다: {table}", 404)
        return f'"{t["owner"]}"."{t["name"]}"'


def column_type_text(c: Optional[Dict]) -> str:
    if not c:
        return "?"
    t = c.get("type") or "?"
    if t in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "VARCHAR", "RAW"):
        return f"{t}({c.get('length')}{' CHAR' if c.get('char_used') == 'C' else ''})"
    if t == "NUMBER":
        if c.get("precision") is None:
            return "NUMBER"
        return f"NUMBER({c['precision']},{c.get('scale') or 0})" if c.get("scale") else f"NUMBER({c['precision']})"
    return t


def preview_rows(table: str, limit: int = 50, where: str = "") -> Dict:
    meta = SchemaMeta()
    q = meta.qualified(table)
    sql = f"SELECT * FROM {q}"
    if where.strip():
        sql += f" WHERE ({where})"
    sql += f" FETCH FIRST {int(max(1, min(limit, 1000)))} ROWS ONLY"
    try:
        return run_readonly(sql, max_rows=limit)
    except DbError as e:
        if "ORA-00933" in e.message:  # 11g: FETCH FIRST 미지원
            sql = f"SELECT * FROM {q}" + (f" WHERE ({where})" if where.strip() else "")
            return run_readonly(f"SELECT * FROM ({sql}) WHERE ROWNUM <= {int(limit)}", max_rows=limit)
        raise
