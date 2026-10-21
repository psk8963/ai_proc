"""설치판 실행기: 서버를 띄우고 브라우저를 연다. (소스 폴더에서도 `python launcher.py` 로 쓸 수 있다)

- 데이터 폴더: PROC_AGENT_DATA_DIR → 없으면 %LOCALAPPDATA%\\ProcImpactAgent (설치판) / 프로젝트 폴더(소스 실행)
  등록한 소스·분석 캐시·.env 가 여기에 있어서 프로그램을 업데이트·재설치해도 그대로 남는다.
- 처음 실행하면 .env 를 만들고 메모장으로 열어 DB 접속 정보를 넣게 한다.
- 이미 실행 중이면 새로 띄우지 않고 브라우저만 연다.
- 이 콘솔 창을 닫으면 서버가 종료된다.
"""

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
INSTALLED = (APP_DIR.parent / "python" / "python.exe").exists()   # 설치판 폴더 구조: {app}\python, {app}\app


def data_dir() -> Path:
    if os.getenv("PROC_AGENT_DATA_DIR"):
        return Path(os.environ["PROC_AGENT_DATA_DIR"])
    if INSTALLED:
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ProcImpactAgent"
    return APP_DIR


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def is_our_server(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as r:
            return b'"assets"' in r.read(4096)
    except Exception:
        return False


def main():
    ddir = data_dir()
    ddir.mkdir(parents=True, exist_ok=True)
    os.environ["PROC_AGENT_DATA_DIR"] = str(ddir)
    env_file = ddir / ".env"
    first_run = not env_file.exists()
    if first_run and (APP_DIR / ".env.example").exists():
        shutil.copy(APP_DIR / ".env.example", env_file)

    sys.path.insert(0, str(APP_DIR))
    import config  # noqa: F401  (.env 를 환경 변수로 읽는다)
    from version import __version__

    port = int(os.getenv("PROC_AGENT_PORT", "8001"))
    url = f"http://localhost:{port}/ui/"
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(f"Pro*C 영향도 분석 에이전트 v{__version__} - 이 창을 닫으면 종료됩니다")
        except Exception:
            pass
    print(f"Pro*C 영향도 분석 에이전트 v{__version__}")
    print(f"  데이터 폴더 : {ddir}")
    print(f"  설정 파일   : {env_file}")
    print(f"  화면 주소   : {url}")
    print()

    if port_in_use(port):
        if is_our_server(port):
            print("이미 실행 중입니다. 브라우저만 엽니다.")
            webbrowser.open(url)
            time.sleep(2)
            return
        print(f"[오류] {port} 포트를 다른 프로그램이 쓰고 있습니다. .env 에 PROC_AGENT_PORT=8002 처럼 다른 포트를 지정하세요.")
        input("엔터를 누르면 닫습니다.")
        return

    if first_run:
        print("처음 실행입니다. 메모장에서 DB 접속 정보(ORACLE_USER / ORACLE_PASSWORD / ORACLE_DSN ...)를 넣고 저장한 뒤,")
        print("이 프로그램을 다시 실행하면 적용됩니다. (DB 없이도 소스 분석은 바로 쓸 수 있습니다)")
        print()
        if os.name == "nt":
            subprocess.Popen(["notepad.exe", str(env_file)])

    os.chdir(APP_DIR)
    import uvicorn
    from app import app

    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print("서버를 시작합니다. (종료: 이 창을 닫거나 Ctrl+C)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 설치판에서 창이 바로 닫히지 않게
        import traceback
        traceback.print_exc()
        print(f"\n[오류] {e}")
        input("엔터를 누르면 닫습니다.")
