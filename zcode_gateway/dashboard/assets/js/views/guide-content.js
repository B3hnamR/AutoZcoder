// User-facing operating reference. No credentials or host-specific secrets.
export const GUIDE = [
  { id: "start", title: "Start here", desc: "Get a working client before configuring optional automation.", route: "server", items: [
    { title: "First successful request", tags: "setup quickstart client API", steps: [
      "Open Accounts and choose Login with Zcode, or Import account if you have a trusted account export. Confirm at least one account has usable quota for the requested model.",
      "Open Server. Copy the reachable API base URL and a client key into your OpenAI-compatible or Anthropic-compatible application. Do not use 0.0.0.0 as a destination address.",
      "Use a model ID listed in Models. Send a short request, then check Analytics and Logs. Some models may require an edge relay for your network; relay setup is optional, not a first-run requirement.",
    ] },
    { title: "Windows exe versus Linux VPS", tags: "platform install headless display", steps: [
      "Windows runs the desktop launcher and visible signup browsers. Import from app is available only when a local Zcode credential store exists.",
      "A Linux VPS runs the gateway as a service and uses headed Chromium on a private Xvfb display when no desktop exists. There is no browser window to see over SSH. Manual captcha requires an actual desktop display.",
      "Fresh Linux generator defaults are AI judge only for both Captcha and AI judge model. Saved dashboard choices and explicit existing configuration take precedence. Windows defaults are unchanged.",
    ] },
    { title: "Use the right key", tags: "admin password login client authorization bearer", steps: [
      "The admin key unlocks this dashboard and powerful administration endpoints. A client key authorizes model requests. Never give a client application your admin key unless you intentionally grant that broader authority.",
      "The login field expects the admin key, not your Linux sudo password, Cloudflare token, inbox secret, or account JWT. Use HTTPS for remote administration.",
      "Keys appear in Server and the private installer report. On Linux, sudo autozcoder info displays credentials; do not paste its output into public logs or support messages.",
    ] },
  ] },
  { id: "accounts", title: "Accounts and sharing", desc: "Understand what is stored, what refreshes, and what sharing grants.", route: "accounts", items: [
    { title: "Login, import, and export", tags: "JWT copy friend transfer Imported clipboard", steps: [
      "Login with Zcode opens the provider approval page. Complete approval for the account you want, then return here. Browser approval can be completed on your own computer even when the gateway is on a VPS.",
      "To share one account, click its Copy account for sharing action and confirm the warning. Your friend pastes the copied text into Accounts > Import account > Validate and import. A raw Zcode CLI JWT is also accepted; its email label is optional.",
      "The destination validates the credential with Zcode before saving. The same account ID updates the existing entry. An Imported badge persists across restarts.",
      "The export includes the current access token and account labels/device metadata, not gateway admin keys, mail secrets, other accounts, or a renewal grant. It is plain text: anyone holding it can use the account. Both installations share quota, provider restrictions, and concurrent usage; sharing does not duplicate entitlement.",
    ] },
    { title: "Quota refresh is not token renewal", tags: "expiry long-lived revoked refresh JWT authentication", steps: [
      "The dashboard regularly refreshes plans, quotas and status. This does not issue a new access token. No declared expiry means the JWT has no expiry timestamp, not that it is permanent.",
      "Refresh quotas performs read-only billing checks. The per-account Refresh action rereads saved credentials and can adopt a newer token. Reload pool rereads account sources; unchanged accounts may keep their current runtime data.",
      "If a token expires or is revoked and there is no newer saved token, use Login with Zcode or import a fresh account export. No unattended CLI-token renewal grant is currently implemented.",
    ] },
    { title: "Read state, caps, and grant windows", tags: "ready cooling dead capped starts reset plan entitlement", steps: [
      "Ready means eligible at the pool level, not that every model is available. A model-specific cap can coexist with ready state. Cooling is temporary; dead indicates an account currently cannot serve normally.",
      "Daily quota is per model. Live remaining quota excludes future grants, which are tracked separately; a future grant does not block an already usable balance for the same model. Reset times and plan ends differ, so check the displayed start/reset time.",
      "A failed quota check shows an error and retains last-known values. These can be stale. Refresh quotas to retry; use Logs if the failure persists. No quota buckets after a successful check is different from a check that never succeeded.",
      "Enable restores eligibility but does not create quota, repair a revoked token, or remove a provider-side restriction. Runs counts current in-flight activity, not lifetime request usage.",
    ] },
    { title: "Remove, purge, and backup safely", tags: "delete destructive persistence snapshot restore", steps: [
      "Remove takes an account out of the running pool; its saved snapshot can restore it after Reload pool or restart. Do not assume removal revokes the provider token.",
      "Purge drained is destructive: it removes dead accounts and fully spent accounts without a future quota reset, and can delete their snapshots. Review the confirmation and back up first.",
      "Snapshots contain usable credentials. Keep account folders and backups private. A copied account export cannot be recalled by deleting it locally; use provider-side session/token revocation if it was exposed.",
    ] },
  ] },
  { id: "generator", title: "Generator and AI setup", desc: "Prepare the browser, inbox, and judge before starting workers.", route: "generator", items: [
    { title: "Prepare the environment", tags: "python Chromium Playwright packages locate permission Xvfb", steps: [
      "If asked, locate the ZCODEAUTO folder containing the generator scripts. Environment setup checks Python, required packages and the Playwright Chromium download. The Linux installer provisions these automatically; source installs may require setup.",
      "Use one browser for the first test. Increase only after a full run works; extra workers consume memory, CPU, inbox capacity and AI quota. A successful installer or blank-browser check does not prove provider signup will succeed.",
      "Worker stages include signup, verification email, CLI/device authorization, account registration, and optional plan claiming. Read the latest worker lines to locate the actual failed stage before changing settings.",
    ] },
    { title: "Auto Generate: save, start, and supervise", tags: "automation goal attempts qualified cooldown concurrency pause resume interrupted safety budget", steps: [
      "Prepare the environment, inbox and vision-capable AI judge first. Auto Generate always uses AI judge only. Choose a qualified-success goal or a fixed number of signup attempts; only new qualified Start Plan successes count toward the goal, not duplicate accounts or no-plan results.",
      "Set Maximum attempts even in goal mode: it is the mandatory safety budget, not a promise to reach the goal. Use 1-3 browser slots, a per-slot cooldown in seconds, an attempt timeout, and a consecutive failure limit. Begin with one browser and check resource usage before increasing concurrency.",
      "Choose Save config explicitly, then Auto Generate. Editing any field requires saving again before starting. Save config does not start a session. The saved configuration survives a gateway restart; an active session keeps its original profile and cannot be edited in place.",
      "Direct means no selected proxy; it does not disable your system VPN/TUN or change OS routing. Proxy pool deliberately uses proxy selection. Neither mode guarantees provider signup or Start Plan entitlement.",
      "Pause prevents new attempts while active attempts finish. Resume continues a paused or interrupted session only after your explicit action. Stop session cancels its queue and active attempts. Review the reason and separate failed, no-plan and duplicate counts before retrying; restarting the gateway must not silently restart automation.",
    ] },
    { title: "Captcha and AI judge model", tags: "ai auto classic manual strategy vision model default", steps: [
      "Captcha selects the interaction mode. AI judge model selects the solving strategy: AI judge only, classic computer vision, or auto combination. Despite its label, this dropdown is not the model ID for an API endpoint.",
      "Linux defaults both dropdowns to AI judge only. This requires a working vision-capable AI endpoint. Without a custom endpoint, the judge uses this gateway's accounts, normally GLM-5.3-Flash; you need a usable account before this can bootstrap.",
      "To use your own judge, fill AI judge endpoint URL, model ID, and its API key. An origin, a base ending in /v1, or the full /chat/completions endpoint is accepted. Ensure the model accepts images; text completion alone does not establish vision support. Leaving the key field blank preserves its saved value.",
      "Manual means you solve in a desktop window. It is unavailable on a normal displayless VPS. Retries are bounded recovery attempts, not a guarantee of successful verification.",
    ] },
    { title: "Browser display", tags: "headless visible desktop virtual WSLg Xvfb F001", steps: [
      "Automatic uses an existing desktop display or starts Xvfb if none exists. Virtual display (background) explicitly selects a private Xvfb screen on Linux. Desktop window requires a reachable DISPLAY.",
      "Linux background uses headed Chromium on a private Xvfb screen. Windows background uses genuine Chromium headless, with no visible browser; provider device checks may reject it. Choose Desktop window explicitly for compatibility/debugging. Auto Generate defaults to background; it never silently opens a visible fallback. Display changes affect new workers only.",
      "Xvfb missing, browser executable missing, missing shared libraries, or permission errors are environment problems. Run Environment setup or repair the Linux dependencies before retrying; switching AI models does not fix a browser that cannot launch.",
    ] },
    { title: "IP source, retries, and job controls", tags: "direct pinned proxy pool country SOCKS stop kill claim", steps: [
      "My network uses the host's current route, including any system VPN/TUN. Proxy pool uses verified candidates; proxy type/country only constrain pool selection. Pinned proxy uses the explicit proxy you enter.",
      "An empty country or SOCKS subset cannot be fixed by increasing browser count. Verify the pool, broaden filters, use a valid pinned proxy, or deliberately select your own network.",
      "Kill stops the selected worker or claim. Stop all currently stops signup workers only; stop running claims individually. Finished rows are removed with Clear finished. Stopping after signup may leave an account awaiting email verification.",
      "Claim all pending processes locally saved successful signups without a recorded grant, one at a time. Direct (no proxy) is the default; Proxy pool, then direct is optional. Stop batch cancels its queue and current claim while leaving the gateway running. The progress line reports processed/granted/not-granted/skipped counts. Definitive empty previews are distinct from preview/network failures; no mode guarantees a grant.",
      "The native Start Plan captcha helper supports direct, HTTP(S) and SOCKS proxy routes. SOCKS uses an internal loopback HTTP bridge; no public listener or extra firewall port is needed. Direct claims do not fall back to a proxy, but still follow OS VPN/TUN routing.",
    ] },
  ] },
  { id: "mail", title: "Email worker and routing", desc: "Worker authentication, DNS routing, and delivery are separate checks.", route: "generator", items: [
    { title: "Configure Cloudflare correctly", tags: "API token permissions zone account workers KV DNS routing", steps: [
      "Use an active domain whose DNS zone is in your Cloudflare account. In Email setup, enter the domain and a custom Cloudflare API token; specify the account ID if setup cannot choose. Automatic subdomain routing is unsupported; configure it manually.",
      "Required token permissions: Account / Workers Scripts / Edit; Account / Workers KV Storage / Edit; Account / Account Settings / Read; Zone / Zone / Read; Zone / DNS / Edit; Zone / Email Routing Rules / Edit; Zone / Zone Settings / Edit. Include the intended account and mail zones.",
      "Set up email provisions/reuses the worker, KV storage and inbox secret, then configures Email Routing. Keep the setup log if any stage fails. Never post the API token, inbox secret, or token-bearing inbox URL.",
    ] },
    { title: "Approve routing changes deliberately", tags: "catch-all confirmation fingerprint MX TXT existing mail", steps: [
      "Automatic Email Routing can add MX/TXT records. A catch-all pointing to the worker receives unmatched mail for the entire domain. Use a dedicated domain or understand the impact on existing mail first.",
      "If an existing catch-all has a real destination, review the current and proposed destinations. Replace routing requires explicit approval; keeping current routing means verification mail may not reach this worker. Named-address rules can still take precedence.",
      "If the rule changes after review, refresh and review again. Do not keep retrying an old confirmation; the safety fingerprint deliberately rejects stale replacement approvals.",
    ] },
    { title: "Test inbox and delivery separately", tags: "401 unauthorized URL secret inbox poll mail missing email", steps: [
      "Test worker inbox verifies the configured endpoint and secret. It does not prove DNS, catch-all routing, or actual message delivery. Send a real message to an address on the configured domain to verify end-to-end delivery.",
      "Inbox poll HTTP 401 means the worker rejects its saved secret. Confirm the exact /inbox URL and INBOX_SECRET pair. Save the raw secret, not a percent-encoded value or URL containing ?k=. Restart jobs after changing credentials: running children retain old settings.",
      "Worker health 200 does not prove inbox authentication. If a shared worker's secret is rotated, update every installation using it. Stop repeated signups while inbox authentication is broken.",
      "Use Advanced / manual recovery to replace a saved Cloudflare token without redeploy, remove the saved token, set inbox URL/secret, or test the inbox. Removing local credentials does not delete the deployed worker or revoke the token at Cloudflare.",
    ] },
  ] },
  { id: "proxy", title: "Proxy pool and TUN networks", desc: "A TCP connection is not proof of a working proxy.", route: "proxy", items: [
    { title: "Scan, verify, and maintain", tags: "scrape raw verified working failing latency timeout sources import", steps: [
      "Scrape + verify downloads candidate lists and checks them. Verify raw checks the saved raw list. Recheck pool retests saved pool members. Import accepts proxy URLs or IP:port lines, but imported entries still need verification.",
      "Raw list counts candidates. Working/failing/never checked counts refer to the saved pool ledger; raw failures do not necessarily become ledger members. A large raw count and zero saved failing entries is not proof that checking is idle.",
      "Verification checks proxy negotiation, HTTPS/TLS, z.ai page expectations, captcha-host reachability and latency. Second chance happens after the initial pass; stopping early may skip it. TCP probes, HTTPS Checks concurrency and timeouts are different controls.",
      "Sources & import supports pasted/file lists, scrape-source editing, and per-source yield. Pool maintenance can clear used, bad, or dead caches and prune stale entries; clearing caches does not repair a broken proxy.",
      "Auto re-verify keeps saved proxies fresh at the selected interval. Country lookup supplies metadata, not a usability test. Prefer known trusted proxies over treating public lists as secure merely because TLS passed once.",
    ] },
    { title: "Xray / VPN TUN warning", tags: "all ports open reset TCP interception 64 Windows tunnel timeout", steps: [
      "Some TUN stacks locally accept TCP before the remote connection succeeds. If reserved-address probes also appear open, the checker warns that TCP results are unreliable and switches to actual CONNECT/SOCKS handshakes.",
      "This path caps verification concurrency at 64 and does not bypass your tunnel or alter routes/firewall rules. All endpoints appearing open does not mean they work. A zero usable result remains possible.",
      "If handshakes time out, reset, refuse CONNECT or fail TLS, compare another VPN exit/routing policy or a known-working proxy. Direct chat.z.ai working only proves direct HTTPS; it does not prove the public proxy can tunnel to the destination.",
      "The detector is a network heuristic, not proof that your public IP is banned. Changing IP may help some paths but does not fix the tunnel's local TCP-accept behavior or dead public proxies.",
    ] },
  ] },
  { id: "relays", title: "Edge relays and models", desc: "Route model traffic when necessary, without confusing relays with signup proxies.", route: "models", items: [
    { title: "Enable a relay only after configuring it", tags: "Cloudflare Vercel Deno Netlify 405 unusual activity 3012", steps: [
      "Edge relay is off on fresh installs. In Proxy tools, deploy Cloudflare/Vercel/Deno/Netlify or add an existing relay URL and its secret. Test it, select an active relay, choose routed models, then enable routing.",
      "Cloudflare relay deployment requires an API token and account ID, with workers.dev enabled. Deno requires a token and organization domain. For Netlify use the generated function URL ending /.netlify/functions/relay. Deployment protection and provider rate limits can still block access; inspect the deployment log.",
      "Single uses the active relay; round robin or random rotate eligible relays. Failed relays are temporarily deprioritized; if all are cooling the full list can be reused. A relay health test is not a Zcode inference test, and changing exit does not guarantee removal of provider restrictions.",
      "A relay carries model API traffic, not signup browser traffic or mail. A working relay does not fill the signup proxy pool. Removing a relay from this gateway does not delete the provider deployment.",
    ] },
    { title: "Model availability and fallback", tags: "catalog sync default reasoning pacing capped reset", steps: [
      "Models shows the catalogue and inferred availability from account state, plan windows, quota and known caps. Expand a model to see per-account reasons. Sync models refreshes the provider catalogue, not account entitlement.",
      "Settings controls default models, reasoning effort, optional fallback and pacing. A fallback can change which model answers; verify the actual model if output characteristics matter.",
      "HTTP 405 with unusual activity or code 3012 can be model/IP-specific. Inspect caps and relay settings. Do not reset every account or purge snapshots merely because one model is temporarily blocked.",
    ] },
  ] },
  { id: "analytics", title: "Analytics, logs and settings", desc: "Use the right evidence before changing credentials or deleting data.", route: "analytics", items: [
    { title: "Understand analytics numbers", tags: "usage stats 1h 6h 24h 7d 30d all CSV latency tokens errors", steps: [
      "Analytics replaces the separate Usage and Stats pages. Select 1h, 6h, 24h, 7d, 30d or All time; summaries, chart and breakdown share that period. Old links redirect to 30d or All time.",
      "Counts represent recorded upstream attempts. Retries can create more than one row per client request, and not every preflight/transport failure enters the ledger. They are not complete ingress access logs.",
      "Switch traffic between tokens, requests and errors. Model/Account/API-key breakdowns support search, sorting and CSV export. Search filters the breakdown/export, not the summary or chart. Account details are enriched from today's pool, while history may include removed accounts.",
      "Previous-period comparison uses an equal preceding interval; All time has no comparison. Average latency includes failures, percentiles use successful samples, and recorded duration is not time to first token or full client latency. Bucket labels use the displayed browser timezone.",
    ] },
    { title: "Logs, refresh and saved configuration", tags: "debug errors warnings restart cache old UI", steps: [
      "Logs reads the process's in-memory ring buffer with level/text filters. Clear sets a view watermark: newer records still arrive, and changing filters does not restore older cleared records. It does not erase server logs. Restart clears the ring; Linux systemd logs remain in journalctl. Redact credentials before sharing lines.",
      "Settings marks unsaved rows and saves only changed values. Restart the gateway to apply configuration. Capture logging stores prompts/responses on disk. The key-regeneration control currently refers you to the desktop launcher rather than rotating keys itself. Restart differs from dashboard Refresh, Reload pool, quota checks and catalogue sync.",
      "If controls are missing or labels look old after an update, hard-refresh with Ctrl+Shift+R or test a private window. Browser JavaScript can remain cached even when the server binary was updated.",
    ] },
    { title: "Update safely on Windows and Linux", tags: "updater download restart apply checksum backup rollback release", steps: [
      "Use Settings > Check for updates now for a manual release check. Windows packaged builds offer Download update, then a separate Restart to apply action. Downloading does not stop work; applying requires generation, claim batches, proxy scans and client requests to be idle.",
      "The Windows helper verifies the staged checksum again, waits for the app and its bootloader, preserves launch arguments/configuration/working directory, and retains a backup. It requires a health response matching a per-launch nonce within 60 seconds; startup failure or timeout triggers rollback. This confirms startup, not inference or generation. Inspect the private helper update.log if relaunch fails.",
      "Linux notifications show release information, not a Windows exe download. Use sudo autozcoder update after stopping work. The manager stages matching gateway/manager assets from one release, verifies checksums, and rolls back deployment files if restart or health fails. Accounts/configuration are retained; keep a separate private backup.",
      "Published Linux builds require x86_64 and glibc 2.35 or newer. Source/custom supervisor installations need their own compatible build/runtime and manual restart policy. If generator dependencies are incompatible after staging, repair the environment rather than disabling checks. Neither updater needs your account export or Cloudflare token.",
    ] },
  ] },
  { id: "linux", title: "Linux install, manager and backups", desc: "The installer provisions runtime; it does not deploy your mail worker or add accounts for you.", route: "server", items: [
    { title: "Practical VPS sizing for generation", tags: "minimum requirements RAM CPU memory vCPU hardware server steal swap capacity", steps: [
      "For one browser, 1 reliable vCPU and 2 GB RAM is a bare minimum with limited headroom. For comfortable use alongside the gateway, start with 2 low-contention vCPUs and 4 GB RAM. These are planning estimates, not measured capacity guarantees.",
      "For two concurrent browsers, plan around 4 vCPUs and 6-8 GB RAM. For three to four, plan around 4-6 vCPUs and 8-12 GB RAM. Start with one worker and increase only after measuring complete successful runs.",
      "A rough one-browser RAM budget is 400-700 MB for the OS, 300-600 MB for the gateway/Python, 600 MB-1.5 GB for Chromium/Xvfb/worker, plus 500 MB-1 GB safety margin. This assumes a remote AI judge; hosting an AI model locally requires additional resources.",
      "Prefer reliable or dedicated CPU over heavily oversold cores. High CPU steal means the hypervisor is withholding CPU time: low browser responsiveness can happen even with free RAM. One slow page load alone does not prove resource exhaustion; check logs, OOM events and network behavior.",
      "1-2 GB swap can absorb short memory peaks but is not browser capacity. Avoid simultaneous large proxy scans or update builds on a small VPS. Gateway-only operation without generation can fit smaller hosts.",
      "On a fresh install, one CPU or roughly 2 GiB RAM or less triggers a warning before dependencies are installed. Continue anyway defaults to No. Unattended installs require explicit ZGW_ALLOW_LOW_RESOURCES=1 acknowledgement, or skip browser setup with ZGW_SKIP_GENERATOR=1. Existing installations are not repeatedly prompted.",
    ], sizing: [
      ["1 browser (bare minimum)", "1 reliable vCPU", "2 GB", "Limited headroom; slow work expected"],
      ["1 browser (recommended)", "2 reliable vCPUs", "4 GB", "Comfortable starting point"],
      ["2 browsers", "4 vCPUs", "6-8 GB", "Measure peaks before increasing"],
      ["3-4 browsers", "4-6 vCPUs", "8-12 GB", "Depends on workload and contention"],
    ], commands: ["free -h", "nproc", "vmstat 1 5"] },
    { title: "Install and manage the service", tags: "autozcoder install status start stop restart update cert uninstall TLS HTTPS firewall 80 8443", steps: [
      "The Linux installer builds/downloads the gateway, provisions generator Python, Chromium/Xvfb, Node/Wrangler, direct HTTPS, a non-root service and renewal. HTTPS defaults to port 8443; issuance may fall back to self-signed TLS, so read the final report. No nginx is required. Builds/downloads can take several minutes.",
      "Run sudo autozcoder for the manager menu. status/logs inspect; start/stop/restart affect availability; update changes the binary and checks restart/health. Source updates need a current staged source tree, not just the old checkout already on the server.",
      "Trusted bare-IP certificates use short-lived issuance and automatic renewal. Port 80 must be reachable for standalone validation; keep firewall/provider rules aligned with the chosen dashboard port. If renewal fails, inspect the timer and installer log rather than repeatedly requesting certificates.",
      "sudo autozcoder cert stages and validates a certificate/key pair, checks gateway health under systemd, and restores prior TLS/configuration/renewal state if the change fails. The chosen TLS mode persists across reinstalls, including self-signed or loopback HTTP. Manager commands other than help require root.",
      "The service normally runs as autozcoder, not root. The binary and private Node installation remain root-owned; generator/browser/npm maintenance uses an unprivileged tooling user. After manual copying, check ownership rather than chmod 777 or running as root. ZGW_SKIP_SYSTEMD=1 leaves startup and renewal to your supervisor. Uninstall removes runtime state; back up first.",
    ], commands: ["sudo autozcoder", "sudo autozcoder status", "sudo autozcoder logs", "sudo systemctl status autozcoder-renew.timer", "sudo tail -n 80 /var/log/autozcoder-install.log"] },
    { title: "Where data lives and what to back up", tags: "root opt config YAML database snapshots mail relays generator-data paths", steps: [
      "Default packaged Linux root: /opt/zcodegateway (custom ZGW_INSTALL_DIR can override it). config.yaml has gateway keys/settings; accounts/ has snapshots; gateway.db has usage history; generator-data/ has options, mail_config.json, relays.json and proxy/job data.",
      "mail-worker/ holds worker sources/configuration; certs/ holds TLS material; genenv/, pw-browsers/, node/ and home/ hold runtime dependencies/caches. Installer state is /etc/autozcoder.env. /root/AutoZCoder is typically staging source, not live account storage.",
      "Backups contain account tokens, admin/client keys and mail secrets. Keep them private. For a consistent whole-install backup, stop the service first or use SQLite-aware backup tooling; copying only gateway.db while WAL writes are active can miss data.",
      "A source install may use generator/output instead of packaged generator-data. Confirm the actual configured paths. Uninstall can remove application state: back up before confirming, and do not assume it deletes Cloudflare deployments.",
    ] },
  ] },
];

export const ERRORS = [
  { title: "finish_signup HTTP 500 / account not confirmed after verification", tags: "captcha passed email verified internal error final signup recovery", route: "generator", steps: [
    "Captcha success and email verification do not complete account creation. If finish_signup returns a server error and no non-guest token is received, no usable account or plan can be confirmed.",
    "Auto Generate reports finish_signup_failed and pauses replacements. Private recovery details are saved in recovery_accounts.txt for the verified address. Do not confuse the failure with no plan, expose recovery credentials, or assume switching display modes repairs a provider-side HTTP 500.",
  ] },
  { title: "Relay health fails / deployment protection / wrong function URL", tags: "Cloudflare Vercel Deno Netlify relay secret 401 403 404", route: "proxy", steps: [
    "Check the deployed relay URL and matching secret, not the mail-worker inbox secret. For Netlify use the function endpoint. A provider's deployment protection can block requests even when the site homepage loads.",
    "Test the selected relay, inspect provider deployment logs and rate limits, and check which models are routed. Relay health establishes connectivity to its test target, not model entitlement or successful inference. Disable routing if you intentionally want to compare the direct path.",
  ] },
  { title: "503 waf_block / HTML 403 from the provider edge", tags: "WAF CDN shared egress circuit Retry-After", route: "proxy", steps: [
    "An HTML edge block is different from a JSON entitlement rejection. The gateway pauses affected traffic and probes recovery; respect Retry-After instead of adding accounts or flooding retries.",
    "Check egress and relay routing. A shared IP/CDN block cannot be repaired by rotating account snapshots. JSON 403 may instead represent a model entitlement restriction; inspect the response stage in Logs.",
  ] },
  { title: "529 / 1305 / model overload", tags: "busy retry backoff model provider", route: "models", steps: [
    "This means a busy model, not necessarily a bad account. The gateway retries before output with backoff. Wait, reduce concurrency, or deliberately choose another model.",
    "Do not purge or repeatedly reauthenticate accounts for an overload response. Once a stream starts, an error can arrive as an event even though the initial HTTP response was 200.",
  ] },
  { title: "502 transport_error / relay_transport_error / truncated stream", tags: "empty invalid stream socket timeout network", route: "logs", steps: [
    "Check whether the failing transport is direct upstream, the relay host, or the client connection. Collect the model, stage, status and minimal redacted log lines. Do not assume this is spent quota.",
    "Increase read timeout only for a demonstrated slow response. Broken connections, invalid stream format and an unavailable relay need different remedies; consult relay tests and network controls first.",
  ] },
  { title: "Start Plan 3007 / 1001 / 1003 / 1005", tags: "Aliyun material offer gone already claimed limit unavailable mint", route: "generator", steps: [
    "3007 requires Aliyun captcha verification material: inspect native captcha-mint/sidecar output or the supported browser path. Rotating proxies is not the prescribed fix for missing material.",
    "1001 can mean the offer is gone; 1003 already claimed; 1005 a business limit/unavailable result. Read the claim response and current billing entitlements. Already claimed alone does not prove usable quota, and these claim codes are distinct from inference failures.",
  ] },
  { title: "SIGNUP_RATE_LIMITED / HTTP 451 / local connection exhaustion", tags: "signup retry interval geo legal file descriptors sockets", route: "generator", steps: [
    "For SIGNUP_RATE_LIMITED, respect the supplied retry interval and scope. Repeated same-path attempts can waste verification addresses and captcha work. Some incomplete signup details may be saved in recovery_accounts.txt; the dashboard does not automatically recover every incomplete signup.",
    "HTTP 451 indicates a regional/legal restriction; inspect provider policy and the affected model/path. Do not treat it as a token-expiry error.",
    "Local file-descriptor/socket exhaustion is a host capacity issue: reduce checker/worker concurrency and inspect resource limits. It is not proof every proxy is dead.",
  ] },
  { title: "Dashboard 401 / 403: admin key rejected", tags: "auth login password remote forbidden origin host", route: "server", steps: [
    "Use this gateway's admin key. Client keys and account JWTs cannot unlock the dashboard. On remote hosts, verify remote-admin settings and the exact HTTPS hostname/port.",
    "If a reverse proxy is involved, check forwarded host/origin configuration. Do not disable origin protection or publish the admin key to work around it. A fresh private window can separate stale sessionStorage from a server-side rejection.",
  ] },
  { title: "Model request 401: account credential rejected", tags: "revoked JWT expires login token refresh", route: "accounts", steps: [
    "Distinguish a rejected gateway client key from an upstream account-token rejection in Logs. Check account state and refresh saved credentials. If no new token exists, log in again or import a fresh export.",
    "No declared expiry is not a promise of validity. Copying the current token copies what the gateway holds; it does not mint a new one.",
  ] },
  { title: "402 / 1005 / quota exhausted / no eligible account", tags: "credits plan lost balance capped no entitlement", route: "accounts", steps: [
    "Refresh quotas, check the exact requested model, and inspect plan start/reset times. Check dead/cooling/model-capped states; a ready count alone does not prove availability for this model.",
    "Wait for a documented reset, add an entitled account, or choose an available model. Enabling an account, reloading a file or restarting the gateway does not create provider credit.",
  ] },
  { title: "405 / 3012 / unusual activity", tags: "blocked model IP edge relay", route: "proxy", steps: [
    "Inspect the model/account cap. If appropriate, configure and test an edge relay for that model before enabling it. A provider restriction may persist, and relays are not a universal bypass.",
    "If the message came from proxy CONNECT during a scan, that proxy refused HTTPS tunneling; this is not the same as a model endpoint returning 405.",
  ] },
  { title: "429 / overloaded / repeated backoff", tags: "rate limit retries concurrency pacing", route: "settings", steps: [
    "Reduce simultaneous requests/workers and allow backoff. Check provider rate limits, available accounts and model pacing. More retries can increase load rather than fix it.",
    "Use Settings' connection/retry controls deliberately. A gateway-level client rate limit and an upstream provider overload are separate causes; check Logs for the failing stage.",
  ] },
  { title: "Quota check unavailable or Awaiting quota check", tags: "HTTP billing timeout stale refresh plans not checked", route: "accounts", steps: [
    "Use Refresh quotas. Transport errors receive one retry; failed checks retain last-known data and show an error. A billing HTTP 401 first checks for newer saved credentials; unresolved authentication failures use three consecutive strikes rather than immediate retirement. A billing HTTP 404 is a check error, not proof of a dead account. Successful checks show their last-update time.",
    "If it persists, check upstream/network availability and logs. Quota polling interval is not a strict per-account deadline: a slow sweep can delay later checks. Do not delete accounts just because billing temporarily times out.",
  ] },
  { title: "Inbox poll 401 / unauthorized", tags: "email secret INBOX_SECRET worker", route: "generator", steps: [
    "The inbox URL and secret do not match the deployed worker. Save the exact /inbox URL with no query/fragment and the raw deployed secret, then test the inbox. Worker /health returning 200 is not an auth test.",
    "Restart generator jobs after saving. If you rotate the shared worker secret, update every installation; copying an old configuration again will not fix a deployed-secret mismatch.",
  ] },
  { title: "Inbox 404 / 503 or verification email never arrives", tags: "routing MX catch-all KV health deployment", route: "generator", steps: [
    "404 can mean the wrong endpoint/path or missing deployment. 503 can indicate missing worker configuration/bindings; inspect the worker setup log. Test the authenticated inbox before another signup.",
    "If inbox auth succeeds but mail is absent, inspect Email Routing status, DNS MX/TXT, catch-all worker target and named rules. Send one real test message. A successful signup response only starts verification; it does not prove email delivery.",
  ] },
  { title: "Cloudflare 403 / permission denied / multiple accounts", tags: "token scopes expired zone settings account ID wrangler", route: "generator", steps: [
    "Review all seven permissions listed under Email setup, including Zone Settings Edit, resource scope, token expiry and IP filters. A token seeing the wrong account/zone cannot deploy or configure that domain.",
    "Set the intended Cloudflare account ID if needed. Save token only replaces local credentials without redeploy. Do not paste API tokens into logs or shell command history.",
  ] },
  { title: "Routing needs confirmation / changed since review / busy", tags: "409 fingerprint catch-all race deploy lock", route: "generator", steps: [
    "Review the existing catch-all destination and approve only the current proposed change. If the rule changed, reload setup and review the new fingerprint. Keep current routing if you do not want unmatched mail diverted.",
    "A busy 409 means another mail operation is in progress. Let it finish; do not run simultaneous deploy/routing requests or disable the confirmation guard.",
  ] },
  { title: "AI judge unavailable / vision request rejected", tags: "captcha model images custom endpoint key no pool bootstrap", route: "generator", steps: [
    "AI-only solving needs either a usable vision-capable account through this gateway or a configured custom vision endpoint. Empty or capped pools cannot bootstrap the gateway-backed judge.",
    "Check endpoint URL, API key, exact model ID, image input support and provider limits. A text-only model cannot judge screenshots. Inspect the judge error; do not assume browser failure or mail failure is an AI issue.",
  ] },
  { title: "Captcha failed / F001 / repeated signup failure", tags: "Aliyun drag browser device verification retries", route: "generator", steps: [
    "Identify whether failure was browser launch, challenge solving, signup submission, email verification or plan claim. Keep one worker for diagnosis and use the same configured browser context through the stage.",
    "F001 can involve device/browser verification; the pipeline uses headed Chromium, including on Xvfb. Native headless substitution, more retries or changing the judge does not guarantee success. Stop repeated attempts if the prerequisite inbox/network stage is broken.",
  ] },
  { title: "Generator not found / Python missing / Chromium executable missing", tags: "environment packages Playwright install locate permission", route: "generator", steps: [
    "Locate the scripts, run Environment setup, and inspect its full output. Confirm the configured Python and browser directory belong to this installation and are readable/executable by the service user.",
    "On Linux, also check shared libraries, available disk space and Xvfb. Do not copy Windows .exe helpers to Linux; the installer bundles a native helper for the server architecture.",
  ] },
  { title: "Manual interaction requires DISPLAY / Xvfb missing", tags: "desktop virtual headless VPS browser window", route: "generator", steps: [
    "Use AI/automatic solving and Virtual display on a displayless Linux server. Manual requires a real interactive desktop; SSH alone provides none. Install/repair Xvfb if background launch says it is missing.",
    "Saved browser settings apply to new workers. Killing existing workers is interruptive; changing a dropdown does not move a currently running window.",
  ] },
  { title: "Proxy scan: 0 alive / all TCP ports open", tags: "TUN Xray reset timeout CONNECT SOCKS rejected", route: "proxy", steps: [
    "Read the network warning and actual protocol-stage counters. TUN paths may accept every TCP connect locally; the checker then requires proxy handshakes and caps concurrency. This does not bypass the tunnel.",
    "Use a known-working proxy as a control and compare another exit/routing policy. Public lists can be stale, refuse CONNECT, require auth, or be unreachable. Raising concurrency does not repair them.",
  ] },
  { title: "Proxy TLS / certificate / MITM / auth failure", tags: "CA HTTPS 407 socks credentials unsafe", route: "proxy", steps: [
    "Check protocol, port, proxy authentication and certificate validity. Bad/MITM entries are excluded intentionally. A TLS interception certificate is not evidence that the candidate is safe.",
    "Do not disable TLS validation to inflate working counts. If your local network intentionally inspects TLS, resolve trust and policy with the network owner rather than trusting arbitrary proxy certificates.",
  ] },
  { title: "JWT import rejected / export unsupported / expired", tags: "share account version invalid malformed missing label", route: "accounts", steps: [
    "Copy the complete account export, not a screenshot, masked token, truncated message, or whole snapshot file. The import accepts the supported bundle version or a single raw CLI JWT.",
    "The token must pass provider validation and its account ID must match the bundle. If expired/revoked, ask the owner for a new export or log in again. A provider outage leaves nothing imported; retry after connectivity recovers.",
    "Clipboard failure on plain remote HTTP can be a browser security restriction. Use HTTPS; some browsers may still deny clipboard access. Do not publish exports in support messages.",
  ] },
  { title: "Gateway unreachable / startup failed / TLS expired", tags: "service port firewall cert renew bind 8443 health", route: "server", steps: [
    "Confirm the configured host/port and HTTPS scheme, service health, firewall and provider network rules. Binding 127.0.0.1 on a VPS requires an SSH tunnel; it is not publicly reachable.",
    "On Linux inspect systemctl status zcodegateway and journalctl -u zcodegateway. For TLS inspect the renewal timer, cert paths/ownership and port 80 validation reachability. Do not expose a remote admin dashboard over plain HTTP as a routine workaround.",
    "After an update, a hard-refresh fixes cached UI assets, not a stopped service. Preserve the rollback binary and investigate the startup error before repeating updates.",
  ] },
  { title: "Plan claim unavailable / withdrawn giveaway / stopped job", tags: "Start Plan 100M 500M global build issued false", route: "generator", steps: [
    "Inspect the claim result: job completion is not the same as a grant being issued. The provider may withdraw a campaign or reject entitlement. Older 100M/500M labels are not a promise those offers remain available.",
    "Refresh plans/quotas after a successful claim. If unavailable, stop retrying across IPs: it cannot create a withdrawn offer. Do not purge the account merely because an optional claim failed.",
  ] },
];
