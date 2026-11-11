"""Daily claim scheduler: once per local day after the configured time."""

from __future__ import annotations

from datetime import datetime, timezone

from zcode_gateway.claim_schedule import ClaimScheduler, parse_hhmm


def epoch(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


def test_parse_hhmm():
    assert parse_hhmm("19:02") == (19, 2)
    assert parse_hhmm("7:5") == (7, 5)
    assert parse_hhmm("25:00") == (19, 2)      # fallback
    assert parse_hhmm("nope") == (19, 2)


def test_waits_until_configured_local_time():
    now = [epoch(2026, 11, 11, 10, 0)]          # 18:00 Beijing
    runs = []
    sched = ClaimScheduler(lambda: runs.append(1), enabled=True,
                           daily_time="19:02", utc_offset_hours=8,
                           clock=lambda: now[0])
    sched._tick()
    assert runs == []
    now[0] = epoch(2026, 11, 11, 11, 1)         # 19:01 Beijing
    sched._tick()
    assert runs == []
    now[0] = epoch(2026, 11, 11, 11, 2)         # 19:02 Beijing
    sched._tick()
    assert runs == [1]


def test_runs_at_most_once_per_local_day():
    now = [epoch(2026, 11, 11, 12, 0)]          # 20:00 Beijing, past the time
    runs = []
    sched = ClaimScheduler(lambda: runs.append(1), enabled=True,
                           daily_time="19:02", utc_offset_hours=8,
                           clock=lambda: now[0])
    sched._tick()
    sched._tick()
    sched._tick()
    assert runs == [1]
    now[0] = epoch(2026, 11, 12, 12, 0)         # next local day
    sched._tick()
    assert runs == [1, 1]


def test_local_day_uses_offset_not_utc_date():
    # 17:30 UTC on the 11th is already 01:30 on the 12th in Beijing; a fresh
    # scheduler must not replay "yesterday".
    now = [epoch(2026, 11, 11, 17, 30)]
    runs = []
    sched = ClaimScheduler(lambda: runs.append(1), enabled=True,
                           daily_time="19:02", utc_offset_hours=8,
                           clock=lambda: now[0])
    sched._tick()
    assert runs == []
    assert sched.last_run_date == ""


def test_disabled_scheduler_never_runs():
    now = [epoch(2026, 11, 11, 23, 0)]
    runs = []
    sched = ClaimScheduler(lambda: runs.append(1), enabled=False,
                           clock=lambda: now[0])
    sched._tick()
    assert runs == []


def test_error_result_keeps_the_day_pending_and_retries():
    now = [epoch(2026, 11, 11, 12, 0)]
    calls = []
    sched = ClaimScheduler(lambda: calls.append(1) or {"error": "claim-all already running"},
                           enabled=True, daily_time="19:02", utc_offset_hours=8,
                           retry_seconds=300, clock=lambda: now[0])
    sched._tick()
    assert sched.last_run_date == ""
    assert "already running" in sched.last_error
    sched._tick()                    # inside the back-off window
    assert len(calls) == 1
    now[0] += 301
    sched._tick()
    assert len(calls) == 2


def test_exception_is_recorded_not_raised():
    now = [epoch(2026, 11, 11, 12, 0)]

    def boom():
        raise RuntimeError("no manager")

    sched = ClaimScheduler(boom, enabled=True, daily_time="19:02",
                           utc_offset_hours=8, clock=lambda: now[0])
    sched._tick()
    assert sched.last_run_date == ""
    assert "no manager" in sched.last_error


def test_state_persists_across_restarts(tmp_path):
    state = tmp_path / "claim_schedule.json"
    now = [epoch(2026, 11, 11, 12, 0)]
    first = ClaimScheduler(lambda: {"started": "claim-1"}, enabled=True,
                           daily_time="19:02", utc_offset_hours=8,
                           state_path=state, clock=lambda: now[0])
    first._tick()
    assert first.last_run_date == "2026-11-11"
    assert state.is_file()

    calls = []
    second = ClaimScheduler(lambda: calls.append(1), enabled=True,
                            daily_time="19:02", utc_offset_hours=8,
                            state_path=state, clock=lambda: now[0])
    second._tick()
    assert calls == []               # the day was already claimed
    assert second.last_result == {"started": "claim-1"}


def test_status_shape():
    sched = ClaimScheduler(lambda: {}, daily_time="08:25", utc_offset_hours=3.5)
    status = sched.status()
    assert status["daily_time"] == "08:25"
    assert status["utc_offset_hours"] == 3.5
    assert status["enabled"] is False
