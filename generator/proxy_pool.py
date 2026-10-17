"""Proxy pool for the z.ai generator.

Reuses kickview's source list (D:\\JB\\kickview\\data\\sources.txt), but verifies each
proxy against **chat.z.ai itself** — ipify/kick reachability is irrelevant here. A proxy
is only kept if it can actually GET https://chat.z.ai/api/v1/auths/.

Usage:
    python proxy_pool.py fetch      # leech sources -> output/proxies_raw.txt
    python proxy_pool.py verify     # test raw list against chat.z.ai -> output/proxies.txt
    python proxy_pool.py            # both
    python proxy_pool.py pick       # print one working proxy URL (for a single run)

Then run the generator pinned to one exit IP for the whole flow:
    $env:ZAI_PROXY='http://1.2.3.4:8080'; python main.py 1
"""

import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3

urllib3.disable_warnings()

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_FILE = os.path.join(HERE, 'output', 'proxies_raw.txt')
GOOD_FILE = os.path.join(HERE, 'output', 'proxies.txt')
BAD_FILE = os.path.join(HERE, 'output', 'proxies_bad.txt')
# Every proxy that EVER passed a probe, best-first. verify/recheck rewrite this
# cumulatively (merge, never truncate) so a good proxy is never lost just
# because it was flaky during one particular scan.
EVERGOOD_FILE = os.path.join(HERE, 'output', 'proxies_evergood.txt')

KICKVIEW_SOURCES = r'D:\JB\kickview\data\sources.txt'
SOURCES_URL = 'https://raw.githubusercontent.com/B3hnamR/kickview/main/data/sources.txt'
# User-editable extra sources, managed from the gateway UI ("proto url" per line).
LOCAL_SOURCES = os.path.join(HERE, 'output', 'proxy_sources.txt')

# Built-in fallback (mirrors kickview's list) if the sources file is missing.
DEFAULT_SOURCES = [
    ('http',   'https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all'),
    ('socks5', 'https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=5000&country=all&ssl=all&anonymity=all'),
    ('socks4', 'https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks4&timeout=5000&country=all&ssl=all&anonymity=all'),
    ('socks5', 'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt'),
    ('http',   'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt'),
    ('http',   'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt'),
    ('socks5', 'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt'),
    ('socks4', 'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt'),
    ('socks5', 'https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5.txt'),
    ('socks4', 'https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4.txt'),
    ('socks5', 'https://api.openproxylist.xyz/socks5.txt'),
    ('http',   'https://api.openproxylist.xyz/http.txt'),
    ('socks4', 'https://api.openproxylist.xyz/socks4.txt'),
    ('http',   'https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt'),
    ('http',   'https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt'),
    ('socks5', 'https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt'),
]

IP_PORT = re.compile(r'^(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})$')
CHECK_URL = 'https://chat.z.ai/api/v1/auths/'
CHECK_PAGE = 'https://chat.z.ai/auth'
# The signup captcha is served from Aliyun (scene 36qgs6xb, prefix no8xfe). A proxy that
# reaches chat.z.ai but NOT this host makes the widget fail with
# "verification service unavailable" — so it is part of proxy quality.
CHECK_CAPTCHA = 'https://no8xfe-verify.captcha-open-southeast.aliyuncs.com/'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def _parse_source_lines(text):
    out = []
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) == 2 and parts[0] in ('http', 'socks4', 'socks5'):
            out.append((parts[0], parts[1]))
        elif len(parts) == 1 and parts[0].startswith('http'):
            out.append(('http', parts[0]))
    return out


def read_local_sources():
    """User-added sources from the editable file (gateway UI)."""
    try:
        if os.path.exists(LOCAL_SOURCES):
            return _parse_source_lines(open(LOCAL_SOURCES, encoding='utf-8').read())
    except Exception:
        pass
    return []


def write_local_sources(text):
    """Replace the editable sources file with `text` (proto url per line)."""
    os.makedirs(os.path.dirname(LOCAL_SOURCES), exist_ok=True)
    tmp = LOCAL_SOURCES + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text or '')
    os.replace(tmp, LOCAL_SOURCES)
    return len(_parse_source_lines(text))


def import_proxies(text, into_good=True):
    """Add pasted/uploaded proxies to the pool.

    Accepts 'proto://ip:port', 'ip:port' (defaults http), comma/space/newline
    separated. `into_good` also adds them to the usable pool + ever-good ledger
    (user-vouched: used before verification)."""
    entries = []
    for raw in re.split(r'[\s,;]+', (text or '').strip()):
        if not raw:
            continue
        if '://' in raw:
            proto, rest = raw.split('://', 1)
            if proto in ('http', 'https', 'socks4', 'socks5') and IP_PORT.match(rest):
                entries.append(f'{proto}://{rest}')
        elif IP_PORT.match(raw):
            entries.append(f'http://{raw}')
    if not entries:
        return 0
    os.makedirs(os.path.dirname(RAW_FILE), exist_ok=True)
    with open(RAW_FILE, 'a', encoding='utf-8') as f:
        f.write('\n'.join(entries) + '\n')
    if into_good:
        existing = set(load_good())
        fresh = [e for e in entries if e not in existing]
        with open(GOOD_FILE, 'a', encoding='utf-8') as f:
            if fresh:
                f.write('\n'.join(fresh) + '\n')
        with open(EVERGOOD_FILE, 'a', encoding='utf-8') as f:
            f.write('\n'.join(fresh) + '\n')
    return len(entries)


def read_sources():
    srcs = list(DEFAULT_SOURCES) + read_local_sources()
    try:
        if os.path.exists(KICKVIEW_SOURCES):
            with open(KICKVIEW_SOURCES, 'r', encoding='utf-8') as f:
                srcs.extend(_parse_source_lines(f.read()))
    except Exception as e:
        print(f"  (sources file unreadable: {e}; using built-ins)")
    # dedupe
    seen, out = set(), []
    for s in srcs:
        if s[1] not in seen:
            seen.add(s[1])
            out.append(s)
    return out


def fetch_source(proto, url):
    try:
        r = requests.get(url, timeout=30, verify=False,
                         headers={'User-Agent': UA})
        got = []
        for line in r.text.splitlines():
            line = line.strip()
            m = IP_PORT.match(line)
            if m:
                got.append(f"{proto}://{line}")
        return got
    except Exception as e:
        print(f"  ! {proto} {url[:60]}… {type(e).__name__}")
        return []


def cmd_fetch():
    os.makedirs(os.path.dirname(RAW_FILE), exist_ok=True)
    all_lines = set()
    for proto, url in read_sources():
        got = fetch_source(proto, url)
        print(f"  + {len(got):6d}  {proto:6s} {url[:70]}")
        all_lines.update(got)
    with open(RAW_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(sorted(all_lines)) + '\n')
    print(f"\nraw pool: {len(all_lines)} proxies -> {RAW_FILE}")
    return len(all_lines)


_lock = threading.Lock()
_file_lock = threading.Lock()   # serializes pool-file read-modify-write across threads
_alive = []
_new_this_run = []   # proxies found alive in THIS invocation (for incremental saves)

# The box's default CA path is broken (verify=True fails for every host), but
# certifi's bundle works — and it is what lets us DETECT MITM proxies: a proxy
# presenting its own cert fails validation, a pass-through proxy does not.
try:
    import certifi
    _VERIFY = certifi.where()
except Exception:
    _VERIFY = False

MAX_LATENCY = float(os.environ.get('PROXY_MAX_LATENCY', '5.0'))


def probe(purl, timeout=10):
    """Return latency if this proxy can deliver the app page AND reach the Aliyun
    captcha host, with a VALID cert.

    Three things must hold or the run is wasted:
      - /auth returns real HTML (a thin proxy answers the API but not the page → blank)
      - the Aliyun captcha host is reachable (else "verification service unavailable")
      - the cert validates (verify=certifi) → rejects MITM proxies
    """
    t0 = time.time()
    try:
        r = requests.get(CHECK_PAGE, proxies={'http': purl, 'https': purl},
                         timeout=timeout, verify=_VERIFY, headers={'User-Agent': UA})
        if r.status_code != 200:
            return None
        ct = (r.headers.get('content-type') or '').lower()
        if 'html' not in ct or len(r.text) < 1500:
            return None
    except Exception:
        return None

    lat = time.time() - t0          # page latency ONLY (was double-counting both fetches)

    # captcha host: ANY http response proves reachability (it 404s/405s on a bare GET).
    # Pass/fail only — not part of the latency score.
    try:
        requests.get(CHECK_CAPTCHA, proxies={'http': purl, 'https': purl},
                     timeout=timeout, verify=_VERIFY, headers={'User-Agent': UA})
    except Exception:
        return None
    return lat


def load_good():
    if not os.path.exists(GOOD_FILE):
        return []
    with open(GOOD_FILE, 'r', encoding='utf-8') as f:
        return [ln.strip() for ln in f if ln.strip()]


def load_bad():
    if not os.path.exists(BAD_FILE):
        return set()
    with open(BAD_FILE, 'r', encoding='utf-8') as f:
        return {ln.split('\t')[0].strip() for ln in f if ln.strip()}


def _atomic_write(path, content):
    """Write via temp file + os.replace — a reader (or a crash mid-write)
    never sees a truncated/half-written pool file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(content)
    os.replace(tmp, path)


def _save_pool(alive):
    """alive: [(lat, url)] sorted best-first. Rewrites the pool AND merges into
    ever-good. Truncates only the pool file; ever-good is cumulative."""
    _atomic_write(GOOD_FILE, '\n'.join(p for _, p in alive) + ('\n' if alive else ''))
    # merge into ever-good: new finds appended on top, existing keep their rank
    prev = []
    if os.path.exists(EVERGOOD_FILE):
        with open(EVERGOOD_FILE, 'r', encoding='utf-8') as f:
            prev = [ln.strip() for ln in f if ln.strip()]
    seen = set(p for _, p in alive)
    merged = [p for _, p in alive] + [p for p in prev if p not in seen]
    bad = load_bad()
    merged = [p for p in merged if p not in bad]
    _atomic_write(EVERGOOD_FILE, '\n'.join(merged) + '\n')


def mark_bad(purl, why=''):
    """Blacklist a proxy (e.g. it MITM'd TLS) and drop it from the good pool.

    Free proxies that intercept HTTPS present their own cert; chromium rejects them
    (ERR_CERT_AUTHORITY_INVALID). probe() now validates certs too, but a proxy can
    start MITM-ing later — so we also learn it at run time and purge it here.
    """
    if not purl:
        return
    os.makedirs(os.path.dirname(BAD_FILE), exist_ok=True)
    with _file_lock:
        with open(BAD_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{purl}\t{why}\n")
        good = load_good()
        if purl in good:
            good = [p for p in good if p != purl]
            _atomic_write(GOOD_FILE, '\n'.join(good) + ('\n' if good else ''))


def socks_only(proxies):
    """Keep only socks4/socks5 entries — residential-style proxies pass the
    Starter gate; HTTP datacenter ranges fail it (2026-10-15/16 yield data)."""
    out = []
    for p in proxies:
        scheme = p.split('://', 1)[0].lower()
        if scheme in ('socks4', 'socks5'):
            out.append(p)
    return out


def pick_verified(tries=30, timeout=8, socks=False):
    """Pick a FAST, live proxy: the pool is latency-sorted, so sample among the
    quickest and re-test right before use. Falls back to ever-good (past finds)
    when the current pool is empty or lacking socks. socks=True: socks4/5 entries only."""
    bad = load_bad()
    good = [p for p in load_good() if p not in bad]
    if socks:
        good = socks_only(good)
    if (not good or len(good) < tries) and os.path.exists(EVERGOOD_FILE):
        with open(EVERGOOD_FILE, 'r', encoding='utf-8') as f:
            eg = [ln.strip() for ln in f if ln.strip() and ln.strip() not in bad]
            if socks:
                eg = socks_only(eg)
            good_set = set(good)
            good = good + [p for p in eg if p not in good_set]
    if not good:
        return None
    head = good[:min(120, len(good))]      # fastest slice
    random.shuffle(head)
    candidates = head[:min(len(head), max(tries, 25))]

    ex = ThreadPoolExecutor(max_workers=min(10, len(candidates)))
    try:
        futs = {ex.submit(probe, p, timeout): p for p in candidates}
        for fut in as_completed(futs):
            try:
                lat = fut.result()
                if lat is not None:
                    # first success: stop now. Returning inside the `with`
                    # used to call shutdown(wait=True), blocking until every
                    # queued probe finished (~24s) — the early exit was fake.
                    for f in futs:
                        f.cancel()
                    return futs[fut]
            except Exception:
                pass
    finally:
        # cancel queued probes and drop running ones; don't wait for them
        ex.shutdown(wait=False, cancel_futures=True)
    return None


def _check_one(purl, timeout):
    lat = probe(purl, timeout)
    if lat is not None:
        with _lock:
            _alive.append((lat, purl))
            _new_this_run.append((lat, purl))
    return purl


def _flush_incremental(total, done_count):
    """Persist partial progress so an interrupted run keeps what it found."""
    with _lock:
        snapshot = sorted([(lat, p) for lat, p in _new_this_run if lat <= MAX_LATENCY])[:200]
    if not snapshot:
        return
    _save_pool(snapshot)
    print(f"  [checkpoint] {done_count}/{total} probed, {len(_new_this_run)} alive "
          f"— saved best {len(snapshot)}", flush=True)


def cmd_verify(timeout=None, concurrency=None, limit=None):
    global _alive, _new_this_run
    timeout = timeout or int(os.environ.get('PROXY_TIMEOUT', '8'))
    concurrency = concurrency or int(os.environ.get('PROXY_CONC', '200'))
    if not os.path.exists(RAW_FILE):
        print("no raw pool — run `python proxy_pool.py fetch` first")
        return 0
    with open(RAW_FILE, 'r', encoding='utf-8') as f:
        proxies = [ln.strip() for ln in f if ln.strip()]
    bad = load_bad()
    proxies = [p for p in proxies if p not in bad]
    if limit:
        proxies = proxies[:limit]
    print(f"verifying {len(proxies)} proxies against chat.z.ai "
          f"(concurrency={concurrency}, timeout={timeout}s)…")
    _alive, _new_this_run = [], []
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(_check_one, p, timeout) for p in proxies]
        for _ in as_completed(futs):
            done += 1
            if done % 200 == 0 or done == len(proxies):
                _flush_incremental(len(proxies), done)
    alive = [(lat, p) for lat, p in _alive if lat <= MAX_LATENCY]
    alive.sort()                       # fastest first — pick_verified samples the head
    _save_pool(alive)
    slow = len(_alive) - len(alive)
    print(f"\nworking proxies: {len(alive)} / {len(proxies)} (<= {MAX_LATENCY}s"
          f"; dropped {slow} too-slow/MITM)  -> {GOOD_FILE}")
    if os.path.exists(EVERGOOD_FILE):
        with open(EVERGOOD_FILE, 'r', encoding='utf-8') as f:
            ever = sum(1 for ln in f if ln.strip())
        print(f"ever-good (cumulative, survives restarts): {ever}  -> {EVERGOOD_FILE}")
    if alive:
        print(f"latency: best {alive[0][0]:.2f}s  median {alive[len(alive)//2][0]:.2f}s")
    return len(alive)


def cmd_pick():
    p = pick_verified()
    if p:
        print(p)
        return p
    print("no live proxy found (pool empty or all probed were dead)")
    return None


def cmd_recheck(timeout=None, concurrency=None):
    """Strictly re-test the CURRENT pool (cert-valid + fast) and rewrite it."""
    global _alive, _new_this_run
    timeout = timeout or int(os.environ.get('PROXY_TIMEOUT', '8'))
    concurrency = concurrency or int(os.environ.get('PROXY_CONC', '100'))
    if not os.path.exists(GOOD_FILE):
        print("no pool — run fetch/verify first")
        return 0
    with open(GOOD_FILE, 'r', encoding='utf-8') as f:
        proxies = [ln.strip() for ln in f if ln.strip()]
    bad = load_bad()
    proxies = [p for p in proxies if p not in bad]
    print(f"strict recheck of {len(proxies)} pooled proxies "
          f"(valid TLS + <= {MAX_LATENCY}s, concurrency={concurrency})…")
    _alive, _new_this_run = [], []
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(_check_one, p, timeout) for p in proxies]
        for _ in as_completed(futs):
            done += 1
            if done % 10 == 0 or done == len(proxies):
                print(f"  [checkpoint] {done}/{len(proxies)} probed, {len(_alive)} alive", flush=True)
    alive = [(lat, p) for lat, p in _alive if lat <= MAX_LATENCY]
    alive.sort()
    _save_pool(alive)
    print(f"\nworking proxies: {len(alive)} / {len(proxies)}  -> {GOOD_FILE}")
    if alive:
        print(f"latency: best {alive[0][0]:.2f}s  median {alive[len(alive)//2][0]:.2f}s")
    return len(alive)


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'all'
    lim = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None
    if cmd in ('fetch', 'all'):
        cmd_fetch()
    if cmd in ('verify', 'all'):
        cmd_verify(limit=lim)
    if cmd == 'recheck':
        cmd_recheck()
    if cmd == 'pick':
        cmd_pick()

