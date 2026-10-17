"""Per-account, per-model availability.

Two independent gates decide whether an account can serve a model:

* **paid models** (usage-billed) need credit: `balance > 0`. One flag per account.
* **free models** are capped **per model, per account**. An account can be out of
  `zai/glm-5.3-flash` while every other free model still works, and a different
  account may have glm available at the same moment.

So availability is a matrix, not a single flag. This module builds it.
"""

from __future__ import annotations

import time
from typing import Collection, Iterable

from .pool import Account, AccountState
from .registry import model_lane
from .upstream import is_entitlement_code

# status values, worst-last so callers can rank them
AVAILABLE = "available"
COOLING = "cooling"            # account in a short transient backoff
CAPPED = "capped"              # this model hit its free-tier daily cap
NO_ENTITLEMENT = "no_entitlement"   # plan does not cover this model
NO_CREDIT = "no_credit"        # paid model, account out of credit
UNAVAILABLE = "unavailable"    # account is dead / out of rotation

# ranking for a model's overall status across the pool
_STATUS_RANK = {
    AVAILABLE: 0, COOLING: 1, CAPPED: 2, NO_ENTITLEMENT: 3,
    NO_CREDIT: 4, UNAVAILABLE: 5,
}


def account_model_status(account: Account, model: str,
                         free_models: Collection[str] | None = None) -> dict:
    """Why can (or can't) this account serve this model right now?"""
    if account.state is AccountState.DEAD:
        return {"status": UNAVAILABLE, "reason": "account is dead"}
    if account.state is AccountState.EXHAUSTED:
        return {"status": UNAVAILABLE, "reason": "account exhausted"}
    if not account.access_token:
        return {"status": UNAVAILABLE, "reason": "account has no access token"}

    if not account.plan_active():
        return {"status": NO_ENTITLEMENT, "reason": "ZCode plan expired or inactive"}
    if not account.model_eligible(model):
        return {"status": CAPPED, "reason": "no available ZCode model quota"}

    if account.is_model_capped(model):
        info = account.model_cap_info.get(model) or {}
        until = account.model_caps.get(model, 0.0)
        code = (info.get("code") or "unknown")
        return {
            "status": NO_ENTITLEMENT if is_entitlement_code(code) else CAPPED,
            "reason": code,
            "message": info.get("message", ""),
            "release_at": info.get("release_at"),
            "release_in_s": int(max(until - time.time(), 0)),
        }

    lane = model_lane(model, free_models)

    if lane == "plan":
        # subscription-gated: a plan record is what matters, not balance
        if account.has_plan is False:
            return {"status": NO_ENTITLEMENT,
                    "reason": "no subscription plan on this account "
                              "(/users/me/plan -> 404)"}
    elif lane == "usage":
        if account.paid_exhausted:
            return {"status": NO_CREDIT,
                    "reason": "paid lane exhausted (balance <= 0)"}

    if (account.state is AccountState.COOLING
            and time.time() < account.cooldown_until):
        return {"status": COOLING, "reason": account.last_error or "cooling",
                "release_in_s": int(max(account.cooldown_until - time.time(), 0))}
    # an expired cooldown is effectively ready (routing already treats it so);
    # reporting "cooling" forever made dashboards disagree with the router

    reason = {
        "free": "ZCode coding plan (daily model quota)",
        "plan": "subscription active",
        "usage": "credit balance is positive",
    }.get(lane, "available")
    return {"status": AVAILABLE, "reason": reason}


def model_availability(accounts: Iterable[Account], model: str,
                       free_models: Collection[str] | None = None) -> dict:
    """Per-account breakdown for one model, plus the pool-wide verdict."""
    rows = []
    for account in accounts:
        row = account_model_status(account, model, free_models)
        row["account_id"] = account.id
        row["email"] = account.email
        rows.append(row)

    serving = [r for r in rows if r["status"] == AVAILABLE]
    if not rows:
        overall = UNAVAILABLE
    elif len(serving) == len(rows):
        overall = "available"          # every account can serve it
    elif serving:
        overall = "partial"            # at least one, but not all
    else:
        overall = max((r["status"] for r in rows), key=lambda s: _STATUS_RANK[s])

    return {
        "model": model,
        "is_free": model_lane(model, free_models) == "free",
        "lane": model_lane(model, free_models),
        "available_on": len(serving),
        "total_accounts": len(rows),
        "overall": overall,
        # soonest moment this model frees up anywhere, if it is only capped
        "next_release_in_s": min(
            (r["release_in_s"] for r in rows
             if r["status"] in (CAPPED, NO_ENTITLEMENT) and r.get("release_in_s")),
            default=None),
        "accounts": rows,
    }


def build_matrix(accounts: Iterable[Account], models: Iterable[str],
                 free_models: Collection[str] | None = None) -> dict:
    """Full availability snapshot for the pool."""
    accounts = list(accounts)
    entries = [model_availability(accounts, m, free_models) for m in models]

    return {
        "generated_at": time.time(),
        "accounts": [
            {
                "id": a.id,
                "email": a.email,
                "state": a.state.value,
                "paid_available": not a.paid_exhausted,
                "balance_micro": a.balance_micro,
                "capped_models": sorted(m for m, until in a.model_caps.items()
                                        if until > time.time()),
            }
            for a in accounts
        ],
        "models": entries,
        "summary": {
            "total_models": len(entries),
            "free_models": sum(1 for e in entries if e["is_free"]),
            "paid_models": sum(1 for e in entries if not e["is_free"]),
            # total_accounts > 0 guard: on an empty pool, available_on ==
            # total_accounts (0 == 0) counted the same model as fully
            # available AND unavailable
            "fully_available": sum(1 for e in entries
                                   if e["total_accounts"] > 0
                                   and e["available_on"] == e["total_accounts"]),
            "partially_available": sum(1 for e in entries
                                       if 0 < e["available_on"] < e["total_accounts"]),
            "unavailable": sum(1 for e in entries if e["available_on"] == 0),
        },
    }
