"""Pool acquire() blocking path (wait_seconds > 0) — previously uncovered."""

from __future__ import annotations

import asyncio

import pytest

from zcode_gateway.config import PoolConfig
from zcode_gateway.pool import Account, PoolManager


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
