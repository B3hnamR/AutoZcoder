"""Per-launch browser fingerprint randomization.

Every signup used to present the IDENTICAL browser fingerprint (frozen
Chrome/120 UA, fixed viewport, fixed timezone/locale regardless of exit-IP
country, identical canvas/WebGL hashes, per-worker reused profile). Fraud
systems cluster accounts on exactly those constants. This module gives each
launch its own coherent identity:

  - recent Chrome UA (130-143) matching the actual bundled chromium major
  - viewport/hardware/memory jitter within plausible laptop ranges
  - timezone + locale matched to the exit IP's country (we already resolve
    proxy countries for the pool)
  - canvas/WebGL/AudioContext noise seeded per profile, so hashes differ
    per account but stay STABLE within one account's session (a device that
    re-draws differently every frame is a detection signal in itself)

The identity is drawn once per launch and consumed by main.py /
invite_claim.py.
"""
import json
import random
import urllib.request

# recent desktop Chrome majors (UA string claims these; keep close to the
# bundled chromium build so JS-visible quirks match the UA claim)
CHROME_MAJORS = [131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143]

VIEWPORTS = [
    (1280, 720), (1366, 768), (1440, 900), (1536, 864),
    (1600, 900), (1707, 1067), (1920, 1080),
]

# locale + timezone candidates per country code (exit-IP matched)
LOCALE_TZ = {
    'US': ('en-US', ['America/New_York', 'America/Chicago', 'America/Los_Angeles', 'America/Denver']),
    'CA': ('en-CA', ['America/Toronto', 'America/Vancouver', 'America/Edmonton']),
    'GB': ('en-GB', ['Europe/London']),
    'DE': ('de-DE', ['Europe/Berlin']),
    'FR': ('fr-FR', ['Europe/Paris']),
    'NL': ('nl-NL', ['Europe/Amsterdam']),
    'ES': ('es-ES', ['Europe/Madrid']),
    'IT': ('it-IT', ['Europe/Rome']),
    'PL': ('pl-PL', ['Europe/Warsaw']),
    'SE': ('sv-SE', ['Europe/Stockholm']),
    'RU': ('ru-RU', ['Europe/Moscow']),
    'UA': ('uk-UA', ['Europe/Kyiv']),
    'TR': ('tr-TR', ['Europe/Istanbul']),
    'BR': ('pt-BR', ['America/Sao_Paulo']),
    'AR': ('es-AR', ['America/Argentina/Buenos_Aires']),
    'CL': ('es-CL', ['America/Santiago']),
    'MX': ('es-MX', ['America/Mexico_City']),
    'IN': ('en-IN', ['Asia/Kolkata']),
    'VN': ('vi-VN', ['Asia/Ho_Chi_Minh']),
    'TH': ('th-TH', ['Asia/Bangkok']),
    'ID': ('id-ID', ['Asia/Jakarta']),
    'PH': ('en-PH', ['Asia/Manila']),
    'MY': ('en-MY', ['Asia/Kuala_Lumpur']),
    'SG': ('en-SG', ['Asia/Singapore']),
    'JP': ('ja-JP', ['Asia/Tokyo']),
    'KR': ('ko-KR', ['Asia/Seoul']),
    'CN': ('zh-CN', ['Asia/Shanghai']),
    'HK': ('zh-HK', ['Asia/Hong_Kong']),
    'TW': ('zh-TW', ['Asia/Taipei']),
    'AU': ('en-AU', ['Australia/Sydney', 'Australia/Melbourne']),
    'NZ': ('en-NZ', ['Pacific/Auckland']),
    'ZA': ('en-ZA', ['Africa/Johannesburg']),
    'AE': ('en-AE', ['Asia/Dubai']),
    'SA': ('en-SA', ['Asia/Riyadh']),
    'IL': ('he-IL', ['Asia/Jerusalem']),
    'EG': ('en-EG', ['Africa/Cairo']),
    'NG': ('en-NG', ['Africa/Lagos']),
    'KE': ('en-KE', ['Africa/Nairobi']),
    'BD': ('en-BD', ['Asia/Dhaka']),
    'PK': ('en-PK', ['Asia/Karachi']),
}

DEFAULT_LOCALE_TZ = ('en-US', ['America/New_York', 'America/Chicago', 'America/Los_Angeles'])

_COUNTRY_CACHE = {}


def _proxy_host(proxy_url: str) -> str:
    """host:port out of any scheme://[user:pass@]host:port URL."""
    try:
        from urllib.parse import urlparse
        return urlparse(proxy_url or "").hostname or ""
    except ValueError:
        return ""


def exit_ip_country(proxy_url=None, timeout=6):
    """2-letter country of the exit IP (through the proxy when given).

    Order: live ip-api lookup through the proxy when its scheme allows it
    (http/https), then an online lookup of the proxy's own host IP
    (proxy_geo). The second path is what makes SOCKS work at all: a socks5://
    URL fed to urllib's HTTP ProxyHandler is a plain HTTP request to the SOCKS
    port — it always failed and every SOCKS-sourced account fell back to
    US/New_York, exactly the mismatch fraud systems look for."""
    key = proxy_url or '__direct__'
    if key in _COUNTRY_CACHE:
        return _COUNTRY_CACHE[key]
    cc = None
    socks = bool(proxy_url) and proxy_url != '0' and \
        str(proxy_url).lower().startswith(('socks4://', 'socks5://', 'socks://'))
    if not socks:
        try:
            proxies = ({'http': proxy_url, 'https': proxy_url}
                       if proxy_url and proxy_url != '0' else None)
            req = urllib.request.Request(
                'http://ip-api.com/json/?fields=countryCode', headers={'User-Agent': 'Mozilla/5.0'})
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler(proxies) if proxies else urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=timeout) as resp:
                cc = (json.loads(resp.read().decode()).get('countryCode') or None)
        except Exception:
            cc = None
    if cc is None and proxy_url and proxy_url != '0':
        host = _proxy_host(str(proxy_url))
        if host:
            try:
                import proxy_geo
                cc = proxy_geo.country(host) or None
            except Exception:
                cc = None
    _COUNTRY_CACHE[key] = cc
    return cc


def random_user_agent(rng=None):
    rng = rng or random
    major = rng.choice(CHROME_MAJORS)
    build = rng.randint(6300, 6999)
    patch = rng.randint(20, 210)
    return (f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            f'(KHTML, like Gecko) Chrome/{major}.0.{build}.{patch} Safari/537.36')


def make_identity(proxy_url=None, seed=None):
    """Draw one coherent per-launch identity. Returns dict consumed by the
    browser launch kwargs and the anti-detection script."""
    rng = random.Random(seed) if seed is not None else random.Random()

    cc = exit_ip_country(proxy_url)
    locale, tzs = LOCALE_TZ.get(cc, DEFAULT_LOCALE_TZ)
    tz = rng.choice(tzs)

    w, h = rng.choice(VIEWPORTS)
    cores = rng.choice([4, 6, 8, 8, 12, 16])
    mem = rng.choice([4, 8, 8, 16])
    # chrome major from the UA so navigator.userAgentVersion-style checks match
    ua = random_user_agent(rng)
    major = int(ua.split('Chrome/')[1].split('.')[0])

    return {
        'country': cc,
        'user_agent': ua,
        'viewport': {'width': w, 'height': h},
        'locale': locale,
        'timezone_id': tz,
        'hardware_concurrency': cores,
        'device_memory': mem,
        'chrome_major': major,
        # per-identity noise seeds (stable within this browser session)
        'canvas_seed': rng.randint(1, 2 ** 31 - 1),
        'webgl_seed': rng.randint(1, 2 ** 31 - 1),
        'audio_seed': rng.randint(1, 2 ** 31 - 1),
    }


def noise_script(ident):
    """Canvas / WebGL / AudioContext noise driven by the identity's seeds.

    Injects deterministic per-draw jitter into:
      - HTMLCanvasElement.toDataURL / toBlob / getImageData (2d)
      - WebGLRenderingContext.readPixels + getParameter (vendor/renderer)
    so the rendered hash differs per identity. Seeds are fixed per identity,
    so the SAME account always produces the SAME hash (required: instability
    across page loads is itself detectable).
    """
    s = ident['canvas_seed']
    w = ident['webgl_seed']
    a = ident['audio_seed']
    js = """
(function (CS, WS, AS) {
  function mulberry32(seed) {
    return function () {
      seed |= 0; seed = seed + 0x6D2B79F5 | 0;
      var t = Math.imul(seed ^ seed >>> 15, 1 | seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  var cRand = mulberry32(CS);
  function jitterByte(v) {
    var d = Math.floor(cRand() * 3) - 1;          // -1..1
    return Math.max(0, Math.min(255, v + d));
  }

  // ---- Canvas 2D ----
  var origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function () {
    var img = origGetImageData.apply(this, arguments);
    try {
      var d = img.data;
      for (var i = 0; i < d.length; i += 997) {   // sparse: cheap + subtle
        d[i] = jitterByte(d[i]);
      }
    } catch (e) {}
    return img;
  };
  var origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function () {
    try {
      var ctx = this.getContext('2d');
      if (ctx) {
        var img = origGetImageData.apply(ctx, [0, 0, Math.max(1, this.width), Math.max(1, this.height)]);
        var d = img.data;
        for (var i = 0; i < d.length; i += 997) d[i] = jitterByte(d[i]);
        ctx.putImageData(img, 0, 0);
      }
    } catch (e) {}
    return origToDataURL.apply(this, arguments);
  };
  var origToBlob = HTMLCanvasElement.prototype.toBlob;
  HTMLCanvasElement.prototype.toBlob = function (cb) {
    try {
      var ctx = this.getContext('2d');
      if (ctx) {
        var img = origGetImageData.apply(ctx, [0, 0, Math.max(1, this.width), Math.max(1, this.height)]);
        var d = img.data;
        for (var i = 0; i < d.length; i += 997) d[i] = jitterByte(d[i]);
        ctx.putImageData(img, 0, 0);
      }
    } catch (e) {}
    return origToBlob.apply(this, arguments);
  };

  // ---- WebGL ----
  function patchGL(proto, consts) {
    var origReadPixels = proto.readPixels;
    proto.readPixels = function () {
      origReadPixels.apply(this, arguments);
      try {
        var px = arguments[6];
        if (px && px.length) {
          for (var i = 0; i < px.length; i += 251) px[i] = jitterByte(px[i]);
        }
      } catch (e) {}
    };
    var origGetParameter = proto.getParameter;
    proto.getParameter = function (p) {
      // vendor / renderer unmasked strings: swap two chars deterministically
      if (consts.indexOf(p) !== -1) {
        var v = origGetParameter.apply(this, arguments);
        try {
          if (typeof v === 'string' && v.length > 8) {
            var i = WS % (v.length - 2);
            return v.slice(0, i) + String.fromCharCode(v.charCodeAt(i + 1)) +
                   String.fromCharCode(v.charCodeAt(i)) + v.slice(i + 2);
          }
        } catch (e) {}
        return v;
      }
      return origGetParameter.apply(this, arguments);
    };
  }
  try { patchGL(WebGLRenderingContext.prototype, [37445, 37446]); } catch (e) {}
  try { patchGL(WebGL2RenderingContext.prototype, [37445, 37446]); } catch (e) {}

  // ---- AudioContext ----
  try {
    var AC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    if (AC) {
      var origGetChannelData = AudioBuffer.prototype.getChannelData;
      AudioBuffer.prototype.getChannelData = function () {
        var arr = origGetChannelData.apply(this, arguments);
        try {
          var aRand = mulberry32(AS);
          for (var i = 0; i < arr.length; i += 411) {
            arr[i] = arr[i] + (aRand() - 0.5) * 1e-7;
          }
        } catch (e) {}
        return arr;
      };
    }
  } catch (e) {}
})(__CANVAS_SEED__, __WEBGL_SEED__, __AUDIO_SEED__);
"""
    return (js.replace('__CANVAS_SEED__', str(s))
              .replace('__WEBGL_SEED__', str(w))
              .replace('__AUDIO_SEED__', str(a)))


def base_script(ident):
    """The anti-detection script, now parameterized by the identity instead of
    hardcoded constants."""
    base = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
Object.defineProperty(navigator, 'languages', { get: () => ['__LOC__', 'en'] });

window.chrome = {
    app: { isInstalled: false },
    runtime: {}
};

Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [
            { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
            { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
            { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
            { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
            { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 }
        ];
        return arr;
    }
});

Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => __CORES__ });
Object.defineProperty(navigator, 'deviceMemory', { get: () => __MEM__ });
"""
    return (base.replace('__LOC__', ident['locale'])
                .replace('__CORES__', str(ident['hardware_concurrency']))
                .replace('__MEM__', str(ident['device_memory'])))
