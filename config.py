import os
from pathlib import Path

# 실행 위치(cwd)와 무관하게 프로젝트 폴더 기준으로 경로를 잡는다.
BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """프로젝트 폴더의 .env 를 읽어 환경 변수로 넣는다(이미 있는 값은 그대로 둔다).

    DB 비밀번호처럼 매번 PowerShell 에서 입력하기 번거로운 값을 위한 것. .env 는 git 에 올리지 않는다.
    python-dotenv 없이 KEY=VALUE 형식만 지원한다.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


if not os.getenv("PROC_AGENT_NO_DOTENV"):
    _load_dotenv(BASE_DIR / ".env")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


# Ollama 설정 (폐쇄망 기준 로컬 모델) — ai_nexacro 와 같은 값
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
LLM_MODEL = os.getenv("LLM_MODEL", "gemma4:e2b")
LLM_NUM_CTX = _int_env("LLM_NUM_CTX", 8192)

# Oracle 접속 (읽기 전용 계정 권장)
#   ORACLE_DSN 예: 10.1.2.3:1521/ORCLPDB  또는  tnsnames.ora 의 별칭(ORACLE_CLIENT_LIB_DIR 사용 시)
ORACLE_USER = os.getenv("ORACLE_USER", "")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "")
ORACLE_DSN = os.getenv("ORACLE_DSN", "")
# 메타데이터를 읽을 스키마(쉼표 구분). 비우면 접속 계정 스키마 + 시노님이 가리키는 스키마.
ORACLE_SCHEMAS = [s.strip().upper() for s in os.getenv("ORACLE_SCHEMAS", "").split(",") if s.strip()]
# DB 서버가 11g 라 thin 모드로 안 붙을 때만 Instant Client(19 이상, 64bit) 폴더를 지정한다(thick 모드).
ORACLE_CLIENT_LIB_DIR = os.getenv("ORACLE_CLIENT_LIB_DIR", "")
# tnsnames.ora 가 있는 폴더. 지정하면 ORACLE_DSN 에 Toad 에서 쓰는 TNS 별칭(예: PRODDB)을 그대로 쓸 수 있다.
ORACLE_TNS_ADMIN = os.getenv("ORACLE_TNS_ADMIN", os.getenv("TNS_ADMIN", ""))
DB_CALL_TIMEOUT_MS = _int_env("DB_CALL_TIMEOUT_MS", 30000)
# 메타데이터 갱신용 제한 시간(11g 의 ALL_* 뷰는 느리다) — 기본 10분
META_CALL_TIMEOUT_MS = _int_env("META_CALL_TIMEOUT_MS", 600000)
# referenced: 등록된 소스가 쓰는 테이블만 메타데이터를 읽는다(빠름) / all: 스키마 전체
ORACLE_META_SCOPE = os.getenv("ORACLE_META_SCOPE", "referenced").strip().lower()
DB_MAX_ROWS = _int_env("DB_MAX_ROWS", 500)
DB_POOL_MAX = _int_env("DB_POOL_MAX", 4)
# VARCHAR2(n CHAR) 컬럼의 바이트 계산용. AL32UTF8=3, KO16MSWIN949=2
DB_BYTES_PER_CHAR = _int_env("DB_BYTES_PER_CHAR", 3)

# Pro*C 가 돌아가는 서버의 C long 크기. 64bit 유닉스(AIX/HP-UX/Solaris/Linux)=64, Windows=32
HOST_LONG_BITS = _int_env("HOST_LONG_BITS", 64)

# 자산 저장 경로
SOURCE_REPOSITORY_DIR = os.getenv("SOURCE_REPOSITORY_DIR", str(BASE_DIR / "source_repository"))
ANALYSIS_INDEX_DIR = os.getenv("ANALYSIS_INDEX_DIR", str(BASE_DIR / "analysis_index"))
SCHEMA_SNAPSHOT_PATH = os.getenv("SCHEMA_SNAPSHOT_PATH", str(Path(ANALYSIS_INDEX_DIR) / "schema_snapshot.json"))

PROC_EXTS = (".pc", ".pcc", ".pc2", ".c", ".cpp", ".cc")
HEADER_EXTS = (".h", ".hpp", ".hh", ".pch")
BATCH_EXTS = (".sh", ".ksh", ".csh", ".bash", ".bat", ".cmd", ".job", ".jcl", ".txt", ".run", "")

# 프롬프트 크기 제한(문자 수). num_ctx를 넘으면 Ollama가 앞부분(지침)부터 잘라낸다.
MAX_CONTEXT_CHARS = _int_env("MAX_CONTEXT_CHARS", 6000)
MAX_FINDINGS_IN_PROMPT = _int_env("MAX_FINDINGS_IN_PROMPT", 25)
MAX_CODE_CHARS = _int_env("MAX_CODE_CHARS", 2500)

# 대화 기록
MAX_HISTORY_TURNS = _int_env("MAX_HISTORY_TURNS", 4)
MAX_HISTORY_CHARS = _int_env("MAX_HISTORY_CHARS", 3000)

# 업로드 크기 제한 (소스 전체를 zip 으로 올릴 수 있게 넉넉히)
MAX_UPLOAD_BYTES = _int_env("MAX_UPLOAD_MB", 200) * 1024 * 1024

os.makedirs(SOURCE_REPOSITORY_DIR, exist_ok=True)
os.makedirs(ANALYSIS_INDEX_DIR, exist_ok=True)


def db_configured() -> bool:
    return bool(ORACLE_USER and ORACLE_DSN)
