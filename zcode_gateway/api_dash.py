"""Dashboard-serving + the read/write admin surface the web UI needs.

Everything here is additive to the existing admin API. Settings writes go
through a whitelist of allowed keys — the config holds credentials and paths,
so free-form key editing is a footgun.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import tempfile
import time
import math
from functools import wraps
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
import yaml

from .deps import admin_key, get_state, internal_or_admin
from .logbuffer import LogBuffer

log = logging.getLogger("zcode_gateway.dash")

router = APIRouter(tags=["dashboard"])

def _dash_dir() -> Path:
    """Bundle-aware: PyInstaller unpacks data to sys._MEIPASS."""
    import sys
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "zcode_gateway" / "dashboard"


DASH_DIR = _dash_dir()

# Settings the dashboard may edit (dotted config paths). Anything not listed
# is read-only from the UI — keeps credentials and upstream URLs out of it.
_EDITABLE = {
    "server.host": str, "server.port": int,
    "pool.strategy": str, "pool.min_balance_micro": int,
    "pool.refresh_lead_seconds": int, "pool.cooldown_seconds": int,
    "pool.max_in_flight_per_account": int, "pool.acquire_wait_seconds": float,
    "pool.balance_poll_seconds": int, "pool.blocked_model_cooldown_seconds": int,
    "upstream.timeout_connect": float, "upstream.timeout_read": float,
    "upstream.max_attempts": int, "upstream.anthropic_cache_control": bool,
    "upstream.overload_max_attempts": int,
    "upstream.overload_base_delay_seconds": float, "upstream.overload_max_delay_seconds": float,
    "accounts.source": str, "accounts.dir": str, "accounts.pool_file": str,
    "logging.level": str, "logging.capture": bool,
    "models.default": str, "models.default_anthropic": str,
    "models.auto_free_fallback.enabled": bool,
    "models.default_effort": str,
    "models.pacing.enabled": bool,
    "models.pacing.min_gap_ms": int,
    "models.pacing.max_gap_ms": int,
    "update.enabled": bool, "update.interval_hours": float,
}


def _config_transaction(endpoint):
    @wraps(endpoint)
    async def serialized(request: Request, *args, **kwargs):
        state = get_state(request)
        lock = getattr(state, "_config_write_lock", None)
        if lock is None:
            lock = state._config_write_lock = asyncio.Lock()
        async with lock:
            return await endpoint(request, *args, **kwargs)
    return serialized


# Settings whose value is one of a fixed list: (value, label, description).
# The dashboard renders these as dropdowns, and PUT rejects anything else (a
# typo in config.yaml used to surface only at the next boot).
_CHOICES: dict[str, list[tuple[str, str, str]]] = {
    "server.host": [
        ("127.0.0.1", "This computer only", "Safest: only programs on this PC can reach the gateway"),
        ("localhost", "This computer only (by name)", "Same as above, using the name localhost"),
        ("0.0.0.0", "Everyone on my network", "Other devices can connect too; keep your client key secret"),
    ],
    "accounts.source": [
        ("accounts_dir", "Account folder", "One snapshot .txt per account in the accounts folder (recommended)"),
        ("providers_json", "Zcode desktop app", "Use the logins of the installed Zcode app"),
        ("pool_file", "Pool file", "Use a saved pool.json"),
    ],
    "pool.strategy": [
        ("drain_first", "Drain one account at a time",
         "Use the account closest to running out first: fewest accounts in play, least likely to be flagged"),
        ("least_in_flight", "Least busy account", "Send each request to the account running the fewest requests"),
        ("round_robin", "Take turns", "Rotate through the accounts in order"),
        ("least_recently_used", "Longest idle first", "Pick the account that has rested the longest"),
        ("quota_aware", "Most quota left", "Prefer the account with the most remaining quota for that model"),
    ],
    "models.default_effort": [
        ("low", "Low", "Fastest answers, least reasoning"),
        ("high", "High", "Balanced (recommended)"),
        ("max", "Max", "Deepest reasoning, slowest"),
    ],
    "logging.level": [
        ("DEBUG", "Debug", "Everything, very noisy"),
        ("INFO", "Info", "Normal activity (recommended)"),
        ("WARNING", "Warnings", "Only problems"),
        ("ERROR", "Errors", "Only failures"),
    ],
}


def _read_config_raw(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write_yaml(path: Path, data: dict) -> None:
    """Write config.yaml atomically (tmp + os.replace): a crash or full disk
    mid-write must never truncate the live config."""
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump(data, stream, sort_keys=False, allow_unicode=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _config_path(state) -> Path | None:
    """The exact config file the process loaded; None when not derivable."""
    file = getattr(state.cfg, "_config_file", None)
    if file:
        return Path(file)
    # manual Config objects without a recorded file: fall back to the
    # folder-based guess
    root = getattr(state.cfg, "_root", None)
    if root:
        return Path(root) / "config.yaml"
    p = Path("config.yaml")
    return p if p.is_file() else None


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _request_is_loopback(request: Request) -> bool:
    """True when the browser reached us under a loopback name.

    CORS does not protect /dash from DNS rebinding: an attacker hostname that
    resolves to 127.0.0.1 is same-origin in the browser, so the request's Host
    is the only tell. Never hand the admin key to a foreign Host.
    """
    return (request.url.hostname or "").strip().lower() in _LOOPBACK_HOSTS


def _peer_is_loopback(request: Request) -> bool:
    """The TCP peer must ALSO be loopback before the admin key is injected.

    Host-header checks alone are spoofable by a local forwarder that preserves
    the Host (e.g. `ssh -R 8788:127.0.0.1:8788`): the remote user's request
    arrives with Host: 127.0.0.1 from a non-loopback peer.
    """
    peer = request.client.host if request.client else ""
    return peer in _LOOPBACK_HOSTS


def _inject_admin_key(html: str, request: Request) -> str:
    """First-launch convenience: hand the page its own admin key so the login
    screen does not gate the local tool (single-user, single-machine — the
    Server tab displays the same key anyway). Both the bind address and the
    request's own Host must be loopback: a DNS-rebinding page is same-origin
    and would otherwise read the key straight out of this response.
    """
    try:
        cfg = get_state(request).cfg
        if ((cfg.server.host or "").strip() in _LOOPBACK_HOSTS
                and _request_is_loopback(request)
                and _peer_is_loopback(request)):
            marker = 'sessionStorage.getItem("gw-admin-key") || ""'
            injected = ('sessionStorage.getItem("gw-admin-key") || '
                        + json.dumps(cfg.server.admin_key))
            html = html.replace(marker, injected, 1)
    except Exception:
        pass
    return html


@router.get("/dash", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    index = DASH_DIR / "index.html"
    if not index.is_file():
        return HTMLResponse("dashboard not bundled", status_code=404)
    return HTMLResponse(_inject_admin_key(index.read_text(encoding="utf-8"), request))


@router.get("/dash/next", response_class=HTMLResponse)
def dashboard_next(request: Request) -> HTMLResponse:
    """Compatibility URL for the canonical index.html shell served at /dash.

    Its CSS/JS live under DASH_DIR/assets and are mounted as static files; only
    this shell goes through the admin-key injection.
    """
    index = DASH_DIR / "index.html"
    if not index.is_file():
        return HTMLResponse("dashboard not bundled", status_code=404)
    return HTMLResponse(_inject_admin_key(index.read_text(encoding="utf-8"), request))


def _model_ids(state) -> list[str]:
    """Model names a default can be set to (the real models first, aliases after)."""
    try:
        return [str(m["id"]) for m in state.registry.catalogue() if m.get("id")]
    except Exception:
        return []


@router.get("/admin/dash/capabilities")
async def dash_capabilities(request: Request, key: str = Depends(admin_key)) -> dict:
    """What this host can actually do, so the UI hides what would only fail.

    The dashboard must not guess from the browser's user agent: the browser can
    sit on Windows while the gateway runs on a headless Linux VPS.
    """
    frozen = bool(getattr(sys, "frozen", False))
    windows = os.name == "nt"
    linux = sys.platform.startswith("linux")
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    xvfb = None
    if linux:
        import shutil
        xvfb = bool(shutil.which("Xvfb"))

    # The desktop Zcode app only stores credentials here on a workstation; a
    # server install has no such file and the import button would always 404.
    try:
        from .paths import _zcode_providers
        zcode_app_present = _zcode_providers().is_file()
    except Exception:
        zcode_app_present = False

    return {
        "os": "windows" if windows else ("linux" if linux else sys.platform),
        "frozen": frozen,
        "headless": linux and not has_display,
        "features": {
            # Swapping the running binary is implemented for the Windows exe
            # only; on Linux the installer/package replaces it.
            "self_update": windows and frozen,
            # Reads the desktop app's credential store.
            "import_from_app": zcode_app_present,
            # Killing the process from the UI makes sense for the windowed exe;
            # under systemd it would just look like a crash and get restarted.
            "shutdown": windows and frozen,
            # A visible browser needs a real display, or Xvfb to fake one.
            "visible_browser": windows or has_display,
            "manual_captcha": windows or has_display,
            "xvfb": bool(xvfb),
        },
        "notes": {
            "self_update": "" if (windows and frozen) else
                ("Update by replacing the binary, or run `autozcoder update`."
                 if linux else "Self-update is only available in the packaged app."),
            "import_from_app": "" if zcode_app_present else
                "No Zcode desktop app credentials found on this machine.",
            "shutdown": "" if (windows and frozen) else
                ("Managed by systemd — use `systemctl stop zcodegateway`."
                 if linux else "Stop the gateway from the terminal that started it."),
            "visible_browser": "" if (windows or has_display) else
                ("This host has no display. Headless solving is used instead"
                 + (" (Xvfb is available for headed runs)." if xvfb else "; install Xvfb for headed runs.")),
        },
    }


@router.get("/admin/dash/settings")
async def dash_settings(request: Request, key: str = Depends(admin_key)) -> dict:
    """Current settings + the raw editable values, plus server-side keys."""
    state = get_state(request)
    cfg = state.cfg
    cfg_path = _config_path(state)
    raw = _read_config_raw(cfg_path) if cfg_path else {}

    def _flat(path: str, default: Any = None) -> Any:
        cur: Any = raw
        for part in path.split("."):
            if not isinstance(cur, dict):
                return default
            cur = cur.get(part)
        return default if cur is None else cur

    return {
        "config_path": str(cfg_path) if cfg_path else "",
        "values": {k: _flat(k) for k in sorted(_EDITABLE)},
        "effective": {
            "server": cfg.server.model_dump(exclude={"client_keys", "admin_key"}),
            "pool": cfg.pool.model_dump(),
            "upstream": cfg.upstream.model_dump(exclude={"fingerprint"}),
            "accounts": cfg.accounts.model_dump(),
            "logging": cfg.logging.model_dump(),
            "models": cfg.models.model_dump(),
            "update": cfg.update.model_dump(),
        },
        "admin_key": cfg.server.admin_key,
        "client_keys": [k.model_dump() for k in cfg.server.client_keys],
        "accounts_source": cfg.accounts.source,
        "choices": {path: [{"value": v, "label": lab, "desc": desc} for v, lab, desc in opts]
                    for path, opts in _CHOICES.items()},
        "models": _model_ids(state),
    }


@router.put("/admin/dash/settings")
@_config_transaction
async def dash_settings_put(request: Request, body: dict = Body(...),
                            key: str = Depends(admin_key)) -> dict:
    """Write whitelisted settings keys back to config.yaml. A restart is
    required for most of them to take effect (pool/tokens are in memory)."""
    state = get_state(request)
    cfg_path = _config_path(state)
    if cfg_path is None:
        raise HTTPException(status_code=400, detail="no writable config.yaml found")

    values = body.get("values")
    if not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="body must be {\"values\": {...}}")

    unknown = sorted(set(values) - set(_EDITABLE))
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"not editable: {', '.join(unknown)}")

    raw = _read_config_raw(cfg_path)
    if not raw:
        # An empty parse means unreadable/corrupt/missing — writing `raw`
        # back would replace the user's whole config.yaml with only the
        # edited key (next boot: default admin key, "accounts disappear").
        # Refuse and tell the user to fix the file first.
        raise HTTPException(
            status_code=409,
            detail="config.yaml is missing or unparsable — fix or restore it "
                   "before saving settings (saving now would wipe every "
                   "other key)")
    changed: list[str] = []
    for dotted, val in values.items():
        cast = _EDITABLE[dotted]
        try:
            if cast is bool and isinstance(val, str):
                if val.lower() not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
                    raise ValueError()
                val = val.lower() in ("1", "true", "yes", "on")
            else:
                val = cast(val)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail=f"{dotted}: cannot cast {val!r}") from None
        # range validation: a 0 or negative written straight into config.yaml
        # can deadlock the pool or break startup (pydantic ge=1 catches it
        # only at next boot — fail here instead)
        if cast in (int, float) and (val < 0 or not math.isfinite(val)):
            raise HTTPException(status_code=400,
                                detail=f"{dotted}: must be >= 0")
        if dotted == "models.default_effort" and val not in ("low", "high", "max"):
            raise HTTPException(status_code=400,
                                detail="models.default_effort must be low, high, or max")
        allowed = _CHOICES.get(dotted)
        if allowed and dotted != "server.host":     # host may be any name/address
            probe = val.upper() if dotted == "logging.level" else val
            if probe not in [c[0] for c in allowed]:
                raise HTTPException(status_code=400,
                                    detail=f"{dotted} must be one of: "
                                           f"{', '.join(c[0] for c in allowed)}")
            val = probe
        if dotted in ("models.default", "models.default_anthropic") and not str(val).strip():
            raise HTTPException(status_code=400, detail=f"{dotted} cannot be empty")
        if dotted == "pool.max_in_flight_per_account" and val < 1:
            raise HTTPException(status_code=400,
                                detail="pool.max_in_flight_per_account must be >= 1")
        if dotted == "pool.balance_poll_seconds" and val < 10:
            raise HTTPException(status_code=400,
                                detail="pool.balance_poll_seconds must be >= 10 "
                                       "(matches the startup validation; a lower "
                                       "value would refuse to boot next restart)")
        if dotted == "server.port" and not (0 <= val <= 65535):
            raise HTTPException(status_code=400,
                                detail="server.port must be 0-65535")
        if dotted == "server.port" and val == 0:
            raise HTTPException(status_code=400,
                                detail="server.port 0 (random) is not supported by the dashboard")
        cur = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            # a scalar config.yaml parent (e.g. `port: 8788` then "port.x")
            # must not 500 the dashboard
            node = cur.get(part)
            if not isinstance(node, dict):
                node = {}
                cur[part] = node
            cur = node
        if cur.get(parts[-1]) != val:
            cur[parts[-1]] = val
            changed.append(dotted)

    # Use the same full schema as startup before committing any changed file.
    # Report field names/reasons only: ValidationError strings can echo secrets.
    try:
        type(state.cfg).model_validate(raw)
    except ValueError as exc:
        errors = exc.errors(include_input=False) if hasattr(exc, "errors") else []
        detail = "; ".join(".".join(map(str, error["loc"])) + ": " + error["msg"] for error in errors)
        raise HTTPException(400, "Configuration validation failed: " + (detail or "invalid setting")) from None
    if changed:
        await asyncio.to_thread(_atomic_write_yaml, cfg_path, raw)
    return {"changed": changed, "restart_required": bool(changed)}


@router.post("/admin/dash/import")
async def dash_import(request: Request, key: str = Depends(admin_key)) -> dict:
    """Snapshot every logged-in Zcode account into the accounts folder."""
    state = get_state(request)
    try:
        # Helpers live in paths.py (tkinter-free) — importing them directly
        # works from source AND from a packaged exe (gui.py only exists in the
        # compiled PYZ there, so reading its source off disk always failed).
        from .paths import _zcode_providers
        from .zcode_accounts import import_credentials
    except Exception as exc:  # pragma: no cover - headless build
        raise HTTPException(status_code=500,
                            detail=f"import helper unavailable: {exc}") from exc

    def _import():
        return import_credentials(_zcode_providers(), out_dir)

    try:
        out_dir = Path(state.cfg.accounts.dir)
        # file scanning + snapshot writes; keep them off the event loop
        result = await asyncio.to_thread(_import)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        log.warning("zcode import failed: %s", exc)
        raise HTTPException(status_code=500,
                            detail=f"import failed ({exc.__class__.__name__})") from exc
    return result


# ---- login with Zcode (CLI OAuth broker) -------------------------------- #

def _device_login(request: Request):
    """The in-flight DeviceLogin for this app state, if any."""
    return getattr(get_state(request), "device_login", None)


@router.post("/admin/dash/auth/login/start")
async def login_start(request: Request, key: str = Depends(admin_key)) -> dict:
    """Begin a device-code login: returns the code + URL for the user."""
    state = get_state(request)
    if state.cfg.accounts.source != "accounts_dir":
        raise HTTPException(status_code=400,
                            detail="login writes account snapshots; set "
                                   "accounts.source=accounts_dir first")
    existing = _device_login(request)
    if existing and ((getattr(existing, "_task", None) and not existing._task.done())
                     or existing.state in ("awaiting_user", "registering")):
        return existing.status()          # one flow at a time; idempotent start
    from .device_auth import DeviceLogin

    async def _add_and_probe(account):
        """New account: join the pool, then settle balance/paid-lane/plan now —
        a login that replaced an exhausted account must not read 'available'."""
        await state.pool.upsert(account)
        try:
            canonical = await state.pool.find(account.id)
            if canonical is not None:
                await state.tokens.check_account(canonical)
        except Exception:
            log.warning("post-login account check failed for %s", account.id)

    login = DeviceLogin(Path(state.cfg.accounts.dir),
                        on_account=_add_and_probe, base_url=state.cfg.upstream.base_url,
                        fingerprint=state.cfg.upstream.fingerprint)
    state.device_login = login
    task = asyncio.create_task(login.run(), name="device-login")
    login._task = task
    # wait (briefly) for the code so the UI can show it immediately
    try:
        await asyncio.wait_for(login._ready.wait(), timeout=20)
    except asyncio.TimeoutError:
        pass
    return login.status()


@router.get("/admin/dash/auth/login/status")
async def login_status(request: Request, key: str = Depends(admin_key)) -> dict:
    login = _device_login(request)
    if login is None:
        return {"state": "idle"}
    status = login.status()
    # a finished login has already been persisted + pooled inside run();
    # surface the resulting account id so the UI can refresh the table
    if login.state == "done" and login.snapshot_path is not None:
        status["snapshot"] = login.snapshot_path.name
    return status


@router.post("/admin/dash/auth/login/cancel")
async def login_cancel(request: Request, key: str = Depends(admin_key)) -> dict:
    login = _device_login(request)
    if login is None:
        return {"state": "idle"}
    login.cancel()
    task = getattr(login, "_task", None)
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
    return login.status()


@router.get("/admin/dash/zcode-status")
async def dash_zcode_status(request: Request, key: str = Depends(admin_key)) -> dict:
    """Installed/running/logged-in status of the desktop Zcode app."""
    get_state(request)
    from .zcode_detect import detect_zcode
    # tasklist + registry reads can block for seconds; never on the event loop
    status = await asyncio.to_thread(detect_zcode)
    chip, kind = status.as_chip()
    return {
        "installed": status.installed,
        "running": status.running,
        "logged_in": status.logged_in,
        "install_path": status.install_path,
        "version": status.version,
        "email": status.email,
        "account_id": status.account_id,
        "expires_at_ms": status.expires_at_ms,
        "token_valid": status.token_valid,
        "detail": chip,
        "chip_kind": kind,
    }


@router.post("/admin/dash/shutdown")
async def dash_shutdown(request: Request, key: str = Depends(admin_key)) -> dict:
    """Stop the tool: answer, then exit the process a moment later.

    There is no other way to stop the exe build (no tray, no stop path, and
    closing the WebView2 window leaves the process — and its port and
    single-instance mutex — alive on non-daemon WebView2 threads). A hard
    exit is deliberate: uvicorn's should_exit would free the port but leave
    the windowless process running, which is exactly the bug this fixes.
    """
    state = get_state(request)
    def finish():
        manager = getattr(state, "generator", None)
        if manager is not None:
            _scheduler = getattr(state, "claim_scheduler", None)
            if _scheduler is not None:
                _scheduler.stop()
            manager.shutdown()
        os._exit(0)
    threading.Timer(1.0, finish).start()
    return {"stopping": True,
            "note": "the gateway process is exiting; you can close this window"}


@router.get("/admin/dash/update/status")
async def update_status(request: Request, key: str = Depends(admin_key)) -> dict:
    """Cached update check result (never hits GitHub from this route)."""
    state = get_state(request)
    updater = getattr(state, "updater", None)
    if updater is None:
        return {"update_available": False, "checked_at": None}
    return {**updater.status, "updater_phase": getattr(state, "update_phase", "idle"),
            "downloaded": getattr(state, "update_phase", "idle") == "ready"}


@router.post("/admin/dash/update/check")
async def update_check(request: Request, key: str = Depends(admin_key)) -> dict:
    """Force a fresh check now (one GitHub round-trip)."""
    state = get_state(request)
    updater = getattr(state, "updater", None)
    if updater is None:
        raise HTTPException(status_code=503, detail="updater not running")
    return await updater.check(force=True)


@router.post("/admin/dash/update/download")
async def update_download(request: Request, key: str = Depends(admin_key)) -> dict:
    """Download the new exe next to the running one (exe builds only)."""
    state = get_state(request)
    _windows_update_only()
    _require_newer_update(state)
    phase = getattr(state, "update_phase", "idle")
    if phase in ("downloading", "applying"):
        raise HTTPException(409, "An update operation is already in progress")
    if phase == "ready":
        from . import updater as updater_mod
        staged = updater_mod.exe_dir() / updater_mod.NEW_EXE_NAME
        if staged.is_file():
            return {"downloaded": str(staged), "size": staged.stat().st_size}
    updater = getattr(state, "updater", None)
    status = updater.status if updater else {}
    url = status.get("download_url")
    if not (status.get("update_available") and url):
        raise HTTPException(status_code=400, detail="no update available")
    from . import updater as updater_mod
    state.update_phase = "downloading"
    try:
        path = await updater_mod.download_update(url, status.get("checksum_url"))
    except RuntimeError as exc:
        state.update_phase = "idle"
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        state.update_phase = "idle"
        raise HTTPException(status_code=502,
                            detail=f"download failed ({exc.__class__.__name__})") \
            from exc
    state.update_phase = "ready"
    return {"downloaded": str(path), "size": path.stat().st_size}


@router.post("/admin/dash/update/apply")
async def update_apply(request: Request, key: str = Depends(admin_key)) -> dict:
    """Swap in the downloaded exe and relaunch (exe builds only)."""
    state = get_state(request)
    _windows_update_only()
    _require_newer_update(state)
    phase = getattr(state, "update_phase", "idle")
    if phase in ("downloading", "applying"):
        raise HTTPException(409, "An update operation is already in progress")
    # Close admission before checking active work; a concurrent Start request
    # cannot slip in while the binary is being hashed/prepared off-thread.
    state.update_phase = "applying"
    try:
        await _check_update_idle(state)
    except Exception:
        state.update_phase = phase
        raise
    from . import updater as updater_mod
    if getattr(state, "update_applying", False):
        raise HTTPException(409, "An update is already being applied")
    state.update_applying = True
    try:
        await asyncio.to_thread(updater_mod.apply_update, updater_mod.exe_dir() / updater_mod.NEW_EXE_NAME,
                                config_path=getattr(state.cfg, "_config_file", None))
    except RuntimeError as exc:
        state.update_applying = False
        state.update_phase = "ready"
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        state.update_applying = False
        state.update_phase = "ready"
        raise HTTPException(500, "Could not prepare the updater; application was not stopped") from None
    # the swapper waits for this process to die; answer first, then exit
    def finish():
        manager = getattr(state, "generator", None)
        if manager is not None:
            _scheduler = getattr(state, "claim_scheduler", None)
            if _scheduler is not None:
                _scheduler.stop()
            manager.shutdown()
        os._exit(0)
    threading.Timer(1.0, finish).start()
    return {"applying": True, "note": "the gateway exits, swaps the exe, and "
                                      "relaunches itself"}


def _windows_update_only() -> None:
    if not getattr(sys, "frozen", False) or os.name != "nt":
        raise HTTPException(400, "In-app updates require the Windows exe. On Linux use sudo autozcoder update.")


def _require_newer_update(state) -> None:
    from . import __version__
    from .updater import is_newer
    checker = getattr(state, "updater", None)
    status = checker.status if checker else {}
    if not status.get("update_available") or not is_newer(status.get("latest", ""), __version__):
        raise HTTPException(409, "No newer release available; same-version updates and downgrades are blocked")


async def _check_update_idle(state) -> None:
    """Never silently discard generation or streaming work for an update."""
    pool = await state.pool.snapshot()
    if any(account.get("in_flight", 0) for account in pool.get("detail", [])):
        raise HTTPException(409, "Requests are active; wait before applying an update")
    manager = getattr(state, "generator", None)
    if manager is None:
        return
    auto = getattr(manager, "auto_generate", None)
    if auto and auto.status().get("status") in ("preparing", "running", "paused", "stopping"):
        raise HTTPException(409, "Stop Auto Generate before applying an update")
    if getattr(manager, "_claim_all_running", False):
        raise HTTPException(409, "Stop the claim batch before applying an update")
    if any(not worker.done.is_set() for worker in manager.workers.values()) or any(job.alive() for job in manager.claims.values()) or manager.checker.alive():
        raise HTTPException(409, "Generator, claim or proxy-check jobs are active; stop them before applying an update")


@router.get("/admin/dash/logs")
async def dash_logs(request: Request, limit: int = 300, level: str = "",
                    q: str = "", key: str = Depends(admin_key)) -> dict:
    """Recent log lines from the in-process ring buffer.

    `q` (optional) case-insensitively filters logger+message — the live log
    viewer's search box."""
    state = get_state(request)
    buf: LogBuffer | None = getattr(state, "log_buffer", None)
    if buf is None:
        return {"records": [], "note": "log buffer not attached"}
    records = buf.tail(limit=max(1, min(limit, 1000)), level=level)
    if q:
        needle = q.lower()
        records = [r for r in records
                   if needle in r.get("message", "").lower()
                   or needle in r.get("logger", "").lower()]
    return {"records": records}


@router.get("/admin/dash/analytics")
async def dash_analytics(request: Request, hours: int = 24,
                         key: str = Depends(admin_key)) -> dict:
    """Pro analytics: hourly series, latency percentiles, error breakdown,
    per-key usage, account burn-down. All from the existing sqlite ledger."""
    state = get_state(request)
    hours = max(1, min(hours, 168))
    window = hours * 3600
    series = await asyncio.to_thread(state.store.hourly_series, hours)
    lat = await asyncio.to_thread(state.store.latency_percentiles, window)
    errors = await asyncio.to_thread(state.store.error_breakdown, window)
    keys = await asyncio.to_thread(state.store.by_client_key, window)
    summary = await asyncio.to_thread(state.store.summary, window)
    # account burn-down: usage per account in-window joined with pool state
    snapshot = await state.pool.snapshot()
    by_acct = {a["id"]: a for a in (snapshot.get("detail") or [])}
    for k in keys:
        pass  # keys are client keys, not accounts — kept separate on purpose
    burn = []
    for row in summary.get("by_account", []):
        aid = row.get("account_id") or "?"
        pub = by_acct.get(aid) or {}
        burn.append({
            "account_id": aid,
            "email": pub.get("email", ""),
            "state": pub.get("state", ""),
            "requests": row.get("requests", 0),
            "tokens": row.get("tokens", 0),
            "expires_in_s": pub.get("expires_in_s"),
        })
    return {"hours": hours, "series": series, "latency": lat,
            "errors": errors, "by_key": keys, "summary": summary,
            "accounts": burn}


@router.get("/admin/dash/analytics/overview")
async def analytics_overview(request: Request, range: str = "24h",
                             key: str = Depends(admin_key)) -> dict:
    state = get_state(request)
    if range not in ("1h", "6h", "24h", "7d", "30d", "all"):
        raise HTTPException(status_code=400, detail="Unsupported analytics range")
    result = await asyncio.to_thread(state.store.analytics_overview, range)
    pool = await state.pool.snapshot()
    accounts = {a["id"]: a for a in pool.get("detail", [])}
    for row in result["by_account"]:
        current = accounts.get(row["account_id"], {})
        row.update(email=current.get("email", ""), state=current.get("state", ""),
                   expires_in_s=current.get("expires_in_s"))
    return result


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #

@router.get("/admin/dash/notifications")
async def dash_notifications(request: Request, since: int = 0,
                             key: str = Depends(admin_key)) -> dict:
    """Events for the bell + panel. `since` enables incremental polling."""
    state = get_state(request)
    return {"events": state.notifications.list(since_id=since),
            "state": state.notifications.unread_state()}


@router.post("/admin/dash/notifications/emit")
async def dash_notifications_emit(request: Request, body: dict = Body(...),
                                  key: str = Depends(internal_or_admin)) -> dict:
    """Emit an event from spawned generator workers (they carry the per-run
    internal token, not the admin key). Loopback-only by token semantics."""
    category = str(body.get("category") or "generator")[:40]
    message = str(body.get("message") or "")[:300]
    level = str(body.get("level") or "info")
    get_state(request).notifications.emit(category, message, level=level)
    return {"ok": True}


@router.post("/admin/dash/notifications/clear")
async def dash_notifications_clear(request: Request,
                                   key: str = Depends(admin_key)) -> dict:
    get_state(request).notifications.clear()
    return {"ok": True}


@router.get("/admin/dash/server")
async def dash_server(request: Request, key: str = Depends(admin_key)) -> dict:
    """Server + config info the dashboard header and Settings tab need."""
    state = get_state(request)
    cfg = state.cfg
    cfg_path = _config_path(state)
    return {
        "host": cfg.server.host, "port": cfg.server.port,
        "config_path": str(cfg_path) if cfg_path else "",
        "accounts_dir": cfg.accounts.dir,
        "accounts_source": cfg.accounts.source,
        "strategy": cfg.pool.strategy,
        "version": __import__("zcode_gateway").__version__,
        "endpoints": {
            "openai": "/v1/chat/completions",
            "anthropic": "/v1/messages",
            "models": "/v1/models",
        },
        "admin_key": cfg.server.admin_key,
        "client_keys": [k.model_dump() for k in cfg.server.client_keys],
        "cwd": os.getcwd(),
        "started_hint": "the gateway process; restart applies settings edits",
    }


# ---- account generator (ZCODEAUTO integration) ----------------------------- #

def _referral_needs_finish(zjwt: str) -> bool:
    """True when the invite chain is at 2/3 — bound + login done, message still
    blocked by risk control (405/3012). Those accounts can be completed later."""
    try:
        from .generator import resolve_zcodeauto_dir
        gen_dir, _checked = resolve_zcodeauto_dir("")
        if gen_dir is None:
            return False
        sys.path.insert(0, str(gen_dir))
        from invite_claim import referral_summary
        s = referral_summary(zjwt, log=lambda *a: None) or {}
        t = s.get("tasks") or {}
        return bool(t.get("referral_bind_code")) and not t.get("send_message")
    except Exception:
        return False


def _start_referral_finisher(mgr, interval_sec: int = 600) -> None:
    """Background loop: retry the pending send_message task until the 100M
    referral reward issues. Mirrors the relay's stance that 3012 is transient."""
    if getattr(mgr, "_finisher_started", False):
        return
    mgr._finisher_started = True
    stop = threading.Event()

    def _loop():
        while not stop.wait(interval_sec):
            try:
                pending = mgr.pending_referrals()
                if not pending:
                    continue
                sys.path.insert(0, str(mgr.gen_dir))
                from invite_claim import send_one_message, referral_summary
                for email, zjwt in list(pending.items()):
                    try:
                        s = referral_summary(zjwt, log=lambda *a: None) or {}
                        tasks = s.get("tasks") or {}
                        if tasks.get("send_message") or s.get("issued"):
                            mgr.remove_pending_referral(email)
                            log.info("generator: 100M referral already complete for %s", email)
                            continue
                        if tasks.get("referral_bind_code"):
                            if send_one_message(zjwt, log=lambda *a: None):
                                mgr.remove_pending_referral(email)
                                log.info("generator: 100M referral FINISHED for %s "
                                         "(message sent, reward issued)", email)
                            else:
                                log.debug("generator: referral message still blocked for %s", email)
                    except Exception:
                        log.debug("generator: referral retry failed for %s", email, exc_info=True)
            except Exception:
                log.exception("generator: referral finisher loop error")

    threading.Thread(target=_loop, name="referral-finisher", daemon=True).start()


def _find_zcodeauto(state) -> tuple[Path | None, list[Path]]:
    """Resolve the generator folder (the one holding main.py)."""
    from .generator import resolve_zcodeauto_dir
    root, checked = resolve_zcodeauto_dir(state.cfg.generator.zcodeauto_dir)
    return root, checked


@router.get("/admin/dash/generator/locate")
async def generator_locate_get(request: Request, key: str = Depends(admin_key)) -> dict:
    """Is the generator reachable, and where did we look? Drives the setup card."""
    state = get_state(request)
    root, checked = _find_zcodeauto(state)
    return {
        "found": root is not None,
        "path": str(root) if root else "",
        "root": str(root.parent) if root else "",
        "configured": state.cfg.generator.zcodeauto_dir or "",
        "enabled": bool(state.cfg.generator.enabled),
        "candidates": [str(c) for c in checked[:40]],
        "running": getattr(state, "generator", None) is not None,
    }


@router.post("/admin/dash/generator/locate")
@_config_transaction
async def generator_locate_set(request: Request, body: dict = Body(default={}),
                               key: str = Depends(admin_key)) -> dict:
    """Point the gateway at the generator (or auto-detect) and apply it live —
    no config.yaml editing, no restart."""
    state = get_state(request)
    from .generator import resolve_zcodeauto_dir, _gen_dir_under
    explicit = str(body.get("path") or "").strip()

    if explicit:
        # accept the generator folder itself, or a root that contains it
        gen = _gen_dir_under(Path(explicit))
        if gen is None:
            raise HTTPException(
                status_code=400,
                detail=f"no main.py under '{explicit}' — pick the generator "
                       f"folder (the one containing main.py + config.py)")
        root, checked = gen, [Path(explicit)]
    else:
        root, checked = _find_zcodeauto(state)
        if root is None:
            raise HTTPException(
                status_code=404,
                detail="generator not found automatically — enter its folder "
                       "path (the one containing main.py)")

    # persist + apply in memory (atomic write: tmp + replace, same shape as
    # the settings PUT — a crash mid-write used to truncate config.yaml)
    cfg_path = _config_path(state)
    if cfg_path is not None:
        raw = _read_config_raw(cfg_path)
        if not raw:
            raise HTTPException(status_code=409,
                                detail="config.yaml is missing or unparsable; restore it before locating the generator")
        gen = raw.setdefault("generator", {})
        if not isinstance(gen, dict):
            raise HTTPException(status_code=409, detail="config.yaml generator section must be an object")
        gen["zcodeauto_dir"] = str(root)
        gen["enabled"] = True
        await asyncio.to_thread(_atomic_write_yaml, cfg_path, raw)
    state.cfg.generator.zcodeauto_dir = str(root)
    state.cfg.generator.enabled = True
    # rebuild the manager so the change takes effect immediately
    old = getattr(state, "generator", None)
    if old is not None:
        try:
            old.stop_all()
        except Exception:
            pass
    state.generator = None
    return {"ok": True, "path": str(root), "found": True, "applied": "live"}


def _qualifying_start_balance(raw: dict, now: float) -> bool:
    """Require spendable quota belonging to an active current Start Plan."""
    from .quota import parse_balance
    def accepted(plan_id):
        return str(plan_id or "").startswith("zcode-v3-start-plan-") and plan_id not in (
            "zcode-v3-start-plan-1024", "zcode-v3-start-plan-500m")
    current = [plan for plan in raw.get("plans", []) if isinstance(plan, dict)
               and accepted(plan.get("plan_id")) and plan.get("status") == "active"]
    for plan in current:
        scoped = {"plans": [plan], "balances": [bucket for bucket in raw.get("balances", [])
                  if isinstance(bucket, dict) and bucket.get("user_plan_id") == plan.get("user_plan_id")
                  and plan.get("user_plan_id")]}
        # A qualification needs a real current balance, not a fallback grant.
        scoped["plans"] = [{**plan, "entitlements": []}]
        plans, quotas = parse_balance(scoped, now)
        if not plans:
            continue
        parsed = plans[0]
        if (parsed.get("starts_at") or 0) > now or (parsed.get("ends_at") and parsed["ends_at"] <= now):
            continue
        if any(float(bucket.get("remaining") or 0) > 0 and (bucket.get("starts_at") or 0) <= now
               and (not bucket.get("resets_at") or bucket["resets_at"] > now)
               for bucket in quotas.values()):
            return True
    return False


def _generator(request: Request):
    """The GeneratorManager for this app state, created lazily."""
    state = get_state(request)
    if getattr(state, "update_phase", "idle") == "applying":
        raise HTTPException(409, "Gateway is preparing an update; no new generator work can start")
    mgr = getattr(state, "generator", None)
    if mgr is None:
        try:
            child_origin, child_ca = state.cfg.server.child_connection()
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=f"invalid generator callback configuration: {exc}") from exc
        gen_dir, checked = _find_zcodeauto(state)
        if gen_dir is None:
            hint = "; ".join(str(c) for c in checked[:5])
            raise HTTPException(
                status_code=404,
                detail="generator not found — open the Generator tab and click "
                       f"'Locate generator'. Looked in: {hint}"
                       + (" …" if len(checked) > 5 else ""))
        main_loop = asyncio.get_running_loop()

        async def _settle_new_account(account_id: str) -> None:
            """A generator account just joined the pool: pull its plan and quota
            now, instead of showing an empty row until the next balance poll."""
            try:
                pooled = await state.pool.find(account_id)
                if pooled is not None:
                    await state.tokens.check_account(pooled)
            except Exception:
                log.warning("generator: quota refresh failed for new account %s",
                            account_id, exc_info=True)

        def _register_account_impl(info: dict):
            """Worker finished with a new account: mint its zcodeJWT via the
            ZCODEAUTO device flow, write an accounts_dir snapshot and upsert
            into the pool — the same path a manual import takes.

            Runs in the worker's pump thread; pool mutations are scheduled onto
            the gateway's main event loop."""
            email = info.get("email") or ""
            chat_token = info.get("chat_token") or ""
            auto = bool(info.get("auto_generate"))
            cancelled = info.get("cancelled", lambda: False)
            def cancelled_result():
                return {"qualified": False, "category": "stopped", "error": "Registration cancelled"}
            if auto and cancelled():
                return cancelled_result()
            if not chat_token or not chat_token.startswith("eyJ"):
                return
            try:
                sys.path.insert(0, str(gen_dir))
                from plan_check import get_zcode_jwt, get_plan
                from .accounts_dir_bridge import mint_and_register
                # Both steps hit the same flaky edge as the worker (a bare 502
                # with an empty body). A single failure used to drop the
                # account silently: no snapshot, no registered marker. Retry,
                # and on a plan that hasn't propagated yet leave it UNMARKED so
                # the start-up catch-up scan can pick it up later.
                import time as _time
                zjwt = None
                for attempt in range(1, 4):
                    if auto and cancelled():
                        return cancelled_result()
                    try:
                        zjwt = get_zcode_jwt(chat_token, log=lambda *a: None)
                    except Exception as exc:
                        # transient network failure: cost ONE attempt, not all
                        # three (an escaping exception used to jump straight
                        # to the outer handler and drop the account).
                        log.warning("generator: zcodeJWT mint error for %s "
                                    "(attempt %d/3): %s", email, attempt,
                                    type(exc).__name__)
                        zjwt = None
                    if zjwt:
                        break
                    log.warning("generator: no zcodeJWT for %s (attempt %d/3)",
                                email, attempt)
                    if attempt < 3:
                        _time.sleep(3)
                if not zjwt:
                    log.warning("generator: %s left unregistered — retried, will "
                                "be picked up by the catch-up scan", email)
                    return
                plans = []
                plan_info = None
                for attempt in range(1, 4):
                    if auto and cancelled():
                        return cancelled_result()
                    try:
                        plan_info = get_plan(zjwt) or {}
                    except Exception as exc:
                        log.warning("generator: plan read error for %s "
                                    "(attempt %d/3): %s", email, attempt,
                                    type(exc).__name__)
                        plan_info = {}
                    plans = plan_info.get("plans") or []
                    if plans:
                        break
                    if attempt < 3:
                        _time.sleep(3)
                if not plans:
                    log.info("generator: %s has no plan after retries — leaving "
                             "it for the catch-up scan", email)
                    return
                if auto:
                    from plan_check import is_start_plan
                    if not plan_info or plan_info.get("code") != 0 or not any(is_start_plan(p) for p in plans):
                        return {"qualified": False, "category": "no_plan", "error": "No active Starter/Start Plan verified"}
                    # The run must count a NEW provider identity, not a duplicate
                    # token for an account already loaded before registration.
                    from .zcode_detect import jwt_claims
                    identity = jwt_claims(zjwt)
                    ident = identity.get("user_id") or identity.get("sub")
                    if not ident:
                        return {"qualified": False, "category": "registration_error", "error": "Account identity missing"}
                    prior = asyncio.run_coroutine_threadsafe(state.pool.find(ident), main_loop).result(timeout=20)
                    if prior:
                        return {"qualified": False, "category": "duplicate", "account_id": ident}
                chosen_plan = next((p for p in plans
                                    if str(p).startswith("zcode-v3-start-plan-")
                                    and p != "zcode-v3-start-plan-1024"),
                                   plans[0] if plans else "")
                if auto and cancelled():
                    return cancelled_result()
                receipt = {}
                if auto:
                    if not _qualifying_start_balance(plan_info.get("raw") or {}, time.time()):
                        return {"qualified": False, "category": "no_plan", "error": "No currently spendable Start Plan balance"}
                    run_id, attempt_id = info.get("auto_run_id"), info.get("auto_attempt_id")
                    if not run_id or not attempt_id:
                        return {"qualified": False, "category": "registration_error", "error": "Registration receipt identity missing"}
                    receipt = {"auto_run_id": run_id, "auto_attempt_id": attempt_id, "auto_qualified": "true"}
                account = mint_and_register(
                    state.cfg.accounts.dir, email, zjwt,
                    state.cfg.upstream.fingerprint.device_mid,
                    plan=chosen_plan, **({"receipt": receipt} if auto else {}))
                if account:
                    log.info("generator: %s joined the pool (plan=%s)", email, chosen_plan)
                    fut = asyncio.run_coroutine_threadsafe(
                        state.pool.upsert(account), main_loop)
                    fut.result(timeout=30)
                    if auto:
                        checked = asyncio.run_coroutine_threadsafe(_verify_auto_account(account.id, plan_info.get("raw")), main_loop).result(timeout=20)
                        if not checked:
                            return {"qualified": False, "category": "registration_error",
                                    "error": "Saved account needs a fresh successful plan/quota check"}
                        if cancelled():
                            return {"qualified": False, "category": "stopped", "error": "Account saved during stop; excluded from run goal"}
                        run_id, attempt_id = info.get("auto_run_id"), info.get("auto_attempt_id")
                        if not run_id or not attempt_id:
                            return {"qualified": False, "category": "registration_error", "error": "Registration receipt identity missing"}
                        from .accounts_dir import save_account_snapshot
                        # Publish the qualification receipt only after a valid
                        # Start-plan-owned balance and canonical pool insertion.
                        save_account_snapshot(Path(state.cfg.accounts.dir), {
                            "account_id": account.id, "auto_run_id": run_id,
                            "auto_attempt_id": attempt_id, "auto_qualified": "true",
                        })
                        mgr.record_auto_registration(run_id, attempt_id, account.id)
                    # quota/plan refresh is network-bound and best-effort: fire and
                    # forget, so a slow upstream can never fail the registration
                    if not auto:
                        asyncio.run_coroutine_threadsafe(
                            _settle_new_account(account.id), main_loop)
                    try:
                        mgr.mark_registered(email)   # done → never re-register
                    except Exception:
                        if auto:
                            return {"qualified": False, "category": "registration_error", "error": "Registration ledger could not be saved"}
                    # 2/3 referral (bind ok, message blocked by risk control):
                    # remember it so the background finisher can complete it
                    if not auto and chosen_plan != "zcode-v3-start-plan-referral":
                        try:
                            if _referral_needs_finish(zjwt):
                                mgr.add_pending_referral(email, zjwt)
                                log.info("generator: %s queued for 100M referral "
                                         "finish (message task pending)", email)
                        except Exception:
                            log.exception("generator: referral queue check failed")
                    if auto:
                        return {"qualified": True, "category": "qualified", "account_id": account.id}
            except Exception:
                log.exception("generator: pool registration failed for %s", email)
                if auto:
                    return {"qualified": False, "category": "registration_error", "error": "Provider verification or account registration failed"}

        def register_account(info: dict):
            if not info.get("auto_generate"):
                return _register_account_impl(info)
            sys.path.insert(0, str(gen_dir))
            from plan_check import request_route
            with request_route(info.get("registration_proxy") or None):
                return _register_account_impl(info)

        async def _verify_auto_account(account_id: str, raw: dict) -> bool:
            account = await state.pool.find(account_id)
            if account is None or not isinstance(raw, dict):
                return False
            now = time.time()
            from .quota import parse_balance
            plans, quotas = parse_balance(raw, now)
            account.notes.update(plans=plans, quotas=quotas)
            account.notes.pop("quota_check_error", None)
            account.balance_checked_at = account.plan_checked_at = now
            account.has_plan = account.plan_active(now)
            return _qualifying_start_balance(raw, now)

        from .generator import GeneratorManager, claim_result_landed
        gc = state.cfg.generator
        # Portable install: worker output (accounts/plans/proxies) lives beside
        # the exe in ./generator-data; this source checkout keeps it in .local.
        from .paths import app_dir, base_dir
        checkout = app_dir()
        data_dir = (Path(sys.executable).resolve().parent / "generator-data"
                    if getattr(sys, "frozen", False)
                    else base_dir() / "generator-data" if (checkout / ".git").is_dir() and gen_dir.resolve() == (checkout / "generator").resolve()
                    else gen_dir / "output")
        mgr = GeneratorManager(gen_dir, on_account=register_account,
                               python_exe=getattr(gc, "python_exe", ""),
                               data_dir=data_dir,
                               captcha_tickets=getattr(gc, "captcha_tickets", 0),
                               captcha_ticket_ttl=getattr(
                                   gc, "captcha_ticket_ttl_seconds", 75))
        from .claim_schedule import ClaimScheduler
        cc = getattr(state.cfg, "claims", None)
        claim_mode = ("proxy" if str(getattr(cc, "daily_mode", "direct")).lower() == "proxy"
                      else "direct")
        scheduler = ClaimScheduler(
            run=lambda: mgr.start_claim_all(mode=claim_mode),
            enabled=bool(getattr(cc, "daily_enabled", False)),
            daily_time=getattr(cc, "daily_time", "19:02"),
            utc_offset_hours=float(getattr(cc, "daily_utc_offset_hours", 8.0)),
            state_path=data_dir / "claim_schedule.json")
        scheduler.start()
        mgr.claim_scheduler = scheduler
        state.claim_scheduler = scheduler
        existing_setup = getattr(state, "_env_setup", None)
        if existing_setup is not None:
            mgr.env_setup = existing_setup
            existing_setup.on_python = mgr._adopt_python
            if existing_setup.python_exe:
                mgr._adopt_python(existing_setup.python_exe)

        def _claim_settle(job) -> None:
            """A claim job just finished: if it claimed, revive + re-settle
            the account so a DEAD entry returns to rotation with the new
            plan instead of waiting for a manual enable."""
            if not claim_result_landed(getattr(job, "result", None)):
                return
            email = (getattr(job, "email", "") or "").strip().lower()
            if not email:
                return
            try:
                fut = asyncio.run_coroutine_threadsafe(_revive_by_email(email),
                                                       main_loop)
                found = fut.result(timeout=60)
                if not found:
                    for info in mgr.pending_accounts():
                        if str(info.get("email", "")).strip().lower() == email:
                            with mgr._registration_lock:
                                register_account(info)
                            break
            except Exception:
                log.exception("claim settle failed for %s", email)

        async def _revive_by_email(email: str) -> bool:
            for acc in await state.pool.all():
                if (acc.email or "").strip().lower() != email:
                    continue
                # fresh quota is live: clear hard state and re-settle reality
                acc.state = type(acc.state).READY if hasattr(acc, "state") else acc.state
                acc.paid_exhausted = False
                acc.cooldown_until = 0.0
                acc.error_count = 0
                acc.last_error = ""
                try:
                    await state.tokens.check_account(acc)
                except Exception:
                    pass
                try:
                    await state.tokens.fetch_plan(acc)
                except Exception:
                    pass
                log.info("claim settle: %s revived after successful claim", email)
                return True
            return False

        mgr._claim_settle = _claim_settle
        mgr._revive_by_email = _revive_by_email
        mgr.internal_token = state.internal_token
        # AI-captcha fallback: children hit the gateway's own /v1 endpoint
        # AND the pool-health guard counts the gateway's real account dir
        mgr._gateway_port = state.cfg.server.port
        mgr.gateway_origin, mgr.gateway_ca_file = child_origin, child_ca
        mgr.accounts_dir = str(Path(state.cfg.accounts.dir))
        ck = (state.cfg.server.client_keys or [None])[0]
        mgr._client_key_for_children = ck.key if ck else state.cfg.server.admin_key
        def auto_ai_ready():
            async def inspect():
                return any(a.state.value == "ready" and a.model_eligible("GLM-5.3-Flash")
                           for a in await state.pool.all())
            return asyncio.run_coroutine_threadsafe(inspect(), main_loop).result(timeout=20)
        mgr.auto_ai_ready = auto_ai_ready
        # defaults come from config.yaml; '' invite_code would mean "off", but
        # the shipped default is the project's referral code. claim500 wins
        # over claim when both would be on (the dashboard offers one dropdown).
        # The claim type seeds from config on the FIRST run only: afterwards
        # the dashboard's saved choice wins (re-seeding every start reverted
        # it, and set_options() below then persisted the reverted value).
        if not mgr._has_saved_options:
            legacy500 = bool(getattr(gc, "claim500", False))
            legacy100 = bool(gc.claim) and not legacy500
            mgr.claim = legacy100
            mgr.claim500 = legacy500
            mgr.claim_start = bool(getattr(gc, "claim_start", True)) and not (legacy100 or legacy500)
        mgr._invite_code = gc.invite_code
        if gc.captcha in ("auto", "manual", "ai", "classic") and not mgr._has_saved_options:
            # First run only (no generator_options.json yet): seed from
            # config.yaml. On later runs the dashboard's SAVED choice wins —
            # overriding every start reset the user's "ai" back to config's
            # "manual" while captcha_strategy stayed "ai" (inconsistent pair).
            mgr.captcha = gc.captcha
        if not mgr._has_saved_options:
            mgr.headless = bool(gc.headless) and mgr.captcha != "manual"
            mgr.set_options(ip_source="direct",
                            proxy_types=GeneratorManager.normalize_proxy_types(
                                gc.proxy_types, legacy_socks_only=gc.socks_only))
        state.generator = mgr
        # Reconcile receipts after an interrupted registration-before-progress
        # write. Never count arbitrary snapshots without a qualification mark.
        try:
            from .accounts_dir import parse_snapshot
            receipts = {}
            for path in Path(state.cfg.accounts.dir).glob("*.txt"):
                fields = parse_snapshot(path)
                if fields.get("auto_qualified") == "true" and fields.get("auto_run_id") == mgr.auto_generate.status().get("id"):
                    if fields.get("auto_attempt_id") and fields.get("account_id"):
                        receipts[fields["auto_attempt_id"]] = fields["account_id"]
            mgr.auto_generate.reconcile_registrations(receipts)
        except (OSError, ValueError):
            log.exception("Auto Generate receipt recovery failed")

        # catch-up: signups that never reached the pool (gateway was closed, or
        # a registration step errored) are registered now instead of being lost
        try:
            pending = mgr.pending_accounts()
            if pending:
                log.info("generator: registering %d account(s) found on disk",
                         len(pending))
                for info in pending:
                    threading.Thread(target=register_account, args=(info,),
                                     daemon=True).start()
        except Exception:
            log.exception("generator: catch-up scan failed")

        _start_referral_finisher(mgr)
    return mgr


# ---- email system (user-owned domains + worker inbox) ---------------------- #

@router.get("/admin/dash/generator/mail")
async def generator_mail_get(request: Request, key: str = Depends(admin_key)) -> dict:
    return _generator(request).mail_config()


@router.post("/admin/dash/generator/mail")
async def generator_mail_set(request: Request, body: dict = Body(...),
                             key: str = Depends(admin_key)) -> dict:
    try:
        return _generator(request).set_mail_config(**body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/dash/generator/mail/export-worker")
async def generator_mail_export_worker(request: Request, key: str = Depends(admin_key)) -> dict:
    """Write the bundled Cloudflare worker source beside the exe (./mail-worker)."""
    return _generator(request).export_worker()


@router.post("/admin/dash/generator/mail/routing")
async def generator_mail_routing(request: Request, body: dict = Body(...),
                                 key: str = Depends(admin_key)) -> dict:
    """Confirm (or clear) that Cloudflare email routing is set up for a domain.

    Email routing can't be verified through wrangler, so this is an explicit
    user confirmation — the inbox test only proves the worker is reachable.
    Body: {"domain": "example.com", "done": true} or {"all": true}."""
    mgr = _generator(request)

    def apply() -> dict:
        if body.get("all"):
            for d in (mgr.mail_cfg.get("domains") or []):
                mgr.set_routing_done(d, bool(body.get("done", True)))
        else:
            mgr.set_routing_done(str(body.get("domain") or ""), bool(body.get("done", True)))
        return mgr.mail_config()

    # disk write + a lock that a deploy can hold: never on the event loop
    try:
        return await asyncio.to_thread(apply)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/admin/dash/generator/mail/routing-auto")
async def generator_mail_routing_auto(request: Request, body: dict = Body(default={}),
                                      key: str = Depends(admin_key)) -> dict:
    """Set up Cloudflare Email Routing (enable + catch-all → worker) through the
    API token. Replacing a rule requires one domain, replace_existing: true and
    the confirmation_fingerprint returned with needs_confirmation. Keep these
    structured results in the response so the UI can request explicit approval."""
    mgr = _generator(request)
    only = str((body or {}).get("domain") or "") if isinstance(body, dict) else ""
    replace_existing = (body or {}).get("replace_existing") is True
    fingerprint = str((body or {}).get("confirmation_fingerprint") or "")
    if replace_existing and (not only.strip() or not fingerprint):
        results = [{"ok": False, "domain": only,
                    "error": "replacement requires one domain and its current confirmation fingerprint"}]
    elif replace_existing or fingerprint:
        results = await asyncio.to_thread(
            mgr.auto_route_all, None, only, replace_existing=replace_existing,
            confirmation_fingerprint=fingerprint)
    else:
        results = await asyncio.to_thread(mgr.auto_route_all, None, only)
    return {"ok": all(r.get("ok") for r in results),
            "results": results, "config": mgr.mail_config()}


@router.post("/admin/dash/generator/mail/cf-credentials")
async def generator_mail_cf_credentials(request: Request, body: dict = Body(default={}),
                                        key: str = Depends(admin_key)) -> dict:
    """Save / replace / remove the Cloudflare API token without redeploying.
    Body: {"cf_token": "...", "cf_account_id": "..."} or {"clear": true}."""
    mgr = _generator(request)
    body = body if isinstance(body, dict) else {}
    res = await asyncio.to_thread(
        mgr.save_cf_credentials, str(body.get("cf_token") or ""),
        str(body.get("cf_account_id") or ""), bool(body.get("clear")))
    res["config"] = mgr.mail_config()
    return res


@router.get("/admin/dash/generator/mail/deploy-status")
async def generator_mail_deploy_status(request: Request, key: str = Depends(admin_key)) -> dict:
    """Pre-deploy checklist state: files exported? wrangler authed? url set?"""
    mgr = _generator(request)
    try:
        return await asyncio.to_thread(mgr.worker_deploy_status)
    except Exception as exc:
        return {"exported": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/admin/dash/generator/mail/deploy")
async def generator_mail_deploy(request: Request, body: dict = Body(default={}),
                                key: str = Depends(admin_key)) -> dict:
    """One-click worker deploy: login (browser only if needed) → KV → deploy
    → secret → URL + smoke test. Long-running: wrangler up to a few minutes,
    so it runs off the event loop."""
    mgr = _generator(request)
    log_lines: list[str] = []

    def progress(msg: str) -> None:
        log_lines.append(msg)

    try:
        body = body if isinstance(body, dict) else {}
        result = await asyncio.to_thread(
            mgr.deploy_worker, progress,
            str(body.get("cf_token") or ""), str(body.get("cf_account_id") or ""))
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result["steps"] = log_lines
    return result


@router.get("/admin/dash/generator/mail/test")
async def generator_mail_test(request: Request, key: str = Depends(admin_key)) -> dict:
    """Round-trip check: is the worker inbox reachable with the stored secret?"""
    mgr = _generator(request)
    try:
        return await asyncio.to_thread(mgr.test_inbox)
    except Exception as exc:
        return {"ok": False, "error": f"inbox verification failed ({type(exc).__name__}); retry after deployment"}


@router.get("/admin/dash/generator/status")
async def generator_status(request: Request, key: str = Depends(admin_key)) -> dict:
    try:
        # status() counts pool countries from the cache files; that is
        # blocking disk work and must not run on the event loop
        return await asyncio.to_thread(_generator(request).status)
    except HTTPException:
        raise


@router.post("/admin/dash/generator/options")
async def generator_options(request: Request, body: dict = Body(...),
                            key: str = Depends(admin_key)) -> dict:
    _generator(request).set_options(**body)
    return {"ok": True}


@router.post("/admin/dash/generator/start")
async def generator_start(request: Request, body: dict = Body(default={}),
                          key: str = Depends(admin_key)) -> dict:
    # unvalidated ints 500 on "abc" and a huge number spawned that many workers
    try:
        count = int(body.get("count", 1)) if isinstance(body, dict) else 1
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="count must be an integer") from None
    count = max(1, min(count, 10))
    mgr = _generator(request)

    def _emit(line: str) -> None:
        # install progress lands in the dashboard's log view
        log.info("playwright-setup: %s", line)

    return await asyncio.to_thread(mgr.start_workers_checked, count, _emit)


@router.get("/admin/dash/generator/auto")
async def auto_generate_status(request: Request, key: str = Depends(admin_key)) -> dict:
    controller = _generator(request).auto_generate
    return _auto_public(controller)


def _auto_public(controller) -> dict:
    status = controller.status()
    cooldowns = [value for value in status.get("cooldowns", []) if value > 0]
    return {"config": controller.config(), "status": {
        **status, "state": status.get("status", "idle"),
        "launched": status.get("attempts", 0), "completed": status.get("completed_attempts", 0),
        "cooling": len(cooldowns), "remaining_cooldown_seconds": min(cooldowns) if cooldowns else 0,
    }}


@router.put("/admin/dash/generator/auto")
async def auto_generate_save(request: Request, body: dict = Body(...), key: str = Depends(admin_key)) -> dict:
    controller = _generator(request).auto_generate
    try:
        await asyncio.to_thread(controller.save_config, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except OSError:
        raise HTTPException(500, "Could not save Auto Generate configuration") from None
    return _auto_public(controller)


@router.post("/admin/dash/generator/auto/{action}")
async def auto_generate_action(request: Request, action: str, key: str = Depends(admin_key)) -> dict:
    if action not in ("start", "pause", "resume", "stop"):
        raise HTTPException(404, "Unknown Auto Generate action")
    controller = _generator(request).auto_generate
    try:
        await asyncio.to_thread(getattr(controller, action))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from None
    except OSError:
        raise HTTPException(500, "Auto Generate state could not be saved") from None
    return _auto_public(controller)


def _env_setup_shared(request: Request):
    """EnvSetup without requiring the generator checkout: the setup modal's
    whole job is to INSTALL python/deps/chromium, so it must work before the
    generator folder has even been located. Shares the instance with the
    GeneratorManager when one exists so install state is not duplicated."""
    state = get_state(request)
    mgr = getattr(state, "generator", None)
    if mgr is not None and getattr(mgr, "env_setup", None) is not None:
        return mgr.env_setup
    cached = getattr(state, "_env_setup", None)
    if cached is None:
        from .generator import EnvSetup
        cached = EnvSetup(getattr(state.cfg.generator, "python_exe", "") or "")
        def adopt_python(py):
            state.cfg.generator.python_exe = py
            manager = getattr(state, "generator", None)
            if manager is not None:
                manager._adopt_python(py)
        cached.on_python = adopt_python
        state._env_setup = cached
    return cached


@router.get("/admin/dash/setup/check")
async def setup_check(request: Request, key: str = Depends(admin_key)) -> dict:
    """Full environment check for the setup modal (python/deps/chromium)."""
    return await asyncio.to_thread(_env_setup_shared(request).check)


@router.post("/admin/dash/setup/install")
async def setup_install(request: Request, key: str = Depends(admin_key)) -> dict:
    """Kick off (or retry) the background environment installer."""
    es = _env_setup_shared(request)
    started = es.start_install()
    return {"started": started, "status": es.status()}


@router.get("/admin/dash/setup/status")
async def setup_status(request: Request, key: str = Depends(admin_key)) -> dict:
    """Polled by the setup modal while installing: log lines + state."""
    return _env_setup_shared(request).status()


@router.post("/admin/dash/generator/stop")
async def generator_stop(request: Request, body: dict = Body(...),
                         key: str = Depends(admin_key)) -> dict:
    name = body.get("name", "")
    return {"stopped": _generator(request).stop_worker(name)}


@router.post("/admin/dash/generator/stopall")
async def generator_stopall(request: Request, key: str = Depends(admin_key)) -> dict:
    _generator(request).stop_all()
    return {"ok": True}


@router.post("/admin/dash/generator/shutdown")
async def generator_shutdown(request: Request, key: str = Depends(admin_key)) -> dict:
    """Close owned jobs, checker and display without creating a manager."""
    manager = getattr(get_state(request), "generator", None)
    if manager is not None:
        await asyncio.to_thread(manager.shutdown)
    return {"ok": True}


@router.post("/admin/dash/generator/workers/remove")
async def generator_worker_remove(request: Request, body: dict = Body(...),
                                  key: str = Depends(admin_key)) -> dict:
    name = str(body.get("name") or "")
    ok = _generator(request).remove_worker(name)
    if not ok:
        raise HTTPException(status_code=404, detail="no such worker")
    return {"removed": name}


@router.post("/admin/dash/generator/workers/clear")
async def generator_workers_clear(request: Request, key: str = Depends(admin_key)) -> dict:
    n = _generator(request).remove_finished_workers()
    return {"cleared": n}


@router.post("/admin/dash/generator/send_claim_message")
async def generator_send_claim_message(request: Request, body: dict = Body(...),
                                       key: str = Depends(internal_or_admin)) -> dict:
    zjwt = str(body.get("zjwt") or "").strip()
    if not zjwt:
        raise HTTPException(status_code=400, detail="missing zjwt")
    state = get_state(request)
    from .upstream import build_headers
    from .zcode_protocol import build_body
    headers = build_headers(zjwt, state.cfg.upstream.fingerprint, include_device_mid=False)
    upstream_body = build_body({
        "model": "GLM-5.3-Flash",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    })
    resp = await state.service.client.send(headers, upstream_body, stream=True)
    raw = await resp.aread()
    await resp.aclose()
    ok = resp.status_code == 200 and b"message_start" in raw
    return {"status": resp.status_code, "ok": ok, "snippet": raw[:120].decode("utf-8", "replace")}


# ---- proxy checker (scrape / verify / recheck) ----------------------------- #

@router.get("/admin/dash/generator/proxies")
async def generator_proxies(request: Request, key: str = Depends(admin_key)) -> dict:
    return _generator(request).checker.status()


@router.post("/admin/dash/generator/proxies/run")
async def generator_proxies_run(request: Request, body: dict = Body(default={}),
                                key: str = Depends(admin_key)) -> dict:
    checker = _generator(request).checker
    action = body.get("action", "verify")

    def _num(name, cast=float):
        v = body.get(name)
        if v is None or v == "":
            return None
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    def _flag(name):
        v = body.get(name)
        return None if v is None else bool(v)

    return checker.run(
        action,
        timeout=_num("timeout", int),
        concurrency=_num("concurrency", int),
        max_latency=_num("max_latency"),
        preset=(body.get("preset") or None),
        prefilter=_flag("prefilter"),
        retry_failed=_flag("retry_failed"),
        dead_cache_hours=_num("dead_cache_hours"),
        probe_timeout=_num("probe_timeout"),
        probe_concurrency=_num("probe_concurrency", int),
    )


@router.post("/admin/dash/generator/proxies/stop")
async def generator_proxies_stop(request: Request, key: str = Depends(admin_key)) -> dict:
    _generator(request).checker.stop()
    return {"ok": True}


@router.post("/admin/dash/generator/proxies/import")
async def generator_proxies_import(request: Request, body: dict = Body(...),
                                   key: str = Depends(admin_key)) -> dict:
    """Add pasted/uploaded proxies to the pool (usable immediately)."""
    text = body.get("text", "")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="text must be a string")
    n = _generator(request).checker.import_proxies(
        text, into_good=bool(body.get("into_good", True)),
        default_proto=str(body.get("default_proto") or "http"))
    return {"imported": n}


@router.get("/admin/dash/generator/proxies/sources")
async def generator_proxies_sources(request: Request, key: str = Depends(admin_key)) -> dict:
    return {"text": _generator(request).checker.sources_text()}


@router.post("/admin/dash/generator/proxies/sources")
async def generator_proxies_sources_set(request: Request, body: dict = Body(...),
                                        key: str = Depends(admin_key)) -> dict:
    text = body.get("text", "")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="text must be a string")
    n = _generator(request).checker.set_sources(text)
    return {"saved": True, "sources": n}


@router.get("/admin/dash/generator/proxies/yield")
async def generator_proxies_yield(request: Request, key: str = Depends(admin_key)) -> dict:
    """Per-source yield: which lists actually produce working proxies."""
    return {"sources": _generator(request).checker.sources_yield()}


@router.post("/admin/dash/generator/proxies/maintain")
async def generator_proxies_maintain(request: Request, body: dict = Body(...),
                                     key: str = Depends(admin_key)) -> dict:
    """Pool housekeeping: prune | clear-bad | clear-used | clear-dead."""
    action = str(body.get("action") or "").strip()
    if action not in ("prune", "clear-bad", "clear-used", "clear-dead"):
        raise HTTPException(status_code=400, detail="unknown maintenance action")
    return _generator(request).checker.run(action)


@router.post("/admin/dash/generator/proxies/countries")
async def generator_proxies_countries(request: Request, key: str = Depends(admin_key)) -> dict:
    """Look up the country of every pool proxy that has none (online, nothing is
    downloaded). Runs in the proxy checker so its log and progress show up."""
    return _generator(request).checker.run("countries")


# ---- Edge Relays (Cloudflare, Vercel, Deno, Netlify) ------------------------ #

def _relay_mgr(request: Request):
    state = get_state(request)
    mgr = getattr(state, "relay_manager", None)
    if mgr is None:
        from .relay_manager import RelayManager
        data_dir = (Path(state.cfg.accounts.dir).resolve().parent / "generator-data") if state.cfg.accounts.dir else Path("./generator-data")
        mgr = RelayManager(data_dir=data_dir)
        state.relay_manager = mgr
        if getattr(state, "client", None) and not getattr(state.client, "relay_manager", None):
            state.client.relay_manager = mgr
    return mgr


@router.get("/admin/dash/relays")
async def get_relays(request: Request, key: str = Depends(admin_key)) -> dict:
    mgr = _relay_mgr(request)
    return mgr.get_settings()


@router.post("/admin/dash/relays/deploy")
async def deploy_relay_endpoint(request: Request, body: dict = Body(...),
                                key: str = Depends(admin_key)) -> dict:
    mgr = _relay_mgr(request)
    relay_type = str(body.get("type") or "").strip().lower()
    try:
        return await asyncio.to_thread(mgr.deploy_and_add, relay_type, body)
    except Exception as e:
        from .neterr import friendly
        # the readable sentence goes to the dashboard; the raw cause to the log
        log.warning("relay deploy (%s) failed: %s: %s [%s]", relay_type, type(e).__name__, e,
                    getattr(e, "detail", ""))
        raise HTTPException(status_code=400, detail=friendly(e))


@router.post("/admin/dash/relays/add")
async def add_relay_endpoint(request: Request, body: dict = Body(...),
                             key: str = Depends(admin_key)) -> dict:
    mgr = _relay_mgr(request)
    name = str(body.get("name") or "").strip()
    url = str(body.get("url") or "").strip()
    type_name = str(body.get("type") or "custom").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Relay URL is required")
    try:
        # add_relay validates the URL and writes relays.json — off the loop
        # a relay added by hand keeps the secret the user typed; with none we
        # send no header (never invent a secret the relay does not know)
        secret = str(body.get("secret") or "").strip()
        entry = await asyncio.to_thread(mgr.add_relay, name, url, type_name, secret)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "relay": entry}


@router.post("/admin/dash/relays/test")
async def test_relay_endpoint(request: Request, body: dict = Body(...),
                              key: str = Depends(admin_key)) -> dict:
    mgr = _relay_mgr(request)
    relay_id = str(body.get("id") or "").strip()
    url = str(body.get("url") or "").strip()
    if relay_id:
        res = await asyncio.to_thread(mgr.test_relay_by_id, relay_id)
    elif url:
        from .relay_deploy import ping_relay
        # hardened relays require the secret; a bare pre-add test of a URL
        # carrying its secret in the body must send it or always see 403
        secret = str(body.get("secret") or "").strip()
        res = await asyncio.to_thread(ping_relay, url, 12.0, secret)
    else:
        raise HTTPException(status_code=400, detail="Relay ID or URL is required")
    return res


@router.post("/admin/dash/relays/settings")
async def save_relay_settings(request: Request, body: dict = Body(...),
                              key: str = Depends(admin_key)) -> dict:
    mgr = _relay_mgr(request)
    models = body.get("models")
    enabled = body.get("enabled")
    active_id = body.get("active_id")
    strategy = body.get("strategy")
    try:
        # update_options ends in a relays.json write — off the loop
        st = await asyncio.to_thread(
            mgr.update_options, models=models, enabled=enabled,
            active_id=active_id, strategy=strategy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "settings": st}


@router.post("/admin/dash/relays/delete")
async def delete_relays_endpoint(request: Request, body: dict = Body(...),
                                 key: str = Depends(admin_key)) -> dict:
    """Bulk delete: {"ids": [...]} removes those relays, {"all": true} every one."""
    mgr = _relay_mgr(request)
    ids = body.get("ids")
    if body.get("all") is not True and not (isinstance(ids, list) and ids):
        raise HTTPException(status_code=400, detail="send {\"ids\": [...]} or {\"all\": true}")
    removed = await asyncio.to_thread(
        mgr.delete_relays, ids if isinstance(ids, list) else None,
        all_relays=body.get("all") is True)
    return {"ok": True, "deleted": removed}


@router.delete("/admin/dash/relays/{relay_id}")
async def delete_relay_endpoint(request: Request, relay_id: str,
                                key: str = Depends(admin_key)) -> dict:
    mgr = _relay_mgr(request)
    ok = await asyncio.to_thread(mgr.delete_relay, relay_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Relay not found")
    return {"ok": True, "deleted": relay_id}


