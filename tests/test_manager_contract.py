"""The API layer must only call things GeneratorManager really has — a method
deleted by accident (socks_ready_count) made /admin/dash/proxy/socks_ready fail
on every dashboard poll — and the claim proxy picker tries several rounds."""
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
from zcode_gateway.config import Config
from zcode_gateway.generator import GeneratorManager

ROOT = Path(__file__).resolve().parent.parent / "zcode_gateway"
ADMIN = {"Authorization": "Bearer adm-test"}


def _manager_attr_uses(src: str) -> tuple:
    """(attributes read on a GeneratorManager, attributes read on its checker,
    attributes the API layer assigns itself), found with the AST so a RelayManager
    that is also called `mgr` is not mistaken for a GeneratorManager."""
    import ast
    tree = ast.parse(src)
    used, checker, assigned = set(), set(), set()
    gen_calls, other_calls = {"_generator", "GeneratorManager"}, {"_relay_mgr", "RelayManager"}

    def kind_of(value):
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            if value.func.id in gen_calls:
                return "gen"
            if value.func.id in other_calls:
                return "other"
        return None

    def walk(node, scopes):
        scope = dict(scopes[-1]) if scopes else {}
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes = scopes + [scope]
            for st in ast.walk(node):          # which kind of manager does `mgr` hold in this function?
                if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "mgr" for t in st.targets):
                    k = kind_of(st.value)
                    if k:
                        scope["mgr"] = k
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Attribute):
                base = child.value
                is_gen = (isinstance(base, ast.Name) and base.id == "mgr" and (scopes[-1] if scopes else {}).get("mgr") == "gen")                     or kind_of(base) == "gen"
                if is_gen:
                    (assigned if isinstance(child.ctx, ast.Store) else used).add(child.attr)
                if (isinstance(base, ast.Attribute) and base.attr == "checker"
                        and ((isinstance(base.value, ast.Name) and base.value.id == "mgr"
                              and (scopes[-1] if scopes else {}).get("mgr") == "gen") or kind_of(base.value) == "gen")):
                    checker.add(child.attr)
            walk(child, scopes)

    walk(tree, [])
    return used, checker, assigned


def test_every_manager_call_in_the_api_layer_exists(tmp_path):
    mgr = GeneratorManager(tmp_path, data_dir=tmp_path)
    missing = []
    for fname in ("api_dash.py", "api_admin.py"):
        used, checker, assigned = _manager_attr_uses((ROOT / fname).read_text(encoding="utf-8"))
        missing += [f"{fname}: mgr.{n}" for n in sorted(used - assigned) if not hasattr(mgr, n)]
        missing += [f"{fname}: mgr.checker.{n}" for n in sorted(checker) if not hasattr(mgr.checker, n)]
    assert not missing, missing


def test_the_manager_owns_every_helper_it_calls_on_itself():
    """GeneratorManager methods call self._proxy_pool() etc.: each must exist on the
    manager (it once lived only on ProxyChecker, so those calls always failed)."""
    import ast
    tree = ast.parse((ROOT / "generator.py").read_text(encoding="utf-8"))
    manager = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GeneratorManager")
    defined = {n.name for n in manager.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assigned = {t.attr for n in ast.walk(manager) for t in getattr(n, "targets", [])
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"}
    called = {n.func.attr for n in ast.walk(manager)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and isinstance(n.func.value, ast.Name) and n.func.value.id == "self"}
    missing = sorted(c for c in called if c not in defined and c not in assigned)
    assert not missing, missing


def _app(tmp_path, pool=None):
    gen = tmp_path / "gen"
    (gen / "output").mkdir(parents=True)
    (gen / "main.py").write_text("", encoding="utf-8")
    (gen / "config.py").write_text("", encoding="utf-8")
    for name in ("proxy_pool.py", "proxy_geo.py", "proxy_parse.py"):    # what a real generator folder holds
        shutil.copy(ROOT.parent / "generator" / name, gen / name)
    if pool is not None:
        (gen / "output" / "pool.json").write_text(json.dumps(pool), encoding="utf-8")
    return create_app(Config(
        server={"host": "127.0.0.1", "port": 8788, "admin_key": "adm-test",
                "client_keys": [{"key": "cli-test", "name": "local"}]},
        accounts={"source": "accounts_dir", "dir": str(tmp_path / "accounts")},
        store={"sqlite_path": str(tmp_path / "gw.db")},
        update={"enabled": False}, logging={"capture": False},
        generator={"enabled": True, "zcodeauto_dir": str(gen)}))


@pytest.fixture
def keep_pool_paths(monkeypatch):
    """proxy_pool is a module-level singleton pointed at a data folder by the code
    under test: put it (and ZAI_OUTPUT_DIR) back afterwards."""
    prev = getattr(sys.modules.get("proxy_pool"), "OUTPUT_DIR", None)
    monkeypatch.setenv("ZAI_OUTPUT_DIR", os.environ.get("ZAI_OUTPUT_DIR", ""))
    yield
    mod = sys.modules.get("proxy_pool")
    if mod is not None and prev:
        mod.set_output_dir(prev)


def test_socks_ready_endpoint_answers_for_the_dashboard_poll(tmp_path, keep_pool_paths):
    import time
    now = int(time.time())

    def e(url, ok=True):
        return {"url": url, "proto": url.split("://")[0], "latency": 0.5, "fails": 0, "checks": 1,
                "last_ok": now if ok else 0, "last_check": now, "last_result": "ok" if ok else "fail"}

    pool = {u: e(u, ok) for u, ok in [("socks5://1.1.1.1:1080", True), ("socks5://2.2.2.2:1080", True),
                                      ("socks5://3.3.3.3:1080", False), ("http://4.4.4.4:80", True)]}
    with TestClient(_app(tmp_path, pool)) as c:
        r = c.get("/admin/dash/proxy/socks_ready", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["socks_ready"] == 2                       # usable socks only (not failing, not http)
    assert {"last_verified_at", "auto_verify", "auto_interval_min"} <= set(body)


# --------------------------------------------------------------------------- #
# claim proxy picker
# --------------------------------------------------------------------------- #

class _Pool:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def pick_verified(self, tries, socks):
        self.calls.append((socks, tries))
        return self.answers.pop(0) if self.answers else None


def test_claim_picker_tries_several_rounds_before_giving_up(tmp_path, monkeypatch):
    mgr = GeneratorManager(tmp_path, data_dir=tmp_path)
    lines = []
    pool = _Pool([None, "socks5://5.5.5.5:1080"])
    monkeypatch.setattr(mgr, "_proxy_pool", lambda: pool)
    assert mgr._proxy_for_claim("500m", lines.append) == "socks5://5.5.5.5:1080"
    assert pool.calls == [(True, 25), (True, 40)]           # found in round 2
    assert any("round 1/3" in ln for ln in lines) and any("round 2/3" in ln for ln in lines)


def test_only_the_500m_claim_may_use_non_socks_proxies(tmp_path, monkeypatch):
    mgr = GeneratorManager(tmp_path, data_dir=tmp_path)
    p500 = _Pool([None, None, "http://6.6.6.6:80"])
    monkeypatch.setattr(mgr, "_proxy_pool", lambda: p500)
    assert mgr._proxy_for_claim("500m") == "http://6.6.6.6:80"
    assert p500.calls[-1] == (False, 40)                    # last round accepts any type

    p100 = _Pool([None, None, "http://6.6.6.6:80"])
    monkeypatch.setattr(mgr, "_proxy_pool", lambda: p100)
    assert mgr._proxy_for_claim("100m") == ""               # the browser flow stays socks-only
    assert [c[0] for c in p100.calls] == [True, True]


def test_a_failing_pool_pick_is_an_empty_answer_not_a_crash(tmp_path, monkeypatch):
    mgr = GeneratorManager(tmp_path, data_dir=tmp_path)

    class Boom:
        def pick_verified(self, tries, socks):
            raise RuntimeError("pool unreadable")

    monkeypatch.setattr(mgr, "_proxy_pool", lambda: Boom())
    assert mgr._proxy_for_claim("500m") == ""


# --------------------------------------------------------------------------- #
# countries: online lookup replaces the offline database download
# --------------------------------------------------------------------------- #

def test_countries_action_replaces_the_geo_database_download(tmp_path, monkeypatch):
    from zcode_gateway.generator import ProxyChecker
    assert "countries" in ProxyChecker.ACTIONS and "geo" not in ProxyChecker.ACTIONS
    ran = []
    with TestClient(_app(tmp_path)) as c:
        c.get("/admin/dash/generator/mail", headers=ADMIN)          # builds the manager
        mgr = c.app.state.app_state.generator
        monkeypatch.setattr(mgr.checker, "run", lambda action, **k: ran.append(action) or {"started": action})
        r = c.post("/admin/dash/generator/proxies/countries", headers=ADMIN, json={})
        assert r.status_code == 200 and ran == ["countries"]
        assert c.post("/admin/dash/generator/proxies/geo", headers=ADMIN, json={}).status_code in (404, 405)


def test_nothing_in_the_app_downloads_a_location_database():
    gen = ROOT.parent / "generator"
    for path in [*ROOT.glob("*.py"), *gen.glob("*.py"), ROOT / "dashboard" / "index.html"]:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        assert "db-ip.com" not in text and "dbip-" not in text, path.name
