"""Prefetched captcha tickets (Aliyun F019 caps their life at 90 s)."""

from __future__ import annotations

import time

from zcode_gateway.captcha_tickets import (CaptchaTicket, CaptchaTicketPool,
                                           MAX_TICKET_TTL)


def test_disabled_pool_never_mints():
    calls = []
    pool = CaptchaTicketPool(size=0, mint=lambda: calls.append(1))
    pool.start()
    assert not pool.enabled
    assert pool.take() is None
    assert calls == []
    pool.stop()


def test_take_returns_live_ticket_and_discards_stale():
    now = [100.0]
    pool = CaptchaTicketPool(size=2, ttl_seconds=75, mint=lambda: None,
                             clock=lambda: now[0])
    pool.put(CaptchaTicket("old-param-value-xxxxxxxx", "sgp", minted_at=100.0))
    now[0] = 171.0          # 71 s old: past ttl - 5 s safety margin
    assert pool.take() is None
    pool.put(CaptchaTicket("fresh-param-value-xxxxxxxx", "sgp", minted_at=now[0]))
    ticket = pool.take()
    assert ticket is not None
    assert ticket.param.startswith("fresh")
    assert pool.take() is None       # single-use


def test_ttl_is_capped_at_aliyun_limit():
    pool = CaptchaTicketPool(size=1, ttl_seconds=1000, mint=lambda: None)
    assert pool.ttl == MAX_TICKET_TTL


def test_put_respects_size():
    pool = CaptchaTicketPool(size=1, ttl_seconds=75, mint=lambda: None)
    pool.put(CaptchaTicket("a", "sgp", time.monotonic()))
    pool.put(CaptchaTicket("b", "sgp", time.monotonic()))
    assert pool.stats()["ready"] == 1


def test_background_minter_fills_the_pool():
    pool = CaptchaTicketPool(
        size=2, ttl_seconds=75,
        mint=lambda: CaptchaTicket("param-value-long-enough", "sgp", time.monotonic()))
    pool.start()
    try:
        deadline = time.monotonic() + 3
        while pool.stats()["ready"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pool.stats()["ready"] >= 1
        assert pool.minted >= 1
    finally:
        pool.stop()


def test_failed_mint_backs_off_without_spinning():
    calls = []

    def broken():
        calls.append(1)
        return None

    pool = CaptchaTicketPool(size=1, ttl_seconds=75, mint=broken)
    pool.start()
    try:
        time.sleep(0.2)
    finally:
        pool.stop()
    # a 5 s back-off means at most one attempt in this window
    assert len(calls) <= 1
    assert pool.stats()["failed"] >= 1
