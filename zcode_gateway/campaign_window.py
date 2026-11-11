"""Weekend Build campaign window.

The card is an upstream drop: it exists only while the marketing feed lists
it, and the feed is impression-deduped per account, so probing it just to
light up a button would consume the card for that account. The drop cadence
is fixed though — Saturday 00:00 to Monday 00:00 Beijing — so the dashboard
gates the per-account claim buttons on that window and does not touch the
feed until the user actually claims.

Fixed UTC offsets on purpose: Windows has no IANA database.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

__all__ = ["weekend_window"]

WEEKEND_START_WEEKDAY = 5      # Saturday (Mon = 0)
WEEKEND_LENGTH_DAYS = 2        # Saturday 00:00 -> Monday 00:00


def _epoch(local: datetime, utc_offset_hours: float) -> float:
    return (local - timedelta(hours=utc_offset_hours)).replace(
        tzinfo=timezone.utc).timestamp()


def weekend_window(now: float | None = None,
                   utc_offset_hours: float = 8.0) -> dict:
    """State of the Beijing-weekend campaign window around `now`.

    `starts_at`/`ends_at` are the current (or most recent) window; when the
    window is closed, `next_starts_at` is the next opening. Epoch seconds so
    the browser renders them in the user's own timezone.
    """
    now = time.time() if now is None else now
    local = (datetime.fromtimestamp(now, tz=timezone.utc)
             + timedelta(hours=utc_offset_hours))
    days_since = (local.weekday() - WEEKEND_START_WEEKDAY) % 7
    start = (local - timedelta(days=days_since)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=WEEKEND_LENGTH_DAYS)
    active = start <= local < end
    next_start = start if start > local else start + timedelta(days=7)
    return {
        "active": active,
        "starts_at": _epoch(start, utc_offset_hours),
        "ends_at": _epoch(end, utc_offset_hours),
        "next_starts_at": _epoch(next_start, utc_offset_hours),
        "utc_offset_hours": utc_offset_hours,
    }
