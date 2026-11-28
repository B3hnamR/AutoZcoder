"""Bundled exit networks (Psiphon / Surfshark) as a generator IP source."""

import base64
import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from zcode_gateway import psiphon, surfshark, vpn
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


def test_worker_env_uses_a_ready_region(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    try:
        manager.set_options(ip_source="psiphon")
        manager.psiphon._settings.update({"regions": ["NL", "DE"], "mode": "spread"})
        manager.psiphon._instances.update({
            "NL": Exit(region="NL", port=47100, state="ready"),
            "DE": Exit(region="DE", port=47101, state="ready"),
        })
        first = manager._worker_env("g1")
        second = manager._worker_env("g2")
        assert first["ZAI_USE_POOL"] == "0" and first["ZAI_REQUIRE_PROXY"] == "1"
        # random, but never the same exit twice while another is free
        assert {first["ZAI_PROXY"], second["ZAI_PROXY"]} == {
            "socks5://127.0.0.1:47100", "socks5://127.0.0.1:47101"}
        from zcode_gateway.generator import GenWorker
        worker = GenWorker("g2", [], second, manager.gen_dir)
        assert worker.to_public()["route_label"].startswith("Psiphon \u00b7 ")
        assert "_GW_WORKER_ROUTE_LABEL" not in worker.env
    finally:
        manager.shutdown()



def test_spread_skips_the_previous_exit_across_providers(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    try:
        manager.set_options(ip_sources=["psiphon", "surfshark"], ip_mode="spread")
        manager.psiphon._settings.update({"regions": ["DE"], "mode": "spread"})
        manager.psiphon._instances["DE"] = Exit(region="DE", port=47100, state="ready")
        manager.surfshark._settings.update({"regions": ["nl-ams"], "mode": "spread"})
        manager.surfshark._instances["nl-ams"] = Exit(region="nl-ams", port=47400, state="ready")
        picks = [manager._exit_key(manager._pick_ip_exit()) for _ in range(6)]
        assert {p.split(":")[0] for p in picks} == {"psiphon", "surfshark"}
        assert all(a != b for a, b in zip(picks, picks[1:]))
    finally:
        manager.shutdown()


def test_spread_draws_the_provider_before_the_region(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    try:
        manager.set_options(ip_sources=["psiphon", "surfshark"], ip_mode="spread")
        manager.psiphon._settings.update({"regions": ["DE", "ES", "FR"], "mode": "spread"})
        for index, region in enumerate(("DE", "ES", "FR")):
            manager.psiphon._instances[region] = Exit(region=region, port=47100 + index, state="ready")
        manager.surfshark._settings.update({"regions": ["nl-ams", "us-bna"], "mode": "spread"})
        for index, region in enumerate(("nl-ams", "us-bna")):
            manager.surfshark._instances[region] = Exit(region=region, port=47400 + index, state="ready")
        from zcode_gateway import generator
        draws = []

        def draw(choices):
            draws.append(choices)
            return choices[-1]

        monkeypatch.setattr(generator.random, "choice", draw)
        assert manager._exit_key(manager._pick_ip_exit()) == "surfshark:us-bna"
        assert draws[0] == ["psiphon", "surfshark"]
        assert [e["region"] for e in draws[1]] == ["nl-ams", "us-bna"]
    finally:
        manager.shutdown()


def test_stay_on_one_keeps_the_chosen_provider_exit(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    try:
        manager.set_options(ip_sources=["direct", "psiphon", "surfshark"], ip_mode="single",
                            ip_pick="surfshark:nl-ams")
        manager.psiphon._settings.update({"regions": ["DE"], "mode": "spread"})
        manager.psiphon._instances["DE"] = Exit(region="DE", port=47100, state="ready")
        manager.surfshark._settings.update({"regions": ["nl-ams"], "mode": "spread"})
        manager.surfshark._instances["nl-ams"] = Exit(region="nl-ams", port=47400, state="ready")
        assert [manager._worker_env(f"g{i}")["ZAI_PROXY"] for i in range(3)] == [
            "socks5://127.0.0.1:47400"] * 3
        manager.surfshark._instances.clear()
        assert manager._worker_env("g4")["ZAI_PROXY"] == "socks5://127.0.0.1:47100"
        assert set(manager.options()["ip_sources"]) == {"direct", "psiphon", "surfshark"}
        assert [c["key"] for c in manager.options()["ip_exits"]] == ["psiphon:DE"]
    finally:
        manager.shutdown()


def test_sources_accept_separated_strings_and_status_omits_vpn_inventory(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    try:
        manager.set_options(ip_sources="psiphon surfshark")
        assert manager.ip_sources == ["psiphon", "surfshark"]
        manager.set_options(ip_sources="pool,psiphon")
        assert manager.ip_sources == ["pool", "psiphon"]
        # The 3s status poll must not carry the whole server list; /vpn/status
        # still does.
        assert "vpn" not in manager.status()
        assert set(manager.vpn_status()) == {"psiphon", "surfshark"}
    finally:
        manager.shutdown()


@pytest.mark.parametrize("mode", ["spread", "single"])
def test_selected_vpn_modes_only_use_vpn_exits(tmp_path, monkeypatch, mode):
    manager = _manager(tmp_path, monkeypatch)
    try:
        manager.psiphon._settings.update({"regions": ["DE"], "mode": "spread"})
        manager.psiphon._instances["DE"] = Exit(region="DE", port=47100, state="ready")
        manager.set_options(ip_source="direct")
        assert manager.options()["ip_exits"] == [
            {"key": "psiphon:DE", "label": "Psiphon \u00b7 Germany (DE)"}]
        manager.set_options(ip_sources=["direct", "psiphon"], ip_mode=mode, ip_pick="direct")
        assert manager._worker_env("g1")["ZAI_PROXY"] == "socks5://127.0.0.1:47100"
        manager.psiphon._instances.clear()
        assert manager._pick_ip_exit() is None
    finally:
        manager.shutdown()


def test_start_is_refused_when_the_exit_network_is_not_connected(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    try:
        manager.set_options(ip_source="surfshark")
        monkeypatch.setattr(manager, "_mail_ready", lambda _: True)
        # the AI-captcha account check must not depend on the host's accounts
        monkeypatch.setattr(manager, "_pool_health", lambda: {"total": 1, "active": 1, "ready": True})
        monkeypatch.setattr(manager, "_ensure_display", lambda interactive=False: "")
        result = manager.start_workers(1)
        assert "Surfshark" in result["error"]
        assert "not connected" in result["error"]
    finally:
        manager.shutdown()


def test_batch_is_not_launched_when_vpn_disappears_during_selection(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    try:
        manager.set_options(ip_source="psiphon")
        monkeypatch.setattr(manager, "_ensure_display", lambda interactive=False: "")
        monkeypatch.setattr(manager, "_mail_ready", lambda _: True)
        monkeypatch.setattr(manager, "_pool_health", lambda: {"total": 1, "active": 1, "ready": True})
        candidates = iter([[Exit(region="NL", port=47100, state="ready")], []])
        monkeypatch.setattr(manager.psiphon, "ready", lambda: next(candidates, []))
        assert "Psiphon" in manager.start_workers(2)["error"]
        assert not manager.workers
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


def test_disconnected_instance_cannot_spawn_after_stop(tmp_path, monkeypatch):
    pool = PsiphonPool(tmp_path)
    inst = Exit(region="NL", port=47100)
    pool._instances[inst.region] = inst
    spawned = []
    monkeypatch.setattr(pool, "_spawn", spawned.append)
    pool.disconnect()
    pool._run(inst)
    assert not spawned


def test_surfshark_spawn_failure_removes_temporary_config(tmp_path, monkeypatch):
    pool = SurfsharkPool(tmp_path)
    inst = Exit(region="nl-ams", port=47400)
    config = tmp_path / "wireproxy-test.conf"
    config.touch()
    inst.extra["config_file"] = str(config)
    pool._instances[inst.region] = inst

    def fail(_):
        raise OSError("client did not start")

    monkeypatch.setattr(pool, "_spawn", fail)
    pool._run(inst)
    assert inst.state == "failed"
    assert not config.exists()


def test_saved_vpn_autostarts_without_opening_dashboard(tmp_path, monkeypatch):
    gen = tmp_path / "generator"
    gen.mkdir()
    (gen / "main.py").touch()
    (gen / "config.py").touch()
    PsiphonPool(gen / "output").set_settings({"autoStart": True, "regions": ["NL"]})
    started = []
    monkeypatch.setattr(PsiphonPool, "connect", lambda pool: started.append(pool.config["regions"]))
    app = _app(tmp_path)
    app.state.cfg.generator.zcodeauto_dir = str(gen)
    assert not app.state.cfg.claims.daily_enabled
    with TestClient(app):
        assert started == [["NL"]]


def test_surfshark_health_probe_marks_unreachable_exit_and_recovers(tmp_path, monkeypatch):
    pool = SurfsharkPool(tmp_path)
    inst = Exit("nl-ams", 47400, state="ready", exit_ip="192.0.2.1",
                proc=SimpleNamespace(poll=lambda: None))
    pool._instances[inst.region] = inst
    monkeypatch.setattr(vpn.threading, "Thread", lambda *, target, **_: SimpleNamespace(start=target))
    monkeypatch.setattr(vpn.time, "sleep", lambda _: None)
    monkeypatch.setattr(vpn, "trace_exit", lambda _: None)
    assert pool.status()["live"] == 0
    assert inst.state == "reconnecting" and inst.exit_ip == ""
    monkeypatch.setattr(vpn, "trace_exit", lambda _: {"ip": "192.0.2.2", "loc": "NL"})
    inst.extra["last_probe"] -= vpn.HEALTH_PROBE_INTERVAL
    assert pool.status()["live"] == 1
    assert inst.exit_ip == "192.0.2.2"


def test_surfshark_refresh_stops_retired_locations(tmp_path, monkeypatch):
    import requests

    pool = SurfsharkPool(tmp_path)
    pool.set_settings({"regions": ["nl-ams", "de-ber"]})
    retired = Exit("de-ber", 47401, state="ready")
    pool._instances.update({"de-ber": retired, "nl-ams": Exit("nl-ams", 47400, state="ready")})
    monkeypatch.setattr(surfshark.SurfsharkPool, "_parse_servers", staticmethod(lambda _: [{"id": "nl-ams"}]))
    monkeypatch.setattr(requests, "get", lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: []))
    status = pool.update()
    assert retired.stop_requested
    assert status["config"]["regions"] == ["nl-ams"]
    assert status["live"] == 1


def test_surfshark_renew_skips_default_servers_in_use(tmp_path, monkeypatch):
    pool = SurfsharkPool(tmp_path)
    pool.set_settings({"regions": ["nl-ams", "nl-rtm"], "servers": [
        {"id": server, "countryCode": "NL"} for server in ("nl-ams", "nl-rtm", "nl-unused")]})
    pool._instances.update({"nl-ams": Exit("nl-ams", 47400), "nl-rtm": Exit("nl-rtm", 47401)})
    started = []
    monkeypatch.setattr(pool, "_start", lambda region, fresh: started.append(pool.server_for(region)["id"]))
    pool.renew("nl-ams")
    assert started == ["nl-unused"]
    assert pool.server_for("nl-rtm")["id"] == "nl-rtm"

    pool.set_settings({"servers": pool.servers()[1:]})
    with pytest.raises(RuntimeError, match="No unused Surfshark server"):
        pool.renew("nl-ams")
    assert started == ["nl-unused"]


def test_surfshark_key_change_restarts_starting_connections(tmp_path, monkeypatch):
    pool = SurfsharkPool(tmp_path)
    monkeypatch.setattr(surfshark, "public_key_of", lambda key: f"public-{key}")
    pool.set_keys("test-old")
    pool.set_settings({"regions": ["nl-ams"]})
    pool._instances["nl-ams"] = Exit("nl-ams", 47400)
    restarted = []
    monkeypatch.setattr(pool, "_start", lambda region, **kw: restarted.append(region))
    pool.set_keys("test-new")
    assert restarted == ["nl-ams"]


@pytest.mark.parametrize("reports_regions", [True, False])
def test_psiphon_refresh_requires_a_region_list(tmp_path, monkeypatch, reports_regions):
    pool = PsiphonPool(tmp_path)
    (tmp_path / "psiphon.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pool, "find_client", lambda: tmp_path / "client")
    monkeypatch.setattr(vpn, "free_port", lambda *args: 47190)

    def notices():
        yield b'{"noticeType":"Tunnels","data":{"count":1}}'
        if reports_regions:
            yield b'{"noticeType":"AvailableEgressRegions","data":{"regions":["NL"]}}'
            yield b'{"noticeType":"AvailableEgressRegions","data":{"regions":["DE"]}}'

    monkeypatch.setattr(psiphon.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(
        stdout=notices(), terminate=lambda: None, wait=lambda **_: 0))
    if reports_regions:
        assert pool.update()["available"] == ["NL"]
    else:
        with pytest.raises(RuntimeError, match="did not report any regions"):
            pool.update()
