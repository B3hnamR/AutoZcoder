"""Read the device identity stored by the installed ZCode client."""
import json
import uuid
from pathlib import Path


def read_device_mid(path=None):
    path = Path(path) if path else Path.home() / '.zcode/v2/telemetry-state.json'
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig')).get('deviceMid')
        if isinstance(value, str) and value.strip() and all(32 <= ord(c) < 127 for c in value):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return None


def default_device_mid():
    return read_device_mid() or str(uuid.uuid4())


def account_device_mid(account_id: str) -> str:
    """Stable per-account device id, derivable from the account identity.

    The account id is identical in the chat token (id/sub) and the zjwt
    (sub/user_id), so generator and gateway derive the SAME id without any
    cross-process coordination. A real install's mid is a uuid4, not a uuid5,
    but this is the smaller diff and removes the three-identity problem.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "zcode-device:" + account_id))


def new_account_device_mid() -> str:
    """A fresh, stable device identity for one account (a real install has one)."""
    return str(uuid.uuid4())
