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
