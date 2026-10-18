"""Persistence: SQLite usage ledger + JSONL exchange capture (Phase-1 shape)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    account_id    TEXT,
    client_key    TEXT,
    dialect       TEXT,
    model         TEXT,
    variant       TEXT,
    stream        INTEGER,
    status        INTEGER,
    duration_ms   REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens  INTEGER,
    error_code    TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_account ON usage(account_id);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage(model);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    _COLS = ("ts", "account_id", "client_key", "dialect", "model", "variant",
             "stream", "status", "duration_ms", "prompt_tokens",
             "completion_tokens", "total_tokens", "error_code")

    def record(self, **row: Any) -> None:
        row.setdefault("ts", time.time())
        cols = self._COLS
        unknown = set(row) - set(cols)
        if unknown:
            # a typo'd column used to be silently dropped; say so
            raise TypeError(f"unknown usage column(s): {sorted(unknown)}")
        values = [row.get(c) for c in cols]
        with self._lock:
            self._conn.execute(
                f"INSERT INTO usage ({','.join(cols)}) "
                f"VALUES ({','.join('?' for _ in cols)})",
                values,
            )
            self._conn.commit()

    # ------------------------------------------------------------------ #

    def summary(self, since_seconds: int | None = None) -> dict:
        where = ""
        params: tuple = ()
        if since_seconds:
            where = "WHERE ts >= ?"
            params = (time.time() - since_seconds,)
        with self._lock:
            cur = self._conn.execute(
                f"""SELECT COUNT(*), SUM(total_tokens), SUM(prompt_tokens),
                           SUM(completion_tokens),
                           AVG(duration_ms),
                           SUM(CASE WHEN status = 200 THEN 1 ELSE 0 END),
                           SUM(CASE WHEN status = 402 THEN 1 ELSE 0 END),
                           SUM(CASE WHEN status = 429 THEN 1 ELSE 0 END)
                    FROM usage {where}""", params)
            row = cur.fetchone()

            by_model = self._conn.execute(
                f"""SELECT model, COUNT(*), SUM(total_tokens)
                    FROM usage {where} GROUP BY model ORDER BY COUNT(*) DESC""",
                params).fetchall()
            by_account = self._conn.execute(
                f"""SELECT account_id, COUNT(*), SUM(total_tokens)
                    FROM usage {where} GROUP BY account_id ORDER BY COUNT(*) DESC""",
                params).fetchall()

        return {
            "requests": row[0] or 0,
            "total_tokens": row[1] or 0,
            "prompt_tokens": row[2] or 0,
            "completion_tokens": row[3] or 0,
            "avg_duration_ms": round(row[4], 1) if row[4] else 0,
            "ok": row[5] or 0,
            "insufficient_credits": row[6] or 0,
            "rate_limited": row[7] or 0,
            "by_model": [
                {"model": m, "requests": c, "tokens": t or 0} for m, c, t in by_model
            ],
            "by_account": [
                {"account_id": a, "requests": c, "tokens": t or 0}
                for a, c, t in by_account
            ],
        }

    def daily_buckets(self, days: int = 14) -> list[dict]:
        """Per-day request/token counts for the usage dashboard."""
        import datetime as _dt
        since = time.time() - days * 86400
        try:
            with self._lock:
                rows = self._conn.execute(
                    """SELECT ts, model, status, total_tokens FROM usage
                       WHERE ts >= ? ORDER BY ts""", (since,)).fetchall()
        except Exception:
            return []
        buckets: dict[str, dict] = {}
        for ts, model, status, tokens in rows:
            day = _dt.datetime.fromtimestamp(ts).strftime("%m-%d")
            b = buckets.setdefault(day, {"day": day, "requests": 0,
                                         "ok": 0, "tokens": 0, "models": {}})
            b["requests"] += 1
            if status == 200:
                b["ok"] += 1
            b["tokens"] += tokens or 0
            b["models"][model] = b["models"].get(model, 0) + 1
        out = []
        for i in range(days - 1, -1, -1):
            day = (_dt.datetime.now() - _dt.timedelta(days=i)).strftime("%m-%d")
            b = buckets.get(day, {"day": day, "requests": 0, "ok": 0,
                                  "tokens": 0, "models": {}})
            b["models"] = sorted(b["models"].items(), key=lambda kv: -kv[1])[:5]
            out.append(b)
        return out


class JsonlCapture:
    """Writes every upstream exchange in the same shape as the Phase-1 capture."""

    def __init__(self, capture_dir: str, enabled: bool = True) -> None:
        self.enabled = enabled
        self.dir = Path(capture_dir)
        if enabled:
            self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        # PID suffix: two processes starting in the same second used to share
        # one JSONL file while the append lock is per-process
        self.path = self.dir / f"gateway-{stamp}-{os.getpid()}.jsonl"
        self._lock = threading.Lock()

    @staticmethod
    def _redact_auth(value: str) -> str:
        if not value:
            return value
        if " " in value:
            scheme, _, tok = value.partition(" ")
            return f"{scheme} {tok[:12]}...{tok[-6:]}(len={len(tok)})"
        return f"{value[:12]}...{value[-6:]}(len={len(value)})"

    def write(self, *, request_headers: dict, request_body: dict,
              status: int, response_headers: dict, response_body: str,
              duration_ms: float, account_id: str, dialect: str,
              model: str, variant: str) -> None:
        if not self.enabled:
            return
        record = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round(duration_ms, 1),
            "account_id": account_id,
            "dialect": dialect,
            "variant": variant,
            "request": {
                "method": "POST",
                "url": "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages",
                "headers": {
                    k: (self._redact_auth(v)
                        if k.lower() in ("authorization", "x-api-key")
                        else v)
                    for k, v in request_headers.items()
                },
                "body": json.dumps(request_body, ensure_ascii=False),
            },
            "response": {
                "status_code": status,
                "headers": response_headers,
                "body": response_body[:200_000],
            },
            "model": model,
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
