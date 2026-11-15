"""Generator bug-sweep regressions.

Each test pins one behaviour a review found broken: the lazy ddddocr factory
escaping calculate(), one shared device id / timezone across every account,
one flaky account aborting a whole batch, a portless proxy crashing Playwright,
and leading-zero octets bypassing the loopback guard.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

GEN = Path(__file__).resolve().parent.parent / "generator"
if str(GEN) not in sys.path:
    sys.path.insert(0, str(GEN))


def _module(name):
    try:
        return __import__(name)
    except ImportError:
        return None


def _load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _jwt(user_id="u1"):
    payload = base64.urlsafe_b64encode(
        json.dumps({"user_id": user_id}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"


class _Resp:
    status_code = 200

    def __init__(self, data=None):
        self._data = data or {}
        self.text = ""

    def json(self):
        return self._data


# --------------------------------------------------------------------- OCR


def _distance_calculator(monkeypatch):
    """captcha_solver imports playwright at module level; stub the page type.

    The class under test never touches a page, so a placeholder is enough and
    the real module still loads on a machine without the browser stack.
    """
    if "playwright" not in sys.modules:
        fake = types.ModuleType("playwright.sync_api")
        fake.Page = object
        pkg = types.ModuleType("playwright")
        pkg.sync_api = fake
        monkeypatch.setitem(sys.modules, "playwright", pkg)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
    solver = _load_script("bugfix_solver", GEN / "captcha_solver.py")
    return solver


def test_need_ocr_returns_none_when_the_lazy_factory_raises(monkeypatch, caplog):
    solver = _distance_calculator(monkeypatch)

    def factory():
        raise RuntimeError("ddddocr is not installed (pip install ddddocr)")

    calc = solver.DistanceCalculator(ocr=factory,
                                     logger=logging.getLogger("bugfix-ocr"))
    with caplog.at_level(logging.WARNING):
        assert calc._need_ocr() is None
    assert "ddddocr is not installed" in caplog.text


def test_need_ocr_memoizes_the_lazy_factory_result(monkeypatch):
    solver = _distance_calculator(monkeypatch)
    calls = []
    ocr = SimpleNamespace(slide_match=lambda *a, **k: None)

    def factory():
        calls.append(1)
        return ocr

    calc = solver.DistanceCalculator(ocr=factory,
                                     logger=logging.getLogger("bugfix-ocr"))
    assert calc._need_ocr() is ocr
    assert calc._need_ocr() is ocr
    assert calls == [1]


# ------------------------------------------------- per-account device / timezone


def _invite_claim():
    return _module("invite_claim")


def test_login_telemetry_sends_the_per_account_device_id(monkeypatch):
    ic = _invite_claim()
    if ic is None:
        pytest.skip("invite_claim unavailable")
    sent = []

    def fake_request(method, url, **kw):
        sent.append(kw)
        return _Resp({"code": 0})

    monkeypatch.setattr(ic, "_oauth_request", fake_request)
    monkeypatch.setattr(ic, "client_timezone", lambda: "UTC")
    token = _jwt()
    assert ic.report_login_device(token, log=lambda m: None) is True

    mid = ic.per_account_device_mid(token)
    assert mid != ic.APP_HEADERS["x-device-mid"]      # not the shared capture
    assert len(sent) == 2                            # app_login_ck + success
    for kw in sent:
        assert kw["headers"]["x-device-mid"] == mid
        assert kw["json"]["device_mid"] == mid


def test_device_login_sends_the_per_account_device_id(monkeypatch):
    ic = _invite_claim()
    if ic is None:
        pytest.skip("invite_claim unavailable")
    sent = []

    def fake_request(method, url, **kw):
        sent.append((url, kw))
        if "cli/init" in url:
            return _Resp({"data": {"flow_id": "f1", "poll_token": "p1",
                                   "authorize_url": "https://x/?state=S1"}})
        if "oauth/authorize" in url:
            return _Resp({"redirect_url": "https://x/?code=code-abc123"})
        if "cli/poll" in url:
            return _Resp({"data": {"status": "ready", "token": "ZJWT",
                                   "user": {"user_id": "u1"}, "zai": {}}})
        return _Resp()

    monkeypatch.setattr(ic, "_oauth_request", fake_request)
    monkeypatch.setattr(ic, "report_login_device", lambda *a, **k: True)
    monkeypatch.setattr(ic, "client_timezone", lambda: "UTC")
    assert ic.device_login("chat-token", log=lambda m: None) == ("ZJWT", False)

    mid = ic.per_account_device_mid("chat-token")
    assert mid != ic.APP_HEADERS["x-device-mid"]
    flows = {marker: kw for url, kw in sent
             for marker in ("cli/init", "cli/poll", "callback") if marker in url}
    assert set(flows) == {"cli/init", "cli/poll", "callback"}
    for kw in flows.values():
        assert kw["headers"]["x-device-mid"] == mid


def test_login_telemetry_header_and_body_share_the_client_timezone(monkeypatch):
    ic = _invite_claim()
    if ic is None:
        pytest.skip("invite_claim unavailable")
    sent = []

    def fake_request(method, url, **kw):
        sent.append(kw)
        return _Resp({"code": 0})

    monkeypatch.setattr(ic, "_oauth_request", fake_request)
    monkeypatch.setattr(ic, "client_timezone", lambda: "Pacific/Auckland")
    assert ic.report_login_device(_jwt(), log=lambda m: None) is True

    assert len(sent) == 2
    for kw in sent:
        assert kw["headers"]["x-client-timezone"] == "Pacific/Auckland"
        assert kw["json"]["client_timezone"] == "Pacific/Auckland"


def test_plan_events_use_the_client_timezone(monkeypatch):
    pc = _module("plan_check")
    monkeypatch.delenv("ZAI_ACTIVITY_EVENTS", raising=False)
    monkeypatch.setattr(pc, "client_timezone", lambda: "America/Chicago")

    events = pc._plan_events(_jwt())

    assert events
    assert {event["client_timezone"] for event in events} == {"America/Chicago"}


def test_claim500_headers_use_the_client_timezone(monkeypatch):
    c500 = _module("claim500")
    if c500 is None:
        pytest.skip("claim500 unavailable")
    monkeypatch.setattr(c500, "client_timezone", lambda: "America/New_York")

    assert c500._headers("zjwt")["x-client-timezone"] == "America/New_York"


# ------------------------------------------------------------------ batches


def _export_module(monkeypatch, tmp_path, accounts, get_jwt):
    stub = SimpleNamespace(
        get_zcode_jwt=get_jwt,
        get_plan=lambda zjwt, **kw: {"plans": ["zcode-v3-start-plan-1024"],
                                     "starter": True},
        is_start_plan=lambda plan_id: True,
        DEVICE_MID="58aca3b3-3239-4cb7-9c70-df3e5488fb99",
        per_account_device_mid=lambda token: "mid-" + token[-6:],
    )
    monkeypatch.setitem(sys.modules, "plan_check", stub)
    monkeypatch.setenv("ZAI_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("ZCODE_GATEWAY_ACCOUNTS", str(tmp_path / "accounts"))
    monkeypatch.setattr(sys, "argv", ["export_starter.py"])
    results = tmp_path / "out" / "results"
    results.mkdir(parents=True)
    (results / "accounts.txt").write_text("\n".join(accounts) + "\n",
                                          encoding="utf-8")
    return _load_script("bugfix_export", GEN / "export_starter.py")


def test_export_snapshot_uses_the_per_account_device_id(tmp_path, monkeypatch):
    token = _jwt("u9")
    module = _export_module(monkeypatch, tmp_path,
                            ["one@example.test:pw:" + token],
                            lambda chat_token, log=None: token)

    module.main()

    snap = (tmp_path / "accounts" / "one@example.test.txt").read_text(
        encoding="utf-8")
    fields = dict(line.split(":", 1) for line in snap.splitlines()
                  if line and not line.startswith("#") and ":" in line)
    assert fields["device_mid"].strip() == "mid-" + token[-6:]
    assert fields["device_mid"].strip() != "58aca3b3-3239-4cb7-9c70-df3e5488fb99"


def test_export_continues_after_one_account_fails(tmp_path, monkeypatch,
                                                  capsys):
    accounts = ["first@example.test:pw:eyJfirst",
                "second@example.test:pw:eyJsecond"]

    def get_jwt(chat_token, log=None):
        if chat_token == "eyJfirst":
            raise RuntimeError("oauth poll never became ready")
        return _jwt("u2")

    module = _export_module(monkeypatch, tmp_path, accounts, get_jwt)

    module.main()                       # must not raise out of the batch

    written = sorted(p.name for p in (tmp_path / "accounts").iterdir())
    assert written == ["second@example.test.txt"]
    out = capsys.readouterr().out
    assert "RuntimeError" in out and "failed" in out


def test_recheck_continues_after_one_account_fails(tmp_path, monkeypatch):
    rp = _module("recheck_plans")
    if rp is None:
        pytest.skip("recheck_plans unavailable")
    results = tmp_path / "results"
    results.mkdir()
    (results / "accounts.txt").write_text(
        "bad@example.test:pw:eyJbad\nok@example.test:pw:eyJok\n",
        encoding="utf-8")
    monkeypatch.setenv("ZAI_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["recheck_plans.py"])
    calls = []

    def get_jwt(token, log=None):
        calls.append(token)
        if token == "eyJbad":
            raise RuntimeError("poll never became ready")
        return "ZJWT"

    monkeypatch.setattr(rp, "get_zcode_jwt", get_jwt)
    monkeypatch.setattr(rp, "get_plan",
                        lambda zjwt, log=None: {"plans": ["zcode-v3-start-plan-1024"]})
    monkeypatch.setattr(rp, "is_start_plan", lambda plan_id: True)

    assert rp.main() == 0
    assert calls == ["eyJbad", "eyJok"]

    rows = [json.loads(line) for line in
            (results / "plans_recheck.jsonl").read_text(
                encoding="utf-8").splitlines()]
    assert [row["email"] for row in rows] == ["bad@example.test",
                                              "ok@example.test"]
    assert "RuntimeError" in rows[0]["error"]
    assert rows[1]["starter"] is True


# ------------------------------------------------------------------- proxies


def test_portless_proxy_is_not_handed_to_playwright():
    ic = _invite_claim()
    if ic is None:
        pytest.skip("invite_claim unavailable")

    assert ic._pw_proxy("socks5://1.2.3.4") is None      # was "...:None"
    assert ic._pw_proxy("http://1.2.3.4") is None
    assert ic._pw_proxy("0") is None
    assert ic._pw_proxy("http://user:pw@1.2.3.4:8080") == {
        "server": "http://1.2.3.4:8080", "username": "user", "password": "pw"}


def test_endpoint_rejects_leading_zero_octets(monkeypatch):
    pp = _module("proxy_parse")
    monkeypatch.delenv("ZAI_ALLOW_LOOPBACK", raising=False)
    monkeypatch.delenv("PROXYHIVE_ALLOW_LOOPBACK", raising=False)
    monkeypatch.delenv("ZAI_ALLOW_PRIVATE", raising=False)

    # inet_aton reads a leading zero as octal: 0177.0.0.1 IS 127.0.0.1
    assert pp.valid_endpoint("0177.0.0.1", 8080) is False
    assert pp.valid_endpoint("0127.0.0.1", 8080) is False
    assert pp.parse_proxy_list("0177.0.0.1:8080") == []
    assert pp.valid_endpoint("127.0.0.1", 8080) is False
    assert pp.valid_endpoint("93.184.216.34", 8080) is True
    assert pp.valid_endpoint("8.8.8.8", 1080) is True
