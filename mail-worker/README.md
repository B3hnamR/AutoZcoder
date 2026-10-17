# zai-mail — Cloudflare Email Worker

Catch-all mail for `behnamrjd.me` → this Worker → KV → the generator polls it over HTTP.
No Gmail, no IMAP, no app password.

```
zai<random>@behnamrjd.me  --(cloudflare email routing)-->  zai-mail Worker  -->  KV
generator:  GET https://zai-mail.<you>.workers.dev/inbox?to=<addr>&k=<secret>
```

## Deploy

```powershell
cd D:\JB\zcode-mitm\ZCODEAUTO\mail-worker
npm i -g wrangler            # once
wrangler login               # opens a browser, authorize

wrangler kv namespace create MAIL
#   -> prints an id like  a1b2c3...  ; paste it into wrangler.toml [[kv_namespaces]] id

# set a real secret (instead of the placeholder in [vars])
wrangler secret put INBOX_SECRET      # paste a random string

wrangler deploy
#   -> prints the URL: https://zai-mail.<subdomain>.workers.dev
```

Then wire it in the dashboard:

1. **Email Routing → behnamrjd.me → Destination Workers** → the `zai-mail` worker should be
   listed (create/bind it if the UI asks).
2. **Routes → Catch-all → Edit** → Action = **Send to a Worker** → select `zai-mail` → Save.
3. Send a test: mail anything to `test123@behnamrjd.me`, then open:
   `https://zai-mail.<you>.workers.dev/inbox?k=<secret>`

## Endpoints

| Request | Result |
|---|---|
| `GET /health` | `{ok:true}` |
| `GET /inbox?k=<secret>` | 20 most recent messages |
| `GET /inbox?to=<addr>&k=<secret>` | that address's message `{to,from,subject,link,ts}` |

`link` is the extracted `https://chat.z.ai/auth/verify_email?...token=verify-<hex32>`.

## Generator side

`gen/config.py`:
```python
'mail_domain': 'behnamrjd.me',
'mail_inbox_url': 'https://zai-mail.<you>.workers.dev/inbox',
'mail_inbox_secret': '<the same secret>',
```
`gen/main.py` `CatchAllMail` polls `mail_inbox_url` when set, else falls back to IMAP.
