"""Conversation affinity: keep one chat on one account for a short window.

A multi-turn conversation that hops accounts loses the thread (the new account
never saw the earlier turns) and the upstream prompt cache with it. The client
is the only party that knows it is the same chat, so the session id it sends
becomes the key.

Deliberately in-memory and bounded: entries expire, a stale binding is ignored
when the account is no longer eligible, and a restart simply re-binds. Nothing
is persisted, so an account swap never strands a conversation on a dead one.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

__all__ = ["AffinityMap", "affinity_key_from_payload"]


def affinity_key_from_payload(payload: dict) -> str:
    """Stable key for the conversation, or '' when the client sent nothing.

    Anthropic clients put a JSON `metadata.user_id` on the request; Claude Code
    includes `session_id` inside it. OpenAI clients may send `user`, and some
    wrappers send `conversation_id`/`session_id` at the top level.
    """
    if not isinstance(payload, dict):
        return ""
    ident = ""
    meta = payload.get("metadata")
    if isinstance(meta, dict):
        raw = meta.get("user_id")
        if isinstance(raw, str) and raw.strip():
            ident = raw.strip()
            try:
                parsed = json.loads(ident)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                ident = str(parsed.get("session_id")
                            or parsed.get("conversation_id")
                            or parsed.get("device_id")
                            or ident).strip()
    if not ident:
        for field in ("conversation_id", "session_id", "user"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                ident = value.strip()
                break
    if not ident:
        return ""
    # hash: the routing table must not hold user identifiers in memory/dumps
    return hashlib.sha256(ident.encode("utf-8", "ignore")).hexdigest()[:32]


class AffinityMap:
    """session key -> (account id, expires at). Bounded, thread-safe, no I/O."""

    def __init__(self, ttl_seconds: float = 600.0, capacity: int = 2048,
                 clock=time.monotonic) -> None:
        self.ttl = max(0.0, float(ttl_seconds))
        self.capacity = max(1, int(capacity))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, float]] = {}

    def enabled(self) -> bool:
        return self.ttl > 0

    def get(self, key: str) -> str | None:
        if not key or not self.enabled():
            return None
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            account_id, expires_at = entry
            if expires_at <= now:
                self._entries.pop(key, None)
                return None
            return account_id

    def bind(self, key: str, account_id: str) -> None:
        if not key or not account_id or not self.enabled():
            return
        now = self._clock()
        with self._lock:
            self._entries[key] = (account_id, now + self.ttl)
            if len(self._entries) > self.capacity:
                self._prune(now)

    def drop(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def drop_account(self, account_id: str) -> None:
        with self._lock:
            for key, (bound, _) in list(self._entries.items()):
                if bound == account_id:
                    self._entries.pop(key, None)

    def _prune(self, now: float) -> None:
        """Drop expired entries first, then the oldest, until under capacity."""
        for key, (_, expires_at) in list(self._entries.items()):
            if expires_at <= now:
                self._entries.pop(key, None)
        overflow = len(self._entries) - self.capacity
        if overflow > 0:
            oldest = sorted(self._entries.items(), key=lambda kv: kv[1][1])[:overflow]
            for key, _ in oldest:
                self._entries.pop(key, None)

    def __len__(self) -> int:
        return len(self._entries)
