"""Timezone-aware account-cap formatting without desktop dependencies."""

import math
import time
from datetime import datetime, timezone


def format_cap_release(info: dict, now: float | None = None, *, exact=False) -> str:
    now = time.time() if now is None else now
    try:
        dt = datetime.fromisoformat(info.get("release_at"))
        if dt.tzinfo is None:
            raise ValueError("ambiguous timestamp")
    except (TypeError, ValueError):
        seconds = info.get("release_in_s")
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
            return "Reset time unavailable"
        dt = datetime.fromtimestamp(now + max(0, seconds), timezone.utc)
    local = dt.astimezone()
    seconds = max(0, math.ceil(dt.timestamp() - now))
    minutes = math.ceil(seconds / 60)
    left = (f"{minutes // 60}h {minutes % 60}m" if minutes >= 60
            else f"{minutes}m" if seconds >= 60 else f"{seconds}s")
    if exact and seconds >= 60:
        left += f" ({seconds}s)"
    offset = local.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}"
    return (f"{local:%Y-%m-%d %H:%M:%S} {zone} \u00b7 "
            + (f"in {left}" if seconds else "Reset due"))


def format_caps(account: dict, now: float | None = None, *, detail=False) -> str:
    now = time.time() if now is None else now
    caps = account.get("model_caps") or {}
    models = sorted(set(caps) | set(account.get("capped_models") or []))
    lines = []
    for model in models:
        info = caps.get(model) or {}
        name = model if detail else model.rsplit("/", 1)[-1]
        line = f"{name} \u2192 {format_cap_release(info, now, exact=detail)}" if info else name
        if detail:
            if info.get("code"):
                line += f"\n  {info['code']}"
            if info.get("message"):
                line += f"\n  {info['message']}"
        lines.append(line)
    return ("\n\n" if detail else "; ").join(lines) or "\u2014"
