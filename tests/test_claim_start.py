"""Regressions for the Start Plan preview -> claim round (claim_start.py).

The signup trial was removed; the plan is offered in the billing preview and
granted by billing/claim. These tests pin:
  * the version pin (app_version 3.16.3 is what makes the preview non-empty)
  * preview parsing across the three envelope shapes seen upstream
  * plan classification (`zcode-v3-start-plan-*`, campaign ids excluded)
  * the claim chain: activity -> preview -> claim -> balance
  * 1003 ("already claimed") only counts when the balance/entitlement proves it
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

GEN = Path(__file__).resolve().parent.parent / "generator"
if str(GEN) not in sys.path:
    sys.path.insert(0, str(GEN))

import plan_check  # noqa: E402
import claim_start as cs  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload


def _install_fake(monkeypatch, *, preview_payload, balance_payloads,
                  claim_codes=None):
    """Route plan_check's HTTP calls to canned envelopes, in order."""
    claim_codes = list(claim_codes or [])
    balances = list(balance_payloads)
    calls = {"preview": 0, "claim": [], "events": 0, "balance": 0}

    def fake_get(url, **kw):
        if "/billing/preview" in url:
            calls["preview"] += 1
            return _Resp(preview_payload)
        if "/billing/current" in url:
            return _Resp({"code": 0, "data": {"plans": []}})
        calls["balance"] += 1
        payload = balances[min(calls["balance"] - 1, len(balances) - 1)]
        return _Resp(payload)

    def fake_post(url, **kw):
        if "/event/report" in url:
            calls["events"] += 1
            return _Resp({"code": 0})
        if "/billing/claim" in url:
            calls["claim"].append(kw.get("json", {}).get("plan_id"))
            code = claim_codes.pop(0) if claim_codes else 0
            return _Resp({"code": code, "msg": "" if code == 0 else "x"})
        return _Resp({"code": 0})

    monkeypatch.setattr(plan_check.requests, "get", fake_get)
    monkeypatch.setattr(plan_check.requests, "post", fake_post)
    return calls


EMPTY = {"code": 0, "data": {"plans": [], "balances": []}}


def _balance(ids):
    return {"code": 0, "data": {
        "plans": [{"plan_id": p, "status": "active"} for p in ids],
        "balances": [],
    }}


# --- version pin -----------------------------------------------------------

def test_app_version_pins_the_preview_gate():
    # < 3.16.1 answers an empty list upstream, so the pin is load-bearing
    assert plan_check.APP_VERSION >= "3.16.1"
    assert f"app_version={plan_check.APP_VERSION}" in plan_check.PREVIEW
    assert f"app_version={plan_check.APP_VERSION}" in plan_check.BALANCE


def test_headers_carry_the_pinned_version(monkeypatch):
    monkeypatch.setattr(plan_check, "APP_VERSION", "3.16.3")
    h = plan_check.build_headers("tok")
    assert h["x-zcode-app-version"] == "3.16.3"
    assert h["user-agent"].startswith("ZCode/3.16.3")


# --- classification --------------------------------------------------------

def test_start_plan_prefix_classification():
    assert plan_check.is_start_plan("zcode-v3-start-plan-referral")
    assert plan_check.is_start_plan("zcode-v3-start-plan-1201")
    assert plan_check.is_start_plan("zcode-v3-start-plan-0817")
    # campaign giveaways are separate expiring buckets
    assert not plan_check.is_start_plan("zcode-v3-start-plan-1024")
    assert plan_check.is_start_plan("zcode-v3-start-plan-1024", include_campaign=True)
    assert not plan_check.is_start_plan("zcode-coding-plan-pro")


# --- preview parsing -------------------------------------------------------

def test_preview_parses_plans_shape(monkeypatch):
    _install_fake(monkeypatch, preview_payload={"code": 0, "data": {
        "plans": [{"plan_id": "zcode-v3-start-plan-referral", "name": "Global Build"}]}},
        balance_payloads=[EMPTY])
    got = plan_check.preview_plans("jwt", log=lambda *a, **k: None)
    assert [p["plan_id"] for p in got] == ["zcode-v3-start-plan-referral"]


def test_preview_parses_campaign_delivery_shape(monkeypatch):
    _install_fake(monkeypatch, preview_payload={"code": 0, "data": {"deliveries": [
        {"campaign_id": "c1", "banner": {"buttons": [
            {"action": {"args": {"plan_id": "zcode-v3-start-plan-1024"}}}]}}]}},
        balance_payloads=[EMPTY])
    got = plan_check.preview_plans("jwt", log=lambda *a, **k: None)
    assert [p["plan_id"] for p in got] == ["zcode-v3-start-plan-1024"]


def test_preview_dedupes_and_survives_flat_items(monkeypatch):
    _install_fake(monkeypatch, preview_payload={"code": 0, "data": {"items": [
        {"plan_id": "zcode-v3-start-plan-referral"},
        {"plan_id": "zcode-v3-start-plan-referral"}]}},
        balance_payloads=[EMPTY])
    got = plan_check.preview_plans("jwt", log=lambda *a, **k: None)
    assert [p["plan_id"] for p in got] == ["zcode-v3-start-plan-referral"]


# --- the chain -------------------------------------------------------------

def _fake_login(monkeypatch, zjwt="ZJWT"):
    # activity reporting is left real so the fake POST route can assert it
    monkeypatch.setattr(cs, "device_login", lambda *a, **k: (zjwt, True))


def test_claim_chain_claims_and_verifies(monkeypatch):
    _fake_login(monkeypatch)
    calls = _install_fake(
        monkeypatch,
        preview_payload={"code": 0, "data": {"plans": [
            {"plan_id": "zcode-v3-start-plan-referral"}]}},
        balance_payloads=[EMPTY, _balance(["zcode-v3-start-plan-referral"])],
        claim_codes=[0])
    res = cs.claim_start("chat-token", log=lambda *a, **k: None,
                         proxy_fallback=False)
    assert res["claimed"] is True
    assert res["plan_id"] == "zcode-v3-start-plan-referral"
    assert calls["events"] >= 1          # preview needs the activity ping
    assert calls["claim"] == ["zcode-v3-start-plan-referral"]


def test_claim_chain_skips_when_already_active(monkeypatch):
    _fake_login(monkeypatch)
    calls = _install_fake(
        monkeypatch,
        preview_payload={"code": 0, "data": {"plans": []}},
        balance_payloads=[_balance(["zcode-v3-start-plan-0817"])])
    res = cs.claim_start("chat-token", log=lambda *a, **k: None,
                         proxy_fallback=False)
    assert res["claimed"] is True and res["already_claimed"] is True
    assert calls["claim"] == [] and calls["preview"] == 0


def test_1003_without_balance_evidence_is_not_success(monkeypatch):
    """'already claimed' alone must not mark the account poolable."""
    _fake_login(monkeypatch)
    _install_fake(
        monkeypatch,
        preview_payload={"code": 0, "data": {"plans": [
            {"plan_id": "zcode-v3-start-plan-referral"}]}},
        balance_payloads=[EMPTY, EMPTY],
        claim_codes=[1003])
    res = cs.claim_start("chat-token", log=lambda *a, **k: None,
                         proxy_fallback=False)
    assert res["claimed"] is False
    assert "already" in (res.get("error") or "").lower()


def test_entitlement_with_future_effective_at_counts_as_entitled(monkeypatch):
    """A granted-but-not-yet-live plan lives in entitlements[], not balances[]."""
    _fake_login(monkeypatch)
    pending = {"code": 0, "data": {
        "plans": [{"plan_id": "zcode-v3-start-plan-referral", "status": "pending",
                   "entitlements": [{"plan_id": "zcode-v3-start-plan-referral",
                                     "effective_at": "2026-10-27T01:00:00+08:00"}]}],
        "balances": []}}
    _install_fake(monkeypatch,
                  preview_payload={"code": 0, "data": {"plans": [
                      {"plan_id": "zcode-v3-start-plan-referral"}]}},
                  balance_payloads=[EMPTY, pending],
                  claim_codes=[1003])
    res = cs.claim_start("chat-token", log=lambda *a, **k: None,
                         proxy_fallback=False)
    assert res["claimed"] is True
    assert res["entitlements"][0]["effective_at"].startswith("2026-10-27")


def test_3007_marks_needs_captcha_without_retrying(monkeypatch):
    _fake_login(monkeypatch)
    calls = _install_fake(
        monkeypatch,
        preview_payload={"code": 0, "data": {"plans": [
            {"plan_id": "zcode-v3-start-plan-referral"}]}},
        balance_payloads=[EMPTY, EMPTY],
        claim_codes=[3007, 3007, 3007])
    res = cs.claim_start("chat-token", log=lambda *a, **k: None,
                         proxy_fallback=False)
    assert res["claimed"] is False
    assert res["needs_captcha"] is True
    assert len(calls["claim"]) == 1       # a captcha refusal is not retried


def test_dry_run_claims_nothing(monkeypatch):
    _fake_login(monkeypatch)
    calls = _install_fake(
        monkeypatch,
        preview_payload={"code": 0, "data": {"plans": [
            {"plan_id": "zcode-v3-start-plan-referral"}]}},
        balance_payloads=[EMPTY])
    res = cs.claim_start("chat-token", log=lambda *a, **k: None,
                         proxy_fallback=False, dry_run=True)
    assert res["dry_run"] is True
    assert calls["claim"] == []
    assert res["candidates"] == ["zcode-v3-start-plan-referral"]


def test_empty_preview_is_a_definitive_failure(monkeypatch):
    _fake_login(monkeypatch)
    _install_fake(monkeypatch, preview_payload={"code": 0, "data": {"plans": []}},
                  balance_payloads=[EMPTY])
    res = cs.claim_start("chat-token", log=lambda *a, **k: None,
                         proxy_fallback=False)
    assert res["claimed"] is False
    assert res["route_problem"] is False    # no point retrying another IP


# --- real 3.16.3 contract (from the 2026-10-24 capture) --------------------

def test_preview_carries_app_version_and_platform():
    # the campaign gateway answers 3001 without a platform, even with a device mid
    assert f"app_version={plan_check.APP_VERSION}" in plan_check.PREVIEW
    assert "platform=" in plan_check.PREVIEW


def test_claim_sends_the_captcha_and_platform_headers(monkeypatch):
    seen = {}

    def fake_post(url, **kw):
        seen.update(kw)
        return _Resp({"code": 0, "msg": ""})

    monkeypatch.setattr(plan_check.requests, "post", fake_post)
    plan_check.claim_plan("jwt", "zcode-v3-start-plan-referral",
                          verify_param="V" * 40, region="sgp")
    h = seen["headers"]
    assert h["x-aliyun-captcha-verify-param"] == "V" * 40
    assert h["x-aliyun-captcha-verify-region"] == "sgp"
    assert h["x-platform"] == plan_check.PLATFORM


def test_claim_omits_captcha_headers_without_a_token(monkeypatch):
    seen = {}

    def fake_post(url, **kw):
        seen.update(kw)
        return _Resp({"code": 3007, "msg": "captcha"})

    monkeypatch.setattr(plan_check.requests, "post", fake_post)
    assert plan_check.claim_plan("jwt", "p")["code"] == 3007
    assert "x-aliyun-captcha-verify-param" not in seen["headers"]
