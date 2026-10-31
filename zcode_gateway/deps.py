"""FastAPI dependencies: auth and state access."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from .peer import peer_is_local
from .state import AppState

def _is_loopback(request: Request) -> bool:
    client = request.client.host if request.client else ""
    return client in ("127.0.0.1", "::1", "localhost")


def _compare(a: str, b: str) -> bool:
    """Constant-time compare that tolerates non-ASCII strings.

    hmac.compare_digest raises TypeError on non-ASCII str (a latin-1-decoded
    header byte >= 0x80), turning a malformed bearer into a pre-auth 500.
    Compare UTF-8 bytes instead.
    """
    try:
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    except Exception:
        return False


def _is_internal(request: Request, state: AppState, supplied: str) -> bool:
    if not supplied:
        return False
    return _is_loopback(request) and bool(state.internal_token) and _compare(state.internal_token, supplied)


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # anthropic clients may send x-api-key
    return request.headers.get("x-api-key", "").strip()


async def client_key(request: Request) -> str:
    """Validate a client key. Localhost + require_client_key=false lets anything through."""
    state = get_state(request)
    if getattr(state, "update_phase", "idle") == "applying":
        raise HTTPException(status_code=503, detail="Gateway is preparing an update; retry after restart", headers={"Retry-After": "15"})
    cfg = state.cfg.server
    if not cfg.require_client_key:
        return "anonymous"

    supplied = _bearer(request)
    if not supplied:
        raise HTTPException(status_code=401, detail="missing API key")

    # Admin key or internal sentinel can also authenticate client routes
    if _has_credential(state, supplied) or _is_internal(request, state, supplied):
        return "admin"

    for entry in cfg.client_keys:
        if _compare(entry.key, supplied):
            if entry.rpm > 0:
                allowed, retry_after = state.limiter.check(entry.key, entry.rpm)
                if not allowed:
                    raise HTTPException(
                        status_code=429,
                        detail="rate limit exceeded for this API key",
                        headers={"Retry-After": str(int(retry_after) + 1)},
                    )
            return entry.name
    raise HTTPException(status_code=401, detail="invalid API key")


def _has_credential(state: AppState, supplied: str) -> bool:
    """Constant-time secret check that CANNOT succeed on an empty secret.

    A missing bearer arrives here as ""; if the configured secret were also
    "" (valid via config/env), compare_digest(b"", b"") would authenticate
    every unauthenticated request. An empty configured secret authenticates
    nothing — the request is rejected below instead.
    """
    if not supplied or not state.cfg.server.admin_key:
        return False
    return _compare(state.cfg.server.admin_key, supplied)


async def admin_key(request: Request) -> str:
    state = get_state(request)
    cfg = state.cfg.server
    # The admin surface answers local peers only unless the operator
    # explicitly opted in (server.allow_remote_admin). Peer, not bind: on the
    # recommended VPS topology (gateway loopback, same-box Caddy in front)
    # every connection arrives from 127.0.0.1 — peer.py resolves the real
    # visitor from the proxy-corrected request.client (uvicorn applies
    # X-Forwarded-For for trusted loopback proxies).
    if not cfg.allow_remote_admin and not peer_is_local(request):
        raise HTTPException(
            status_code=403,
            detail="admin is local-only; set server.allow_remote_admin: true "
                   "to reach it from other machines")
    supplied = _bearer(request)
    if _is_internal(request, state, supplied) or _has_credential(state, supplied):
        return "admin"
    raise HTTPException(status_code=403, detail="invalid admin key")


async def internal_or_admin(request: Request) -> str:
    """For the few endpoints spawned generator workers call with the per-run
    internal token (they don't hold the admin key).

    Unlike _is_internal this accepts the token from ANY peer, on purpose: with
    built-in TLS the children call back through the certificate's public
    address (server.child_origin / SAN), so their connection is not loopback.
    The token is a random per-run secret and only these two routes use this
    dependency (notifications/emit, generator/send_claim_message); the admin
    key keeps its local-only / allow_remote_admin rule."""
    state = get_state(request)
    supplied = _bearer(request)
    if supplied and state.internal_token and _compare(state.internal_token, supplied):
        return "internal"
    if _has_credential(state, supplied) and (state.cfg.server.allow_remote_admin or peer_is_local(request)):
        return "internal"
    raise HTTPException(status_code=403, detail="invalid internal token")
