"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api_admin import router as admin_router
from .api_anthropic import router as anthropic_router
from .api_dash import router as dash_router
from .api_health import router as health_router
from .api_openai import router as openai_router
from .config import Config, load_config
from .logbuffer import LogBuffer
from .model_catalog import ModelCatalog
from .pool import PoolManager, load_accounts
from .ratelimit import RateLimiter
from .registry import Registry
from .service import ChatService
from .state import AppState
from .store import JsonlCapture, Store
from .tokens import TokenManager
from .upstream import UpstreamClient, anthropic_error, openai_error

log = logging.getLogger("zcode_gateway")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: Config = app.state.cfg
    cfg.ensure_dirs()

    store = Store(cfg.store.sqlite_path)
    capture = JsonlCapture(cfg.logging.capture_dir, enabled=cfg.logging.capture)
    registry = Registry(
        aliases=cfg.models.aliases,
        default=cfg.models.default,
        default_anthropic=cfg.models.default_anthropic,
        # probe_unknown intentionally not forwarded: probing unknown ids
        # against a plan-gated upstream burns quota for no benefit; the
        # registry no longer implements it and the config field is inert.
    )
    catalog = ModelCatalog(cfg.upstream.base_url, registry)

    try:
        accounts = load_accounts(cfg.accounts)
    except Exception as exc:
        log.warning("could not load accounts from %s: %s", cfg.accounts.source, exc)
        accounts = []

    from .relay_manager import RelayManager
    relays_data_dir = (Path(cfg.accounts.dir).resolve().parent / "generator-data") if cfg.accounts.dir else Path("./generator-data")
    relay_manager = RelayManager(data_dir=relays_data_dir)
    # relay health loop: background ping every 5 minutes; failures feed the
    # rotation cooldown and the dashboard health column
    try:
        relay_manager.start_health_loop(300)
    except Exception:
        log.warning("relay health loop failed to start", exc_info=True)

    pool = PoolManager(cfg.pool, accounts)
    # persist per-model parks (UNUSUAL_ACTIVITY / GEO_BLOCKED / caps) next to
    # the sqlite store so a restart doesn't forget and re-burn every account
    try:
        caps_file = Path(cfg.store.sqlite_path).resolve().parent / "model-caps.json"
        pool.set_caps_path(caps_file)
        pool.load_caps()
    except Exception:
        log.warning("model-cap persistence unavailable", exc_info=True)
    tokens = TokenManager(cfg, pool)
    client = UpstreamClient(cfg.upstream, relay_manager=relay_manager,
                            pacing=cfg.models.pacing)
    service = ChatService(cfg, pool, tokens, client, registry, store, capture)

    # feed the dashboard's log view: one ring-buffer handler on the package log
    log_buffer = LogBuffer(capacity=1000)
    pkg_log = logging.getLogger("zcode_gateway")
    pkg_log.addHandler(log_buffer)

    app.state.app_state = AppState(
        cfg=cfg, pool=pool, tokens=tokens, client=client,
        registry=registry, store=store, capture=capture, service=service,
        limiter=RateLimiter(),
        catalog=catalog,
        relay_manager=relay_manager,
    )
    app.state.app_state.log_buffer = log_buffer
    # pool + relay events (drained, DEAD, expiring, cooled) feed the bell
    pool.attach_notifications(app.state.app_state.notifications)
    relay_manager._notify = app.state.app_state.notifications

    # release feed: one GitHub call on startup, then every interval; the
    # dashboard polls this cache, never GitHub directly
    from .updater import UpdateChecker
    update_repo = cfg.update.repo
    if update_repo in getattr(cfg.update, "legacy_repos", ()):
        # config written before the repo rename: follow the current feed so
        # existing installs receive updates without a manual config edit
        update_repo = "B3hnamR/AutoZcoder"
    updater = UpdateChecker(update_repo, __version__,
                            enabled=cfg.update.enabled,
                            interval_hours=cfg.update.interval_hours)
    app.state.app_state.updater = updater

    async def update_loop() -> None:
        # first check is deferred: startup must never block on GitHub, and
        # short-lived apps (tests, CLI runs) should not phone home at all
        await asyncio.sleep(min(20.0, updater.interval_s))
        while True:
            try:
                await updater.check()
            except Exception:
                log.exception("update check iteration failed")
            await asyncio.sleep(updater.interval_s)

    tasks = [
        asyncio.create_task(tokens.refresh_loop(), name="token-refresh"),
        asyncio.create_task(update_loop(), name="update-check"),
    ]
    if cfg.pool.balance_poll_seconds > 0:
        tasks.append(asyncio.create_task(tokens.balance_loop(), name="balance-poll"))

    log.info("zcode gateway %s up — %d account(s), strategy=%s — Coded by @B3hnamR",
             __version__, len(accounts), cfg.pool.strategy)

    try:
        yield
    finally:
        generator = getattr(app.state.app_state, "generator", None)
        if generator is not None:
            try:
                await asyncio.to_thread(generator.shutdown)
            except Exception:
                log.exception("generator shutdown failed")
        relay_manager._health_stop.set()
        login = getattr(app.state.app_state, "device_login", None)
        if login is not None and getattr(login, "_task", None) is not None:
            login.cancel()
            login._task.cancel()
            tasks.append(login._task)
        pkg_log.removeHandler(log_buffer)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await tokens.aclose()
        await client.aclose()
        await catalog.aclose()
        await updater.aclose()
        await pool.aclose()
        await service.aclose()
        store.close()


def _norm_netloc(netloc: str, scheme: str) -> str:
    """Lower-cased host[:port] with the scheme's default port dropped, so
    `https://h` and `h:443` compare equal."""
    netloc = (netloc or "").strip().lower()
    for sch, port in (("https", ":443"), ("http", ":80")):
        if (scheme or "").lower() == sch and netloc.endswith(port):
            return netloc[: -len(port)]
    return netloc


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    setup_logging(cfg.logging.level)

    # --- bind security (VPS / remote deployments) --------------------------
    # The admin key is local root-equivalent: it can point the generator at
    # arbitrary code, swap the binary, and rewrite config. A non-loopback
    # bind is only allowed with real credentials in place.
    if not cfg.server.is_loopback_bind():
        if not cfg.server.require_client_key:
            raise RuntimeError(
                f"server.host={cfg.server.host} is a public bind but "
                "server.require_client_key is false — an unauthenticated "
                "proxy on a public interface is an open relay. Set "
                "require_client_key: true (and configure client_keys).")
        if not cfg.server.client_keys:
            raise RuntimeError(
                "server.host is a public bind but no client_keys are "
                "configured — nothing would authenticate. Configure "
                "client_keys.")
        if cfg.server.has_placeholder_client_key():
            raise RuntimeError(
                "server.host is a public bind but a client key is still the "
                "example placeholder from config.example.yaml — set real "
                "long random keys (see docs/guides/VPS-DEPLOY.md).")
        if cfg.server.admin_key_is_placeholder():
            raise RuntimeError(
                "server.host is a public bind but server.admin_key is still "
                "a template placeholder — set a long random admin key first.")
        if not cfg.server.tls_cert:
            log.warning("serving PLAIN HTTP on a public bind: set server.tls_cert / "
                        "tls_key (built-in HTTPS) or put a TLS proxy in front")
        log.warning(
            "NON-LOCAL BIND (%s): the admin key is equivalent to code "
            "execution on this machine — keep it secret, prefer a reverse "
            "proxy with TLS, and leave allow_remote_admin=false unless the "
            "admin API must be reachable from non-loopback peers",
            cfg.server.host)

    app = FastAPI(title="Zcode Gateway", version=__version__, lifespan=lifespan)
    app.state.cfg = cfg

    # No CORS middleware on purpose: the dashboard is served same-origin and
    # every API client is a non-browser tool. `allow_origins=["*"]` let any
    # visited website read /dash (which embeds the admin key on loopback) and
    # then drive the admin API — a remote origin controlling a local tool.
    @app.middleware("http")
    async def cross_origin_mutation_guard(request: Request, call_next):
        """Block browser-driven cross-origin mutations (CSRF / DNS rebinding).

        Non-browser clients (curl, SDKs) send no Origin/Sec-Fetch headers and
        pass untouched. Same-origin browser calls pass. A malicious page can
        only send simple cross-site requests — those carry Origin and/or
        Sec-Fetch-Site: cross-site and are rejected before auth runs.

        same-site is rejected too (intentionally strict): the gateway has no
        legitimate cross-subdomain browser deployment. Origin "null"
        (sandboxed iframe / redirected POST) is treated as hostile, and the
        comparison includes the port (Origin http://127.0.0.1:9999 must not
        pass against Host 127.0.0.1:8788).
        """
        path = request.url.path
        privileged = path == "/dash" or path.startswith("/admin/") or path in ("/health", "/ready", "/metrics")
        if privileged:
            host = (request.url.hostname or "").lower()
            allowed_hosts = cfg.server.trusted_hosts()
            if host not in allowed_hosts:
                return JSONResponse(status_code=400, content={"detail": "untrusted Host header"})
        # /v1/* is bearer-authenticated inference (no ambient browser
        # credentials), so there is nothing for CSRF to ride on: browser-based
        # chat UIs and editor webviews must keep working there.
        if request.method in ("POST", "PUT", "PATCH", "DELETE") \
                and not request.url.path.startswith("/v1/"):
            site = request.headers.get("sec-fetch-site", "").lower()
            if site in ("cross-site", "same-site"):
                return JSONResponse(status_code=403, content={
                    "detail": "cross-site mutation blocked"})
            origin = request.headers.get("origin", "")
            if origin:
                parsed_origin = urlparse(origin)
                origin_netloc = _norm_netloc(parsed_origin.netloc,
                                             parsed_origin.scheme)
                # a TLS front may forward Host without the public port; the
                # browser's Origin always carries it, so also accept the
                # forwarded host the front reports
                allowed = {
                    _norm_netloc(request.headers.get(h) or "",
                                 request.headers.get("x-forwarded-proto")
                                 or request.url.scheme)
                    for h in ("host", "x-forwarded-host")
                } - {""}
                if not origin_netloc or origin_netloc == "null" \
                        or origin_netloc not in allowed:
                    return JSONResponse(status_code=403, content={
                        "detail": "cross-origin mutation blocked"})
        return await call_next(request)

    # dual-dialect surfaces
    app.include_router(openai_router)        # /v1/chat/completions, /v1/completions, /v1/models
    app.include_router(anthropic_router)     # /v1/messages, /v1/messages/count_tokens
    app.include_router(admin_router)         # /admin/*
    app.include_router(dash_router)          # /dash + /admin/dash/*
    app.include_router(health_router)        # /health, /ready, /metrics

    # Dashboard CSS/JS modules. Static assets only — no secrets are served from
    # here (the admin key is injected into the HTML shell, not the modules), so
    # these stay unauthenticated like the shell itself.
    from .api_dash import DASH_DIR
    _assets = DASH_DIR / "assets"
    if _assets.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/dash/assets", StaticFiles(directory=str(_assets)), name="dash-assets")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        # Auth/rate-limit failures raised in deps.py must reach /v1 clients in
        # their own dialect, not FastAPI's {"detail": ...} shape. Admin and
        # dashboard routes keep the plain detail (the dashboard reads it).
        status = exc.status_code
        detail = str(exc.detail)
        headers = getattr(exc, "headers", None)
        if request.url.path.startswith("/v1/messages"):
            code = {401: "invalid_api_key", 403: "permission_denied",
                    429: "rate_limit_exceeded"}.get(status, "invalid_request_error")
            return JSONResponse(status_code=status, headers=headers,
                                content=anthropic_error(status, code, detail))
        if request.url.path.startswith("/v1/"):
            code = {401: "invalid_api_key", 403: "permission_denied",
                    429: "rate_limit_exceeded"}.get(status, "invalid_request_error")
            return JSONResponse(status_code=status, headers=headers,
                                content=openai_error(status, code, detail))
        return JSONResponse(status_code=status, headers=headers,
                            content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def unhandled(request, exc):  # pragma: no cover
        log.exception("unhandled error on %s", request.url.path)
        # generic message + correlation id server-side: str(exc) can leak
        # paths, SQL errors and upstream URLs; and Anthropic-dialect callers
        # must not receive an OpenAI-shaped error envelope
        rid = uuid4().hex[:12]
        message = f"internal error (request id {rid})"
        if request.url.path.startswith("/v1/messages"):
            return JSONResponse(
                status_code=500,
                content=anthropic_error(500, "api_error", message))
        return JSONResponse(status_code=500,
                            content=openai_error(500, "internal_error", message))

    @app.get("/")
    async def root() -> dict:
        return {
            "name": "zcode-gateway",
            "version": __version__,
            "dialects": {
                "openai": ["POST /v1/chat/completions", "POST /v1/completions",
                           "GET /v1/models"],
                "anthropic": ["POST /v1/messages",
                              "POST /v1/messages/count_tokens"],
            },
            "admin": ["/admin/pool/state", "/admin/accounts", "/admin/stats"],
            "ops": ["/health", "/ready", "/metrics"],
        }

    return app
