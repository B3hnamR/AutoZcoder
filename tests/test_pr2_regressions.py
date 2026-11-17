"""Regression coverage for the selectively integrated PR #2 fixes."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

from zcode_gateway import api_admin, api_dash, gui, notify
from zcode_gateway.app import create_app
from zcode_gateway.breaker import CLEAN_AFTER_S, EdgeBlockBreaker
from zcode_gateway.captcha_tickets import CaptchaTicket, CaptchaTicketPool
from zcode_gateway.claim_schedule import ClaimScheduler
from zcode_gateway.config import Config
from zcode_gateway.generator import GeneratorManager, ProxyChecker
from zcode_gateway.pool import Account, AccountState, PoolManager
from zcode_gateway.relay_manager import RelayManager
from zcode_gateway.service import ChatService
from zcode_gateway.store import JsonlCapture
from zcode_gateway.tokens import TokenManager
from zcode_gateway.translate_anthropic import extract_stream_error, stream_had_content
from zcode_gateway.upstream import RelayTransportError, UpstreamClient, UpstreamConfig, iter_sse_events

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('sep', ['\u2028', '\u2029', '\u0085'])
@pytest.mark.parametrize('ending', ['\r\n', '\n', '\r'])
def test_sse_unicode_payload(sep, ending):
    text = f'before{sep}after'
    def frame(value):
        return 'data: ' + json.dumps(value, ensure_ascii=False) + ending * 2
    event = {'choices': [{'delta': {'content': text}}]}
    assert iter_sse_events(frame(event)) == [event]
    assert stream_had_content(frame(event))
    assert not stream_had_content(frame({'choices': [{'delta': {'role': 'assistant'}}]}))
    error = extract_stream_error(frame({'error': {'code': 'bad', 'message': text}}))
    assert error['message'] == text


@pytest.mark.asyncio
@pytest.mark.parametrize('state', [AccountState.EXHAUSTED, AccountState.DEAD, AccountState.COOLING])
async def test_upsert_preserves_state_and_resets_strikes(state):
    account = Account(id='a', access_token='old', state=state, auth_strikes=3, quota_strikes=3)
    account.cooldown_until = 12345678900
    account.model_caps['GLM-5.3'] = 12345678900
    pool = PoolManager(Config().pool, [account])
    await pool.upsert(Account(id='a', access_token='new'))
    assert account.state is state and account.cooldown_until == 12345678900
    assert account.model_caps['GLM-5.3'] == 12345678900
    assert account.access_token == 'new'
    assert account.auth_strikes == account.quota_strikes == 0
    account.auth_strikes = account.quota_strikes = 3
    assert await pool.enable('a')
    assert account.state is AccountState.READY
    assert account.auth_strikes == account.quota_strikes == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_exhausted_stays_retired_until_credentials_reset():
    account = Account(id='a', access_token='old', state=AccountState.EXHAUSTED, quota_strikes=3)
    pool = PoolManager(Config().pool, [account])
    await pool.cool(account)
    await pool.kill_after_strikes(account, 'revoked')
    assert account.state is AccountState.EXHAUSTED and not account.is_ready()
    tokens = TokenManager(Config(), pool)
    await tokens.apply_credentials(account, access_token='new')
    assert account.state is AccountState.READY and account.quota_strikes == 0
    await tokens.aclose()
    await pool.aclose()


def test_breaker_and_notification_episode_dedupe(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    clock.time = lambda: clock.now
    monkeypatch.setattr('zcode_gateway.breaker.time', clock)
    monkeypatch.setattr(notify, 'time', clock)
    breaker = EdgeBlockBreaker()
    fired = []
    breaker.on_block = fired.append
    body = ('<html><title>403 Forbidden</title><h2>Your client does not have permission '
            'to get URL <code>/api/v1/chat/completions</code> from this server.</h2></html>')
    for _ in range(3):
        assert breaker.note_edge_block(body)
    assert fired == [1] and breaker.consecutive == 3
    clock.now += CLEAN_AFTER_S + 1
    breaker.note_edge_block(body)
    assert fired == [1, 1]
    center = notify.NotificationCenter()
    first = center.emit('case', 'A')
    clock.now += 1
    center.emit('case', 'B')
    clock.now += 54
    assert center.emit('case', 'A') == first
    clock.now += 7
    center.emit('case', 'C')
    assert center.emit('case', 'A') == first
    assert center.list()[0]['count'] == 3


def test_captcha_take_retains_unused_fresh_ticket():
    pool = CaptchaTicketPool(size=3, ttl_seconds=75, mint=lambda: None, clock=lambda: 100)
    for name, minted in [('stale', 10), ('first', 100), ('second', 100)]:
        pool.put(CaptchaTicket(name + '-param-value-xxxxxxxx', 'sgp', minted_at=minted))
    assert pool.take().param.startswith('second')
    assert pool.stats()['ready'] == 1
    assert pool.take().param.startswith('first')
    assert pool.take() is None


def test_health_non_ascii_nonce(tmp_path, monkeypatch):
    monkeypatch.setenv('ZCODE_GATEWAY_UPDATE_NONCE', 'a' * 32)
    cfg = Config(server={'host': '0.0.0.0', 'expose_ops': False, 'admin_key': 'dummy-admin',
                         'client_keys': [{'key': 'fixture', 'name': 'fixture'}]},
                 accounts={'source': 'accounts_dir', 'dir': str(tmp_path / 'accounts')},
                 store={'sqlite_path': str(tmp_path / 'usage.db')}, update={'enabled': False})
    with TestClient(create_app(cfg), client=('198.51.100.3', 12345),
                    base_url='http://127.0.0.1:8788') as client:
        assert client.get('/health', headers={b'X-AutoZCoder-Update': b'\xe9'}).status_code == 404
        assert client.get('/health', headers={'X-AutoZCoder-Update': 'a' * 32}).status_code == 200


@pytest.mark.asyncio
async def test_locate_shutdown(tmp_path, monkeypatch):
    gen = tmp_path / 'gen'
    gen.mkdir()
    (gen / 'main.py').touch()
    (gen / 'config.py').touch()
    old = GeneratorManager(gen, data_dir=tmp_path / 'data')
    scheduler = ClaimScheduler(run=lambda: None, enabled=True,
                               state_path=tmp_path / 'schedule.json', poll_seconds=1)
    scheduler.start()
    old.claim_scheduler = scheduler
    cfg = Config()
    cfg._config_file = tmp_path / 'config.yaml'
    cfg._config_file.write_text(yaml.safe_dump(cfg.model_dump(mode='json')))
    state = SimpleNamespace(cfg=cfg, generator=old, claim_scheduler=scheduler)
    monkeypatch.setattr(api_dash, 'get_state', lambda _: state)
    try:
        assert (await api_dash.generator_locate_set(None, {'path': str(gen)}, 'admin'))['ok']
        assert state.generator is state.claim_scheduler is None
        assert old._closed.is_set() and not scheduler._thread.is_alive()
    finally:
        old.shutdown()


@pytest.mark.asyncio
async def test_blocking_endpoints_off_loop(monkeypatch):
    main = threading.current_thread()
    calls = []
    def blocking(*args, **kwargs):
        calls.append(threading.current_thread() is main)
        return True
    checker = SimpleNamespace(stop=blocking, status=blocking, run=blocking)
    manager = SimpleNamespace(stop_claim=blocking, remove_claim=blocking, stop_worker=blocking,
                              stop_all=blocking, remove_worker=blocking, checker=checker)
    monkeypatch.setattr(api_dash, '_generator', lambda _: manager)
    for endpoint in [api_admin.claim_stop, api_admin.claim_remove, api_dash.generator_stop,
                     api_dash.generator_worker_remove]:
        await endpoint(None, {'name': 'job'}, 'admin')
    for endpoint in [api_dash.generator_stopall, api_dash.generator_proxies_stop,
                     api_dash.generator_proxies, api_dash.generator_proxies_countries]:
        await endpoint(None, 'admin')
    await api_dash.generator_proxies_run(None, {'action': 'recheck'}, 'admin')
    await api_dash.generator_proxies_maintain(None, {'action': 'prune'}, 'admin')
    assert len(calls) == 10 and not any(calls)


@pytest.mark.asyncio
async def test_update_snapshot_and_removal_use_same_lock(tmp_path):
    manager = GeneratorManager(tmp_path, data_dir=tmp_path / 'data')
    observations = []
    class LockedDict(dict):
        def values(self):
            assert manager._lock.locked()
            return super().values()
        def pop(self, *args):
            assert manager._lock.locked()
            observations.append('pop')
            return super().pop(*args)
    def stop():
        assert not manager._lock.locked()
        observations.append('stop')
    job = SimpleNamespace(done=threading.Event(), alive=lambda: True, stop=stop)
    manager.workers = LockedDict(job=job)
    manager.claims = LockedDict(job=job)
    state = SimpleNamespace(generator=manager, pool=SimpleNamespace(
        snapshot=AsyncMock(return_value={'detail': []})))
    with pytest.raises(HTTPException) as error:
        await api_dash._check_update_idle(state)
    assert error.value.status_code == 409
    assert await asyncio.to_thread(manager.remove_worker, 'job')
    assert await asyncio.to_thread(manager.remove_claim, 'job')
    assert observations == ['stop', 'pop', 'stop', 'pop']
    await api_dash._check_update_idle(state)
    manager.shutdown()


@pytest.mark.asyncio
async def test_proxy_interval_and_pool_encoding(tmp_path, monkeypatch):
    checker = ProxyChecker(tmp_path)
    monkeypatch.setattr(api_dash, '_generator', lambda _: SimpleNamespace(checker=checker))
    with pytest.raises(HTTPException) as error:
        await api_admin.proxy_auto_verify(None, {'enabled': True, 'interval_min': 'abc'}, 'admin')
    assert error.value.status_code == 400 and not checker.auto_verify
    assert (await api_admin.proxy_auto_verify(
        None, {'enabled': True, 'interval_min': '12'}, 'admin'))['interval_min'] == 12
    await api_admin.proxy_auto_verify(None, {'enabled': False}, 'admin')
    manager = GeneratorManager(tmp_path, data_dir=tmp_path / 'data')
    manager.pool_file.write_bytes(b'# \x81 bad comment\nsocks5://127.0.0.1:1080\n')
    assert manager._pool_has_candidates()
    manager.pool_file.write_bytes(b'# \x81 bad comment\n')
    assert not manager._pool_has_candidates()
    manager.shutdown()


def test_disabled_capture_skips_deepcopy(tmp_path):
    class Body(dict):
        def __deepcopy__(self, memo):
            raise AssertionError('disabled capture must not copy')
    records = []
    service = ChatService(Config(), None, None, None, None,
                          SimpleNamespace(record=lambda **rec: records.append(rec)),
                          JsonlCapture(str(tmp_path), enabled=False))
    service._record(Account(id='a'), {}, Body(stream=True), 200, {}, 'response', 1,
                    'key', 'openai', 'GLM-5.3', {'input_tokens': 2, 'output_tokens': 3})
    assert records[0]['total_tokens'] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize('strategy,expected', [('single', 1), ('round_robin', 2)])
async def test_relay_retry_does_not_repeat(tmp_path, strategy, expected):
    mgr = RelayManager(data_dir=tmp_path)
    for name in ['cf1', 'cf2']:
        mgr.add_relay(name, f'https://{name}.workers.dev', 'cloudflare')
    mgr.update_options(enabled=True, strategy=strategy)
    if strategy == 'single':
        mgr.mark_relay_failed('https://cf2.workers.dev')
    client = UpstreamClient.__new__(UpstreamClient)
    client.cfg = UpstreamConfig(base_url='https://zcode.z.ai/api/v1',
                                chat_path='/zcode-plan/anthropic/v1/messages')
    client.pacing, client.relay_manager = None, mgr
    calls = []
    async def refuse(*args, **kwargs):
        raise httpx.ConnectError('refused')
    client._client = SimpleNamespace(build_request=lambda method, url, **kw: calls.append(url),
                                     send=refuse)
    with pytest.raises(RelayTransportError):
        await client.send({}, {'model': 'GLM-5.3'}, True)
    assert len(calls) == len(set(calls)) == expected


def test_relay_reload_and_single_model(tmp_path, monkeypatch):
    mgr = RelayManager(data_dir=tmp_path)
    entry = mgr.add_relay('cf', 'https://cf.workers.dev', 'cloudflare')
    fresh = RelayManager(data_dir=tmp_path)
    real_read = Path.read_text
    calls = []
    def flaky(path, *args, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError('locked')
        return real_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', flaky)
    assert fresh.get_settings()['relays'] == []
    fresh.update_options(enabled=True, models='GLM-5.3')
    assert fresh.get_settings()['relays'][0]['id'] == entry['id']
    assert fresh.get_settings()['models'] == ['GLM-5.3']
    (tmp_path / 'relays.json').write_text('{ truncated')
    repaired = RelayManager(data_dir=tmp_path)
    assert repaired.get_settings()['relays'] == []
    (tmp_path / 'relays.json').write_text(json.dumps({'relays': [{'id': 'repaired'}]}))
    assert repaired.get_settings()['relays'][0]['id'] == 'repaired'


@pytest.mark.parametrize('fail', [False, True])
def test_atomic_gui_config(tmp_path, monkeypatch, fail):
    path = tmp_path / 'config.yaml'
    path.write_text('old', encoding='utf-8')
    replace = os.replace
    calls = []
    def replace_checked(src, dst):
        assert Path(src).parent == path.parent and Path(src) != path
        assert path.read_text() == 'old'
        calls.append(dst)
        if fail:
            raise OSError('disk failure')
        replace(src, dst)
    monkeypatch.setattr(os, 'replace', replace_checked)
    if fail:
        with pytest.raises(OSError):
            gui._write_config_atomic(path, 'new')
    else:
        gui._write_config_atomic(path, 'new')
    assert calls == [path]
    assert path.read_text() == ('old' if fail else 'new')
    assert list(tmp_path.iterdir()) == [path]


def test_gui_stop_during_startup(tmp_path, monkeypatch):
    import uvicorn
    from zcode_gateway import app as app_module
    entered, release = threading.Event(), threading.Event()
    def slow_create(cfg):
        entered.set()
        assert release.wait(5)
        return object()
    monkeypatch.setattr(app_module, 'create_app', slow_create)
    created = []
    monkeypatch.setattr(uvicorn, 'Server', lambda cfg: created.append(cfg))
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({'server': {'port': 9124},
                                    'accounts': {'dir': str(tmp_path)}}))
    controller = gui.ServerController()
    controller.start(path)
    try:
        assert entered.wait(5)
        controller.stop(wait=0.01)
    finally:
        release.set()
        assert controller.wait_stopped(5)
    assert created == [] and controller.error is None


def test_gui_readiness_waits_for_config(monkeypatch):
    import urllib.request
    calls = []
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: calls.append(a))
    assert not gui.ServerController().wait_ready(timeout=0.1)
    assert calls == []


def _load_script(name):
    spec = importlib.util.spec_from_file_location('pr2_' + name, ROOT / 'generator' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_ocr_and_proxy_guards(monkeypatch):
    import types
    monkeypatch.syspath_prepend(str(ROOT / 'generator'))
    fake = types.ModuleType('playwright.sync_api')
    fake.Page = object
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', fake)
    solver = _load_script('captcha_solver')
    def missing():
        raise RuntimeError('ddddocr missing')
    calc = solver.DistanceCalculator(ocr=missing, logger=logging.getLogger('pr2-ocr'))
    assert calc._need_ocr() is None
    calls = []
    ocr = SimpleNamespace(slide_match=lambda *a, **k: None)
    calc = solver.DistanceCalculator(ocr=lambda: (calls.append(1), ocr)[1],
                                     logger=logging.getLogger('pr2-ocr'))
    assert calc._need_ocr() is calc._need_ocr() is ocr and calls == [1]
    ic = _load_script('invite_claim')
    assert ic._pw_proxy('socks5://1.2.3.4') is None
    assert ic._pw_proxy('http://user:pw@1.2.3.4:8080') == {
        'server': 'http://1.2.3.4:8080', 'username': 'user', 'password': 'pw'}
    pp = _load_script('proxy_parse')
    monkeypatch.delenv('ZAI_ALLOW_LOOPBACK', raising=False)
    monkeypatch.delenv('PROXYHIVE_ALLOW_LOOPBACK', raising=False)
    monkeypatch.delenv('ZAI_ALLOW_PRIVATE', raising=False)
    assert not pp.valid_endpoint('0177.0.0.1', 8080)
    assert not pp.valid_endpoint('0127.0.0.1', 8080)
    assert pp.valid_endpoint('93.184.216.34', 8080)


def test_generator_telemetry_timezone_and_device(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'generator'))
    ic = _load_script('invite_claim')
    sent = []
    def request(method, url, **kwargs):
        sent.append((url, kwargs))
        data = {'code': 0}
        if 'cli/init' in url:
            data = {'data': {'flow_id': 'f1', 'poll_token': 'p1',
                             'authorize_url': 'https://x/?state=S1'}}
        elif 'oauth/authorize' in url:
            data = {'redirect_url': 'https://x/?code=code-abc123'}
        elif 'cli/poll' in url:
            data = {'data': {'status': 'ready', 'token': 'ZJWT',
                             'user': {'user_id': 'u1'}, 'zai': {}}}
        return SimpleNamespace(status_code=200, text='', json=lambda: data)
    monkeypatch.setattr(ic, '_oauth_request', request)
    monkeypatch.setattr(ic, 'client_timezone', lambda: 'Pacific/Auckland')
    assert ic.report_login_device('token', log=lambda _: None)
    for _, kw in sent:
        assert kw['headers']['x-device-mid'] == ic.per_account_device_mid('token')
        assert kw['json']['device_mid'] == ic.per_account_device_mid('token')
        assert kw['headers']['x-client-timezone'] == kw['json']['client_timezone'] == 'Pacific/Auckland'
    sent.clear()
    monkeypatch.setattr(ic, 'report_login_device', lambda *a, **k: True)
    assert ic.device_login('chat-token', log=lambda _: None) == ('ZJWT', False)
    flows = [kw for url, kw in sent if any(s in url for s in ['cli/init', 'cli/poll', 'callback'])]
    assert len(flows) == 3
    assert all(kw['headers']['x-device-mid'] == ic.per_account_device_mid('chat-token') for kw in flows)
    pc = _load_script('plan_check')
    monkeypatch.delenv('ZAI_ACTIVITY_EVENTS', raising=False)
    monkeypatch.setattr(pc, 'client_timezone', lambda: 'UTC')
    assert {e['client_timezone'] for e in pc._plan_events('token')} == {'UTC'}
    claim = _load_script('claim500')
    monkeypatch.setattr(claim, 'client_timezone', lambda: 'UTC')
    assert claim._headers('token')['x-client-timezone'] == 'UTC'


@pytest.mark.parametrize('script', ['export_starter', 'recheck_plans'])
def test_generator_batch_continues_after_failure(tmp_path, monkeypatch, script):
    monkeypatch.syspath_prepend(str(ROOT / 'generator'))
    def get_jwt(token, **kwargs):
        if token == 'eyJbad':
            raise RuntimeError('poll failed')
        return 'eyJgood'
    stub = SimpleNamespace(get_zcode_jwt=get_jwt,
        get_plan=lambda *a, **k: {'plans': ['start'], 'starter': True},
        is_start_plan=lambda _: True, DEVICE_MID='shared',
        per_account_device_mid=lambda _: 'per-account')
    monkeypatch.setitem(sys.modules, 'plan_check', stub)
    monkeypatch.setenv('ZAI_OUTPUT_DIR', str(tmp_path))
    monkeypatch.setenv('ZCODE_GATEWAY_ACCOUNTS', str(tmp_path / 'accounts'))
    monkeypatch.setattr(sys, 'argv', [script + '.py'])
    results = tmp_path / 'results'
    results.mkdir()
    (results / 'accounts.txt').write_text('bad@example.test:pw:eyJbad\nok@example.test:pw:eyJgood\n')
    module = _load_script(script)
    module.main()
    if script == 'export_starter':
        assert [p.name for p in (tmp_path / 'accounts').iterdir()] == ['ok@example.test.txt']
        text = (tmp_path / 'accounts' / 'ok@example.test.txt').read_text()
        assert 'per-account' in text and 'shared' not in text
    else:
        rows = [json.loads(line) for line in (results / 'plans_recheck.jsonl').read_text().splitlines()]
        assert len(rows) == 2 and 'RuntimeError' in rows[0]['error']
        assert rows[1]['starter']


@pytest.mark.parametrize('user_stop', [False, True])
def test_gui_stop_invalidates_queued_restart(user_stop):
    pending, started = [], []
    queued = threading.Event()
    def after(delay, callback, *args):
        pending.append((callback, args))
        queued.set()
    widget = SimpleNamespace(configure=lambda **kw: None)
    app = SimpleNamespace(_lifecycle=0, _restart_pending=False, _restart_message=None,
        _stopping=False, server_urls={}, status_dot=widget, status_lbl=widget,
        server=SimpleNamespace(stop=lambda: None, wait_stopped=lambda: True),
        toast=lambda *a: None, log=lambda *a: None, after=after,
        start_everything=lambda: started.append(True))
    app.stop_everything = lambda: gui.App.stop_everything(app)
    app._start_after_restart = lambda generation: gui.App._start_after_restart(app, generation)
    app._restart_failed = lambda generation: gui.App._restart_failed(app, generation)
    gui.App.restart_server(app, 'saved')
    assert queued.wait(5)
    if user_stop:
        app.stop_everything()
    for callback, args in pending:
        callback(*args)
    assert started == ([] if user_stop else [True])


@pytest.mark.asyncio
async def test_smoke_drains_service_before_store_close(monkeypatch):
    spec = importlib.util.spec_from_file_location('pr2_smoke', ROOT / 'scripts' / 'smoke' / 'live.py')
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
    events = []
    monkeypatch.setattr(live, 'load_config', lambda _: SimpleNamespace(accounts=None, pool=None, upstream=None))
    monkeypatch.setattr(live, 'load_accounts', lambda _: [SimpleNamespace(notes={'quotas': {}})])
    monkeypatch.setattr(live, 'PoolManager', lambda *a: None)
    monkeypatch.setattr(live, 'TokenManager', lambda *a: SimpleNamespace(
        check_account=AsyncMock(return_value={}), aclose=AsyncMock()))
    monkeypatch.setattr(live, 'UpstreamClient', lambda *a: SimpleNamespace(aclose=AsyncMock()))
    monkeypatch.setattr(live, 'Store', lambda *a: SimpleNamespace(close=lambda: events.append('store.close')))
    monkeypatch.setattr(live, 'JsonlCapture', lambda *a: None)
    monkeypatch.setattr(live, 'ChatService', lambda *a: SimpleNamespace(
        complete=AsyncMock(return_value=SimpleNamespace(raw=json.dumps({'stop_reason': 'end_turn', 'usage': {}}))),
        aclose=AsyncMock(side_effect=lambda: events.append('service.aclose'))))
    assert await live.run(SimpleNamespace(config='unused', infer=True)) == 0
    assert events == ['service.aclose', 'store.close']


@pytest.mark.asyncio
async def test_admin_readd_explicitly_revives(monkeypatch):
    account = Account(id='a', access_token='old', state=AccountState.EXHAUSTED, quota_strikes=3)
    pool = PoolManager(Config().pool, [account])
    monkeypatch.setattr(api_admin, 'get_state', lambda _: SimpleNamespace(cfg=Config(), pool=pool))
    await api_admin.add_account(None, {'id': 'a', 'access_token': 'new'}, 'admin')
    assert account.state is AccountState.READY and account.quota_strikes == 0
    await pool.aclose()
