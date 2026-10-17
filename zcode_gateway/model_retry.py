"""Retry model overloads before output, using the native client's backoff."""
from __future__ import annotations

import asyncio
import math
import random
import time
from email.utils import parsedate_to_datetime


def is_model_overload(status, code):
    return status == 529 or str(code) in ('1305', 'overloaded_error')


def retry_after_seconds(headers):
    """Accept the native client's millisecond, seconds and HTTP-date headers."""
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    for key, scale in [('retry-after-ms', .001), ('retry-after', 1)]:
        value = headers.get(key)
        if value is None:
            continue
        try:
            seconds = float(value) * scale
        except (TypeError, ValueError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return None


def overload_delay(cfg, retry_number, retry_after=None):
    base = cfg.overload_base_delay_seconds * 2 ** (retry_number - 1)
    # Bound server-supplied delays too, so retry waits remain cancellable and finite.
    if retry_after is not None:
        return min(retry_after, cfg.overload_max_delay_seconds)
    delay = min(base, cfg.overload_max_delay_seconds)
    return delay * random.uniform(.5, 1)


async def wait_for_retry(delay, is_disconnected=None):
    if is_disconnected is None:
        await asyncio.sleep(delay)
        return
    # FastAPI doesn't cancel a handler waiting for response headers on disconnect.
    # Check during backoff so cancelling a chat doesn't keep sending retries.
    while delay > 0:
        if await is_disconnected():
            raise asyncio.CancelledError()
        part = min(delay, .5)
        await asyncio.sleep(part)
        delay -= part
