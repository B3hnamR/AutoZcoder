"""Bundled exit networks (Psiphon, Surfshark) shared plumbing.

Psiphon runs its own client (`psiphon-tunnel-core`) once per chosen region;
Surfshark runs `wireproxy` once per chosen location. Either way every exit is a
local SOCKS5 port, and the generator sends a signup through one of them, so a
different region means a different exit address.

The pool here owns the whole lifecycle: free local ports, one supervised client
per region, restart-with-backoff after a crash, the settings file, and the
status shape the dashboard reads. Subclasses provide the binary, the config and
the client's own output handling.
"""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .paths import app_dir

REGION_CODE = re.compile(r"^[A-Z]{2}$")

# A client that keeps failing is left stopped instead of spinning.
MAX_RESTARTS = 5
HEALTH_PROBE_INTERVAL = 30
_SETTINGS_WRITE_LOCK = threading.Lock()


def bundle_dirs(kind: str) -> list[Path]:
    """Every place a bundled client folder may live, most specific first."""
    env = os.environ.get(f"ZCODE_{kind.upper()}_DIR", "").strip()
    meipass = getattr(sys, "_MEIPASS", None)
    dirs: list[Path] = []
    if env:
        dirs.append(Path(env))
    if meipass:
        dirs += [Path(meipass) / kind, Path(meipass) / "vendor" / kind]
    here = app_dir()
    dirs += [here / kind, here / "vendor" / kind,
             Path(__file__).resolve().parent.parent / "vendor" / kind]
    return dirs


def executable(path: Path) -> bool:
    """True when the client really runs here (POSIX needs the execute bit)."""
    try:
        if not path.is_file():
            return False
        if os.name != "nt" and not os.access(path, os.X_OK):
            try:
                os.chmod(path, 0o755)
            except OSError:
                return False
        return True
    except OSError:
        return False


def free_port(base: int, avoid: set[int]) -> int:
    """First free loopback port at or above `base` (skips the ones in use)."""
    for port in range(base, min(65535, base + 300)):
        if port in avoid:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free local port was found from {base} up")


def trace_exit(port: int, timeout: float = 20.0) -> dict[str, str] | None:
    """Ask Cloudflare's trace page which address leaves through this SOCKS port."""
    try:
        import requests
    except Exception:
        return None
    proxy = f"socks5h://127.0.0.1:{port}"
    try:
        response = requests.get(
            "https://1.1.1.1/cdn-cgi/trace", timeout=timeout,
            proxies={"http": proxy, "https": proxy},
            headers={"User-Agent": "curl/8.0"})
        if response.status_code != 200:
            return None
        values = {}
        for line in response.text.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        if not values.get("ip"):
            return None
        return {"ip": values["ip"], "loc": values.get("loc", "")}
    except Exception:
        return None


def usable_proxy(ip: str) -> str:
    return f"socks5://127.0.0.1:{ip}"


@dataclass
class Exit:
    region: str
    port: int
    state: str = "starting"          # starting|ready|reconnecting|failed|stopped
    exit_ip: str = ""
    location: str = ""
    error: str = ""
    since: float = 0.0
    restarts: int = 0
    proc: subprocess.Popen | None = None
    thread: threading.Thread | None = None
    stop_requested: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict:
        return {"region": self.region, "state": self.state, "port": self.port,
                "exitIp": self.exit_ip, "location": self.location,
                "since": self.since, "error": self.error}


class ExitPool:
    """One supervised client per chosen region, together one IP source."""

    kind = ""
    label = ""
    base_port = 47000
    defaults: dict[str, Any] = {}

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir) / "vpn" / self.kind
        self._settings_path = Path(data_dir) / "vpn.json"
        self._lock = threading.RLock()
        self._instances: dict[str, Exit] = {}
        self._rr = 0
        self._busy = ""
        self.last_error = ""
        self._closed = False
        self._settings = self._load_settings()
        self._spawn_error = ""

    # ---- settings -------------------------------------------------------
    def _load_settings(self) -> dict:
        raw: dict[str, Any] = {}
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get(self.kind), dict):
                raw = data[self.kind]
        except (OSError, ValueError):
            raw = {}
        return self.normalize({**self.defaults, **raw})

    def _store(self) -> None:
        with _SETTINGS_WRITE_LOCK:
            self._store_locked()

    def _store_locked(self) -> None:
        data: dict[str, Any] = {}
        try:
            current = json.loads(self._settings_path.read_text(encoding="utf-8"))
            if isinstance(current, dict):
                data = current
        except (OSError, ValueError):
            data = {}
        data[self.kind] = self._settings
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self._settings_path.parent, prefix="vpn-", suffix=".tmp")
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=1)
            os.replace(tmp, self._settings_path)
        finally:
            tmp.unlink(missing_ok=True)

    def normalize(self, config: dict) -> dict:
        return config

    @property
    def config(self) -> dict:
        with self._lock:
            return dict(self._settings)

    def public_config(self) -> dict:
        return self.config

    # ---- client lookup --------------------------------------------------
    def binary_name(self) -> str:
        raise NotImplementedError

    def find_client(self) -> Path | None:
        name = self.binary_name()
        if not name:
            return None
        for folder in bundle_dirs(self.kind):
            candidate = folder / name
            if executable(candidate):
                return candidate
        return None

    def missing_message(self) -> str:
        raise NotImplementedError

    @property
    def installed(self) -> bool:
        return self.find_client() is not None

    # ---- lifecycle ------------------------------------------------------
    def _spawn(self, inst: Exit) -> subprocess.Popen:
        raise NotImplementedError

    def _on_line(self, inst: Exit, line: str) -> None:
        raise NotImplementedError

    def _fresh(self, inst: Exit) -> None:
        """A new address start: forget the region's saved connection."""

    def _prepare(self, inst: Exit) -> None:
        """Called after a port is chosen, before spawning."""

    def _cleanup(self, inst: Exit) -> None:
        """Remove temporary files owned by this connection attempt."""

    def _mark_ready(self, inst: Exit) -> None:
        if inst.state == "ready":
            return
        inst.state = "ready"
        inst.since = time.time()
        inst.error = ""
        inst.restarts = 0
        self._learn_exit(inst)

    def _learn_exit(self, inst: Exit) -> None:
        """Trace the exit address in the background; the SOCKS port can take a
        moment to answer after the client reports it is connected."""
        with self._lock:
            if inst.extra.get("tracing") or inst.stop_requested or inst.proc is None:
                return
            inst.extra["tracing"] = True
            inst.extra["last_probe"] = time.monotonic()

        def work() -> None:
            try:
                for attempt in range(4):
                    if inst.stop_requested or inst.proc.poll() is not None:
                        return
                    info = trace_exit(inst.port)
                    with self._lock:
                        if inst.stop_requested or self._instances.get(inst.region) is not inst:
                            return
                        if info and (inst.state == "ready" or self.kind == "surfshark"):
                            if inst.state == "reconnecting":
                                self._mark_ready(inst)
                            inst.exit_ip = info["ip"]
                            inst.location = info.get("loc", "")
                            return
                        if not info and attempt == 3 and self.kind == "surfshark" and inst.state == "ready":
                            inst.state = "reconnecting"
                            inst.exit_ip = inst.location = ""
                            inst.error = "The tunnel did not answer its connection check"
                    if attempt < 3:
                        time.sleep(3)
            finally:
                with self._lock:
                    inst.extra["last_probe"] = time.monotonic()
                    inst.extra["tracing"] = False

        threading.Thread(target=work, daemon=True, name=f"{self.kind}-trace").start()

    def _used_ports(self) -> set[int]:
        with self._lock:
            return {i.port for i in self._instances.values()}

    def _start(self, region: str, fresh: bool = False) -> Exit:
        with self._lock:
            if self._closed or not self._settings.get("enabled", True):
                raise RuntimeError(f"{self.label} is stopped")
            old = self._instances.get(region)
            if old is not None:
                old.stop_requested = True
                self._terminate(old)
            avoid = {i.port for i in self._instances.values() if i is not old}
            if old is not None and old.port and not fresh:
                port = old.port
            else:
                port = free_port(self.base_port, avoid)
            inst = Exit(region=region, port=port, restarts=old.restarts if old else 0)
            self._instances[region] = inst
            try:
                self._prepare(inst)
                if fresh:
                    self._fresh(inst)
            except Exception:
                self._instances.pop(region, None)
                self._cleanup(inst)
                raise
            thread = threading.Thread(target=self._run, args=(inst,), daemon=True,
                                      name=f"{self.kind}-{region}")
            inst.thread = thread
            thread.start()
            return inst

    def _creationflags(self) -> int:
        if os.name == "nt":
            return getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return 0

    def _run(self, inst: Exit) -> None:
        with self._lock:
            if self._closed or inst.stop_requested or self._instances.get(inst.region) is not inst:
                self._cleanup(inst)
                return
            try:
                proc = inst.proc = self._spawn(inst)
            except Exception as exc:
                inst.state = "failed"
                inst.error = str(exc)
                self._spawn_error = str(exc)
                self._cleanup(inst)
                return
        try:
            stream = proc.stdout
            if stream is not None:
                for raw in stream:
                    line = raw.decode("utf-8", "replace").rstrip()
                    try:
                        self._on_line(inst, line)
                    except Exception:
                        pass
        finally:
            try:
                code = proc.wait(timeout=5)
            except Exception:
                code = -1
                self._terminate(inst)
            self._cleanup(inst)
        with self._lock:
            if inst.stop_requested or self._closed:
                inst.state = "stopped"
                return
            inst.state = "failed"
            inst.error = inst.error or f"The {self.label} client stopped (code {code})"
            if not self._settings.get("enabled", True) or inst.restarts >= MAX_RESTARTS:
                return
            inst.restarts += 1
            delay = min(120, 5 * 2 ** (inst.restarts - 1))
        time.sleep(delay)
        if self._closed:
            return
        with self._lock:
            if inst.stop_requested or self._instances.get(inst.region) is not inst:
                return
            try:
                self._start(inst.region)
            except Exception:
                pass

    def _terminate(self, inst: Exit) -> None:
        proc = inst.proc
        try:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            self._cleanup(inst)

    # ---- actions --------------------------------------------------------
    def connect(self) -> dict:
        with self._lock:
            if not self._settings.get("enabled", True):
                raise RuntimeError(f"{self.label} is turned off. Turn it on first.")
            if not self.find_client():
                raise RuntimeError(self.missing_message())
            regions = self._settings.get("regions") or []
            if not regions:
                raise RuntimeError("Choose at least one region.")
            self._busy = "connecting"
            self.last_error = ""
            try:
                for region in list(self._instances):
                    if region not in regions:
                        inst = self._instances.pop(region)
                        inst.stop_requested = True
                        self._terminate(inst)
                pending = []
                for region in regions:
                    inst = self._instances.get(region)
                    if inst is None or inst.state in ("failed", "stopped"):
                        if inst is not None:
                            inst.restarts = 0
                        pending.append(region)
                for region in pending:
                    self._start(region)
            except Exception as exc:
                self.last_error = str(exc)
                raise
            finally:
                self._busy = ""
        return self.status()

    def disconnect(self) -> dict:
        with self._lock:
            self._busy = "disconnecting"
        try:
            self.stop_all()
        finally:
            with self._lock:
                self._busy = ""
        return self.status()

    def stop_all(self) -> None:
        with self._lock:
            live = list(self._instances.values())
            self._instances.clear()
            for inst in live:
                inst.stop_requested = True
                self._terminate(inst)

    def renew(self, region: str) -> dict:
        with self._lock:
            if region not in (self._settings.get("regions") or []):
                raise RuntimeError(f"{region} is not one of the chosen regions")
            if region not in self._instances:
                raise RuntimeError(f"{self.label} {region} is not running. Connect it first.")
            self._start(region, fresh=True)
        return self.status()

    def set_settings(self, patch: dict) -> dict:
        with self._lock:
            merged = {**self._settings, **{k: v for k, v in patch.items() if k in self.defaults}}
            self._settings = self.normalize(merged)
            self._store()
            enabled = self._settings.get("enabled", True)
            regions = self._settings.get("regions") or []
            if not enabled:
                self.stop_all()
            elif self._instances:
                for region in list(self._instances):
                    if region not in regions:
                        inst = self._instances.pop(region)
                        inst.stop_requested = True
                        self._terminate(inst)
                for region in regions:
                    if region not in self._instances:
                        try:
                            self._start(region)
                        except Exception:
                            pass
        return self.status()

    def autostart(self) -> None:
        cfg = self.config
        if cfg.get("enabled", True) and cfg.get("autoStart"):
            try:
                self.connect()
            except Exception as exc:
                self.last_error = str(exc)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        self.stop_all()

    # ---- picking an exit for a signup -----------------------------------
    def candidates(self) -> list[Exit]:
        cfg = self.config
        regions = (cfg.get("regions") or []) if cfg.get("mode") != "single" else \
            [cfg.get("pinned") or (cfg.get("regions") or [""])[0]]
        with self._lock:
            return [i for i in (self._instances.get(r) for r in regions if r) if i is not None]

    def ready(self) -> list[Exit]:
        return [i for i in self.candidates() if i.state == "ready"]

    def live(self) -> int:
        """Connected exits, whether or not the current mode can pick them."""
        with self._lock:
            return sum(1 for i in self._instances.values() if i.state == "ready")

    def pick(self, tried: set[str] | None = None) -> Exit | None:
        tried = tried or set()
        live = [i for i in self.ready() if i.region not in tried]
        if not live:
            return None
        cfg = self.config
        if cfg.get("mode") == "single":
            return live[0]
        with self._lock:
            chosen = live[self._rr % len(live)]
            self._rr += 1
        return chosen

    def unavailable(self) -> str:
        with self._lock:
            if not self._settings.get("enabled", True):
                return f"{self.label} is turned off. Turn it on in Generator → IP source."
            if not self._instances:
                return f"{self.label} is not connected. Press Connect under Generator → IP source."
            if not self.ready():
                states = ", ".join(f"{r}: {i.state}" for r, i in self._instances.items())
                return f"No {self.label} region is connected yet ({states})."
        return f"{self.label} has no ready exit right now."

    # ---- status ---------------------------------------------------------
    def status(self) -> dict:
        cfg = self.config
        found = self.find_client()
        with self._lock:
            instances = list(self._instances.values())
            busy = self._busy
        for inst in instances:
            if ((inst.state == "ready" and not inst.exit_ip)
                    or (self.kind == "surfshark" and inst.state in ("ready", "reconnecting"))) \
                    and time.monotonic() - inst.extra.get("last_probe", 0) >= HEALTH_PROBE_INTERVAL:
                self._learn_exit(inst)
        chosen = cfg.get("regions") or []
        by_region = {i.region: i for i in instances}
        # Chosen regions that are not running still get a row, so the panel can
        # show what happens to them (PolyGate's shape).
        rows = []
        for region in chosen:
            inst = by_region.pop(region, None)
            rows.append(inst.snapshot() if inst else
                        {"region": region, "state": "stopped", "port": 0, "exitIp": "",
                         "location": "", "since": 0, "error": ""})
        rows += [i.snapshot() for i in by_region.values()]
        return {
            "installed": bool(found),
            "problem": "" if found else self.missing_message(),
            "platform": f"{sys.platform}-{platform.machine()}",
            "busy": busy,
            "lastError": self.last_error,
            "running": bool(instances),
            "live": sum(1 for i in instances if i.state == "ready"),
            "config": self.public_config(),
            "regions": rows,
        }
