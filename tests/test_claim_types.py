"""Regressions for the claim-type (100M referral / 500M giveaway) round."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
from zcode_gateway.config import Config, GeneratorConfig
from zcode_gateway.generator import GenWorker, GeneratorManager, claim_result_landed

GEN = Path(__file__).resolve().parent.parent / "generator"
ADMIN = {"Authorization": "Bearer adm-test"}


def test_generator_config_explicit_claim_beats_claim500_default():
    fresh = GeneratorConfig()
    assert fresh.claim_start is True and fresh.claim is False
    assert fresh.claim500 is False       # legacy giveaway is off by default

    # a config.yaml written before claim500 existed keeps its meaning
    assert GeneratorConfig(claim=True).claim500 is False
    assert GeneratorConfig(claim=False).claim500 is False
    # ...unless claim500 is spelled out too
    assert GeneratorConfig(claim=False, claim500=True).claim500 is True


def test_saved_options_from_before_claim500_stay_100m_or_off(tmp_path):
    for n, saved_claim in enumerate((True, False)):
        data = tmp_path / str(n)
        data.mkdir()
        (data / "generator_options.json").write_text(
            json.dumps({"claim": saved_claim}), encoding="utf-8")
        mgr = GeneratorManager(data, data_dir=data)
        assert mgr.claim is saved_claim
        assert mgr.claim500 is False     # the new default must not switch on


def _generator_app(root: Path, claim=None, saved=None):
    gen = root / "gen"
    (gen / "output").mkdir(parents=True)
    (gen / "main.py").write_text("", encoding="utf-8")
    (gen / "config.py").write_text("", encoding="utf-8")
    if saved is not None:
        (gen / "output" / "generator_options.json").write_text(
            json.dumps(saved), encoding="utf-8")
    g = {"enabled": True, "zcodeauto_dir": str(gen)}
    if claim is not None:
        g["claim"] = claim
    app = create_app(Config(
        server={"host": "127.0.0.1", "port": 8788, "admin_key": "adm-test",
                "client_keys": [{"key": "cli-test", "name": "local"}]},
        accounts={"source": "accounts_dir", "dir": str(root / "accounts")},
        store={"sqlite_path": str(root / "gw.db")},
        logging={"capture": False}, generator=g))
    return app, gen


def test_saved_claim_choice_survives_gateway_restart(tmp_path):
    # the dashboard saved "100M"; config.yaml still carries the 500M default
    app, gen = _generator_app(tmp_path, saved={"claim": True, "claim500": False})
    with TestClient(app) as c:
        assert c.get("/admin/dash/generator/mail", headers=ADMIN).status_code == 200
        mgr = app.state.app_state.generator
        assert (mgr.claim, mgr.claim500) == (True, False)
    persisted = json.loads((gen / "output" / "generator_options.json").read_text())
    assert (persisted["claim"], persisted["claim500"]) == (True, False)


def test_first_run_seeds_claim_type_from_config(tmp_path):
    app, _ = _generator_app(tmp_path / "default")
    with TestClient(app) as c:
        c.get("/admin/dash/generator/mail", headers=ADMIN)
        mgr = app.state.app_state.generator
        # shipped default: Start Plan claim on, both legacy campaigns off
        assert (mgr.claim, mgr.claim500, mgr.claim_start) == (False, False, True)

    app, _ = _generator_app(tmp_path / "legacy", claim=True)    # old config.yaml
    with TestClient(app) as c:
        c.get("/admin/dash/generator/mail", headers=ADMIN)
        mgr = app.state.app_state.generator
        assert (mgr.claim, mgr.claim500) == (True, False)


def test_giveaway500_account_without_starter_reaches_the_pool(tmp_path):
    (tmp_path / "results").mkdir()
    w = GenWorker("w1", ["x"], {"ZAI_OUTPUT_DIR": str(tmp_path)}, tmp_path)
    (tmp_path / "results" / "accounts.txt").write_text(
        "n@x.me:pw:eyJabc.token\n", encoding="utf-8")

    # no Starter and no 500M claim -> still skipped (nothing worth pooling)
    w.starter = False
    assert w.check_new_account() is None

    # the 500M giveaway landed -> poolable even without Starter
    w.giveaway500 = True
    assert w.check_new_account()["email"] == "n@x.me"


def test_giveaway500_worker_log_line_sets_the_flag(tmp_path):
    import io
    import types

    w = GenWorker("w1", ["x"], {"ZAI_OUTPUT_DIR": str(tmp_path)}, tmp_path)
    w.proc = types.SimpleNamespace(
        stdout=io.StringIO("[INFO] 500M giveaway claimed for n@x.me\n"),
        wait=lambda: None, returncode=0)
    w._pump()
    assert w.giveaway500 is True


def test_pending_accounts_scan_includes_giveaway500_file(tmp_path):
    (tmp_path / "accounts500m.txt").write_text(
        "g@x.me:pw:eyJabc.token\n", encoding="utf-8")
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "accounts.txt").write_text(
        "failed@x.me:pw:eyJdef.token\n", encoding="utf-8")      # not scanned
    mgr = GeneratorManager(tmp_path, data_dir=tmp_path)
    assert [p["email"] for p in mgr.pending_accounts()] == ["g@x.me"]


def test_claim_result_landed_reads_both_claim_script_shapes():
    assert claim_result_landed({"claimed": True})                      # claim500.py
    assert claim_result_landed({"summary": {"issued": True}})          # claim100.py
    assert not claim_result_landed({"summary": {"issued": False}})
    assert not claim_result_landed({"claimed": False, "summary": None})
    assert not claim_result_landed(None)
    assert not claim_result_landed({"raw": "garbled"})


def _gen_module(name):
    if not GEN.is_dir():
        return None
    if str(GEN) not in sys.path:
        sys.path.insert(0, str(GEN))
    try:
        return __import__(name)
    except ImportError:      # requests/certifi missing in this env
        return None


def test_claim500_trusts_balance_not_the_already_message(monkeypatch):
    c500 = _gen_module("claim500")
    if c500 is None:
        return

    plan = "zcode-v3-start-plan-1024"
    active = {"plans": [{"plan_id": plan, "status": "active"}], "balances": []}
    empty = {"plans": [], "balances": []}
    monkeypatch.setattr(c500, "device_login", lambda *a, **k: ("zjwt", True))
    monkeypatch.setattr(c500, "discover_campaign", lambda *a, **k: ("", ""))
    monkeypatch.setattr(c500, "marketing_touch", lambda *a, **k: False)
    monkeypatch.setattr(c500.time, "sleep", lambda s: None)
    quiet = lambda m: None

    # "already" message but the plan is NOT on the account -> not claimed
    monkeypatch.setattr(c500, "billing_claim",
                        lambda *a, **k: {"code": 1003, "msg": "already exists"})
    monkeypatch.setattr(c500, "billing_balance", lambda *a, **k: empty)
    res = c500.claim500("tok", plan, log=quiet)
    assert res["claimed"] is False and res["error"]

    # same message, and the plan really is there (landed on an earlier try)
    balances = iter([empty, active])               # pre-check, then post-check
    monkeypatch.setattr(c500, "billing_balance", lambda *a, **k: next(balances))
    res = c500.claim500("tok", plan, log=quiet)
    assert res["claimed"] is True and res["already_claimed"] is True

    # fresh claim: claimed, and not reported as "already"
    monkeypatch.setattr(c500, "billing_claim",
                        lambda *a, **k: {"code": 0, "msg": "ok"})
    balances = iter([empty, active])
    monkeypatch.setattr(c500, "billing_balance", lambda *a, **k: next(balances))
    res = c500.claim500("tok", plan, log=quiet)
    assert res["claimed"] is True and res["already_claimed"] is False


def test_ca_path_check_accepts_directories_and_rejects_junk(tmp_path):
    ic = _gen_module("invite_claim")
    if ic is None:
        return

    assert ic._ca_path_ok("")
    assert ic._ca_path_ok(str(tmp_path))                  # a CA directory is valid
    assert not ic._ca_path_ok(str(tmp_path / "missing.pem"))
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate", encoding="utf-8")
    assert not ic._ca_path_ok(str(junk))


def _fake_jwt(user_id):
    import base64

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'HS256'})}.{b64({'user_id': user_id, 'exp': 1893456000})}.sig"


def test_new_generator_account_gets_its_quota_refreshed_immediately(tmp_path, monkeypatch):
    """The account joined the pool but its row stayed empty until the next
    60s balance poll (or a manual refresh): registration now checks it."""
    import time
    import types

    from zcode_gateway import api_dash

    zjwt = _fake_jwt("acct-new")
    monkeypatch.setitem(sys.modules, "plan_check", types.SimpleNamespace(
        get_zcode_jwt=lambda chat_token, log=None: zjwt,
        get_plan=lambda z: {"plans": ["zcode-v3-start-plan-1024"]}))
    monkeypatch.setattr(api_dash, "_referral_needs_finish", lambda z: False)

    app, _ = _generator_app(tmp_path)
    with TestClient(app) as c:
        c.get("/admin/dash/generator/mail", headers=ADMIN)       # builds the manager
        state = app.state.app_state
        checked = []

        async def fake_check(account):
            checked.append(account.id)

        monkeypatch.setattr(state.tokens, "check_account", fake_check)
        state.generator.on_account({"email": "new@x.me", "chat_token": "eyJabc.def.ghi"})

        deadline = time.time() + 5
        while not checked and time.time() < deadline:
            time.sleep(0.05)
        assert checked == ["acct-new"]                            # refreshed right away
        assert "new@x.me" in state.generator.registered_emails()  # and still registered


# --- Start Plan round (signup grant removed) -------------------------------

def test_genworker_start_plan_is_poolable_without_a_signup_starter(tmp_path):
    (tmp_path / "results").mkdir()
    w = GenWorker("w1", ["x"], {"ZAI_OUTPUT_DIR": str(tmp_path)}, tmp_path)
    (tmp_path / "results" / "accounts.txt").write_text(
        "s@x.me:pw:eyJabc.token\n", encoding="utf-8")

    w.starter = False
    assert w.check_new_account() is None       # nothing worth pooling yet

    w.start_plan = True                        # billing preview claim landed
    assert w.check_new_account()["email"] == "s@x.me"


def test_genworker_parses_the_start_plan_log_line(tmp_path):
    (tmp_path / "results").mkdir()
    w = GenWorker("w1", ["x"], {"ZAI_OUTPUT_DIR": str(tmp_path)}, tmp_path)
    # the pump only needs a proc-like iterator
    class _Proc:
        stdout = iter(["Start Plan claimed for s@x.me (zcode-v3-start-plan-referral)\n"])
        returncode = 0
        def wait(self):
            return 0
        def poll(self):
            return 0
    w.proc = _Proc()
    w._pump()
    assert w.start_plan is True
    assert w.to_public()["start_plan"] is True


def test_claim_job_defaults_to_the_start_plan_script(tmp_path):
    from zcode_gateway.generator import ClaimJob
    job = ClaimJob("j1", "a@x.me", "direct", "", tmp_path, "py", "", data_dir=tmp_path)
    assert job.kind == "start"
    assert job._build_argv()[2] == "claim_start.py"


def test_pending_accounts_reads_startplan_ledgers(tmp_path):
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "startplan.txt").write_text(
        "p@x.me:pw:eyJabc.token\n", encoding="utf-8")
    mgr = GeneratorManager(tmp_path, data_dir=tmp_path)
    emails = [a["email"] for a in mgr.pending_accounts()]
    assert "p@x.me" in emails
