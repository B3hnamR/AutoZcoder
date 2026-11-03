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
import invite_claim as ic  # noqa: E402


@pytest.fixture(autouse=True)
def _offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected real HTTP request")

    monkeypatch.setattr(plan_check.requests.Session, "send", forbidden)
    monkeypatch.setenv("ZAI_CLAIM_START_PROXY_FALLBACK", "0")


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


def test_claim_does_not_retry_a_spent_captcha_token(monkeypatch):
    # verify_param is single-use: a transport error may still have delivered
    # the claim, so a blind retry burns 3007. The caller mints a fresh token
    # on code -1; the claim itself must fire exactly once.
    calls = []

    def fake_post(url, **kw):
        calls.append(kw)
        raise plan_check.requests.ConnectionError("edge reset")

    monkeypatch.setattr(plan_check.requests, "post", fake_post)
    j = plan_check.claim_plan("jwt", "p", verify_param="V" * 40, region="sgp")
    assert j["code"] == -1
    assert len(calls) == 1


def test_sessions_are_per_thread():
    import threading

    main = plan_check._session()
    assert plan_check._session() is main          # reused within a thread

    seen = []
    t = threading.Thread(target=lambda: seen.append(plan_check._session()))
    t.start()
    t.join()

    assert seen and seen[0] is not main           # no cookie jar shared
    assert seen[0].trust_env is False
    assert main.trust_env is False


@pytest.mark.parametrize("data", [[], {"plans": []}])
def test_strict_preview_accepts_valid_empty_data(monkeypatch, data):
    monkeypatch.setattr(plan_check.requests, "get",
                        lambda *a, **k: _Resp({"code": 0, "data": data}))
    assert plan_check.preview_plans("jwt", strict=True) == []


@pytest.mark.parametrize("failure,kind,route_problem", [
    ("http", "http", True),
    ("non_json", "non_json", True),
    ("business", "business", False),
    ("blocked", "business", True),
    ("transport", "transport", True),
    ("list_envelope", "malformed", True),
    ("missing_code", "malformed", True),
    ("missing_data", "malformed", True),
])
def test_preview_failures_are_not_empty_offers(monkeypatch, failure, kind, route_problem):
    secret = "sensitive-account-or-proxy-detail"

    def fake_get(*args, **kwargs):
        if failure == "transport":
            raise plan_check.requests.ConnectionError(secret)
        if failure == "non_json":
            response = _Resp(secret)
            response.json = lambda: (_ for _ in ()).throw(ValueError(secret))
            return response
        payload = {
            "http": {"code": 0, "data": {"plans": []}},
            "business": {"code": 1005, "msg": secret, "data": {"plans": []}},
            "blocked": {"code": 3012, "msg": secret, "data": {}},
            "list_envelope": [],
            "missing_code": {"data": []},
            "missing_data": {"code": 0},
        }[failure]
        return _Resp(payload, status=502 if failure == "http" else 200)

    monkeypatch.setattr(plan_check.requests, "get", fake_get)
    logs = []
    # Legacy callers keep their non-throwing list contract.
    assert plan_check.preview_plans("jwt", log=logs.append) == []
    with pytest.raises(plan_check.PreviewError) as caught:
        plan_check.preview_plans("jwt", log=logs.append, strict=True)
    assert caught.value.kind == kind
    assert caught.value.route_problem is route_problem
    assert secret not in str(caught.value)
    assert secret not in "\n".join(logs)

    monkeypatch.setattr(cs, "device_login", lambda *a, **k: ("jwt", True))
    monkeypatch.setattr(cs, "get_plan", lambda *a, **k: {})
    monkeypatch.setattr(cs, "report_app_activity", lambda *a, **k: True)
    result = cs.claim_start("chat", force_direct=True, plan_id="explicit-plan", log=logs.append)
    assert result["claimed"] is False
    assert result["preview_error"] == kind
    assert result["route_problem"] is route_problem
    assert "no claimable plan" not in result["error"]
    assert secret not in result["error"]


@pytest.mark.parametrize("fallback", [None, True, False])
@pytest.mark.parametrize("failure", ["login", "preview", "claim"])
def test_direct_mode_never_uses_pool_fallback(monkeypatch, fallback, failure):
    monkeypatch.setenv("ZAI_PROXY", "http://signup-proxy:8080")
    monkeypatch.setenv("ZAI_CLAIM_START_PROXY_FALLBACK", "1")
    monkeypatch.setattr(cs.time, "sleep", lambda *a: None)

    def forbidden(*a, **k):
        pytest.fail("direct mode selected a pool proxy")

    monkeypatch.setattr(cs, "_verified_pool_proxy", forbidden)
    monkeypatch.setattr(cs, "device_login",
                        lambda *a, **k: (None if failure == "login" else "jwt", False))
    monkeypatch.setattr(cs, "get_plan", lambda *a, **k: {})
    monkeypatch.setattr(cs, "report_app_activity", lambda *a, **k: True)

    def preview(*a, **k):
        if failure == "preview":
            raise plan_check.PreviewError("preview transport failed", kind="transport")
        return [{"plan_id": "zcode-v3-start-plan-referral"}]

    monkeypatch.setattr(cs, "preview_plans", preview)
    monkeypatch.setattr(cs, "_captcha_token", lambda *a, **k: ("", ""))
    monkeypatch.setattr(cs, "claim_plan", lambda *a, **k: {"code": -1, "msg": "request failed"})
    result = cs.claim_start("chat", force_direct=True, proxy_fallback=fallback, log=lambda m: None)
    assert result["claimed"] is False and result["route_problem"] is True
    assert plan_check._proxies()["https"] == "http://signup-proxy:8080"


@pytest.mark.parametrize("force_direct", [True, False])
def test_full_chain_uses_trusted_session_and_requested_route(monkeypatch, force_direct):
    monkeypatch.setenv("ZAI_PROXY", "http://signup-proxy:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://ambient-proxy:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-proxy:8080")
    monkeypatch.setenv("ALL_PROXY", "http://ambient-proxy:8080")
    monkeypatch.setattr(plan_check._tls, "session", None, raising=False)
    seen = []
    balances = []
    pid = "zcode-v3-start-plan-referral"

    def send(session, request, **kwargs):
        seen.append((request.url, kwargs["proxies"]))
        assert session.trust_env is False
        url = request.url
        if "/cli/init" in url:
            return _Resp({"code": 0, "data": {"flow_id": "flow", "authorize_url": "?state=s"}})
        if "/oauth/authorize" in url:
            return _Resp({"redirect_url": "https://callback/?code=code-abcd"})
        if "/cli/poll/" in url:
            return _Resp({"data": {"status": "ready", "token": "jwt",
                                   "zai": {"access_token": "zai"}}})
        if "/billing/balance" in url:
            balances.append(url)
            return _Resp(EMPTY if len(balances) == 1 else _balance([pid]))
        if "/billing/preview" in url:
            return _Resp({"code": 0, "data": {"plans": [{"plan_id": pid}]}})
        if "/client/configs" in url:
            return _Resp({"data": {"configs": {}}})
        return _Resp({"code": 0})

    monkeypatch.setattr(plan_check.requests.Session, "send", send)

    def captcha(log, **kwargs):
        cs._captcha_config(log)
        return "", ""

    monkeypatch.setattr(cs, "_captcha_token", captcha)
    result = cs.claim_start("chat", force_direct=force_direct, proxy_fallback=False, log=lambda m: None)
    assert result["claimed"] is True
    assert len(seen) == 14  # OAuth, login telemetry, zai login, balances, activity, preview, config, claim
    expected = {} if force_direct else {"http": "http://signup-proxy:8080", "https": "http://signup-proxy:8080"}
    assert all(proxies == expected for _, proxies in seen)
    assert ic._FORCE_DIRECT is False
    assert plan_check._proxies()["https"] == "http://signup-proxy:8080"


def test_direct_context_restores_after_exception(monkeypatch):
    monkeypatch.setenv("ZAI_PROXY", "http://signup-proxy:8080")

    def fail(*a, **k):
        assert plan_check._proxies() is None
        raise RuntimeError("test failure")

    monkeypatch.setattr(cs, "_run_chain", fail)
    with pytest.raises(RuntimeError, match="test failure"):
        cs.claim_start("chat", force_direct=True)
    assert plan_check._proxies()["https"] == "http://signup-proxy:8080"


def test_proxy_mode_can_still_retry_a_failed_preview(monkeypatch):
    import os

    original = "http://signup-proxy:8080"
    fallback = "http://fallback-proxy:8080"
    monkeypatch.setenv("ZAI_PROXY", original)
    monkeypatch.setattr(cs, "_verified_pool_proxy", lambda log: fallback)
    routes = []

    def chain(*a, **kw):
        routes.append(os.environ["ZAI_PROXY"])
        if len(routes) == 1:
            return {"claimed": False, "zjwt": True, "route_problem": True}, "jwt"
        assert kw["zjwt"] == "jwt"
        return {"claimed": True}, "jwt"

    monkeypatch.setattr(cs, "_run_chain", chain)
    result = cs.claim_start("chat", proxy_fallback=True, log=lambda m: None)
    assert routes == [original, fallback]
    assert result["claimed"] is True and result["via_proxy"] == fallback
    assert os.environ["ZAI_PROXY"] == original


def test_direct_request_scope_is_nested_and_thread_local(monkeypatch):
    import threading

    monkeypatch.setenv("ZAI_PROXY", "http://signup-proxy:8080")
    seen = []
    with plan_check.direct_requests(True):
        with plan_check.direct_requests(False):
            assert plan_check._proxies() is None
        thread = threading.Thread(target=lambda: seen.append(plan_check._proxies()))
        thread.start()
        thread.join()
        assert plan_check._proxies() is None
    assert seen[0]["https"] == "http://signup-proxy:8080"
    assert plan_check._proxies()["https"] == "http://signup-proxy:8080"


def test_standalone_direct_oauth_keeps_get_post_patchable(monkeypatch):
    monkeypatch.setenv("ZAI_PROXY", "http://signup-proxy:8080")
    seen = []

    def post(url, **kwargs):
        seen.append(kwargs["proxies"])
        if "/cli/init" in url:
            return _Resp({"data": {"flow_id": "flow", "authorize_url": "?state=s"}})
        if "/oauth/authorize" in url:
            return _Resp({"redirect_url": "?code=code-abcd"})
        return _Resp({"code": 200})

    def get(url, **kwargs):
        seen.append(kwargs["proxies"])
        return _Resp({"data": {"status": "ready", "token": "jwt",
                               "zai": {"access_token": "zai"}}})

    monkeypatch.setattr(plan_check.requests, "post", post)
    monkeypatch.setattr(plan_check.requests, "get", get)
    assert ic.device_login("chat", force_direct=True, log=lambda m: None) == ("jwt", True)
    assert seen == [None] * 7
    assert ic._FORCE_DIRECT is False
    assert plan_check._proxies()["https"] == "http://signup-proxy:8080"


def test_direct_captcha_sidecar_does_not_inherit_proxy_settings(monkeypatch):
    import os

    for key in ("ZAI_PROXY", "ZAI_CAPTCHA_PROXY", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "https_proxy", "no_proxy"):
        monkeypatch.setenv(key, "http://proxy:8080")
    monkeypatch.delenv("ZAI_CAPTCHA_PARAM", raising=False)
    monkeypatch.setenv("ZAI_CAPTCHA_CMD", "fake-mint")
    monkeypatch.setattr(cs, "_captcha_config", lambda log: {})
    seen = {}

    def run(argv, env):
        assert argv == ["fake-mint"]
        seen.update(env)
        return types.SimpleNamespace(returncode=0, stdout="V" * 40 + " sgp\n", stderr="")

    monkeypatch.setattr(cs, "_run_sidecar", run)
    assert cs._captcha_token(lambda m: None, force_direct=True) == ("V" * 40, "sgp")
    assert not any(k.upper() in ("ZAI_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") for k in seen)
    assert seen["NO_PROXY"] == "*"
    assert seen["ZAI_CAPTCHA_PROXY"] == ""
    assert "no_proxy" not in seen
    assert os.environ["ZAI_PROXY"] == "http://proxy:8080"
