"""Bridge between the ZCODEAUTO generator and the gateway's accounts_dir.

`mint_and_register` converts a freshly generated account (chat token) into a
pool Account: zcodeJWT via the device flow, snapshot written in the exact
ZcodeGate format, Account object ready for pool.upsert.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from .accounts_dir import (
    parse_snapshot,
    snapshot_filename,
    snapshot_path,
    write_snapshot,
)

log = logging.getLogger("zcode_gateway.accounts_dir_bridge")

_DIRECTORY_LOCKS: dict[str, threading.Lock] = {}
_DIRECTORY_LOCKS_GUARD = threading.Lock()


def _directory_lock(directory: Path):
    key = os.path.normcase(str(directory.resolve()))
    with _DIRECTORY_LOCKS_GUARD:
        return _DIRECTORY_LOCKS.setdefault(key, threading.Lock())


def _jwt_claims(token: str) -> dict:
    try:
        p = token.split(".")[1]
        p += "=" * ((4 - len(p) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except (AttributeError, IndexError, TypeError, ValueError):
        return {}


def _snapshot_name(email: str, account_id: str, used: set[str]) -> str:
    return snapshot_filename(email, account_id, {name: "\0" for name in used})


def _write_snapshot(path: Path, fields: dict) -> None:
    for value in fields.values():
        if "\r" in str(value) or "\n" in str(value):
            raise ValueError("snapshot values must not contain newlines")
    write_snapshot(path, fields)


def mint_and_register(accounts_dir: str | Path, email: str, zcode_jwt: str,
                      device_mid: str = "", plan: str = ""):
    """Write the snapshot and return the pool Account for upsert.

    Imports Account lazily so this module stays import-safe outside the gateway.
    """
    try:
        from .pool import Account
    except (ImportError, ValueError):
        from zcode_gateway.pool import Account

    claims = _jwt_claims(zcode_jwt)
    account_id = claims.get("user_id") or claims.get("sub") or email
    expires_ms = int(claims.get("exp") or 0) * 1000
    for value in (account_id, email, device_mid, plan, zcode_jwt):
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise ValueError("snapshot values must be single-line strings")

    d = Path(accounts_dir).resolve()
    # Only local scan/allocation/write belongs under this process-local lock.
    with _directory_lock(d):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        used: dict[str, str] = {}
        name = ""
        fields: dict[str, str] = {}
        for path in sorted(d.iterdir()):
            if path.suffix.casefold() != ".txt":
                continue
            existing = (parse_snapshot(path)
                        if path.is_file() and not path.is_symlink() else {})
            owner = existing.get("account_id", "")
            used[path.name] = owner
            if owner == account_id and not name:
                name, fields = path.name, existing
        if not name:
            name = snapshot_filename(email, account_id, used)
        if fields.get("source") == "Manual JWT import":
            fields["imported_jwt"] = "true"
        fields.update({
            "captured_at": datetime.now(UTC).isoformat(),
            "source": "ZCODEAUTO generator (device flow)",
            "account_id": account_id,
            "access_token": zcode_jwt,
            "expires_at_ms": str(expires_ms),
        })
        fields.update({k: v for k, v in {
            "email": email, "device_mid": device_mid, "plan": plan,
        }.items() if v != ""})
        if claims.get("exp"):
            fields["expires_at"] = str(claims["exp"])
        else:
            fields.pop("expires_at", None)
        _write_snapshot(snapshot_path(d, name), fields)

    return Account(
        id=account_id,
        email=fields.get("email", ""),
        access_token=zcode_jwt,
        refresh_token=fields.get("refresh_token", ""),
        expires_at=expires_ms,
        source="generator",
        notes={
            **{key: fields.get(key, "") for key in (
                "captured_at", "name", "balance_usd", "plan", "workos_user_id", "device_mid",
            )},
            "file": name,
            "imported_jwt": fields.get("imported_jwt") == "true",
        },
    )
