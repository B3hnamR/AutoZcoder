"""Conversation affinity: a chat stays on the account that served its first turn."""

from __future__ import annotations

import json

import pytest

from zcode_gateway.affinity import AffinityMap, affinity_key_from_payload
from zcode_gateway.config import PoolConfig
from zcode_gateway.pool import Account, PoolManager


def test_key_prefers_session_inside_metadata_user_id():
    payload = {"metadata": {"user_id": json.dumps(
        {"device_id": "dev-1", "account_uuid": "", "session_id": "sess-9"})}}
    key = affinity_key_from_payload(payload)
    assert key and len(key) == 32
    # same session -> same key, different session -> different key
    again = affinity_key_from_payload({"metadata": {"user_id": json.dumps(
        {"device_id": "dev-1", "account_uuid": "", "session_id": "sess-9"})}})
    other = affinity_key_from_payload({"metadata": {"user_id": json.dumps(
        {"device_id": "dev-1", "account_uuid": "", "session_id": "sess-10"})}})
    assert again == key
    assert other != key


def test_key_falls_back_to_top_level_fields_and_hashes():
    assert affinity_key_from_payload({"conversation_id": "c-1"}) == \
        affinity_key_from_payload({"session_id": "c-1"})
    assert affinity_key_from_payload({"user": "u-1"})
    # plain strings are hashed, not stored verbatim
    assert "c-1" not in affinity_key_from_payload({"conversation_id": "c-1"})


def test_key_empty_when_client_sends_nothing():
    assert affinity_key_from_payload({}) == ""
    assert affinity_key_from_payload({"metadata": {}}) == ""
    assert affinity_key_from_payload({"user": "  "}) == ""


def test_map_expires_and_drops():
    now = [1000.0]
    amap = AffinityMap(ttl_seconds=60, clock=lambda: now[0])
    amap.bind("k", "a1")
    assert amap.get("k") == "a1"
    now[0] += 61
    assert amap.get("k") is None
    amap.bind("k", "a1")
    amap.drop_account("a1")
    assert amap.get("k") is None


@pytest.mark.asyncio
async def test_acquire_reuses_bound_account():
    pool = PoolManager(PoolConfig(strategy="round_robin", conversation_affinity_seconds=600),
                       [Account(id="a1", access_token="t1"),
                        Account(id="a2", access_token="t2")])
    key = affinity_key_from_payload({"session_id": "conv-1"})
    pool._affinity.bind(key, "a2")

    picked = await pool.acquire(affinity_key=key)
    assert picked is not None and picked.id == "a2"
    await pool.release(picked)


@pytest.mark.asyncio
async def test_acquire_binds_on_first_pick_then_sticks():
    pool = PoolManager(PoolConfig(strategy="round_robin", conversation_affinity_seconds=600),
                       [Account(id="a1", access_token="t1"),
                        Account(id="a2", access_token="t2")])
    key = affinity_key_from_payload({"session_id": "conv-2"})

    first = await pool.acquire(affinity_key=key)
    assert first is not None
    bound = first.id
    await pool.release(first)

    second = await pool.acquire(affinity_key=key)
    assert second is not None and second.id == bound
    await pool.release(second)


@pytest.mark.asyncio
async def test_stale_binding_falls_back_to_a_candidate():
    pool = PoolManager(PoolConfig(strategy="round_robin", conversation_affinity_seconds=600),
                       [Account(id="a1", access_token="t1")])
    key = affinity_key_from_payload({"session_id": "conv-3"})
    pool._affinity.bind(key, "gone")

    picked = await pool.acquire(affinity_key=key)
    assert picked is not None and picked.id == "a1"
    await pool.release(picked)


@pytest.mark.asyncio
async def test_affinity_disabled_ignores_binding():
    pool = PoolManager(PoolConfig(strategy="round_robin", conversation_affinity_seconds=0),
                       [Account(id="a1", access_token="t1"),
                        Account(id="a2", access_token="t2")])
    key = affinity_key_from_payload({"session_id": "conv-4"})
    pool._affinity.bind(key, "a2")

    first = await pool.acquire(affinity_key=key)
    assert first is not None
    await pool.release(first)
    assert not pool._affinity.enabled()


@pytest.mark.asyncio
async def test_snapshot_reports_affinity_size():
    pool = PoolManager(PoolConfig(conversation_affinity_seconds=600),
                       [Account(id="a1", access_token="t1")])
    key = affinity_key_from_payload({"session_id": "conv-5"})
    await pool.acquire(affinity_key=key)
    snap = await pool.snapshot()
    assert snap["affinity"]["bound_conversations"] == 1
    assert snap["affinity"]["enabled"] is True
