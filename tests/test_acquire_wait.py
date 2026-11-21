"""Pool acquire() blocking path (wait_seconds > 0) — previously uncovered."""

from __future__ import annotations

import asyncio
import time

import pytest

from zcode_gateway.config import PoolConfig
from zcode_gateway.pool import Account, AccountState, PoolManager


@pytest.mark.asyncio
async def test_acquire_waits_for_a_freed_slot():
    pool = PoolManager(PoolConfig(max_in_flight_per_account=1),
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
    pool = PoolManager(PoolConfig(max_in_flight_per_account=1),
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
