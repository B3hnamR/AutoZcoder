import json
import uuid

import pytest

from zcode_gateway import zcode_device
from zcode_gateway.zcode_accounts import read_credentials


@pytest.mark.parametrize('contents', ['invalid JSON', '[]', '{"deviceMid":""}', '{"deviceMid":"bad\\nheader"}'])
def test_invalid_device_state_ignored(tmp_path, contents):
    path=tmp_path/'telemetry-state.json'
    path.write_text(contents)
    assert zcode_device.read_device_mid(path) is None


def test_saved_device_imported_with_credentials(tmp_path):
    import base64
    claims=base64.urlsafe_b64encode(json.dumps({'user_id':'test-account'}).encode()).decode().rstrip('=')
    path=tmp_path/'credentials.json'
    path.write_text(json.dumps({'zcodejwttoken':f'header.{claims}.signature'}))
    (tmp_path/'telemetry-state.json').write_text(json.dumps({'deviceMid':'saved-device'}))
    assert read_credentials(path)[0]['device_mid']=='saved-device'


def test_default_device_uses_saved_identity_or_uuid(monkeypatch):
    monkeypatch.setattr(zcode_device,'read_device_mid',lambda:'saved-device')
    assert zcode_device.default_device_mid()=='saved-device'
    monkeypatch.setattr(zcode_device,'read_device_mid',lambda:None)
    assert str(uuid.UUID(zcode_device.default_device_mid()))
