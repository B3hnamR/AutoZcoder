"""Local hardening regressions (review findings, local-first threat model).

- `load_config` never recorded the config file, so dashboard settings writes
  fell back to a cwd-relative config.yaml — the running process could edit a
  file it never loaded (and `--config prod.yml` wrote a decoy config.yaml).
- The dashboard log view raised on custom level names and ignored CRITICAL.
- CORS `*` let any visited website read `/dash` (which embeds the admin key on
  loopback); the key is now only injected when the request's own Host is
  loopback too — DNS rebinding makes a foreign hostname same-origin.
- Auth failures on /v1/* returned FastAPI's {"detail": ...} instead of the
  OpenAI/Anthropic error envelopes or clients expect.
- `detect_zcode()` (two `tasklist` runs, up to ~20 s) ran on the event loop.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
from zcode_gateway.config import ClientKey, Config, load_config
from zcode_gateway.logbuffer import LogBuffer


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.server.require_client_key = True
    cfg.server.admin_key = "adm-test"
    cfg.server.client_keys = [ClientKey(key="ck-test", name="t", rpm=0)]
    cfg.accounts.source = "pool_file"
    cfg.accounts.pool_file = str(tmp_path / "empty.json")
    (tmp_path / "empty.json").write_text('{"accounts": []}', encoding="utf-8")
    cfg.store.sqlite_path = str(tmp_path / "g.db")
    cfg.logging.capture_dir = str(tmp_path / "logs")
    return cfg


# --------------------------------------------------------------------------- #
# config root
# --------------------------------------------------------------------------- #


def test_load_config_records_the_configs_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)          # no stray config.yaml in the cwd
    conf = tmp_path / "custom" / "config.yaml"
    conf.parent.mkdir()
    conf.write_text("server:\n  port: 9998\n", encoding="utf-8")

    cfg = load_config(conf)

    assert cfg.server.port == 9998
    assert Path(cfg._root) == conf.parent.resolve()


def test_dashboard_config_path_uses_the_loaded_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    conf = tmp_path / "custom" / "config.yaml"
    conf.parent.mkdir()
    conf.write_text("server:\n  port: 9998\n", encoding="utf-8")
    cfg = load_config(conf)

    from zcode_gateway.api_dash import _config_path
    assert _config_path(SimpleNamespace(cfg=cfg)) == conf


def test_dashboard_config_path_uses_the_loaded_file_not_the_default_name(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)          # no stray config.yaml in the cwd
    conf = tmp_path / "custom" / "prod.yml"
    conf.parent.mkdir()
    conf.write_text("server:\n  port: 9997\n", encoding="utf-8")
    cfg = load_config(conf)

    from zcode_gateway.api_dash import _config_path
    # --config prod.yml must be edited in place, not a decoy config.yaml
    assert _config_path(SimpleNamespace(cfg=cfg)) == conf


def test_dashboard_settings_write_to_the_loaded_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)          # no stray config.yaml in the cwd
    conf = tmp_path / "custom" / "prod.yml"
    conf.parent.mkdir()
    conf.write_text("server:\n  port: 9997\n", encoding="utf-8")
    cfg = load_config(conf)
    # hermetic: no real accounts/store/logs, no background pollers
    cfg.update.enabled = False
    cfg.pool.balance_poll_seconds = 0
    cfg.accounts.source = "pool_file"
    cfg.accounts.pool_file = str(tmp_path / "empty.json")
    (tmp_path / "empty.json").write_text('{"accounts": []}', encoding="utf-8")
    cfg.store.sqlite_path = str(tmp_path / "g.db")
    cfg.logging.capture_dir = str(tmp_path / "logs")

    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.put("/admin/dash/settings",
                       headers={"Authorization":
                                f"Bearer {cfg.server.admin_key}"},
                       json={"values": {"pool.cooldown_seconds": 77}})

    assert r.status_code == 200
    assert r.json()["changed"] == ["pool.cooldown_seconds"]
    written = yaml.safe_load(conf.read_text(encoding="utf-8"))
    assert written["pool"]["cooldown_seconds"] == 77
    assert not (tmp_path / "config.yaml").exists()   # no decoy file


# --------------------------------------------------------------------------- #
# log buffer
# --------------------------------------------------------------------------- #


def test_logbuffer_tail_survives_custom_level_names():
    buf = LogBuffer(capacity=10)
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", (), None)
    rec.levelname = "TRACE"              # getLevelName returns a string for this
    buf.emit(rec)

    # must not raise; a name below every floor is filtered out, not 500'd
    assert [r["message"] for r in buf.tail(level="INFO")] == []
    assert [r["message"] for r in buf.tail(level="")] == ["hello"]


def test_logbuffer_tail_filters_critical():
    buf = LogBuffer(capacity=10)
    for level, msg in ((logging.INFO, "i"), (logging.CRITICAL, "c")):
        buf.emit(logging.LogRecord("x", level, __file__, 1, msg, (), None))

    assert [r["message"] for r in buf.tail(level="CRITICAL")] == ["c"]


# --------------------------------------------------------------------------- #
# CORS: same-origin only
# --------------------------------------------------------------------------- #


def test_no_cors_headers_for_foreign_origins(tmp_path):
    app = create_app(_cfg(tmp_path))
    with TestClient(app) as client:
        r = client.get("/health", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# --------------------------------------------------------------------------- #
# /dash admin-key injection: loopback Host only (DNS rebinding)
# --------------------------------------------------------------------------- #


def test_dash_does_not_inject_the_admin_key_for_a_foreign_host(tmp_path):
    app = create_app(_cfg(tmp_path))
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        r = client.get("/dash", headers={"Host": "evil.example:8788"})
    assert r.status_code == 200
    assert "adm-test" not in r.text


def test_dash_does_not_inject_the_admin_key_for_a_non_loopback_peer(tmp_path):
    """A loopback Host from a NON-loopback TCP peer (local forwarder, ssh -R)
    must not receive the admin key either."""
    app = create_app(_cfg(tmp_path))
    with TestClient(app, client=("10.0.0.5", 50000)) as client:
        r = client.get("/dash", headers={"Host": "127.0.0.1:8788"})
    assert r.status_code == 200
    assert "adm-test" not in r.text


@pytest.mark.parametrize("host", ["127.0.0.1:8788", "localhost:8788",
                                  "[::1]:8788"])
def test_dash_injects_the_admin_key_for_loopback_hosts(tmp_path, host):
    app = create_app(_cfg(tmp_path))
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        r = client.get("/dash", headers={"Host": host})
    assert r.status_code == 200
    assert '"adm-test"' in r.text        # injected into the sessionStorage fallback


# --------------------------------------------------------------------------- #
# dialect-shaped auth errors on /v1
# --------------------------------------------------------------------------- #


def test_v1_auth_error_uses_the_openai_envelope(tmp_path):
    app = create_app(_cfg(tmp_path))
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": "Bearer nope"},
                        json={"model": "m",
                              "messages": [{"role": "user", "content": "hi"}]})
    body = r.json()
    assert r.status_code == 401
    assert "detail" not in body
    assert body["error"]["code"] == "invalid_api_key"
    assert body["error"]["type"] == "authentication_error"


def test_v1_messages_auth_error_uses_the_anthropic_envelope(tmp_path):
    app = create_app(_cfg(tmp_path))
    with TestClient(app) as client:
        r = client.post("/v1/messages",
                        headers={"x-api-key": "nope"},
                        json={"model": "m", "max_tokens": 16,
                              "messages": [{"role": "user", "content": "hi"}]})
    body = r.json()
    assert r.status_code == 401
    assert "detail" not in body
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"


def test_admin_auth_errors_keep_the_plain_detail_shape(tmp_path):
    app = create_app(_cfg(tmp_path))
    with TestClient(app) as client:
        r = client.get("/admin/pool/state",
                       headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 403
    assert r.json() == {"detail": "invalid admin key"}


# --------------------------------------------------------------------------- #
# zcode detection must not block the event loop
# --------------------------------------------------------------------------- #


async def _call_zcode_status(tmp_path):
    from zcode_gateway import api_dash, zcode_detect

    seen: dict[str, str] = {}

    def fake_detect():
        seen["thread"] = threading.current_thread().name
        return SimpleNamespace(
            installed=True, running=False, logged_in=False, install_path="",
            version="", email="", account_id="", expires_at_ms=0,
            token_valid=False, as_chip=lambda: ("chip", "kind"))

    original = zcode_detect.detect_zcode
    zcode_detect.detect_zcode = fake_detect
    try:
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(
                app_state=SimpleNamespace(cfg=Config()))))
        result = await api_dash.dash_zcode_status(request, key="x")
    finally:
        zcode_detect.detect_zcode = original
    return seen, result


def test_zcode_status_runs_detection_off_the_main_thread(tmp_path):
    import asyncio

    seen, result = asyncio.run(_call_zcode_status(tmp_path))
    assert result["installed"] is True
    assert seen["thread"] != "MainThread"


# --------------------------------------------------------------------------- #
# CLI + dashboard escaping
# --------------------------------------------------------------------------- #


def test_cli_no_longer_accepts_a_host_override():
    """--host bypassed the loopback check that gates admin-key injection."""
    from zcode_gateway.main import build_parser

    parser = build_parser()
    assert all("--host" not in action.option_strings
               for action in parser._actions)


def test_dashboard_esc_neutralises_single_quotes():
    html = (Path(__file__).resolve().parents[1] / "zcode_gateway" / "dashboard"
            / "index.html").read_text(encoding="utf-8")
    lines = html.splitlines()
    esc_line = next(l for l in lines if "const esc" in l)
    map_line = next(l for l in lines if "&amp;" in l and "&quot;" in l)
    assert "'" in esc_line          # the regex character class includes it
    assert "&#39;" in map_line      # ...and it maps to a safe entity
