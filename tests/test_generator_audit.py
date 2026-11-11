"""Generator audit regressions. All process/network boundaries are mocked."""
import base64
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from zcode_gateway import generator as gen


ROOT = Path(__file__).resolve().parents[1]


def test_concurrent_playwright_setup_runs_once(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    setup = gen.PlaywrightSetup()
    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(setup, "_probe", lambda py: "ready" if calls else "chromium-missing")

    def install(argv, emit):
        calls.append(argv)
        entered.set()
        assert release.wait(3)
        return True

    monkeypatch.setattr(setup, "_run", install)
    with ThreadPoolExecutor(max_workers=2) as threads:
        first = threads.submit(setup.ensure, "python", lambda _: None)
        assert entered.wait(3)
        second = threads.submit(setup.ensure, "python", lambda _: None)
        release.set()
        assert first.result(3) and second.result(3)
    assert len(calls) == 1


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "resolve_python", lambda explicit="": explicit or "python-test")
    monkeypatch.delenv("ZAI_CAPTCHA_CMD", raising=False)
    monkeypatch.setenv("ZAI_OUTPUT_DIR", str(tmp_path / "data"))
    return gen.GeneratorManager(tmp_path / "generator", data_dir=tmp_path / "data")


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.mark.parametrize("mode", ["off", "claim", "claim500", "claim_start"])
def test_claim_mode_round_trip(manager, mode):
    manager.set_options(claim=mode == "claim", claim500=mode == "claim500",
                        claim_start=mode == "claim_start")
    restored = gen.GeneratorManager(manager.gen_dir, data_dir=manager.data_dir)
    assert [restored.claim, restored.claim500, restored.claim_start] == [
        mode == "claim", mode == "claim500", mode == "claim_start"]


def test_enabling_legacy_claim_disables_default_start_plan(manager):
    manager.set_options(claim=True)
    assert manager.claim and not manager.claim500 and not manager.claim_start


def test_old_options_do_not_enable_new_claim_by_default(manager):
    manager._options_path().write_text(json.dumps({"claim": False, "claim500": False}))
    manager.load_options()
    assert not manager.claim_start


def test_display_missing_fails_before_worker_or_proxy(manager, monkeypatch):
    manager.captcha = "classic"
    monkeypatch.setattr(gen.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr(gen.GenWorker, "start", lambda _: pytest.fail("worker started"))
    monkeypatch.setattr(manager, "_ensure_playwright", lambda *_: pytest.fail("installer started"))
    assert "DISPLAY or Xvfb" in manager.start_workers_checked()["error"]
    assert "visible DISPLAY" in manager.start_claim("test@example.com")["error"]
    assert not manager.workers and not manager.claims


def test_managed_xvfb_readiness_environment_and_cleanup(manager, monkeypatch):
    manager.captcha = "classic"
    monkeypatch.setattr(gen.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/Xvfb")
    calls = []
    proc = SimpleNamespace(poll=lambda: None, wait=lambda **_: 0)

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        os.write(kwargs["pass_fds"][0], b"91\n")
        return proc

    killed = []
    monkeypatch.setattr(gen.subprocess, "Popen", spawn)
    monkeypatch.setattr(gen, "_kill_tree", killed.append)
    assert manager._ensure_display() == ""
    assert manager._ensure_display() == ""
    assert len(calls) == 1
    assert calls[0][0][-2:] == ["-nolisten", "tcp"]
    assert manager._worker_env("g1")["DISPLAY"] == ":91"
    assert manager._worker_env("g1")["ZAI_HEADLESS"] == "0"
    manager.shutdown()
    assert proc in killed
    assert not manager._display


def test_existing_display_does_not_spawn_xvfb(manager, monkeypatch):
    monkeypatch.setattr(gen.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":23")
    monkeypatch.setattr(gen.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("spawn"))
    assert manager._ensure_display() == ""


def test_virtual_display_overrides_wslg_and_desktop_does_not_inherit_it(manager, monkeypatch):
    monkeypatch.setattr(gen.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/Xvfb")
    proc = SimpleNamespace(poll=lambda: None, wait=lambda **_: 0)
    calls = []

    def spawn(argv, **kwargs):
        calls.append(argv)
        os.write(kwargs["pass_fds"][0], b"92\n")
        return proc

    monkeypatch.setattr(gen.subprocess, "Popen", spawn)
    manager.set_options(captcha="ai", browser_display="virtual")
    assert manager._ensure_display() == ""
    env = manager._worker_env("g1")
    assert env["DISPLAY"] == ":92"
    assert env["WAYLAND_DISPLAY"] == ""
    assert env["ZAI_HEADLESS"] == "0"
    assert len(calls) == 1
    manager.set_options(browser_display="desktop")
    assert manager._ensure_display() == ""
    assert "DISPLAY" not in manager._worker_env("g2")
    assert "DISPLAY" not in manager._child_env()
    manager.set_options(captcha="manual", browser_display="virtual")
    assert manager._ensure_display(interactive=True) == ""
    assert "DISPLAY" not in manager._worker_env("g3")


def test_display_mode_persists_and_legacy_background_migrates_on_linux(manager, monkeypatch):
    monkeypatch.setattr(gen.sys, "platform", "linux")
    manager.set_options(captcha="ai", headless=True)
    assert manager.browser_display == "virtual"
    manager.set_options(browser_display="desktop")
    restored = gen.GeneratorManager(manager.gen_dir, data_dir=manager.data_dir)
    assert restored.options()["browser_display"] == "desktop"
    manager.set_options(browser_display="invalid")
    assert manager.browser_display == "desktop"


def test_virtual_display_missing_never_falls_back_to_visible_wslg(manager, monkeypatch):
    monkeypatch.setattr(gen.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("shutil.which", lambda _: None)
    manager.set_options(captcha="ai", browser_display="virtual")
    assert "DISPLAY or Xvfb" in manager._ensure_display()
    monkeypatch.delenv("DISPLAY")
    manager.set_options(browser_display="desktop")
    assert "Desktop window requires" in manager._ensure_display()


def test_windows_display_selection_keeps_existing_launch_behavior(manager, monkeypatch):
    monkeypatch.setattr(gen.sys, "platform", "win32")
    monkeypatch.setattr(gen.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("spawn"))
    manager.set_options(captcha="ai", browser_display="virtual", headless=True)
    assert manager._ensure_display() == ""
    assert "DISPLAY" not in manager._worker_env("g1")
    assert manager._worker_env("g1")["ZAI_HEADLESS"] == "0"


@pytest.mark.parametrize("platform,captcha,strategy", [("linux", "ai", "ai"), ("win32", "manual", "auto")])
def test_platform_captcha_defaults_preserve_saved_options(tmp_path, monkeypatch, platform, captcha, strategy):
    from zcode_gateway.config import GeneratorConfig
    monkeypatch.setattr(gen.sys, "platform", platform)
    data = tmp_path / "options"
    first = gen.GeneratorManager(tmp_path, data_dir=data)
    assert first.captcha == captcha and first.captcha_strategy == strategy
    assert GeneratorConfig().captcha == captcha
    first.set_options(captcha="classic", captcha_strategy="classic")
    restored = gen.GeneratorManager(tmp_path, data_dir=data)
    assert restored.captcha == "classic" and restored.captcha_strategy == "classic"


def test_wslg_readonly_socket_directory_uses_private_abstract_display(manager, monkeypatch):
    monkeypatch.setattr(gen.sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-test")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(Path, "is_dir", lambda _: True)
    monkeypatch.setattr(gen.os, "access", lambda *_: False)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/Xvfb")
    proc = SimpleNamespace(poll=lambda: None, wait=lambda **_: 0)
    calls = []

    def spawn(argv, **kwargs):
        calls.append(argv)
        assert argv[1].startswith(":") and int(argv[1][1:]) >= 1000
        os.write(kwargs["pass_fds"][0], argv[1][1:].encode() + b"\n")
        return proc

    monkeypatch.setattr(gen.subprocess, "Popen", spawn)
    manager.captcha = "ai"
    manager.browser_display = "virtual"
    assert manager._ensure_display() == ""
    assert calls[0][2:4] == ["-nolisten", "unix"]
    assert calls[0][-2:] == ["-nolisten", "tcp"]


def test_manual_mode_does_not_hide_browser_in_virtual_display(manager, monkeypatch):
    monkeypatch.setattr(gen.sys, "platform", "linux")
    manager.captcha = "manual"
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(gen.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("spawn"))
    assert "visible DISPLAY" in manager.start_workers()["error"]


def test_tls_origin_and_custom_ai_are_distinct(manager, monkeypatch):
    manager.gateway_origin = "https://gateway.example.test:8443"
    manager.gateway_ca_file = ""
    manager.internal_token = "internal-secret"
    manager.custom_ai_url = "https://vision.example.test"
    manager.custom_ai_key = "vision-key"
    manager.custom_ai_model = "vision-model"
    env = manager._worker_env("g1")
    assert env["ZAI_GATEWAY_URL"] == manager.gateway_origin
    assert env["ZAI_GATEWAY_INTERNAL_TOKEN"] == "internal-secret"
    assert env["ZAI_CUSTOM_AI"] == "1"
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    cfg = load_script("audit_config", ROOT / "generator" / "config.py").CONFIG
    assert cfg["custom_ai"] is True
    assert cfg["gateway_url"] == manager.custom_ai_url
    assert cfg["gateway_key"] == manager.custom_ai_key
    assert cfg["gateway_ai_model"] == manager.custom_ai_model


def test_child_trust_bundle_preserves_public_roots(manager, tmp_path, monkeypatch):
    ca, public = tmp_path / "gateway.pem", tmp_path / "public.pem"
    ca.write_bytes(b"test-local-ca")
    public.write_bytes(b"test-public-roots")
    monkeypatch.setitem(sys.modules, "certifi", SimpleNamespace(where=lambda: str(public)))
    manager.gateway_ca_file = str(ca)
    env = manager._child_env()
    assert env["ZAI_GATEWAY_CA_FILE"] == str(ca)
    assert Path(env["REQUESTS_CA_BUNDLE"]).read_bytes() == b"test-public-roots\ntest-local-ca"


def test_standalone_claim_receives_bundled_sidecar(manager, tmp_path, monkeypatch):
    extracted = tmp_path / "extracted"
    sidecar = extracted / "captcha-mint" / ("captcha-mint.exe" if os.name == "nt" else "captcha-mint")
    sidecar.parent.mkdir(parents=True)
    sidecar.touch()
    monkeypatch.setattr(gen.sys, "_MEIPASS", str(extracted), raising=False)
    captured = {}

    def spawn(*_args, **kwargs):
        captured.update(kwargs["env"])
        return SimpleNamespace(stdout=io.StringIO(""), wait=lambda: 0, returncode=0)

    monkeypatch.setattr(gen.subprocess, "Popen", spawn)
    job = gen.ClaimJob("claim", "x@example.test", "direct", "", manager.gen_dir,
                       "external-python", "", data_dir=manager.data_dir)
    job.start()
    job._thread.join(timeout=1)
    assert captured["ZAI_CAPTCHA_CMD"] == manager._worker_env("g1")["ZAI_CAPTCHA_CMD"]
    assert str(sidecar) in captured["ZAI_CAPTCHA_CMD"]


def test_subdomain_routing_has_no_mutation(manager, monkeypatch):
    monkeypatch.setattr(manager, "_cf_zone_for", lambda _: ("zone-id", "example.test", "active"))
    monkeypatch.setattr(manager, "_cf_call", lambda *_a, **_kw: pytest.fail("routing touched"))
    result = manager.auto_route_domain("mail.example.test")
    assert not result["ok"] and "subdomain" in result["error"]


@pytest.mark.parametrize("enabled", [True, False])
def test_existing_catchall_preserved_before_dns_mutation(manager, monkeypatch, enabled):
    monkeypatch.setattr(manager, "_cf_zone_for", lambda _: ("zone-id", "example.test", "active"))
    monkeypatch.setattr(manager, "_worker_name", lambda: "zai-mail")
    calls = []

    def cf(method, path, *args, **kwargs):
        calls.append((method, path))
        return {"result": {"enabled": enabled, "actions": [{"type": "forward", "value": ["owner@example.test"]}]}}

    monkeypatch.setattr(manager, "_cf_call", cf)
    assert not manager.auto_route_domain("example.test")["ok"]
    assert len(calls) == 1 and calls[0][0] == "GET"
    assert "routing_done" not in manager.mail_cfg


def test_explicit_catchall_replacement_and_readback(manager, monkeypatch):
    monkeypatch.setattr(manager, "_cf_zone_for", lambda _: ("zone-id", "example.test", "active"))
    monkeypatch.setattr(manager, "_worker_name", lambda: "zai-mail")
    calls = []
    existing = {"enabled": True, "actions": [{"type": "forward", "value": ["owner@example.test"]}]}

    def cf(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if path.endswith("catch_all"):
            if method == "PUT":
                existing.clear()
                existing.update(body)
            return {"result": dict(existing)}
        return {"result": {"enabled": True, "status": "ready"}}

    monkeypatch.setattr(manager, "_cf_call", cf)
    preview = manager.auto_route_domain("example.test")
    assert preview["needs_confirmation"]
    assert manager.auto_route_domain("example.test", replace_existing=True,
                                     confirmation_fingerprint=preview["confirmation_fingerprint"])["ok"]
    body = next(body for method, _, body in calls if method == "PUT")
    assert body["matchers"] == [{"type": "all"}]
    assert body["actions"] == [{"type": "worker", "value": ["zai-mail"]}]


def test_partial_venv_is_repaired_instead_of_adopted(tmp_path, monkeypatch):
    monkeypatch.setattr("zcode_gateway.paths.app_dir", lambda: tmp_path)
    candidate = tmp_path / "genenv" / "bin" / "python"
    candidate.parent.mkdir(parents=True)
    candidate.touch()
    ready = False
    monkeypatch.setattr(gen, "_python_has_pip", lambda _: ready)
    assert gen.venv_python() == ""
    assert gen.venv_python(ready=False) == str(candidate)
    setup = gen.EnvSetup()
    calls = []

    def run(argv):
        nonlocal ready
        calls.append(argv)
        if len(calls) == 1:
            return False  # first failed creation leaves the interpreter without pip
        ready = True
        return True

    monkeypatch.setattr(setup, "_run", run)
    assert setup._ensure_venv("base-python") == ""
    assert gen.venv_python() == ""
    assert setup._ensure_venv("base-python") == str(candidate)
    assert calls == [["base-python", "-m", "venv", str(tmp_path / "genenv")]] * 2


def test_setup_always_adopts_python_and_invalidates_browser_cache(manager, monkeypatch):
    setup = manager.env_setup
    manager.playwright._done = True
    monkeypatch.setattr(setup, "_find_python", lambda: "new-python")
    monkeypatch.setattr(setup, "_externally_managed", lambda _: False)
    monkeypatch.setattr(gen, "venv_python", lambda **_: "")
    monkeypatch.setattr(setup, "_missing_deps", lambda _: [])
    monkeypatch.setattr(setup, "_chromium_ok", lambda _: True)
    setup._install_flow()
    assert setup.state == "done"
    assert manager.python_exe == manager.checker.python_exe == "new-python"
    assert not manager.playwright._done


@pytest.mark.parametrize("installer", ["env", "playwright"])
def test_installer_timeout_kills_and_reaps_open_pipe(monkeypatch, installer):
    killed, waited = [], []
    blocked = threading.Event()

    class Pipe:
        def __iter__(self):
            yield "initial progress\n"
            blocked.wait(1)

        def close(self):
            pass

    class Proc:
        stdout = Pipe()
        returncode = None

        def wait(self, timeout=None):
            waited.append(timeout)
            if not blocked.is_set():
                raise subprocess.TimeoutExpired("installer", timeout)
            self.returncode = -9
            return -9

    proc = Proc()
    monkeypatch.setattr(gen.subprocess, "Popen", lambda *_a, **_kw: proc)

    def kill(child):
        killed.append(child)
        blocked.set()

    monkeypatch.setattr(gen, "_kill_tree", kill)
    if installer == "env":
        assert not gen.EnvSetup()._run(["installer"], timeout=0.1)
    else:
        assert not gen.PlaywrightSetup()._run(["installer"], lambda _: None, timeout=0.1)
    assert killed == [proc]
    assert len(waited) == 2 and waited[0] <= 0.1


def jwt(email):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return "eyJhbGciOiJIUzI1NiJ9." + payload + ".signature"


def test_unclaimed_selector_and_success_survive_restart(manager):
    results = manager.data_dir / "results"
    results.mkdir(exist_ok=True)
    pending, granted = "pending@example.test", "granted@example.test"
    (results / "accountsg1.txt").write_text(
        f"{pending}:password:{jwt(pending)}\n{granted}:password:{jwt(granted)}\n"
        "invalid@example.test:password:VERIFICATION_NEEDED\n"
        f"guest-1@guest.com:password:{jwt('guest-1@guest.com')}\n")
    (results / "startplang1.txt").write_text(f"{granted}:password:{jwt(granted)}\n")
    manager.mark_registered(pending)  # pool bookkeeping is not claim state
    assert [a["email"] for a in manager.unclaimed_accounts()] == [pending]
    job = gen.ClaimJob("startplan", pending, "direct", "", manager.gen_dir,
                       "python-test", "", data_dir=manager.data_dir)
    job.result = {"claimed": True}
    manager._claim_done(job)
    restored = gen.GeneratorManager(manager.gen_dir, data_dir=manager.data_dir)
    assert restored.unclaimed_accounts() == []


def test_bulk_claim_uses_unclaimed_selector_not_registration(manager, monkeypatch):
    monkeypatch.setattr(manager, "pending_accounts", lambda: pytest.fail("registration selector used"))
    monkeypatch.setattr(manager, "unclaimed_accounts", lambda: [{"email": "recover@example.test"}])
    called = []

    def start(email, mode):
        called.append((email, mode))
        return {"started": "job"}

    monkeypatch.setattr(manager, "start_claim_start", start)
    assert manager.claim_all_pending("direct")["started"] == ["recover@example.test"]
    assert called == [("recover@example.test", "direct")]


def test_shutdown_cancels_queued_claim_and_prevents_late_launch(manager, monkeypatch):
    job = gen.ClaimJob("queued", "x@example.test", "proxy", "", manager.gen_dir,
                       "python-test", "", data_dir=manager.data_dir)
    manager.claims[job.name] = job
    manager.checker.set_auto_verify(True)
    manager.shutdown()
    monkeypatch.setattr(manager, "_proxy_for_claim", lambda *_: pytest.fail("route resolved"))
    manager._resolve_claim_route(job, True)
    assert job.stopped and job.state == "killed"
    assert not manager.checker.auto_verify
    assert manager.checker._auto_stop.is_set()
    assert "error" in manager.start_claim_start("other@example.test")


def test_export_reads_current_and_legacy_redirected_ledgers(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "plan_check", SimpleNamespace(
        get_zcode_jwt=None, get_plan=None, is_start_plan=None, DEVICE_MID="test"))
    module = load_script("audit_export", ROOT / "generator" / "export_starter.py")
    results = tmp_path / "results"
    results.mkdir()
    (results / "accountsg1.txt").write_text("one@example.test:p:eyJcurrent\n")
    (tmp_path / "accounts.txt").write_text("one@example.test:p:eyJold\ntwo@example.test:p:eyJlegacy\n")
    monkeypatch.setenv("ZAI_OUTPUT_DIR", str(tmp_path))
    assert module.collect_accounts() == [("one@example.test", "eyJcurrent"),
                                         ("two@example.test", "eyJlegacy")]


def test_custom_ai_emitted_body_omits_gateway_only_fields(monkeypatch):
    image_module = pytest.importorskip("PIL.Image")
    pytest.importorskip("playwright.sync_api")
    module = load_script("audit_solver", ROOT / "generator" / "captcha_solver.py")
    solver_class = next(cls for cls in vars(module).values()
                        if isinstance(cls, type) and "_method_gateway_ai" in vars(cls))
    solver = solver_class.__new__(solver_class)
    solver.config = {"custom_ai": True, "gateway_url": "https://custom.example.test",
                     "gateway_key": "vision-key", "save_captcha_images": False,
                     "gateway_ca_file": "must-not-be-loaded"}
    solver.logger = logging.getLogger("audit-test")
    solver._ai_seed_refine = lambda *_: None
    image = io.BytesIO()
    image_module.new("RGBA", (100, 40), (255, 255, 255, 255)).save(image, format="PNG")
    captured = {}

    class Opener:
        def open(self, request, **kwargs):
            captured.update(json.loads(request.data))
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"percent": 80}'}}]}).encode())

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    result = solver._method_gateway_ai({"bg_bytes": image.getvalue(), "shadow_bytes": image.getvalue(),
                                        "bg_css_width": 300})
    assert result is not None
    assert "reasoning_effort" not in captured
    assert captured["max_tokens"] == 2048

def test_mint_captcha_mode_reaches_the_child(manager):
    manager.set_options(captcha="mint")
    assert manager.captcha == "mint"
    restored = gen.GeneratorManager(manager.gen_dir, data_dir=manager.data_dir)
    assert restored.captcha == "mint"
    env = manager._worker_env("g1")
    assert env["ZAI_MANUAL"] == "0"
    assert env["ZAI_CAPTCHA_MINT"] == "1"
    # Only mint mode carries the flag: the other modes must not silently switch
    # the claim minter on or off.
    manager.set_options(captcha="auto")
    assert "ZAI_CAPTCHA_MINT" not in manager._worker_env("g2")
    manager.set_options(captcha="manual")
    assert manager._worker_env("g3")["ZAI_MANUAL"] == "1"
