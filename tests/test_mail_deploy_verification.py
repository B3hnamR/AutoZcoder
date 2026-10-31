"""Offline regressions: no Wrangler process or external HTTP request is allowed."""
import asyncio
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
from types import SimpleNamespace

import httpx
import pytest

from zcode_gateway import generator
from zcode_gateway.generator import GeneratorManager


URL = "https://zai-mail.fixture.workers.dev/inbox"
DUMMY = "offline-fixture-value"


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected process or network access")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def manager(tmp_path):
    mgr = GeneratorManager.__new__(GeneratorManager)
    mgr.mail_cfg_path = tmp_path / "mail.json"
    mgr.mail_cfg = {"domains": [], "active": "", "inbox_url": URL,
                    "inbox_secret": DUMMY, "inbox_ok": True}
    return mgr


def test_credential_and_routing_changes_rejected_while_setup_holds_lock(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def setup():
        with mgr._mail_config_lock():
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=setup)
    thread.start()
    try:
        assert entered.wait(3)
        assert "running" in mgr.set_cf_credentials("dummy-token-long-enough")
        assert not mgr.save_cf_credentials(clear=True)["ok"]
        assert not mgr.auto_route_domain("example.test")["ok"]
        assert mgr.mail_cfg["inbox_secret"] == DUMMY
    finally:
        release.set()
        thread.join(5)


def scripted_http(monkeypatch, responses):
    calls, sleeps = [], []
    client_type = httpx.AsyncClient
    outcomes = iter(responses)

    async def handler(request):
        calls.append(request)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        return client_type(transport=httpx.MockTransport(handler), **kwargs)

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return calls, sleeps


@pytest.mark.parametrize("statuses", [(401, 401), (503, 502), (429, 408)])
def test_propagation_failures_are_delayed(monkeypatch, statuses):
    calls, sleeps = scripted_http(monkeypatch,
                                  [*(httpx.Response(s) for s in statuses), httpx.Response(200, json=[])])
    assert GeneratorManager._verify_inbox(URL, DUMMY) == {"ok": True, "status": 200}
    assert sleeps == [3, 3]
    assert len(calls) == 3
    assert all(request.url.params["k"] == DUMMY for request in calls)


def test_404_is_not_retried(monkeypatch):
    calls, sleeps = scripted_http(monkeypatch, [httpx.Response(404)])
    result = GeneratorManager._verify_inbox(URL, DUMMY)
    assert not result["ok"] and result["status"] == 404
    assert "production /inbox" in result["error"]
    assert len(calls) == 1 and not sleeps


@pytest.mark.parametrize("body", ["<html>ok</html>", "[invalid", '{"ok":true}',
                                  '[{"error":"unauthorized"}]', '[{"ts":true}]'])
def test_200_requires_inbox_array(monkeypatch, body):
    calls, sleeps = scripted_http(monkeypatch, [httpx.Response(200, text=body)])
    result = GeneratorManager._verify_inbox(URL, DUMMY)
    assert not result["ok"] and "not an inbox" in result["error"]
    assert len(calls) == 1 and not sleeps


def test_nonempty_inbox_shape(monkeypatch):
    scripted_http(monkeypatch, [httpx.Response(200, json=[
        {"to": "a@example.test", "from": "b@example.test", "subject": "test", "link": None, "ts": 123}])])
    assert GeneratorManager._verify_inbox(URL, DUMMY)["ok"]


@pytest.mark.parametrize("url", [URL + "?k=old-dummy", URL + "/", URL.replace("/inbox", "/health")])
def test_wrong_inbox_url_is_rejected_before_http(url):
    result = GeneratorManager._verify_inbox(url, DUMMY)
    assert not result["ok"] and "exact /inbox" in result["error"]


def test_401_is_actionable_and_bounded(monkeypatch):
    calls, sleeps = scripted_http(monkeypatch, [httpx.Response(401) for _ in range(11)])
    result = GeneratorManager._verify_inbox(URL, DUMMY)
    assert result["status"] == 401 and not result["ok"]
    assert "endpoint/secret mismatch" in result["error"]
    assert "routing does not fix" in result["error"]
    assert DUMMY not in str(result)
    assert len(calls) == 11 and len(sleeps) == 10


def test_total_deadline_cancels_slow_body(monkeypatch):
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"["
            await asyncio.Event().wait()

    scripted_http(monkeypatch, [httpx.Response(200, stream=SlowBody())])
    result = GeneratorManager._verify_inbox(URL, DUMMY, deadline_seconds=0.02)
    assert not result["ok"] and "deadline expired" in result["error"]


def test_network_exception_does_not_return_request_secret(monkeypatch):
    calls, _ = scripted_http(monkeypatch, [httpx.ConnectError("private query " + DUMMY) for _ in range(11)])
    result = GeneratorManager._verify_inbox(URL, DUMMY)
    assert not result["ok"] and "ConnectError" in result["error"]
    assert DUMMY not in str(result) and len(calls) == 11


def test_production_url_rejects_preview_stale_and_ambiguity():
    output = ("https://old.fixture.workers.dev\n"
              "https://version-zai-mail.fixture.workers.dev\n"
              "https://zai-mail.fixture.workers.dev.attacker.test\n"
              "https://zai-mail.fixture.workers.dev\n")
    assert GeneratorManager._production_worker_url(output, "zai-mail") == URL.removesuffix("/inbox")
    assert GeneratorManager._production_worker_url(output.splitlines()[1], "zai-mail") is None
    with pytest.raises(ValueError, match="ambiguous"):
        GeneratorManager._production_worker_url(output + "https://zai-mail.other.workers.dev", "zai-mail")


def test_deploy_blocks_autosave_and_invalidates_before_work(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    monkeypatch.setattr(mgr, "set_cf_credentials", lambda *args: "")
    errors = []

    def deploy(progress):
        assert not mgr.mail_cfg["inbox_ok"]

        def autosave():
            try:
                mgr.set_mail_config(inbox_secret="different-dummy")
            except ValueError as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=autosave)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert mgr.mail_cfg["inbox_secret"] == DUMMY
        return {"ok": False, "error": "fixture failure"}

    monkeypatch.setattr(mgr, "_deploy_worker_locked", deploy)
    assert not mgr.deploy_worker()["ok"]
    assert errors and "busy" in errors[0]
    mgr.set_mail_config(inbox_secret="next-dummy")
    assert mgr.mail_cfg["inbox_secret"] == "next-dummy"


def test_failed_config_validation_is_not_partially_applied(tmp_path):
    mgr = manager(tmp_path)
    before = dict(mgr.mail_cfg)
    with pytest.raises(ValueError):
        mgr.set_mail_config(domains=["new.example.test"], inbox_url="invalid")
    assert mgr.mail_cfg == before


def test_deployment_errors_redact_credentials(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    mgr.mail_cfg["cf_api_token"] = "offline-token-fixture"
    monkeypatch.setattr(mgr, "set_cf_credentials", lambda *args: "")
    monkeypatch.setattr(mgr, "_deploy_worker_locked", lambda progress: {
        "ok": False, "error": DUMMY + " offline-token-fixture"})
    result = mgr.deploy_worker()
    assert DUMMY not in str(result)
    assert "offline-token-fixture" not in str(result)

    def fail(progress):
        raise RuntimeError(DUMMY)

    monkeypatch.setattr(mgr, "_deploy_worker_locked", fail)
    result = mgr.deploy_worker()
    assert "RuntimeError" in result["error"] and DUMMY not in str(result)


def test_secure_atomic_save_uses_distinct_private_files(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    replace = os.replace
    observed = []

    def check_replace(source, destination):
        source = Path(source)
        observed.append(source)
        if os.name != "nt":
            assert stat.S_IMODE(source.stat().st_mode) == 0o600
        replace(source, destination)

    monkeypatch.setattr(os, "replace", check_replace)
    mgr._save_mail_config()
    mgr._save_mail_config()
    assert observed[0] != observed[1]
    assert json.loads(mgr.mail_cfg_path.read_text())["inbox_secret"] == DUMMY
    assert not list(tmp_path.glob(".mail-*.tmp"))


def test_manual_check_invalidates_before_unexpected_failure(tmp_path, monkeypatch):
    mgr = manager(tmp_path)

    def fail(*args):
        assert not mgr.mail_cfg["inbox_ok"]
        raise RuntimeError("fixture")

    monkeypatch.setattr(mgr, "_verify_inbox", fail)
    with pytest.raises(RuntimeError, match="fixture"):
        mgr.test_inbox()
    assert not mgr.mail_cfg["inbox_ok"]
    mgr.set_mail_config(inbox_url=URL)


@pytest.mark.parametrize("returncode,expected", [(0, True), (1, False)])
def test_secret_upload_exact_input_and_exit_contract(tmp_path, monkeypatch, returncode, expected):
    mgr = manager(tmp_path)
    worker = tmp_path / "worker"
    worker.mkdir()
    (worker / "wrangler.toml").write_text('name = "zai-mail"\n[[kv_namespaces]]\nid = "' + "0" * 32 + '"\n')
    monkeypatch.setattr(mgr, "_ensure_node", lambda progress: {"ok": True, "npx": "offline-npx"})
    monkeypatch.setattr(mgr, "export_worker", lambda: {"ok": True, "path": str(worker)})
    monkeypatch.setattr(mgr, "_full_env", lambda: {})
    monkeypatch.setattr(mgr, "set_cf_credentials", lambda *args: "")

    def run(args, **kwargs):
        command = args[2:]
        if command == ["whoami"]:
            output = "You are logged in"
        elif command == ["kv", "namespace", "list"]:
            output = json.dumps([{"title": "MAIL", "id": "a" * 32}])
        elif command == ["deploy"]:
            output = "https://preview-zai-mail.fixture.workers.dev\n" + URL.removesuffix("/inbox")
        else:
            pytest.fail("unexpected offline command")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    class Upload:
        def __init__(self, args, **kwargs):
            assert args[2:] == ["secret", "put", "INBOX_SECRET"]
            self.returncode = returncode

        def communicate(self, *, input, timeout):
            assert input == DUMMY and timeout == 180
            # Neither successful prose on failure nor absent prose on success
            # changes the process exit-status contract.
            return ("" if returncode == 0 else "Success! " + DUMMY), None

    monkeypatch.setattr(generator, "_run_capture", run)
    monkeypatch.setattr(subprocess, "Popen", Upload)
    checked = []

    def verify(url, secret):
        checked.append((url, secret))
        return {"ok": True, "status": 200}

    monkeypatch.setattr(mgr, "_verify_inbox", verify)
    result = mgr.deploy_worker()
    assert result["ok"] is expected
    assert checked == ([(URL, DUMMY)] if expected else [])
    assert DUMMY not in str(result)


def test_manual_endpoint_uses_threaded_shared_verifier(monkeypatch):
    from zcode_gateway import api_dash

    called = []

    def test_inbox():
        called.append(threading.current_thread().name)
        return {"ok": False, "status": 401, "error": "fixture"}

    monkeypatch.setattr(api_dash, "_generator", lambda request: SimpleNamespace(test_inbox=test_inbox))
    result = asyncio.run(api_dash.generator_mail_test(None, "dummy-admin"))
    assert result["status"] == 401
    assert called and called[0] != threading.current_thread().name
