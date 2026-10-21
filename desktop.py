"""데스크톱 앱 실행기 (Ollama · Claude 앱처럼).

- 콘솔 창 없이(pythonw) 서버를 백그라운드에서 돌리고, 브라우저 주소창·탭이 없는 **전용 앱 창**을 연다.
  창은 Windows 에 기본으로 깔린 Microsoft Edge(WebView 와 같은 엔진)의 앱 모드를 쓴다. 없으면 Chrome, 그것도 없으면 기본 브라우저.
- 작업 표시줄 알림 영역(트레이)에 아이콘을 띄운다: 열기 · 설정 · 데이터 폴더 · 로그 · 다시 시작 · 종료.
  앱 창을 닫아도 트레이에 남아 있고(분석 캐시 유지), 트레이에서 '종료'해야 서버가 끝난다.
- 이미 실행 중이면 새로 띄우지 않고 창만 연다(단일 실행).
- 로그: <데이터 폴더>\\logs\\app.log

옵션:  --background  창 없이 트레이로만 시작 (Windows 시작 시 자동 실행용)
       --restart     이전 프로세스가 끝나기를 기다렸다가 시작 (앱 안 '다시 시작')
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from launcher import data_dir, is_our_server, port_in_use  # noqa: E402

APP_TITLE = "Pro*C 영향도 분석 에이전트"
ICON_PATH = APP_DIR / "assets" / "app.ico"


# ---------- 공용 ----------

def message_box(text: str, title: str = APP_TITLE, error: bool = False):
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)
            return
        except Exception:
            pass
    print(f"[{title}] {text}")


def open_path(path: Path):
    if os.name == "nt":
        os.startfile(str(path))  # noqa: S606 (탐색기/메모장으로 여는 로컬 경로)
    else:
        subprocess.Popen(["xdg-open", str(path)])


def setup_logging(ddir: Path) -> Path:
    logs = ddir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "app.log"
    if log.exists() and log.stat().st_size > 5 * 1024 * 1024:
        old = logs / "app.log.1"
        if old.exists():
            old.unlink()
        log.rename(old)
    # pythonw 에는 콘솔이 없어서 stdout/stderr 가 None 이다 → 로그 파일로 보낸다
    if sys.stdout is None or sys.stderr is None or "--log-to-file" in sys.argv:
        fh = open(log, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = fh
    return log


# ---------- 앱 창 ----------

def find_app_browser() -> str | None:
    env = os.environ
    cands = [
        Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(env.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(env.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(env.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for c in cands:
        if c.exists():
            return str(c)
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def open_app_window(url: str, ddir: Path):
    """주소창 없는 전용 창. 앱 전용 프로필을 써서 평소 쓰는 브라우저와 섞이지 않는다."""
    exe = find_app_browser()
    if not exe:
        webbrowser.open(url)
        return None
    profile = ddir / "app-window"
    profile.mkdir(parents=True, exist_ok=True)
    args = [exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1440,920",
            "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
            "--disable-sync", "--hide-crash-restore-bubble"]
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    return subprocess.Popen(args, creationflags=flags)


# ---------- 서버 ----------

class ServerThread:
    def __init__(self, port: int):
        import uvicorn
        from app import app
        self.app = app
        self.config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, log_level="info")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, name="server", daemon=True)

    def start(self, timeout: float = 60) -> bool:
        self.thread.start()
        end = time.time() + timeout
        while time.time() < end:
            if self.server.started:
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.1)
        return False

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


# ---------- 트레이 ----------

def run_tray(ctx: dict) -> bool:
    """트레이 아이콘을 띄우고 종료할 때까지 막는다. pystray 가 없으면 False."""
    try:
        import pystray
        from PIL import Image
    except Exception:
        return False

    url, ddir = ctx["url"], ctx["ddir"]

    def on_open(icon=None, item=None):
        open_app_window(url, ddir)

    def on_browser(icon, item):
        webbrowser.open(url)

    def on_quit(icon, item):
        ctx["quit"]()

    def on_restart(icon, item):
        ctx["restart"]()

    menu = pystray.Menu(
        pystray.MenuItem("열기", on_open, default=True),
        pystray.MenuItem("브라우저에서 열기", on_browser),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("설정 파일(.env) 열기", lambda i, it: open_path(ddir / ".env")),
        pystray.MenuItem("데이터 폴더 열기", lambda i, it: open_path(ddir)),
        pystray.MenuItem("로그 보기", lambda i, it: open_path(ddir / "logs" / "app.log")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("다시 시작", on_restart),
        pystray.MenuItem("종료", on_quit),
    )
    image = Image.open(ICON_PATH) if ICON_PATH.exists() else Image.new("RGB", (64, 64), (37, 99, 235))
    icon = pystray.Icon("ProcImpactAgent", image, f"{APP_TITLE} v{ctx['version']}", menu)
    ctx["icon"] = icon
    icon.run()
    return True


# ---------- 메인 ----------

def main():
    background = "--background" in sys.argv
    restarting = "--restart" in sys.argv

    ddir = data_dir()
    ddir.mkdir(parents=True, exist_ok=True)
    os.environ["PROC_AGENT_DATA_DIR"] = str(ddir)
    os.environ["PROC_AGENT_DESKTOP"] = "1"
    log_path = setup_logging(ddir)
    env_file = ddir / ".env"
    first_run = not env_file.exists()
    if first_run and (APP_DIR / ".env.example").exists():
        shutil.copy(APP_DIR / ".env.example", env_file)

    import config  # noqa: F401  (.env → 환경 변수)
    from version import __version__

    port = int(os.getenv("PROC_AGENT_PORT", "8001"))
    url = f"http://localhost:{port}/ui/" + ("?welcome=1" if first_run else "")
    print(f"--- {APP_TITLE} v{__version__} 시작 (port {port}, data {ddir})", flush=True)

    if restarting:
        end = time.time() + 20
        while port_in_use(port) and time.time() < end:
            time.sleep(0.3)

    if port_in_use(port):
        if is_our_server(port):
            if not background:
                open_app_window(url, ddir)
            return
        message_box(f"{port} 포트를 다른 프로그램이 쓰고 있습니다.\n설정 파일(.env)에 PROC_AGENT_PORT=8002 처럼 다른 포트를 지정해 주세요.\n\n{env_file}",
                    error=True)
        return

    os.chdir(APP_DIR)
    try:
        server = ServerThread(port)
    except Exception as e:
        import traceback
        traceback.print_exc()
        message_box(f"앱을 시작하지 못했습니다.\n{e}\n\n로그: {log_path}", error=True)
        return
    if not server.start():
        message_box(f"서버를 시작하지 못했습니다.\n로그를 확인해 주세요: {log_path}", error=True)
        return

    done = threading.Event()
    ctx = {"url": url.split("?")[0], "ddir": ddir, "version": __version__}

    def quit_app():
        server.stop()
        icon = ctx.get("icon")
        if icon:
            icon.stop()
        done.set()

    def restart_app():
        exe = sys.executable
        if os.name == "nt" and exe.lower().endswith("python.exe"):
            pyw = Path(exe).with_name("pythonw.exe")
            exe = str(pyw) if pyw.exists() else exe
        args = [exe, str(Path(__file__).resolve()), "--restart", "--background"]
        if "--log-to-file" in sys.argv:
            args.append("--log-to-file")
        subprocess.Popen(args, cwd=str(APP_DIR))
        threading.Timer(0.3, quit_app).start()

    ctx["quit"], ctx["restart"] = quit_app, restart_app
    # 화면의 '다시 시작' / '종료' 버튼이 이 함수를 부른다 (app.py /api/app/*)
    server.app.state.desktop = {"restart": restart_app, "quit": quit_app, "log": str(log_path)}

    window = None
    if not background:
        window = open_app_window(url, ddir)

    if run_tray(ctx):
        return
    # 트레이를 못 띄우면: 전용 창이 닫힐 때 같이 종료
    if window is not None:
        window.wait()
        quit_app()
    else:
        done.wait()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        message_box(f"예상하지 못한 오류로 종료합니다.\n{e}", error=True)
