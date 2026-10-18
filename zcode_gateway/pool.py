"""Account pool: credential records, selection strategies, runtime state."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable

from .config import AccountsConfig, PoolConfig


log = logging.getLogger("zcode_gateway.pool")


def _is_ent(code: str) -> bool:
    # local import: upstream imports nothing from pool, so this stays acyclic
    from .upstream import is_entitlement_code
    return is_entitlement_code(code)


class AccountState(str, Enum):
    READY = "ready"
    COOLING = "cooling"
    EXHAUSTED = "exhausted"     # 402 insufficient_credits — retired until balance returns
    DEAD = "dead"               # refresh permanently failed / credential invalid


@dataclass(eq=False)          # identity equality: mutable runtime object
class Account:
    id: str
    email: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0            # epoch ms
    source: str = "unknown"

    state: AccountState = AccountState.READY
    last_used: float = 0.0
    in_flight: int = 0
    error_count: int = 0
    cooldown_until: float = 0.0
    balance_micro: int | None = None
    balance_checked_at: float = 0.0
    last_error: str = ""
    # rolling autopsy log: last N upstream errors with timestamps, for the
    # dashboard "why did this account die" view and the health sparkline
    error_history: deque = field(default_factory=lambda: deque(maxlen=50))

    # Credit exhaustion is per model-class, not per account: a paid model can 402
    # (insufficient_credits) while zcode-free/* models keep working on the same
    # account, because free models do not consume Zcode Credits.
    paid_exhausted: bool = False

    # Per-model daily caps (e.g. INFERENCE_CAP_ERROR on a free model). Keyed by
    # upstream model id -> epoch seconds when the cap lifts. A capped model on one
    # account must not cool the account or block its other models.
    model_caps: dict[str, float] = field(default_factory=dict)
    # model -> {"code","message","release_at"} so a parked model can surface its
    # real reason and its absolute release time instead of a generic
    # "no accounts available".
    model_cap_info: dict[str, dict] = field(default_factory=dict)

    # free-form context from a snapshot file (name, balance, plan, file, ...)
    notes: dict = field(default_factory=dict)

    # Does this account have a paid subscription (Zcode Pass / Cloud)?
    # None = not checked yet. False is the common case: /users/me/plan returns
    # 404 "no plan history found for user".
    has_plan: bool | None = None
    # when has_plan was last verified (epoch seconds) — the balance loop
    # re-checks hourly so a subscription bought later is actually picked up
    plan_checked_at: float = 0.0

    # ------------------------------------------------------------------ #

    def is_model_capped(self, model: str, now: float | None = None) -> bool:
        now = now or time.time()
        until = self.model_caps.get(model)
        return bool(until and until > now)

    @property
    def is_free_tier(self) -> bool:
        return False  # credential-level; free/paid is per-model, kept for future use

    def is_ready(self, now: float | None = None, require_paid: bool = False) -> bool:
        now = now or time.time()
        if self.state is AccountState.DEAD:
            return False
        if self.state is AccountState.EXHAUSTED:
            return False
        if require_paid and self.paid_exhausted:
            return False
        if self.state is AccountState.COOLING and now < self.cooldown_until:
            return False
        return bool(self.access_token) and self.plan_active(now)

    def plan_active(self, now=None):
        now = time.time() if now is None else now
        plans = self.notes.get("plans")
        # "never checked" (None) and "checked, none active" ([]) both stay
        # routable: a quota endpoint that returns an empty list must not
        # silently drop a healthy account from rotation. Hard state (DEAD /
        # EXHAUSTED / explicit caps) handles real removals.
        if plans is None or not plans:
            return True
        return any(p.get("status") == "active" and
                   (not p.get("starts_at") or p["starts_at"] <= now) and
                   (not p.get("ends_at") or p["ends_at"] > now) for p in plans)

    def model_eligible(self, model, now=None):
        now = time.time() if now is None else now
        if not self.plan_active(now): return False
        quotas = self.notes.get("quotas")
        if quotas is None: return True
        quota = quotas.get(model)
        if quota is None: return False
        return quota.get("remaining", 0) > 0 or (quota.get("resets_at", 0) > 0 and quota["resets_at"] < now)

    def expires_in(self, now: float | None = None) -> float:
        now = now or time.time()
        return (self.expires_at / 1000.0) - now if self.expires_at else 1e9

    def needs_refresh(self, lead_seconds: int) -> bool:
        return self.expires_in() < lead_seconds

    # ------------------------------------------------------------------ #

    def to_public(self) -> dict:
        tok = self.access_token or ""
        return {
            "id": self.id,
            "email": self.email,
            "state": self.state.value,
            "paid_exhausted": self.paid_exhausted,
            "expires_at": self.expires_at,
            "expires_in_s": round(self.expires_in(), 1),
            "needs_refresh": self.needs_refresh(600),
            "in_flight": self.in_flight,
            "error_count": self.error_count,
            "balance_micro": self.balance_micro,
            "last_used": self.last_used,
            "token_fingerprint": __import__("hashlib").sha256(tok.encode()).hexdigest()[:12],
            "quotas": self.notes.get("quotas", {}),
            "plans": self.notes.get("plans", []),
            "has_refresh_token": bool(self.refresh_token),
            "last_error": self.last_error,
            "error_history": [
                {**e, "ts": round(e.get("ts", 0), 1),
                 "ago_s": int(time.time() - e.get("ts", 0))}
                for e in list(self.error_history)[-20:]
            ],
            "notes": self.notes,
            "has_plan": self.has_plan,
            "capped_models": sorted(m for m, until in self.model_caps.items()
                                    if until > time.time()),
            "model_caps": {
                m: {
                    **self.model_cap_info.get(m, {}),
                    "release_in_s": int(max(until - time.time(), 0)),
                }
                for m, until in self.model_caps.items()
                if until > time.time()
            },
        }


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #


def _account_from_auth(auth: dict, ident: str, source: str) -> Account:
    meta = (auth.get("metadata") or {}).get("userInfo") or {}
    return Account(
        id=auth.get("accountId") or ident,
        email=meta.get("email", ""),
        access_token=auth.get("accessToken", ""),
        refresh_token=auth.get("refreshToken", ""),
        expires_at=_expiry_ms(auth.get("expiresAt")),
        source=source,
    )


def _expiry_ms(value) -> int:
    """Normalise an expiresAt/expires_at to epoch ms (ISO 8601 or epoch s/ms).

    The captured wire contract is ISO 8601; Zcode's stored config is epoch ms;
    a hand-written pool file may use either. Reuses the tokens.py parser.
    """
    from .tokens import _parse_expiry
    return _parse_expiry(value)


def load_from_providers_json(path: str) -> list[Account]:
    """Read ZCode's credentials.json or the supplied tokens.json extract."""
    from .zcode_accounts import read_credentials
    return [Account(id=f["account_id"], email=f["email"], access_token=f["access_token"],
                    expires_at=int(f["expires_at_ms"]), source="providers_json",
                    notes={"name": f["name"]}) for f in read_credentials(path)]


def load_from_pool_file(path: str) -> list[Account]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"pool file not found: {path}")
    data = json.loads(p.read_text(encoding="utf-8"))
    raw = data.get("accounts") if isinstance(data, dict) else data
    out: list[Account] = []
    for i, item in enumerate(raw or []):
        if "auth" in item:
            out.append(_account_from_auth(item["auth"], item.get("id", f"pool-{i}"),
                                          "pool.json"))
        else:
            out.append(Account(
                id=item.get("id") or f"pool-{i}",
                email=item.get("email", ""),
                access_token=item.get("access_token") or item.get("accessToken", ""),
                refresh_token=item.get("refresh_token") or item.get("refreshToken", ""),
                expires_at=_expiry_ms(item.get("expires_at") or item.get("expiresAt")),
                source="pool.json",
            ))
    return out


def load_accounts(cfg: AccountsConfig) -> list[Account]:
    if cfg.source == "pool_file":
        return load_from_pool_file(cfg.pool_file)
    if cfg.source == "providers_json":
        return load_from_providers_json(cfg.providers_json)
    if cfg.source == "accounts_dir":
        from .accounts_dir import load_from_accounts_dir
        accounts = load_from_accounts_dir(cfg.dir)
        return accounts
    raise ValueError(f"unknown accounts.source: {cfg.source}")


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #


class Router:
    """Selects an account from the ready set according to a strategy."""

    def __init__(self, cfg: PoolConfig) -> None:
        self.cfg = cfg
        self._rr_index = 0

    def candidates(self, accounts: Iterable[Account], exclude: set[str],
                   require_paid: bool = False, model: str | None = None) -> list[Account]:
        now = time.time()
        pool = [a for a in accounts
                if a.id not in exclude
                and a.state is not AccountState.DEAD
                and a.state is not AccountState.EXHAUSTED
                and not (require_paid and a.paid_exhausted)
                and a.plan_active(now)
                and not (model and (a.is_model_capped(model, now) or not a.model_eligible(model, now)))
                and not (a.state is AccountState.COOLING and now < a.cooldown_until)
                and a.access_token]
        if self.cfg.strategy == "quota_aware" and model and pool:
            pool = sorted(pool, key=lambda a: -(a.notes.get("quotas", {}).get(model, {}).get("remaining", 0)))
            best = pool[0].notes.get("quotas", {}).get(model, {}).get("remaining", 0)
            pool = [a for a in pool if a.notes.get("quotas", {}).get(model, {}).get("remaining", 0) == best]
        if self.cfg.strategy == "quota_aware" and require_paid:
            thr = self.cfg.min_balance_micro
            filtered = [a for a in pool
                        if a.balance_micro is None or a.balance_micro > thr]
            if filtered:
                pool = filtered
            # if every account is below threshold, fall through and try anyway
        if self.cfg.strategy == "drain_first" and model:
            # Use one account at a time: order by ascending remaining quota so
            # the account nearest exhaustion is drained first and retires
            # soonest. Ties (and accounts whose quota is not yet known) keep the
            # stable pool order, so the same account is reused until it is
            # capped, cooled, saturated or excluded. Spreading load across every
            # account at once is what gets whole pools flagged.
            def remaining(account: Account) -> float:
                quotas = account.notes.get("quotas")
                if quotas is None:
                    return float("inf")        # unknown: fall back to pool order
                return quotas.get(model, {}).get("remaining", 0)
            pool = sorted(pool, key=remaining)
        return pool

    def pick(self, accounts: list[Account]) -> Account | None:
        if not accounts:
            return None
        cap = self.cfg.max_in_flight_per_account
        under_cap = [a for a in accounts if a.in_flight < cap]
        if not under_cap:
            # The configured cap must be a cap, not a preference: when every
            # candidate is saturated, report no candidate. The manager waits
            # for a slot (see PoolManager.acquire) or the caller fails over.
            return None

        strategy = self.cfg.strategy
        if strategy == "round_robin":
            acc = under_cap[self._rr_index % len(under_cap)]
            self._rr_index = (self._rr_index + 1) % len(under_cap)
            return acc
        if strategy == "least_recently_used":
            return min(under_cap, key=lambda a: a.last_used)
        if strategy == "drain_first":
            # candidates() already ordered the pool; keep draining the head.
            return under_cap[0]
        # least_in_flight (default) and quota_aware fallback
        return min(under_cap, key=lambda a: (a.in_flight, a.last_used))

    def acquire(self, accounts: list[Account], exclude: set[str],
                require_paid: bool = False, model: str | None = None) -> Account | None:
        acc = self.pick(self.candidates(accounts, exclude, require_paid, model))
        if acc is not None:
            acc.in_flight += 1
            acc.last_used = time.time()
        return acc


# --------------------------------------------------------------------------- #
# manager
# --------------------------------------------------------------------------- #


class PoolManager:
    """Owns the account list and its runtime mutations. Concurrency-safe."""

    def __init__(self, cfg: PoolConfig, accounts: list[Account]) -> None:
        self.cfg = cfg
        self._accounts: dict[str, Account] = {a.id: a for a in accounts}
        self._lock = asyncio.Lock()
        self._router = Router(cfg)
        # incremented on every release()/reacquire(); acquire() waiters sleep on
        # a generation mismatch instead of polling on a fixed timer
        self._slot_gen = 0

    # -- access ---------------------------------------------------------- #

    async def all(self) -> list[Account]:
        async with self._lock:
            return list(self._accounts.values())

    async def acquire(self, exclude: set[str] | None = None,
                      require_paid: bool = False,
                      model: str | None = None,
                      wait_seconds: float | None = None,
                      only_id: str | None = None,
                      ) -> Account | None:
        """Pick an account, optionally waiting for an in-flight slot.

        When every eligible account is at `max_in_flight_per_account`, this
        waits up to `wait_seconds` (default: `pool.acquire_wait_seconds`) for a
        slot instead of oversubscribing. Waits never happen when the blocker is
        absence (dead/cooling/excluded accounts) — only saturation.

        `only_id` pins the choice to a single account (per-request routing,
        e.g. a generator claim that must run on the account being claimed).
        """
        if wait_seconds is None:
            wait_seconds = self.cfg.acquire_wait_seconds
        deadline = time.monotonic() + max(wait_seconds, 0.0)
        while True:
            async with self._lock:
                accounts = list(self._accounts.values())
                if only_id:
                    accounts = [a for a in accounts if a.id == only_id]
                excl = exclude or set()
                acc = self._router.acquire(accounts, excl, require_paid, model)
                if acc is not None:
                    return acc
                # wait only if a slot may free up: eligible accounts exist but
                # all are at the in-flight cap
                if (not self._router.candidates(accounts, excl, require_paid, model)
                        or time.monotonic() >= deadline):
                    return None
                gen = self._slot_gen
            # sleep until a slot is freed (generation bump) or the next cheap
            # re-check; the 50 ms floor caps the added latency when a release
            # happens just before we snapshot the generation
            await asyncio.sleep(0.05)
            async with self._lock:
                if self._slot_gen != gen:
                    continue
            # no release happened during the nap; loop re-checks the deadline

    async def reacquire(self, account: Account) -> None:
        """Re-mark an account as in-flight for a same-account retry.

        The 401 path releases the original reservation before classifying the
        error, then retries on the same account; without taking the slot back,
        the final release would decrement another request's reservation.

        Respects `max_in_flight_per_account`: if a concurrent request grabbed
        the freed slot in between, this account may sit at the cap — the retry
        still proceeds (the reservation is conceptually the same request's),
        but the overshoot is logged rather than silent.
        """
        async with self._lock:
            cap = self.cfg.max_in_flight_per_account
            if cap > 0 and account.in_flight >= cap:
                log.debug("reacquire over the in-flight cap (%s >= %s) on %s",
                          account.in_flight, cap, account.id)
            account.in_flight += 1
            account.last_used = time.time()
            self._slot_gen += 1

    async def cap_model(self, account: Account, model: str,
                        seconds: float, reason: str = "inference_cap",
                        message: str = "") -> None:
        """Record a per-model daily cap. Deliberately does NOT cool the account:
        other models on the same account remain usable.

        `seconds` is the window parsed out of the upstream message; the absolute
        release time is stored so tools and clients can see when the model frees up.
        """
        async with self._lock:
            release_at = time.time() + max(seconds, 1.0)
            account.model_caps[model] = release_at
            account.model_cap_info[model] = {
                "code": reason,
                "message": message,
                "release_at": datetime.fromtimestamp(
                    release_at, timezone.utc).isoformat(),
                "release_in_s": int(max(seconds, 1.0)),
            }
            account.last_error = f"{reason}:{model}"

    async def unavailable_reason(self, model: str) -> dict | None:
        """Why can this model not be served right now?

        Returns {status, code, message, release_at, release_in_s} for the most
        specific applicable reason, or None. Keeps the gateway from answering a
        generic "no usable account" when the truth is a parked model or a short
        cooldown — which matters because those are retryable in seconds, whereas
        a generic 503 reads as "broken".
        """
        async with self._lock:
            accounts = list(self._accounts.values())

            # 1) the model is parked (daily cap / no entitlement)
            for account in accounts:
                if not account.is_model_capped(model):
                    continue
                info = account.model_cap_info.get(model, {})
                code = info.get("code", "MODEL_PARKED")
                until = account.model_caps.get(model, 0.0)
                return {
                    "status": 403 if _is_ent(code) else 429,
                    "code": code,
                    "message": info.get("message", ""),
                    "release_at": info.get("release_at"),
                    "release_in_s": int(max(until - time.time(), 0)),
                }

            if not accounts:
                return None

            # 2) every account is cooling: transient, retry in seconds
            if all(a.state is AccountState.COOLING for a in accounts):
                wait = max(a.cooldown_until - time.time() for a in accounts)
                reason = next((a.last_error for a in accounts if a.last_error), "")
                return {
                    "status": 503,
                    "code": "ACCOUNT_COOLING",
                    "message": (f"account cooling after {reason}; "
                                f"retry in {int(max(wait, 0))}s"),
                    "release_at": None,
                    "release_in_s": int(max(wait, 0)),
                }

            # 3) nothing usable at all
            if all(not a.is_ready() for a in accounts):
                return {
                    "status": 503,
                    "code": "NO_USABLE_ACCOUNTS",
                    "message": "no usable Zcode account in the pool",
                    "release_at": None,
                    "release_in_s": None,
                }

            # 4) everyone eligible is at the in-flight cap right now
            ready = [a for a in accounts if a.is_ready() and a.access_token]
            if ready and all(a.in_flight >= self.cfg.max_in_flight_per_account
                             for a in ready):
                return {
                    "status": 503,
                    "code": "ACCOUNT_BUSY",
                    "message": "all accounts are at max in-flight capacity",
                    "release_at": None,
                    "release_in_s": 1,
                }
        return None

    async def parked_reason(self, model: str) -> dict | None:
        """Parked-model-only view of `unavailable_reason` (kept for callers that
        specifically want the cap/entitlement case)."""
        reason = await self.unavailable_reason(model)
        if reason and reason.get("code") in ("MODEL_CAPPED", "MODEL_PARKED",
                                             "INFERENCE_CAP_ERROR",
                                             "ENTITLEMENT_ERROR",
                                             "DAILY_FREE_LIMIT",
                                             "FREE_TIER_LIMIT",
                                             "UNUSUAL_ACTIVITY"):
            return reason
        return None

    async def release(self, account: Account) -> None:
        async with self._lock:
            account.in_flight = max(0, account.in_flight - 1)
            self._slot_gen += 1     # wake acquire() waiters

    # -- state transitions ----------------------------------------------- #

    async def on_success(self, account: Account) -> None:
        async with self._lock:
            account.error_count = 0
            account.last_error = ""
            account.auth_strikes = 0
            if account.state is AccountState.COOLING:
                account.state = AccountState.READY

    async def restore(self, account: Account) -> None:
        """A retired account's balance recovered: back into rotation."""
        async with self._lock:
            account.paid_exhausted = False
            if account.state is AccountState.EXHAUSTED:
                account.state = AccountState.READY
            account.last_error = ""

    async def cool(self, account: Account, seconds: int | None = None,
                   reason: str = "") -> None:
        async with self._lock:
            account.state = AccountState.COOLING
            account.cooldown_until = time.time() + (seconds or self.cfg.cooldown_seconds)
            account.error_count += 1
            account.last_error = reason or "cooling"
            account.error_history.append(
                {"ts": time.time(), "status": None, "code": reason or "cooling",
                 "message": "", "state": "cooling"})

    async def retire(self, account: Account, reason: str = "insufficient_credits",
                     paid_only: bool = True) -> None:
        """402. Credit exhaustion is per model-class: by default only the paid
        lane is retired, because zcode-free/* models do not consume credits."""
        async with self._lock:
            account.last_error = reason
            if paid_only:
                account.paid_exhausted = True
                if account.state is AccountState.COOLING:
                    account.state = AccountState.READY
            else:
                account.state = AccountState.EXHAUSTED

    async def kill(self, account: Account, reason: str = "") -> None:
        async with self._lock:
            account.state = AccountState.DEAD
            account.last_error = reason

    async def kill_after_strikes(self, account: Account, reason: str = "",
                                 strikes: int = 3) -> None:
        """Kill only after N consecutive auth failures; earlier ones just cool.

        A single transient upstream 401 blip used to permanently kill accounts
        whose token never rotates on disk (admin-added / generator / device
        login). Those accounts now cool for the normal cooldown window and get
        re-tried; only repeated failures escalate to DEAD."""
        async with self._lock:
            n = getattr(account, 'auth_strikes', 0) + 1
            account.auth_strikes = n
            if n >= strikes:
                account.state = AccountState.DEAD
                account.last_error = reason
                account.error_history.append(
                    {"ts": time.time(), "status": 401, "code": "auth_dead",
                     "message": reason, "state": "dead"})
            else:
                account.state = AccountState.COOLING
                account.cooldown_until = time.time() + self.cfg.cooldown_seconds
                account.last_error = f"{reason} (auth strike {n}/{strikes})"
                account.error_history.append(
                    {"ts": time.time(), "status": 401, "code": f"auth_strike_{n}",
                     "message": reason, "state": "cooling"})

    async def enable(self, account_id: str) -> bool:
        async with self._lock:
            acc = self._accounts.get(account_id)
            if not acc:
                return False
            acc.state = AccountState.READY
            acc.paid_exhausted = False
            acc.cooldown_until = 0.0
            acc.error_count = 0
            acc.last_error = ""
            return True

    async def upsert(self, account: Account) -> None:
        async with self._lock:
            self._accounts[account.id] = account

    async def remove(self, account_id: str) -> bool:
        async with self._lock:
            return self._accounts.pop(account_id, None) is not None

    async def find(self, account_id: str) -> Account | None:
        async with self._lock:
            return self._accounts.get(account_id)

    # -- reporting -------------------------------------------------------- #

    async def snapshot(self) -> dict:
        accounts = await self.all()
        counts: dict[str, int] = {}
        for a in accounts:
            counts[a.state.value] = counts.get(a.state.value, 0) + 1
        return {
            "strategy": self.cfg.strategy,
            "accounts": len(accounts),
            "by_state": counts,
            "ready": sum(1 for a in accounts if a.is_ready()),
            "ready_paid": sum(1 for a in accounts if a.is_ready(require_paid=True)),
            "paid_exhausted": sum(1 for a in accounts if a.paid_exhausted),
            "detail": [a.to_public() for a in accounts],
        }

    async def ready_count(self, require_paid: bool = False) -> int:
        return sum(1 for a in await self.all() if a.is_ready(require_paid=require_paid))
