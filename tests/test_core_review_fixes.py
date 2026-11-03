"""Offline regressions for core stream cleanup, parsing, tools and telemetry."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest

from test_inference_audit import MODEL, START, STOP, response, service_for
from zcode_gateway.aggregate import aggregate_openai_stream
from zcode_gateway.config import Config
from zcode_gateway.pool import Account, AccountState
from zcode_gateway.service import ChatService, StreamHandle, UpstreamFailure
from zcode_gateway.tokens import TokenManager
from zcode_gateway.upstream import iter_sse_events
from zcode_gateway.zcode_protocol import OpenAITranslator


PAYLOAD = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'dummy'}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['send', 'read', 'first_event', 'handle'])
async def test_level_cancel_waits_for_full_cleanup_and_contended_release(stage):
    service, _ = service_for([])
    entered, releasing, finished = anyio.Event(), anyio.Event(), anyio.Event()
    scope = None
    cancelled = []
    account = await service.pool.find('one')
    release = service.pool.release

    async def track_release(account):
        releasing.set()
        await release(account)

    async def blocked(*args, **kwargs):
        entered.set()
        await anyio.sleep_forever()

    class PendingResponse:
        status_code = 500 if stage == 'read' else 200
        headers = {'content-type': 'text/event-stream'}
        closed = False

        aread = blocked

        async def aiter_bytes(self):
            if stage == 'handle':
                yield response(START).content
            await blocked()

        async def aclose(self):
            await anyio.lowlevel.checkpoint()
            self.closed = True

    pending_response = PendingResponse()
    if stage == 'send':
        service.client.send = blocked
    else:
        service.client.send = AsyncMock(return_value=pending_response)
    service.pool.release = track_release

    async def worker():
        nonlocal scope
        with anyio.CancelScope() as scope:
            try:
                handle = await service.open_stream(copy.deepcopy(PAYLOAD), model=MODEL)
                try:
                    entered.set()
                    await anyio.sleep_forever()
                finally:
                    await service._close(handle)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        finished.set()

    with anyio.fail_after(2):
        async with anyio.create_task_group() as group:
            group.start_soon(worker)
            await entered.wait()
            await service.pool._lock.acquire()
            try:
                scope.cancel()
                await releasing.wait()
                assert not finished.is_set()
                if stage != 'send':
                    assert pending_response.closed
            finally:
                service.pool._lock.release()
            await finished.wait()
    assert cancelled and account.in_flight == 0
    await service.pool.aclose()


@pytest.mark.asyncio
async def test_close_releases_even_when_event_and_response_closure_raise():
    service, _ = service_for([])
    account = await service.pool.acquire(set(), model=MODEL)
    events = SimpleNamespace(aclose=AsyncMock(side_effect=ValueError('event close')))
    resp = SimpleNamespace(aclose=AsyncMock(side_effect=RuntimeError('response close')))
    handle = StreamHandle(account, {}, {}, resp, 'anthropic', 0, events=events)
    with pytest.raises(RuntimeError, match='response close'):
        await service._close(handle)
    assert account.in_flight == 0
    await service._close(handle)
    assert resp.aclose.await_count == events.aclose.await_count == 1
    await service.pool.aclose()


@pytest.mark.parametrize('data', ['{"type":', 'not-json', 'null', '[]', '"ping"'])
def test_sse_rejects_corrupt_or_nonobject_data(data):
    with pytest.raises(ValueError):
        iter_sse_events('data: ' + data + '\n\n')


def test_sse_skips_only_framing_and_empty_keepalives():
    assert iter_sse_events(': keepalive\n\nevent: ping\n\ndata: \n\n'
                           'data: {\ndata: "type": "ping"\ndata: }\n\n'
                           'data: [DONE]\n\n') == [{'type': 'ping'}, '[DONE]']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('corrupt', [b'{"type":', b'{"type":"ping","text":"\xff"}'])
async def test_malformed_sse_fails_without_retry_or_silent_success(streaming, corrupt):
    raw = response(START).content + b'data: ' + corrupt + b'\n\n' + response(STOP).content
    resp = httpx.Response(200, content=raw, headers={'content-type': 'text/event-stream'})
    service, records = service_for([resp])
    if streaming:
        handle = await service.open_stream(copy.deepcopy(PAYLOAD), model=MODEL)
        raw = b''.join([frame async for frame in service.stream_from(handle, model=MODEL)])
        errors = [event['error'] for event in iter_sse_events(raw.decode())
                  if isinstance(event, dict) and 'error' in event]
        assert len(errors) == 1 and errors[0]['code'] == 'invalid_stream'
    else:
        with pytest.raises(UpstreamFailure) as failure:
            await service.complete(copy.deepcopy(PAYLOAD), model=MODEL)
        assert failure.value.status == 502 and failure.value.code == 'invalid_stream'
    assert len(service.client.calls) == 1
    assert records[-1][3] == 502 and resp.is_closed
    assert (await service.pool.find('one')).in_flight == 0
    await service.pool.aclose()


@pytest.mark.asyncio
async def test_malformed_first_event_is_reported_before_handle_transfer():
    resp = httpx.Response(200, content=b'data: {broken\n\n',
                          headers={'content-type': 'text/event-stream'})
    service, _ = service_for([resp])
    with pytest.raises(UpstreamFailure) as failure:
        await service.open_stream(copy.deepcopy(PAYLOAD), model=MODEL)
    assert failure.value.code == 'invalid_stream' and resp.is_closed
    assert (await service.pool.find('one')).in_flight == 0
    await service.pool.aclose()


@pytest.mark.parametrize('partials,expected', [([], '{}'), ([''], '{}'),
                                                (['{"x":', '1}'], '{"x":1}')])
def test_tool_empty_input_is_deferred_until_stop_without_prefixing_deltas(partials, expected):
    translator = OpenAITranslator(MODEL)
    start = {'type': 'content_block_start', 'index': 3, 'content_block': {
        'type': 'tool_use', 'id': 'tool_dummy', 'name': 'dummy', 'input': {}}}
    chunks = translator.feed(start)
    assert chunks[0]['choices'][0]['delta']['tool_calls'][0]['function']['arguments'] == ''
    for partial in partials:
        chunks.extend(translator.feed({'type': 'content_block_delta', 'index': 3,
                                      'delta': {'type': 'input_json_delta', 'partial_json': partial}}))
    chunks.extend(translator.feed({'type': 'content_block_stop', 'index': 3}))
    arguments = ''.join(chunk['choices'][0]['delta']['tool_calls'][0]['function']['arguments']
                        for chunk in chunks)
    assert arguments == expected and isinstance(json.loads(arguments), dict)
    assert translator.feed({'type': 'content_block_stop', 'index': 3}) == []


@pytest.mark.asyncio
async def test_zero_argument_tool_is_valid_in_complete_openai_response():
    service, _ = service_for([response(START, {'type': 'content_block_start', 'index': 0,
        'content_block': {'type': 'tool_use', 'id': 'dummy', 'name': 'zero', 'input': {}}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}}, STOP)])
    result = await service.complete(copy.deepcopy(PAYLOAD), model=MODEL)
    message = aggregate_openai_stream(result.raw.decode(), MODEL, 'fallback')['choices'][0]['message']
    assert message['tool_calls'][0]['function']['arguments'] == '{}'
    await service.pool.aclose()


@pytest.mark.asyncio
async def test_geo_fallback_remains_available_after_account_model_is_parked():
    service, _ = service_for([response(status=451, error={'message': 'dummy region blocked'}),
                              response(START, STOP), response(START, STOP)])
    service.cfg.models.auto_free_fallback.enabled = True
    service.cfg.models.auto_free_fallback.chain = [MODEL, 'GLM-5.3-Flash']
    assert (await service.complete(copy.deepcopy(PAYLOAD), model=MODEL)).status == 200
    assert (await service.pool.find('one')).model_cap_info[MODEL]['code'] == 'GEO_BLOCKED'
    assert (await service.complete(copy.deepcopy(PAYLOAD), model=MODEL)).status == 200
    assert [body['model'] for _, body in service.client.calls] == [MODEL, 'GLM-5.3-Flash', 'GLM-5.3-Flash']
    await service.pool.aclose()


def recording_service():
    records, captures = [], []
    service = ChatService(Config(), None, None, None, None,
                          SimpleNamespace(record=lambda **rec: records.append(rec)),
                          SimpleNamespace(write=lambda **cap: captures.append(cap)))
    return service, records, captures


def record_dummy(service):
    service._record(Account(id='dummy'), {}, {'stream': True}, 200, {}, 'dummy', 1,
                    'dummy-key', 'openai', MODEL, {'input_tokens': 2})


def test_record_remains_synchronous_without_running_loop():
    service, records, captures = recording_service()
    record_dummy(service)
    assert len(records) == len(captures) == 1
    assert records[0]['total_tokens'] == 2 and not service._pending_records


@pytest.mark.asyncio
async def test_aclose_shields_and_drains_queued_records_before_store_close(monkeypatch):
    service, records, captures = recording_service()
    loop = asyncio.get_running_loop()
    queued = []

    def enqueue(executor, fn, *args):
        future = loop.create_future()
        queued.append((fn, args, future))
        return future

    monkeypatch.setattr(loop, 'run_in_executor', enqueue)
    record_dummy(service)
    record_dummy(service)
    entered, finished = anyio.Event(), anyio.Event()

    async def shutdown():
        with anyio.CancelScope() as scope:
            scope.cancel()
            entered.set()
            await service.aclose()
            assert len(records) == len(captures) == 2
            service.store.close()
            finished.set()

    closed = []
    service.store.close = lambda: closed.append(True)
    with anyio.fail_after(2):
        async with anyio.create_task_group() as group:
            group.start_soon(shutdown)
            await entered.wait()
            assert not finished.is_set() and not closed
            for fn, args, future in queued:
                assert not future.cancelled()
                fn(*args)
                future.set_result(None)
            await finished.wait()
    assert closed == [True] and not service._pending_records
    await service.aclose()


@pytest.mark.asyncio
async def test_stale_http_401_retries_rotated_token_without_auth_strike():
    service, _ = service_for([])
    account = await service.pool.find('one')
    service.tokens = TokenManager(service.cfg, service.pool)
    old = account.credential_key()
    calls = []

    async def send(headers, body, stream):
        calls.append(headers['authorization'])
        if len(calls) == 1:
            await service.tokens.apply_credentials(account, access_token='dummy-new')
            return response(status=401, error={'code': 'authentication_error', 'message': 'old token'})
        return response(START, STOP)

    service.client.send = send
    result = await service.complete(copy.deepcopy(PAYLOAD), model=MODEL)
    assert result.status == 200 and calls == ['Bearer dummy-one', 'Bearer dummy-new']
    assert account.credential_key() != old
    assert account.auth_strikes == 0 and account.state is AccountState.READY
    assert account.in_flight == 0
    await service.tokens.aclose()
    await service.pool.aclose()


@pytest.mark.asyncio
async def test_retried_http_401_uses_credentials_of_second_send():
    service, _ = service_for([])
    service.cfg.upstream.max_attempts = 1
    account = await service.pool.find('one')
    calls, refresh_keys = [], []

    async def refresh(account, force=False, *, expected_credentials=None):
        refresh_keys.append(expected_credentials)
        account.access_token = 'dummy-second'
        account.credential_generation += 1
        return True

    async def send(headers, body, stream):
        calls.append(headers['authorization'])
        return response(status=401, error={'code': 'authentication_error', 'message': 'revoked'})

    before = account.credential_key()
    service.tokens.refresh = refresh
    service.client.send = send
    with pytest.raises(UpstreamFailure):
        await service.complete(copy.deepcopy(PAYLOAD), model=MODEL)
    assert refresh_keys == [before]
    assert calls == ['Bearer dummy-one', 'Bearer dummy-second']
    assert account.auth_strikes == 1 and account.state is AccountState.COOLING
    assert account.in_flight == 0
    await service.pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code', [(401, 'authentication_error'), (402, '11113'),
                                        (403, 'entitlement_error'), (405, '3012'),
                                        (429, 'rate_limit_error'), (451, 'http_451'), (500, 'api_error')])
async def test_stale_error_does_not_mutate_current_account(status, code):
    service, _ = service_for([])
    account = await service.pool.find('one')
    before = account.credential_key()
    account.access_token = 'dummy-rotated'
    account.credential_generation += 1
    await service._apply_error(account, MODEL, status, code, 'stale response',
                               expected_credentials=before)
    assert account.state is AccountState.READY
    assert account.auth_strikes == account.quota_strikes == account.error_count == 0
    assert not account.model_caps and not account.error_history
    await service.pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code', [(401, 'authentication_error'), (429, 'rate_limit_error'),
                                        (500, 'api_error')])
async def test_response_guard_rechecks_credentials_after_waiting_for_pool_lock(status, code):
    service, _ = service_for([])
    account = await service.pool.find('one')
    before = account.credential_key()
    async with service.pool._lock:
        task = asyncio.create_task(service._apply_error(account, MODEL, status, code, 'old response',
                                                        expected_credentials=before))
        await asyncio.sleep(0)
        account.access_token = 'dummy-rotated'
        account.credential_generation += 1
    await asyncio.wait_for(task, 2)
    assert account.state is AccountState.READY and account.auth_strikes == 0
    assert not account.model_caps
    await service.pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('kind', ['authentication_error', 'rate_limit_error', 'success'])
async def test_transferred_handle_retains_credentials_across_rotation(streaming, kind):
    service, _ = service_for([response(START)])
    handle = await service.open_stream(copy.deepcopy(PAYLOAD), model=MODEL)
    account = handle.account
    before = account.credential_key()
    assert handle.credentials == before

    async def events():
        account.access_token = 'dummy-rotated'
        account.credential_generation += 1
        account.auth_strikes = 2
        account.error_count = 2
        account.last_error = 'current credential error'
        if kind == 'success':
            yield STOP
        else:
            yield {'type': 'error', 'error': {'type': kind, 'message': 'old response'}}

    handle.events = events()
    if streaming:
        raw = b''.join([frame async for frame in service.stream_from(handle, model=MODEL)])
        assert (b'"error"' in raw) == (kind != 'success')
    else:
        service.open_stream = AsyncMock(return_value=handle)
        if kind == 'success':
            assert (await service.complete(copy.deepcopy(PAYLOAD), model=MODEL)).status == 200
        else:
            with pytest.raises(UpstreamFailure):
                await service.complete(copy.deepcopy(PAYLOAD), model=MODEL)
    assert account.auth_strikes == account.error_count == 2
    assert account.last_error == 'current credential error' and not account.model_caps
    assert account.in_flight == 0 and handle.closed
    await service.pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['expired', 'initial_sse', 'transport'])
async def test_rotation_during_open_does_not_penalize_new_credentials(failure):
    service, _ = service_for([])
    service.cfg.upstream.max_attempts = 1
    account = await service.pool.find('one')
    if failure == 'expired':
        account.expires_at = 1
    before = account.credential_key()

    def rotate():
        account.access_token = 'dummy-new'
        account.expires_at = 0
        account.credential_generation += 1

    async def refresh(account, force=False, *, expected_credentials=None):
        assert expected_credentials == before
        rotate()
        return False

    async def send(headers, body, stream):
        assert headers['authorization'] == 'Bearer dummy-one'
        rotate()
        if failure == 'transport':
            raise httpx.ReadError('dummy transport failure')
        return response({'type': 'error', 'error': {
            'type': 'authentication_error', 'message': 'old token'}})

    service.tokens.refresh = refresh
    service.client.send = send
    with pytest.raises(UpstreamFailure):
        await service.open_stream(copy.deepcopy(PAYLOAD), model=MODEL)
    assert account.state is AccountState.READY
    assert account.auth_strikes == account.error_count == 0
    assert not account.error_history and not account.model_caps
    assert account.in_flight == 0
    await service.pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_success_guard_rechecks_rotated_credentials_under_pool_lock(streaming):
    service, _ = service_for([response(START, STOP)])
    handle = await service.open_stream(copy.deepcopy(PAYLOAD), model=MODEL)
    account = handle.account
    entered = asyncio.Event()
    on_success = service.pool.on_success

    async def success(account, *, expected_credentials=None):
        assert expected_credentials == handle.credentials
        entered.set()
        await on_success(account, expected_credentials=expected_credentials)

    async def consume():
        if streaming:
            return [frame async for frame in service.stream_from(handle, model=MODEL)]
        service.open_stream = AsyncMock(return_value=handle)
        return await service.complete(copy.deepcopy(PAYLOAD), model=MODEL)

    service.pool.on_success = success
    async with service.pool._lock:
        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 2)
        account.access_token = 'dummy-new'
        account.credential_generation += 1
        account.state = AccountState.COOLING
        account.auth_strikes = 2
        account.error_count = 2
        account.last_error = 'current credential failure'
    await asyncio.wait_for(task, 2)
    assert account.state is AccountState.COOLING
    assert account.auth_strikes == account.error_count == 2
    assert account.last_error == 'current credential failure'
    assert account.in_flight == 0
    await service.pool.aclose()
