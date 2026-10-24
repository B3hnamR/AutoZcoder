"""Claim jobs register instantly (route resolved in the background) and a
blocked 500M claim is retried through verified pool proxies."""
import os
import sys
import threading
import time
import types
from pathlib import Path

from zcode_gateway.generator import ClaimJob, GeneratorManager

GEN = Path(__file__).resolve().parent.parent / "generator"
PLAN = "zcode-v3-start-plan-1024"
ACTIVE = {"plans": [{"plan_id": PLAN, "status": "active"}], "balances": []}
EMPTY = {"plans": [], "balances": []}


def _mgr(tmp_path):
    return GeneratorManager(tmp_path, data_dir=tmp_path)


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_claim_is_registered_immediately_even_when_the_proxy_pick_is_slow(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    gate = threading.Event()
    started = []

    def slow_pick(*a, **k):
        gate.wait(10)                     # a live-probing pool pick that takes "minutes"
        return "socks5://9.9.9.9:1080"

    monkeypatch.setattr(mgr, "_proxy_for_claim", slow_pick)
    monkeypatch.setattr(ClaimJob, "start", lambda self: started.append((self.mode, self.proxy, self.argv)))

    t0 = time.time()
    res = mgr.start_claim500("A@x.me", "proxy")
    assert time.time() - t0 < 1.0                          # the HTTP call returns at once
    assert res["started"] in mgr.claims and res["mode"] == "proxy"
    job = mgr.claims[res["started"]]
    assert job.state == "starting" and job.alive()         # visible, and counted as alive
    assert any("verified proxy" in ln for ln in job.lines)

    # a second click while it is still resolving must NOT queue a duplicate
    assert "already running" in mgr.start_claim500("a@x.me", "proxy")["error"]

    gate.set()
    assert _wait(lambda: started)
    mode, proxy, argv = started[0]
    assert (mode, proxy) == ("proxy", "socks5://9.9.9.9:1080")
    assert argv[-2:] == ["--proxy", "socks5://9.9.9.9:1080"]
    assert "claim500.py" in argv[2]


def test_no_verified_proxy_falls_back_to_direct_with_a_visible_reason(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    started = []
    monkeypatch.setattr(mgr, "_proxy_for_claim", lambda *a, **k: "")
    seq = [True]                                                     # a scan is running, then it ends
    monkeypatch.setattr(mgr.checker, "alive", lambda: seq.pop(0) if seq else False)
    monkeypatch.setattr(ClaimJob, "start", lambda self: started.append((self.mode, self.argv)))
    res = mgr.start_claim500("b@x.me", "proxy")
    assert _wait(lambda: started)
    job = mgr.claims[res["started"]]
    assert started[0][0] == "direct" and "--direct" in started[0][1]
    assert any("falling back to the DIRECT IP" in ln for ln in job.lines)


def test_no_proxy_and_no_fallback_fails_the_job_visibly(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    monkeypatch.setattr(mgr, "_proxy_for_claim", lambda *a, **k: "")
    seq = [True]
    monkeypatch.setattr(mgr.checker, "alive", lambda: seq.pop(0) if seq else False)
    res = mgr.start_claim500("c@x.me", "proxy", allow_direct_fallback=False)
    job = mgr.claims[res["started"]]
    assert _wait(lambda: job.state == "failed")
    assert any("no usable proxy" in ln for ln in job.lines) and not job.alive()


def test_500m_claim_never_waits_for_playwright_but_100m_does(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    calls = []
    started = []
    monkeypatch.setattr(mgr, "_ensure_playwright", lambda *a, **k: calls.append("pw") or False)
    monkeypatch.setattr(ClaimJob, "start", lambda self: started.append(self.kind))

    mgr.start_claim500("d@x.me", "direct")
    assert _wait(lambda: started == ["500m"])
    assert calls == []                                       # pure HTTP: no browser setup

    r1 = mgr.start_claim("e@x.me", "direct")
    job = mgr.claims[r1["started"]]
    assert _wait(lambda: job.state == "failed")              # playwright "failed" -> visible failure
    assert calls == ["pw"] and any("playwright setup failed" in ln for ln in job.lines)


def test_stopping_a_queued_claim_prevents_the_launch(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    gate = threading.Event()
    started = []

    def pick(*a, **k):
        gate.wait(10)
        return "socks5://1.1.1.1:1080"

    monkeypatch.setattr(mgr, "_proxy_for_claim", pick)
    monkeypatch.setattr(ClaimJob, "start", lambda self: started.append(1))
    res = mgr.start_claim500("f@x.me", "proxy")
    assert mgr.stop_claim(res["started"]) is True
    gate.set()
    time.sleep(0.3)
    assert started == [] and mgr.claims[res["started"]].state == "killed"


# --------------------------------------------------------------------------- #
# claim500: retry through verified pool proxies
# --------------------------------------------------------------------------- #

def _c500():
    if not GEN.is_dir():
        return None
    if str(GEN) not in sys.path:
        sys.path.insert(0, str(GEN))
    try:
        import claim500
        return claim500
    except ImportError:
        return None


def _stub_chain(c, monkeypatch, claim_fn, seen_proxies):
    monkeypatch.setattr(c, "device_login", lambda *a, **k: ("ZJWT", True))
    monkeypatch.setattr(c, "discover_campaign", lambda *a, **k: ("", ""))
    monkeypatch.setattr(c, "marketing_touch", lambda *a, **k: False)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)

    def billing_claim(*a, **k):
        seen_proxies.append(os.environ.get("ZAI_PROXY"))
        return claim_fn(len(seen_proxies))

    monkeypatch.setattr(c, "billing_claim", billing_claim)


def test_blocked_claim_is_retried_through_a_verified_pool_proxy(monkeypatch):
    c = _c500()
    if c is None:
        return
    monkeypatch.setenv("ZAI_CLAIM500_PROXY_FALLBACK", "1")
    monkeypatch.delenv("ZAI_PROXY", raising=False)
    seen = []
    state = {"claimed": False}

    def claim(n):
        # the direct route is blocked (3 attempts); the proxy route works
        if n <= 3:
            return {"code": 3012, "msg": "request has been blocked due to unusual activity."}
        state["claimed"] = True
        return {"code": 0, "msg": "ok"}

    _stub_chain(c, monkeypatch, claim, seen)
    monkeypatch.setattr(c, "billing_balance",
                        lambda *a, **k: ACTIVE if state["claimed"] else EMPTY)
    monkeypatch.setattr(c, "_verified_pool_proxy", lambda log: "socks5://user:pw@8.8.8.8:1080")
    logs = []
    res = c.claim500("tok", PLAN, log=logs.append)
    assert res["claimed"] is True and res["already_claimed"] is False
    assert seen[:3] == [None, None, None]                       # first route: direct
    assert seen[3] == "socks5://user:pw@8.8.8.8:1080"           # then through the proxy
    assert res["via_proxy"] == "socks5://8.8.8.8:1080"          # credentials never reported
    assert any("verified pool proxy 1/2" in m for m in logs)
    assert not any("pw" in m for m in logs)
    assert "ZAI_PROXY" not in os.environ                         # route restored afterwards


def test_each_fallback_proxy_is_tried_then_the_original_error_is_kept(monkeypatch):
    c = _c500()
    if c is None:
        return
    monkeypatch.setenv("ZAI_CLAIM500_PROXY_FALLBACK", "1")
    monkeypatch.setenv("ZAI_PROXY", "http://orig:1")
    seen = []
    _stub_chain(c, monkeypatch, lambda n: {"code": 3012, "msg": "blocked"}, seen)
    monkeypatch.setattr(c, "billing_balance", lambda *a, **k: EMPTY)
    proxies = iter(["socks5://1.1.1.1:1", "socks5://2.2.2.2:1", "socks5://3.3.3.3:1"])
    monkeypatch.setattr(c, "_verified_pool_proxy", lambda log: next(proxies))
    res = c.claim500("tok", PLAN, log=lambda m: None)
    assert res["claimed"] is False and "3012" in res["error"]
    assert seen == ["http://orig:1"] * 3 + ["socks5://1.1.1.1:1"] * 2 + ["socks5://2.2.2.2:1"] * 2
    assert os.environ["ZAI_PROXY"] == "http://orig:1"            # restored


def test_no_pool_proxy_means_no_retry_and_fallback_can_be_disabled(monkeypatch):
    c = _c500()
    if c is None:
        return
    seen = []
    _stub_chain(c, monkeypatch, lambda n: {"code": 3012, "msg": "blocked"}, seen)
    monkeypatch.setattr(c, "billing_balance", lambda *a, **k: EMPTY)
    monkeypatch.setattr(c, "_verified_pool_proxy", lambda log: "")
    monkeypatch.setenv("ZAI_CLAIM500_PROXY_FALLBACK", "1")
    res = c.claim500("tok", PLAN, log=lambda m: None)
    assert res["claimed"] is False and len(seen) == 3            # nothing to retry with

    picked = []
    monkeypatch.setattr(c, "_verified_pool_proxy", lambda log: picked.append(1) or "socks5://1.1.1.1:1")
    monkeypatch.setenv("ZAI_CLAIM500_PROXY_FALLBACK", "0")
    c.claim500("tok", PLAN, log=lambda m: None)
    assert picked == []                                          # switched off


def test_verified_pool_proxy_asks_the_pool_to_verify_first(monkeypatch, tmp_path):
    c = _c500()
    if c is None:
        return
    asked = []
    fake_pool = types.SimpleNamespace(
        set_output_dir=lambda p: asked.append(("dir", p)),
        pick_verified=lambda tries, timeout, socks: asked.append(("pick", socks)) or (
            None if socks else "http://5.5.5.5:80"))
    monkeypatch.setitem(sys.modules, "proxy_pool", fake_pool)
    monkeypatch.setenv("ZAI_OUTPUT_DIR", str(tmp_path))
    assert c._verified_pool_proxy(lambda m: None) == "http://5.5.5.5:80"
    assert ("dir", str(tmp_path)) in asked and asked[1:] == [("pick", True), ("pick", False)]
