"""Weekend Build window: Saturday 00:00 -> Monday 00:00 Beijing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from zcode_gateway.campaign_window import weekend_window

BEIJING = timezone(timedelta(hours=8))


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=BEIJING).timestamp()


def test_open_through_the_beijing_weekend():
    state = weekend_window(at(2026, 11, 14, 10))       # Saturday 10:00
    assert state["active"] is True
    assert state["starts_at"] == at(2026, 11, 14, 0)
    assert state["ends_at"] == at(2026, 11, 16, 0)     # Monday 00:00


def test_sunday_night_is_still_open_monday_is_not():
    assert weekend_window(at(2026, 11, 15, 23, 59))["active"] is True
    monday = weekend_window(at(2026, 11, 16, 0, 0))
    assert monday["active"] is False
    assert monday["next_starts_at"] == at(2026, 11, 21, 0)


def test_weekday_is_closed_and_points_at_the_next_saturday():
    state = weekend_window(at(2026, 11, 11, 12))       # Wednesday
    assert state["active"] is False
    assert state["next_starts_at"] == at(2026, 11, 14, 0)


def test_friday_before_midnight_still_points_forward():
    state = weekend_window(at(2026, 11, 13, 23, 59))
    assert state["active"] is False
    assert state["next_starts_at"] == at(2026, 11, 14, 0)


def test_offset_is_respected():
    # Same instant, viewed from UTC: Saturday 10:00 Beijing is 02:00 UTC.
    state = weekend_window(at(2026, 11, 14, 10), utc_offset_hours=0)
    assert state["active"] is True
