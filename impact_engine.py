"""[A05] 영향도 분석 엔진 (규칙 기반, LLM 미사용).

입력: 변경 시나리오 — 어떤 테이블/컬럼을 어떻게 바꾸는지
출력: 영향받는 Pro*C 문장 목록(프로그램 · 함수 · 줄 · 예상 ORA 오류 · 이유), 배치 실행 순서,
      호출 경로, 뒤 단계로 번지는 영향(downstream), DB 로 확인해 볼 조회 SQL

예상 오류는 '정적 분석으로 본 가능성'이다. 실제 발생 여부는 데이터 값과 실행 경로에 달려 있다.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import config
from db_runtime import DbError, SchemaMeta, column_type_text, db_dependents, ensure_tables
from source_repository import ProjectIndex

CHANGE_TYPES = {
    "length": "컬럼 길이 변경",
    "type": "컬럼 타입/정밀도 변경",
    "nullable": "NULL 허용 여부 변경",
    "drop_column": "컬럼 삭제",
    "rename_column": "컬럼 이름 변경",
    "add_column": "컬럼 추가",
    "drop_table": "테이블 삭제",
    "rename_table": "테이블 이름 변경",
    "data": "데이터 변경",
}
DATA_OPS = {"update": "값 수정", "delete": "행 삭제", "insert": "행 추가", "set_null": "NULL 로 변경"}

SEVERITY_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}
CHAR_TYPES = ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "VARCHAR", "LONG", "CLOB")
PASS_THROUGH_FUNCS = {"", "NVL", "NVL2", "COALESCE", "TRIM", "RTRIM", "LTRIM", "UPPER", "LOWER", "MAX", "MIN"}
NULL_SAFE_FUNCS = {"NVL", "NVL2", "COALESCE", "COUNT", "DECODE"}
INT_DIGITS = {16: 4, 32: 9, 64: 18}


class ScenarioError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ---------- 시나리오 정리 ----------

def normalize_scenario(s: Dict) -> Dict:
    out = {k: v for k, v in (s or {}).items() if v not in (None, "")}
    table = str(out.get("table") or "").strip().upper()
    if "." in table:
        table = table.split(".")[-1]
    if not table:
        raise ScenarioError("테이블 이름을 입력해 주세요.")
    out["table"] = table
    change = out.get("change") or ""
    if change not in CHANGE_TYPES:
        raise ScenarioError("변경 종류를 선택해 주세요.")
    col = str(out.get("column") or "").strip().upper()
    if col:
        out["column"] = col
    else:
        out.pop("column", None)
    needs_col = change in ("length", "type", "nullable", "drop_column", "rename_column", "add_column")
    if needs_col and not col:
        raise ScenarioError("컬럼 이름을 입력해 주세요.")
    for k in ("new_length", "new_precision", "new_scale"):
        if k in out:
            try:
                out[k] = int(out[k])
            except (TypeError, ValueError):
                raise ScenarioError(f"{k} 는 숫자여야 합니다.")
    if change == "length" and not out.get("new_length"):
        raise ScenarioError("새 길이를 입력해 주세요.")
    if change == "type":
        out["new_type"] = str(out.get("new_type") or "").strip().upper()
        if not out["new_type"]:
            raise ScenarioError("새 타입을 입력해 주세요. (예: VARCHAR2, NUMBER, DATE)")
    if change == "nullable":
        v = out.get("nullable")
        if isinstance(v, str):
            v = v.strip().upper() in ("Y", "YES", "TRUE", "1", "NULL")
        if v is None:
            raise ScenarioError("NULL 허용(Y) / NOT NULL(N) 중 하나를 선택해 주세요.")
        out["nullable"] = bool(v)
    if change in ("rename_column", "rename_table"):
        out["new_name"] = str(out.get("new_name") or "").strip().upper()
    if change == "data":
        op = out.get("operation") or "update"
        if op not in DATA_OPS:
            raise ScenarioError("데이터 변경 종류가 올바르지 않습니다.")
        out["operation"] = op
        if op in ("update", "set_null") and not col:
            raise ScenarioError("값을 바꿀 컬럼을 입력해 주세요.")
    return out


def describe_scenario(s: Dict, meta_col: Optional[Dict]) -> str:
    target = f"{s['table']}.{s['column']}" if s.get("column") else s["table"]
    ch = s["change"]
    cur = f" (현재 {column_type_text(meta_col)}{', NULL 허용' if meta_col and meta_col.get('nullable') else (', NOT NULL' if meta_col else '')})" if meta_col else ""
    if ch == "length":
        return f"{target}{cur} 길이를 {s['new_length']}(으)로 변경"
    if ch == "type":
        p = f"({s['new_precision']}{',' + str(s['new_scale']) if s.get('new_scale') else ''})" if s.get("new_precision") else ""
        return f"{target}{cur} 타입을 {s['new_type']}{p}(으)로 변경"
    if ch == "nullable":
        return f"{target}{cur} 을(를) {'NULL 허용' if s['nullable'] else 'NOT NULL'}(으)로 변경"
    if ch in ("rename_column", "rename_table"):
        return f"{target}{cur} 이름을 {s.get('new_name') or '?'}(으)로 변경"
    if ch == "data":
        op = s["operation"]
        parts = [f"{target}{cur} {DATA_OPS[op]}"]
        if s.get("old_value") is not None and op == "update":
            parts.append(f"'{s['old_value']}' → '{s.get('new_value', '')}'")
        elif s.get("new_value") is not None and op in ("update", "insert"):
            parts.append(f"새 값 '{s['new_value']}'")
        if s.get("condition"):
            parts.append(f"조건: {s['condition']}")
        return " ".join(parts)
    return f"{target}{cur} {CHANGE_TYPES[ch]}"


# ---------- 컬럼 참조 매칭 ----------

class _Matcher:
    def __init__(self, meta: SchemaMeta, table: str, column: Optional[str]):
        self.meta, self.table, self.column = meta, table, column

    def table_match(self, name: Optional[str]) -> bool:
        if not name:
            return False
        if name.upper() == self.table:
            return True
        t = self.meta.table(name)
        return bool(t and t["name"] == self.table)

    def ref(self, r: Dict, column: Optional[str] = None) -> Tuple[bool, bool]:
        """(맞음, 확실함). 테이블이 명시 안 된 컬럼은 후보 테이블과 메타데이터로 판단한다."""
        col = (column or self.column or "").upper()
        if not r.get("column") or r["column"].upper() != col:
            return False, False
        if r.get("table"):
            return self.table_match(r["table"]), True
        cands = r.get("candidates") or []
        if not any(self.table_match(c) for c in cands):
            return False, False
        owners_known = [c for c in cands if self.meta.has_column(c, col) is True]
        if owners_known:
            return any(self.table_match(c) for c in owners_known), len(owners_known) == 1
        unknown = [c for c in cands if self.meta.has_column(c, col) is None]
        return (any(self.table_match(c) for c in unknown) or len(cands) == 1), len(cands) == 1


# ---------- 결과 만들기 ----------

def _snippet(sql: str, n: int = 300) -> str:
    return sql if len(sql) <= n else sql[:n] + " …"


def _host_text(b: Dict) -> str:
    hv = b.get("hv")
    name = b.get("host", "?")
    ind = f":{b['indicator']}" if b.get("indicator") else ""
    if not hv:
        return f":{name}{ind} (선언을 찾지 못함)"
    ctype = hv.get("ctype") or "?"
    dims = "".join(f"[{d}]" for d in (hv.get("dims_expr") or [])) if hv.get("dims_expr") else ""
    return f":{name}{ind} ({ctype}{'*' if hv.get('pointer') else ''}{dims}, {hv.get('line', '?')}줄 선언)"


class _Collector:
    def __init__(self, idx: ProjectIndex):
        self.idx = idx
        self.items: List[Dict] = []
        self.keys = set()

    def add(self, st: Dict, severity: str, code: str, title: str, message: str, binding: Optional[Dict] = None,
            existing: bool = False, certain: bool = True):
        key = (st["key"], code, title, (binding or {}).get("host"))
        if key in self.keys:
            return
        self.keys.add(key)
        if not certain and severity == "ERROR":
            severity = "WARN"
        self.items.append({
            "severity": severity, "code": code, "title": title, "message": message,
            "program": st["program"], "file_name": st["file_name"], "function": st["function"],
            "line": st["line"], "stmt": st["key"], "kind": st["kind"], "sql": _snippet(st.get("sql", "")),
            "host": _host_text(binding) if binding else None, "existing": existing, "certain": certain,
        })


def _cap_bytes(meta_col: Optional[Dict], length: Optional[int]) -> Optional[int]:
    if length is None:
        return None
    if meta_col and meta_col.get("char_used") == "C":
        return length * config.DB_BYTES_PER_CHAR
    return length


def _int_digits(hv: Dict) -> Optional[int]:
    bits = hv.get("int_bits") or 0
    if not bits:
        bits = config.HOST_LONG_BITS if "long" in (hv.get("ctype") or "") else 32
    return INT_DIGITS.get(bits)


def _is_numeric_text(v) -> bool:
    return v is not None and re.fullmatch(r"\s*[+-]?(\d+(\.\d*)?|\.\d+)\s*", str(v)) is not None


def _handled_notfound(st: Dict) -> bool:
    w = (st.get("whenever") or {}).get("NOT FOUND", "")
    return bool(st.get("checks", {}).get("notfound")) or (w and not w.upper().startswith("CONTINUE"))


# ---------- 규칙 ----------

def analyze_impact(idx: ProjectIndex, scenario: Dict, meta: Optional[SchemaMeta] = None) -> Dict:
    s = normalize_scenario(scenario)
    meta = meta or SchemaMeta()
    T, C = s["table"], s.get("column")
    m = _Matcher(meta, T, C)
    tmeta = meta.table(T)
    cmeta = meta.column(T, C) if C else None
    out = _Collector(idx)
    notes: List[str] = []
    if not meta.available:
        notes.append("DB 메타데이터(스냅샷)가 없어 소스만으로 분석했습니다. 컬럼 길이·NULL 여부 비교는 DB 연결 후 더 정확해집니다.")
    elif not tmeta:
        notes.append(f"메타데이터에서 {T} 테이블을 찾지 못했습니다. (스키마·시노님 확인)")
    elif C and not cmeta and s["change"] != "add_column":
        notes.append(f"메타데이터에서 {T}.{C} 컬럼을 찾지 못했습니다.")

    rule = {
        "length": _rule_length, "type": _rule_type, "nullable": _rule_nullable,
        "drop_column": _rule_drop_column, "rename_column": _rule_drop_column, "add_column": _rule_add_column,
        "drop_table": _rule_drop_table, "rename_table": _rule_drop_table, "data": _rule_data,
    }[s["change"]]
    db_checks: List[Dict] = []
    _mark_cursor_open(idx)
    rule(idx, s, m, cmeta, tmeta, out, db_checks, notes)

    # 동적 SQL 안에서 테이블을 쓰는 것으로 보이는 곳
    for u in idx.table_usage.get(T, []):
        if u.get("guessed"):
            st = idx.statements.get(u["stmt"])
            if st:
                out.add(st, "WARN", "동적 SQL", "동적 SQL 확인 필요",
                        f"{T} 를 문자열로 조립한 동적 SQL 에서 쓰는 것으로 보입니다. 정적 분석으로는 컬럼 사용을 확인할 수 없으니 직접 확인해 주세요.",
                        certain=False)

    findings = sorted(out.items, key=lambda f: (SEVERITY_ORDER[f["severity"]], _batch_rank(idx, f["program"]),
                                                f["program"], f["line"]))
    for f in findings:
        f["batch"] = idx.program_runs.get(f["program"], [])[:5]
        fk = f"{f['program']}.{f['function']}"
        f["call_paths"] = [[p.split(".", 1)[1] for p in path] for path in idx.call_paths(fk)] if fk in idx.functions else []
    downstream = _downstream(idx, findings, s)
    progs = sorted({f["program"] for f in findings}, key=lambda p: (_batch_rank(idx, p), p))
    summary = {
        "error": sum(1 for f in findings if f["severity"] == "ERROR"),
        "warn": sum(1 for f in findings if f["severity"] == "WARN"),
        "info": sum(1 for f in findings if f["severity"] == "INFO"),
        "programs": progs,
        "functions": sorted({f"{f['program']}.{f['function']}" for f in findings}),
    }
    usage = [u for u in idx.table_usage.get(T, []) if not u.get("guessed")]
    if not usage:
        notes.append(f"등록된 Pro*C 소스에서 {T} 를 쓰는 SQL 을 찾지 못했습니다.")
    return {
        "scenario": s,
        "description": describe_scenario(s, cmeta),
        "column_meta": cmeta,
        "table_meta": {k: tmeta.get(k) for k in ("owner", "name", "comment", "num_rows", "pk", "unique")} if tmeta else None,
        "summary": summary,
        "findings": findings,
        "downstream": downstream,
        "db_checks": db_checks,
        "notes": notes,
    }


def _batch_rank(idx: ProjectIndex, prog: str) -> int:
    runs = idx.program_runs.get(prog) or []
    orders = [r["order"] for r in runs if r.get("order")]
    return min(orders) if orders else 10 ** 6


def _stmts(idx: ProjectIndex):
    """분석 대상 문장. OPEN 이 있는 커서 선언은 OPEN/FETCH 쪽에서 보므로 뺀다(중복 방지)."""
    for st in idx.statements.values():
        if st["kind"] == "cursor" and st.get("_has_open"):
            continue
        yield st


def _stmts_on_table(idx: ProjectIndex, m: _Matcher):
    for st in _stmts(idx):
        if any(m.table_match(t["name"]) for t in st.get("tables") or []):
            yield st


def _structural_stmt(st: Dict) -> bool:
    """테이블/컬럼 존재 오류(ORA-00942/00904)가 나는 문장. 커서는 DECLARE 대신 OPEN 에서 난다."""
    return st["kind"] not in ("fetch", "close", "cursor") or (st["kind"] == "cursor" and not st.get("_has_open"))


def _mark_cursor_open(idx: ProjectIndex):
    opened = {(s["program"], s.get("cursor_stmt")) for s in idx.statements.values() if s["kind"] == "open"}
    for s in idx.statements.values():
        if s["kind"] == "cursor":
            s["_has_open"] = (s["program"], s["id"]) in opened


def _host_flows(idx: ProjectIndex, st: Dict, b: Dict) -> List[Tuple[Dict, Dict]]:
    """out 바인딩의 호스트 변수가 같은 프로그램의 INSERT/UPDATE 에 다시 쓰이는 곳."""
    host = b["host"]
    hv = b.get("hv") or {}
    res = []
    for other in idx.statements.values():
        if other["program"] != st["program"] or other["key"] == st["key"]:
            continue
        for ob in other.get("bindings") or []:
            if ob["direction"] == "in" and ob["host"] == host:
                ohv = ob.get("hv") or {}
                if ohv.get("line") == hv.get("line"):  # 같은 선언(같은 변수)
                    res.append((other, ob))
    return res


def _rule_length(idx, s, m, cmeta, tmeta, out, db_checks, notes):
    T, C, new_len = s["table"], s["column"], s["new_length"]
    old_len = cmeta.get("length") if cmeta else None
    if cmeta and cmeta.get("type") not in CHAR_TYPES + ("RAW",):
        notes.append(f"{T}.{C} 은(는) {column_type_text(cmeta)} 이라 '길이' 보다 '타입/정밀도 변경'으로 분석하는 것이 맞습니다.")
    new_b, old_b = _cap_bytes(cmeta, new_len), _cap_bytes(cmeta, old_len)
    grow = old_len is None or new_len > old_len
    for st in _stmts(idx):
        for b in st.get("bindings") or []:
            ok, certain = m.ref(b)
            if not ok:
                continue
            hv = b.get("hv")
            func = b.get("func") or ("" if not b.get("expr") else "?")
            if b["direction"] == "out":
                if func not in PASS_THROUGH_FUNCS:
                    continue
                if not hv or hv.get("capacity") is None:
                    if hv and hv.get("kind") in ("string", "varchar", "char1") or not hv:
                        out.add(st, "INFO", "확인 필요", "호스트 변수 크기 확인 불가",
                                f"{C} 값을 받는 변수의 크기를 소스에서 계산하지 못했습니다. 선언 크기가 {new_b}바이트 이상인지 확인해 주세요.",
                                b, certain=certain)
                    continue
                if hv.get("kind") not in ("string", "varchar", "char1"):
                    continue
                cap = hv["capacity"]
                if cap < new_b:
                    existing = old_b is not None and cap < old_b
                    sev = "WARN" if b.get("indicator") else "ERROR"
                    unit = f"{new_len}자×{config.DB_BYTES_PER_CHAR}바이트" if cmeta and cmeta.get("char_used") == "C" else f"{new_b}바이트"
                    msg = (f"{C} 를 길이 {new_len}({unit})로 늘리면, 받는 변수는 최대 {cap}바이트"
                           f"{'(char 배열은 널 문자 1바이트 제외)' if hv.get('kind') == 'string' else ''}만 담을 수 있습니다. "
                           + ("indicator 변수가 있어 오류 대신 값이 잘리고 indicator 가 원래 길이로 설정됩니다."
                              if b.get("indicator") else f"{cap}바이트보다 긴 값을 FETCH/SELECT 하면 ORA-01406(값 잘림) 오류가 납니다."))
                    if existing:
                        msg += f" (현재 길이 {old_len} 기준으로도 이미 부족한 변수입니다)"
                    out.add(st, sev, "ORA-01406", "조회 값 잘림", msg, b, existing=existing, certain=certain)
                    for other, ob in _host_flows(idx, st, b):
                        tcol = meta_col(m.meta, ob)
                        if tcol and tcol.get("length") and _cap_bytes(tcol, tcol["length"]) < new_b:
                            out.add(other, "WARN", "ORA-12899", "다른 테이블 저장 시 길이 초과",
                                    f"{st['function']}() 에서 {C} 를 받은 :{b['host']} 를 {ob.get('table')}.{ob['column']}"
                                    f"({column_type_text(tcol)}) 에 저장합니다. 변수를 {new_len} 길이로 늘리면 이 INSERT/UPDATE 에서 ORA-12899(값이 너무 큼)가 날 수 있습니다.",
                                    ob, certain=certain)
            elif b["direction"] == "in":
                if not hv or hv.get("capacity") is None:
                    continue
                cap = hv["capacity"]
                if not grow and cap > new_b:
                    out.add(st, "WARN", "ORA-12899", "저장 값 길이 초과",
                            f"{C} 를 {new_len} 로 줄이면, :{b['host']}(최대 {cap}바이트) 값이 {new_b}바이트를 넘을 때 ORA-12899(값이 너무 큼) 오류가 납니다.",
                            b, certain=certain)
                elif grow and cap < new_b:
                    out.add(st, "INFO", "참고", "입력 변수도 함께 확장 필요",
                            f"저장에 쓰는 :{b['host']} 는 최대 {cap}바이트라 {C} 를 늘려도 {cap}바이트까지만 저장됩니다. 늘린 길이를 쓰려면 변수도 키워야 합니다.",
                            b, certain=certain)
            elif b["direction"] == "filter" and hv and hv.get("capacity") is not None and grow and hv["capacity"] < new_b:
                out.add(st, "INFO", "참고", "조건 변수 크기",
                        f"WHERE 조건의 :{b['host']}(최대 {hv['capacity']}바이트)가 새 길이보다 작아 긴 값으로는 조회할 수 없습니다.",
                        b, certain=certain)
    # SQL 에 직접 적은 상수가 새 길이보다 긴 경우
    if not grow:
        for st in _stmts(idx):
            for lit in st.get("literals") or []:
                ok, certain = m.ref(lit)
                if not ok:
                    continue
                long_vals = [v for v in lit.get("values") or [] if v is not None
                             and len(str(v).encode(_db_encoding())) > new_b]
                if not long_vals:
                    continue
                if lit.get("context") in ("set", "insert"):
                    out.add(st, "ERROR", "ORA-12899", "저장 상수 길이 초과",
                            f"{C} 에 상수 {', '.join(repr(v) for v in long_vals[:3])} 를 저장합니다. 길이를 {new_len} 로 줄이면 ORA-12899 오류가 납니다.",
                            certain=certain)
                else:
                    out.add(st, "WARN", "조건 불일치", "조건 상수가 새 길이보다 김",
                            f"조건 {C} {lit['op']} {', '.join(repr(v) for v in long_vals[:3])} 는 길이를 {new_len} 로 줄이면 맞는 행이 없어집니다(처리 대상 0건).",
                            certain=certain)
    # INSERT ... SELECT / UPDATE SET 로 다른 테이블에 옮겨 담는 경우
    for st in _stmts(idx):
        for ln in st.get("lineage") or []:
            src = ln["source"]
            ok, certain = m.ref(src)
            if ok and grow:
                tcol = m.meta.column(ln["target_table"], ln["target_column"])
                if tcol and tcol.get("length") and _cap_bytes(tcol, tcol["length"]) < new_b and not src.get("expr"):
                    out.add(st, "ERROR", "ORA-12899", "옮겨 담는 컬럼 길이 초과",
                            f"{T}.{C} 값을 {ln['target_table']}.{ln['target_column']}({column_type_text(tcol)}) 에 그대로 넣습니다. "
                            f"{tcol['length']} 보다 긴 값이 생기면 ORA-12899 오류가 납니다.", certain=certain)
            if m.table_match(ln.get("target_table")) and ln["target_column"] == C and not grow:
                scol = m.meta.column(src.get("table") or (src.get("candidates") or [None])[0], src["column"])
                if scol and scol.get("length") and scol["length"] > new_len:
                    out.add(st, "WARN", "ORA-12899", "원본 컬럼이 더 김",
                            f"{src.get('table') or ''}.{src['column']}({column_type_text(scol)}) 값을 {C} 에 넣는데, {new_len} 로 줄이면 길이 초과가 날 수 있습니다.")
    if old_len and new_len < old_len and m.meta.available:
        db_checks.append({"title": f"새 길이 {new_len}을 넘는 기존 데이터 건수 (0이 아니면 ALTER 가 ORA-01441 로 실패)",
                          "sql": f"SELECT COUNT(*) AS CNT FROM {T} WHERE {'LENGTH' if cmeta.get('char_used') == 'C' else 'LENGTHB'}({C}) > {new_len}"})
    if old_len and m.meta.available:
        db_checks.append({"title": f"현재 {C} 최대 길이",
                          "sql": f"SELECT MAX(LENGTHB({C})) AS MAX_BYTES, MAX(LENGTH({C})) AS MAX_CHARS FROM {T}"})


def meta_col(meta: SchemaMeta, b: Dict) -> Optional[Dict]:
    tables = [b.get("table")] if b.get("table") else (b.get("candidates") or [])
    for t in tables:
        c = meta.column(t, b["column"])
        if c:
            return c
    return None


def _rule_type(idx, s, m, cmeta, tmeta, out, db_checks, notes):
    T, C = s["table"], s["column"]
    new_t = s["new_type"]
    old_t = (cmeta or {}).get("type")
    p, sc = s.get("new_precision"), s.get("new_scale") or 0
    to_char = new_t in CHAR_TYPES
    to_num = new_t in ("NUMBER", "INTEGER", "FLOAT", "BINARY_DOUBLE", "BINARY_FLOAT")
    to_date = new_t in ("DATE", "TIMESTAMP")
    from_num = old_t == "NUMBER"
    from_char = old_t in CHAR_TYPES
    for st in _stmts(idx):
        for b in st.get("bindings") or []:
            ok, certain = m.ref(b)
            if not ok:
                continue
            hv = b.get("hv") or {}
            kind = hv.get("kind")
            if b["direction"] == "out":
                if b.get("func") not in (None, "") and b.get("func") not in PASS_THROUGH_FUNCS | {"SUM", "AVG", "ROUND", "TRUNC"}:
                    continue
                if to_char and kind in ("int", "float"):
                    out.add(st, "WARN", "ORA-01722", "숫자 변수로 문자 조회",
                            f"{C} 가 {new_t} 가 되면 숫자 변수 :{b['host']} 로 받을 때 숫자가 아닌 값이 있으면 ORA-01722(수치가 부적합) 오류가 납니다.",
                            b, certain=certain)
                elif to_num and kind == "int":
                    digits = _int_digits(hv)
                    if sc and sc > 0:
                        out.add(st, "WARN", "소수점 잘림", "정수 변수로 소수 조회",
                                f"{C} 가 소수점 {sc}자리를 가지면 정수 변수 :{b['host']} 에는 소수 부분이 버려진 값이 들어갑니다(오류 없이).",
                                b, certain=certain)
                    if digits and (p is None or p - (sc or 0) > digits):
                        out.add(st, "WARN", "ORA-01455", "정수 변수 범위 초과",
                                f":{b['host']}({hv.get('ctype')})는 약 {digits}자리까지 담습니다. {C} 가 NUMBER{('(' + str(p) + ')') if p else ''} 이면 큰 값에서 ORA-01455(정수 범위 초과) 오류가 날 수 있습니다.",
                                b, certain=certain)
                elif to_num and kind == "float" and (p is None or p > 15):
                    out.add(st, "INFO", "정밀도", "실수 변수 정밀도",
                            f":{b['host']}({hv.get('ctype')})는 유효숫자 약 15자리입니다. NUMBER({p or 38}) 의 큰 값은 끝자리가 달라질 수 있습니다.",
                            b, certain=certain)
                elif to_num and kind in ("string", "varchar") and hv.get("capacity") is not None:
                    need = (p or 38) + (1 if sc else 0) + 1
                    if hv["capacity"] < need:
                        out.add(st, "WARN", "ORA-01406", "문자 변수 길이 부족",
                                f"NUMBER({p or 38}{',' + str(sc) if sc else ''}) 값은 부호·소수점 포함 최대 {need}자리입니다. :{b['host']}(최대 {hv['capacity']}바이트)로 받으면 잘릴 수 있습니다.",
                                b, certain=certain)
                    if from_char:
                        out.add(st, "INFO", "형식 변경", "문자→숫자 표시 형식",
                                "문자일 때 '0012' 처럼 앞자리 0이 있던 값은 NUMBER 로 바뀌면 '12' 로 조회됩니다.", b, certain=certain)
                elif to_date and kind in ("string", "varchar"):
                    out.add(st, "WARN", "NLS_DATE_FORMAT", "날짜 문자열 형식",
                            f"DATE 를 문자 변수 :{b['host']} 로 받으면 세션의 NLS_DATE_FORMAT 형식으로 바뀌어 들어옵니다. 기존 문자열 형식과 다를 수 있고, 변수 크기도 확인해야 합니다.",
                            b, certain=certain)
                elif to_date and kind in ("int", "float"):
                    out.add(st, "ERROR", "ORA-00932", "날짜를 숫자 변수로 조회",
                            f"DATE 컬럼은 숫자 변수 :{b['host']} 로 받을 수 없습니다(ORA-00932 일관성 없는 데이터 타입).",
                            b, certain=certain)
            elif b["direction"] == "in":
                if to_num and kind in ("string", "varchar"):
                    out.add(st, "WARN", "ORA-01722", "문자 변수를 숫자 컬럼에 저장",
                            f"문자 변수 :{b['host']} 에 숫자가 아닌 값이 들어 있으면 ORA-01722 오류가 납니다.", b, certain=certain)
                if to_num and p and kind in ("int", "float"):
                    out.add(st, "INFO", "ORA-01438", "정밀도 확인",
                            f":{b['host']} 값이 NUMBER({p}{',' + str(sc) if sc else ''}) 범위를 넘으면 ORA-01438 오류가 납니다.", b, certain=certain)
                if to_date and kind in ("string", "varchar"):
                    out.add(st, "WARN", "ORA-01861", "문자열을 날짜 컬럼에 저장",
                            f"문자 변수 :{b['host']} 를 TO_DATE 없이 DATE 컬럼에 넣으면 NLS_DATE_FORMAT 과 형식이 다를 때 ORA-01861 등 오류가 납니다.",
                            b, certain=certain)
            elif b["direction"] == "filter":
                if to_char and kind in ("int", "float"):
                    out.add(st, "WARN", "ORA-01722", "숫자 조건으로 문자 컬럼 비교",
                            f"WHERE {C} = :{b['host']}(숫자) 이면 Oracle 이 {C} 를 숫자로 바꿔 비교합니다. 숫자가 아닌 데이터가 있으면 ORA-01722, 없더라도 인덱스를 못 탈 수 있습니다.",
                            b, certain=certain)
                elif to_num and kind in ("string", "varchar"):
                    out.add(st, "INFO", "형변환", "문자 조건으로 숫자 컬럼 비교",
                            f"WHERE {C} = :{b['host']}(문자) 는 변수 값을 숫자로 바꿔 비교합니다. 숫자가 아닌 값이면 ORA-01722.", b, certain=certain)
        for lit in st.get("literals") or []:
            ok, certain = m.ref(lit)
            if not ok:
                continue
            vals = [v for v in lit.get("values") or [] if v is not None]
            if to_num and any(not _is_numeric_text(v) for v in vals):
                bad = [v for v in vals if not _is_numeric_text(v)][:3]
                out.add(st, "ERROR", "ORA-01722", "숫자가 아닌 상수",
                        f"{C} 를 숫자 타입으로 바꾸면 SQL 의 상수 {', '.join(repr(v) for v in bad)} 와 비교/저장할 때 ORA-01722 오류가 납니다.",
                        certain=certain)
            elif to_char and from_num and lit.get("context") == "where" and lit["op"] in ("<", ">", "<=", ">=", "BETWEEN"):
                out.add(st, "WARN", "정렬 기준 변경", "크기 비교 결과 변경",
                        f"{C} 가 문자 타입이 되면 '{lit['op']}' 비교가 문자 순서로 바뀝니다('10' < '9'). 결과 건수가 달라질 수 있습니다.",
                        certain=certain)
    if to_num and from_char and m.meta.available:
        db_checks.append({"title": "숫자로 바꿀 수 없는 기존 값 건수 (0이 아니면 변환 불가)",
                          "sql": f"SELECT COUNT(*) AS CNT FROM {T} WHERE {C} IS NOT NULL AND NOT REGEXP_LIKE(TRIM({C}), '^[+-]?([0-9]+(\\.[0-9]*)?|\\.[0-9]+)$')"})


def _rule_nullable(idx, s, m, cmeta, tmeta, out, db_checks, notes):
    T, C = s["table"], s["column"]
    to_null = s["nullable"]
    if cmeta is not None and cmeta.get("nullable") == to_null:
        notes.append(f"{T}.{C} 은(는) 이미 {'NULL 허용' if to_null else 'NOT NULL'} 입니다. 현재 상태 기준 점검 결과입니다.")
    for st in _stmts(idx):
        if to_null:
            for b in st.get("bindings") or []:
                ok, certain = m.ref(b)
                if not ok or b["direction"] != "out":
                    continue
                if b.get("null_safe") or (b.get("func") in NULL_SAFE_FUNCS):
                    continue
                if b.get("func") in ("SUM", "MAX", "MIN", "AVG"):
                    if not b.get("indicator"):
                        out.add(st, "WARN", "ORA-01405", "집계 결과 NULL",
                                f"{b['func']}({C}) 는 모든 값이 NULL 이거나 대상 행이 없으면 NULL 을 돌려줍니다. indicator 없는 :{b['host']} 로 받으면 ORA-01405 가 날 수 있습니다.",
                                b, certain=certain)
                    continue
                if b.get("indicator"):
                    out.add(st, "INFO", "indicator", "NULL 표시 변수 확인",
                            f"indicator :{b['indicator']} 가 있어 오류는 나지 않습니다. NULL(-1)일 때 :{b['host']} 에 이전 값이 남으므로 C 코드에서 indicator 를 확인하는지 보세요.",
                            b, certain=certain)
                else:
                    out.add(st, "ERROR", "ORA-01405", "NULL 조회 오류",
                            f"{C} 가 NULL 을 허용하면, indicator 변수 없이 :{b['host']} 로 받을 때 NULL 인 행에서 ORA-01405(인출된 열 값이 NULL) 오류가 납니다. NVL({C}, ...) 이나 indicator 변수가 필요합니다.",
                            b, certain=certain)
            for lit in st.get("literals") or []:
                ok, certain = m.ref(lit)
                if ok and lit.get("context") == "where" and lit["op"] in ("<>", "!=", "NOT IN", "^="):
                    out.add(st, "INFO", "NULL 비교", "NULL 행 제외",
                            f"WHERE {C} {lit['op']} ... 조건은 {C} 가 NULL 인 행을 포함하지 않습니다. NULL 행이 처리 대상에서 빠집니다.",
                            certain=certain)
        else:
            if st["kind"] == "insert" and any(m.table_match(t["name"]) and "W" in t["access"] for t in st.get("tables") or []):
                cols = st.get("insert_columns")
                if cols is not None and C not in [c.upper() for c in cols]:
                    out.add(st, "ERROR", "ORA-01400", "NOT NULL 컬럼 누락",
                            f"이 INSERT 는 {C} 를 넣지 않습니다. 기본값(DEFAULT) 없이 NOT NULL 로 바꾸면 ORA-01400(NULL 삽입 불가) 오류가 납니다.")
            for b in st.get("bindings") or []:
                ok, certain = m.ref(b)
                if ok and b["direction"] == "in" and b.get("indicator"):
                    out.add(st, "WARN", "ORA-01400", "NULL 저장 가능성",
                            f"indicator :{b['indicator']} 가 -1 이면 NULL 이 저장됩니다. NOT NULL 로 바꾸면 ORA-01400/ORA-01407 이 납니다.",
                            b, certain=certain)
            for lit in st.get("literals") or []:
                ok, certain = m.ref(lit)
                if ok and lit.get("context") in ("set", "insert") and lit.get("values") == [None]:
                    code = "ORA-01407" if lit["context"] == "set" else "ORA-01400"
                    out.add(st, "ERROR", code, "NULL 저장",
                            f"{C} 에 NULL 을 직접 넣습니다. NOT NULL 로 바꾸면 {code} 오류가 납니다.", certain=certain)
    if not to_null and m.meta.available:
        db_checks.append({"title": "현재 NULL 인 행 건수 (0이 아니면 ALTER 가 ORA-02296 으로 실패)",
                          "sql": f"SELECT COUNT(*) AS CNT FROM {T} WHERE {C} IS NULL"})
    if to_null:
        notes.append("NULL 허용으로 바꾸는 것만으로는 오류가 나지 않습니다. 실제로 NULL 데이터가 들어온 뒤 조회할 때 위 지점에서 오류가 납니다.")


def _rule_drop_column(idx, s, m, cmeta, tmeta, out, db_checks, notes):
    C = s["column"]
    rename = s["change"] == "rename_column"
    for st in _stmts_on_table(idx, m):
        hits = [c for c in st.get("columns") or [] if m.ref(c)[0]]
        if hits and _structural_stmt(st):
            certain = all(m.ref(c)[1] for c in hits)
            roles = sorted({c.get("role", "") for c in hits})
            out.add(st, "ERROR", "ORA-00904", "없는 컬럼 참조",
                    f"{C} 를 {'/'.join(roles)} 에서 사용합니다. {'이름을 ' + (s.get('new_name') or '새 이름') + '(으)로 바꾸면' if rename else '삭제하면'} "
                    f"실행 시 ORA-00904(부적합한 식별자) 오류가 납니다. (SQLCHECK=SEMANTICS 로 프리컴파일하면 컴파일 단계에서 오류)",
                    certain=certain)
        if st["kind"] == "insert" and st.get("insert_no_columns") and not rename and \
                any(m.table_match(t["name"]) and "W" in t["access"] for t in st.get("tables") or []):
            out.add(st, "ERROR", "ORA-00913", "INSERT 값 개수 초과",
                    f"컬럼 목록 없는 INSERT 는 테이블의 모든 컬럼 순서대로 값을 넣습니다. {C} 를 삭제하면 값이 하나 많아져 ORA-00913(값의 수가 너무 많음) 오류가 납니다.")
        if st.get("select_star") and st["kind"] in ("select", "fetch") and not rename:
            out.add(st, "ERROR", "ORA-01007", "SELECT * 컬럼 수 변경",
                    f"SELECT * 로 받는데 {C} 를 삭제하면 컬럼 수·순서가 INTO 변수와 어긋납니다(ORA-01007 등). 변수 순서도 밀립니다.")


def _rule_add_column(idx, s, m, cmeta, tmeta, out, db_checks, notes):
    C = s["column"]
    not_null = s.get("nullable") in (False, "N", "n")
    has_default = bool(s.get("default"))
    for st in _stmts_on_table(idx, m):
        writes = any(m.table_match(t["name"]) and "W" in t["access"] for t in st.get("tables") or [])
        if st["kind"] == "insert" and writes:
            if st.get("insert_no_columns"):
                out.add(st, "ERROR", "ORA-00947", "INSERT 값 개수 부족",
                        f"컬럼 목록 없는 INSERT 입니다. {C} 가 추가되면 값이 하나 모자라 ORA-00947(값의 수가 충분하지 않음) 오류가 납니다.")
            elif not_null and not has_default:
                out.add(st, "ERROR", "ORA-01400", "NOT NULL 신규 컬럼 누락",
                        f"기본값 없는 NOT NULL 컬럼 {C} 를 추가하면, {C} 를 넣지 않는 이 INSERT 에서 ORA-01400 오류가 납니다.")
        if st.get("select_star") and st["kind"] in ("select", "fetch"):
            out.add(st, "ERROR", "ORA-01007", "SELECT * 컬럼 수 변경",
                    f"SELECT * 로 받는데 {C} 가 추가되면 select 목록이 INTO 변수보다 많아져 순서가 어긋납니다.")
    if not_null and not has_default:
        notes.append("기존 행이 있는 테이블에 기본값 없는 NOT NULL 컬럼은 바로 추가할 수 없습니다(ORA-01758).")


def _rule_drop_table(idx, s, m, cmeta, tmeta, out, db_checks, notes):
    T = s["table"]
    rename = s["change"] == "rename_table"
    for st in _stmts_on_table(idx, m):
        if not _structural_stmt(st):
            continue
        acc = next((t["access"] for t in st.get("tables") or [] if m.table_match(t["name"])), "")
        out.add(st, "ERROR", "ORA-00942", "없는 테이블 참조",
                f"이 문장은 {T} 를 {'읽고 씁' if acc == 'RW' else ('씁' if 'W' in acc else '읽습')}니다. "
                f"{'이름을 ' + (s.get('new_name') or '새 이름') + '(으)로 바꾸면' if rename else '삭제하면'} "
                f"ORA-00942(테이블 또는 뷰가 존재하지 않음) 오류가 납니다." + (" 같은 이름의 시노님을 만들면 피할 수 있습니다." if rename else ""))


def _rule_data(idx, s, m, cmeta, tmeta, out, db_checks, notes):
    T, C, op = s["table"], s.get("column"), s["operation"]
    old_v = s.get("old_value")
    new_v = None if op == "set_null" else s.get("new_value")
    pk = [c.upper() for c in ((tmeta or {}).get("pk") or [])]
    uniques = [[c.upper() for c in u] for u in ((tmeta or {}).get("unique") or [])]

    for st in _stmts_on_table(idx, m):
        reads = any(m.table_match(t["name"]) and "R" in t["access"] for t in st.get("tables") or [])
        writes = any(m.table_match(t["name"]) and "W" in t["access"] for t in st.get("tables") or [])
        if st["kind"] == "close":
            continue

        # 1) 조건 상수와 비교 → 처리 대상이 바뀌는지
        if C:
            for lit in st.get("literals") or []:
                ok, certain = m.ref(lit)
                if not ok or lit.get("context") in ("set", "insert"):
                    continue
                vals = [None if v is None else str(v) for v in lit.get("values") or []]
                opx = lit["op"]
                negative = opx in ("<>", "!=", "^=", "NOT IN", "NOT LIKE")
                if lit.get("context") == "decode":
                    if op in ("update", "insert", "set_null") and new_v is not None and str(new_v) not in vals:
                        out.add(st, "WARN", "분기 변경", f"{opx} 분기에 없는 값",
                                f"{opx}({C}, ...) 는 {', '.join(repr(v) for v in vals)} 만 처리합니다. 새 값 '{new_v}' 는 "
                                + ("기본값(ELSE) 분기로 처리됩니다." if lit.get("has_default") else "어느 분기에도 없어 NULL 이 됩니다."),
                                certain=certain)
                    elif old_v is not None and str(old_v) in vals and (new_v is None or str(new_v) not in vals):
                        out.add(st, "INFO", "분기 변경", f"{opx} 분기 결과 변경",
                                f"'{old_v}' 에 해당하던 분기 결과가 바뀝니다.", certain=certain)
                    continue
                if opx not in ("=", "IN", "<>", "!=", "^=", "NOT IN", "IS NULL", "IS NOT NULL"):
                    if op == "update":
                        out.add(st, "INFO", "조건 확인", "범위 조건",
                                f"{C} {opx} {vals} 조건이 있어 값 변경에 따라 대상 건수가 달라질 수 있습니다.", certain=certain)
                    continue
                def matches(v):
                    if opx == "IS NULL":
                        return v is None
                    if opx == "IS NOT NULL":
                        return v is not None
                    hit = v is not None and str(v) in vals
                    return not hit if negative else hit
                if op == "update":
                    before = matches(old_v) if old_v is not None else None
                    after = matches(new_v) if new_v is not None or op == "set_null" else None
                    cond = f"{C} {opx} {', '.join(repr(v) for v in vals) if vals != [None] else ''}".strip()
                    if before is True and after is False:
                        out.add(st, "WARN", "대상 제외", "처리 대상에서 빠짐",
                                f"조건 [{cond}] 에 걸리던 행이 '{new_v}' 로 바뀌면 이 문장의 처리 대상에서 빠집니다.", certain=certain)
                    elif before is False and after is True:
                        out.add(st, "WARN", "대상 추가", "처리 대상에 새로 포함",
                                f"조건 [{cond}] 에 안 걸리던 행이 '{new_v}' 로 바뀌면 이 문장이 새로 처리합니다.", certain=certain)
                    elif before is None and after is not None:
                        out.add(st, "INFO", "조건 확인", "조건 상수 비교",
                                f"이 문장은 [{cond}] 조건을 씁니다. 새 값 '{new_v}' 는 {'포함' if after else '제외'}됩니다.", certain=certain)
                elif op == "set_null":
                    if matches(old_v) if old_v is not None else (opx in ("=", "IN")):
                        out.add(st, "WARN", "대상 제외", "NULL 은 조건에서 빠짐",
                                f"{C} 가 NULL 이 되면 [{C} {opx} ...] 조건에 걸리지 않습니다.", certain=certain)
                elif op in ("delete", "insert"):
                    v = new_v if op == "insert" else old_v
                    if v is not None and matches(v):
                        out.add(st, "INFO", "건수 변동", "처리 건수 변동",
                                f"[{C} {opx} {vals}] 조건의 처리 건수가 {'늘어' if op == 'insert' else '줄어'}듭니다.", certain=certain)

        # 2) 한 건 조회(SELECT INTO): 행이 없어지거나 여러 건이 될 때
        if st["kind"] == "select" and reads:
            filt = [b for b in st.get("bindings") or [] if b["direction"] == "filter" and m.ref(b, b["column"])[0]
                    and m.table_match(b.get("table") or ((b.get("candidates") or [None])[0]))]
            filt_cols = {b["column"].upper() for b in filt} | {
                l["column"].upper() for l in st.get("literals") or [] if l.get("context") == "where" and l["op"] == "="
                and m.ref(l, l["column"])[0]}
            uses_c = C is None or C in filt_cols
            aggregate = all((b.get("func") in ("SUM", "COUNT", "MAX", "MIN", "AVG")) for b in st.get("bindings") or []
                            if b["direction"] == "out") and any(b["direction"] == "out" for b in st.get("bindings") or [])
            if uses_c and op in ("delete", "update", "set_null") and not aggregate:
                handled = _handled_notfound(st)
                out.add(st, "INFO" if handled else "WARN", "ORA-01403", "조회 결과 없음",
                        f"이 SELECT INTO 는 {T} 에서 한 건을 찾습니다. 대상 행이 {'삭제' if op == 'delete' else '바뀌'}면 데이터 없음(1403)이 됩니다. "
                        + ("소스에 NOT FOUND 처리가 있습니다." if handled else "NOT FOUND 처리가 보이지 않아 이전 변수 값으로 계속 처리할 수 있습니다."))
            if C and C in filt_cols and op in ("update", "insert"):
                key_cols = [k for k in [pk] + uniques if k]
                unique_hit = any(set(k) <= filt_cols for k in key_cols)
                if not unique_hit and not aggregate:
                    out.add(st, "WARN", "ORA-01422", "여러 건 조회",
                            f"조건에 {C} 를 쓰는데 PK/UNIQUE 로 한 건이 보장되지 않습니다. 같은 값을 가진 행이 둘 이상 되면 ORA-01422(실제 인출이 요구된 것보다 많음) 오류가 납니다.")

        # 3) NULL / 긴 값이 조회 변수로 들어올 때
        if C and op in ("update", "insert", "set_null"):
            for b in st.get("bindings") or []:
                ok, certain = m.ref(b)
                if not ok or b["direction"] != "out":
                    continue
                hv = b.get("hv") or {}
                if op == "set_null":
                    if not b.get("indicator") and not b.get("null_safe") and b.get("func") not in NULL_SAFE_FUNCS:
                        out.add(st, "ERROR", "ORA-01405", "NULL 조회 오류",
                                f"{C} 가 NULL 이 되면 indicator 없는 :{b['host']} 로 받을 때 ORA-01405 오류가 납니다.", b, certain=certain)
                elif new_v is not None and hv.get("capacity") is not None and hv.get("kind") in ("string", "varchar") \
                        and b.get("func") in PASS_THROUGH_FUNCS | {None}:
                    nbytes = len(str(new_v).encode(_db_encoding()))
                    if nbytes > hv["capacity"]:
                        out.add(st, "WARN" if b.get("indicator") else "ERROR", "ORA-01406", "조회 값 잘림",
                                f"새 값 '{new_v}'({nbytes}바이트)가 :{b['host']}(최대 {hv['capacity']}바이트)보다 깁니다.",
                                b, certain=certain)
                elif new_v is not None and hv.get("kind") in ("int", "float") and not _is_numeric_text(new_v):
                    out.add(st, "ERROR", "ORA-01722", "숫자 변수로 문자 조회",
                            f"새 값 '{new_v}' 는 숫자가 아니라 숫자 변수 :{b['host']} 로 받을 때 ORA-01722 오류가 납니다.", b, certain=certain)
                elif op == "update":
                    out.add(st, "INFO", "값 변경", "조회 값 변경",
                            f"이 문장이 읽는 {C} 값이 바뀝니다. 이후 계산/저장 결과가 달라집니다.", b, certain=certain)

        # 4) 같은 컬럼을 다시 쓰는 프로그램
        if C and writes:
            sets = [b for b in st.get("bindings") or [] if b["direction"] == "in" and m.ref(b)[0]] + \
                   [l for l in st.get("literals") or [] if l.get("context") in ("set", "insert") and m.ref(l)[0]]
            for x in sets:
                val = (x.get("values") or [None])[0] if "values" in x else f":{x.get('host')}"
                out.add(st, "INFO", "덮어쓰기", "같은 컬럼을 다시 씀",
                        f"이 프로그램이 {T}.{C} 를 {val!r} 로 다시 씁니다. 배치 순서상 뒤에 실행되면 바꾼 값이 덮어써집니다.",
                        x if "host" in x else None)
            if op == "delete" and st["kind"] in ("update", "delete"):
                out.add(st, "INFO", "건수 변동", "처리 건수 변동",
                        f"행을 지우면 이 {st['kind'].upper()} 의 처리 건수(sqlca.sqlerrd[2])가 줄어듭니다.")
        # 5) 집계/목록 조회
        if reads and st["kind"] in ("open", "select") and not C:
            out.add(st, "INFO", "건수 변동", "조회 건수 변동", f"{T} 행이 {DATA_OPS[op]}되면 이 조회 결과가 달라집니다.")

    # DB 확인 SQL
    if m.meta.available:
        if s.get("condition"):
            db_checks.append({"title": "변경 대상 건수", "sql": f"SELECT COUNT(*) AS CNT FROM {T} WHERE {s['condition']}"})
        elif C and old_v is not None:
            db_checks.append({"title": f"{C} = '{old_v}' 인 행 건수",
                              "sql": f"SELECT COUNT(*) AS CNT FROM {T} WHERE {C} = '{str(old_v).replace(chr(39), chr(39) * 2)}'"})
        if C:
            db_checks.append({"title": f"{C} 값 분포 (상위 20개)",
                              "sql": f"SELECT {C}, COUNT(*) AS CNT FROM {T} GROUP BY {C} ORDER BY CNT DESC FETCH FIRST 20 ROWS ONLY"})


def _db_encoding() -> str:
    return "utf-8" if config.DB_BYTES_PER_CHAR >= 3 else "cp949"


def _downstream(idx: ProjectIndex, findings: List[Dict], s: Dict) -> List[Dict]:
    """영향받은 함수(와 그 함수가 부르는 함수)가 쓰는 테이블 → 그 테이블을 읽는 다른 프로그램."""
    seen_funcs = {f"{f['program']}.{f['function']}" for f in findings if f["severity"] in ("ERROR", "WARN")}
    flows, seen = [], set()
    for fk in seen_funcs:
        if fk not in idx.functions:
            continue
        closure = idx.callees_closure(fk)
        for ck in closure:
            fn = idx.functions[ck]
            for sk in fn.get("statements") or []:
                st = idx.statements.get(f"{fn['program']}:{sk.split(':')[-1]}")
                if not st:
                    continue
                for t in st.get("tables") or []:
                    if "W" not in t["access"] or t["name"] == s["table"] and s["change"] != "data":
                        continue
                    key = (fn["program"], t["name"])
                    if key in seen:
                        continue
                    seen.add(key)
                    my_rank = _batch_rank(idx, fn["program"])
                    readers = []
                    for u in idx.table_usage.get(t["name"], []):
                        if u["program"] == fn["program"] or "R" not in u["access"] or u["kind"] in ("cursor", "close"):
                            continue
                        rank = _batch_rank(idx, u["program"])
                        readers.append({"program": u["program"], "function": u["function"], "line": u["line"],
                                        "kind": u["kind"], "after": rank > my_rank and my_rank < 10 ** 6,
                                        "batch_order": rank if rank < 10 ** 6 else None})
                    uniq = {}
                    for r in readers:
                        uniq.setdefault((r["program"], r["function"]), r)
                    if not uniq:
                        continue
                    flows.append({
                        "from_program": fn["program"], "from_function": fn["name"], "table": t["name"],
                        "stmt_line": st["line"], "via": fk.split(".", 1)[1],
                        "readers": sorted(uniq.values(), key=lambda r: (not r["after"], r["batch_order"] or 10 ** 6,
                                                                       r["program"]))[:20],
                    })
    return flows


# ---------- DB 연동 (필요한 테이블만 그때그때) ----------

def related_tables(idx: ProjectIndex, table: str, cap: int = 300) -> List[str]:
    """table 과 함께 분석해야 할 테이블: table 을 쓰는 프로그램들이 읽고 쓰는 모든 테이블."""
    t = table.upper()
    progs = {u["program"] for u in idx.table_usage.get(t, [])}
    names = [t]
    for st in idx.statements.values():
        if st["program"] in progs:
            for tb in st.get("tables") or []:
                names.append(tb["name"])
            for ln in st.get("lineage") or []:
                if ln.get("target_table"):
                    names.append(ln["target_table"])
    return list(dict.fromkeys(names))[:cap]


def analyze_impact_live(idx: ProjectIndex, scenario: Dict) -> Dict:
    """DB 가 연결되어 있으면 필요한 테이블 메타데이터만 먼저 읽어 오고(캐시), DB 안의 프로시저·트리거 사용처도 붙인다."""
    notes: List[str] = []
    s = normalize_scenario(scenario)
    if config.db_configured():
        try:
            r = ensure_tables(related_tables(idx, s["table"]))
            if r["fetched"]:
                notes.append(f"DB 에서 테이블 {len(r['fetched'])}개의 정보를 읽어 왔습니다: {', '.join(r['fetched'][:10])}"
                             + (" …" if len(r["fetched"]) > 10 else ""))
            if s["table"] in r["not_found"]:
                notes.append(f"DB 에서 {s['table']} 를 찾지 못했습니다. (스키마 · 시노님 · 조회 권한 확인)")
        except DbError as e:
            notes.append(f"DB 메타데이터를 읽지 못해 저장된 정보로 분석했습니다. ({e.message})")
    result = analyze_impact(idx, s)
    result["db_objects"] = []
    if config.db_configured():
        try:
            col = s.get("column") if s["change"] not in ("drop_table", "rename_table") else None
            objs = db_dependents(s["table"], col)
            result["db_objects"] = objs
            if objs:
                using = [o for o in objs if o.get("uses_column")]
                notes.append(f"DB 안에서 {s['table']} 를 참조하는 오브젝트 {len(objs)}개"
                             + (f" (그중 {col} 를 쓰는 소스 {len(using)}개)" if col else "")
                             + " — Pro*C 와 별도로 확인이 필요합니다.")
        except DbError as e:
            notes.append(f"DB 프로시저·트리거 사용처를 조회하지 못했습니다. ({e.message})")
    result["notes"] = notes + list(result.get("notes") or [])
    return result
