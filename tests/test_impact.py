import pytest

from conftest import load_samples
from db_runtime import validate_readonly_sql, DbError
from impact_engine import ScenarioError, analyze_impact
from source_repository import get_index


def _run(**scenario):
    return analyze_impact(get_index(), scenario)


def _codes(r):
    return {(f["program"], f["function"], f["code"], f["severity"]) for f in r["findings"]}


def test_length_increase_finds_truncation_and_host_flow():
    r = _run(table="TB_CUST", column="CUST_NM", change="length", new_length=50)
    c = _codes(r)
    assert ("ORD010", "fetch_orders", "ORA-01406", "ERROR") in c
    assert ("ORD020", "load_customer", "ORA-01406", "ERROR") in c
    # 받은 변수를 TB_ORDER_HIST 에 다시 저장 → 길이 초과 경고
    assert ("ORD010", "confirm_order", "ORA-12899", "WARN") in c
    f = next(x for x in r["findings"] if x["function"] == "fetch_orders")
    assert f["batch"][0]["order"] == 1 and f["call_paths"][0] == ["main", "fetch_orders"]
    assert r["summary"]["programs"] == ["ORD010", "ORD020"]       # 배치 순서


def test_length_within_capacity_is_quiet():
    r = _run(table="TB_CUST", column="CUST_NM", change="length", new_length=20)
    assert not [f for f in r["findings"] if f["code"] == "ORA-01406"]


def test_nullable_reports_ora_01405():
    r = _run(table="TB_CUST", column="CUST_NM", change="nullable", nullable=True)
    assert ("ORD010", "fetch_orders", "ORA-01405", "ERROR") in _codes(r)


def test_data_change_condition_and_overwrite():
    r = _run(table="TB_ORDER", column="STATUS", change="data", operation="update", old_value="01", new_value="05")
    titles = {(f["function"], f["title"]) for f in r["findings"]}
    assert ("fetch_orders", "처리 대상에서 빠짐") in titles
    assert ("confirm_order", "같은 컬럼을 다시 씀") in titles
    # 커서 선언과 OPEN 이 중복으로 나오지 않는다
    assert len([f for f in r["findings"] if f["title"] == "처리 대상에서 빠짐"]) == 1
    assert any("GROUP BY STATUS" in c["sql"] for c in r["db_checks"])


def test_decode_new_value_goes_default():
    r = _run(table="TB_CUST", column="GRADE", change="data", operation="update", old_value="A", new_value="S")
    assert any(f["code"] == "분기 변경" and "ELSE" in f["message"] for f in r["findings"])


def test_drop_column_and_add_column():
    r = _run(table="TB_ORDER", column="QTY", change="drop_column")
    errs = [f for f in r["findings"] if f["code"] == "ORA-00904"]
    assert len(errs) == 1 and errs[0]["kind"] == "open"
    r = _run(table="TB_CUST_SUM", column="NEW_COL", change="add_column")
    assert ("ORD020", "sum_confirmed", "ORA-00947", "ERROR") in _codes(r)


def test_drop_table_dynamic_sql_warning():
    r = _run(table="TB_ORDER_STAT", change="drop_table")
    assert r["findings"] and all(f["code"] == "동적 SQL" for f in r["findings"])


def test_downstream_reader_after_in_batch():
    r = _run(table="TB_CUST", column="CUST_NM", change="length", new_length=50)
    flow = next(d for d in r["downstream"] if d["table"] == "TB_ORDER")
    assert any(x["program"] == "ORD020" and x["after"] for x in flow["readers"])


def test_without_snapshot_still_works_and_notes():
    load_samples(with_snapshot=False)
    r = _run(table="TB_CUST", column="CUST_NM", change="nullable", nullable=True)
    assert r["findings"] and any("메타데이터" in n for n in r["notes"])


def test_invalid_scenario():
    with pytest.raises(ScenarioError):
        _run(table="TB_CUST", change="length")
    with pytest.raises(ScenarioError):
        _run(table="", change="drop_table")


def test_readonly_sql_validation():
    assert validate_readonly_sql("SELECT ';' FROM DUAL;") == "SELECT ';' FROM DUAL"
    for bad in ("DELETE FROM T", "SELECT 1 FROM DUAL; DROP TABLE T", "SELECT * FROM T FOR UPDATE", "  "):
        with pytest.raises(DbError):
            validate_readonly_sql(bad)


def test_length_shrink_literals():
    r = _run(table="TB_ORDER", column="STATUS", change="length", new_length=1)
    c = _codes(r)
    assert ("ORD010", "confirm_order", "ORA-12899", "ERROR") in c      # SET STATUS = '02'
    assert any(f["code"] == "조건 불일치" and f["function"] == "fetch_orders" for f in r["findings"])
