"""Admin API: pool inspection and control."""

from __future__ import annotations

import asyncio
import logging
import json
import math
import time
import uuid

from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

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


@router.post("/accounts/import-account")
@router.post("/accounts/import-jwt", include_in_schema=False)
async def import_jwt_account(request: Request, body: dict[str, Any] = Body(...),
                             key: str = Depends(admin_key)) -> JSONResponse:
    """Validate an account transfer or CLI JWT before persisting it."""
    from .accounts_dir import save_account_snapshot
    from .upstream import build_headers
    from .zcode_detect import jwt_claims

    state = get_state(request)
    token = body.get("token")
    if not isinstance(token, str):
        raise HTTPException(400, "Account export or JWT token is required")
    token = token.strip()
    bundle = None
    if token.startswith("{"):
        if len(token) > 24576:
            raise HTTPException(400, "Account export is too large")
        try:
            bundle = json.loads(token)
        except ValueError:
            raise HTTPException(400, "Account export is not valid JSON") from None
        if not isinstance(bundle, dict) or bundle.get("format") != "autozcoder-account" or type(bundle.get("version")) is not int or bundle["version"] != 1:
            raise HTTPException(400, "Unsupported AutoZCoder account export")
        token = bundle.get("access_token")
        if not isinstance(token, str):
            raise HTTPException(400, "Account export contains no access token")
        token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or len(token) > 16384 or any(c.isspace() for c in token):
        raise HTTPException(400, "Enter a single JWT token (not the whole snapshot)")
    claims = jwt_claims(token)
    if not isinstance(claims, dict):
        raise HTTPException(400, "JWT payload must be an object")
    ident = claims.get("user_id") or claims.get("sub")
    if claims.get("user_id") and claims.get("sub") and claims["user_id"] != claims["sub"]:
        raise HTTPException(400, "JWT contains conflicting account IDs")
    try:
        ident = str(uuid.UUID(ident))
        exp = claims.get("exp", 0)
        if isinstance(exp, bool):
            raise ValueError()
        expiry = float(exp)
        if not math.isfinite(expiry) or expiry < 0 or expiry > 253402300799:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "JWT must contain a valid Zcode account ID and expiry") from None
    if expiry and expiry <= time.time():
        raise HTTPException(400, "JWT has expired; obtain a current token")
    if bundle and bundle.get("account_id") != ident:
        raise HTTPException(400, "Account export ID does not match its token")
    email = body.get("email") or (bundle.get("email", "") if bundle else "")
    if not isinstance(email, str) or len(email) > 254 or any(ord(c) < 32 or ord(c) == 127 for c in email):
        raise HTTPException(400, "Invalid email label")
    email = email.strip()
    if email and (email.count("@") != 1 or any(c.isspace() for c in email)):
        raise HTTPException(400, "Invalid email label")
    name = bundle.get("name", "") if bundle else ""
    device = bundle.get("device_mid", "") if bundle else ""
    if any(not isinstance(v, str) or len(v) > 254 or any(ord(c) < 32 or ord(c) == 127 for c in v)
           for v in (name, device)):
        raise HTTPException(400, "Invalid account export metadata")
    if state.cfg.accounts.source not in ("accounts_dir", "pool_file"):
        raise HTTPException(409, "Account import requires Accounts source: Accounts folder or Pool file")
    # Decoding a JWT is not signature validation. Require the configured Zcode
    # billing service to authenticate this exact credential; never use the chat
    # profile endpoint (CLI JWTs can be silently treated as guest sessions there).
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10),
                                     follow_redirects=False, trust_env=False) as client:
            response = await client.get(
                state.cfg.upstream.base_url.rstrip("/") + "/zcode-plan/billing/balance",
                params={"app_version": state.cfg.upstream.fingerprint.client_version},
                headers=build_headers(token, state.cfg.upstream.fingerprint, device_mid=device or None))
        if response.status_code in (401, 403):
            raise HTTPException(400, "Zcode rejected this token")
        response.raise_for_status()
        envelope = response.json()
        if not isinstance(envelope, dict) or envelope.get("code") != 0 or not isinstance(envelope.get("data"), dict):
            raise HTTPException(400, "Zcode did not accept this token as a billing account")
        from .quota import parse_balance
        plans, quotas = parse_balance(envelope["data"], time.time())
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        # Exceptions can embed upstream content/headers. Return no raw values.
        raise HTTPException(502, "Could not validate token with Zcode; nothing imported. Retry later.") from None

    if not hasattr(state, "account_persist_lock"):
        state.account_persist_lock = asyncio.Lock()
    # Serialize filename allocation and pool-file writes, and use the existing
    # per-account refresh lock so importing cannot race credential adoption.
    async with state.account_persist_lock, state.tokens._lock_for(ident):
        existing = await state.pool.find(ident)
        email = email or (existing.email if existing else "")
        account = Account(id=ident, email=email, access_token=token,
                          refresh_token=existing.refresh_token if existing else "", expires_at=int(expiry * 1000),
                          source="jwt-import", notes={"name": name or (existing.notes.get("name", "") if existing else ""),
                                                       "imported_jwt": True})
        if device:
            account.notes["device_mid"] = device
        account.notes.update(plans=plans, quotas=quotas, quota_check_attempted_at=time.time())
        account.balance_checked_at = time.time()
        account.plan_checked_at = account.balance_checked_at
        account.has_plan = account.plan_active()
        try:
            if state.cfg.accounts.source == "accounts_dir":
                folder = Path(state.cfg.accounts.dir)
                fields = dict(account_id=ident, email=email, name=account.notes["name"],
                                access_token=token, expires_at_ms=str(account.expires_at),
                                source="Manual JWT import", imported_jwt="true")
                if device:
                    fields["device_mid"] = device
                path = await asyncio.to_thread(save_account_snapshot, folder, fields)
                account.notes["file"] = path.name
            else:
                accounts = [a for a in await state.pool.all() if a.id != ident] + [account]
                await asyncio.to_thread(persist_pool_snapshot, state.cfg, accounts)
        except (OSError, ValueError):
            raise HTTPException(500, "Could not save account snapshot; nothing added to pool") from None
        await state.pool.upsert(account)
        current = await state.pool.find(ident)
        current.balance_checked_at = account.balance_checked_at
        current.plan_checked_at = account.plan_checked_at
        current.has_plan = account.has_plan
        current.notes.pop("quota_check_error", None)
        state.tokens._scan_at = 0
    return JSONResponse({"account": current.to_public(), "updated": existing is not None},
                        headers={"Cache-Control": "no-store"})


@router.get("/accounts/{account_id}/token")
async def current_account_token(request: Request, account_id: str,
                                 key: str = Depends(admin_key)) -> JSONResponse:
    """Explicit admin-only disclosure; never include bearer tokens in list APIs."""
    state = get_state(request)
    account = await state.pool.find(account_id)
    if account is None:
        raise HTTPException(404, "account not found")
    async with state.tokens._lock_for(account_id):
        return JSONResponse({"access_token": account.access_token, "expires_at": account.expires_at},
                            headers={"Cache-Control": "no-store, private", "Pragma": "no-cache"})


@router.get("/accounts/{account_id}/export")
async def export_account(request: Request, account_id: str,
                         key: str = Depends(admin_key)) -> JSONResponse:
    """A credential transfer, not a new account or a token-renewal grant."""
    state = get_state(request)
    account = await state.pool.find(account_id)
    if account is None:
        raise HTTPException(404, "account not found")
    async with state.tokens._lock_for(account_id):
        bundle = {"format": "autozcoder-account", "version": 1,
                  "account_id": account.id, "email": account.email,
                  "name": account.notes.get("name", ""),
                  "device_mid": account.notes.get("device_mid", ""),
                  "access_token": account.access_token, "expires_at_ms": account.expires_at}
        return JSONResponse(bundle, headers={"Cache-Control": "no-store, private", "Pragma": "no-cache"})


@router.post("/accounts/purge_drained")
async def purge_drained_accounts(request: Request,
                                 key: str = Depends(admin_key)) -> dict:
    """Remove drained AND dead accounts.

    Drained: quota data exists AND every bucket is empty with no pending
    reset. Dead: state is DEAD (plan lost / credential invalid / auth
    strikes) — the user clicking purge after seeing red accounts expects
    them GONE, files included; keeping them as "unknown" (empty quotas)
    left plan-lost zombies un-purgeable.
    Takes them out of rotation, deletes their snapshot file from the
    accounts/ folder, and drops token locks."""
    state = get_state(request)
    from .pool import AccountState
    import time as _time
    purged, kept_unknown = [], 0
    for account in list(state.pool._accounts.values()):
        quotas = (account.notes or {}).get("quotas") or {}
        is_dead = account.state is AccountState.DEAD
        # proven-drained: quota data exists AND every bucket is empty
        drained = bool(quotas) and all(
            (q.get("remaining", 0) or 0) <= 0 for q in quotas.values())
        # any model still capped-with-reset means tokens come back later
        recovers = any(
            (q.get("resets_at", 0) or 0) > _time.time()
            for q in quotas.values())
        if not is_dead and not drained:
            if not quotas:
                kept_unknown += 1
            continue
        if not is_dead and recovers:
            continue          # daily quota: it will refill — keep the account
        # 1) drop from the running pool
        await state.pool.remove(account.id)
        state.tokens.drop_lock(account.id)
        # 2) delete the snapshot file so a restart doesn't resurrect it
        fname = (account.notes or {}).get("file")
        deleted = False
        if fname:
            snap = Path(state.cfg.accounts.dir) / fname
            try:
                if snap.is_file():
                    snap.unlink()
                    deleted = True
            except OSError:
                pass
        purged.append({"id": account.id, "email": account.email,
                       "file_deleted": deleted})
    return {"purged": purged, "count": len(purged),
            "unknown_state_kept": kept_unknown}


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
            "balance_checked_at": account.balance_checked_at,
            "quota_check_error": account.notes.get("quota_check_error", ""),
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
                expires_at=account.expires_at, email=account.email,
                name=account.notes.get("name"), device_mid=account.notes.get("device_mid"),
                notes=account.notes)
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
    if not hasattr(state, "account_persist_lock"):
        state.account_persist_lock = asyncio.Lock()
    async with state.account_persist_lock:
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

# ---- per-account 100M claim (Accounts tab) --------------------------------- #
# Runs the standalone claim (generator/claim100.py) for one account, either
# through a pool proxy (default; pinned proxy first) or on the direct IP.


@router.post("/accounts/{account_id}/claim100m")
async def claim_100m(request: Request, account_id: str, body: dict = Body(default={}),
                     key: str = Depends(admin_key)) -> dict:
    state = get_state(request)
    account = await state.pool.find(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    email = (account.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="account has no email on file")
    mode = "direct" if str(body.get("mode") or "").lower() == "direct" else "proxy"
    from .api_dash import _generator
    mgr = _generator(request)
    if mgr is None:
        raise HTTPException(status_code=400, detail="generator manager unavailable")
    # claims touch disk and browsers; keep the loop free
    return await asyncio.to_thread(mgr.start_claim, email, mode)


@router.post("/accounts/{account_id}/claim_start")
async def claim_start_plan(request: Request, account_id: str,
                           body: dict = Body(default={}),
                           key: str = Depends(admin_key)) -> dict:
    """Claim the Start Plan from the billing preview for one account."""
    state = get_state(request)
    account = await state.pool.find(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    email = (account.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="account has no email on file")
    mode = "direct" if str(body.get("mode") or "").lower() == "direct" else "proxy"
    from .api_dash import _generator
    mgr = _generator(request)
    if mgr is None:
        raise HTTPException(status_code=400, detail="generator manager unavailable")
    return await asyncio.to_thread(mgr.start_claim_start, email, mode)


@router.post("/accounts/{account_id}/claim500m")
async def claim_500m(request: Request, account_id: str, body: dict = Body(default={}),
                     key: str = Depends(admin_key)) -> dict:
    """Legacy 500M giveaway plan (1024 Dev Day) claim for one account."""
    state = get_state(request)
    account = await state.pool.find(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    email = (account.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="account has no email on file")
    mode = "direct" if str(body.get("mode") or "").lower() == "direct" else "proxy"
    from .api_dash import _generator
    mgr = _generator(request)
    if mgr is None:
        raise HTTPException(status_code=400, detail="generator manager unavailable")
    return await asyncio.to_thread(mgr.start_claim500, email, mode)


@router.post("/claims/stop")
async def claim_stop(request: Request, body: dict = Body(...),
                     key: str = Depends(admin_key)) -> dict:
    name = str(body.get("name") or "")
    from .api_dash import _generator
    mgr = _generator(request)
    if mgr is None:
        raise HTTPException(status_code=404, detail="generator manager unavailable")
    ok = mgr.stop_claim(name)
    if not ok:
        raise HTTPException(status_code=404, detail="no such claim")
    return {"stopped": name}


@router.post("/claims/remove")
async def claim_remove(request: Request, body: dict = Body(...),
                       key: str = Depends(admin_key)) -> dict:
    name = str(body.get("name") or "")
    from .api_dash import _generator
    mgr = _generator(request)
    if mgr is None:
        raise HTTPException(status_code=404, detail="generator manager unavailable")
    ok = mgr.remove_claim(name)
    if not ok:
        raise HTTPException(status_code=404, detail="no such claim")
    return {"removed": name}


@router.post("/claims/clear")
async def claim_clear(request: Request, key: str = Depends(admin_key)) -> dict:
    from .api_dash import _generator
    mgr = _generator(request)
    if mgr is None:
        raise HTTPException(status_code=404, detail="generator manager unavailable")
    n = mgr.clear_finished_claims()
    return {"cleared": n}


@router.post("/claims/claim_all_pending")
async def claim_all_pending(request: Request, body: dict = Body(default={}),
                            key: str = Depends(admin_key)) -> dict:
    """Sequentially claim the Start Plan for every pending account.
    Runs in the background: returns immediately."""
    mode = "proxy" if str(body.get("mode") or "").lower() == "proxy" else "direct"
    from .api_dash import _generator
    mgr = _generator(request)
    return mgr.start_claim_all(mode=mode)


@router.get("/claims/claim_all_status")
async def claim_all_status(request: Request, key: str = Depends(admin_key)) -> dict:
    from .api_dash import _generator
    mgr = _generator(request)
    return {**mgr.claim_all_status(), "claims": mgr.claims_public()}


@router.get("/claims/schedule")
async def claim_schedule_status(request: Request, key: str = Depends(admin_key)) -> dict:
    from .api_dash import _generator
    mgr = _generator(request)
    scheduler = getattr(mgr, "claim_scheduler", None)
    return scheduler.status() if scheduler is not None else {"enabled": False}


@router.post("/claims/schedule/run")
async def claim_schedule_run(request: Request, key: str = Depends(admin_key)) -> dict:
    """Claim now, without waiting for the daily time."""
    from .api_dash import _generator
    mgr = _generator(request)
    scheduler = getattr(mgr, "claim_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=404, detail="claim scheduler unavailable")
    return scheduler.run_now()


@router.post("/claims/claim_all_stop")
async def claim_all_stop(request: Request, key: str = Depends(admin_key)) -> dict:
    mgr = getattr(get_state(request), "generator", None)
    return await asyncio.to_thread(mgr.stop_claim_all) if mgr else {"state": "idle", "running": False}


@router.get("/dash/proxy/socks_ready")
async def proxy_socks_ready(request: Request, key: str = Depends(admin_key)) -> dict:
    """Live socks count + last verified time for the claim health badge."""
    from .api_dash import _generator
    mgr = _generator(request)

    def _read():
        return {"socks_ready": mgr.socks_ready_count(),
                "last_verified_at": mgr.checker.last_verified_at,
                "auto_verify": mgr.checker.auto_verify,
                "auto_interval_min": mgr.checker.auto_interval_min}

    return await asyncio.to_thread(_read)


@router.post("/dash/proxy/auto_verify")
async def proxy_auto_verify(request: Request, body: dict = Body(...),
                            key: str = Depends(admin_key)) -> dict:
    """Toggle the background pool re-verify loop."""
    from .api_dash import _generator
    mgr = _generator(request)
    enabled = bool(body.get("enabled"))
    interval = body.get("interval_min")
    return mgr.checker.set_auto_verify(enabled, interval)


@router.get("/dash/usage")
async def usage_dashboard(request: Request, key: str = Depends(admin_key)) -> dict:
    """Usage stats for the dashboard: totals + daily buckets."""
    state = get_state(request)
    summary = await asyncio.to_thread(state.store.summary, 86400 * 30)
    summary_all = await asyncio.to_thread(state.store.summary)
    daily = await asyncio.to_thread(state.store.daily_buckets, 14)
    return {"last_30d": summary, "all_time": summary_all, "daily": daily}


def raise404(name):
    from fastapi import HTTPException as _H
    raise _H(status_code=404, detail="no such claim")
