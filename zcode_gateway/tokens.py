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
from .accounts_dir import write_snapshot
from .upstream import build_headers

log = logging.getLogger('zcode_gateway.tokens')

class TokenManager:
    # One accounts-directory scan serves every expiring account within this
    # window: refresh() used to call load_accounts PER expiring account, so
    # N accounts meant N full directory scans every 30s loop.
    _SCAN_TTL_S = 10.0

    def __init__(self, cfg: Config, pool: PoolManager):
        self.cfg, self.pool = cfg, pool
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
        self._locks = {}
        self._stop = asyncio.Event()
        self._scan_lock = asyncio.Lock()
        self._scan_cache: list | None = None
        self._scan_at = 0.0

    def _lock_for(self, ident):
        return self._locks.setdefault(ident, asyncio.Lock())

    def drop_lock(self, ident):
        self._locks.pop(ident, None)

    async def aclose(self):
        self._stop.set()
        await self._client.aclose()

    async def _found_accounts(self, force_scan: bool = False):
        """Cached load_accounts: one directory scan per cycle, shared across
        every expiring account. force_scan bypasses the cache (a 401 strike
        needs the freshest possible on-disk token)."""
        async with self._scan_lock:
            now = time.time()
            if force_scan or self._scan_cache is None \
                    or now - self._scan_at > self._SCAN_TTL_S:
                try:
                    self._scan_cache = await asyncio.to_thread(
                        load_accounts, self.cfg.accounts)
                except (OSError, ValueError):
                    return None
                self._scan_at = now
            return self._scan_cache

    async def refresh(self, account, force=False):
        # Captured JWT has no exp or refresh token. Never invent a refresh API.
        async with self._lock_for(account.id):
            if not force and account.access_token and account.expires_in() > 0:
                return True
            found = await self._found_accounts(force_scan=force)
            if found is None:
                return False
            for fresh in found:
                if fresh.id == account.id and fresh.access_token != account.access_token:
                    # freshness guard: on force-reload (every upstream 401) the
                    # on-disk snapshot may be OLDER than the token we already
                    # hold (adopted at runtime, disk not yet updated). Rolling
                    # back to a stale token re-killed accounts the next request.
                    if force and (fresh.expires_at or 0) < (account.expires_at or 0) \
                            and account.expires_in() > 0:
                        continue
                    account.access_token, account.expires_at = fresh.access_token, fresh.expires_at
                    # adopt-persist: a runtime-adopted token used to live only
                    # in memory and died with the process; after restart (with
                    # no refresh grant anywhere) the account hit 401 strikes.
                    try:
                        fname = account.notes.get('file')
                        if fname:
                            await asyncio.to_thread(
                                write_snapshot,
                                Path(self.cfg.accounts.dir) / fname,
                                {'account_id': account.id, 'email': account.email,
                                 'name': account.notes.get('name', ''),
                                 'access_token': account.access_token,
                                 'expires_at_ms': str(account.expires_at),
                                  'device_mid': account.notes.get('device_mid', ''),
                                  'imported_jwt': 'true' if account.notes.get('imported_jwt') else 'false',
                                  'source': account.notes.get('source', 'token refresh')})
                    except Exception:
                        log.debug('snapshot persist after refresh failed for %s',
                                  account.id, exc_info=True)
                    return True
            return False

    async def apply_credentials(self, account, *, access_token, refresh_token='', expires_at=0):
        async with self._lock_for(account.id):
            account.access_token, account.expires_at = access_token, expires_at
            account.last_error = ''
            account.state = AccountState.READY
            account.death_reason = ''

    async def refresh_loop(self):
        while not self._stop.is_set():
            await asyncio.sleep(30)
            for account in await self.pool.all():
                try:
                    if account.needs_refresh(self.cfg.pool.refresh_lead_seconds):
                        await self.refresh(account)
                    # expiry horizon: there is no refresh grant anywhere (the
                    # captured JWT has exp and no refresh token), so an expiring
                    # account is a KNOWN future death. Surface it loudly instead
                    # of discovering it via 401 strikes mid-request.
                    hours = account.expires_in() / 3600.0
                    if 0 < hours < 24 and not account.expires_warned:
                        account.expires_warned = True
                        log.warning('account %s token expires in %.1fh — no refresh '
                                    'grant exists; re-login before it dies',
                                    account.id, hours)
                        try:
                            self.pool.notify('token_expiring',
                                             f"account {account.id} token expires in "
                                             f"{hours:.1f}h — re-login before it dies",
                                             level='warning')
                        except Exception:
                            pass
                except Exception:
                    log.debug('refresh pass failed for %s', account.id, exc_info=True)

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
        account.notes['quota_check_attempted_at'] = time.time()
        headers = build_headers(account.access_token, self.cfg.upstream.fingerprint,
                                device_mid=account.notes.get('device_mid'))
        url = self.cfg.upstream.base_url.rstrip('/') + '/zcode-plan/billing/balance'
        try:
            for attempt in range(2):
                try:
                    resp = await self._client.get(url, params={'app_version': self.cfg.upstream.fingerprint.client_version},
                                                  headers=headers)
                    break
                except httpx.TransportError:
                    if attempt:
                        raise
            if resp.status_code == 401:
                account.notes['quota_check_error'] = 'Quota check rejected (HTTP 401); refreshing credentials'
                if not await self.refresh(account, force=True):
                    await self.pool.kill(account, 'ZCode token revoked; sign in again')
                return None
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError('Invalid quota response')
            if payload.get('code') != 0 or not isinstance(payload.get('data'), dict):
                account.notes['quota_check_error'] = 'Quota API rejected the check (code %s)' % payload.get('code')
                log.warning('quota check rejected for %s (code %s)', account.id, payload.get('code'))
                return None
            data = payload['data']
            plans, quotas = parse_balance(data, time.time())
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
            # Keep the last successful values and expose a credential-safe
            # status instead of pretending this account was never checked.
            if isinstance(exc, httpx.HTTPStatusError):
                detail = 'HTTP %s' % exc.response.status_code
            elif isinstance(exc, httpx.TimeoutException):
                detail = 'request timed out'
            else:
                detail = type(exc).__name__
            account.notes['quota_check_error'] = 'Quota check unavailable: ' + detail
            log.warning('quota check unavailable for %s', account.id)
            return None
        account.notes['plans'] = plans
        account.notes.pop('quota_check_error', None)
        account.notes['quotas'] = quotas
        account.notes['plan'] = ', '.join(p['name'] or p['plan_id'] or '' for p in plans)
        account.balance_checked_at = time.time()
        account.has_plan = account.plan_active()
        account.plan_checked_at = account.balance_checked_at
        # PLAN LOST: the poll SUCCEEDED and shows no active plan and no quota
        # buckets at all. Such an account is unroutable at the upstream (every
        # request answers 1005 "exceed quota limit") — mark DEAD so the router
        # skips it and the dashboard shows it red instead of ready. If a plan
        # reappears on a later poll (renewal/recovery), restore() brings it
        # back automatically.
        if not plans and not quotas:
            if await self.pool.kill(account, 'plan lost - no active plan or quota buckets',
                                    cause='plan_lost'):
                log.warning('account %s: plan lost (poll confirmed) - marked DEAD', account.id)
            return None
        now = time.time()
        from .pool import WINDOW_GRACE_S
        for model, quota in quotas.items():
            starts = quota.get('starts_at') or 0
            if starts > now + WINDOW_GRACE_S:
                # grant window not open yet (night-only referral grant): the
                # grant is folded into `remaining`, so a remaining>0 check
                # would never catch this — park on starts_at, but only when it
                # is MEANINGFULLY in the future. The old unconditional check
                # parked every account for 1-2s at each window boundary /
                # clock-skew blip and clients saw quota errors flash.
                await self.pool.cap_model(account, model, starts - now,
                                          reason='WINDOW_CLOSED',
                                          message='grant window not open yet (e.g. night-only)',
                                          quota_only=True)
                continue
            if quota.get('remaining', 0) <= 0:
                if quota.get('resets_at', 0) > now:
                    await self.pool.cap_model(account, model, quota['resets_at'] - now,
                                              reason='MODEL_CAPPED', message='daily ZCode token quota exhausted',
                                              quota_only=True)
                    continue
                # remaining 0 but the reset window already passed: leave any
                # existing park alone (the next poll sees the refreshed
                # remaining and clears it below)
            else:
                await self.pool.clear_quota_cap(account, model)
        # fully drained: every known quota bucket at zero and none waiting on
        # a reset window -> retire the whole account, not just per-model caps.
        # Conversely: a retired account whose buckets recovered (daily 03:00
        # reset, plan renewal) comes BACK — restore() used to never run, so
        # EXHAUSTED was a one-way death.
        if quotas and all(q.get('remaining', 0) <= 0 for q in quotas.values()) \
                and not any((q.get('starts_at', 0) or 0) > now for q in quotas.values()):
            if account.state not in (AccountState.EXHAUSTED, AccountState.DEAD):
                log.info('account %s drained (all quotas 0) — retired from rotation', account.id)
                self.pool.notify('quota',
                                 f"account {account.id} drained — all quotas at 0, "
                                 f"retired from rotation",
                                 level='warning')
            await self.pool.retire(account, 'no tokens remaining on any model', paid_only=False)
        elif account.state is AccountState.EXHAUSTED:
            if await self.pool.restore(account):
                log.info('account %s quotas recovered — back in rotation', account.id)
        # plan recovered after a plan_lost DEAD mark: revive. Only this
        # specific cause — a credential-dead account stays dead.
        if account.state is AccountState.DEAD and plans and account.plan_active(now) and \
                any(q.get('remaining', 0) > 0 and (q.get('starts_at') or 0) <= now + WINDOW_GRACE_S
                    for q in quotas.values()):
            if await self.pool.restore(account, plan_recovered=True):
                log.info('account %s plan recovered — back in rotation', account.id)
                self.pool.notify('quota',
                                 f"account {account.id} plan recovered — "
                                 f"back in rotation",
                                 level='success')
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
