"""Self-update: check GitHub Releases, download the new exe, swap it in.

The tool is a single-file exe, so updating means replacing the running binary.
Windows forbids overwriting a running exe, so `apply` writes a tiny swapper
script that waits for this process to exit, replaces the file, and relaunches.

Design notes:
- one HTTP call per check to the public GitHub API (no auth, 60/h/IP is plenty);
- the checker caches its result; the dashboard polls the cache, not GitHub;
- downloads only ever come from the release asset URL GitHub itself returned,
  pinned to the github.com host;
- everything except the status check is disabled in non-frozen (dev) runs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger("zcode_gateway.updater")

API = "https://api.github.com/repos/{repo}/releases/latest"
UA = "ZcodeGate-updater"           # GitHub rejects requests without a UA
EXE_NAME = "ZcodeGateway.exe"
NEW_EXE_NAME = "ZcodeGateway.new.exe"
CHECKSUM_ASSET_NAME = "checksums.sha256"
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

_VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)*)", re.ASCII)
_HASH_RE = re.compile(r"[0-9a-fA-F]{64}", re.ASCII)


def parse_version(text: str) -> tuple[int, ...]:
    """Parse stable dotted release tags; prerelease/metadata tags fail closed."""
    match = _VERSION_RE.fullmatch(text) if isinstance(text, str) and len(text) <= 128 else None
    return tuple(int(n) for n in match[1].split(".")) if match else ()


def is_newer(latest: str, current: str) -> bool:
    """True when `latest` is a strictly higher dotted version than `current`."""
    new, cur = parse_version(latest), parse_version(current)
    if not new or not cur:
        return False
    width = max(len(new), len(cur))
    new += (0,) * (width - len(new))
    cur += (0,) * (width - len(cur))
    return new > cur


def pick_asset(assets: list[dict], name: str = EXE_NAME) -> dict | None:
    """The release asset matching the exe name, else None."""
    for asset in assets if isinstance(assets, list) else []:
        if (isinstance(asset, dict) and asset.get("name") == name
                and isinstance(asset.get("browser_download_url"), str)
                and asset["browser_download_url"]):
            return asset
    return None


class UpdateChecker:
    """Background 'is there a newer release?' poller with a cached answer."""

    def __init__(self, repo: str, current_version: str, *,
                 enabled: bool = True, interval_hours: float = 6.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self.repo = repo
        self.current = current_version
        self.enabled = enabled
        self.interval_s = max(interval_hours, 0.1) * 3600
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0), headers={"user-agent": UA},
            follow_redirects=True)
        self._last: dict[str, Any] = {"checked_at": None, "update_available": False}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def check(self, *, force: bool = False) -> dict[str, Any]:
        """Check once; an explicit manual check may bypass disabled polling."""
        if not self.enabled and not force:
            self._last = {"checked_at": None, "update_available": False,
                          "disabled": True}
            return self._last
        try:
            resp = await self._client.get(API.format(repo=self.repo))
            if resp.status_code == 404:
                # repo or first release does not exist yet — not an error
                self._last = {"checked_at": time.time(), "update_available": False,
                              "note": "no releases yet"}
                return self._last
            resp.raise_for_status()
            rel = resp.json()
            if (not isinstance(rel, dict) or not isinstance(rel.get("tag_name"), str)
                    or not isinstance(rel.get("assets"), list)
                    or not parse_version(rel["tag_name"])):
                raise ValueError("malformed release metadata")
            windows = os.name == "nt"
            asset = pick_asset(rel["assets"], EXE_NAME if windows else "ZcodeGateway")
            checksum_asset = pick_asset(
                rel["assets"], CHECKSUM_ASSET_NAME if windows else "checksums-linux.sha256")
            if asset and checksum_asset:
                _release_urls(asset["browser_download_url"],
                              checksum_asset["browser_download_url"],
                              asset_name=EXE_NAME if windows else "ZcodeGateway",
                              checksum_name=CHECKSUM_ASSET_NAME if windows else "checksums-linux.sha256")
        except Exception as exc:
            log.info("update check failed: %s", exc)
            self._last = {**self._last, "attempted_at": time.time(),
                          "stale": True, "error": exc.__class__.__name__}
            return self._last

        tag = rel.get("tag_name") or ""
        newer = (not rel.get("draft") and not rel.get("prerelease")
                 and is_newer(tag, self.current))
        self._last = {
            "checked_at": time.time(),
            "current": self.current,
            "latest": tag.lstrip("v"),
            "update_available": bool(newer and asset and checksum_asset),
            "release_available": bool(newer),
            "self_update_available": bool(newer and asset and checksum_asset
                                          and windows and getattr(sys, "frozen", False)),
            "release_url": rel.get("html_url"),
            "download_url": asset["browser_download_url"] if asset else None,
            "download_size": asset.get("size") if asset else None,
            "checksum_url": (checksum_asset["browser_download_url"]
                             if checksum_asset else None),
            "notes": str(rel.get("body") or "")[:4000],
            "published_at": rel.get("published_at"),
        }
        if self._last["update_available"]:
            log.info("update available: %s -> %s", self.current, tag)
        return self._last

    @property
    def status(self) -> dict[str, Any]:
        return dict(self._last)


def exe_dir() -> Path:
    return Path(sys.executable).resolve().parent


def _startup_probe_spec(config_path: str | Path | None) -> dict[str, str]:
    from .config import load_config

    cfg = load_config(config_path)
    origin, certificate = cfg.server.child_connection()
    pin = ""
    if origin.startswith("https://") and certificate:
        pem = Path(certificate).read_text(encoding="ascii")
        leaf = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.DOTALL)
        if leaf is None:
            raise RuntimeError("listener certificate has no PEM certificate")
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(leaf[0])).hexdigest()
    return {"health_url": origin + "/health", "certificate_sha256": pin,
            "startup_nonce": uuid.uuid4().hex}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release_urls(url: str, checksum_url: str, *, asset_name: str = EXE_NAME,
                  checksum_name: str = CHECKSUM_ASSET_NAME) -> None:
    """Only accept two HTTPS assets from the same explicit GitHub release."""
    parsed = []
    for candidate, name in ((url, asset_name), (checksum_url, checksum_name)):
        try:
            part = urlparse(candidate)
            valid = (part.scheme == "https" and part.hostname == "github.com"
                     and part.port is None and part.username is None
                     and part.password is None and not part.query and not part.fragment
                     and re.fullmatch(r"/[^/]+/[^/]+/releases/download/[^/]+/" + re.escape(name),
                                      part.path))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise RuntimeError("refusing download: expected an HTTPS github.com release asset URL")
        parsed.append(part.path.rsplit("/", 1)[0])
    if parsed[0] != parsed[1]:
        raise RuntimeError("executable and checksum must belong to the same release")


async def _expected_sha256(http: httpx.AsyncClient, checksum_url: str) -> str:
    """The published SHA-256 for the exe, from the release's checksums file."""
    try:
        async with http.stream("GET", checksum_url) as resp:
            resp.raise_for_status()
            if any(r.url.scheme != "https" for r in [*resp.history, resp]):
                raise RuntimeError("refusing insecure checksum redirect")
            data = bytearray()
            async for chunk in resp.aiter_bytes(1 << 14):
                data.extend(chunk)
                if len(data) > 1 << 20:
                    raise RuntimeError("checksum file is too large; refusing update")
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"could not fetch {CHECKSUM_ASSET_NAME}: {exc.__class__.__name__}"
            "; refusing update") from exc
    matches = []
    for line in data.decode("utf-8-sig", errors="strict").splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == EXE_NAME:
            if not _HASH_RE.fullmatch(parts[0]):
                raise RuntimeError("invalid SHA-256 in checksum file; refusing update")
            matches.append(parts[0].lower())
    if len(matches) == 1:
        return matches[0]
    raise RuntimeError(
        f"{CHECKSUM_ASSET_NAME} lists no entry for {EXE_NAME}; refusing update")


async def download_update(url: str, checksum_url: str | None,
                          client: httpx.AsyncClient | None = None) -> Path:
    """Download the new exe next to the running one. Frozen builds only.

    The URLs must be the github.com asset URLs from the release we fetched —
    never caller-supplied addresses. The download is refused unless the
    published checksums.sha256 lists a matching SHA-256. This detects corrupt
    downloads, not compromise of the release publisher.
    """
    if not getattr(sys, "frozen", False):
        raise RuntimeError("self-update is only available in the exe build")
    if os.name != "nt":
        raise RuntimeError("self-update download is Windows-only; use the Linux installer")
    if not checksum_url:
        raise RuntimeError(
            f"release has no {CHECKSUM_ASSET_NAME}; refusing to update "
            "(download manually from the release page if you trust it)")
    _release_urls(url, checksum_url)
    target = exe_dir() / NEW_EXE_NAME
    sidecar = target.with_name(target.name + ".sha256")
    temporary = None
    own = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=15.0), headers={"user-agent": UA},
        follow_redirects=True)
    try:
        # Invalidate a previous stage before starting, never publish partial bytes.
        sidecar.unlink(missing_ok=True)
        expected = await _expected_sha256(http, checksum_url)
        fd, name = tempfile.mkstemp(prefix=".zcode-download-", dir=exe_dir())
        temporary = Path(name)
        async with http.stream("GET", url) as resp:
            resp.raise_for_status()
            if any(r.url.scheme != "https" for r in [*resp.history, resp]):
                raise RuntimeError("refusing insecure executable redirect")
            with os.fdopen(fd, "wb") as fh:
                fd = None
                size = 0
                async for chunk in resp.aiter_bytes(1 << 16):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError("download exceeds the size limit; discarding it")
                    fh.write(chunk)
        if temporary.stat().st_size < 1_000_000:
            raise RuntimeError("downloaded file is implausibly small; discarding it")
        actual = await asyncio.to_thread(_sha256_file, temporary)
        if actual.lower() != expected.lower():
            raise RuntimeError(
                f"checksum mismatch for {EXE_NAME}: expected {expected}, "
                f"got {actual}; discarding the download")
        # persist the verified hash so apply_update can re-verify even if the
        # file sits on disk for hours before the operator presses Apply
        try:
            os.replace(temporary, target)
            sidecar.write_text(actual.lower(), encoding="ascii")
        except OSError as exc:
            # apply_update refuses without it; fail now, not at Apply time
            raise RuntimeError(
                f"could not record the verified checksum ({exc}); "
                "the update folder is not writable") from exc
    except BaseException:
        sidecar.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
    finally:
        if "fd" in locals() and fd is not None:
            os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if own:
            await http.aclose()
    return target


_SWAPPER_PS = r'''$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$m = Get-Content -LiteralPath (Join-Path $root 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$log = Join-Path $root 'update.log'
$replaced = $false
$backedUp = $false
$exited = $false
$newProcess = $null
function Write-UpdateLog([string]$message) {
    Add-Content -LiteralPath $log -Value ((Get-Date -Format o) + ' ' + $message) -Encoding UTF8
}
function Retry-FileOperation([scriptblock]$operation) {
    $deadline = [DateTime]::UtcNow.AddSeconds($m.retry_seconds)
    while ($true) {
        try { & $operation; return } catch {
            if ([DateTime]::UtcNow -ge $deadline) { throw }
            Start-Sleep -Milliseconds 250
        }
    }
}
function Get-StreamSHA256([IO.Stream]$stream) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
function Start-Application([bool]$checkStartup = $false) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $m.target
    $info.Arguments = $m.argument_line
    $info.WorkingDirectory = $m.cwd
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    foreach ($name in @($info.EnvironmentVariables.Keys)) {
        if ($name.StartsWith('_PYI_')) { $info.EnvironmentVariables.Remove($name) }
    }
    $info.EnvironmentVariables['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    $info.EnvironmentVariables.Remove('ZCODE_GATEWAY_UPDATE_NONCE')
    if ($checkStartup) {
        $info.EnvironmentVariables['ZCODE_GATEWAY_UPDATE_NONCE'] = $m.startup_nonce
    }
    return [System.Diagnostics.Process]::Start($info)
}
function Wait-StartupHealth {
    # The callback is scoped to this request; never disable process-wide TLS.
    Add-Type -TypeDefinition @'
using System;
using System.Net;
using System.Security.Cryptography;
public static class ZcodeUpdateHealth {
    public static bool Matches(string url, string nonce, string pin) {
        HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url);
        request.Headers["X-AutoZCoder-Update"] = nonce;
        request.Proxy = null;
        request.AllowAutoRedirect = false;
        request.Timeout = 1500;
        request.ReadWriteTimeout = 1500;
        if (!String.IsNullOrEmpty(pin)) {
            request.ServerCertificateValidationCallback = (sender, cert, chain, errors) => {
                if (cert == null) return false;
                using (SHA256 sha = SHA256.Create()) {
                    string actual = BitConverter.ToString(sha.ComputeHash(cert.GetRawCertData())).Replace("-", "").ToLowerInvariant();
                    return String.Equals(actual, pin, StringComparison.Ordinal);
                }
            };
        }
        try {
            using (HttpWebResponse response = (HttpWebResponse)request.GetResponse()) {
                return response.StatusCode == HttpStatusCode.OK &&
                    String.Equals(response.Headers["X-AutoZCoder-Update"], nonce, StringComparison.Ordinal);
            }
        } catch (WebException) { return false; }
    }
}
'@
    $deadline = [DateTime]::UtcNow.AddSeconds($m.startup_seconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($newProcess.HasExited) {
            throw ('Replacement exited during startup, code ' + $newProcess.ExitCode)
        }
        if ([ZcodeUpdateHealth]::Matches($m.health_url, $m.startup_nonce, $m.certificate_sha256)) {
            if ($newProcess.HasExited) { throw 'Replacement exited after startup acknowledgement' }
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw 'Replacement did not acknowledge healthy startup before the deadline'
}
try {
    # Keep Process objects (and their creation times), not reusable naked PIDs.
    $processes = @()
    foreach ($number in @($m.pid, $m.parent_pid)) {
        try { $p = [System.Diagnostics.Process]::GetProcessById([int]$number) }
        catch [System.ArgumentException] { continue }
        try {
            if ($number -eq $m.parent_pid -and $p.MainModule.FileName -ine $m.target) { continue }
            if ($p.StartTime.ToUniversalTime().Ticks -gt [long]$m.started_before_ticks) { continue }
        } catch {
            if ($p.HasExited) { continue }
            throw
        }
        $processes += $p
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($m.wait_seconds)
    foreach ($p in $processes) {
        $remaining = [Math]::Max(0, ($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        if (-not $p.WaitForExit([int]$remaining)) { throw 'Application exit timed out; no files replaced' }
    }
    $exited = $true
    # Hold the verified source against writes/deletion through the replacement.
    $source = [IO.File]::Open($m.source, 'Open', 'Read', 'Read')
    try {
        $hash = Get-StreamSHA256 $source
        if ($hash -ne $m.sha256) { throw 'Staged checksum changed; no files replaced' }
        Retry-FileOperation { [IO.File]::Copy($m.target, $m.backup, $true) }
        $backupStream = [IO.File]::OpenRead($m.backup)
        try {
            if ((Get-StreamSHA256 $backupStream) -ine $m.old_sha256) {
                throw 'Backup checksum mismatch; no files replaced'
            }
        } finally { $backupStream.Dispose() }
        $backedUp = $true
        # Copy to a unique same-volume candidate, then atomically replace.
        Retry-FileOperation { [IO.File]::Copy($m.source, $m.candidate, $true) }
        Retry-FileOperation { [IO.File]::Replace($m.candidate, $m.target, [NullString]::Value) }
        $replaced = $true
    } finally { $source.Dispose() }
    $newProcess = Start-Application $true
    if ($null -eq $newProcess) { throw 'Replacement could not be started' }
    Wait-StartupHealth
    Write-UpdateLog 'Update healthy startup acknowledged; backup retained'
    Remove-Item -LiteralPath $m.source -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $m.sidecar -Force -ErrorAction SilentlyContinue
} catch {
    Write-UpdateLog ('Update failed: ' + $_.Exception.Message)
    if ($replaced -and $backedUp) {
        try {
            if ($null -ne $newProcess -and -not $newProcess.HasExited) {
                # Kill only the replacement tree we own, never the user's launcher.
                & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /PID $newProcess.Id /T /F 2>&1 | Out-Null
                $null = $newProcess.WaitForExit(10000)
            }
            Retry-FileOperation { [IO.File]::Copy($m.backup, $m.candidate, $true) }
            Retry-FileOperation { [IO.File]::Replace($m.candidate, $m.target, [NullString]::Value) }
            $null = Start-Application
            Write-UpdateLog 'Previous executable restored and restarted'
        } catch { Write-UpdateLog ('ROLLBACK FAILED: ' + $_.Exception.Message) }
    } elseif ($exited) {
        try { $null = Start-Application } catch { Write-UpdateLog 'Old executable could not be restarted' }
    }
    exit 1
} finally {
    Remove-Item -LiteralPath $m.candidate -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $root 'manifest.json') -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
'''


def _private_update_dir() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="zcode-update-"))
    try:
        # mkdtemp's mode is not an ACL on Windows. Protect inherited helper and
        # manifest permissions before writing paths/arguments into this folder.
        import ctypes
        from ctypes import wintypes
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        descriptor = ctypes.c_void_p()
        convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        convert.restype = wintypes.BOOL
        set_security = advapi.SetFileSecurityW
        set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        set_security.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        if not convert("D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)", 1,
                       ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not set_security(str(directory), 0x80000004, descriptor):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.LocalFree(descriptor)
        return directory
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def apply_update(new_exe: Path, *, config_path: str | Path | None = None) -> None:
    """Swap the running exe for the downloaded one and relaunch.

    Spawns a detached swapper that waits for THIS process (and its PyInstaller
    same-executable bootloader parent) to exit, then replaces the file and
    starts it again. The caller must exit after spawning. A per-launch nonce
    health response is required before accepting the replacement.
    """
    if not getattr(sys, "frozen", False):
        raise RuntimeError("self-update is only available in the exe build")
    if os.name != "nt":
        raise RuntimeError("self-update is Windows-only in this build; on "
                           "Linux, replace the binary with the release artifact")
    current = Path(sys.executable).resolve()
    new_exe = new_exe.resolve()
    if not new_exe.is_file():
        raise RuntimeError(f"downloaded update not found: {new_exe}")
    # Re-verify at APPLY time against the hash that was checked when the file
    # was downloaded — a partial/corrupted/tampered .new.exe left on disk since
    # then must not be swapped in blindly.
    expected_path = exe_dir() / (NEW_EXE_NAME + ".sha256")
    if not expected_path.is_file():
        raise RuntimeError("no recorded checksum for the downloaded update; "
                           "download again before applying")
    expected_hash = expected_path.read_text(encoding="ascii").strip()
    if not _HASH_RE.fullmatch(expected_hash):
        raise RuntimeError("invalid recorded checksum; download again before applying")
    own_hash = _sha256_file(current)
    new_hash = _sha256_file(new_exe)
    if new_hash.lower() != expected_hash.lower():
        expected_path.unlink(missing_ok=True)
        raise RuntimeError("downloaded update no longer matches the checksum "
                           "verified at download time (modified on disk?); "
                           "refusing to apply — download again")
    if own_hash.lower() == new_hash.lower():
        raise RuntimeError("downloaded update is identical to the running exe; nothing to apply")
    if new_exe.stat().st_size < 1_000_000:
        raise RuntimeError("update file is implausibly small; refusing to apply")
    arguments = list(sys.argv[1:])
    active_config = config_path or os.environ.get("ZCODE_GATEWAY_CONFIG")
    startup_probe = _startup_probe_spec(active_config)
    if active_config:
        filtered = []
        iterator = iter(arguments)
        for arg in iterator:
            if arg == "--config":
                next(iterator, None)
            elif not arg.startswith("--config="):
                filtered.append(arg)
        arguments = [*filtered, "--config", str(Path(active_config).resolve())]
    directory = None
    try:
        directory = _private_update_dir()
        token = uuid.uuid4().hex
        manifest = {
            "source": str(new_exe), "target": str(current),
            "sidecar": str(expected_path), "sha256": new_hash.lower(),
            "old_sha256": own_hash.lower(),
            "backup": str(current.with_name(current.stem + "." + token + ".old.exe")),
            "candidate": str(current.with_name(".zcode-swap-" + token + ".exe")),
            "pid": os.getpid(), "parent_pid": os.getppid(),
            "started_before_ticks": str(int(time.time() * 10_000_000) + 621355968000000000),
            "argument_line": subprocess.list2cmdline(arguments), "cwd": str(Path.cwd()),
            "wait_seconds": 120, "retry_seconds": 30, "startup_seconds": 60,
            **startup_probe,
        }
        (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=True), encoding="utf-8")
        script = directory / "swap.ps1"
        script.write_text(_SWAPPER_PS, encoding="utf-8-sig")
        # EncodedCommand transports just the fixed helper path, never user args.
        command = "& '" + str(script).replace("'", "''") + "'"
        encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("_PYI_")}
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            env=env, cwd=str(Path.cwd()), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                           | getattr(subprocess, "CREATE_NO_WINDOW", 0)), close_fds=True)
        log.info("update helper started; diagnostics: %s", directory / "update.log")
    except (OSError, ValueError) as exc:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise RuntimeError("could not start update helper; verified download retained") from exc
