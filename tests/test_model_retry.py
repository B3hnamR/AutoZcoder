import math
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from zcode_gateway.config import UpstreamConfig
from zcode_gateway.model_retry import concurrency_delay, overload_delay, retry_after_seconds
from zcode_gateway.upstream import ErrorKind, classify, is_concurrency_code


@pytest.mark.parametrize('code', [3008, '3008', ' 3008 ', '3009', '3010'])
def test_concurrency_codes(code):
    assert is_concurrency_code(code)


@pytest.mark.parametrize('code', [None, '', 3007, '3011', 'rate_limit_error'])
def test_non_concurrency_codes(code):
    assert not is_concurrency_code(code)


@pytest.mark.parametrize('status', [200, 429, 502])
def test_concurrency_classifies_as_rate_limited(status):
    assert classify(status, '{"error":{"code":3008,"message":"user concurrency limit exceeded"}}') is ErrorKind.RATE_LIMITED


def test_concurrency_backoff_bounds():
    cfg = UpstreamConfig()
    for attempt in range(1, 6):
        ceiling = min(2 ** (attempt - 1), 10)
        assert ceiling / 2 <= concurrency_delay(cfg, attempt) <= ceiling
    assert concurrency_delay(cfg, 1, 600) == 10
    assert concurrency_delay(cfg, 1, 0) == 0


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
    ('concurrency_max_attempts',0), ('concurrency_max_attempts',31),
    ('concurrency_base_delay_seconds',-1), ('concurrency_base_delay_seconds',math.nan),
    ('concurrency_max_delay_seconds',math.inf), ('concurrency_max_delay_seconds',61),
])
def test_invalid_retry_configuration(field, value):
    with pytest.raises(ValueError):
        UpstreamConfig(**{field:value})
