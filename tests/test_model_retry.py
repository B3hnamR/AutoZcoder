import math
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from zcode_gateway.config import UpstreamConfig
from zcode_gateway.model_retry import overload_delay, retry_after_seconds


@pytest.mark.parametrize('headers,expected', [
    ({'retry-after':'3.5'}, 3.5), ({'Retry-After-Ms':'2500'}, 2.5),
    ({'retry-after-ms':'invalid','retry-after':'4'}, 4),
    ({'retry-after':'NaN'}, None), ({'retry-after':'inf'}, None),
    ({'retry-after':'-5'}, None), ({}, None),
])
def test_retry_headers(headers, expected):
    assert retry_after_seconds(headers) == expected


def test_retry_date(monkeypatch):
    monkeypatch.setattr('zcode_gateway.model_retry.time.time', lambda: 1000)
    date = format_datetime(datetime.fromtimestamp(1012, timezone.utc), usegmt=True)
    assert retry_after_seconds({'retry-after':date}) == 12


def test_backoff_jitter_and_server_delay_bounds():
    cfg = UpstreamConfig()
    for attempt in range(1, 12):
        ceiling = min(2 * 2 ** (attempt - 1), 60)
        assert ceiling / 2 <= overload_delay(cfg, attempt) <= ceiling
    assert overload_delay(cfg, 1, 600) == 60
    assert overload_delay(cfg, 1, 0) == 0


@pytest.mark.parametrize('field,value', [
    ('overload_max_attempts',0), ('overload_max_attempts',31),
    ('overload_base_delay_seconds',-1), ('overload_base_delay_seconds',math.nan),
    ('overload_max_delay_seconds',math.inf), ('overload_max_delay_seconds',61),
])
def test_invalid_retry_configuration(field, value):
    with pytest.raises(ValueError):
        UpstreamConfig(**{field:value})
