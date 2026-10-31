"""Typed environment overrides and GUI-scoped verified TLS plumbing."""

import ssl
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import gui_main
from zcode_gateway.config import Config, load_config


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    for key in list(gui_main.os.environ):
        if key.startswith("ZCODE_GATEWAY_"):
            monkeypatch.delenv(key)
    path = tmp_path / "config.yaml"
    path.write_text("server:\n  admin_key: dummy-admin-credential\n", encoding="utf-8")
    return path


def test_collection_environment_overrides_are_typed(config_path, monkeypatch):
    monkeypatch.setenv("ZCODE_GATEWAY_SERVER__ALLOWED_HOSTS", '["gw.example.test", "::1"]')
    monkeypatch.setenv("ZCODE_GATEWAY_SERVER__CLIENT_KEYS", '[{"key":"dummy-client","rpm":7}]')
    monkeypatch.setenv("ZCODE_GATEWAY_MODELS__ALIASES", '{"short":"GLM-5.3"}')
    monkeypatch.setenv("ZCODE_GATEWAY_UPDATE__LEGACY_REPOS", '["example/old"]')
    cfg = load_config(config_path)
    assert cfg.server.allowed_hosts == ["gw.example.test", "::1"]
    assert "gw.example.test" in cfg.server.trusted_hosts()
    assert cfg.server.client_keys[0].key == "dummy-client"
    assert cfg.server.client_keys[0].rpm == 7
    assert cfg.models.aliases == {"short": "GLM-5.3"}
    assert cfg.update.legacy_repos == ("example/old",)


@pytest.mark.parametrize("field,value,error_field", [
    ("SERVER__ALLOWED_HOSTS", "gw.example.test", "server.allowed_hosts"),
    ("SERVER__ALLOWED_HOSTS", '"gw.example.test"', "server.allowed_hosts"),
    ("SERVER__ALLOWED_HOSTS", '[12]', "server.allowed_hosts"),
    ("SERVER__CLIENT_KEYS", '["not-an-object"]', "server.client_keys"),
    ("SERVER__ADMIN_KEY", "short", "server.admin_key"),
    ("SERVER__REQUIRE_CLIENT_KEY", "maybe", "server.require_client_key"),
    ("POOL__MAX_IN_FLIGHT_PER_ACCOUNT", "0", "pool.max_in_flight_per_account"),
    ("POOL__ACQUIRE_WAIT_SECONDS", "not-a-number", "pool.acquire_wait_seconds"),
])
def test_invalid_environment_override_fails_closed(config_path, monkeypatch, field, value, error_field):
    monkeypatch.setenv("ZCODE_GATEWAY_" + field, value)
    with pytest.raises(SystemExit, match=error_field):
        load_config(config_path)


def test_scalar_overrides_validate_and_unknown_fields_are_ignored(config_path, monkeypatch):
    monkeypatch.setenv("ZCODE_GATEWAY_SERVER__PORT", "9443")
    monkeypatch.setenv("ZCODE_GATEWAY_SERVER__REQUIRE_CLIENT_KEY", "false")
    monkeypatch.setenv("ZCODE_GATEWAY_POOL__ACQUIRE_WAIT_SECONDS", "1.5")
    monkeypatch.setenv("ZCODE_GATEWAY_SERVER__ADMIN_KEY", "123456789012")
    monkeypatch.setenv("ZCODE_GATEWAY_SERVER__UNKNOWN", "ignored")
    cfg = load_config(config_path)
    assert cfg.server.port == 9443
    assert cfg.server.require_client_key is False
    assert cfg.pool.acquire_wait_seconds == 1.5
    assert cfg.server.admin_key == "123456789012"


def test_environment_collection_replaces_yaml_value(config_path, monkeypatch):
    config_path.write_text('models:\n  aliases:\n    old: old-model\n', encoding="utf-8")
    monkeypatch.setenv("ZCODE_GATEWAY_MODELS__ALIASES", '{"new":"new-model"}')
    assert load_config(config_path).models.aliases == {"new": "new-model"}


def test_override_can_repair_invalid_yaml_field(config_path, monkeypatch):
    config_path.write_text("server:\n  admin_key: short\n", encoding="utf-8")
    monkeypatch.setenv("ZCODE_GATEWAY_SERVER__ADMIN_KEY", "dummy-repaired-admin")
    assert load_config(config_path).server.admin_key == "dummy-repaired-admin"


def test_gui_tls_context_retains_certificate_and_hostname_verification(monkeypatch):
    default_context = ssl.create_default_context()
    global_https_factory = ssl._create_default_https_context
    loaded = []
    monkeypatch.setattr(ssl, "create_default_context", lambda: default_context)
    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations",
                        lambda self, **kwargs: loaded.append(kwargs))
    context = gui_main._gateway_context("dummy-local-ca.pem")
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert loaded == [{"cafile": "dummy-local-ca.pem"}]
    assert context is default_context
    assert ssl._create_default_https_context is global_https_factory


def test_gui_readiness_uses_full_tls_origin_and_scoped_context(monkeypatch):
    context = object()
    calls = []

    def urlopen(url, **kwargs):
        calls.append((url, kwargs))
        return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(gui_main.urllib.request, "urlopen", urlopen)
    assert gui_main._wait_for("https://gw.example.test:9443/dash", context=context)
    assert calls == [("https://gw.example.test:9443/dash", {"timeout": 2, "context": context})]


def test_gui_readiness_does_not_retry_without_tls_verification(monkeypatch):
    calls = []
    clock = iter([0, 0, 2])
    monkeypatch.setattr(gui_main.time, "time", lambda: next(clock))
    monkeypatch.setattr(gui_main.time, "sleep", lambda _: None)
    context = object()

    def urlopen(url, **kwargs):
        calls.append(kwargs)
        raise ssl.SSLCertVerificationError("dummy hostname mismatch")

    monkeypatch.setattr(gui_main.urllib.request, "urlopen", urlopen)
    assert not gui_main._wait_for("https://gw.example.test:9443/dash", timeout=1, context=context)
    assert calls == [{"timeout": 2, "context": context}]


def test_gui_shutdown_preserves_admin_key_and_scoped_tls(monkeypatch):
    context = object()
    monkeypatch.setattr(gui_main, "_last_gateway_origin", "https://gw.example.test:9443")
    monkeypatch.setattr(gui_main, "_last_gateway_context", context)
    monkeypatch.setattr(gui_main, "_shutdown_admin_key", "dummy-admin-credential", raising=False)
    calls = []

    def urlopen(request, **kwargs):
        calls.append((request, kwargs))
        return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(gui_main.urllib.request, "urlopen", urlopen)
    gui_main._shutdown_generator()
    request, kwargs = calls[0]
    assert request.full_url == "https://gw.example.test:9443/admin/dash/generator/shutdown"
    assert request.get_header("Authorization") == "Bearer dummy-admin-credential"
    assert request.method == "POST"
    assert kwargs == {"timeout": 90, "context": context}


@pytest.mark.parametrize("already_running", [False, True])
@pytest.mark.parametrize("origin,ca_file", [
    ("https://gw.example.test:9443", "dummy-local-ca.pem"),
    ("http://[::1]:9888", ""),
])
def test_gui_main_uses_child_connection_for_all_urls(monkeypatch, tmp_path, already_running, origin, ca_file):
    monkeypatch.setattr(gui_main, "_last_gateway_origin", None)
    monkeypatch.setattr(gui_main, "_last_gateway_context", None)
    cfg = Config()
    monkeypatch.setattr(type(cfg.server), "child_connection", lambda self: (origin, ca_file))
    monkeypatch.setattr("zcode_gateway.config.load_config", lambda _: cfg)
    monkeypatch.setattr(gui_main, "_fix_streams", lambda: None)
    monkeypatch.setattr(gui_main, "_already_running", lambda: already_running)
    monkeypatch.setattr(sys, "argv", ["gui_main.py", "--config", str(tmp_path / "config.yaml")])
    context = object() if ca_file else None
    monkeypatch.setattr(gui_main, "_gateway_context", lambda ca: context)
    calls = []
    monkeypatch.setattr(gui_main.webbrowser, "open", lambda url: calls.append(("browser", url)))

    def urlopen(url, **kwargs):
        calls.append(("probe", url, kwargs["context"]))
        return nullcontext(SimpleNamespace(status=200))

    def ready(url, *, context):
        calls.append(("ready", url, context))
        return True

    monkeypatch.setattr(gui_main.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(gui_main, "_wait_for", ready)
    monkeypatch.setattr(gui_main, "_webview2_available", lambda: True)
    monkeypatch.setattr(gui_main.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(gui_main, "_shutdown_generator", lambda: calls.append(("shutdown",)))
    monkeypatch.setattr(gui_main.os, "_exit", lambda code: calls.append(("exit", code)))
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(
        create_window=lambda title, url, **kwargs: calls.append(("window", url)),
        start=lambda **kwargs: None))
    gui_main.main()
    assert gui_main._last_gateway_origin == origin
    assert gui_main._last_gateway_context is context
    if already_running:
        assert calls == [("probe", origin + "/dash", context), ("browser", origin + "/dash")]
    else:
        assert calls == [("ready", origin + "/dash", context), ("window", origin + "/dash"),
                         ("shutdown",), ("exit", 0)]
