"""ZCode long-lived JWTs: reload credentials on rejection; poll model quotas."""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import time

import httpx
from .config import Config
from .pool import Account, AccountState, PoolManager, load_accounts
from .quota import parse_balance
from .token_utils import _parse_expiry, _atomic_write, persist_pool_snapshot
from .accounts_dir import (parse_snapshot, snapshot_path, snapshot_transaction,
                           write_snapshot)
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
        if not hasattr(self, '_locks'):
            self._locks = {}
        return self._locks.setdefault(ident, asyncio.Lock())

    def drop_lock(self, ident):
        # A removed account can still have requests waiting on its lock. Keep
        # that lock stable if the same ID is imported again.
        pass

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

    async def refresh(self, account, force=False, *, expected_credentials=None):
        # Captured JWT has no exp or refresh token. Never invent a refresh API.
        async with self._lock_for(account.id):
            if expected_credentials is not None and account.credential_key() != expected_credentials:
                return bool(account.access_token)
            return await self._refresh_locked(account, force)

    @staticmethod
    def _issued_at(token):
        try:
            payload = token.removeprefix('workos:').split('.')[1]
            return float(json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))).get('iat', 0))
        except (ValueError, TypeError, IndexError, AttributeError):
            return 0

    @staticmethod
    def _persist_refreshed(path, fields, previous_token):
        with snapshot_transaction(path.parent):
            current = parse_snapshot(path)
            if current.get('access_token') not in (None, previous_token, fields['access_token']):
                return  # a newer external capture won while the scan was running
            write_snapshot(path, {**current, **fields})

    async def _refresh_locked(self, account, force=False):
        """Caller owns the per-ID lock; never recursively acquire it on a 401."""
        if not force and account.access_token and account.expires_in() > 0:
            return True
        credentials = account.credential_key()
        found = await self._found_accounts(force_scan=force)
        if await self.pool.find(account.id) is not account:
            return False
        if account.credential_key() != credentials:
            return bool(account.access_token)
        if found is None:
            return False
        for fresh in found:
            if fresh.id != account.id or not fresh.access_token:
                continue
            if fresh.access_token != account.access_token:
                old_iat, new_iat = self._issued_at(account.access_token), self._issued_at(fresh.access_token)
                if old_iat and new_iat and new_iat < old_iat:
                    continue
                # Unknown expiry is not an expiry of zero: a captured no-exp
                # token is authoritative unless its issuance proves it older.
                if not (old_iat and new_iat) and fresh.expires_at and account.expires_at \
                        and fresh.expires_at < account.expires_at and account.expires_in() > 0:
                    continue
            else:
                # A fresh scan may carry new device/identity metadata even when
                # the JWT did not rotate. It did not resolve a rejected token.
                self._apply_credentials_locked(account, access_token=fresh.access_token,
                    refresh_token=fresh.refresh_token or account.refresh_token,
                    expires_at=fresh.expires_at, email=fresh.email,
                    source=fresh.source, notes=fresh.notes, reset=False)
                return False
            previous_token = account.access_token
            self._apply_credentials_locked(account, access_token=fresh.access_token,
                refresh_token=fresh.refresh_token or account.refresh_token,
                expires_at=fresh.expires_at, email=fresh.email,
                source=fresh.source, notes=fresh.notes)
            try:
                fname = account.notes.get('file')
                if fname:
                    path = snapshot_path(self.cfg.accounts.dir, fname)
                    fields = {k: str(v) for k, v in fresh.notes.items()
                              if isinstance(v, (str, int, float)) and k != 'file' and v != ''}
                    fields.update(account_id=account.id, email=account.email,
                        name=account.notes.get('name', ''), access_token=account.access_token,
                        refresh_token=account.refresh_token, expires_at_ms=str(account.expires_at),
                        device_mid=account.notes.get('device_mid', ''),
                        imported_jwt='true' if account.notes.get('imported_jwt') else 'false')
                    await asyncio.to_thread(self._persist_refreshed, path, fields, previous_token)
            except Exception:
                log.debug('snapshot persist after refresh failed for %s', account.id, exc_info=True)
            return True
        return False

    def _apply_credentials_locked(self, account, *, access_token, refresh_token=None,
                                  expires_at=0, email=None, name=None, device_mid=None,
                                  source=None, notes=None, reset=True):
        previous = account.credential_key()
        account.access_token, account.expires_at = access_token, expires_at
        if refresh_token is not None:
            account.refresh_token = refresh_token
        if email:
            account.email = email
        if source:
            account.source = source
        if notes:
            account.notes.update({k: v for k, v in notes.items() if v != ''})
        if name is not None:
            account.notes['name'] = name
        if device_mid is not None:
            account.notes['device_mid'] = device_mid
        if reset or account.credential_key() != previous:
            account.credential_generation += 1
            account.expires_warned = False
        if reset:
            account.last_error = ''
            account.state = AccountState.READY
            account.death_reason = ''
            account.auth_strikes = 0
            account.notes.pop('quota_check_error', None)

    async def apply_credentials(self, account, **credentials):
        async with self._lock_for(account.id):
            self._apply_credentials_locked(account, **credentials)

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
        async with self._lock_for(account.id):
            account = await self.pool.find(account.id)
            if account is None:
                return None
            account.quota_check_generation += 1
            revision = account.quota_check_generation
            credentials = account.credential_key()
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
                async with self._lock_for(account.id):
                    if not await self._quota_current(account, credentials, revision):
                        return None
                    account.notes['quota_check_error'] = 'Quota check rejected (HTTP 401); refreshing credentials'
                    if not await self._refresh_locked(account, force=True):
                        await self.pool.auth_failed(account, 'ZCode token revoked; sign in again',
                                                    expected_credentials=credentials)
                return None
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError('Invalid quota response')
            if payload.get('code') != 0 or not isinstance(payload.get('data'), dict):
                async with self._lock_for(account.id):
                    if not await self._quota_current(account, credentials, revision):
                        return None
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
            async with self._lock_for(account.id):
                if not await self._quota_current(account, credentials, revision):
                    return None
                account.notes['quota_check_error'] = 'Quota check unavailable: ' + detail
            log.warning('quota check unavailable for %s', account.id)
            return None
        async with self._lock_for(account.id):
            if not await self._quota_current(account, credentials, revision):
                return None
            return await self._commit_balance(account, data, plans, quotas, credentials)

    async def _quota_current(self, account, credentials, revision):
        current = await self.pool.find(account.id)
        return (current is account and account.credential_key() == credentials
                and account.quota_check_generation == revision)

    async def _commit_balance(self, account, data, plans, quotas, credentials):
        # Per-ID lock serializes quota commits with credential adoption. Pool
        # guards cover direct upserts while waiting for its own mutation lock.
        account.notes['plans'] = plans
        account.auth_strikes = 0
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
                                     cause='plan_lost', expected_credentials=credentials):
                log.warning('account %s: plan lost (poll confirmed) - marked DEAD', account.id)
            return None
        now = time.time()
        from .pool import WINDOW_GRACE_S
        for model, quota in quotas.items():
            if account.credential_key() != credentials:
                return None
            starts = quota.get('starts_at') or 0
            if starts > now + WINDOW_GRACE_S:
                # Only pending capacity exists for this model. Do not close
                # mixed live/future buckets or park for minor clock skew.
                await self.pool.cap_model(account, model, starts - now,
                                           reason='WINDOW_CLOSED',
                                           message='grant window not open yet (e.g. night-only)',
                                           quota_only=True, expected_credentials=credentials)
                continue
            if quota.get('remaining', 0) <= 0:
                releases = [at for at in (quota.get('resets_at', 0),
                                           quota.get('pending_starts_at', 0)) if at > now]
                if releases:
                    await self.pool.cap_model(account, model, min(releases) - now,
                                              reason='MODEL_CAPPED', message='daily ZCode token quota exhausted',
                                               quota_only=True, expected_credentials=credentials)
                    continue
                # remaining 0 but the reset window already passed: leave any
                # existing park alone (the next poll sees the refreshed
                # remaining and clears it below)
            else:
                await self.pool.clear_quota_cap(account, model, expected_credentials=credentials)
        # fully drained: every known quota bucket at zero and none waiting on
        # a reset window -> retire the whole account, not just per-model caps.
        # Conversely: a retired account whose buckets recovered (daily 03:00
        # reset, plan renewal) comes BACK — restore() used to never run, so
        # EXHAUSTED was a one-way death.
        if account.credential_key() != credentials:
            return None
        if quotas and all(q.get('remaining', 0) <= 0 for q in quotas.values()) \
                and not any((q.get('starts_at', 0) or q.get('pending_starts_at', 0) or 0) > now
                            for q in quotas.values()):
            if account.state not in (AccountState.EXHAUSTED, AccountState.DEAD):
                log.info('account %s drained (all quotas 0) — retired from rotation', account.id)
                self.pool.notify('quota',
                                 f"account {account.id} drained — all quotas at 0, "
                                 f"retired from rotation",
                                 level='warning')
            await self.pool.retire(account, 'no tokens remaining on any model', paid_only=False,
                                   expected_credentials=credentials)
        elif account.state is AccountState.EXHAUSTED:
            if await self.pool.restore(account, expected_credentials=credentials):
                log.info('account %s quotas recovered — back in rotation', account.id)
        # plan recovered after a plan_lost DEAD mark: revive. Only this
        # specific cause — a credential-dead account stays dead.
        if account.state is AccountState.DEAD and plans and account.plan_active(now) and \
                any(q.get('remaining', 0) > 0 and (q.get('starts_at') or 0) <= now + WINDOW_GRACE_S
                    for q in quotas.values()):
            if await self.pool.restore(account, plan_recovered=True, expected_credentials=credentials):
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
