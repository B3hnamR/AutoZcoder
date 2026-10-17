"""Auto-updater: version compare, asset pick, check() shaping, route auth."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from zcode_gateway import updater as updater_mod
from zcode_gateway.updater import (
    UpdateChecker, is_newer, parse_version, pick_asset,
)


def test_parse_version_common_forms():
    assert parse_version("v0.3.0") == (0, 3, 0)
    assert parse_version("0.10.2") == (0, 10, 2)
    assert parse_version("1.3.0.0") == (1, 3, 0, 0)
    assert parse_version("") == ()


def test_is_newer():
    assert is_newer("v0.3.1", "0.3.0")
    assert is_newer("1.0.0", "0.9.9")
    assert not is_newer("v0.3.0", "0.3.0")     # equal is not newer
    assert not is_newer("v0.2.9", "0.3.0")
    assert not is_newer("garbage", "0.3.0")    # unparseable tag
    assert not is_newer("v0.3.0", "")          # unparseable current


def test_pick_asset():
    assets = [
        {"name": "checksums.txt", "browser_download_url": "https://github.com/x/s"},
        {"name": "ZcodeGateway.exe",
         "browser_download_url": "https://github.com/x/ZcodeGateway.exe",
         "size": 32_000_000},
    ]
    assert pick_asset(assets)["name"] == "ZcodeGateway.exe"
    assert pick_asset(assets[:1]) is None
    assert pick_asset([]) is None


def test_pick_asset_by_name():
    assets = [
        {"name": "ZcodeGateway.exe", "browser_download_url": "https://github.com/x/e"},
        {"name": "checksums.sha256",
         "browser_download_url": "https://github.com/x/checksums.sha256"},
    ]
    assert pick_asset(assets, "checksums.sha256")["name"] == "checksums.sha256"
    assert pick_asset(assets, "missing.txt") is None


class _StubClient:
    """Stands in for httpx.AsyncClient; returns a canned GitHub response."""

    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    async def get(self, url):
        # raise_for_status() needs a bound request on the response
        return httpx.Response(self._status, json=self._payload,
                              request=httpx.Request("GET", url))

    async def aclose(self):
        return None


def _release(tag="v0.4.0", with_asset=True, with_checksum=True,
             checksum_url="https://github.com/a/checksums.sha256"):
    assets = ([{"name": "ZcodeGateway.exe",
                "browser_download_url": "https://github.com/a/ZcodeGateway.exe",
                "size": 32_000_000}] if with_asset else [])
    if with_checksum:
        assets.append({"name": "checksums.sha256",
                       "browser_download_url": checksum_url})
    return {
        "tag_name": tag,
        "html_url": "https://github.com/B3hnamR/ZcodeGate/releases/tag/" + tag,
        "body": "what changed",
        "published_at": "2026-09-22T00:00:00Z",
        "assets": assets,
    }


async def test_check_reports_newer_release():
    checker = UpdateChecker("B3hnamR/ZcodeGate", "0.3.0",
                            client=_StubClient(_release()))
    status = await checker.check()
    assert status["update_available"] is True
    assert status["latest"] == "0.4.0"
    assert status["current"] == "0.3.0"
    assert status["download_url"].startswith("https://github.com/")
    assert status["notes"] == "what changed"


async def test_check_no_update_when_current():
    checker = UpdateChecker("B3hnamR/ZcodeGate", "0.4.0",
                            client=_StubClient(_release()))
    assert (await checker.check())["update_available"] is False


async def test_check_no_update_without_exe_asset():
    checker = UpdateChecker("B3hnamR/ZcodeGate", "0.3.0",
                            client=_StubClient(_release(with_asset=False)))
    assert (await checker.check())["update_available"] is False


async def test_check_no_automatic_update_without_checksum_asset():
    checker = UpdateChecker("B3hnamR/ZcodeGate", "0.3.0",
                            client=_StubClient(_release(with_checksum=False)))
    status = await checker.check()
    assert status["update_available"] is False
    assert status["checksum_url"] is None


async def test_check_404_first_release_is_not_an_error():
    checker = UpdateChecker("B3hnamR/ZcodeGate", "0.3.0",
                            client=_StubClient({}, status=404))
    status = await checker.check()
    assert status["update_available"] is False
    assert "error" not in status


async def test_check_failure_is_cached_not_raised():
    class _Boom:
        async def get(self, url):
            raise httpx.ConnectError("nope")

        async def aclose(self):
            return None

    checker = UpdateChecker("B3hnamR/ZcodeGate", "0.3.0", client=_Boom())
    status = await checker.check()
    assert status["update_available"] is False
    assert status["error"] == "ConnectError"


async def test_disabled_checker_never_calls_network():
    class _Explode:
        async def get(self, url):
            raise AssertionError("must not be called")

        async def aclose(self):
            return None

    checker = UpdateChecker("B3hnamR/ZcodeGate", "0.3.0", enabled=False,
                            client=_Explode())
    status = await checker.check()
    assert status["update_available"] is False
    assert status["disabled"] is True


def test_routes_registered_and_auth_gated():
    from fastapi.testclient import TestClient

    from zcode_gateway.app import create_app
    from zcode_gateway.config import load_config

    cfg = load_config()
    cfg.update.enabled = False
    cfg.pool.balance_poll_seconds = 0   # hermetic: no live balance sweeps
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/admin/dash/update/status").status_code in (401, 403)
        assert client.post("/admin/dash/update/check").status_code in (401, 403)
        assert client.post("/admin/dash/update/download").status_code in (401, 403)
        assert client.post("/admin/dash/update/apply").status_code in (401, 403)
        ok = {"Authorization": f"Bearer {cfg.server.admin_key}"}
        status = client.get("/admin/dash/update/status", headers=ok)
        assert status.status_code == 200
        assert status.json()["update_available"] is False
        # downloads/applies are impossible without an available update
        assert client.post("/admin/dash/update/apply", headers=ok).status_code \
            == 400


# --------------------------------------------------------------------------- #
# checksum verification: the published checksums.sha256 gates the swap
# --------------------------------------------------------------------------- #

EXE_BYTES = b"MZ" + b"x" * 1_000_001        # > 1 MB: passes the size sanity check
EXE_URL = "https://github.com/B3hnamR/ZcodeGate/releases/download/v9/ZcodeGateway.exe"
CHECKSUM_URL = ("https://github.com/B3hnamR/ZcodeGate/releases/download/v9/"
                "checksums.sha256")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _download_client(checksum_text: str | None, exe_bytes: bytes = EXE_BYTES):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("checksums.sha256"):
            if checksum_text is None:
                return httpx.Response(404, request=request)
            return httpx.Response(200, text=checksum_text, request=request)
        return httpx.Response(200, content=exe_bytes, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def frozen_download(tmp_path, monkeypatch):
    """Pretend to be a frozen exe whose folder is tmp_path."""
    monkeypatch.setattr(updater_mod, "exe_dir", lambda: tmp_path)
    monkeypatch.setattr(updater_mod.sys, "frozen", True, raising=False)
    return tmp_path


async def test_check_exposes_the_checksum_asset_url():
    rel = _release(checksum_url=CHECKSUM_URL)
    checker = UpdateChecker("B3hnamR/ZcodeGate", "0.3.0",
                            client=_StubClient(rel))
    status = await checker.check()
    assert status["update_available"] is True
    assert status["checksum_url"] == CHECKSUM_URL


async def test_download_verifies_and_installs(frozen_download):
    checksum = f"{_sha256(EXE_BYTES)}  ZcodeGateway.exe\n"
    client = _download_client(checksum)
    try:
        path = await updater_mod.download_update(EXE_URL, CHECKSUM_URL,
                                                 client=client)
    finally:
        await client.aclose()
    assert path == frozen_download / "ZcodeGateway.new.exe"
    assert path.read_bytes() == EXE_BYTES


async def test_download_rejects_a_tampered_file(frozen_download):
    checksum = f"{'0' * 64}  ZcodeGateway.exe\n"        # wrong hash
    client = _download_client(checksum)
    try:
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            await updater_mod.download_update(EXE_URL, CHECKSUM_URL,
                                              client=client)
    finally:
        await client.aclose()
    assert not (frozen_download / "ZcodeGateway.new.exe").exists()


async def test_download_refuses_without_a_checksum_asset(frozen_download):
    client = _download_client(None)
    try:
        with pytest.raises(RuntimeError, match="checksums.sha256"):
            await updater_mod.download_update(EXE_URL, CHECKSUM_URL,
                                              client=client)
    finally:
        await client.aclose()
    assert not (frozen_download / "ZcodeGateway.new.exe").exists()


async def test_download_refuses_when_no_checksum_url_was_published(frozen_download):
    with pytest.raises(RuntimeError, match="checksums.sha256"):
        await updater_mod.download_update(EXE_URL, None)


async def test_download_rejects_a_checksum_for_a_different_file(frozen_download):
    checksum = f"{_sha256(EXE_BYTES)}  something-else.exe\n"
    client = _download_client(checksum)
    try:
        with pytest.raises(RuntimeError, match="no entry for ZcodeGateway.exe"):
            await updater_mod.download_update(EXE_URL, CHECKSUM_URL,
                                              client=client)
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# swapper: paths travel via the environment, quoted (spaces / non-ASCII safe)
# --------------------------------------------------------------------------- #


def test_apply_update_quotes_paths_via_environment(tmp_path, monkeypatch):
    new_exe = tmp_path / "ZcodeGateway.new.exe"
    new_exe.write_bytes(b"MZ")
    fake_target = tmp_path / "ZcodeGateway.exe"

    monkeypatch.setattr(updater_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater_mod.sys, "executable", str(fake_target))
    monkeypatch.setattr(updater_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    captured: dict = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return None

    monkeypatch.setattr(updater_mod.subprocess, "Popen", fake_popen)

    updater_mod.apply_update(new_exe)

    bat = (tmp_path / "zcodegate_update.bat").read_text(encoding="ascii")
    assert 'move /y "%ZCODEGATE_SRC%" "%ZCODEGATE_TGT%"' in bat
    assert 'start "" "%ZCODEGATE_TGT%"' in bat
    assert str(new_exe) not in bat              # paths never embedded in the .bat
    assert captured["env"]["ZCODEGATE_SRC"] == str(new_exe.resolve())
    assert captured["env"]["ZCODEGATE_TGT"] == str(fake_target.resolve())
