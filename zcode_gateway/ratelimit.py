"""Per-client-key rate limiting: sliding-window RPM, enforced at auth time."""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Sliding-window requests-per-minute limiter, keyed by client key."""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window = window_seconds
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, rpm: int) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds). rpm <= 0 means unlimited."""
        if rpm <= 0:
            return True, 0.0

        # monotonic: wall-clock jumps (NTP, VM resume) either cleared the
        # window early or stretched retry_after past the window size
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.setdefault(key, deque())
            cutoff = now - self.window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= rpm:
                retry_after = self.window - (now - bucket[0])
                return False, max(retry_after, 0.0)
            bucket.append(now)
            return True, 0.0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)