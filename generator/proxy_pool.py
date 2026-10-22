"""Proxy pool for the z.ai generator.

Verifies each proxy against **chat.z.ai itself**: the auth page must load with a
valid certificate *and* the Aliyun captcha host must be reachable — ipify or
generic reachability is irrelevant here.

Pipeline for every candidate:
    TCP prefilter -> page check -> captcha-host check -> second chance
A proxy is only kept if all of that holds and it is faster than the latency cap.

State lives in ``pool.json`` with a health score per proxy (a failed re-check
adds one, a pass removes one) instead of a binary good/bad list, so one bad
moment does not evict a good proxy. ``proxies.txt`` stays as a best-first export
so ``main.py`` keeps working unchanged.

Usage:
    python proxy_pool.py fetch      # leech sources -> output/proxies_raw.txt
    python proxy_pool.py verify     # test raw list against chat.z.ai
    python proxy_pool.py recheck    # strictly re-test the live pool
    python proxy_pool.py pick       # print one claimed proxy URL
    python proxy_pool.py geo        # download the offline country/ASN database
    python proxy_pool.py stats      # pool health summary
    python proxy_pool.py            # fetch + verify

Then run the generator pinned to one exit IP for the whole flow:
    $env:ZAI_PROXY='http://1.2.3.4:8080'; python main.py 1
"""

from __future__ import annotations

import json
import os
import random
import re
import socket
import ssl
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3

import proxy_geo as geo
import proxy_parse as pp

urllib3.disable_warnings()

HERE = os.path.dirname(os.path.abspath(__file__))
COUNTRY_CACHE_FILE = None

# ----------------------------------------------------------------------------- paths


def _resolve_paths():
    global OUTPUT_DIR, RAW_FILE, GOOD_FILE, BAD_FILE, EVERGOOD_FILE, LOCAL_SOURCES
    global USED_FILE, COUNTRY_CACHE_FILE, POOL_FILE, DEAD_FILE, HISTORY_FILE
    global SOURCE_STATS_FILE, LOCK_FILE
    OUTPUT_DIR = os.environ.get('ZAI_OUTPUT_DIR') or os.path.join(HERE, 'output')
    RAW_FILE = os.path.join(OUTPUT_DIR, 'proxies_raw.txt')
    GOOD_FILE = os.path.join(OUTPUT_DIR, 'proxies.txt')
    BAD_FILE = os.path.join(OUTPUT_DIR, 'proxies_bad.txt')
    # best-first export of every proxy that ever passed; rebuilt from pool.json
    EVERGOOD_FILE = os.path.join(OUTPUT_DIR, 'proxies_evergood.txt')
    LOCAL_SOURCES = os.path.join(OUTPUT_DIR, 'proxy_sources.txt')
    USED_FILE = os.path.join(OUTPUT_DIR, 'proxies_used.txt')
    COUNTRY_CACHE_FILE = os.path.join(OUTPUT_DIR, 'ip_country.json')
    POOL_FILE = os.path.join(OUTPUT_DIR, 'pool.json')
    DEAD_FILE = os.path.join(OUTPUT_DIR, 'dead_cache.json')
    HISTORY_FILE = os.path.join(OUTPUT_DIR, 'proxy_history.json')
    SOURCE_STATS_FILE = os.path.join(OUTPUT_DIR, 'proxy_sources_stats.json')
    LOCK_FILE = os.path.join(OUTPUT_DIR, '.proxy.lock')
    try:
        geo.set_data_dir(OUTPUT_DIR)
    except Exception:
        pass


def set_output_dir(path: str):
    """Point the pool ledger files at a custom folder (e.g. gateway data_dir)."""
    os.environ['ZAI_OUTPUT_DIR'] = str(path)
    _resolve_paths()


_resolve_paths()

KICKVIEW_SOURCES = os.environ.get('KICKVIEW_SOURCES', r'D:\JB\kickview\data\sources.txt')
SOURCES_URL = 'https://raw.githubusercontent.com/B3hnamR/kickview/main/data/sources.txt'

DEFAULT_SOURCES = [
    ('http',   'https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all'),
    ('socks5', 'https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=5000&country=all&ssl=all&anonymity=all'),
    ('socks4', 'https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks4&timeout=5000&country=all&ssl=all&anonymity=all'),
    ('socks5', 'https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=5000&country=US&ssl=all&anonymity=all'),
    ('socks4', 'https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks4&timeout=5000&country=US&ssl=all&anonymity=all'),
    ('http',   'https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=US&ssl=all&anonymity=all'),
    ('socks5', 'https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt'),
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

CHECK_URL = 'https://chat.z.ai/api/v1/auths/'
CHECK_PAGE = 'https://chat.z.ai/auth'
CHECK_CAPTCHA = 'https://no8xfe-verify.captcha-open-southeast.aliyuncs.com/'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
PROBE_MARKER = os.environ.get('ZAI_PROBE_MARKER', 'z.ai')
MAX_PER_SOURCE = int(os.environ.get('PROXY_MAX_PER_SOURCE', '150000'))

SCAN_PRESETS = {
    'thorough': {'timeout': 12, 'concurrency': 300, 'probe_timeout': 4, 'probe_concurrency': 300},
    'balanced': {'timeout': 10, 'concurrency': 400, 'probe_timeout': 3, 'probe_concurrency': 300},
    'fast':     {'timeout': 6,  'concurrency': 800, 'probe_timeout': 2, 'probe_concurrency': 500},
}

# why a check failed: id -> short label (shown in the dashboard)
FAIL_LABELS = {
    'tcp_timeout': 'Port timed out',
    'tcp_refused': 'Port refused',
    'tcp_unreachable': 'Host unreachable',
    'timeout': 'Timed out',
    'refused': 'Refused',
    'reset': 'Connection dropped',
    'proxy_rejected': 'Proxy refused request',
    'proxy_auth': 'Needs a login',
    'wrong_protocol': 'Wrong protocol',
    'tls': 'TLS / certificate error',
    'bad_status': 'Bad HTTP status',
    'bad_response': 'Hijacked / invalid page',
    'blocked': 'Blocked by the site',
    'local_limit': 'Your PC ran out of connections',
    'other': 'Other error',
}
# failures that are often just a bad moment (flaky free proxy): worth a second chance
RETRY_REASONS = {'timeout', 'reset', 'proxy_rejected', 'bad_status', 'other', 'local_limit'}
# "this PC ran out of ports/buffers" — our fault, not the proxy's
LOCAL_LIMIT_ERRNOS = {10055, 10048, 24, 23, 105, 99, 10024}


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ('1', 'true', 'yes', 'on')


MAX_LATENCY = _env_float('PROXY_MAX_LATENCY', 6.0)
MAX_FAILS = _env_int('PROXY_MAX_FAILS', 3)
BAD_TTL_DAYS = _env_int('PROXY_BAD_TTL_DAYS', 30)


def resolve_scan_options(preset=None, timeout=None, concurrency=None,
                         probe_timeout=None, probe_concurrency=None,
                         prefilter=None, retry_failed=None, dead_cache_hours=None) -> dict:
    """Merge a preset with explicit values and environment defaults."""
    p = SCAN_PRESETS.get((preset or os.environ.get('ZAI_SCAN_PRESET') or '').lower(), SCAN_PRESETS['balanced'])
    return {
        'timeout': float(timeout if timeout is not None else os.environ.get('PROXY_TIMEOUT', p['timeout'])),
        'concurrency': int(concurrency if concurrency is not None else os.environ.get('PROXY_CONC', p['concurrency'])),
        'probe_timeout': float(probe_timeout if probe_timeout is not None
                               else os.environ.get('PROXY_PROBE_TIMEOUT', p['probe_timeout'])),
        'probe_concurrency': int(probe_concurrency if probe_concurrency is not None
                                 else os.environ.get('PROXY_PROBE_CONC', p['probe_concurrency'])),
        'prefilter': _env_bool('PROXY_PREFILTER', True) if prefilter is None else bool(prefilter),
        'retry_failed': _env_bool('PROXY_RETRY_FAILED', True) if retry_failed is None else bool(retry_failed),
        'retry_max': _env_int('PROXY_RETRY_MAX', 5000),
        'dead_cache_hours': float(dead_cache_hours if dead_cache_hours is not None
                                  else os.environ.get('PROXY_DEAD_HOURS', 0)),
        'max_latency': _env_float('PROXY_MAX_LATENCY', MAX_LATENCY),
        'max_fails': _env_int('PROXY_MAX_FAILS', MAX_FAILS),
    }


# ----------------------------------------------------------------------------- events / io

_EVENTS = _env_bool('PROXY_EVENTS', True)


def emit(**kw):
    """Structured JSONL line the gateway reads instead of scraping prose."""
    if not _EVENTS:
        return
    try:
        print('@event ' + json.dumps(kw, separators=(',', ':')), flush=True)
    except Exception:
        pass


def _atomic_write(path, content):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    # UNIQUE tmp name per process: eight workers share this code path, and a
    # fixed '<path>.tmp' let two processes interleave bytes into one file —
    # the loser's os.replace then installed corrupt JSON as the pool.
    tmp = f'{path}.{os.getpid()}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(content)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class _FileLock:
    """Cross-process lock: eight workers are eight processes, not eight threads.

    A live holder heartbeats the file's mtime so a slow critical section (a big
    pool import, frozen-AV hiccup) can never be mistaken for a crashed process
    and have its lock stolen mid-write. A crashed holder still expires in
    ``stale`` seconds.
    """

    def __init__(self, path: str, timeout: float = 15.0, stale: float = 60.0):
        self.path = path
        self.timeout = timeout
        self.stale = stale
        self.held = False
        self._lost = False
        self._beat: threading.Thread | None = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                self._beat = threading.Thread(target=self._heartbeat, daemon=True)
                self._beat.start()
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                if time.time() >= deadline:
                    # Timed out waiting: proceed WITHOUT the lock rather than
                    # deadlock, but leave a marker so operators can see it in
                    # the log instead of silently racing pool.json writes.
                    print(f"[proxy_pool] WARNING: lock wait >{self.timeout:.0f}s on "
                          f"{self.path}; proceeding unlocked (pool.json races possible)",
                          flush=True)
                    return self
                time.sleep(0.02)

    def _heartbeat(self):
        interval = max(0.5, self.stale / 3)
        failures = 0
        while self.held:
            time.sleep(interval)
            if not self.held:
                break
            try:
                os.utime(self.path, None)
                failures = 0
            except OSError:
                # our lockfile was removed (stale-steal) — stop touching a
                # file another process now owns; keep looping only so the
                # thread ends cleanly on release
                failures += 1
                if failures >= 3:
                    self._lost = True
                    return

    def __exit__(self, *exc):
        if self.held:
            self.held = False
            beat, self._beat = self._beat, None
            if beat:
                beat.join(timeout=0.2)
            # Only remove the lockfile if we still own it: after a stale-steal
            # the file on disk belongs to the new holder and deleting it would
            # admit a third concurrent writer.
            if not getattr(self, '_lost', False):
                try:
                    with open(self.path, 'r', encoding='ascii', errors='ignore') as f:
                        owner = f.read().strip()
                except OSError:
                    owner = None
                if owner is None or owner == str(os.getpid()):
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
        return False


def _lock():
    return _FileLock(LOCK_FILE)


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return []


# ----------------------------------------------------------------------------- country cache

def load_country_cache() -> dict:
    if COUNTRY_CACHE_FILE and os.path.exists(COUNTRY_CACHE_FILE):
        try:
            with open(COUNTRY_CACHE_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
                    return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def save_country_cache(cache):
    if not COUNTRY_CACHE_FILE or not cache:
        return
    try:
        os.makedirs(os.path.dirname(COUNTRY_CACHE_FILE), exist_ok=True)
        tmp = COUNTRY_CACHE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cache, f)
        os.replace(tmp, COUNTRY_CACHE_FILE)
    except Exception:
        pass


def extract_ip(purl):
    m = re.search(r'://(?:[^@/]*@)?([^:/]+)', purl or '')
    return m.group(1) if m else None


def resolve_proxy_countries(proxies, max_lookup=300):
    """purl -> 2-letter country. Offline DB first, then the local cache.

    No network calls: the old ip-api batching got rate-limited halfway through
    long scans and left country filters half-empty.
    """
    cache = load_country_cache()
    changed = False
    result = {}
    for p in proxies:
        ip = extract_ip(p)
        cc = cache.get(ip, '') if ip else ''
        if not cc and ip:
            info = geo.lookup(ip)
            cc = info.get('country', '')
            if cc:
                cache[ip] = cc
                changed = True
        result[p] = cc
    if changed:
        save_country_cache(cache)
    return result


# ----------------------------------------------------------------------------- parsing / sources

def read_local_sources():
    """User-added sources from the editable file (gateway UI)."""
    return pp.parse_source_lines(_read_text(LOCAL_SOURCES))


def _read_text(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except OSError:
        return ''


def write_local_sources(text):
    """Replace the editable sources file with `text` (proto url per line)."""
    os.makedirs(os.path.dirname(LOCAL_SOURCES), exist_ok=True)
    _atomic_write(LOCAL_SOURCES, text or '')
    return len(pp.parse_source_lines(text))


def read_sources():
    srcs = list(DEFAULT_SOURCES) + read_local_sources()
    try:
        if os.path.exists(KICKVIEW_SOURCES):
            srcs.extend(pp.parse_source_lines(_read_text(KICKVIEW_SOURCES)))
    except Exception as e:
        print(f"  (sources file unreadable: {e}; using built-ins)")
    seen, out = set(), []
    for s in srcs:
        if s[1] not in seen:
            seen.add(s[1])
            out.append(s)
    return out


def _source_stats_load() -> dict:
    try:
        data = json.loads(_read_text(SOURCE_STATS_FILE) or '{}')
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _source_stats_add(proto: str, url: str, found: int, error: str = '') -> None:
    with _lock():
        stats = _source_stats_load()
        row = stats.setdefault(url, {'proto': proto, 'runs': 0, 'found': 0, 'last_found': 0, 'errors': 0})
        row['runs'] = int(row.get('runs', 0)) + 1
        row['found'] = int(row.get('found', 0)) + int(found)
        row['last_run'] = int(time.time())
        row['last_found'] = int(found)
        row['ok'] = not error
        if error:
            row['errors'] = int(row.get('errors', 0)) + 1
            row['last_error'] = error[:200]
        try:
            _atomic_write(SOURCE_STATS_FILE, json.dumps(stats, indent=1))
        except OSError:
            pass


def source_stats() -> list[dict]:
    """Per-source yield, best first — shows which lists are worth keeping."""
    stats = _source_stats_load()
    rows = []
    for url, row in stats.items():
        if not isinstance(row, dict):
            continue
        runs = max(1, int(row.get('runs', 1)))
        rows.append({**row, 'url': url, 'avg': round(int(row.get('found', 0)) / runs, 1)})
    rows.sort(key=lambda r: (int(r.get('found', 0)), -int(r.get('errors', 0))), reverse=True)
    return rows


def fetch_source(proto, url):
    try:
        r = requests.get(url, timeout=30, verify=False, headers={'User-Agent': UA})
        parsed = pp.parse_proxy_list(r.text, proto)
        got = [pp.to_url(e['proto'], e['host'], e['port'], e['auth']) for e in parsed]
        if MAX_PER_SOURCE and len(got) > MAX_PER_SOURCE:
            print(f"  ! {proto} {url[:60]}… rejected: {len(got):,} over the per-source limit")
            _source_stats_add(proto, url, 0, 'too many proxies')
            return []
        _source_stats_add(proto, url, len(got))
        emit(t='source', proto=proto, url=url, found=len(got))
        return got
    except Exception as e:
        print(f"  ! {proto} {url[:60]}… {type(e).__name__}")
        _source_stats_add(proto, url, 0, f'{type(e).__name__}: {e}')
        emit(t='source', proto=proto, url=url, found=0, error=type(e).__name__)
        return []


def cmd_fetch():
    os.makedirs(os.path.dirname(RAW_FILE), exist_ok=True)
    all_lines = set()
    sources = read_sources()
    emit(t='start', action='fetch', total=len(sources))
    for proto, url in sources:
        got = fetch_source(proto, url)
        print(f"  + {len(got):6d}  {proto:6s} {url[:70]}")
        all_lines.update(got)
    with _lock():
        # union with anything imported while the scrape was running: an atomic
        # replace under the same lock keeps both writers from dropping each other
        all_lines.update(u for u in _read_lines(RAW_FILE) if '://' in u)
        _atomic_write(RAW_FILE, '\n'.join(sorted(all_lines)) + ('\n' if all_lines else ''))
    print(f"\nraw pool: {len(all_lines)} proxies -> {RAW_FILE}")
    emit(t='fetch_done', raw=len(all_lines))
    return len(all_lines)


# ----------------------------------------------------------------------------- failure classification

def _local_errno(e: BaseException) -> bool:
    seen = set()
    node = e
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, OSError) and getattr(node, 'errno', None) in LOCAL_LIMIT_ERRNOS:
            return True
        for arg in getattr(node, 'args', ()):
            if isinstance(arg, OSError) and getattr(arg, 'errno', None) in LOCAL_LIMIT_ERRNOS:
                return True
        node = node.__cause__ or node.__context__
    text = str(e).lower()
    return any(f"winerror {n}" in text or f"errno {n}" in text for n in LOCAL_LIMIT_ERRNOS)


def classify_error(e: BaseException) -> str:
    text = f"{type(e).__name__}: {e}".lower()
    if isinstance(e, (ssl.SSLError, requests.exceptions.SSLError)) or \
            'certificate verify' in text or '[ssl:' in text or 'certificate' in text:
        return 'tls'
    if _local_errno(e):
        return 'local_limit'
    if isinstance(e, (requests.exceptions.Timeout, TimeoutError, socket.timeout)):
        return 'timeout'
    if isinstance(e, requests.exceptions.ProxyError):
        # NB: the check URL is chat.z.ai/auth, so virtually every proxied
        # ProxyError text contains the substring "auth" (url: /auth) — a bare
        # 'auth' in text misclassified nearly all rejections as proxy_auth,
        # which is excluded from second-chance retries. Match explicit
        # auth-failure markers only.
        if '407' in text or 'authentication' in text or 'authenticationrequired' in text \
                or 'invalid credentials' in text or 'username/password' in text:
            return 'proxy_auth'
        if 'socks' in text or 'unexpected' in text:
            return 'wrong_protocol'
        return 'proxy_rejected'
    if isinstance(e, requests.exceptions.ConnectionError):
        if 'refused' in text:
            return 'refused'
        if 'reset' in text or 'aborted' in text or 'remote end closed' in text:
            return 'reset'
        if 'name or service' in text or 'getaddrinfo' in text:
            return 'other'
        return 'reset'
    if isinstance(e, OSError):
        return 'reset'
    return 'other'


def tcp_reason(e: BaseException) -> str:
    if _local_errno(e):
        return 'local_limit'
    if isinstance(e, (TimeoutError, socket.timeout)):
        return 'tcp_timeout'
    if isinstance(e, ConnectionRefusedError):
        return 'tcp_refused'
    return 'tcp_unreachable'


# ----------------------------------------------------------------------------- checking

try:
    import certifi
    _VERIFY = certifi.where()
except Exception as _cert_err:  # noqa: BLE001
    _VERIFY = True
    print(f"WARNING: certifi unavailable ({_cert_err}) — falling back to the system "
          f"trust store; MITM detection is weaker than usual.", flush=True)


def probe_detailed(purl, timeout=10):
    """(latency, 'ok') or (None, reason).

    Three things must hold or the run is wasted:
      - /auth returns real HTML containing the app shell (a hijack/block page does not)
      - the Aliyun captcha host is reachable
      - the certificate validates (certifi) → rejects MITM proxies
    """
    t0 = time.time()
    try:
        r = requests.get(CHECK_PAGE, proxies={'http': purl, 'https': purl},
                         timeout=timeout, verify=_VERIFY, headers={'User-Agent': UA},
                         allow_redirects=True)
    except Exception as e:
        return None, classify_error(e)
    if r.status_code != 200:
        return None, 'bad_status'
    ct = (r.headers.get('content-type') or '').lower()
    if 'html' not in ct or len(r.text) < 1500:
        return None, 'bad_response'
    marker = (PROBE_MARKER or '').lower()
    if marker and marker not in r.text.lower():
        return None, 'bad_response'
    lat = time.time() - t0                       # page latency only

    try:
        requests.get(CHECK_CAPTCHA, proxies={'http': purl, 'https': purl},
                     timeout=timeout, verify=_VERIFY, headers={'User-Agent': UA},
                     allow_redirects=True)
    except Exception as e:
        return None, classify_error(e)
    return lat, 'ok'


def probe(purl, timeout=10):
    """Backwards-compatible: latency if the proxy works, else None."""
    return probe_detailed(purl, timeout)[0]


def probe_endpoint(host, port, timeout=3.0):
    """Cheap TCP connect. (alive, reason) — reason is '' when alive."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ''
    except Exception as e:  # noqa: BLE001
        return False, tcp_reason(e)


# ----------------------------------------------------------------------------- dead cache

def _dead_load() -> dict:
    try:
        data = json.loads(_read_text(DEAD_FILE) or '{}')
        ep = data.get('ep') if isinstance(data.get('ep'), dict) else {}
        px = data.get('px') if isinstance(data.get('px'), dict) else {}
        return {'ep': ep, 'px': px}
    except ValueError:
        return {'ep': {}, 'px': {}}


def _dead_save(data: dict) -> None:
    cutoff = time.time() - 7 * 86400
    data['ep'] = {k: v for k, v in data['ep'].items() if isinstance(v, (int, float)) and v > cutoff}
    data['px'] = {k: v for k, v in data['px'].items() if isinstance(v, (int, float)) and v > cutoff}
    try:
        _atomic_write(DEAD_FILE, json.dumps(data))
    except OSError:
        pass


def _dead_hours() -> float:
    try:
        return _env_float('PROXY_DEAD_HOURS', 0)
    except Exception:
        return 0.0


def dead_set(hours=None) -> tuple[set, set]:
    """(dead endpoints, dead proxies) within the cache window."""
    hours = _dead_hours() if hours is None else hours
    if hours <= 0:
        return set(), set()
    data = _dead_load()
    cutoff = time.time() - hours * 3600
    return ({k for k, v in data['ep'].items() if v > cutoff},
            {k for k, v in data['px'].items() if v > cutoff})


def _dead_mark(kind: str, key: str) -> None:
    _dead_mark_many(kind, [key])


def _dead_mark_many(kind: str, keys) -> None:
    """Batch variant: one lock, one write for thousands of failures."""
    if _dead_hours() <= 0 or not keys:
        return
    with _lock():
        data = _dead_load()
        bucket = data['ep' if kind == 'ep' else 'px']
        now = time.time()
        for key in keys:
            bucket[key] = now
        _dead_save(data)


def clear_dead() -> int:
    with _lock():
        data = _dead_load()
        n = len(data['ep']) + len(data['px'])
        _dead_save({'ep': {}, 'px': {}})
    return n


# ----------------------------------------------------------------------------- pool persistence

def _new_entry(url: str, latency: float = 0.0, country: str = '') -> dict:
    s = pp.split_url(url)
    now = int(time.time())
    return {'url': url, 'public': pp.redact(url), 'proto': s['proto'], 'host': s['host'],
            'port': s['port'], 'auth': s['auth'], 'latency': round(float(latency or 0), 2),
            'latency_ms': int(float(latency or 0) * 1000), 'country': country or '',
            'fails': 0, 'checks': 0, 'last_ok': 0, 'last_check': 0, 'first_seen': now,
            'https': True}


def _ensure_seeded() -> None:
    """First run on an install that only has the old text files: import them."""
    if os.path.exists(POOL_FILE):
        return
    urls = []
    for path in (GOOD_FILE, EVERGOOD_FILE):
        urls.extend(_read_lines(path))
    if not urls:
        return
    pool = {}
    now = int(time.time())
    for url in urls:
        if '://' not in url:
            continue
        e = _new_entry(url)
        e['last_ok'] = now
        pool[url] = e
    try:
        _save_pool_map(pool)
    except OSError:
        pass


def _load_pool_map() -> dict:
    _ensure_seeded()
    try:
        data = json.loads(_read_text(POOL_FILE) or '{}')
        if isinstance(data, dict):
            return {url: e for url, e in data.items() if isinstance(e, dict) and 'url' in e}
        if isinstance(data, list):        # tolerate a list-shaped pool
            return {e['url']: e for e in data if isinstance(e, dict) and 'url' in e}
    except (ValueError, KeyError):
        pass
    # corrupt/unparsable pool.json: fall back to the .bak instead of reading
    # as permanently empty (_ensure_seeded skips re-seeding whenever
    # pool.json EXISTS, so an empty parse used to strand the whole ledger)
    try:
        bak = json.loads(_read_text(POOL_FILE + '.bak') or '{}')
        if isinstance(bak, dict) and bak:
            print('[proxy_pool] WARNING: pool.json corrupt — recovered from '
                  'pool.json.bak', flush=True)
            return {url: e for url, e in bak.items() if isinstance(e, dict) and 'url' in e}
    except (ValueError, KeyError, OSError):
        pass
    return {}


def load_pool() -> dict:
    return _load_pool_map()


def _bad_set() -> set:
    out = set()
    now = time.time()
    for ln in _read_lines(BAD_FILE):
        parts = ln.split('\t')
        url = parts[0].strip()
        if not url:
            continue
        ts = None
        for p in parts[1:]:
            try:
                ts = float(p)
                break
            except ValueError:
                continue
        if ts is not None and BAD_TTL_DAYS and now - ts > BAD_TTL_DAYS * 86400:
            continue
        out.add(url)
    return out


def _used_set() -> set:
    return {ln.split('\t')[0].strip() for ln in _read_lines(USED_FILE) if ln.strip()}


def load_bad() -> set:
    return _bad_set()


def load_used() -> set:
    return _used_set()


def _is_usable(entry: dict, bad: set, used: set, max_fails: int) -> bool:
    """Usable = verified by a real check AND the latest check was a pass.

    Imports are 'vouched' (pending), never usable until `record_successes`
    promotes them. A proxy whose most recent check failed drops out until the
    next pass, so the pool count matches what the last scan actually proved.
    """
    url = entry.get('url')
    last_ok = int(entry.get('last_ok', 0) or 0)
    last_check = int(entry.get('last_check', 0) or 0)
    # second-resolution timestamps can tie; an explicit result is authoritative
    result = entry.get('last_result')
    if result is None:
        result = 'ok' if last_check <= last_ok else 'fail'
    return (url not in bad and url not in used
            and last_ok > 0 and result == 'ok'
            and int(entry.get('fails', 0)) <= max_fails)


def _write_exports(pool: dict) -> None:
    """Rebuild the text ledgers from pool.json — one source of truth."""
    bad, used = _bad_set(), _used_set()
    rows = [e for e in pool.values() if _is_usable(e, bad, used, MAX_FAILS)]
    rows.sort(key=lambda e: (int(e.get('fails', 0)), float(e.get('latency', 0))))
    _atomic_write(GOOD_FILE, '\n'.join(e['url'] for e in rows) + ('\n' if rows else ''))
    ever = [e for e in pool.values() if int(e.get('last_ok', 0)) > 0 and
            e.get('url') not in bad and e.get('url') not in used]
    ever.sort(key=lambda e: (int(e.get('fails', 0)), float(e.get('latency', 0))))
    _atomic_write(EVERGOOD_FILE, '\n'.join(e['url'] for e in ever) + ('\n' if ever else ''))


def _save_pool_map(pool: dict) -> None:
    # never leak credentials into the JSON in a readable dashboard field, but
    # keep the full URL so the proxy can actually be used
    try:
        payload = json.dumps(pool, indent=0)
        # keep the last good copy beside the live file: a crash/corruption
        # event then costs at most one save, not the whole 1 MB ledger
        try:
            if os.path.exists(POOL_FILE):
                os.replace(POOL_FILE, POOL_FILE + '.bak')
        except OSError:
            pass
        _atomic_write(POOL_FILE, payload)
        _write_exports(pool)
    except OSError:
        pass


def record_successes(items) -> int:
    """items: [(url, latency, country?)] — add/refresh, one lock, one write."""
    clean = []
    for item in items:
        url, latency = item[0], item[1] if len(item) > 1 else 0.0
        country = item[2] if len(item) > 2 else ''
        if url and '://' in url:
            clean.append((url, latency, country or ''))
    items = clean
    if not items:
        return 0
    # country lookups (and the one-time geo DB parse) happen outside the lock so
    # other workers' claims are not held up behind a disk-backed lookup
    cache = load_country_cache()
    changed_cache = False
    resolved = []
    for url, latency, country in items:
        if not country:
            ip = extract_ip(url)
            country = cache.get(ip, '') if ip else ''
            if not country and ip:
                country = geo.lookup(ip).get('country', '')
                if country:
                    cache[ip] = country
                    changed_cache = True
        resolved.append((url, latency, country))
    with _lock():
        pool = _load_pool_map()
        now = int(time.time())
        for url, latency, country in resolved:
            e = pool.get(url) or _new_entry(url)
            e.update({'url': url, 'public': pp.redact(url), 'latency': round(float(latency or 0), 2),
                      'latency_ms': int(float(latency or 0) * 1000),
                      'fails': max(0, int(e.get('fails', 0)) - 1), 'checks': int(e.get('checks', 0)) + 1,
                      'last_ok': now, 'last_check': now, 'last_result': 'ok'})
            if country:
                e['country'] = country.upper()
            s = pp.split_url(url)
            e.update({'proto': s['proto'], 'host': s['host'], 'port': s['port'], 'auth': s['auth']})
            e.pop('vouched', None)
            pool[url] = e
        _save_pool_map(pool)
        # a success proves the host and exit work again: don't let an old dead
        # mark keep it out of the next scan for the whole cache window
        dead = _dead_load()
        cleared = False
        for url, _, _ in resolved:
            if dead['px'].pop(url, None) is not None:
                cleared = True
            s = pp.split_url(url)
            if dead['ep'].pop(f"{s['host']}:{s['port']}", None) is not None:
                cleared = True
        if cleared:
            _dead_save(dead)
    if changed_cache:
        save_country_cache(cache)
    return len(items)


def record_imports(urls) -> int:
    """User-vouched imports: usable now, but NOT a verified success.

    They are deliberately not written to the ever-good export and keep
    ``last_ok == 0``; the first real probe decides their score.
    """
    clean = [u for u in dict.fromkeys(urls) if u and '://' in u]
    if not clean:
        return 0
    with _lock():
        pool = _load_pool_map()
        now = int(time.time())
        for url in clean:
            e = pool.get(url) or _new_entry(url)
            s = pp.split_url(url)
            e.update({'url': url, 'public': pp.redact(url), 'proto': s['proto'],
                      'host': s['host'], 'port': s['port'], 'auth': s['auth'],
                      'fails': 0, 'vouched': True})
            e.setdefault('first_seen', now)
            pool[url] = e
        _save_pool_map(pool)
    return len(clean)


def record_failures(items) -> int:
    """items: [(url, reason)] — penalise known pool members only."""
    items = [(u, r) for (u, r) in items if u and '://' in u]
    if not items:
        return 0
    with _lock():
        pool = _load_pool_map()
        now = int(time.time())
        touched = 0
        for url, reason in items:
            e = pool.get(url)
            if not e:
                continue
            e['fails'] = int(e.get('fails', 0)) + 1
            e['checks'] = int(e.get('checks', 0)) + 1
            e['last_check'] = now
            e['last_result'] = 'fail'
            e['last_reason'] = reason
            touched += 1
        if touched:
            _save_pool_map(pool)
    return touched


def add_success(url, latency=0.0, country=''):
    return record_successes([(url, latency, country)])


def add_failure(url, reason='', max_fails=None):
    return bool(record_failures([(url, reason or 'other')]))


def pool_report(url, ok, reason=''):
    """A script using the pool reports whether a proxy worked."""
    if ok:
        add_success(url)
    else:
        add_failure(url, reason or 'other')
    return True


def prune(days: float = 14.0) -> dict:
    """Drop entries that have been unhealthy for a while; prune the dead cache."""
    cutoff = time.time() - days * 86400
    removed = 0
    with _lock():
        pool = _load_pool_map()
        keep = {}
        for url, e in pool.items():
            unhealthy = int(e.get('fails', 0)) > MAX_FAILS
            stale = float(e.get('last_ok', 0)) < cutoff
            if unhealthy and stale:
                removed += 1
                continue
            keep[url] = e
        _save_pool_map(keep)
        data = _dead_load()
        _dead_save(data)
    return {'removed': removed, 'pool': len(keep)}


def _bad_urls_raw() -> set:
    """Every URL ever blacklisted, including rows past the TTL filter."""
    return {ln.split('\t')[0].strip() for ln in _read_lines(BAD_FILE) if ln.strip()}


def clear_bad() -> int:
    with _lock():
        # deliberately raw: an expired row still needs its score repaired when
        # the user asks to forget the blacklist
        urls = _bad_urls_raw()
        n = len(_read_lines(BAD_FILE))
        _atomic_write(BAD_FILE, '')
        pool = _load_pool_map()
        # clearing the blacklist means "try these again", so forgive their score
        for url in urls:
            if url in pool:
                pool[url]['fails'] = 0
        _save_pool_map(pool)
    return n


def clear_used() -> int:
    with _lock():
        n = len(_read_lines(USED_FILE))
        _atomic_write(USED_FILE, '')
        pool = _load_pool_map()
        _write_exports(pool)
    return n


def load_good() -> list[str]:
    """Usable proxies, best first (excludes bad, used and over-failed entries)."""
    pool = _load_pool_map()
    bad, used = _bad_set(), _used_set()
    rows = [e for e in pool.values() if _is_usable(e, bad, used, MAX_FAILS)]
    rows.sort(key=lambda e: (int(e.get('fails', 0)), float(e.get('latency', 0))))
    return [e['url'] for e in rows]


def filter_types(proxies, types):
    if not types:
        return list(proxies)
    allowed = {str(t).lower() for t in types}
    return [p for p in proxies if p.split('://', 1)[0].lower() in allowed]


def socks_only(proxies):
    return filter_types(proxies, ('socks4', 'socks5'))


def pool_stats() -> dict:
    pool = _load_pool_map()
    bad, used = _bad_set(), _used_set()
    by_proto = Counter()
    healthy = 0
    total_latency = 0.0
    usable = 0
    for e in pool.values():
        by_proto[e.get('proto', 'http')] += 1
        if int(e.get('fails', 0)) == 0:
            healthy += 1
        total_latency += float(e.get('latency', 0))
        if _is_usable(e, bad, used, MAX_FAILS):
            usable += 1
    return {'count': len(pool), 'usable': usable, 'healthy': healthy,
            'unverified': sum(1 for e in pool.values()
                              if e.get('vouched') and not int(e.get('last_ok', 0))),
            'by_proto': dict(by_proto), 'avg_latency': round(total_latency / len(pool), 2) if pool else 0,
            'failed': sum(1 for e in pool.values() if int(e.get('fails', 0)) > 0),
            'bad': len(bad), 'used': len(used),
            'geo': geo.status(), 'max_fails': MAX_FAILS}


# ----------------------------------------------------------------------------- pool ledger writes

def mark_bad(purl, why=''):
    """Blacklist a proxy (MITM, broken route) and drop it from the usable set."""
    if not purl:
        return
    with _lock():
        with open(BAD_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{purl}\t{int(time.time())}\t{(why or '')}\n")
        # the blacklist itself excludes it; do not poison the health score, so an
        # entry that expires (30-day TTL) is usable again without extra passes
        _write_exports(_load_pool_map())
    emit(t='bad', url=pp.redact(purl), why=why)


def mark_used(purl, details=''):
    """Record a proxy as used so it is never picked again for signups."""
    if not purl:
        return
    with _lock():
        with open(USED_FILE, 'a', encoding='utf-8') as f:
            ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            f.write(f"{purl}\t{ts}\t{details}\n")
        pool = _load_pool_map()
        _write_exports(pool)


def claim_used(purl, details=''):
    """Atomically claim a proxy so concurrent workers never get the same one.

    The lock file makes this hold across processes too — eight generator workers
    are eight processes, and the old thread lock could hand the same exit IP to two
    of them.
    """
    if not purl:
        return False
    with _lock():
        if os.path.exists(USED_FILE):
            for ln in _read_lines(USED_FILE):
                if ln.split('\t')[0].strip() == purl:
                    return False
        with open(USED_FILE, 'a', encoding='utf-8') as f:
            ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            f.write(f"{purl}\t{ts}\t{details}\n")
        pool = _load_pool_map()
        _write_exports(pool)
    return True


# ----------------------------------------------------------------------------- import / pick

def import_proxies(text, into_good=True, default_proto='http'):
    """Add pasted/uploaded proxies to the pool.

    Accepts everything `proxy_parse` understands: schemeful, bare, user:pass@,
    provider style host:port:user:pass, CIDR ranges, HTML and JSON.
    ``into_good`` queues them as vouched/pending: they only become usable after
    a real check passes (Verify raw / Recheck pool).
    """
    entries = [pp.to_url(e['proto'], e['host'], e['port'], e['auth'])
               for e in pp.parse_proxy_list(text, default_proto)]
    if not entries:
        return 0
    os.makedirs(os.path.dirname(RAW_FILE), exist_ok=True)
    with _lock():
        existing = set(_read_lines(RAW_FILE))
        fresh_raw = [e for e in dict.fromkeys(entries) if e not in existing]
        if fresh_raw:
            with open(RAW_FILE, 'a', encoding='utf-8') as f:
                f.write('\n'.join(fresh_raw) + '\n')
    if into_good:
        record_imports(entries)
    emit(t='import', count=len(entries), into_good=bool(into_good))
    return len(entries)


def _country_of(entry: dict, cache: dict) -> str:
    cc = (entry.get('country') or '').upper()
    if cc:
        return cc
    ip = entry.get('host') or extract_ip(entry.get('url', ''))
    cc = cache.get(ip, '') if ip else ''
    if not cc and ip:
        cc = geo.lookup(ip).get('country', '')
    return cc.upper()


def pick_verified(tries=30, timeout=8, socks=False, country=None, types=None):
    """Pick a FAST, live, UNUSED proxy and atomically claim it.

    Candidates come from the pooled + historically-good set, healthiest first;
    each is re-verified before it is handed out.
    """
    bad, used = _bad_set(), _used_set()
    pool = _load_pool_map()
    good = [e for e in pool.values() if _is_usable(e, bad, used, MAX_FAILS)]
    urls = [e['url'] for e in good]
    if socks:
        urls = socks_only(urls)
    if types:
        urls = filter_types(urls, types)
    target_country = (country or os.environ.get('ZAI_PROXY_COUNTRY', '')).strip().upper()
    if target_country and target_country != 'ALL':
        cache = load_country_cache()
        keep = []
        by_url = {e['url']: e for e in good}
        for u in urls:
            if _country_of(by_url.get(u, {}), cache) == target_country:
                keep.append(u)
        urls = keep
    if not urls:
        return None
    ranked = {e['url']: e for e in good}
    urls.sort(key=lambda u: (int(ranked.get(u, {}).get('fails', 0)),
                             float(ranked.get(u, {}).get('latency', 0))))
    head = urls[:min(120, len(urls))]
    if len(head) > 10:               # large pool: spread load between workers
        random.shuffle(head)
    candidates = head[:min(len(head), max(int(tries), 25))]

    failures: list[tuple[str, str]] = []
    max_lat = _env_float('PROXY_MAX_LATENCY', MAX_LATENCY)
    ex = ThreadPoolExecutor(max_workers=min(10, max(1, len(candidates))))
    try:
        futs = {ex.submit(probe_detailed, p, timeout): p for p in candidates}
        for fut in as_completed(futs):
            purl = futs[fut]
            try:
                lat, reason = fut.result()
            except Exception as e:  # noqa: BLE001
                lat, reason = None, classify_error(e)
            if lat is not None and lat <= max_lat:
                if claim_used(purl, f"lat={lat:.2f}s"):
                    record_successes([(purl, lat, '')])
                    for f in futs:
                        f.cancel()
                    return purl
            elif lat is not None:
                failures.append((purl, 'timeout'))     # alive but too slow to use
            else:
                failures.append((purl, reason))
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if failures:
        record_failures([f for f in failures if f[1] != 'local_limit'])
    return None


# ----------------------------------------------------------------------------- verify pipeline

def _check_one(purl, timeout):
    return purl, probe_detailed(purl, timeout)


def _run_probes(jobs, timeout, concurrency, results, failures, reasons, progress=None,
                counter=None):
    """Probe `jobs` with a fixed worker pool; callbacks stay on this thread."""
    if not jobs:
        return
    done = counter or {'n': 0}
    workers = max(1, int(concurrency))
    # bounded submission: 200k jobs at once would queue 200k futures in memory
    chunk = max(workers * 4, 1000)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for start in range(0, len(jobs), chunk):
            batch = jobs[start:start + chunk]
            futs = {ex.submit(_check_one, p, timeout): p for p in batch}
            for fut in as_completed(futs):
                purl = futs[fut]
                try:
                    _, (lat, reason) = fut.result()
                except Exception as e:  # noqa: BLE001
                    lat, reason = None, classify_error(e)
                if lat is not None:
                    results.append((lat, purl))
                else:
                    failures.append((purl, reason))
                    reasons[reason] += 1
                done['n'] += 1
                if progress:
                    progress(done['n'], lat is not None)
    # dead-cache writes are batched: one lock, one file write per pass
    _dead_mark_many('px', [u for u, r in failures if r != 'local_limit'])


def _print_checkpoint(probed, total, alive_this_run, saved_now=0, reasons=None, quiet=False):
    if quiet:
        return
    extra = f" — saved {saved_now} verified" if saved_now else ""
    print(f"  [checkpoint] {probed}/{total} probed, {alive_this_run} alive{extra}", flush=True)
    if reasons:
        emit(t='reasons', probed=probed, total=total, alive=alive_this_run,
             reasons={k: v for k, v in reasons.items() if v},
             labels={k: FAIL_LABELS.get(k, k) for k in reasons})


def _record_new(results, already_saved: set, max_lat=None) -> int:
    """Persist fresh successes; proxies over the latency cap are not pooled."""
    fresh = [(u, l, '') for l, u in results
             if u not in already_saved and (max_lat is None or l <= max_lat)]
    if not fresh:
        return 0
    record_successes(fresh)
    already_saved.update(url for url, *_ in fresh)
    return len(fresh)


def _verify_list(proxies, opts, label, quiet=False, limit=None):
    """Shared body of verify/recheck. Returns (alive, reasons, failures)."""
    if limit:
        proxies = proxies[:limit]
    concurrency = int(opts['concurrency'])
    timeout = float(opts['timeout'])
    max_lat = float(opts['max_latency'])

    reasons = Counter()
    failures: list[tuple[str, str]] = []
    results: list[tuple[float, str]] = []
    saved: set[str] = set()

    # skip proxies that recently failed, when the dead cache is enabled
    _, dead_px = dead_set(opts.get('dead_cache_hours'))
    if dead_px:
        before = len(proxies)
        proxies = [p for p in proxies if p not in dead_px]
        skipped = before - len(proxies)
        if skipped:
            print(f"  dead cache: skipped {skipped} recently failed proxies")
            emit(t='dead_cache', skipped=skipped)

    print(f"{label} {len(proxies)} proxies against chat.z.ai "
          f"(concurrency={concurrency}, timeout={timeout:g}s)…")
    emit(t='start', action='verify', total=len(proxies), timeout=timeout,
         concurrency=concurrency, prefilter=bool(opts['prefilter']))

    # 1. TCP prefilter: never spend an HTTPS request on a closed port
    if opts['prefilter'] and proxies:
        endpoints = set()
        for p in proxies:
            s = pp.split_url(p)
            if s['port']:
                endpoints.add((s['host'], s['port']))
        endpoints = sorted(endpoints)
        alive_eps: set = set()
        ep_reason: dict = {}
        dead_eps, _ = dead_set(opts.get('dead_cache_hours'))
        ptimeout = float(opts['probe_timeout'])
        pconc = int(opts['probe_concurrency'])

        def _probe_ep(ep):
            if f"{ep[0]}:{ep[1]}" in dead_eps:
                return ep, False, 'tcp_timeout'
            ok, why = probe_endpoint(ep[0], ep[1], ptimeout)
            return ep, ok, why

        # Progress matters here: with a 96k-raw pool the prefilter runs 15+
        # minutes, and the old ex.map loop emitted NOTHING until every
        # endpoint was done — the dashboard counter sat at 0/96614 looking
        # frozen (users stopped a healthy scan).
        with ThreadPoolExecutor(max_workers=max(1, pconc)) as ex:
            futures = {ex.submit(_probe_ep, ep): ep for ep in endpoints}
            done_ct = 0
            last_emit = time.time()
            emit_every = max(2.0, min(30.0, len(endpoints) / max(pconc, 1)))
            for fut in as_completed(futures):
                ep, ok, why = fut.result()
                done_ct += 1
                if ok:
                    alive_eps.add(ep)
                else:
                    ep_reason[ep] = why
                now = time.time()
                if now - last_emit >= emit_every or done_ct == len(endpoints):
                    last_emit = now
                    emit(t='prefilter_progress', done=done_ct,
                         total=len(endpoints), open=len(alive_eps))
        _dead_mark_many('ep', [f"{ep[0]}:{ep[1]}" for ep in ep_reason])
        filtered = []
        for p in proxies:
            s = pp.split_url(p)
            ep = (s['host'], s['port'])
            if s['port'] and ep not in alive_eps:
                reasons[ep_reason.get(ep, 'tcp_unreachable')] += 1
                continue
            filtered.append(p)
        print(f"  tcp prefilter: {len(alive_eps)}/{len(endpoints)} endpoints open -> "
              f"{len(filtered)} checks")
        emit(t='prefilter', endpoints=len(endpoints), open=len(alive_eps), remaining=len(filtered))
        proxies = filtered

    # 2. main pass
    step = max(25, min(200, max(1, len(proxies)) // 10))

    def progress(done, ok):
        if done % step == 0:
            n = _record_new(results, saved, max_lat)
            _print_checkpoint(done, len(proxies), len(results), n, reasons, quiet)
            # structured progress for the dashboard counter: the `reasons`
            # event only fired at checkpoints on some paths and never during
            # long stretches, leaving 0/96614 on screen for minutes
            emit(t='reasons', probed=done, total=len(proxies),
                 alive=len(results))
        elif done == len(proxies):
            _print_checkpoint(done, len(proxies), len(results), 0, reasons, quiet)
            emit(t='reasons', probed=done, total=len(proxies),
                 alive=len(results))

    _run_probes(proxies, timeout, concurrency, results, failures, reasons, progress)

    # 3. second chance: flaky free proxies are slow, not dead
    if opts['retry_failed'] and failures:
        retry_urls = [u for u, r in failures if r in RETRY_REASONS]
        retry_urls = retry_urls[:int(opts.get('retry_max') or 5000)]
        if retry_urls:
            retry_set = set(retry_urls)
            failures = [f for f in failures if f[0] not in retry_set]
            reasons = Counter()
            for _, r in failures:
                reasons[r] += 1
            print(f"  second chance: {len(retry_urls)} retryable failures")
            emit(t='second_chance', count=len(retry_urls))
            before = len(results)
            _run_probes(retry_urls, timeout + 2, min(concurrency, 200), results, failures, reasons)
            gained = len(results) - before
            print(f"  second chance recovered {gained}")

    # trim by latency, persist everything
    alive = [(lat, u) for lat, u in results if lat <= max_lat]
    alive.sort()
    _record_new(alive, saved, max_lat)
    slow = len(results) - len(alive)

    usable = len(load_good())
    print(f"\nworking proxies: {len(alive)} / {len(proxies)} (<= {max_lat:g}s"
          f"; dropped {slow} too slow)  -> {GOOD_FILE}")
    if alive:
        best, median = alive[0][0], alive[len(alive) // 2][0]
        print(f"latency: best {best:.2f}s  median {median:.2f}s")
        emit(t='summary', total=len(proxies), alive=len(alive), slow=slow,
             best=round(best, 2), median=round(median, 2), pool=usable,
             reasons=dict(reasons), by_reason_labels={k: FAIL_LABELS.get(k, k) for k in reasons})
    else:
        print("latency: —")
        emit(t='summary', total=len(proxies), alive=0, pool=usable, reasons=dict(reasons))
    if reasons:
        top = ', '.join(f"{k}={v}" for k, v in reasons.most_common(6) if v)
        if top:
            print(f"why they failed: {top}")
    return alive, reasons, failures


def _history_add(entry: dict) -> None:
    try:
        data = json.loads(_read_text(HISTORY_FILE) or '[]')
        rows = data if isinstance(data, list) else []
    except ValueError:
        rows = []
    rows.insert(0, entry)
    try:
        _atomic_write(HISTORY_FILE, json.dumps(rows[:100], indent=1))
    except OSError:
        pass


def cmd_verify(timeout=None, concurrency=None, limit=None, preset=None, quiet=False, **kw):
    if not os.path.exists(RAW_FILE):
        print("no raw pool — run `python proxy_pool.py fetch` first")
        return 0
    opts = resolve_scan_options(preset=preset, timeout=timeout, concurrency=concurrency, **kw)
    proxies = [p for p in _read_lines(RAW_FILE) if '://' in p]
    bad = _bad_set()
    proxies = [p for p in proxies if p not in bad]
    if limit:
        proxies = proxies[:limit]
    if not proxies:
        print("raw pool is empty")
        return 0
    t0 = time.time()
    alive, reasons, failures = _verify_list(proxies, opts, 'verifying', quiet=quiet)
    ever = len([e for e in _load_pool_map().values() if int(e.get('last_ok', 0)) > 0])
    print(f"ever-good (cumulative, survives restarts): {ever}  -> {EVERGOOD_FILE}")
    emit(t='done', action='verify', alive=len(alive), total=len(proxies),
         elapsed=round(time.time() - t0, 1), ever_good=ever)
    _history_add({'ts': int(time.time()), 'action': 'verify', 'total': len(proxies),
                  'alive': len(alive), 'elapsed': round(time.time() - t0, 1),
                  'reasons': dict(reasons)})
    return len(alive)


def cmd_recheck(timeout=None, concurrency=None, preset=None, quiet=False, **kw):
    """Strictly re-test the CURRENT pool (cert-valid, fast) and rescore it."""
    opts = resolve_scan_options(preset=preset, timeout=timeout, concurrency=concurrency, **kw)
    proxies = load_good()
    if not proxies:
        print("no pool — run fetch/verify first")
        return 0
    print(f"strict recheck of {len(proxies)} pooled proxies "
          f"(valid TLS + <= {opts['max_latency']:g}s, concurrency={int(opts['concurrency'])})…")
    # recheck tests existing pool members, so a failure is real scoring signal
    alive, reasons, failures = _verify_list(proxies, opts, 'rechecking', quiet=quiet)
    # anything that failed this pass is penalised once (a pass already de-credited it)
    record_failures([(u, r) for u, r in failures if r != 'local_limit'])
    print(f"\nworking proxies: {len(alive)} / {len(proxies)}  -> {GOOD_FILE}")
    emit(t='done', action='recheck', alive=len(alive), total=len(proxies))
    _history_add({'ts': int(time.time()), 'action': 'recheck', 'total': len(proxies),
                  'alive': len(alive), 'reasons': dict(reasons)})
    return len(alive)


def cmd_pick():
    p = pick_verified()
    if p:
        print(p)
        return p
    print("no live proxy found (pool empty or all probed were dead)")
    return None


def cmd_geo():
    print("downloading DB-IP Lite (country + ASN)…")
    emit(t='start', action='geo')
    res = geo.download(progress=lambda m: print("  " + m), asn=True)
    st = res.get('status') or {}
    if res.get('ok'):
        print(f"geo database ready: {st.get('size_mb', '?')} MB, updated {st.get('updated', '?')}")
    else:
        print(f"geo download failed: {res.get('country', {}).get('error', 'unknown error')}")
    emit(t='geo', ok=bool(res.get('ok')), **{k: v for k, v in st.items() if k in ('available', 'updated', 'size_mb')})
    return 1 if res.get('ok') else 0


def cmd_stats():
    st = pool_stats()
    print(f"pool: {st['count']} known, {st['usable']} usable, {st['healthy']} healthy, "
          f"{st['failed']} with failures")
    print(f"ledgers: bad={st['bad']} used={st['used']}  avg latency={st['avg_latency']}s")
    if st['by_proto']:
        print("types: " + ", ".join(f"{k}={v}" for k, v in sorted(st['by_proto'].items())))
    g = st.get('geo') or {}
    print(f"geo: {'ready (' + str(g.get('updated')) + ')' if g.get('available') else 'not downloaded'}")
    emit(t='stats', **st)
    return st


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else 'all'
    lim = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else None
    if cmd in ('fetch', 'all'):
        cmd_fetch()
    if cmd in ('verify', 'all'):
        cmd_verify(limit=lim)
    if cmd == 'recheck':
        cmd_recheck()
    if cmd == 'pick':
        cmd_pick()
    if cmd == 'geo':
        cmd_geo()
    if cmd == 'stats':
        cmd_stats()
    if cmd == 'prune':
        print(prune())
    if cmd == 'clear-bad':
        print(f"cleared {clear_bad()} blacklisted proxies")
    if cmd == 'clear-used':
        print(f"cleared {clear_used()} used proxies")
    if cmd == 'clear-dead':
        print(f"cleared {clear_dead()} dead-cache entries")


if __name__ == '__main__':
    main()
