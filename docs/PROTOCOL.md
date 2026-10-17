# ZCode 3.14.4 protocol notes

Evidence: the user's six model captures and OAuth/billing flows from
`D:\JB\zcode-mitm`, plus the locally installed
`C:\Program Files\ZCode\resources\glm\zcode.cjs` and `resources\app.asar`.
No live credential values are included in this document or the committed fixtures.

## Messages

`POST https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages`

The same coding-plan JWT is sent as `Authorization: Bearer ...` and `x-api-key`.
Anthropic version is `2023-06-01`; beta is
`mid-conversation-system-2026-04-07`. Fingerprinting matches the captured
ZCode UA, version, platform, agent and session headers. Native Anthropic blocks
are kept intact. OpenAI requests are converted at the transport boundary.
Both routes include ZCode's default identity and harness system blocks. Existing
native bootstrap blocks are preserved without duplication; caller instructions
follow the defaults. No captured workspace context or extra tools are injected.
Model requests include HTTP-Referer and carry the saved ZCode device identity
in metadata; billing/OAuth requests also include X-Device-Mid. See
[the 3012 investigation](DIAGNOSIS-3012.md) for the resolved initialization issue.
The coding-plan catalog is separate from `chat.z.ai/api/models` and the API-key
providers. Only GLM-5.3 and GLM-5.3-Flash were validated in the supplied captures.

## OAuth: correction to the audit prose

The installed client's `Xdn`/OAuth broker implementation generates a 32-byte
random hex poll token **before** `POST /oauth/cli/init`. It sends that value as
Bearer authorization, with JSON `{ "provider": "zai" }`. The response returns
`flow_id`, `poll_token`, `authorize_url`, `expires_at`, `poll_interval_sec`.
Polling `/oauth/cli/poll/{flow_id}` uses the returned poll token as Bearer auth.
Poll states include pending, failed and ready. The ready response contains the
coding-plan JWT and user identity. No WorkOS exchange, short user code or
secondary business token exchange is needed.

The captured coding-plan JWT has no `exp` and no refresh token. ZcodeGate stores
expiry zero as long-lived. A 401 may recover only by loading a changed credential
from its source; otherwise the account is marked dead and needs login/import.

## Credential encryption: correction to the audit prose

The local client implements `enc:v1:<iv>.<tag>.<ciphertext>`, each part base64url.
The cipher is AES-256-GCM with a 12-byte nonce and 16-byte authentication tag.
Its key is SHA-256 of `ZCODE_CREDENTIAL_SECRET`, if supplied, otherwise of
`zcode-credential-fallback:<node platform>:<home directory>:<username>`.
This is the implementation in the installed source, rather than the audit's
Electron safeStorage description. The port uses authenticated Windows CNG
and verifies a known AES-GCM vector and tamper rejection in tests.

## Quotas

The observed working URL is `/zcode-plan/billing/balance?app_version=3.14.4`
(the slash form, not `billing_balance`). The device/client headers are required.
A successful business envelope has `code: 0` and data with plans and balances.
Buckets are aggregated by GLM model only for active, unexpired plans.
`available_units` takes precedence over remaining_units, with daily period_end
kept separately from the plan's ends_at. A depleted model does not park the
other model. No stale captured trial dates or token balances are hard-coded.

## Failure behavior

401 requires credentials; 402/code 11113 parks the affected model until its
known quota reset; 429 uses Retry-After or a short cooldown; 403 parks only the
model's entitlement. Transport and server failures can rotate before output.
Model overload (HTTP 529, code 1305, or overloaded_error) uses a separate retry
budget: 11 attempts by default, 2-second exponential base delay, 60-second maximum
and 50-100% jitter. These defaults match the installed ZCode client's model retry
options and delay calculation. Retry-After/Retry-After-Ms are respected within
the maximum delay. Each rejected response closes and releases its reservation
before waiting. Overload does not cool an account, consume its quota locally or
park the model; another model stays routable. Quota/authentication failover keeps
its separate account-attempt limit. Client disconnects stop delayed retries.

Once content is emitted, failures are surfaced without replaying the request.
Truncated streams are errors; account reservations and HTTP responses close on
success, failure, cancellation and early consumer disconnect.

Live quota and inference checks succeeded after the native system bootstrap was
added. Flash rejected disabled thinking with code 1210. The adapter supports
low/high/max and returns a clear 400 for unsupported off/none requests. Persistent
model overload is returned as 529/code 1305 after bounded retries.
