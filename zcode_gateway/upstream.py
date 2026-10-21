"""Upstream layer: fingerprint headers, body variants, HTTP client, error model.

All shapes here come from the Phase-1 capture. Do not "improve" the header set
or the body variants without re-capturing — the upstream fingerprints the client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator

import httpx

from .config import UpstreamConfig

log = logging.getLogger("zcode_gateway.upstream")

# --------------------------------------------------------------------------- #
# headers
# --------------------------------------------------------------------------- #

# The 16 static headers observed on every captured call. `authorization` and
# `x-task-id` are filled per request; `host` and `content-length` are set by httpx.
STATIC_HEADERS = {"accept": "*/*", "content-type": "application/json",
                  "http-referer": "https://zcode.z.ai",
                  "anthropic-version": "2023-06-01",
                  "anthropic-beta": "mid-conversation-system-2026-04-07"}
REQUIRED_HEADER_NAMES = set(STATIC_HEADERS) | {"authorization", "x-api-key", "x-device-mid",
    "user-agent", "x-zcode-app-version", "x-zcode-agent"}
VARIANTS = ("anthropic",)

def new_task_id():
    return str(uuid.uuid4())

def merge_anthropic_beta(base: str, extra: str | None) -> str:
    """Comma-merge the captured static beta with a client-supplied one.

    A client sending its own `anthropic-beta` used to be silently overridden
    by the static captured value; when Z.ai rotates the beta, clients that
    already know the new one had no way to pass it through. Order-stable,
    dedupes, keeps our captured beta first.
    """
    parts = [p.strip() for p in (base or '').split(',') if p.strip()]
    for p in (extra or '').split(','):
        p = p.strip()
        if p and p not in parts:
            parts.append(p)
    return ','.join(parts)


def build_headers(access_token, fp, task_id=None, *, device_mid=None, include_device_mid=True,
                  anthropic_beta: str | None = None):
    token = access_token.removeprefix("Bearer ").strip()
    h = dict(STATIC_HEADERS)
    if anthropic_beta:
        h["anthropic-beta"] = merge_anthropic_beta(
            STATIC_HEADERS.get("anthropic-beta", ""), anthropic_beta)
    h.update({"authorization": "Bearer " + token, "x-api-key": token,
        "user-agent": fp.user_agent, "x-device-mid": device_mid or fp.device_mid,
        "x-client-language": fp.language, "x-client-timezone": fp.timezone,
        "x-os-category": fp.os_category, "x-os-version": fp.os_version,
        "x-platform": fp.platform, "x-release-channel": "production",
        "x-title": "Z Code@electron", "x-zcode-agent": "glm",
        "x-zcode-app-version": fp.client_version, "x-zcode-session-type": "main",
        "x-query-id": new_task_id(), "x-request-id": new_task_id(),
        "x-zcode-trace-id": new_task_id(), "x-session-id": task_id or new_task_id()})
    # The model captures carry the device in metadata, while billing/OAuth
    # requests carry X-Device-Mid. Keep those two observed header sets distinct.
    if not include_device_mid:
        h.pop('x-device-mid')
    return h

def build_upstream_body(payload, variant="anthropic", default_max_tokens=128000,
                        anthropic_cache_control=True, model_caps=None,
                        default_effort='high'):
    from .zcode_protocol import build_body
    return build_body(payload, default_max_tokens, model_caps=model_caps,
                      default_effort=default_effort)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class ErrorKind(str, Enum):
    OK = "ok"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    UNAUTHORIZED = "unauthorized"
    ENTITLEMENT = "entitlement"     # 403: no plan/subscription for THIS model
    EDGE_BLOCK = "edge_block"       # 403 HTML from the CDN/edge, not the API
    RATE_LIMITED = "rate_limited"
    SERVER = "server"
    CLIENT = "client"
    TRANSPORT = "transport"


# Codes that mean "this account cannot use this model", not "this account is bad".
ENTITLEMENT_CODES = frozenset({
    "ENTITLEMENT_ERROR",
    "NOT_SUBSCRIBED",
    "PLAN_REQUIRED",
    "MODEL_NOT_IN_PLAN",
})


def is_entitlement_code(code: str) -> bool:
    """Single source of truth for entitlement classification.

    Three call sites used to classify independently (this frozenset plus two
    substring checks), and they had already drifted: NOT_SUBSCRIBED matched
    here but not the substring test. Accepts exact codes and any code
    containing ENTITLEMENT/SUBSCRIB/PLAN (covers future plan-gate codes).
    """
    c = (code or "").upper()
    if not c:
        return False
    if c in ENTITLEMENT_CODES:
        return True
    return ("ENTITLEMENT" in c) or ("SUBSCRIB" in c) or ("PLAN" in c)


# Abuse-heuristic rejection: HTTP 405 + code 3012, message
# "request has been blocked due to unusual activity." It is neither a quota nor
# an account defect, and it is scoped to one account AND one model at a time —
# the same account keeps serving other models (captured: GLM-5.3 blocked while
# GLM-5.3-Flash still returns 200). Treated like a per-model park: the account
# is skipped for that model and the request rotates to the next account.
BLOCKED_CODES = frozenset({"3012", "405", "HTTP_405"})


def is_block_code(code: str, status: int = 0) -> bool:
    if status == 405:
        return True
    c = str(code or "").upper()
    return c in BLOCKED_CODES or "3012" in c or "405" in c


def _error_code(body: str | bytes | None) -> str:
    """Best-effort extraction of error.code from a JSON error body."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else (body or "")
    try:
        parsed = json.loads(text)
    except Exception:
        return ""
    err = parsed.get("error", parsed)
    if isinstance(err, dict):
        return str(err.get("code") or err.get("statusCode") or "").upper()
    return ""


# Captured 2026-09-16: a global 403 served as HTML by Google's front end, for every
# model regardless of entitlement:
#   <html>...<title>403 Forbidden</title>...
#   <h2>Your client does not have permission to get URL
#       <code>/api/v1/chat/completions</code> from this server.</h2>
# This is infrastructure, not the API, and not the account's fault.
EDGE_BLOCK_MARKERS = (
    "does not have permission to get url",
    "403 forbidden",
    "error: forbidden",
)


def _looks_like_edge_block(body: str | bytes | None) -> bool:
    text = (body.decode("utf-8", "replace") if isinstance(body, bytes)
            else (body or "")).lower()
    if "<html" not in text:
        return False
    return any(marker in text for marker in EDGE_BLOCK_MARKERS)


def classify(status: int, body: str | bytes | None = None) -> ErrorKind:
    info = parse_upstream_error(status, body)
    if info["code"] == "11113":
        return ErrorKind.INSUFFICIENT_CREDITS
    if status == 200:
        return ErrorKind.OK
    if status == 401:
        return ErrorKind.UNAUTHORIZED
    if status == 403:
        if is_entitlement_code(_error_code(body)):
            return ErrorKind.ENTITLEMENT
        if _looks_like_edge_block(body):
            return ErrorKind.EDGE_BLOCK
        return ErrorKind.CLIENT
    if status == 402:
        return ErrorKind.INSUFFICIENT_CREDITS
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if 500 <= status < 600:
        return ErrorKind.SERVER
    return ErrorKind.CLIENT


def edge_block_message(body: str | bytes | None) -> str:
    """Short, human-readable summary of an edge block (bodies are HTML)."""
    text = (body.decode("utf-8", "replace") if isinstance(body, bytes)
            else (body or ""))
    lowered = text.lower()
    if "does not have permission to get url" in lowered:
        return ("upstream edge rejected the request (403): the client has no "
                "permission for this URL")
    return "upstream edge rejected the request (403 Forbidden)"


def parse_upstream_error(status: int, body: str | bytes | None) -> dict[str, Any]:
    """Normalise an upstream error body (captured shape) into a dict."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else (body or "")
    try:
        parsed = json.loads(text)
        err = parsed.get("error", parsed)
        if isinstance(err, dict):
            return {
                "code": str(err.get("code") or err.get("statusCode") or err.get("type") or f"http_{status}"),
                "message": err.get("message") or err.get("msg") or text[:400],
                "raw": parsed,
            }
    except Exception:
        pass
    return {"code": f"http_{status}", "message": text[:400], "raw": None}


# Mapping for the OpenAI-compatible error envelope.
ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "insufficient_quota",
    403: "permission_error",
    404: "not_found_error",
    413: "invalid_request_error",
    429: "rate_limit_error",
    500: "server_error",
    502: "server_error",
    503: "server_error",
    504: "server_error",
}


def openai_error(status: int, code: str, message: str,
                 extra: dict[str, Any] | None = None) -> dict[str, Any]:
    error = {
        "message": message,
        "type": ERROR_TYPE_BY_STATUS.get(status, "api_error"),
        "param": None,
        "code": code,
    }
    if extra:
        error.update(extra)
    return {"error": error}


def anthropic_error(status: int, code: str, message: str,
                    extra: dict[str, Any] | None = None) -> dict[str, Any]:
    # Anthropic error types: invalid_request_error, authentication_error,
    # permission_error, not_found_error, request_too_large, rate_limit_error,
    # api_error, overloaded_error
    mapping = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        413: "request_too_large",
        429: "rate_limit_error",
    }
    if status >= 500:
        etype = "overloaded_error" if status == 529 else "api_error"
    else:
        etype = mapping.get(status, "api_error")
    error: dict[str, Any] = {"type": etype, "message": message}
    # Anthropic's envelope has no standard code field, but clients doing
    # programmatic retry need the machine-readable code the OpenAI dialect
    # already exposes — carry it as an additive field (ignored by strict clients)
    if code:
        error["code"] = code
    if extra:
        error.update(extra)
    return {"type": "error", "error": error}


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #


@dataclass
class UpstreamResponse:
    status_code: int
    headers: dict[str, str]
    _resp: httpx.Response

    async def aread(self) -> bytes:
        return await self._resp.aread()

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        return self._resp.aiter_bytes()

    async def aclose(self) -> None:
        await self._resp.aclose()


class UpstreamClient:
    """Thin httpx wrapper. Adds nothing to the request the caller did not set.

    A GLOBAL randomized pacing gate spaces out upstream sends (config
    upstream.pacing): one egress IP hammering the CDN with zero gap is the
    traffic shape edge blocks are trained on. The gate is shared across all
    accounts and workers, randomized so parallel requests don't
    self-synchronize into lockstep.
    """

    _last_send_at: float = 0.0            # class-level: shared per process
    _pace_lock: asyncio.Lock | None = None

    def __init__(self, cfg: UpstreamConfig, relay_manager: Any = None,
                 pacing=None) -> None:
        self.cfg = cfg
        self.relay_manager = relay_manager
        self.pacing = pacing               # PacingConfig or None = off
        if UpstreamClient._pace_lock is None:
            UpstreamClient._pace_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout_read, connect=cfg.timeout_connect),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            headers={},          # never inject a default User-Agent
            follow_redirects=False,
            http2=False,
        )

    async def _pace(self) -> None:
        """Hold the shared lock for a randomized gap after the previous send.
        Waiting INSIDE the lock is what serializes the gap: concurrent callers
        queue, and each observes the gap of the one before it."""
        if self.pacing is None or not getattr(self.pacing, 'enabled', False):
            return
        import random as _random
        gap = _random.uniform(self.pacing.min_gap_ms / 1000.0,
                              self.pacing.max_gap_ms / 1000.0)
        async with UpstreamClient._pace_lock:
            elapsed = time.monotonic() - UpstreamClient._last_send_at
            wait = gap - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            UpstreamClient._last_send_at = time.monotonic()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, headers: dict[str, str], body: dict[str, Any],
                   stream: bool) -> UpstreamResponse:
        # Rotation: get_relay_for_model applies the strategy (single /
        # round_robin / random) and skips cooled-down relays.
        tried_relay_urls: list[str] = []
        max_relay_tries = 1
        if self.relay_manager and self.relay_manager.get_relay_count() > 1:
            max_relay_tries = min(3, self.relay_manager.get_relay_count())

        last_exc: Exception | None = None
        for attempt in range(max_relay_tries):
            await self._pace()
            relay_url = None
            if self.relay_manager:
                req_model = str(body.get("model") or "")
                relay_url = self.relay_manager.get_relay_for_model(req_model)
                if relay_url and relay_url in tried_relay_urls:
                    # already tried this one this call — stop rotating
                    relay_url = None if len(tried_relay_urls) >= self.relay_manager.get_relay_count() else relay_url
                    if relay_url is None:
                        break

            try:
                if relay_url:
                    import urllib.parse
                    parsed = urllib.parse.urlparse(self.cfg.chat_url)
                    target = f"{parsed.scheme}://{parsed.netloc}"
                    relay_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")

                    fwd_headers = dict(headers)
                    fwd_headers["x-relay-target"] = target
                    fwd_headers["x-relay-path"] = relay_path
                    req = self._client.build_request(
                        "POST", relay_url, headers=fwd_headers, json=body,
                    )
                else:
                    if attempt > 0:
                        break  # no relay path left — re-raise the last error
                    req = self._client.build_request(
                        "POST", self.cfg.chat_url, headers=headers, json=body,
                    )
                resp = await self._client.send(req, stream=stream)
            except httpx.HTTPError as exc:
                # relay (or direct) transport failure: mark + rotate
                if relay_url and self.relay_manager:
                    self.relay_manager.mark_relay_failed(relay_url)
                    tried_relay_urls.append(relay_url)
                    last_exc = TransportError(str(exc))
                    continue
                raise TransportError(str(exc)) from exc

            if relay_url and resp.status_code >= 500 and self.relay_manager:
                # the relay itself is unhappy — cool it and try the next one
                await resp.aclose()
                self.relay_manager.mark_relay_failed(relay_url)
                tried_relay_urls.append(relay_url)
                last_exc = TransportError(f"relay {relay_url} returned {resp.status_code}")
                continue

            if relay_url and resp.status_code == 405 and self.relay_manager:
                # 405/3012 through a relay = the RELAY'S exit IP is flagged
                # by risk control ("unusual activity"). Rotating ACCOUNTS
                # cannot help — every account goes through the same relay —
                # so cool this relay and immediately try the next one before
                # the error ever reaches the account layer. The 405 body is
                # a small JSON error (never a real stream), so reading it
                # here is safe on this branch.
                body_peek = await resp.aread()
                await resp.aclose()
                if b"3012" in body_peek or b"unusual activity" in body_peek:
                    self.relay_manager.mark_relay_failed(relay_url)
                    tried_relay_urls.append(relay_url)
                    last_exc = UpstreamBlockThroughRelay(
                        f"relay {relay_url} exit IP flagged (405/3012)")
                    continue
                # plain 405 without 3012: a request-shape block, not the
                # relay's IP — fall through to the caller (account layer)
                synthetic = httpx.Response(status_code=405, content=body_peek,
                                           headers=dict(resp.headers))
                return UpstreamResponse(status_code=405,
                                        headers={k.lower(): v for k, v in resp.headers.items()},
                                        _resp=synthetic)

            if relay_url and self.relay_manager:
                self.relay_manager.mark_relay_ok(relay_url)
            return UpstreamResponse(
                status_code=resp.status_code,
                headers={k.lower(): v for k, v in resp.headers.items()},
                _resp=resp,
            )

        if last_exc is not None:
            raise last_exc
        raise TransportError("no relay attempt succeeded")


class TransportError(Exception):
    """Network-level failure — safe to retry on another account."""


class UpstreamBlockThroughRelay(Exception):
    """405/3012 'unusual activity' arrived THROUGH a relay: the relay's exit
    IP is flagged, not the account. The client layer must rotate the RELAY
    (already done in UpstreamClient.send) rather than park the account."""


# --------------------------------------------------------------------------- #
# SSE helpers
# --------------------------------------------------------------------------- #


def iter_sse_events(chunk_text):
    events = []
    for frame in __import__('re').split(r'\r\n\r\n|\n\n|\r\r', chunk_text):
        lines = [line[5:].lstrip(' ') for line in frame.splitlines() if line.startswith('data:')]
        if not lines: continue
        data = '\n'.join(lines)
        if data == '[DONE]':
            events.append(data)
            continue
        try: events.append(json.loads(data))
        except json.JSONDecodeError: pass
    return events


def sse_frame(obj: dict[str, Any]) -> bytes:
    return ("data: " + json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
            + "\n\n").encode("utf-8")
