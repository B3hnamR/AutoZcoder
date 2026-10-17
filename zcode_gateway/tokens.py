"""ZCode long-lived JWTs: reload credentials on rejection; poll model quotas."""
from __future__ import annotations
import asyncio
import logging
import time
from pathlib import Path

import httpx
from .config import Config
from .pool import Account, AccountState, PoolManager, load_accounts
from .quota import parse_balance
from .token_utils import _parse_expiry, _atomic_write, persist_pool_snapshot
from .upstream import build_headers

log = logging.getLogger('zcode_gateway.tokens')

class TokenManager:
    def __init__(self, cfg: Config, pool: PoolManager):
        self.cfg, self.pool = cfg, pool
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
        self._locks = {}
        self._stop = asyncio.Event()

    def _lock_for(self, ident):
        return self._locks.setdefault(ident, asyncio.Lock())

    def drop_lock(self, ident):
        self._locks.pop(ident, None)

    async def aclose(self):
        self._stop.set()
        await self._client.aclose()

    async def refresh(self, account, force=False):
        # Captured JWT has no exp or refresh token. Never invent a refresh API.
        async with self._lock_for(account.id):
            if not force and account.access_token and account.expires_in() > 0:
                return True
            try:
                found = await asyncio.to_thread(load_accounts, self.cfg.accounts)
            except (OSError, ValueError):
                return False
            for fresh in found:
                if fresh.id == account.id and fresh.access_token != account.access_token:
                    account.access_token, account.expires_at = fresh.access_token, fresh.expires_at
                    return True
            return False

    async def apply_credentials(self, account, *, access_token, refresh_token='', expires_at=0):
        async with self._lock_for(account.id):
            account.access_token, account.expires_at = access_token, expires_at
            account.last_error = ''
            account.state = AccountState.READY

    async def refresh_loop(self):
        while not self._stop.is_set():
            await asyncio.sleep(30)
            for account in await self.pool.all():
                if account.needs_refresh(self.cfg.pool.refresh_lead_seconds):
                    await self.refresh(account)

    async def balance_loop(self):
        while not self._stop.is_set():
            accounts = await self.pool.all()
            sem = asyncio.Semaphore(4)
            async def check(account):
                async with sem:
                    await self.check_account(account)
            await asyncio.gather(*(check(a) for a in accounts), return_exceptions=True)
            await asyncio.sleep(max(1, self.cfg.pool.balance_poll_seconds))

    async def check_account(self, account):
        headers = build_headers(account.access_token, self.cfg.upstream.fingerprint,
                                device_mid=account.notes.get('device_mid'))
        url = self.cfg.upstream.base_url.rstrip('/') + '/zcode-plan/billing/balance'
        try:
            resp = await self._client.get(url, params={'app_version': self.cfg.upstream.fingerprint.client_version},
                                          headers=headers)
            if resp.status_code == 401:
                if not await self.refresh(account, force=True):
                    await self.pool.kill(account, 'ZCode token revoked; sign in again')
                return None
            resp.raise_for_status()
            payload = resp.json()
            if payload.get('code') != 0 or not isinstance(payload.get('data'), dict):
                log.warning('quota check rejected for %s (code %s)', account.id, payload.get('code'))
                return None
            data = payload['data']
            plans, quotas = parse_balance(data, time.time())
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            log.warning('quota check unavailable for %s', account.id)
            return None
        account.notes['plans'] = plans
        account.notes['quotas'] = quotas
        account.notes['plan'] = ', '.join(p['name'] or p['plan_id'] or '' for p in plans)
        account.balance_checked_at = time.time()
        account.has_plan = account.plan_active()
        account.plan_checked_at = account.balance_checked_at
        now = time.time()
        _cap_codes = ('MODEL_CAPPED', '11113', 'WINDOW_CLOSED')
        for model, quota in quotas.items():
            starts = quota.get('starts_at') or 0
            if starts > now:
                # grant exists but its window (e.g. daily_night) is not open yet —
                # park the model until then instead of sending doomed requests
                await self.pool.cap_model(account, model, starts - now,
                                          reason='WINDOW_CLOSED',
                                          message='grant window not open yet (e.g. night-only)')
            elif quota['remaining'] <= 0 and quota['resets_at'] > now:
                await self.pool.cap_model(account, model, quota['resets_at'] - now,
                                          reason='MODEL_CAPPED', message='daily ZCode token quota exhausted')
            elif quota['remaining'] > 0 and account.model_cap_info.get(model, {}).get('code') in _cap_codes:
                account.model_caps.pop(model, None)
                account.model_cap_info.pop(model, None)
        return data

    async def fetch_balance(self, account):
        return await self.check_account(account)

    async def fetch_plan(self, account):
        if not account.balance_checked_at:
            await self.check_account(account)
        return account.has_plan

    async def fetch_me(self, account):
        return {'email': account.email, 'name': account.notes.get('name', ''),
                'plans': account.notes.get('plans', []), 'quotas': account.notes.get('quotas', {})}
