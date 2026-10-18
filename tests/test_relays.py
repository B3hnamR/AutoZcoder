"""Unit tests for edge relays (Cloudflare, Vercel, Deno, Netlify) and model routing."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from zcode_gateway.relay_deploy import (
    deploy_cloudflare,
    deploy_deno,
    deploy_netlify,
    deploy_vercel,
    ping_relay,
)
from zcode_gateway.relay_manager import RelayManager
from zcode_gateway.upstream import UpstreamClient, UpstreamConfig


def test_relay_manager_defaults(tmp_path: Path):
    mgr = RelayManager(data_dir=tmp_path)
    st = mgr.get_settings()
    assert st["enabled"] is True
    # By default, only GLM-5.3 routes through relay!
    assert st["models"] == ["GLM-5.3"]
    assert st["active_url"] == ""
    assert st["relays"] == []


def test_relay_manager_add_and_select(tmp_path: Path):
    mgr = RelayManager(data_dir=tmp_path)
    entry = mgr.add_relay("my-cf", "https://relay.workers.dev", "cloudflare")
    assert entry["id"].startswith("relay-")
    assert entry["type"] == "cloudflare"

    st = mgr.get_settings()
    assert len(st["relays"]) == 1
    # First added relay automatically becomes active
    assert st["active_url"] == "https://relay.workers.dev"
    assert st["active_id"] == entry["id"]

    # Model routing check
    assert mgr.get_relay_for_model("GLM-5.3") == "https://relay.workers.dev"
    assert mgr.get_relay_for_model("glm-5.3") == "https://relay.workers.dev"
    # GLM-5.3-Flash is NOT routed by default
    assert mgr.get_relay_for_model("GLM-5.3-Flash") is None


def test_relay_manager_model_toggle(tmp_path: Path):
    mgr = RelayManager(data_dir=tmp_path)
    mgr.add_relay("cf", "https://relay.workers.dev", "cloudflare")

    # Add GLM-5.3-Flash to enabled models
    mgr.update_options(models=["GLM-5.3", "GLM-5.3-Flash"])
    assert mgr.get_relay_for_model("GLM-5.3") == "https://relay.workers.dev"
    assert mgr.get_relay_for_model("GLM-5.3-Flash") == "https://relay.workers.dev"

    # Disable relay master switch
    mgr.update_options(enabled=False)
    assert mgr.get_relay_for_model("GLM-5.3") is None
    assert mgr.get_relay_for_model("GLM-5.3-Flash") is None


def test_relay_manager_delete(tmp_path: Path):
    mgr = RelayManager(data_dir=tmp_path)
    r1 = mgr.add_relay("cf1", "https://cf1.workers.dev", "cloudflare")
    r2 = mgr.add_relay("cf2", "https://cf2.workers.dev", "cloudflare")

    assert mgr.get_settings()["active_id"] == r1["id"]
    mgr.delete_relay(r1["id"])
    st = mgr.get_settings()
    assert len(st["relays"]) == 1
    # Next relay becomes active
    assert st["active_id"] == r2["id"]
    assert st["active_url"] == r2["url"]


def test_test_relay_success(monkeypatch):
    import requests
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "ok"

    captured_req = {}
    def mock_get(url, headers=None, timeout=None, verify=None):
        captured_req["url"] = url
        captured_req["headers"] = headers
        return mock_resp

    monkeypatch.setattr(requests, "get", mock_get)
    res = ping_relay("https://my-relay.workers.dev")
    assert res["ok"] is True
    assert res["status"] == 200
    assert "elapsed_ms" in res
    assert captured_req["headers"]["x-relay-target"] == "https://httpbin.org"
    assert captured_req["headers"]["x-relay-path"] == "/get"


def test_deploy_cloudflare_mock(monkeypatch):
    import requests

    def mock_request(method, url, **kwargs):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        if "subdomain" in url and method == "get":
            resp.json.return_value = {"success": True, "result": {"subdomain": "my-sub"}}
        else:
            resp.json.return_value = {"success": True}
        return resp

    monkeypatch.setattr(requests, "put", lambda url, **kwargs: mock_request("put", url, **kwargs))
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: mock_request("post", url, **kwargs))
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: mock_request("get", url, **kwargs))

    res = deploy_cloudflare("acc123", "token_xyz", "my-cf")
    assert res["ok"] is True
    assert res["type"] == "cloudflare"
    assert res["url"] == "https://my-cf.my-sub.workers.dev"


@pytest.mark.asyncio
async def test_upstream_client_routes_through_relay(tmp_path: Path):
    mgr = RelayManager(data_dir=tmp_path)
    mgr.add_relay("cf", "https://cf-relay.workers.dev", "cloudflare")

    cfg = UpstreamConfig(base_url="https://zcode.z.ai/api/v1", chat_path="/zcode-plan/anthropic/v1/messages")
    client = UpstreamClient(cfg, relay_manager=mgr)

    # 1. For GLM-5.3, build_request targets relay_url with x-relay-target and x-relay-path
    req_model = "GLM-5.3"
    relay_url = client.relay_manager.get_relay_for_model(req_model)
    assert relay_url == "https://cf-relay.workers.dev"

    # 2. For GLM-5.3-Flash, it does NOT route through relay
    assert client.relay_manager.get_relay_for_model("GLM-5.3-Flash") is None
    await client.aclose()


# --------------------------------------------------------------------------- #
# rotation strategies
# --------------------------------------------------------------------------- #

def test_strategy_validation(tmp_path: Path):
    mgr = RelayManager(data_dir=tmp_path)
    mgr.update_options(strategy="round_robin")
    assert mgr.get_settings()["strategy"] == "round_robin"
    with pytest.raises(ValueError):
        mgr.update_options(strategy="bogus")


def test_strategy_single_uses_active(tmp_path: Path):
    mgr = RelayManager(data_dir=tmp_path)
    r1 = mgr.add_relay("cf1", "https://cf1.workers.dev", "cloudflare")
    mgr.add_relay("cf2", "https://cf2.workers.dev", "cloudflare")
    mgr.update_options(strategy="single", active_id=r1["id"])
    for _ in range(5):
        assert mgr.get_relay_for_model("GLM-5.3") == "https://cf1.workers.dev"


def test_strategy_round_robin_cycles(tmp_path: Path):
    mgr = RelayManager(data_dir=tmp_path)
    mgr.add_relay("cf1", "https://cf1.workers.dev", "cloudflare")
    mgr.add_relay("cf2", "https://cf2.workers.dev", "cloudflare")
    mgr.update_options(strategy="round_robin")
    picks = [mgr.get_relay_for_model("GLM-5.3") for _ in range(4)]
    assert picks == ["https://cf1.workers.dev", "https://cf2.workers.dev",
                     "https://cf1.workers.dev", "https://cf2.workers.dev"]


def test_strategy_random_picks_from_pool(tmp_path: Path):
    mgr = RelayManager(data_dir=tmp_path)
    mgr.add_relay("cf1", "https://cf1.workers.dev", "cloudflare")
    mgr.add_relay("cf2", "https://cf2.workers.dev", "cloudflare")
    mgr.update_options(strategy="random")
    allowed = {"https://cf1.workers.dev", "https://cf2.workers.dev"}
    picks = {mgr.get_relay_for_model("GLM-5.3") for _ in range(30)}
    assert picks.issubset(allowed) and len(picks) >= 1


def test_failed_relay_is_skipped_and_recovers(tmp_path: Path):
    mgr = RelayManager(data_dir=tmp_path)
    mgr.add_relay("cf1", "https://cf1.workers.dev", "cloudflare")
    r2 = mgr.add_relay("cf2", "https://cf2.workers.dev", "cloudflare")
    mgr.update_options(strategy="round_robin")

    # cool down cf1: rotation must fall to cf2 only
    mgr.mark_relay_failed("https://cf1.workers.dev")
    for _ in range(4):
        assert mgr.get_relay_for_model("GLM-5.3") == "https://cf2.workers.dev"

    # success on cf1 clears the failure: full rotation resumes
    mgr.mark_relay_ok("https://cf1.workers.dev")
    picks = {mgr.get_relay_for_model("GLM-5.3") for _ in range(10)}
    assert picks == {"https://cf1.workers.dev", "https://cf2.workers.dev"}


def test_all_relays_failed_still_routes(tmp_path: Path):
    """Everything cooling down must not disable the relay path entirely."""
    mgr = RelayManager(data_dir=tmp_path)
    mgr.add_relay("cf1", "https://cf1.workers.dev", "cloudflare")
    mgr.add_relay("cf2", "https://cf2.workers.dev", "cloudflare")
    mgr.mark_relay_failed("https://cf1.workers.dev")
    mgr.mark_relay_failed("https://cf2.workers.dev")
    assert mgr.get_relay_for_model("GLM-5.3") in {
        "https://cf1.workers.dev", "https://cf2.workers.dev"}

