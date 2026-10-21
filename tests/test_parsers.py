from batch_parser import expand_steps, parse_batch_script
from proc_parser import find_exec_sql, parse_proc_source, strip_comments
from source_repository import get_index
from sql_analyzer import analyze_sql


# ---------- SQL ----------

def test_select_into_bindings_and_indicator():
    r = analyze_sql("SELECT A.NAME, NVL(A.AMT, 0) INTO :h_name:i_name, :h_amt FROM TB_X A WHERE A.ID = :h_id AND A.ST = '01'")
    assert r["kind"] == "select"
    assert r["tables"] == [{"name": "TB_X", "access": "R"}]
    outs = {b["column"]: b for b in r["bindings"] if b["direction"] == "out"}
    assert outs["NAME"]["host"] == "h_name" and outs["NAME"]["indicator"] == "i_name"
    assert outs["AMT"]["null_safe"] is True
    assert any(b["direction"] == "filter" and b["column"] == "ID" for b in r["bindings"])
    assert any(l["column"] == "ST" and l["values"] == ["01"] for l in r["literals"])


def test_insert_update_delete_tables():
    ins = analyze_sql("INSERT INTO TB_H (A, B) VALUES (:a, 'X')")
    assert ins["tables"][0] == {"name": "TB_H", "access": "W"}
    assert ins["insert_columns"] == ["A", "B"]
    assert ins["bindings"][0]["column"] == "A" and ins["bindings"][0]["direction"] == "in"

    upd = analyze_sql("UPDATE SCH.TB_O O SET O.ST = :s, AMT = AMT + 1 WHERE O.NO = :n")
    assert upd["tables"][0]["name"] == "TB_O"
    assert {"column": "ST"}.items() <= next(b for b in upd["bindings"] if b["direction"] == "in").items()

    dele = analyze_sql("DELETE FROM TB_D WHERE K IN ('A', 'B')")
    assert dele["tables"][0]["access"] == "W"
    assert dele["literals"][0]["op"] == "IN" and dele["literals"][0]["values"] == ["A", "B"]


def test_insert_select_lineage_and_no_columns():
    r = analyze_sql("INSERT INTO TB_T (C1, C2) SELECT S.X, S.Y FROM TB_S S WHERE S.Z = 1")
    assert {t["name"]: t["access"] for t in r["tables"]} == {"TB_T": "W", "TB_S": "R"}
    assert r["lineage"][0]["target_column"] == "C1" and r["lineage"][0]["source"]["column"] == "X"
    r2 = analyze_sql("INSERT INTO TB_T VALUES (:a, :b)")
    assert r2["insert_no_columns"] is True


def test_cursor_join_and_decode():
    r = analyze_sql("DECLARE c1 CURSOR FOR SELECT DECODE(A.T, 'A', 1, 'B', 2) FROM TB_A A JOIN TB_B B ON A.K = B.K")
    assert r["kind"] == "cursor" and r["cursor"] == "C1"
    assert {t["name"] for t in r["tables"]} == {"TB_A", "TB_B"}
    assert any(l["op"] == "DECODE" and l["values"] == ["A", "B"] for l in r["literals"])


def test_whenever_and_dynamic():
    assert analyze_sql("WHENEVER NOT FOUND DO break")["whenever"] == {"condition": "NOT FOUND", "action": "DO break"}
    assert analyze_sql("PREPARE s FROM :buf")["dynamic"] is True


# ---------- Pro*C ----------

KR_SRC = """
#define LEN 10
EXEC SQL INCLUDE sqlca;
int old_style(a, b)
char *a;
int b;
{
    char h_v[LEN + 1];
    EXEC SQL SELECT COL INTO :h_v FROM TB WHERE K = :a; /* 주석 { */
    if (sqlca.sqlcode == 1403) return 0;
    helper(b);
    return 1;
}
static void helper(int x) { EXEC SQL DELETE FROM TB2 WHERE N = :x; }
"""


def test_proc_functions_and_hostvars():
    r = parse_proc_source(KR_SRC, "k.pc")
    names = [f["name"] for f in r["functions"]]
    assert names == ["old_style", "helper"]
    f = r["functions"][0]
    assert f["calls"] == ["helper"]
    assert f["host_vars"]["h_v"]["capacity"] == 10
    st = next(s for s in r["statements"] if s["kind"] == "select")
    assert st["function"] == "old_style" and st["checks"]["notfound"] is True
    assert next(s for s in r["statements"] if s["kind"] == "delete")["function"] == "helper"


def test_comments_and_semicolon_in_string():
    src = "void f(){ EXEC SQL UPDATE T SET A = 'x;y' -- c\n WHERE B = 1; }"
    spans = find_exec_sql(strip_comments(src))
    assert len(spans) == 1 and "WHERE B = 1" in spans[0][2]


def test_sample_index_links_cursor_struct_and_headers():
    idx = get_index()
    assert set(idx.programs) == {"ORD010", "ORD020", "ORD030"}
    fetch = next(s for s in idx.statements.values() if s["program"] == "ORD010" and s["kind"] == "fetch")
    outs = {b["column"]: b for b in fetch["bindings"] if b["direction"] == "out"}
    assert outs["CUST_NM"]["hv"]["capacity"] == 20          # LEN_CUST_NM(헤더) + 1
    sel = next(s for s in idx.statements.values() if s["program"] == "ORD020" and s["line"] == 21)
    outs = {b["column"]: b["host"] for b in sel["bindings"] if b["direction"] == "out"}
    assert outs["CUST_NM"] == "st_cust.cust_nm"              # typedef 구조체 펼치기
    assert idx.call_paths("ORD010.confirm_order")[0] == ["ORD010.main", "ORD010.fetch_orders", "ORD010.confirm_order"]


# ---------- 배치 ----------

def test_batch_order_and_nested_script():
    idx = get_index()
    runs = idx.program_runs
    assert runs["ORD010"][0]["order"] == 1 and runs["ORD010"][0]["checks_exit"] is True
    assert runs["ORD020"][0]["order"] == 2 and runs["ORD020"][0]["via"] == ["daily_ord.sh"]


def test_windows_batch():
    text = "@echo off\r\nREM 주석\r\ncall sub.bat\r\nORD010.exe %1\r\nif errorlevel 1 goto err\r\n"
    s = parse_batch_script(text, "run.bat")
    names = [c["name"] for c in s["commands"]]
    assert names == ["call", "ORD010"]
    steps = expand_steps({"run.bat": s}, "run.bat", {"ord010": "ORD010"})
    prog = next(x for x in steps if x.get("program"))
    assert prog["order"] == 1 and prog["checks_exit"] is True


def test_cp949_crlf_source():
    from proc_parser import decode_source
    src = "/* 한글 주석 */\r\nint f()\r\n{\r\n    EXEC SQL SELECT NM INTO :h FROM TB_K\r\n             WHERE ID = '가';\r\n    return 0;\r\n}\r\n"
    r = parse_proc_source(decode_source(src.encode("cp949")), "k.pc")
    st = next(s for s in r["statements"] if s["kind"] == "select")
    assert st["function"] == "f" and st["line"] == 4 and st["literals"][0]["values"] == ["가"]


def test_dynamic_sql_reconstruct_unit():
    from dynamic_sql import reconstruct
    text = """
        char w[100];
        strcpy(w, " WHERE A = :1");
        if (x) strcat(w, " AND B = 'Y'");
        sprintf(buf, "SELECT C1, C2 FROM %s%s", "TB_X", w);
        sprintf(buf + strlen(buf), " ORDER BY %d", n);
    """
    r = reconstruct(text, ":buf", {})
    assert r["sql"] == "SELECT C1, C2 FROM TB_X WHERE A = :p1 AND B = 'Y' ORDER BY 0" and not r["partial"]
    r2 = reconstruct('sprintf(q, "DELETE FROM %s", tab);', "q", {})
    assert r2["partial"] and "__V_TAB__" in r2["sql"]
    assert reconstruct("strcpy(other, \"x\");", "buf", {}) is None
