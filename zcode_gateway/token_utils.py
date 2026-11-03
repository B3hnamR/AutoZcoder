from typing import Any
from pathlib import Path
import os, json
import tempfile
from .accounts_dir import snapshot_transaction
from .config import Config
from .pool import Account

def _parse_expiry(value: Any) -> int:
    """Normalise an expiry to epoch milliseconds.

    Captured shape is an ISO 8601 string ("2026-09-16T02:52:30Z"); epoch seconds
    and epoch milliseconds are also accepted defensively.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        v = int(value)
        return v if v > 10_000_000_000 else v * 1000
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return _parse_expiry(int(text))
        try:
            from datetime import datetime, timezone
            iso = text.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def _atomic_write(path: Path, text: str) -> None:
    """Write via a sibling temp file + os.replace.

    Truncate-in-place can destroy a credential file mid-write (crash, disk
    full, AV lock), leaving a partial pool that fails to parse on the next
    start. os.replace is atomic on the same volume, including Windows.
    """
    path = Path(path)
    with snapshot_transaction(path.parent) as folder:
        path = folder / path.name
        if path.is_symlink() or path.resolve().parent != folder:
            raise ValueError("pool snapshot path escapes its directory")
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".pool-", suffix=".tmp", dir=folder)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def persist_pool_snapshot(cfg: Config, accounts: list[Account]) -> None:
    """Write the current pool to pool.json (secrets included — keep it restricted)."""
    path = Path(cfg.accounts.pool_file)
    payload = {
        "accounts": [
            {
                "id": a.id,
                "email": a.email,
                "access_token": a.access_token,
                "refresh_token": a.refresh_token,
                "expires_at": a.expires_at,
                "source": a.source,
                "imported_jwt": bool(a.notes.get("imported_jwt") or a.source == "jwt-import"),
                "name": a.notes.get("name", ""),
                "device_mid": a.notes.get("device_mid", ""),
            }
            for a in accounts
        ]
    }
    _atomic_write(path, json.dumps(payload, indent=2))
