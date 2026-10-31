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
      // Per-recipient pointers. The verify link gets its OWN key, written
      // only when a link is present — the old single 'to:' slot was a
      // GET-then-PUT race: a linkless welcome mail landing between the
      // verify mail's get and put clobbered the stored link with null.
      if (to) {
        if (rec.link) {
          await env.MAIL.put('link:' + to, JSON.stringify(rec), { expirationTtl: 3600 });
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

    // every other path (including '/', '/inbox/', '/inboxx') is NOT the
    // inbox — answering 200 'ok' here made paste-the-wrong-URL health
    // checks pass while the real inbox poll failed on non-JSON
    if (url.pathname !== '/inbox') return new Response('not found', { status: 404 });

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
      // prefer the dedicated link pointer; fall back to the generic slot
      const v = (await env.MAIL.get('link:' + to)) || (await env.MAIL.get('to:' + to));
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
  // Decode individual MIME bodies, never a body plus the following boundary.
  // Bound nesting and part count so malformed mail cannot recurse indefinitely.
  let remaining = 100;
  function visit(part, depth) {
    if (depth > 12 || remaining-- <= 0) return null;
    const split = part.indexOf('\n\n');
    const headers = Object.create(null);
    let body = part;
    if (split >= 0) {
      const head = part.slice(0, split).replace(/\n[ \t]+/g, ' ');
      for (const line of head.split('\n')) {
        const match = line.match(/^([\w-]+):[ \t]*(.*)$/);
        if (match) headers[match[1].toLowerCase()] = match[2];
      }
      if (Object.keys(headers).length) body = part.slice(split + 2);
    }
    const type = headers['content-type'] || 'text/plain';
    if (/^multipart\//i.test(type)) {
      const match = type.match(/;\s*boundary\s*=\s*(?:"([^"\n]+)"|([^;\s]+))/i);
      const boundary = match && (match[1] || match[2]);
      if (!boundary || boundary.length > 200) return null;
      let lines = null;
      for (const line of body.split('\n')) {
        const delimiter = line.trimEnd();
        if (delimiter === '--' + boundary || delimiter === '--' + boundary + '--') {
          if (lines !== null) {
            const link = visit(lines.join('\n'), depth + 1);
            if (link) return link;
          }
          if (delimiter === '--' + boundary + '--') return null;
          if (remaining <= 0) return null;
          lines = [];
        } else if (lines !== null) {
          lines.push(line);
        }
      }
      return lines === null ? null : visit(lines.join('\n'), depth + 1);
    }
    if (!/^text\/(?:plain|html)(?:\s*;|\s*$)/i.test(type)
        || /^attachment(?:\s*;|\s*$)/i.test(headers['content-disposition'] || '')) return null;
    const encoding = (headers['content-transfer-encoding'] || '').trim().toLowerCase();
    if (encoding === 'base64') {
      try {
        body = atob(body.replace(/[ \t\n]/g, ''));
      } catch (e) {
        return null; // A malformed part must not hide a later valid alternative.
      }
    } else if (encoding === 'quoted-printable') {
      body = body.replace(/=\n/g, '').replace(/=([0-9a-f]{2})/gi,
        (_, hex) => String.fromCharCode(parseInt(hex, 16)));
    } else if (encoding && !['7bit', '8bit', 'binary'].includes(encoding)) {
      return null;
    }
    const match = body.replace(/&amp;/gi, '&').match(/https?:\/\/[^\s"'<>]*verify_email[^\s"'<>]*/i);
    return match ? match[0].replace(/[)\].,;"']+$/, '') : null;
  }
  return visit(raw.replace(/\r\n/g, '\n'), 0);
}
