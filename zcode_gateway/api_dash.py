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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
import yaml

from .deps import admin_key, get_state
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
    "models.probe_unknown": bool,
    "models.auto_free_fallback.enabled": bool,
    "update.enabled": bool, "update.interval_hours": float,
}


def _read_config_raw(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


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


@router.get("/dash", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    index = DASH_DIR / "index.html"
    if not index.is_file():
        return HTMLResponse("dashboard not bundled", status_code=404)
    html = index.read_text(encoding="utf-8")
    # First-launch convenience: hand the page its own admin key so the login
    # screen does not gate the local tool (single-user, single-machine — the
    # Server tab displays the same key anyway). Both the bind address and the
    # request's own Host must be loopback: a DNS-rebinding page is same-origin
    # and would otherwise read the key straight out of this response.
    try:
        cfg = get_state(request).cfg
        if ((cfg.server.host or "").strip() in _LOOPBACK_HOSTS
                and _request_is_loopback(request)):
            marker = 'sessionStorage.getItem("gw-admin-key") || ""'
            injected = ('sessionStorage.getItem("gw-admin-key") || '
                        + json.dumps(cfg.server.admin_key))
            html = html.replace(marker, injected, 1)
    except Exception:
        pass
    return HTMLResponse(html)


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
        },
        "admin_key": cfg.server.admin_key,
        "client_keys": [k.model_dump() for k in cfg.server.client_keys],
        "accounts_source": cfg.accounts.source,
    }


@router.put("/admin/dash/settings")
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
    changed: list[str] = []
    for dotted, val in values.items():
        cast = _EDITABLE[dotted]
        try:
            if cast is bool and isinstance(val, str):
                val = val.lower() in ("1", "true", "yes", "on")
            else:
                val = cast(val)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail=f"{dotted}: cannot cast {val!r}") from None
        # range validation: a 0 or negative written straight into config.yaml
        # can deadlock the pool or break startup (pydantic ge=1 catches it
        # only at next boot — fail here instead)
        if cast is int and val < 0:
            raise HTTPException(status_code=400,
                                detail=f"{dotted}: must be >= 0")
        if dotted == "pool.max_in_flight_per_account" and val < 1:
            raise HTTPException(status_code=400,
                                detail="pool.max_in_flight_per_account must be >= 1")
        if dotted == "server.port" and not (0 <= val <= 65535):
            raise HTTPException(status_code=400,
                                detail="server.port must be 0-65535")
        if dotted == "server.port" and val == 0:
            raise HTTPException(status_code=400,
                                detail="server.port 0 (random) is not supported by the dashboard")
        cur = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        if cur.get(parts[-1]) != val:
            cur[parts[-1]] = val
            changed.append(dotted)

    # Validate bounded overload controls before persisting a restart-breaking value.
    if any(k.startswith('upstream.overload_') for k in values):
        try:
            type(state.cfg.upstream).model_validate(raw.get('upstream', {}))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    if changed:
        cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False,
                                           allow_unicode=True),
                            encoding="utf-8")
    return {"changed": changed, "restart_required": bool(changed)}


@router.post("/admin/dash/import")
async def dash_import(request: Request, key: str = Depends(admin_key)) -> dict:
    """Snapshot every logged-in Zcode account into the accounts folder."""
    state = get_state(request)
    try:
        # The two import helpers live in gui.py, but gui.py imports tkinter
        # at module level (absent on headless servers) and uses it 130+ times.
        # Rather than executing that module, read just the two needed defs
        # with ast and exec them against a minimal namespace.
        from .zcode_accounts import import_credentials
        import ast as _ast
        _src = Path(__file__).with_name("gui.py").read_text(encoding="utf-8")
        _tree = _ast.parse(_src)
        _wanted = {"app_dir", "base_dir", "find_accounts_dir",
                   "_zcode_providers"}
        _ns = {"__name__": "zcode_gateway._gui_helpers",
               "Path": Path, "os": os, "sys": sys}
        for _node in _tree.body:
            if isinstance(_node, _ast.FunctionDef) and _node.name in _wanted:
                _mod = _ast.Module(body=[_node], type_ignores=[])
                _code = compile(_mod, "gui.py", "exec")
                exec(_code, _ns)
        _missing = _wanted - set(_ns)
        if _missing:
            raise RuntimeError(f"helpers not found in gui.py: {_missing}")
        _zcode_providers = _ns["_zcode_providers"]
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
    if existing and existing.state in ("awaiting_user", "registering"):
        return existing.status()          # one flow at a time; idempotent start
    from .device_auth import DeviceLogin

    async def _add_and_probe(account):
        """New account: join the pool, then settle balance/paid-lane/plan now —
        a login that replaced an exhausted account must not read 'available'."""
        await state.pool.upsert(account)
        try:
            await state.tokens.check_account(account)
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
    get_state(request)
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return {"stopping": True,
            "note": "the gateway process is exiting; you can close this window"}


@router.get("/admin/dash/update/status")
async def update_status(request: Request, key: str = Depends(admin_key)) -> dict:
    """Cached update check result (never hits GitHub from this route)."""
    state = get_state(request)
    updater = getattr(state, "updater", None)
    if updater is None:
        return {"update_available": False, "checked_at": None}
    return updater.status


@router.post("/admin/dash/update/check")
async def update_check(request: Request, key: str = Depends(admin_key)) -> dict:
    """Force a fresh check now (one GitHub round-trip)."""
    state = get_state(request)
    updater = getattr(state, "updater", None)
    if updater is None:
        raise HTTPException(status_code=503, detail="updater not running")
    return await updater.check()


@router.post("/admin/dash/update/download")
async def update_download(request: Request, key: str = Depends(admin_key)) -> dict:
    """Download the new exe next to the running one (exe builds only)."""
    state = get_state(request)
    updater = getattr(state, "updater", None)
    status = updater.status if updater else {}
    url = status.get("download_url")
    if not (status.get("update_available") and url):
        raise HTTPException(status_code=400, detail="no update available")
    from . import updater as updater_mod
    try:
        path = await updater_mod.download_update(url, status.get("checksum_url"))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502,
                            detail=f"download failed ({exc.__class__.__name__})") \
            from exc
    return {"downloaded": str(path), "size": path.stat().st_size}


@router.post("/admin/dash/update/apply")
async def update_apply(request: Request, key: str = Depends(admin_key)) -> dict:
    """Swap in the downloaded exe and relaunch (exe builds only)."""
    get_state(request)
    from . import updater as updater_mod
    try:
        updater_mod.apply_update(updater_mod.exe_dir() / updater_mod.NEW_EXE_NAME)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # the swapper waits for this process to die; answer first, then exit
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return {"applying": True, "note": "the gateway exits, swaps the exe, and "
                                      "relaunches itself"}


@router.get("/admin/dash/logs")
async def dash_logs(request: Request, limit: int = 300, level: str = "",
                    key: str = Depends(admin_key)) -> dict:
    """Recent log lines from the in-process ring buffer."""
    state = get_state(request)
    buf: LogBuffer | None = getattr(state, "log_buffer", None)
    if buf is None:
        return {"records": [], "note": "log buffer not attached"}
    return {"records": buf.tail(limit=max(1, min(limit, 1000)), level=level)}


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

    # persist + apply in memory
    cfg_path = _config_path(state)
    if cfg_path is not None:
        raw = _read_config_raw(cfg_path)
        gen = raw.setdefault("generator", {})
        gen["zcodeauto_dir"] = str(root)
        gen["enabled"] = True
        cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False,
                                           allow_unicode=True), encoding="utf-8")
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


def _generator(request: Request):
    """The GeneratorManager for this app state, created lazily."""
    state = get_state(request)
    mgr = getattr(state, "generator", None)
    if mgr is None:
        gen_dir, checked = _find_zcodeauto(state)
        if gen_dir is None:
            hint = "; ".join(str(c) for c in checked[:5])
            raise HTTPException(
                status_code=404,
                detail="generator not found — open the Generator tab and click "
                       f"'Locate generator'. Looked in: {hint}"
                       + (" …" if len(checked) > 5 else ""))
        main_loop = asyncio.get_running_loop()

        def register_account(info: dict):
            """Worker finished with a new account: mint its zcodeJWT via the
            ZCODEAUTO device flow, write an accounts_dir snapshot and upsert
            into the pool — the same path a manual import takes.

            Runs in the worker's pump thread; pool mutations are scheduled onto
            the gateway's main event loop."""
            email = info.get("email") or ""
            chat_token = info.get("chat_token") or ""
            if not chat_token or not chat_token.startswith("eyJ"):
                return
            try:
                sys.path.insert(0, str(gen_dir))
                from plan_check import get_zcode_jwt, get_plan
                from .accounts_dir_bridge import mint_and_register
                zjwt = get_zcode_jwt(chat_token, log=lambda *a: None)
                if not zjwt:
                    log.warning("generator: no zcodeJWT for %s", email)
                    return
                plan_info = get_plan(zjwt) or {}
                plans = plan_info.get("plans") or []
                chosen_plan = "zcode-v3-start-plan-referral" if "zcode-v3-start-plan-referral" in plans else (plans[0] if plans else "")
                account = mint_and_register(
                    state.cfg.accounts.dir, email, zjwt,
                    state.cfg.upstream.fingerprint.device_mid,
                    plan=chosen_plan)
                if account:
                    log.info("generator: %s joined the pool (plan=%s)", email, chosen_plan)
                    fut = asyncio.run_coroutine_threadsafe(
                        state.pool.upsert(account), main_loop)
                    fut.result(timeout=30)
                    try:
                        mgr.mark_registered(email)   # done → never re-register
                    except Exception:
                        pass
                    # 2/3 referral (bind ok, message blocked by risk control):
                    # remember it so the background finisher can complete it
                    if chosen_plan != "zcode-v3-start-plan-referral":
                        try:
                            if _referral_needs_finish(zjwt):
                                mgr.add_pending_referral(email, zjwt)
                                log.info("generator: %s queued for 100M referral "
                                         "finish (message task pending)", email)
                        except Exception:
                            log.exception("generator: referral queue check failed")
            except Exception:
                log.exception("generator: pool registration failed for %s", email)

        from .generator import GeneratorManager
        gc = state.cfg.generator
        # Portable install: worker output (accounts/plans/proxies) lives beside
        # the exe in ./generator-data; a source checkout uses ZCODEAUTO's output/.
        data_dir = (Path(sys.executable).resolve().parent / "generator-data"
                    if getattr(sys, "frozen", False)
                    else gen_dir / "output")
        mgr = GeneratorManager(gen_dir, on_account=register_account,
                               python_exe=getattr(gc, "python_exe", ""),
                               data_dir=data_dir)
        # defaults come from config.yaml; '' invite_code would mean "off", but
        # the shipped default is the project's referral code (claim stays on)
        mgr.claim = bool(gc.claim)
        mgr._invite_code = gc.invite_code
        if gc.captcha in ("auto", "manual"):
            mgr.captcha = gc.captcha
        mgr.headless = bool(gc.headless) and mgr.captcha != "manual"
        mgr.set_options(ip_source="socks" if gc.socks_only else "pool")
        state.generator = mgr

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
    if body.get("all"):
        for d in (mgr.mail_cfg.get("domains") or []):
            mgr.set_routing_done(d, bool(body.get("done", True)))
    else:
        mgr.set_routing_done(str(body.get("domain") or ""), bool(body.get("done", True)))
    return mgr.mail_config()


@router.get("/admin/dash/generator/mail/deploy-status")
async def generator_mail_deploy_status(request: Request, key: str = Depends(admin_key)) -> dict:
    """Pre-deploy checklist state: files exported? wrangler authed? url set?"""
    mgr = _generator(request)
    try:
        return await asyncio.to_thread(mgr.worker_deploy_status)
    except Exception as exc:
        return {"exported": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/admin/dash/generator/mail/deploy")
async def generator_mail_deploy(request: Request, key: str = Depends(admin_key)) -> dict:
    """One-click worker deploy: login (browser only if needed) → KV → deploy
    → secret → URL + smoke test. Long-running: wrangler up to a few minutes,
    so it runs off the event loop."""
    mgr = _generator(request)
    log_lines: list[str] = []

    def progress(msg: str) -> None:
        log_lines.append(msg)

    try:
        result = await asyncio.to_thread(mgr.deploy_worker, progress)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result["steps"] = log_lines
    return result


@router.get("/admin/dash/generator/mail/test")
async def generator_mail_test(request: Request, key: str = Depends(admin_key)) -> dict:
    """Round-trip check: is the worker inbox reachable with the stored secret?"""
    mgr = _generator(request)
    url = (mgr.mail_cfg.get("inbox_url") or "").rstrip('/')
    if not url:
        return {"ok": False, "error": "inbox_url not configured"}
    try:
        # async: a sync httpx.get in an async route stalls the whole event
        # loop (all /v1/* streaming) for up to the full timeout
        import httpx as _hx
        async with _hx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params={"k": mgr.mail_cfg.get("inbox_secret", "")})
        ok = r.status_code == 200
        mgr.mark_inbox_verified(ok)   # drives the wizard's step 3 checkmark
        return {"ok": ok, "status": r.status_code, "body": r.text[:200]}
    except Exception as e:
        try:
            mgr.mark_inbox_verified(False)
        except Exception:
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.get("/admin/dash/generator/status")
async def generator_status(request: Request, key: str = Depends(admin_key)) -> dict:
    try:
        return _generator(request).status()
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
    count = int(body.get("count", 1)) if isinstance(body, dict) else 1
    return _generator(request).start_workers(count)


@router.post("/admin/dash/generator/stop")
async def generator_stop(request: Request, body: dict = Body(...),
                         key: str = Depends(admin_key)) -> dict:
    name = body.get("name", "")
    return {"stopped": _generator(request).stop_worker(name)}


@router.post("/admin/dash/generator/stopall")
async def generator_stopall(request: Request, key: str = Depends(admin_key)) -> dict:
    _generator(request).stop_all()
    return {"ok": True}


# ---- proxy checker (scrape / verify / recheck) ----------------------------- #

@router.get("/admin/dash/generator/proxies")
async def generator_proxies(request: Request, key: str = Depends(admin_key)) -> dict:
    return _generator(request).checker.status()


@router.post("/admin/dash/generator/proxies/run")
async def generator_proxies_run(request: Request, body: dict = Body(default={}),
                                key: str = Depends(admin_key)) -> dict:
    checker = _generator(request).checker
    action = body.get("action", "verify")
    return checker.run(
        action,
        timeout=int(body.get("timeout", 8)),
        concurrency=int(body.get("concurrency", 400)),
        max_latency=float(body.get("max_latency", 6.0)),
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
        text, into_good=bool(body.get("into_good", True)))
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
