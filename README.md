# AutoZCoder

A self-hosted gateway for the **ZCode coding-plan API** (GLM-5.3 / GLM-5.3-Flash),
bundled with an **account generator** and a **Cloudflare mail worker**.

It exposes OpenAI- and Anthropic-compatible endpoints locally, pools multiple
ZCode accounts behind one key, and can create new accounts for you on demand.
Runs on **Windows (desktop exe)** and **Linux (headless VPS)**.

**Version 2.0.1** | [Release notes](docs/releases/2.0.1.md) | [Latest published downloads](https://github.com/B3hnamR/AutoZcoder/releases/latest)
| [Windows exe](https://github.com/B3hnamR/AutoZcoder/releases/latest/download/ZcodeGateway.exe)
| [Linux installation](#run-on-a-linux-vps)

> Unofficial, independent project. Not affiliated with ZCode or z.ai.

---

## What's inside

| Path | What it is |
|---|---|
| `zcode_gateway/` | The gateway: FastAPI app, account pool, admin/dashboard API, Windows desktop shell, web dashboard |
| `zcode_gateway/dashboard/` | The dashboard SPA (served at `/dash`) |
| `generator/` | The account generator: signup, captcha solving, invite claim, proxy pool |
| `mail-worker/` | Cloudflare Email Worker that receives signup verification mail |
| `tests/` | Offline test suite; run `python -m pytest tests -q` (Windows and Linux CI) |
| `scripts/accounts/` | Account import utility |
| `scripts/smoke/` | Explicit live checks and the isolated frozen-binary smoke utility |
| `scripts/build/`, `scripts/packaging/` | Windows/Linux build entry points and packaging inputs |
| `tools/captcha-mint/` | Stable JavaScript/TypeScript captcha-mint subsystem |
| `requirements/` | Windows, server and development dependency sets |
| `docs/` | Setup/deployment guides, protocol reference and troubleshooting |
| `.local/` | Ignored local runtime data, build output and scratch files |
| `.venv/` | Standard local Python virtual environment (ignored) |

---

## Quick start

### Run the packaged app (Windows)

Double-click `ZcodeGateway.exe` from the latest release. On first launch it
writes its own `config.yaml`, `accounts/`, `logs/` and `gateway.db` **beside
the executable** (portable — move the folder anywhere). Default port **8788**.

Dashboard: <http://127.0.0.1:8788/dash>

In **Settings → Check for updates now**, packaged Windows builds offer
**Download update**, then a separate **Restart to apply** action. Downloading
does not stop work; applying requires generator jobs, claims, proxy scans and
client requests to be idle. The helper retains a backup and requires a health
response matching a per-launch nonce within 60 seconds, rolling back on startup
failure or timeout. This confirms startup, not successful inference or generation.

### Run on a Linux VPS

On an x86_64 Linux VPS with systemd and glibc 2.35+ (Ubuntu 22.04+ or a
compatible distribution), run this single command over SSH:

```bash
curl -fsSL https://raw.githubusercontent.com/B3hnamR/AutoZcoder/v2.0.1/install-linux.sh | sudo bash -s -- install
```

This executes the versioned installer as root. Review it first if your policy
requires it. The installer obtains and checksum-verifies the release binary,
creates private keys/configuration, installs the generator environment,
Chromium/Xvfb and private Node/Wrangler, and starts an unprivileged service.
It does **not** import accounts or deploy a Cloudflare mail worker for you.

The default dashboard is **`https://<server-ip>:8443/dash`**. Ports **80/tcp**
(certificate validation/renewal) and **8443/tcp** must be reachable; read the
final report for the admin key and whether the issued certificate is trusted
or self-signed. No nginx is installed. Use **2 reliable vCPUs and 4 GB RAM**
for comfortable one-browser generation. Small servers receive a confirmation
warning; gateway-only operation can use less.

```bash
sudo autozcoder          # interactive manager
sudo autozcoder status   # service and dashboard status
sudo autozcoder logs     # service logs
sudo autozcoder update   # verified update with deployment rollback
```

Default data location: `/opt/zcodegateway`. Back it up privately before
maintenance; `sudo autozcoder info` displays secrets. For manual/source installs,
ARM systems, firewall/TLS choices, updates and uninstall precautions, see the
**[VPS deployment guide](docs/guides/VPS-DEPLOY.md)**.

### Run from source

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements/windows.txt
.\.venv\Scripts\python.exe -m zcode_gateway.main
```

Run these commands from the repository root. On Linux, use
`requirements/server.txt` and `.venv/bin/python` instead. The standard virtual
environment is `.venv/`; `.local/` is reserved for ignored local runtime data,
build products and scratch files, not Python packages. Packaged installs retain
their portable data layout beside the executable; this repository reorganization
does not move or migrate existing runtime files.

Point any OpenAI/Anthropic client at `http://127.0.0.1:8788/v1` with the client
key from the **Server** tab.

```json
{
  "model": "GLM-5.3-Flash",
  "messages": [{"role": "user", "content": "Hello"}],
  "reasoning_effort": "high",
  "stream": true
}
```

---

## Adding accounts

Generator and email setup: **[Account generator setup](docs/guides/SETUP.md)**.

**Accounts → Login with Zcode** (browser approval) or **Import from Zcode app**
(reads `~/.zcode/v2/credentials.json`). The pool reloads automatically.

To transfer one account, use **Copy account for sharing**, then **Accounts →
Import account → Validate and import** on the destination. A raw CLI JWT is
also accepted. Exports contain the current bearer token and account/device
metadata: treat them as credentials, not public support attachments. Sharing
does not duplicate quota or entitlement and includes no renewal grant. Quota
refresh is read-only; credential refresh can adopt a newer saved token but does
not renew it. An expired or revoked token needs a fresh login/import if no newer
credential is available; a JWT without declared expiry is not permanent.

## Generating accounts (optional)

The **Generator** tab drives `generator/` — it needs a real Python (not the
packaged exe) with the generator dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r generator\requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

The gateway auto-detects the bundled `generator/` folder. If you keep it
elsewhere, set it in **Generator → Locate generator** (applies live, no restart)
or `generator.zcodeauto_dir` in `config.yaml`.

### Version 2 workflow

- **Auto Generate** supports a goal of new, qualified Start Plan successes or
  a fixed attempt budget. Duplicates and no-plan results do not count toward
  the goal; a mandatory maximum-attempt budget can end a run before its goal.
- Run **1-3 concurrent browser slots**, each with its own cooldown after an
  attempt finishes. Start with one and measure resource use before increasing.
- Auto Generate uses **AI judge only** and defaults to background browsing:
  native headless Chromium on Windows (which provider checks may reject),
  headed Chromium on a private **Xvfb** display on Linux. Desktop mode requires
  a real display; background mode never silently opens a visible fallback.
- Save the profile explicitly before starting. Pause lets active attempts
  finish without replacements; Stop cancels owned attempts. Saved settings
  survive restarts, but interrupted sessions require explicit continuation.
- The dashboard brings together plan/model quota details, batch claims,
  account import/sharing, and **Analytics** with period filters and CSV export.

## Email for signups (optional)

Signups need verification mail. Use your own domain with the bundled worker:

In **Generator → Email setup**, enter your domain (already on Cloudflare) and
API token, then click **Set up email**. **How to create a Cloudflare token**
lists the required permissions and account/zone scopes. Setup deploys the
worker, KV storage and secret, configures routing, and checks the inbox.

If an existing catch-all has a real destination, review **Keep current routing**
or **Replace routing** explicitly: replacement diverts unmatched mail to the
worker; named-address rules are unchanged. **Advanced / manual recovery**
offers **Save token only** to replace credentials without redeploying, optional
account ID, manual inbox settings, and **Test worker inbox**. That test proves
the worker endpoint and secret work, not that mail is delivered. Send a real
message to an address on the domain to verify end-to-end delivery.

---

## Build and test

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements/windows.txt -r requirements/dev.txt pyinstaller
.\.venv\Scripts\python.exe -m pytest tests -q
.\scripts\build\windows.ps1  # Windows exe (bun compiles the captcha-mint sidecar)
```

On Linux, from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements/server.txt -r requirements/dev.txt pyinstaller
.venv/bin/python -m pytest tests -q
bash scripts/build/linux.sh
```

Output: `.local/dist/ZcodeGateway.exe` / `.local/dist/ZcodeGateway` (single
file, no Python needed to run); intermediate build files stay in `.local/build/`.
CI runs the suite on both Windows and Ubuntu, plus a runtime-error lint gate.
The root [`install-linux.sh`](install-linux.sh) remains the public Linux
bootstrap for compatibility; `tools/captcha-mint/` stays in place as its own
JavaScript/TypeScript subsystem.

Live checks (explicit, separate from tests):

```powershell
.\.venv\Scripts\python.exe scripts\smoke\live.py
.\.venv\Scripts\python.exe scripts\smoke\live.py --infer
```

Account import: `python scripts/accounts/import_account.py <source>` (use
`--config <path>` for a non-default configuration). The frozen-binary check at
`scripts/smoke/frozen_local.py` requires `AUDIT_BINARY` and a separate,
network-isolated sandbox working directory; it writes dummy state there, not
beside its script.

---

## Features

- OpenAI Chat Completions, legacy Completions and Models endpoints.
- Native Anthropic Messages, streaming and non-streaming.
- Thinking signatures, cache controls, tool blocks, images, system blocks and
  metadata preserved on native requests; full translation for the OpenAI adapter.
- Reasoning levels `low` / `high` / `max` (`medium`→`high`, `xhigh`→`max`).
- Per-model token quotas, reset times and plan expiry tracking.
- `405 / code 3012` "unusual activity" is a **per-model park**: the account is
  skipped for that model only and the request rotates on. See
  [unusual activity troubleshooting](docs/troubleshooting/UNUSUAL-ACTIVITY.md).
- Strategies: least-in-flight, **drain-first**, round-robin, LRU, quota-aware.
- Model overload (`529 / code 1305`) retries before output with backoff + jitter.
- Authenticated client/admin routes, per-install keys, redacted captures (off by default).
- One-click Cloudflare worker deploy with automatic Node.js install.

### Proxy pool (generator)

The generator's proxy tooling verifies every candidate against `chat.z.ai`
itself: the auth page must load with a valid certificate **and** the Aliyun
captcha host must be reachable. Around that gate:

- **One parser** for every list shape: `ip:port`, `scheme://`, `user:pass@`,
  provider style `host:port:user:pass`, CIDR ranges (up to /16), HTML tables
  and JSON records (`generator/proxy_parse.py`).
- **TCP prefilter** before any HTTPS request (measured: ~3 s/port instead of two
  full HTTP timeouts per dead host).
- **Health scoring** in `pool.json` — a failed check adds one, a pass removes
  one; a proxy stays usable while its score is ≤ 3. `proxies.txt` is the
  best-first export, so `main.py` is unchanged. Pasted proxies are `vouched`
  and stay pending until a real check passes; only verified proxies are exported
  or counted by the generator.
- **Second chance** pass over timeouts/resets (recovers roughly 30% more on
  free lists).
- **Typed failure reasons** (timeout, refused, TLS/MITM, hijacked page,
  wrong protocol, *your PC ran out of connections*, …) shown live in the
  dashboard instead of a bare "0 found".
- **Dead cache** (opt-in) so repeat scans skip hosts that just failed.
- **Cross-process claims**: eight workers are eight processes; the claim lock
  is a file lock, so two workers can never get the same exit IP.
- **Per-source yield** and scan presets (`balanced` / `thorough` / `fast`).
- **Countries** — the country of each proxy is looked up online (ip-api.com and
  geojs.io in batches, with ipwho.is / ipinfo.io as fallbacks) and cached in
  `ip_country.json`. Nothing is downloaded.

CLI: `python generator/proxy_pool.py{ fetch | verify | recheck | pick | countries | stats | prune | clear-bad | clear-used | clear-dead }`.

## Security notes

- Packaged Windows data normally lives beside the executable (or in
  `%LOCALAPPDATA%/ZcodeGateway` if that directory is not writable); the Linux
  installer defaults to `/opt/zcodegateway`. Back up `config.yaml`, `accounts/`,
  `gateway.db` and packaged `generator-data/` privately, plus any custom data
  paths and TLS material. Stop the service or use SQLite-aware backup tooling
  rather than copying an actively written database alone.
- **The admin key grants code execution as the gateway's OS user** — it can
  point the generator at arbitrary code and request updates. Installer-managed
  Linux services normally run unprivileged. See **[SECURITY.md](SECURITY.md)**.
- Runtime secrets (`config.yaml`, `gateway.db`, `accounts/`, `generator/mail.json`,
  `*.har`) are gitignored — they are never part of the repo or releases.
- The gateway binds to `127.0.0.1` by default and mints **per-install** random
  admin/client keys on first run. An empty admin key authenticates nothing
  (fail closed); public binds refuse to start without client keys and a
  non-default admin key.
- Cross-origin browser mutations (CSRF / DNS rebinding) are rejected at the
  app layer; non-browser clients are unaffected.
- Edge relays are pinned to `zcode.z.ai` with a per-relay secret and must be
  reached over https — a deployed relay is not an open proxy.
- Credential decryption uses the Windows AES-GCM implementation via CNG. On
  non-Windows, encrypted imports require `cryptography`
  (included in [requirements/server.txt](requirements/server.txt)).

## Layout

Source-checkout launches keep private configuration, accounts, logs, database
and generator output under `.local/runtime/`; packaged installations retain
their portable/service data locations. Build products go to `.local/build/`
and `.local/dist/`. `.local/archive/` preserves older local artifacts and
relocation manifests; it is not source or release input. The standard `.venv/`
and component `node_modules/` folders remain in place and are ignored.

```
AutoZCoder/
|-- zcode_gateway/        gateway package (+ dashboard/)
|-- generator/            account generator scripts (+ requirements.txt)
|-- mail-worker/          Cloudflare Email Worker template
|-- scripts/
|   |-- accounts/         import_account.py
|   |-- build/            windows.ps1, linux.sh
|   |-- packaging/        packaging inputs
|   `-- smoke/            live.py, frozen_local.py
|-- tools/captcha-mint/   stable JS/TS subsystem
|-- requirements/        windows.txt, server.txt, dev.txt
|-- docs/
|   |-- guides/           SETUP.md, VPS-DEPLOY.md
|   |-- reference/        PROTOCOL.md
|   `-- troubleshooting/  DIAGNOSIS-*.md, UNUSUAL-ACTIVITY.md
|-- tests/                offline tests (Windows + Linux)
|-- assets/
|-- .local/               ignored runtime/build/scratch files; dist/ output
|-- .venv/                ignored Python virtual environment
|-- config.example.yaml
`-- install-linux.sh      public bootstrap (stable root URL)
```

Documentation: [Setup](docs/guides/SETUP.md),
[VPS deployment](docs/guides/VPS-DEPLOY.md),
[Protocol](docs/reference/PROTOCOL.md),
[Model overload](docs/troubleshooting/DIAGNOSIS-1305.md),
[Agent initialization](docs/troubleshooting/DIAGNOSIS-3012.md),
[Unusual activity](docs/troubleshooting/UNUSUAL-ACTIVITY.md),
[Security](SECURITY.md).

MIT — see [LICENSE](LICENSE).
