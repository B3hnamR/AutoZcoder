"""Regressions for retirement stickiness, strike-budget resets, the edge-block
callback, captcha ticket reuse and notification dedupe ordering."""
from __future__ import annotations

import time

import pytest

from zcode_gateway import notify
from zcode_gateway.breaker import CLEAN_AFTER_S, EdgeBlockBreaker
from zcode_gateway.captcha_tickets import CaptchaTicket, CaptchaTicketPool
from zcode_gateway.config import Config
from zcode_gateway.pool import Account, AccountState, PoolManager, Router
from zcode_gateway.tokens import TokenManager

MODEL = "GLM-5.3"
EDGE_403 = ("<html><head><title>403 Forbidden</title></head><body>"
            "<h2>Your client does not have permission to get URL "
            "<code>/api/v1/chat/completions</code> from this server.</h2>"
            "</body></html>")


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


def _pool(*accounts: Account) -> tuple[PoolManager, Router]:
    cfg = Config().pool
    return PoolManager(cfg, list(accounts)), Router(cfg)


# --------------------------------------------------------------------------- #
# EXHAUSTED is quota retirement, not a transient error
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_cool_keeps_exhausted_account_retired():
    account = Account(id="drained", access_token="token",
                      state=AccountState.EXHAUSTED)
    pool, router = _pool(account)
    await pool.cool(account, reason="mid-flight upstream_error")
    assert account.state is AccountState.EXHAUSTED
    assert not account.is_ready()
    assert router.candidates([account], set()) == []
    await pool.aclose()


@pytest.mark.asyncio
async def test_kill_after_strikes_keeps_exhausted_account_retired():
    account = Account(id="drained", access_token="token",
                      state=AccountState.EXHAUSTED)
    pool, router = _pool(account)
    await pool.kill_after_strikes(account, "token revoked")
    assert account.state is AccountState.EXHAUSTED
    assert not account.is_ready()
    assert router.candidates([account], set()) == []
    await pool.aclose()


# --------------------------------------------------------------------------- #
# credential replacement / admin enable give a fresh strike budget
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_upsert_resets_banked_quota_strikes_and_keeps_model_caps():
    existing = Account(id="dummy", access_token="old", quota_strikes=3,
                       auth_strikes=2)
    existing.model_caps[MODEL] = time.time() + 600
    pool, _ = _pool(existing)
    await pool.upsert(Account(id="dummy", access_token="new"))
    assert existing.access_token == "new"
    assert existing.quota_strikes == 0
    assert existing.auth_strikes == 0
    assert existing.model_caps[MODEL] > time.time()
    await pool.aclose()


@pytest.mark.asyncio
async def test_enable_resets_banked_quota_strikes():
    account = Account(id="dummy", access_token="token",
                      state=AccountState.EXHAUSTED, quota_strikes=3,
                      auth_strikes=3)
    pool, _ = _pool(account)
    assert await pool.enable("dummy")
    assert account.state is AccountState.READY
    assert account.quota_strikes == 0
    assert account.auth_strikes == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_apply_credentials_reset_clears_banked_quota_strikes():
    account = Account(id="dummy", access_token="old", quota_strikes=3,
                      state=AccountState.EXHAUSTED)
    pool, _ = _pool(account)
    manager = TokenManager(Config(), pool)
    await manager.apply_credentials(account, access_token="new")
    assert account.quota_strikes == 0
    assert account.state is AccountState.READY
    await manager.aclose()
    await pool.aclose()


# --------------------------------------------------------------------------- #
# upsert keeps live state
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_upsert_preserves_cooling_state_and_cooldown():
    existing = Account(id="dummy", access_token="old",
                       state=AccountState.COOLING)
    existing.cooldown_until = time.time() + 300
    cooling_until = existing.cooldown_until
    pool, _ = _pool(existing)
    await pool.upsert(Account(id="dummy", access_token="new"))
    assert existing.access_token == "new"
    assert existing.state is AccountState.COOLING
    assert existing.cooldown_until == cooling_until
    await pool.aclose()


@pytest.mark.asyncio
async def test_upsert_preserves_exhausted_state():
    existing = Account(id="dummy", access_token="old",
                       state=AccountState.EXHAUSTED)
    pool, _ = _pool(existing)
    await pool.upsert(Account(id="dummy", access_token="new"))
    assert existing.state is AccountState.EXHAUSTED
    await pool.aclose()


# --------------------------------------------------------------------------- #
# edge-block callback fires once per episode
# --------------------------------------------------------------------------- #

def test_breaker_callback_fires_once_per_episode(monkeypatch):
    clock = _Clock(1000.0)
    monkeypatch.setattr("zcode_gateway.breaker.time", clock)
    breaker = EdgeBlockBreaker()
    fired = []
    breaker.on_block = fired.append

    assert breaker.note_edge_block(EDGE_403) is True
    assert breaker.note_edge_block(EDGE_403) is True
    assert breaker.note_edge_block(EDGE_403) is True
    assert fired == [1]
    assert breaker.consecutive == 3

    clock.now += CLEAN_AFTER_S + 1        # stale episode: the next block is new
    assert breaker.note_edge_block(EDGE_403) is True
    assert fired == [1, 1]
    assert breaker.consecutive == 1


# --------------------------------------------------------------------------- #
# captcha take() keeps the tickets it did not hand out
# --------------------------------------------------------------------------- #

def test_take_keeps_remaining_fresh_tickets_and_drops_stale():
    clock = _Clock(100.0)
    pool = CaptchaTicketPool(size=3, ttl_seconds=75, mint=lambda: None,
                             clock=clock.time)
    pool.put(CaptchaTicket("stale-param-value-xxxxxxxx", "sgp", minted_at=10.0))
    pool.put(CaptchaTicket("first-param-value-xxxxxxxx", "sgp", minted_at=100.0))
    pool.put(CaptchaTicket("second-param-value-xxxxxxxx", "sgp", minted_at=100.0))

    ticket = pool.take()
    assert ticket is not None and ticket.param.startswith("second")
    assert pool.stats()["ready"] == 1     # stale one discarded, sibling kept

    ticket = pool.take()
    assert ticket is not None and ticket.param.startswith("first")
    assert pool.stats()["ready"] == 0
    assert pool.take() is None


# --------------------------------------------------------------------------- #
# notification dedupe does not depend on deque position
# --------------------------------------------------------------------------- #

def test_notify_dedupe_bumps_entry_behind_a_stale_one(monkeypatch):
    clock = _Clock(1000.0)
    monkeypatch.setattr("zcode_gateway.notify.time", clock)
    center = notify.NotificationCenter()

    first = center.emit("dedupe_case", "A")
    clock.now += 1
    center.emit("dedupe_case", "B")
    clock.now += 54                        # 55 s after the first A
    assert center.emit("dedupe_case", "A") == first
    clock.now += 5                         # B is now 60 s old, A was bumped
    center.emit("dedupe_case", "C")
    clock.now += 2

    assert center.emit("dedupe_case", "A") == first
    events = center.list()
    assert [e["message"] for e in events] == ["A", "B", "C"]
    assert events[0]["id"] == first
    assert events[0]["count"] == 3
    assert events[0]["ts"] == 1062
