# Zcode Gateway — Account Generator setup

The gateway can create z.ai accounts and claim the referral **Global Build**
(100M GLM-5.3-Flash) automatically. This file covers the parts that need **you**
(once), and the portable layout.

---

## Portable install

Drop `ZcodeGateway.exe` in **any** folder and run it. Everything is created
beside it on first run — nothing is written elsewhere:

```
your-folder/
├─ ZcodeGateway.exe
├─ config.yaml        ← created on first run (random keys per install)
├─ accounts/          ← pool snapshots (auto-imported)
├─ logs/
├─ gateway.db
├─ generator-data/    ← generated accounts, proxy pool, email config
└─ mail-worker/       ← exported by the UI (see below)
```

Moving the exe to a new folder gives you a fresh, isolated install.

---

## 1. Point at the generator engine

The account generator is bundled in this repo as `generator/`. The gateway
auto-detects it — a folder called `generator/` (or `gen/`) beside the exe, or
anywhere on the search path. If it lives somewhere else, set it once:

```yaml
# config.yaml
generator:
  enabled: true
  zcodeauto_dir: ""         # '' = auto-detect; or the generator folder (has main.py)
  python_exe: ""            # blank = auto-detect; else full path to a python that
                            # has the generator deps (playwright/ddddocr/...)
  invite_code: ""           # your referral code ('' = off; empty by default)
  socks_only: true          # residential-style proxies (see Proxy tools)
```

You can also set this from the UI (**Generator → Locate generator**) — it
applies immediately, no restart and no config editing.

Install the generator's own dependencies into that Python:

```powershell
python -m pip install -r generator\requirements.txt
python -m playwright install chromium
```

The **Generator** tab appears once the folder is found.

---

## 2. Email — your own domain (one-time, ~5 min)

z.ai refuses disposable addresses, so you need a domain you own with a
catch-all that forwards verification mail to a worker the tool can read.

Do this **once per domain** (repeat for each domain you add):

1. **Buy a domain** (Namecheap / Porkbun / Cloudflare, ~$10/yr) and add it to a
   free <https://dash.cloudflare.com> account (Cloudflare becomes your DNS).
2. In Cloudflare → your domain → **Email → Email Routing**:
   - enable it (Cloudflare adds the MX records for you),
   - add a **catch-all** rule with action **Send to a Worker**.
3. In the gateway → **Generator** tab → **Email setup** → press
   **⤓ Export worker files**. That writes `mail-worker/` beside the exe.
4. In a terminal in that folder:
   ```
   cd mail-worker
   npm i -g wrangler
   wrangler login
   wrangler deploy
   wrangler secret put INBOX_SECRET      # type a long random secret
   ```
   Deploy prints your worker URL, e.g.
   `https://zai-mail.you.workers.dev`.
5. Back in **Email setup**: add your domain, paste the worker URL **plus
   `/inbox`** (e.g. `https://zai-mail.you.workers.dev/inbox`) and the same
   `INBOX_SECRET`, then **Save email setup** → **Test inbox** (should say
   `✓ inbox reachable`).

Every signup then rotates across your domains and reads its verification mail
from your own worker. Add more domains with **＋ Add domain** — the radio
selector also lets you pin the exact domain used next.

> No Gmail app password, no third-party inbox, no shared secret.

---

## 3. Proxies — pick an exit IP

The **Proxy tools** tab scrapes free proxy lists, verifies each against the
qualification rules (page loads, Aliyun captcha host reachable, valid TLS, under
the latency cap) and keeps the good ones.

| option | meaning |
|---|---|
| **socks pool** | socks4/5 only — residential-style IPs that pass the Starter gate |
| **full pool** | any verified proxy (http + socks) |
| **my network (no proxy)** | run from this machine's own IP |
| **pinned proxy** | one fixed `http://ip:port` or `socks5://ip:port` |

Buttons: **Scrape + Verify** (fresh pull + verify), **Verify raw**,
**Recheck pool**, **Stop**. **Custom proxies** lets you paste or upload your own
`ip:port` list; **Proxy sources** is the editable scrape list.

> Note: free datacenter proxies rarely earn the Starter plan — residential
> connections do. If Starter yield is 0, use your own network or a residential
> proxy you trust.

---

## 4. Run

In **Generator**: choose how many browsers, captcha mode
(**auto** = solver, **manual** = you solve the slider), IP source, retries, and
whether to claim the 100M referral. Press **START**. Each worker shows its proxy,
captcha mode, live log and result; successful accounts
(Starter + Global Build) join the pool automatically.

The pipeline per account: signup → Starter check → device login (+telemetry) →
invite bind → client message → **100M Global Build issues**.

---

## Troubleshooting

- **“px is not defined” / blank Generator tab** — stale build; use the latest exe.
- **“exceed quota limit”** — the Global Build is a *night-only* grant; outside
  its window the account shows the plan but the model is parked. It frees up
  automatically when the window opens.
- **Test inbox fails** — check the worker URL ends in `/inbox`, the secret
  matches `wrangler secret put INBOX_SECRET`, and the catch-all is enabled.
- **Generator unavailable** — use **Generator → Locate generator** to pick the
  folder, or check `generator.python_exe` points at a Python that has the
  generator dependencies installed.
