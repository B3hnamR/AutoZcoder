"""Load account snapshots from an `accounts/` folder.

Each file is the plain-text snapshot written by `tools/capture_account.py`:

    account_id:       usr-...
    email:            someone@example.com
    access_token:     workos:eyJ...
    refresh_token:    ...
    expires_at_ms:    1789576656000

Human-readable on purpose; also trivially machine-readable. This module turns a
folder of them into pool `Account` objects, so a library of accounts can live
independently of whatever Zcode is currently logged into.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from .pool import Account

log = logging.getLogger("zcode_gateway.accounts_dir")

LINE = re.compile(r"^([a-z_]+):\s*(.*)$")


def safe_name(email: str, fallback: str) -> str:
    """Filesystem-safe snapshot filename base from an email (or fallback)."""
    label = email or fallback or "account"
    label = re.sub(r"[^A-Za-z0-9._@-]+", "_", label).strip("._")
    return f"{label}.txt"


def snapshot_filename(email: str, account_id: str,
                      used: dict[str, str]) -> str:
    """Collision-safe snapshot filename.

    `used` maps an already-taken filename -> the account id that owns it. Two
    provider slots sharing one email used to overwrite each other's snapshot;
    on a collision the account id becomes part of the name instead.
    """
    name = safe_name(email, account_id)
    owner = used.get(name)
    if owner is not None and owner != account_id:
        stem = name[:-len(".txt")]
        suffix = (account_id or "x").replace("/", "_")[:16]
        name = f"{stem}.{suffix}.txt"
        n = 2
        while name in used and used[name] != account_id:
            name = f"{stem}.{suffix}.{n}.txt"
            n += 1
    return name


def parse_snapshot(path: Path) -> dict[str, str]:
    """Parse one snapshot file into a flat key -> value dict."""
    fields: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return fields

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LINE.match(line)
        if match:
            fields[match.group(1)] = match.group(2).strip()
    return fields


def _to_account(fields: dict[str, str], path: Path) -> Account | None:
    token = fields.get("access_token", "")
    if not token:
        return None

    ident = (fields.get("account_id")
             or fields.get("zcode_user_id")
             or path.stem)

    raw_expiry = fields.get("expires_at_ms")
    if raw_expiry:
        try:
            expires_at = int(raw_expiry)
        except ValueError:
            # a present-but-malformed expiry means the snapshot is corrupt;
            # loading it as 0 would make the account permanently "needs refresh"
            return None
    else:
        expires_at = 0        # unknown: the refresh loop will establish it

    account = Account(
        id=ident,
        email=fields.get("email", ""),
        access_token=token,
        refresh_token=fields.get("refresh_token", ""),
        expires_at=expires_at,
        source=f"accounts/{path.name}",
    )
    # extra context the CLI tool recorded, useful in the GUI
    account.notes = {  # type: ignore[attr-defined]
        "captured_at": fields.get("captured_at", ""),
        "name": fields.get("name", ""),
        "balance_usd": fields.get("balance_usd", ""),
        "plan": fields.get("plan", ""),
        "workos_user_id": fields.get("workos_user_id", ""),
        "file": path.name,
        "device_mid": fields.get("device_mid", ""),
    }
    return account


def load_from_accounts_dir(directory: str | Path) -> list[Account]:
    """Every valid snapshot in the folder, newest-expiry first.

    A malformed file must not take the other snapshots down with it: one bad
    `expires_at_ms` used to raise out of here and the gateway started with an
    empty pool.
    """
    folder = Path(directory)
    if not folder.is_dir():
        return []

    accounts: dict[str, Account] = {}
    for path in sorted(folder.glob("*.txt")):
        try:
            fields = parse_snapshot(path)
            account = _to_account(fields, path)
        except Exception:
            log.warning("skipping unreadable account snapshot %s", path,
                        exc_info=True)
            continue
        if account is None:
            continue
        # de-dupe by account id, keeping the newer token
        prev = accounts.get(account.id)
        if prev is None or account.expires_at > prev.expires_at:
            accounts[account.id] = account

    return sorted(accounts.values(), key=lambda a: -a.expires_at)


def write_snapshot(path: Path, fields: dict[str, str]) -> None:
    """Write a snapshot in the same format (used by import/export helpers).

    Atomic: a temp file in the same directory + os.replace, so a crash or
    full disk mid-write cannot truncate an existing credential file.
    """
    order = [
        "captured_at", "source", "provider_slot", "app_config_active",
        "", "account_id", "zcode_user_id", "workos_user_id", "session_id",
        "email", "name", "device_mid",
        "", "token_prefix", "expires_at", "expires_at_ms", "token_lifetime_s",
        "issuer", "client_id",
        "", "balance_micro", "balance_usd", "plan",
        "", "access_token", "refresh_token",
    ]
    lines = ["# Zcode account snapshot"]
    for key in order:
        if key == "":
            lines.append("")
            continue
        if key in fields:
            lines.append(f"{key + ':':<18}{fields[key]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
