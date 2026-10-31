"""Regression coverage for privileged hosts, credentials and secure config writes."""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from zcode_gateway.api_dash import _atomic_write_yaml
from zcode_gateway.app import create_app
from zcode_gateway.config import Config, ServerConfig
from zcode_gateway.deps import admin_key


def _app(tmp_path, **server):
    return create_app(Config(
        server={"admin_key": "audit-admin-credential", **server},
        accounts={"dir": str(tmp_path / "accounts")},
        store={"sqlite_path": str(tmp_path / "gateway.db")},
        update={"enabled": False}))


def test_atomic_config_replacement_retains_private_permissions(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("server: {}\n")
    path.chmod(0o600)
    old_umask = os.umask(0o022)
    try:
        _atomic_write_yaml(path, {"server": {"admin_key": "dummy-secret"}})
    finally:
        os.umask(old_umask)
    assert yaml.safe_load(path.read_text())["server"]["admin_key"] == "dummy-secret"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("path", ["/admin/dash/settings", "/dash", "/health"])
def test_untrusted_host_rejected_even_on_get(tmp_path, path):
    with TestClient(_app(tmp_path)) as client:
        response = client.get(path, headers={"Host": "rebound.attacker.invalid",
                                             "Authorization": "Bearer audit-admin-credential"})
    assert response.status_code == 400


def test_public_static_internal_token_no_longer_authenticates():
    state = SimpleNamespace(cfg=Config(server={"admin_key": "audit-admin-credential"}),
                            internal_token="unpredictable-test-run-token")
    app = SimpleNamespace(state=SimpleNamespace(app_state=state))
    request = Request({"type": "http", "method": "GET", "path": "/admin/accounts",
                       "headers": [(b"authorization", b"Bearer gateway-internal")],
                       "client": ("127.0.0.1", 1234), "app": app})
    with pytest.raises(HTTPException):
        asyncio.run(admin_key(request))


def test_explicit_trusted_host_preserves_remote_dashboard(tmp_path):
    with TestClient(_app(tmp_path, allowed_hosts=["gateway.example.test"])) as client:
        response = client.get("/admin/dash/settings", headers={
            "Host": "gateway.example.test", "Authorization": "Bearer audit-admin-credential"})
    assert response.status_code == 200


def test_missing_config_locate_does_not_replace_file(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("[broken yaml")
    gen = tmp_path / "generator"
    gen.mkdir()
    (gen / "main.py").touch()
    (gen / "config.py").touch()
    app = _app(tmp_path)
    app.state.cfg._config_file = cfg
    with TestClient(app) as client:
        response = client.post("/admin/dash/generator/locate", json={"path": str(gen)},
                               headers={"Authorization": "Bearer audit-admin-credential"})
    assert response.status_code == 409
    assert cfg.read_text() == "[broken yaml"


def test_tls_child_origin_uses_certificate_identity(monkeypatch):
    server = ServerConfig(tls_cert="dummy-cert.pem", tls_key="dummy-key.pem", host="0.0.0.0", port=8443)
    monkeypatch.setattr(ServerConfig, "_certificate_hosts", lambda self: ["203.0.113.5"])
    assert server.child_connection() == ("https://203.0.113.5:8443", "dummy-cert.pem")
    assert "203.0.113.5" in server.trusted_hosts()


def test_invalid_child_origins_are_rejected():
    with pytest.raises(ValueError):
        ServerConfig(child_origin="https://user:secret@host/path").child_connection()
    with pytest.raises(ValueError):
        ServerConfig(tls_cert="cert", tls_key="key", child_origin="http://localhost:8443").child_connection()


def test_saved_route_survives_actual_api_manager_creation(tmp_path, monkeypatch):
    from zcode_gateway import api_dash, generator
    gen = tmp_path / "generator"
    gen.mkdir()
    (gen / "main.py").touch()
    (gen / "config.py").touch()
    monkeypatch.setattr(generator, "resolve_python", lambda explicit="": "/usr/bin/python3")
    seed = generator.GeneratorManager(gen, data_dir=gen / "output")
    seed.set_options(ip_source="pin", pin_proxy="socks5://proxy.example.test:1080",
                     proxy_types="socks5", claim_start=False)
    seed.shutdown()
    app = _app(tmp_path)
    app.state.cfg.generator.zcodeauto_dir = str(gen)
    with TestClient(app) as client:
        response = client.get("/admin/dash/generator/status", headers={
            "Authorization": "Bearer audit-admin-credential"})
        assert response.status_code == 200
        manager = app.state.app_state.generator
        assert manager.ip_source == "pin"
        assert manager.pin_proxy == "socks5://proxy.example.test:1080"
        assert manager.proxy_types == "socks5"
        assert not manager.claim_start


def test_setup_created_before_manager_publishes_interpreter(tmp_path):
    from zcode_gateway.api_dash import _env_setup_shared
    app = _app(tmp_path)
    with TestClient(app):
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})
        setup = _env_setup_shared(request)
        setup.on_python("/dummy/completed/python")
        assert app.state.app_state.cfg.generator.python_exe == "/dummy/completed/python"


def test_internal_token_only_expands_dedicated_callbacks(tmp_path):
    app = _app(tmp_path)
    with TestClient(app, client=("203.0.113.5", 1234)) as client:
        auth = {"Authorization": "Bearer " + app.state.app_state.internal_token}
        assert client.post("/admin/dash/notifications/emit", json={"message": "dummy"}, headers=auth).status_code == 200
        assert client.post("/admin/dash/generator/send_claim_message", json={}, headers=auth).status_code == 400
        assert client.get("/admin/dash/settings", headers=auth).status_code == 403


def test_invalid_callback_configuration_fails_before_manager_creation(tmp_path, monkeypatch):
    from zcode_gateway import generator
    app = _app(tmp_path)
    app.state.cfg.server.child_origin = "http://invalid.example.test/path"
    monkeypatch.setattr(generator, "GeneratorManager", lambda *a, **kw: pytest.fail("manager must not be created"))
    with TestClient(app) as client:
        response = client.get("/admin/dash/generator/status", headers={
            "Authorization": "Bearer audit-admin-credential"})
        assert response.status_code in (200, 409)
        assert "callback configuration" in response.text
        assert getattr(app.state.app_state, "generator", None) is None


def test_concurrent_config_transactions_preserve_both_updates(tmp_path, monkeypatch):
    from zcode_gateway import api_dash
    import threading
    config_path = tmp_path / "config.yaml"
    config_path.write_text("pool:\n  cooldown_seconds: 30\nlogging:\n  level: INFO\n")
    app = _app(tmp_path)
    app.state.cfg._config_file = config_path
    entered, release = threading.Event(), threading.Event()
    original = api_dash._atomic_write_yaml
    calls = []

    def delayed(path, data):
        calls.append(data)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=5)
        return original(path, data)

    monkeypatch.setattr(api_dash, "_atomic_write_yaml", delayed)
    with TestClient(app):
        request = Request({"type": "http", "method": "PUT", "path": "/", "headers": [], "app": app})

        async def save_both():
            first = asyncio.create_task(api_dash.dash_settings_put(request, {"values": {"pool.cooldown_seconds": 31}}, "admin"))
            assert await asyncio.to_thread(entered.wait, 5)
            second = asyncio.create_task(api_dash.dash_settings_put(request, {"values": {"logging.level": "WARNING"}}, "admin"))
            await asyncio.sleep(0.02)
            release.set()
            await asyncio.gather(first, second)
        asyncio.run(save_both())
    data = yaml.safe_load(config_path.read_text())
    assert data["pool"]["cooldown_seconds"] == 31
    assert data["logging"]["level"] == "WARNING"
