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

## 0. 설치 프로그램으로 쓰기 (권장)

[Releases](https://github.com/psk8963/ai_proc/releases) 에서 `ProcImpactAgent-Setup-<버전>.exe` 를 받아 실행합니다.

- **관리자 권한 없이** 사용자 폴더(`%LOCALAPPDATA%\Programs\ProcImpactAgent`)에 설치됩니다. Python 도 포함되어 있습니다.
- 시작 메뉴/바탕화면의 **ProC 영향도 분석 에이전트** 를 누르면 Ollama·Claude 앱처럼 **전용 앱 창**이 열립니다
  (주소창·탭 없는 창, Windows 에 기본으로 있는 Edge 엔진 사용). 콘솔 창은 뜨지 않습니다.
- 작업 표시줄 오른쪽 **트레이 아이콘**: 열기 · 설정 파일 · 데이터 폴더 · 로그 보기 · 다시 시작 · 종료.
  앱 창을 닫아도 트레이에 남아 있고(분석 캐시 유지), 트레이에서 **종료**해야 완전히 끝납니다.
- 처음 실행하면 **설정** 창이 열립니다. DB 접속 정보를 넣고 저장 → **다시 시작**. 이후에는 화면 왼쪽 위 **⚙ 설정**에서 바꿉니다.
- 설치할 때 "Windows 시작할 때 트레이에서 자동 실행"을 고르면 로그인 시 트레이로 조용히 시작됩니다.
- 등록한 소스·분석 캐시·설정·로그는 `%LOCALAPPDATA%\ProcImpactAgent` 에 있어 **새 버전을 덮어 설치해도 그대로** 남습니다.
  새 버전 설치 시 실행 중인 앱은 자동으로 종료됩니다.
- Oracle 11g 서버라면 Instant Client 19(64bit)를 따로 풀어 두고 설정의 "Instant Client 19 폴더"에 경로를 넣습니다(4-2 참고).
- 문제가 생기면 시작 메뉴의 **문제 해결 (콘솔 모드)** 로 실행하면 서버 로그를 바로 볼 수 있습니다.

### 새 버전 릴리스하기 (개발자)

1. `version.py` 의 `__version__` 을 올려 커밋합니다. (예: `0.1.1`)
2. 같은 이름의 태그를 push 합니다.
   ```powershell
   git tag v0.1.1
   git push origin main v0.1.1
   ```
3. GitHub Actions(`.github/workflows/release.yml`)가 Windows 에서 테스트 → 설치 파일 빌드 → **Releases 에 자동 등록**합니다.

PC 에서 직접 만들려면 Python 3.14 와 [Inno Setup 6](https://jrsoftware.org/isdl.php) 을 설치하고
`powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1` 을 실행합니다(결과: `build\out\`).


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

### 4-1. 인터넷이 막힌 PC에 설치하기 (wheels 포함)

저장소의 `wheels` 폴더에 필요한 패키지 설치 파일이 모두 들어 있습니다(**Windows 64bit + Python 3.14**, 테스트용 pytest 포함).

```powershell
cd C:\workspace\ai_proc
python -m venv venv
venv\Scripts\activate
python -m pip install --no-index --find-links wheels -r requirements-dev.txt
python -m pytest -q
```

패키지 버전이나 Python 버전을 바꿨으면 인터넷 되는 PC 에서 `make_wheels.bat` 로 `wheels` 를 다시 만듭니다
(끝에 `make_wheels_log.txt` → `ALL_OK`). Ollama 모델은 ai_nexacro 와 같은 것을 쓰므로 추가로 옮길 것이 없습니다.

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
- **DB 접속만 되면 따로 갱신할 필요가 없습니다.**
  - **DB 조회** 탭은 Toad 처럼 스키마의 오브젝트 목록(테이블·뷰·프로시저·함수·패키지·트리거·시퀀스·시노님)을 먼저 가볍게 읽고,
    클릭한 오브젝트의 상세(컬럼·인덱스·제약·소스·참조 관계)만 그때 읽습니다. 실제 데이터는 SQL 창에서 SELECT 로 봅니다.
  - **영향도 분석**은 분석하는 테이블과 같은 프로그램들이 쓰는 테이블의 메타데이터만 그때 읽어 `analysis_index` 에 캐시하고,
    DB 안에서 그 테이블을 쓰는 프로시저·패키지·트리거·뷰(컬럼 이름이 나오는 소스 줄 포함)도 같이 보여줍니다.
- 왼쪽 **메타데이터 저장** 은 소스가 쓰는 테이블 정보를 한 번에 파일로 저장하는 기능입니다(집 PC 오프라인 분석용).
  `ORACLE_META_SCOPE=all` 이면 스키마 전체를 저장합니다(오래 걸리면 `META_CALL_TIMEOUT_MS` 를 늘림).
- 저장된 메타데이터는 `analysis_index\schema_snapshot.json` 입니다.
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

### 동적 SQL 재구성 (dynamic_sql.py)

`PREPARE s FROM :buf` / `EXECUTE IMMEDIATE :buf` / `DECLARE c CURSOR FOR s` 처럼 문자열로 만드는 SQL 은
같은 함수 안에서 그 버퍼를 만드는 C 코드를 따라가 **SQL 문장을 다시 조립**한 뒤 일반 SQL 과 똑같이 분석합니다.

- 따라가는 것: `strcpy`/`strncpy`/`strcat`/`strncat`/`sprintf`/`snprintf`/`memcpy`, `p = "..."`, `#define` 문자열(여러 줄 포함),
  `VARCHAR` 의 `.arr`, `sprintf(buf + strlen(buf), ...)`, 다른 버퍼에 만들어 둔 WHERE 조각을 `strcat` 하는 경우
- `if/else` 로 갈라지는 조립은 모든 갈래를 이어 붙여 **쓰일 수 있는 컬럼을 모두** 찾습니다.
- `USING :a, :b` 는 `:1, :2` 자리 순서대로 호스트 변수에 연결해 길이·타입 규칙을 그대로 적용합니다.
- 실행 중에만 알 수 있는 값(함수 인자, 읽어 온 테이블 이름 등)은 `__V_이름__` 으로 남기고 "일부 값은 실행 시 결정" 으로 표시합니다.
- 결과 표에는 종류가 `SELECT(동적)` 처럼 나오고, 소스·배치 탭의 프로그램 상세에서 재구성한 문장을 볼 수 있습니다.

### 소스 보기 · 색상 표시

- 영향도 결과의 **DB 안의 사용처**(프로시저·패키지·트리거 등)를 누르면 DB 에서 소스를 읽어 바로 보여 주고, 컬럼을 쓰는 줄로 이동합니다(다음 사용 위치 ▼).
- Pro*C 코드 보기와 DB 소스 모두 색으로 구분합니다: **분석 대상 테이블**(노란 배경), **분석 대상 컬럼**(주황 배경),
  그 소스가 쓰는 다른 테이블(청록 밑줄), SQL 키워드(파랑), 문자열(초록), 주석(회색), 호스트 변수(분홍).

### 한계 (정적 분석)

- 예상 오류는 **가능성**입니다. 실제 발생은 데이터 값과 실행 경로에 따라 다릅니다.
- 동적 SQL 은 **같은 함수 안**에서 조립하는 경우만 재구성합니다. 다른 함수에서 만들어 넘겨받은 버퍼, 반복문으로 붙이는 횟수는 추적하지 않습니다(이때는 C 문자열 안의 테이블 이름만 추정).
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
| `desktop.py` | 데스크톱 앱 실행기 (전용 창 + 트레이, 콘솔 없음) |
| `launcher.py` | 콘솔 모드 실행기 (문제 해결용) |
| `settings_store.py` | 앱 안 설정 화면 ↔ `.env` |
| `assets/` | 앱 아이콘 |
| `requirements-app.txt` | 설치판 전용 패키지 (트레이 아이콘) |
| `version.py` | 앱 버전 (릴리스 태그와 같아야 함) |
| `installer/` | 설치 프로그램 빌드 스크립트 (Inno Setup) |
| `.github/workflows/release.yml` | 태그 push 시 설치 프로그램 빌드 · 릴리스 |

## 8. 테스트

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```
Oracle · Ollama 없이 샘플 소스와 가짜 LLM 으로 돕니다.
