"""Regressions: wrangler login on IPv6-broken machines, and the claim chain
surviving a flaky api.z.ai / network blip."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import zcode_gateway.generator as gen_mod
from zcode_gateway.generator import GeneratorManager

GEN = Path(__file__).resolve().parent.parent / "generator"
LOGGED_IN = "👋 You are logged in with an OAuth Token, associated with a@b.c"
NOT_LOGGED_IN = "You are not authenticated. Please run `wrangler login`."


def _proc(text: str, code: int = 0):
    return SimpleNamespace(stdout=text, stderr="", returncode=code)


class _Wrangler:
    """Scripted stand-in for `npx wrangler …`: login attempts pop outcomes in
    order; whoami reports whatever the last successful login left behind."""

    def __init__(self, login_outcomes):
        self.login_outcomes = list(login_outcomes)
        self.calls: list[tuple] = []
        self.authed = False

    def __call__(self, args, timeout=300, env_extra=None):
        sub = tuple(args[2:])           # [npx, "wrangler", ...]
        self.calls.append(sub)
        if sub == ("whoami",):
            return _proc(LOGGED_IN if self.authed else NOT_LOGGED_IN)
        assert sub[0] == "login"
        outcome = self.login_outcomes.pop(0)
        if outcome is subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(args, timeout)
        if outcome == "ok":
            self.authed = True
            return _proc("Successfully logged in.")
        return _proc(outcome, code=1)


def _mgr(tmp_path):
    return GeneratorManager(tmp_path, data_dir=tmp_path)


def test_login_first_tries_the_ipv4_callback_host(tmp_path):
    w = _Wrangler(["ok"])
    ok, out = _mgr(tmp_path)._wrangler_login(w, "npx", lambda m: None)
    assert ok and "You are logged in" in out
    logins = [c for c in w.calls if c[0] == "login"]
    assert logins == [("login", "--callback-host", "127.0.0.1")]   # no device flow needed


def test_login_falls_back_to_device_flow(tmp_path):
    w = _Wrangler(["ERROR  Timed out waiting for authorization code, please try again.", "ok"])
    ok, out = _mgr(tmp_path)._wrangler_login(w, "npx", lambda m: None)
    assert ok
    logins = [c for c in w.calls if c[0] == "login"]
    assert logins == [("login", "--callback-host", "127.0.0.1"), ("login", "--device")]


def test_login_failure_reports_wranglers_real_error(tmp_path):
    steps: list[str] = []
    w = _Wrangler(["ERROR  Timed out waiting for authorization code, please try again.",
                   subprocess.TimeoutExpired])
    ok, err = _mgr(tmp_path)._wrangler_login(w, "npx", steps.append)
    assert not ok
    assert "Timed out waiting for authorization code" in err      # not a generic "try again"
    assert "timed out waiting for the browser" in err             # the TimeoutExpired attempt
    assert "--device" in err and "CLOUDFLARE_API_TOKEN" in err    # manual way out
    assert any("login attempt failed" in s for s in steps)


def test_deploy_surfaces_the_login_error(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    monkeypatch.setattr(mgr, "_ensure_node", lambda progress=None: {"ok": True, "npx": "npx"})
    monkeypatch.setattr(mgr, "export_worker", lambda: {"ok": True, "path": str(tmp_path)})

    def fake_run_capture(args, cwd, timeout, env=None):
        sub = tuple(args[2:])
        if sub == ("whoami",):
            return _proc(NOT_LOGGED_IN)
        return _proc("ERROR  Timed out waiting for authorization code", code=1)

    monkeypatch.setattr(gen_mod, "_run_capture", fake_run_capture)
    res = mgr.deploy_worker()
    assert res["ok"] is False
    assert "Timed out waiting for authorization code" in res["error"]


def test_run_capture_returns_output_and_kills_on_timeout(tmp_path):
    r = gen_mod._run_capture([sys.executable, "-c", "print('hi')"], cwd=str(tmp_path), timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "hi"

    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        gen_mod._run_capture([sys.executable, "-c", "import time; time.sleep(60)"],
                             cwd=str(tmp_path), timeout=1)
    assert time.time() - t0 < 20            # killed, not waited out


# --------------------------------------------------------------------------- #
# claim chain resilience
# --------------------------------------------------------------------------- #

def _gen_module(name):
    if not GEN.is_dir():
        return None
    if str(GEN) not in sys.path:
        sys.path.insert(0, str(GEN))
    try:
        return __import__(name)
    except ImportError:
        return None


class _Resp:
    def __init__(self, data=None, status=200):
        self._data, self.status_code, self.text = data or {}, status, ""

    def json(self):
        return self._data


def test_device_login_keeps_the_zjwt_when_zai_login_fails(monkeypatch):
    ic = _gen_module("invite_claim")
    if ic is None:
        return
    import requests

    def fake_post(url, **kw):
        if "cli/init" in url:
            return _Resp({"data": {"flow_id": "f1", "poll_token": "p1",
                                   "authorize_url": "https://x/?state=S1"}})
        return _Resp({"redirect_url": "https://x/?code=code-abc123"})

    def fake_get(url, **kw):
        if "cli/poll" in url:
            return _Resp({"data": {"status": "ready", "token": "ZJWT",
                                   "user": {"user_id": "u1"},
                                   "zai": {"access_token": "zt"}}})
        return _Resp()

    def dns_down(*a, **k):
        raise requests.ConnectionError("Failed to resolve 'api.z.ai'")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(ic, "report_login_device", lambda *a, **k: True)
    monkeypatch.setattr(ic, "zai_login", dns_down)
    logs: list[str] = []
    zjwt, ok = ic.device_login("chat-token", log=logs.append)
    assert zjwt == "ZJWT" and ok is False
    assert any("zai login skipped" in m for m in logs)


def test_claim500_survives_network_blips(monkeypatch):
    c500 = _gen_module("claim500")
    if c500 is None:
        return
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("blip")

    monkeypatch.setattr(requests, "post", boom)
    monkeypatch.setattr(requests, "get", boom)
    env = c500.billing_claim("zjwt", "plan")
    assert env["code"] == -1 and "request failed" in env["msg"]       # retried, not raised
    assert c500.billing_balance("zjwt", log=lambda m: None) == {}


def test_claim500_retries_a_failed_device_login(monkeypatch):
    c500 = _gen_module("claim500")
    if c500 is None:
        return
    plan = "zcode-v3-start-plan-1024"
    attempts = iter([RuntimeError("DNS"), ("ZJWT", True)])

    def device_login(*a, **k):
        v = next(attempts)
        if isinstance(v, Exception):
            raise v
        return v

    active = {"plans": [{"plan_id": plan, "status": "active"}]}
    monkeypatch.setattr(c500, "device_login", device_login)
    monkeypatch.setattr(c500, "billing_balance", lambda *a, **k: active)
    monkeypatch.setattr(c500.time, "sleep", lambda s: None)
    res = c500.claim500("tok", plan, log=lambda m: None)
    assert res["zjwt"] is True and res["claimed"] is True


def _device_flow_fakes(ic, requests, *, init502=0, cb502=0, poll_fail=0, ready=True):
    """Scripted zcode.z.ai: the first N init/callback calls answer 502, the
    first N polls time out; counters let the tests assert what was retried."""
    n = {"init": 0, "cb": 0, "poll": 0}

    def fake_post(url, **kw):
        if "cli/init" in url:
            n["init"] += 1
            if n["init"] <= init502:
                return _Resp(status=502)
            return _Resp({"data": {"flow_id": "f1", "poll_token": "p1",
                                   "authorize_url": "https://x/?state=S1"}})
        return _Resp({"redirect_url": "https://x/?code=code-abc123"})

    def fake_get(url, **kw):
        if "cli/callback" in url:
            n["cb"] += 1
            return _Resp(status=502 if n["cb"] <= cb502 else 200)
        if "cli/poll" in url:
            n["poll"] += 1
            if n["poll"] <= poll_fail:
                raise requests.ReadTimeout("slow")
            if not ready:
                return _Resp({"data": {"status": "pending"}})
            return _Resp({"data": {"status": "ready", "token": "ZJWT",
                                   "user": {"user_id": "u1"},
                                   "zai": {"access_token": "zt"}}})
        return _Resp()

    return n, fake_post, fake_get


def test_device_login_rides_out_502s_and_a_slow_poll(monkeypatch):
    ic = _gen_module("invite_claim")
    if ic is None:
        return
    import requests

    n, fake_post, fake_get = _device_flow_fakes(ic, requests, init502=1, cb502=1, poll_fail=1)
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(ic, "report_login_device", lambda *a, **k: True)
    monkeypatch.setattr(ic, "zai_login", lambda *a, **k: True)
    monkeypatch.setattr(ic.time, "sleep", lambda s: None)
    zjwt, ok = ic.device_login("chat-token", log=lambda m: None)
    assert zjwt == "ZJWT" and ok is True
    assert n == {"init": 2, "cb": 2, "poll": 2}      # each blip retried exactly once


def test_device_login_does_not_spin_when_the_callback_never_lands(monkeypatch):
    ic = _gen_module("invite_claim")
    if ic is None:
        return
    import requests

    n, fake_post, fake_get = _device_flow_fakes(ic, requests, cb502=99, ready=False)
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(ic.time, "sleep", lambda s: None)
    zjwt, ok = ic.device_login("chat-token", log=lambda m: None)
    assert zjwt is None
    assert n["cb"] == 3 and n["poll"] == 5           # was 30 polls on a dead flow


# --------------------------------------------------------------------------- #
# deploy-status: `wrangler whoami` is cached (it ran on every Generator-tab visit)
# --------------------------------------------------------------------------- #

def test_whoami_is_cached_and_a_deploy_clears_it(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    calls = []
    answers = iter([LOGGED_IN, LOGGED_IN, NOT_LOGGED_IN, LOGGED_IN])

    def fake_run_capture(args, cwd, timeout, env=None):
        calls.append(tuple(args[2:]))
        return _proc(next(answers))

    now = [1000.0]
    monkeypatch.setattr(gen_mod, "_run_capture", fake_run_capture)
    monkeypatch.setattr(gen_mod.time, "time", lambda: now[0])

    first = mgr._cached_whoami("npx", tmp_path)
    assert first["wrangler_authed"] is True and len(calls) == 1
    now[0] += 300
    assert mgr._cached_whoami("npx", tmp_path)["wrangler_authed"] is True
    assert len(calls) == 1                                  # served from cache, no npx spawn

    now[0] += 400                                           # >10 min since the check
    assert mgr._cached_whoami("npx", tmp_path)["wrangler_authed"] is True
    assert len(calls) == 2

    mgr._whoami_cache = None                                # what deploy_worker does on exit
    now[0] += 1
    assert mgr._cached_whoami("npx", tmp_path)["wrangler_authed"] is False
    assert len(calls) == 3
    now[0] += 5
    mgr._cached_whoami("npx", tmp_path)
    assert len(calls) == 3                                  # a "no" is trusted briefly too
    now[0] += 30                                            # ...but re-checked after 20s
    assert mgr._cached_whoami("npx", tmp_path)["wrangler_authed"] is True
    assert len(calls) == 4


def test_whoami_timeout_is_reported_as_unknown_not_an_error(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)

    def slow(args, cwd, timeout, env=None):
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(gen_mod, "_run_capture", slow)
    got = mgr._cached_whoami("npx", tmp_path)
    assert got == {"wrangler_authed": None, "whoami_timeout": True}


def test_deploy_clears_the_whoami_cache(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    mgr._whoami_cache = (1.0, {"wrangler_authed": True})
    monkeypatch.setattr(mgr, "_deploy_worker_locked", lambda progress=None: {"ok": True})
    assert mgr.deploy_worker() == {"ok": True}
    assert mgr._whoami_cache is None
