"""Claim the invite-referral Global Build (100M GLM-5.3-Flash tokens).

Chain (all from the 100m-accept HAR):
  1. chat.z.ai token  ->  browser session on zcode.z.ai
  2. open /invite/<code>  ->  page runs TRACELESS captcha (scene 11xygtvd)
  3. click "Accept"       ->  POST /zcode-api/incentive/referral/bind-code
                              (zcodeJWT bearer + x-aliyun-captcha-verify-param header)
  4. GET summary           ->  referral_bind_code: true
  5. CLI OAuth (plan_check.get_zcode_jwt) ticks login_device
  6. one chat message      ->  send_message: true  ->  3/3 -> Global Build issues

Usage from the generator (after finish_signup):
    from invite_claim import claim_invite
    ok = claim_invite(chat_token, invite_code='MT9GNDQ8')

Standalone:
    python invite_claim.py <chat_token> [invite_code]
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from utils import BROWSER_ARGS
from plan_check import APP_VERSION

# NOTE: playwright is imported lazily inside claim_invite(). The referral
# helpers below (referral_summary / send_one_message) are imported by the
# gateway's background finisher, which runs inside the packaged exe where
# playwright is NOT installed — a module-level import would break it.

try:
    import certifi
    _VERIFY = certifi.where() if not os.environ.get('ZAI_INSECURE_SSL') else False
except Exception:
    _VERIFY = False

# A stale REQUESTS_CA_BUNDLE (e.g. a leftover mitmproxy cert) silently breaks
# verify=_VERIFY too, since requests prefers the env var over the verify arg.
# Neutralize it unless the user deliberately points it at a valid bundle.
import ssl as _ssl


def _ca_path_ok(path: str) -> bool:
    if not path:
        return True
    if os.path.isdir(path):      # requests accepts a CA directory too
        return True
    try:
        ctx = _ssl.create_default_context(cafile=path)
        return bool(ctx.get_ca_certs())
    except Exception:
        return False


# only the variable that is actually broken is dropped — a valid corporate
# SSL_CERT_FILE must survive a stale REQUESTS_CA_BUNDLE
for _var in ('REQUESTS_CA_BUNDLE', 'SSL_CERT_FILE'):
    if not _ca_path_ok(os.environ.get(_var, '')):
        os.environ.pop(_var, None)

import urllib3
urllib3.disable_warnings()

ZCODE = 'https://zcode.z.ai'
SUMMARY = ZCODE + '/api/v1/incentive/referral/summary'
CLI_MESSAGES = ZCODE + '/api/v1/zcode-plan/anthropic/v1/messages'

# Aliyun traceless captcha: the VerifyCaptchaV3 response carries the signed
# material the app turns into `captcha_verify_param`. Same technique as
# main.py's signup hook, but for the invite scene — this is what lets the
# client message clear risk control (405/3012 without it).
INVITE_SCENE = os.environ.get('ZAI_INVITE_CAPTCHA_SCENE', '11xygtvd')
CAPTCHA_HOOK = r"""
(function () {
  var SCENE = '__SCENE__';
  function b64(s) { try { return btoa(unescape(encodeURIComponent(s))); } catch (e) { return null; } }
  function capture(txt) {
    try {
      if (!txt || txt.indexOf('securityToken') === -1) return;
      var j = JSON.parse(txt);
      var r = (j || {}).Result;
      if (r && r.securityToken && (r.VerifyResult === true || r.VerifyCode === 'T001')) {
        window.__verifyParam = b64(JSON.stringify({
          certifyId: r.certifyId, sceneId: SCENE, isSign: true, securityToken: r.securityToken
        }));
        // region travels with the verdict; must be sent as
        // x-aliyun-captcha-verify-region or the API answers 405/3012
        try { window.__captchaRegion = (j.Region || j.region || ''); } catch (e2) {}
      }
    } catch (e) {}
  }
  var of = window.fetch;
  if (of) {
    window.fetch = function () {
      return of.apply(this, arguments).then(function (res) {
        try { res.clone().text().then(capture).catch(function () {}); } catch (e) {}
        return res;
      });
    };
  }
  var ox = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function () { this.__u = arguments[1]; return ox.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    var self = this;
    this.addEventListener('load', function () {
      try { if (self.responseType === '' || self.responseType === 'text') capture(self.responseText); } catch (e) {}
    });
    return os.apply(this, arguments);
  };
})();
""".replace('__SCENE__', INVITE_SCENE)
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def _oauth_bridge(page, chat_token, log=print):
    """Establish a real zcode.z.ai web session:
    chat.z.ai cookie -> /en/login -> 'Continue with Z.ai' -> consent 'Continue'
    -> authorize code -> /api/v1/oauth/token. Without this dance bind-code
    never fires (verified by diagnosis)."""
    ctx = page.context
    ctx.add_cookies([{
        'name': 'token', 'value': chat_token,
        'domain': 'chat.z.ai', 'path': '/',
        'httpOnly': True, 'secure': True, 'sameSite': 'Lax',
    }])
    login_url = f'{ZCODE}/en/login?redirectPath=%2Fen%2Finvite%2F{os.environ.get("ZAI_INVITE_CODE", "")}'
    try:
        page.goto(login_url, wait_until='domcontentloaded', timeout=60000)
    except Exception:
        pass
    time.sleep(2)

    # provider picker (first login on this profile)
    try:
        btn = page.locator('button:has-text("Continue with Z.ai")').first
        btn.wait_for(state='visible', timeout=8000)
        btn.click()
        log('  invite: bridge -> provider clicked')
    except Exception:
        pass

    # wait for the authorize/consent page to actually render
    for _ in range(20):
        if 'authorize' in page.url or 'oauth' in page.url:
            break
        time.sleep(1)
    time.sleep(2)

    # consent screen: tick the required Terms checkbox, THEN Continue — keep
    # driving until the URL actually leaves login/authorize. A single click
    # can hit a lookalike button (e.g. an empty-email "Continue" on a login
    # page) and the exchange then silently bounces back to /login.
    deadline = time.time() + 45
    consent_clicked = False
    while time.time() < deadline:
        url = page.url
        if 'zcode.z.ai' in url and '/login' not in url and 'authorize' not in url:
            break
        try:
            cb = page.locator('input[type="checkbox"]').first
            if cb.count() and not cb.is_checked():
                cb.check(force=True)
                log('  invite: bridge -> terms checkbox ticked')
                time.sleep(0.5)
        except Exception:
            pass
        clicked = False
        try:
            btn = page.get_by_role('button', name='Continue')
            if btn.count() and btn.first.is_enabled():
                btn.first.click(timeout=5000)
                clicked = True
                log('  invite: bridge -> consent approved (role)')
        except Exception:
            pass
        if not clicked:
            for sel in ('button:has-text("Authorize")',
                        'button:has-text("Allow")',
                        'button:has-text("Continue")'):
                try:
                    loc = page.locator(sel).last
                    if loc.is_visible() and loc.is_enabled():
                        loc.click()
                        clicked = True
                        log(f'  invite: bridge -> consent approved ({sel})')
                        break
                except Exception:
                    continue
        consent_clicked = consent_clicked or clicked
        time.sleep(1)
    if not consent_clicked:
        log('  invite: bridge -> consent not completed')

    time.sleep(2)
    # settled = really logged in. The URL alone is a lie: the invite page is
    # public and renders while logged out. The web app stores its session JWT in
    # localStorage as `zcodejwttoken` after /api/v1/oauth/token; without it the
    # Accept click can never fire bind-code (headless Chromium silently fails
    # this exchange).
    token = False
    deadline = time.time() + 12
    while time.time() < deadline:
        try:
            token = bool(page.evaluate(
                "() => { try { return !!localStorage.getItem('zcodejwttoken'); }"
                " catch (e) { return false; } }"))
        except Exception:
            token = False
        if token:
            break
        time.sleep(0.5)
    url_ok = ('zcode.z.ai' in page.url and '/login' not in page.url
              and 'authorize' not in page.url)
    ok = url_ok and token
    log(f'  invite: bridge settled at {page.url[:80]}'
        + (' (ok)' if ok else
           (' (NO SESSION — token missing)' if not token else ' (no session)')))
    return ok


def _summary_progress(s):
    """`current`, null-safe. referral_summary copies the API's explicit
    nulls into the dict, and dict.get(k, default) does NOT substitute the
    default for an existing None — `None < 3` raised TypeError AFTER the
    claim already succeeded, reporting success as failure."""
    v = s.get('current')
    return v if isinstance(v, (int, float)) else 0


def _summary_target(s):
    v = s.get('target')
    return v if isinstance(v, (int, float)) else 3


def referral_summary(zcode_jwt, log=print):
    import requests
    from plan_check import build_headers
    r = requests.get(SUMMARY, headers=build_headers(zcode_jwt),
                     timeout=30, verify=_VERIFY, proxies=_proxies())
    try:
        d = r.json().get('data') or {}
    except Exception:
        return None
    tasks = {t.get('key'): t.get('completed') for t in (d.get('invitee', {}).get('tasks') or [])}
    reward = next((r0 for r0 in d.get('invitee', {}).get('rewards') or []
                   if r0.get('state')), None)
    issued = reward.get('issued') if reward else None
    cur = reward.get('current') if reward else None
    return {'tasks': tasks, 'issued': issued, 'current': cur,
            'target': reward.get('target') if reward else None}


_LAST_CAPTCHA_REGION = None


def _deleted_captcha_service_stub():
    """(removed) The external Go captcha-service fallback was deleted: it was
    not in this repo, degraded silently, and per the Aliyun A/B report an
    HTTP-minted token can never pass (F001). _page_captcha_param() replaces
    it — params are minted inside the same headful browser that earned the
    captcha score."""
    return None


ZCODE_IDENTITY = 'You are ZCode, an interactive coding agent'
ZCODE_HARNESS = '''You are an interactive ZCode agent that helps users with software engineering tasks.

# Harness
- Text you output outside of tool use is displayed to the user as Github-flavored markdown in a terminal.
- Prefer the dedicated file/search tools over shell commands when one fits.'''


_LAST_CAPTCHA_REGION = None


def _page_captcha_param(page, log=print, max_wait=15):
    """Mint a FRESH traceless-captcha param inside the logged-in page.

    Replaces the old external Go service (not in this repo, silently
    degrading, and per the Aliyun A/B report a pure HTTP minter can never
    pass anyway: the judge scores the BROWSER that earned the token). The
    page's own Aliyun SDK performs InitCaptchaV3 + VerifyCaptchaV3 when the
    invite page loads; CAPTCHA_HOOK captures the T001 material. Each call
    clears window.__verifyParam first so a retry can never replay a
    consumed certifyId (F008 "duplicate" looks exactly like a bad token)."""
    global _LAST_CAPTCHA_REGION
    try:
        page.evaluate("window.__verifyParam = ''")
        deadline = time.time() + max_wait
        while time.time() < deadline:
            vp = page.evaluate("window.__verifyParam || ''")
            if vp:
                _LAST_CAPTCHA_REGION = page.evaluate(
                    "window.__captchaRegion || ''") or _LAST_CAPTCHA_REGION
                return str(vp)
            time.sleep(1)
        log('  invite: no fresh page captcha param within '
            f'{max_wait}s (scene={INVITE_SCENE})')
    except Exception as e:
        log(f'  invite: page captcha mint failed ({type(e).__name__})')
    return None


def send_message_in_page(page, zcode_jwt, log=print, captcha_param=None,
                         captcha_region=None) -> bool:
    """Send the one client message from INSIDE the logged-in browser page.

    The client chat endpoint is guarded by the Aliyun traceless captcha: a plain
    requests/httpx call answers 405 / code 3012 "unusual activity". The browser
    session already passed that guard when it bound the invite code, so issuing
    the request from the page origin (real TLS + cookies + fingerprint) is the
    reliable path. Returns True on HTTP 200."""
    import json as _json
    from plan_check import build_headers

    h = build_headers(zcode_jwt)
    session_id = h.get('x-session-id')
    device_mid = h.get('x-device-mid')
    body = {
        'model': 'glm-5.3-flash',
        'max_tokens': 512,
        'stream': True,
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}],
        'system': [{'type': 'text', 'text': ZCODE_IDENTITY},
                   {'type': 'text', 'text': ZCODE_HARNESS}],
        'thinking': {'type': 'enabled'},
        'output_config': {'effort': 'low'},
        'metadata': {'user_id': _json.dumps(
            {'device_id': device_mid, 'account_uuid': '', 'session_id': session_id},
            separators=(',', ':'))},
    }
    js = """async (a) => {
      try {
        const h = {'content-type': 'application/json',
                   'accept': 'text/event-stream',
                   'anthropic-version': '2023-06-01',
                   'authorization': 'Bearer ' + a.jwt,
                   'x-api-key': a.jwt,
                   'x-zcode-agent': 'glm',
                   'x-zcode-app-version': APP_VERSION,
                   'x-zcode-session-type': 'main',
                   'x-client-language': 'en-US',
                   'x-os-category': 'windows',
                   'x-platform': 'win32-x64',
                   'http-referer': 'https://zcode.z.ai',
                   'x-device-mid': a.dev,
                   'x-session-id': a.sid};
        if (a.cap) h['x-aliyun-captcha-verify-param'] = a.cap;
        if (a.reg) h['x-aliyun-captcha-verify-region'] = a.reg;
        const r = await fetch(a.url, {
          method: 'POST', credentials: 'include', headers: h,
          body: JSON.stringify(a.body),
        });
        const t = await r.text();
        return {status: r.status, text: t.slice(0, 300)};
      } catch (e) { return {status: -1, text: String(e)}; }
    }"""
    try:
        res = page.evaluate(js, {'url': CLI_MESSAGES, 'jwt': zcode_jwt,
                                 'body': body, 'cap': captcha_param or '',
                                 'reg': captcha_region or '',
                                 'dev': device_mid or '', 'sid': session_id or ''})
    except Exception as e:
        log(f'  invite: in-page send failed to run ({type(e).__name__})')
        return False
    st = (res or {}).get('status')
    log(f"  invite: in-page message -> {st} {str((res or {}).get('text'))[:120]}")
    return st == 200


def _send_confirmed(zcode_jwt, log=print, last=None):
    """The endpoint may answer 405/3012 on the wire while still crediting the
    task server-side — the referral summary is the only truth."""
    try:
        s = referral_summary(zcode_jwt, log=lambda *a, **k: None)
        if s and s['tasks'].get('send_message'):
            log('  invite: send_message confirmed by summary')
            return True
    except Exception:
        pass
    if last:
        log(f'  invite: message not sent: {last}')
    return False


_SEND_PAGE = [None]     # live claim page for in-page captcha retries


def send_one_message(zcode_jwt, log=print, retries=2):
    """Complete the send_message task via the ZCode client chat API.

    Uses the exact wire contract from the gateway (GLM-5.3-Flash, anthropic-beta,
    electron headers, stream=True) which passes upstream risk control with 200."""
    import requests
    gateway_url = os.environ.get("ZAI_GATEWAY_URL") or "http://127.0.0.1:8788"
    # Per-run token when spawned by the gateway; legacy static value only
    # authenticates from loopback (manual CLI runs of this repo's tools).
    internal = os.environ.get("ZAI_GATEWAY_INTERNAL_TOKEN") or "gateway-internal"
    try:
        gr = requests.post(
            f"{gateway_url}/admin/dash/generator/send_claim_message",
            json={"zjwt": zcode_jwt},
            headers={"Authorization": f"Bearer {internal}"},
            timeout=15,
        )
        if gr.status_code == 200 and gr.json().get("ok"):
            log("  invite: client message sent via gateway (200)")
            return _send_confirmed(zcode_jwt, log)
        log(f"  invite: gateway send -> {gr.status_code} {gr.text[:120]}")
    except Exception as e:
        log(f"  invite: gateway send skipped ({type(e).__name__}) — falling back to direct")

    url = 'https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages'
    last = None

    try:
        from zcode_gateway.upstream import build_headers as gw_headers
        from zcode_gateway.config import Fingerprint
        from zcode_gateway.zcode_protocol import build_body
        fp = Fingerprint()
        h = gw_headers(zcode_jwt, fp, include_device_mid=False)
        body = build_body({"model": "GLM-5.3-Flash", "max_tokens": 10,
                           "messages": [{"role": "user", "content": "hi"}], "stream": True})
    except Exception:
        from plan_check import build_headers
        h = build_headers(zcode_jwt)
        h.pop('x-device-mid', None)
        h['anthropic-beta'] = 'mid-conversation-system-2026-04-07'
        session_id = h.get('x-session-id', '')
        device_mid = h.get('x-device-mid', '')
        body = {
            'model': 'GLM-5.3-Flash',
            'max_tokens': 10,
            'stream': True,
            'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}],
            'system': [{'type': 'text', 'text': ZCODE_IDENTITY},
                       {'type': 'text', 'text': ZCODE_HARNESS}],
            'thinking': {'type': 'enabled'},
            'output_config': {'effort': 'max'},
        }

    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, headers=h, json=body, stream=True,
                              timeout=120, verify=_VERIFY, proxies=_proxies())
            if r.status_code == 200:
                n = 0
                for chunk in r.iter_content(chunk_size=None):
                    n += len(chunk)
                    if n > 512:
                        break
                r.close()
                log(f'  invite: client message sent ({n} bytes streamed)')
                return _send_confirmed(zcode_jwt, log)
            last = f'{r.status_code} {r.text[:150]}'
            log(f'  invite: client chat -> {last}')
            # 405/3012 = risk control wants the traceless-captcha headers.
            # Retrying the identical captcha-less request can never succeed
            # (the regression 86576bd deleted this branch). Send ONE fresh
            # in-page attempt with a just-minted param: the page's Aliyun SDK
            # earns the score in the browser risk-control actually judges.
            if (r.status_code == 405 or '3012' in (r.text or '')) and _SEND_PAGE[0] is not None:
                try:
                    page = _SEND_PAGE[0]
                    cap = _page_captcha_param(page, log=log)
                    reg = _LAST_CAPTCHA_REGION or ''
                    if cap and send_message_in_page(page, zcode_jwt, log=log,
                                                    captcha_param=cap,
                                                    captcha_region=reg):
                        return _send_confirmed(zcode_jwt, log)
                    log('  invite: in-page captcha retry did not land')
                except Exception as e:
                    log(f'  invite: in-page captcha retry failed ({type(e).__name__})')
            time.sleep(3 * attempt)
        except Exception as e:
            last = f'{type(e).__name__}: {e}'
            log(f'  invite: client chat error {last}')
            time.sleep(3)
    return _send_confirmed(zcode_jwt, log, last)


_NO_PROXY = {"", "0", "none", "null", "direct", "off", "false", "no"}
_FORCE_DIRECT = False


def _proxies():
    # force_direct claims run on the real IP: suppressed here instead of mutating
    # os.environ, so an exception can never strip the caller's signup proxy.
    if _FORCE_DIRECT:
        return None
    # ZAI_PROXY may carry a sentinel meaning 'no proxy' - the gateway sets
    # '0' for direct connections. Anything falsy or one of these tokens means
    # go direct; otherwise the value would be used as a proxy host and every
    # request dies with "Failed to resolve '0'".
    raw = (os.environ.get('ZAI_PROXY') or '').strip()
    if raw.lower() in _NO_PROXY:
        return None
    return {'http': raw, 'https': raw}


def _direct_proxy_while(fn):
    """Run `fn` with direct mode when force_direct=True; always restore."""
    import functools
    import inspect

    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _FORCE_DIRECT
        try:
            force = sig.bind_partial(*args, **kwargs).arguments.get('force_direct', False)
        except TypeError:
            force = kwargs.get('force_direct', False)
        prev = _FORCE_DIRECT
        _FORCE_DIRECT = bool(force) or prev
        try:
            return fn(*args, **kwargs)
        finally:
            _FORCE_DIRECT = prev

    return wrapper


def per_account_device_mid(token: str) -> str:
    """Stable per-account device id (uuid5 of the chat token) instead of the
    hardcoded shared one — every account reporting the SAME x-device-mid in
    telemetry is a direct cross-account correlation signal."""
    import uuid as _u
    return str(_u.uuid5(_u.NAMESPACE_URL, 'zcode-device:' + (token or '')))


APP_HEADERS = {  # exact cli/init header set captured from the desktop app
    'content-type': 'application/json',
    'http-referer': 'https://zcode.z.ai',
    'user-agent': 'ZCode/' + APP_VERSION,
    'x-client-language': 'en-US',
    'x-client-timezone': 'Asia/Tehran',
    'x-device-mid': '58aca3b3-3239-4cb7-9c70-df3e5488fb99',
    'x-os-category': 'windows',
    'x-os-version': '10.0.26200',
    'x-platform': 'win32-x64',
    'x-release-channel': 'production',
    'x-title': 'Z Code@electron',
    'x-zcode-app-version': APP_VERSION,
    'accept': '*/*',
}


def report_login_device(zjwt: str, user_id: str = None, device_mid: str = None, log=print) -> bool:
    """Complete the login_device task by emitting the official desktop client
    telemetry events:
      1. app_login_ck (elementName: 'app_login_ck', eventRegion: 'app', eventType: 'ck')
      2. app_login_success (elementName: 'app_login_success', eventRegion: 'app_profile', eventType: 'view')
    This reproduces the exact client telemetry emitted on desktop sign-in and
    immediately flips login_device to True on the server."""
    import requests
    import uuid as _uuid
    import base64 as _b64

    if not zjwt:
        return False

    if not user_id:
        try:
            p = zjwt.split('.')[1]
            p += '=' * ((4 - len(p) % 4) % 4)
            claims = json.loads(_b64.urlsafe_b64decode(p))
            user_id = claims.get('user_id') or claims.get('sub') or ''
        except Exception:
            user_id = ''

    mid = device_mid or per_account_device_mid(zjwt)
    h = dict(APP_HEADERS)
    h['authorization'] = 'Bearer ' + zjwt
    endpoint = 'https://zcode.z.ai/api/v1/event/report'

    # 1. app_login_ck
    body_ck = {
        'event_id': str(_uuid.uuid4()),
        'client_timezone': 'Asia/Tehran',
        'client_language': 'en-US',
        'element_name': 'app_login_ck',
        'event_region': 'app',
        'event_type': 'ck',
        'event_text': '',
        'event_extra_detail': {
            'login_url': 'https://chat.z.ai/auth/oauth/authorize?response_type=code&client_id=client_P8X5CMWmlaRO9gyO-KSqtg'
        },
        'user_id': user_id,
        'screen_resolution': '1849x1041',
        'app_version': APP_VERSION,
        'device_os_category': 'Windows',
        'device_os_version': '10.0.26200',
        'device_mid': mid,
        'mac_id': '',
        'marketing_params': '{}'
    }
    try:
        requests.post(endpoint, headers=h, json=body_ck, timeout=30, verify=_VERIFY, proxies=_proxies())
    except Exception as e:
        log(f'  invite: app_login_ck telemetry notice: {e}')

    # 2. app_login_success (the critical event that credits login_device)
    body_success = {
        'event_id': str(_uuid.uuid4()),
        'client_timezone': 'Asia/Tehran',
        'client_language': 'en-US',
        'element_name': 'app_login_success',
        'event_region': 'app_profile',
        'event_type': 'view',
        'event_text': '',
        'event_extra_detail': {
            'login_provider': 'zai'
        },
        'user_id': user_id,
        'screen_resolution': '1849x1041',
        'app_version': APP_VERSION,
        'device_os_category': 'Windows',
        'device_os_version': '10.0.26200',
        'device_mid': mid,
        'mac_id': '',
        'marketing_params': '{}'
    }
    try:
        r = requests.post(endpoint, headers=h, json=body_success, timeout=30, verify=_VERIFY, proxies=_proxies())
        ok = r.status_code == 200 and (r.json().get('code') == 0 or r.json().get('code') == 200)
        log(f'  invite: app_login_success telemetry -> {r.status_code} (login_device signaled)')
        return ok
    except Exception as e:
        log(f'  invite: app_login_success failed: {e}')
        return False


@_direct_proxy_while
def device_login(chat_token, log=print, force_direct: bool = False):
    """Full desktop-app-equivalent login:
    cli/init -> authorize (chat token approves) -> cli/poll ready
    -> report_login_device (app_login_success telemetry) -> zai_login.
    Returns (zcodeJWT, ok).

    force_direct: the OAuth flow is authenticated by the chat token, not the IP,
    so the 100M claim runs from the user's own connection instead of the
    account-generation proxy."""
    import requests
    import secrets
    import re as _re
    import uuid as _uuid
    CLIENT_ID = 'client_P8X5CMWmlaRO9gyO-KSqtg'
    INIT = 'https://zcode.z.ai/api/v1/oauth/cli/init'
    AUTHORIZE = 'https://chat.z.ai/api/oauth/authorize'
    CALLBACK = 'https://zcode.z.ai/api/v1/oauth/cli/callback/zai'
    POLL = 'https://zcode.z.ai/api/v1/oauth/cli/poll/'

    poll_token = secrets.token_hex(32)
    h = dict(APP_HEADERS)
    h['authorization'] = 'Bearer ' + poll_token
    # zcode.z.ai answers 502 in bursts; init is idempotent (it just opens a
    # new flow), so a few quick retries beat aborting the whole login
    r = None
    for _try in range(3):
        try:
            r = requests.post(INIT, headers=h, json={'provider': 'zai'},
                              timeout=30, verify=_VERIFY, proxies=_proxies())
        except requests.RequestException as e:
            log(f'  invite: device init error ({type(e).__name__})')
            r = None
        if r is not None and r.status_code < 500:
            break
        if r is not None:
            log(f'  invite: device init {r.status_code} — retrying')
        time.sleep(2)
    if r is None:
        return None, False
    try:
        d = (r.json().get('data') or {})
    except Exception:
        d = {}
    flow_id, poll_token = d.get('flow_id'), d.get('poll_token') or poll_token
    m = _re.search(r'state=([^&]+)', d.get('authorize_url', ''))
    state = m.group(1) if m else ''
    if not flow_id:
        log(f'  invite: device init failed {r.status_code} {r.text[:120]}')
        return None, False

    r = requests.post(AUTHORIZE, headers={
        'authorization': 'Bearer ' + chat_token,
        'content-type': 'application/x-www-form-urlencoded',
        'x-region': 'overseas', 'origin': 'https://chat.z.ai',
        'user-agent': 'Mozilla/5.0',
    }, data={'client_id': CLIENT_ID,
             'redirect_uri': CALLBACK,
             'state': state, 'response_type': 'code',
             'action': 'approve'}, timeout=30, verify=_VERIFY, proxies=_proxies())
    try:
        rj = r.json()
    except Exception:
        rj = {}
    m = _re.search(r'code=(code-[0-9a-fA-F]+)', str(rj.get('redirect_url', '')))
    if not m:
        log(f'  invite: device authorize failed {r.status_code} {r.text[:120]}')
        return None, False
    cb = dict(h)
    cb['x-request-id'] = str(_uuid.uuid4())
    # The callback is what makes the flow "ready". A 502 here used to be
    # ignored, and the poll loop then waited minutes on a flow that could never
    # complete — so retry it, and poll only briefly if it still never landed.
    cb_ok = False
    for _try in range(3):
        try:
            cr = requests.get(CALLBACK + f'?code={m.group(1)}&state={state}',
                              headers=cb, timeout=30, verify=_VERIFY, proxies=_proxies())
            cb_ok = cr.status_code < 500
        except requests.RequestException as e:
            log(f'  invite: device callback error ({type(e).__name__})')
        if cb_ok:
            break
        log('  invite: device callback not accepted — retrying')
        time.sleep(2)

    zjwt = zai_tok = user_id = None
    for _ in range(30 if cb_ok else 5):
        try:
            r = requests.get(POLL + flow_id, headers=h,
                             timeout=30, verify=_VERIFY, proxies=_proxies())
        except requests.RequestException as e:
            # one slow/failed poll must not discard a flow that is still alive
            log(f'  invite: device poll error ({type(e).__name__})')
            time.sleep(2)
            continue
        try:
            data = (r.json().get('data') or {})
        except Exception:
            data = {}
        if data.get('status') == 'ready':
            zjwt = data.get('token')
            user_info = data.get('user') or {}
            user_id = user_info.get('user_id')
            zai_tok = (data.get('zai') or {}).get('access_token')
            break
        time.sleep(2)
    if not zjwt:
        log('  invite: device poll never ready')
        return None, False

    # Flip login_device via official client telemetry
    report_login_device(zjwt, user_id=user_id, log=log)

    ok = False
    if zai_tok:
        # best-effort: the zcodeJWT is already minted, and a flaky api.z.ai
        # lookup/connect here used to raise out of device_login and throw the
        # whole login (and the claim built on it) away
        try:
            ok = zai_login(zai_tok, log=log)
        except Exception as e:
            log(f'  invite: zai login skipped ({type(e).__name__}) — continuing with the zcodeJWT')
    return zjwt, ok


def zai_login(zai_access_token, log=print):
    """Complete the login_device task: register the login with the z.ai
    account service — exactly what the desktop app does after the CLI OAuth
    poll returns ready (captured via MITM 2026-10-15)."""
    import requests
    import uuid as _uuid
    r = requests.post('https://api.z.ai/api/auth/z/login',
                      json={'token': zai_access_token},
                      headers={'content-type': 'application/json',
                               'x-request-id': str(_uuid.uuid4()),
                               'user-agent': 'undici',
                               'accept': '*/*'},
                      timeout=30, verify=_VERIFY, proxies=_proxies())
    try:
        j = r.json()
    except Exception:
        j = {}
    ok = r.status_code == 200 and j.get('code') == 200
    log(f'  invite: zai login -> {r.status_code} '
        f'({"login_device done" if ok else str(j)[:100]})')
    return ok


@_direct_proxy_while
def claim_invite(chat_token: str, invite_code: str = 'MT9GNDQ8', log=print,
                 headless: bool = False, manual_accept: bool = False,
                 zjwt: str = None, captcha_param: str = None,
                 force_direct: bool = False) -> dict:
    """Full invite-claim chain. zjwt: pass a pre-minted zcodeJWT (from
    device_login run EARLIER) to skip step 4 — the proven order for fresh
    accounts is login FIRST, then bind, then message.

    captcha_param: ACCEPTED FOR COMPATIBILITY BUT IGNORED. Params are
    single-use per certifyId (F008 "duplicate"), so a param captured at
    signup is consumed there and replaying it reads as a bad token. The
    claim flow mints its own fresh params inside the live page.

    force_direct: run the whole claim on the REAL IP — the bind is authenticated
    by the account session, not by IP, so the signup proxy is neither needed nor
    desirable here (verified 2026-10-18: same-account bind succeeded from a
    different IP). The claim window stays visible: headless Chromium never
    completes /api/v1/oauth/token, which leaves the invite page logged out and
    bind-code can never fire."""
    result = {'bound': False, 'message_sent': False, 'summary': None,
              'zcode_jwt': None}
    if force_direct:
        # Headless is NOT safe for this step: Chromium silently drops the OAuth
        # token exchange, the invite page renders logged out, and Accept never
        # emits bind-code. Visible + manual accept is the working recipe.
        if os.environ.get('ZAI_CLAIM_HEADLESS', '').strip().lower() not in ('1', 'true', 'yes', 'on'):
            headless = False
    if not headless:
        manual_accept = True            # the window is visible: allow a human click
    suffix = os.environ.get('ZAI_OUT_SUFFIX', '')
    # per-account profile: a reused profile carries the PREVIOUS account's
    # zcode session and the consent page then names the wrong user.
    tag = chat_token[-12:] if chat_token else 'x'
    profile = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           f'.pw-profile-inv-{tag}-{suffix}')
    os.environ['ZAI_INVITE_CODE'] = invite_code
    log('  invite: launching browser…')

    bind_seen = {'code': None}

    # lazy: keeps this module importable without playwright (gateway finisher)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        launch_kwargs = dict(
            headless=headless, args=BROWSER_ARGS,
            viewport={'width': 1280, 'height': 820}, user_agent=UA,
            locale='en-US', timezone_id='America/New_York')
        _px = _proxies()
        if _px and _px.get('https'):
            # embedded credentials (http://user:pass@host:port) must be split
            # into separate fields — Chromium drops them from 'server' alone
            # and then shows an auth popup (net::ERR_INVALID_AUTH_CREDENTIALS)
            from urllib.parse import urlparse
            _u = urlparse(_px['https'])
            _pd = {'server': f"{_u.scheme or 'http'}://{_u.hostname}:{_u.port}"}
            if _u.username:
                _pd['username'] = _u.username
                _pd['password'] = _u.password or ''
            launch_kwargs['proxy'] = _pd
        elif force_direct:
            log('  invite: direct mode — claiming on the real IP')

        import fingerprint as _fp
        _px_url = (_px or {}).get('https')
        ident = _fp.make_identity(proxy_url=_px_url if not force_direct else None)
        launch_kwargs['viewport'] = ident['viewport']
        launch_kwargs['user_agent'] = ident['user_agent']
        launch_kwargs['locale'] = ident['locale']
        launch_kwargs['timezone_id'] = ident['timezone_id']
        ctx = p.chromium.launch_persistent_context(
            profile, **launch_kwargs)
        log('  invite: browser up')

        def on_response(resp):
            try:
                if 'bind-code' in resp.url:
                    bind_seen['code'] = resp.status
                    try:
                        bind_seen['body'] = resp.json()
                    except Exception:
                        bind_seen['body'] = None
            except Exception:
                pass

        def on_request(req):
            # the bind-code POST carries the Aliyun traceless-captcha param the
            # page just earned — keep it so send_one_message can replay it
            try:
                if 'bind-code' in req.url:
                    hdrs = {str(k).lower(): v for k, v in (req.headers or {}).items()}
                    bind_seen['headers'] = hdrs
                    for key in ('x-aliyun-captcha-verify-param',
                                'x-captcha-verify-param'):
                        if hdrs.get(key):
                            bind_seen['captcha'] = hdrs[key]
                            break
            except Exception:
                pass

        page = ctx.new_page()
        page.set_default_timeout(45000)
        page.add_init_script(_fp.base_script(ident))
        page.add_init_script(_fp.noise_script(ident))
        page.add_init_script(CAPTCHA_HOOK)     # build the client-call captcha param
        page.on('response', on_response)
        page.on('request', on_request)
        page.on('dialog', lambda d: d.dismiss())   # never hang on a modal

        # 1. real web session on zcode.z.ai via the OAuth bridge
        _oauth_bridge(page, chat_token, log=log)

        # 2. land on the invite page logged-in. The OAuth bridge itself lands
        #    there (redirectPath), but if it stalled we retry the bridge and
        #    then force-navigate — otherwise the Accept click hits the wrong page
        #    and bind-code silently never fires.
        invite_url = f'{ZCODE}/invite/{invite_code}'

        def _on_invite_page() -> bool:
            return '/invite/' in page.url and 'authorize' not in page.url \
                and '/login' not in page.url

        try:
            page.goto(invite_url, wait_until='domcontentloaded', timeout=60000)
        except Exception as e:
            log(f'  invite: page load issue ({str(e)[:60]}) — continuing')
        time.sleep(4)
        if not _on_invite_page():
            log('  invite: not on the invite page after bridge — retrying bridge')
            try:
                _oauth_bridge(page, chat_token, log=log)
            except Exception as e:
                log(f'  invite: bridge retry failed ({type(e).__name__})')
            for attempt in range(3):
                if _on_invite_page():
                    break
                try:
                    page.goto(invite_url, wait_until='domcontentloaded', timeout=60000)
                except Exception:
                    pass
                time.sleep(3)
        log(f'  invite: at {page.url[:90]}')

        # 3. click the accept button (several phrasings/renderings seen in the
        #    UI — it also renders as a link), then wait for the bind-code POST.
        def _logged_out() -> bool:
            # localStorage is authoritative: the header can still render a
            # "Log in" link (footer/account switcher) on a logged-in page, and
            # the old visual check then re-ran the bridge for no reason.
            try:
                return not bool(page.evaluate(
                    "() => { try { return !!localStorage.getItem('zcodejwttoken'); }"
                    " catch (e) { return false; } }"))
            except Exception:
                pass
            try:
                for sel in ('a:has-text("Log in")', 'button:has-text("Log in")',
                            'a:has-text("Sign in")'):
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible():
                        return True
            except Exception:
                pass
            return False

        def _click_accept() -> bool:
            # Security-verification failure detection: the page shows
            # "Security verification failed. Please try again." under the
            # button when the Aliyun risk engine rejected the captcha token.
            # Reload + re-click earns a FRESH captcha score instead of
            # hammering the rejected one.
            def _security_failed() -> bool:
                try:
                    for sel in ('text=Security verification failed',
                                'text=安全验证失败'):
                        if page.locator(sel).first.count():
                            return True
                except Exception:
                    pass
                return False

            def _fresh_page_and_click() -> bool:
                try:
                    page.goto(invite_url, wait_until='domcontentloaded',
                              timeout=60000)
                except Exception:
                    pass
                time.sleep(2.5)
                return _click_buttons()

            def _click_buttons() -> bool:
                for sel in ('button:has-text("Accept invitation")',
                            'button:has-text("Accept Invitation")',
                            'button:has-text("Accept")',
                            'button:has-text("接受")',
                            'a:has-text("Accept invitation")',
                            'a:has-text("Accept")',
                            '[role="button"]:has-text("Accept")'):
                    try:
                        loc = page.locator(sel).first
                        loc.wait_for(state='visible', timeout=4000)
                        loc.click()
                        log(f'  invite: clicked accept ({sel})')
                        return True
                    except Exception:
                        continue
                for role in ('button', 'link'):
                    try:
                        page.get_by_role(role, name='Accept invitation') \
                            .first.click(timeout=4000)
                        log(f'  invite: clicked accept (role={role})')
                        return True
                    except Exception:
                        continue
                try:
                    page.get_by_text('Accept invitation').first.click(timeout=4000)
                    log('  invite: clicked accept (text)')
                    return True
                except Exception:
                    return False

            ok = _click_buttons()
            if ok and _security_failed():
                log('  invite: security verification failed — reloading for a fresh captcha')
                return _fresh_page_and_click()
            return ok

        # the invite page is PUBLIC — it renders even logged out, so being on
        # the right URL proves nothing. A "Log in" pill means the bridge
        # failed: re-run it before trying to accept.
        if _logged_out():
            log('  invite: invite page is LOGGED OUT — re-running the OAuth bridge')
            try:
                _oauth_bridge(page, chat_token, log=log)
            except Exception as e:
                log(f'  invite: bridge retry failed ({type(e).__name__})')
            for attempt in range(3):
                if _on_invite_page() and not _logged_out():
                    break
                try:
                    page.goto(invite_url, wait_until='domcontentloaded', timeout=60000)
                except Exception:
                    pass
                time.sleep(3)
            log(f'  invite: at {page.url[:90]} (after re-bridge)')

        clicked = _click_accept()
        if not clicked:
            if _logged_out():
                log('  invite: no accept click — page is LOGGED OUT')
            else:
                log('  invite: no accept button found (maybe already bound)')

        if manual_accept and not bind_seen['code'] and clicked:
            try:
                page.bring_to_front()      # make sure the human can see it
            except Exception:
                pass
            log('  invite: MANUAL — click "Accept invitation" in the window now')
            log('  invite: watching for the bind-code POST (up to 90s)…')

        # wait for the bind-code POST (traceless captcha runs on-page). Retry
        # the click a couple of times: a page that is still hydrating swallows
        # it. A click while logged out bounces to /login — re-bridge then.
        # If no Accept button exists the code is already bound (or the page is
        # logged out): don't burn the manual window on a POST that can't happen.
        deadline = (90 if manual_accept else 90) if clicked else 12
        waited = 0
        bind_retries = 0
        MAX_BIND_RETRIES = 3
        while waited < deadline and not bind_seen['code']:
            time.sleep(1)
            waited += 1
            if 'zcode.z.ai' in page.url and '/login' in page.url:
                log('  invite: bounced to login — re-running the OAuth bridge')
                try:
                    _oauth_bridge(page, chat_token, log=log)
                except Exception as e:
                    log(f'  invite: bridge retry failed ({type(e).__name__})')
                try:
                    page.goto(invite_url, wait_until='domcontentloaded', timeout=60000)
                except Exception:
                    pass
                time.sleep(3)
                _click_accept()
                continue
            # risk-controlled bind: a NON-200 bind-code response used to end
            # the wait loop for good — the account was silently left unbound
            # and nothing but a manual re-run could recover it. Treat only 200
            # as done; on a failure status, reload for a fresh captcha score
            # (certifyId is single-use) and click again, bounded.
            if isinstance(bind_seen['code'], int) and bind_seen['code'] != 200 \
                    and bind_retries < MAX_BIND_RETRIES:
                bind_retries += 1
                log(f"  invite: bind-code -> {bind_seen['code']} — reload + re-click "
                    f"({bind_retries}/{MAX_BIND_RETRIES}) for a fresh captcha")
                bind_seen['code'] = 0
                bind_seen.pop('body', None)
                try:
                    page.goto(invite_url, wait_until='domcontentloaded', timeout=60000)
                    time.sleep(2.5)
                except Exception:
                    pass
                _click_accept()
                waited = 0        # a fresh attempt earns a fresh window
                continue
            if (waited in (15, 30, 45)
                    and _on_invite_page() and not bind_seen['code']):
                log(f'  invite: no bind-code after {waited}s — retrying the click')
                reloaded = False
                try:
                    if page.locator('text=Security verification failed').first.count():
                        log('  invite: security verification failed — reloading for a fresh captcha')
                        try:
                            page.goto(invite_url, wait_until='domcontentloaded',
                                      timeout=60000)
                        except Exception:
                            pass
                        time.sleep(2.5)
                        reloaded = True
                except Exception:
                    pass
                if not reloaded and not _click_accept() and _logged_out():
                    log('  invite: accept vanished and page is LOGGED OUT')
                    _oauth_bridge(page, chat_token, log=log)
                    try:
                        page.goto(invite_url, wait_until='domcontentloaded', timeout=60000)
                    except Exception:
                        pass
                    time.sleep(3)
                    _click_accept()
            # the bind POST can fire from a different frame/popup the page-level
            # request hook never sees. The server summary is the source of truth.
            if zjwt and waited % 15 == 0 and not bind_seen['code']:
                try:
                    s_now = referral_summary(zjwt, log=lambda *a, **k: None)
                    if s_now and s_now['tasks'].get('referral_bind_code'):
                        bind_seen['code'] = 200
                        bind_seen['body'] = {'data': {'bound': True}}
                        log('  invite: bind confirmed by summary')
                        break
                except Exception:
                    pass
        if bind_seen['code'] == 200:
            body = bind_seen.get('body') or {}
            result['bound'] = bool((body.get('data') or {}).get('bound', True))
            log(f"  invite: bind-code -> 200 bound={result['bound']}")
        elif bind_seen['code']:
            log(f"  invite: bind-code -> {bind_seen['code']} {str(bind_seen.get('body'))[:120]}")
        else:
            log('  invite: bind-code never fired')

        # the invite page's own captcha may have produced a usable param even if
        # the bind request never fired (e.g. the code was already bound)
        try:
            vp = page.evaluate("window.__verifyParam || ''")
            if vp:
                bind_seen.setdefault('verify_param', vp)
                log('  invite: built captcha verify param from the page')
        except Exception:
            pass

        # 4. zcodeJWT: pre-supplied (device_login ran earlier) or mint now
        if not zjwt:
            zjwt, _ = device_login(chat_token, log=log)
        result['zcode_jwt'] = zjwt
        if not zjwt:
            log('  invite: no zcodeJWT — cannot finish task chain')
            try:
                ctx.close()
            except Exception:
                pass
            return result

        # 5. verify tasks and ensure login_device is credited
        time.sleep(2)
        s = referral_summary(zjwt, log=log)
        if s and not s['tasks'].get('login_device'):
            log('  invite: login_device not set — reporting client telemetry')
            report_login_device(zjwt, log=log)
            time.sleep(1)
            s = referral_summary(zjwt, log=log)

        # 6. send the one message — plain client-shape request through the
        #    signup proxy. No captcha header, no device headers: that exact
        #    shape is what passes risk control (the endpoint may still answer
        #    405/3012 on the wire while crediting the task — the summary is
        #    the only truth). A 405/3012 falls back to ONE fresh in-page
        #    send with a just-minted captcha param (see send_one_message).
        if s and s['tasks'].get('referral_bind_code') and not s['tasks'].get('send_message'):
            _SEND_PAGE[0] = page          # page still open inside the ctx
            try:
                result['message_sent'] = send_one_message(zjwt, log=log)
            finally:
                _SEND_PAGE[0] = None

        try:
            ctx.close()
        except Exception:
            pass

    # 7. final summary & plan issuance check
    s = None
    for attempt in range(6):
        time.sleep(2)
        s = referral_summary(zjwt, log=log)
        if not s:
            continue
        if s.get('issued') or _summary_progress(s) < _summary_target(s):
            break
        # All tasks complete (3/3) but issued is granting asynchronously: poll up to 12s
        log(f"  invite: newcomer tasks 3/3 complete — waiting for plan issuance ({attempt + 1}/6)…")
    result['summary'] = s
    result['bound'] = bool(s and s['tasks'].get('referral_bind_code'))
    if s:
        log(f"  invite: tasks={s['tasks']} progress={_summary_progress(s)}/{_summary_target(s)} "
            f"issued={s['issued']}")
    return result


if __name__ == '__main__':
    tok = sys.argv[1] if len(sys.argv) > 1 else None
    code = sys.argv[2] if len(sys.argv) > 2 else 'MT9GNDQ8'
    if not tok:
        # try newest account from output
        import glob
        rows = []
        out_dir = os.environ.get('ZAI_OUTPUT_DIR') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
        for f in glob.glob(os.path.join(out_dir, 'accounts*.txt')):
            for ln in open(f, encoding='utf-8', errors='ignore'):
                parts = ln.strip().split(':')
                if len(parts) >= 3 and parts[-1].startswith('eyJ'):
                    rows.append(parts[-1])
        tok = rows[-1] if rows else None
        if not tok:
            print('usage: python invite_claim.py <chat_token> [invite_code]')
            sys.exit(1)
        print(f'using newest saved token ({tok[:25]}…)')
    out = claim_invite(tok, code)
    print(json.dumps(out, indent=1, default=str)[:1200])
