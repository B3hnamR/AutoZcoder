"""Pool acquire() blocking path (wait_seconds > 0) — previously uncovered."""

from __future__ import annotations

import asyncio
import time

import pytest

from zcode_gateway.config import PoolConfig
from zcode_gateway.pool import Account, AccountState, PoolManager


@pytest.mark.asyncio
async def test_acquire_waits_for_a_freed_slot():
    pool = PoolManager(PoolConfig(max_in_flight_per_account=1,
                                  concurrency_settle_seconds=0),
                       [Account(id="a1", access_token="t")])
    first = await pool.acquire(wait_seconds=0.0)
    assert first is not None

    async def release_soon():
        await asyncio.sleep(0.05)
        await pool.release(first)

    task = asyncio.create_task(release_soon())
    # would return None immediately with wait_seconds=0; must block until the
    # release lands and then hand back the same account
    second = await pool.acquire(wait_seconds=2.0)
    await task
    assert second is not None
    assert second.id == "a1"
    assert second.in_flight == 1


@pytest.mark.asyncio
async def test_acquire_times_out_when_no_slot_frees():
    pool = PoolManager(PoolConfig(max_in_flight_per_account=1,
                                  concurrency_settle_seconds=0),
                       [Account(id="a1", access_token="t")])
    await pool.acquire(wait_seconds=0.0)
    # nothing releases -> must give up after the wait, not hang forever
    result = await pool.acquire(wait_seconds=0.2)
    assert result is None


@pytest.mark.asyncio
async def test_concurrency_refusal_routes_to_another_account():
    pool = PoolManager(
        PoolConfig(max_in_flight_per_account=1, concurrency_hold_seconds=30),
        [Account(id="a1", access_token="t"), Account(id="a2", access_token="t")])
    first = await pool.acquire(wait_seconds=0.0)
    assert first is not None and first.id == "a1"
    await pool.release(first)
    await pool.on_concurrency_refusal(first)

    second = await pool.acquire(wait_seconds=0.0)
    assert second is not None and second.id == "a2"


@pytest.mark.asyncio
async def test_concurrency_hold_queues_instead_of_failing():
    pool = PoolManager(
        PoolConfig(max_in_flight_per_account=1, concurrency_hold_seconds=30),
        [Account(id="a1", access_token="t")])
    account = (await pool.all())[0]
    await pool.on_concurrency_refusal(account)
    assert account.state is AccountState.READY

    waiter = asyncio.create_task(pool.acquire(wait_seconds=2.0))
    await asyncio.sleep(0.05)
    assert not waiter.done()        # held, so acquire must wait, not return None

    account.concurrency_hold_until = 0.0
    picked = await waiter
    assert picked is account and picked.in_flight == 1


@pytest.mark.asyncio
async def test_concurrency_hold_escalates_then_success_clears():
    pool = PoolManager(
        PoolConfig(max_in_flight_per_account=1,
                   concurrency_hold_seconds=10,
                   concurrency_hold_max_seconds=60),
        [Account(id="a1", access_token="t")])
    account = (await pool.all())[0]

    await pool.on_concurrency_refusal(account)
    first_hold = account.concurrency_hold_until - time.time()
    assert account.concurrency_strikes == 1
    assert 9.0 < first_hold <= 10.0

    await pool.on_concurrency_refusal(account)
    second_hold = account.concurrency_hold_until - time.time()
    assert account.concurrency_strikes == 2
    assert 19.0 < second_hold <= 20.0

    await pool.on_success(account)
    assert account.concurrency_strikes == 0
    assert account.concurrency_hold_until == 0.0


@pytest.mark.asyncio
async def test_settling_account_is_not_pickable_until_window_passes():
    pool = PoolManager(
        PoolConfig(max_in_flight_per_account=1,
                   concurrency_settle_seconds=30),
        [Account(id="a1", access_token="t")])
    account = (await pool.all())[0]
    account.in_flight = 1
    await pool.release(account)     # marks the settle window

    # released but still settling: pick() must refuse, acquire() reports no
    # candidate (the pool is briefly saturated, not absent)
    assert pool._router.pick([account]) is None
    got = await pool.acquire(wait_seconds=0.0)
    assert got is None
    assert account.state is AccountState.READY
    assert account.in_flight == 0

    # after the window the account serves again
    account.settled_until = 0.0
    assert pool._router.pick([account]) is account


@pytest.mark.asyncio
async def test_settle_window_queues_instead_of_failing():
    pool = PoolManager(
        PoolConfig(max_in_flight_per_account=1,
                   concurrency_settle_seconds=30),
        [Account(id="a1", access_token="t")])
    first = await pool.acquire(wait_seconds=0.0)
    await pool.release(first)

    waiter = asyncio.create_task(pool.acquire(wait_seconds=2.0))
    await asyncio.sleep(0.05)
    assert not waiter.done()        # settling, so acquire must wait, not fail

    first.settled_until = 0.0
    picked = await waiter
    assert picked is first and picked.in_flight == 1


@pytest.mark.asyncio
async def test_zero_settle_keeps_current_behavior():
    pool = PoolManager(
        PoolConfig(max_in_flight_per_account=1,
                   concurrency_settle_seconds=0),
        [Account(id="a1", access_token="t")])
    first = await pool.acquire(wait_seconds=0.0)
    await pool.release(first)
    assert pool._router.pick([first]) is first


@pytest.mark.asyncio
async def test_new_concurrency_refusal_fails_pick_but_readmission_waits():
    """A fresh refusal must exclude the account from selection even though the
    readmission queue would let it back in — the hold wins."""
    pool = PoolManager(
        PoolConfig(max_in_flight_per_account=1,
                   concurrency_hold_seconds=60,
                   concurrency_settle_seconds=0),
        [Account(id="a1", access_token="t")])
    account = (await pool.all())[0]
    await pool.on_concurrency_refusal(account)
    # hold active, so the account is not pickable now...
    assert pool._router.pick([account]) is None
    # ...and it stays not pickable regardless of any settle window state,
    # proving the hold outlives readmission
    account.settled_until = 0.0
    assert pool._router.pick([account]) is None
    account.concurrency_hold_until = 0.0
    assert pool._router.pick([account]) is account


@pytest.mark.asyncio
async def test_concurrency_strikes_decay_after_quiet_gap():
    pool = PoolManager(
        PoolConfig(max_in_flight_per_account=1,
                   concurrency_hold_seconds=10,
                   concurrency_hold_max_seconds=60),
        [Account(id="a1", access_token="t")])
    account = (await pool.all())[0]
    await pool.on_concurrency_refusal(account)
    await pool.on_concurrency_refusal(account)
    assert account.concurrency_strikes == 2

    # a served request clears the counter, but holds can also expire with no
    # success in between: a quiet gap LONGER than the max hold decays the
    # escalation, so the next refusal starts near the base again
    account.concurrency_hold_until = time.time() - 61
    await pool.on_concurrency_refusal(account)
    # strike 3 on 10s base would be 40s; decayed to 2 it is 20s
    hold = account.concurrency_hold_until - time.time()
    assert account.concurrency_strikes == 2
    assert 9.0 < hold <= 20.0


@pytest.mark.asyncio
async def test_consecutive_refusals_inside_the_hold_still_escalate():
    pool = PoolManager(
        PoolConfig(max_in_flight_per_account=1,
                   concurrency_hold_seconds=10,
                   concurrency_hold_max_seconds=60),
        [Account(id="a1", access_token="t")])
    account = (await pool.all())[0]
    await pool.on_concurrency_refusal(account)
    await pool.on_concurrency_refusal(account)      # hold still ~10s out
    assert account.concurrency_strikes == 2
    account.concurrency_hold_until = time.time() + 5    # not expired: still chaining
    await pool.on_concurrency_refusal(account)
    assert account.concurrency_strikes == 3
    hold = account.concurrency_hold_until - time.time()
    assert 19.0 < hold <= 40.0
