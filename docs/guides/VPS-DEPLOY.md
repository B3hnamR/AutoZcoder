# Deploying the gateway on a Linux VPS

The gateway runs headless on Linux with the same config, dashboard, pool,
relays, and generator as the Windows desktop build. The WebView shell,
taskbar integration, and in-app update application are Windows-only; on Linux
use the `autozcoder` service manager for updates.

## 0. One-command install + manager (recommended)

The public bootstrap remains at the repository root as
[`install-linux.sh`](../../install-linux.sh); existing install URLs stay valid.

```bash
curl -fsSL https://github.com/B3hnamR/AutoZcoder/releases/latest/download/install-linux.sh | sudo bash -s -- install
```

This URL follows the latest published release automatically; it does not run
unreleased `master` code. For an existing installation, stop active work and use
`sudo autozcoder update` instead of reinstalling. Review the installer before
running it as root if required by your security policy.

Defaults to **HTTPS on the server's bare IP** (Let's Encrypt shortlived IP
certificate, renewed by a systemd timer), so the dashboard opens at
`https://<server-ip>:8443/dash`. Issuance failure may fall back to a self-signed
certificate (browser warning); check the final report. Ports **80/tcp**
(certificate issue/renewal) and **8443/tcp** must be reachable. The installer
ends with a status report (also saved to `/opt/zcodegateway/install-report.txt`).

It also installs the `autozcoder` manager command. All commands except help
require root:

| Command | What it does |
|---|---|
| `sudo autozcoder` | interactive menu (status, start, stop, restart, logs, keys, TLS, update, uninstall) |
| `sudo autozcoder start` / `stop` / `restart` | control the gateway service |
| `sudo autozcoder status` | service, certificate expiry, dashboard URL |
| `sudo autozcoder info` | dashboard URL + admin/client keys |
| `sudo autozcoder cert` | transactional certificate/mode change (IP / domain / self-signed / none); restores prior TLS/config/renewal state on failure |
| `sudo autozcoder update` | transactional, SHA-256 verified update; startup/health failure rolls back deployment files; config and runtime data kept |
| `sudo autozcoder uninstall` | fully deletes service, certificates, files (`--yes` to skip the prompt) |

Non-interactive overrides: `ZGW_SSL_MODE=ip|domain|self|none`, `ZGW_DOMAIN`,
`ZGW_PORT`, `ZGW_SERVER_IP`, `ZGW_OPEN_FIREWALL=1`.

The selected TLS mode persists across reinstalls, including self-signed and
loopback HTTP (`none`). Certificate changes validate a staged certificate/key
pair before activation and, with systemd, check gateway health before committing.
Install, certificate changes, updates, uninstall and timer renewal share a lock.

Published Linux binaries target x86_64 and glibc 2.35 or newer (Ubuntu 22.04
build baseline). Older glibc and musl/Alpine require a compatible source build;
ARM systems must build from source with a native sidecar. The installer checks
architecture and libc before replacing a release binary.

`sudo autozcoder update` stages the gateway and manager from one immutable release,
verifies both checksums, and backs up deployment files before stopping the
service. Activation, startup or health failures roll back the binary, manager,
service unit and installer state. It preserves `config.yaml`, gateway keys,
accounts and runtime data without migrating the account database or replacing
operator service-unit overrides. Existing generator environments are checked
for basic compatibility, not silently rebuilt while jobs run; if that check fails, the
update rolls back and reports how to repair/reinstall the environment.

Finish or stop generation, claims, proxy scans and client requests before
updating: the Linux manager's service restart interrupts active work. In manual
service mode (`ZGW_SKIP_SYSTEMD=1`), the updater leaves supervision to you and
requires a manual restart. Keep your own private data backup as well; deployment
rollback is not a substitute for a database/account backup.

Reinstall keeps existing credentials and unrelated YAML settings, including
reordered or flow-style mappings. Python3 and the distro PyYAML package are
installed for parser-backed config updates. Invalid YAML or missing credential
fields abort installation without replacing the config or generating new keys.
Updates can normalize YAML formatting and comments; back up the file if its
original presentation matters. Listener/TLS and generator-environment settings
are updated to match the selected installation options.

With `ZGW_SKIP_SYSTEMD=1` (also selected when `systemctl` is unavailable),
installation completes with the gateway **not started**. The manager, saved
state, and report are still written, but running-service/public HTTPS checks
are skipped. Start it under your own supervisor with a private umask and the
installed browser location, for example:

```bash
cd /opt/zcodegateway
umask 077
HOME=/opt/zcodegateway/home PLAYWRIGHT_BROWSERS_PATH=/opt/zcodegateway/pw-browsers \
  ./ZcodeGateway --config /opt/zcodegateway/config.yaml
```

Service control and log commands require systemd. Without it, arrange certificate
renewal yourself (run `/root/.acme.sh/acme.sh --renew --home /root/.acme.sh --server letsencrypt -d <active-IP-or-domain>`
as root twice daily for a Let's Encrypt certificate), and restart your supervisor
after renewed certificate files are installed. The installer does not create a
renewal timer in this mode. Reused certificates must have over 24 hours remaining;
expired and nearly expired certificates go through issuance again.

The gateway serves HTTPS **itself** (`server.tls_cert` / `server.tls_key` in
`config.yaml`), so no nginx or other reverse proxy is installed and nothing
else on the VPS is touched. Port 80 is used for about 10 seconds while Let's
Encrypt validates the certificate; if another service owns port 80 the
installer asks before pausing it (or falls back to a self-signed certificate).
All package/build output goes to `/var/log/autozcoder-install.log`; the screen
only shows what is being done.

The sections below describe the manual setup.

## 1. Install

From a release (recommended):

```bash
curl -LO https://github.com/B3hnamR/AutoZcoder/releases/latest/download/ZcodeGateway
chmod +x ZcodeGateway
./ZcodeGateway            # writes ./config.yaml beside itself on first run
```

From source:

Run from the repository root with the server dependencies in
[`requirements/server.txt`](../../requirements/server.txt):

```bash
git clone https://github.com/B3hnamR/AutoZcoder
cd AutoZCoder
python3 -m venv .venv
.venv/bin/pip install -r requirements/server.txt
.venv/bin/python -m zcode_gateway.main --config config.yaml
```

Requirements: Python 3.9+ (3.13 recommended). The first run works on a
minimal server image — no Tk/GUI packages are needed.

The captcha-mint sidecar (only needed for headless Start-Plan claims) is
compiled for Linux by the release build; from source, build it with
`bun install && bun build --compile mint.ts --outfile captcha-mint` inside
`tools/captcha-mint/`.

For a packaged source build, run `bash scripts/build/linux.sh` from the repository
root ([build script](../../scripts/build/linux.sh)) on Linux with Python3,
venv/pip, binutils and Bun available. `ZGW_FROM_SOURCE=/path/to/checkout` uses
this same build. If Bun is absent, an executable prebuilt sidecar must already
exist at `tools/captcha-mint/captcha-mint`. The build checks its ELF format and
host architecture (x86_64 or aarch64), rejects Windows `.exe` fallbacks, and fails
instead of silently producing a package without the required sidecar. Bun is
not automatically installed by the installer. The sidecar is embedded as a
binary and also staged at `.local/dist/captcha-mint/captcha-mint`. The gateway
output is `.local/dist/ZcodeGateway`. Header checks do not
establish runtime compatibility with every Linux libc/CPU; smoke-test the release
and its bundled sidecar on the intended Linux platform before deployment.

## 2. Configure before exposing

Edit `config.yaml`:

```yaml
server:
  host: 127.0.0.1        # keep loopback and put TLS/tunnel in front (preferred)
  port: 8788             # to bind all interfaces set host: 0.0.0.0 — then ALL
                         # of the following apply (see SECURITY.md)
  require_client_key: true
  admin_key: <long random key>       # 8+ chars enforced; the default is rejected on public binds
  client_keys:
    - key: <long random key>
      name: vps
  # allow_remote_admin: true   # only if the admin API must be reachable off-box
  # allowed_hosts: [gw.example.com, 127.0.0.1, localhost, '::1']
  # expose_ops: true           # only if you want /health /ready /metrics public
```

The gateway refuses to start on a non-loopback bind without client keys and a
non-default admin key (see [SECURITY.md](../../SECURITY.md)).

## 3. Put TLS in front

Simplest hardened setup — Caddy on the same box, gateway stays loopback:

```
gw.example.com {
    reverse_proxy 127.0.0.1:8788
}
```

Point clients at `https://gw.example.com/v1/...` with the client key. The
dashboard lives at `https://gw.example.com/dash` (enter the admin key there
manually; the loopback auto-inject does not apply remotely).

For an intentionally remote dashboard, set `server.allow_remote_admin: true`
and configure `server.allowed_hosts` to include the actual dashboard hostname
(for example `[gw.example.com, 127.0.0.1, localhost, '::1']`). Otherwise keep
remote administration disabled and use an SSH tunnel. Privileged dashboard,
admin and operational routes reject untrusted Host headers. Built-in TLS adds
certificate SAN hostnames/IPs automatically; a reverse proxy's public hostname
must be configured explicitly.

Built-in TLS generator callbacks derive a verified origin from the certificate's
SANs. If that origin is not reachable from the server, set `server.child_origin`
to a full origin such as `https://gw.example.com:8443` whose hostname matches
the certificate and resolves from the server. The local certificate is trusted
only for these gateway calls; do not globally replace public HTTPS trust with
`REQUESTS_CA_BUNDLE` pointing at the gateway certificate.

For private-only access, prefer WireGuard or an SSH tunnel and leave
everything on loopback.

## 4. Run as a service (systemd)

`/etc/systemd/system/zcodegateway.service`:

For this manual setup, create the service account and give it ownership of the
installation's writable state first (the automated installer does the same: it
creates an unprivileged `autozcoder` user, unless `ZGW_RUN_AS_ROOT=1`). Place the binary and config in `/opt/zcodegateway` before
these commands:

```bash
sudo useradd --system --home-dir /opt/zcodegateway --shell /usr/sbin/nologin zcodegateway
sudo install -d -m 700 -o zcodegateway -g zcodegateway \
  /opt/zcodegateway/accounts /opt/zcodegateway/home
sudo chown root:zcodegateway /opt/zcodegateway
sudo chmod 1770 /opt/zcodegateway
sudo chown root:root /opt/zcodegateway/ZcodeGateway
sudo chmod 755 /opt/zcodegateway/ZcodeGateway
sudo chown zcodegateway:zcodegateway /opt/zcodegateway/config.yaml
sudo chmod 600 /opt/zcodegateway/config.yaml
```

Skip `useradd` if the account already exists. Keep config/account files private;
new bootstrap config files use mode `0600`, new accounts directories use `0700`,
and the service umask is an additional safeguard for runtime files.
Keep the executable and install-directory owner as root. The sticky, group-writable
install directory permits atomic config/database writes without allowing the
service user to replace the root-owned executable. Give only runtime subfolders
to the service user; protect TLS keys as root-owned, service-group-readable files.

The automated installer also keeps its private Node installation root-owned.
Generator-environment, browser and npm maintenance runs as an unprivileged tooling
user, including when `ZGW_RUN_AS_ROOT=1` is selected for the gateway itself.

```ini
[Unit]
Description=Zcode Gateway
After=network-online.target

[Service]
# config.yaml, accounts/, gateway.db live here
WorkingDirectory=/opt/zcodegateway
Environment=HOME=/opt/zcodegateway/home
Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/zcodegateway/pw-browsers
ExecStart=/opt/zcodegateway/ZcodeGateway --config /opt/zcodegateway/config.yaml
Restart=on-failure
User=zcodegateway
Group=zcodegateway
UMask=0077
# hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/zcodegateway
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now zcodegateway
```

## 5. Accounts, generator, relays

Everything is driven from the dashboard:

- **Accounts**: drop snapshots into `accounts/` (same format as Windows).
- **Generator**: the signup/claim pipeline runs on Linux (your VPS exit IP
  replaces proxies; set a SOCKS source for signup-vs-claim IP separation).
  Signup requires a headed Chromium browser. The installer provisions Xvfb;
  on Linux without `DISPLAY` the manager starts a private virtual display and
  stops it on shutdown. For manual installs, install your distro's `xvfb`
  (Debian/Ubuntu), `xorg-x11-server-Xvfb` (Fedora/RHEL/openSUSE), or
  `xorg-server-xvfb` (Arch) package before starting generator jobs. Selecting
  automatic captcha handling alone is not a headless-browser implementation.
  Automatic Python installation is Windows-only — install `python3` via the
  distro package manager and the setup modal will find it.
- **Relays**: deploy from the Relays tab exactly as on Windows. Relays are
  pinned to `zcode.z.ai` with a per-relay secret; the URL must be https.
- **Mail worker (Cloudflare)**: a server has no browser, so paste a Cloudflare
  API token and a domain already on Cloudflare into **Generator → Email setup**,
  then choose **Set up email** instead of `wrangler login`. **How to create a
  Cloudflare token** lists the permissions: Account → Workers Scripts: Edit,
  Workers KV Storage: Edit, Account Settings: Read; Zone → Zone: Read, DNS: Edit,
  Email Routing Rules: Edit, Zone Settings: Edit. Scope resources to your account
  and mail zones (or all zones). Setup creates the worker, KV storage and secret,
  configures routing, and checks the inbox. Replacing an existing catch-all
  destination requires explicit **Replace routing** confirmation; **Keep current
  routing** leaves it unchanged. Replacement diverts unmatched mail, not named
  address rules. Under **Advanced / manual recovery**, use **Save token only**
  to replace a saved token without redeploying, or set the optional account ID
  and inbox URL/secret. **Test worker inbox** checks the endpoint and secret,
  not actual delivery; send a real message to the domain to verify that.
- **Updates**: stop active work, then run **`sudo autozcoder update`** for an
  installer-managed VPS (transaction and rollback details above). Windows
  packaged builds instead offer **Download update** followed by **Restart to
  apply** in the app; Linux notifications do not apply a Windows executable.
  Source/custom-supervisor installs need a compatible build and their own
  restart and rollback procedure.

## 6. Verify

```bash
curl -s http://127.0.0.1:8788/health          # loopback always answers
curl -s -H "Authorization: Bearer <client key>" \
     http://127.0.0.1:8788/v1/models
```
