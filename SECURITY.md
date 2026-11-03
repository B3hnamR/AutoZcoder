# Security notes

Read this before exposing the gateway to anything beyond loopback.

## The admin key is code execution

The admin API can:

- point the generator at **any directory containing `main.py`** and run it as a subprocess (`/admin/dash/generator/locate` — by design, for legitimate generator checkouts),
- download and swap the running binary (self-update),
- rewrite `config.yaml`,
- stop the process.

Treat the admin key as code-execution authority for the gateway's OS user (root
if you run the gateway as root). Never put it in a URL, a screenshot, or a chat.
The Linux installer normally uses an unprivileged service user, keeps the gateway
binary and private Node installation root-owned, and runs generator-environment,
browser and npm maintenance as an unprivileged tooling user rather than executing
service-owned tooling as root.

## Default posture (local use)

- Binds `127.0.0.1` only. The dashboard, admin API, and client API are reachable from the local machine alone.
- Per-install random `admin_key` / `client_keys` are generated on first launch — there is no shared default key.
- No CORS: a visited website cannot read responses from the gateway. Cross-origin **mutations** (CSRF / DNS-rebinding shapes) are rejected before auth (`Origin` / `Sec-Fetch-Site` guard in `app.py`); non-browser clients (curl, SDKs) are unaffected.
- An **empty** configured `admin_key` authenticates nothing (fail closed), and `ServerConfig` rejects keys shorter than 8 characters at load time.
- `/health`, `/ready`, `/metrics` carry no secrets and answer to local peers only.

## VPS / non-loopback binds (enforced at startup)

Starting the gateway with `server.host` other than loopback requires, or the process refuses to boot:

1. `require_client_key: true` **and** at least the configured key set — an unauthenticated proxy on a public interface is an open relay.
2. A changed `admin_key` (the well-known default is rejected).

Additional runtime behavior on public binds:

- Admin endpoints answer **local peers only** unless `server.allow_remote_admin: true` is set explicitly.
- `/health`, `/ready`, `/metrics` return 404 to non-local peers unless `server.expose_ops: true` (they leak pool and usage state).

"Local peer" means the actual visitor, not the TCP connection: when a same-box
reverse proxy (Caddy/nginx) forwards traffic, uvicorn applies `X-Forwarded-For`
from trusted loopback proxies, so an internet visitor behind the proxy is
treated as remote and a local one as local. **The proxy must pass
`X-Forwarded-For` through** (Caddy and nginx do by default). A proxy that
strips it makes every visitor indistinguishable from a local process — do not
front the gateway with such a proxy while relying on these gates.

Put TLS in front (Caddy/nginx + Let's Encrypt, or a tunnel such as cloudflared/WireGuard) and keep the gateway itself on `127.0.0.1` bound to the tunnel when possible.

## Edge relays

Relays deployed through the dashboard are **not** open proxies:

- workers accept `x-relay-target` only for `https://` hosts on a fixed allowlist (`zcode.z.ai` for traffic, `httpbin.org` for the health ping),
- each relay carries a per-relay secret; the gateway sends it as `x-relay-secret` and the worker compares it in constant time,
- relay URLs must be `https://` (plain `http://` is accepted only for loopback dev workers) — relays forward live account bearer tokens,
- relay responses are marked `cache-control: no-store` so CDN edges never cache one caller's proxied response.

Relays deployed by older versions of this tool did none of that: if you still run one, redeploy it from the dashboard (or delete it). Note the relay secret is sent as an HTTP header to whatever relay URL is configured — a relay you did NOT deploy from this dashboard (self-hosted or legacy) receives and may forward it; only rely on the secret for relays this tool deployed.

## Accounts and credentials

- Account snapshots hold live bearer tokens. The accounts directory is generated with per-user permissions; do not commit or sync it anywhere public.
- The gateway's own key material lives in `config.yaml` next to the binary (or `ZCODE_GATEWAY_CONFIG`). On Windows, credentials from a ZCode desktop install are decrypted via CNG; on Linux via the `cryptography` package ([requirements/server.txt](requirements/server.txt)).
- In a source checkout, `.local/` is the ignored home for local runtime data, build output and scratch files; `.venv/` remains the standard Python virtual environment. Ignored does not mean safe to publish: keep configs, account snapshots, databases and logs private, including legacy root-level runtime files.

See the [VPS deployment guide](docs/guides/VPS-DEPLOY.md) for installation and
service configuration. The public Linux bootstrap stays at
[`install-linux.sh`](install-linux.sh).

## Reporting

Open an issue for anything found. Do not open issues containing live tokens.
