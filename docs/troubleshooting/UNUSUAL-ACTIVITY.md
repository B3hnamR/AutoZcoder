# 405 / code 3012 — "request has been blocked due to unusual activity"

Observed 2026-10-15 against the coding-plan Messages endpoint. A per-account,
per-model abuse rejection that is distinct from the earlier bootstrap 3012.

## Wire shape

```json
{
  "error": {
    "message": "request has been blocked due to unusual activity.",
    "type": "api_error",
    "param": null,
    "code": "3012"
  }
}
```

Served with HTTP **405** and `content-type: application/json`. It also arrives
as an SSE `error` event mid-handshake, which is why the handling lives in both
the JSON path and the initial-stream path of `ChatService`.

## Live probe (2026-10-15)

Each imported account was probed once per model with a minimal reasoning body:

| account | GLM-5.3 | GLM-5.3-Flash |
| --- | --- | --- |
| dahlia.balistreri.4745@bluenode.cc | 405 / 3012 | 200 |
| essential.bat.oxli@hidingpost.com | 405 / 3012 | 200 |
| zai4p0iupp70z@<redacted-owner-domain> | 405 / 3012 | 502 / 1005 (quota) |
| zai9y9838zs8h@<redacted-owner-domain> | 405 / 3012 | 502 / 1005 (quota) |

GLM-5.3 request-shape variants (effort low/high, with/without a caller system
message, 64–4096 max_tokens) all returned 405/3012 on a healthy account, so the
block is **model-scoped**, not body-scoped.

## Behaviour in the gateway

`3012` is classified by `upstream.is_block_code` and treated as a **per-model
park**, not an account failure:

- `ChatService._apply_error` calls `pool.cap_model(..., reason='UNUSUAL_ACTIVITY')`
  for `blocked_model_cooldown_seconds` (default 900 s). The account stays
  `READY` and keeps serving every other model.
- The request rotates to the next account in the same call instead of failing.
- Model fallback (`models.auto_free_fallback`) is **off by default**. When it is
  enabled (`models.auto_free_fallback.enabled=true`, with a `chain`), the chain
  now also advances on this code, so a blocked model can be served by its
  fallback. It is exposed as a toggle in the desktop Settings and the web
  Settings tab.
- Use `pool.strategy: drain_first` to keep hammering one account until its quota
  is gone before moving on. Spreading one model across every account at once is
  what tends to get a whole pool flagged together; drain-first avoids that.
- When every account is parked for the model, the caller receives the real
  `405 / 3012` (after the retry budget), and the dashboard shows the model under
  "capped models" with the `UNUSUAL_ACTIVITY` reason and its local reset time.

This does not fix the upstream block; it keeps the gateway usable by moving off
the blocked account/model lane and reporting the true cause.
