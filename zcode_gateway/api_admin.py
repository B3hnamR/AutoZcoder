"""Admin API: pool inspection and control."""

from __future__ import annotations

import asyncio
import logging

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .deps import admin_key, get_state
from .pool import Account, load_accounts
from .tokens import _parse_expiry, persist_pool_snapshot

log = logging.getLogger("zcode_gateway.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


def _admin_expiry(value: Any) -> int:
    """Parse an admin expiry and reject malformed values as client errors."""
    if value is None or value == "":
        return 0
    # an explicit 0 means "no expiry" (zcode JWTs carry no exp) — valid, not junk
    if isinstance(value, (int, float)) and int(value) == 0:
        return 0
    if isinstance(value, str) and value.strip() in ("0",):
        return 0
    parsed = _parse_expiry(value)
    if not parsed:
        raise HTTPException(status_code=400,
                            detail="expiresAt must be an ISO date or epoch time")
    return parsed


@router.get("/pool/state")
async def pool_state(request: Request, key: str = Depends(admin_key)) -> dict:
    return await get_state(request).pool.snapshot()


@router.get("/accounts")
async def list_accounts(request: Request, key: str = Depends(admin_key)) -> dict:
    accounts = await get_state(request).pool.all()
    return {"accounts": [a.to_public() for a in accounts]}


@router.post("/accounts")
async def add_account(
    request: Request,
    body: dict[str, Any] = Body(...),
    key: str = Depends(admin_key),
) -> dict:
    """Add an account. Accepts either an `auth` blob (Zcode shape) or flat fields."""
    state = get_state(request)

    if "auth" in body:
        auth = body["auth"]
    else:
        auth = {
            "accessToken": body.get("access_token") or body.get("accessToken"),
            "refreshToken": body.get("refresh_token") or body.get("refreshToken"),
            "expiresAt": body.get("expires_at") or body.get("expiresAt") or 0,
            "accountId": body.get("id") or body.get("accountId"),
            "metadata": {"userInfo": {"email": body.get("email", "")}},
        }

    if not isinstance(auth, dict):
        raise HTTPException(status_code=400, detail="auth must be an object")
    if not auth.get("accessToken"):
        raise HTTPException(status_code=400, detail="accessToken is required")

    email = ((auth.get("metadata") or {}).get("userInfo") or {}).get("email", "")
    account_id = auth.get("accountId") or body.get("id")
    if not account_id and email:
        # re-adding the same account after a token rotation must update the
        # existing entry, not mint a second one from the new token's prefix
        for existing in await state.pool.all():
            if existing.email and existing.email == email \
                    and existing.source == "admin":
                account_id = existing.id
                break
    if not account_id:
        # long hash prefix: 8 chars of a high-entropy token can collide across
        # accounts and upsert() would silently overwrite the existing entry
        import hashlib as _hashlib
        account_id = "manual-" + _hashlib.sha256(
            auth["accessToken"].encode()).hexdigest()[:24]
    else:
        existing = await state.pool.find(account_id)
        if existing is not None and email and existing.email \
                and existing.email != email:
            raise HTTPException(
                status_code=409,
                detail=f"id '{account_id}' already belongs to account "
                       f"'{existing.email}'; refusing to overwrite it with "
                       f"'{email}' — pick a different accountId")
    account = Account(
        id=account_id or f"manual-{auth['accessToken'][:8]}",
        email=email,
        access_token=auth["accessToken"],
        refresh_token=auth.get("refreshToken", ""),
        expires_at=_admin_expiry(auth.get("expiresAt")),
        source="admin",
    )
    await state.pool.upsert(account)
    return {"added": account.to_public()}


@router.delete("/accounts/{account_id}")
async def delete_account(request: Request, account_id: str,
                         key: str = Depends(admin_key)) -> dict:
    state = get_state(request)
    removed = await state.pool.remove(account_id)
    if not removed:
        raise HTTPException(status_code=404, detail="account not found")
    state.tokens.drop_lock(account_id)
    return {"removed": account_id}


@router.post("/accounts/{account_id}/refresh")
async def refresh_account(request: Request, account_id: str,
                          key: str = Depends(admin_key)) -> dict:
    """Refresh one account.

    First re-read its on-disk snapshot (a re-export after a plan change writes a
    fresh token — the pool entry must adopt it), then re-settle plan/balance so
    the dashboard shows the new quota immediately. Falls back to an OAuth token
    refresh only when the snapshot brought nothing new.
    """
    state = get_state(request)
    account = await state.pool.find(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    adopted = False
    try:
        from .accounts_dir import load_from_accounts_dir
        if state.cfg.accounts.source == "accounts_dir":
            fresh = await asyncio.to_thread(load_from_accounts_dir,
                                            state.cfg.accounts.dir)
            match = next((f for f in fresh if f.id == account_id), None)
            if match and match.access_token and \
                    match.access_token != account.access_token:
                await state.tokens.apply_credentials(
                    account, access_token=match.access_token,
                    refresh_token=match.refresh_token,
                    expires_at=match.expires_at)
                adopted = True
    except Exception:
        log.warning("snapshot re-read failed for %s", account_id, exc_info=True)

    # settle plan + balance so the UI reflects reality right away
    try:
        await state.tokens.check_account(account)
    except Exception:
        pass
    try:
        await state.tokens.fetch_plan(account)
    except Exception:
        pass

    if not adopted:
        try:
            await state.tokens.refresh(account)
        except Exception:
            pass
    return {"account": account_id, "adopted_snapshot": adopted,
            "state": account.to_public()}


@router.post("/accounts/{account_id}/enable")
async def enable_account(request: Request, account_id: str,
                         key: str = Depends(admin_key)) -> dict:
    ok = await get_state(request).pool.enable(account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="account not found")
    return {"enabled": account_id}


@router.post("/accounts/{account_id}/balance")
async def check_balance(request: Request, account_id: str,
                        key: str = Depends(admin_key)) -> dict:
    state = get_state(request)
    account = await state.pool.find(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    # full check, not a bare fetch: settles paid_exhausted + plan now
    await state.tokens.check_account(account)
    me = await state.tokens.fetch_me(account)
    return {"account_id": account_id, "balance_micro": account.balance_micro,
            "paid_exhausted": account.paid_exhausted, "has_plan": account.has_plan,
            "me": me, "quotas": account.notes.get("quotas", {}),
            "plans": account.notes.get("plans", [])}


@router.post("/pool/reload")
async def reload_pool(request: Request, key: str = Depends(admin_key)) -> dict:
    """Re-read the credential source (e.g. providers.json after a desktop login)."""
    state = get_state(request)
    try:
        # providers.json can be large and AV-scanned; keep it off the loop
        accounts = await asyncio.to_thread(load_accounts, state.cfg.accounts)
    except Exception as exc:
        # no str(exc): it can embed absolute paths and file-content snippets
        log.warning("pool reload failed from %s: %s",
                    state.cfg.accounts.source, exc)
        raise HTTPException(
            status_code=400,
            detail=f"could not load accounts from {state.cfg.accounts.source}"
                   f" ({exc.__class__.__name__})") from exc

    added = 0
    updated = 0
    for account in accounts:
        existing = await state.pool.find(account.id)
        if existing is None:
            await state.pool.upsert(account)
            added += 1
            # populate balance/plan immediately: the GUI (and quota_aware
            # routing) should see fresh data on import, not after the 300s
            # background poller. Best-effort — upstream may be unreachable.
            try:
                await state.tokens.check_account(account)
            except Exception:
                pass
            try:
                await state.tokens.fetch_plan(account)
            except Exception:
                pass
        elif account.access_token != existing.access_token or account.expires_at > existing.expires_at:
            # mutate under the account's refresh lock: writing the fields
            # directly raced concurrent requests building headers and any
            # in-flight token refresh
            await state.tokens.apply_credentials(
                existing,
                access_token=account.access_token,
                refresh_token=account.refresh_token,
                expires_at=account.expires_at)
            updated += 1
            # re-settle plan/balance too — a re-exported snapshot after a plan
            # change (e.g. Global Build issued) must surface here, otherwise the
            # dashboard keeps showing the stale plan until the poller catches up
            try:
                await state.tokens.check_account(existing)
            except Exception:
                pass
            try:
                await state.tokens.fetch_plan(existing)
            except Exception:
                pass
    return {"loaded": len(accounts), "added": added, "updated": updated,
            "pool": await state.pool.snapshot()}


@router.post("/pool/persist")
async def persist_pool(request: Request, key: str = Depends(admin_key)) -> dict:
    state = get_state(request)
    accounts = await state.pool.all()
    await asyncio.to_thread(persist_pool_snapshot, state.cfg, accounts)
    return {"persisted": len(accounts), "path": state.cfg.accounts.pool_file}


@router.get("/stats")
async def stats(request: Request, since: int = 0,
                key: str = Depends(admin_key)) -> dict:
    state = get_state(request)
    return await asyncio.to_thread(state.store.summary, since_seconds=since or None)

@router.get("/models/availability")
async def model_availability(request: Request, key: str = Depends(admin_key)) -> dict:
    """Per-account, per-model availability for the whole pool.

    Answers "which models can this account serve right now, and if not, why and
    until when" - the paid gate is per account (balance), the free gate is per
    model per account (daily cap).
    """
    from .availability import build_matrix

    state = get_state(request)
    if state.catalog is not None:
        await state.catalog.refresh_if_due()
    accounts = await state.pool.all()

    # Curated feed models, minus the client-facing aliases.
    models: list[str] = []
    seen = set()
    for entry in state.registry.catalogue():
        if entry.get("alias_of"):
            continue
        mid = entry["id"]
        if mid not in seen:
            seen.add(mid)
            models.append(mid)

    result = build_matrix(accounts, models, free_models=state.registry.free_models)
    result["catalog"] = state.catalog.status if state.catalog is not None else {
        "source": "snapshot", "updated_at": None, "error": None}
    return result


@router.post("/models/catalog/refresh")
async def refresh_model_catalog(request: Request,
                                key: str = Depends(admin_key)) -> dict:
    """Force a new feed fetch when the user presses Sync models."""
    state = get_state(request)
    if state.catalog is None:
        raise HTTPException(status_code=503, detail="model catalogue unavailable")
    return await state.catalog.refresh_if_due(force=True)
