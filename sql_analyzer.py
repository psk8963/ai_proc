"""[A02] EXEC SQL 문 한 개를 분석한다.

정식 SQL 문법 파서가 아니라, 영향도 분석에 필요한 정보만 뽑는 토큰 스캐너다.
- 어떤 테이블을 읽고(R) 쓰는지(W)
- 어떤 컬럼을 어느 절(select / where / set / insert ...)에서 쓰는지
- 컬럼 ↔ 호스트 변수 연결(SELECT INTO, FETCH, INSERT VALUES, UPDATE SET, WHERE 비교)
- WHERE / DECODE / CASE 에서 컬럼과 비교하는 상수(리터럴)
- INSERT ... SELECT, UPDATE SET 으로 생기는 테이블 간 데이터 흐름(lineage)

컬럼이 어느 테이블 소속인지 SQL만으로 알 수 없으면 candidates(후보 테이블 목록)로 남기고,
DB 메타데이터가 있을 때 impact_engine 에서 확정한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ---------- 토큰 ----------


@dataclass
class Tok:
    kind: str          # id | str | num | host | op
    value: str         # id 는 대문자, host 는 변수명(원래 대소문자)
    pos: int = 0
    indicator: str = ""  # host 토큰의 indicator 변수


_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<str>'(?:[^']|'')*'?)
  | (?P<qid>"[^"]*"?)
  | (?P<host>:\s*[A-Za-z_][\w]*(?:\s*(?:\.|->)\s*[A-Za-z_]\w*)*)
  | (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)
  | (?P<id>[A-Za-z_][\w$#]*(?:@[\w$#.]+)?)
  | (?P<op><>|!=|\^=|>=|<=|\|\||=>|\(\+\)|[(),.;=<>+\-*/%])
  | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)


def tokenize(sql: str) -> List[Tok]:
    toks: List[Tok] = []
    for m in _TOKEN_RE.finditer(sql):
        kind = m.lastgroup
        text = m.group()
        if kind in ("ws", "other"):
            continue
        if kind == "qid":
            toks.append(Tok("id", text.strip('"').upper(), m.start()))
        elif kind == "id":
            toks.append(Tok("id", text.upper(), m.start()))
        elif kind == "host":
            name = re.sub(r"\s+", "", text[1:]).replace("->", ".")
            toks.append(Tok("host", name, m.start()))
        elif kind == "op" and text == "(+)":
            continue  # 옛 외부조인 표기는 분석에 영향 없음
        else:
            toks.append(Tok(kind, text, m.start()))
    # :host:ind  /  :host INDICATOR :ind  → indicator 로 합친다
    merged: List[Tok] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.kind == "host":
            if i + 1 < len(toks) and toks[i + 1].kind == "host" and _adjacent_host(t, toks[i + 1], sql):
                t.indicator = toks[i + 1].value
                i += 1
            elif (i + 2 < len(toks) and toks[i + 1].kind == "id" and toks[i + 1].value == "INDICATOR"
                  and toks[i + 2].kind == "host"):
                t.indicator = toks[i + 2].value
                i += 2
        merged.append(t)
        i += 1
    return merged


def _adjacent_host(a: Tok, b: Tok, sql: str) -> bool:
    """`:a:b` 처럼 공백 없이 붙어 있으면 b 는 indicator 다. (`:a, :b` 는 별개 변수)"""
    between = sql[a.pos + 1:b.pos]
    return "," not in between and len(between.strip()) <= len(a.value) + 1


# ---------- 키워드 ----------

KEYWORDS = set("""
SELECT FROM WHERE AND OR NOT IN IS NULL LIKE BETWEEN EXISTS INTO VALUES INSERT UPDATE DELETE MERGE SET
AS ON JOIN INNER LEFT RIGHT FULL OUTER CROSS NATURAL USING GROUP BY ORDER HAVING UNION ALL MINUS INTERSECT
DISTINCT UNIQUE CASE WHEN THEN ELSE END ASC DESC NULLS FIRST LAST FOR OF NOWAIT WAIT SKIP LOCKED
CONNECT START WITH PRIOR LEVEL ROWNUM ROWID SYSDATE SYSTIMESTAMP USER UID DUAL TRUE FALSE
DECLARE CURSOR OPEN FETCH CLOSE COMMIT ROLLBACK WORK RELEASE SAVEPOINT PREPARE EXECUTE IMMEDIATE
DESCRIPTOR BEGIN SECTION INCLUDE WHENEVER SQLERROR SQLWARNING FOUND CONTINUE GOTO GO TO STOP DO BREAK
CALL CONNECT IDENTIFIED TYPE VAR TABLE MATCHED ROWS ONLY FETCH NEXT OFFSET PERCENT TIES
NEXTVAL CURRVAL INTERVAL DAY MONTH YEAR HOUR MINUTE SECOND DATE TIMESTAMP CHAR VARCHAR2 NUMBER
OVER PARTITION ROWS RANGE UNBOUNDED PRECEDING FOLLOWING CURRENT ROW KEEP DENSE_RANK
RETURNING RETURN INDICATOR ESCAPE ANY SOME SIBLINGS NOCYCLE SAMPLE MODEL PIVOT UNPIVOT LATERAL
""".split())

_FUNC_NULL_SAFE = {"NVL", "NVL2", "COALESCE", "DECODE", "COUNT", "SUM", "MAX", "MIN", "AVG"}
_COMPARE_OPS = {"=", "<>", "!=", "^=", "<", ">", "<=", ">=", "LIKE", "IN"}
_TABLE_LIST_END = {"WHERE", "GROUP", "ORDER", "HAVING", "CONNECT", "START", "UNION", "MINUS", "INTERSECT",
                   "FOR", "WHEN", "SET", "VALUES", "RETURNING", "INTO", "ON", "USING", "JOIN", "INNER",
                   "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "FETCH", "OFFSET", "MODEL", "PIVOT", "UNPIVOT",
                   "SELECT", "WITH", "THEN"}
_CLAUSE_WORDS = {"SELECT": "select", "FROM": "from", "WHERE": "where", "ON": "where", "HAVING": "where",
                 "CONNECT": "where", "START": "where", "SET": "set", "VALUES": "value", "GROUP": "group",
                 "ORDER": "order", "INTO": "into", "USING": "from", "WHEN": "where", "RETURNING": "returning"}


def _is_name(t: Optional[Tok]) -> bool:
    return t is not None and t.kind == "id" and t.value not in KEYWORDS


def _match_paren(toks: List[Tok], i: int) -> int:
    """toks[i] 가 '(' 일 때 짝이 되는 ')' 위치. 없으면 마지막 위치."""
    depth = 0
    for j in range(i, len(toks)):
        if toks[j].value == "(" and toks[j].kind == "op":
            depth += 1
        elif toks[j].value == ")" and toks[j].kind == "op":
            depth -= 1
            if depth == 0:
                return j
    return len(toks) - 1


def _split_commas(toks: List[Tok]) -> List[List[Tok]]:
    """괄호 바깥 쉼표 기준으로 나눈다."""
    parts, cur, depth = [], [], 0
    for t in toks:
        if t.kind == "op" and t.value == "(":
            depth += 1
        elif t.kind == "op" and t.value == ")":
            depth -= 1
        if t.kind == "op" and t.value == "," and depth == 0:
            parts.append(cur)
            cur = []
            continue
        cur.append(t)
    if cur:
        parts.append(cur)
    return parts


# ---------- 결과 ----------


@dataclass
class SqlInfo:
    kind: str
    cursor: str = ""
    dynamic: bool = False
    tables: List[Dict] = None
    columns: List[Dict] = None
    bindings: List[Dict] = None
    literals: List[Dict] = None
    lineage: List[Dict] = None
    into_hosts: List[Dict] = None      # FETCH 처럼 select list 를 나중에 붙여야 하는 INTO 목록
    select_items: List[Dict] = None    # DECLARE CURSOR 의 select list (FETCH 연결용)
    insert_no_columns: bool = False
    select_star: bool = False
    whenever: Dict = None
    host_refs: List[str] = None

    def to_dict(self) -> Dict:
        return {k: v for k, v in self.__dict__.items()}


def classify(toks: List[Tok]) -> Tuple[str, int]:
    """문장 종류와 실제 SQL 이 시작하는 토큰 위치."""
    i = 0
    # EXEC SQL AT db_name ... / EXEC SQL FOR :n INSERT ...
    if i < len(toks) and toks[i].value == "AT" and i + 1 < len(toks):
        i += 2
    if i < len(toks) and toks[i].value == "FOR" and i + 1 < len(toks) and toks[i + 1].kind in ("host", "num"):
        i += 2
    if i >= len(toks):
        return "empty", i
    v = toks[i].value
    nxt = toks[i + 1].value if i + 1 < len(toks) else ""
    if v == "SELECT" or v == "WITH":
        return "select", i
    if v == "DECLARE":
        if "CURSOR" in [t.value for t in toks[i:i + 4]]:
            return "cursor", i
        if nxt == "SECTION" or nxt in ("BEGIN",):
            return "declare_section", i
        return "declare", i
    if v == "BEGIN" and nxt == "DECLARE":
        return "declare_section", i
    if v == "END" and nxt == "DECLARE":
        return "declare_section", i
    mapping = {"INSERT": "insert", "UPDATE": "update", "DELETE": "delete", "MERGE": "merge",
               "OPEN": "open", "FETCH": "fetch", "CLOSE": "close", "COMMIT": "commit", "ROLLBACK": "rollback",
               "WHENEVER": "whenever", "INCLUDE": "include", "CONNECT": "connect", "PREPARE": "prepare",
               "EXECUTE": "execute", "CALL": "call", "SAVEPOINT": "commit", "TYPE": "other", "VAR": "other",
               "LOCK": "lock", "TRUNCATE": "truncate", "ALTER": "ddl", "CREATE": "ddl", "DROP": "ddl",
               "CONTEXT": "other", "ENABLE": "other", "ALLOCATE": "other", "FREE": "other",
               "DESCRIBE": "other", "GET": "other", "OBJECT": "other", "ARRAYLEN": "other"}
    kind = mapping.get(v, "other")
    if kind == "execute" and nxt in ("BEGIN", "DECLARE"):
        kind = "plsql"
    elif kind == "execute" and nxt == "IMMEDIATE":
        kind = "execute_immediate"
    return kind, i


class _Analyzer:
    def __init__(self, toks: List[Tok]):
        self.t = toks
        self.tables: List[Dict] = []
        self.alias: Dict[str, str] = {}
        self.skip_idx = set()     # 테이블명/별칭/컬럼별칭 처럼 컬럼이 아닌 토큰 위치
        self.columns: List[Dict] = []
        self.bindings: List[Dict] = []
        self.literals: List[Dict] = []
        self.lineage: List[Dict] = []
        self.exclude_unqualified = None   # INSERT ... SELECT 에서 SELECT 쪽 컬럼은 대상 테이블 소속이 아니다

    # ----- 테이블 -----
    def add_table(self, idx: int, access: str) -> int:
        """idx 위치의 [schema.]table [alias] 를 등록하고 다음 위치를 돌려준다."""
        t = self.t
        if idx >= len(t):
            return idx
        if t[idx].kind == "op" and t[idx].value == "(":
            end = _match_paren(t, idx)
            nxt = end + 1
            if nxt < len(t) and _is_name(t[nxt]):
                self.skip_idx.add(nxt)
                self.alias[t[nxt].value] = ""  # 인라인 뷰 별칭 (실제 테이블 아님)
                nxt += 1
            return nxt
        if t[idx].kind != "id" or t[idx].value in ("TABLE", "LATERAL"):
            return idx + 1
        name_idx = idx
        name = t[idx].value
        self.skip_idx.add(idx)
        j = idx + 1
        if j + 1 < len(t) and t[j].value == "." and t[j + 1].kind == "id":
            name = t[j + 1].value  # schema.table → table 만 쓴다(시노님/스키마 차이 무시)
            self.skip_idx.update({j, j + 1})
            j += 2
        name = name.split("@")[0]
        if j < len(t) and t[j].value == "AS":
            j += 1
        if j < len(t) and _is_name(t[j]) and t[j].value not in _TABLE_LIST_END:
            self.alias[t[j].value] = name
            self.skip_idx.add(j)
            j += 1
        if name != "DUAL":
            self.tables.append({"name": name, "access": access, "pos": name_idx})
        self.alias.setdefault(name, name)
        return j

    def scan_tables(self, kind: str, start: int):
        t = self.t
        i = start
        while i < len(t):
            tok = t[i]
            if tok.kind != "id":
                i += 1
                continue
            v = tok.value
            prev = t[i - 1].value if i > start else ""
            if v == "FROM" and prev != "DELETE":
                j = i + 1
                while True:
                    j = self.add_table(j, "R")
                    if j < len(t) and t[j].value == ",":
                        j += 1
                        continue
                    break
            elif v == "JOIN" or (v == "USING" and kind == "merge" and prev not in (")",)):
                self.add_table(i + 1, "R")
            elif v == "UPDATE" and i == start:
                self.add_table(i + 1, "W")
            elif v == "DELETE" and i == start:
                j = i + 1
                if j < len(t) and t[j].value == "FROM":
                    j += 1
                self.add_table(j, "W")
            elif v == "INTO" and prev in ("INSERT", "MERGE"):
                self.add_table(i + 1, "W")
            elif v == "TRUNCATE" and i == start:
                j = i + 1
                if j < len(t) and t[j].value == "TABLE":
                    j += 1
                self.add_table(j, "W")
            elif v == "LOCK" and i == start:
                j = i + 1
                if j < len(t) and t[j].value == "TABLE":
                    j += 1
                self.add_table(j, "R")
            i += 1
        # 같은 테이블이 R/W 로 여러 번 나오면 W 우선으로 한 번만
        seen: Dict[str, Dict] = {}
        for tb in self.tables:
            cur = seen.get(tb["name"])
            if cur is None:
                seen[tb["name"]] = {"name": tb["name"], "access": tb["access"]}
            elif tb["access"] == "W":
                cur["access"] = "RW" if cur["access"] == "R" else cur["access"]
            elif cur["access"] == "W":
                cur["access"] = "RW"
        self.table_summary = list(seen.values())

    # ----- 컬럼 -----
    def real_tables(self) -> List[str]:
        return list(dict.fromkeys(tb["name"] for tb in self.tables))

    def colref_at(self, i: int) -> Optional[Tuple[Optional[str], str, int]]:
        """i 위치에서 시작하는 컬럼 참조. (테이블 또는 None, 컬럼, 다음 위치)"""
        t = self.t
        if i >= len(t) or i in self.skip_idx or not _is_name(t[i]):
            return None
        if i + 1 < len(t) and t[i + 1].value == "(":
            return None  # 함수 호출
        if i > 0 and t[i - 1].value == ".":
            return None  # a.b 의 b — a 쪽에서 처리
        if i + 2 < len(t) and t[i + 1].value == "." and t[i + 2].kind == "id":
            first, second = t[i].value, t[i + 2].value
            if second in ("NEXTVAL", "CURRVAL"):
                return None
            # schema.table.column
            if i + 4 < len(t) and t[i + 3].value == "." and t[i + 4].kind == "id":
                return (second, t[i + 4].value, i + 5)
            if first in self.alias:
                return (self.alias[first] or None, second, i + 3) if self.alias[first] else ("", second, i + 3)
            return (first, second, i + 3)
        return (None, t[i].value, i + 1)

    def _col_entry(self, table: Optional[str], col: str) -> Dict:
        if table == "":
            return {"table": None, "candidates": [], "column": col, "derived": True}
        if table:
            return {"table": table, "candidates": [table], "column": col}
        cands = [c for c in self.real_tables() if c != self.exclude_unqualified] or self.real_tables()
        return {"table": cands[0] if len(cands) == 1 else None, "candidates": cands, "column": col}

    def scan_columns(self, start: int):
        t = self.t
        stack: List[str] = []
        clause = ""
        i = start
        while i < len(t):
            tok = t[i]
            if tok.kind == "op" and tok.value == "(":
                stack.append(clause)
            elif tok.kind == "op" and tok.value == ")":
                clause = stack.pop() if stack else clause
            elif tok.kind == "id" and tok.value in _CLAUSE_WORDS and i not in self.skip_idx:
                clause = _CLAUSE_WORDS[tok.value]
                if tok.value == "INTO" and i > 0 and t[i - 1].value in ("INSERT", "MERGE"):
                    clause = "insert"
            ref = self.colref_at(i)
            if ref:
                table, col, nxt = ref
                role = clause or "select"
                if role in ("from", "into"):
                    i = nxt
                    continue
                entry = self._col_entry(table, col)
                entry["role"] = role
                self.columns.append(entry)
                i = nxt
                continue
            i += 1

    # ----- SELECT 목록 -----
    def select_items(self, sel_idx: int) -> Tuple[List[List[Tok]], int]:
        """sel_idx(SELECT) 다음부터 같은 깊이의 INTO/FROM 전까지 항목을 나눈다."""
        t = self.t
        j = sel_idx + 1
        if j < len(t) and t[j].value in ("DISTINCT", "UNIQUE", "ALL"):
            j += 1
        depth, end = 0, len(t)
        for k in range(j, len(t)):
            if t[k].value == "(":
                depth += 1
            elif t[k].value == ")":
                depth -= 1
            elif depth == 0 and t[k].kind == "id" and t[k].value in ("INTO", "FROM"):
                end = k
                break
        items = _split_commas(t[j:end])
        # 컬럼 별칭 표시: expr AS alias / expr alias
        for item in items:
            if len(item) >= 2 and _is_name(item[-1]):
                before = item[-2]
                if before.value == "AS" or before.kind in ("id", "str", "num") or before.value == ")":
                    if not (before.value == "." ):
                        self.skip_idx.add(self.t.index(item[-1]))
        return items, end

    def item_source(self, item: List[Tok]) -> Optional[Dict]:
        """select 항목에서 원본 컬럼(첫 번째 컬럼 참조)과 식 여부를 돌려준다."""
        if not item:
            return None
        if len(item) == 1 and item[0].value == "*":
            return {"star": True}
        base = self.t.index(item[0])
        simple = None
        ref = self.colref_at(base)
        if ref and ref[2] - base == len(item):
            simple = ref
        elif ref and ref[2] - base == len(item) - 1 and _is_name(item[-1]):
            simple = ref  # col alias
        elif ref and len(item) >= 2 and item[-2].value == "AS" and ref[2] - base == len(item) - 2:
            simple = ref
        if simple:
            e = self._col_entry(simple[0], simple[1])
            e.update({"expr": None, "null_safe": False})
            return e
        # 식: 첫 컬럼 참조를 원본으로
        func = item[0].value if item[0].kind == "id" and len(item) > 1 and item[1].value == "(" else ""
        for k in range(len(item)):
            r = self.colref_at(base + k)
            if r:
                e = self._col_entry(r[0], r[1])
                e.update({
                    "expr": " ".join(x.value for x in item)[:120],
                    "null_safe": func in _FUNC_NULL_SAFE,
                    "func": func,
                })
                return e
        return {"expr": " ".join(x.value for x in item)[:120], "column": None, "table": None,
                "candidates": [], "null_safe": True, "func": func, "const": True}

    def host_list(self, start: int) -> List[Dict]:
        """start 위치부터 쉼표로 이어진 호스트 변수 목록."""
        t = self.t
        out = []
        j = start
        while j < len(t) and t[j].kind == "host":
            out.append({"host": t[j].value, "indicator": t[j].indicator})
            j += 1
            if j < len(t) and t[j].value == ",":
                j += 1
            else:
                break
        return out

    # ----- 비교 / 리터럴 -----
    def scan_comparisons(self, start: int):
        t = self.t
        clause_stack: List[str] = []
        clause = ""
        i = start
        while i < len(t):
            tok = t[i]
            if tok.kind == "op" and tok.value == "(":
                clause_stack.append(clause)
            elif tok.kind == "op" and tok.value == ")":
                clause = clause_stack.pop() if clause_stack else clause
            elif tok.kind == "id" and tok.value in _CLAUSE_WORDS and i not in self.skip_idx:
                clause = _CLAUSE_WORDS[tok.value]
            elif tok.kind == "id" and tok.value == "CASE":
                self._case(i)
            elif tok.kind == "id" and tok.value == "DECODE" and i + 1 < len(t) and t[i + 1].value == "(":
                self._decode(i + 1)
            ref = self.colref_at(i)
            if ref and clause in ("where",):
                table, col, nxt = ref
                self._compare(table, col, nxt, before=i - 1)
                i = nxt
                continue
            i += 1

    def _operand(self, j: int) -> Tuple[Optional[Dict], int]:
        t = self.t
        if j >= len(t):
            return None, j
        if t[j].kind == "host":
            return {"host": t[j].value, "indicator": t[j].indicator}, j + 1
        if t[j].kind in ("str", "num"):
            return {"literal": _lit(t[j])}, j + 1
        if t[j].kind == "id" and t[j].value == "NULL":
            return {"null": True}, j + 1
        if t[j].value == "(":
            end = _match_paren(t, j)
            inner = t[j + 1:end]
            if inner and all(x.kind in ("str", "num") or x.value == "," for x in inner):
                return {"literals": [_lit(x) for x in inner if x.kind in ("str", "num")]}, end + 1
            hosts = [x for x in inner if x.kind == "host"]
            if inner and all(x.kind == "host" or x.value == "," for x in inner):
                return {"hosts": [{"host": h.value, "indicator": h.indicator} for h in hosts]}, end + 1
        if t[j].kind == "id" and j + 1 < len(t) and t[j + 1].value == "(":
            # col = NVL(:h, ' ') / TO_DATE(:h, 'YYYYMMDD') 처럼 함수로 감싼 호스트 변수
            end = _match_paren(t, j + 1)
            hosts = [x for x in t[j + 2:end] if x.kind == "host"]
            if hosts:
                return {"hosts": [{"host": h.value, "indicator": h.indicator} for h in hosts[:1]],
                        "func": t[j].value}, end + 1
        return None, j

    def _compare(self, table, col, nxt, before):
        t = self.t
        entry_base = self._col_entry(table, col)
        if nxt < len(t):
            op_tok = t[nxt]
            op = op_tok.value
            neg = False
            j = nxt + 1
            if op == "NOT" and nxt + 1 < len(t) and t[nxt + 1].value in ("IN", "LIKE", "BETWEEN"):
                neg, op, j = True, t[nxt + 1].value, nxt + 2
            if op == "IS":
                is_not = j < len(t) and t[j].value == "NOT"
                self.literals.append(dict(entry_base, op="IS NOT NULL" if is_not else "IS NULL", values=[None],
                                          context="where"))
                return
            if op == "BETWEEN":
                a, j2 = self._operand(j)
                b, _ = self._operand(j2 + 1) if j2 < len(t) and t[j2].value == "AND" else (None, j2)
                for x in (a, b):
                    self._record_operand(entry_base, "NOT BETWEEN" if neg else "BETWEEN", x)
                return
            if op in _COMPARE_OPS or op in ("^=",):
                opnd, _ = self._operand(j)
                if opnd:
                    self._record_operand(entry_base, ("NOT " if neg else "") + op, opnd)
                return
        # 반대 방향:  :host = col  /  'A' = col
        if before >= 1 and t[before].value in _COMPARE_OPS:
            left = t[before - 1]
            if left.kind == "host":
                self._record_operand(entry_base, _flip(t[before].value), {"host": left.value, "indicator": left.indicator})
            elif left.kind in ("str", "num"):
                self._record_operand(entry_base, _flip(t[before].value), {"literal": _lit(left)})

    def _record_operand(self, entry_base: Dict, op: str, opnd: Optional[Dict], context: str = "where"):
        if not opnd:
            return
        if "host" in opnd:
            self.bindings.append(dict(entry_base, host=opnd["host"], indicator=opnd["indicator"], direction="filter",
                                      op=op, expr=None, null_safe=False))
        elif "hosts" in opnd:
            for h in opnd["hosts"]:
                self.bindings.append(dict(entry_base, host=h["host"], indicator=h["indicator"], direction="filter",
                                          op=op, expr=None, null_safe=False))
        elif "literal" in opnd:
            self.literals.append(dict(entry_base, op=op, values=[opnd["literal"]], context=context))
        elif "literals" in opnd:
            self.literals.append(dict(entry_base, op=op, values=opnd["literals"], context=context))
        elif opnd.get("null"):
            self.literals.append(dict(entry_base, op=op, values=[None], context=context))

    def _decode(self, lp: int):
        t = self.t
        end = _match_paren(t, lp)
        args = _split_commas(t[lp + 1:end])
        if len(args) < 3 or not args[0]:
            return
        base = t.index(args[0][0])
        ref = self.colref_at(base)
        if not ref or ref[2] - base != len(args[0]):
            return
        vals = []
        for k in range(1, len(args) - 1, 2):
            a = args[k]
            if len(a) == 1 and a[0].kind in ("str", "num"):
                vals.append(_lit(a[0]))
            elif len(a) == 1 and a[0].value == "NULL":
                vals.append(None)
        has_default = len(args) % 2 == 0
        if vals:
            e = self._col_entry(ref[0], ref[1])
            self.literals.append(dict(e, op="DECODE", values=vals, context="decode", has_default=has_default))

    def _case(self, ci: int):
        """CASE col WHEN 'A' ... (단순 CASE). 검색 CASE(CASE WHEN col = 'A')는 비교 스캔에서 잡는다."""
        t = self.t
        ref = self.colref_at(ci + 1)
        if not ref or ref[2] >= len(t) or t[ref[2]].value != "WHEN":
            return
        vals, depth, has_else = [], 0, False
        for k in range(ref[2], len(t)):
            v = t[k].value
            if v == "CASE":
                depth += 1
            elif v == "END":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and v == "WHEN" and k + 1 < len(t) and t[k + 1].kind in ("str", "num"):
                vals.append(_lit(t[k + 1]))
            elif depth == 0 and v == "ELSE":
                has_else = True
        if vals:
            e = self._col_entry(ref[0], ref[1])
            self.literals.append(dict(e, op="CASE", values=vals, context="decode", has_default=has_else))


def _lit(t: Tok):
    if t.kind == "str":
        return t.value[1:-1].replace("''", "'") if t.value.endswith("'") and len(t.value) >= 2 else t.value[1:]
    return t.value


def _flip(op: str) -> str:
    return {"<": ">", ">": "<", "<=": ">=", ">=": "<="}.get(op, op)


# ---------- 진입점 ----------

_WHENEVER_RE = re.compile(r"^\s*WHENEVER\s+(SQLERROR|NOT\s+FOUND|SQLWARNING)\s+(.+?)\s*$", re.I | re.S)


def analyze_sql(sql: str) -> Dict:
    """EXEC SQL 뒤의 문장(세미콜론 제외)을 분석해 dict 로 돌려준다."""
    text = sql.strip()
    m = _WHENEVER_RE.match(text)
    if m:
        cond = re.sub(r"\s+", " ", m.group(1).upper())
        action = re.sub(r"\s+", " ", m.group(2)).strip()
        return SqlInfo(kind="whenever", whenever={"condition": cond, "action": action}, tables=[], columns=[],
                       bindings=[], literals=[], lineage=[], host_refs=[]).to_dict()

    toks = tokenize(text)
    kind, start = classify(toks)
    info = SqlInfo(kind=kind, tables=[], columns=[], bindings=[], literals=[], lineage=[], host_refs=[])
    info.host_refs = list(dict.fromkeys(t.value for t in toks if t.kind == "host"))

    if kind in ("include", "declare_section", "commit", "rollback", "connect", "whenever", "other", "empty",
                "close", "declare", "ddl"):
        if kind == "close" and start + 1 < len(toks):
            info.cursor = toks[start + 1].value
        return info.to_dict()

    if kind in ("prepare", "execute_immediate", "execute"):
        info.dynamic = True
        if kind == "prepare" and start + 1 < len(toks):
            info.cursor = toks[start + 1].value
        # EXECUTE IMMEDIATE 'literal SQL' 은 문자열 안을 다시 분석한다
        if kind == "execute_immediate" and start + 2 < len(toks) and toks[start + 2].kind == "str":
            inner = analyze_sql(_lit(toks[start + 2]))
            inner["kind"] = "execute_immediate"
            inner["dynamic"] = True
            return inner
        return info.to_dict()

    if kind == "plsql":
        return _analyze_plsql(text, info)

    if kind == "open":
        if start + 1 < len(toks):
            info.cursor = toks[start + 1].value
        return info.to_dict()

    if kind == "fetch":
        j = start + 1
        if j < len(toks) and toks[j].kind == "id":
            info.cursor = toks[j].value
            j += 1
        while j < len(toks) and not (toks[j].kind == "id" and toks[j].value in ("INTO", "USING")):
            j += 1
        if j < len(toks) and toks[j].value == "INTO":
            info.into_hosts = _Analyzer(toks).host_list(j + 1)
        return info.to_dict()

    a = _Analyzer(toks)
    body_start = start
    if kind == "cursor":
        # DECLARE c CURSOR FOR <select> | <stmt_name>
        info.cursor = toks[start + 1].value if start + 1 < len(toks) else ""
        k = start
        while k < len(toks) and toks[k].value != "FOR":
            k += 1
        body_start = k + 1
        a.skip_idx.update(range(start, body_start))
        if body_start < len(toks) and toks[body_start].kind == "id" and toks[body_start].value not in ("SELECT", "WITH"):
            info.dynamic = True
            return info.to_dict()

    a.scan_tables(kind, body_start)
    a.scan_columns(body_start)

    if kind in ("select", "cursor"):
        sel = _first_top_select(toks, body_start)
        if sel is not None:
            items, end = a.select_items(sel)
            sources = [a.item_source(it) for it in items]
            info.select_star = any(s and s.get("star") for s in sources)
            info.select_items = [s for s in sources]
            # 컬럼 별칭으로 표시된 토큰은 columns 에서 뺀다
            a.columns = []
            a.scan_columns(body_start)
            if kind == "select" and end < len(toks) and toks[end].value == "INTO":
                hosts = a.host_list(end + 1)
                for src, h in zip(sources, hosts):
                    if src and not src.get("star") and src.get("column"):
                        a.bindings.append(dict(src, host=h["host"], indicator=h["indicator"], direction="out"))
                info.into_hosts = hosts

    if kind == "insert":
        target = next((tb["name"] for tb in a.tables if tb["access"] == "W"), None)
        if target and any(tb["access"] == "R" for tb in a.tables):
            a.exclude_unqualified = target
            a.columns = []
            a.scan_columns(body_start)
        _insert_bindings(a, toks, body_start, info)

    if kind == "update":
        _update_bindings(a, toks, body_start)

    a.scan_comparisons(body_start)

    info.tables = a.table_summary
    info.columns = _dedupe(a.columns, ("table", "column", "role"))
    info.bindings = a.bindings
    info.literals = a.literals
    info.lineage = a.lineage
    return info.to_dict()


def _first_top_select(toks: List[Tok], start: int) -> Optional[int]:
    depth = 0
    for k in range(start, len(toks)):
        if toks[k].value == "(":
            depth += 1
        elif toks[k].value == ")":
            depth -= 1
        elif depth == 0 and toks[k].kind == "id" and toks[k].value == "SELECT":
            return k
    return None


def _insert_bindings(a: _Analyzer, toks: List[Tok], start: int, info: SqlInfo):
    target = next((tb["name"] for tb in a.tables if tb["access"] == "W"), None)
    # INSERT INTO t [alias] ( cols ) VALUES ( ... ) | SELECT ...
    k = start
    while k < len(toks) and toks[k].value != "INTO":
        k += 1
    k += 1
    if k < len(toks) and toks[k].kind == "id":
        k += 1
        if k + 1 < len(toks) and toks[k].value == "." and toks[k + 1].kind == "id":
            k += 2
        if k < len(toks) and _is_name(toks[k]):
            k += 1  # alias
    cols: Optional[List[str]] = None
    if k < len(toks) and toks[k].value == "(":
        end = _match_paren(toks, k)
        inner = toks[k + 1:end]
        if inner and inner[0].value not in ("SELECT", "WITH"):
            cols = [p[-1].value for p in _split_commas(inner) if p]
            for p in _split_commas(inner):
                if p:
                    a.columns.append({"table": target, "candidates": [target] if target else [], "column": p[-1].value,
                                      "role": "insert"})
            k = end + 1
    info.insert_no_columns = cols is None
    info.insert_columns = cols
    if k < len(toks) and toks[k].value == "VALUES" and k + 1 < len(toks) and toks[k + 1].value == "(":
        end = _match_paren(toks, k + 1)
        values = _split_commas(toks[k + 2:end])
        info.insert_values = [" ".join(x.value for x in v)[:80] for v in values]
        if cols:
            for c, v in zip(cols, values):
                hosts = [x for x in v if x.kind == "host"]
                expr = None if len(v) == 1 else " ".join(x.value for x in v)[:120]
                if hosts:
                    h = hosts[0]
                    a.bindings.append({"table": target, "candidates": [target] if target else [], "column": c,
                                       "host": h.value, "indicator": h.indicator, "direction": "in", "expr": expr,
                                       "null_safe": False})
                elif len(v) == 1 and v[0].value == "NULL":
                    a.literals.append({"table": target, "candidates": [target], "column": c, "op": "SET",
                                       "values": [None], "context": "insert"})
                elif len(v) == 1 and v[0].kind in ("str", "num"):
                    a.literals.append({"table": target, "candidates": [target], "column": c, "op": "SET",
                                       "values": [_lit(v[0])], "context": "insert"})
        else:
            info.insert_value_hosts = [
                ([x.value for x in v if x.kind == "host"] or [None])[0] for v in values
            ]
    else:
        sel = _first_top_select(toks, k)
        if sel is not None:
            items, _ = a.select_items(sel)
            srcs = [a.item_source(it) for it in items]
            info.insert_select_items = srcs
            if cols:
                for c, s in zip(cols, srcs):
                    if s and s.get("column"):
                        a.lineage.append({"target_table": target, "target_column": c,
                                          "source": {"table": s.get("table"), "candidates": s.get("candidates", []),
                                                     "column": s["column"], "expr": s.get("expr")}})
            info.insert_select = True


def _update_bindings(a: _Analyzer, toks: List[Tok], start: int):
    target = next((tb["name"] for tb in a.tables if tb["access"] in ("W", "RW")), None)
    k = start
    while k < len(toks) and toks[k].value != "SET":
        k += 1
    if k >= len(toks):
        return
    depth, end = 0, len(toks)
    for j in range(k + 1, len(toks)):
        if toks[j].value == "(":
            depth += 1
        elif toks[j].value == ")":
            depth -= 1
        elif depth == 0 and toks[j].kind == "id" and toks[j].value in ("WHERE", "RETURNING"):
            end = j
            break
    for part in _split_commas(toks[k + 1:end]):
        if "=" not in [x.value for x in part]:
            continue
        eq = [x.value for x in part].index("=")
        left, right = part[:eq], part[eq + 1:]
        if len(left) == 0:
            continue
        lcol = left[-1].value
        hosts = [x for x in right if x.kind == "host"]
        expr = None if len(right) == 1 else " ".join(x.value for x in right)[:120]
        if hosts and len(left) == 1 or (hosts and len(left) == 3 and left[1].value == "."):
            h = hosts[0]
            a.bindings.append({"table": target, "candidates": [target] if target else [], "column": lcol,
                               "host": h.value, "indicator": h.indicator, "direction": "in", "expr": expr,
                               "null_safe": False})
        elif len(right) == 1 and right[0].value == "NULL":
            a.literals.append({"table": target, "candidates": [target], "column": lcol, "op": "SET",
                               "values": [None], "context": "set"})
        elif len(right) == 1 and right[0].kind in ("str", "num"):
            a.literals.append({"table": target, "candidates": [target], "column": lcol, "op": "SET",
                               "values": [_lit(right[0])], "context": "set"})
        else:
            rb = toks.index(right[0]) if right else None
            srcs = []
            if rb is not None:
                for j in range(len(right)):
                    r = a.colref_at(rb + j)
                    if r:
                        e = a._col_entry(r[0], r[1])
                        srcs.append(e)
            for s in srcs:
                if not (s["column"] == lcol and (s["table"] in (None, target))):
                    a.lineage.append({"target_table": target, "target_column": lcol,
                                      "source": {"table": s.get("table"), "candidates": s.get("candidates", []),
                                                 "column": s["column"], "expr": expr}})
        # SET 왼쪽 컬럼의 role 을 set_target 으로 바꾼다
        for c in a.columns:
            if c["column"] == lcol and c.get("role") == "set" and not c.get("_marked"):
                c["role"] = "set_target"
                c["_marked"] = True
                break
    for c in a.columns:
        c.pop("_marked", None)


def _analyze_plsql(text: str, info: SqlInfo) -> Dict:
    """EXEC SQL EXECUTE BEGIN ... END; END-EXEC 블록. 안쪽 DML 을 문장별로 다시 분석해 합친다."""
    body = re.sub(r"^\s*EXECUTE\s+", "", text, flags=re.I)
    body = re.sub(r"END-EXEC\s*$", "", body, flags=re.I)
    tables: Dict[str, str] = {}
    for stmt in _split_plsql(body):
        s = stmt.strip()
        if not re.match(r"(SELECT|INSERT|UPDATE|DELETE|MERGE|WITH)\b", s, re.I):
            continue
        sub = analyze_sql(s)
        for tb in sub["tables"]:
            prev = tables.get(tb["name"])
            tables[tb["name"]] = tb["access"] if not prev or prev == tb["access"] else "RW"
        info.columns.extend(sub["columns"])
        info.bindings.extend(sub["bindings"])
        info.literals.extend(sub["literals"])
        info.lineage.extend(sub["lineage"])
    info.tables = [{"name": k, "access": v} for k, v in tables.items()]
    info.kind = "plsql"
    return info.to_dict()


def _split_plsql(body: str) -> List[str]:
    parts, cur, q = [], [], False
    for ch in body:
        if ch == "'":
            q = not q
        if ch == ";" and not q:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    out = []
    for p in parts:
        # BEGIN / DECLARE / IF ... THEN 같은 머리말 뒤의 DML 만 남긴다
        m = re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE)\b.*", p, re.I | re.S)
        if m:
            out.append(m.group())
    return out


def _dedupe(items: List[Dict], keys) -> List[Dict]:
    seen, out = set(), []
    for it in items:
        k = tuple(str(it.get(x)) for x in keys)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out
