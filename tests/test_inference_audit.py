"""Bounded, dummy-only regressions for AUD-006 through AUD-014 and VAL-002."""
import asyncio
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from zcode_gateway.config import Config
from zcode_gateway.pool import Account, AccountState, PoolManager
from zcode_gateway.registry import Registry
from zcode_gateway.relay_manager import RelayManager
from zcode_gateway.service import ChatService, UpstreamFailure
from zcode_gateway.store import JsonlCapture
from zcode_gateway.tokens import TokenManager
from zcode_gateway.upstream import RelayTransportError, UpstreamClient
from zcode_gateway.zcode_protocol import build_body


MODEL = 'GLM-5.3'
TOOL = {'type': 'function', 'function': {'name': 'f', 'parameters': {'type': 'object'}}}
START = {'type': 'message_start', 'message': {
    'id': 'msg_dummy', 'type': 'message', 'role': 'assistant', 'content': [],
    'usage': {'input_tokens': 1, 'output_tokens': 0}}}
STOP = {'type': 'message_stop'}


def response(*events, status=200, error=None):
    if error is not None:
        return httpx.Response(status, json={'error': error})
    raw = b''.join(('data: ' + json.dumps(e) + '\n\n').encode() for e in events)
    return httpx.Response(status, content=raw, headers={'content-type': 'text/event-stream'})


class DummyClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def send(self, headers, body, stream):
        self.calls.append((dict(headers), copy.deepcopy(body)))
        return next(self.responses)


def service_for(responses, accounts=None):
    cfg = Config()
    cfg.pool.acquire_wait_seconds = 0
    cfg.upstream.max_attempts = 2
    cfg.upstream.overload_max_attempts = 2
    pool = PoolManager(cfg.pool, accounts or [Account(id='one', access_token='dummy-one')])
    service = ChatService(cfg, pool, SimpleNamespace(refresh=AsyncMock(return_value=False)),
                          DummyClient(responses), Registry(), None, SimpleNamespace(enabled=False))
    records = []
    service._record = lambda *args: records.append(copy.deepcopy(args))
    return service, records


def token_manager(pool, plans, quotas, monkeypatch):
    manager = TokenManager.__new__(TokenManager)
    manager.cfg, manager.pool = Config(), pool
    manager._client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(
        200, json={'code': 0, 'data': {}}, request=httpx.Request('GET', 'https://dummy.invalid'))))
    monkeypatch.setattr('zcode_gateway.tokens.parse_balance', lambda data, now: (plans, quotas))
    return manager


@pytest.mark.asyncio
async def test_quota_transport_retry_and_safe_error_status(monkeypatch):
    account = Account(id='one', access_token='dummy')
    pool = PoolManager(Config().pool, [account])
    plans = [{'name': 'dummy', 'plan_id': 'p', 'status': 'active'}]
    quotas = {MODEL: {'remaining': 10}}
    manager = token_manager(pool, plans, quotas, monkeypatch)
    response = httpx.Response(200, json={'code': 0, 'data': {}},
                              request=httpx.Request('GET', 'https://dummy.invalid'))
    manager._client.get = AsyncMock(side_effect=[httpx.ReadTimeout('private credential URL'), response])
    await manager.check_account(account)
    assert manager._client.get.await_count == 2
    assert account.to_public()['quotas'] == quotas
    assert account.to_public()['balance_checked_at'] > 0
    manager._client.get = AsyncMock(side_effect=httpx.ReadTimeout('private credential URL'))
    await manager.check_account(account)
    public = account.to_public()
    assert public['quotas'] == quotas
    assert public['quota_check_error'] == 'Quota check unavailable: request timed out'
    assert 'private credential' not in public['quota_check_error']
    manager._client.get = AsyncMock(return_value=response)
    await manager.check_account(account)
    assert account.to_public()['quota_check_error'] == ''
    await pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [None, [], {'code': 0, 'data': {'plans': [None]}},
                                     {'code': 0, 'data': {'plans': [], 'balances': [None]}}])
async def test_malformed_quota_reports_error_without_erasing_values(payload):
    account = Account(id='one', access_token='dummy')
    account.notes['quotas'] = {MODEL: {'remaining': 5}}
    pool = PoolManager(Config().pool, [account])
    manager = TokenManager.__new__(TokenManager)
    manager.cfg, manager.pool = Config(), pool
    manager._client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(
        200, json=payload, request=httpx.Request('GET', 'https://dummy.invalid'))))
    await manager.check_account(account)
    public = account.to_public()
    assert public['quota_check_error'].startswith('Quota check unavailable:')
    assert public['quotas'][MODEL]['remaining'] == 5
    assert public['quota_check_attempted_at'] > 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_plan_loss_recovers_only_current_plan_death(monkeypatch):
    account = Account(id='one', access_token='dummy')
    pool = PoolManager(Config().pool, [account])
    manager = token_manager(pool, [], {}, monkeypatch)
    await manager.check_account(account)
    assert account.state is AccountState.DEAD and account.death_reason == 'plan_lost'
    plans = [{'name': 'dummy', 'plan_id': 'p', 'status': 'active'}]
    manager = token_manager(pool, plans, {MODEL: {'remaining': 10}}, monkeypatch)
    await manager.check_account(account)
    assert account.state is AccountState.READY
    await pool.kill(account, 'lost again', cause='plan_lost')
    await pool.kill(account, 'credentials revoked')
    await manager.check_account(account)
    assert account.state is AccountState.DEAD
    assert account.last_error == 'credentials revoked'
    # A zero-quota poll must not turn an auth-dead account into recoverable exhaustion.
    manager = token_manager(pool, plans, {MODEL: {'remaining': 0}}, monkeypatch)
    await manager.check_account(account)
    assert account.state is AccountState.DEAD
    assert not await pool.restore(account, plan_recovered=True)
    await pool.aclose()


@pytest.mark.asyncio
async def test_plan_recovery_checks_cause_after_waiting_for_pool_lock():
    account = Account(id='one', access_token='dummy')
    pool = PoolManager(Config().pool, [account])
    await pool.kill(account, 'lost', cause='plan_lost')
    async with pool._lock:
        killed = asyncio.create_task(pool.kill(account, 'revoked'))
        await asyncio.sleep(0)
        recovered = asyncio.create_task(pool.restore(account, plan_recovered=True))
    await asyncio.wait_for(asyncio.gather(killed, recovered), 2)
    assert account.state is AccountState.DEAD and not recovered.result()


@pytest.mark.asyncio
async def test_billing_clears_only_proven_quota_parks_and_persists(tmp_path, monkeypatch):
    account = Account(id='one', access_token='dummy')
    pool = PoolManager(Config().pool, [account])
    pool.set_caps_path(tmp_path / 'caps.json')
    reasons = {'window': ('WINDOW_CLOSED', ''), 'quota': ('MODEL_CAPPED', 'daily ZCode token quota exhausted'),
               'business': ('11113', ''), 'business2': ('1005', ''),
               'risk': ('UNUSUAL_ACTIVITY', ''), 'geo': ('GEO_BLOCKED', ''),
               'rate': ('RATE_LIMITED', ''), 'legacy_rate': ('MODEL_CAPPED', 'too many requests')}
    for model, (code, message) in reasons.items():
        await pool.cap_model(account, model, 900, reason=code, message=message)
    await pool.aclose()
    manager = token_manager(pool, [{'name': 'p', 'plan_id': 'p', 'status': 'active'}],
                            {model: {'remaining': 10} for model in reasons}, monkeypatch)
    await manager.check_account(account)
    assert set(account.model_caps) == {'risk', 'geo', 'rate', 'legacy_rate'}
    await pool.aclose()
    fresh = Account(id='one', access_token='dummy')
    restored = PoolManager(Config().pool, [fresh])
    restored.set_caps_path(pool.caps_path)
    restored.load_caps()
    assert fresh.model_caps == account.model_caps
    # Window/empty-bucket bookkeeping must not overwrite a still-active risk park.
    before = copy.deepcopy(account.model_cap_info['risk'])
    await pool.cap_model(account, 'risk', 60, reason='WINDOW_CLOSED', quota_only=True)
    assert account.model_cap_info['risk'] == before


@pytest.mark.parametrize('status', [200, 502])
def test_real_capture_contract_records_redacted_exchange(tmp_path, status):
    capture = JsonlCapture(str(tmp_path), enabled=True)
    stored = []
    service = ChatService(Config(), None, None, None, None,
                          SimpleNamespace(record=lambda **record: stored.append(record)), capture)
    token = 'dummy-secret-' + 'x' * 40
    service._record(Account(id='dummy'), {'authorization': 'Bearer ' + token, 'x-api-key': token},
                    {'model': MODEL, 'messages': []}, status, {}, 'dummy response', 12.3,
                    'dummy-client', 'anthropic', MODEL, {})
    records = [json.loads(line) for line in capture.path.read_text(encoding='utf-8').splitlines()]
    assert len(stored) == len(records) == 1
    record = records[0]
    assert record['account_id'] == 'dummy' and record['dialect'] == 'anthropic'
    assert record['model'] == MODEL and record['variant'] == 'anthropic'
    assert record['duration_ms'] == 12.3 and record['response']['status_code'] == status
    assert token not in capture.path.read_text(encoding='utf-8')


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('history', [False, True])
def test_tool_none_preserves_prohibition_or_rejects_without_mutation(native, history):
    messages = [{'role': 'user', 'content': 'hi'}]
    if history:
        messages += [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't', 'name': 'f', 'input': {}}]},
                     {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't', 'content': 'ok'}]}]
    payload = {'model': MODEL, 'messages': messages, 'tools': [TOOL], 'tool_choice': 'none'}
    if native:
        payload = {'model': MODEL, '_anthropic': {
            'messages': messages, 'tools': [{'name': 'f', 'input_schema': {'type': 'object'}}],
            'tool_choice': {'type': 'none'}}}
    before = copy.deepcopy(payload)
    if history:
        with pytest.raises(ValueError, match='tool_choice none with tool history'):
            build_body(payload)
    else:
        body = build_body(payload)
        assert 'tools' not in body and 'tool_choice' not in body
    assert payload == before


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['overload', 'fallback'])
async def test_native_pin_survives_retries_and_never_leaks(failure, monkeypatch):
    failed = response(status=529, error={'code': '1305', 'message': 'busy'}) if failure == 'overload' else (
        response(status=429, error={'code': 'rate_limit_error', 'message': 'rate'}))
    service, _ = service_for([failed, response(START, STOP)], [
        Account(id='other', access_token='dummy-other'), Account(id='pinned', access_token='dummy-pin')])
    pins = []
    acquire = service.pool.acquire

    async def track(*args, **kwargs):
        pins.append(kwargs.get('only_id'))
        return await acquire(*args, **kwargs)

    service.pool.acquire = track
    monkeypatch.setattr('zcode_gateway.service.wait_for_retry', AsyncMock())
    if failure == 'fallback':
        service.cfg.models.auto_free_fallback.enabled = True
        service.cfg.models.auto_free_fallback.chain = [MODEL, 'GLM-5.3-Flash']
    payload = {'_anthropic': {'messages': [{'role': 'user', 'content': 'hi'}],
                              'account': 'pinned', 'account_id': 'unused'}}
    before = copy.deepcopy(payload)
    handle = await asyncio.wait_for(service.open_stream(payload, model=MODEL), 2)
    try:
        assert handle.account.id == 'pinned' and pins and set(pins) == {'pinned'}
        assert len(service.client.calls) == 2
        assert all(headers['authorization'] == 'Bearer dummy-pin' for headers, _ in service.client.calls)
        assert all('account' not in body and 'account_id' not in body for _, body in service.client.calls)
        assert payload == before
    finally:
        await service._close(handle)
        await service.pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata', [{}, {'tag': 'keep'}, {'user_id': 'caller-owned', 'tag': 'keep'}])
async def test_rotated_metadata_matches_each_accounts_headers(metadata):
    accounts = [Account(id='one', access_token='dummy-one', notes={'device_mid': 'device-one'}),
                Account(id='two', access_token='dummy-two', notes={'device_mid': 'device-two'})]
    service, _ = service_for([response(status=500, error={'code': 'dummy', 'message': 'failed'}),
                              response(START, STOP)], accounts)
    payload = {'messages': [{'role': 'user', 'content': 'hi'}], 'metadata': metadata}
    handle = await asyncio.wait_for(service.open_stream(payload, model=MODEL), 2)
    try:
        assert handle.account.id == 'two'
        for account, (headers, body) in zip(accounts, service.client.calls):
            assert headers['authorization'] == 'Bearer ' + account.access_token
            if 'user_id' in metadata:
                assert body['metadata'] == metadata
            else:
                identity = json.loads(body['metadata']['user_id'])
                assert identity['device_id'] == account.notes['device_mid']
                assert identity['session_id'] == headers['x-session-id']
                assert {k: v for k, v in body['metadata'].items() if k != 'user_id'} == metadata
        assert 'user_id' in metadata or 'user_id' not in payload['metadata']
    finally:
        await service._close(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,headers,code,forwarded', [
    (529, {}, '1305', True), (503, {}, '1305', True),
    (502, {'x-relay-hop': 'zcode-relay'}, 'dummy', True),
    (529, {'x-relay-reject': '1'}, '1305', False), (502, {}, 'dummy', False)])
async def test_relay_forwarded_failures_are_not_transport_faults(status, headers, code, forwarded):
    client = UpstreamClient.__new__(UpstreamClient)
    client.cfg, client.pacing = Config().upstream, None
    failed, healthy = [], []
    client.relay_manager = SimpleNamespace(
        get_relay_count=lambda: 1, pick_relay_for_model=lambda model: {'url': 'https://relay.invalid'},
        mark_relay_failed=failed.append, mark_relay_ok=healthy.append)
    raw = httpx.Response(status, headers=headers, json={'error': {'code': code, 'message': 'busy'}})
    client._client = SimpleNamespace(build_request=lambda *a, **kw: object(), send=AsyncMock(return_value=raw))
    if forwarded:
        result = await asyncio.wait_for(client.send({}, {'model': MODEL}, True), 2)
        assert result.status_code == status and json.loads(await result.aread())['error']['code'] == code
        assert not failed and healthy == ['https://relay.invalid']
        await result.aclose()
    else:
        with pytest.raises(RelayTransportError):
            await asyncio.wait_for(client.send({}, {'model': MODEL}, True), 2)
        assert failed == ['https://relay.invalid']


@pytest.mark.asyncio
@pytest.mark.parametrize('relayed', [False, True])
async def test_direct_and_relay_overload_reach_same_bounded_retry(relayed, monkeypatch):
    responses = [response(status=529, error={'code': '1305', 'message': 'busy'}), response(START, STOP)]
    service, _ = service_for([])
    client = UpstreamClient.__new__(UpstreamClient)
    client.cfg, client.pacing, client.relay_manager = service.cfg.upstream, None, None
    failed = []
    if relayed:
        responses[0].headers['x-relay-hop'] = 'zcode-relay'
        client.relay_manager = SimpleNamespace(
            get_relay_count=lambda: 1, pick_relay_for_model=lambda model: {'url': 'https://relay.invalid'},
            mark_relay_failed=failed.append, mark_relay_ok=lambda url: None)
    sends = AsyncMock(side_effect=responses)
    client._client = SimpleNamespace(build_request=lambda *a, **kw: object(), send=sends)
    service.client = client
    waits = AsyncMock()
    monkeypatch.setattr('zcode_gateway.service.wait_for_retry', waits)
    handle = await asyncio.wait_for(service.open_stream({'messages': [{'role': 'user', 'content': 'hi'}]},
                                                       model=MODEL), 2)
    try:
        assert sends.await_count == 2 and waits.await_count == 1 and not failed
        assert handle.account.state is AccountState.READY
    finally:
        await service._close(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['all', 'single'])
@pytest.mark.parametrize('delete', [False, True])
async def test_slow_health_result_merges_only_into_current_settings(tmp_path, monkeypatch, operation, delete):
    manager = RelayManager(tmp_path)
    entry = manager.add_relay('one', 'https://one.invalid')
    other = manager.add_relay('two', 'https://two.invalid')
    entered, release = threading.Event(), threading.Event()

    def ping(url, **kwargs):
        if url == entry['url']:
            entered.set()
            assert release.wait(3), 'test ping was not released'
        return {'ok': True, 'status': 200, 'elapsed_ms': 1}

    monkeypatch.setattr('zcode_gateway.relay_manager.ping_relay', ping)
    call = manager.ping_all if operation == 'all' else lambda: manager.test_relay_by_id(entry['id'])
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
        manager.update_options(enabled=False, models=['different'], active_id=other['id'], strategy='random')
        if delete:
            assert manager.delete_relay(entry['id'])
        release.set()
        await asyncio.wait_for(task, 3)
        settings = manager.get_settings()
        assert settings['enabled'] is False and settings['models'] == ['different']
        assert settings['active_id'] == other['id'] and settings['strategy'] == 'random'
        assert any(r['id'] == entry['id'] for r in settings['relays']) is not delete
        settings['relays'].clear()
        assert manager.get_settings()['relays']
        assert RelayManager(tmp_path).get_settings() == manager.get_settings()
    finally:
        release.set()
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_shutdown_joins_writer_then_flushes_latest_snapshot(tmp_path):
    account = Account(id='one', access_token='dummy')
    pool = PoolManager(Config().pool, [account])
    pool.set_caps_path(tmp_path / 'caps.json')
    pool._CAPS_SAVE_DEBOUNCE_S = 0
    entered, release = threading.Event(), threading.Event()
    original = pool._write_caps_snapshot
    active, peak, writes = 0, 0, []

    def gated(out):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            if not writes:
                entered.set()
                assert release.wait(3), 'writer was not released'
            writes.append(copy.deepcopy(out))
            return original(out)
        finally:
            active -= 1

    pool._write_caps_snapshot = gated
    close = None
    try:
        await pool.cap_model(account, 'old', 900, reason='WINDOW_CLOSED')
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
        await pool.clear_quota_cap(account, 'old')
        await pool.cap_model(account, 'latest', 900, reason='GEO_BLOCKED')
        close = asyncio.create_task(pool.aclose())
        await asyncio.sleep(0)
        assert not close.done()
        release.set()
        await asyncio.wait_for(close, 3)
        assert peak == 1 and active == 0 and len(writes) == 2
        assert set(json.loads(pool.caps_path.read_text(encoding='utf-8'))['one']) == {'latest'}
        assert pool._caps_write_task.done() and pool._caps_save_task.done() and not pool._caps_dirty
    finally:
        release.set()
        await asyncio.wait_for(close if close is not None else pool.aclose(), 3)


@pytest.mark.asyncio
async def test_failed_shutdown_write_keeps_dirty_state_for_retry(tmp_path):
    account = Account(id='one', access_token='dummy')
    pool = PoolManager(Config().pool, [account])
    pool.set_caps_path(tmp_path / 'caps.json')
    writer = pool._write_caps_snapshot
    pool._write_caps_snapshot = lambda out: False
    await pool.cap_model(account, MODEL, 900, reason='GEO_BLOCKED')
    await asyncio.wait_for(pool.aclose(), 2)
    assert pool._caps_dirty and pool._caps_write_task.done()
    pool._write_caps_snapshot = writer
    await asyncio.wait_for(pool.aclose(), 2)
    assert not pool._caps_dirty and MODEL in json.loads(pool.caps_path.read_text())['one']


@pytest.mark.asyncio
@pytest.mark.parametrize('terminated', [False, True])
async def test_native_completion_rejects_bare_error_after_message_start(terminated):
    events = [START, {'error': {'code': '1305', 'message': 'dummy overload'}}]
    if terminated:
        events.append(STOP)
    service, records = service_for([response(*events)])
    with pytest.raises(UpstreamFailure) as failure:
        await asyncio.wait_for(service.complete({'messages': [{'role': 'user', 'content': 'hi'}]},
                                                model=MODEL, dialect='anthropic'), 2)
    assert failure.value.status == 529 and failure.value.code == '1305'
    assert len(service.client.calls) == 1 and records[-1][3] == 529
    account = await service.pool.find('one')
    assert account.state is AccountState.READY and account.in_flight == 0


@pytest.mark.asyncio
async def test_single_asyncio_cancellation_during_open_releases_reservation():
    # VAL-003 diagnostic: ordinary asyncio cancellation alone is not evidence
    # for repeated AnyIO cancel-scope interruption; do not infer that result here.
    service, _ = service_for([])
    entered = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    service.client.send = send
    task = asyncio.create_task(service.open_stream({'messages': [{'role': 'user', 'content': 'hi'}]}, model=MODEL))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert (await service.pool.find('one')).in_flight == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_level_cancellation_during_open_releases_reservation():
    import anyio
    service, _ = service_for([])
    entered = anyio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await anyio.sleep_forever()

    service.client.send = send
    async with anyio.create_task_group() as group:
        async def opening():
            await service.open_stream({"messages": [{"role": "user", "content": "hi"}]}, model=MODEL)
        group.start_soon(opening)
        await entered.wait()
        group.cancel_scope.cancel()
    assert (await service.pool.find("one")).in_flight == 0
