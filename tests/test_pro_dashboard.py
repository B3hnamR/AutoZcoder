"""Tests for the pro dashboard round: analytics queries, notifications,
log search, proxy health endpoint."""
from __future__ import annotations

import time
import json
import shutil
import subprocess
from pathlib import Path

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


@pytest.mark.parametrize("period,seconds", [("1h", 3600), ("6h", 21600), ("24h", 86400),
                                           ("7d", 604800), ("30d", 2592000)])
def test_overview_boundaries_and_partial_buckets(store, period, seconds):
    now = 1793363143.5
    since = now - seconds
    for ts, status in [(since - .1, 200), (since, 200), (now - .1, 429), (now, 200)]:
        store.record(ts=ts, status=status, duration_ms=25, total_tokens=7,
                     prompt_tokens=5, completion_tokens=2, model="model", account_id="removed")
    result = store.analytics_overview(period, now=now)
    assert result["summary"]["requests"] == 2
    assert result["previous"]["requests"] == 1
    assert result["summary"]["total_tokens"] == 14
    assert sum(r["requests"] for r in result["series"]) == 2
    assert sum(r["tokens"] for r in result["series"]) == 14
    assert sum(r["errors"] for r in result["series"]) == 1
    assert result["by_account"][0]["account_id"] == "removed"
    assert result["by_key"][0]["client_key"] is None


def test_overview_all_time_empty_and_null_status(store):
    now = 1793363143.5
    empty = store.analytics_overview("all", now=now)
    assert empty["summary"]["requests"] == 0
    assert empty["previous"] is None
    assert empty["latency"]["p95"] is None
    store.record(ts=now - 800 * 86400, status=None, model=None)
    store.record(ts=now - 1, status=200, duration_ms=0, total_tokens=0)
    result = store.analytics_overview("all", now=now)
    assert result["summary"]["requests"] == 2
    assert len(result["series"]) <= 182
    assert sum(r["requests"] for r in result["series"]) == 2
    assert result["errors"][0]["status"] is None
    assert result["latency"] == {"samples": 1, "p50": 0, "p95": 0, "p99": 0}


def test_overview_nearest_rank_percentiles_and_common_breakdowns(store):
    now = 1793363143.5
    for i in range(1, 21):
        store.record(ts=now - i, model="test", account_id="a", client_key="label",
                     status=200, duration_ms=i * 100, total_tokens=3)
    store.record(ts=now - 1, model="test", account_id="a", client_key="label",
                 status=500, duration_ms=99999, total_tokens=0)
    result = store.analytics_overview("1h", now=now)
    assert result["latency"] == {"samples": 20, "p50": 1000, "p95": 1900, "p99": 2000}
    for dimension in ("by_model", "by_account", "by_key"):
        assert sum(r["requests"] for r in result[dimension]) == 21
        assert sum(r["tokens"] for r in result[dimension]) == 60
    with pytest.raises(ValueError):
        store.analytics_overview("bogus", now=now)


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


def test_default_dashboard_serves_modular_shell_and_assets(tmp_path):
    with TestClient(create_app(_cfg(tmp_path))) as client:
        main = client.get("/dash")
        assert main.status_code == 200
        assert '/dash/assets/js/boot.js' in main.text
        assert client.get("/dash/next").text == main.text
        for path in ("js/boot.js", "js/views/analytics.js", "css/tokens.css"):
            assert client.get("/dash/assets/" + path).status_code == 200


def test_desktop_cleanup_uses_existing_manager_only(tmp_path):
    from unittest.mock import Mock
    from zcode_gateway.deps import get_state
    with TestClient(create_app(_cfg(tmp_path))) as client:
        state = client.app.state.app_state
        assert getattr(state, "generator", None) is None
        headers = {"Authorization": "Bearer adm-test"}
        assert client.post("/admin/dash/generator/shutdown", headers=headers).status_code == 200
        assert getattr(state, "generator", None) is None
        manager = Mock()
        state.generator = manager
        assert client.post("/admin/dash/generator/shutdown", headers=headers).status_code == 200
        manager.shutdown.assert_called_once()
        state.generator = None


def test_bootstrap_selects_start_plan_not_withdrawn_legacy_campaign():
    from zcode_gateway.paths import default_config_yaml
    import yaml
    defaults = yaml.safe_load(default_config_yaml())["generator"]
    assert defaults["claim"] is False and defaults["claim_start"] is True
    assert defaults["captcha"] == "manual"


def test_unified_analytics_endpoint_requires_admin_and_valid_range(tmp_path):
    with TestClient(create_app(_cfg(tmp_path))) as client:
        assert client.get("/admin/dash/analytics/overview").status_code in (401, 403)
        headers = {"Authorization": "Bearer adm-test"}
        for period in ("1h", "6h", "24h", "7d", "30d", "all"):
            response = client.get("/admin/dash/analytics/overview", params={"range": period}, headers=headers)
            assert response.status_code == 200
            assert response.json()["range"]["id"] == period
        assert client.get("/admin/dash/analytics/overview?range=invalid", headers=headers).status_code == 400


def test_analytics_csv_escapes_formulas_and_comparison_handles_zero():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the dashboard's pure JS helpers")
    module = (Path(__file__).resolve().parents[1] / "zcode_gateway/dashboard/assets/js/views/analytics-export.js").as_uri()
    script = f"""import {{csvText, comparison}} from {json.dumps(module)};
      console.log(JSON.stringify({{
        csv: csvText(['name','tokens'], [['=HYPERLINK("x")',24000], ['a,b\\nc',3]]),
        pp: comparison(10,0,'pp'), zero: comparison(2,0),
        absent: comparison(0,null), unchanged: comparison(0,0)
      }}));"""
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, check=True, timeout=15)
    data = json.loads(result.stdout)
    assert "\"'=HYPERLINK(\"\"x\"\")\"" in data["csv"]
    assert '"a,b\nc",' in data["csv"]
    assert '"24000"' in data["csv"]
    assert data["pp"] == "+10.0 percentage points vs previous period"
    assert data["zero"] == "New activity; previous period had none"
    assert data["absent"] == "No previous-period comparison"
    assert data["unchanged"] == "No change from previous period"


def test_guide_chapters_errors_and_actions_are_complete():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node needed for Guide content contract")
    module = (Path(__file__).resolve().parents[1] / "zcode_gateway/dashboard/assets/js/views/guide-content.js").as_uri()
    script = f"import {{GUIDE,ERRORS}} from {json.dumps(module)};console.log(JSON.stringify({{GUIDE,ERRORS}}));"
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True,
                            text=True, check=True, timeout=15)
    data = json.loads(result.stdout)
    chapters = data["GUIDE"]
    assert len(chapters) == 8
    assert len({c["id"] for c in chapters}) == len(chapters)
    assert len(data["ERRORS"]) >= 25
    for chapter in chapters:
        assert chapter["route"] in ("server", "accounts", "models", "generator", "proxy", "analytics")
        assert chapter["items"]
    for topic in [item for chapter in chapters for item in chapter["items"]] + data["ERRORS"]:
        assert topic["title"] and topic["tags"] and len(topic["steps"]) >= 2
    text = json.dumps(data)
    for term in ("INBOX_SECRET", "Xvfb", "TUN", "3007", "529", "451", "Imported", "autozcoder"):
        assert term in text
    sizing = next(item for chapter in chapters for item in chapter["items"] if item.get("sizing"))
    assert len(sizing["sizing"]) == 4
    assert sizing["sizing"][1][1:3] == ["2 reliable vCPUs", "4 GB"]
    assert "ZGW_ALLOW_LOW_RESOURCES=1" in " ".join(sizing["steps"])


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


def test_proxy_overview_code_removed_but_scan_import_remains(tmp_path):
    app = create_app(_cfg(tmp_path))
    with TestClient(app) as c:
        c.get("/admin/dash/server", headers={"Authorization": "Bearer adm-test"})
        r = c.get("/admin/dash/proxies/health",
                  headers={"Authorization": "Bearer adm-test"})
        assert r.status_code == 404
        js = c.get("/dash/assets/js/views/proxy.js").text
        assert "createPoolCard" not in js and "pool.node" not in js
        assert "sources.node" in js and "proxies/run" in js
        assert c.get("/dash/assets/js/views/proxy/sources.js").status_code == 200


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
