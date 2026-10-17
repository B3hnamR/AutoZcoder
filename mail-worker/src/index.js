// Cloudflare Email Worker for z.ai signup verification mail.
//
//   Email Routing (catch-all)  ->  this Worker  ->  KV
//   GET /inbox?to=<addr>&k=<secret>  ->  {to, from, subject, link, ts}
//   GET /inbox?k=<secret>            ->  most recent messages
//
// Stores the extracted chat.z.ai verify link so the generator can poll it over HTTP
// instead of IMAP (no Gmail app password, no third-party inbox).

export default {
  async email(message, env, ctx) {
    const to = (message.to || '').toLowerCase();
    let raw = '';
    try {
      raw = await streamToString(message.raw);
    } catch (e) {
      raw = '';
    }
    const rec = {
      to,
      from: message.from,
      subject: message.headers.get('subject') || '',
      link: extractVerifyLink(raw),
      ts: Date.now(),
    };
    try {
      // 13-digit zero-padded ts: lexicographic order == chronological order
      const key = 'msg:' + String(rec.ts).padStart(13, '0') + ':' + rand(6);
      await env.MAIL.put(key, JSON.stringify(rec), { expirationTtl: 3600 });
      // Per-recipient pointer slot. Overwrite only when this mail actually
      // carries a verify link — z.ai sends a welcome mail first, and a blind
      // overwrite used to destroy the stored verification link before the
      // signup flow ever polled it.
      if (to) {
        if (rec.link) {
          await env.MAIL.put('to:' + to, JSON.stringify(rec), { expirationTtl: 3600 });
        } else {
          const existing = await env.MAIL.get('to:' + to);
          if (!existing) {
            await env.MAIL.put('to:' + to, JSON.stringify(rec), { expirationTtl: 3600 });
          }
        }
      }
    } catch (e) {
      // swallow — a storage failure must not bounce the mail
    }
  },

  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === '/health') return json({ ok: true });

    if (url.pathname !== '/inbox') return new Response('ok');

    // The inbox serves verification links: it must NEVER be public. If the
    // secret is not configured, reject everything (fail closed).
    const secret = env.INBOX_SECRET;
    if (!secret) return json({ error: 'server misconfigured: INBOX_SECRET not set' }, 503);
    // constant-time compare: hash both sides first (WebCrypto lacks a direct
    // timingSafeEqual; SHA-256 of both then comparing digests is equivalent)
    const given = url.searchParams.get('k') || '';
    const enc = new TextEncoder();
    const [givenDigest, secretDigest] = await Promise.all([
      crypto.subtle.digest('SHA-256', enc.encode(given)),
      crypto.subtle.digest('SHA-256', enc.encode(secret)),
    ]);
    const a = new Uint8Array(givenDigest), b = new Uint8Array(secretDigest);
    let diff = 0;
    for (let i = 0; i < b.length; i++) diff |= a[i] ^ b[i];
    if (diff !== 0) {
      return json({ error: 'unauthorized' }, 401);
    }

    const to = (url.searchParams.get('to') || '').toLowerCase();
    if (to) {
      const v = await env.MAIL.get('to:' + to);
      return json(v ? JSON.parse(v) : { error: 'none' });
    }

    // Keys are 'msg:<13-digit zero-padded ts>:<rand>' — lexicographic order IS
    // chronological order. To return the newest records regardless of depth:
    // collect ALL msg keys (KV pages ascend; inboxes expire after 1h so the
    // full set is small), sort keys DESCENDING, and fetch only the newest 100.
    const allNames = [];
    let cursor;
    do {
      const list = await env.MAIL.list({ prefix: 'msg:', limit: 100, cursor });
      for (const k of list.keys) allNames.push(k.name);
      cursor = list.list_complete ? undefined : list.cursor;
    } while (cursor);
    allNames.sort((a, b) => (a < b ? 1 : -1));   // descending == newest first
    const out = [];
    for (const name of allNames.slice(0, 100)) {
      const v = await env.MAIL.get(name);
      if (v) { try { out.push(JSON.parse(v)); } catch (e) {} }
    }
    out.sort((a, b) => b.ts - a.ts);
    return json(out.slice(0, 20));
  },
};

function rand(n) {
  return Math.random().toString(36).slice(2, 2 + n);
}

function json(o, status = 200) {
  return new Response(JSON.stringify(o), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

async function streamToString(stream) {
  const reader = stream.getReader();
  const chunks = [];
  let len = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    len += value.length;
    if (len > 512 * 1024) break; // 512 KB cap
  }
  const buf = new Uint8Array(len);
  let off = 0;
  for (const c of chunks) { buf.set(c, off); off += c.length; }
  return new TextDecoder().decode(buf);
}

function extractVerifyLink(raw) {
  // Bodies are usually quoted-printable or base64. Undo the common mangling, then
  // look for the verify URL.
  let s = raw.replace(/=\r?\n/g, '').replace(/=3D/gi, '=').replace(/&amp;/gi, '&');
  let m = s.match(/https?:\/\/[^\s"'<>]*verify_email[^\s"'<>]*/i);
  if (m) return m[0].replace(/[)\].,;"']+$/, '');
  // base64 body fallback: decode each candidate INDEPENDENTLY — a non-base64
  // part (headers, mime cruft) must not abort the whole loop
  const parts = raw.split(/\r?\n\r?\n/);
  for (const p of parts) {
    try {
      const b = atob(p.replace(/\s+/g, ''));
      const mm = b.match(/https?:\/\/[^\s"'<>]*verify_email[^\s"'<>]*/i);
      if (mm) return mm[0];
    } catch (e) { /* not base64 — try the next part */ }
  }
  return null;
}
