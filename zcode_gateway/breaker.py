"""IP-wide CDN/WAF edge-block breaker.

The upstream's CDN occasionally answers every request from our egress IP with
an HTML 403 (captured 2026-09-16: "Your client does not have permission to get
URL /api/v1/chat/completions from this server"). That block is:

  * IP-wide — it has NOTHING to do with any individual account;
  * invisible to status-code routing — it is HTML, not the API's JSON errors.

Without a breaker, the 403 path parks EVERY account it touches, 300s at a
time, request after request, until the whole pool is drained for a condition
no account caused. This module implements the stop-probe-resume discipline:

  * `note_edge_block()`   — the hot path calls it when an HTML 403 is seen;
  * `check()`             — fail fast with 503 + Retry-After while blocked;
  * a background prober   — polls a cheap upstream endpoint with exponential
                            backoff (60s -> 30min) and auto-resumes;
  * `/health`-style state — {blocked, state, retryIn, consecutiveBlocks}.

Detection is by BODY MARKER, not status: the CDN returns 403, but the same
edge could answer 429 or 502 with the same HTML. `upstream._looks_like_edge_block`
is the single source of truth for the marker match.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from .upstream import _looks_like_edge_block

log = logging.getLogger("zcode_gateway.breaker")

# probe backoff: 60s doubling to 30min, with jitter so multiple gateways
# (or a restart loop) don't synchronized-hammer the probe
PROBE_MIN_S = 60.0
PROBE_MAX_S = 1800.0

BLOCK_RETRY_AFTER_S = 35        # advertised to clients while blocked
CLEAN_AFTER_S = 900             # ignore stale blocks older than 15 min


class EdgeBlockBreaker:
    """Shared across ALL accounts: one egress IP, one breaker."""

    def __init__(self):
        self.blocked_at: float = 0.0
        self.consecutive: int = 0
        self.last_probe_at: float = 0.0
        self.next_probe_at: float = 0.0
        self._probe_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # optional callback: fired once per block episode with the consecutive
        # count — used by ChatService to push a notification
        self.on_block = None

    # -- hot path ----------------------------------------------------------- #

    def is_blocked(self) -> bool:
        if not self.blocked_at:
            return False
        if time.time() - self.blocked_at > CLEAN_AFTER_S:
            # stale (prober died with the event loop, or a transient blip):
            # expire rather than wedge the gateway for eternity
            self._reset()
            return False
        return True

    def note_edge_block(self, body: str | bytes | None = None) -> bool:
        """Record an edge block. Returns True when the body matched (callers
        use the return to skip their normal per-account 403 handling)."""
        if not _looks_like_edge_block(body):
            return False
        now = time.time()
        self.blocked_at = now
        self.consecutive += 1
        log.warning(
            "edge block detected (HTML 403, #%d) — failing fast %ds, probing",
            self.consecutive, BLOCK_RETRY_AFTER_S)
        if self.on_block is not None:
            try:
                self.on_block(self.consecutive)
            except Exception:
                pass
        return True

    def note_success(self) -> None:
        """Any 200 through the upstream is evidence the block lifted."""
        if self.blocked_at:
            log.info("edge block lifted after %ds (%d consecutive)",
                     int(time.time() - self.blocked_at), self.consecutive)
        self._reset()

    def _reset(self) -> None:
        self.blocked_at = 0.0
        self.consecutive = 0
        self.next_probe_at = 0.0

    # -- client surface ------------------------------------------------------ #

    def check(self) -> dict | None:
        """Return an error payload when requests must fail fast, else None."""
        if not self.is_blocked():
            return None
        retry_in = max(1, int(BLOCK_RETRY_AFTER_S
                              - (time.time() - self.blocked_at) % BLOCK_RETRY_AFTER_S))
        return {
            "status": 503,
            "code": "waf_block",
            "message": ("upstream edge block (IP-wide): requests would be "
                        "rejected for every account; retry shortly"),
            "retry_in_s": retry_in,
        }

    def status(self) -> dict:
        return {
            "blocked": self.is_blocked(),
            "state": "blocked" if self.is_blocked() else "open",
            "retry_in_s": (max(0, int(BLOCK_RETRY_AFTER_S - (time.time() - self.blocked_at)))
                           if self.is_blocked() else 0),
            "consecutive_blocks": self.consecutive,
        }

    # -- background prober ---------------------------------------------------- #

    async def _probe_loop(self, probe):
        """`probe` is an async callable returning (status, body) of a cheap
        upstream GET. An edge-block body keeps the block; anything else
        (including a normal API error like 401 JSON) means the CDN is no
        longer intercepting, so we resume."""
        backoff = PROBE_MIN_S
        while self.is_blocked():
            await asyncio.sleep(max(0.0, self.next_probe_at - time.time()))
            if not self.is_blocked():
                break
            try:
                status, body = await probe()
                if _looks_like_edge_block(body):
                    backoff = min(backoff * 2, PROBE_MAX_S)
                    self.next_probe_at = time.time() + backoff * random.uniform(0.8, 1.2)
                    log.info("edge probe: still blocked (HTTP %s); next probe in %ds",
                             status, int(self.next_probe_at - time.time()))
                else:
                    log.info("edge probe: HTTP %s, not an edge block — resuming", status)
                    self.note_success()
                    return
            except Exception as exc:
                # transport errors are ambiguous (our net vs the block):
                # keep probing on backoff, never resume on an exception
                backoff = min(backoff * 2, PROBE_MAX_S)
                self.next_probe_at = time.time() + backoff
                log.debug("edge probe error (%s); next in %ds",
                          type(exc).__name__, int(backoff))

    def ensure_prober(self, probe):
        """Start the background prober exactly once per block episode."""
        if self._probe_task is None or self._probe_task.done():
            self.next_probe_at = time.time() + PROBE_MIN_S
            self._probe_task = asyncio.get_running_loop().create_task(
                self._probe_loop(probe))

    async def aclose(self):
        if self._probe_task is not None:
            self._probe_task.cancel()
            try:
                await self._probe_task
            except (asyncio.CancelledError, Exception):
                pass
            self._probe_task = None
