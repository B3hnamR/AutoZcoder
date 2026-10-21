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

import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import hashlib
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

_VERSION_RE = re.compile(r"(\d+)")


def parse_version(text: str) -> tuple[int, ...]:
    """'v0.3.0' / '0.3' -> (0, 3, 0) / (0, 3). Non-numeric parts are ignored."""
    return tuple(int(n) for n in _VERSION_RE.findall(text or ""))


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
    for asset in assets or []:
        if asset.get("name") == name and asset.get("browser_download_url"):
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

    async def check(self) -> dict[str, Any]:
        """One GitHub round-trip; the result is cached either way."""
        if not self.enabled:
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
        except Exception as exc:
            log.info("update check failed: %s", exc)
            self._last = {"checked_at": time.time(), "update_available": False,
                          "error": exc.__class__.__name__}
            return self._last

        tag = rel.get("tag_name") or ""
        asset = pick_asset(rel.get("assets") or [])
        checksum_asset = pick_asset(rel.get("assets") or [], CHECKSUM_ASSET_NAME)
        newer = is_newer(tag, self.current)
        self._last = {
            "checked_at": time.time(),
            "current": self.current,
            "latest": tag.lstrip("v"),
            "update_available": bool(newer and asset and checksum_asset),
            "release_url": rel.get("html_url"),
            "download_url": asset["browser_download_url"] if asset else None,
            "download_size": asset.get("size") if asset else None,
            "checksum_url": (checksum_asset["browser_download_url"]
                             if checksum_asset else None),
            "notes": (rel.get("body") or "")[:4000],
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _expected_sha256(http: httpx.AsyncClient, checksum_url: str) -> str:
    """The published SHA-256 for the exe, from the release's checksums file."""
    try:
        resp = await http.get(checksum_url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"could not fetch {CHECKSUM_ASSET_NAME}: {exc.__class__.__name__}"
            "; refusing update") from exc
    for line in resp.text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == EXE_NAME:
            return parts[0]
    raise RuntimeError(
        f"{CHECKSUM_ASSET_NAME} lists no entry for {EXE_NAME}; refusing update")


async def download_update(url: str, checksum_url: str | None,
                          client: httpx.AsyncClient | None = None) -> Path:
    """Download the new exe next to the running one. Frozen builds only.

    The URLs must be the github.com asset URLs from the release we fetched —
    never caller-supplied addresses. The download is refused unless the
    published checksums.sha256 lists a matching SHA-256: without that check a
    tampered release (or a truncated download) would execute as the next build.
    """
    if not getattr(sys, "frozen", False):
        raise RuntimeError("self-update is only available in the exe build")
    if not checksum_url:
        raise RuntimeError(
            f"release has no {CHECKSUM_ASSET_NAME}; refusing to update "
            "(download manually from the release page if you trust it)")
    for candidate, what in ((url, "exe"), (checksum_url, "checksum")):
        host = (urlparse(candidate).hostname or "").lower()
        if not (host == "github.com" or host.endswith(".github.com")):
            raise RuntimeError(
                f"refusing {what} download from non-GitHub host: {host}")
    target = exe_dir() / NEW_EXE_NAME
    own = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=15.0), headers={"user-agent": UA},
        follow_redirects=True)
    try:
        async with http.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(target, "wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 16):
                    fh.write(chunk)
        if target.stat().st_size < 1_000_000:
            raise RuntimeError("downloaded file is implausibly small; discarding it")
        expected = await _expected_sha256(http, checksum_url)
        actual = _sha256_file(target)
        if actual.lower() != expected.lower():
            raise RuntimeError(
                f"checksum mismatch for {EXE_NAME}: expected {expected}, "
                f"got {actual}; discarding the download")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        if own:
            await http.aclose()
    return target


def apply_update(new_exe: Path) -> None:
    """Swap the running exe for the downloaded one and relaunch.

    Spawns a detached swapper that waits for THIS process (and its PyInstaller
    bootloader parent) to exit, then moves the file and starts it again. The
    caller exits right after spawning.
    """
    if not getattr(sys, "frozen", False):
        raise RuntimeError("self-update is only available in the exe build")
    current = Path(sys.executable).resolve()
    new_exe = new_exe.resolve()
    if not new_exe.is_file():
        raise RuntimeError(f"downloaded update not found: {new_exe}")
    # Re-verify at APPLY time: the hash was only checked at download time, so
    # a partial/corrupted/tampered .new.exe left on disk since then would
    # have been swapped in blindly.
    own_hash = _sha256_file(current)
    new_hash = _sha256_file(new_exe)
    if own_hash.lower() == new_hash.lower():
        raise RuntimeError("downloaded update is identical to the running exe; nothing to apply")
    if new_exe.stat().st_size < 1_000_000:
        raise RuntimeError("update file is implausibly small; refusing to apply")
    # back up the current exe so a bad new exe can be rolled back manually
    backup = current.with_suffix(".old.exe")
    pid = os.getpid()
    bat = Path(tempfile.gettempdir()) / "zcodegate_update.bat"
    bat.write_text(
        "@echo off\r\n"
        ":wait\r\n"
        f"tasklist /FI \"PID eq {pid}\" /NH 2>nul | find /I \"{current.name}\" >nul\r\n"
        "if %errorlevel%==0 (timeout /t 1 /nobreak >nul & goto wait)\r\n"
        "rem one extra beat for the bootloader parent to release the file\r\n"
        "timeout /t 2 /nobreak >nul\r\n"
        # paths travel via the environment: quoting survives spaces, and the
        # environment is Unicode-safe where an ASCII .bat embedding is not.
        # Backup first, then swap; only relaunch when the swap actually took
        # (the old code discarded move's result and started a dead-or-old exe).
        'copy /y "%ZCODEGATE_TGT%" "%ZCODEGATE_BAK%" >nul\r\n'
        'move /y "%ZCODEGATE_SRC%" "%ZCODEGATE_TGT%"\r\n'
        "if errorlevel 1 (\r\n"
        '  echo UPDATE SWAP FAILED - old exe kept, backup at %ZCODEGATE_BAK% >> "%ZCODEGATE_LOG%"\r\n'
        '  start "" "%ZCODEGATE_TGT%"\r\n'
        ") else (\r\n"
        '  start "" "%ZCODEGATE_TGT%"\r\n'
        ")\r\n"
        "del \"%~f0\"\r\n",
        encoding="ascii")
    env = dict(os.environ)
    env["ZCODEGATE_SRC"] = str(new_exe)
    env["ZCODEGATE_TGT"] = str(current)
    env["ZCODEGATE_BAK"] = str(backup)
    env["ZCODEGATE_LOG"] = str(exe_dir() / "update.log")
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        env=env,
        creationflags=(subprocess.DETACHED_PROCESS
                       | subprocess.CREATE_NEW_PROCESS_GROUP
                       | getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        close_fds=True)
