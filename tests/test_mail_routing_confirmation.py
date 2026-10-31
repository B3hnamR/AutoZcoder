"""Offline routing approval regressions; every Cloudflare operation is mocked."""

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from zcode_gateway import api_dash
from zcode_gateway.generator import GeneratorManager


DOMAIN = "example.test"
WORKER = "zai-mail"
CATCH_ALL = "/zones/zone-id/email/routing/rules/catch_all"


def worker_rule():
    return {"enabled": True, "actions": [{"type": "worker", "value": [WORKER]}],
            "matchers": [{"type": "all"}]}


def forward_rule():
    return {"id": "existing-id", "name": "Personal mail", "enabled": True,
            "actions": [{"type": "forward", "value": ["owner@example.net"]}],
            "matchers": [{"type": "all"}], "priority": 10}


@pytest.fixture
def routing():
    mgr = GeneratorManager.__new__(GeneratorManager)
    mgr.mail_cfg = {"domains": [DOMAIN], "routing_done": []}
    mgr._worker_name = Mock(return_value=WORKER)
    mgr._cf_zone_for = Mock(return_value=("zone-id", DOMAIN, "active"))
    mgr.set_routing_done = Mock()
    mgr.mail_config = Mock(return_value={"domains": [DOMAIN]})
    state = {"rule": forward_rule(), "reads": 0, "calls": [], "on_read": None,
             "enabled": True, "on_enable": None, "readback_metadata": False}

    def cf_call(method, path, body=None, params=None):
        state["calls"].append((method, path, deepcopy(body)))
        if path == CATCH_ALL:
            if method == "GET":
                state["reads"] += 1
                if state["on_read"]:
                    state["on_read"](state)
                return {"result": deepcopy(state["rule"])}
            if method == "PUT":
                state["rule"] = deepcopy(body)
                if state["readback_metadata"]:
                    state["rule"]["actions"][0]["extra_api_field"] = "ignored"
                    state["rule"]["matchers"][0].update(field="", value="", id="matcher-id")
                return {"result": deepcopy(state["rule"])}
        if path == "/zones/zone-id/email/routing" and method == "GET":
            return {"result": {"enabled": state["enabled"], "status": "ready"}}
        if path == "/zones/zone-id/email/routing/dns" and method == "POST":
            state["enabled"] = True
            if state["on_enable"]:
                state["on_enable"](state)
            return {"result": {}}
        raise AssertionError((method, path, body, params))

    mgr._cf_call = Mock(side_effect=cf_call)
    return mgr, state


def mutations(state):
    return [call for call in state["calls"] if call[0] != "GET"]


def test_existing_forwarding_returns_action_destinations_and_no_mutations(routing):
    mgr, state = routing
    result = mgr.auto_route_domain(DOMAIN)
    assert result["ok"] is False and result["needs_confirmation"] is True
    assert result["existing_rule"]["actions"] == state["rule"]["actions"]
    assert result["expected_rule"]["actions"] == worker_rule()["actions"]
    assert result["worker"] == WORKER
    assert len(result["confirmation_fingerprint"]) == 64
    assert "owner@example.net" not in result["confirmation_fingerprint"]
    assert not mutations(state)
    mgr.set_routing_done.assert_not_called()


@pytest.mark.parametrize("rule", [worker_rule(), {
    "id": "rule-id", "enabled": True, "name": "Different display name",
    "actions": [{"type": "worker", "value": [WORKER], "metadata": {"id": "action-id"}}],
    "matchers": [{"type": "all", "field": "", "value": "", "metadata": "extra"}],
}])
def test_matching_enabled_worker_accepts_api_metadata_without_replacement(routing, rule):
    mgr, state = routing
    state["rule"] = deepcopy(rule)
    assert mgr.auto_route_domain(DOMAIN)["ok"] is True
    assert not mutations(state)
    mgr.set_routing_done.assert_called_once_with(DOMAIN, True)


@pytest.mark.parametrize("action", [{"type": "drop"}, {"type": "drop", "value": []},
                                     {"type": "drop", "value": [], "id": "drop-id"}])
def test_disabled_drop_default_normalizes_missing_empty_values(routing, action):
    mgr, state = routing
    state["rule"] = {"enabled": False, "actions": [action], "matchers": [{"type": "all"}]}
    state["readback_metadata"] = True
    result = mgr.auto_route_domain(DOMAIN)
    assert result["ok"] is True
    assert [call[0] for call in mutations(state)] == ["PUT"]


@pytest.mark.parametrize("rule", [
    {"enabled": True, "actions": [{"type": "drop"}], "matchers": [{"type": "all"}]},
    {"enabled": False, "actions": [{"type": "forward", "value": ["owner@example.net"]}]},
    {"enabled": False, "actions": [{"type": "worker", "value": [WORKER]}]},
    {"enabled": False, "actions": [{"type": "drop", "value": ["unexpected"]}]},
    {"enabled": True, "actions": [{"type": "worker", "value": [WORKER, "another-worker"]}],
     "matchers": [{"type": "all"}]},
    {"enabled": True, "actions": [{"type": "worker", "value": [WORKER]}],
     "matchers": [{"type": "literal", "field": "to", "value": "someone@example.test"}]},
])
def test_nonmatching_rules_are_never_automatically_overwritten(routing, rule):
    mgr, state = routing
    state["rule"] = deepcopy(rule)
    assert mgr.auto_route_domain(DOMAIN)["needs_confirmation"] is True
    assert not mutations(state)


def test_explicit_approval_with_current_fingerprint_replaces_rule(routing):
    mgr, state = routing
    review = mgr.auto_route_domain(DOMAIN)
    result = mgr.auto_route_domain(
        DOMAIN, replace_existing=True, confirmation_fingerprint=review["confirmation_fingerprint"])
    assert result["ok"] is True
    assert [call[0] for call in mutations(state)] == ["PUT"]
    assert state["rule"]["actions"] == worker_rule()["actions"]


@pytest.mark.parametrize("replace, fingerprint", [(True, ""), (True, "stale"), (False, "stale")])
def test_boolean_flag_or_unbound_fingerprint_never_authorizes_replacement(routing, replace, fingerprint):
    mgr, state = routing
    assert mgr.auto_route_domain(
        DOMAIN, replace_existing=replace, confirmation_fingerprint=fingerprint)["needs_confirmation"]
    assert not mutations(state)


def test_fingerprint_alone_is_not_explicit_approval(routing):
    mgr, state = routing
    review = mgr.auto_route_domain(DOMAIN)
    assert mgr.auto_route_domain(
        DOMAIN, confirmation_fingerprint=review["confirmation_fingerprint"])["needs_confirmation"]
    assert not mutations(state)


@pytest.mark.parametrize("field, value", [("name", "Renamed"), ("id", "new-id"),
                                         ("priority", 11), ("extra_api_field", {"version": 2})])
def test_fingerprint_covers_entire_raw_rule_not_just_action(routing, field, value):
    mgr, state = routing
    review = mgr.auto_route_domain(DOMAIN)
    state["rule"][field] = value
    result = mgr.auto_route_domain(
        DOMAIN, replace_existing=True, confirmation_fingerprint=review["confirmation_fingerprint"])
    assert result["needs_confirmation"] is True
    assert result["confirmation_fingerprint"] != review["confirmation_fingerprint"]
    assert not mutations(state)


def test_approval_is_bound_to_expected_worker(routing):
    mgr, state = routing
    review = mgr.auto_route_domain(DOMAIN)
    mgr._worker_name.return_value = "different-worker"
    result = mgr.auto_route_domain(
        DOMAIN, replace_existing=True, confirmation_fingerprint=review["confirmation_fingerprint"])
    assert result["needs_confirmation"] is True
    assert result["worker"] == "different-worker"
    assert not mutations(state)


def test_rule_change_between_approval_check_and_put_requires_fresh_review(routing):
    mgr, state = routing
    review = mgr.auto_route_domain(DOMAIN)

    def race(current):
        if current["reads"] == 3:
            current["rule"]["actions"][0]["value"] = ["changed@example.net"]

    state["on_read"] = race
    result = mgr.auto_route_domain(
        DOMAIN, replace_existing=True, confirmation_fingerprint=review["confirmation_fingerprint"])
    assert result["needs_confirmation"] is True
    assert result["existing_rule"]["actions"][0]["value"] == ["changed@example.net"]
    assert not mutations(state)


def test_placeholder_that_becomes_forwarding_during_onboarding_is_preserved(routing):
    mgr, state = routing
    state["enabled"] = False
    state["rule"] = {"enabled": False, "actions": [{"type": "drop", "value": []}]}
    state["on_enable"] = lambda current: current.update(rule=forward_rule())
    result = mgr.auto_route_domain(DOMAIN)
    assert result["needs_confirmation"] is True
    assert all(call[0] != "PUT" for call in mutations(state))
    mgr.set_routing_done.assert_not_called()


def test_readback_mismatch_never_marks_routing_done(routing):
    mgr, state = routing
    review = mgr.auto_route_domain(DOMAIN)

    def changed_readback(current):
        if current["reads"] == 4:
            current["rule"]["enabled"] = False

    state["on_read"] = changed_readback
    result = mgr.auto_route_domain(
        DOMAIN, replace_existing=True, confirmation_fingerprint=review["confirmation_fingerprint"])
    assert result["ok"] is False
    mgr.set_routing_done.assert_not_called()


def test_bulk_route_cannot_reuse_one_approval_across_domains(routing):
    mgr, state = routing
    review = mgr.auto_route_domain(DOMAIN)
    result = mgr.auto_route_all(
        replace_existing=True, confirmation_fingerprint=review["confirmation_fingerprint"])
    assert result[0]["needs_confirmation"] is True
    assert not mutations(state)


def test_api_preserves_structured_confirmation_and_forwards_explicit_approval(routing, monkeypatch):
    mgr, state = routing
    monkeypatch.setattr(api_dash, "_generator", lambda request: mgr)
    result = asyncio.run(api_dash.generator_mail_routing_auto(
        SimpleNamespace(), {"domain": DOMAIN}, key="dummy"))
    assert result["ok"] is False
    review = result["results"][0]
    assert review["needs_confirmation"] is True
    assert review["existing_rule"]["actions"] == forward_rule()["actions"]
    confirmed = asyncio.run(api_dash.generator_mail_routing_auto(
        SimpleNamespace(), {"domain": DOMAIN, "replace_existing": True,
                            "confirmation_fingerprint": review["confirmation_fingerprint"]}, key="dummy"))
    assert confirmed["ok"] is True
    assert [call[0] for call in mutations(state)] == ["PUT"]


@pytest.mark.parametrize("body", [{"replace_existing": True},
                                  {"replace_existing": True, "domain": DOMAIN},
                                  {"replace_existing": True, "confirmation_fingerprint": "not-scoped"}])
def test_api_rejects_unscoped_or_missing_approval_without_cf_calls(routing, monkeypatch, body):
    mgr, state = routing
    monkeypatch.setattr(api_dash, "_generator", lambda request: mgr)
    result = asyncio.run(api_dash.generator_mail_routing_auto(SimpleNamespace(), body, key="dummy"))
    assert result["ok"] is False
    assert "fingerprint" in result["results"][0]["error"]
    assert not state["calls"]


def test_api_keeps_cf_failure_in_result_for_ui(routing, monkeypatch):
    mgr, _ = routing
    mgr._cf_call.side_effect = RuntimeError("Email Routing Rules: Edit permission required")
    monkeypatch.setattr(api_dash, "_generator", lambda request: mgr)
    result = asyncio.run(api_dash.generator_mail_routing_auto(
        SimpleNamespace(), {"domain": DOMAIN}, key="dummy"))
    assert result["ok"] is False
    assert "permission required" in result["results"][0]["error"]


def test_email_setup_is_one_action_with_independent_readiness_checks():
    mail = (Path(__file__).resolve().parents[1]
            / "zcode_gateway/dashboard/assets/js/views/generator/mail.js").read_text(encoding="utf-8")
    wizard = mail[mail.index("function paint(m)"):mail.index("function build()")]
    assert "const credentialsSaved = !!m.inbox_url && m.inbox_secret_set === true" in wizard
    assert "const inboxVerified = credentialsSaved && m.inbox_ok === true" in wizard
    assert "doms.length > 0 && doms.every((d) => (m.routing_done || []).includes(d))" in wizard
    assert "doms.some((d) => state.review.has(d))" in wizard
    assert "const ready = inboxVerified && routingDone && !needsConfirmation" in wizard
    assert "Email ready" in wizard
    assert 'id: "mail-new-domain"' in mail and 'id: "mail-cf-token"' in mail
    assert 'label: "Set up email"' in mail
    assert "How to create a Cloudflare token" in mail
    assert "Advanced / manual recovery" in mail
    assert "An inbox test only proves the worker answers" in mail
    review = mail[mail.index("function routingRow("):mail.index("function ruleDestination(")]
    assert "ruleDestination(review.existing_rule)" in review
    assert "JSON.stringify(review.expected_rule, null, 2)" in review
    assert 'label: "Keep current routing"' in review
    assert 'label: "Replace routing"' in review
    assert "onClick: () => autoRoute(domain, true)" in review
    setup = mail[mail.index("async function deploy("):mail.index("async function autoRoute(")]
    assert 'api.post("/admin/dash/generator/mail", {' in setup
    assert 'api.post("/admin/dash/generator/mail/deploy", {' in setup
    assert "cf_token: token, cf_account_id: account" in setup
    assert "rememberRouting(r.routing)" in setup
    routing_handler = mail[mail.index("async function autoRoute("):mail.index("function keepRouting(")]
    assert "replaceExisting = false" in routing_handler
    assert 'api.post("/admin/dash/generator/mail/routing-auto", {' in routing_handler
    assert "domain, replace_existing: replaceExisting" in routing_handler
    assert "...(replaceExisting ? {" in routing_handler
    assert "confirmation_fingerprint: state.review.get(domain)?.confirmation_fingerprint" in routing_handler
    assert "state.review.set(d, res)" in routing_handler
