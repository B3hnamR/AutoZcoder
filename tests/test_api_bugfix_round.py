"""Regression tests for the second review-bug round (API/health/service/generator)."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient
from zcode_gateway import api_admin, api_dash
from zcode_gateway.app import create_app
from zcode_gateway.claim_schedule import ClaimScheduler
from zcode_gateway.config import Config
from zcode_gateway.generator import GeneratorManager, ProxyChecker
from zcode_gateway.pool import Account, AccountState, PoolManager
from zcode_gateway.service import ChatService
from zcode_gateway.store import JsonlCapture


def _cfg(tmp_path):
    return Config(
        server={'host': '127.0.0.1', 'admin_key': 'dummy-admin',
                'client_keys': [{'key': 'fixture-client-key', 'name': 'fixture'}]},
        accounts={'source': 'accounts_dir', 'dir': str(tmp_path / 'accounts')},
        store={'sqlite_path': str(tmp_path / 'usage.db')},
        update={'enabled': False})


def test_health_tolerates_non_ascii_update_probe(tmp_path, monkeypatch):
    monkeypatch.setenv('ZCODE_GATEWAY_UPDATE_NONCE', 'a' * 32)
    cfg = _cfg(tmp_path)
    cfg.server.host = '0.0.0.0'
    cfg.server.expose_ops = False
    with TestClient(create_app(cfg), client=('198.51.100.3', 12345),
                    base_url='http://127.0.0.1:8788') as client:
        assert client.get('/health').status_code == 404
        malformed = client.get('/health', headers={b'X-AutoZCoder-Update': b'\xe9'})
        assert malformed.status_code == 404
        probe = client.get('/health', headers={'X-AutoZCoder-Update': 'a' * 32})
        assert probe.status_code == 200


@pytest.mark.asyncio
async def test_locate_tears_down_scheduler_and_old_manager(tmp_path, monkeypatch):
    gen = tmp_path / 'gen'
    gen.mkdir()
    (gen / 'main.py').write_text('')
    (gen / 'config.py').write_text('')
    old = GeneratorManager(gen, data_dir=tmp_path / 'data')
    scheduler = ClaimScheduler(run=lambda: None, enabled=True,
                               state_path=tmp_path / 'schedule.json',
                               poll_seconds=1.0)
    scheduler.start()
    old.claim_scheduler = scheduler

    cfg = Config()
    cfg._config_file = tmp_path / 'config.yaml'
    cfg._config_file.write_text(yaml.safe_dump(cfg.model_dump(mode='json')))
    state = SimpleNamespace(cfg=cfg, generator=old, claim_scheduler=scheduler)
    monkeypatch.setattr(api_dash, 'get_state', lambda request: state)

    result = await api_dash.generator_locate_set(None, {'path': str(gen)}, 'admin')

    assert result['ok'] is True
    assert state.generator is None and state.claim_scheduler is None
    assert scheduler._thread is not None and not scheduler._thread.is_alive()
    assert old._closed.is_set()


@pytest.mark.asyncio
async def test_blocking_generator_and_claim_endpoints_run_off_event_loop(monkeypatch):
    main = threading.current_thread()
    calls = []

    class Manager:
        def stop_claim(self, name):
            calls.append(('stop_claim', threading.current_thread() is main))
            return True

        def remove_claim(self, name):
            calls.append(('remove_claim', threading.current_thread() is main))
            return True

        def stop_worker(self, name):
            calls.append(('stop_worker', threading.current_thread() is main))
            return True

        def stop_all(self):
            calls.append(('stop_all', threading.current_thread() is main))

        def remove_worker(self, name):
            calls.append(('remove_worker', threading.current_thread() is main))
            return True

    class Checker:
        def stop(self):
            calls.append(('checker.stop', threading.current_thread() is main))

        def status(self):
            calls.append(('checker.status', threading.current_thread() is main))
            return {'state': 'idle'}

        def run(self, action, **_kwargs):
            calls.append((f'checker.run:{action}', threading.current_thread() is main))
            return {'action': action}

    mgr = Manager()
    mgr.checker = Checker()
    monkeypatch.setattr(api_dash, '_generator', lambda request: mgr)

    assert await api_admin.claim_stop(None, {'name': 'c1'}, 'admin') == {'stopped': 'c1'}
    assert await api_admin.claim_remove(None, {'name': 'c1'}, 'admin') == {'removed': 'c1'}
    assert await api_dash.generator_stop(None, {'name': 'w1'}, 'admin') == {'stopped': True}
    assert await api_dash.generator_stopall(None, 'admin') == {'ok': True}
    assert await api_dash.generator_worker_remove(None, {'name': 'w1'}, 'admin') == {'removed': 'w1'}
    assert await api_dash.generator_proxies_stop(None, 'admin') == {'ok': True}
    assert await api_dash.generator_proxies(None, 'admin') == {'state': 'idle'}
    assert await api_dash.generator_proxies_run(None, {'action': 'recheck'}, 'admin') == {'action': 'recheck'}
    assert await api_dash.generator_proxies_maintain(None, {'action': 'prune'}, 'admin') == {'action': 'prune'}
    assert await api_dash.generator_proxies_countries(None, 'admin') == {'action': 'countries'}

    assert [name for name, _ in calls] == [
        'stop_claim', 'remove_claim', 'stop_worker', 'stop_all', 'remove_worker',
        'checker.stop', 'checker.status', 'checker.run:recheck',
        'checker.run:prune', 'checker.run:countries']
    assert not any(ran_on_loop for _, ran_on_loop in calls)


def test_record_skips_deepcopy_when_capture_is_disabled(tmp_path):
    class Body(dict):
        def __deepcopy__(self, memo):
            raise AssertionError('capture disabled: body must not be deep-copied')

    records = []
    capture = JsonlCapture(str(tmp_path / 'captures'), enabled=False)
    service = ChatService(Config(), None, None, None, None,
                          SimpleNamespace(record=lambda **rec: records.append(rec)),
                          capture)
    service._record(Account(id='dummy'), {'x': 'y'}, Body(stream=True), 200, {}, 'resp', 1.0,
                    'key', 'openai', 'GLM-5.3-Flash', {'input_tokens': 2, 'output_tokens': 3})

    assert len(records) == 1
    assert records[0]['total_tokens'] == 5 and records[0]['status'] == 200
    assert not capture.path.exists() or capture.path.read_text(encoding='utf-8') == ''

    enabled_capture = JsonlCapture(str(tmp_path / 'captures-on'), enabled=True)
    service = ChatService(Config(), None, None, None, None,
                          SimpleNamespace(record=lambda **rec: records.append(rec)),
                          enabled_capture)
    service._record(Account(id='dummy'), {'x': 'y'}, {'stream': True}, 200, {}, 'resp', 1.0,
                    'key', 'openai', 'GLM-5.3-Flash', {'input_tokens': 1})
    lines = enabled_capture.path.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 1 and '"account_id": "dummy"' in lines[0]


@pytest.mark.asyncio
async def test_update_idle_check_snapshots_manager_maps_under_lock():
    lock = threading.Lock()
    observed = []

    class LockedDict(dict):
        def values(self):
            observed.append(lock.locked())
            return super().values()

    mgr = SimpleNamespace(
        auto_generate=SimpleNamespace(status=lambda: {'status': 'idle'}),
        _claim_all_running=False,
        workers=LockedDict({'w': SimpleNamespace(done=threading.Event())}),
        claims=LockedDict(),
        checker=SimpleNamespace(alive=lambda: False),
        _lock=lock)
    state = SimpleNamespace(pool=SimpleNamespace(
        snapshot=AsyncMock(return_value={'detail': []})), generator=mgr)

    with pytest.raises(HTTPException) as error:
        await api_dash._check_update_idle(state)
    assert error.value.status_code == 409

    mgr.workers.clear()
    await api_dash._check_update_idle(state)

    assert observed and all(observed)


@pytest.mark.asyncio
async def test_proxy_auto_verify_validates_interval(tmp_path, monkeypatch):
    checker = ProxyChecker(tmp_path)
    monkeypatch.setattr(api_dash, '_generator',
                        lambda request: SimpleNamespace(checker=checker))

    with pytest.raises(HTTPException) as error:
        await api_admin.proxy_auto_verify(None, {'enabled': True, 'interval_min': 'abc'}, 'admin')
    assert error.value.status_code == 400
    assert 'interval_min' in error.value.detail
    assert checker.auto_verify is False

    assert await api_admin.proxy_auto_verify(
        None, {'enabled': True, 'interval_min': '12'}, 'admin') == {
            'auto_verify': True, 'interval_min': 12}
    assert await api_admin.proxy_auto_verify(
        None, {'enabled': True, 'interval_min': 9}, 'admin') == {
            'auto_verify': True, 'interval_min': 9}
    assert await api_admin.proxy_auto_verify(
        None, {'enabled': True, 'interval_min': ''}, 'admin') == {
            'auto_verify': True, 'interval_min': 9}
    await api_admin.proxy_auto_verify(None, {'enabled': False}, 'admin')
    assert checker.auto_verify is False


def test_pool_has_candidates_tolerates_non_ascii_bytes(tmp_path):
    gen = tmp_path / 'gen'
    gen.mkdir()
    mgr = GeneratorManager(gen, data_dir=tmp_path / 'data')

    # cp1252 (Windows default) and UTF-8 both reject these bytes; errors="ignore"
    # must keep the scan alive instead of escaping as UnicodeDecodeError.
    mgr.pool_file.write_bytes(b'# \x81\x8d unreadable comment\nsocks5://127.0.0.1:1080\n')
    assert mgr._pool_has_candidates() is True

    mgr.pool_file.write_bytes(b'# \x81\x8d no candidates here\n')
    assert mgr._pool_has_candidates() is False


@pytest.mark.asyncio
async def test_admin_readd_revives_parked_accounts(monkeypatch):
    dead = Account(id='dead-1', email='dead@example.test', access_token='old',
                   source='admin', state=AccountState.DEAD)
    dead.death_reason = 'token revoked'
    dead.auth_strikes = 3
    drained = Account(id='drained-1', email='drained@example.test', access_token='old',
                      source='admin', state=AccountState.EXHAUSTED, quota_strikes=2)
    pool = PoolManager(Config().pool, [dead, drained])
    state = SimpleNamespace(cfg=Config(), pool=pool)
    monkeypatch.setattr(api_admin, 'get_state', lambda request: state)

    await api_admin.add_account(None, {'access_token': 'fresh-dead', 'id': 'dead-1',
                                       'email': 'dead@example.test'}, 'admin')
    revived = await pool.find('dead-1')
    assert revived.state is AccountState.READY and revived.death_reason == ''
    assert revived.access_token == 'fresh-dead' and revived.auth_strikes == 0

    await api_admin.add_account(None, {'access_token': 'fresh-drained', 'id': 'drained-1',
                                       'email': 'drained@example.test'}, 'admin')
    revived = await pool.find('drained-1')
    assert revived.state is AccountState.READY and revived.quota_strikes == 0
    assert revived.access_token == 'fresh-drained'
    await pool.aclose()


@pytest.mark.asyncio
async def test_device_login_revives_parked_account(tmp_path, monkeypatch):
    from zcode_gateway import device_auth

    canonical = Account(id='same', access_token='old', state=AccountState.DEAD)
    canonical.death_reason = 'auth strikes'
    pool = PoolManager(Config().pool, [canonical])
    cfg = Config(accounts={'source': 'accounts_dir', 'dir': str(tmp_path)})
    checked = []
    state = SimpleNamespace(
        cfg=cfg, pool=pool,
        tokens=SimpleNamespace(check_account=AsyncMock(
            side_effect=lambda account: checked.append(account))))
    monkeypatch.setattr(api_dash, 'get_state', lambda _: state)
    gate = asyncio.Event()
    created = []

    class Login:
        def __init__(self, *args, **kwargs):
            self.state = 'starting'
            self._ready = asyncio.Event()
            self._ready.set()
            self.callback = kwargs['on_account']
            created.append(self)

        async def run(self):
            await gate.wait()

        def status(self):
            return {'state': self.state}

    monkeypatch.setattr(device_auth, 'DeviceLogin', Login)
    await api_dash.login_start(None, 'admin')
    await created[0].callback(Account(id='same', access_token='new'))
    assert canonical.state is AccountState.READY and canonical.death_reason == ''
    assert canonical.access_token == 'new' and checked == [canonical]
    gate.set()
    await created[0]._task
    await pool.aclose()


@pytest.mark.asyncio
async def test_jwt_import_revives_parked_account(tmp_path, monkeypatch):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')

    ident = '99aa3247-908c-433c-874f-616aa15d6e02'
    token = (part({'alg': 'HS256'}) + '.'
             + part({'sub': ident, 'exp': int(time.time()) + 3600}) + '.sig')
    cfg = Config(accounts={'source': 'accounts_dir', 'dir': str(tmp_path / 'accounts')})
    existing = Account(id=ident, access_token='old', state=AccountState.DEAD,
                       auth_strikes=3, quota_strikes=2)
    pool = PoolManager(cfg.pool, [existing])
    lock = asyncio.Lock()
    state = SimpleNamespace(cfg=cfg, pool=pool,
                            tokens=SimpleNamespace(_lock_for=lambda _: lock, _scan_at=1))
    monkeypatch.setattr(api_admin, 'get_state', lambda request: state)
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={'code': 0, 'data': {'plans': [], 'balances': []}}))
    monkeypatch.setattr(api_admin.httpx, 'AsyncClient',
                        lambda **kwargs: original(**kwargs, transport=transport))

    await api_admin.import_jwt_account(None, {'token': token}, 'admin')

    revived = await pool.find(ident)
    assert revived.state is AccountState.READY
    assert revived.access_token == token
    assert revived.auth_strikes == 0 and revived.quota_strikes == 0
    await pool.aclose()
