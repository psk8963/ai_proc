"""[A04] 업로드한 소스·배치 파일 저장과 분석 인덱스.

- 원본 파일은 source_repository/proc, source_repository/batch 에 파일 이름 그대로 저장한다.
  (프로그램 이름 = 파일 이름이라 이름을 바꾸면 배치 스크립트와 연결이 끊어진다)
- 파싱 결과는 analysis_index/parsed/ 에 JSON 으로 캐시한다(파일 내용 + 헤더 내용 해시 기준).
- 메모리 인덱스(ProjectIndex)는 파일 목록이 바뀌면 다시 만든다.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import threading
import zipfile
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from batch_parser import expand_steps, parse_batch_script
from config import (
    ANALYSIS_INDEX_DIR,
    BATCH_EXTS,
    HEADER_EXTS,
    MAX_UPLOAD_BYTES,
    PROC_EXTS,
    SOURCE_REPOSITORY_DIR,
)
from proc_parser import PARSER_VERSION, decode_source, pair_out_bindings, parse_proc_source, resolve_host

logger = logging.getLogger(__name__)

PROC_DIR = os.path.join(SOURCE_REPOSITORY_DIR, "proc")
BATCH_DIR = os.path.join(SOURCE_REPOSITORY_DIR, "batch")
PARSED_DIR = os.path.join(ANALYSIS_INDEX_DIR, "parsed")
for _d in (PROC_DIR, BATCH_DIR, PARSED_DIR):
    os.makedirs(_d, exist_ok=True)

_SAFE_RE = re.compile(r"[^\w.\-가-힣]")


class AssetError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def safe_name(name: str) -> str:
    """경로를 떼고 위험한 문자를 _ 로 바꾼 파일 이름. 비면 빈 문자열."""
    base = (name or "").replace("\\", "/").split("/")[-1].strip()
    base = _SAFE_RE.sub("_", base)
    if base in ("", ".", "..") or base.startswith(".."):
        return ""
    return base[:150]


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def classify_source(name: str) -> Optional[str]:
    e = _ext(name)
    if e in PROC_EXTS:
        return "proc"
    if e in HEADER_EXTS:
        return "header"
    return None


def is_batch_name(name: str) -> bool:
    return _ext(name) in BATCH_EXTS


def _looks_text(data: bytes) -> bool:
    return b"\x00" not in data[:8192]


# ---------- 저장 ----------

def _zip_entries(data: bytes) -> Iterable[Tuple[str, bytes]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise AssetError("INVALID_ZIP", "zip 파일을 열 수 없습니다.")
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if not (info.flag_bits & 0x800):
            # 한글 Windows 에서 만든 zip 은 파일명이 CP949 인 경우가 많다
            try:
                name = info.filename.encode("cp437").decode("cp949")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        total += info.file_size
        if total > MAX_UPLOAD_BYTES * 5:
            raise AssetError("FILE_TOO_LARGE", "압축을 푼 크기가 너무 큽니다.", 413)
        yield name, zf.read(info)


def save_assets(files: List[Tuple[str, bytes]], kind: str) -> Dict:
    """kind = 'source' | 'batch'. zip 이면 풀어서 해당 종류 파일만 저장한다."""
    saved, skipped, replaced = [], [], []
    entries: List[Tuple[str, bytes]] = []
    for name, data in files:
        if _ext(name) == ".zip":
            entries.extend(_zip_entries(data))
        else:
            entries.append((name, data))
    seen = set()
    for name, data in entries:
        fname = safe_name(name)
        if not fname:
            continue
        if kind == "source":
            ok = classify_source(fname) is not None
            target_dir = PROC_DIR
        else:
            ok = is_batch_name(fname) and not fname.startswith(".")
            target_dir = BATCH_DIR
        if not ok or not data or not _looks_text(data):
            skipped.append(name)
            continue
        if fname.lower() in seen:
            skipped.append(f"{name} (같은 이름 파일이 이미 있어 건너뜀)")
            continue
        seen.add(fname.lower())
        path = os.path.join(target_dir, fname)
        if os.path.exists(path):
            with open(path, "rb") as f:
                if f.read() == data:
                    saved.append(fname)
                    continue
            replaced.append(fname)
        with open(path, "wb") as f:
            f.write(data)
        saved.append(fname)
    invalidate()
    return {"saved": len(saved), "replaced": replaced[:50], "skipped": skipped[:50], "skipped_count": len(skipped)}


def import_folder(folder: str, kind: str) -> Dict:
    """서버(이 PC)의 폴더에서 바로 가져온다. 하위 폴더 포함."""
    if not folder or not os.path.isdir(folder):
        raise AssetError("FOLDER_NOT_FOUND", "폴더를 찾을 수 없습니다. 이 프로그램이 실행 중인 PC의 경로를 입력해 주세요.")
    files = []
    total = 0
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in names:
            ok = classify_source(n) if kind == "source" else is_batch_name(n)
            if not ok:
                continue
            p = os.path.join(root, n)
            try:
                size = os.path.getsize(p)
                if size > 20 * 1024 * 1024:
                    continue
                total += size
                if total > MAX_UPLOAD_BYTES * 5:
                    raise AssetError("FILE_TOO_LARGE", "폴더 전체 크기가 너무 큽니다.", 413)
                with open(p, "rb") as f:
                    files.append((n, f.read()))
            except OSError:
                continue
    if not files:
        raise AssetError("NO_FILES", "가져올 파일이 없습니다. (확장자를 확인해 주세요)")
    return save_assets(files, kind)


def delete_asset(kind: str, name: str) -> bool:
    fname = safe_name(name)
    if not fname:
        return False
    path = os.path.join(PROC_DIR if kind == "source" else BATCH_DIR, fname)
    if os.path.exists(path):
        os.remove(path)
        invalidate()
        return True
    return False


def clear_assets(kind: str) -> int:
    d = PROC_DIR if kind == "source" else BATCH_DIR
    n = 0
    for name in os.listdir(d):
        os.remove(os.path.join(d, name))
        n += 1
    invalidate()
    return n


def read_source_text(file_name: str) -> Optional[str]:
    fname = safe_name(file_name)
    path = os.path.join(PROC_DIR, fname)
    if not fname or not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return decode_source(f.read())


def read_batch_text(file_name: str) -> Optional[str]:
    fname = safe_name(file_name)
    path = os.path.join(BATCH_DIR, fname)
    if not fname or not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return decode_source(f.read())


# ---------- 인덱스 ----------

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cached_parse(kind: str, name: str, data: bytes, ctx_key: str, parse):
    key = f"{kind}-{_sha(data)[:24]}-{ctx_key[:12]}-v{PARSER_VERSION}"
    path = os.path.join(PARSED_DIR, key + ".json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                result = json.load(f)
            if result.get("file_name") == name:
                return result
        except (OSError, ValueError):
            pass
    result = parse()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        logger.warning("parse cache write failed: %s", name)
    return result


def _merge_context(headers: Dict[str, Dict]) -> Dict:
    ctx = {"defines": {}, "typedefs": {}, "structs": {}, "global_vars": {}}
    for name in sorted(headers):
        h = headers[name]
        for k in ctx:
            ctx[k].update(h.get(k) or {})
    return ctx


class ProjectIndex:
    def __init__(self):
        self.headers: Dict[str, Dict] = {}
        self.programs: Dict[str, Dict] = {}          # 프로그램 이름(파일 stem) → 파싱 결과
        self.scripts: Dict[str, Dict] = {}
        self.statements: Dict[str, Dict] = {}        # stmt key → 문장 (program 필드 포함)
        self.functions: Dict[str, Dict] = {}         # "PROG.func" → 함수
        self.func_defs: Dict[str, List[str]] = defaultdict(list)   # 함수 이름 → ["PROG.func", ...]
        self.callers: Dict[str, List[str]] = defaultdict(list)     # "PROG.func" → 호출하는 함수 키 목록
        self.table_usage: Dict[str, List[Dict]] = defaultdict(list)
        self.batch_steps: Dict[str, List[Dict]] = {}  # 루트 스크립트 → 펼친 단계
        self.program_runs: Dict[str, List[Dict]] = defaultdict(list)
        self.warnings: List[str] = []

    # ----- 만들기 -----
    def build(self):
        headers_raw = {}
        for name in sorted(os.listdir(PROC_DIR)):
            if classify_source(name) == "header":
                with open(os.path.join(PROC_DIR, name), "rb") as f:
                    headers_raw[name] = f.read()
        for name, data in headers_raw.items():
            self.headers[name] = _cached_parse("h", name, data, "noctx",
                                               lambda d=data, n=name: parse_proc_source(decode_source(d), n))
        ctx = _merge_context(self.headers)
        ctx_key = _sha(json.dumps(ctx, sort_keys=True, ensure_ascii=False).encode())
        parse_ctx = {k: ctx[k] for k in ("defines", "typedefs", "structs")}

        for name in sorted(os.listdir(PROC_DIR)):
            if classify_source(name) != "proc":
                continue
            with open(os.path.join(PROC_DIR, name), "rb") as f:
                data = f.read()
            try:
                parsed = _cached_parse("p", name, data, ctx_key,
                                       lambda d=data, n=name: parse_proc_source(decode_source(d), n, parse_ctx))
            except Exception:
                logger.exception("parse failed: %s", name)
                self.warnings.append(f"{name}: 분석 중 오류가 나서 건너뛰었습니다.")
                continue
            prog = os.path.splitext(name)[0]
            if prog in self.programs:
                self.warnings.append(f"{name}: 같은 프로그램 이름이 이미 있어 건너뛰었습니다.")
                continue
            parsed["program"] = prog
            self.programs[prog] = parsed
        self._link(ctx["global_vars"])

        for name in sorted(os.listdir(BATCH_DIR)):
            with open(os.path.join(BATCH_DIR, name), "rb") as f:
                data = f.read()
            self.scripts[name] = _cached_parse("b", name, data, "noctx",
                                               lambda d=data, n=name: parse_batch_script(decode_source(d), n))
        self._link_batches()
        return self

    def _link(self, header_vars: Dict):
        for prog, p in self.programs.items():
            gvars = p.get("global_vars") or {}
            funcs_by_name = {}
            for fn in p["functions"]:
                key = f"{prog}.{fn['name']}"
                fn = dict(fn)
                fn["program"] = prog
                fn["key"] = key
                fn["file_name"] = p["file_name"]
                self.functions[key] = fn
                self.func_defs[fn["name"]].append(key)
                funcs_by_name[fn["name"]] = fn
            for st in p["statements"]:
                st = dict(st)
                st["program"] = prog
                st["file_name"] = p["file_name"]
                st["key"] = f"{prog}:{st['line']}"
                fvars = (funcs_by_name.get(st["function"]) or {}).get("host_vars") or {}

                def resolver(h, fvars=fvars, gvars=gvars):
                    return resolve_host(h, fvars, gvars, header_vars)

                if st["kind"] in ("select", "fetch") and st.get("select_items") is not None:
                    outs, n_items, n_hosts = pair_out_bindings(st, resolver)
                    st["bindings"] = [b for b in st.get("bindings") or [] if b["direction"] != "out"] + outs
                    if not st.get("select_star") and st.get("into_hosts") and n_items != n_hosts:
                        st["into_mismatch"] = {"items": n_items, "hosts": n_hosts}
                for b in st.get("bindings") or []:
                    hv = resolver(b["host"])
                    b["hv"] = _hv_summary(hv)
                    if b.get("indicator"):
                        b["indicator_hv"] = _hv_summary(resolver(b["indicator"]))
                self.statements[st["key"]] = st
                tables = list(st.get("tables") or [])
                for g in st.get("guessed_tables") or []:
                    tables.append({"name": g, "access": "?", "guessed": True})
                for tb in tables:
                    self.table_usage[tb["name"]].append({
                        "program": prog, "function": st["function"], "line": st["line"], "kind": st["kind"],
                        "access": tb["access"], "stmt": st["key"], "guessed": tb.get("guessed", False),
                    })
        # 호출 관계: 같은 프로그램 안 → 없으면 다른 파일(공통 라이브러리)
        for key, fn in self.functions.items():
            prog = fn["program"]
            for callee in fn.get("calls") or []:
                same = f"{prog}.{callee}"
                if same in self.functions:
                    self.callers[same].append(key)
                else:
                    for other in self.func_defs.get(callee, []):
                        self.callers[other].append(key)

    def _link_batches(self):
        names = {k.lower(): k for k in self.programs}
        called = set()
        by_lower = {k.lower(): k for k in self.scripts}
        for s in self.scripts.values():
            for c in s["commands"]:
                t = c.get("calls_script")
                if t:
                    real = by_lower.get(t.lower()) or by_lower.get(os.path.splitext(t)[0].lower())
                    if real:
                        called.add(real)
        for name in self.scripts:
            steps = expand_steps(self.scripts, name, names)
            self.batch_steps[name] = steps
            if name in called:
                continue  # 다른 스크립트 안에서 실행되는 스크립트는 루트 기준 순서만 기록
            for s in steps:
                if s.get("program"):
                    self.program_runs[s["program"]].append({
                        "script": name, "order": s.get("order"), "line": s["line"],
                        "via": s.get("via", []), "inner_script": s["script"], "checks_exit": s.get("checks_exit"),
                    })
        # 루트가 없는(순환) 스크립트도 실행 기록은 남긴다
        for name in called:
            for s in self.batch_steps.get(name, []):
                prog = s.get("program")
                if prog and not self.program_runs.get(prog):
                    self.program_runs[prog].append({"script": name, "order": s.get("order"), "line": s["line"],
                                                    "via": [], "inner_script": name,
                                                    "checks_exit": s.get("checks_exit")})
        self.root_scripts = [n for n in self.scripts if n not in called]

    # ----- 조회 -----
    def program_name(self, name: str) -> Optional[str]:
        if name in self.programs:
            return name
        low = name.lower()
        for k in self.programs:
            if k.lower() == low or os.path.splitext(self.programs[k]["file_name"])[0].lower() == low:
                return k
        return None

    def call_paths(self, func_key: str, max_depth: int = 6) -> List[List[str]]:
        """func_key 를 부르는 경로(main 쪽부터). 최대 5개."""
        paths, frontier = [], [[func_key]]
        for _ in range(max_depth):
            nxt = []
            for path in frontier:
                callers = [c for c in self.callers.get(path[0], []) if c not in path]
                if not callers:
                    paths.append(path)
                    continue
                for c in callers:
                    nxt.append([c] + path)
            frontier = nxt
            if not frontier or len(paths) >= 5:
                break
        paths.extend(frontier)
        return paths[:5]

    def callees_closure(self, func_key: str, max_depth: int = 6) -> List[str]:
        seen, stack = {func_key}, [(func_key, 0)]
        while stack:
            k, d = stack.pop()
            fn = self.functions.get(k)
            if not fn or d >= max_depth:
                continue
            for callee in fn.get("calls") or []:
                for target in ([f"{fn['program']}.{callee}"] if f"{fn['program']}.{callee}" in self.functions
                               else self.func_defs.get(callee, [])):
                    if target not in seen:
                        seen.add(target)
                        stack.append((target, d + 1))
        return list(seen)

    def stats(self) -> Dict:
        return {
            "programs": len(self.programs),
            "headers": len(self.headers),
            "functions": len(self.functions),
            "statements": sum(1 for s in self.statements.values() if s.get("tables")),
            "tables": len(self.table_usage),
            "scripts": len(self.scripts),
            "batch_steps": sum(1 for steps in self.batch_steps.values() for s in steps if s.get("program")),
            "warnings": self.warnings[:20],
        }


def _hv_summary(hv: Optional[Dict]) -> Optional[Dict]:
    if not hv:
        return None
    keys = ("name", "ctype", "kind", "size", "capacity", "array", "pointer", "line", "int_bits", "dims_expr")
    return {k: hv.get(k) for k in keys if k in hv}


_lock = threading.Lock()
_index: Optional[ProjectIndex] = None
_signature = None


def _dir_signature():
    sig = []
    for d in (PROC_DIR, BATCH_DIR):
        for name in sorted(os.listdir(d)):
            st = os.stat(os.path.join(d, name))
            sig.append((d, name, st.st_size, st.st_mtime_ns))
    return tuple(sig)


def invalidate():
    global _signature
    with _lock:
        _signature = None


def get_index() -> ProjectIndex:
    global _index, _signature
    with _lock:
        sig = _dir_signature()
        if _index is None or sig != _signature:
            _index = ProjectIndex().build()
            _signature = sig
            logger.info("index built programs=%d scripts=%d", len(_index.programs), len(_index.scripts))
        return _index
