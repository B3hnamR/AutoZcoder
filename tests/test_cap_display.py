"""Reset displays must preserve the instant while using the viewer's timezone."""
from datetime import datetime, timedelta, timezone

import pytest

from zcode_gateway import gui


class TehranDateTime(datetime):
    def astimezone(self, tz=None):
        return super().astimezone(tz or timezone(timedelta(hours=3, minutes=30)))


def test_cap_local_date_rollover_and_countdown(monkeypatch):
    monkeypatch.setattr(gui, "datetime", TehranDateTime)
    cap = {"release_at": "2026-09-29T22:00:00+00:00"}
    now = datetime(2026, 9, 29, 21, 14, tzinfo=timezone.utc).timestamp()
    text = gui.format_cap_release(cap, now=now)
    assert "2026-09-30 01:30:00" in text
    assert "UTC+03:30" in text
    assert "46m" in text


@pytest.mark.parametrize("value", [None, "bad", "2026-09-29T22:00:00"])
def test_invalid_or_naive_timestamp_is_not_invented(value):
    assert gui.format_cap_release({"release_at": value}, now=0) == "Reset time unavailable"


def test_expired_cap_never_has_negative_countdown():
    assert "Reset due" in gui.format_cap_release(
        {"release_at": "2026-09-29T00:00:00Z"}, now=2_000_000_000)


def test_relative_fallback_uses_supplied_clock_and_rolls_over(monkeypatch):
    monkeypatch.setattr(gui, "datetime", TehranDateTime)
    now = datetime(2026, 9, 29, 20, 29, 30, tzinfo=timezone.utc).timestamp()
    text = gui.format_cap_release({"release_in_s": 90}, now=now)
    assert "2026-09-30 00:01:00" in text
    assert "in 2m" in text
    assert "90s" in gui.format_cap_release({"release_in_s": 90}, now=now, exact=True)


def test_caps_detail_keeps_all_models_and_upstream_reason():
    account = {"model_caps": {
        "vendor/first": {"release_at": "2026-09-29T22:00:00Z",
                         "code": "INFERENCE_CAP_ERROR", "message": "Try again in 46m"},
        "vendor/second": {"release_at": "invalid"},
    }}
    text = gui.format_caps(account, detail=True, now=0)
    assert "vendor/first" in text and "vendor/second" in text
    assert "INFERENCE_CAP_ERROR" in text and "Try again in 46m" in text
    assert "Reset time unavailable" in text


def test_legacy_caps_and_empty_account():
    assert gui.format_caps({"capped_models": ["vendor/legacy"]}) == "legacy"
    assert gui.format_caps({}) == "—"
