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
        else:
            exe_dir = Path(__file__).resolve().parent.parent
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
        self.proxy = _px or (
            "pool" + (" (socks)" if env.get("ZAI_SOCKS_ONLY") == "1" else "")
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
        assert self.proc and self.proc.stdout
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
            if "100M Global Build active" in line or "issued=1" in line:
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
        the email/password for pool registration."""
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
    """Runs `proxy_pool.py <action>` (fetch | verify | recheck) and parses the
    funnel: raw -> alive -> pool, latency, ever-good, MITM blacklist."""

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

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def run(self, action: str, timeout: int = 8, concurrency: int = 400,
            max_latency: float = 6.0) -> dict:
        action = action if action in ("fetch", "verify", "recheck", "all") else "verify"
        with self._lock:
            if self.alive():
                return {"error": "checker already running", "action": self.action}
            self.action = action
            self.state = "running"
            self.started = time.time()
            self.lines.clear()
            self.stats.pop("probed", None)
            self.stats.pop("alive", None)
            self.stats.pop("total", None)
            self.stats.pop("best", None)
            self.stats.pop("median", None)
            self.refresh_stats()
            env = os.environ.copy()
            env.update({"PYTHONUNBUFFERED": "1",
                        "PROXY_TIMEOUT": str(timeout),
                        "PROXY_CONC": str(concurrency),
                        "PROXY_MAX_LATENCY": str(max_latency)})
            self.proc = subprocess.Popen(
                [self.python_exe, "-u", "proxy_pool.py", action],
                cwd=str(self.gen_dir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                **_child_kwargs())
            self._thread = threading.Thread(target=self._pump, daemon=True)
            self._thread.start()
        return {"started": action}

    def _pump(self):
        import re
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip()
            self.lines.append(line)
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
        self.proc.wait()
        self.state = "exited" if self.proc.returncode == 0 else "failed"
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

    def _socks(self) -> int:
        seen = set()
        for f in ("proxies.txt", "proxies_evergood.txt"):
            try:
                for ln in (self.data_dir / f).read_text(
                        encoding="utf-8", errors="ignore").splitlines():
                    p = ln.strip()
                    if p and p.split("://", 1)[0].lower() in ("socks4", "socks5"):
                        seen.add(p)
            except OSError:
                pass
        return len(seen)

    def refresh_stats(self):
        self.stats.update(pool_file=self._count("proxies.txt"),
                          ever_good_file=self._count("proxies_evergood.txt"),
                          bad=self._count("proxies_bad.txt"),
                          socks=self._socks(),
                          raw=self._count("proxies_raw.txt"))

    def status(self) -> dict:
        self.refresh_stats()
        elapsed = int(time.time() - self.started) if self.started else 0
        return {"state": self.state, "action": self.action,
                "running": self.alive(), "elapsed": elapsed,
                "stats": self.stats, "lines": list(self.lines)[-100:]}

    # ---- import + editable sources ----------------------------------------- #
    # Implemented here (not via proxy_pool) so the frozen gateway never needs the
    # generator's third-party deps (requests/pysocks) just to manage files.

    _IPPORT = re.compile(r'^(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})$')

    def import_proxies(self, text: str, into_good: bool = True) -> int:
        """Add pasted/uploaded proxies to the pool. Accepts proto://ip:port or
        bare ip:port (defaults http); splits on whitespace/comma/semicolon."""
        entries = []
        for raw in re.split(r'[\s,;]+', (text or '').strip()):
            if not raw:
                continue
            if '://' in raw:
                proto, rest = raw.split('://', 1)
                if proto in ('http', 'https', 'socks4', 'socks5') and self._IPPORT.match(rest):
                    entries.append(f'{proto}://{rest}')
            elif self._IPPORT.match(raw):
                entries.append(f'http://{raw}')
        if not entries:
            return 0
        out = self.data_dir
        out.mkdir(parents=True, exist_ok=True)
        with open(out / 'proxies_raw.txt', 'a', encoding='utf-8') as f:
            f.write('\n'.join(entries) + '\n')
        if into_good:
            existing = set()
            try:
                existing = set((out / 'proxies.txt').read_text(
                    encoding='utf-8').split())
            except OSError:
                pass
            fresh = [e for e in entries if e not in existing]
            if fresh:
                with open(out / 'proxies.txt', 'a', encoding='utf-8') as f:
                    f.write('\n'.join(fresh) + '\n')
                with open(out / 'proxies_evergood.txt', 'a', encoding='utf-8') as f:
                    f.write('\n'.join(fresh) + '\n')
        self.refresh_stats()
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
        self.workers: dict[str, GenWorker] = {}
        self.checker = ProxyChecker(self.gen_dir, self.python_exe,
                                    data_dir=self.data_dir)
        self._seq = 0
        self._lock = threading.Lock()
        # options (dashboard-editable)
        self.captcha = "auto"               # auto | manual
        self.ip_source = "socks"            # socks | pool | direct | pin
        self.pin_proxy = ""
        self.retries = 5
        self.claim = True                   # invite Global Build claim on/off
        self.headless = True
        self._invite_code = ""              # claimed via the worker env (set by api layer)
        self.pool_file = self.data_dir / "proxies.txt"
        self.evergood_file = self.data_dir / "proxies_evergood.txt"
        self.bad_file = self.data_dir / "proxies_bad.txt"
        # email system: user-owned domains + their own Cloudflare Worker inbox
        self.mail_cfg_path = self.data_dir / "mail_config.json"
        self._load_mail_config()

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
        m.pop("inbox_secret", None)
        return m

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
        out = (r.stdout or "") + (r.stderr or "")
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

        # 1) auth: whoami first — if logged in, skip the browser entirely
        r = run([npx, "wrangler", "whoami"], timeout=90)
        out = (r.stdout or "") + (r.stderr or "")
        if "You are logged in" not in out:
            say("opening browser for Cloudflare login (one time)…")
            r = run([npx, "wrangler", "login"], timeout=600)
            out = (r.stdout or "") + (r.stderr or "")
            if "You are logged in" not in out:
                # login may have succeeded even on nonzero exit; recheck
                r2 = run([npx, "wrangler", "whoami"], timeout=90)
                out2 = (r2.stdout or "") + (r2.stderr or "")
                if "You are logged in" not in out2:
                    return {"ok": False,
                            "error": "Cloudflare login did not complete — try again"}

        # 2) KV namespace: reuse the id already in wrangler.toml if valid,
        #    otherwise create one and patch the file
        toml_path = wdir / "wrangler.toml"
        toml_src = toml_path.read_text(encoding="utf-8")
        m = _re.search(r'id\s*=\s*"([0-9a-f]{32})"', toml_src)
        kv_id = m.group(1) if m else ""
        kv_ok = False
        if kv_id:
            chk = run([npx, "wrangler", "kv", "namespace", "info",
                       "--namespace-id", kv_id], timeout=120)
            kv_ok = chk.returncode == 0
        if not kv_ok:
            say("creating KV namespace…")
            r = run([npx, "wrangler", "kv", "namespace", "create", "MAIL"],
                    timeout=180)
            out = (r.stdout or "") + (r.stderr or "")
            m = _re.search(r'id\s*=\s*"([0-9a-f]{32})"', out)
            if not m:
                return {"ok": False,
                        "error": "KV namespace creation failed: " + out[-400:]}
            kv_id = m.group(1)
            toml_src = _re.sub(r'(id\s*=\s*)"[0-9a-f]{32}"', r'\1"%s"' % kv_id,
                               toml_src, count=1)
            toml_path.write_text(toml_src, encoding="utf-8")
            say("KV namespace created and wired into wrangler.toml")

        # 3) deploy
        say("deploying worker…")
        r = run([npx, "wrangler", "deploy"], timeout=600)
        out = (r.stdout or "") + (r.stderr or "")
        m = _re.search(r"(https://[a-z0-9-]+\.workers\.dev)", out, _re.I)
        if r.returncode != 0 and not m:
            return {"ok": False, "error": "deploy failed: " + out[-600:]}
        base_url = m.group(1) if m else None
        if not base_url:
            return {"ok": False,
                    "error": "deployed but could not read the workers.dev URL — "
                             "check the Cloudflare dashboard"}
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
            errors="replace", shell=False)
        out, _ = proc.communicate(input=secret + "\n", timeout=180)
        if proc.returncode != 0 or "Successfully created secret" not in (out or "") \
                and "secret" not in (out or "").lower():
            return {"ok": False, "inbox_url": base_url + "/inbox",
                    "secret_generated": True,
                    "error": "worker deployed but the secret upload failed: "
                             + (out or "")[-400:]}

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
            cfg["inbox_url"] = u
        if "inbox_secret" in kw and str(kw["inbox_secret"] or "").strip():
            cfg["inbox_secret"] = str(kw["inbox_secret"]).strip()
        self._save_mail_config()
        return self.mail_config()

    # ---- options ------------------------------------------------------------

    def set_options(self, **kw):
        if "captcha" in kw and kw["captcha"] in ("auto", "manual"):
            self.captcha = kw["captcha"]
        if "ip_source" in kw and kw["ip_source"] in ("socks", "pool", "direct", "pin"):
            self.ip_source = kw["ip_source"]
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
        if "invite_code" in kw:
            self._invite_code = str(kw["invite_code"] or "").strip()

    def options(self) -> dict:
        return {"captcha": self.captcha, "ip_source": self.ip_source,
                "pin_proxy": self.pin_proxy, "retries": self.retries,
                "claim": self.claim, "headless": self.headless,
                "invite_code": self._invite_code,
                "socks_available": self._socks_count(),
                "mail": self.mail_config(),
                "data_dir": str(self.data_dir)}

    def _socks_count(self) -> int:
        seen = set()
        for f in (self.pool_file, self.evergood_file):
            try:
                for ln in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    s = ln.strip().split("://", 1)[0].lower()
                    if s in ("socks4", "socks5"):
                        seen.add(s)
            except OSError:
                continue
        return len(seen)

    def _load_bad(self) -> set[str]:
        try:
            return {ln.split("\t")[0].strip()
                    for ln in self.bad_file.read_text(encoding="utf-8",
                                                      errors="ignore").splitlines()
                    if ln.strip()}
        except OSError:
            return set()

    def _pick_proxy(self, socks_only: bool, tries: int = 25) -> str | None:
        """Verify-on-pick from the pool + ever-good ledger (socks4/5 only when
        asked). Uses proxy_pool.pick_verified."""
        sys.path.insert(0, str(self.gen_dir))
        try:
            import proxy_pool
            return proxy_pool.pick_verified(tries=tries, socks=socks_only)
        except Exception as e:
            log.warning("proxy_pool.pick_verified failed: %s", e)
        return None

    # ---- workers ------------------------------------------------------------

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

    def _worker_env(self, name: str) -> dict[str, str]:
        env = {
            "ZAI_OUT_SUFFIX": name,
            "ZAI_OUTPUT_DIR": str(self.data_dir),
            "ZAI_PROXY": "0", "ZAI_USE_POOL": "0", "ZAI_SOCKS_ONLY": "0",
            "ZAI_MANUAL": "1" if self.captcha == "manual" else "0",
            "ZAI_HEADLESS": "0" if self.captcha == "manual" else ("1" if self.headless else "0"),
            "ZAI_PROXY_RETRIES": str(self.retries),
        }
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
        if self.ip_source == "socks":
            env["ZAI_USE_POOL"] = "1"
            env["ZAI_SOCKS_ONLY"] = "1"
        elif self.ip_source == "pool":
            env["ZAI_USE_POOL"] = "1"
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

    def status(self) -> dict:
        return {
            "options": self.options(),
            "workers": [w.to_public() for w in self.workers.values()],
            "alive": sum(1 for w in self.workers.values() if w.alive()),
        }
