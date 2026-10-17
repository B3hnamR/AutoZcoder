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
