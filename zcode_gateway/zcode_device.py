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
