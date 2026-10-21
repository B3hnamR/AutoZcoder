"""Tests for the pro dashboard round: analytics queries, notifications,
log search, proxy health endpoint."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
from zcode_gateway.config import Config
from zcode_gateway.notify import NotificationCenter
from zcode_gateway.store import Store


# --------------------------------------------------------------------------- #
# notifications bus
# --------------------------------------------------------------------------- #

def test_notify_emit_and_list():
    n = NotificationCenter()
    n.emit("pool_empty", "no usable accounts", level="warning")
    n.emit("account_dead", "acct-1 DEAD", level="error")
    events = n.list()
    assert len(events) == 2
    assert events[0]["category"] == "pool_empty"
    assert events[1]["level"] == "error"


def test_notify_dedupe_bumps_count():
    n = NotificationCenter()
    for _ in range(5):
        n.emit("edge_block", "upstream edge block", level="error")
    events = n.list()
    assert len(events) == 1
    assert events[0]["count"] == 5


def test_notify_since_incremental():
    n = NotificationCenter()
    n.emit("quota", "a")
    n.emit("quota", "b")
    first = n.list()
    last_id = first[-1]["id"]
    n.emit("quota", "c")
    tail = n.list(since_id=last_id)
    assert len(tail) == 1 and tail[0]["message"] == "c"


def test_notify_unread_state():
    n = NotificationCenter()
    n.emit("quota", "x", level="error")
    n.emit("quota", "y", level="info")
    st = n.unread_state()
    assert st["total_24h"] == 2 and st["errors_24h"] == 1


# --------------------------------------------------------------------------- #
# store analytics
# --------------------------------------------------------------------------- #

@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "analytics.db"))
    yield s
    s.close()


def test_hourly_series_and_percentiles(store):
    now = time.time()
    # timestamps spread across the last 50 minutes: at most a single hour
    # boundary can split them, so assert on the SUM (robust near XX:00)
    for i in range(10):
        store.record(ts=now - i * 60, account_id="a1", client_key="k1",
                     dialect="openai", model="GLM-5.3", variant="anthropic",
                     stream=1, status=200, duration_ms=100.0 + i * 50,
                     prompt_tokens=10, completion_tokens=5, total_tokens=15,
                     error_code="")
    series = store.hourly_series(2)
    assert sum(b["requests"] for b in series) == 10
    lat = store.latency_percentiles(3600)
    assert lat["samples"] == 10
    assert lat["p50"] <= lat["p90"] <= lat["p99"]


def test_error_breakdown_and_by_key(store):
    now = time.time()
    store.record(ts=now, status=200, duration_ms=10, client_key="k1",
                 total_tokens=5, model="GLM-5.3", dialect="openai")
    store.record(ts=now, status=429, duration_ms=0, client_key="k1",
                 error_code="rate_limit_error", model="GLM-5.3", dialect="openai")
    store.record(ts=now, status=429, duration_ms=0, client_key="k2",
                 error_code="rate_limit_error", model="GLM-5.3", dialect="openai")
    errs = store.error_breakdown(3600)
    assert errs and errs[0]["status"] == 429 and errs[0]["count"] == 2
    keys = store.by_client_key(3600)
    byk = {k["client_key"]: k for k in keys}
    assert byk["k1"]["requests"] == 2 and byk["k1"]["ok"] == 1


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #

def _cfg(tmp_path, admin="adm-test"):
    return Config(server={"host": "127.0.0.1", "port": 8788,
                          "admin_key": admin,
                          "client_keys": [{"key": "cli-test", "name": "local"}]},
                  accounts={"source": "accounts_dir",
                            "dir": str(tmp_path / "accounts")},
                  store={"sqlite_path": str(tmp_path / "gw.db")},
                  logging={"capture": False})


def test_analytics_endpoint(tmp_path):
    app = create_app(_cfg(tmp_path))
    with TestClient(app) as c:
        c.get("/admin/dash/server", headers={"Authorization": "Bearer adm-test"})
        r = c.get("/admin/dash/analytics?hours=24",
                  headers={"Authorization": "Bearer adm-test"})
        assert r.status_code == 200
        body = r.json()
        assert {"series", "latency", "errors", "by_key", "summary",
                "accounts"} <= set(body)


def test_notifications_endpoints(tmp_path):
    app = create_app(_cfg(tmp_path))
    with TestClient(app) as c:
        c.get("/admin/dash/server", headers={"Authorization": "Bearer adm-test"})
        r = c.get("/admin/dash/notifications",
                  headers={"Authorization": "Bearer adm-test"})
        assert r.status_code == 200 and "events" in r.json()
        r2 = c.post("/admin/dash/notifications/clear",
                    headers={"Authorization": "Bearer adm-test"})
        assert r2.status_code == 200


def test_logs_search_endpoint(tmp_path):
    app = create_app(_cfg(tmp_path))
    with TestClient(app) as c:
        c.get("/admin/dash/server", headers={"Authorization": "Bearer adm-test"})
        r = c.get("/admin/dash/logs?q=nonexistent-needle",
                  headers={"Authorization": "Bearer adm-test"})
        assert r.status_code == 200
        assert r.json()["records"] == []


def test_proxy_health_endpoint_no_pool(tmp_path):
    app = create_app(_cfg(tmp_path))
    with TestClient(app) as c:
        c.get("/admin/dash/server", headers={"Authorization": "Bearer adm-test"})
        r = c.get("/admin/dash/proxies/health",
                  headers={"Authorization": "Bearer adm-test"})
        assert r.status_code == 200
        body = r.json()
        # no pool.json yet -> graceful unavailable, not a 500
        assert body["available"] is False or "kpis" in body


def test_purge_dead_accounts_endpoint(tmp_path):
    """Purge must DELETE dead (plan-lost) accounts and their files — they
    used to be skipped as 'unknown' (empty quotas) and survived purges."""
    import time as _t
    acc_dir = tmp_path / "accounts"
    acc_dir.mkdir()
    snap = acc_dir / "dead1@example.com.txt"
    snap.write_text("account_id: dead1\nemail: dead1@example.com\n"
                    "token: abc\n", encoding="utf-8")
    live = acc_dir / "live1@example.com.txt"
    live.write_text("account_id: live1\nemail: live1@example.com\n"
                    "token: def\n", encoding="utf-8")
    app = create_app(_cfg_dir(tmp_path, str(acc_dir)))
    with TestClient(app) as c:
        c.get("/admin/dash/server", headers={"Authorization": "Bearer adm-test"})
        pool = app.state.app_state.pool
        import asyncio
        from zcode_gateway.pool import Account, AccountState
        dead = Account(id="dead1", email="dead1@example.com",
                       access_token="abc", expires_at=0)
        dead.state = AccountState.DEAD
        dead.notes["file"] = "dead1@example.com.txt"
        alive = Account(id="live1", email="live1@example.com",
                        access_token="def", expires_at=0)
        alive.state = AccountState.READY
        alive.notes["file"] = "live1@example.com.txt"
        asyncio.run(pool.upsert(dead))
        asyncio.run(pool.upsert(alive))
        r = c.post("/admin/accounts/purge_drained",
                   headers={"Authorization": "Bearer adm-test"})
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["purged"][0]["id"] == "dead1"
        assert body["purged"][0]["file_deleted"] is True
        assert not snap.exists()          # file gone
        assert live.exists()              # healthy account untouched


def _cfg_dir(tmp_path, accounts_dir):
    return Config(server={"host": "127.0.0.1", "port": 8788,
                          "admin_key": "adm-test",
                          "client_keys": [{"key": "cli-test", "name": "local"}]},
                  accounts={"source": "accounts_dir",
                            "dir": accounts_dir},
                  store={"sqlite_path": str(tmp_path / "gw.db")},
                  logging={"capture": False})


def test_window_grace_constant():
    from zcode_gateway.pool import WINDOW_GRACE_S
    assert WINDOW_GRACE_S == 30
