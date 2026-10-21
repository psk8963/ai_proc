"""[A06] 동적 SQL 재구성.

Pro*C 의 동적 SQL(PREPARE s FROM :buf / EXECUTE IMMEDIATE :buf)은 SQL 문장이 C 문자열로 만들어져서
정적 분석으로는 테이블·컬럼을 알 수 없다. 여기서는 함수 안에서 그 버퍼를 만드는 C 코드를 따라가
SQL 문장을 다시 조립한다.

  strcpy / strncpy / strcat / strncat / sprintf / snprintf / memcpy / 포인터 대입(p = "...")
  VARCHAR 버퍼(.arr), 캐스트((char *)), sprintf(buf + strlen(buf), ...) 같은 이어 붙이기
  #define 문자열 상수, 다른 버퍼에 먼저 만들어 둔 조각(where 절 등)을 strcat 하는 경우

알 수 없는 값(함수 인자, 실행 중 읽은 값)은 __V_이름__ 자리표시로 남긴다.
if / else 로 갈라지는 조립은 모든 갈래를 이어 붙여 한 문장으로 본다(쓰일 수 있는 컬럼을 모두 찾기 위해).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

PLACEHOLDER_PREFIX = "__V_"

_CALL_RE = re.compile(r"\b(strcpy|strncpy|strcat|strncat|sprintf|snprintf|memcpy|strlcpy|strlcat)\s*\(")
_ASSIGN_RE = re.compile(r"(?:^|[;{}\s])(?:const\s+)?(?:char\s*\*\s*)?([A-Za-z_][\w.]*)\s*=\s*(\"(?:\\.|[^\"\\])*\"(?:\s*\"(?:\\.|[^\"\\])*\")*)")
_FMT_RE = re.compile(r"%[-+ #0]*(\d+|\*)?(?:\.(\d+|\*))?(hh|h|ll|l|L|z|j|t)?([diouxXeEfgGcspn%])")
_STR_RE = re.compile(r"\"((?:\\.|[^\"\\])*)\"")


def split_c_statements(text: str) -> List[str]:
    """세미콜론 기준으로 C 문장을 나눈다(문자열·문자 상수 안의 ; 는 무시)."""
    out, cur, q, i = [], [], "", 0
    while i < len(text):
        ch = text[i]
        if q:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 2
                continue
            if ch == q:
                q = ""
        elif ch in "\"'":
            q = ch
            cur.append(ch)
        elif ch == ";":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        out.append("".join(cur))
    return out


def _split_args(s: str) -> List[str]:
    args, cur, depth, q, i = [], [], 0, "", 0
    while i < len(s):
        ch = s[i]
        if q:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 2
                continue
            if ch == q:
                q = ""
        elif ch in "\"'":
            q = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    if "".join(cur).strip():
        args.append("".join(cur).strip())
    return args


def _call_args(stmt: str, start: int) -> Optional[List[str]]:
    """stmt[start] 가 '(' 일 때 짝 괄호까지의 인자 목록."""
    depth, q, i = 0, "", start
    while i < len(stmt):
        ch = stmt[i]
        if q:
            if ch == "\\":
                i += 2
                continue
            if ch == q:
                q = ""
        elif ch in "\"'":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return _split_args(stmt[start + 1:i])
        i += 1
    return None


def _unescape(s: str) -> str:
    return (s.replace("\\n", " ").replace("\\t", " ").replace("\\r", " ")
             .replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\"))


def normalize_target(expr: str) -> Tuple[str, bool]:
    """'(char *)v_sql.arr' → 'v_sql', 'buf + strlen(buf)' → ('buf', True: 이어 붙이기)"""
    e = expr.strip()
    append = False
    m = re.match(r"^(.*?)\s*\+\s*strlen\s*\((.*)\)\s*$", e)
    if m:
        e, append = m.group(1), True
    e = re.sub(r"\(\s*(?:unsigned\s+|signed\s+|const\s+)?char\s*\*\s*\)", "", e)
    e = e.replace("&", "").replace("(", "").replace(")", "").replace(" ", "")
    e = re.sub(r"\[\s*0\s*\]$", "", e)
    e = re.sub(r"(\.|->)arr$", "", e)
    e = e.replace("->", ".")
    return e, append


def _placeholder(name: str) -> str:
    return PLACEHOLDER_PREFIX + re.sub(r"\W", "_", name.upper())[:30] + "__"


class _Env:
    def __init__(self, defines: Dict[str, str]):
        self.vals: Dict[str, str] = {}
        self.defines = defines
        self.unknown = False

    def eval(self, expr: str, depth: int = 0) -> str:
        e = expr.strip()
        lits = _STR_RE.findall(e)
        if lits and re.fullmatch(r'(\s*"(?:\\.|[^"\\])*"\s*)+', e):
            return "".join(_unescape(x) for x in lits)
        name, _ = normalize_target(e)
        if name in self.vals:
            return self.vals[name]
        if depth < 5 and re.fullmatch(r"[A-Za-z_]\w*", name) and name in self.defines:
            return self.eval(self.defines[name], depth + 1)
        if re.fullmatch(r"-?\d+(\.\d+)?[lLuU]*", e):
            return e.rstrip("lLuU")
        self.unknown = True
        return _placeholder(name or "X")

    def fmt(self, fmt_expr: str, args: List[str]) -> str:
        fmt = self.eval(fmt_expr)
        out, pos, ai = [], 0, 0
        for m in _FMT_RE.finditer(fmt):
            out.append(fmt[pos:m.start()])
            pos = m.end()
            conv = m.group(4)
            if conv == "%":
                out.append("%")
                continue
            for star in (m.group(1), m.group(2)):
                if star == "*":
                    ai += 1
            arg = args[ai] if ai < len(args) else "X"
            ai += 1
            if conv == "s":
                out.append(self.eval(arg))
            elif conv == "c":
                out.append("X")
            else:
                v = self.eval(arg)
                out.append(v if re.fullmatch(r"-?\d+(\.\d+)?", v) else "0")
        out.append(fmt[pos:])
        return "".join(out)


def reconstruct(func_text: str, buffer: str, defines: Dict[str, str]) -> Optional[Dict]:
    """func_text(함수 시작 ~ 동적 SQL 문 직전, 주석 제거) 에서 buffer 에 들어가는 SQL 을 조립한다.

    반환: {"sql": 조립한 문장, "partial": 자리표시 포함 여부, "steps": 반영한 C 문장 수} 또는 None
    """
    target, _ = normalize_target(buffer.lstrip(":"))
    env = _Env(defines)
    steps = 0
    touched = False
    for raw in split_c_statements(func_text):
        stmt = raw.strip()
        if not stmt:
            continue
        handled = False
        for m in _CALL_RE.finditer(stmt):
            fn = m.group(1)
            args = _call_args(stmt, m.end() - 1)
            if not args:
                continue
            name, append = normalize_target(args[0])
            if fn in ("strcpy", "strlcpy", "strncpy", "memcpy"):
                if len(args) < 2:
                    continue
                val = env.eval(args[1])
                env.vals[name] = env.vals.get(name, "") + val if append else val
            elif fn in ("strcat", "strncat", "strlcat"):
                if len(args) < 2:
                    continue
                env.vals[name] = env.vals.get(name, "") + env.eval(args[1])
            elif fn == "sprintf":
                if len(args) < 2:
                    continue
                val = env.fmt(args[1], args[2:])
                env.vals[name] = env.vals.get(name, "") + val if append else val
            elif fn == "snprintf":
                if len(args) < 3:
                    continue
                val = env.fmt(args[2], args[3:])
                env.vals[name] = env.vals.get(name, "") + val if append else val
            if name == target:
                touched = True
                steps += 1
            handled = True
        if not handled:
            am = _ASSIGN_RE.search(stmt)
            if am:
                name, _ = normalize_target(am.group(1))
                env.vals[name] = env.eval(am.group(2))
                if name == target:
                    touched, steps = True, steps + 1
    if not touched or not env.vals.get(target, "").strip():
        return None
    sql = re.sub(r"\s+", " ", env.vals[target]).strip().rstrip(";")
    # 동적 SQL 의 위치 자리표시(:1, :2) 는 이름 있는 자리표시로 바꿔 둔다(분석기가 호스트 변수로 인식하도록)
    sql = re.sub(r":(\d+)\b", r":p\1", sql)
    return {"sql": sql, "partial": PLACEHOLDER_PREFIX in sql, "steps": steps}


def is_placeholder(name: Optional[str]) -> bool:
    return bool(name) and name.upper().startswith(PLACEHOLDER_PREFIX)
