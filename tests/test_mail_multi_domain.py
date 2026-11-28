"""Multi-domain email: active-domain selection, rotation, and readiness."""
import ast
import json
import logging
import os
import random
import string
from pathlib import Path
from types import SimpleNamespace

import pytest

from zcode_gateway.generator import GeneratorManager

MAIN = Path(__file__).resolve().parents[1] / "generator" / "main.py"


@pytest.fixture
def mgr(tmp_path):
    m = GeneratorManager(tmp_path, data_dir=tmp_path)
    m.set_mail_config(domains=["a.com", "b.com", "c.com"],
                      inbox_url="https://w.invalid/inbox", inbox_secret="s3cret")
    m.mark_inbox_verified(True)
    return m


def test_active_mode_uses_only_the_active_domain(mgr):
    cfg = mgr.mail_config()
    assert cfg["active"] == "a.com" and cfg["rotate"] is False
    assert cfg["domains_in_use"] == ["a.com"]
    assert cfg["ready"] is False                     # a.com not routed yet

    mgr.set_routing_done("a.com", True)
    assert mgr.mail_config()["ready"] is True        # b/c pending no longer block


def test_selecting_another_active_domain(mgr):
    mgr.set_routing_done("a.com", True)
    mgr.set_mail_config(active="b.com")
    cfg = mgr.mail_config()
    assert cfg["domains_in_use"] == ["b.com"]
    assert cfg["ready"] is False                     # b.com itself must be routed
    mgr.set_routing_done("b.com", True)
    assert mgr.mail_config()["ready"] is True

    mgr.set_mail_config(active="unknown.com")        # not in the list: ignored
    assert mgr.mail_config()["active"] == "b.com"


def test_rotate_uses_every_routed_domain_active_first(mgr):
    mgr.set_mail_config(rotate=True, active="c.com")
    assert mgr.mail_config()["ready"] is False       # nothing routed yet
    mgr.set_routing_done("a.com", True)
    mgr.set_routing_done("c.com", True)
    cfg = mgr.mail_config()
    assert cfg["domains_in_use"] == ["c.com", "a.com"]
    assert cfg["ready"] is True

    mgr.set_mail_config(rotate=False)
    assert mgr.mail_config()["domains_in_use"] == ["c.com"]


def test_rotate_flag_is_strictly_boolean(mgr):
    mgr.set_mail_config(rotate="yes")
    assert mgr.mail_config()["rotate"] is False


def test_removing_active_domain_falls_back(mgr):
    mgr.set_mail_config(active="b.com")
    mgr.set_mail_config(domains=["a.com", "c.com"])
    assert mgr.mail_config()["active"] == "a.com"


def _catch_all_mail_class(env, mail_domains=("stale.example",)):
    """Load CatchAllMail from generator/main.py without running the script."""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CatchAllMail")
    ns = {"os": SimpleNamespace(environ=env), "random": random, "string": string,
          "logging": logging, "json": json, "Path": Path,
          "__file__": str(MAIN.parent / "no-such-dir" / "main.py"),
          "CONFIG": {"mail_domains": list(mail_domains), "mail_domain": ""},
          "_secret_filter": SimpleNamespace(remember=lambda _v: None)}
    exec(compile(ast.Module([cls], []), str(MAIN), "exec"), ns)
    return ns["CatchAllMail"]


def test_signup_worker_rotates_across_env_domains():
    env = {"ZAI_MAIL_DOMAIN": "a.com", "ZAI_MAIL_DOMAINS": "a.com,B.com"}
    mail = _catch_all_mail_class(env)()
    seen = set()
    for _ in range(200):
        assert mail.create_account()
        seen.add(mail.email.split("@", 1)[1])
    # only the dashboard's routed set; never the unrelated CONFIG entry
    assert seen == {"a.com", "b.com"}


def test_signup_worker_single_domain_without_list():
    mail = _catch_all_mail_class({"ZAI_MAIL_DOMAIN": "a.com"}, mail_domains=())()
    assert mail.create_account()
    assert mail.email.endswith("@a.com")


MAIL_JS = (Path(__file__).resolve().parents[1] / "zcode_gateway" / "dashboard" / "assets"
           / "js" / "views" / "generator" / "mail.js")
VALID = ["b3hnamr.me", "mail.example.co.uk", "xn--d1acufc.xn--p1ai", "example.xn--p1ai"]
INVALID = ["a.b", "-bad.com", "bad-.com", "example.c0m", "http://x.com", "a..com",
           "user@x.com", "example"]


def test_backend_accepts_idn_tlds_and_rejects_junk(mgr):
    mgr.set_mail_config(domains=VALID + INVALID)
    assert mgr.mail_config()["domains"] == VALID


def test_dashboard_domain_pattern_matches_backend():
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    line = next(l for l in MAIL_JS.read_text(encoding="utf-8").splitlines()
                if l.startswith("const DOMAIN_RE"))
    script = line + "\nconsole.log(JSON.stringify(%s.map(d => DOMAIN_RE.test(d))));" % json.dumps(
        VALID + INVALID)
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [True] * len(VALID) + [False] * len(INVALID)
    assert re.search(r"added\.some\(d => !DOMAIN_RE\.test\(d\)\)", MAIL_JS.read_text(encoding="utf-8"))


def test_review_state_is_never_stale_or_hidden():
    src = MAIL_JS.read_text(encoding="utf-8")
    # setup starts from fresh routing results
    assert "state.review.clear();\n    state.deferred.clear();" in src
    # a changed rule after "Replace routing" opens its review instead of collapsing it
    auto_route = src[src.index("async function autoRoute"):src.index("function keepRouting")]
    assert "state.deferred.add" not in auto_route
    assert "state.deferred.delete(d);" in auto_route
    # an error result drops the old review in both result handlers
    remember = src[src.index("function rememberRouting"):src.index("async function deploy")]
    assert "state.review.delete(domain);\n        state.deferred.delete(domain);\n        state.errors.set" in remember
    assert "state.review.delete(d);\n          state.errors.set(d, res.error);" in auto_route
