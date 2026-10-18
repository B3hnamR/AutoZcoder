"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
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
        probe_unknown=cfg.models.probe_unknown,
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
    tokens = TokenManager(cfg, pool)
    client = UpstreamClient(cfg.upstream, relay_manager=relay_manager)
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

    # release feed: one GitHub call on startup, then every interval; the
    # dashboard polls this cache, never GitHub directly
    from .updater import UpdateChecker
    updater = UpdateChecker(cfg.update.repo, __version__,
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
        store.close()


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    setup_logging(cfg.logging.level)

    app = FastAPI(title="Zcode Gateway", version=__version__, lifespan=lifespan)
    app.state.cfg = cfg

    # No CORS middleware on purpose: the dashboard is served same-origin and
    # every API client is a non-browser tool. `allow_origins=["*"]` let any
    # visited website read /dash (which embeds the admin key on loopback) and
    # then drive the admin API — a remote origin controlling a local tool.

    # dual-dialect surfaces
    app.include_router(openai_router)        # /v1/chat/completions, /v1/completions, /v1/models
    app.include_router(anthropic_router)     # /v1/messages, /v1/messages/count_tokens
    app.include_router(admin_router)         # /admin/*
    app.include_router(dash_router)          # /dash + /admin/dash/*
    app.include_router(health_router)        # /health, /ready, /metrics

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
