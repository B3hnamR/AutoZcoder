"""Persistence: SQLite usage ledger + JSONL exchange capture (Phase-1 shape)."""

from __future__ import annotations

import json
import math
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
        # WAL: the request path commits one row per request; with the default
        # journal every commit fsyncs and blocks readers. WAL keeps commits
        # cheap and lets /metrics summaries read without stalling writes.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass  # e.g. a WAL-less network filesystem; keep the default journal
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

    # ------------------------------------------------------------------ #
    # analytics (pro dashboard)
    # ------------------------------------------------------------------ #

    def analytics_overview(self, period: str = "24h", *, now: float | None = None) -> dict:
        """One consistent ledger window, including both partial edge buckets.

        The ledger records attempts, not unique client requests. Epoch-aligned
        buckets avoid local DST collisions; the browser formats their labels.
        """
        windows = {"1h": 3600, "6h": 21600, "24h": 86400,
                   "7d": 7 * 86400, "30d": 30 * 86400, "all": None}
        if period not in windows:
            raise ValueError("unsupported analytics range")
        until = time.time() if now is None else now
        window = windows[period]
        aggregate = """COUNT(*) AS requests,
            COALESCE(SUM(total_tokens),0) AS total_tokens,
            COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens),0) AS completion_tokens,
            AVG(duration_ms) AS avg_duration_ms,
            COALESCE(SUM(CASE WHEN status=200 THEN 1 ELSE 0 END),0) AS ok,
            COALESCE(SUM(CASE WHEN status=402 THEN 1 ELSE 0 END),0) AS insufficient_credits,
            COALESCE(SUM(CASE WHEN status=429 THEN 1 ELSE 0 END),0) AS rate_limited"""

        def query(sql, params):
            cursor = self._conn.execute(sql, params)
            names = [c[0] for c in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

        with self._lock:
            first = self._conn.execute("SELECT MIN(ts) FROM usage WHERE ts < ?", (until,)).fetchone()[0]
            since = until - window if window else (first if first is not None else until)
            bounds = (since, until)
            where = "WHERE ts >= ? AND ts < ?"
            summary = query(f"SELECT {aggregate} FROM usage {where}", bounds)[0]
            previous = query(f"SELECT {aggregate} FROM usage {where}",
                             (since - window, since))[0] if window else None
            span = until - since
            bucket = 300 if span <= 3600 else 3600 if span <= 86400 else 86400
            if span / bucket > 180:
                bucket = math.ceil(span / (180 * 86400)) * 86400
            grouped = query(f"""SELECT CAST(ts / ? AS INTEGER) * ? AS ts,
                COUNT(*) AS requests, COALESCE(SUM(total_tokens),0) AS tokens,
                SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) AS ok,
                SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) AS errors
                FROM usage {where} GROUP BY CAST(ts / ? AS INTEGER) ORDER BY ts""",
                (bucket, bucket, *bounds, bucket))
            indexed = {int(row["ts"]): row for row in grouped}
            start = math.floor(since / bucket) * bucket
            stop = math.floor(math.nextafter(until, -math.inf) / bucket) * bucket
            series = [indexed.get(ts, {"ts": ts, "requests": 0, "tokens": 0, "ok": 0, "errors": 0})
                      for ts in range(start, stop + 1, bucket)]
            values = [row[0] for row in self._conn.execute(
                f"SELECT duration_ms FROM usage {where} AND status=200 AND duration_ms >= 0 "
                "ORDER BY duration_ms", bounds).fetchall()]
            latency = {"samples": len(values)}
            for key, fraction in (("p50", .50), ("p95", .95), ("p99", .99)):
                latency[key] = round(values[max(0, math.ceil(len(values) * fraction) - 1)], 1) if values else None
            errors = query(f"""SELECT status, COALESCE(error_code,'') AS code,
                COUNT(*) AS count, MAX(ts) AS last_ts FROM usage {where}
                AND (status IS NULL OR status != 200)
                GROUP BY status,error_code ORDER BY count DESC,code""", bounds)
            result = {"range": {"id": period, "since": since, "until": until,
                                "bucket_seconds": bucket}, "summary": summary,
                      "previous": previous, "latency": latency, "series": series, "errors": errors}
            for dimension, column in (("model", "model"), ("account", "account_id"), ("key", "client_key")):
                # Columns are internal constants, never interpolated user input.
                rows = query(f"""SELECT {column}, {aggregate}, MAX(ts) AS last_ts
                    FROM usage {where} GROUP BY {column}
                    ORDER BY total_tokens DESC, requests DESC, {column}""", bounds)
                for row in rows:
                    row["tokens"] = row.pop("total_tokens")
                result["by_" + dimension] = rows
        return result

    def hourly_series(self, hours: int = 24) -> list[dict]:
        """Per-hour requests/tokens/errors for the analytics chart."""
        import datetime as _dt
        since = time.time() - hours * 3600
        with self._lock:
            rows = self._conn.execute(
                """SELECT ts, status, total_tokens, prompt_tokens, completion_tokens
                   FROM usage WHERE ts >= ? ORDER BY ts""", (since,)).fetchall()
        buckets: dict[str, dict] = {}
        for ts, status, tokens, pt, ct in rows:
            hr = _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:00")
            b = buckets.setdefault(hr, {"hour": hr, "requests": 0, "ok": 0,
                                        "errors": 0, "tokens": 0})
            b["requests"] += 1
            if status == 200:
                b["ok"] += 1
            else:
                b["errors"] += 1
            b["tokens"] += tokens or 0
        out = []
        now = _dt.datetime.now()
        for i in range(hours - 1, -1, -1):
            hr = (now - _dt.timedelta(hours=i)).strftime("%m-%d %H:00")
            out.append(buckets.get(hr, {"hour": hr, "requests": 0, "ok": 0,
                                        "errors": 0, "tokens": 0}))
        return out

    def latency_percentiles(self, since_seconds: int = 86400) -> dict:
        """p50/p90/p99 duration of successful requests."""
        since = time.time() - since_seconds
        with self._lock:
            rows = self._conn.execute(
                """SELECT duration_ms FROM usage
                   WHERE status = 200 AND duration_ms IS NOT NULL AND ts >= ?
                   ORDER BY duration_ms""", (since,)).fetchall()
        vals = [r[0] for r in rows if r[0] is not None]
        if not vals:
            return {"p50": 0, "p90": 0, "p99": 0, "samples": 0}
        def pct(p):
            return round(vals[min(int(len(vals) * p), len(vals) - 1)], 1)
        return {"p50": pct(0.50), "p90": pct(0.90), "p99": pct(0.99),
                "samples": len(vals)}

    def error_breakdown(self, since_seconds: int = 86400) -> list[dict]:
        """Top error codes with counts + last seen."""
        since = time.time() - since_seconds
        with self._lock:
            rows = self._conn.execute(
                """SELECT status, error_code, COUNT(*), MAX(ts) FROM usage
                   WHERE status != 200 AND ts >= ?
                   GROUP BY status, error_code ORDER BY COUNT(*) DESC
                   LIMIT 12""", (since,)).fetchall()
        return [{"status": s, "code": c or "", "count": n, "last_ts": t}
                for s, c, n, t in rows]

    def by_client_key(self, since_seconds: int = 86400 * 30) -> list[dict]:
        """Per-API-key usage (analytics + key manager stats)."""
        since = time.time() - since_seconds
        with self._lock:
            rows = self._conn.execute(
                """SELECT client_key, COUNT(*), SUM(total_tokens),
                          SUM(CASE WHEN status = 200 THEN 1 ELSE 0 END),
                          MAX(ts)
                   FROM usage WHERE ts >= ? AND client_key IS NOT NULL
                   GROUP BY client_key ORDER BY COUNT(*) DESC""",
                (since,)).fetchall()
        return [{"client_key": k, "requests": n, "tokens": t or 0,
                 "ok": ok or 0, "last_ts": mx}
                for k, n, t, ok, mx in rows]


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
