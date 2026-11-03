"""Offline startup supervision and per-launch update health regressions."""

import hashlib
import os
import ssl
import subprocess
import sys
import threading
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

import pytest

import gui_main as gui
from zcode_gateway import updater


@pytest.fixture(autouse=True)
def reset_supervisor(monkeypatch):
    monkeypatch.setattr(gui, "_server", None)
    monkeypatch.setattr(gui, "_server_error", None)
    monkeypatch.setattr(gui, "_server_failed", threading.Event())
    monkeypatch.delenv("ZCODE_GATEWAY_UPDATE_NONCE", raising=False)


@pytest.mark.parametrize("error", [SystemExit(1), ValueError("bad configuration")])
def test_worker_publishes_terminal_failure(monkeypatch, error):
    def fail():
        raise error

    monkeypatch.setattr(gui, "_run_server", fail)
    gui._server_worker()
    assert gui._server_failed.is_set()
    assert gui._server_error is error
    network = Mock(side_effect=AssertionError("must not probe a failed server"))
    monkeypatch.setattr(gui.urllib.request, "urlopen", network)
    assert not gui._wait_for("http://127.0.0.1/dash")
    network.assert_not_called()


def test_clean_worker_exit_also_wakes_browser_supervisor(monkeypatch):
    monkeypatch.setattr(gui, "_run_server", lambda: None)
    gui._server_worker()
    assert gui._server_failed.is_set()
    assert isinstance(gui._server_error, RuntimeError)


@pytest.mark.parametrize("mode", ["bind_failure", "lifespan_return", "configuration_failure"])
def test_actual_server_startup_failure_is_terminal_without_retries(monkeypatch, mode):
    from zcode_gateway import app, config
    import uvicorn

    cfg = SimpleNamespace(server=SimpleNamespace(
        admin_key="dummy-key", host="127.0.0.1", port=9876, ssl_kwargs=lambda: {}))
    load = Mock(return_value=cfg)
    if mode == "configuration_failure":
        load.side_effect = ValueError("invalid config")
    monkeypatch.setattr(config, "load_config", load)
    monkeypatch.setattr(app, "create_app", lambda _: object())
    monkeypatch.setattr(uvicorn, "Config", lambda *_, **__: object())
    run = Mock(side_effect=SystemExit(1) if mode == "bind_failure" else None)
    monkeypatch.setattr(uvicorn, "Server", lambda _: SimpleNamespace(started=False, run=run))
    sleep = Mock(side_effect=AssertionError("startup failure must not enter backoff"))
    monkeypatch.setattr(gui.time, "sleep", sleep)
    gui._server_worker()
    assert gui._server_failed.is_set()
    assert gui._server_error is not None
    load.assert_called_once()
    sleep.assert_not_called()


def test_readiness_never_accepts_unowned_listener(monkeypatch):
    monkeypatch.setattr(gui, "_server", SimpleNamespace(started=False))
    network = Mock(side_effect=AssertionError("must wait for own bound listener"))
    monkeypatch.setattr(gui.urllib.request, "urlopen", network)
    assert not gui._wait_for("http://127.0.0.1/dash", timeout=0.001)
    network.assert_not_called()


@pytest.mark.parametrize("received,ready", [("other-instance", False), ("a" * 32, True)])
def test_update_readiness_requires_exact_nonce(monkeypatch, received, ready):
    monkeypatch.setenv("ZCODE_GATEWAY_UPDATE_NONCE", "a" * 32)
    monkeypatch.setattr(gui, "_server", SimpleNamespace(started=True))
    response = Mock(status=200, headers={"X-AutoZCoder-Update": received})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    network = Mock(return_value=response)
    monkeypatch.setattr(gui.urllib.request, "urlopen", network)
    assert gui._wait_for("http://127.0.0.1:9876/dash", timeout=0.001) is ready
    request = network.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:9876/health"
    assert request.get_header("X-autozcoder-update") == "a" * 32


def test_gui_failed_readiness_exits_without_browser_fallback(monkeypatch, tmp_path):
    from zcode_gateway import config

    monkeypatch.setattr(sys, "argv", ["gateway", "--config", str(tmp_path / "dummy.yaml")])
    monkeypatch.setattr(gui, "_fix_streams", lambda: None)
    monkeypatch.setattr(config, "load_config", lambda _: SimpleNamespace(
        server=SimpleNamespace(child_connection=lambda: ("http://127.0.0.1:9876", ""))))
    monkeypatch.setattr(gui, "_already_running", lambda: False)
    monkeypatch.setattr(gui.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(gui, "_wait_for", lambda *_, **__: False)
    notify = Mock()
    browser = Mock()
    monkeypatch.setattr(gui, "_notify", notify)
    monkeypatch.setattr(gui.webbrowser, "open", browser)
    with pytest.raises(SystemExit) as exited:
        gui.main()
    assert exited.value.code == 1
    notify.assert_called_once()
    browser.assert_not_called()


def test_headless_startup_exception_is_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["gateway", "--headless", "--config", str(tmp_path / "dummy.yaml")])
    monkeypatch.setattr(gui, "_fix_streams", lambda: None)
    monkeypatch.setattr(gui, "_run_server", Mock(side_effect=ValueError("invalid")))
    with pytest.raises(SystemExit) as exited:
        gui.main()
    assert exited.value.code == 1


def test_startup_probe_manifest_has_no_config_secrets(monkeypatch):
    from zcode_gateway import config

    monkeypatch.setattr(config, "load_config", lambda _: SimpleNamespace(server=SimpleNamespace(
        admin_key="must-not-leak", child_connection=lambda: ("http://127.0.0.1:9876", ""))))
    first = updater._startup_probe_spec(None)
    second = updater._startup_probe_spec(None)
    assert first["health_url"] == "http://127.0.0.1:9876/health"
    assert first["certificate_sha256"] == ""
    assert len(bytes.fromhex(first["startup_nonce"])) == 16
    assert first["startup_nonce"] != second["startup_nonce"]
    assert "must-not-leak" not in str(first)


def test_powershell_health_protocol_is_fail_closed():
    script = updater._SWAPPER_PS
    assert "Wait-StartupHealth" in script
    assert "response.Headers[\"X-AutoZCoder-Update\"]" in script
    assert "StringComparison.Ordinal" in script
    assert "request.Proxy = null" in script
    assert "request.AllowAutoRedirect = false" in script
    assert "request.ServerCertificateValidationCallback" in script
    assert "ServicePointManager.ServerCertificateValidationCallback" not in script
    assert "Replacement did not acknowledge healthy startup" in script
    assert "Start-Application $true" in script
    assert "Startup liveness is not a health check" not in script


def test_https_probe_pins_exact_certificate(monkeypatch, tmp_path):
    from zcode_gateway import config

    der = b"dummy DER certificate bytes: conversion only, never loaded as a certificate"
    path = tmp_path / "fixture.pem"
    path.write_text(ssl.DER_cert_to_PEM_cert(der) + ssl.DER_cert_to_PEM_cert(b"issuer"), encoding="ascii")
    monkeypatch.setattr(config, "load_config", lambda _: SimpleNamespace(server=SimpleNamespace(
        child_connection=lambda: ("https://localhost:9876", str(path)))))
    spec = updater._startup_probe_spec(None)
    assert spec["certificate_sha256"] == hashlib.sha256(der).hexdigest()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell helper")
@pytest.mark.parametrize("header,success", [("a" * 32, True), ("other-instance", False), (None, False)])
def test_real_powershell_health_matches_only_replacement_nonce(tmp_path, header, success):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/health"
            self.send_response(200)
            if header:
                self.send_header("X-AutoZCoder-Update", header)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    function = updater._SWAPPER_PS.split("function Wait-StartupHealth {", 1)[1].split("\ntry {\n    # Keep Process", 1)[0]
    script = tmp_path / "health.ps1"
    script.write_text(
        "$ErrorActionPreference='Stop'\nfunction Wait-StartupHealth {" + function
        + "\n$m=@{startup_seconds=1;startup_nonce='" + "a" * 32
        + "';certificate_sha256='';health_url='http://127.0.0.1:"
        + str(server.server_port) + "/health'}\n"
        + "$newProcess=[Diagnostics.Process]::GetCurrentProcess()\n"
        + "try { Wait-StartupHealth; exit 0 } catch { Write-Error $_; exit 1 }",
        encoding="utf-8-sig")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-File", str(script)],
                                capture_output=True, timeout=15)
        assert result.returncode == (0 if success else 1), result.stderr.decode(errors="replace")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
