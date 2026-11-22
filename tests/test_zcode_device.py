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


def test_new_account_device_mid_is_a_distinct_uuid():
    first, second = zcode_device.new_account_device_mid(), zcode_device.new_account_device_mid()
    assert str(uuid.UUID(first)) == first
    assert str(uuid.UUID(second)) == second
    assert first != second


def test_account_device_mid_is_stable_per_account():
    assert zcode_device.account_device_mid('usr-one') == zcode_device.account_device_mid('usr-one')
    assert zcode_device.account_device_mid('usr-one') != zcode_device.account_device_mid('usr-two')
