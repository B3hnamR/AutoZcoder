import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from zcode_gateway import api_admin, api_dash, updater
from zcode_gateway.config import Config
from zcode_gateway.pool import Account, AccountState, PoolManager
from zcode_gateway.registry import Registry, model_caps
from zcode_gateway.service import UpstreamFailure
from zcode_gateway.upstream import UpstreamClient
from zcode_gateway.zcode_protocol import build_body

from test_inference_audit import START, STOP, response, service_for


@pytest.mark.asyncio
async def test_glm54_native_vision_and_wire_contract():
    registry = Registry()
    model = registry.resolve("glm-5.4", "anthropic")
    assert model == "GLM-5.4"
    assert registry.knows(model)
    assert any(entry["id"] == model and entry["vision"] for entry in registry.catalogue())
    image = {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "fixture",
    }}
    body = build_body({
        "model": model,
        "_anthropic": {
            "model": "glm-5.4", "max_tokens": 200_000,
            "messages": [{"role": "user", "content": [image]},
                         {"role": "system", "content": "fixture context"}],
            "output_config": {"effort": "max"},
        },
    }, model_caps=model_caps(model), harness=False)
    seen = {}

    def respond(request):
        seen.update(json.loads(request.content))
        assert request.headers["anthropic-beta"] == "mid-conversation-system-2026-04-07"
        return response(START, STOP)

    cfg = Config()
    cfg.models.pacing.enabled = False
    client = UpstreamClient(cfg.upstream, pacing=cfg.models.pacing)
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        result = await client.send({}, body, stream=True)
        await result.aclose()
    finally:
        await client.aclose()
    assert seen["model"] == "glm-5.4"
    assert body["model"] == "GLM-5.4"
    assert seen["max_tokens"] == 131_072
    assert seen["messages"][0]["content"][0] == image
    assert seen["messages"][0]["content"][1]["text"] == "[Image: source: image.png]"
    assert seen["messages"][1]["role"] == "system"
    assert seen["output_config"] == {"effort": "max"}


def test_quota_ignores_retired_bucket_and_preserves_supported_amounts():
    """A historical balance may still carry a bucket for the retired model."""
    from zcode_gateway.quota import parse_balance

    now = time.time()
    data = {
        "plans": [{
            "user_plan_id": "plan", "plan_id": "zcode-v3-start-plan-0817",
            "status": "active", "starts_at": now - 60, "ends_at": now + 3600,
            "entitlements": [{
                "entitlement_id": "ent-zcode-v3-start-plan-0817-1",
                "show_name": "GLM-5.4", "capabilities": ["model:glm-5.4"],
                "grant_units": 3_000_000, "period": "daily",
            }],
        }],
        "balances": [
            {"user_plan_id": "plan", "entitlement_id": "ent_2_0817_glm_5p3",
             "show_name": "", "remaining_units": 3_000_000, "total_units": 3_000_000,
             "used_units": 0, "period_end": now + 3600},
            {"user_plan_id": "plan", "entitlement_id": "ent-zcode-v3-start-plan-0817-1",
             "show_name": "GLM-5.4", "capabilities": ["model:glm-5.4"],
             "remaining_units": 2_842_979, "total_units": 3_000_000,
             "used_units": 157_021, "period_end": now + 3600},
            {"user_plan_id": "plan", "entitlement_id": "ent_2_0817_glm_5p3f",
             "show_name": "GLM-5.3-Flash", "remaining_units": 5_000_000,
             "total_units": 5_000_000, "used_units": 0, "period_end": now + 3600},
        ],
    }
    _, quotas = parse_balance(data, now)
    assert set(quotas) == {"GLM-5.4", "GLM-5.3-Flash"}
    assert quotas["GLM-5.4"]["remaining"] == 2_842_979
    assert quotas["GLM-5.3-Flash"]["remaining"] == 5_000_000


def test_quota_capability_wins_over_misleading_show_name():
    from zcode_gateway.quota import bucket_model

    assert bucket_model({"show_name": "GLM-5.3", "capabilities": ["model:glm-5.4"]}) == "GLM-5.4"
    assert bucket_model({"show_name": "GLM-5.3-Flash", "capabilities": ["model:glm-5.3"]}) is None
    assert bucket_model({"show_name": "GLM-5.3", "entitlement_id": "ent_2_0817_glm_5p3f"}) is None
    assert bucket_model({"show_name": "unknown", "entitlement_id": "ent_2_0817_glm_5p3f"}) \
        == "GLM-5.3-Flash"
    assert bucket_model({"show_name": "unknown", "entitlement_id": "x"}) is None


@pytest.mark.asyncio
async def test_sse_invalid_request_does_not_rotate_or_cool_accounts():
    event = {"type": "error", "error": {"type": "invalid_request_error", "message": "fixture"}}
    accounts = [Account(id="a", access_token="fixture-a"), Account(id="b", access_token="fixture-b")]
    service, _ = service_for([response(event), response(START, STOP)], accounts)
    try:
        with pytest.raises(UpstreamFailure) as error:
            await service.open_stream({"messages": [{"role": "user", "content": "fixture"}]}, model="GLM-5.4")
        assert error.value.status == 400
        assert len(service.client.calls) == 1
        assert all(a.state is AccountState.READY and a.in_flight == 0 for a in accounts)
    finally:
        await service.pool.aclose()


@pytest.mark.asyncio
async def test_purge_keeps_pending_account_before_and_after_window_start(tmp_path):
    now = time.time()
    snapshot = tmp_path / "pending.txt"
    snapshot.write_text("dummy", encoding="utf-8")
    quota = {"remaining": 0, "resets_at": 0, "pending_remaining": 100, "pending_starts_at": now + 10}
    account = Account(id="a", access_token="fixture", notes={
        "file": snapshot.name, "quotas": {"GLM-5.4": quota},
    })
    cfg = Config()
    cfg.accounts.dir = str(tmp_path)
    pool = PoolManager(cfg.pool, [account])
    state = SimpleNamespace(pool=pool, cfg=cfg)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(app_state=state)))
    try:
        assert not account.model_eligible("GLM-5.4", now)
        assert account.model_eligible("GLM-5.4", now + 11)
        assert (await api_admin.purge_drained_accounts(request, key="fixture"))["count"] == 0
        quota["pending_starts_at"] = now - 10
        assert (await api_admin.purge_drained_accounts(request, key="fixture"))["count"] == 0
        assert snapshot.exists()
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_staged_update_must_match_latest_and_failed_apply_can_redownload(tmp_path, monkeypatch):
    current = {"update_available": True, "latest": "99.0.0", "download_url": "fixture-a"}
    state = SimpleNamespace(
        cfg=Config(), updater=SimpleNamespace(status=current), generator=None,
        pool=SimpleNamespace(snapshot=AsyncMock(return_value={"detail": []})),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        app_state=state, request_shutdown=lambda: None,
    )))
    path = tmp_path / updater.NEW_EXE_NAME
    path.write_bytes(b"fixture")
    download = AsyncMock(return_value=path)
    apply = AsyncMock(side_effect=AssertionError("must not stage a mismatched release"))
    monkeypatch.setattr(api_dash, "_windows_update_only", lambda: None)
    monkeypatch.setattr(updater, "download_update", download)
    monkeypatch.setattr(updater, "apply_update", apply)
    monkeypatch.setattr(updater, "exe_dir", lambda: tmp_path)
    await api_dash.update_download(request, key="fixture")
    current.update(latest="99.1.0", download_url="fixture-b")
    assert not (await api_dash.update_status(request, key="fixture"))["downloaded"]
    with pytest.raises(HTTPException, match="Download the current release"):
        await api_dash.update_apply(request, key="fixture")
    apply.assert_not_called()

    await api_dash.update_download(request, key="fixture")
    assert download.await_args.args[0] == "fixture-b"

    def bad_checksum(*args, **kwargs):
        raise RuntimeError("fixture checksum failed")

    monkeypatch.setattr(updater, "apply_update", bad_checksum)
    with pytest.raises(HTTPException, match="fixture checksum failed"):
        await api_dash.update_apply(request, key="fixture")
    assert not (await api_dash.update_status(request, key="fixture"))["downloaded"]
    await api_dash.update_download(request, key="fixture")
    assert download.await_count == 3



@pytest.mark.parametrize("entitlement_id", ["ent_legacy_glm_5p4f", "ent_legacy_glm_5p30"])
def test_unknown_legacy_id_is_not_credited_to_a_shorter_model_id(entitlement_id):
    from zcode_gateway.quota import bucket_model

    assert bucket_model({"show_name": "", "entitlement_id": entitlement_id}) is None


@pytest.mark.parametrize("enabled", [True, False])
def test_service_honors_user_cache_setting_without_removing_image_marker(enabled):
    service, _ = service_for([])
    service.cfg.upstream.anthropic_cache_control = enabled
    request = {
        "model": "GLM-5.4",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,fixture"}}
        ]}],
    }
    marker = service._build(request)["messages"][0]["content"][-1]
    assert marker["text"] == "[Image: source: image.png]"
    assert ("cache_control" in marker) == enabled


def test_retired_model_is_removed_from_existing_settings(tmp_path):
    from zcode_gateway.config import load_config
    from zcode_gateway.relay_manager import RelayManager

    path = tmp_path / "config.yaml"
    path.write_text("""models:
  default: primary
  default_anthropic: glm-5.3
  aliases:
    primary: GLM-5.3
    glm-5.3: GLM-5.4
    flash: GLM-5.3-Flash
  auto_free_fallback:
    enabled: true
    chain: [primary, GLM-5.3, GLM-5.3-Flash]
""", encoding="utf-8")
    models = load_config(path).models
    assert models.default == models.default_anthropic == "GLM-5.4"
    assert models.aliases == {"flash": "GLM-5.3-Flash"}
    assert models.auto_free_fallback.chain == ["GLM-5.4", "GLM-5.3-Flash"]

    relay_dir = tmp_path / "relay"
    relay_dir.mkdir()
    (relay_dir / "relays.json").write_text(json.dumps({
        "enabled": True, "models": ["glm-5.3", "GLM-5.3-Flash"],
    }), encoding="utf-8")
    relays = RelayManager(relay_dir)
    assert relays.get_settings()["models"] == ["GLM-5.3-Flash"]
    assert not relays._model_matches("GLM-5.4", relays.get_settings()["models"])
