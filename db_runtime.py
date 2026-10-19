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


def refresh_snapshot(referenced_tables: Optional[List[str]] = None) -> Dict:
    """DB 에서 메타데이터를 읽어 스냅샷을 새로 저장한다.

    대상 스키마: ORACLE_SCHEMAS(없으면 접속 계정) + 소스가 쓰는 테이블의 시노님이 가리키는 스키마.
    """
    user = (test_connection().get("user") or config.ORACLE_USER).upper()
    owners = list(config.ORACLE_SCHEMAS) or [user]
    synonyms: Dict[str, Dict] = {}
    refs = sorted({t.upper() for t in (referenced_tables or [])})
    for chunk in _in_chunks(refs):
        ph, binds = _bind_list("n", chunk)
        r = run_readonly(
            f"SELECT SYNONYM_NAME, TABLE_OWNER, TABLE_NAME, OWNER FROM ALL_SYNONYMS "
            f"WHERE OWNER IN (:usr, 'PUBLIC') AND SYNONYM_NAME IN ({ph}) AND DB_LINK IS NULL",
            dict(binds, usr=user), max_rows=5000, validate=False)
        for syn, towner, tname, owner in r["rows"]:
            # 내 스키마 시노님이 PUBLIC 보다 우선
            if syn not in synonyms or owner != "PUBLIC":
                synonyms[syn] = {"owner": towner, "table": tname}
    for s in synonyms.values():
        if s["owner"] not in owners:
            owners.append(s["owner"])

    tables: Dict[str, Dict] = {}
    for owner in owners:
        r = run_readonly(
            "SELECT t.TABLE_NAME, t.NUM_ROWS, c.COMMENTS FROM ALL_TABLES t "
            "LEFT JOIN ALL_TAB_COMMENTS c ON c.OWNER = t.OWNER AND c.TABLE_NAME = t.TABLE_NAME "
            "WHERE t.OWNER = :o "
            "UNION ALL SELECT v.VIEW_NAME, NULL, c.COMMENTS FROM ALL_VIEWS v "
            "LEFT JOIN ALL_TAB_COMMENTS c ON c.OWNER = v.OWNER AND c.TABLE_NAME = v.VIEW_NAME WHERE v.OWNER = :o",
            {"o": owner}, max_rows=5000, validate=False)
        for name, num_rows, comment in r["rows"]:
            key = name if name not in tables else f"{owner}.{name}"
            tables[key] = {"owner": owner, "name": name, "comment": comment, "num_rows": num_rows,
                           "columns": [], "pk": [], "unique": [], "fks": []}
        _fetch_columns(owner, tables)
        _fetch_constraints(owner, tables)
    snap = {"taken_at": _dt.datetime.now().isoformat(timespec="seconds"), "source": config.ORACLE_DSN,
            "owners": owners, "tables": tables, "synonyms": synonyms}
    save_snapshot(snap)
    return {"tables": len(tables), "owners": owners, "synonyms": len(synonyms), "taken_at": snap["taken_at"]}


def _table_key(tables: Dict, owner: str, name: str) -> Optional[str]:
    if name in tables and tables[name]["owner"] == owner:
        return name
    k = f"{owner}.{name}"
    return k if k in tables else None


def _fetch_columns(owner: str, tables: Dict):
    sql = ("SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.DATA_LENGTH, c.CHAR_LENGTH, c.CHAR_USED, "
           "c.DATA_PRECISION, c.DATA_SCALE, c.NULLABLE, c.COLUMN_ID, m.COMMENTS "
           "FROM ALL_TAB_COLUMNS c LEFT JOIN ALL_COL_COMMENTS m "
           "ON m.OWNER = c.OWNER AND m.TABLE_NAME = c.TABLE_NAME AND m.COLUMN_NAME = c.COLUMN_NAME "
           "WHERE c.OWNER = :o ORDER BY c.TABLE_NAME, c.COLUMN_ID")
    r = _run_all(sql, {"o": owner})
    for tname, col, dtype, dlen, clen, cused, prec, scale, nullable, cid, comment in r:
        key = _table_key(tables, owner, tname)
        if not key:
            continue
        length = clen if cused == "C" and clen else dlen
        tables[key]["columns"].append({
            "name": col, "type": re.sub(r"\(.*\)", "", dtype or ""), "length": length, "char_used": cused,
            "byte_length": dlen, "precision": prec, "scale": scale, "nullable": nullable == "Y", "id": cid,
            "comment": comment,
        })


def _fetch_constraints(owner: str, tables: Dict):
    sql = ("SELECT c.TABLE_NAME, c.CONSTRAINT_NAME, c.CONSTRAINT_TYPE, cc.COLUMN_NAME, cc.POSITION, "
           "r.TABLE_NAME AS R_TABLE, rc.COLUMN_NAME AS R_COLUMN "
           "FROM ALL_CONSTRAINTS c JOIN ALL_CONS_COLUMNS cc ON cc.OWNER = c.OWNER AND cc.CONSTRAINT_NAME = c.CONSTRAINT_NAME "
           "LEFT JOIN ALL_CONSTRAINTS r ON r.OWNER = c.R_OWNER AND r.CONSTRAINT_NAME = c.R_CONSTRAINT_NAME "
           "LEFT JOIN ALL_CONS_COLUMNS rc ON rc.OWNER = r.OWNER AND rc.CONSTRAINT_NAME = r.CONSTRAINT_NAME AND rc.POSITION = cc.POSITION "
           "WHERE c.OWNER = :o AND c.CONSTRAINT_TYPE IN ('P','U','R') ORDER BY c.TABLE_NAME, c.CONSTRAINT_NAME, cc.POSITION")
    groups: Dict = {}
    for tname, cname, ctype, col, pos, rtable, rcol in _run_all(sql, {"o": owner}):
        key = _table_key(tables, owner, tname)
        if key:
            g = groups.setdefault((key, cname), {"type": ctype, "cols": [], "rtable": rtable, "rcols": []})
            g["cols"].append(col)
            if rcol:
                g["rcols"].append(rcol)
    for (key, _), g in groups.items():
        t = tables[key]
        if g["type"] == "P":
            t["pk"] = g["cols"]
        elif g["type"] == "U":
            t["unique"].append(g["cols"])
        else:
            t["fks"].append({"columns": g["cols"], "ref_table": g["rtable"], "ref_columns": g["rcols"]})


def _run_all(sql: str, params: Dict) -> List[List]:
    """메타데이터 조회용: 행 수 제한 없이 전부 읽는다."""
    pool = _get_pool()
    try:
        with pool.acquire() as conn:
            conn.call_timeout = max(config.DB_CALL_TIMEOUT_MS, 120000)
            cur = conn.cursor()
            cur.arraysize = 2000
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
