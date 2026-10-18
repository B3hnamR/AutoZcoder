"""Single-file desktop entry: start the gateway, open the dashboard in WebView2.

Replaces the Tkinter GUI. The gateway runs on a background thread; the window
is the served dashboard (/dash) in a native WebView2 shell via pywebview.
A single-instance mutex prevents a second exe from fighting over the port.
"""

from __future__ import annotations

import ctypes
import os
import secrets
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

# Same name the Tkinter build used, so the old and new exe are mutually
# exclusive (they share the port). Session-local: no Local\ prefix needed.
MUTEX_NAME = "ZcodeGatewayByB3hnamR-SingleInstance"
DASH_PATH = "/dash"

_mutex_handle = None


def _exe_dir() -> Path:
    """Folder the tool lives in (the exe's folder when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bootstrap_config() -> Path:
    """First-run config for a standalone, portable install.

    A lone downloaded .exe must work with nothing alongside it: write a
    config.yaml next to the exe with per-install random keys (never a
    hardcoded default), portable relative paths (./accounts, ./logs,
    ./gateway.db — resolved against the config's folder by load_config),
    and create the accounts folder. If the exe's folder is not writable
    (e.g. Program Files), fall back to a per-user data dir so the tool
    still works.
    """
    base = _exe_dir()
    cfg = base / "config.yaml"
    if not cfg.is_file():
        from zcode_gateway.gui import _default_config_yaml
        from zcode_gateway.config import Fingerprint
        import yaml
        data = yaml.safe_load(_default_config_yaml())
        data["upstream"]["fingerprint"] = Fingerprint().model_dump()
        template = yaml.safe_dump(data, sort_keys=False)
        try:
            cfg.write_text(template, encoding="utf-8")
        except OSError:
            base = Path(os.environ.get("LOCALAPPDATA", ".")) / "ZcodeGateway"
            base.mkdir(parents=True, exist_ok=True)
            cfg = base / "config.yaml"
            if not cfg.is_file():
                cfg.write_text(template, encoding="utf-8")
    accounts = cfg.parent / "accounts"
    try:
        accounts.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # both the window and the server thread resolve the same file
    os.environ["ZCODE_GATEWAY_CONFIG"] = str(cfg)
    return cfg


def _already_running() -> bool:
    """Session-local named mutex: True when another copy already holds it."""
    global _mutex_handle
    ERROR_ALREADY_EXISTS = 183
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS


def _notify(message: str) -> None:
    """Minimal message box without pulling in a GUI toolkit."""
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "Zcode Gateway", 0x40)
    except Exception:
        sys.stderr.write(message + "\n")


def _fix_streams() -> None:
    """A --windowed frozen exe has no console; sys.stdout/err may be None.
    pywebview and the app log through these — give them a real file."""
    log_dir = os.path.join(os.environ.get("LOCALAPPDATA", "."), "ZcodeGateway")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "zcode-gateway.log")
    for name in ("stdout", "stderr"):
        try:
            f = open(path, "a", encoding="utf-8", buffering=1)
            setattr(sys, name, f)
        except OSError:
            pass


def _wait_for(url: str, timeout: float = 30.0) -> bool:
    """Poll until the gateway answers /health (then the dashboard can load)."""
    health = url.rstrip("/")[:-len(DASH_PATH)] + "/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def _run_server() -> None:
    import uvicorn
    from zcode_gateway.app import create_app
    from zcode_gateway.config import load_config

    # watchdog: if the gateway thread dies, rebuild and restart it (max 5
    # attempts / 10 minutes) instead of leaving a dead UI behind
    import datetime as _dt
    attempts = 0
    first_start = time.time()
    while True:
        try:
            cfg = load_config(os.environ.get("ZCODE_GATEWAY_CONFIG") or None)
            config = uvicorn.Config(
                create_app(cfg), host=cfg.server.host, port=cfg.server.port,
                log_level="info", access_log=False, log_config=None)
            server = uvicorn.Server(config)
            server.run()
            return  # clean shutdown
        except Exception:
            attempts += 1
            now = time.time()
            print(f"[watchdog] gateway crashed ({_dt.datetime.now():%H:%M:%S}) "
                  f"attempt {attempts}", file=sys.stderr, flush=True)
            if attempts >= 5 or now - first_start > 600:
                print("[watchdog] giving up", file=sys.stderr, flush=True)
                raise
            time.sleep(min(2 ** attempts, 15))


def main() -> None:
    _fix_streams()
    import argparse
    parser = argparse.ArgumentParser(description='Zcode Gateway desktop or headless server')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--config')
    args = parser.parse_args()
    if args.config:
        cfg_path = Path(args.config).resolve()
        os.environ['ZCODE_GATEWAY_CONFIG'] = str(cfg_path)
    else:
        cfg_path = _bootstrap_config()
    if args.headless:
        _run_server()
        return
    if _already_running():
        # another instance holds the mutex. If it's THIS build the dashboard is
        # already live — open it in the browser. If it's the old Tkinter build
        # (no /dash route) that returns 404, so surface a clear message instead.
        from zcode_gateway.config import load_config
        cfg0 = load_config(cfg_path)
        base = f"http://{cfg0.server.host}:{cfg0.server.port}"
        try:
            with urllib.request.urlopen(base + DASH_PATH, timeout=3) as r:
                if r.status == 200:
                    webbrowser.open(base + DASH_PATH)
                    return
        except Exception:
            pass
        _notify("Zcode Gateway is already running.\n\n"
                "The running copy is an older build without the web dashboard.\n"
                "Close it first, then start this one.")
        return

    import webview

    # read host/port from the same config the server will bind
    from zcode_gateway.config import load_config
    cfg = load_config(cfg_path)
    host, port = cfg.server.host, cfg.server.port
    url = f"http://{host}:{port}{DASH_PATH}"

    threading.Thread(target=_run_server, daemon=True, name="gateway").start()
    if not _wait_for(url):
        webbrowser.open(url)   # fall back to the browser rather than a dead window
        return

    webview.create_window(
        "Zcode Gateway", url,
        width=1280, height=800, min_size=(980, 620),
        background_color="#0f1115", text_select=True)
    webview.start(gui="edgechromium")
    # The window closed. edgechromium/pythonnet leave non-daemon threads that
    # keep the interpreter — and with it the port and the single-instance
    # mutex — alive forever, so the next launch hits the "already running"
    # alert. Hard-exit: window close must mean the tool actually quit.
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        sys.stderr.write(traceback.format_exc()[-1200:] + "\n")
