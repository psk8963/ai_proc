"""앱 안 '설정' 화면에서 고치는 .env 값 읽기/쓰기.

- .env 의 다른 줄(주석 포함)은 그대로 두고, 해당 KEY= 줄만 바꾼다. 없으면 끝에 붙인다.
- 비밀번호는 화면으로 내보내지 않는다. 빈 값으로 저장하면 기존 비밀번호를 유지한다.
- 저장한 값은 앱을 다시 시작해야 적용된다(Oracle 클라이언트 초기화는 한 번만 가능).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List

import config

FIELDS: List[Dict] = [
    {"key": "ORACLE_USER", "label": "DB 계정", "group": "Oracle"},
    {"key": "ORACLE_PASSWORD", "label": "DB 비밀번호", "group": "Oracle", "secret": True},
    {"key": "ORACLE_DSN", "label": "접속 주소 (IP:포트/서비스명 또는 TNS 이름)", "group": "Oracle"},
    {"key": "ORACLE_TNS_ADMIN", "label": "tnsnames.ora 폴더 (TNS 이름을 쓸 때)", "group": "Oracle"},
    {"key": "ORACLE_CLIENT_LIB_DIR", "label": "Instant Client 19 폴더 (DB 가 11g 일 때)", "group": "Oracle"},
    {"key": "ORACLE_SCHEMAS", "label": "스키마 (쉼표 구분, 비우면 접속 계정)", "group": "Oracle"},
    {"key": "DB_BYTES_PER_CHAR", "label": "문자셋 바이트 (KO16MSWIN949=2, AL32UTF8=3)", "group": "Oracle"},
    {"key": "HOST_LONG_BITS", "label": "Pro*C 서버 C long 비트 (64bit 유닉스=64)", "group": "분석"},
    {"key": "OLLAMA_HOST", "label": "Ollama 주소", "group": "LLM"},
    {"key": "LLM_MODEL", "label": "LLM 모델", "group": "LLM"},
    {"key": "PROC_AGENT_PORT", "label": "앱 포트", "group": "앱"},
]
KEYS = {f["key"] for f in FIELDS}
SECRET = {f["key"] for f in FIELDS if f.get("secret")}


def env_path() -> Path:
    return Path(config.DATA_DIR) / ".env"


def _parse(text: str) -> Dict[str, str]:
    vals = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        vals[k.strip()] = v
    return vals


def read_settings() -> Dict:
    p = env_path()
    file_vals = _parse(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    values, has_secret = {}, {}
    for f in FIELDS:
        k = f["key"]
        v = file_vals.get(k, os.getenv(k, ""))
        if k in SECRET:
            has_secret[k] = bool(v)
            v = ""
        values[k] = v
    return {"path": str(p), "fields": FIELDS, "values": values, "has_secret": has_secret,
            "desktop": bool(os.getenv("PROC_AGENT_DESKTOP"))}


def write_settings(new_values: Dict[str, str]) -> Dict:
    p = env_path()
    text = p.read_text(encoding="utf-8-sig") if p.exists() else ""
    lines = text.splitlines()
    changed = []
    for k, v in new_values.items():
        if k not in KEYS:
            continue
        v = "" if v is None else str(v).strip()
        if "\n" in v or "\r" in v:
            raise ValueError(f"{k}: 줄바꿈은 넣을 수 없습니다.")
        if k in SECRET and v == "":
            continue  # 빈 값이면 기존 비밀번호 유지
        line = f"{k}={v}"
        pat = re.compile(rf"^\s*#?\s*{re.escape(k)}\s*=")
        idx = next((i for i, ln in enumerate(lines) if pat.match(ln) and not ln.lstrip().startswith("#")), None)
        if idx is None:
            idx = next((i for i, ln in enumerate(lines) if pat.match(ln)), None)  # 주석 처리된 예시 줄
        if idx is None:
            lines.append(line)
        elif lines[idx] != line:
            lines[idx] = line
        else:
            continue
        changed.append(k)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return {"saved": True, "changed": changed, "restart_required": bool(changed)}
