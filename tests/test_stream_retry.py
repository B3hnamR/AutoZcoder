"""Mid-stream failures: the real cause is logged, and a stream that dies before
any content reached the client is re-opened transparently (never replayed)."""
import logging
from types import SimpleNamespace

import httpx
import pytest

from zcode_gateway.config import ClientKey, Config
from zcode_gateway.pool import Account, PoolManager
from zcode_gateway.registry import Registry
from zcode_gateway.service import ChatService, UpstreamFailure
from zcode_gateway.store import JsonlCapture, Store
from tests_fixtures import FakeClient, FakeResponse, frame, sample_events, sample_sse

REQ = {'model': 'GLM-5.3', 'messages': [{'role': 'user', 'content': 'hi'}]}
LOGGER = 'zcode_gateway.service'


class CutResponse(FakeResponse):
    """Delivers `body`, then the connection breaks (like a reset mid-response)."""

    def __init__(self, body, relay_url=''):
        super().__init__(body)
        self.relay_url = relay_url

    async def aiter_bytes(self):
        async for chunk in super().aiter_bytes():
            yield chunk
        try:
            raise ConnectionResetError('peer reset')
        except ConnectionResetError as exc:
            raise httpx.ReadError('') from exc


@pytest.fixture
def cfg(tmp_path):
    config = Config()
    config.accounts.dir = str(tmp_path / 'accounts')
    config.accounts.pool_file = str(tmp_path / 'pool.json')
    config.store.sqlite_path = str(tmp_path / 'gateway.db')
    config.logging.capture_dir = str(tmp_path / 'logs')
    config.logging.capture = False
    config.pool.balance_poll_seconds = 0
    config.pool.acquire_wait_seconds = 0
    config.server.admin_key = 'test-admin'
    config.server.client_keys = [ClientKey(key='test-client')]
    return config


@pytest.fixture
def service(cfg):
    pool = PoolManager(cfg.pool, [Account(id='one', access_token='test-token')])
    store = Store(cfg.store.sqlite_path)

    class FakeTokens:
        async def refresh(self, account, force=False):
            return False

    svc = ChatService(cfg, pool, FakeTokens(), FakeClient(), Registry(), store,
                      JsonlCapture(cfg.logging.capture_dir, False))
    yield svc
    store.close()


PRELUDE_ONLY = frame(sample_events()[0])                      # message_start, then nothing
WITH_CONTENT = b''.join(frame(e) for e in sample_events()[:3])  # + thinking block started


async def run_stream(service, anthropic=False):
    h = await service.open_stream(dict(REQ), model='GLM-5.3')
    out = b''.join([x async for x in service.stream_from(
        h, model='GLM-5.3', dialect='anthropic' if anthropic else 'openai', anthropic=anthropic)])
    return h, out


async def idle(service):
    return (await service.pool.all())[0].in_flight == 0


@pytest.mark.asyncio
async def test_truncation_before_content_is_retried_invisibly(service, caplog):
    service.client = FakeClient(responses=[FakeResponse(PRELUDE_ONLY), FakeResponse(sample_sse())])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        h, out = await run_stream(service, anthropic=True)
    assert len(service.client.calls) == 2
    assert b'hello' in out and b'truncated_stream' not in out and b'event: error' not in out
    assert out.count(b'event: message_start') == 1            # the client sees ONE message
    assert out.count(b'event: message_stop') == 1
    assert h.closed and await idle(service)
    assert all(r.closed for r in service.client.responses)
    log = caplog.text
    assert 'stream failed mid-flight' in log and 'account=one' in log
    assert 'relay=direct' in log and 'content_sent=False' in log
    assert 'connection closed cleanly before message_stop' in log
    assert 'retrying transparently' in log


@pytest.mark.asyncio
async def test_openai_dialect_retry_emits_one_role_chunk_and_one_done(service):
    service.client = FakeClient(responses=[FakeResponse(PRELUDE_ONLY), FakeResponse(sample_sse())])
    _, out = await run_stream(service, anthropic=False)
    assert len(service.client.calls) == 2
    assert out.count(b'data: [DONE]') == 1
    assert out.count(b'"role":"assistant"') == 1
    assert b'hello' in out


@pytest.mark.asyncio
async def test_connection_cut_before_content_is_retried_and_cause_logged(service, caplog):
    service.client = FakeClient(responses=[CutResponse(PRELUDE_ONLY), FakeResponse(sample_sse())])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _, out = await run_stream(service, anthropic=True)
    assert len(service.client.calls) == 2 and b'hello' in out and b'event: error' not in out
    # the REAL exception, with its cause — not a generic "stream interrupted"
    assert 'ReadError' in caplog.text and 'ConnectionResetError: peer reset' in caplog.text
    assert await idle(service)


@pytest.mark.asyncio
async def test_failure_after_content_is_never_replayed(service, caplog):
    service.client = FakeClient(responses=[FakeResponse(WITH_CONTENT), FakeResponse(sample_sse())])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        h, out = await run_stream(service, anthropic=True)
    assert len(service.client.calls) == 1                      # no second attempt
    assert b'truncated_stream' in out                          # client is told, as before
    assert out.count(b'event: message_start') == 1
    assert 'content_sent=True' in caplog.text and 'retrying transparently' not in caplog.text
    assert h.closed and await idle(service)


@pytest.mark.asyncio
async def test_retries_are_bounded(service):
    service.client = FakeClient(responses=[FakeResponse(PRELUDE_ONLY) for _ in range(5)])
    _, out = await run_stream(service, anthropic=True)
    assert len(service.client.calls) == 1 + service.STREAM_RETRIES
    assert b'truncated_stream' in out
    assert await idle(service)


@pytest.mark.asyncio
async def test_failed_reopen_reports_the_original_error(service, caplog):
    service.client = FakeClient(responses=[FakeResponse(PRELUDE_ONLY)])   # nothing to retry with
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _, out = await run_stream(service, anthropic=True)
    assert b'truncated_stream' in out
    assert 'could not open a new upstream stream' in caplog.text
    assert await idle(service)


@pytest.mark.asyncio
async def test_upstream_error_event_is_logged_not_retried(service, caplog):
    err = frame({'type': 'error', 'error': {'type': 'api_error', 'message': 'oops'}})
    service.client = FakeClient(responses=[FakeResponse(PRELUDE_ONLY + err), FakeResponse(sample_sse())])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _, out = await run_stream(service, anthropic=True)
    assert len(service.client.calls) == 1 and b'oops' in out
    assert 'stream failed mid-flight' in caplog.text and 'api_error: oops' in caplog.text


@pytest.mark.asyncio
async def test_failure_log_names_the_relay(service, caplog):
    service.client = FakeClient(responses=[
        CutResponse(PRELUDE_ONLY, relay_url='https://v9.vercel.app'), FakeResponse(sample_sse())])
    service.client.relay_manager = SimpleNamespace(
        get_settings=lambda: {'relays': [{'name': 'vercel9', 'url': 'https://v9.vercel.app'}]})
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await run_stream(service, anthropic=True)
    assert 'relay=vercel9 (v9.vercel.app)' in caplog.text


@pytest.mark.asyncio
async def test_nonstream_truncation_is_retried(service, caplog):
    service.client = FakeClient(responses=[FakeResponse(WITH_CONTENT), FakeResponse(sample_sse())])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = await service.complete(dict(REQ), model='GLM-5.3', dialect='anthropic')
    assert len(service.client.calls) == 2 and b'hello' in result.raw
    assert 'retrying transparently' in caplog.text
    assert all(r.closed for r in service.client.responses) and await idle(service)


@pytest.mark.asyncio
async def test_nonstream_cut_connection_is_retried(service):
    service.client = FakeClient(responses=[CutResponse(WITH_CONTENT), FakeResponse(sample_sse())])
    result = await service.complete(dict(REQ), model='GLM-5.3', dialect='anthropic')
    assert len(service.client.calls) == 2 and b'hello' in result.raw
    assert await idle(service)


@pytest.mark.asyncio
async def test_nonstream_gives_up_with_the_original_error(service):
    service.client = FakeClient(responses=[FakeResponse(WITH_CONTENT) for _ in range(5)])
    with pytest.raises(UpstreamFailure) as err:
        await service.complete(dict(REQ), model='GLM-5.3', dialect='anthropic')
    assert err.value.code == 'truncated_stream'
    assert len(service.client.calls) == 1 + service.STREAM_RETRIES
    assert await idle(service)


@pytest.mark.asyncio
async def test_retry_keeps_the_anthropic_account_pin(cfg, service):
    """open_stream pops the account pin from the payload; the retry must still
    honour it (it is re-opened from a snapshot, not from the mutated payload)."""
    payload = {'model': 'GLM-5.3', '_anthropic': {**REQ, 'account': 'one'}}
    service.client = FakeClient(responses=[FakeResponse(PRELUDE_ONLY), FakeResponse(sample_sse())])
    h = await service.open_stream(payload, model='GLM-5.3', dialect='anthropic', variant='anthropic')
    out = b''.join([x async for x in service.stream_from(h, model='GLM-5.3', dialect='anthropic', anthropic=True)])
    assert len(service.client.calls) == 2 and b'hello' in out


# --------------------------------------------------------------------------- #
# a relay whose exit IP z.ai flagged (405/3012) must be a clean error, not a crash
# --------------------------------------------------------------------------- #

class _BlockedRelayClient:
    """The upstream client when every relay's exit IP is flagged by z.ai."""
    calls = 0

    async def send(self, headers, body, stream):
        from zcode_gateway.upstream import UpstreamBlockThroughRelay
        type(self).calls += 1
        raise UpstreamBlockThroughRelay("relay https://relay.example.dev exit IP flagged (405/3012)")


@pytest.mark.asyncio
async def test_flagged_relay_ip_is_a_clean_405_not_an_unhandled_crash(service):
    service.client = _BlockedRelayClient()
    with pytest.raises(UpstreamFailure) as err:
        await service.open_stream(dict(REQ), model='GLM-5.3')
    assert err.value.status == 405 and err.value.code == '3012'
    assert 'relay exit IP' in err.value.message and 'retry shortly' in err.value.message
    assert await idle(service)                       # the account slot was released
    # the ACCOUNT is not blamed for the relay's flagged IP
    assert (await service.pool.all())[0].state.value == 'ready'


def test_flagged_relay_ip_reaches_api_clients_as_405_json(tmp_path):
    from fastapi.testclient import TestClient
    from zcode_gateway.app import create_app
    from zcode_gateway.config import Config

    cfg = Config(server={"host": "127.0.0.1", "port": 8788, "admin_key": "adm-test",
                         "client_keys": [{"key": "cli-test", "name": "local"}]},
                 accounts={"source": "accounts_dir", "dir": str(tmp_path / "accounts")},
                 store={"sqlite_path": str(tmp_path / "gw.db")},
                 update={"enabled": False}, logging={"capture": False})
    cfg.pool.balance_poll_seconds = 0
    app = create_app(cfg)
    with TestClient(app, raise_server_exceptions=True) as c:
        from zcode_gateway.pool import Account
        import asyncio
        state = app.state.app_state
        c.portal.call(state.pool.upsert, Account(id='one', access_token='tok', expires_at=4102444800000))
        state.service.client = _BlockedRelayClient()
        for stream in (False, True):
            r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer cli-test"},
                       json={"model": "GLM-5.3", "stream": stream, "messages": [{"role": "user", "content": "hi"}]})
            assert r.status_code == 405, (stream, r.status_code, r.text)
            assert "internal error" not in r.text and "3012" in r.text
