"""Signup captcha capture and claim identity regressions.

The signup fallback used to synthesize `captcha_verify_param` from Aliyun's
verify response with a hardcoded scene id and a T001-only success test, so a
rotated scene or a T006 traceless pass produced no token at all. These tests pin
the replacement contract:

  * the hook takes the live scene and accepts T006 / lowercase `result`
  * an unknown scene never fabricates a token
  * the fallback signup body matches the web client (data-URI avatar)
  * claim headers no longer claim one hardcoded personal timezone
"""
from __future__ import annotations

import ast
import json
import sys
import types
from pathlib import Path

import pytest

GEN = Path(__file__).resolve().parent.parent / "generator"
SOURCE = GEN / "main.py"

if str(GEN) not in sys.path:
    sys.path.insert(0, str(GEN))

import plan_check  # noqa: E402


def _load_main_helpers(*names: str) -> dict:
    """Pull pure helpers out of main.py without importing playwright."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    wanted_functions = {"_captcha_verify_hook", "_signup_payload"}
    wanted_assigns = {"AVATAR_DATA_URI"}
    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted_functions:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in wanted_assigns for t in node.targets
        ):
            body.append(node)
    module = ast.Module(body=body, type_ignores=[])
    ns: dict = {"json": json}
    exec(compile(module, str(SOURCE), "exec"), ns)
    return {name: ns[name] for name in names}


def test_hook_uses_live_scene_and_accepts_traceless_success():
    hook = _load_main_helpers("_captcha_verify_hook")["_captcha_verify_hook"]("36qgs6xb")

    assert 'var SCENE = "36qgs6xb";' in hook
    assert "T006" in hook, "a traceless pass must count as success"
    assert "j.Result || j.result" in hook, "the SDK switches casing"
    assert "securityToken" in hook
    assert "certifyId" in hook


def test_hook_never_fabricates_a_token_without_a_scene():
    hook = _load_main_helpers("_captcha_verify_hook")["_captcha_verify_hook"]("")

    assert 'var SCENE = "";' in hook
    assert "if (!scene || !r.certifyId) return;" in hook
    # The widget's own init options win over the baked-in default.
    assert "__aliyunCaptchaOptions" in hook


def test_signup_payload_matches_the_client_body():
    helpers = _load_main_helpers("_signup_payload", "AVATAR_DATA_URI")
    payload = helpers["_signup_payload"]("A B", "a@b.test", "pw", "token")

    assert payload == {
        "name": "A B",
        "email": "a@b.test",
        "password": "pw",
        "profile_image_url": helpers["AVATAR_DATA_URI"],
        "sso_redirect": None,
        "captcha_verify_param": "token",
    }
    assert payload["profile_image_url"].startswith("data:image/png;base64,")
    assert payload["profile_image_url"] != "/user.png"


@pytest.fixture(autouse=True)
def _reset_timezone_cache():
    plan_check._TZ_CACHE = None
    yield
    plan_check._TZ_CACHE = None


def test_client_timezone_prefers_explicit_override(monkeypatch):
    monkeypatch.setenv("ZAI_CLIENT_TIMEZONE", "Europe/Berlin")

    assert plan_check.client_timezone() == "Europe/Berlin"


def test_client_timezone_follows_the_exit_ip(monkeypatch):
    monkeypatch.delenv("ZAI_CLIENT_TIMEZONE", raising=False)
    fake = types.ModuleType("fingerprint")
    fake.exit_ip_geo = lambda proxy=None: ("US", "America/New_York")
    monkeypatch.setitem(sys.modules, "fingerprint", fake)

    assert plan_check.client_timezone() == "America/New_York"


def test_client_timezone_falls_back_to_utc(monkeypatch):
    monkeypatch.delenv("ZAI_CLIENT_TIMEZONE", raising=False)
    fake = types.ModuleType("fingerprint")
    fake.exit_ip_geo = lambda proxy=None: ("", "")
    monkeypatch.setitem(sys.modules, "fingerprint", fake)

    assert plan_check.client_timezone() == "UTC"
    assert plan_check.build_headers("tok")["x-client-timezone"] == "UTC"
