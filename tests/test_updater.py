"""Auto-updater: version compare, asset pick, check() shaping, route auth."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from zcode_gateway import updater as updater_mod
from zcode_gateway.updater import (
    UpdateChecker, is_newer, parse_version, pick_asset,
)

# the exe-swap machinery (bat file, taskkill dance) is Windows-only; apply_update
# refuses to run on other platforms by design
windows_only = pytest.mark.skipif(os.name != "nt",
                                  reason="self-update apply is Windows-only")


@pytest.fixture(autouse=True)
def windows_platform(monkeypatch):
    # Do not monkeypatch process-wide os.name: pathlib uses it too.
    monkeypatch.setattr(updater_mod, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))


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


@pytest.mark.parametrize("tag", ["v2.0.0-rc.1", "2.0.0+build.7", "release-2.0", None, 2])
def test_version_rejects_nonstable_tags(tag):
    assert parse_version(tag) == ()
    assert not is_newer(tag, "1.0")


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
              checksum_url=None):
    base = f"https://github.com/B3hnamR/ZcodeGate/releases/download/{tag}/"
    assets = ([{"name": "ZcodeGateway.exe",
                 "browser_download_url": base + "ZcodeGateway.exe",
                "size": 32_000_000}] if with_asset else [])
    if with_checksum:
        assets.append({"name": "checksums.sha256",
                        "browser_download_url": checksum_url or base + "checksums.sha256"})
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


@pytest.mark.parametrize("payload", [None, [], {"tag_name": None},
                                     {"tag_name": "v2.0", "assets": {}},
                                     {"tag_name": "v2.0-rc.1", "assets": []}])
async def test_malformed_check_preserves_successful_status(payload):
    checker = UpdateChecker("B3hnamR/ZcodeGate", "0.3.0", client=_StubClient(_release()))
    first = await checker.check()
    checker._client = _StubClient(payload)
    status = await checker.check()
    assert status["stale"] is True
    assert status["error"] in {"ValueError", "JSONDecodeError"}
    assert status["latest"] == first["latest"]
    assert status["checked_at"] == first["checked_at"]
    assert status["update_available"] is True


async def test_check_linux_selects_linux_assets(monkeypatch):
    monkeypatch.setattr(updater_mod.os, "name", "posix")
    release = _release()
    base = "https://github.com/B3hnamR/ZcodeGate/releases/download/v0.4.0/"
    release["assets"].extend([
        {"name": "ZcodeGateway", "browser_download_url": base + "ZcodeGateway"},
        {"name": "checksums-linux.sha256", "browser_download_url": base + "checksums-linux.sha256"},
    ])
    checker = UpdateChecker("B3hnamR/ZcodeGate", "0.3.0", client=_StubClient(release))
    status = await checker.check()
    assert status["update_available"] and status["release_available"]
    assert not status["self_update_available"]
    assert status["download_url"] == base + "ZcodeGateway"
    assert status["checksum_url"].endswith("checksums-linux.sha256")


async def test_check_newer_release_without_assets_still_notifies():
    checker = UpdateChecker("B3hnamR/ZcodeGate", "0.3.0",
                            client=_StubClient(_release(with_asset=False)))
    status = await checker.check()
    assert status["release_available"]
    assert not status["update_available"]


async def test_check_ignores_malformed_assets():
    release = _release()
    release["assets"].insert(0, None)
    checker = UpdateChecker("B3hnamR/ZcodeGate", "0.3.0", client=_StubClient(release))
    assert (await checker.check())["update_available"]


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


async def test_force_check_bypasses_disabled_polling_without_enabling_it():
    class CountingClient(_StubClient):
        calls = 0

        async def get(self, url):
            self.calls += 1
            return await super().get(url)

    client = CountingClient(_release())
    checker = UpdateChecker("B3hnamR/ZcodeGate", "0.3.0", enabled=False, client=client)
    assert (await checker.check())["disabled"]
    assert client.calls == 0
    manual = await checker.check(force=True)
    assert manual["update_available"]
    assert not manual.get("disabled")
    assert checker.enabled is False
    assert client.calls == 1
    assert (await checker.check())["disabled"]
    assert client.calls == 1


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
    rel = _release(tag="v9", checksum_url=CHECKSUM_URL)
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


@pytest.mark.parametrize("url", [
    EXE_URL.replace("https:", "http:"),
    EXE_URL.replace("github.com", "evil.github.com"),
    EXE_URL.replace("github.com", "github.com:443"),
    EXE_URL.replace("github.com", "user:password@github.com"),
    EXE_URL + "?redirect=1", EXE_URL + "#x",
    "https://github.com/x/ZcodeGateway.exe",
    EXE_URL.replace("/v9/", "/v8/"),
])
async def test_download_rejects_untrusted_or_mismatched_urls(frozen_download, url):
    with pytest.raises(RuntimeError):
        await updater_mod.download_update(url, CHECKSUM_URL)
    assert list(frozen_download.iterdir()) == []


@pytest.mark.parametrize("checksum", ["bad", "f" * 63, "g" * 64, "f" * 65])
async def test_download_rejects_invalid_hash_and_cleans_stale_stage(frozen_download, checksum):
    (frozen_download / updater_mod.NEW_EXE_NAME).write_bytes(b"stale")
    sidecar = frozen_download / (updater_mod.NEW_EXE_NAME + ".sha256")
    sidecar.write_text("stale", encoding="ascii")
    async with _download_client(checksum + "  ZcodeGateway.exe\n") as client:
        with pytest.raises(RuntimeError, match="invalid SHA-256"):
            await updater_mod.download_update(EXE_URL, CHECKSUM_URL, client=client)
    assert list(frozen_download.iterdir()) == []


async def test_download_size_limit_and_temp_cleanup(frozen_download, monkeypatch):
    monkeypatch.setattr(updater_mod, "MAX_DOWNLOAD_BYTES", 100)
    async with _download_client(f"{_sha256(EXE_BYTES)}  ZcodeGateway.exe\n") as client:
        with pytest.raises(RuntimeError, match="size limit"):
            await updater_mod.download_update(EXE_URL, CHECKSUM_URL, client=client)
    assert list(frozen_download.iterdir()) == []


async def test_download_windows_gate_precedes_io(frozen_download, monkeypatch):
    monkeypatch.setattr(updater_mod.os, "name", "posix")
    with pytest.raises(RuntimeError, match="Windows-only"):
        await updater_mod.download_update(EXE_URL, CHECKSUM_URL)
    assert list(frozen_download.iterdir()) == []


async def test_download_does_not_publish_partial_stage(frozen_download):
    class InspectStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            assert not (frozen_download / updater_mod.NEW_EXE_NAME).exists()
            yield EXE_BYTES[:500_000]
            assert not (frozen_download / updater_mod.NEW_EXE_NAME).exists()
            yield EXE_BYTES[500_000:]

    def handler(request):
        if request.url.path.endswith("checksums.sha256"):
            return httpx.Response(200, text=f"{_sha256(EXE_BYTES)}  ZcodeGateway.exe\n")
        return httpx.Response(200, stream=InspectStream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await updater_mod.download_update(EXE_URL, CHECKSUM_URL, client=client)
    assert (frozen_download / updater_mod.NEW_EXE_NAME).read_bytes() == EXE_BYTES


async def test_download_rejects_insecure_redirect(frozen_download):
    def handler(request):
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://cdn.invalid/checksums.sha256"})
        return httpx.Response(200, text=f"{_sha256(EXE_BYTES)}  ZcodeGateway.exe\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(RuntimeError, match="insecure checksum redirect"):
            await updater_mod.download_update(EXE_URL, CHECKSUM_URL, client=client)
    assert list(frozen_download.iterdir()) == []


# --------------------------------------------------------------------------- #
# swapper: user data lives in JSON, never interpolated into shell source
# --------------------------------------------------------------------------- #


@windows_only
def test_apply_update_preserves_launch_in_private_manifest(tmp_path, monkeypatch):
    new_exe = tmp_path / "ZcodeGateway.new.exe"
    new_exe.write_bytes(b"MZ" * 600_000)   # above apply's plausibility floor
    fake_target = tmp_path / "ZcodeGateway.exe"
    fake_target.write_bytes(b"MZ" * 600_001)  # different hash from new_exe
    # the download-time verified hash rides next to the .new.exe; apply
    # re-verifies against it before swapping
    (tmp_path / "ZcodeGateway.new.exe.sha256").write_text(
        _sha256(b"MZ" * 600_000), encoding="ascii")

    monkeypatch.setattr(updater_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater_mod.sys, "executable", str(fake_target))
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setattr(updater_mod, "_private_update_dir", lambda: private)
    monkeypatch.setattr(updater_mod.sys, "argv", [str(fake_target), "--headless", "--config", "old.yaml"])
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "old-extraction")
    captured: dict = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        return None

    monkeypatch.setattr(updater_mod.subprocess, "Popen", fake_popen)

    config = tmp_path / "config & % ' name.yaml"
    updater_mod.apply_update(new_exe, config_path=config)

    manifest = json.loads((private / "manifest.json").read_text(encoding="utf-8"))
    script = (private / "swap.ps1").read_text(encoding="utf-8-sig")
    assert script == updater_mod._SWAPPER_PS
    assert str(new_exe) not in script
    assert manifest["source"] == str(new_exe.resolve())
    assert manifest["target"] == str(fake_target.resolve())
    assert manifest["argument_line"] == updater_mod.subprocess.list2cmdline(
        ["--headless", "--config", str(config.resolve())])
    assert manifest["cwd"] == captured["cwd"] == str(Path.cwd())
    assert manifest["sha256"] == _sha256(b"MZ" * 600_000)
    assert manifest["parent_pid"] == os.getppid()
    assert captured["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert not any(key.startswith("_PYI_") for key in captured["env"])
    assert Path(captured["args"][0]).is_absolute()
    assert Path(captured["args"][0]).name == "powershell.exe"
    command = base64.b64decode(captured["args"][-1]).decode("utf-16le")
    assert command == "& '" + str(private / "swap.ps1").replace("'", "''") + "'"
    assert (tmp_path / "ZcodeGateway.new.exe.sha256").is_file()


@windows_only
def test_apply_refuses_a_tampered_download(tmp_path, monkeypatch):
    """A .new.exe modified on disk after download must never be swapped in."""
    new_exe = tmp_path / "ZcodeGateway.new.exe"
    payload = b"MZ" * 600_000
    new_exe.write_bytes(payload)
    fake_target = tmp_path / "ZcodeGateway.exe"
    fake_target.write_bytes(b"MZ" * 600_001)
    # sidecar records the ORIGINAL hash, then the file changes on disk
    (tmp_path / "ZcodeGateway.new.exe.sha256").write_text(
        _sha256(payload), encoding="ascii")
    new_exe.write_bytes(b"MZ" * 600_002)

    monkeypatch.setattr(updater_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater_mod.sys, "executable", str(fake_target))

    with pytest.raises(RuntimeError, match="no longer matches"):
        updater_mod.apply_update(new_exe)
    # the poisoned sidecar is removed so a retry re-downloads cleanly
    assert not (tmp_path / "ZcodeGateway.new.exe.sha256").exists()


@windows_only
def test_apply_refuses_without_a_recorded_checksum(tmp_path, monkeypatch):
    """No sidecar (sidecar deleted / manual download): refuse, never trust."""
    new_exe = tmp_path / "ZcodeGateway.new.exe"
    new_exe.write_bytes(b"MZ" * 600_000)
    fake_target = tmp_path / "ZcodeGateway.exe"
    fake_target.write_bytes(b"MZ" * 600_001)

    monkeypatch.setattr(updater_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater_mod.sys, "executable", str(fake_target))

    with pytest.raises(RuntimeError, match="no recorded checksum"):
        updater_mod.apply_update(new_exe)


@windows_only
def test_apply_spawn_failure_retains_verified_stage(tmp_path, monkeypatch):
    new_exe = tmp_path / updater_mod.NEW_EXE_NAME
    new_exe.write_bytes(EXE_BYTES)
    target = tmp_path / updater_mod.EXE_NAME
    target.write_bytes(EXE_BYTES + b"old")
    sidecar = tmp_path / (updater_mod.NEW_EXE_NAME + ".sha256")
    sidecar.write_text(_sha256(EXE_BYTES), encoding="ascii")
    private = tmp_path / "helper"
    private.mkdir()
    monkeypatch.setattr(updater_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater_mod.sys, "executable", str(target))
    monkeypatch.setattr(updater_mod, "_private_update_dir", lambda: private)

    def fail(*args, **kwargs):
        raise OSError("denied")

    monkeypatch.setattr(updater_mod.subprocess, "Popen", fail)
    with pytest.raises(RuntimeError, match="verified download retained"):
        updater_mod.apply_update(new_exe)
    assert sidecar.read_text(encoding="ascii") == _sha256(EXE_BYTES)
    assert new_exe.read_bytes() == EXE_BYTES
    assert not private.exists()


def test_swapper_contains_required_safety_guards():
    script = updater_mod._SWAPPER_PS
    assert "WaitForExit([int]$remaining)" in script
    assert "$p.MainModule.FileName -ine $m.target" in script
    assert "$m.started_before_ticks" in script
    assert "ComputeHash($stream)" in script
    assert "Get-StreamSHA256 $source" in script
    assert "Get-StreamSHA256 $backupStream" in script
    assert "Get-FileHash" not in script
    assert "if ($hash -ne $m.sha256)" in script
    assert "Retry-FileOperation" in script
    assert "[IO.File]::Replace($m.candidate, $m.target, [NullString]::Value)" in script
    assert "Backup checksum mismatch" in script
    assert "Replacement exited during startup" in script
    assert "ROLLBACK FAILED" in script
    assert "PYINSTALLER_RESET_ENVIRONMENT" in script
    assert "argument_line" in script and "WorkingDirectory = $m.cwd" in script
    assert "cmd.exe" not in script


@windows_only
def test_private_helper_directories_are_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(updater_mod.tempfile, "tempdir", str(tmp_path))
    first = updater_mod._private_update_dir()
    second = updater_mod._private_update_dir()
    assert first != second
    assert first.is_dir() and second.is_dir()


@windows_only
@pytest.mark.parametrize("early_failure", [False, True])
def test_powershell_swapper_local_fixture(tmp_path, early_failure):
    """Run only trusted generated fixture executables, never the installed app.

    The parent may run this in its isolated Windows scratch test environment.
    It compiles tiny .NET fixtures; no network, configuration, or real updates.
    """
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    root = tmp_path / "Unicode \u03a9 & % ' fixture"
    root.mkdir()
    helper = root / "helper"
    helper.mkdir()
    target = root / "app.exe"
    staged = root / "new.exe"
    marker = root / "launch.json"
    for output, label, exit_early in ((target, "old", False), (staged, "new", early_failure)):
        code = (
            'using System; using System.IO; using System.Threading; '
            'public class Fixture { public static void Main(string[] args) { '
            'File.WriteAllLines(args[0], new string[] { "' + label + '", '
            'Environment.CurrentDirectory, args[1], Environment.GetEnvironmentVariable("PYINSTALLER_RESET_ENVIRONMENT") }); '
            + ('Environment.Exit(13);' if exit_early else 'Thread.Sleep(30000);') + ' } }'
        )
        build = root / "compile.ps1"
        build.write_text("Add-Type -TypeDefinition '" + code.replace("'", "''")
                         + "' -OutputAssembly '" + str(output).replace("'", "''")
                         + "' -OutputType ConsoleApplication\n", encoding="utf-8-sig")
        subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-File", str(build)],
                       check=True, timeout=30, capture_output=True)
    sidecar = root / "new.exe.sha256"
    manifest = {
        "source": str(staged), "target": str(target), "sidecar": str(sidecar),
        "sha256": _sha256(staged.read_bytes()), "old_sha256": _sha256(target.read_bytes()),
        "backup": str(root / "backup.exe"), "candidate": str(root / "candidate.exe"),
        "pid": 2147483646, "parent_pid": 2147483645,
        "started_before_ticks": "3155378975999999999",
        "argument_line": subprocess.list2cmdline([str(marker), "Unicode \u03a9 & % ' arg"]),
        "cwd": str(root), "wait_seconds": 2, "retry_seconds": 2, "startup_seconds": 1,
    }
    (helper / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    sentinels = {name: b"unchanged dummy data" for name in
                 ("config.yaml", "accounts.txt", "gateway.db", "generator-data.json")}
    for name, content in sentinels.items():
        (root / name).write_bytes(content)
    script = helper / "swap.ps1"
    script.write_text("function Get-FileHash { throw 'Get-FileHash must not be required' }\n"
                      + updater_mod._SWAPPER_PS, encoding="utf-8-sig")
    try:
        completed = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-File", str(script)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        assert completed.returncode == (1 if early_failure else 0), (helper / "update.log").read_text(encoding="utf-8-sig")
        deadline = time.monotonic() + 3
        while (not marker.exists() or marker.read_text(encoding="utf-8").splitlines()[0] != ("old" if early_failure else "new")):
            if time.monotonic() > deadline:
                pytest.fail((helper / "update.log").read_text(encoding="utf-8-sig"))
            time.sleep(0.05)
        lines = marker.read_text(encoding="utf-8").splitlines()
        assert lines == ["old" if early_failure else "new", str(root), "Unicode \u03a9 & % ' arg", "1"]
        assert _sha256(target.read_bytes()) == manifest["old_sha256" if early_failure else "sha256"]
        assert _sha256((root / "backup.exe").read_bytes()) == manifest["old_sha256"]
        for name, content in sentinels.items():
            assert (root / name).read_bytes() == content
    finally:
        # Kill only generated executables under this fixture's unique directory.
        cleanup = "$p='" + str(target).replace("'", "''") + "'; Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $p } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", cleanup],
                       capture_output=True, timeout=15)
