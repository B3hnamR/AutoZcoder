"""Settings: dropdown choices come from the server, and enum values are validated."""
import yaml
from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
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
    assert "GLM-5.3" in s["models"] and "GLM-5.3-Flash" in s["models"]
    assert "enabled" in s["effective"]["update"]                 # update.* shows its real value


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
        assert put({"server.host": "0.0.0.0"}).status_code == 200
        assert put({"server.host": "my-pc.lan"}).status_code == 200    # any host name is allowed
    written = yaml.safe_load(conf.read_text(encoding="utf-8"))
    assert written["pool"]["strategy"] == "round_robin"
    assert written["logging"]["level"] == "DEBUG"
    assert written["server"]["host"] == "my-pc.lan"
