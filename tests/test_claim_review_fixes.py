"""Offline regressions for reviewed claim transport and sidecar boundaries."""
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import claim_start as cs
import plan_check as pc


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('unexpected network access')
    monkeypatch.setattr(pc.requests.Session, 'send', forbidden)
    monkeypatch.setattr(cs.time, 'sleep', lambda *_: None)


def response(payload, status=200):
    return SimpleNamespace(status_code=status, json=lambda: payload)


@pytest.mark.parametrize('payload,status,kind', [
    (None, 200, 'malformed'), ([], 200, 'malformed'), ('text', 200, 'malformed'),
    ({'code': True, 'data': {}}, 200, 'malformed'),
    ({'code': 0, 'data': {}}, 503, 'http'),
    ({'code': 1005, 'data': {}}, 200, 'business'),
    ({'code': 0, 'data': None}, 200, 'malformed'),
    ({'code': 0, 'data': {'plans': [None]}}, 200, 'malformed'),
    ({'code': 0, 'data': {'plans': [{'plan_id': 3}]}}, 200, 'malformed'),
    ({'code': 0, 'data': {'plans': [{'entitlements': [None]}]}}, 200, 'malformed'),
    ({'code': 0, 'data': {'plans': [{'entitlements': 'bad'}]}}, 200, 'malformed'),
    ({'code': 0, 'data': {'balances': [None]}}, 200, 'malformed'),
])
def test_balance_errors_are_structured(monkeypatch, payload, status, kind):
    monkeypatch.setattr(pc, '_request', lambda *a, **k: response(payload, status))
    result = pc.get_plan('offline', log=lambda *_: None)
    assert result['error_kind'] == kind
    assert not result['starter'] and result['plans'] == []


@pytest.mark.parametrize('payload', [None, [], 'text', {'code': None}, {'code': True}])
def test_claim_always_returns_error_dict(monkeypatch, payload):
    monkeypatch.setattr(pc, '_request', lambda *a, **k: response(payload))
    assert pc.claim_plan('offline', 'p')['code'] == -1


def test_http_error_cannot_supply_business_success_or_limit(monkeypatch):
    monkeypatch.setattr(pc, '_request', lambda *a, **k: response({'code': 1005}, 503))
    assert pc.claim_plan('offline', 'p')['error_kind'] == 'http'


def test_transport_and_non_json_balance_errors(monkeypatch):
    def fail(*a, **k):
        raise RuntimeError('offline transport')
    monkeypatch.setattr(pc, '_request', fail)
    assert pc.get_plan('offline')['error_kind'] == 'transport'
    monkeypatch.setattr(pc, '_request', lambda *a, **k: SimpleNamespace(status_code=200, json=fail))
    assert pc.get_plan('offline')['error_kind'] == 'non_json'


@pytest.mark.parametrize('codes,unavailable,route', [
    ([1005, 1005], True, False), ([1005, -1], False, True),
    ([-1, 1005], False, True), ([1001, 1005], False, False),
])
def test_all_offers_must_actually_return_1005(monkeypatch, codes, unavailable, route):
    monkeypatch.setattr(cs, 'get_plan', lambda *a, **k: {})
    monkeypatch.setattr(cs, 'report_app_activity', lambda *a, **k: True)
    monkeypatch.setattr(cs, 'claimable_plans', lambda *a, **k: [
        {'plan_id': 'zcode-offer-a'}, {'plan_id': 'zcode-offer-b'}])
    monkeypatch.setattr(cs, '_captcha_token', lambda *a, **k: ('', ''))
    values = iter(codes)
    monkeypatch.setattr(cs, 'claim_plan', lambda *a, **k: {'code': next(values)})
    result, _ = cs._run_chain('offline', '', lambda *_: None, False,
                              zjwt='jwt', claim_attempts=1)
    assert bool(result.get('unavailable')) is unavailable
    assert result['route_problem'] is route


@pytest.mark.parametrize('direct,route', [(True, ''), (False, 'http://selected:8080')])
def test_sidecar_receives_explicit_route_and_no_ambient_proxies(monkeypatch, direct, route):
    monkeypatch.delenv('ZAI_CAPTCHA_PARAM', raising=False)
    monkeypatch.setenv('ZAI_CAPTCHA_CMD', 'offline-sidecar')
    monkeypatch.setenv('ZAI_PROXY', 'http://selected:8080')
    for key in ('http_proxy', 'HTTPS_PROXY', 'ALL_PROXY', 'ZAI_CAPTCHA_PROXY'):
        monkeypatch.setenv(key, 'http://ambient:8080')
    monkeypatch.setattr(cs, '_captcha_config', lambda *_: {})
    seen = {}

    def run(argv, env):
        seen.update(env)
        return subprocess.CompletedProcess(argv, 0, 'V' * 40 + ' sgp\n', '')

    monkeypatch.setattr(cs, '_run_sidecar', run)
    assert cs._captcha_token(lambda *_: None, force_direct=direct) == ('V' * 40, 'sgp')
    assert seen['ZAI_CAPTCHA_PROXY'] == route
    assert seen['NO_PROXY'] == '*'
    assert not any(k.upper() in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY') for k in seen)
    assert seen.get('ZAI_PROXY', '') == route


def test_sidecar_captures_exact_record():
    out = cs._run_sidecar([sys.executable, '-c', "print('V'*40+' sgp')"], dict(os.environ))
    assert out.returncode == 0 and out.stdout.strip() == 'V' * 40 + ' sgp'


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_sidecar_limits_both_streams_independently(monkeypatch, stream):
    monkeypatch.setattr(cs, 'SIDECAR_STDOUT_BYTES', 1024)
    monkeypatch.setattr(cs, 'SIDECAR_STDERR_BYTES', 2048)
    with pytest.raises(ValueError, match='output limit'):
        cs._run_sidecar([sys.executable, '-c',
                        f"import sys;sys.{stream}.write('x'*8192);sys.{stream}.flush()"],
                       dict(os.environ))


def test_sidecar_deadline_kills_descendant_holding_pipes(monkeypatch):
    monkeypatch.setattr(cs, 'SIDECAR_TIMEOUT', 0.3)
    script = "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])"
    start = time.monotonic()
    with pytest.raises(TimeoutError, match='deadline'):
        cs._run_sidecar([sys.executable, '-c', script], dict(os.environ))
    assert time.monotonic() - start < 5


@pytest.mark.parametrize('stdout,rc,accepted', [
    ('V' * 40 + ' sgp\n', 0, True), ('V' * 40 + ' sgp\r\n', 0, True),
    ('V' * 40 + ' sgp\n', 1, False), ('noise\n' + 'V' * 40 + ' sgp\n', 0, False),
    ('V' * 40 + ' sgp extra\n', 0, False), ('short sgp\n', 0, False),
])
def test_sidecar_protocol_remains_exact(monkeypatch, stdout, rc, accepted):
    monkeypatch.setenv('ZAI_CAPTCHA_CMD', 'offline-sidecar')
    monkeypatch.setattr(cs, '_captcha_config', lambda *_: {})
    monkeypatch.setattr(cs, '_run_sidecar', lambda argv, env:
                        subprocess.CompletedProcess(argv, rc, stdout, 'private-token'))
    logs = []
    result = cs._captcha_token(logs.append)
    assert bool(result[0]) is accepted
    assert 'private-token' not in str(logs)


def test_sidecar_uses_thread_scoped_route(monkeypatch):
    monkeypatch.setenv('ZAI_PROXY', 'http://ambient:80')
    monkeypatch.setenv('ZAI_CAPTCHA_CMD', 'offline-sidecar')
    monkeypatch.setattr(cs, '_captcha_config', lambda *_: {})
    seen = []
    monkeypatch.setattr(cs, '_run_sidecar', lambda argv, env:
                        seen.append(env['ZAI_CAPTCHA_PROXY']) or
                        subprocess.CompletedProcess(argv, 1, '', ''))
    with pc.request_route('http://thread-route:8080'):
        cs._captcha_token(lambda *_: None)
    with pc.request_route(''):
        cs._captcha_token(lambda *_: None)
    assert seen == ['http://thread-route:8080', '']


def test_captcha_config_body_is_streamed_and_capped(monkeypatch):
    seen = {}
    reply = SimpleNamespace(status_code=200, close=lambda: seen.update(closed=True))
    def request(*a, **kw):
        seen.update(kw)
        return reply
    def body(r, limit, deadline):
        assert r is reply and limit == 64 << 10 and deadline > time.monotonic()
        return b'{"data":{"configs":{"captcha":{"enabled":true,"sceneId":"offline"}}}}'
    import proxy_pool
    monkeypatch.setattr(cs, '_request', request)
    monkeypatch.setattr(proxy_pool, '_probe_body', body)
    assert cs._captcha_config(lambda *_: None)['sceneId'] == 'offline'
    assert seen['stream'] is True and seen['allow_redirects'] is False and seen['closed']

def test_captcha_token_scene_override_skips_client_config(monkeypatch):
    """Signup uses a different Aliyun scene; the override must reach the sidecar."""
    monkeypatch.setenv('ZAI_CAPTCHA_CMD', 'offline-sidecar')

    def fails(*_):
        raise AssertionError('the signup scene override must skip /client/configs')

    monkeypatch.setattr(cs, '_captcha_config', fails)
    seen = {}
    monkeypatch.setattr(cs, '_run_sidecar', lambda argv, env:
                        seen.update(env) or
                        subprocess.CompletedProcess(argv, 0, 'V' * 40 + ' sgp\n', ''))
    assert cs._captcha_token(lambda *_: None, scene='36qgs6xb', region='sgp',
                             prefix='no8xfe') == ('V' * 40, 'sgp')
    assert seen['ZAI_CAPTCHA_SCENE'] == '36qgs6xb'
    assert seen['ZAI_CAPTCHA_PREFIX'] == 'no8xfe'
    assert seen['ZAI_CAPTCHA_REGION'] == 'sgp'
