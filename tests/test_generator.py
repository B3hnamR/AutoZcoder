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

def test_invite_captcha_chain_is_in_page():
    """The external Go captcha service is gone; params are minted in the
    live page and the hook also captures the verify region."""
    import sys
    gen = Path(__file__).resolve().parent.parent / "generator"
    if not gen.is_dir():
        return
    sys.path.insert(0, str(gen))
    try:
        import invite_claim as ic
    except ImportError:
        return    # playwright not installed in this env — generator-side only
    # no external-service fetcher, no embedded default credential
    assert not hasattr(ic, '_captcha_param')
    src = Path(ic.__file__).read_text(encoding='utf-8')
    assert 'admin' not in src.split('_deleted_captcha_service_stub')[0].split('def ')[-1]
    assert '8687' not in src
    # the hook captures the region alongside the verdict
    assert isinstance(ic.CAPTCHA_HOOK, str) and "11xygtvd" in ic.CAPTCHA_HOOK
    assert '__captchaRegion' in ic.CAPTCHA_HOOK
    # fresh-mint helper exists and clears stale params before waiting
    assert hasattr(ic, '_page_captcha_param')


def test_invite_captcha_param_is_header_safe():
    import sys
    gen = Path(__file__).resolve().parent.parent / "generator"
    if not gen.is_dir():
        return
    sys.path.insert(0, str(gen))
    try:
        import invite_claim as ic
    except ImportError:
        return
    # legacy tuple input is still tolerated by the in-page sender path
    assert isinstance(ic.CAPTCHA_HOOK, str) and "11xygtvd" in ic.CAPTCHA_HOOK


def test_proxy_pool_respects_output_dir():
    import tempfile, os, sys
    gen = Path(__file__).resolve().parent.parent / "generator"
    if not gen.is_dir():
        return
    sys.path.insert(0, str(gen))
    import proxy_pool
    with tempfile.TemporaryDirectory() as tmpdir:
        proxy_pool.set_output_dir(tmpdir)
        assert proxy_pool.OUTPUT_DIR == tmpdir
        assert proxy_pool.GOOD_FILE == os.path.join(tmpdir, "proxies.txt")
        assert proxy_pool.EVERGOOD_FILE == os.path.join(tmpdir, "proxies_evergood.txt")
        assert proxy_pool.RAW_FILE == os.path.join(tmpdir, "proxies_raw.txt")
        assert proxy_pool.LOCAL_SOURCES == os.path.join(tmpdir, "proxy_sources.txt")


def test_generator_manager_proxy_migration_and_socks_count():
    import tempfile
    from zcode_gateway.generator import GeneratorManager

    with tempfile.TemporaryDirectory() as gen_tmp, tempfile.TemporaryDirectory() as data_tmp:
        gen_out = Path(gen_tmp) / "output"
        gen_out.mkdir()
        (gen_out / "proxies.txt").write_text(
            "http://1.1.1.1:80\nsocks5://2.2.2.2:1080\nsocks4://3.3.3.3:1080\n",
            encoding="utf-8"
        )
        (gen_out / "proxies_evergood.txt").write_text(
            "socks5://4.4.4.4:1080\n",
            encoding="utf-8"
        )
        data_path = Path(data_tmp)
        mgr = GeneratorManager(Path(gen_tmp), data_dir=data_path)

        # verified files were copied over into data_dir
        assert (data_path / "proxies.txt").is_file()
        assert (data_path / "proxies_evergood.txt").is_file()

        # socks count reflects only live proxies in proxies.txt (2 unique live socks proxies)
        assert mgr._socks_count() == 2
        assert mgr.options()["socks_available"] == 2


def test_proxy_types_option_flow():
    import tempfile
    from zcode_gateway.generator import GeneratorManager

    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = GeneratorManager(Path(tmpdir), data_dir=Path(tmpdir))

        # default matches the old socks-only behavior
        assert mgr.options()["proxy_types"] == "socks"
        assert mgr.options()["ip_source"] == "direct"

        mgr.set_options(ip_source="pool", proxy_types="socks5")
        env = mgr._worker_env("g1")
        assert env["ZAI_USE_POOL"] == "1"
        assert env["ZAI_PROXY_TYPES"] == "socks5"
        assert "ZAI_SOCKS_ONLY" not in env or env["ZAI_SOCKS_ONLY"] != "1"

        mgr.set_options(proxy_types="http")
        assert mgr._worker_env("g2")["ZAI_PROXY_TYPES"] == "http,https"

        mgr.set_options(proxy_types="all")
        assert "ZAI_PROXY_TYPES" not in mgr._worker_env("g3")

        # csv of raw schemes survives normalization
        mgr.set_options(proxy_types="Socks5, HTTP")
        assert mgr.options()["proxy_types"] == "socks5,http"

        # invalid values fall back to all
        mgr.set_options(proxy_types="bogus")
        assert mgr.options()["proxy_types"] == "all"

        # legacy 'socks' ip_source is a pool alias
        mgr.set_options(ip_source="socks")
        assert mgr.ip_source == "pool"


def test_proxy_types_legacy_config_mapping():
    import tempfile
    from zcode_gateway.generator import GeneratorManager

    with tempfile.TemporaryDirectory() as tmpdir:
        # old socks_only: false meant "use the full pool"
        assert GeneratorManager.normalize_proxy_types(
            "socks", legacy_socks_only=False) == "all"
        assert GeneratorManager.normalize_proxy_types(
            "socks", legacy_socks_only=True) == "socks"


def test_pool_type_counts_and_options():
    import tempfile
    from zcode_gateway.generator import GeneratorManager

    with tempfile.TemporaryDirectory() as tmpdir:
        data = Path(tmpdir)
        (data / "proxies.txt").write_text(
            "http://1.1.1.1:80\nhttp://1.1.1.2:80\n"
            "socks5://2.2.2.2:1080\nsocks4://3.3.3.3:1080\n",
            encoding="utf-8")
        (data / "proxies_used.txt").write_text(
            "socks5://2.2.2.2:1080\t2026-10-17T00:00:00Z\ttest\n", encoding="utf-8")
        mgr = GeneratorManager(Path(tmpdir), data_dir=data)

        tc = mgr._types_counts()
        assert tc["all"] == 3          # used socks5 excluded
        assert tc["socks"] == 1
        assert tc["socks5"] == 0
        assert tc["socks4"] == 1
        assert tc["http"] == 2

        # pool_available follows the selected type filter
        mgr.set_options(proxy_types="http")
        assert mgr.options()["pool_available"] == 2
        mgr.set_options(proxy_types="socks5")
        assert mgr.options()["pool_available"] == 0
        mgr.set_options(proxy_types="all")
        assert mgr.options()["pool_available"] == 3


def test_proxy_pool_filter_types():
    import sys
    gen = Path(__file__).resolve().parent.parent / "generator"
    if not gen.is_dir():
        return
    sys.path.insert(0, str(gen))
    import proxy_pool
    pool = ["http://1.1.1.1:80", "socks5://2.2.2.2:1080", "socks4://3.3.3.3:1080"]
    assert proxy_pool.filter_types(pool, None) == pool
    assert proxy_pool.filter_types(pool, ("socks5",)) == ["socks5://2.2.2.2:1080"]
    assert proxy_pool.filter_types(pool, ("socks4", "socks5")) == pool[1:]
    assert proxy_pool.filter_types(pool, ("http", "https")) == [pool[0]]
    assert proxy_pool.socks_only(pool) == pool[1:]


def test_proxy_pool_used_ledger_and_claim():
    import tempfile, os, sys
    gen = Path(__file__).resolve().parent.parent / "generator"
    if not gen.is_dir():
        return
    sys.path.insert(0, str(gen))
    import proxy_pool
    with tempfile.TemporaryDirectory() as tmpdir:
        proxy_pool.set_output_dir(tmpdir)
        (Path(tmpdir) / "proxies.txt").write_text("socks5://1.2.3.4:1080\n", encoding="utf-8")
        assert proxy_pool.load_used() == set()
        
        # claim succeeds
        assert proxy_pool.claim_used("socks5://1.2.3.4:1080") is True
        assert "socks5://1.2.3.4:1080" in proxy_pool.load_used()
        
        # already claimed: second claim fails
        assert proxy_pool.claim_used("socks5://1.2.3.4:1080") is False
        
        # evicted from proxies.txt
        good = proxy_pool.load_good()
        assert "socks5://1.2.3.4:1080" not in good
