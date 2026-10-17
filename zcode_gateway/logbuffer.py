"""In-memory ring buffer of log records, served to the dashboard.

The Tkinter GUI kept its own queue-driven ring; the web UI cannot, so the app
attaches one logging.Handler here and the dashboard polls it. Bounded, and
starts empty per process (fine for a personal tool).
"""

from __future__ import annotations

import logging
import time
from collections import deque


class LogBuffer(logging.Handler):
    """Thread-safe ring buffer of the newest log records."""

    def __init__(self, capacity: int = 1000) -> None:
        super().__init__()
        self.records: deque[dict] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append({
                "ts": record.created,
                "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name.rsplit(".", 1)[-1],
                "message": record.getMessage(),
            })
        except Exception:
            pass  # logging must never raise into the app

    def tail(self, limit: int = 300, level: str = "") -> list[dict]:
        """Newest `limit` records, oldest first; optional level floor filter.

        A deque snapshot is atomic enough for a log view — no lock needed.
        """
        floors = {"": -1, "ALL": -1, "DEBUG": 10, "INFO": 20,
                  "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        floor = floors.get((level or "").upper(), -1)
        rows = []
        for r in list(self.records):
            # getLevelName returns a *string* ("Level TRACE") for custom level
            # names — comparing that to an int raised TypeError and 500'd the
            # dashboard log view. Unknown names rank below every floor.
            numeric = logging.getLevelName(r["level"])
            if not isinstance(numeric, int):
                numeric = logging.NOTSET
            if numeric >= floor:
                rows.append(r)
        return rows[-limit:]
