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

import hashlib
import base64
import json
import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .pool import Account

log = logging.getLogger("zcode_gateway.accounts_dir")

LINE = re.compile(r"^([a-z_]+):\s*(.*)$")
_DIRECTORY_LOCKS: dict[str, threading.RLock] = {}
_DIRECTORY_LOCKS_GUARD = threading.Lock()


@contextmanager
def snapshot_transaction(directory: str | Path):
    """Serialize a complete sync scan/read/allocate/merge/write transaction.

    Run the entire context in one worker thread; never hold it across an await.
    """
    folder = Path(directory).resolve()
    key = os.path.normcase(str(folder))
    with _DIRECTORY_LOCKS_GUARD:
        lock = _DIRECTORY_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield folder


def snapshot_account_id(fields: dict[str, str]) -> str:
    """Return a validated snapshot owner, not a filename-derived identity."""
    ident = fields.get("account_id") or fields.get("zcode_user_id") or ""
    if not isinstance(ident, str) or not ident.strip() or "\r" in ident or "\n" in ident:
        raise ValueError("snapshot account identity must be a nonempty single-line string")
    return ident


def safe_name(email: str, fallback: str) -> str:
    """Filesystem-safe snapshot filename base from an email (or fallback)."""
    label = email or fallback or "account"
    label = re.sub(r"[^A-Za-z0-9._@-]+", "_", label).strip("._")[:160] or "account"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]",
                    label.split(".")[0], re.IGNORECASE):
        label = "account_" + label
    return f"{label}.txt"


def snapshot_filename(email: str, account_id: str,
                      used: dict[str, str]) -> str:
    """Collision-safe snapshot filename.

    `used` maps an already-taken filename -> the account id that owns it. Two
    provider slots sharing one email used to overwrite each other's snapshot;
    on a collision the account id becomes part of the name instead.
    """
    name = safe_name(email, account_id)
    owners = {key.casefold(): value for key, value in used.items()}
    if name.casefold() in owners and owners[name.casefold()] != account_id:
        stem = name[:-len(".txt")]
        suffix = hashlib.sha256((account_id or "x").encode("utf-8")).hexdigest()[:16]
        name = f"{stem}.{suffix}.txt"
        n = 2
        while name.casefold() in owners and owners[name.casefold()] != account_id:
            name = f"{stem}.{suffix}.{n}.txt"
            n += 1
    return name


def snapshot_path(directory: str | Path, name: str) -> Path:
    """Validate an untrusted filename before joining it to the snapshot root."""
    if (not name or name in (".", "..") or re.search(r'[\\/:\x00-\x1f]', name)
            or name.rstrip(" .") != name or Path(name).name != name):
        raise ValueError("snapshot filename must be a basename")
    folder = Path(directory)
    path = folder / name
    if path.resolve().parent != folder.resolve() or path.is_symlink():
        raise ValueError("snapshot path escapes the accounts directory")
    return path


def parse_snapshot(path: Path) -> dict[str, str]:
    """Parse one snapshot file into a flat key -> value dict."""
    fields: dict[str, str] = {}
    try:
        with snapshot_transaction(path.parent) as folder:
            path = snapshot_path(folder, path.name)
            text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return fields

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LINE.match(line)
        if match:
            fields[match.group(1)] = match.group(2).strip()
    return fields


def _snapshot_freshness(fields: dict[str, str], path: Path) -> tuple:
    """Capture time (or file time), then JWT issuance, never token expiry."""
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = 0.0
    try:
        captured = datetime.fromisoformat(fields.get("captured_at", "").replace("Z", "+00:00"))
        observed = captured.replace(tzinfo=captured.tzinfo or timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError):
        observed = modified
    try:
        payload = fields.get("access_token", "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        issued = float(claims.get("iat") or 0)
    except (ValueError, TypeError, IndexError, AttributeError):
        issued = 0.0
    try:
        expiry = int(fields.get("expires_at_ms") or 0)
    except (ValueError, TypeError):
        expiry = 0
    return observed, issued, modified, expiry, path.name.casefold(), path.name


def save_account_snapshot(directory: str | Path, fields: dict[str, str], *, writer=None) -> Path:
    """Save one verified owner atomically, retaining metadata and its filename."""
    ident = snapshot_account_id(fields)
    for key, value in fields.items():
        if not re.fullmatch(r"[a-z_]+", key) or "\r" in str(value) or "\n" in str(value):
            raise ValueError("snapshot fields must be single-line values with valid keys")
    with snapshot_transaction(directory) as folder:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        used: dict[str, str] = {}
        owned = []
        for path in sorted(folder.iterdir()):
            if path.suffix.casefold() != ".txt":
                continue
            previous = parse_snapshot(path) if path.is_file() and not path.is_symlink() else {}
            try:
                owner = snapshot_account_id(previous)
            except ValueError:
                owner = ""
            used[path.name] = owner
            if owner == ident:
                owned.append((path, previous))
        if owned:
            path, previous = max(owned, key=lambda item: _snapshot_freshness(item[1], item[0]))
        else:
            path = snapshot_path(folder, snapshot_filename(fields.get("email", ""), ident, used))
            previous = {}
        merged = dict(previous)
        if previous.get("source") == "Manual JWT import":
            merged["imported_jwt"] = "true"
        # Missing/blank optional profile data must not erase captured metadata.
        merged.update({key: value for key, value in fields.items()
                       if value != "" or key in ("access_token", "expires_at_ms")})
        merged["account_id"] = ident
        if "access_token" in fields:
            if "captured_at" not in fields:
                merged["captured_at"] = datetime.now(timezone.utc).isoformat()
            if "expires_at_ms" in fields and "expires_at" not in fields:
                merged.pop("expires_at", None)
        (writer or write_snapshot)(path, merged)
        return path


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
        "auto_run_id": fields.get("auto_run_id", ""),
        "auto_attempt_id": fields.get("auto_attempt_id", ""),
        "imported_jwt": fields.get("imported_jwt") == "true" or fields.get("source") == "Manual JWT import",
    }
    return account


def load_from_accounts_dir(directory: str | Path) -> list[Account]:
    """Every valid snapshot, deduplicated by capture freshness, expiry sorted.

    A malformed file must not take the other snapshots down with it: one bad
    `expires_at_ms` used to raise out of here and the gateway started with an
    empty pool.
    """
    folder = Path(directory)
    if not folder.is_dir():
        return []

    accounts: dict[str, Account] = {}
    freshness: dict[str, tuple] = {}
    with snapshot_transaction(folder) as folder:
        for path in sorted(folder.iterdir()):
            if path.suffix.casefold() != ".txt" or not path.is_file() or path.is_symlink():
                continue
            try:
                fields = parse_snapshot(path)
                account = _to_account(fields, path)
            except Exception:
                log.warning("skipping unreadable account snapshot %s", path,
                            exc_info=True)
                continue
            if account is None:
                continue
            rank = _snapshot_freshness(fields, path)
            if account.id not in accounts or rank > freshness[account.id]:
                accounts[account.id] = account
                freshness[account.id] = rank

    return sorted(accounts.values(), key=lambda a: -a.expires_at)


def write_snapshot(path: Path, fields: dict[str, str]) -> None:
    """Write a snapshot in the same format (used by import/export helpers).

    Atomic: a temp file in the same directory + os.replace, so a crash or
    full disk mid-write cannot truncate an existing credential file.
    """
    order = [
        "captured_at", "source", "imported_jwt", "provider_slot", "app_config_active",
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
    for key, value in fields.items():
        if not re.fullmatch(r"[a-z_]+", key) or "\r" in str(value) or "\n" in str(value):
            raise ValueError("snapshot fields must be single-line values with valid keys")
        if key not in order:
            lines.append(f"{key + ':':<18}{value}")
    with snapshot_transaction(path.parent) as folder:
        path = snapshot_path(folder, path.name)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkstemp creates an exclusive 0600 inode, including during replacement.
        fd, tmp_name = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=folder)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write("\n".join(lines) + "\n")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
