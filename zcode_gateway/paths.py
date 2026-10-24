"""Tkinter-free path helpers shared by gui.py and the API layer.

gui.py imports tkinter at module level, which breaks headless/packaged
imports of its helper functions (api_dash used to ast-exec them out of the
gui.py source — impossible in a PyInstaller exe where gui.py exists only in
the compiled PYZ, not as a file on disk). The helpers live here instead and
gui.py re-exports them.
"""
from __future__ import annotations

import os
import secrets
import shutil
import sys
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def base_dir() -> Path:
    """Where config/accounts/logs live.

    Portable by default: the exe's folder. If that folder is not writable
    (Program Files, some corporate lockdowns), fall back to
    %LOCALAPPDATA%/ZcodeGateway so a read-only install still works.
    """
    base = app_dir()
    try:
        probe = base / ".zcode-gateway-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return base
    except OSError:
        local = os.environ.get("LOCALAPPDATA", str(Path.home()))
        fallback = Path(local) / "ZcodeGateway"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def find_accounts_dir() -> Path:
    """Locate the accounts/ folder.

    Preference order: a folder that exists AND holds snapshots (so the repo's
    ../accounts wins over an empty ./accounts), then any existing folder, then
    <base>/accounts for a fresh install.
    """
    base = base_dir()
    here = app_dir()
    candidates = [base / "accounts", here / "accounts",
                  *(p / "accounts" for p in list(here.parents)[:3])]

    populated = next((c for c in candidates
                      if c.is_dir() and any(c.glob("*.txt"))), None)
    if populated:
        return populated
    existing = next((c for c in candidates if c.is_dir()), None)
    return existing or base / "accounts"


# Test/overridable hooks; when None the path is resolved lazily at call time.
# (Constants resolved at import time bake in the importing user's profile —
# wrong when the exe later runs elevated / as another session.)
ZCODE_PROVIDERS: Path | None = None
ZCODE_GLOBAL: Path | None = None


def _zcode_global() -> Path:
    if ZCODE_GLOBAL is not None:
        return ZCODE_GLOBAL
    return Path(os.path.expanduser("~/.zcode/data/globalState.json"))


def _zcode_providers() -> Path:
    if ZCODE_PROVIDERS is not None:
        return ZCODE_PROVIDERS
    return Path(os.path.expanduser("~/.zcode/v2/credentials.json"))
