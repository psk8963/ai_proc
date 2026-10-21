"""메타데이터 갱신(refresh_snapshot)을 가짜 DB 연결로 확인한다. (Oracle 없이)"""

import db_runtime
from db_runtime import SchemaMeta


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.rows, self.description = conn, [], None
        self.arraysize = self.prefetchrows = 0

    def execute(self, sql, params=None):
        self.conn.sqls.append((sql, dict(params or {})))
        names = [v for k, v in (params or {}).items() if k.startswith("n")]
        if "SET TRANSACTION" in sql:
            self.rows = []
        elif "FROM DUAL" in sql:
            self.description = [("DB_USER", None), ("DB_NAME", None), ("VERSION", None)]
            self.rows = [("APP", "ORCL", "11.2.0.4.0")]
        elif "ALL_SYNONYMS" in sql:
            self.rows = [("TB_CUST", "COM", "TB_CUST", "PUBLIC")] if "TB_CUST" in names else []
        elif "FROM ALL_TABLES" in sql:
            data = {"APP": [("TB_ORDER", 100), ("TB_OTHER", 5)], "COM": [("TB_CUST", 50)]}[params["o"]]
            self.rows = [r for r in data if not names or r[0] in names]
        elif "FROM ALL_VIEWS" in sql:
            self.rows = []
        elif "FROM ALL_OBJECTS" in sql:
            self.rows = [("TB_ORDER", "TABLE", "VALID", None), ("PRC_CLOSE", "PROCEDURE", "INVALID", None)]
        elif "FROM ALL_DEPENDENCIES WHERE REFERENCED_OWNER" in sql:
            self.rows = [("APP", "PRC_CLOSE", "PROCEDURE"), ("APP", "TRG_ORDER", "TRIGGER")]
        elif "FROM ALL_DEPENDENCIES" in sql:
            self.rows = [("APP", "TB_ORDER", "TABLE")]
        elif "FROM ALL_SOURCE" in sql and "INSTR" in sql:
            self.rows = [("PRC_CLOSE", "PROCEDURE", 12, "  UPDATE TB_ORDER SET STATUS = '09'\n"),
                         ("PRC_CLOSE", "PROCEDURE", 13, "  -- STATUS_OLD 는 안 씀\n")]
        elif "FROM ALL_SOURCE" in sql:
            self.rows = [(1, "PROCEDURE PRC_CLOSE IS\n"), (2, "BEGIN NULL; END;\n")]
        elif "FROM ALL_INDEXES" in sql:
            self.rows = [("PK_ORDER", "UNIQUE")]
        elif "FROM ALL_IND_COLUMNS" in sql:
            self.rows = [("PK_ORDER", "ORD_NO", 1)]
        elif "ALL_TAB_COMMENTS" in sql:
            self.rows = [("TB_ORDER", "주문")] if params["o"] == "APP" else []
        elif "ALL_TAB_COLUMNS" in sql:
            self.rows = ([("TB_ORDER", "STATUS", "VARCHAR2", 2, 2, "B", None, None, "N", 2),
                          ("TB_ORDER", "ORD_NO", "VARCHAR2", 12, 12, "B", None, None, "N", 1)]
                         if params["o"] == "APP" else
                         [("TB_CUST", "CUST_NM", "VARCHAR2", 60, 20, "C", None, None, "Y", 1)])
        elif "ALL_COL_COMMENTS" in sql:
            self.rows = [("TB_ORDER", "STATUS", "상태")] if params["o"] == "APP" else []
        elif "FROM ALL_CONSTRAINTS" in sql:
            self.rows = [("TB_ORDER", "PK_ORDER", "P", None, None)] if params["o"] == "APP" else []
        elif "ALL_CONS_COLUMNS" in sql:
            self.rows = [("TB_ORDER", "PK_ORDER", "ORD_NO", 1)] if params["o"] == "APP" else []
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return list(self.rows)

    def fetchmany(self, n):
        return list(self.rows)[:n]

    def close(self):
        pass


class FakeConn:
    def __init__(self, sqls):
        self.sqls, self.call_timeout = sqls, 0

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakePool:
    def __init__(self):
        self.sqls = []

    def acquire(self):
        return FakeConn(self.sqls)


def test_refresh_snapshot_referenced_scope(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(db_runtime, "_get_pool", lambda: pool)
    monkeypatch.setattr(db_runtime.config, "ORACLE_SCHEMAS", [])
    monkeypatch.setattr(db_runtime.config, "ORACLE_META_SCOPE", "referenced")
    r = db_runtime.refresh_snapshot(["TB_ORDER", "TB_CUST"])
    assert r["owners"] == ["APP", "COM"] and r["scope"] == "referenced"
    meta = SchemaMeta()
    assert set(meta.tables) == {"TB_ORDER", "TB_CUST"}             # 소스가 쓰는 테이블만
    t = meta.table("TB_ORDER")
    assert [c["name"] for c in t["columns"]] == ["ORD_NO", "STATUS"]  # COLUMN_ID 순서
    assert t["comment"] == "주문" and t["pk"] == ["ORD_NO"]
    assert meta.column("TB_ORDER", "STATUS")["comment"] == "상태"
    assert meta.column("TB_CUST", "CUST_NM")["length"] == 20        # CHAR 단위 길이
    assert not any(" JOIN " in s for s, _ in pool.sqls)              # 11g 에서 느린 조인 없음


def test_timeout_error_message():
    err = db_runtime._wrap_db_error(Exception("DPY-4024: call timeout of 30000 ms exceeded"))
    assert err.code == "DB_TIMEOUT" and "META_CALL_TIMEOUT_MS" in err.message


def _fake(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(db_runtime, "_get_pool", lambda: pool)
    monkeypatch.setattr(db_runtime.config, "ORACLE_SCHEMAS", [])
    monkeypatch.setattr(db_runtime.config, "ORACLE_USER", "app")
    monkeypatch.setattr(db_runtime.config, "ORACLE_DSN", "fake:1521/x")
    db_runtime._user_cache.clear()
    db_runtime._objects_cache.clear()
    return pool


def test_object_browser(monkeypatch):
    _fake(monkeypatch)
    objs = db_runtime.list_objects()
    assert objs["owner"] == "APP" and objs["counts"] == {"TABLE": 1, "PROCEDURE": 1}
    d = db_runtime.object_detail("APP", "TB_ORDER", "TABLE")
    assert d["table"]["pk"] == ["ORD_NO"] and d["indexes"][0]["columns"] == ["ORD_NO"]
    assert {o["name"] for o in d["used_by"]} == {"PRC_CLOSE", "TRG_ORDER"}
    assert SchemaMeta().table("TB_ORDER")                           # 상세 조회한 테이블은 캐시에 저장
    p = db_runtime.object_detail("APP", "PRC_CLOSE", "PROCEDURE")
    assert p["source"].startswith("PROCEDURE PRC_CLOSE") and p["references"][0]["name"] == "TB_ORDER"


def test_live_impact_fetches_only_needed_tables(monkeypatch):
    import os
    from impact_engine import analyze_impact_live
    from source_repository import get_index
    pool = _fake(monkeypatch)
    os.remove(db_runtime.config.SCHEMA_SNAPSHOT_PATH)               # 캐시 없음에서 시작
    r = analyze_impact_live(get_index(), {"table": "TB_ORDER", "column": "STATUS", "change": "length", "new_length": 1})
    tables_sql = [p for s, p in pool.sqls if "FROM ALL_TABLES" in s]
    assert tables_sql and all(any(k.startswith("n") for k in p) for p in tables_sql)   # 이름을 지정해서만 조회
    assert SchemaMeta().table("TB_ORDER") and any("DB 에서 테이블" in n for n in r["notes"])
    prc = next(o for o in r["db_objects"] if o["name"] == "PRC_CLOSE")
    assert prc["uses_column"] and [l["line"] for l in prc["lines"]] == [12]   # STATUS_OLD 는 단어가 달라 제외
    n_before = len(pool.sqls)
    analyze_impact_live(get_index(), {"table": "TB_ORDER", "column": "STATUS", "change": "nullable", "nullable": True})
    assert not any("FROM ALL_TABLES" in s for s, _ in pool.sqls[n_before:])  # 두 번째는 캐시 사용
