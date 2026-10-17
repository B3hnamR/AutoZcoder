"""
Zcode Gateway - desktop client.

A single window that launches the gateway locally and manages a pool of captured
Zcode accounts: import them from the official Zcode app with one click, see every
field, refresh tokens, watch the load balancing, and read live stats.

    python -m zcode_gateway.gui
    ZcodeGateway.exe                (frozen build)

Theme: dark + purple. Branding: "Zcode Gateway, Coded by @B3hnamR".
"""

from __future__ import annotations

import json
import math
import os
import queue
import secrets
import shutil
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk

from .accounts_dir import parse_snapshot, write_snapshot
from .zcode_detect import ZCODE_DOWNLOAD_URL, detect_zcode, jwt_claims
from .client_sync import inspect_kilo, sync_kilo

TELEGRAM_URL = "https://t.me/B3hnamR"
BRAND_TITLE = "Zcode Gateway, Coded by @B3hnamR"

# --------------------------------------------------------------------------- #
# theme: modern dark — neutral charcoal surfaces, single violet accent
# --------------------------------------------------------------------------- #

PALETTE = {
    "bg":          "#0f1115",   # window background (neutral, near-black)
    "panel":       "#151820",   # header / tab bar background
    "card":        "#1a1e27",   # raised cards, toolbars
    "elev":        "#222736",   # hover / pressed elevation
    "field":       "#10131a",   # entries, text widgets, tree field
    "stripe":      "#161a22",   # zebra row on the field background
    "border":      "#262c3a",   # hairline borders
    "accent":      "#8b7cf7",   # primary violet
    "accent_deep": "#7466e0",   # pressed / active
    "accent_soft": "#b3a7fb",   # light violet text accent
    "accent_dim":  "#2c2b45",   # quiet violet fills (selection rows)
    "fg":          "#e8eaf0",
    "muted":       "#9aa1b2",
    "faint":       "#666e80",
    "ok":          "#3ddc84",
    "warn":        "#ffc857",
    "err":         "#ff6b7a",
    "on_accent":   "#0e0b22",   # text on accent fills
}

# account/model state colors, tuned for the dark background
STATE_COLORS = {
    "ready":           "#3ddc84",
    "cooling":         "#ffc857",
    "dead":            "#ff6b7a",
    "exhausted":       "#ff6b7a",
    "paid_exhausted":  "#b3a7fb",
    "available":       "#3ddc84",
    "partial":         "#b3a7fb",
    "blocked":         "#ff6b7a",
    "account":         "#9aa1b2",
}

# type scale — Segoe UI 10 body keeps the UI from feeling cramped/legacy
F_DISPLAY = ("Segoe UI", 16, "bold")
F_H1 = ("Segoe UI Semibold", 13)
F_H2 = ("Segoe UI Semibold", 10)
F_BODY = ("Segoe UI", 10)
F_SMALL = ("Segoe UI", 8)
F_MONO = ("Cascadia Mono", 9)

# spacing grid (px) — 4/8 rhythm, generous at card level
PAD = 14
GAP = 8


def format_cap_release(info: dict, now: float | None = None, *, exact=False) -> str:
    """Convert at the viewer, never at the gateway which may run in UTC."""
    now = time.time() if now is None else now
    try:
        dt = datetime.fromisoformat(info.get("release_at"))
        if dt.tzinfo is None:
            raise ValueError("ambiguous timestamp")
    except (TypeError, ValueError):
        seconds = info.get("release_in_s")
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
            return "Reset time unavailable"
        dt = datetime.fromtimestamp(now + max(0, seconds), timezone.utc)
    local = dt.astimezone()
    seconds = max(0, math.ceil(dt.timestamp() - now))
    minutes = math.ceil(seconds / 60)
    left = (f"{minutes // 60}h {minutes % 60}m" if minutes >= 60
            else f"{minutes}m" if seconds >= 60 else f"{seconds}s")
    if exact and seconds >= 60:
        left += f" ({seconds}s)"
    offset = local.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}"
    return (f"{local:%Y-%m-%d %H:%M:%S} {zone} · "
            + (f"in {left}" if seconds else "Reset due"))


def format_caps(account: dict, now: float | None = None, *, detail=False) -> str:
    now = time.time() if now is None else now
    caps = account.get("model_caps") or {}
    models = sorted(set(caps) | set(account.get("capped_models") or []))
    lines = []
    for model in models:
        info = caps.get(model) or {}
        name = model if detail else model.rsplit("/", 1)[-1]
        line = f"{name} → {format_cap_release(info, now, exact=detail)}" if info else name
        if detail:
            if info.get("code"):
                line += f"\n  {info['code']}"
            if info.get("message"):
                line += f"\n  {info['message']}"
        lines.append(line)
    return ("\n\n" if detail else "; ").join(lines) or "—"


def _dark_titlebar(widget: tk.Misc) -> None:
    """Ask Windows 10/11 for a dark title bar (stdlib only, best effort)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import ctypes.wintypes
        val = ctypes.c_int(1)
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.wintypes.HWND(int(widget.winfo_id())),
            DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


def apply_theme(root: tk.Tk) -> ttk.Style:
    """Modern dark theme: flat controls (no clam bevels), 4/8px rhythm,
    one accent colour, generous touch-friendly paddings."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")       # fully colorable on Windows
    except tk.TclError:
        pass

    p = PALETTE
    root.configure(bg=p["bg"])

    # kill every bevel at the root: lightcolor == darkcolor == surface makes
    # clam draw flat fills instead of 3D edges (the "Windows XP" feel)
    style.configure(".", background=p["bg"], foreground=p["fg"],
                    bordercolor=p["border"], borderwidth=0, focuscolor=p["accent"],
                    lightcolor=p["bg"], darkcolor=p["bg"],
                    troughcolor=p["field"], selectbackground=p["accent_dim"],
                    selectforeground=p["fg"],
                    insertcolor=p["fg"], arrowcolor=p["muted"])

    # -- surfaces --------------------------------------------------------- #
    style.configure("TFrame", background=p["bg"])
    style.configure("Panel.TFrame", background=p["panel"])
    style.configure("Card.TFrame", background=p["card"], relief="flat")

    # -- type ------------------------------------------------------------- #
    style.configure("TLabel", background=p["bg"], foreground=p["fg"],
                    font=F_BODY)
    style.configure("Panel.TLabel", background=p["panel"], foreground=p["fg"])
    style.configure("Card.TLabel", background=p["card"], foreground=p["fg"])
    style.configure("PanelTitle.TLabel", background=p["panel"], font=F_H1,
                    foreground=p["fg"])
    style.configure("CardTitle.TLabel", background=p["card"], font=F_H2,
                    foreground=p["fg"])
    style.configure("H1.TLabel", background=p["bg"], font=F_H1, foreground=p["fg"])
    # section labels: uppercase micro-caps, quiet — hierarchy without boxes
    style.configure("H2.TLabel", background=p["bg"], font=F_SMALL,
                    foreground=p["faint"])
    style.configure("CardH2.TLabel", background=p["card"], font=F_SMALL,
                    foreground=p["faint"])
    style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
    style.configure("Faint.TLabel", background=p["bg"], foreground=p["faint"],
                    font=F_SMALL)
    style.configure("PanelMuted.TLabel", background=p["panel"],
                    foreground=p["muted"], font=F_BODY)
    style.configure("CardMuted.TLabel", background=p["card"],
                    foreground=p["muted"])
    style.configure("Toast.TLabel", font=F_BODY)

    link_font = ("Segoe UI", 9, "underline")
    style.configure("Link.TLabel", background=p["bg"], foreground=p["accent_soft"],
                    font=link_font, cursor="hand2")
    style.configure("PanelLink.TLabel", background=p["panel"],
                    foreground=p["accent_soft"], font=link_font, cursor="hand2")
    style.configure("CardLink.TLabel", background=p["card"],
                    foreground=p["accent_soft"], font=link_font, cursor="hand2")

    # -- buttons ----------------------------------------------------------- #
    # primary: solid accent pill (flat — borderwidth 0 + matched light/dark)
    style.configure("TButton", background=p["accent"], foreground=p["on_accent"],
                    bordercolor=p["accent"], lightcolor=p["accent"],
                    darkcolor=p["accent"], relief="flat", borderwidth=0,
                    padding=(16, 8), font=("Segoe UI Semibold", 10),
                    anchor="center")
    style.map("TButton",
              background=[("pressed", p["accent_deep"]),
                          ("active", p["accent_deep"]),
                          ("disabled", p["elev"])],
              foreground=[("disabled", p["faint"])],
              lightcolor=[("pressed", p["accent_deep"]),
                          ("active", p["accent_deep"])],
              darkcolor=[("pressed", p["accent_deep"]),
                         ("active", p["accent_deep"])])
    # ghost: card surface, hairline border, elevates on hover
    style.configure("Ghost.TButton", background=p["card"], foreground=p["fg"],
                    bordercolor=p["border"], lightcolor=p["card"],
                    darkcolor=p["card"], relief="flat", borderwidth=1,
                    padding=(12, 7), font=F_BODY)
    style.map("Ghost.TButton",
              background=[("pressed", p["elev"]),
                          ("active", p["elev"]),
                          ("disabled", p["card"])],
              foreground=[("disabled", p["faint"])],
              bordercolor=[("active", p["accent_dim"])])
    # quiet: borderless text button for toolbar actions
    style.configure("Quiet.TButton", background=p["card"], foreground=p["muted"],
                    bordercolor=p["card"], lightcolor=p["card"],
                    darkcolor=p["card"], relief="flat", borderwidth=0,
                    padding=(10, 6), font=F_BODY)
    style.map("Quiet.TButton",
              background=[("active", p["elev"]), ("pressed", p["elev"])],
              foreground=[("active", p["fg"]), ("disabled", p["faint"])])

    # -- inputs ------------------------------------------------------------ #
    style.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"],
                    insertcolor=p["fg"], bordercolor=p["border"],
                    lightcolor=p["field"], darkcolor=p["field"],
                    padding=(8, 6))
    style.map("TEntry",
              bordercolor=[("focus", p["accent"])],
              lightcolor=[("focus", p["field"])],
              darkcolor=[("focus", p["field"])])
    style.configure("TCombobox", fieldbackground=p["field"], foreground=p["fg"],
                    arrowcolor=p["muted"], background=p["card"],
                    bordercolor=p["border"], lightcolor=p["field"],
                    darkcolor=p["field"], padding=(8, 5))
    style.map("TCombobox",
              fieldbackground=[("readonly", p["field"])],
              background=[("active", p["card"])],
              bordercolor=[("focus", p["accent"])])
    # flat listbox behind the combobox popup
    root.option_add("*TCombobox*Listbox.background", p["card"])
    root.option_add("*TCombobox*Listbox.foreground", p["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", p["accent_dim"])
    root.option_add("*TCombobox*Listbox.selectForeground", p["fg"])
    root.option_add("*TCombobox*Listbox.font", F_BODY)
    root.option_add("*TCombobox*Listbox.relief", "flat")
    root.option_add("*TCombobox*Listbox.borderwidth", 0)

    style.configure("TCheckbutton", background=p["card"], foreground=p["muted"],
                    font=F_BODY, focuscolor=p["card"])
    style.map("TCheckbutton",
              background=[("active", p["card"])],
              foreground=[("active", p["fg"]), ("disabled", p["faint"])],
              indicatorcolor=[("selected", p["accent"]), ("!selected", p["field"])],
              lightcolor=[("selected", p["card"]), ("!selected", p["card"])],
              darkcolor=[("selected", p["card"]), ("!selected", p["card"])])

    # -- notebook: borderless quiet tabs, accent text when selected -------- #
    style.configure("TNotebook", background=p["bg"], bordercolor=p["bg"],
                    lightcolor=p["bg"], darkcolor=p["bg"], borderwidth=0,
                    tabmargins=(10, 8, 10, 0), tabposition="nw")
    style.configure("TNotebook.Tab", background=p["bg"], foreground=p["muted"],
                    padding=(18, 9), font=("Segoe UI Semibold", 10),
                    bordercolor=p["bg"], lightcolor=p["bg"], darkcolor=p["bg"],
                    borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", p["card"]), ("active", p["elev"])],
              foreground=[("selected", p["accent_soft"]),
                          ("active", p["fg"])],
              lightcolor=[("selected", p["card"]), ("active", p["elev"])],
              darkcolor=[("selected", p["card"]), ("active", p["elev"])])

    # -- treeview: tall rows, subtle zebra, quiet uppercase headers -------- #
    style.configure("Treeview", background=p["field"], foreground=p["fg"],
                    fieldbackground=p["field"], bordercolor=p["field"],
                    lightcolor=p["field"], darkcolor=p["field"],
                    borderwidth=0, relief="flat", rowheight=34, font=F_BODY)
    style.map("Treeview",
              background=[("selected", p["accent_dim"])],
              foreground=[("selected", p["fg"])],
              lightcolor=[("selected", p["accent_dim"])],
              darkcolor=[("selected", p["accent_dim"])])
    style.configure("Treeview.Heading", background=p["card"],
                    foreground=p["faint"], relief="flat", borderwidth=0,
                    lightcolor=p["card"], darkcolor=p["card"],
                    font=("Segoe UI Semibold", 8), padding=(10, 9))
    style.map("Treeview.Heading", background=[("active", p["card"])])

    # -- scrollbars: slim, flat, dark -------------------------------------- #
    style.configure("Vertical.TScrollbar", background=p["card"],
                    troughcolor=p["bg"], bordercolor=p["bg"],
                    lightcolor=p["card"], darkcolor=p["card"],
                    arrowcolor=p["faint"], relief="flat", arrowsize=11)
    style.map("Vertical.TScrollbar",
              background=[("active", p["elev"]), ("pressed", p["elev"])])
    style.configure("Horizontal.TScrollbar", background=p["card"],
                    troughcolor=p["bg"], bordercolor=p["bg"],
                    lightcolor=p["card"], darkcolor=p["card"],
                    arrowcolor=p["faint"], relief="flat", arrowsize=11)
    style.map("Horizontal.TScrollbar",
              background=[("active", p["elev"]), ("pressed", p["elev"])])
    style.configure("TSeparator", background=p["border"])
    return style


# --------------------------------------------------------------------------- #
# paths - work both from source and from a frozen exe
# --------------------------------------------------------------------------- #


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def base_dir() -> Path:
    """Where config/accounts/logs live.

    Portable by default: the exe's folder. If that folder is not writable
    (Program Files, some corporate lockdowns), fall back to
    %LOCALAPPDATA%/ZcodeGateway so a read-only install still works.
    """
    base = app_dir()
    try:
        probe = base / ".zcode-gateway-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return base
    except OSError:
        local = os.environ.get("LOCALAPPDATA", str(Path.home()))
        fallback = Path(local) / "ZcodeGateway"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


# Written on first launch when no config.yaml or config.example.yaml is present.
# This is what lets a lone .exe work with nothing alongside it.
# Keys are per-install random, never a shared hardcoded default.
def _default_config_yaml() -> str:
    return DEFAULT_CONFIG_YAML.replace(
        "gw-admin-local", f"gw-admin-{secrets.token_hex(8)}").replace(
        "gw-local-1", f"gw-client-{secrets.token_hex(8)}")


DEFAULT_CONFIG_YAML = 'server:\n  host: 127.0.0.1\n  port: 8788\n  require_client_key: true\n  admin_key: gw-admin-local\n  client_keys:\n    - key: gw-local-1\n      name: local\nupstream:\n  base_url: https://zcode.z.ai/api/v1\n  chat_path: /zcode-plan/anthropic/v1/messages\n  timeout_connect: 15\n  timeout_read: 600\n  max_attempts: 3\npool:\n  strategy: least_in_flight\n  cooldown_seconds: 30\n  max_in_flight_per_account: 4\n  acquire_wait_seconds: 5\n  balance_poll_seconds: 60\naccounts:\n  source: accounts_dir\n  providers_json: "~/.zcode/v2/credentials.json"\n  dir: ./accounts\n  pool_file: ./pool.json\nmodels:\n  aliases:\n    glm-5.3: GLM-5.3\n    glm-5.3-flash: GLM-5.3-Flash\n  default: GLM-5.3-Flash\n  default_anthropic: GLM-5.3\n  default_max_tokens: 128000\n  probe_unknown: false\n  auto_free_fallback:\n    enabled: false\n    chain: [GLM-5.3, GLM-5.3-Flash]\nlogging:\n  level: INFO\n  capture: false\n  capture_dir: ./logs\nstore:\n  sqlite_path: ./gateway.db\nupdate:\n  enabled: false\n  repo: B3hnamR/ZcodeGate\ngenerator:\n  enabled: false\n  zcodeauto_dir: ""\n  python_exe: ""\n  invite_code: ""\n  socks_only: true\n'



def find_config() -> Path | None:
    """Resolve the config file, creating one on first run.

    Order: an existing config.yaml beside the exe/source, then a bundled
    config.example.yaml (in PyInstaller's _MEIPASS), then a built-in default
    written to disk. The last fallback is what makes a copied .exe work with no
    files alongside it.
    """
    base = base_dir()
    cfg = base / "config.yaml"
    if cfg.is_file():
        return cfg

    candidates = [base / "config.example.yaml", app_dir() / "config.example.yaml"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "config.example.yaml")

    for example in candidates:
        if example.is_file():
            try:
                shutil.copyfile(example, cfg)
                return cfg
            except OSError:
                break

    try:
        cfg.write_text(_default_config_yaml(), encoding="utf-8")
        return cfg
    except OSError:
        return None


def _normalize_config(cfg_path: Path | None) -> None:
    """First-run repair: point the config at a real accounts folder.

    A config copied from the example (or written fresh) can point at
    "../accounts" relative to a folder that has no such directory. Resolve to
    a folder that actually holds snapshots — the walk-up in
    find_accounts_dir() covers the repo layout (exe in dist/, accounts/ two
    levels up) — and only create an empty local folder when nothing
    discoverable exists (true fresh-folder install). Existing configs whose
    target is populated are never touched.
    """
    if cfg_path is None:
        return
    import yaml
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if not isinstance(data, dict):
        return

    accounts = data.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        return
    raw_dir = accounts.get("dir")
    if raw_dir:
        target = Path(str(raw_dir))
        if not target.is_absolute():
            target = (cfg_path.parent / target).resolve()
        if target.is_dir() and any(target.glob("*.txt")):
            return                          # a real, populated dir: leave it

    populated = find_accounts_dir()
    if populated.is_dir() and any(populated.glob("*.txt")):
        accounts["dir"] = str(populated)   # e.g. repo layout: accounts/ up-tree
    else:
        accounts["dir"] = str(base_dir() / "accounts")
        try:
            (base_dir() / "accounts").mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    try:
        cfg_path.write_text(yaml.safe_dump(data, sort_keys=False),
                            encoding="utf-8")
    except OSError:
        pass


def find_accounts_dir() -> Path:
    """Locate the accounts/ folder.

    Preference order: a folder that exists AND holds snapshots (so the repo's
    ../accounts wins over an empty ./accounts), then any existing folder, then
    <base>/accounts for a fresh install.
    """
    base = base_dir()
    here = app_dir()
    candidates = [base / "accounts", here / "accounts",
                  *(p / "accounts" for p in list(here.parents)[:3])]

    populated = next((c for c in candidates
                      if c.is_dir() and any(c.glob("*.txt"))), None)
    if populated:
        return populated
    existing = next((c for c in candidates if c.is_dir()), None)
    return existing or base / "accounts"


# Test/overridable hooks; when None the path is resolved lazily at call time.
# (Constants resolved at import time bake in the importing user's profile —
# wrong when the exe later runs elevated / as another session.)
ZCODE_PROVIDERS: Path | None = None
ZCODE_GLOBAL: Path | None = None


def _zcode_global() -> Path:
    if ZCODE_GLOBAL is not None:
        return ZCODE_GLOBAL
    return Path(os.path.expanduser("~/.zcode/data/globalState.json"))


def _zcode_providers() -> Path:
    if ZCODE_PROVIDERS is not None:
        return ZCODE_PROVIDERS
    return Path(os.path.expanduser("~/.zcode/v2/credentials.json"))
DEFAULT_FREE_FALLBACK_CHAIN = [
    "GLM-5.3",
    "GLM-5.3-Flash",
]


def icon_paths() -> list[Path]:
    """Where the window icon may live (source tree, exe dir, frozen bundle)."""
    return [
        app_dir() / "assets" / "zcode.png",
        base_dir() / "assets" / "zcode.png",
        *( [Path(sys._MEIPASS) / "assets" / "zcode.png"]
           if getattr(sys, "_MEIPASS", None) else [] ),
    ]


# --------------------------------------------------------------------------- #
# server controller
# --------------------------------------------------------------------------- #


class ServerController:
    """Runs uvicorn in a background thread and reports state."""

    def __init__(self) -> None:
        self._server = None
        self._thread: threading.Thread | None = None
        self.host = "127.0.0.1"
        self.port = 8788
        self.error: str | None = None
        self.accounts_loaded = 0
        self.effective_accounts_dir: str | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, cfg_path: Path | None) -> None:
        if self.running:
            return
        self.error = None

        def run() -> None:
            try:
                import uvicorn
                from zcode_gateway.app import create_app
                from zcode_gateway.config import load_config

                cfg = load_config(str(cfg_path) if cfg_path else None)
                self.host, self.port = cfg.server.host, cfg.server.port

                # If the configured accounts folder is not there (e.g. a default
                # config next to a copied exe), fall back to the folder we found.
                if (cfg.accounts.source == "accounts_dir"
                        and not Path(cfg.accounts.dir).is_dir()):
                    discovered = find_accounts_dir()
                    if discovered.is_dir():
                        cfg.accounts.dir = str(discovered)
                # remember the dir the server actually used — the Settings label
                # shows this, not the raw file value (which may have been
                # normalised or fallen back above)
                self.effective_accounts_dir = cfg.accounts.dir

                try:
                    from zcode_gateway.pool import load_accounts
                    self.accounts_loaded = len(load_accounts(cfg.accounts))
                except Exception:
                    self.accounts_loaded = 0

                app = create_app(cfg)
                # log_config=None stops uvicorn installing its own stdout-based
                # handlers. In a --windowed frozen build sys.stdout can still be
                # None, and uvicorn's default config would then raise on a
                # background thread - the window opens but nothing ever binds.
                config = uvicorn.Config(app, host=self.host, port=self.port,
                                        log_level="info", access_log=False,
                                        log_config=None)
                self._server = uvicorn.Server(config)
                self._server.install_signal_handlers = lambda: None
                self._server.run()
            except Exception as exc:                       # surfaced in the UI
                self.error = f"{exc.__class__.__name__}: {exc}"

        self._thread = threading.Thread(target=run, name="gateway", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 5.0) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            # actually wait for uvicorn to release the port — a 10 ms join let
            # a manual Stop → quick Start race "address already in use". Called
            # from the UI thread, so bound the wait and leave the thread
            # referenced (not dropped) if shutdown overruns.
            thread.join(timeout=wait)
        self._server = None
        if thread is None or not thread.is_alive():
            self._thread = None

    def wait_stopped(self, timeout: float = 10.0) -> bool:
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        if thread is None or not thread.is_alive():
            self._thread = None
            return True
        return False

    def wait_ready(self, timeout: float = 25.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.error:
                return False
            try:
                with urllib.request.urlopen(self.base_url + "/health", timeout=1):
                    return True
            except Exception:
                time.sleep(0.3)
        return False


# --------------------------------------------------------------------------- #
# tiny admin client (stdlib only - keeps the frozen build small)
# --------------------------------------------------------------------------- #


class Api:
    def __init__(self, controller: ServerController, admin_key: str) -> None:
        self.c = controller
        self.key = admin_key

    def _call(self, path: str, method: str = "GET", body: dict | None = None,
              timeout: float = 12.0):
        url = self.c.base_url + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8") or "{}")
            except Exception:
                return e.code, {}
        except Exception as e:
            return 0, {"error": str(e)}

    def pool_state(self):
        return self._call("/admin/pool/state")

    def stats(self):
        return self._call("/admin/stats")

    def accounts(self):
        return self._call("/admin/accounts")

    def refresh(self, aid):
        return self._call(f"/admin/accounts/{aid}/refresh", "POST")

    def enable(self, aid):
        return self._call(f"/admin/accounts/{aid}/enable", "POST")

    def balance(self, aid):
        return self._call(f"/admin/accounts/{aid}/balance", "POST")

    def remove(self, aid):
        return self._call(f"/admin/accounts/{aid}", "DELETE")

    def reload_pool(self):
        return self._call("/admin/pool/reload", "POST")

    def availability(self):
        return self._call("/admin/models/availability", timeout=25.0)


# --------------------------------------------------------------------------- #
# account import from the official Zcode app
# --------------------------------------------------------------------------- #


def import_from_zcode_config(retries=2, output_dir=None):
    from .zcode_accounts import import_credentials
    return import_credentials(_zcode_providers(), output_dir or find_accounts_dir())


def format_balance(entry):
    quotas = entry.get("quotas") or entry.get("notes", {}).get("quotas", {})
    return "; ".join(f"{m}: {q['remaining']:,}/{q['total']:,}" for m,q in quotas.items()) or "Not checked"



class App(tk.Tk):
    POLL_MS = 2500
    ZCODE_POLL_MS = 30_000

    def __init__(self, autostart: bool = True) -> None:
        super().__init__()
        self.title(BRAND_TITLE)
        self.geometry("1280x800")
        self.minsize(1060, 680)

        self.cfg_path = find_config()
        _normalize_config(self.cfg_path)
        self.accounts_dir = self._configured_accounts_dir()
        self.admin_key = self._read_admin_key()

        self.server = ServerController()
        self.api = Api(self.server, self.admin_key)
        self.log_queue: queue.Queue[str] = queue.Queue()
        self._polling = False      # admin-API poll in-flight guard
        self._importing = False    # import button in-flight guard
        self._log_lines: list[tuple[str, str]] = []   # (level, text) ring
        self._log_filter = "all"
        self._last_state: dict | None = None
        self._last_avail: dict | None = None
        self._toast_after: str | None = None
        self._lifecycle = 0
        self._stopping = False
        self._restart_pending = False
        self._restart_message: str | None = None

        self._style = apply_theme(self)
        self._set_icon()

        self._build()
        _dark_titlebar(self)

        if autostart:
            self.after(300, self.start_everything)
        self.after(self.POLL_MS, self.poll)
        self.after(200, self._refresh_zcode_status)
        self.after(self.ZCODE_POLL_MS, self._zcode_timer)
        self.after(1500, self._check_client_reasoning)

    # -- startup helpers --------------------------------------------------- #

    def _set_icon(self) -> None:
        for path in icon_paths():
            if path.is_file():
                try:
                    self._icon_img = tk.PhotoImage(file=str(path))
                    self.iconphoto(True, self._icon_img)
                    return
                except tk.TclError:
                    continue

    def _read_admin_key(self) -> str:
        try:
            import yaml
            data = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8")) or {}
            return (data.get("server") or {}).get("admin_key", "gw-admin-change-me")
        except Exception:
            return "gw-admin-change-me"

    def _read_client_key(self) -> str:
        try:
            import yaml
            data = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8")) or {}
            keys = (data.get("server") or {}).get("client_keys") or []
            return keys[0]["key"] if keys else "gw-local-1"
        except Exception:
            return "gw-local-1"

    def _cfg_get(self, dotted: str):
        try:
            import yaml
            data = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8")) or {}
            cur = data
            for part in dotted.split("."):
                cur = (cur or {}).get(part)
            return "" if cur is None else cur
        except Exception:
            return ""

    def _configured_accounts_dir(self) -> Path:
        raw = self._cfg_get("accounts.dir")
        if raw:
            path = Path(str(raw))
            return path if path.is_absolute() else (self.cfg_path.parent / path).resolve()
        return find_accounts_dir()

    # -- layout ------------------------------------------------------------ #

    def _build(self) -> None:
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(18, 14, 18, 12))
        bar.pack(fill="x")

        # brand block: accent mark + wordmark + coder link
        brand = ttk.Frame(bar, style="Panel.TFrame")
        brand.pack(side="left")
        mark = tk.Canvas(brand, width=12, height=42, bg=PALETTE["panel"],
                         highlightthickness=0)
        mark.pack(side="left", padx=(0, 12))
        mark.create_rectangle(0, 0, 5, 40, fill=PALETTE["accent"], width=0)
        btxt = ttk.Frame(brand, style="Panel.TFrame")
        btxt.pack(side="left")
        ttk.Label(btxt, text="Zcode Gateway", style="PanelTitle.TLabel",
                  font=F_H1).pack(anchor="w")
        link = ttk.Label(btxt, text="Coded by @B3hnamR  \u00b7  t.me/B3hnamR",
                         style="PanelLink.TLabel", font=F_SMALL)
        link.pack(anchor="w", pady=(2, 0))
        link.bind("<Button-1>", lambda e: webbrowser.open(TELEGRAM_URL))

        # right: Start / Stop
        self.start_btn = ttk.Button(bar, text="Start",
                                    command=self.start_everything)
        self.stop_btn = ttk.Button(bar, text="Stop", style="Ghost.TButton",
                                   command=self.stop_everything)
        self.stop_btn.pack(side="right", padx=(0, 10))
        self.start_btn.pack(side="right")

        # status chips: slim card pills (server, Zcode), pool summary right
        self.status_chip = ttk.Frame(bar, style="Card.TFrame", padding=(12, 6))
        self.status_chip.pack(side="right", padx=8)
        self.status_dot = tk.Label(self.status_chip, text="\u25cf",
                                   font=("Segoe UI", 8), bg=PALETTE["card"],
                                   fg=PALETTE["muted"])
        self.status_dot.pack(side="left")
        self.status_lbl = ttk.Label(self.status_chip, text="starting\u2026",
                                    style="CardMuted.TLabel")
        self.status_lbl.pack(side="left", padx=(7, 0))

        self.zcode_chip = ttk.Frame(bar, style="Card.TFrame", padding=(12, 6))
        self.zcode_dot = tk.Label(self.zcode_chip, text="\u25cf",
                                  font=("Segoe UI", 8), bg=PALETTE["card"],
                                  fg=PALETTE["muted"])
        self.zcode_dot.pack(side="left")
        self.zcode_lbl = ttk.Label(self.zcode_chip, text="Zcode: checking\u2026",
                                   style="CardMuted.TLabel")
        self.zcode_lbl.pack(side="left", padx=(7, 0))
        self.zcode_chip.pack(side="right", padx=(0, 10))

        self.ready_lbl = ttk.Label(bar, text="", style="PanelMuted.TLabel",
                                   font=F_SMALL)
        self.ready_lbl.pack(side="right", padx=14)

        # toast line
        self.toast_frame = ttk.Frame(self, style="Card.TFrame", padding=(12, 5))
        self.toast_lbl = ttk.Label(self.toast_frame, text="", style="Toast.TLabel")
        self.toast_lbl.pack(anchor="w", padx=4, pady=3)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.nb = nb

        self._tab_accounts(nb)
        self._tab_models(nb)
        self._tab_server(nb)
        self._tab_settings(nb)
        self._tab_stats(nb)
        self._tab_logs(nb)

    def _select_tab(self, title: str) -> None:
        """Select a notebook tab by its display title (order-independent)."""
        for tab_id in self.nb.tabs():
            if self.nb.tab(tab_id, "text") == title:
                self.nb.select(tab_id)
                return

    def toast(self, msg: str, kind: str = "info") -> None:
        """Non-modal feedback line at the bottom of the window."""
        color = {"info": PALETTE["accent_soft"], "ok": PALETTE["ok"],
                 "warn": PALETTE["warn"], "err": PALETTE["err"]}.get(
                     kind, PALETTE["accent_soft"])
        self.toast_lbl.configure(text=msg, foreground=color)
        self.toast_frame.pack(fill="x", side="bottom", before=self.nb)
        if self._toast_after:
            self.after_cancel(self._toast_after)
        self._toast_after = self.after(5000, lambda: self.toast_frame.pack_forget())

    # -- accounts tab ------------------------------------------------------ #

    def _tab_accounts(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=14)
        nb.add(f, text="Accounts")

        # toolbar card: import (accent) / quiet actions / search right
        toolbar = ttk.Frame(f, style="Card.TFrame", padding=(12, 9))
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="\u2b73  Import from Zcode",
                   command=self.act_import).pack(side="left")
        ttk.Button(toolbar, text="Reload pool", style="Ghost.TButton",
                   command=self.act_reload).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="Refresh", style="Quiet.TButton",
                   command=lambda: self.poll(force=True)).pack(
                       side="left", padx=(4, 0))
        ttk.Button(toolbar, text="Reasoning controls", style="Quiet.TButton",
                   command=self.act_sync_clients).pack(side="left", padx=(4, 0))
        self.acct_filter = tk.StringVar()
        self.acct_filter.trace_add("write", lambda *_: self._apply_filters())
        ttk.Label(toolbar, text="\u2315", style="CardMuted.TLabel",
                  font=("Segoe UI", 11)).pack(side="right", padx=(10, 6))
        ttk.Entry(toolbar, textvariable=self.acct_filter, width=26).pack(
            side="right")

        # onboarding panel (0 accounts) — centered card, steps inline
        self.onboard = ttk.Frame(f, style="Card.TFrame", padding=24)
        ob = self.onboard
        ttk.Label(ob, text="No accounts yet", style="CardTitle.TLabel",
                  font=F_H1).pack(anchor="w")
        ttk.Label(ob, style="CardMuted.TLabel", justify="left",
                  text=("Get started in three steps \u2014 your account joins "
                        "the load-balancing pool immediately."))\
            .pack(anchor="w", pady=(4, 12))
        steps = ttk.Frame(ob, style="Card.TFrame")
        steps.pack(fill="x")
        for i, step in enumerate((
                "1   Install the official Zcode app",
                "2   Sign in inside Zcode",
                "3   Click Import \u2014 done")):
            cell = ttk.Frame(steps, style="Card.TFrame", padding=(0, 2))
            cell.grid(row=0, column=i, sticky="w", padx=(0, 22))
            ttk.Label(cell, text=step.split()[0], foreground=PALETTE["accent"],
                      font=("Segoe UI", 13, "bold")).pack(side="left")
            ttk.Label(cell, text=step[1:].strip(), style="Card.TLabel").pack(
                side="left", padx=(8, 0))
        btns_ob = ttk.Frame(ob, style="Card.TFrame")
        btns_ob.pack(anchor="w", pady=(14, 0))
        ttk.Button(btns_ob, text="Import from Zcode",
                   command=self.act_import).pack(side="left")
        ttk.Button(btns_ob, text="Open the Zcode download page",
                   style="Ghost.TButton",
                   command=lambda: webbrowser.open(ZCODE_DOWNLOAD_URL)).pack(
                       side="left", padx=8)
        ttk.Label(btns_ob, text="Coded by @B3hnamR  \u00b7  "
                  "t.me/B3hnamR", style="CardLink.TLabel").pack(
                      side="left", padx=8)

        # tree card: tree + scrollbar packed together
        body = ttk.Frame(f)
        body.pack(fill="both", expand=True)
        self.acct_body = body
        cols = ("email", "id", "state", "paid_lane", "expires", "balance",
                "inflight", "capped", "file")
        widths = (200, 220, 70, 95, 120, 85, 55, 360, 175)
        headings = {
            "email": "EMAIL", "id": "ACCOUNT ID", "state": "STATE",
            "paid_lane": "PAID LANE", "expires": "TOKEN EXPIRES",
            "balance": "BALANCE $", "inflight": "RUNS",
            "capped": "CAPPED MODELS", "file": "SOURCE FILE",
        }
        self.tree = ttk.Treeview(body, columns=cols, show="headings", height=14)
        for c, w in zip(cols, widths):
            self.tree.heading(c, text=headings.get(c, c.title()))
            self.tree.column(c, width=w, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        hsb = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.configure(xscrollcommand=hsb.set)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.tree.tag_configure("odd", background=PALETTE["stripe"])
        for tag, color in STATE_COLORS.items():
            self.tree.tag_configure(tag, foreground=color)

        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_detail())
        self.tree.bind("<Double-1>", lambda e: self._show_detail())
        self.tree.bind("<Button-3>", self._acct_menu)

        # detail card: selection summary left, row actions right
        detail = ttk.Frame(f, style="Card.TFrame", padding=(12, 10))
        detail.pack(fill="x", pady=(8, 0))
        self.detail_lbl = ttk.Label(detail, text="Select an account for details",
                                    style="CardMuted.TLabel", font=F_MONO)
        self.detail_lbl.pack(side="left", anchor="w")
        for text, cmd in (("Check balance", self.act_balance),
                          ("Refresh token", self.act_refresh),
                          ("Enable", self.act_enable),
                          ("Remove", self.act_remove)):
            ttk.Button(detail, text=text, style="Ghost.TButton",
                       command=cmd).pack(side="right", padx=(6, 0))
        cap_detail = ttk.Frame(f)
        cap_detail.pack(fill="x", pady=(4, 0))
        self.cap_detail = tk.Text(cap_detail, height=5, wrap="word", font=F_MONO,
                                  background=PALETTE["field"], foreground=PALETTE["fg"],
                                  relief="flat", padx=12, pady=8, state="disabled")
        self.cap_detail.pack(side="left", fill="both", expand=True)
        cap_scroll = ttk.Scrollbar(cap_detail, command=self.cap_detail.yview)
        cap_scroll.pack(side="right", fill="y")
        self.cap_detail.configure(yscrollcommand=cap_scroll.set)

        ttk.Label(
            f, style="Faint.TLabel", justify="left",
            text=("Daily quota is per GLM model. Quota exhaustion ("
                  "\u2264 0 marks it exhausted). Exhausted accounts keep serving "
                  "other GLM models. Balance shows the live polled "
                  "value; ~ means import-time figure, not yet re-polled "
                  "(use Check balance for a fresh reading). Right-click "
                  "an account for actions.")
        ).pack(anchor="w", pady=(6, 0))

    def _acct_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        menu = tk.Menu(self, tearoff=0, bg=PALETTE["card"], fg=PALETTE["fg"],
                       activebackground=PALETTE["elev"],
                       activeforeground=PALETTE["accent_soft"],
                       borderwidth=1, relief="flat")
        menu.add_command(label="Refresh token", command=self.act_refresh)
        menu.add_command(label="Enable", command=self.act_enable)
        menu.add_command(label="Check balance", command=self.act_balance)
        menu.add_command(label="Copy account id", command=self._copy_id)
        menu.add_separator()
        menu.add_command(label="Open snapshot file", command=self._open_snapshot)
        menu.add_command(label="Remove from pool", command=self.act_remove)
        menu.tk_popup(event.x_root, event.y_root)

    def _copy_id(self) -> None:
        aid = self.selected_id()
        if aid:
            self.clipboard_clear()
            self.clipboard_append(aid)
            self.toast(f"copied {aid}", "ok")

    def _open_snapshot(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        path = self._configured_accounts_dir() / (vals[8] or "")
        if path.is_file():
            os.startfile(str(path))         # noqa: S602 - user-initiated
        else:
            self.toast("snapshot file not found", "warn")

    def selected_id(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.item(sel[0], "values")[1]

    def _show_detail(self) -> None:
        sel = self.tree.selection()
        if not sel:
            self.detail_lbl.configure(text="")
            self._set_cap_detail("")
            return
        v = self.tree.item(sel[0], "values")
        account = next((a for a in (self._last_state or {}).get("detail", [])
                        if a.get("id") == v[1]), {})
        caps = format_caps(account, detail=True)
        self._set_cap_detail("Cap resets · local time (estimated)\n\n" + caps
                             if caps != "—" else "No known model caps on this account.")
        self.detail_lbl.configure(
            text=(f"Account: {v[0] or '(no email)'}  \u00b7  id {v[1]}\n"
                  f"state {v[2]}  \u00b7  paid lane {v[3]}  \u00b7  "
                  f"balance {v[5]} USD  \u00b7  token {v[4]}"
                   f"  \u00b7  in flight {v[6]}  \u00b7  file {v[8]}"),
            foreground=PALETTE["fg"])

    def _set_cap_detail(self, text: str) -> None:
        view = self.cap_detail.yview()
        self.cap_detail.configure(state="normal")
        self.cap_detail.delete("1.0", "end")
        self.cap_detail.insert("1.0", text)
        self.cap_detail.configure(state="disabled")
        if view:
            self.cap_detail.yview_moveto(view[0])

    # -- actions ------------------------------------------------------------ #

    def act_refresh(self):
        aid = self.selected_id()
        if not aid:
            return self.toast("Select an account first.", "warn")
        threading.Thread(target=self._act_bg, args=(self.api.refresh, aid,
                                                    "token refreshed"), daemon=True).start()

    def act_enable(self):
        aid = self.selected_id()
        if not aid:
            return
        threading.Thread(target=self._act_bg, args=(self.api.enable, aid,
                                                    "account enabled"), daemon=True).start()

    def act_balance(self):
        aid = self.selected_id()
        if not aid:
            return self.toast("Select an account first.", "warn")
        def show(res, _ctx):
            code, body = res
            if code == 200:
                micro = body.get("balance_micro") or 0
                self.toast(f"balance: {micro/1e6:.6f} USD", "ok")
            else:
                self.toast(f"balance check failed: HTTP {code}", "err")
        threading.Thread(target=self._act_bg, args=(self.api.balance, aid,
                                                    "balance checked", show), daemon=True).start()

    def act_remove(self):
        aid = self.selected_id()
        if not aid:
            return
        if not messagebox.askyesno("Remove", f"Remove {aid} from the running pool?\n"
                                             f"(the file in accounts/ is not deleted)"):
            return
        threading.Thread(target=self._act_bg, args=(self.api.remove, aid,
                                                    "account removed"), daemon=True).start()

    def _act_bg(self, fn, arg, label, on_done=None):
        """Run an admin action off the UI thread, then refresh the view."""
        try:
            res = fn(arg)
            if on_done:
                self.after(0, on_done, res, label)
            else:
                code, body = res
                self.after(0, self.toast,
                           f"{label}: HTTP {code}", "ok" if code == 200 else "err")
        except Exception as exc:
            self.after(0, self.toast, f"{label}: {exc}", "err")
        finally:
            self.after(100, self.poll, True)

    def act_reload(self):
        if not self.server.running:
            return self.toast("Gateway is not running.", "warn")

        def work():
            code, body = self.api.reload_pool()
            if code == 200:
                self.after(0, self.toast,
                           f"pool reloaded: {body.get('loaded')} loaded, "
                           f"{body.get('added')} added", "ok")
            else:
                self.after(0, self.toast,
                           f"reload failed: HTTP {code}", "err")
            self.after(100, self.poll, True)
        threading.Thread(target=work, daemon=True).start()

    def act_sync_clients(self):
        """Give reasoning-capable models a thinking picker in local clients.

        Kilo Code (and similar) render their thinking selector only when a
        model declares `reasoning: true` in the client config — the value is
        never read from /v1/models. This patches those entries for every model
        that points at this gateway, keeping a backup of the file.
        """
        def work():
            try:
                status = inspect_kilo()
                if not status.found:
                    self.after(0, self.toast,
                               "no Kilo config found (~/.config/kilo/kilo.jsonc)",
                               "warn")
                    return
                if status.error:
                    self.after(0, self.toast,
                               f"Kilo config unreadable: {status.error}", "err")
                    return
                if not status.gateway_providers:
                    self.after(0, self.toast,
                               "Kilo has no provider pointing at this gateway "
                               "— add one first", "warn")
                    return
                if not status.needs_sync:
                    self.after(0, self.toast,
                               f"Kilo already has reasoning enabled on all "
                               f"{status.models_flagged} gateway models", "ok")
                    return

                result = sync_kilo()
                if result["error"]:
                    self.after(0, self.toast,
                               f"sync failed: {result['error']}", "err")
                    return
                self.after(0, self.toast,
                           f"Kilo updated: {result['changed']} model(s) can now "
                           f"pick a reasoning level (reload the VS Code window)",
                           "ok")
            except Exception as exc:
                self.after(0, self.toast, f"sync failed: {exc}", "err")

        threading.Thread(target=work, daemon=True).start()

    def _check_client_reasoning(self) -> None:
        """Startup hint: tell the user once when a client still lacks the flag."""
        def work():
            try:
                status = inspect_kilo()
                if status.found and status.needs_sync and not status.error:
                    self.after(0, self.toast,
                               f"Kilo: {len(status.models_missing_flag)} gateway "
                               f"model(s) lack reasoning controls — click "
                               f"\"Reasoning controls\" to add them", "info")
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def act_import(self):
        """One-click: extract the logged-in account from Zcode into the pool.

        Works whether the gateway is running or stopped; running gateways pick
        the account up immediately via /admin/pool/reload (no restart needed —
        snapshot writes are atomic and reload upserts under the pool lock).
        """
        if self._importing:
            return
        self._importing = True

        def work():
            try:
                self.after(0, lambda: self.toast("importing from Zcode\u2026"))
                status = detect_zcode()

                if not status.installed:
                    self.after(0, self._import_not_installed, status)
                    return
                if not status.logged_in:
                    self.after(0, self._import_not_logged_in, status)
                    return

                try:
                    result = import_from_zcode_config(output_dir=self.accounts_dir)
                except Exception as exc:
                    self.after(0, self.toast, f"import failed: {exc}", "err")
                    return

                source = (self._cfg_get("accounts.source") or "accounts_dir")
                if self.server.running and source == "accounts_dir":
                    code, body = self.api.reload_pool()
                    if code == 200:
                        added = body.get("added", len(result["new_ids"]))
                        who = ", ".join(result["emails"][:3]) or "account"
                        self.after(0, self.toast,
                                   f"Imported {len(result['written'])} account(s) "
                                   f"({added} new): {who} \u2014 load balancing now",
                                   "ok")
                    else:
                        self.after(0, self.toast,
                                   f"saved snapshots, but pool reload failed "
                                   f"(HTTP {code}) \u2014 they load on next start",
                                   "warn")
                elif self.server.running:
                    self.after(0, self._import_source_mismatch, result)
                    return
                else:
                    self.after(0, self.toast,
                               f"Saved {result['written']} account(s) to "
                               f"{self.accounts_dir} \u2014 they load when the "
                               f"gateway starts", "ok")
                self.after(0, self._refresh_zcode_status)
                self.after(100, self.poll, True)
            finally:
                self._importing = False

        threading.Thread(target=work, daemon=True).start()

    def _import_not_installed(self, status) -> None:
        self.toast(status.detail, "warn")
        if messagebox.askyesno(
                "Zcode not found",
                "The official Zcode app was not found on this machine.\n\n"
                "Open the download page?"):
            webbrowser.open(ZCODE_DOWNLOAD_URL)

    def _import_not_logged_in(self, status) -> None:
        self.toast(status.detail, "warn")
        if status.install_path and messagebox.askyesno(
                "Not logged in",
                "Zcode is installed but no logged-in account was found.\n\n"
                "Open the Zcode app so you can sign in, then import again?"):
            try:
                os.startfile(status.install_path)   # noqa: S602 - user-initiated
            except OSError as exc:
                self.toast(f"could not launch Zcode: {exc}", "err")

    def _import_source_mismatch(self, result) -> None:
        if messagebox.askyesno(
                "Accounts source mismatch",
                "The gateway is configured to load accounts from "
                f"'{self._cfg_get('accounts.source')}' instead of the "
                "accounts folder the import wrote to.\n\n"
                "Switch the config to accounts_dir and restart the gateway?"):
            try:
                import yaml
                data = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8")) or {}
                (data.setdefault("accounts", {}))["source"] = "accounts_dir"
                self.cfg_path.write_text(yaml.safe_dump(data, sort_keys=False),
                                         encoding="utf-8")
                self.restart_server(f"Imported {result['written']} account(s); gateway restarted with accounts_dir")
            except Exception as exc:
                self.toast(f"could not update config: {exc}", "err")

    # -- models tab -------------------------------------------------------- #

    def _tab_models(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Models")

        top = ttk.Frame(f, style="Card.TFrame", padding=(8, 6))
        top.pack(fill="x", pady=(0, 6))
        self.models_summary = ttk.Label(top, text="", style="Card.TLabel",
                                        font=("Segoe UI", 9, "bold"))
        self.models_summary.pack(side="left")
        ttk.Label(top, text="search:", style="CardMuted.TLabel").pack(
            side="right", padx=(8, 0))
        self.model_filter = tk.StringVar()
        self.model_filter.trace_add("write", lambda *_: self._apply_filters())
        ttk.Entry(top, textvariable=self.model_filter, width=24).pack(side="right")
        self.only_blocked = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="only blocked",
                        variable=self.only_blocked,
                        command=lambda: self._apply_filters()).pack(
                            side="right", padx=10)

        cols = ("model", "type", "on", "status", "next_free", "detail")
        widths = (330, 60, 70, 110, 340, 470)
        headings = {
            "model": "Model", "type": "Lane", "on": "Accounts",
            "status": "Status", "next_free": "Next reset · local", "detail": "Why / release",
        }
        body = ttk.Frame(f)
        body.pack(fill="both", expand=True)
        self.mtree = ttk.Treeview(body, columns=cols, show="tree headings",
                                  height=20)
        self.mtree.heading("#0", text="")
        self.mtree.column("#0", width=28, stretch=False)
        for c, w in zip(cols, widths):
            self.mtree.heading(c, text=headings[c])
            self.mtree.column(c, width=w, anchor="w")
        self.mtree.grid(row=0, column=0, sticky="nsew")
        msb = ttk.Scrollbar(body, orient="vertical", command=self.mtree.yview)
        msb.grid(row=0, column=1, sticky="ns")
        self.mtree.configure(yscrollcommand=msb.set)
        mhsb = ttk.Scrollbar(body, orient="horizontal", command=self.mtree.xview)
        mhsb.grid(row=1, column=0, sticky="ew")
        self.mtree.configure(xscrollcommand=mhsb.set)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.mtree.tag_configure("odd", background=PALETTE["stripe"])
        for tag, color in STATE_COLORS.items():
            self.mtree.tag_configure(tag, foreground=color)

        ttk.Label(
            f, style="Faint.TLabel", justify="left",
            text=("Free models are capped per model per account — an account out "
                  "of one free model still serves the others. Paid (usage-billed) "
                  "models need balance > 0 on that account. Expand a model to see "
                  "each account's status and when a cap lifts.")
        ).pack(anchor="w", pady=(6, 0))

    # -- server tab -------------------------------------------------------- #

    def _tab_server(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=14)
        nb.add(f, text="Server")

        ttk.Label(f, text="LOCAL ENDPOINTS", style="H2.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        rows = [
            ("OpenAI compatible", "/v1/chat/completions"),
            ("Anthropic compatible", "/v1/messages"),
            ("Models", "/v1/models"),
            ("Admin", "/admin/pool/state"),
            ("Health", "/health"),
        ]
        self.server_urls: dict[str, tk.StringVar] = {}
        for i, (label, path) in enumerate(rows, start=1):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar(value="")
            self.server_urls[path] = var
            e = ttk.Entry(f, textvariable=var, width=52, state="readonly")
            e.grid(row=i, column=1, sticky="w", padx=8, pady=2)
            ttk.Button(f, text="copy", style="Ghost.TButton", width=5,
                       command=lambda v=var: self._copy_var(v)).grid(
                           row=i, column=2, pady=2)

        ttk.Label(f, text="Client key").grid(row=7, column=0, sticky="w",
                                             pady=(14, 2))
        self.client_key_var = tk.StringVar(value=self._read_client_key())
        ttk.Entry(f, textvariable=self.client_key_var, width=52,
                  state="readonly").grid(row=7, column=1, sticky="w", padx=8)
        ttk.Button(f, text="copy", style="Ghost.TButton", width=5,
                   command=lambda: self._copy_var(self.client_key_var)).grid(
                       row=7, column=2)

        ttk.Label(f, text="Admin key").grid(row=8, column=0, sticky="w", pady=2)
        self.admin_key_var = tk.StringVar(value=self.admin_key)
        ttk.Entry(f, textvariable=self.admin_key_var, width=52,
                  state="readonly").grid(row=8, column=1, sticky="w", padx=8)
        ttk.Button(f, text="copy", style="Ghost.TButton", width=5,
                   command=lambda: self._copy_var(self.admin_key_var)).grid(
                       row=8, column=2)

        ttk.Label(f, text="Config file").grid(row=9, column=0, sticky="w",
                                              pady=(14, 2))
        ttk.Label(f, text=str(self.cfg_path),
                  foreground=PALETTE["muted"]).grid(row=9, column=1,
                                                    sticky="w", padx=8)
        ttk.Label(f, text="Accounts folder").grid(row=10, column=0, sticky="w",
                                                  pady=2)
        # show the dir the running server actually used (may differ from the
        # file value after the start-time fallback), falling back to the file
        shown_dir = (self.server.effective_accounts_dir
                     if self.server.effective_accounts_dir else self.accounts_dir)
        ttk.Label(f, text=str(shown_dir),
                  foreground=PALETTE["muted"]).grid(row=10, column=1,
                                                    sticky="w", padx=8)

        ttk.Label(f, text="TRY IT", style="H2.TLabel").grid(
            row=11, column=0, sticky="w", pady=(18, 4))
        self.copy_hint = ttk.Label(
            f, foreground=PALETTE["muted"], justify="left",
            text=("curl " + "{base}" + "/v1/chat/completions \\\n"
                  '  -H "Authorization: Bearer ' + self._read_client_key() + '" \\\n'
                  '  -H "Content-Type: application/json" \\\n'
                  '  -d \'{"model":"GLM-5.3-Flash",'
                  '"messages":[{"role":"user","content":"hi"}]}\''))
        self.copy_hint.grid(row=12, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Button(f, text="copy curl", style="Ghost.TButton",
                   command=self._copy_curl).grid(row=13, column=0, sticky="w")

        ttk.Label(f, text=BRAND_TITLE,
                  foreground=PALETTE["muted"]).grid(row=14, column=0,
                                                    columnspan=2, sticky="w",
                                                    pady=(24, 0))
        link = ttk.Label(f, text=TELEGRAM_URL, style="Link.TLabel")
        link.grid(row=15, column=0, sticky="w")
        link.bind("<Button-1>", lambda e: webbrowser.open(TELEGRAM_URL))

    def _copy_var(self, var: tk.StringVar) -> None:
        self.clipboard_clear()
        self.clipboard_append(var.get())
        self.toast("copied", "ok")

    def _copy_curl(self) -> None:
        text = self.copy_hint.cget("text").replace("{base}", self.server.base_url)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.toast("curl command copied", "ok")

    def act_regen_keys(self):
        if not messagebox.askyesno(
                "Regenerate keys",
                "Replace admin/client keys with fresh random secrets?\n"
                "Clients configured with the old key stop working."):
            return
        try:
            import yaml
            data = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8")) or {}
            server = data.setdefault("server", {})
            server["admin_key"] = f"gw-admin-{secrets.token_hex(8)}"
            keys = server.get("client_keys")
            if isinstance(keys, list) and keys and isinstance(keys[0], dict):
                keys[0]["key"] = f"gw-{secrets.token_hex(8)}"
            self.cfg_path.write_text(yaml.safe_dump(data, sort_keys=False),
                                     encoding="utf-8")
            self.accounts_dir = self._configured_accounts_dir()
        except Exception as exc:
            return self.toast(f"could not update config: {exc}", "err")
        self.admin_key = self._read_admin_key()
        self.api.key = self.admin_key
        self.admin_key_var.set(self.admin_key)
        self.client_key_var.set(self._read_client_key())
        self.restart_server("new keys generated; gateway restarted")

    # -- settings tab ------------------------------------------------------ #

    def _tab_settings(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=14)
        nb.add(f, text="Settings")

        ttk.Label(f, text="SERVER", style="H2.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.cfg_vars: dict[str, tk.StringVar] = {}
        self.free_fallback_enabled = tk.BooleanVar(
            value=bool(self._cfg_get("models.auto_free_fallback.enabled")))

        def row(i, label, key, values=None, width=26):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(self._cfg_get(key)))
            self.cfg_vars[key] = var
            if values:
                w = ttk.Combobox(f, textvariable=var, values=values,
                                 width=width - 2, state="readonly")
            else:
                w = ttk.Entry(f, textvariable=var, width=width)
            w.grid(row=i, column=1, sticky="w", padx=10)

        row(1, "host", "server.host")
        row(2, "port", "server.port")

        ttk.Separator(f, orient="horizontal").grid(row=3, column=0, columnspan=3,
                                                   sticky="ew", pady=12)
        ttk.Label(f, text="LOAD BALANCING", style="H2.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 8))
        row(5, "strategy", "pool.strategy",
            ["least_in_flight", "drain_first", "round_robin", "least_recently_used", "quota_aware"])
        row(6, "cooldown_seconds", "pool.cooldown_seconds")
        row(7, "max_in_flight_per_account", "pool.max_in_flight_per_account")
        row(8, "acquire_wait_seconds", "pool.acquire_wait_seconds")
        row(9, "min_balance_micro", "pool.min_balance_micro")
        row(10, "balance_poll_seconds", "pool.balance_poll_seconds")

        ttk.Separator(f, orient="horizontal").grid(row=11, column=0, columnspan=3,
                                                   sticky="ew", pady=12)
        ttk.Label(f, text="ACCOUNTS SOURCE", style="H2.TLabel").grid(
            row=12, column=0, columnspan=2, sticky="w", pady=(0, 8))
        row(13, "source", "accounts.source",
            ["accounts_dir", "providers_json", "pool_file"])
        row(14, "accounts dir", "accounts.dir")

        ttk.Separator(f, orient="horizontal").grid(row=15, column=0, columnspan=3,
                                                   sticky="ew", pady=12)
        ttk.Label(f, text="UPSTREAM", style="H2.TLabel").grid(
            row=16, column=0, columnspan=2, sticky="w", pady=(0, 8))
        row(17, "max_attempts", "upstream.max_attempts")
        row(18, "timeout_read", "upstream.timeout_read")
        row(19, "anthropic_cache_control", "upstream.anthropic_cache_control",
            ["true", "false"])
        row(20, "default model", "models.default")

        ttk.Separator(f, orient="horizontal").grid(row=21, column=0, columnspan=3,
                                                    sticky="ew", pady=12)
        ttk.Label(f, text="AUTO FREE-MODEL FALLBACK", style="H2.TLabel").grid(
            row=22, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Checkbutton(
            f, text="Enable automatic fallback", variable=self.free_fallback_enabled
        ).grid(row=23, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(
            f, text="Ordered chain (one model ID per line; first available is selected)",
            foreground=PALETTE["muted"],
        ).grid(row=24, column=0, columnspan=2, sticky="w", pady=(3, 2))
        self.free_fallback_chain = tk.Text(
            f, width=52, height=4, wrap="none", undo=True,
            bg=PALETTE["field"], fg=PALETTE["fg"],
            insertbackground=PALETTE["fg"], selectbackground=PALETTE["accent"],
        )
        chain = self._cfg_get("models.auto_free_fallback.chain")
        if isinstance(chain, list):
            self.free_fallback_chain.insert("1.0", "\n".join(str(model) for model in chain))
        elif chain:
            self.free_fallback_chain.insert("1.0", str(chain))
        else:
            self.free_fallback_chain.insert("1.0", "\n".join(DEFAULT_FREE_FALLBACK_CHAIN))
        self.free_fallback_chain.grid(row=25, column=0, columnspan=2,
                                      sticky="w", padx=10, pady=(0, 3))

        ttk.Button(f, text="Save & Restart", command=self.act_save_settings).grid(
            row=26, column=0, pady=18, sticky="w")
        ttk.Button(f, text="Regenerate keys", style="Ghost.TButton",
                   command=self.act_regen_keys).grid(row=26, column=1,
                                                      pady=18, sticky="w")
        ttk.Label(f, foreground=PALETTE["muted"],
                  text="Changes are written to config.yaml and the gateway restarts."
                  ).grid(row=27, column=0, columnspan=2, sticky="w")

    INT_FIELDS = {"server.port", "pool.cooldown_seconds",
                  "pool.max_in_flight_per_account", "pool.balance_poll_seconds",
                  "upstream.max_attempts", "upstream.timeout_read",
                  "pool.min_balance_micro"}
    FLOAT_FIELDS = {"pool.acquire_wait_seconds"}

    @staticmethod
    def _fallback_chain_from_text(value: str) -> list[str]:
        """Parse and validate the ordered model IDs entered in the settings tab."""
        chain = [line.strip() for line in value.splitlines() if line.strip()]
        if not chain:
            raise ValueError("models.auto_free_fallback.chain: enter at least one model ID")
        if any(any(char.isspace() for char in model) for model in chain):
            raise ValueError("models.auto_free_fallback.chain: model IDs cannot contain spaces")
        if len(set(chain)) != len(chain):
            raise ValueError("models.auto_free_fallback.chain: duplicate model IDs are not allowed")
        return chain

    def act_save_settings(self):
        # validate before touching the file
        for dotted, var in self.cfg_vars.items():
            raw = var.get().strip()
            if not raw:
                continue
            if dotted in self.INT_FIELDS:
                try:
                    int(raw)
                except ValueError:
                    return self.toast(f"{dotted}: '{raw}' is not a whole number",
                                      "err")
            if dotted in self.FLOAT_FIELDS:
                try:
                    float(raw)
                except ValueError:
                    return self.toast(f"{dotted}: '{raw}' is not a number", "err")

        try:
            fallback_chain = self._fallback_chain_from_text(
                self.free_fallback_chain.get("1.0", "end-1c"))
        except ValueError as exc:
            return self.toast(str(exc), "err")

        try:
            import yaml
            data = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8")) or {}
            for dotted, var in self.cfg_vars.items():
                parts = dotted.split(".")
                cur = data
                for p in parts[:-1]:
                    cur = cur.setdefault(p, {})
                raw = var.get()
                old = cur.get(parts[-1])
                if isinstance(old, bool):
                    cur[parts[-1]] = raw.lower() in ("1", "true", "yes")
                elif isinstance(old, int) and dotted not in self.FLOAT_FIELDS:
                    cur[parts[-1]] = int(raw)
                elif isinstance(old, float) or dotted in self.FLOAT_FIELDS:
                    cur[parts[-1]] = float(raw)
                else:
                    cur[parts[-1]] = raw
            fallback = data.setdefault("models", {}).setdefault("auto_free_fallback", {})
            fallback["enabled"] = bool(self.free_fallback_enabled.get())
            fallback["chain"] = fallback_chain
            self.cfg_path.write_text(yaml.safe_dump(data, sort_keys=False),
                                     encoding="utf-8")
        except Exception as exc:
            return self.toast(str(exc), "err")

        self.admin_key = self._read_admin_key()
        self.api.key = self.admin_key
        self.admin_key_var.set(self.admin_key)
        self.restart_server("Saved. Gateway restarted.")

    # -- stats tab --------------------------------------------------------- #

    def _tab_stats(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=14)
        nb.add(f, text="Stats")
        self.stats_txt = tk.Text(f, wrap="none", font=("Cascadia Mono", 10),
                                 bg=PALETTE["field"], fg=PALETTE["fg"],
                                 insertbackground=PALETTE["fg"],
                                 selectbackground=PALETTE["accent_dim"],
                                 relief="flat", borderwidth=0,
                                 padx=12, pady=10)
        self.stats_txt.pack(fill="both", expand=True)

    # -- logs tab ---------------------------------------------------------- #

    def _tab_logs(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=14)
        nb.add(f, text="Logs")

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="level:", style="Muted.TLabel").pack(side="left")
        self.log_level_var = tk.StringVar(value="all")
        levels = ttk.Combobox(bar, textvariable=self.log_level_var, width=10,
                              state="readonly",
                              values=["all", "INFO", "WARNING", "ERROR"])
        levels.pack(side="left", padx=(6, 0))
        levels.bind("<<ComboboxSelected>>", lambda e: self._rerender_logs())
        ttk.Button(bar, text="Clear", style="Ghost.TButton",
                   command=self._clear_logs).pack(side="right")

        self.log_txt = tk.Text(f, wrap="none", font=("Cascadia Mono", 9),
                               bg=PALETTE["field"], fg=PALETTE["fg"],
                               insertbackground=PALETTE["fg"],
                               selectbackground=PALETTE["accent_dim"],
                               relief="flat", borderwidth=0,
                               padx=12, pady=10)
        self.log_txt.pack(fill="both", expand=True)

        self.queue_listener = threading.Thread(target=self._drain_queue, daemon=True)
        self.queue_listener.start()

    def _clear_logs(self) -> None:
        self._log_lines.clear()
        self.log_txt.delete("1.0", "end")

    def _rerender_logs(self) -> None:
        flt = self.log_level_var.get().upper()
        self.log_txt.delete("1.0", "end")
        for level, text in self._log_lines[-2000:]:
            if flt == "ALL" or level in (flt, "ERROR"):
                self.log_txt.insert("end", text + "\n")
        self.log_txt.see("end")

    def _drain_queue(self) -> None:
        while True:
            try:
                line = self.log_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.after(0, self._append_log, line)

    def _append_log(self, line: str) -> None:
        upper = line.upper()
        if "ERROR" in upper or "CRITICAL" in upper:
            level = "ERROR"
        elif "WARNING" in upper or "WARN " in upper:
            level = "WARNING"
        else:
            level = "INFO"
        self._log_lines.append((level, line))
        if len(self._log_lines) > 4000:
            del self._log_lines[:2000]
        flt = self.log_level_var.get().upper()
        if flt == "ALL" or level in (flt, "ERROR"):
            self.log_txt.insert("end", line + "\n")
            self.log_txt.see("end")
            if int(self.log_txt.index("end-1c").split(".")[0]) > 2000:
                self.log_txt.delete("1.0", "500.0")

    def log(self, msg: str) -> None:
        self.log_queue.put(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")

    # -- clime status chip -------------------------------------------------- #

    def _zcode_timer(self) -> None:
        # only refresh while the accounts tab is visible (tasklist costs a
        # subprocess; no need to run it in the background all day)
        try:
            if self.nb.index(self.nb.select()) == 0:
                self._refresh_zcode_status()
        except tk.TclError:
            pass
        self.after(self.ZCODE_POLL_MS, self._zcode_timer)

    def _refresh_zcode_status(self) -> None:
        def work():
            try:
                status = detect_zcode()
                self.after(0, self._apply_zcode_status, status)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _apply_zcode_status(self, status) -> None:
        color = {
            "running": PALETTE["ok"],
            "logged_in": PALETTE["warn"],
            "installed": PALETTE["warn"],
            "off": PALETTE["muted"],
        }.get(status.chip_kind, PALETTE["muted"])
        self.zcode_dot.configure(fg=color)
        self.zcode_lbl.configure(text=status.detail)

    # -- lifecycle --------------------------------------------------------- #

    def start_everything(self) -> None:
        if self.server.running or self._stopping:
            return
        self._lifecycle += 1
        generation = self._lifecycle
        self.log(f"starting gateway (config={self.cfg_path})")
        self.server.start(self.cfg_path)
        threading.Thread(target=self._await_ready, args=(generation,), daemon=True).start()

    def _await_ready(self, generation: int) -> None:
        ok = self.server.wait_ready()
        self.after(0, self._on_ready, ok, generation)

    def _on_ready(self, ok: bool, generation: int | None = None) -> None:
        if generation is not None and generation != self._lifecycle:
            return
        if self._stopping:
            return
        if ok:
            self.log(f"gateway up at {self.server.base_url} "
                     f"({self.server.accounts_loaded} account(s) from config)")
            self.status_dot.configure(fg=PALETTE["ok"])
            self.status_lbl.configure(text=f"running  {self.server.base_url}")
            for path, var in self.server_urls.items():
                var.set(self.server.base_url + path)
            self.poll(force=True)
            if self._restart_message:
                self.toast(self._restart_message, "ok")
                self._restart_message = None
            self._restart_pending = False
        else:
            self._restart_pending = False
            self._restart_message = None
            self.status_dot.configure(fg=PALETTE["err"])
            err = self.server.error or "no error captured"
            self.status_lbl.configure(text="failed to start")
            self.log(f"start failed: {err}")
            if self._looks_like_port_conflict(err):
                self._select_tab("Settings")
                messagebox.showerror(
                    "Port in use",
                    f"Another process is already listening on "
                    f"{self.server.host}:{self.server.port} (perhaps a second "
                    f"Zcode Gateway?).\n\nChange 'port' on the Settings tab, "
                    f"then Save & Restart.")
            else:
                messagebox.showerror("Gateway", f"Could not start:\n{err}")

    @staticmethod
    def _looks_like_port_conflict(err: str) -> bool:
        low = err.lower()
        return ("10048" in low or "address already in use" in low
                or "only one usage" in low
                or ("bind" in low and "error" in low))

    def stop_everything(self) -> None:
        self._lifecycle += 1
        self._stopping = True
        for var in getattr(self, "server_urls", {}).values():
            var.set("")
        self._last_state = None
        self._last_avail = None
        self.server.stop()
        self._stopping = False
        self.status_dot.configure(fg=PALETTE["muted"])
        self.status_lbl.configure(text="stopped")
        self.log("gateway stopped")
        self.toast("gateway stopped")

    def restart_server(self, success_message: str | None = None) -> None:
        if self._restart_pending:
            return
        self._restart_pending = True
        self._restart_message = success_message
        self.stop_everything()
        self._stopping = True
        def wait_and_start() -> None:
            if self.server.wait_stopped():
                self.after(0, self._start_after_restart)
            else:
                self.after(0, self._restart_failed)
        threading.Thread(target=wait_and_start, daemon=True).start()

    def _start_after_restart(self) -> None:
        if not self._restart_pending:
            return
        self._stopping = False
        self.start_everything()

    def _restart_failed(self) -> None:
        self._restart_pending = False
        self._restart_message = None
        self.toast("gateway did not stop cleanly; restart aborted", "err")

    # -- polling ----------------------------------------------------------- #

    def poll(self, force: bool = False) -> None:
        # one worker at a time: a slow/unreachable admin API used to stack a
        # new thread every 2.5s, with older results rendering over newer ones
        if self.server.running and not self._stopping and not self._polling:
            self._polling = True
            threading.Thread(target=self._poll_worker, daemon=True).start()
        if not force:
            self.after(self.POLL_MS, self.poll)

    def _poll_worker(self) -> None:
        try:
            code, state = self.api.pool_state()
            if code != 200:
                return
            _, stats = self.api.stats()
            _, avail = self.api.availability()
            self.after(0, self._render, state, stats, avail)
        finally:
            self._polling = False

    def _render(self, state: dict, stats: dict, avail: dict | None = None) -> None:
        self._last_state = state
        self._last_avail = avail
        self._render_accounts(state)
        self._render_stats(stats)

        if avail and avail.get("models"):
            self._render_models(avail)

    def _render_accounts(self, state: dict | None) -> None:
        """(Re)build the accounts tree, applying the search filter.

        The full rebuild (every poll, 2.5 s) would drop the user's selection
        — snapshot the selected account ids first and re-select afterwards so
        a clicked row (and its detail strip) stays put.
        """
        if state is None:
            return
        now_ms = time.time() * 1000
        needle = (self.acct_filter.get() or "").lower() if hasattr(
            self, "acct_filter") else ""
        selected = {self.tree.item(i, "values")[1]
                    for i in self.tree.selection()
                    if len(self.tree.item(i, "values")) > 1}
        self.tree.delete(*self.tree.get_children())
        restored = False
        for idx, a in enumerate(state.get("detail", [])):
            row = (a.get("email", ""), a.get("id", ""))
            if needle and not any(needle in str(v).lower() for v in row):
                continue
            exp = a.get("expires_at") or 0
            if exp:
                left = exp - now_ms
                when = (f"{left / 60000:.0f} min" if left > 0
                        else f"expired {abs(left) / 60000:.0f} min ago")
            else:
                when = "?"

            notes = a.get("notes") or {}
            bal = format_balance(a)

            caps = format_caps(a, now=now_ms / 1000)
            acct_state = a.get("state", "ready")
            # colour by state; a ready account with a spent paid lane gets its own
            # colour rather than being mislabelled "exhausted"
            # NB: do not name this `state` - it would shadow the `state` parameter
            # holding the whole pool snapshot, breaking the summary line below.
            tag = ("paid_exhausted"
                   if (acct_state == "ready" and a.get("paid_exhausted"))
                   else acct_state)
            if idx % 2 == 1:
                tag = (tag, "odd")

            self.tree.insert("", "end", tags=tag if isinstance(tag, tuple) else (tag,), values=(
                a.get("email", ""),
                a.get("id", ""),
                a.get("state", ""),
                # NB: this is the PAID LANE, not "is a paid account".
                # exhausted => cannot spend Zcode Credits (402 / balance <= 0);
                # free models still work on this account either way.
                "exhausted" if a.get("paid_exhausted") else "available",
                when,
                bal or "",
                a.get("in_flight", 0),
                caps,
                notes.get("file", ""),
            ))
            if a.get("id", "") in selected:
                self.tree.selection_add(self.tree.get_children()[-1])
                restored = True

        self.ready_lbl.configure(
            text=f"accounts {state.get('accounts', 0)}  \u00b7  "
                 f"ready {state.get('ready', 0)}  \u00b7  "
                 f"paid-ready {state.get('ready_paid', 0)}  \u00b7  "
                 f"strategy {state.get('strategy', '')}")

        # a re-selected row keeps the detail strip in sync
        if restored:
            self._show_detail()
        else:
            self.detail_lbl.configure(text="Select an account for details")
            self._set_cap_detail("")

        # onboarding panel: only when the pool is empty
        if state.get("accounts", 0) == 0:
            self.onboard.pack(fill="x", pady=(0, 8), before=self.acct_body)
        else:
            self.onboard.pack_forget()

    def _render_stats(self, stats: dict) -> None:
        if stats:
            lines = [
                f"requests        : {stats.get('requests', 0)}",
                f"ok              : {stats.get('ok', 0)}",
                f"insufficient_credits: {stats.get('insufficient_credits', 0)}",
                f"rate_limited    : {stats.get('rate_limited', 0)}",
                f"avg duration ms : {stats.get('avg_duration_ms', 0)}",
                f"prompt tokens   : {stats.get('prompt_tokens', 0)}",
                f"completion tokens: {stats.get('completion_tokens', 0)}",
                f"total tokens    : {stats.get('total_tokens', 0)}",
                "",
                "by model:",
            ]
            for m in stats.get("by_model", [])[:20]:
                lines.append(f"  {m['requests']:>5}  {m['tokens']:>10}  {m['model']}")
            lines.append("")
            lines.append("by account:")
            for acc in stats.get("by_account", [])[:20]:
                lines.append(f"  {acc['requests']:>5}  {acc['tokens']:>10}  {acc['account_id']}")
            self.stats_txt.delete("1.0", "end")
            self.stats_txt.insert("1.0", "\n".join(lines))

    def _apply_filters(self) -> None:
        # re-render from the last poll data with the current filter text
        self._render_accounts(self._last_state)
        if self._last_avail and self._last_avail.get("models"):
            self._render_models(self._last_avail)

    def _render_models(self, avail: dict) -> None:
        s = avail.get("summary", {})
        self.models_summary.configure(
            text=(f"{s.get('free_models', 0)} free  \u00b7  {s.get('paid_models', 0)} paid  \u00b7  "
                  f"fully available {s.get('fully_available', 0)}  \u00b7  "
                  f"partial {s.get('partially_available', 0)}  \u00b7  "
                  f"none {s.get('unavailable', 0)}"))

        opened = {self.mtree.item(i, "values")[0] for i in self.mtree.get_children()
                  if self.mtree.item(i, "open")}
        self.mtree.delete(*self.mtree.get_children())
        only_blocked = self.only_blocked.get()
        needle = (self.model_filter.get() or "").lower()

        for m in avail["models"]:
            if needle and needle not in m["model"].lower():
                continue
            blocked = [a for a in m["accounts"] if a["status"] != "available"]
            if only_blocked and not blocked:
                continue

            if m["overall"] == "available":
                tag = "available"
            elif m["overall"] == "partial":
                tag = "partial"
            else:
                tag = "blocked"

            nxt = m.get("next_release_in_s")
            nxt_txt = ""
            if nxt is not None:
                nxt_txt = format_cap_release({"release_in_s": nxt},
                                             now=avail.get("generated_at"))

            detail = ""
            if blocked:
                reasons = {}
                for a in blocked:
                    reasons[a["status"]] = reasons.get(a["status"], 0) + 1
                detail = ", ".join(f"{v} {k}" for k, v in sorted(reasons.items()))

            node = self.mtree.insert("", "end", text="", tags=(tag,),
                                     open=m["model"] in opened, values=(
                m["model"],
                m.get("lane") or ("free" if m["is_free"] else "paid"),
                f"{m['available_on']}/{m['total_accounts']}",
                m["overall"],
                nxt_txt,
                detail,
            ))

            for a in m["accounts"]:
                if a["status"] == "available":
                    continue
                rel_txt = (format_cap_release(a) if a.get("release_at") or
                           a.get("release_in_s") is not None else "")
                why = a.get("reason", "")[:70]
                if rel_txt:
                    why = f"{why}  ({rel_txt})"
                self.mtree.insert(node, "end", text="", tags=("account",), values=(
                    f"   {a.get('email') or a['account_id']}",
                    "", "", a["status"], "", why))


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
