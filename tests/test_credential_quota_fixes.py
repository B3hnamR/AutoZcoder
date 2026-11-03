"""Offline regressions for credential adoption and quota request consistency."""
import asyncio
import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from zcode_gateway.accounts_dir import parse_snapshot, write_snapshot
from zcode_gateway.config import Config
from zcode_gateway.pool import Account, AccountState, PoolManager, Router, load_from_providers_json
from zcode_gateway.quota import parse_balance
from zcode_gateway.tokens import TokenManager


MODEL = 'GLM-5.3'


def balance(remaining=10):
    return {'plans': [{'user_plan_id': 'plan', 'status': 'active', 'name': 'Dummy Plan'}],
            'balances': [{'user_plan_id': 'plan', 'show_name': MODEL,
                          'available_units': remaining, 'total_units': 100,
                          'period_end': time.time() + 3600}]}


def response(status=200, data=None):
    return httpx.Response(status, json={'code': 0, 'data': balance() if data is None else data},
                          request=httpx.Request('GET', 'https://dummy.invalid/balance'))


def manager_for(account, get):
    cfg = Config()
    pool = PoolManager(cfg.pool, [account])
    manager = TokenManager.__new__(TokenManager)
    manager.cfg, manager.pool, manager._locks = cfg, pool, {}
    manager._client = SimpleNamespace(get=get)
    manager._found_accounts = AsyncMock(return_value=[])
    return manager, pool


@pytest.mark.asyncio
@pytest.mark.parametrize('result', ['401', 'success', 'empty', 'api_error', 'http_error', 'transport_error'])
async def test_stale_billing_result_cannot_mutate_rotated_credentials(result):
    started, release = asyncio.Event(), asyncio.Event()

    async def get(*args, **kwargs):
        started.set()
        await release.wait()
        if result == 'transport_error':
            raise httpx.ReadTimeout('dummy failure')
        if result == 'api_error':
            return httpx.Response(200, json={'code': 123, 'data': {}},
                                  request=httpx.Request('GET', 'https://dummy.invalid'))
        return response(401 if result == '401' else 500 if result == 'http_error' else 200,
                        {} if result == 'empty' else balance(0))

    account = Account(id='dummy', access_token='old', notes={'quotas': {MODEL: {'remaining': 4}}})
    manager, pool = manager_for(account, get)
    pending = asyncio.create_task(manager.check_account(account))
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(manager.apply_credentials(account, access_token='new',
                                                     device_mid='new-device'), 1)
    account.notes.update(quotas={MODEL: {'remaining': 99}}, quota_check_error='new result')
    account.balance_checked_at = 123
    release.set()
    assert await asyncio.wait_for(pending, 1) is None
    assert account.access_token == 'new'
    assert account.state is AccountState.READY
    assert account.auth_strikes == 0
    assert account.notes['quotas'][MODEL]['remaining'] == 99
    assert account.notes['quota_check_error'] == 'new result'
    assert account.balance_checked_at == 123
    assert not account.model_caps
    manager._found_accounts.assert_not_awaited()
    await pool.aclose()


@pytest.mark.asyncio
async def test_latest_quota_request_wins_even_with_same_token():
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def get(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return response(data=balance(0))
        return response(data=balance(80))

    account = Account(id='dummy', access_token='dummy-token')
    manager, pool = manager_for(account, get)
    older = asyncio.create_task(manager.check_account(account))
    await asyncio.wait_for(started.wait(), 1)
    result = await manager.check_account(account)
    assert result['balances'][0]['available_units'] == 80
    checked = account.balance_checked_at
    release.set()
    await asyncio.wait_for(older, 1)
    assert account.notes['quotas'][MODEL]['remaining'] == 80
    assert account.balance_checked_at == checked
    assert account.state is AccountState.READY
    assert not account.model_caps
    await pool.aclose()


@pytest.mark.asyncio
async def test_billing_401_keeps_three_strike_policy_without_recursive_lock():
    account = Account(id='dummy', access_token='dummy-token')
    manager, pool = manager_for(account, AsyncMock(return_value=response(401)))
    for strike in range(1, 4):
        await asyncio.wait_for(manager.check_account(account), 1)
        assert account.auth_strikes == strike
        assert account.state is (AccountState.DEAD if strike == 3 else AccountState.COOLING)
    await pool.aclose()


@pytest.mark.asyncio
async def test_successful_billing_resets_auth_strikes():
    account = Account(id='dummy', access_token='dummy-token', auth_strikes=2)
    manager, pool = manager_for(account, AsyncMock(return_value=response()))
    await manager.check_account(account)
    assert account.auth_strikes == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_billing_401_adopts_rotated_disk_credentials_without_auth_strike():
    account = Account(id='dummy', access_token='old')
    manager, pool = manager_for(account, AsyncMock(return_value=response(401)))
    manager._found_accounts.return_value = [Account(id='dummy', access_token='new',
                                                   notes={'device_mid': 'new-device'})]
    await asyncio.wait_for(manager.check_account(account), 1)
    assert account.access_token == 'new'
    assert account.notes['device_mid'] == 'new-device'
    assert account.state is AccountState.READY
    assert account.auth_strikes == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_upsert_and_removed_account_invalidate_inflight_quota():
    started, release = asyncio.Event(), asyncio.Event()

    async def get(*args, **kwargs):
        started.set()
        await release.wait()
        return response(data=balance(0))

    account = Account(id='dummy', access_token='same-token')
    manager, pool = manager_for(account, get)
    pending = asyncio.create_task(manager.check_account(account))
    await asyncio.wait_for(started.wait(), 1)
    await pool.upsert(Account(id='dummy', access_token='same-token', notes={'name': 'new'}))
    release.set()
    assert await asyncio.wait_for(pending, 1) is None
    assert 'quotas' not in account.notes
    started.clear()
    release.clear()
    pending = asyncio.create_task(manager.check_account(account))
    await asyncio.wait_for(started.wait(), 1)
    await pool.remove(account.id)
    replacement = Account(id='dummy', access_token='replacement')
    await pool.upsert(replacement)
    release.set()
    await asyncio.wait_for(pending, 1)
    assert not account.model_caps and not replacement.model_caps
    assert 'quotas' not in replacement.notes
    await pool.aclose()


@pytest.mark.asyncio
async def test_stale_auth_failure_guard_and_lock_survives_remove():
    account = Account(id='dummy', access_token='old', auth_strikes=2)
    manager, pool = manager_for(account, AsyncMock())
    credentials = account.credential_key()
    lock = manager._lock_for(account.id)
    manager.drop_lock(account.id)
    assert manager._lock_for(account.id) is lock
    await manager.apply_credentials(account, access_token='new', refresh_token='refresh')
    await pool.auth_failed(account, 'stale response', expected_credentials=credentials)
    assert account.state is AccountState.READY
    assert account.auth_strikes == 0
    assert await manager.refresh(account, force=True, expected_credentials=credentials)
    manager._found_accounts.assert_not_awaited()
    await pool.aclose()


@pytest.mark.asyncio
async def test_quota_probe_resolves_canonical_object_after_upsert():
    canonical = Account(id='dummy', access_token='old')
    manager, pool = manager_for(canonical, AsyncMock(return_value=response()))
    orphan = Account(id='dummy', access_token='new')
    await pool.upsert(orphan)
    await manager.check_account(orphan)
    assert canonical.notes['quotas'][MODEL]['remaining'] == 10
    assert canonical.balance_checked_at > 0
    assert 'quotas' not in orphan.notes
    await pool.aclose()


@pytest.mark.asyncio
async def test_pool_guards_auth_and_quota_transitions_after_waiting_for_lock():
    account = Account(id='dummy', access_token='old', auth_strikes=2)
    manager, pool = manager_for(account, AsyncMock())
    credentials = account.credential_key()
    async with pool._lock:
        update = asyncio.create_task(pool.upsert(Account(id='dummy', access_token='new')))
        await asyncio.sleep(0)
        stale = [asyncio.create_task(call) for call in (
            pool.auth_failed(account, 'old rejection', expected_credentials=credentials),
            pool.kill(account, 'old empty quota', cause='plan_lost', expected_credentials=credentials),
            pool.retire(account, paid_only=False, expected_credentials=credentials),
            pool.cap_model(account, MODEL, 600, expected_credentials=credentials))]
    await asyncio.wait_for(asyncio.gather(update, *stale), 1)
    assert account.access_token == 'new' and account.state is AccountState.READY
    assert account.auth_strikes == 0 and not account.model_caps
    await pool.aclose()


def jwt(iat):
    payload = base64.urlsafe_b64encode(json.dumps({'iat': iat}).encode()).decode().rstrip('=')
    return 'workos:dummy.' + payload + '.dummy'


@pytest.mark.asyncio
async def test_refresh_adopts_no_exp_token_metadata_and_preserves_snapshot_fields(tmp_path):
    path = tmp_path / 'dummy.txt'
    old, new = jwt(10), jwt(20)
    write_snapshot(path, {'account_id': 'dummy', 'email': 'new@example.invalid',
        'access_token': new, 'refresh_token': 'new-refresh', 'expires_at_ms': '0',
        'device_mid': 'new-device', 'name': 'New Name', 'session_id': 'keep-session',
        'workos_user_id': 'keep-user', 'captured_at': 'keep-date', 'provider_slot': 'keep-slot'})
    account = Account(id='dummy', access_token=old, expires_at=int((time.time() + 3600) * 1000),
                      refresh_token='old-refresh', email='old@example.invalid',
                      notes={'file': path.name, 'device_mid': 'old-device'})
    manager, pool = manager_for(account, AsyncMock())
    manager.cfg.accounts.dir = str(tmp_path)
    fresh = Account(id='dummy', access_token=new, refresh_token='new-refresh',
                    email='new@example.invalid', notes={'file': path.name,
                    'name': 'New Name', 'device_mid': 'new-device'})
    manager._found_accounts.return_value = [fresh]
    assert await manager.refresh(account, force=True)
    assert account.access_token == new and account.expires_at == 0
    assert account.refresh_token == 'new-refresh'
    assert account.email == 'new@example.invalid'
    assert account.notes['name'] == 'New Name'
    assert account.notes['device_mid'] == 'new-device'
    fields = parse_snapshot(path)
    assert fields['session_id'] == 'keep-session'
    assert fields['workos_user_id'] == 'keep-user'
    assert fields['captured_at'] == 'keep-date'
    assert fields['provider_slot'] == 'keep-slot'
    assert fields['refresh_token'] == 'new-refresh'
    assert fields['device_mid'] == 'new-device'
    await pool.aclose()


def test_refresh_persistence_does_not_overwrite_newer_external_capture(tmp_path):
    path = tmp_path / 'dummy.txt'
    write_snapshot(path, {'account_id': 'dummy', 'access_token': 'newest',
                          'device_mid': 'newest-device', 'session_id': 'keep'})
    TokenManager._persist_refreshed(path, {'account_id': 'dummy', 'access_token': 'scanned',
                                         'device_mid': 'scanned-device'}, 'old')
    fields = parse_snapshot(path)
    assert fields['access_token'] == 'newest'
    assert fields['device_mid'] == 'newest-device'
    assert fields['session_id'] == 'keep'


@pytest.mark.asyncio
@pytest.mark.parametrize('old,new,old_exp,new_exp,adopt', [
    (jwt(20), jwt(10), 0, 0, False),
    (jwt(10), jwt(20), 9_999_999_999_000, 0, True),
    ('opaque-old', 'opaque-new', 9_999_999_999_000, 0, True),
    ('opaque-old', 'opaque-new', 9_999_999_999_000, 9_999_999_998_000, False),
])
async def test_refresh_freshness_does_not_treat_missing_exp_as_old(old, new, old_exp, new_exp, adopt):
    account = Account(id='dummy', access_token=old, expires_at=old_exp)
    manager, pool = manager_for(account, AsyncMock())
    manager._found_accounts.return_value = [Account(id='dummy', access_token=new, expires_at=new_exp)]
    assert await manager.refresh(account, force=True) is adopt
    assert account.access_token == (new if adopt else old)
    await pool.aclose()


@pytest.mark.asyncio
async def test_same_token_scan_adopts_device_metadata_but_does_not_clear_auth_strikes():
    account = Account(id='dummy', access_token='same', auth_strikes=2,
                      notes={'device_mid': 'device'})
    manager, pool = manager_for(account, AsyncMock())
    manager._found_accounts.return_value = [Account(id='dummy', access_token='same',
        email='new@example.invalid', refresh_token='new-refresh',
        notes={'device_mid': 'new-device', 'name': 'New Name'})]
    assert not await manager.refresh(account, force=True)
    assert account.auth_strikes == 2
    assert account.notes['device_mid'] == 'new-device'
    assert account.refresh_token == 'new-refresh'
    assert account.email == 'new@example.invalid'
    await pool.aclose()


def test_provider_loader_preserves_device_mid(monkeypatch):
    monkeypatch.setattr('zcode_gateway.zcode_accounts.read_credentials', lambda path: [
        {'account_id': 'dummy', 'email': 'dummy@example.invalid', 'access_token': 'dummy-token',
         'refresh_token': 'dummy-refresh', 'expires_at_ms': '0', 'name': 'Dummy',
         'device_mid': 'dummy-device'}])
    account, = load_from_providers_json('dummy-unused.json')
    assert account.notes['device_mid'] == 'dummy-device'
    assert account.refresh_token == 'dummy-refresh'


@pytest.mark.parametrize('live', [0, 7])
def test_future_entitlements_preserve_live_quota_and_earliest_pending_start(live):
    data = balance(live)
    data['plans'][0]['entitlements'] = [
        {'entitlement_id': 'night-late', 'show_name': MODEL, 'grant_units': 100, 'effective_at': 3000},
        {'entitlement_id': 'night-early', 'show_name': MODEL, 'grant_units': 200, 'effective_at': 2000}]
    _, quotas = parse_balance(data, now=1000)
    quota = quotas[MODEL]
    assert quota['remaining'] == live
    assert quota['total'] == 100
    assert quota['starts_at'] == 0
    assert quota['pending_remaining'] == 300
    assert quota['pending_starts_at'] == 2000
    account = Account(id='dummy', access_token='dummy-token', notes={'quotas': quotas})
    assert account.model_eligible(MODEL, now=1000) is bool(live)


def test_only_pending_grants_use_earliest_start_without_claiming_live_capacity():
    data = balance()
    data['balances'] = []
    data['plans'][0]['entitlements'] = [
        {'show_name': MODEL, 'grant_units': 100, 'effective_at': 3000},
        {'show_name': MODEL, 'grant_units': 200, 'effective_at': 2000}]
    _, quotas = parse_balance(data, now=1000)
    quota = quotas[MODEL]
    assert quota['remaining'] == quota['total'] == 0
    assert quota['starts_at'] == quota['pending_starts_at'] == 2000
    account = Account(id='dummy', access_token='dummy-token', notes={'quotas': quotas})
    assert not account.model_eligible(MODEL, now=1000)


@pytest.mark.asyncio
async def test_live_plus_future_grant_is_not_parked_or_retired():
    data = balance(7)
    data['plans'][0]['entitlements'] = [
        {'show_name': MODEL, 'grant_units': 200, 'effective_at': time.time() + 3600}]
    account = Account(id='dummy', access_token='dummy-token')
    manager, pool = manager_for(account, AsyncMock(return_value=response(data=data)))
    await manager.check_account(account)
    assert account.notes['quotas'][MODEL]['remaining'] == 7
    assert account.model_eligible(MODEL)
    assert not account.is_model_capped(MODEL)
    assert account.state is AccountState.READY
    await pool.aclose()


@pytest.mark.asyncio
async def test_zero_live_with_future_grant_waits_until_first_future_start():
    now = time.time()
    data = balance(0)
    data['plans'][0]['entitlements'] = [
        {'show_name': MODEL, 'grant_units': 200, 'effective_at': now + 600}]
    account = Account(id='dummy', access_token='dummy-token')
    manager, pool = manager_for(account, AsyncMock(return_value=response(data=data)))
    await manager.check_account(account)
    assert account.state is AccountState.READY
    assert account.model_caps[MODEL] == pytest.approx(now + 600, abs=1)
    await pool.aclose()


def test_future_balance_buckets_do_not_hide_or_inflate_live_balance():
    data = balance(7)
    data['balances'].append({'user_plan_id': 'plan', 'show_name': MODEL,
        'available_units': 100, 'total_units': 100, 'period_start': 2000, 'period_end': 3000})
    _, quotas = parse_balance(data, now=1000)
    assert quotas[MODEL]['remaining'] == 7
    assert quotas[MODEL]['pending_remaining'] == 100
    assert quotas[MODEL]['starts_at'] == 0


def test_quota_aware_skips_saturated_highest_quota_before_ranking():
    cfg = Config().pool
    cfg.strategy, cfg.max_in_flight_per_account = 'quota_aware', 1
    best = Account(id='best', access_token='dummy-best', in_flight=1,
                   notes={'quotas': {MODEL: {'remaining': 100}}})
    next_best = Account(id='available', access_token='dummy-next',
                        notes={'quotas': {MODEL: {'remaining': 20}}})
    router = Router(cfg)
    assert router.acquire([best, next_best], set(), model=MODEL) is next_best
    assert router.acquire([best, next_best], set(), model=MODEL) is None
    assert router.candidates([best, next_best], set(), model=MODEL)


@pytest.mark.asyncio
async def test_quota_aware_waits_only_when_all_eligible_accounts_are_saturated():
    cfg = Config().pool
    cfg.strategy, cfg.max_in_flight_per_account = 'quota_aware', 1
    accounts = [Account(id='best', access_token='dummy-best', in_flight=1,
                        notes={'quotas': {MODEL: {'remaining': 100}}}),
                Account(id='next', access_token='dummy-next', in_flight=1,
                        notes={'quotas': {MODEL: {'remaining': 20}}})]
    pool = PoolManager(cfg, accounts)
    pending = asyncio.create_task(pool.acquire(model=MODEL, wait_seconds=1))
    await asyncio.sleep(0)
    await pool.release(accounts[1])
    assert await asyncio.wait_for(pending, 1) is accounts[1]
    await pool.aclose()
