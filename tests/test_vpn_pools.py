"""Bundled exit networks (Psiphon / Surfshark) as a generator IP source."""

import base64
import json
import os

import pytest
from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
from zcode_gateway.config import Config
from zcode_gateway.psiphon import PsiphonPool
from zcode_gateway.surfshark import SurfsharkPool, public_key_of
from zcode_gateway.vpn import Exit

HEADERS = {"Authorization": "Bearer vpn-admin-credential"}


def _app(tmp_path):
    return create_app(Config(
        server={"admin_key": "vpn-admin-credential"},
        accounts={"dir": str(tmp_path / "accounts")},
        store={"sqlite_path": str(tmp_path / "gateway.db")},
        update={"enabled": False}))


def _manager(tmp_path, monkeypatch, data_dir=None):
    from zcode_gateway import generator
    gen = tmp_path / "generator"
    gen.mkdir(exist_ok=True)
    (gen / "main.py").touch()
    (gen / "config.py").touch()
    monkeypatch.setattr(generator, "resolve_python", lambda explicit="": "/usr/bin/python3")
    return generator.GeneratorManager(gen, data_dir=data_dir or (tmp_path / "data"))


def test_surfshark_public_key_matches_rfc7748():
    private = base64.b64encode(bytes.fromhex(
        "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")).decode()
    expected = base64.b64encode(bytes.fromhex(
        "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")).decode()
    assert public_key_of(private) == expected


def test_surfshark_rejects_a_mismatched_pair(tmp_path):
    pool = SurfsharkPool(tmp_path)
    private = base64.b64encode(bytes(range(32))).decode()
    other = base64.b64encode(bytes(range(1, 33))).decode()
    with pytest.raises(ValueError):
        pool.set_keys(private, other)
    pool.set_keys(private)
    assert pool.has_keys
    assert pool.keys_public()["publicKey"] == public_key_of(private)


def test_psiphon_normalizes_regions_and_single_mode(tmp_path):
    pool = PsiphonPool(tmp_path)
    pool.set_settings({"regions": ["nl", "DE", "nl", "x"], "mode": "single"})
    config = pool.status()["config"]
    assert config["regions"] == ["DE", "NL"]
    assert config["pinned"] == "DE"


def test_settings_file_keeps_the_private_key_out_of_status(tmp_path):
    pool = SurfsharkPool(tmp_path)
    private = base64.b64encode(bytes(range(32))).decode()
    pool.set_keys(private)
    stored = json.loads((tmp_path / "vpn.json").read_text(encoding="utf-8"))
    assert stored["surfshark"]["privateKey"] == private
    if os.name != "nt":
        assert (tmp_path / "vpn.json").stat().st_mode & 0o777 == 0o600
    status = pool.status()
    assert "privateKey" not in json.dumps(status)
    assert status["keys"] == {"saved": True, "publicKey": public_key_of(private)}


def test_pick_takes_turns_and_single_mode_stays_pinned(tmp_path):
    pool = PsiphonPool(tmp_path)
    pool.set_settings({"regions": ["NL", "DE"], "mode": "spread"})
    pool._instances.update({
        "NL": Exit(region="NL", port=47100, state="ready"),
        "DE": Exit(region="DE", port=47101, state="ready"),
    })
    # Regions are normalized to sorted order, so the rotation is deterministic.
    assert [pool.pick().region for _ in range(3)] == ["DE", "NL", "DE"]
    pool.set_settings({"mode": "single", "pinned": "DE"})
    assert [pool.pick().region for _ in range(3)] == ["DE", "DE", "DE"]
    assert pool.live() == 2


def test_unavailable_explains_what_to_do(tmp_path):
    pool = PsiphonPool(tmp_path)
    pool.set_settings({"regions": ["NL"], "mode": "spread"})
    assert "not connected" in pool.unavailable()


def test_worker_env_uses_the_next_ready_region(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    try:
        manager.set_options(ip_source="psiphon")
        manager.psiphon._settings.update({"regions": ["NL", "DE"], "mode": "spread"})
        manager.psiphon._instances.update({
            "NL": Exit(region="NL", port=47100, state="ready"),
            "DE": Exit(region="DE", port=47101, state="ready"),
        })
        env = manager._worker_env("g1")
        assert env["ZAI_PROXY"] == "socks5://127.0.0.1:47100"
        assert env["ZAI_USE_POOL"] == "0"
        assert manager._worker_env("g2")["ZAI_PROXY"] == "socks5://127.0.0.1:47101"
    finally:
        manager.shutdown()


def test_start_is_refused_when_the_exit_network_is_not_connected(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    try:
        manager.set_options(ip_source="surfshark")
        monkeypatch.setattr(manager, "_ensure_display", lambda interactive=False: "")
        result = manager.start_workers(1)
        assert "Surfshark" in result["error"]
        assert "not connected" in result["error"]
    finally:
        manager.shutdown()


def test_vpn_endpoints_report_and_save(tmp_path):
    app = _app(tmp_path)
    gen = tmp_path / "generator"
    gen.mkdir()
    (gen / "main.py").touch()
    (gen / "config.py").touch()
    app.state.cfg.generator.zcodeauto_dir = str(gen)
    with TestClient(app) as client:
        status = client.get("/admin/dash/generator/vpn/status", headers=HEADERS)
        assert status.status_code == 200
        assert set(status.json()) == {"psiphon", "surfshark"}
        assert "privateKey" not in status.text

        saved = client.post("/admin/dash/generator/vpn/psiphon/settings", headers=HEADERS,
                            json={"regions": ["nl", "de"], "mode": "single"})
        assert saved.status_code == 200
        config = saved.json()["config"]
        assert config["regions"] == ["DE", "NL"] and config["pinned"] == "DE"

        missing = client.post("/admin/dash/generator/vpn/psiphon/renew", headers=HEADERS,
                              json={"region": "US"})
        assert missing.status_code == 400
        assert client.post("/admin/dash/generator/vpn/psiphon/renew", headers=HEADERS,
                           json={}).status_code == 400

        bad_key = client.post("/admin/dash/generator/vpn/surfshark/keys", headers=HEADERS,
                              json={"privateKey": "not-a-key"})
        assert bad_key.status_code == 400

        assert client.get("/admin/dash/generator/vpn/status").status_code == 403
