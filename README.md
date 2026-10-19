# Pro*C 영향도 분석 에이전트 (Local PoC)

폐쇄망 PC에서 로컬 LLM(Ollama)으로 동작하는 **Pro*C 소스 ↔ DB 테이블 ↔ 배치 순서** 영향도 분석 에이전트입니다.
`ai_nexacro` 와 같은 실행 환경(Python 3.14 · FastAPI · LangGraph · Ollama `gemma4:e2b` · wheels 오프라인 설치)을 씁니다.

- Pro*C 소스(.pc/.h)와 배치 스크립트(.sh/.ksh/.bat)를 등록하면, 어떤 프로그램의 어떤 함수가 어떤 테이블·컬럼을
  읽고/쓰는지, 배치에서 몇 번째로 실행되는지 연결해 둡니다.
- "이 컬럼 길이를 늘리면 / NULL 을 허용하면 / 이 값을 바꾸면" 같은 변경을 넣으면
  **어느 프로그램 · 함수 · 줄에서 어떤 ORA 오류가 날 수 있는지**, 처리 대상 데이터가 어떻게 달라지는지,
  배치 뒤 단계로 어떻게 번지는지 보여줍니다.
- Oracle DB 에 읽기 전용으로 붙어 테이블 메타데이터(타입·길이·NULL·PK·코멘트)를 가져오고,
  Toad 처럼 테이블을 둘러보고 SELECT 를 실행할 수 있습니다.

---

## 1. 준비물

| 항목 | 버전 / 비고 |
|---|---|
| Windows PC | 10 / 11 |
| Python | 3.14 (ai_nexacro 와 동일). 설치 시 "Add python.exe to PATH" 체크 |
| Ollama | ai_nexacro 에서 쓰던 것 그대로 (`gemma4:e2b`). 없어도 규칙 분석은 동작 |
| Oracle 접속 정보 | 조회 전용 계정 권장. 회사 PC 에서만 필요 (집에서는 메타데이터 스냅샷/샘플로 개발) |

## 2. 설치 (집 PC · 인터넷 됨)

```powershell
cd C:\workspace\ai_proc
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

> PowerShell에서 `venv\Scripts\activate`가 막히면 한 번만 실행:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

설치가 끝나면 `pip show oracledb` 로 버전을 확인해서 `requirements.txt` 의 `oracledb>=3.4` 를
`oracledb==<버전>` 으로 고정해 두세요(회사 PC 와 같은 버전을 쓰기 위해).

## 3. 실행

```powershell
cd C:\workspace\ai_proc
venv\Scripts\activate
python -m uvicorn app:app --reload --port 8001
```

브라우저에서 **http://localhost:8001/ui/** 를 엽니다. (ai_nexacro 가 8000 을 쓰므로 8001 로 띄웁니다)

### 처음 해 볼 것 (샘플)

1. 왼쪽 **① Pro*C 소스** 에서 `tests\samples\proc` 의 파일 3개를, **② 배치 스크립트** 에서
   `tests\samples\batch` 의 파일 2개를 올립니다.
2. 집 PC 처럼 DB 가 없으면 `tests\samples\schema_snapshot.json` 을 `analysis_index\` 폴더에 복사합니다
   (샘플 테이블 메타데이터).
3. **에이전트 대화** 에서 예시 질문을 누르거나, **영향도 분석** 탭에서 `TB_CUST` / `CUST_NM` / 컬럼 길이 변경 / 50 으로 분석합니다.

## 4. 회사 PC 설정

### 4-1. 인터넷이 막힌 PC에 설치하기 (wheels)

집 PC 에서 `make_wheels.bat` 를 더블클릭하면 `wheels` 폴더를 만들고 오프라인 설치까지 시험합니다
(끝에 `make_wheels_log.txt` → `ALL_OK`). 프로젝트 폴더째 회사 PC 로 옮긴 뒤:

```powershell
cd C:\workspace\ai_proc
python -m venv venv
venv\Scripts\activate
python -m pip install --no-index --find-links wheels -r requirements.txt
```

**Windows 64bit + Python 3.14** 기준입니다. 회사 PC 의 Python 버전이 다르면 그 버전으로 wheels 를 다시 만들어야 합니다.
Ollama 모델은 ai_nexacro 와 같은 것을 쓰므로 추가로 옮길 것이 없습니다.

### 4-2. Oracle 접속 (.env)

`.env.example` 을 `.env` 로 복사하고 값을 채웁니다. `.env` 는 git 에 올라가지 않습니다.

```ini
ORACLE_USER=readonly_user
ORACLE_PASSWORD=********
ORACLE_DSN=10.1.2.3:1521/ORCLPDB   # 또는 TNS 별칭 (ORACLE_TNS_ADMIN 지정 시)
ORACLE_SCHEMAS=APP,COM          # 테이블 소유 스키마 (비우면 접속 계정 + 시노님 대상)
DB_BYTES_PER_CHAR=3             # DB 문자셋 AL32UTF8=3, KO16MSWIN949=2
HOST_LONG_BITS=64               # Pro*C 가 도는 서버의 C long 크기 (64bit 유닉스=64)
```

- **Toad 접속 이름을 그대로 쓰려면**: `ORACLE_TNS_ADMIN` 에 Toad 가 쓰는 `tnsnames.ora` 폴더
  (예: `C:\oracle\product\11.2.0\client_1\network\admin`)를 넣고 `ORACLE_DSN=<TNS 별칭>` 으로 씁니다.
- **DB 서버 버전에 따라** (Toad 에서 `SELECT * FROM v$version` 으로 확인)

  | DB 서버 | 방법 |
  |---|---|
  | 12c 이상 | thin 모드. 추가 설치 없음 (`ORACLE_CLIENT_LIB_DIR` 비워 둠) |
  | 11g (11.2) | thin 모드로는 붙지 않습니다(`DPY-3010`). **Instant Client 19 이상 64bit** (Basic 또는 Basic Light zip)를 받아 압축을 풀고 `ORACLE_CLIENT_LIB_DIR=<그 폴더>` 로 지정합니다(thick 모드). |

  PC 에 이미 깔린 **11g 클라이언트(Toad 용)는 쓸 수 없습니다** — python-oracledb thick 모드는 클라이언트 19 이상만 지원하고,
  Python 이 64bit 라 클라이언트도 64bit 여야 합니다. Instant Client 는 설치 없이 zip 만 풀면 되고, 기존 11g 클라이언트와 Toad 에는 영향이 없습니다.
  집 PC 에서 oracle.com 에서 받아 프로젝트 폴더(`C:\workspace\ai_proc\instantclient_19_xx`)에 풀어서 같이 옮기면 됩니다.
  Instant Client 19 를 쓰려면 **Visual C++ 2017 재배포 패키지(x64)** 가 PC 에 있어야 합니다.
- 서버 실행 후 왼쪽 **연결 확인** → **메타데이터 갱신** 을 누르면 `analysis_index\schema_snapshot.json` 이 만들어집니다.
  이 파일을 집 PC 로 가져가면 DB 없이도 같은 메타데이터로 분석할 수 있습니다(회사 보안 정책을 먼저 확인하세요).

**안전장치**: SQL 창은 `SELECT` / `WITH` 한 문장만 받고, 모든 조회는 `SET TRANSACTION READ ONLY` 안에서
실행한 뒤 ROLLBACK 합니다. 그래도 DB 계정은 조회 권한만 있는 계정을 쓰세요.

### 4-3. 소스가 많을 때

수천 개 파일은 업로드 대신 왼쪽 **이 PC 폴더에서 바로 가져오기** 에 경로(예: `D:\src\proc`)를 넣으면
하위 폴더까지 한 번에 가져옵니다. 분석 결과는 `analysis_index\parsed\` 에 캐시되어 다음 실행부터 빠릅니다.

## 5. 동작 방식

```
[등록]  .pc/.h  ─ proc_parser ─┬─ 함수(시작·끝 줄, 호출 관계, K&R 스타일 포함)
                               ├─ EXEC SQL 문 ─ sql_analyzer ─ 테이블 R/W, 컬럼(용도), 호스트 변수 연결,
                               │                               WHERE/DECODE 상수, INSERT…SELECT 흐름
                               └─ 호스트 변수 선언(char[n]/VARCHAR/int/struct, #define 크기 계산)
        .sh/.bat ─ batch_parser ─ 실행 순서(하위 스크립트 펼침), 실패 검사($?, || exit, errorlevel)
        Oracle   ─ db_runtime   ─ 메타데이터 스냅샷(컬럼 타입·길이·NULL·PK/UK/FK·코멘트, 시노님)

[질문]  질문 → 질의 재작성(이전 대화가 있을 때만, LLM)
             → 질문 이해(규칙: 테이블·컬럼·프로그램 이름, 변경 종류·값)
                ├ 대상 없음 → LLM 호출 없이 안내
                ├ 변경 질문 → (규칙으로 못 채우면 LLM 이 JSON 으로 추출) → impact_engine(규칙) → LLM 보고서
                └ 조회 질문 → 사용처·컬럼·함수 코드 모으기 → LLM 답변
        LLM(Ollama)이 꺼져 있으면 규칙 분석 결과 표를 그대로 보여줍니다.
```

### 영향도 규칙 (impact_engine.py)

| 변경 | 찾는 것 | 예상 오류 |
|---|---|---|
| 컬럼 길이 ↑ | 그 컬럼을 받는 `char[n]`/`VARCHAR[n]` 이 새 길이보다 작음 (indicator 유무 구분) | ORA-01406 |
|  | 받은 변수를 다른 테이블의 더 짧은 컬럼에 저장 / INSERT…SELECT 로 옮겨 담음 | ORA-12899 |
| 컬럼 길이 ↓ | 저장 변수가 새 길이보다 큼 · 기존 데이터 길이 확인 SQL | ORA-12899, ORA-01441 |
| 타입/정밀도 | 정수 변수 자릿수 초과, 숫자↔문자 변수, 숫자가 아닌 상수 비교, DATE 형식 | ORA-01455, 01722, 00932, 01438 |
| NULL 허용 | indicator·NVL 없이 받는 SELECT/FETCH | ORA-01405 |
| NOT NULL | 컬럼을 빠뜨린 INSERT, NULL 저장 | ORA-01400, 01407, 02296 |
| 컬럼 삭제/이름 변경 | 참조하는 모든 SQL, 컬럼 목록 없는 INSERT, SELECT * | ORA-00904, 00913, 01007 |
| 컬럼 추가 | 컬럼 목록 없는 INSERT, NOT NULL 신규 컬럼 | ORA-00947, 01400 |
| 테이블 삭제/이름 변경 | 참조하는 모든 SQL (커서는 OPEN 위치) · 동적 SQL 추정 | ORA-00942 |
| 데이터 변경 | WHERE/DECODE/CASE 상수와 비교해 처리 대상에서 빠짐/추가됨, 1건 조회의 NOT FOUND 처리 여부, PK 아닌 조건의 다건 조회, 같은 컬럼을 다시 쓰는 프로그램 | ORA-01403, 01422, 01405, 01406 |

모든 결과에는 **배치 실행 순서**, **호출 경로**(main → … → 함수), 그리고 영향받은 함수가 쓰는 테이블을
배치상 뒤에서 읽는 프로그램(**뒤 단계 영향**)이 붙습니다. DB 가 연결되어 있으면 건수·값 분포를 확인하는 조회 SQL 을 바로 실행할 수 있습니다.

### 한계 (정적 분석)

- 예상 오류는 **가능성**입니다. 실제 발생은 데이터 값과 실행 경로에 따라 다릅니다.
- 문자열로 조립하는 동적 SQL(`PREPARE`/`EXECUTE IMMEDIATE :buf`)은 C 문자열 안의 테이블 이름만 추정합니다.
- `#ifdef` 분기, SQL 을 감싼 매크로, 호스트 변수를 다른 C 변수로 복사한 뒤의 흐름(`strcpy` 등)은 추적하지 않습니다.
- `WHENEVER` 는 Pro*C 규칙대로 **소스 위치 기준**으로 적용합니다.

## 6. 주요 설정 (환경 변수 / .env)

| 이름 | 기본값 | 설명 |
|---|---|---|
| `OLLAMA_HOST` | http://localhost:11434 | Ollama 주소 |
| `LLM_MODEL` | gemma4:e2b | 답변 모델 (ai_nexacro 와 동일) |
| `LLM_NUM_CTX` | 8192 | 모델 컨텍스트 길이 |
| `ORACLE_USER` / `ORACLE_PASSWORD` / `ORACLE_DSN` | (없음) | DB 접속 |
| `ORACLE_SCHEMAS` | 접속 계정 | 메타데이터를 읽을 스키마 |
| `ORACLE_CLIENT_LIB_DIR` | (없음) | DB 서버가 11g 일 때 Instant Client 19+ 64bit 경로 (thick 모드) |
| `ORACLE_TNS_ADMIN` | (없음) | tnsnames.ora 폴더 — TNS 별칭으로 접속 |
| `DB_BYTES_PER_CHAR` | 3 | `VARCHAR2(n CHAR)` 바이트 계산 |
| `HOST_LONG_BITS` | 64 | Pro*C 서버의 C long 크기 |
| `DB_MAX_ROWS` | 500 | SQL 창 최대 행 |
| `DB_CALL_TIMEOUT_MS` | 30000 | 조회 제한 시간 |
| `MAX_FINDINGS_IN_PROMPT` | 25 | LLM 에 넘기는 분석 결과 수 |

## 7. 폴더 구성

| 경로 | 내용 |
|---|---|
| `app.py` | FastAPI 서버 · API |
| `agent_runtime.py` | LangGraph 에이전트 그래프 (질문 이해 · 보고서) |
| `impact_engine.py` | 영향도 규칙 엔진 (LLM 미사용) |
| `proc_parser.py` | Pro*C 소스 분석 (함수, EXEC SQL, 호스트 변수) |
| `sql_analyzer.py` | EXEC SQL 문 분석 (테이블·컬럼·바인딩·상수) |
| `batch_parser.py` | 배치 스크립트 실행 순서 |
| `source_repository.py` | 업로드 파일 저장 · 분석 인덱스 · 캐시 |
| `db_runtime.py` | Oracle 연결(읽기 전용) · 메타데이터 스냅샷 |
| `prompts.py` | LLM 프롬프트 |
| `static/` | 웹 화면 (`index.html`) |
| `tests/` | 자동 테스트 · 샘플 소스/배치/스냅샷 (`tests/samples`) |
| `source_repository/`, `analysis_index/` | 실행 중 생성 (git 제외) |
| `make_wheels.bat` | `wheels` 폴더 만들기 |

## 8. 테스트

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```
Oracle · Ollama 없이 샘플 소스와 가짜 LLM 으로 돕니다.
