"""Notification center: bounded in-memory event bus for the dashboard.

Every operational signal worth a human's attention (token expiring, pool
empty, edge block, relay cooled, generator failure) used to live and die in
a log line. This bus collects them with severity + category so the dashboard
bell and the Notifications panel can show what happened while the user wasn't
watching.

Design constraints:
  * writes come from ANY thread (generator workers, watchdogs) — only
    deque.append, which is atomic under the GIL;
  * bounded: oldest events fall off (a personal tool does not need a DB
    table of notifications);
  * ids are monotonic so the UI can poll with ?since=<last_id>.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque

_LEVELS = ("info", "success", "warning", "error")

# dedupe windows per (category, message-shape): a condition that fires every
# poll cycle must not flood the bell — identical events inside the window
# bump `count` on the existing entry instead of appending a new one
_DEDUPE_S = {
    "pool_empty": 300,
    "edge_block": 120,
    "relay_cooled": 300,
    "token_expiring": 3600,
    "account_dead": 0,
    "generator": 0,
    "update": 0,
    "quota": 0,
    "default": 60,
}


class NotificationCenter:
    def __init__(self, capacity: int = 200) -> None:
        self._events: deque[dict] = deque(maxlen=capacity)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def emit(self, category: str, message: str, level: str = "info",
             data: dict | None = None) -> int:
        """Record an event. Thread-safe; never raises."""
        try:
            with self._lock:
                window = _DEDUPE_S.get(category, _DEDUPE_S["default"])
                if window:
                    key = (category, message)
                    now = time.time()
                    for e in reversed(self._events):
                        if now - e["ts"] > window:
                            break
                        if (e["category"], e["message"]) == key:
                            e["count"] += 1
                            e["ts"] = now
                            e["level"] = level
                            return e["id"]
                eid = next(self._ids)
                self._events.append({
                    "id": eid,
                    "ts": time.time(),
                    "category": category,
                    "level": level if level in _LEVELS else "info",
                    "message": str(message)[:300],
                    "data": data or {},
                    "count": 1,
                })
                return eid
        except Exception:
            return 0

    def list(self, since_id: int = 0, limit: int = 100) -> list[dict]:
        """Events with id > since_id, oldest first."""
        with self._lock:
            return [dict(e) for e in self._events if e["id"] > since_id][-limit:]

    def unread_state(self) -> dict:
        """Bell state: total + counts by level of the last 24h."""
        cutoff = time.time() - 86400
        with self._lock:
            recent = [e for e in self._events if e["ts"] >= cutoff]
        return {
            "total_24h": len(recent),
            "errors_24h": sum(1 for e in recent if e["level"] == "error"),
            "warnings_24h": sum(1 for e in recent if e["level"] == "warning"),
            "last": ({"id": recent[-1]["id"], "message": recent[-1]["message"],
                      "level": recent[-1]["level"], "ts": recent[-1]["ts"]}
                     if recent else None),
        }

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
