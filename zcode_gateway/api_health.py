"""Health, readiness and metrics."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request):
    state = request.app.state.app_state
    ready_count = await state.pool.ready_count()
    if ready_count == 0:
        # orchestrators decide readiness from the status code; a 200 with
        # ready=false kept them routing to a dead gateway
        return JSONResponse(
            status_code=503,
            content={"ready": False, "accounts_ready": 0})
    return {"ready": True, "accounts_ready": ready_count}


@router.get("/metrics")
async def metrics(request: Request, since: int | None = None) -> dict:
    """JSON metrics (Prometheus text would need a client library).

    `since` bounds the usage window in seconds; default is the last 24h so a
    long-lived DB does not turn every scrape into a full-table scan. Pass
    since=0 for all-time.
    """
    state = request.app.state.app_state
    snapshot = await state.pool.snapshot()
    window = 86_400 if since is None else (since or None)
    summary = await asyncio.to_thread(state.store.summary, since_seconds=window)
    return {
        "pool": {
            "accounts": snapshot["accounts"],
            "ready": snapshot["ready"],
            "by_state": snapshot["by_state"],
        },
        "usage": summary,
        "usage_window_s": window,
    }
