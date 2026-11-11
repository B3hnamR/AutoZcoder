"""Daily claim-all scheduler.

Campaign cards and Start Plans are time-boxed upstream (a weekend drop starts
at a fixed Beijing wall clock), and the claim does not need a human, so the
gateway can walk the pending accounts once a day on its own.

Time zones are fixed UTC offsets on purpose: Windows ships without the IANA
database, so `zoneinfo` would raise on a stock install. Beijing is UTC+8 all
year; Tehran is UTC+3:30 and has had no DST since 2022.

State is a tiny JSON file next to the generator data, so a restart does not
re-run a completed day and a missed day is caught up on the next tick.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = ["ClaimScheduler", "parse_hhmm"]

log = logging.getLogger(__name__)

DEFAULT_TIME = "19:02"          # matches zcode2api's polling default
DEFAULT_UTC_OFFSET = 8.0        # Asia/Shanghai


def parse_hhmm(text: str) -> tuple[int, int]:
    """'19:02' -> (19, 2); bad input falls back to the default."""
    try:
        hh, mm = str(text).strip().split(":", 1)
        hour, minute = int(hh), int(mm)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, AttributeError):
        pass
    hh, mm = DEFAULT_TIME.split(":")
    return int(hh), int(mm)


class ClaimScheduler:
    """Runs `run()` once per local day once the configured time has passed."""

    def __init__(self, run, enabled: bool = False, daily_time: str = DEFAULT_TIME,
                 utc_offset_hours: float = DEFAULT_UTC_OFFSET,
                 state_path: str | Path | None = None,
                 poll_seconds: float = 30.0, retry_seconds: float = 300.0,
                 clock=time.time) -> None:
        self._run = run
        self.enabled = bool(enabled)
        self.hour, self.minute = parse_hhmm(daily_time)
        self.utc_offset_hours = float(utc_offset_hours)
        self.state_path = Path(state_path) if state_path else None
        self.poll_seconds = max(float(poll_seconds), 1.0)
        self.retry_seconds = max(float(retry_seconds), 1.0)
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run_date = ""
        self.last_run_at = 0.0
        self.last_attempt_at = 0.0
        self.last_result: dict | None = None
        self.last_error = ""
        self._load()

    # ---- lifecycle ------------------------------------------------------ #

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="claim-schedule")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)

    def run_now(self) -> dict:
        """Claim immediately, bypassing the time-of-day gate."""
        return self._attempt()

    def status(self) -> dict:
        return {"enabled": self.enabled,
                "daily_time": f"{self.hour:02d}:{self.minute:02d}",
                "utc_offset_hours": self.utc_offset_hours,
                "last_run_date": self.last_run_date,
                "last_run_at": self.last_run_at,
                "last_result": self.last_result,
                "last_error": self.last_error}

    # ---- loop ----------------------------------------------------------- #

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:      # a scheduler bug must not kill the thread
                log.exception("claim scheduler tick failed")
            self._stop.wait(timeout=self.poll_seconds)

    def _local_now(self, now: float | None = None) -> datetime:
        now = self._clock() if now is None else now
        return (datetime.fromtimestamp(now, tz=timezone.utc)
                + timedelta(hours=self.utc_offset_hours))

    def _tick(self, now: float | None = None) -> None:
        if not self.enabled:
            return
        now = self._clock() if now is None else now
        local = self._local_now(now)
        target = local.replace(hour=self.hour, minute=self.minute,
                               second=0, microsecond=0)
        if local < target:
            return
        today = local.date().isoformat()
        if self.last_run_date == today:
            return
        if self.last_attempt_at and now - self.last_attempt_at < self.retry_seconds:
            return
        self.last_attempt_at = now
        self._attempt(today, now)

    def _attempt(self, today: str | None = None, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        today = today or self._local_now(now).date().isoformat()
        try:
            result = self._run() or {}
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("scheduled claim failed: %s", self.last_error)
            self._save()
            return {"error": self.last_error}
        if isinstance(result, dict) and result.get("error"):
            # "claim-all already running" and similar: leave the day pending
            # so the next retry window tries again.
            self.last_error = str(result["error"])
            log.info("scheduled claim skipped: %s", self.last_error)
            self._save()
            return result
        self.last_error = ""
        self.last_result = result if isinstance(result, dict) else {"result": result}
        self.last_run_date = today
        self.last_run_at = now
        log.info("scheduled claim finished for %s", today)
        self._save()
        return self.last_result

    # ---- persistence ---------------------------------------------------- #

    def _load(self) -> None:
        if not self.state_path or not self.state_path.is_file():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.last_run_date = str(data.get("last_run_date") or "")
        self.last_run_at = float(data.get("last_run_at") or 0.0)
        self.last_result = data.get("last_result")

    def _save(self) -> None:
        if not self.state_path:
            return
        data = {"last_run_date": self.last_run_date,
                "last_run_at": self.last_run_at,
                "last_result": self.last_result}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.debug("could not persist claim schedule: %s", exc)
