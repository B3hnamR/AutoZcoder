import tempfile
from pathlib import Path

from zcode_gateway.accounts_dir_bridge import mint_and_register
from zcode_gateway.generator import GeneratorManager, ProxyChecker


def test_mint_and_register():
    with tempfile.TemporaryDirectory() as tmpdir:
        acc = mint_and_register(
            tmpdir,
            "test@example.com",
            "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiMTIzNCIsImV4cCI6MTc5MjAwMDAwMH0.xyz",
            device_mid="device-mid-123",
            plan="zcode-v3-start-plan-referral",
        )
        assert acc.id == "1234"
        assert acc.email == "test@example.com"
        assert acc.notes["plan"] == "zcode-v3-start-plan-referral"
        assert acc.notes["device_mid"] == "device-mid-123"

        # Verify file written
        files = list(Path(tmpdir).glob("*.txt"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "plan:             zcode-v3-start-plan-referral" in content
        assert "account_id:       1234" in content


def test_generator_manager_options():
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = GeneratorManager(tmpdir)
        mgr.set_options(
            captcha="manual",
            ip_source="pin",
            pin_proxy="socks5://1.2.3.4:1080",
            retries=3,
            claim=True,
            headless=False,
            invite_code="CUSTOM123",
        )
        opts = mgr.options()
        assert opts["captcha"] == "manual"
        assert opts["ip_source"] == "pin"
        assert opts["pin_proxy"] == "socks5://1.2.3.4:1080"
        assert opts["retries"] == 3
        assert opts["claim"] is True
        assert opts["headless"] is False
        assert opts["invite_code"] == "CUSTOM123"

        env = mgr._worker_env("g1")
        assert env["ZAI_OUT_SUFFIX"] == "g1"
        assert env["ZAI_MANUAL"] == "1"
        assert env["ZAI_HEADLESS"] == "0"
        assert env["ZAI_PROXY"] == "socks5://1.2.3.4:1080"
        assert env["ZAI_PROXY_RETRIES"] == "3"
        assert env["ZAI_INVITE_CODE"] == "CUSTOM123"


def test_proxy_checker_parsing():
    with tempfile.TemporaryDirectory() as tmpdir:
        checker = ProxyChecker(Path(tmpdir))

        class MockProc:
            stdout = [
                "verifying 500 proxies against chat.z.ai\n",
                "[checkpoint] 200/500 probed, 12 alive — saved best 12\n",
                "working proxies: 25 / 500 (<= 6.0s) -> proxies.txt\n",
                "latency: best 1.25s  median 3.40s\n",
                "ever-good: 45\n",
            ]
            returncode = 0

            def wait(self):
                pass

        checker.proc = MockProc()
        checker._pump()
        assert checker.stats.get("total") == 500
        assert checker.stats.get("probed") == 200
        assert checker.stats.get("alive") == 12
        assert checker.stats.get("pool") == 25
        assert checker.stats.get("scanned") == 500
        assert checker.stats.get("best") == 1.25
        assert checker.stats.get("median") == 3.40
        assert checker.stats.get("ever_good") == 45

def test_ansi_output_is_stripped():
    from zcode_gateway.generator import _clean_output, _tail

    esc = "\x1b"
    raw = (esc + "[31mX " + esc + "[41;97mERROR" + esc + "[0m "
           + esc + "[1mboom" + esc + "[0m")
    clean = _clean_output(raw)
    assert esc not in clean and "[31m" not in clean
    assert clean == "X ERROR boom"

    # when ESC was already consumed upstream, the parameter tail is removed too
    assert "[31m" not in _tail("[31mX [0m[1mhello")

    # genuine brackets survive
    assert "[MAIL]" in _clean_output("created namespace [MAIL] ok")


def test_inbox_verification_flag_persists():
    import tempfile
    from zcode_gateway.generator import GeneratorManager

    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = GeneratorManager(Path(tmpdir), data_dir=Path(tmpdir))
        assert mgr.mail_config()["inbox_ok"] is False

        mgr.mark_inbox_verified(True)
        assert mgr.mail_config()["inbox_ok"] is True

        # a fresh manager sees the persisted flag
        again = GeneratorManager(Path(tmpdir), data_dir=Path(tmpdir))
        assert again.mail_config()["inbox_ok"] is True

        # changing the endpoint invalidates verification
        again.set_mail_config(inbox_url="https://x.workers.dev/inbox")
        assert again.mail_config()["inbox_ok"] is False

def test_routing_confirmation_is_manual_and_per_domain():
    import tempfile
    from zcode_gateway.generator import GeneratorManager

    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = GeneratorManager(Path(tmpdir), data_dir=Path(tmpdir))
        mgr.set_mail_config(domains=["a.com", "b.com"])

        # not confirmed by default, and the inbox test must NOT tick it
        cfg = mgr.mail_config()
        assert cfg["routing_all_done"] is False
        assert set(cfg["routing_pending"]) == {"a.com", "b.com"}

        mgr.mark_inbox_verified(True)
        cfg = mgr.mail_config()
        assert cfg["inbox_ok"] is True
        assert cfg["routing_all_done"] is False      # worker reachable != routed

        # per-domain explicit confirmation
        mgr.set_routing_done("a.com", True)
        cfg = mgr.mail_config()
        assert cfg["routing_all_done"] is False
        assert cfg["routing_pending"] == ["b.com"]

        mgr.set_routing_done("b.com", True)
        assert mgr.mail_config()["routing_all_done"] is True

        # undo works, and adding a new domain reopens the step
        mgr.set_routing_done("a.com", False)
        assert mgr.mail_config()["routing_all_done"] is False
        mgr.set_mail_config(domains=["a.com", "b.com", "c.com"])
        cfg = mgr.mail_config()
        assert cfg["routing_all_done"] is False
        assert cfg["routing_pending"] == ["a.com", "c.com"]

def test_manual_captcha_forces_visible_browser():
    import tempfile
    from zcode_gateway.generator import GeneratorManager

    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = GeneratorManager(Path(tmpdir), data_dir=Path(tmpdir))
        # defaults: you solve it, in a visible window, claiming the referral
        opt = mgr.options()
        assert opt["captcha"] == "manual"
        assert opt["headless"] is False
        assert opt["claim"] is True
        assert opt["invite_code"] == "MT9GNDQ8"

        # asking for headless while manual must not stick
        mgr.set_options(headless=True)
        assert mgr.headless is False

        # auto captcha may run headless
        mgr.set_options(captcha="auto", headless=True)
        assert mgr.headless is True

        # switching back to manual forces it off again
        mgr.set_options(captcha="manual")
        assert mgr.headless is False

def test_pending_accounts_and_registration_ledger():
    import tempfile
    from zcode_gateway.generator import GeneratorManager

    with tempfile.TemporaryDirectory() as tmpdir:
        data = Path(tmpdir)
        (data / "accountsg1.txt").write_text(
            "a@x.me:pw:eyJabc.token\nbad-line\nb@x.me:pw:notajwt\n", encoding="utf-8")
        (data / "starterg1.txt").write_text(
            "a@x.me:pw:eyJabc.token\nc@x.me:pw:eyJdef.token\n", encoding="utf-8")

        mgr = GeneratorManager(Path(tmpdir), data_dir=data)
        # deduped across files, non-JWT tokens skipped, password with ':' kept
        assert sorted(p["email"] for p in mgr.pending_accounts()) == ["a@x.me", "c@x.me"]
        assert mgr.registered_emails() == set()

        mgr.mark_registered("a@x.me")
        assert mgr.registered_emails() == {"a@x.me"}
        assert [p["email"] for p in mgr.pending_accounts()] == ["c@x.me"]

        # ledger survives a restart (fresh manager, same data dir)
        assert GeneratorManager(Path(tmpdir), data_dir=data).registered_emails() == {"a@x.me"}


def test_zai_proxy_direct_sentinel_is_not_a_proxy():
    import importlib, os, sys
    gen = Path(__file__).resolve().parent.parent / "generator"
    if not gen.is_dir():
        return
    sys.path.insert(0, str(gen))
    try:
        import plan_check
        saved = os.environ.get("ZAI_PROXY")
        for sentinel in ("0", "", "none", "direct", "off"):
            os.environ["ZAI_PROXY"] = sentinel
            assert plan_check._proxies() is None, sentinel
        os.environ["ZAI_PROXY"] = "http://1.2.3.4:8080"
        assert plan_check._proxies() == {"http": "http://1.2.3.4:8080",
                                        "https": "http://1.2.3.4:8080"}
        if saved is None:
            os.environ.pop("ZAI_PROXY", None)
        else:
            os.environ["ZAI_PROXY"] = saved
    except ImportError:
        pass   # requests not installed in this env — skip

def test_invite_captcha_param_is_header_safe():
    import sys
    gen = Path(__file__).resolve().parent.parent / "generator"
    if not gen.is_dir():
        return
    sys.path.insert(0, str(gen))
    try:
        import invite_claim as ic
    except ImportError:
        return    # playwright not installed in this env — generator-side only
    # the old _captcha_param returned (param, region) and that tuple was used
    # directly as an HTTP header value -> InvalidHeader crash
    assert ic._norm_captcha_param(None) is None
    assert ic._norm_captcha_param("") is None
    assert ic._norm_captcha_param(("p", "region")) == "p"
    assert ic._norm_captcha_param(["q"]) == "q"
    assert ic._norm_captcha_param("abc") == "abc"
    assert isinstance(ic.CAPTCHA_HOOK, str) and "11xygtvd" in ic.CAPTCHA_HOOK
