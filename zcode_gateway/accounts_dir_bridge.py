"""Bridge between the ZCODEAUTO generator and the gateway's accounts_dir.

`mint_and_register` converts a freshly generated account (chat token) into a
pool Account: zcodeJWT via the device flow, snapshot written in the exact
ZcodeGate format, Account object ready for pool.upsert.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("zcode_gateway.accounts_dir_bridge")

# mirrors accounts_dir.write_snapshot field order
_ORDER = [
    "captured_at", "source", "provider_slot", "app_config_active",
    "", "account_id", "zcode_user_id", "workos_user_id", "session_id",
    "email", "name", "device_mid",
    "", "token_prefix", "expires_at", "expires_at_ms", "token_lifetime_s",
    "issuer", "client_id",
    "", "balance_micro", "balance_usd", "plan",
    "", "access_token", "refresh_token",
]


def _jwt_claims(token: str) -> dict:
    try:
        p = token.split(".")[1]
        p += "=" * ((4 - len(p) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def _snapshot_name(email: str, account_id: str, used: set[str]) -> str:
    label = email or account_id or "account"
    safe = "".join(c if c.isalnum() or c in "._@-" else "_" for c in label)
    name = f"{safe}.txt"
    n = 1
    while name in used:
        n += 1
        name = f"{safe} ({n}).txt"
    return name


def _write_snapshot(path: Path, fields: dict) -> None:
    lines = ["# Zcode account snapshot"]
    for key in _ORDER:
        if key == "":
            lines.append("")
            continue
        if key in fields:
            lines.append(f"{key + ':':<18}{fields[key]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os_replace(tmp, path)


def os_replace(a: str, b: str):
    import os
    os.replace(a, b)


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

    d = Path(accounts_dir)
    d.mkdir(parents=True, exist_ok=True)
    used = {p.name for p in d.glob("*.txt")}
    name = _snapshot_name(email, account_id, used)
    fields = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": "ZCODEAUTO generator (device flow)",
        "account_id": account_id,
        "email": email,
        "device_mid": device_mid,
        "plan": plan,
        "access_token": zcode_jwt,
    }
    if claims.get("exp"):
        fields["expires_at"] = str(claims["exp"])
        fields["expires_at_ms"] = str(expires_ms)
    else:
        fields["expires_at_ms"] = "0"
    fields = {k: v for k, v in fields.items() if v != ""}
    _write_snapshot(d / name, fields)

    return Account(
        id=account_id,
        email=email,
        access_token=zcode_jwt,
        expires_at=expires_ms,
        source="generator",
        notes={"file": name, "plan": plan, "device_mid": device_mid},
    )
