# AutoZCoder

A self-hosted gateway for the **ZCode coding-plan API** (GLM-5.3 / GLM-5.3-Flash),
bundled with an **account generator** and a **Cloudflare mail worker**.

It exposes OpenAI- and Anthropic-compatible endpoints locally, pools multiple
ZCode accounts behind one key, and can create new accounts for you on demand.

> Unofficial, independent project. Not affiliated with ZCode or z.ai.

---

## What's inside

| Path | What it is |
|---|---|
| `zcode_gateway/` | The gateway: FastAPI app, account pool, admin/dashboard API, Windows desktop shell, web dashboard |
| `zcode_gateway/dashboard/` | The dashboard SPA (served at `/dash`) |
| `generator/` | The account generator: signup, captcha solving, invite claim, proxy pool |
| `mail-worker/` | Cloudflare Email Worker that receives signup verification mail |
| `tests/` | Offline test suite (161 tests) |
| `tools/` | `import_account.py`, `smoke_live.py` |
| `docs/` | Protocol notes and diagnosis write-ups |

---

## Quick start

### Run the packaged app

Double-click `dist\ZcodeGateway.exe`. On first launch it writes its own
`config.yaml`, `accounts/`, `logs/` and `gateway.db` **beside the executable**
(portable — move the folder anywhere). Default port **8788**.

Dashboard: <http://127.0.0.1:8788/dash>

### Run from source

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m zcode_gateway.main
```

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

**Accounts → Login with Zcode** (browser approval) or **Import from Zcode app**
(reads `~/.zcode/v2/credentials.json`). The pool reloads automatically.

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

## Email for signups (optional)

Signups need verification mail. Use your own domain with the bundled worker:

1. Add your domain to a free Cloudflare account.
2. **Generator → Email setup → step 2**, click **Deploy worker** — it installs
   Node.js if missing, creates the KV namespace + secret and deploys.
3. In Cloudflare: **Email → Email Routing → Catch-all → Send to a Worker →
   `zai-mail`**.
4. Back in the dashboard: **Test inbox**.

---

## Build and test

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt pyinstaller
.\.venv\Scripts\python.exe -m pytest tests -q
.\build-exe.ps1
```

Output: `dist\ZcodeGateway.exe` (single file, no Python needed to run).

Live checks (explicit, separate from tests):

```powershell
.\.venv\Scripts\python.exe tools\smoke_live.py
.\.venv\Scripts\python.exe tools\smoke_live.py --infer
```

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
  [docs/UNUSUAL-ACTIVITY.md](docs/UNUSUAL-ACTIVITY.md).
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

- Runtime secrets (`config.yaml`, `gateway.db`, `accounts/`, `generator/mail.json`,
  `*.har`) are gitignored — they are never part of the repo or releases.
- The gateway binds to `127.0.0.1` by default and mints **per-install** random
  admin/client keys on first run.
- Credential decryption uses the Windows AES-GCM implementation via CNG. On
  non-Windows, encrypted imports require `cryptography`.

## Layout

```
AutoZCoder/
├─ zcode_gateway/        gateway package (+ dashboard/)
├─ generator/            account generator scripts
├─ mail-worker/          Cloudflare Email Worker template
├─ tests/                offline tests
├─ tools/  docs/  assets/
├─ config.example.yaml
└─ build-exe.ps1
```

MIT — see [LICENSE](LICENSE).
