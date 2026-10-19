"""[A03] 배치 스크립트(.sh / .ksh / .csh / .bat / .cmd ...) 에서 실행 순서를 뽑는다.

- 줄 단위로 명령을 나누고(;  &&  ||  |), 각 명령의 첫 단어(실행 파일)를 기록한다.
- 실행 파일이 등록된 Pro*C 프로그램인지는 나중에(인덱스에서) 이름으로 연결한다.
  → 스크립트를 먼저 올리고 소스를 나중에 올려도 된다.
- 다른 스크립트를 부르면(sh other.sh / . other.sh / call other.bat) 그 스크립트의 단계를 펼친다(순환 방지).
- 명령 바로 뒤(같은 줄 || exit 또는 다음 3줄 안의 $? / ERRORLEVEL 검사)에 실패 검사가 있는지 표시한다.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Dict, List, Optional

SHELL_WORDS = set("""
if then else elif fi for do done while until case esac in function select time return exit break continue
echo print printf read cd pwd export set unset setenv unsetenv source alias unalias eval exec shift wait trap
test true false let typeset local declare readonly umask ulimit sleep date rm cp mv ln cat grep egrep fgrep
awk nawk gawk sed sort uniq head tail wc cut tr tee find ls mkdir rmdir chmod chown touch mail mailx sendmail
compress uncompress gzip gunzip tar zip unzip ftp sftp scp ssh rsh nohup basename dirname expr bc sh ksh csh bash
env xargs kill ps df du diff cmp logger stty tput clear which whence type hash getopts banner
rem call goto start pause cls title color setlocal endlocal pushd popd del erase copy move xcopy robocopy ren
rename md mkdir rd type findstr dir timeout choice errorlevel not exist defined equ neq lss leq gtr geq
""".split())
SCRIPT_RUNNERS = {"sh", "ksh", "bash", "csh", "tcsh", ".", "source", "call", "cmd"}
WRAPPERS = {"nohup", "time", "exec", "nice", "start", "/b", "/wait", "env"}
SCRIPT_EXTS = (".sh", ".ksh", ".csh", ".bash", ".bat", ".cmd")


def _strip_comment(line: str, is_win: bool) -> str:
    s = line.strip()
    if is_win:
        if re.match(r"(?i)^(rem\b|::)", s):
            return ""
        return s
    if s.startswith("#"):
        return ""
    # 따옴표 밖의 ' #' 이후는 주석
    out, q = [], ""
    for i, ch in enumerate(s):
        if ch in ("'", '"'):
            q = "" if q == ch else (ch if not q else q)
        if ch == "#" and not q and (i == 0 or s[i - 1] in " \t;"):
            break
        out.append(ch)
    return "".join(out).strip()


def _split_commands(line: str) -> List[str]:
    parts, cur, q, i = [], [], "", 0
    while i < len(line):
        ch = line[i]
        if ch in ("'", '"', "`"):
            q = "" if q == ch else (ch if not q else q)
        if not q:
            two = line[i:i + 2]
            if two in ("&&", "||"):
                parts.append(("".join(cur), two))
                cur = []
                i += 2
                continue
            if ch == "&" and (line[i - 1:i] == ">" or line[i + 1:i + 2] in (">", "1", "2")):
                cur.append(ch)  # 2>&1, &> 같은 리다이렉션
                i += 1
                continue
            if ch in (";", "|", "&"):
                parts.append(("".join(cur), ch))
                cur = []
                i += 1
                continue
        cur.append(ch)
        i += 1
    parts.append(("".join(cur), ""))
    return parts


def _words(cmd: str) -> List[str]:
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return cmd.split()


def _drop_redirect_targets(words: List[str]) -> List[str]:
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
            continue
        m = re.match(r"^(\d?|&)(>>?|<)(&\d)?(.*)$", w)
        if m:
            if not m.group(4) and not m.group(3):
                skip = True  # '> file' 형태
            continue
        out.append(w)
    return out


def program_name(word: str) -> str:
    """'$BIN_DIR/ORD010.exe' → 'ORD010'"""
    w = word.strip().strip("\"'")
    w = re.sub(r"\$\{?\w+\}?", "", w)
    w = w.replace("\\", "/").split("/")[-1]
    w = re.sub(r"\.(exe|out|sh|ksh|csh|bash|bat|cmd)$", "", w, flags=re.I)
    return w


def parse_batch_script(text: str, file_name: str) -> Dict:
    is_win = file_name.lower().endswith((".bat", ".cmd"))
    raw_lines = text.splitlines()
    # 줄 이어쓰기(\ / ^) 합치기 — 원래 줄 번호 유지
    logical = []
    buf, start_no = "", None
    for no, line in enumerate(raw_lines, start=1):
        cont = line.rstrip().endswith("^" if is_win else "\\")
        piece = line.rstrip()[:-1] if cont else line
        if start_no is None:
            start_no = no
        buf += piece + " "
        if not cont:
            logical.append((start_no, buf.strip()))
            buf, start_no = "", None
    if buf:
        logical.append((start_no, buf.strip()))

    commands = []
    for idx, (no, line) in enumerate(logical):
        code = _strip_comment(line, is_win)
        if not code:
            continue
        # 명령 치환(`...`, $(...))은 값으로 보고 지운다
        code = re.sub(r"`[^`]*`", "X", code)
        code = re.sub(r"\$\([^()]*\)", "X", code)
        for cmd, sep in _split_commands(code):
            # 리다이렉션 대상(> file)도 지운다
            words = _drop_redirect_targets(_words(cmd))
            # 변수 대입(A=1), 래퍼(nohup, time), 제어 키워드(if/then/do) 건너뛰기
            k = 0
            while k < len(words):
                w = words[k]
                lw = w.lower()
                if re.match(r"^[A-Za-z_]\w*=", w) or lw in WRAPPERS or lw in ("if", "then", "else", "elif", "do",
                                                                               "while", "until", "!", "not", "@"):
                    k += 1
                    continue
                if lw.startswith("@"):
                    words[k] = w[1:]
                break
            if k >= len(words):
                continue
            exe = words[k]
            args = words[k + 1:]
            lw = program_name(exe).lower()
            if not lw or lw in ("[", "[[", "((", "{", "}", "(", ")"):
                continue
            entry = {"line": no, "command": exe, "name": program_name(exe), "args": args[:20],
                     "text": cmd.strip()[:300], "sep": sep}
            if lw in SCRIPT_RUNNERS or exe in (".",):
                target = next((a for a in args if not a.startswith("-")), "")
                base = target.replace("\\", "/").split("/")[-1]
                if base.startswith(".") or base.lower() in ("profile", ".profile", ".bashrc", ".kshrc", "env.sh"):
                    continue  # 환경 설정 파일 읽기
                if target:
                    entry["calls_script"] = target.replace("\\", "/").split("/")[-1]
            elif exe.lower().endswith(SCRIPT_EXTS):
                entry["calls_script"] = exe.replace("\\", "/").split("/")[-1]
            elif lw in SHELL_WORDS:
                continue
            entry["checks_exit"] = _checks_exit(code, logical, idx, sep, is_win)
            commands.append(entry)
    return {"file_name": file_name, "line_count": len(raw_lines), "commands": commands}


def _checks_exit(code: str, logical, idx: int, sep: str, is_win: bool) -> bool:
    if sep == "||" or re.search(r"\|\|\s*(exit|return)", code):
        return True
    window = " ".join(l for _, l in logical[idx + 1: idx + 4])
    if is_win:
        return bool(re.search(r"(?i)errorlevel", code + " " + window))
    return "$?" in window or "$?" in code


def expand_steps(scripts: Dict[str, Dict], name: str, program_names: Dict[str, str],
                 _stack: Optional[List[str]] = None) -> List[Dict]:
    """스크립트 name 의 실행 단계를 펼친다. program_names: 소문자 이름 → 실제 프로그램 이름."""
    stack = (_stack or []) + [name]
    script = scripts.get(name)
    if not script:
        return []
    steps = []
    by_lower = {k.lower(): k for k in scripts}
    for c in script["commands"]:
        target = c.get("calls_script")
        if target:
            real = by_lower.get(target.lower()) or by_lower.get(os.path.splitext(target)[0].lower())
            if real and real not in stack:
                for s in expand_steps(scripts, real, program_names, stack):
                    s = dict(s)
                    s["via"] = [name] + s.get("via", [])
                    steps.append(s)
                continue
            steps.append({"script": name, "line": c["line"], "program": None, "command": c["command"],
                          "text": c["text"], "kind": "script", "missing_script": target,
                          "checks_exit": c.get("checks_exit", False)})
            continue
        prog = program_names.get(c["name"].lower())
        steps.append({
            "script": name, "line": c["line"], "program": prog, "command": c["command"], "args": c.get("args", []),
            "text": c["text"], "kind": "proc" if prog else _other_kind(c["name"]),
            "checks_exit": c.get("checks_exit", False),
        })
    if _stack is None:
        order = 0
        for s in steps:
            if s["kind"] == "proc":
                order += 1
                s["order"] = order
    return steps


def _other_kind(name: str) -> str:
    n = name.lower()
    if n in ("sqlplus", "sqlldr", "exp", "imp", "expdp", "impdp", "tbsql"):
        return "db_tool"
    return "command"
