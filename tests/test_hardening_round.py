"""Tests for the 2026-10 hardening round.

Covers: edge-block breaker, capability layer (image strip + max_tokens clamp),
pacing gate, reasoning default effort, anthropic-beta merge, device-login
poll_token tolerance, and aggregate reasoning keys.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from zcode_gateway.breaker import EdgeBlockBreaker
from zcode_gateway.registry import Registry, MODEL_CAPS, model_caps
from zcode_gateway.upstream import merge_anthropic_beta, UpstreamClient
from zcode_gateway.config import UpstreamConfig, PacingConfig
from zcode_gateway.zcode_protocol import build_body
from zcode_gateway.aggregate import aggregate_openai_stream


EDGE_403 = ("<html><head><title>403 Forbidden</title></head><body>"
            "<h2>Your client does not have permission to get URL "
            "<code>/api/v1/chat/completions</code> from this server.</h2>"
            "</body></html>")
API_403_JSON = '{"error":{"code":"PLAN_REQUIRED","message":"no plan"}}'


# --------------------------------------------------------------------------- #
# breaker
# --------------------------------------------------------------------------- #

def test_breaker_ignores_api_json_403():
    br = EdgeBlockBreaker()
    assert br.note_edge_block(API_403_JSON) is False
    assert br.is_blocked() is False


def test_breaker_trips_on_html_edge_block():
    br = EdgeBlockBreaker()
    assert br.note_edge_block(EDGE_403) is True
    assert br.is_blocked() is True
    err = br.check()
    assert err is not None and err["code"] == "waf_block" and err["status"] == 503


def test_breaker_success_resets():
    br = EdgeBlockBreaker()
    br.note_edge_block(EDGE_403)
    br.note_success()
    assert br.is_blocked() is False
    assert br.check() is None


def test_breaker_stale_block_expires():
    br = EdgeBlockBreaker()
    br.note_edge_block(EDGE_403)
    br.blocked_at = time.time() - 3600      # simulate an hour-old block
    assert br.is_blocked() is False


@pytest.mark.asyncio
async def test_breaker_prober_resumes_on_non_edge_answer():
    br = EdgeBlockBreaker()
    br.note_edge_block(EDGE_403)
    br.next_probe_at = time.time()          # probe immediately
    calls = {"n": 0}

    async def probe():
        calls["n"] += 1
        return 401, '{"error":"unauthorized"}'   # API answered: not the CDN

    await br._probe_loop(probe)
    assert br.is_blocked() is False
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# capability layer
# --------------------------------------------------------------------------- #

def test_model_caps_table():
    assert MODEL_CAPS["GLM-5.3"]["vision"] is True
    assert MODEL_CAPS["GLM-5.3-Flash"]["vision"] is True
    assert model_caps("unknown-future-model")["vision"] is True   # permissive default


def test_build_body_forwards_images_to_53_by_owner_decision():
    """2026-10-21 owner live-test: GLM-5.3 DOES read images — vision ON for
    both plan models; nothing is stripped any more."""
    payload = {"model": "GLM-5.3", "max_tokens": 100, "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="}},
        ]}]}
    body = build_body(payload, model_caps=model_caps("GLM-5.3"))
    kinds = [b["type"] for b in body["messages"][0]["content"]]
    assert kinds == ["text", "image"]


def test_model_caps_all_vision_on():
    assert MODEL_CAPS["GLM-5.3"]["vision"] is True
    assert MODEL_CAPS["GLM-5.3-Flash"]["vision"] is True


def test_build_body_keeps_images_for_vision():
    payload = {"model": "GLM-5.3-Flash", "max_tokens": 100, "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="}},
        ]}]}
    body = build_body(payload, model_caps=model_caps("GLM-5.3-Flash"))
    kinds = [b["type"] for b in body["messages"][0]["content"]]
    assert kinds == ["text", "image"]


def test_build_body_clamps_max_tokens():
    payload = {"model": "GLM-5.3", "max_tokens": 999_999_999,
               "messages": [{"role": "user", "content": "hi"}]}
    body = build_body(payload, model_caps=model_caps("GLM-5.3"))
    assert body["max_tokens"] == MODEL_CAPS["GLM-5.3"]["max_output_tokens"]


def test_build_body_no_caps_is_uncapped_forwards():
    payload = {"model": "GLM-5.3", "max_tokens": 999_999_999,
               "messages": [{"role": "user", "content": "hi"}]}
    body = build_body(payload)      # no model_caps: legacy behaviour
    assert body["max_tokens"] == 999_999_999


def test_registry_vision_helpers():
    r = Registry()
    assert r.supports_vision("GLM-5.3-Flash") is True
    assert r.supports_vision("GLM-5.3") is True       # owner live-test 2026-10-21
    assert r.max_output("GLM-5.3") == 128000


# --------------------------------------------------------------------------- #
# default effort
# --------------------------------------------------------------------------- #

def test_default_effort_high_when_unspecified():
    body = build_body({"model": "GLM-5.3", "max_tokens": 10,
                       "messages": [{"role": "user", "content": "hi"}]})
    assert body["output_config"]["effort"] == "high"


def test_explicit_effort_wins():
    body = build_body({"model": "GLM-5.3", "max_tokens": 10, "reasoning_effort": "max",
                       "messages": [{"role": "user", "content": "hi"}]})
    assert body["output_config"]["effort"] == "max"


# --------------------------------------------------------------------------- #
# anthropic-beta merge
# --------------------------------------------------------------------------- #

def test_merge_anthropic_beta():
    assert merge_anthropic_beta("a-2026-04-07", None) == "a-2026-04-07"
    assert merge_anthropic_beta("a-2026-04-07", "b-2027-01-01") == "a-2026-04-07,b-2027-01-01"
    # dedupe + no dupes from client repeating ours
    assert merge_anthropic_beta("a,b", "b,c") == "a,b,c"


# --------------------------------------------------------------------------- #
# pacing gate
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_pacing_gate_enforces_gap():
    cfg = UpstreamConfig(base_url="https://zcode.z.ai/api/v1",
                         chat_path="/x")
    pacing = PacingConfig(enabled=True, min_gap_ms=200, max_gap_ms=200)
    client = UpstreamClient(cfg, pacing=pacing)
    try:
        t0 = time.monotonic()
        await client._pace()
        await client._pace()          # second call must wait the full gap
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.19        # ~200ms gap (tolerance for timer slop)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pacing_gate_off_is_free():
    cfg = UpstreamConfig(base_url="https://zcode.z.ai/api/v1",
                         chat_path="/x")
    client = UpstreamClient(cfg, pacing=PacingConfig(enabled=False))
    try:
        t0 = time.monotonic()
        for _ in range(10):
            await client._pace()
        assert time.monotonic() - t0 < 0.05
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# aggregate reasoning keys (captcha judge depends on this)
# --------------------------------------------------------------------------- #

def _sse(delta_json: str) -> str:
    return f"data: {delta_json}\n\n"


def test_aggregate_emits_both_reasoning_keys():
    text = (_sse('{"id":"1","choices":[{"delta":{"reasoning":"think"}}]}')
            + _sse('{"id":"1","choices":[{"delta":{"content":"answer"}}]}')
            + _sse('{"id":"1","choices":[{"delta":{},"finish_reason":"stop"}]}'))
    msg = aggregate_openai_stream(text, "GLM-5.3", "x")["choices"][0]["message"]
    assert msg["reasoning"] == "think"
    assert msg["reasoning_content"] == "think"
    assert msg["content"] == "answer"


def test_aggregate_reads_reasoning_content_delta():
    text = _sse('{"id":"1","choices":[{"delta":{"reasoning_content":"alt"}}]}')
    msg = aggregate_openai_stream(text, "GLM-5.3", "x")["choices"][0]["message"]
    assert msg["reasoning"] == "alt"


def test_aggregate_only_reasoning_content_not_none():
    text = _sse('{"id":"1","choices":[{"delta":{"reasoning":"only thinking"}}]}')
    msg = aggregate_openai_stream(text, "GLM-5.3", "x")["choices"][0]["message"]
    assert msg["content"] == ""          # '' not None (clients .strip() it)


# --------------------------------------------------------------------------- #
# window-boundary grace (quota-flash regression)
# --------------------------------------------------------------------------- #

def test_model_eligible_ignores_tiny_start_skew():
    """A starts_at 1-2s in the future must NOT unroutable the model: that was
    the 1-2s quota-error flash at every window boundary / clock-skew blip."""
    from zcode_gateway.pool import Account, AccountState, WINDOW_GRACE_S
    a = Account(id="a", email="a@x", access_token="t", expires_at=0)
    a.state = AccountState.READY
    now = time.time()
    a.notes["quotas"] = {"GLM-5.3": {"remaining": 5_000_000,
                                     "starts_at": now + 2,
                                     "resets_at": now + 3600}}
    assert a.model_eligible("GLM-5.3", now=now) is True
    # a REAL future window (night grant) still parks
    a.notes["quotas"]["GLM-5.3"]["starts_at"] = now + WINDOW_GRACE_S + 600
    assert a.model_eligible("GLM-5.3", now=now) is False


def test_cap_model_skips_duplicate_writes():
    """Re-parking the same model/window every poll must not rewrite disk."""
    import json
    from zcode_gateway.pool import PoolManager
    from zcode_gateway.config import PoolConfig
    from zcode_gateway.pool import Account, AccountState
    import asyncio

    pm = PoolManager(PoolConfig(), accounts=[])
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        pm.set_caps_path(pathlib.Path(td) / "caps.json")
        a = Account(id="a", email="a@x", access_token="t", expires_at=0)
        a.state = AccountState.READY
        asyncio.run(pm.upsert(a))
        asyncio.run(pm.cap_model(a, "GLM-5.3", 900, reason="MODEL_CAPPED",
                                 message="x"))
        first = (pathlib.Path(td) / "caps.json").read_text()
        # same park again: no rewrite (mtime/content identical check)
        import time as _t
        _t.sleep(0.02)
        asyncio.run(pm.cap_model(a, "GLM-5.3", 900, reason="MODEL_CAPPED",
                                 message="x"))
        second = (pathlib.Path(td) / "caps.json").read_text()
        assert first == second


# --------------------------------------------------------------------------- #
# 1005 quota churn (plan-less account flash regression)
# --------------------------------------------------------------------------- #

def test_apply_error_1005_escalates_to_exhausted():
    """A plan-less account rejecting with 502/1005 'exceed quota limit' must
    not churn forever: 3 strikes -> EXHAUSTED (the balance loop restores it
    when real quota appears)."""
    import asyncio
    from unittest.mock import MagicMock
    from zcode_gateway.service import ChatService
    from zcode_gateway.pool import Account, AccountState, PoolManager
    from zcode_gateway.config import PoolConfig

    pm = PoolManager(PoolConfig(), accounts=[])
    a = Account(id="churn", email="c@x", access_token="t", expires_at=0)
    a.state = AccountState.READY
    a.notes["plans"] = []          # polled: no active plan
    a.notes["quotas"] = {}         # polled: no buckets
    asyncio.run(pm.upsert(a))

    svc = ChatService.__new__(ChatService)   # no __init__ side effects
    svc.cfg = MagicMock()
    svc.cfg.pool.cooldown_seconds = 30
    svc.cfg.pool.blocked_model_cooldown_seconds = 900
    svc.pool = pm

    for i in range(3):
        asyncio.run(svc._apply_error(a, "GLM-5.3", 502, "1005",
                                     "exceed quota limit"))
    assert a.state is AccountState.EXHAUSTED
    assert a.quota_strikes == 3


def test_apply_error_1005_with_resets_at_parks_model():
    """1005 with a visible bucket schedules the park from resets_at."""
    import asyncio
    from unittest.mock import MagicMock
    from zcode_gateway.service import ChatService
    from zcode_gateway.pool import Account, AccountState, PoolManager
    from zcode_gateway.config import PoolConfig

    pm = PoolManager(PoolConfig(), accounts=[])
    a = Account(id="ok", email="o@x", access_token="t", expires_at=0)
    a.state = AccountState.READY
    a.notes["quotas"] = {"GLM-5.3": {"remaining": 0, "total": 100,
                                     "resets_at": time.time() + 7200}}
    asyncio.run(pm.upsert(a))

    svc = ChatService.__new__(ChatService)
    svc.cfg = MagicMock()
    svc.cfg.pool.cooldown_seconds = 30
    svc.cfg.pool.blocked_model_cooldown_seconds = 900
    svc.pool = pm

    asyncio.run(svc._apply_error(a, "GLM-5.3", 502, "1005",
                                 "exceed quota limit"))
    assert a.state is AccountState.READY          # not exhausted
    assert "GLM-5.3" in a.model_caps              # parked with a release time
