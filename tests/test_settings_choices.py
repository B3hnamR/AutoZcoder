"""Settings: dropdown choices come from the server, and enum values are validated."""
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
from zcode_gateway.api_dash import _EDITABLE
from zcode_gateway.config import load_config


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    conf = tmp_path / "prod.yml"
    conf.write_text("server:\n  port: 9997\n", encoding="utf-8")
    cfg = load_config(conf)
    cfg.update.enabled = False
    cfg.pool.balance_poll_seconds = 0
    cfg.accounts.source = "pool_file"
    cfg.accounts.pool_file = str(tmp_path / "empty.json")
    (tmp_path / "empty.json").write_text('{"accounts": []}', encoding="utf-8")
    cfg.store.sqlite_path = str(tmp_path / "g.db")
    cfg.logging.capture_dir = str(tmp_path / "logs")
    return cfg, conf, create_app(cfg)


def test_settings_endpoint_ships_the_dropdown_choices(tmp_path, monkeypatch):
    cfg, _, app = _client(tmp_path, monkeypatch)
    h = {"Authorization": f"Bearer {cfg.server.admin_key}"}
    with TestClient(app) as c:
        s = c.get("/admin/dash/settings", headers=h).json()
    choices = s["choices"]
    assert [o["value"] for o in choices["pool.strategy"]] == [
        "drain_first", "least_in_flight", "round_robin", "least_recently_used", "quota_aware"]
    for path, opts in choices.items():
        for o in opts:
            assert o["label"] and o["desc"], (path, o)         # every option is explained
    assert {"accounts.source", "logging.level", "models.default_effort", "server.host"} <= set(choices)
    assert "GLM-5.4" in s["models"] and "GLM-5.3-Flash" in s["models"]
    assert "enabled" in s["effective"]["update"]                 # update.* shows its real value


def test_saving_strategy_updates_live_accounts_pool_without_restart(tmp_path, monkeypatch):
    cfg, conf, app = _client(tmp_path, monkeypatch)
    h = {"Authorization": f"Bearer {cfg.server.admin_key}"}
    with TestClient(app) as c:
        # Spreading load is the default; switching it must still apply live.
        assert c.get("/admin/pool/state", headers=h).json()["strategy"] == "round_robin"
        result = c.put("/admin/dash/settings", headers=h, json={"values": {"pool.strategy": "drain_first"}})
        assert result.json() == {"changed": ["pool.strategy"], "restart_required": False}
        assert c.get("/admin/pool/state", headers=h).json()["strategy"] == "drain_first"
    assert yaml.safe_load(conf.read_text(encoding="utf-8"))["pool"]["strategy"] == "drain_first"


def test_enum_settings_reject_values_that_would_break_the_next_boot(tmp_path, monkeypatch):
    cfg, conf, app = _client(tmp_path, monkeypatch)
    h = {"Authorization": f"Bearer {cfg.server.admin_key}"}
    with TestClient(app) as c:
        put = lambda values: c.put("/admin/dash/settings", headers=h, json={"values": values})
        for bad in ({"pool.strategy": "fastest"}, {"accounts.source": "cloud"},
                    {"logging.level": "LOUD"}, {"models.default": "  "}):
            r = put(bad)
            assert r.status_code == 400, bad
        assert "must be one of" in put({"pool.strategy": "fastest"}).json()["detail"]

        assert put({"pool.strategy": "round_robin"}).status_code == 200
        assert put({"logging.level": "debug"}).status_code == 200      # normalised
        assert put({"server.host": "0.0.0.0"}).status_code == 400
        assert "host" not in yaml.safe_load(conf.read_text(encoding="utf-8"))["server"]
        monkeypatch.setenv("ZCODE_GATEWAY_SERVER__ADMIN_KEY", "fixture-public-admin")
        monkeypatch.setenv("ZCODE_GATEWAY_SERVER__CLIENT_KEYS", '[{"key":"fixture-public-client"}]')
        assert put({"server.host": "0.0.0.0"}).status_code == 200
        assert put({"server.host": "my-pc.lan"}).status_code == 200
    written = yaml.safe_load(conf.read_text(encoding="utf-8"))
    assert written["pool"]["strategy"] == "round_robin"
    assert written["logging"]["level"] == "DEBUG"
    assert written["server"]["host"] == "my-pc.lan"


def test_every_editable_setting_round_trips(tmp_path, monkeypatch):
    cfg, conf, app = _client(tmp_path, monkeypatch)
    values = {
        "server.host": "localhost", "server.port": 18788,
        "pool.strategy": "round_robin", "pool.min_balance_micro": 2,
        "pool.refresh_lead_seconds": 90, "pool.cooldown_seconds": 12,
        "pool.allow_token_refresh": False,
        "pool.max_in_flight_per_account": 2, "pool.acquire_wait_seconds": 1.5,
        "pool.balance_poll_seconds": 42, "pool.blocked_model_cooldown_seconds": 60,
        "upstream.timeout_connect": 3.5, "upstream.timeout_read": 90.5,
        "upstream.max_attempts": 2, "upstream.anthropic_cache_control": False,
        "upstream.overload_max_attempts": 5, "upstream.overload_base_delay_seconds": 0.5,
        "upstream.overload_max_delay_seconds": 12,
        "accounts.source": "pool_file", "accounts.dir": "./saved", "accounts.pool_file": "./saved.json",
        "logging.level": "WARNING", "logging.capture": True,
        "models.default": "GLM-5.4", "models.default_anthropic": "GLM-5.3-Flash",
        "models.auto_free_fallback.enabled": False, "models.default_effort": "max",
        "models.pacing.enabled": False, "models.pacing.min_gap_ms": 75, "models.pacing.max_gap_ms": 175,
        "update.enabled": False, "update.interval_hours": 2.5,
    }
    assert values.keys() == _EDITABLE.keys()
    headers = {"Authorization": f"Bearer {cfg.server.admin_key}"}
    with TestClient(app) as client:
        result = client.put("/admin/dash/settings", headers=headers, json={"values": values})
        assert result.status_code == 200, result.json()
        assert result.json()["restart_required"]
        assert client.get("/admin/dash/settings", headers=headers).json()["values"] == values
        for invalid in ({"server.port": 1.5}, {"logging.capture": []}, {"accounts.dir": " "},
                        {"upstream.max_attempts": 0}, {"models.pacing.min_gap_ms": 200}):
            assert client.put("/admin/dash/settings", headers=headers,
                              json={"values": invalid}).status_code == 400
    effective = load_config(conf).model_dump()
    for path, value in values.items():
        actual = effective
        for part in path.split("."):
            actual = actual[part]
        if path.startswith("accounts.") and path != "accounts.source":
            actual = "./" + Path(actual).name
        assert actual == value, path
