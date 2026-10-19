"""ZCODEAUTO account generator integration.

Runs the ZCODEAUTO signup pipeline as worker subprocesses and feeds successful
accounts straight into the gateway pool:

    signup -> Starter check -> device login -> invite bind -> client message
    -> (Global Build issues server-side)

A worker is `python main.py 1` inside the ZCODEAUTO `gen/` folder with routing
env vars set from the dashboard options. On success (accounts<SUF>.txt grows)
the new account is minted a zcodeJWT, written as an accounts_dir snapshot and
upserted into the pool — the same path a manual import takes, so restarts keep
it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path


def _gen_dir_under(path: Path) -> Path | None:
    """If `path` is (or contains) a generator folder with main.py, return it.

    Supported layouts:
      * ZCODEAUTO checkout:  <root>/gen/main.py
      * this repo (merged):  <root>/generator/main.py
      * the path itself is the folder holding main.py
    """
    try:
        if not path.is_dir():
            return None
        for cand in (path, path / "gen", path / "generator"):
            if (cand / "main.py").is_file() and (cand / "config.py").is_file():
                return cand
    except OSError:
        return None
    return None


def zcodeauto_candidates(configured: str = "") -> list[Path]:
    """Every place we'd look for the generator folder, best-guess first.

    Lets a copied .exe find its generator without anyone editing config.yaml.
    """
    cands: list[Path] = []

    def add(p):
        if not p:
            return
        try:
            ap = Path(str(p)).expanduser()
        except (TypeError, ValueError):
            return
        if ap not in cands:
            cands.append(ap)

    # explicit config wins
    add(configured)
    # explicit env overrides
    for var in ("ZCODEAUTO_DIR", "ZCODEAUTO_PATH", "ZAI_ZCODEAUTO_DIR"):
        add(os.environ.get(var))

    roots = []
    # beside the exe / source tree
    try:
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            add(exe_dir / "generator")
            add(exe_dir / "gen")
            # If bundled inside PyInstaller single-exe, use internal copy
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                add(Path(meipass) / "generator")
                add(Path(meipass) / "gen")
        else:
            exe_dir = Path(__file__).resolve().parent.parent
            add(exe_dir / "generator")
            add(exe_dir / "gen")
        roots.append(exe_dir)
        roots.extend(list(exe_dir.parents)[:3])
    except OSError:
        pass
    roots.append(Path.cwd())
    add(Path.home() / "ZCODEAUTO")
    add(Path.home() / "AutoZCoder")
    for r in roots:
        add(r)
        add(r / "gen")
        add(r / "generator")
        add(r / "ZCODEAUTO")
        add(r / "AutoZCoder")
        add(r / "AutoZCoder" / "generator")
        add(r / "zcode-mitm" / "ZCODEAUTO")
    # common clone locations
    for base in (Path.home() / "Documents", Path.home() / "source", Path.home() / "repos"):
        add(base / "ZCODEAUTO")
        add(base / "AutoZCoder")
        add(base / "GitHub" / "ZCODEAUTO")
        add(base / "GitHub" / "AutoZCoder")
    for drive in ("D:", "C:", "E:"):
        add(Path(drive + "\\JB\\zcode-mitm\\ZCODEAUTO"))
        add(Path(drive + "\\zcode-mitm\\ZCODEAUTO"))
    return cands


def resolve_zcodeauto_dir(configured: str = "") -> tuple[Path | None, list[Path]]:
    """Return (generator_folder, checked_candidates); folder is None if missing.

    The returned folder is the one that directly holds main.py — i.e. `gen/` in
    a ZCODEAUTO checkout, or `generator/` in this repo.
    """
    checked = zcodeauto_candidates(configured)
    for c in checked:
        gen = _gen_dir_under(c)
        if gen is not None:
            return gen, checked
    return None, checked


log = logging.getLogger("zcode_gateway.generator")

# ANSI escapes: full sequences (\x1b[..m, erase-line, OSC hyperlinks) plus the
# parameter tail that survives when something upstream already ate the ESC —
# wrangler colourises with ESC[31m etc. and we never want them in the UI.
_ANSI_ESC = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_TAIL = re.compile(r"\[[0-9;]*m")


def _clean_output(s: str) -> str:
    """Strip ANSI colour/erase codes and normalise \r progress noise."""
    if not s:
        return ""
    s = _ANSI_ESC.sub("", s)
    s = _ANSI_TAIL.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(ln.rstrip() for ln in s.split("\n"))


def _tail(s: str, limit: int = 700) -> str:
    """Cleaned output, trimmed to the last `limit` chars on a whole-line edge."""
    s = _clean_output(s).strip()
    if len(s) <= limit:
        return s
    return "…" + s[-limit:].split("\n", 1)[-1]


# per-worker live state ------------------------------------------------------- #

WORKER_LOG_LINES = 400   # ring buffer per worker


def _child_kwargs() -> dict:
    """Popen kwargs that keep spawned python/taskkill children windowless.

    The gateway ships as a --windowed (no console) exe; a bare subprocess spawn
    would pop a console window for every worker / proxy check.
    """
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": si}


def resolve_python(explicit: str = "") -> str:
    """A REAL interpreter to run ZCODEAUTO scripts with.

    In a frozen (PyInstaller) gateway, sys.executable is the gateway exe itself —
    spawning `sys.executable main.py` would run the exe with bogus args. Prefer
    an explicit config path, else the running interpreter when unfrozen, else a
    python on PATH.
    """
    if explicit:
        p = Path(explicit)
        if p.exists():
            return str(p)
    if not getattr(sys, "frozen", False):
        return sys.executable
    import shutil
    for cand in ("python", "python3"):
        found = shutil.which(cand)
        if found:
            return found
    # common Windows locations
    import glob
    for pat in (
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python*\python.exe"),
        r"C:\Program Files\Python*\python.exe",
        r"C:\Python*\python.exe",
    ):
        matches = glob.glob(pat)
        if matches:
            return matches[-1]
    # last resort: py launcher
    found = shutil.which("py")
    return found or sys.executable


# modules the generator scripts import at runtime (generator/requirements.txt)
GEN_DEPS = ["playwright", "ddddocr", "cv2", "PIL", "numpy", "requests",
            "urllib3", "certifi", "socks"]
# pip names for the ones whose import name differs or that are missing
GEN_PIP_NAMES = {"cv2": "opencv-python", "PIL": "Pillow", "socks": "PySocks"}


class EnvSetup:
    """First-run environment installer with live progress + retries.

    Checks the full generator chain — real Python (not the Microsoft Store
    stub), all pip dependencies, and the Playwright Chromium browser — and
    installs whatever is missing, streaming progress lines the dashboard
    setup modal polls. Runs in a background thread; one installation at a
    time; Retry just restarts the missing-items list."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.lines: deque[str] = deque(maxlen=400)
        self.state = "idle"            # idle|checking|installing|done|failed
        self.missing: list[dict] = []
        self.error: str = ""
        self.python_exe: str = ""
        self.started_at: float = 0.0
        self.finished_at: float = 0.0

    # ---------------------------------------------------------------- emit
    def _emit(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.lines.append(f"[{stamp}] {line}")
        log.info("env-setup: %s", line)

    # ------------------------------------------------------------ python
    def _python_real(self, candidate: str) -> bool:
        """A real runnable CPython >= 3.9 — not the WindowsApps Store stub."""
        if not candidate:
            return False
        try:
            p = Path(candidate)
            if not p.is_file():
                return False
            low = str(p).lower().replace("/", "\\")
            if "\\microsoft\\windowsapps\\" in low \
                    and "pythonsoftwarefoundation" not in low:
                return False          # Store alias: opens the Store, exit 9009
            r = subprocess.run(
                [str(p), "-c",
                 "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"],
                capture_output=True, timeout=20, **_child_kwargs())
            return r.returncode == 0
        except Exception:
            return False

    def _find_python(self) -> str:
        import shutil
        import glob
        # explicit/configured path first
        if self.python_exe and self._python_real(self.python_exe):
            return self.python_exe
        # PATH (verified — the Store alias must never win)
        for cand in ("python", "python3"):
            found = shutil.which(cand)
            if found and self._python_real(found):
                return found
        # standard install locations
        for pat in (
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python3*\python.exe"),
            r"C:\Program Files\Python*\python.exe",
            r"C:\Python*\python.exe",
            # real Store package install (runnable, unlike the alias)
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps"
                               r"\PythonSoftwareFoundation.Python.3*\python.exe"),
        ):
            for match in sorted(glob.glob(pat)):
                if self._python_real(match):
                    return match
        # py launcher with an installed 3.x
        launcher = shutil.which("py")
        if launcher:
            try:
                r = subprocess.run(
                    [launcher, "-3", "-c",
                     "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"],
                    capture_output=True, timeout=20, **_child_kwargs())
                if r.returncode == 0:
                    return launcher            # callers use `py -3` form
            except Exception:
                pass
        return ""

    def _install_python(self) -> bool:
        """No real Python on the machine: install one for the user.

        Order: winget (silent, no UAC for user scope) → official installer
        download (per-user, /passive). PATH is stale afterwards, so the
        caller must re-scan, not trust os.environ."""
        self._emit("no usable Python found — installing Python 3.12 (one-time, ~70 MB)…")
        # 1) winget
        try:
            r = subprocess.run(
                ["winget", "install", "--id", "Python.Python.3.12",
                 "--scope", "user", "--silent",
                 "--accept-package-agreements", "--accept-source-agreements",
                 "--disable-interactivity"],
                capture_output=True, text=True, timeout=900, **_child_kwargs())
            if r.returncode == 0:
                self._emit("Python installed via winget ✓")
                return True
            self._emit(f"  winget failed (exit {r.returncode}) — trying direct download")
        except FileNotFoundError:
            self._emit("  winget not available — trying direct download")
        except Exception as e:
            self._emit(f"  winget error: {type(e).__name__} — trying direct download")
        # 2) official per-user installer
        url = ("https://www.python.org/ftp/python/3.12.10/"
               "python-3.12.10-amd64.exe")
        dest = Path(tempfile.gettempdir()) / "AutoZCoder-python-install.exe"
        try:
            self._emit("  downloading Python installer…")
            import urllib.request
            with urllib.request.urlopen(url, timeout=120) as resp, \
                    open(dest, "wb") as out:
                total = int(resp.headers.get("Content-Length", 0) or 0)
                got = 0
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
                    if total and got % (1 << 22) < (1 << 16):
                        self._emit(f"  downloaded {got // (1 << 20)} / {total // (1 << 20)} MB")
            self._emit("  running installer (per-user, passive)…")
            r = subprocess.run(
                [str(dest), "/quiet", "InstallAllUsers=0", "PrependPath=1",
                 "Include_launcher=1", "Include_pip=1"],
                capture_output=True, timeout=900, **_child_kwargs())
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            if r.returncode == 0:
                self._emit("Python installed ✓")
                return True
            self._emit(f"  installer exited {r.returncode}")
        except Exception as e:
            self._emit(f"  download/install failed: {type(e).__name__}: {e}")
        return False

    def _rediscover_python(self) -> str:
        """PATH is stale right after an install: re-scan the disk."""
        import glob
        for pat in (
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python312\python.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python3*\python.exe"),
            r"C:\Program Files\Python*\python.exe",
        ):
            for match in sorted(glob.glob(pat)):
                if self._python_real(match):
                    return match
        # Store package installs land here too
        return self._find_python()

    # --------------------------------------------------------------- deps
    def _missing_deps(self, py: str) -> list[str]:
        """Which generator imports fail in the resolved Python."""
        script = "import importlib\nimport sys\nmods = " + repr(
            [m for m in GEN_DEPS]) + "\nbad = []\n" + \
            "for m in mods:\n    try:\n        importlib.import_module(m)\n" + \
            "    except Exception:\n        bad.append(m)\nprint(','.join(bad))"
        try:
            r = subprocess.run([py, "-c", script], capture_output=True,
                               text=True, timeout=120, **_child_kwargs())
            if r.returncode != 0:
                return list(GEN_DEPS)      # can't even run: install everything
            return [m for m in (r.stdout or "").strip().split(",") if m]
        except Exception:
            return list(GEN_DEPS)

    def _chromium_ok(self, py: str) -> bool:
        """Playwright's own cache check: `install` is idempotent and fast
        when the browser is present, but a dry existence probe is cheaper
        for the status panel."""
        try:
            script = ("from playwright.sync_api import sync_playwright\n"
                      "with sync_playwright() as p:\n"
                      "    import os\n"
                      "    exe = p.chromium.executable_path\n"
                      "    print('1' if exe and os.path.exists(exe) else '0')")
            r = subprocess.run([py, "-c", script], capture_output=True,
                               text=True, timeout=60, **_child_kwargs())
            return (r.stdout or "").strip().endswith("1")
        except Exception:
            return False

    def _pip_install(self, py: str, pkgs: list[str]) -> bool:
        self._emit(f"pip install {' '.join(pkgs)} …")
        return self._run([py, "-m", "pip", "install", "--upgrade"] + pkgs)

    def _run(self, argv: list[str], timeout: int = 1200) -> bool:
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                **_child_kwargs())
        except Exception as e:
            self._emit(f"  failed to start: {type(e).__name__}: {e}")
            return False
        last = 0.0
        for line in proc.stdout or []:
            line = line.rstrip()
            if not line:
                continue
            # stream installer output but throttle to keep the log readable
            now = time.time()
            if now - last > 0.5 or "%" not in line:
                self._emit("  " + line[:200])
                last = now
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            self._emit("  step timed out — killed")
            return False
        return proc.returncode == 0

    # ---------------------------------------------------------- the flow
    def check(self) -> dict:
        """Synchronous full check (fast, read-only). Returns the status the
        modal shows: each step ok/missing."""
        with self._lock:
            steps = []
            py = self._find_python()
            self.python_exe = py
            steps.append({"id": "python", "label": "Python 3.9+",
                          "ok": bool(py), "detail": py or "not found"})
            if py:
                missing = self._missing_deps(py)
                steps.append({"id": "deps", "label": "Python packages (9)",
                              "ok": not missing,
                              "detail": "all present" if not missing
                              else f"missing: {', '.join(missing)}"})
                chromium = self._chromium_ok(py)
                steps.append({"id": "chromium", "label": "Chromium browser",
                              "ok": chromium,
                              "detail": "installed" if chromium
                              else "not installed"})
            else:
                steps.append({"id": "deps", "label": "Python packages (9)",
                              "ok": False, "detail": "needs Python first"})
                steps.append({"id": "chromium", "label": "Chromium browser",
                              "ok": False, "detail": "needs Python first"})
            self.missing = [s for s in steps if not s["ok"]]
            return {"steps": steps, "all_ok": not self.missing,
                    "state": self.state, "python": py}

    def start_install(self) -> bool:
        """Kick off the background installer thread (False if one runs)."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self.state = "installing"
            self.error = ""
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._install_flow,
                                            daemon=True, name="env-setup")
            self._thread.start()
            return True

    def _install_flow(self) -> None:
        try:
            # -- 1) python
            py = self._find_python()
            if not py:
                if not self._install_python():
                    self.state = "failed"
                    self.error = ("Python could not be installed automatically. "
                                  "Install Python 3.12 from python.org, then "
                                  "press Retry.")
                    return
                py = self._rediscover_python()
                if not py:
                    self.state = "failed"
                    self.error = ("Python installed but not found afterwards — "
                                  "restart the tool and press Retry.")
                    return
                self.python_exe = py
            self._emit(f"using Python: {py}")

            # -- 2) pip deps (full requirement set in one shot)
            missing = self._missing_deps(py)
            if missing:
                pip_names = [GEN_PIP_NAMES.get(m, m) for m in missing]
                if not self._pip_install(py, pip_names):
                    self.state = "failed"
                    self.error = "pip install failed — see the log lines above"
                    return
                still = self._missing_deps(py)
                if still:
                    self.state = "failed"
                    self.error = f"still missing after install: {', '.join(still)}"
                    return
            else:
                self._emit("all Python packages already installed ✓")

            # -- 3) chromium
            if not self._chromium_ok(py):
                self._emit("installing Playwright Chromium (one-time, ~170 MB)…")
                if not self._run([py, "-m", "playwright", "install", "chromium"]):
                    self.state = "failed"
                    self.error = "chromium install failed — see the log above"
                    return
            else:
                self._emit("Chromium already installed ✓")

            self.state = "done"
            self.finished_at = time.time()
            self._emit("environment ready ✓ — you can start workers / claims now")
        except Exception as e:
            self.state = "failed"
            self.error = f"{type(e).__name__}: {e}"
            log.exception("env setup failed")

    def status(self) -> dict:
        return {
            "state": self.state,           # idle|checking|installing|done|failed
            "error": self.error,
            "python": self.python_exe,
            "missing": self.missing,
            "running": bool(self._thread and self._thread.is_alive()),
            "lines": list(self.lines)[-60:],
            "elapsed": int(time.time() - self.started_at) if self.started_at else 0,
        }


class PlaywrightSetup:
    """On-demand installer for the generator's browser dependency.

    The exe cannot bundle Playwright + Chromium (~200 MB); instead, the first
    time a signup worker or 100M claim needs a browser, missing pieces are
    installed into the system Python right then: `pip install playwright`
    and `playwright install chromium`. Progress streams into the dashboard
    log so the user sees what is happening."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = False           # verified once per process
        self._installing = False

    def status(self) -> dict:
        return {"installing": self._installing, "ready": self._done}

    def _run(self, argv: list[str], emit) -> bool:
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                cwd=str(Path(argv[0]).parent) if os.path.isfile(argv[0]) else None,
                **_child_kwargs())
        except Exception as e:
            emit(f"  playwright setup failed to start: {type(e).__name__}: {e}")
            return False
        for line in proc.stdout or []:
            line = line.rstrip()
            if line:
                emit(f"  {line[:160]}")
        proc.wait()
        return proc.returncode == 0

    def ensure(self, python_exe: str, emit=print) -> bool:
        """Make sure `python_exe` can run Playwright + Chromium. Fast no-op
        when everything is already installed."""
        with self._lock:
            if self._done:
                return True
            self._installing = True
        try:
            py = python_exe
            # 1) package present?
            chk = subprocess.run([py, "-c", "import playwright"],
                                 capture_output=True, **_child_kwargs())
            if chk.returncode != 0:
                emit("playwright not found — installing into your Python "
                     "(one-time, ~40 MB)…")
                if not self._run([py, "-m", "pip", "install", "--upgrade",
                                  "playwright"], emit):
                    emit("  playwright install FAILED — see lines above")
                    return False
            # 2) chromium browser present? `playwright install` is idempotent:
            # it verifies the cache and exits fast when up to date.
            emit("checking chromium browser (one-time setup)…")
            if not self._run([py, "-m", "playwright", "install", "chromium"],
                             emit):
                emit("  chromium install FAILED — see lines above")
                return False
            self._done = True
            emit("playwright ready ✓")
            return True
        finally:
            self._installing = False


class GenWorker:
    def __init__(self, name: str, argv: list[str], env: dict[str, str],
                 gen_dir: Path, on_exit=None):
        self.name = name
        self.argv = argv
        self.env = env
        self.gen_dir = gen_dir
        self.on_exit = on_exit
        self.proc: subprocess.Popen | None = None
        self.lines: deque[str] = deque(maxlen=WORKER_LOG_LINES)
        self.state = "starting"          # starting|running|exited|failed|killed
        self.exit_code: int | None = None
        # '0'/'' are the panel's "off" sentinels, not real proxies
        _px = env.get("ZAI_PROXY", "").strip()
        if _px in ("", "0"):
            _px = None
        _ptypes = env.get("ZAI_PROXY_TYPES", "").strip()
        self.proxy = _px or (
            "pool" + (f" ({_ptypes})" if _ptypes else
                      (" (socks)" if env.get("ZAI_SOCKS_ONLY") == "1" else ""))
            if env.get("ZAI_USE_POOL") == "1" else "direct IP")
        self.captcha = "manual" if env.get("ZAI_MANUAL") == "1" else "auto"
        self.email: str | None = None
        self.starter: bool | None = None
        self.stopped = False
        self.created = time.time()
        # outcomes observed on the output files (set by the watcher)
        out_dir = Path(env.get("ZAI_OUTPUT_DIR", gen_dir / "output"))
        self.account_file = out_dir / f"accounts{env.get('ZAI_OUT_SUFFIX', '')}.txt"
        self._account_rows_at_start = self._rows_now()

    def _rows_now(self) -> int:
        try:
            return sum(1 for ln in self.account_file.read_text(encoding="utf-8",
                                                                errors="ignore").splitlines()
                       if ln.strip())
        except OSError:
            return 0

    def start(self):
        env = os.environ.copy()
        env.update(self.env)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self.proc = subprocess.Popen(
                self.argv, cwd=str(self.gen_dir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                **_child_kwargs())
        except Exception as e:
            self.state = "failed"
            self.lines.append(f"launch error: {e}")
            if self.on_exit:
                self.on_exit(self)
            return
        self.state = "running"
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        import re
        if not (self.proc and self.proc.stdout):
            self.state = "failed"
            return
        for line in self.proc.stdout:
            line = line.rstrip()
            self.lines.append(line)
            if "Account token email=" in line:
                self.email = line.split("Account token email=")[-1].strip()
            elif "Using pool proxy:" in line:
                self.proxy = line.split("Using pool proxy:")[-1].strip()
            elif "proxy=" in line and "email=" in line:
                m_px = re.search(r"proxy=([^\s]+)", line)
                if m_px:
                    self.proxy = m_px.group(1)
            elif "1 with Starter plan" in line:
                self.starter = True
            elif "0 with Starter plan" in line and self.starter is None:
                self.starter = False
            if ("100M Global Build active" in line or "issued=1" in line
                    or "progress=3/3" in line or "tasks 3/3 complete" in line):
                self.global_build = True
        self.proc.wait()
        code = self.proc.returncode
        if self.stopped:
            self.state = "killed"
        elif code:
            self.state = "failed"
        else:
            self.state = "exited"
        self.exit_code = code
        self.check_new_account()
        if self.on_exit:
            self.on_exit(self)

    def check_new_account(self):
        """A successful run leaves one more row in accounts<SUF>.txt — surface
        the email/password for pool registration only if it received a plan."""
        if self.starter is False:
            return None
        try:
            rows = [ln for ln in self.account_file.read_text(
                encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
        except OSError:
            return None
        if len(rows) > self._account_rows_at_start:
            # Pick the newest valid line added during this run
            for row in reversed(rows[self._account_rows_at_start:]):
                parts = row.split(":")
                if len(parts) >= 3 and parts[-1].startswith("eyJ"):
                    self.email = parts[0]
                    return {"email": parts[0], "password": parts[1],
                            "chat_token": parts[-1]}
        return None

    def stop(self):
        self.stopped = True
        if self.proc and self.proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               capture_output=True, timeout=5, **_child_kwargs())
            except Exception:
                pass
            try:
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()
            except Exception:
                pass
            self.state = "killed"

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def to_public(self) -> dict:
        return {
            "name": self.name,
            "proxy": self.proxy,
            "captcha": self.captcha,
            "state": self.state,
            "exit_code": self.exit_code,
            "email": self.email,
            "starter": self.starter,
            "global_build": getattr(self, "global_build", False),
            "lines": list(self.lines)[-80:],
        }


class ProxyChecker:
    """Runs `proxy_pool.py <action>` and tracks the funnel in live state.

    The child emits JSONL events (`@event {...}`) alongside its human lines, so
    stats no longer depend on regexing prose. Legacy prose parsing stays as a
    fallback for older generator folders.
    """

    ACTIONS = ("fetch", "verify", "recheck", "all", "geo", "stats", "prune",
               "clear-bad", "clear-used", "clear-dead")

    def __init__(self, gen_dir: Path, python_exe: str = "", data_dir: Path | None = None):
        self.gen_dir = gen_dir
        self.data_dir = Path(data_dir) if data_dir else gen_dir / "output"
        self.python_exe = resolve_python(python_exe)
        self.proc: subprocess.Popen | None = None
        self.lines: deque[str] = deque(maxlen=600)
        self.action: str | None = None
        self.state = "idle"          # idle|running|exited|failed
        self.started = 0.0
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.stats: dict = {}
        # background auto-verify (feature: pool never goes stale)
        self.auto_verify = False
        self.auto_interval_min = 45
        self._auto_thread: threading.Thread | None = None
        self._auto_stop = threading.Event()
        self.last_verified_at: float | None = None

    def set_auto_verify(self, enabled: bool, interval_min: int | None = None) -> dict:
        """Background re-verify loop: re-probes the usable pool so the claim
        picker never faces an all-stale candidate set."""
        with self._lock:
            self.auto_verify = bool(enabled)
            if interval_min:
                self.auto_interval_min = max(5, int(interval_min))
        if enabled and not (self._auto_thread and self._auto_thread.is_alive()):
            self._auto_stop.clear()
            self._auto_thread = threading.Thread(target=self._auto_loop, daemon=True)
            self._auto_thread.start()
        if not enabled:
            self._auto_stop.set()
        return {"auto_verify": self.auto_verify,
                "interval_min": self.auto_interval_min}

    def _auto_loop(self) -> None:
        while not self._auto_stop.wait(self.auto_interval_min * 60):
            try:
                if not self.auto_verify:
                    continue
                # recheck = re-probe the existing usable pool (fast, no fetch)
                res = self.run("recheck")
                if "error" not in res:
                    self.last_verified_at = time.time()
            except Exception:
                log.exception("proxy auto-verify failed")

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def run(self, action: str = "verify", timeout: int | None = None,
            concurrency: int | None = None, max_latency: float | None = None,
            preset: str | None = None, prefilter: bool | None = None,
            retry_failed: bool | None = None, dead_cache_hours: float | None = None,
            probe_timeout: float | None = None,
            probe_concurrency: int | None = None) -> dict:
        action = action if action in self.ACTIONS else "verify"
        with self._lock:
            if self.alive():
                return {"error": "checker already running", "action": self.action}
            self.action = action
            self.state = "running"
            self.started = time.time()
            self.lines.clear()
            for key in ("probed", "alive", "total", "best", "median", "raw",
                        "pool", "scanned", "ever_good", "reasons", "labels",
                        "second_chance", "prefilter", "geo_ok", "geo_updated",
                        "elapsed"):
                self.stats.pop(key, None)
            self.refresh_stats()
            env = os.environ.copy()
            env.update({"PYTHONUNBUFFERED": "1",
                        "ZAI_OUTPUT_DIR": str(self.data_dir),
                        "PROXY_EVENTS": "1"})
            if preset:
                env["ZAI_SCAN_PRESET"] = str(preset)
            if timeout is not None:
                env["PROXY_TIMEOUT"] = str(timeout)
            if concurrency is not None:
                env["PROXY_CONC"] = str(concurrency)
            if max_latency is not None:
                env["PROXY_MAX_LATENCY"] = str(max_latency)
            if probe_timeout is not None:
                env["PROXY_PROBE_TIMEOUT"] = str(probe_timeout)
            if probe_concurrency is not None:
                env["PROXY_PROBE_CONC"] = str(probe_concurrency)
            if prefilter is not None:
                env["PROXY_PREFILTER"] = "1" if prefilter else "0"
            if retry_failed is not None:
                env["PROXY_RETRY_FAILED"] = "1" if retry_failed else "0"
            if dead_cache_hours is not None:
                env["PROXY_DEAD_HOURS"] = str(dead_cache_hours)
            self.proc = subprocess.Popen(
                [self.python_exe, "-u", "proxy_pool.py", action],
                cwd=str(self.gen_dir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                **_child_kwargs())
            self._thread = threading.Thread(target=self._pump, daemon=True)
            self._thread.start()
        return {"started": action}

    def _apply_event(self, ev: dict) -> None:
        t = ev.get("t")
        if t == "start":
            self.stats.update(total=ev.get("total", 0), probed=0, alive=0)
            for key in ("timeout", "concurrency", "prefilter"):
                if key in ev:
                    self.stats[key] = ev[key]
        elif t == "prefilter":
            self.stats.update(prefilter={"endpoints": ev.get("endpoints", 0),
                                         "open": ev.get("open", 0),
                                         "remaining": ev.get("remaining", 0)})
        elif t == "reasons":
            self.stats.update(probed=ev.get("probed", 0), total=ev.get("total", 0),
                              alive=ev.get("alive", 0), reasons=ev.get("reasons") or {})
            if ev.get("labels"):
                self.stats["labels"] = ev["labels"]
        elif t == "second_chance":
            self.stats["second_chance"] = ev.get("count", 0)
        elif t == "summary":
            self.stats.update(total=ev.get("total", 0), alive=ev.get("alive", 0),
                              pool=ev.get("pool"), slow=ev.get("slow", 0),
                              reasons=ev.get("reasons") or {},
                              labels=ev.get("by_reason_labels") or {})
            if ev.get("best") is not None:
                self.stats["best"] = ev["best"]
            if ev.get("median") is not None:
                self.stats["median"] = ev["median"]
        elif t == "done":
            if ev.get("ever_good") is not None:
                self.stats["ever_good"] = ev["ever_good"]
            if ev.get("elapsed") is not None:
                self.stats["elapsed"] = ev["elapsed"]
        elif t == "fetch_done":
            self.stats["raw"] = ev.get("raw", 0)
        elif t == "geo":
            self.stats.update(geo_ok=bool(ev.get("ok")), geo_updated=ev.get("updated"),
                              geo_size_mb=ev.get("size_mb"))
        elif t == "stats":
            for key in ("count", "usable", "healthy", "failed", "bad", "used",
                        "avg_latency", "by_proto"):
                if key in ev:
                    self.stats[key] = ev[key]

    def _pump(self):
        import json as _json
        if not (self.proc and self.proc.stdout):
            self.state = "failed"
            return
        for line in self.proc.stdout:
            line = line.rstrip()
            self.lines.append(line)
            if line.startswith('@event '):
                try:
                    self._apply_event(_json.loads(line[7:]))
                except (ValueError, TypeError):
                    pass
                continue
            m = re.search(r"\[\s*checkpoint\]\s*(\d+)/(\d+)\s+probed,\s*(\d+)\s+alive", line)
            if m:
                self.stats.update(probed=int(m.group(1)), total=int(m.group(2)),
                                  alive=int(m.group(3)))
            m = re.search(r"(\d+)/(\d+)\s+good=(\d+)", line)
            if m:
                self.stats.update(probed=int(m.group(1)), total=int(m.group(2)),
                                  alive=int(m.group(3)))
            m = re.search(r"verifying\s+(\d+)\s+proxies", line)
            if m:
                self.stats.update(total=int(m.group(1)), probed=0, alive=0)
            m = re.search(r"strict recheck of\s+(\d+)\s+pooled proxies", line)
            if m:
                self.stats.update(total=int(m.group(1)), probed=0, alive=0)
            m = re.search(r"raw pool:\s*(\d+)\s*proxies", line)
            if m:
                self.stats.update(raw=int(m.group(1)))
            m = re.search(r"(?:working proxies|pool now):\s*(\d+)\s*/\s*(\d+)", line)
            if m:
                self.stats.update(pool=int(m.group(1)), scanned=int(m.group(2)))
            m = re.search(r"latency:\s*best\s*([\d.]+)s\s*median\s*([\d.]+)s", line)
            if m:
                self.stats.update(best=float(m.group(1)), median=float(m.group(2)))
            m = re.search(r"ever-good[^:]*:\s*(\d+)", line)
            if m:
                self.stats["ever_good"] = int(m.group(1))
            m = re.search(r"second chance:\s*(\d+)", line)
            if m:
                self.stats["second_chance"] = int(m.group(1))
            m = re.search(r"why they failed:\s*(.+)$", line)
            if m:
                self.stats["failure_line"] = m.group(1)
        self.proc.wait()
        self.state = "exited" if self.proc.returncode == 0 else "failed"
        if self.state == "exited" and self.action in ("verify", "recheck", "all"):
            self.last_verified_at = time.time()
        self.refresh_stats()

    def stop(self):
        if self.alive():
            try:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               capture_output=True, timeout=5, **_child_kwargs())
            except Exception:
                pass
            try:
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()
            except Exception:
                pass
            self.state = "exited"

    def _count(self, name: str) -> int:
        try:
            return sum(1 for ln in (self.data_dir / name).read_text(
                encoding="utf-8", errors="ignore").splitlines() if ln.strip())
        except OSError:
            return 0

    def _load_bad(self) -> set[str]:
        try:
            return {ln.split("\t")[0].strip()
                    for ln in (self.data_dir / "proxies_bad.txt").read_text(
                        encoding="utf-8", errors="ignore").splitlines()
                    if ln.strip()}
        except OSError:
            return set()

    def _load_used(self) -> set[str]:
        try:
            return {ln.split("\t")[0].strip()
                    for ln in (self.data_dir / "proxies_used.txt").read_text(
                        encoding="utf-8", errors="ignore").splitlines()
                    if ln.strip()}
        except OSError:
            return set()

    def _socks(self) -> int:
        bad = self._load_bad()
        used = self._load_used()
        excluded = bad | used
        seen = set()
        try:
            for ln in (self.data_dir / "proxies.txt").read_text(
                    encoding="utf-8", errors="ignore").splitlines():
                p = ln.strip()
                if p and p not in excluded and p.split("://", 1)[0].lower() in ("socks4", "socks5"):
                    seen.add(p)
        except OSError:
            pass
        return len(seen)

    def refresh_stats(self):
        self.stats.update(pool_file=self._count("proxies.txt"),
                          ever_good_file=self._count("proxies_evergood.txt"),
                          bad=self._count("proxies_bad.txt"),
                          used=self._count("proxies_used.txt"),
                          socks=self._socks(),
                          raw=self._count("proxies_raw.txt"))
        # pool.json is the source of truth now: health, types, usable count
        pool = self._read_json("pool.json")
        entries = pool if isinstance(pool, dict) else {}
        if entries:
            usable = healthy = failed = unverified = 0
            by_proto: dict[str, int] = {}
            lat = 0.0
            bad = self._load_bad()
            used = self._load_used()
            for url, e in entries.items():
                if not isinstance(e, dict):
                    continue
                fails = int(e.get("fails", 0) or 0)
                if fails == 0:
                    healthy += 1
                if fails > 0:
                    failed += 1
                if e.get("vouched") and not int(e.get("last_ok", 0) or 0):
                    unverified += 1
                last_ok = int(e.get("last_ok", 0) or 0)
                last_check = int(e.get("last_check", 0) or 0)
                result = e.get("last_result")
                if result is None:
                    result = "ok" if last_check <= last_ok else "fail"
                verified_now = last_ok > 0 and result == "ok"
                if url not in bad and url not in used and verified_now and fails <= 3:
                    usable += 1
                proto = str(e.get("proto") or url.split("://", 1)[0]).lower()
                by_proto[proto] = by_proto.get(proto, 0) + 1
                try:
                    lat += float(e.get("latency", 0) or 0)
                except (TypeError, ValueError):
                    pass
            self.stats.update(pool_json=len(entries), usable=usable, healthy=healthy,
                              failed=failed, unverified=unverified, types=by_proto,
                              avg_latency=round(lat / len(entries), 2) if entries else 0)
        geo = self.data_dir / "geo" / "dbip-country-lite.csv.gz"
        try:
            st = geo.stat()
            self.stats["geo"] = {"available": True, "size_mb": round(st.st_size / 1e6, 1),
                                 "updated": time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))}
        except OSError:
            self.stats["geo"] = {"available": False}

    def _read_json(self, name: str):
        try:
            return json.loads((self.data_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def sources_yield(self) -> list[dict]:
        """Per-source yield from the last scans (best first)."""
        data = self._read_json("proxy_sources_stats.json")
        rows = []
        if isinstance(data, dict):
            for url, row in data.items():
                if not isinstance(row, dict):
                    continue
                runs = max(1, int(row.get("runs", 1) or 1))
                rows.append({**row, "url": url,
                             "avg": round(int(row.get("found", 0) or 0) / runs, 1)})
        rows.sort(key=lambda r: (int(r.get("found", 0) or 0), -int(r.get("errors", 0) or 0)),
                  reverse=True)
        return rows

    def status(self) -> dict:
        self.refresh_stats()
        elapsed = int(time.time() - self.started) if self.started else 0
        return {"state": self.state, "action": self.action,
                "running": self.alive(), "elapsed": elapsed,
                "stats": self.stats, "lines": list(self.lines)[-100:]}

    # ---- import + editable sources ----------------------------------------- #
    # Parsing is shared with the generator (generator/proxy_parse.py) so a pasted
    # list is understood identically in the CLI and the dashboard.

    def _proxy_pool(self):
        sys.path.insert(0, str(self.gen_dir))
        import proxy_pool
        proxy_pool.set_output_dir(str(self.data_dir))
        return proxy_pool

    def import_proxies(self, text: str, into_good: bool = True,
                       default_proto: str = "http") -> int:
        """Add pasted/uploaded proxies to the pool.

        Accepts schemeful URLs, bare ip:port, user:pass@, provider style
        host:port:user:pass, CIDR ranges, HTML tables and JSON. ``default_proto``
        decides what a bare ip:port means ("http", "socks5", "all", ...).
        """
        try:
            n = self._proxy_pool().import_proxies(text, into_good=into_good,
                                                  default_proto=default_proto or "http")
        except Exception as e:
            log.warning("proxy import via pool failed (%s); using local parser", e)
            n = self._import_local(text, into_good, default_proto)
        self.refresh_stats()
        return n

    def _import_local(self, text: str, into_good: bool, default_proto: str) -> int:
        sys.path.insert(0, str(self.gen_dir))
        import proxy_parse as pp
        entries = [pp.to_url(e["proto"], e["host"], e["port"], e["auth"])
                   for e in pp.parse_proxy_list(text, default_proto or "http")]
        if not entries:
            return 0
        out = self.data_dir
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "proxies_raw.txt", "a", encoding="utf-8") as f:
            f.write("\n".join(entries) + "\n")
        if into_good:
            with open(out / "proxies.txt", "a", encoding="utf-8") as f:
                f.write("\n".join(entries) + "\n")
            with open(out / "proxies_evergood.txt", "a", encoding="utf-8") as f:
                f.write("\n".join(entries) + "\n")
        return len(entries)

    def sources_text(self) -> str:
        """Current editable sources file ('' if none yet)."""
        p = self.data_dir / "proxy_sources.txt"
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return ""

    def set_sources(self, text: str) -> int:
        """Replace the editable sources file; returns the source-line count."""
        p = self.data_dir / 'proxy_sources.txt'
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + '.tmp')
        tmp.write_text(text or '', encoding='utf-8', newline='\n')
        os.replace(tmp, p)
        return sum(1 for ln in (text or '').splitlines()
                   if ln.strip() and not ln.strip().startswith('#'))


class ClaimJob:
    """Standalone 100M claim for one account (generator/claim100.py).

    Runs with a visible browser and manual accept, like a normal claim; the
    final `CLAIM_RESULT {...}` line carries the referral summary."""
    _RES = "CLAIM_RESULT "

    def __init__(self, name: str, email: str, mode: str, proxy: str,
                 gen_dir: Path, python_exe: str, invite_code: str,
                 data_dir: Path | None = None):
        self.name, self.email, self.mode, self.proxy = name, email, mode, proxy
        self.gen_dir = Path(gen_dir)
        self.data_dir = Path(data_dir) if data_dir else self.gen_dir / "output"
        argv = [python_exe, "-u", "claim100.py", email]
        if mode == "direct":
            argv.append("--direct")
        elif proxy:
            argv += ["--proxy", proxy]
        self.argv = argv
        self.invite_code = invite_code
        self.proc: subprocess.Popen | None = None
        self.state = "starting"      # starting|running|exited|failed|killed
        self.stopped = False
        self.started = 0.0
        self.result: dict | None = None
        self.lines: deque[str] = deque(maxlen=220)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def start(self):
        env = os.environ.copy()
        env.update({"PYTHONUNBUFFERED": "1",
                    "ZAI_OUTPUT_DIR": str(self.data_dir),
                    "ZAI_INVITE_CODE": self.invite_code or ""})
        if getattr(self, "_internal_token", ""):
            env["ZAI_GATEWAY_INTERNAL_TOKEN"] = self._internal_token
        self.started = time.time()
        self.state = "running"
        try:
            self.proc = subprocess.Popen(
                self.argv, cwd=str(self.gen_dir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                **_child_kwargs())
        except Exception as e:
            self.state = "failed"
            self.lines.append(f"launch error: {e}")
            return
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def to_public(self) -> dict:
        # strip the private claim JWT before the result reaches the dashboard
        res = self.result
        if isinstance(res, dict):
            res = {k: v for k, v in res.items() if k != "_claim_jwt"}
        return {"name": self.name, "email": self.email, "mode": self.mode,
                "proxy": re.sub(r"//[^/]*@", "//", self.proxy) if self.proxy else "", "state": self.state,
                "elapsed": int(time.time() - self.started) if self.started else 0,
                "result": res,
                "lines": list(self.lines)[-12:]}

    def _pump(self):
        import json as _json
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = (line or "").rstrip()
            if not line:
                continue
            if line.startswith(self._RES):
                try:
                    self.result = _json.loads(line[len(self._RES):])
                except (ValueError, TypeError):
                    self.result = {"raw": line[len(self._RES):][:400]}
                continue
            if line.startswith("CLAIM_RESULT"):
                try:
                    self.result = _json.loads(line.split(" ", 1)[1])
                except (ValueError, TypeError, IndexError):
                    self.result = {"raw": line[:400]}
                continue
            self.lines.append(line)
        self.proc.wait()
        self.state = "exited" if self.proc.returncode == 0 else "failed"

    def stop(self):
        self.stopped = True
        if self.proc and self.proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               capture_output=True, timeout=5, **_child_kwargs())
            except Exception:
                pass
            try:
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()
            except Exception:
                pass
        self.state = "killed"


class GeneratorManager:
    """Owns worker processes + pipeline options. One manager per gateway."""

    def __init__(self, gen_dir: str | Path, on_account=None, python_exe: str = "",
                 data_dir: str | Path = None):
        self.gen_dir = Path(gen_dir)
        self.on_account = on_account        # callback(dict) -> pool registration
        self.python_exe = resolve_python(python_exe)
        # Portable data dir: a frozen gateway keeps worker output beside the exe
        # (./generator-data); a source run uses ZCODEAUTO's own output/.
        self.data_dir = Path(data_dir) if data_dir else self.gen_dir / "output"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["ZAI_OUTPUT_DIR"] = str(self.data_dir)

        # Migrate/preserve existing proxies from gen_dir/output if data_dir is empty
        try:
            if self.data_dir.resolve() != (self.gen_dir / "output").resolve():
                import shutil
                for fname in ("proxies.txt", "proxies_evergood.txt", "proxies_raw.txt", "proxy_sources.txt"):
                    target = self.data_dir / fname
                    src = self.gen_dir / "output" / fname
                    if not target.exists() and src.exists():
                        shutil.copy2(src, target)
        except OSError:
            pass
        self.workers: dict[str, GenWorker] = {}
        self.claims: dict[str, ClaimJob] = {}
        self.checker = ProxyChecker(self.gen_dir, self.python_exe,
                                    data_dir=self.data_dir)
        self._seq = 0
        self._lock = threading.Lock()
        # per-run internal auth token for gateway admin calls from children
        self.internal_token = ""
        # on-demand playwright installer (signup workers + 100M claims)
        self.playwright = PlaywrightSetup()
        # full first-run environment setup (python + deps + chromium)
        self.env_setup = EnvSetup()
        # options (dashboard-editable)
        self.captcha = "manual"             # auto | manual — default: you solve it
        self.ip_source = "direct"           # socks | pool | direct | pin — default: my network (no proxy)
        self.proxy_types = "socks"          # all | socks | socks5 | socks4 | http | csv
        self.country = "ALL"                # ALL | US | DE | ...
        self.pin_proxy = ""
        self.retries = 5
        self.claim = True                   # invite Global Build claim on/off
        self.headless = False               # default: visible window (manual captcha needs it)
        self._invite_code = "MT9GNDQ8"      # referral code ('' = off; overridden by config)
        self.pool_file = self.data_dir / "proxies.txt"
        self.evergood_file = self.data_dir / "proxies_evergood.txt"
        self.bad_file = self.data_dir / "proxies_bad.txt"
        # email system: user-owned domains + their own Cloudflare Worker inbox
        self.mail_cfg_path = self.data_dir / "mail_config.json"
        self._load_mail_config()
        # re-apply dashboard options saved by the previous run
        self.load_options()

    # ---- email system -------------------------------------------------------

    def _default_mail(self) -> dict:
        return {"domains": [], "active": "", "inbox_url": "", "inbox_secret": ""}

    def _load_mail_config(self):
        try:
            self.mail_cfg = json.loads(self.mail_cfg_path.read_text(encoding="utf-8"))
            if not isinstance(self.mail_cfg, dict):
                raise ValueError
        except (OSError, ValueError):
            self.mail_cfg = self._default_mail()
        for k, v in self._default_mail().items():
            self.mail_cfg.setdefault(k, v)

    def _save_mail_config(self):
        self.mail_cfg_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.mail_cfg_path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(self.mail_cfg, indent=1), encoding='utf-8')
        os.replace(tmp, self.mail_cfg_path)

    def mail_config(self) -> dict:
        """Current config with the secret masked for display."""
        m = dict(self.mail_cfg)
        sec = m.get("inbox_secret") or ""
        m["inbox_secret_set"] = bool(sec)
        m["inbox_secret_masked"] = (sec[:4] + "…" + sec[-3:]) if len(sec) > 8 else ("•" * len(sec))
        # inbox_ok  = the worker endpoint answers (worker reachability only)
        # routing_done = the user confirmed Cloudflare forwards mail to it —
        #   this CANNOT be detected automatically (no API for catch-all rules),
        #   so it is an explicit per-domain confirmation.
        m["inbox_ok"] = bool(m.get("inbox_ok"))
        done = {str(d).strip().lower() for d in (m.get("routing_done") or [])}
        doms = [str(d).strip().lower() for d in (m.get("domains") or [])]
        m["routing_done"] = sorted(done)
        m["routing_all_done"] = bool(doms) and all(d in done for d in doms)
        m["routing_pending"] = [d for d in doms if d not in done]
        m.pop("inbox_secret", None)
        return m

    def mark_inbox_verified(self, ok: bool) -> None:
        """Persist the last inbox smoke-test result (worker reachability)."""
        self.mail_cfg["inbox_ok"] = bool(ok)
        self._save_mail_config()

    def set_routing_done(self, domain: str, done: bool = True) -> dict:
        """Record that the user finished Cloudflare email routing for a domain.

        There is no API for Email Routing catch-all rules through wrangler, so
        this is a deliberate manual confirmation — never inferred from the
        inbox test (which only proves the worker itself answers)."""
        d = str(domain or "").strip().lower()
        if not d:
            return self.mail_config()
        doms = [str(x).strip().lower() for x in (self.mail_cfg.get("routing_done") or [])]
        if done and d not in doms:
            doms.append(d)
        elif not done and d in doms:
            doms.remove(d)
        self.mail_cfg["routing_done"] = doms
        self._save_mail_config()
        return self.mail_config()

    # ---- registration ledger ------------------------------------------------ #

    def _registered_path(self) -> Path:
        return self.data_dir / "registered.json"

    def registered_emails(self) -> set[str]:
        try:
            data = json.loads(self._registered_path().read_text(encoding="utf-8"))
            return {str(e).strip().lower() for e in (data.get("emails") or []) if str(e).strip()}
        except (OSError, ValueError):
            return set()

    def mark_registered(self, email: str) -> None:
        """Remember that an account was written into the gateway pool."""
        email = str(email or "").strip().lower()
        if not email:
            return
        got = self.registered_emails() | {email}
        p = self._registered_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"emails": sorted(got)}, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    def pending_accounts(self) -> list[dict]:
        """Accounts sitting in accounts*.txt / starter*.txt that never reached
        the pool — e.g. created while the gateway was closed, or a registration
        step failed. Lets the gateway catch up on start instead of silently
        leaving successful signups out of rotation."""
        done = self.registered_emails()
        out, seen = [], set()
        for pat in ("starter*.txt",):
            for f in sorted(self.data_dir.glob(pat)):
                try:
                    lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
                except OSError:
                    continue
                for ln in lines:
                    ln = ln.strip()
                    if not ln or ln.count(":") < 2:
                        continue
                    email, password, token = ln.split(":", 2)
                    email, token = email.strip().lower(), token.strip()
                    if not email or email in seen or email in done:
                        continue
                    if not token.startswith("eyJ"):
                        continue
                    seen.add(email)
                    out.append({"email": email, "password": password,
                                "chat_token": token})
        return out

    # ---- 100M referral finisher --------------------------------------------- #
    # The invite chain needs: bind + login_device + ONE client message. The
    # message sits behind Aliyun risk control, which answers 405/3012 until it
    # relents (same behaviour the relay sees). So an account that got 2/3 is
    # remembered here and retried in the background until the reward issues.

    def _pending_ref_path(self) -> Path:
        return self.data_dir / "pending_referral.json"

    def pending_referrals(self) -> dict:
        """{email: zcodeJWT} for accounts still waiting on the send_message task."""
        try:
            data = json.loads(self._pending_ref_path().read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in (data.get("pending") or {}).items() if v}
        except (OSError, ValueError):
            return {}

    def _save_pending_refs(self, pending: dict) -> None:
        p = self._pending_ref_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"pending": pending}, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    def add_pending_referral(self, email: str, zjwt: str) -> None:
        email, zjwt = str(email or "").strip().lower(), str(zjwt or "").strip()
        if not email or not zjwt:
            return
        pending = self.pending_referrals()
        pending[email] = zjwt
        self._save_pending_refs(pending)

    def remove_pending_referral(self, email: str) -> None:
        email = str(email or "").strip().lower()
        pending = self.pending_referrals()
        if email in pending:
            pending.pop(email)
            self._save_pending_refs(pending)

    def export_worker(self, dest: str | Path = None) -> dict:
        """Copy the bundled mail-worker source to disk so the user can deploy it.

        The worker (Cloudflare Email Worker) is bundled inside the exe; without
        this a public user has nothing to run `wrangler deploy` against.
        """
        import shutil as _sh
        candidates = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "mail-worker")
        candidates.append(Path(__file__).resolve().parent.parent / "mail-worker")
        candidates.append(self.gen_dir.parent / "mail-worker")
        src = next((c for c in candidates if c.is_dir()), None)
        if not src:
            return {"ok": False, "error": "worker source not found in this build"}
        out = Path(dest) if dest else self.data_dir.parent / "mail-worker"
        try:
            if out.exists():
                _sh.rmtree(out, ignore_errors=True)
            _sh.copytree(src, out)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        files = sorted(p.name for p in out.rglob('*') if p.is_file())
        return {"ok": True, "path": str(out), "files": files[:40]}

    # ---- one-click worker deploy -------------------------------------------- #

    def _worker_dir(self) -> Path:
        return self.data_dir.parent / "mail-worker"

    # ---- node.js tooling ----------------------------------------------------- #

    @staticmethod
    def _npx() -> str:
        """Full path to npx. Windows CreateProcess does NOT apply PATHEXT, so a
        bare 'npx' raises WinError 2 — always resolve through shutil.which."""
        import shutil as _sh
        npx = _sh.which("npx") or _sh.which("npx.cmd") or _sh.which("npx.exe")
        if npx:
            return npx
        # common install dirs not on the *process* PATH (e.g. node installed
        # after this process started)
        for cand in (r"C:\Program Files\nodejs\npx.cmd",
                     r"C:\Program Files (x86)\nodejs\npx.cmd",
                     os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs\npx.cmd")):
            if os.path.isfile(cand):
                return cand
        return ""

    @staticmethod
    def _refresh_path_from_registry() -> None:
        """Node was just installed: pull machine+user PATH from the registry so
        this process (and shutil.which) can see it without a restart."""
        try:
            import winreg as _reg
        except ImportError:
            return
        parts: list[str] = []
        for root, key_path in (
            (_reg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (_reg.HKEY_CURRENT_USER, r"Environment"),
        ):
            try:
                with _reg.OpenKey(root, key_path) as k:
                    val, _typ = _reg.QueryValueEx(k, "Path")
                    parts.extend(p for p in val.split(";") if p and p not in parts)
            except OSError:
                continue
        if parts:
            os.environ["PATH"] = ";".join(parts) + ";" + os.environ.get("PATH", "")

    def _ensure_node(self, progress=None) -> dict:
        """Node.js present? If not, INSTALL it for the user right now:
        winget first (handles UAC), else the official MSI downloaded from
        nodejs.org. Returns {"ok", "node", "npm", "npx"}."""
        import shutil as _sh

        def say(msg):
            log.info("node install: %s", msg)
            if progress:
                try:
                    progress(msg)
                except Exception:
                    pass

        if _sh.which("node") and _sh.which("npm"):
            npx = self._npx()
            if npx:
                return {"ok": True, "node": _sh.which("node"),
                        "npm": _sh.which("npm"), "npx": npx}

        say("Node.js not found — installing it now (one time)…")

        # 1) winget: present on stock Win10/11; MSI install with silent flags
        winget = _sh.which("winget") or _sh.which("winget.exe")
        if winget:
            say("installing Node.js LTS via winget…")
            try:
                r = subprocess.run(
                    [winget, "install", "--id", "OpenJS.NodeJS.LTS", "-e",
                     "--silent", "--accept-package-agreements",
                     "--accept-source-agreements"],
                    capture_output=True, text=True, timeout=900,
                    encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                out = (r.stdout or "") + (r.stderr or "")
                if r.returncode == 0 or "installed" in out.lower():
                    say("winget install finished")
                else:
                    say(f"winget failed (exit {r.returncode}) — trying the official MSI")
            except Exception as exc:
                say(f"winget unavailable ({type(exc).__name__}) — trying the official MSI")

        # 2) verify; if still missing, download + run the official MSI silently
        self._refresh_path_from_registry()
        if not _sh.which("node"):
            try:
                import httpx as _hx
                say("fetching the latest Node.js LTS version…")
                idx = _hx.get("https://nodejs.org/dist/index.json", timeout=30).json()
                lts = next((e for e in idx if e.get("lts")), idx[0])
                ver = lts["version"]  # e.g. v22.14.0
                msi_url = f"https://nodejs.org/dist/{ver}/node-{ver}-x64.msi"
                msi_path = Path(os.environ.get("TEMP", ".")) / f"node-{ver}-x64.msi"
                say(f"downloading Node.js {ver} (~30 MB)…")
                with _hx.stream("GET", msi_url, timeout=60, follow_redirects=True) as resp:
                    resp.raise_for_status()
                    with open(msi_path, "wb") as fh:
                        for chunk in resp.iter_bytes(1 << 20):
                            fh.write(chunk)
                say("running the Node.js installer (confirm the UAC prompt)…")
                r = subprocess.run(
                    ["msiexec", "/i", str(msi_path), "/passive", "/norestart"],
                    timeout=900, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode != 0:
                    return {"ok": False,
                            "error": f"Node.js installer exited {r.returncode} — "
                                     f"install it manually from https://nodejs.org and retry"}
                self._refresh_path_from_registry()
            except Exception as exc:
                return {"ok": False,
                        "error": f"automatic Node.js install failed "
                                 f"({type(exc).__name__}: {exc}) — install it "
                                 f"manually from https://nodejs.org and retry"}

        # 3) final check
        node = _sh.which("node")
        npm = _sh.which("npm")
        npx = self._npx()
        if not (node and npm and npx):
            return {"ok": False,
                    "error": "Node.js was installed but is not on this "
                             "process's PATH yet — restart the gateway once"}
        say("Node.js ready")
        return {"ok": True, "node": node, "npm": npm, "npx": npx}

    def _node_tools(self) -> dict:
        return self._ensure_node()

    def worker_deploy_status(self) -> dict:
        """What the dashboard needs to show before/after a deploy."""
        wdir = self._worker_dir()
        info = {"worker_dir": str(wdir), "exported": wdir.is_dir(),
                "has_url": bool(self.mail_cfg.get("inbox_url")),
                "has_secret": bool(self.mail_cfg.get("inbox_secret")),
                "node_ok": bool(self._npx())}
        if not info["exported"]:
            return info
        npx = self._npx()
        if not npx:
            # node genuinely missing: say so instead of a cryptic 500
            info["wrangler_authed"] = None
            info["node_missing"] = True
            return info
        # whoami tells us wrangler auth without opening a browser
        r = subprocess.run(
            [npx, "wrangler", "whoami"], cwd=str(wdir),
            capture_output=True, text=True, timeout=60,
            shell=False, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = _clean_output((r.stdout or "") + (r.stderr or ""))
        info["wrangler_authed"] = "You are logged in" in out
        if "@" in out and "logged in" in out:
            for tok in out.split():
                if "@" in tok and "." in tok:
                    info["account"] = tok.strip("👋🔐 ")
                    break
        return info

    def deploy_worker(self, progress=None) -> dict:
        """One-click deploy: export files, ensure Node (auto-install if
        missing), wrangler login (browser only if needed), create KV namespace
        + patch wrangler.toml, deploy, set INBOX_SECRET, return the worker URL.
        `progress` gets human strings."""
        import re as _re

        def say(msg):
            log.info("worker deploy: %s", msg)
            if progress:
                try:
                    progress(msg)
                except Exception:
                    pass

        tools = self._ensure_node(progress)
        if not tools["ok"]:
            return tools
        npx = tools["npx"]
        exp = self.export_worker()
        if not exp.get("ok"):
            return exp
        wdir = Path(exp["path"])
        say("worker files exported")

        def run(args, timeout=300):
            return subprocess.run(args, cwd=str(wdir), capture_output=True,
                                  text=True, timeout=timeout, shell=False,
                                  encoding="utf-8", errors="replace",
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def kv_id_for_title(title: str = "MAIL") -> str:
            """Find an existing KV namespace by title. Idempotent deploys: the
            namespace is reused instead of failing with 'already exists'."""
            r = run([npx, "wrangler", "kv", "namespace", "list"], timeout=120)
            out = _clean_output((r.stdout or "") + (r.stderr or ""))
            # wrangler prints a JSON array; fall back to the table form
            a, b = out.find("["), out.rfind("]")
            if a != -1 and b > a:
                try:
                    for ns in json.loads(out[a:b + 1]):
                        if str(ns.get("title", "")).strip().lower() == title.lower():
                            return ns.get("id") or ""
                except Exception:
                    pass
            for pat in (r"\b([0-9a-f]{32})\b[^\n]*?\b" + title + r"\b",
                        r"\b" + title + r"\b[^\n]*?\b([0-9a-f]{32})\b"):
                m = _re.search(pat, out, _re.I)
                if m:
                    return m.group(1)
            return ""

        # 1) auth: whoami first — if logged in, skip the browser entirely
        r = run([npx, "wrangler", "whoami"], timeout=90)
        out = _clean_output((r.stdout or "") + (r.stderr or ""))
        if "You are logged in" not in out:
            say("opening browser for Cloudflare login (one time)…")
            r = run([npx, "wrangler", "login"], timeout=600)
            out = _clean_output((r.stdout or "") + (r.stderr or ""))
            if "You are logged in" not in out:
                # login may have succeeded even on nonzero exit; recheck
                r2 = run([npx, "wrangler", "whoami"], timeout=90)
                out2 = _clean_output((r2.stdout or "") + (r2.stderr or ""))
                if "You are logged in" not in out2:
                    return {"ok": False,
                            "error": "Cloudflare login did not complete — try again"}

        # 2) KV namespace: REUSE an existing one titled MAIL (idempotent),
        #    else create it; always write the resolved id back into wrangler.toml
        toml_path = wdir / "wrangler.toml"
        toml_src = toml_path.read_text(encoding="utf-8")

        kv_id = kv_id_for_title("MAIL")
        if kv_id:
            say("found existing KV namespace 'MAIL' — reusing it")

        if not kv_id:
            # maybe the id already in wrangler.toml is real (not the placeholder)
            m = _re.search(r'id\s*=\s*"([0-9a-f]{32})"', toml_src)
            cand = m.group(1) if m else ""
            if cand and set(cand) != {"0"}:
                chk = run([npx, "wrangler", "kv", "namespace", "info",
                           "--namespace-id", cand], timeout=120)
                if chk.returncode == 0:
                    kv_id = cand
                    say("reusing KV namespace from wrangler.toml")

        if not kv_id:
            say("creating KV namespace…")
            r = run([npx, "wrangler", "kv", "namespace", "create", "MAIL"],
                    timeout=180)
            out = _clean_output((r.stdout or "") + (r.stderr or ""))
            m = _re.search(r'id\s*=\s*"([0-9a-f]{32})"', out) \
                or _re.search(r"\b([0-9a-f]{32})\b", out)
            if m:
                kv_id = m.group(1)
            else:
                # created concurrently / output unparsed — re-list and recover
                kv_id = kv_id_for_title("MAIL")
            if not kv_id:
                return {"ok": False,
                        "error": "KV namespace creation failed: " + _tail(out)}

        toml_src = _re.sub(r'(id\s*=\s*)"[^"]*"', r'\1"%s"' % kv_id,
                           toml_src, count=1)
        toml_path.write_text(toml_src, encoding="utf-8")

        # 3) deploy
        say("deploying worker…")
        r = run([npx, "wrangler", "deploy"], timeout=600)
        out = _clean_output((r.stdout or "") + (r.stderr or ""))
        # the URL is https://<worker>.<account-subdomain>.workers.dev — the
        # subdomain is its own label, so allow dotted labels before .workers.dev
        m = _re.search(r"https://[a-z0-9][a-z0-9.\-]*\.workers\.dev", out, _re.I)
        if r.returncode != 0 and not m:
            return {"ok": False, "error": "deploy failed: " + _tail(out)}
        base_url = m.group(0).rstrip(".,)") if m else None
        if not base_url:
            # deployed fine but the account has no workers.dev route enabled
            low = out.lower()
            if "workers.dev" in low and ("disabled" in low or "not enabled" in low
                                         or "enable" in low):
                return {"ok": False, "error":
                        "worker deployed, but this account has workers.dev "
                        "disabled — enable it in the Cloudflare dashboard "
                        "(Workers → your worker → Settings → Domains & Routes) "
                        "or paste the worker's /inbox URL below manually"}
            return {"ok": False,
                    "error": "deployed but could not read the workers.dev URL "
                             "from wrangler output — paste the worker's /inbox "
                             "URL into the field below and press Save manually. "
                             "Deploy said: " + _tail(out, 300)}
        say("deployed at " + base_url)

        # 4) INBOX_SECRET: use the configured one, or generate and store it
        secret = self.mail_cfg.get("inbox_secret") or ""
        if not secret:
            import secrets as _secrets
            secret = _secrets.token_urlsafe(24)
            self.mail_cfg["inbox_secret"] = secret
            self._save_mail_config()
        say("uploading INBOX_SECRET…")
        proc = subprocess.Popen(
            [npx, "wrangler", "secret", "put", "INBOX_SECRET"],
            cwd=str(wdir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out, _ = proc.communicate(input=secret + "\n", timeout=180)
        clean = _clean_output(out or "")
        if proc.returncode != 0 or "success" not in clean.lower():
            return {"ok": False, "inbox_url": base_url + "/inbox",
                    "secret_generated": True,
                    "error": "worker deployed but the secret upload failed: "
                             + _tail(out)}

        # 5) persist the URL and smoke-test
        self.mail_cfg["inbox_url"] = base_url + "/inbox"
        self._save_mail_config()
        say("testing inbox…")
        import time as _time
        test = {"ok": False}
        for _ in range(3):
            try:
                import httpx as _hx
                resp = _hx.get(base_url + "/inbox",
                               params={"k": secret}, timeout=15)
                test = {"ok": resp.status_code == 200}
                break
            except Exception as e:
                test = {"ok": False, "error": str(e)}
                _time.sleep(3)
        # remember the result so the wizard can mark step 3 done (persisted)
        self.mark_inbox_verified(bool(test.get("ok")))
        return {"ok": True, "inbox_url": self.mail_cfg["inbox_url"],
                "worker_url": base_url, "kv_id": kv_id,
                "secret_generated": True, "test": test}

    def set_mail_config(self, **kw) -> dict:
        cfg = self.mail_cfg
        if "domains" in kw:
            doms = []
            for d in (kw["domains"] or []):
                d = str(d or "").strip().lower().lstrip('@')
                if d and re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', d) and d not in doms:
                    doms.append(d)
            cfg["domains"] = doms
            if doms and cfg.get("active") not in doms:
                cfg["active"] = doms[0]
        if "active" in kw:
            a = str(kw["active"] or "").strip().lower().lstrip('@')
            if a in cfg["domains"]:
                cfg["active"] = a
        if "inbox_url" in kw:
            u = str(kw["inbox_url"] or "").strip()
            if u and not u.startswith(("http://", "https://")):
                raise ValueError("inbox_url must start with http:// or https://")
            if u != cfg.get("inbox_url"):
                cfg["inbox_ok"] = False   # endpoint changed → re-verify
            cfg["inbox_url"] = u
        if "inbox_secret" in kw and str(kw["inbox_secret"] or "").strip():
            cfg["inbox_secret"] = str(kw["inbox_secret"]).strip()
            cfg["inbox_ok"] = False       # new secret → re-verify
        self._save_mail_config()
        return self.mail_config()

    # ---- options ------------------------------------------------------------

    PROXY_TYPE_SCHEMES = {
        "all": (),
        "socks": ("socks4", "socks5"),
        "socks5": ("socks5",),
        "socks4": ("socks4",),
        "http": ("http", "https"),
    }

    @classmethod
    def normalize_proxy_types(cls, value: str, *, legacy_socks_only: bool | None = None) -> str:
        """Validate/canonicalize a proxy_types setting. Accepts the keywords
        above or a comma list of schemes. Returns '' for 'all'."""
        v = str(value or "").strip().lower()
        if legacy_socks_only is not None and v == "socks" and not legacy_socks_only:
            v = "all"      # old socks_only: false config meant the full pool
        if not v or v == "all":
            return "all"
        if v in cls.PROXY_TYPE_SCHEMES:
            return v
        # comma list of raw schemes
        parts = [p.strip().lower() for p in v.split(",") if p.strip()]
        valid = [p for p in parts if p in ("http", "https", "socks4", "socks5")]
        return ",".join(valid) if valid else "all"

    def _proxy_type_schemes(self) -> tuple[str, ...]:
        v = self.normalize_proxy_types(self.proxy_types)
        if v == "all":
            return ()
        if v in self.PROXY_TYPE_SCHEMES:
            return self.PROXY_TYPE_SCHEMES[v]
        return tuple(v.split(","))

    def _options_path(self) -> Path:
        return self.data_dir / "generator_options.json"

    _OPTION_FIELDS = ("captcha", "ip_source", "country", "proxy_types",
                      "pin_proxy", "retries", "claim", "headless",
                      "invite_code")

    def save_options(self) -> None:
        """Persist dashboard generator options so they survive restarts."""
        data = {
            "captcha": self.captcha,
            "ip_source": self.ip_source,
            "country": getattr(self, "country", "ALL"),
            "proxy_types": self.normalize_proxy_types(self.proxy_types),
            "pin_proxy": self.pin_proxy,
            "retries": self.retries,
            "claim": self.claim,
            "headless": self.headless,
            "invite_code": self._invite_code,
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._options_path().with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self._options_path())
        except OSError:
            log.warning("could not persist generator options", exc_info=True)

    def load_options(self) -> None:
        """Re-apply saved options at manager creation (missing file = defaults)."""
        try:
            raw = json.loads(self._options_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        # reuse the validation in set_options; invite_code maps to the field
        kw = {k: v for k, v in raw.items() if k in self._OPTION_FIELDS}
        if kw:
            self.set_options(**kw)

    def set_options(self, **kw):
        if "captcha" in kw and kw["captcha"] in ("auto", "manual"):
            self.captcha = kw["captcha"]
        if "ip_source" in kw and kw["ip_source"] in ("socks", "pool", "direct", "pin"):
            # 'socks' was the old socks-only pool entry — now the pool with the
            # proxy_types filter applied; normalize so the UI shows one option
            self.ip_source = "pool" if kw["ip_source"] == "socks" else kw["ip_source"]
        if "country" in kw:
            self.country = str(kw["country"] or "ALL").strip().upper()
        if "proxy_types" in kw:
            self.proxy_types = self.normalize_proxy_types(kw["proxy_types"])
        if "pin_proxy" in kw:
            self.pin_proxy = str(kw["pin_proxy"] or "").strip()
        if "retries" in kw:
            try:
                self.retries = max(0, int(kw["retries"]))
            except (TypeError, ValueError):
                pass
        if "claim" in kw:
            v = kw["claim"]
            self.claim = v if isinstance(v, bool) else (str(v).lower() in ("true", "1", "yes", "on"))
        if "headless" in kw:
            v = kw["headless"]
            self.headless = v if isinstance(v, bool) else (str(v).lower() in ("true", "1", "yes", "on", "auto"))
        # manual captcha means a human must see and solve it — a headless browser
        # makes that impossible, so headless is forced off whenever captcha=manual
        if self.captcha == "manual":
            self.headless = False
        if "invite_code" in kw:
            self._invite_code = str(kw["invite_code"] or "").strip()
        # options changed from the dashboard: persist for the next start
        self.save_options()

    def _geo_db(self):
        """Offline DB-IP lookup if the generator downloaded it; never network."""
        try:
            sys.path.insert(0, str(self.gen_dir))
            import proxy_geo
            proxy_geo.set_data_dir(str(self.data_dir))
            return proxy_geo
        except Exception:
            return None

    def _country_counts(self) -> dict[str, int]:
        bad = self._load_bad()
        used = self._load_used()
        excluded = bad | used
        try:
            lines = [ln.strip() for ln in self.pool_file.read_text(
                encoding="utf-8", errors="ignore").splitlines()
                if ln.strip() and ln.strip() not in excluded]
        except OSError:
            return {}
        cache_file = self.data_dir / "ip_country.json"
        cache = {}
        if cache_file.exists():
            try:
                raw = json.loads(cache_file.read_text(encoding="utf-8", errors="ignore"))
                if isinstance(raw, dict):
                    cache = {k: str(v).upper() for k, v in raw.items() if v}
            except Exception:
                pass
        # the verified pool already carries the country it was scored with
        try:
            pool = json.loads((self.data_dir / "pool.json").read_text(encoding="utf-8"))
            for e in (pool.values() if isinstance(pool, dict) else []):
                if not isinstance(e, dict):
                    continue
                cc = str(e.get("country") or "").upper()
                host = e.get("host") or ""
                if cc and host and host not in cache:
                    cache[host] = cc
        except (OSError, ValueError):
            pass

        ip_re = re.compile(r"://(?:[^@/]+@)?([^:/]+)")
        missing = list(dict.fromkeys(
            m.group(1) for p in lines
            for m in [ip_re.search(p)] if m and m.group(1) not in cache))
        if missing:
            db = self._geo_db()
            if db is not None and db.available():
                added = False
                for ip in missing[:5000]:
                    cc = db.lookup(ip).get("country", "")
                    if cc:
                        cache[ip] = cc.upper()
                        added = True
                if added:
                    try:
                        cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")
                    except OSError:
                        pass

        from collections import Counter
        counts = Counter()
        schemes = self._proxy_type_schemes()
        for p in lines:
            if schemes and p.split("://", 1)[0].lower() not in schemes:
                continue
            m = ip_re.search(p)
            if m:
                cc = cache.get(m.group(1), "")
                if cc:
                    counts[cc] += 1
        return dict(counts.most_common(20))

    def _pool_type_counts(self) -> dict[str, int]:
        """Distinct usable proxies per scheme in the live pool (bad/used
        excluded, country-filtered). Scheme keys: http/https/socks4/socks5."""
        bad = self._load_bad()
        used = self._load_used()
        excluded = bad | used
        counts: dict[str, int] = {}
        cache_file = self.data_dir / "ip_country.json"
        cache = {}
        target_country = str(getattr(self, "country", "ALL") or "ALL").upper()
        if target_country != "ALL" and cache_file.exists():
            try:
                raw = json.loads(cache_file.read_text(encoding="utf-8", errors="ignore"))
                if isinstance(raw, dict):
                    cache = {k: str(v).upper() for k, v in raw.items() if v}
            except Exception:
                pass
        if target_country != "ALL":
            try:
                pool = json.loads((self.data_dir / "pool.json").read_text(encoding="utf-8"))
                for e in (pool.values() if isinstance(pool, dict) else []):
                    if isinstance(e, dict):
                        cc = str(e.get("country") or "").upper()
                        if cc and e.get("host") and e["host"] not in cache:
                            cache[e["host"]] = cc
            except (OSError, ValueError):
                pass
        try:
            for ln in self.pool_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                v = ln.strip()
                if not v or v in excluded or "://" not in v:
                    continue
                if target_country != "ALL":
                    m = re.search(r"://(?:[^@/]+@)?([^:/]+)", v)
                    if not m or cache.get(m.group(1), "") != target_country:
                        continue
                scheme = v.split("://", 1)[0].lower()
                counts[scheme] = counts.get(scheme, 0) + 1
        except OSError:
            pass
        return counts

    def _types_counts(self) -> dict[str, int]:
        """Usable pool size for each proxy_types keyword (for the dropdown)."""
        c = self._pool_type_counts()
        return {
            "all": sum(c.values()),
            "socks": c.get("socks4", 0) + c.get("socks5", 0),
            "socks5": c.get("socks5", 0),
            "socks4": c.get("socks4", 0),
            "http": c.get("http", 0) + c.get("https", 0),
        }

    def _socks_count(self) -> int:
        """Number of distinct SOCKS proxies currently available in the live pool."""
        return self._types_counts()["socks"]

    def options(self) -> dict:
        tc = self._types_counts()
        return {"captcha": self.captcha, "ip_source": self.ip_source,
                "proxy_types": self.normalize_proxy_types(self.proxy_types),
                "types_counts": tc,
                "pool_available": tc.get(
                    self.normalize_proxy_types(self.proxy_types), tc["all"]),
                "country": getattr(self, "country", "ALL"),
                "countries_available": self._country_counts(),
                "pin_proxy": self.pin_proxy, "retries": self.retries,
                "claim": self.claim, "headless": self.headless,
                "invite_code": self._invite_code,
                "socks_available": tc["socks"],
                "mail": self.mail_config(),
                "data_dir": str(self.data_dir)}

    def _load_used(self) -> set[str]:
        try:
            return {ln.split("\t")[0].strip()
                    for ln in (self.data_dir / "proxies_used.txt").read_text(
                        encoding="utf-8", errors="ignore").splitlines()
                    if ln.strip()}
        except OSError:
            return set()

    def _load_bad(self) -> set[str]:
        try:
            return {ln.split("\t")[0].strip()
                    for ln in self.bad_file.read_text(encoding="utf-8",
                                                      errors="ignore").splitlines()
                    if ln.strip()}
        except OSError:
            return set()

    def _pick_proxy(self, socks_only: bool, tries: int = 25) -> str | None:
        """Verify-on-pick from the pool, honouring the configured type filter."""
        try:
            proxy_pool = self._proxy_pool()
            schemes = self._proxy_type_schemes()
            return proxy_pool.pick_verified(tries=tries, socks=socks_only,
                                            country=self.country,
                                            types=schemes or None)
        except Exception as e:
            log.warning("proxy_pool.pick_verified failed: %s", e)
        return None

    # ---- workers ------------------------------------------------------------

    def _ensure_playwright(self, emit=None) -> bool:
        """Signup workers and claims drive a real Chromium via Playwright.
        The dependency is installed on first use, into the system Python —
        never bundled in the exe."""
        if emit is None:
            emit = lambda line: log.info("playwright-setup: %s", line)
        return self.playwright.ensure(self.python_exe, emit)

    def start_workers(self, count: int = 1) -> dict:
        with self._lock:
            started = []
            for _ in range(max(1, min(count, 8))):
                self._seq += 1
                name = f"g{self._seq}"
                env = self._worker_env(name)
                w = GenWorker(name, [self.python_exe, "-u", "main.py", "1"], env,
                              self.gen_dir, on_exit=self._worker_done)
                self.workers[name] = w
                w.start()
                started.append(name)
        return {"started": started}

    def start_workers_checked(self, count: int = 1, emit=print) -> dict:
        """start_workers, but with a guaranteed Playwright setup first
        (streams one-time install progress to the caller via `emit`)."""
        if not self._ensure_playwright(emit):
            return {"error": "playwright setup failed — check the log"}
        return self.start_workers(count)

    def _worker_env(self, name: str) -> dict[str, str]:
        env = {
            "ZAI_OUT_SUFFIX": name,
            "ZAI_OUTPUT_DIR": str(self.data_dir),
            "ZAI_PROXY": "0", "ZAI_USE_POOL": "0", "ZAI_SOCKS_ONLY": "0",
            "ZAI_PROXY_COUNTRY": self.country or "ALL",
            "ZAI_MANUAL": "1" if self.captcha == "manual" else "0",
            "ZAI_HEADLESS": "0" if self.captcha == "manual" else ("1" if self.headless else "0"),
            "ZAI_PROXY_RETRIES": str(self.retries),
        }
        if self.internal_token:
            env["ZAI_GATEWAY_INTERNAL_TOKEN"] = self.internal_token
        if self.claim and self._invite_code:
            env["ZAI_INVITE_CODE"] = self._invite_code
        else:
            env["ZAI_INVITE_CODE"] = ""   # explicit OFF (main.py treats '' as off)
        # email: the user's own domains + worker inbox (public-ready setup)
        m = self.mail_cfg
        if m.get("active"):
            env["ZAI_MAIL_DOMAIN"] = m["active"]
        if m.get("inbox_url"):
            env["ZAI_INBOX_URL"] = m["inbox_url"]
        if m.get("inbox_secret"):
            env["ZAI_INBOX_SECRET"] = m["inbox_secret"]
        if self.ip_source in ("socks", "pool"):
            env["ZAI_USE_POOL"] = "1"
            schemes = self._proxy_type_schemes()
            if schemes:
                env["ZAI_PROXY_TYPES"] = ",".join(schemes)
        elif self.ip_source == "pin" and self.pin_proxy:
            env["ZAI_PROXY"] = self.pin_proxy
        elif self.ip_source == "direct":
            env["ZAI_PROXY"] = "0"
        return env

    def _worker_done(self, w: GenWorker):
        new = w.check_new_account()
        if new and self.on_account:
            try:
                self.on_account(new)
            except Exception:
                log.exception("pool registration failed for %s", new.get("email"))

    def stop_worker(self, name: str) -> bool:
        w = self.workers.get(name)
        if w and w.alive():
            w.stop()
            return True
        return False

    def stop_all(self):
        for w in self.workers.values():
            if w.alive():
                w.stop()

    def remove_worker(self, name: str) -> bool:
        """Drop one worker card (running ones are killed first)."""
        w = self.workers.get(name)
        if not w:
            return False
        if w.alive():
            w.stop()
        self.workers.pop(name, None)
        return True

    def remove_finished_workers(self) -> int:
        names = [n for n, w in self.workers.items() if not w.alive()]
        for n in names:
            self.workers.pop(n, None)
        return len(names)

    # ---- per-account 100M claims (Accounts tab) ------------------------------ #

    def start_claim(self, email: str, mode: str = "proxy", allow_direct_fallback: bool = True) -> dict:
        """Claim 100M for one account, separately from the generator flow.

        mode: 'proxy' — a pool proxy (pinned proxy first), 'direct' — no proxy.
        The claim browser is visible and waits for a manual Accept like a
        generator claim. When the pool pick fails in proxy mode, a quick
        re-verify runs once and the pick retries; if it still fails, the claim
        falls back to direct (the bind is session-authenticated, not
        IP-bound) instead of dead-ending."""
        email = (email or "").strip().lower()
        if not email:
            return {"error": "account has no email"}
        alive = [j for j in self.claims.values() if j.alive()]
        if len(alive) >= 3:
            return {"error": "too many claims running"}
        if any(j.alive() and j.email == email for j in self.claims.values()):
            return {"error": "a claim for this account is already running"}
        if mode == "proxy":
            proxy = self._proxy_for_claim()
            if not proxy:
                # one quick background recheck of the usable pool, then retry
                log.info("claim: pool pick empty — running a quick recheck")
                try:
                    self.checker.run("recheck")
                    # wait briefly for the recheck to finish (bounded)
                    for _ in range(120):
                        if not self.checker.alive():
                            break
                        time.sleep(1)
                    proxy = self._proxy_for_claim()
                except Exception:
                    log.exception("claim: auto-recheck failed")
            if not proxy:
                if not allow_direct_fallback:
                    return {"error": "no usable proxy in the pool (run a proxy scan first)"}
                log.info("claim: pool still empty — falling back to DIRECT")
                mode = "direct"
        else:
            proxy = ""
        if not self._ensure_playwright():
            return {"error": "playwright setup failed — check the log"}
        name = f"claim-{int(time.time())}"
        job = ClaimJob(name, email, mode, proxy, self.gen_dir,
                       self.python_exe, self._invite_code,
                       data_dir=self.data_dir)
        job._internal_token = self.internal_token
        job.start()
        self.claims[name] = job
        return {"started": name, "proxy": job.proxy, "mode": mode}

    def socks_ready_count(self) -> int:
        """Live socks count for the claim health badge (cheap heuristic:
        usable + socks entries in the current pool snapshot)."""
        try:
            pool = self._proxy_pool()
            bad = pool.load_bad()
            used = pool.load_used()
            entries = pool._load_pool_map()
            return sum(1 for e in entries.values()
                       if pool._is_usable(e, bad, used, pool.MAX_FAILS)
                       and 'socks' in str(e.get('url', '')).lower())
        except Exception:
            return -1

    def claim_all_pending(self, mode: str = "proxy") -> dict:
        """Queue 100M claims sequentially for every starter account whose
        referral summary shows the Global Build not yet issued."""
        started, skipped, errors = [], [], []
        for info in self.pending_accounts():
            email = (info.get("email") or "").strip().lower()
            if not email:
                continue
            if any(j.email == email for j in self.claims.values()):
                skipped.append(email)
                continue
            res = self.start_claim(email, mode=mode)
            if res.get("started"):
                started.append(email)
                # throttle: wait for the job to finish before the next one
                job = self.claims.get(res["started"])
                while job and job.alive():
                    time.sleep(2)
            else:
                errors.append(f"{email}: {res.get('error', '?')}")
        return {"started": started, "skipped": skipped, "errors": errors}

    def _proxy_for_claim(self) -> str:
        o = self.options()
        pinned = str(o.get("pinned") or "").strip()
        if pinned:
            return pinned
        try:
            pool = self._proxy_pool()
            return pool.pick_verified(tries=8, socks=True) or ""
        except Exception as e:
            log.warning("claim pool pick failed: %s", e)
            return ""

    def claims_public(self) -> list[dict]:
        return [j.to_public() for j in self.claims.values()]

    def stop_claim(self, name: str) -> bool:
        j = self.claims.get(name)
        if j and j.alive():
            j.stop()
            return True
        return False

    def remove_claim(self, name: str) -> bool:
        j = self.claims.get(name)
        if not j:
            return False
        if j.alive():
            j.stop()
        self.claims.pop(name, None)
        return True

    def clear_finished_claims(self) -> int:
        names = [n for n, j in self.claims.items() if not j.alive()]
        for n in names:
            self.claims.pop(n, None)
        return len(names)

    def status(self) -> dict:
        return {
            "options": self.options(),
            "workers": [w.to_public() for w in self.workers.values()],
            "claims": self.claims_public(),
            "alive": sum(1 for w in self.workers.values() if w.alive()),
        }
