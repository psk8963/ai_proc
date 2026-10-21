"""[A01] Pro*C 소스(.pc) / 헤더(.h) 정적 분석.

한 파일을 읽어서 다음을 뽑는다.
- C 함수 목록(시작·끝 줄, 호출하는 함수) — K&R 스타일 함수 정의도 인식
- 함수별 EXEC SQL 문(종류, 테이블 R/W, 컬럼, 호스트 변수 연결) — sql_analyzer 사용
- 호스트 변수 선언(char[n], VARCHAR[n], int/long/double, struct 멤버) 과 #define 상수
- 문장 위치의 WHENEVER 설정, 문장 직후의 sqlca.sqlcode / NOT FOUND 검사 여부
- 커서(DECLARE CURSOR) 를 OPEN / FETCH 문과 연결

정규식·토큰 기반 근사 분석이다. 매크로로 감싼 SQL, 조건부 컴파일(#ifdef) 분기는 구분하지 않는다.
"""

from __future__ import annotations

import bisect
import copy
import re
from typing import Dict, List, Optional, Tuple

from dynamic_sql import is_placeholder, reconstruct
from sql_analyzer import analyze_sql, tokenize

PARSER_VERSION = "2"

C_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "default", "return", "sizeof", "goto", "break",
    "continue", "typedef", "struct", "union", "enum", "static", "extern", "const", "volatile", "register",
    "auto", "signed", "unsigned", "defined", "__attribute__", "asm", "inline",
}
# 호출 목록에서 뺄 표준 라이브러리 함수(영향도 경로에 의미 없음)
C_LIB_FUNCS = set("""
printf fprintf sprintf snprintf vsprintf vsnprintf scanf sscanf fscanf puts fputs fgets gets putchar getchar
strcpy strncpy strcat strncat strcmp strncmp strlen strchr strrchr strstr strtok strdup memset memcpy memmove
memcmp malloc calloc realloc free exit abort atoi atol atof strtol strtoul strtod fopen fclose fread fwrite
fflush fseek ftell rewind remove rename time localtime gmtime strftime mktime ctime sleep system getenv
isdigit isalpha isspace toupper tolower abs labs qsort bsearch rand srand perror errno va_start va_end
sqlglm sqlgls sqlprc sqlnul sqlca main
""".split())

BASE_TYPES = r"(?:unsigned\s+|signed\s+)?(?:char|short(?:\s+int)?|int|long\s+long(?:\s+int)?|long(?:\s+int)?|float|double|VARCHAR|varchar|OCIDate|sql_cursor)"


# ---------- 공통 ----------

def decode_source(data: bytes) -> str:
    """한국어 레거시 소스는 EUC-KR(CP949) 인 경우가 많다."""
    for enc in ("utf-8", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("cp949", errors="replace")


def strip_comments(src: str) -> str:
    """주석을 공백으로 바꾼다(줄바꿈과 길이 유지). 따옴표 안은 건드리지 않는다."""
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c in ("'", '"'):
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                if src[j] == "\\" and c == '"':
                    j += 1
                j += 1
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _blank(chars: List[str], start: int, end: int):
    for k in range(start, min(end, len(chars))):
        if chars[k] != "\n":
            chars[k] = " "


def _strip_sql_line_comments(sql: str) -> str:
    out, q, i = [], False, 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            q = not q
        if not q and ch == "-" and sql[i:i + 2] == "--":
            j = sql.find("\n", i)
            i = len(sql) if j < 0 else j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class _Lines:
    def __init__(self, text: str):
        self.nl = [i for i, c in enumerate(text) if c == "\n"]

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.nl, offset - 1) + 1


# ---------- EXEC SQL 추출 ----------

_EXEC_RE = re.compile(r"\bEXEC\s+(SQL|ORACLE|TOOLS|IAF)\b", re.I)


def find_exec_sql(text: str) -> List[Tuple[int, int, str]]:
    """(시작, 끝, SQL 본문) 목록. 끝은 세미콜론 다음 위치."""
    out = []
    pos = 0
    while True:
        m = _EXEC_RE.search(text, pos)
        if not m:
            break
        start, body_start = m.start(), m.end()
        rest = text[body_start:body_start + 40]
        if re.match(r"\s*EXECUTE\s+(BEGIN|DECLARE)\b", rest, re.I) and m.group(1).upper() == "SQL":
            em = re.search(r"END-EXEC\s*;", text[body_start:], re.I)
            end = body_start + (em.end() if em else len(text) - body_start)
            body = text[body_start:end].rstrip().rstrip(";")
        else:
            j, q = body_start, False
            while j < len(text):
                ch = text[j]
                if ch == "'":
                    q = not q
                elif ch == ";" and not q:
                    break
                elif ch == "\n" and q and text[j - 1] != "\\":
                    pass
                j += 1
            end = min(j + 1, len(text))
            body = text[body_start:j]
        if m.group(1).upper() == "SQL":
            out.append((start, end, _strip_sql_line_comments(body)))
        pos = end
    return out


# ---------- 전처리기 / 선언 ----------

_DEFINE_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]+(.+?)[ \t]*$", re.M)
_INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"]+)[>"]', re.M)
_SQL_INCLUDE_RE = re.compile(r"^\s*INCLUDE\s+['\"]?([\w./\\-]+)", re.I)
_PP_LINE_RE = re.compile(r"^[ \t]*#.*?(?:\\\n.*?)*$", re.M)


def eval_size(expr: str, defines: Dict[str, str], depth: int = 0) -> Optional[int]:
    """배열 크기 식(예: LEN_NAME+1)을 계산한다. 계산할 수 없으면 None."""
    expr = expr.strip()
    if not expr:
        return None
    if depth > 5:
        return None

    def sub(m):
        name = m.group(0)
        if name in defines:
            v = eval_size(defines[name], defines, depth + 1)
            return str(v) if v is not None else name
        return name

    e = re.sub(r"[A-Za-z_]\w*", sub, expr)
    e = re.sub(r"(?<=\d)[uUlL]+", "", e)
    if not re.fullmatch(r"[\d\s+\-*/()]+", e) or "**" in e or len(e) > 60:
        return None
    try:
        v = eval(e, {"__builtins__": {}}, {})  # 숫자와 사칙연산만 통과한 식
        return int(v)
    except Exception:
        return None


_DECLARATOR_RE = re.compile(r"^\s*(\**)\s*([A-Za-z_]\w*)\s*((?:\[[^\]]*\]\s*)*)\s*(?:=.*)?$", re.S)


def parse_declarators(ctype: str, decl_text: str, defines: Dict[str, str], line: int) -> List[Dict]:
    out = []
    depth, cur, parts = 0, [], []
    for ch in decl_text:
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    for p in parts:
        m = _DECLARATOR_RE.match(p)
        if not m:
            continue
        ptr, name, dims_txt = m.group(1), m.group(2), m.group(3)
        exprs = re.findall(r"\[([^\]]*)\]", dims_txt)
        dims = [eval_size(d, defines) for d in exprs]
        hv = make_hostvar(ctype, name, dims, bool(ptr), line)
        hv["dims_expr"] = [e.strip() for e in exprs]
        out.append(hv)
    return out


def make_hostvar(ctype: str, name: str, dims: List[Optional[int]], pointer: bool, line: int) -> Dict:
    ct = re.sub(r"\s+", " ", ctype.strip()).lower()
    hv = {"name": name, "ctype": ct, "dims": dims, "pointer": pointer, "line": line}
    if ct == "varchar":
        hv["kind"] = "varchar"
        hv["size"] = dims[-1] if dims else None
        hv["array"] = dims[0] if len(dims) > 1 else None
        hv["capacity"] = hv["size"]
    elif ct.endswith("char") and dims:
        hv["kind"] = "string"
        hv["size"] = dims[-1]              # char[n] → n-1 글자 + 널 문자
        hv["array"] = dims[0] if len(dims) > 1 else None
        hv["capacity"] = dims[-1] - 1 if dims[-1] else None
    elif ct.endswith("char"):
        hv["kind"] = "char1"
        hv["size"] = 1
        hv["capacity"] = 1
        hv["array"] = None
    elif ct in ("float", "double"):
        hv["kind"] = "float"
        hv["array"] = dims[0] if dims else None
    elif "int" in ct or "short" in ct or "long" in ct:
        hv["kind"] = "int"
        hv["int_bits"] = 16 if "short" in ct else (64 if "long long" in ct else (32 if ct in ("int", "unsigned int", "signed int") else 0))
        # long 은 64bit 유닉스(LP64)=64bit, Windows=32bit 로 플랫폼마다 다르다 → 0(미정)으로 둔다
        hv["array"] = dims[0] if dims else None
    else:
        hv["kind"] = "other"
        hv["array"] = dims[0] if dims else None
    if pointer:
        hv["capacity"] = None  # 포인터는 크기를 알 수 없다
    return hv


_STRUCT_RE = re.compile(r"\b(typedef\s+)?struct\s*([A-Za-z_]\w*)?\s*\{([^{}]*)\}\s*([^;{}]*);", re.S)


def parse_decls(text: str, base_offset: int, lines: _Lines, defines: Dict[str, str], typedefs: Dict[str, Dict],
                structs: Dict[str, Dict]) -> Dict[str, Dict]:
    """text(이미 주석·문자열·EXEC SQL 이 지워진 C 코드)에서 변수 선언을 찾는다."""
    found: Dict[str, Dict] = {}
    chars = list(text)

    for m in _STRUCT_RE.finditer(text):
        is_typedef, tag, body, names = m.group(1), m.group(2), m.group(3), m.group(4)
        members = {}
        for mm in re.finditer(r"(?:^|[;\s])(" + BASE_TYPES + r")\s+([^;]+);", body):
            for hv in parse_declarators(mm.group(1), mm.group(2), defines, lines.line(base_offset + m.start())):
                members[hv["name"]] = hv
        if tag:
            structs[tag] = members
        line = lines.line(base_offset + m.start())
        if is_typedef:
            for nm in re.findall(r"[A-Za-z_]\w*", names):
                typedefs[nm] = members
        else:
            for d in names.split(","):
                dm = _DECLARATOR_RE.match(d)
                if dm:
                    dims = [eval_size(x, defines) for x in re.findall(r"\[([^\]]*)\]", dm.group(3))]
                    found[dm.group(2)] = {"name": dm.group(2), "ctype": "struct", "kind": "struct", "dims": dims,
                                          "array": dims[0] if dims else None, "members": members, "line": line,
                                          "pointer": bool(dm.group(1))}
        _blank(chars, m.start(), m.end())
    text2 = "".join(chars)

    # struct tag var;  /  TypedefName var;
    user_types = list(typedefs.keys())
    for m in re.finditer(r"(?:^|[;{}(\s])struct\s+([A-Za-z_]\w*)\s+([^;(){}]+);", text2):
        members = structs.get(m.group(1), {})
        for d in m.group(2).split(","):
            dm = _DECLARATOR_RE.match(d)
            if dm:
                dims = [eval_size(x, defines) for x in re.findall(r"\[([^\]]*)\]", dm.group(3))]
                found[dm.group(2)] = {"name": dm.group(2), "ctype": "struct " + m.group(1), "kind": "struct",
                                      "dims": dims, "array": dims[0] if dims else None, "members": members,
                                      "line": lines.line(base_offset + m.start()), "pointer": bool(dm.group(1)),
                                      "struct_tag": m.group(1)}
    if user_types:
        ut = "|".join(re.escape(u) for u in sorted(user_types, key=len, reverse=True))
        for m in re.finditer(r"(?:^|[;{}\s])(" + ut + r")\s+([^;(){}]+);", text2):
            members = typedefs.get(m.group(1), {})
            for d in m.group(2).split(","):
                dm = _DECLARATOR_RE.match(d)
                if dm:
                    dims = [eval_size(x, defines) for x in re.findall(r"\[([^\]]*)\]", dm.group(3))]
                    found[dm.group(2)] = {"name": dm.group(2), "ctype": m.group(1), "kind": "struct", "dims": dims,
                                          "array": dims[0] if dims else None, "members": members,
                                          "line": lines.line(base_offset + m.start()), "pointer": bool(dm.group(1))}

    decl_re = re.compile(r"(?:^|[;{}\s])((?:(?:static|extern|const|register|volatile)\s+)*)(" + BASE_TYPES + r")\s+([^;(){}]+);")
    for m in decl_re.finditer(text2):
        for hv in parse_declarators(m.group(2), m.group(3), defines, lines.line(base_offset + m.start(2))):
            found.setdefault(hv["name"], hv)
    return found


# ---------- 함수 ----------

_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_KR_TAIL_RE = re.compile(r"(?:\s*[A-Za-z_][\w\s\*\[\],]*;)+\s*")


def _function_name(header: str) -> Optional[str]:
    """'{' 바로 앞 텍스트에서 함수 이름을 찾는다. (ANSI / K&R 모두)"""
    for m in reversed(list(_CALL_RE.finditer(header))):
        name = m.group(1)
        if name in C_KEYWORDS:
            continue
        depth, j = 0, m.end() - 1
        while j < len(header):
            if header[j] == "(":
                depth += 1
            elif header[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            continue
        tail = header[j + 1:]
        if tail.strip() == "" or _KR_TAIL_RE.fullmatch(tail):
            before = header[:m.start()].rstrip()
            if before.endswith("=") or before.endswith(","):
                return None
            return name
        return None
    return None


def find_functions(masked: str) -> List[Dict]:
    funcs = []
    depth, last_top, start_brace, cur = 0, 0, None, None
    for i, ch in enumerate(masked):
        if ch == "{":
            if depth == 0:
                name = _function_name(masked[last_top:i])
                cur = {"name": name, "body_start": i} if name else None
                start_brace = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                if cur:
                    # 함수 헤더 시작: 이름이 나온 줄의 시작
                    hdr = masked[last_top:start_brace]
                    name_pos = hdr.rfind(cur["name"])
                    line_start = masked.rfind("\n", 0, last_top + name_pos) + 1
                    cur["start"] = line_start
                    cur["end"] = i + 1
                    funcs.append(cur)
                cur = None
                last_top = i + 1
            elif depth < 0:
                depth = 0
        elif ch == ";" and depth == 0:
            last_top = i + 1 if cur is None and _function_name(masked[last_top:i + 1]) is None else last_top
    return funcs


# ---------- 문장 뒤 오류 검사 ----------

_SQLCODE_RE = re.compile(r"sqlca\s*\.\s*sqlcode|\bSQLCODE\b|sqlca\s*\.\s*sqlerrd|\bSQLSTATE\b|sqlca\s*\.\s*sqlwarn", re.I)
_NOTFOUND_RE = re.compile(
    r"(?:sqlcode|SQLCODE)\s*(?:==|!=|<|>|<=|>=)\s*-?\s*(?:1403|100)\b|\b(?:1403|100)\s*(?:==|!=)\s*(?:sqlca\s*\.\s*)?sqlcode"
    r"|\b\w*(?:NOTFOUND|NOT_FOUND|NO_DATA|NODATA)\w*\b",
    re.I,
)
_ROWS_RE = re.compile(r"sqlca\s*\.\s*sqlerrd\s*\[\s*2\s*\]", re.I)


def post_checks(text_after: str) -> Dict:
    return {
        "sqlcode": bool(_SQLCODE_RE.search(text_after)),
        "notfound": bool(_NOTFOUND_RE.search(text_after)),
        "rows": bool(_ROWS_RE.search(text_after)),
    }


# ---------- 메인 ----------

def parse_proc_source(src: str, file_name: str, context: Optional[Dict] = None) -> Dict:
    """Pro*C 소스(또는 헤더) 하나를 분석한 결과(JSON 저장 가능한 dict).

    context: 헤더에서 모은 {"defines", "typedefs", "structs"}. 소스의 배열 크기 상수(LEN_NAME+1)나
             typedef 구조체(CUST_REC st;)를 풀 때 쓴다. 파일 자신의 정의가 우선한다.
    """
    context = context or {}
    stripped = strip_comments(src)
    lines = _Lines(src)

    joined = re.sub(r"\\\r?\n", " ", stripped)   # 여러 줄 #define (줄 끝 \) 합치기
    own_defines = {m.group(1): m.group(2) for m in _DEFINE_RE.finditer(joined)}
    defines = dict(context.get("defines") or {})
    defines.update(own_defines)
    includes = [m.group(1).replace("\\", "/").split("/")[-1] for m in _INCLUDE_RE.finditer(stripped)]

    sql_spans = find_exec_sql(stripped)
    for _, _, body in sql_spans:
        im = _SQL_INCLUDE_RE.match(body)
        if im:
            name = im.group(1).replace("\\", "/").split("/")[-1]
            if name.lower() not in ("sqlca", "oraca", "sqlda", "sqlca.h", "oraca.h", "sqlda.h"):
                includes.append(name if "." in name else name + ".h")

    # C 구조 분석용: EXEC SQL, 문자열, 전처리기 줄을 지운 텍스트
    chars = list(stripped)
    for s, e, _ in sql_spans:
        _blank(chars, s, e)
        chars[s] = ";"  # 문장 자리 표시 (함수 헤더 판별에서 앞 텍스트와 섞이지 않게)
    masked = "".join(chars)
    masked = re.sub(r'"(?:\\.|[^"\\\n])*"', lambda m: '"' + " " * (len(m.group()) - 2) + '"', masked)
    masked = re.sub(r"'(?:\\.|[^'\\\n])*'", lambda m: "'" + " " * (len(m.group()) - 2) + "'", masked)
    masked = _PP_LINE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), masked)

    funcs = find_functions(masked)
    typedefs: Dict[str, Dict] = dict(context.get("typedefs") or {})
    structs: Dict[str, Dict] = dict(context.get("structs") or {})
    ctx_typedefs, ctx_structs = set(typedefs), set(structs)

    # 전역 선언: 함수 본문을 지운 텍스트
    gchars = list(masked)
    for f in funcs:
        _blank(gchars, f["start"], f["end"])
    global_vars = parse_decls("".join(gchars), 0, lines, defines, typedefs, structs)

    functions: List[Dict] = []
    for f in funcs:
        body = masked[f["body_start"]:f["end"]]
        local_vars = parse_decls(body, f["body_start"], lines, defines, typedefs, structs)
        # K&R 파라미터 선언 + ANSI 파라미터도 지역 변수로 본다
        header = masked[f["start"]:f["body_start"]]
        hp = header[header.find("(") + 1:header.rfind(")")] if "(" in header else ""
        for p in hp.split(","):
            pm = re.match(r"\s*(" + BASE_TYPES + r")\s*(\**)\s*([A-Za-z_]\w*)\s*((?:\[[^\]]*\]\s*)*)\s*$", p)
            if pm:
                dims = [eval_size(x, defines) for x in re.findall(r"\[([^\]]*)\]", pm.group(4))]
                local_vars.setdefault(pm.group(3), make_hostvar(pm.group(1), pm.group(3), dims,
                                                                bool(pm.group(2)) or bool(pm.group(4) and not dims[0]),
                                                                lines.line(f["start"])))
        kr = header[header.rfind(")") + 1:] if ")" in header else ""
        if kr.strip():
            local_vars.update({k: v for k, v in parse_decls(kr, f["start"] + header.rfind(")") + 1, lines, defines,
                                                             typedefs, structs).items() if k not in local_vars})
        calls = []
        for cm in _CALL_RE.finditer(body):
            nm = cm.group(1)
            if nm not in C_KEYWORDS and nm not in C_LIB_FUNCS and nm != f["name"] and nm not in calls:
                calls.append(nm)
        functions.append({
            "name": f["name"],
            "line": lines.line(f["start"]),
            "end_line": lines.line(f["end"] - 1),
            "calls": calls,
            "host_vars": local_vars,
            "statements": [],
            "_start": f["start"],
            "_end": f["end"],
        })

    def owner(offset: int) -> Optional[Dict]:
        for fn in functions:
            if fn["_start"] <= offset < fn["_end"]:
                return fn
        return None

    statements: List[Dict] = []
    whenever = {"SQLERROR": "", "NOT FOUND": "", "SQLWARNING": ""}
    cursors: Dict[str, Dict] = {}
    for idx, (s, e, body) in enumerate(sql_spans):
        info = analyze_sql(body)
        fn = owner(s)
        if info["kind"] == "whenever":
            w = info["whenever"]
            whenever[w["condition"]] = w["action"]
        next_start = sql_spans[idx + 1][0] if idx + 1 < len(sql_spans) else len(stripped)
        limit = min(next_start, e + 800, fn["_end"] if fn else len(stripped))
        stmt = {
            "id": f"{file_name}:{lines.line(s)}",
            "function": fn["name"] if fn else "(전역)",
            "line": lines.line(s),
            "end_line": lines.line(e - 1),
            "sql": re.sub(r"\s+", " ", body).strip()[:2000],
            "checks": post_checks(stripped[e:limit]) if info["kind"] not in ("whenever", "include",
                                                                             "declare_section") else {},
        }
        stmt.update(info)
        stmt["whenever"] = {k: v for k, v in whenever.items() if v}
        if info["kind"] == "cursor" and info.get("cursor"):
            cursors[info["cursor"]] = stmt
        if info.get("dynamic") and fn:
            stmt["_func_text"] = stripped[fn["_start"]:s]
            stmt["guessed_tables"] = guess_dynamic_tables(stripped[fn["_start"]:fn["_end"]])
        statements.append(stmt)
        if fn:
            fn["statements"].append(stmt["id"])

    link_dynamic_sql(statements, defines)

    # 커서 연결: OPEN / FETCH 문에 커서의 테이블·컬럼 정보를 붙인다
    for st in statements:
        if st["kind"] in ("open", "fetch", "close") and st.get("cursor") in cursors:
            cur = cursors[st["cursor"]]
            st["cursor_stmt"] = cur["id"]
            if st["kind"] in ("open", "fetch"):
                st["tables"] = cur["tables"]
                st["columns"] = cur["columns"] if st["kind"] == "open" else []
            if st["kind"] == "open":
                st["literals"] = cur["literals"]
                st["bindings"] = [dict(b) for b in cur["bindings"] if b["direction"] == "filter"]
                if st.get("open_using") and cur.get("dynamic_sql"):
                    _apply_dynamic(st, {"kind": "select", "bindings": st["bindings"]},
                                   {"sql": cur["dynamic_sql"], "partial": cur.get("dynamic_partial", False)},
                                   st["open_using"])
                    st["tables"], st["columns"] = cur["tables"], cur["columns"]
            if cur.get("dynamic_sql") and st["kind"] in ("open", "fetch"):
                st["dynamic_sql"] = cur["dynamic_sql"]
                st["dynamic_partial"] = cur.get("dynamic_partial", False)
            if st["kind"] == "fetch":
                st["select_items"] = cur.get("select_items")
                st["select_star"] = cur.get("select_star", False)
                st["cursor_dynamic"] = cur.get("dynamic", False)

    for fn in functions:
        fn.pop("_start", None)
        fn.pop("_end", None)

    return {
        "parser_version": PARSER_VERSION,
        "file_name": file_name,
        "line_count": src.count("\n") + 1,
        "includes": list(dict.fromkeys(includes)),
        "defines": own_defines,
        "global_vars": global_vars,
        "typedefs": {k: v for k, v in typedefs.items() if k not in ctx_typedefs or k in _own_names(stripped)},
        "structs": {k: v for k, v in structs.items() if k not in ctx_structs or k in _own_names(stripped)},
        "functions": functions,
        "statements": statements,
        "has_main": any(f["name"] == "main" for f in functions),
    }


# ---------- 동적 SQL ----------

_DYN_FIELDS = ("tables", "columns", "bindings", "literals", "lineage", "select_items", "select_star",
               "insert_columns", "insert_no_columns", "insert_values", "insert_select", "insert_select_items")


def _using_hosts(sql: str) -> List[Dict]:
    m = re.search(r"\bUSING\b(.*)$", sql, re.I | re.S)
    if not m:
        return []
    return [{"host": t.value, "indicator": t.indicator} for t in tokenize(m.group(1)) if t.kind == "host"]


def _clean_dynamic(dyn: Dict) -> Dict:
    """자리표시(__V_..__)로 남은 테이블·컬럼은 버린다."""
    dyn = dict(dyn)
    dyn["tables"] = [t for t in dyn.get("tables") or [] if not is_placeholder(t["name"])]
    keep = lambda e: not is_placeholder(e.get("column")) and not is_placeholder(e.get("table"))  # noqa: E731
    for k in ("columns", "bindings", "literals"):
        dyn[k] = [e for e in dyn.get(k) or [] if keep(e)]
        for e in dyn[k]:
            e["candidates"] = [c for c in e.get("candidates") or [] if not is_placeholder(c)]
    return dyn


def _apply_dynamic(st: Dict, dyn: Dict, rec: Dict, using: List[Dict]):
    for k in _DYN_FIELDS:
        if k in dyn:
            st[k] = dyn[k]
    # 자리표시(:p1, :v_id ...)를 USING 의 호스트 변수로 순서대로 바꾼다
    if using:
        order = []
        for t in tokenize(rec["sql"]):
            if t.kind == "host" and t.value not in order:
                order.append(t.value)
        mapping = {ph: using[i] for i, ph in enumerate(order) if i < len(using)}
        for b in st.get("bindings") or []:
            u = mapping.get(b["host"])
            if u:
                b["host"], b["indicator"] = u["host"], u["indicator"] or b.get("indicator", "")
    st["dynamic_sql"] = rec["sql"][:2000]
    st["dynamic_partial"] = rec["partial"]
    st["dynamic_kind"] = dyn["kind"]
    st.pop("guessed_tables", None)


def link_dynamic_sql(statements: List[Dict], defines: Dict[str, str]):
    """PREPARE / EXECUTE IMMEDIATE 의 버퍼를 C 코드에서 재구성해 EXECUTE · 커서 문장에 붙인다."""
    prepared: Dict[str, Tuple[Dict, Dict]] = {}
    for st in statements:
        func_text = st.pop("_func_text", None)
        if func_text is None:
            continue
        m = re.match(r"\s*PREPARE\s+(\w+)\s+FROM\s+:\s*([\w.]+)", st["sql"], re.I)
        mi = re.match(r"\s*EXECUTE\s+IMMEDIATE\s+:\s*([\w.]+)", st["sql"], re.I)
        buf = m.group(2) if m else (mi.group(1) if mi else None)
        if not buf:
            continue
        rec = reconstruct(func_text, buf, defines)
        if not rec:
            continue
        dyn = _clean_dynamic(analyze_sql(rec["sql"]))
        if dyn["kind"] not in ("select", "insert", "update", "delete", "merge", "plsql"):
            st["dynamic_sql"] = rec["sql"][:2000]
            continue
        if m:
            prepared[m.group(1).upper()] = (dyn, rec)
            st["dynamic_sql"] = rec["sql"][:2000]
            st["dynamic_partial"] = rec["partial"]
            st.pop("guessed_tables", None)
        else:
            _apply_dynamic(st, dyn, rec, [])
            st["kind"] = dyn["kind"] if dyn["kind"] != "select" else "execute_immediate"
    if not prepared:
        return
    for st in statements:
        sql = st["sql"]
        me = re.match(r"\s*EXECUTE\s+(\w+)\b", sql, re.I)
        mc = re.match(r"\s*DECLARE\s+(\w+)\s+CURSOR\s+FOR\s+(\w+)\s*$", sql, re.I)
        if me and me.group(1).upper() in prepared and me.group(1).upper() not in ("IMMEDIATE", "BEGIN", "DECLARE"):
            dyn, rec = prepared[me.group(1).upper()]
            _apply_dynamic(st, copy.deepcopy(dyn), rec, _using_hosts(sql))
            if dyn["kind"] != "select":
                st["kind"] = dyn["kind"]
        elif mc and mc.group(2).upper() in prepared:
            dyn, rec = prepared[mc.group(2).upper()]
            if dyn["kind"] == "select":
                _apply_dynamic(st, copy.deepcopy(dyn), rec, [])
                st["dynamic"] = True
    # OPEN c USING :a, :b → 커서 조건의 자리표시를 실제 호스트 변수로
    cursors = {st.get("cursor"): st for st in statements if st["kind"] == "cursor" and st.get("dynamic_sql")}
    for st in statements:
        if st["kind"] == "open" and st.get("cursor") in cursors:
            st["open_using"] = _using_hosts(st["sql"])


def _own_names(text: str) -> set:
    return set(re.findall(r"\}\s*([A-Za-z_]\w*)\s*;", text)) | set(re.findall(r"struct\s+([A-Za-z_]\w*)\s*\{", text))


_DYN_TABLE_RE = re.compile(r"\b(FROM|JOIN|INTO|UPDATE)\s+([A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?)", re.I)


def guess_dynamic_tables(func_text: str) -> List[str]:
    """동적 SQL 은 C 문자열 안의 SQL 조각에서 테이블 이름을 추정한다."""
    names = []
    for lit in re.findall(r'"((?:\\.|[^"\\])*)"', func_text):
        for m in _DYN_TABLE_RE.finditer(lit):
            nm = m.group(2).split(".")[-1].upper()
            if nm not in ("DUAL",) and not nm.startswith("%") and nm not in names:
                names.append(nm)
    return names


# ---------- 호스트 변수 해석 / FETCH·SELECT INTO 연결 ----------

def resolve_host(name: str, func_vars: Dict, global_vars: Dict, header_vars: Dict) -> Optional[Dict]:
    """':a.b' 처럼 구조체 멤버면 멤버 선언을, 아니면 변수 선언을 찾는다."""
    parts = name.split(".")
    base = parts[0]
    hv = func_vars.get(base) or global_vars.get(base) or header_vars.get(base)
    if hv is None:
        return None
    if len(parts) > 1:
        members = hv.get("members") or {}
        mem = members.get(parts[-1])
        if mem:
            mem = dict(mem)
            mem["name"] = name
            if hv.get("array") and not mem.get("array"):
                mem["array"] = hv["array"]
        return mem
    return hv


def pair_out_bindings(stmt: Dict, resolve) -> List[Dict]:
    """SELECT INTO / FETCH 의 select 목록과 INTO 호스트 변수를 순서대로 짝짓는다.
    구조체 호스트 변수(:st_rec)는 멤버 순서대로 펼친다."""
    items = stmt.get("select_items") or []
    hosts = stmt.get("into_hosts") or []
    expanded = []
    for h in hosts:
        hv = resolve(h["host"])
        if hv and hv.get("kind") == "struct" and hv.get("members"):
            for mname in hv["members"]:
                expanded.append({"host": f"{h['host']}.{mname}", "indicator": h.get("indicator", "")})
        else:
            expanded.append(h)
    out = []
    for src, h in zip(items, expanded):
        if not src or src.get("star") or not src.get("column"):
            continue
        b = dict(src)
        b.update({"host": h["host"], "indicator": h.get("indicator", ""), "direction": "out"})
        out.append(b)
    return out, len(items), len(expanded)
