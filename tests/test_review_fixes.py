"""Regression tests for the review round: nginx Host/Origin, relay secrets,
tool_choice none, POSIX process-group kill, proxy filter notice, updater sidecar,
debounced caps persistence."""
import asyncio
import os
import sys

import pytest
from fastapi.testclient import TestClient

from tests.test_security_round import ADMIN, _local_app


# --------------------------------------------------------------------------- #
# CSRF guard behind a TLS front that drops the public port from Host
# --------------------------------------------------------------------------- #

def test_origin_with_public_port_passes_when_front_forwards_host_without_port(tmp_path):
    with TestClient(_local_app(tmp_path)) as c:
        r = c.post("/admin/dash/relays/delete", json={"ids": []}, headers={
            **ADMIN, "Host": "203.0.113.5", "X-Forwarded-Host": "203.0.113.5:8443",
            "Origin": "https://203.0.113.5:8443", "Sec-Fetch-Site": "same-origin"})
        assert r.status_code != 403


def test_default_port_origin_matches_bare_host(tmp_path):
    with TestClient(_local_app(tmp_path)) as c:
        r = c.post("/admin/dash/relays/delete", json={"ids": []}, headers={
            **ADMIN, "Host": "gw.example.com", "X-Forwarded-Proto": "https",
            "Origin": "https://gw.example.com:443", "Sec-Fetch-Site": "same-origin"})
        assert r.status_code != 403


def test_foreign_origin_still_blocked_with_forwarded_host(tmp_path):
    with TestClient(_local_app(tmp_path)) as c:
        r = c.post("/admin/dash/relays/delete", json={"ids": []}, headers={
            **ADMIN, "Host": "203.0.113.5", "X-Forwarded-Host": "203.0.113.5:8443",
            "Origin": "https://evil.example"})
        assert r.status_code == 403


def test_v1_inference_is_not_origin_guarded(tmp_path):
    with TestClient(_local_app(tmp_path)) as c:
        r = c.post("/v1/chat/completions", json={}, headers={
            "Authorization": "Bearer nope", "Origin": "https://some-chat-ui.example",
            "Sec-Fetch-Site": "cross-site"})
        # rejected by auth (401), not by the CSRF guard (403 "cross-...")
        assert "cross-" not in str(r.json().get("detail", ""))


# --------------------------------------------------------------------------- #
# relay secret handling
# --------------------------------------------------------------------------- #

def test_manual_relay_add_keeps_typed_secret_and_never_invents_one(tmp_path):
    with TestClient(_local_app(tmp_path)) as c:
        r = c.post("/admin/dash/relays/add", headers=ADMIN, json={
            "name": "a", "url": "https://a.example.app", "type": "custom", "secret": "zr_typed"})
        assert r.json()["relay"]["secret"] == "zr_typed"
        r = c.post("/admin/dash/relays/add", headers=ADMIN, json={
            "name": "b", "url": "https://b.example.app", "type": "custom"})
        assert r.json()["relay"]["secret"] == ""


def test_worker_rejections_are_tagged_so_the_client_rotates_the_relay():
    from zcode_gateway.relay_deploy import (CLOUDFLARE_WORKER_CODE, VERCEL_RELAY_CODE,
                                            DENO_RELAY_CODE, NETLIFY_RELAY_CODE)
    for code in (CLOUDFLARE_WORKER_CODE, VERCEL_RELAY_CODE, DENO_RELAY_CODE, NETLIFY_RELAY_CODE):
        assert '"x-relay-reject": "1"' in code


# --------------------------------------------------------------------------- #
# tool_choice "none"
# --------------------------------------------------------------------------- #

_TOOL = {"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}


def test_tool_choice_none_drops_tools_for_a_fresh_conversation():
    from zcode_gateway.zcode_protocol import build_body
    body = build_body({"model": "GLM-5.3", "tool_choice": "none", "tools": [_TOOL],
                       "messages": [{"role": "user", "content": "hi"}]})
    assert "tools" not in body and "tool_choice" not in body


def test_tool_choice_none_rejects_unsupported_history_combination():
    from zcode_gateway.zcode_protocol import build_body
    with pytest.raises(ValueError, match="tool_choice"):
        build_body({"model": "GLM-5.3", "tool_choice": "none", "tools": [_TOOL],
                       "messages": [
                           {"role": "user", "content": "hi"},
                           {"role": "assistant", "content": None, "tool_calls": [
                               {"id": "t1", "type": "function",
                                "function": {"name": "f", "arguments": "{}"}}]},
                           {"role": "tool", "tool_call_id": "t1", "content": "ok"}]})


# --------------------------------------------------------------------------- #
# POSIX: the whole process group dies
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_kill_tree_kills_grandchildren():
    import subprocess
    import time
    from zcode_gateway.generator import _child_kwargs, _kill_tree
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,sys,time;"
         "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
         "print(c.pid,flush=True);time.sleep(60)"],
        stdout=subprocess.PIPE, text=True, **_child_kwargs())
    grandchild = int(proc.stdout.readline())
    _kill_tree(proc)
    proc.wait(timeout=5)
    for _ in range(20):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail("grandchild survived _kill_tree")


# --------------------------------------------------------------------------- #
# proxy filter notice, updater sidecar
# --------------------------------------------------------------------------- #

def test_private_proxy_is_skipped_with_a_one_time_notice(capsys, monkeypatch):
    monkeypatch.delenv("ZAI_ALLOW_PRIVATE", raising=False)
    monkeypatch.delenv("ZAI_ALLOW_LOOPBACK", raising=False)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "generator"))
    import proxy_parse as pp
    pp._warned_private = False
    assert pp.valid_endpoint("192.168.1.5", 1080) is False
    assert pp.valid_endpoint("10.0.0.2", 1080) is False
    out = capsys.readouterr().out
    assert out.count("ZAI_ALLOW_PRIVATE=1") == 1
    monkeypatch.setenv("ZAI_ALLOW_PRIVATE", "1")
    assert pp.valid_endpoint("192.168.1.5", 1080) is True


# --------------------------------------------------------------------------- #
# debounced caps persistence
# --------------------------------------------------------------------------- #

def test_failed_caps_write_stays_dirty_and_aclose_flushes(tmp_path):
    from zcode_gateway.config import PoolConfig
    from zcode_gateway.pool import PoolManager

    async def run():
        pool = PoolManager(PoolConfig(), [])
        # a write that fails must report it so the saver keeps the data dirty
        pool.caps_path = tmp_path / "missing-dir" / "caps.json"
        assert pool._write_caps_snapshot({}) is False
        # aclose flushes dirty data even though no timer ever fired
        pool.caps_path = tmp_path / "caps.json"
        pool._caps_dirty = True
        await pool.aclose()
        assert pool.caps_path.exists() and not pool._caps_dirty

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# built-in TLS
# --------------------------------------------------------------------------- #

def test_ssl_kwargs_empty_without_tls():
    from zcode_gateway.config import ServerConfig
    assert ServerConfig(admin_key="adm-test-123456").ssl_kwargs() == {}


def test_ssl_kwargs_requires_both_and_existing_files(tmp_path):
    from zcode_gateway.config import ServerConfig
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    with pytest.raises(RuntimeError):
        ServerConfig(admin_key="adm-test-123456", tls_cert=str(cert)).ssl_kwargs()
    with pytest.raises(RuntimeError):   # both set, files missing
        ServerConfig(admin_key="adm-test-123456", tls_cert=str(cert), tls_key=str(key)).ssl_kwargs()
    cert.write_text("x"); key.write_text("y")
    kw = ServerConfig(admin_key="adm-test-123456", tls_cert=str(cert), tls_key=str(key)).ssl_kwargs()
    assert kw == {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}


# --------------------------------------------------------------------------- #
# generator environment: private venv (PEP 668), headless opencv, chromium libs
# --------------------------------------------------------------------------- #

def test_resolve_python_prefers_the_private_venv(tmp_path, monkeypatch):
    from zcode_gateway import generator, paths
    venv_py = tmp_path / "genenv" / ("Scripts" if os.name == "nt" else "bin") / \
        ("python.exe" if os.name == "nt" else "python")
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(generator, "_python_has_pip", lambda py: py == str(venv_py))
    assert generator.venv_python() == str(venv_py)
    assert generator.resolve_python("") == str(venv_py)
    # an explicit config path still wins
    assert generator.resolve_python(str(venv_py)) == str(venv_py)


def test_no_venv_means_no_override(tmp_path, monkeypatch):
    from zcode_gateway import generator, paths
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    assert generator.venv_python() == ""


def test_opencv_is_headless_off_windows_and_chromium_gets_deps_as_root():
    from zcode_gateway import generator
    if os.name != "nt":
        assert generator.GEN_PIP_NAMES["cv2"] == "opencv-python-headless"
        argv = generator._chromium_install_argv("py")
        assert argv[:4] == ["py", "-m", "playwright", "install"] and argv[-1] == "chromium"
        assert ("--with-deps" in argv) == (os.geteuid() == 0)
    else:
        assert generator.GEN_PIP_NAMES["cv2"] == "opencv-python"
        assert "--with-deps" not in generator._chromium_install_argv("py")


def test_env_setup_uses_the_configured_python_and_reports_adoption():
    from zcode_gateway.generator import EnvSetup
    es = EnvSetup("/opt/x/genenv/bin/python")
    assert es.python_exe == "/opt/x/genenv/bin/python"
    assert es.on_python is None


def test_externally_managed_python_gets_a_venv_not_a_pip_failure(tmp_path, monkeypatch):
    from zcode_gateway import generator
    es = generator.EnvSetup()
    es.python_exe = sys.executable
    made = []
    monkeypatch.setattr(es, "_find_python", lambda: sys.executable)
    monkeypatch.setattr(es, "_externally_managed", lambda py: True)
    monkeypatch.setattr(es, "_ensure_venv", lambda py: made.append(py) or "")
    es._install_flow()
    assert made == [sys.executable]
    assert es.state == "failed" and "externally managed" in es.error


# --------------------------------------------------------------------------- #
# mail worker on Linux: Node, API-token login, automatic Email Routing
# --------------------------------------------------------------------------- #

def _mail_mgr(tmp_path):
    from zcode_gateway.generator import GeneratorManager
    return GeneratorManager(tmp_path, data_dir=tmp_path)


TOKEN = "T" * 40


def test_cf_credentials_validated_stored_and_never_echoed(tmp_path):
    mgr = _mail_mgr(tmp_path)
    assert "token" in mgr.set_cf_credentials("short!", "")
    assert "account id" in mgr.set_cf_credentials("", "zzz")
    assert mgr.set_cf_credentials(TOKEN, "A" * 32) == ""
    cfg = mgr.mail_config()
    assert cfg["cf_token_set"] is True and "cf_api_token" not in cfg
    env = mgr._cf_env()
    assert env == {"CLOUDFLARE_API_TOKEN": TOKEN, "CLOUDFLARE_ACCOUNT_ID": "a" * 32}
    if os.name != "nt":
        assert oct((tmp_path / "mail.json").stat().st_mode & 0o777) == "0o600" \
            if (tmp_path / "mail.json").exists() else True


def test_headless_server_without_token_gets_a_clear_error_not_a_browser_hang(tmp_path, monkeypatch):
    from zcode_gateway import generator
    mgr = _mail_mgr(tmp_path)
    monkeypatch.setattr(generator, "_is_headless", lambda: True)
    monkeypatch.setattr(mgr, "_ensure_node", lambda progress=None: {"ok": True, "npx": "npx"})
    monkeypatch.setattr(mgr, "export_worker", lambda: {"ok": True, "path": str(tmp_path)})
    seen = []

    def fake_run(args, cwd, timeout, env=None):
        seen.append(args)
        import subprocess
        return subprocess.CompletedProcess(args, 1, "", "not logged in")

    monkeypatch.setattr(generator, "_run_capture", fake_run)
    res = mgr.deploy_worker()
    assert res["ok"] is False and "API token" in res["error"]
    assert all("login" not in a for a in seen)      # never tried the browser login


def test_private_node_goes_first_on_path(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX only")
    from zcode_gateway import generator, paths
    (tmp_path / "node" / "bin").mkdir(parents=True)
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin")
    generator._use_private_node()
    assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path / "node" / "bin")


def test_auto_routing_refuses_parent_zone_mutation_for_subdomain(tmp_path):
    mgr = _mail_mgr(tmp_path)
    mgr.set_cf_credentials(TOKEN)
    mgr.set_mail_config(domains=["mail.example.com"])
    calls = []
    state = {"enabled": False}

    def fake(method, path, body=None, params=None):
        calls.append((method, path))
        if path == "/zones":
            if params["name"] == "mail.example.com":
                return {"result": []}                      # subdomain is not a zone
            return {"result": [{"id": "Z1", "name": "example.com", "status": "active"}]}
        if path == "/zones/Z1/email/routing" and method == "GET":
            return {"result": {"enabled": state["enabled"], "status": "ready"}}
        if path == "/zones/Z1/email/routing/dns":
            state["enabled"] = True
            return {"result": {}}
        if path.endswith("/catch_all") and method == "PUT":
            state["rule"] = body
            return {"result": body}
        if path.endswith("/catch_all"):
            return {"result": state["rule"]}
        raise AssertionError(path)

    mgr._cf_call = fake
    res = mgr.auto_route_domain("mail.example.com")
    assert res["ok"] is False
    assert not any(method != "GET" for method, path in calls)
    assert mgr.mail_config()["routing_all_done"] is False


def test_auto_routing_reports_missing_zone_and_bad_token_without_marking_done(tmp_path):
    mgr = _mail_mgr(tmp_path)
    mgr.set_cf_credentials(TOKEN)
    mgr.set_mail_config(domains=["nope.example"])
    mgr._cf_call = lambda *a, **k: {"result": []}
    res = mgr.auto_route_domain("nope.example")
    assert res["ok"] is False and "not in this Cloudflare account" in res["error"]

    def boom(*a, **k):
        raise RuntimeError("Authentication error — token needs Email Routing Rules: Edit")
    mgr._cf_call = boom
    assert mgr.auto_route_domain("nope.example")["ok"] is False
    assert mgr.mail_config()["routing_all_done"] is False


def test_auto_route_all_only_touches_pending_domains(tmp_path):
    mgr = _mail_mgr(tmp_path)
    mgr.set_mail_config(domains=["a.example", "b.example"])
    mgr.set_routing_done("a.example", True)
    done = []
    mgr.auto_route_domain = lambda d: done.append(d) or {"ok": True, "domain": d}
    assert [r["domain"] for r in mgr.auto_route_all()] == ["b.example"]
    assert done == ["b.example"]


def test_cf_pick_account_uses_the_account_that_owns_the_domain(tmp_path):
    mgr = _mail_mgr(tmp_path)
    mgr.set_cf_credentials(TOKEN)
    mgr.set_mail_config(domains=["mail.example.com"])
    A, B = "a" * 32, "b" * 32

    def fake(method, path, json_body=None, params=None):
        if path == "/accounts":
            return {"result": [{"id": A, "name": "one"}, {"id": B, "name": "two"}]}
        if path == "/zones" and params["name"] == "example.com":
            return {"result": [{"id": "Z1", "account": {"id": B}}]}
        return {"result": []}

    mgr._cf_call = fake
    assert mgr._cf_pick_account() == (B, "")
    mgr.set_mail_config(domains=["other.test"])
    acct, why = mgr._cf_pick_account()
    assert acct == "" and A in why and B in why and "account id" in why

    mgr._cf_call = lambda *a, **k: {"result": [{"id": A, "name": "only"}]}
    assert mgr._cf_pick_account() == (A, "")


def test_tail_drops_the_wrangler_banner():
    from zcode_gateway.generator import _tail
    out = ("\n ⛅️ wrangler 4.155.0\n────────────────────\n\n"
           "✘ [ERROR] More than one account available\n\nLogs were written to \"/x.log\"\n")
    assert _tail(out).startswith("✘ [ERROR] More than one account")


def test_whoami_account_label_is_the_deployed_account_not_the_token_owner(tmp_path, monkeypatch):
    import zcode_gateway.generator as g

    class R:
        returncode = 0
        stdout = " 👋 You are logged in with an API Token, associated with the email owner@example.com\n"
        stderr = ""

    monkeypatch.setattr(g, "_run_capture", lambda *a, **k: R())
    mgr = _mail_mgr(tmp_path)
    mgr.set_cf_credentials(TOKEN, "b" * 32)
    mgr._cf_call = lambda method, path, *a, **k: {"result": {"name": "Br.1515@yahoo.com's Account"}}
    assert mgr._cached_whoami("npx", tmp_path)["account"] == "Br.1515@yahoo.com's Account"
    mgr._whoami_cache = None
    mgr.mail_cfg.pop("cf_account_id")
    assert mgr._cached_whoami("npx", tmp_path)["account"] == "owner@example.com"


def test_save_cf_credentials_replaces_token_without_deploy_and_can_clear(tmp_path):
    mgr = _mail_mgr(tmp_path)
    mgr.set_cf_credentials(TOKEN, "a" * 32)
    new = "N" * 40
    mgr._cf_call = lambda m, p, *a, **k: {"result": [{"id": "b" * 32, "name": "two"}]}
    res = mgr.save_cf_credentials(new, "")
    assert res["ok"] is True and res["accounts"] == [{"id": "b" * 32, "name": "two"}]
    assert mgr.mail_cfg["cf_api_token"] == new and mgr.mail_cfg["cf_account_id"] == "a" * 32
    assert mgr.save_cf_credentials("", "")["ok"] is False          # nothing to save
    assert mgr.save_cf_credentials("bad token!", "")["ok"] is False

    def refused(*a, **k):
        raise RuntimeError("Invalid API Token")
    mgr._cf_call = refused
    bad = mgr.save_cf_credentials("M" * 40, "")
    assert bad["ok"] is False and bad["saved"] is True and "Invalid API Token" in bad["error"]

    assert mgr.save_cf_credentials(clear=True)["ok"] is True
    assert mgr.mail_config()["cf_token_set"] is False
    assert "cf_account_id" not in mgr.mail_cfg


def test_auto_routing_sets_catch_all_even_when_routing_switch_is_refused(tmp_path):
    mgr = _mail_mgr(tmp_path)
    mgr.set_cf_credentials(TOKEN)
    mgr.set_mail_config(domains=["example.com"])
    state = {}

    def fake(method, path, body=None, params=None):
        if path == "/zones":
            return {"result": [{"id": "Z1", "name": "example.com", "status": "active"}]}
        if path.endswith("/catch_all") and method == "PUT":
            state["rule"] = body
            return {"result": body}
        if path.endswith("/catch_all"):
            return {"result": state.get("rule", {})}
        raise RuntimeError("Authentication error")             # GET/enable refused

    mgr._cf_call = fake
    res = mgr.auto_route_domain("example.com")
    assert res["ok"] is False and "Zone Settings" in res["error"]
    assert state["rule"]["actions"] == [{"type": "worker", "value": ["zai-mail"]}]
    assert mgr.mail_config()["routing_all_done"] is False


# --------------------------------------------------------------------------- #
# review round: lock/event loop, TLS SAN cache, routing metadata, shutdown
# --------------------------------------------------------------------------- #

def test_set_routing_done_never_waits_on_a_running_deploy(tmp_path):
    import threading
    mgr = _mail_mgr(tmp_path)
    held, release = threading.Event(), threading.Event()

    def deploy():                         # another thread owns the mail lock
        with mgr._mail_config_lock():
            held.set()
            release.wait(5)

    t = threading.Thread(target=deploy)
    t.start()
    assert held.wait(5)
    try:
        with pytest.raises(ValueError, match="busy"):
            mgr.set_routing_done("a.example", True)
    finally:
        release.set()
        t.join()
    mgr.set_routing_done("a.example", True)            # free again
    assert "a.example" in mgr.mail_config()["routing_done"]


def test_routing_route_reports_409_instead_of_blocking(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from fastapi import HTTPException
    from zcode_gateway import api_dash
    mgr = SimpleNamespace(mail_cfg={"domains": ["a.example"]},
                          set_routing_done=lambda d, v: (_ for _ in ()).throw(ValueError("busy")),
                          mail_config=lambda: {})
    monkeypatch.setattr(api_dash, "_generator", lambda request: mgr)
    with pytest.raises(HTTPException) as err:
        asyncio.run(api_dash.generator_mail_routing(object(), {"domain": "a.example"}, "k"))
    assert err.value.status_code == 409


def test_certificate_hosts_are_cached_and_follow_renewal(tmp_path):
    cryptography = pytest.importorskip("cryptography")
    import datetime, ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from zcode_gateway import config

    def make(path, ip):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(1)
                .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=2))
                .add_extension(x509.SubjectAlternativeName(
                    [x509.IPAddress(ipaddress.ip_address(ip)), x509.DNSName("*.bad.example"),
                     x509.DNSName("ok.example")]), critical=False)
                .sign(key, hashes.SHA256()))
        path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    pem = tmp_path / "fullchain.pem"
    make(pem, "203.0.113.7")
    assert set(config._certificate_hosts_cached(str(pem))) == {"203.0.113.7", "ok.example"}
    calls = []
    real = config._read_certificate_hosts
    config._read_certificate_hosts = lambda p: calls.append(p) or real(p)
    try:
        config._certificate_hosts_cached(str(pem))
        assert calls == []                                 # served from the cache
        import os
        make(pem, "203.0.113.9")
        os.utime(pem, ns=(10**18, 10**18))                 # a renewal changes mtime
        assert set(config._certificate_hosts_cached(str(pem))) == {"203.0.113.9", "ok.example"}
    finally:
        config._read_certificate_hosts = real
    assert config._certificate_hosts_cached(str(tmp_path / "missing.pem")) == ()
    assert config._certificate_hosts_cached("") == ()


def test_routing_ignores_rule_metadata_rewritten_when_routing_is_enabled(tmp_path):
    from copy import deepcopy
    mgr = _mail_mgr(tmp_path)
    mgr.set_cf_credentials(TOKEN)
    mgr.set_mail_config(domains=["example.test"])
    mgr._worker_name = lambda: "zai-mail"
    mgr._cf_zone_for = lambda d: ("Z1", "example.test", "active")
    catch_all = "/zones/Z1/email/routing/rules/catch_all"
    st = {"enabled": False,
          "rule": {"id": "old", "enabled": False, "priority": 1, "name": "",
                   "actions": [{"type": "drop"}], "matchers": [{"type": "all"}]}}

    def fake(method, path, body=None, params=None):
        if path == catch_all and method == "GET":
            return {"result": deepcopy(st["rule"])}
        if path == catch_all and method == "PUT":
            st["rule"] = deepcopy(body)
            return {"result": deepcopy(body)}
        if path == "/zones/Z1/email/routing" and method == "GET":
            return {"result": {"enabled": st["enabled"], "status": "ready"}}
        if path == "/zones/Z1/email/routing/dns":
            st["enabled"] = True                           # Cloudflare rewrites the placeholder
            st["rule"].update(id="new-id", priority=2, name="Catch-all")
            return {"result": {}}
        raise AssertionError((method, path))

    mgr._cf_call = fake
    res = mgr.auto_route_domain("example.test")
    assert res["ok"] is True and not res.get("needs_confirmation")
    assert st["rule"]["actions"] == [{"type": "worker", "value": ["zai-mail"]}]


def test_shutdown_survives_a_child_that_will_not_die(tmp_path, monkeypatch):
    import subprocess
    import threading
    from types import SimpleNamespace
    from zcode_gateway import generator
    mgr = _mail_mgr(tmp_path)
    killed = []
    monkeypatch.setattr(generator, "_kill_tree", lambda proc: killed.append(proc))

    class Stuck:
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("x", timeout)

    class Job:
        def __init__(self, proc):
            self.proc, self._thread, self.stopped = proc, None, False

        def stop(self):
            self.stopped = True

    stuck, fine = Job(Stuck()), Job(SimpleNamespace(wait=lambda timeout=None: 0))
    mgr.workers = {"a": stuck, "b": fine}
    mgr.claims = {}
    mgr.checker = SimpleNamespace(shutdown=lambda: None)
    display = Stuck()                                     # even the display may hang
    mgr._display_proc, mgr._display = display, ":99"
    mgr._display_lock = threading.Lock()
    mgr.shutdown()                                        # must not raise
    assert stuck.stopped and fine.stopped
    assert display in killed and mgr._display_proc is None
