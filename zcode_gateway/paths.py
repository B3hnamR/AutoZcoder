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
    A source checkout uses ignored .local/runtime instead of the source root.
    """
    base = app_dir()
    if not getattr(sys, "frozen", False) and (base / ".git").is_dir():
        base = base / ".local" / "runtime"
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        probe = base / ".zcode-gateway-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return base
    except OSError:
        fallback = _per_user_dir()
        fallback.mkdir(mode=0o700, parents=True, exist_ok=True)
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


# ---------------------------------------------------------------------------
# First-run config bootstrap (tkinter-free on purpose)
#
# This lives here, not in gui.py, so `python -m zcode_gateway.main` on a
# headless box never touches tkinter (gui.py imports tkinter at module
# level; a minimal Linux VPS has no Tk and would die before creating the
# config). gui.py re-exports the constants for backwards compatibility.
# ---------------------------------------------------------------------------

# Written on first launch when no config.yaml is present. This is what lets
# a lone binary work with nothing alongside it. Keys are per-install random,
# never a shared hardcoded default.
DEFAULT_CONFIG_YAML = 'server:\n  host: 127.0.0.1\n  port: 8788\n  require_client_key: true\n  admin_key: gw-admin-local\n  client_keys:\n    - key: gw-local-1\n      name: local\nupstream:\n  base_url: https://zcode.z.ai/api/v1\n  chat_path: /zcode-plan/anthropic/v1/messages\n  timeout_connect: 15\n  timeout_read: 600\n  max_attempts: 3\npool:\n  strategy: drain_first\n  cooldown_seconds: 30\n  max_in_flight_per_account: 4\n  acquire_wait_seconds: 5\n  conversation_affinity_seconds: 600\n  balance_poll_seconds: 60\naccounts:\n  source: accounts_dir\n  providers_json: "~/.zcode/v2/credentials.json"\n  dir: ./accounts\n  pool_file: ./pool.json\nmodels:\n  aliases:\n    glm-5.3: GLM-5.3\n    glm-5.3-flash: GLM-5.3-Flash\n  default: GLM-5.3-Flash\n  default_anthropic: GLM-5.3\n  default_max_tokens: 128000\n  default_effort: high\n  pacing:\n    enabled: true\n    min_gap_ms: 150\n    max_gap_ms: 400\n  auto_free_fallback:\n    enabled: false\n    chain: [GLM-5.3, GLM-5.3-Flash]\nlogging:\n  level: INFO\n  capture: false\n  capture_dir: ./logs\nstore:\n  sqlite_path: ./gateway.db\nupdate:\n  enabled: false\n  repo: B3hnamR/AutoZcoder\ngenerator:\n  enabled: false\n  zcodeauto_dir: ""\n  python_exe: ""\n  captcha: "manual"\n  headless: false\n  claim: true\n  invite_code: "MT9GNDQ8"\n  captcha_tickets: 0\n  captcha_ticket_ttl_seconds: 75\n  socks_only: true\n  proxy_types: "socks"\nclaims:\n  daily_enabled: false\n  daily_time: "19:02"\n  daily_utc_offset_hours: 8\n  daily_mode: direct\n  campaign_utc_offset_hours: 8\n  campaign_gate: true\n'


def default_config_yaml() -> str:
    return DEFAULT_CONFIG_YAML.replace("  claim: true\n", "  claim: false\n  claim_start: true\n").replace(
        "gw-admin-local", f"gw-admin-{secrets.token_hex(8)}").replace(
        "gw-local-1", f"gw-client-{secrets.token_hex(8)}")


def _per_user_dir() -> Path:
    """Per-user data dir when the binary's folder is not writable
    (Program Files, corporate lockdowns) — Windows and POSIX."""
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", str(Path.home()))
        return Path(local) / "ZcodeGateway"
    xdg = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))
    return Path(xdg) / "ZcodeGateway"


def _write_initial_config(path: Path, template: str) -> None:
    # Set permissions at creation, not after secrets have been written. O_EXCL
    # also prevents concurrent first launches from overwriting one another.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not path.is_file():
            raise
        return
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(template)


def bootstrap_config() -> Path:
    """First-run config for a standalone, portable install.

    A lone downloaded binary must work with nothing alongside it: write a
    config.yaml next to the executable with per-install random keys (never
    a hardcoded default), portable relative paths (./accounts, ./logs,
    ./gateway.db — resolved against the config's folder by load_config),
    and create the accounts folder. If that folder is not writable, fall
    back to a per-user data dir so the tool still works. Works identically
    on Windows and Linux; imports no GUI modules.
    """
    base = base_dir()
    cfg = base / "config.yaml"
    if not cfg.is_file():
        from zcode_gateway.config import Fingerprint
        import yaml
        data = yaml.safe_load(default_config_yaml())
        data["upstream"]["fingerprint"] = Fingerprint().model_dump()
        template = yaml.safe_dump(data, sort_keys=False)
        try:
            _write_initial_config(cfg, template)
        except OSError:
            base = _per_user_dir()
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
            cfg = base / "config.yaml"
            if not cfg.is_file():
                _write_initial_config(cfg, template)
    accounts = cfg.parent / "accounts"
    try:
        accounts.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        pass
    # both the window and the server thread resolve the same file
    os.environ["ZCODE_GATEWAY_CONFIG"] = str(cfg)
    return cfg


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
