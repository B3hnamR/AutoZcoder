"""Security hardening round: auth fail-closed, relay secrets, CSRF guard,
ops gating, tool_choice mapping, worker allowlist."""
import pytest
from fastapi.testclient import TestClient

from zcode_gateway.config import Config, ServerConfig
from zcode_gateway.deps import _has_credential
from zcode_gateway.relay_deploy import (CLOUDFLARE_WORKER_CODE, VERCEL_RELAY_CODE,
                                        DENO_RELAY_CODE, NETLIFY_RELAY_CODE,
                                        bake_relay_code, RELAY_ALLOW_DEFAULT)


# --------------------------------------------------------------------------- #
# H2: an empty admin key authenticates NOTHING
# --------------------------------------------------------------------------- #

class _State:
    def __init__(self, admin_key):
        from types import SimpleNamespace
        self.cfg = SimpleNamespace(server=SimpleNamespace(admin_key=admin_key))


def test_empty_admin_key_authenticates_nothing():
    state = _State("")
    assert _has_credential(state, "") is False          # empty vs empty: reject
    assert _has_credential(state, "anything") is False


def test_real_admin_key_still_compares():
    state = _State("adm-test-12345")
    assert _has_credential(state, "adm-test-12345") is True
    assert _has_credential(state, "adm-test-12346") is False
    assert _has_credential(state, "") is False


def test_server_config_rejects_short_admin_keys():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ServerConfig(admin_key="short")
    with pytest.raises(ValidationError):
        ServerConfig(admin_key="")


# --------------------------------------------------------------------------- #
# H1: relay hardening
# --------------------------------------------------------------------------- #

def test_add_relay_rejects_plain_http(tmp_path):
    from zcode_gateway.relay_manager import RelayManager
    mgr = RelayManager(tmp_path / "relays.json")
    with pytest.raises(ValueError, match="https"):
        mgr.add_relay("r", "http://relay.example.app", "custom")


def test_add_relay_allows_loopback_http_for_dev(tmp_path):
    from zcode_gateway.relay_manager import RelayManager
    mgr = RelayManager(tmp_path / "relays.json")
    entry = mgr.add_relay("local", "http://127.0.0.1:8787/relay", "custom")
    assert entry["url"].startswith("http://127.0.0.1")


def test_add_relay_generates_secret_and_pick_returns_it(tmp_path):
    from zcode_gateway.relay_manager import RelayManager
    mgr = RelayManager(tmp_path / "relays.json")
    entry = mgr.add_relay("cf", "https://relay.workers.dev", "cloudflare")
    assert entry["secret"].startswith("zr_")
    mgr.update_options(enabled=True, models=["GLM-5.3"], active_id=entry["id"])
    picked = mgr.pick_relay_for_model("GLM-5.3")
    assert picked["url"] == "https://relay.workers.dev"
    assert picked["secret"] == entry["secret"]


def test_worker_codes_carry_allowlist_and_secret_baking():
    for code in (CLOUDFLARE_WORKER_CODE, VERCEL_RELAY_CODE,
                 DENO_RELAY_CODE, NETLIFY_RELAY_CODE):
        # module constants carry placeholders; a baked deploy has the real
        # allowlist + per-relay secret
        assert "__RELAY_SECRET__" in code
        baked_default = bake_relay_code(code, "")
        assert "zcode.z.ai" in baked_default              # allowlist default
        assert "x-relay-target host not allowed" in baked_default
        assert "must be https" in baked_default
        assert "no-store" in baked_default                # CDN cache off
        # no placeholder survives baking
        assert "__RELAY_ALLOW__" not in baked_default
    baked = bake_relay_code(VERCEL_RELAY_CODE, "zr_deadbeef")
    assert '"zr_deadbeef"' in baked
    assert "zcode.z.ai" in baked
    assert "__RELAY_SECRET__" not in baked


# --------------------------------------------------------------------------- #
# H3: cross-origin mutation guard + ops gating on public binds
# --------------------------------------------------------------------------- #

def _public_app(tmp_path):
    return create_app_public(tmp_path)


def create_app_public(tmp_path):
    from zcode_gateway.app import create_app
    return create_app(Config(
        server={"host": "0.0.0.0", "port": 8788, "admin_key": "adm-test-123456",
                "require_client_key": True,
                "client_keys": [{"key": "cli-test", "name": "local"}]},
        accounts={"source": "accounts_dir", "dir": str(tmp_path / "accounts")},
        store={"sqlite_path": str(tmp_path / "gw.db")},
        update={"enabled": False}, logging={"capture": False}))


def _local_app(tmp_path):
    from zcode_gateway.app import create_app
    return create_app(Config(
        server={"host": "127.0.0.1", "port": 8788, "admin_key": "adm-test-123456",
                "allowed_hosts": ["127.0.0.1", "localhost", "203.0.113.5", "gw.example.com"],
                "client_keys": [{"key": "cli-test", "name": "local"}]},
        accounts={"source": "accounts_dir", "dir": str(tmp_path / "accounts")},
        store={"sqlite_path": str(tmp_path / "gw.db")},
        update={"enabled": False}, logging={"capture": False}))


ADMIN = {"Authorization": "Bearer adm-test-123456"}


def test_cross_site_mutation_blocked(tmp_path):
    with TestClient(_local_app(tmp_path)) as c:
        r = c.post("/admin/dash/relays/delete", headers={
            **ADMIN, "Origin": "https://evil.example",
            "Content-Type": "application/json"}, json={"ids": []})
        assert r.status_code == 403
        assert "cross-origin" in r.json()["detail"] or "cross-site" in r.json()["detail"]
        r2 = c.post("/admin/dash/relays/delete", headers={
            **ADMIN, "Sec-Fetch-Site": "cross-site",
            "Content-Type": "application/json"}, json={"ids": []})
        assert r2.status_code == 403


def test_same_origin_and_non_browser_mutations_pass(tmp_path):
    with TestClient(_local_app(tmp_path)) as c:
        # no Origin/Sec-Fetch headers at all: curl / SDK clients
        r = c.post("/admin/dash/relays/add", headers=ADMIN,
                   json={"name": "r", "url": "https://r.example.app", "type": "custom"})
        assert r.status_code == 200
        rid = r.json()["relay"]["id"]
        # same-origin browser context (TestClient's Host header is "testserver")
        r2 = c.post("/admin/dash/relays/delete",
                    headers={**ADMIN, "Origin": "http://testserver",
                             "Sec-Fetch-Site": "same-origin"},
                    json={"ids": [rid]})
        assert r2.status_code == 200


def test_ops_endpoints_hidden_on_public_bind(tmp_path):
    """Default TestClient peer models the local operator (ops visible); a
    remote visitor on the public bind sees 404s (F4: peer, not bind)."""
    with TestClient(_public_app(tmp_path)) as c:
        assert c.get("/health").status_code == 200
    with _remote_client(_public_app(tmp_path)) as c:
        assert c.get("/metrics").status_code == 404
        assert c.get("/ready").status_code == 404
        assert c.get("/health").status_code == 404


def test_admin_loopback_only_on_public_bind(tmp_path):
    """F4: on a public bind the remote visitor is refused admin even with a
    valid key (peer check, not bind check). The local operator passes."""
    with _remote_client(_public_app(tmp_path)) as c:
        r = c.get("/admin/dash/relays", headers=ADMIN)
        assert r.status_code == 403
        assert "allow_remote_admin" in r.json()["detail"]
    with TestClient(_public_app(tmp_path)) as c:
        r2 = c.get("/admin/dash/relays", headers=ADMIN)
        assert r2.status_code == 200


def _remote_client(app, ip="203.0.113.9"):
    return TestClient(app, client=(ip, 50000))


def test_ops_endpoints_hidden_from_remote_visitor_behind_local_proxy(tmp_path):
    """F4: the documented VPS topology — gateway on 127.0.0.1, same-box Caddy
    in front. uvicorn rewrites request.client from X-Forwarded-For, so the
    remote visitor (TestClient client=203.0.113.9) must NOT see ops endpoints
    despite the loopback bind."""
    with _remote_client(_local_app(tmp_path)) as c:
        assert c.get("/metrics").status_code == 404
        assert c.get("/ready").status_code == 404
        assert c.get("/health").status_code == 404


def test_admin_hidden_from_remote_visitor_behind_local_proxy(tmp_path):
    """F4: same topology — /admin/* must refuse the remote visitor even with
    a VALID admin key, unless allow_remote_admin is set."""
    with _remote_client(_local_app(tmp_path)) as c:
        r = c.get("/admin/dash/relays", headers=ADMIN)
        assert r.status_code == 403
        assert "allow_remote_admin" in r.json()["detail"]

    # opt-in flag reopens it (still key-gated)
    from zcode_gateway.app import create_app
    app = create_app(Config(
        server={"host": "127.0.0.1", "port": 8788,
                "admin_key": "adm-test-123456",
                "allow_remote_admin": True,
                "client_keys": [{"key": "cli-test", "name": "local"}]},
        accounts={"source": "accounts_dir", "dir": str(tmp_path / "accounts")},
        store={"sqlite_path": str(tmp_path / "gw.db")},
        update={"enabled": False}, logging={"capture": False}))
    with _remote_client(app) as c:
        r = c.get("/admin/dash/relays", headers=ADMIN)
        assert r.status_code == 200
        r2 = c.get("/admin/dash/relays")   # no key still refused
        assert r2.status_code == 403


def test_csrf_guard_compares_ports_and_rejects_null_origin(tmp_path):
    with TestClient(_local_app(tmp_path)) as c:
        # same host, different port: must be rejected (old check compared
        # hostnames only and let this through)
        r = c.post("/admin/dash/relays/add", headers={
            **ADMIN, "Origin": "http://testserver:9999"}, json={})
        assert r.status_code == 403
        # Origin: null (sandboxed iframe): must be rejected, not skipped
        r2 = c.post("/admin/dash/relays/add", headers={
            **ADMIN, "Origin": "null"}, json={})
        assert r2.status_code == 403


def test_public_bind_requires_client_keys_and_real_admin_key():
    from zcode_gateway.app import create_app
    with pytest.raises(RuntimeError, match="require_client_key"):
        create_app(Config(
            server={"host": "0.0.0.0", "require_client_key": False,
                    "admin_key": "adm-test-123456"},
            update={"enabled": False}, logging={"capture": False}))
    with pytest.raises(RuntimeError, match="admin_key"):
        create_app(Config(
            server={"host": "0.0.0.0", "require_client_key": True,
                    "admin_key": "gw-admin-change-me",
                    "client_keys": [{"key": "cli-test", "name": "local"}]},
            update={"enabled": False}, logging={"capture": False}))


def test_public_bind_rejects_every_template_placeholder():
    """F2: the example keys that ship in config.example.yaml must not boot a
    public bind — the old guard compared only the exact default and let
    'gw-admin-change-me-bytes' (and the placeholder client key) through."""
    from zcode_gateway.app import create_app
    for bad_admin in ("gw-admin-change-me", "gw-admin-change-me-bytes",
                      "gw-admin-local"):
        with pytest.raises(RuntimeError, match="placeholder|admin_key"):
            create_app(Config(
                server={"host": "0.0.0.0", "require_client_key": True,
                        "admin_key": bad_admin,
                        "client_keys": [{"key": "cli-test", "name": "local"}]},
                update={"enabled": False}, logging={"capture": False}))
    # placeholder CLIENT key with a good admin key: also refuses
    with pytest.raises(RuntimeError, match="placeholder"):
        create_app(Config(
            server={"host": "0.0.0.0", "require_client_key": True,
                    "admin_key": "adm-test-123456",
                    "client_keys": [{"key": "gw-client-change-me",
                                     "name": "local"}]},
            update={"enabled": False}, logging={"capture": False}))
    # no client keys at all: refuses
    with pytest.raises(RuntimeError, match="client_keys"):
        create_app(Config(
            server={"host": "0.0.0.0", "require_client_key": True,
                    "admin_key": "adm-test-123456"},
            update={"enabled": False}, logging={"capture": False}))


def test_config_example_placeholders_are_all_rejected():
    """Load the literal keys out of config.example.yaml and prove none of
    them passes the public-bind guard (keeps the guard honest when the
    example file changes)."""
    import pathlib
    import yaml
    from zcode_gateway.config import ServerConfig
    from zcode_gateway.app import create_app

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    raw = yaml.safe_load(
        (repo_root / "config.example.yaml").read_text(encoding="utf-8"))
    admin_key = raw["server"]["admin_key"]
    client_key = raw["server"]["client_keys"][0]["key"]
    sc = ServerConfig(admin_key=admin_key,
                      client_keys=[{"key": client_key, "name": "local"}])
    assert sc.admin_key_is_placeholder(), \
        f"config.example.yaml admin_key {admin_key!r} is not in the guard set"
    assert sc.has_placeholder_client_key(), \
        f"config.example.yaml client key {client_key!r} is not in the guard set"
    with pytest.raises(RuntimeError):
        create_app(Config(
            server={"host": "0.0.0.0", "require_client_key": True,
                    "admin_key": admin_key,
                    "client_keys": [{"key": client_key, "name": "local"}]},
            update={"enabled": False}, logging={"capture": False}))


# --------------------------------------------------------------------------- #
# M5: tool_choice "none"
# --------------------------------------------------------------------------- #

def test_tool_choice_none_omits_tools():
    from zcode_gateway.zcode_protocol import build_body
    body = build_body({
        "model": "GLM-5.3", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function",
                   "function": {"name": "f", "parameters": {"type": "object"}}}],
        "tool_choice": "none",
    })
    assert "tools" not in body
    assert "tool_choice" not in body


def test_tool_choice_required_maps_to_any():
    from zcode_gateway.zcode_protocol import build_body
    body = build_body({
        "model": "GLM-5.3", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function",
                   "function": {"name": "f", "parameters": {"type": "object"}}}],
        "tool_choice": "required",
    })
    assert body["tool_choice"] == {"type": "any"}
    assert body["tools"]


# --------------------------------------------------------------------------- #
# M12: harness flag
# --------------------------------------------------------------------------- #

def test_bootstrap_harness_flag():
    from zcode_gateway.zcode_protocol import build_body
    from zcode_gateway.zcode_bootstrap import ZCODE_IDENTITY, ZCODE_HARNESS
    full = build_body({"model": "GLM-5.3", "max_tokens": 16,
                       "messages": [{"role": "user", "content": "hi"}]},
                      harness=True)
    assert any(b.get("text") == ZCODE_HARNESS for b in full["system"])
    ident = build_body({"model": "GLM-5.3", "max_tokens": 16,
                        "messages": [{"role": "user", "content": "hi"}]},
                       harness=False)
    assert ident["system"][0]["text"] == ZCODE_IDENTITY
    assert not any(b.get("text") == ZCODE_HARNESS for b in ident["system"])


# --------------------------------------------------------------------------- #
# M2: relay transport errors must not cool the account
# --------------------------------------------------------------------------- #

def test_relay_transport_error_carries_relay_url():
    from zcode_gateway.upstream import RelayTransportError, TransportError
    exc = RelayTransportError("boom", relay_url="https://r.example.app")
    assert isinstance(exc, TransportError)
    assert exc.relay_url == "https://r.example.app"
