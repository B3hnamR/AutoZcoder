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
import io
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from utils import ANTI_DETECTION_SCRIPT, BROWSER_ARGS
from config import CONFIG

# NOTE: playwright is imported lazily inside claim_invite(). The referral
# helpers below (referral_summary / send_one_message) are imported by the
# gateway's background finisher, which runs inside the packaged exe where
# playwright is NOT installed — a module-level import would break it.

try:
    import certifi
    _VERIFY = certifi.where() if not os.environ.get('ZAI_INSECURE_SSL') else False
except Exception:
    _VERIFY = False

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
      var r = (JSON.parse(txt) || {}).Result;
      if (r && r.securityToken && (r.VerifyResult === true || r.VerifyCode === 'T001')) {
        window.__verifyParam = b64(JSON.stringify({
          certifyId: r.certifyId, sceneId: SCENE, isSign: true, securityToken: r.securityToken
        }));
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

    # consent screen: tick the required Terms checkbox, THEN Continue enables
    consent_clicked = False
    for _ in range(15):
        # required agreement checkbox (Continue is disabled until checked)
        try:
            cb = page.locator('input[type="checkbox"]').first
            if cb.count() and not cb.is_checked():
                cb.check(force=True)
                log('  invite: bridge -> terms checkbox ticked')
                time.sleep(0.5)
        except Exception:
            pass
        # role-based click is the one proven to advance this page — try it first
        try:
            btn = page.get_by_role('button', name='Continue')
            if btn.count() and btn.first.is_enabled():
                btn.first.click(timeout=5000)
                consent_clicked = True
                log('  invite: bridge -> consent approved (role)')
        except Exception:
            pass
        if not consent_clicked:
            for sel in ('button:has-text("Continue")',
                        'button:has-text("Authorize")',
                        'button:has-text("Allow")'):
                try:
                    loc = page.locator(sel).last
                    if loc.is_visible() and loc.is_enabled():
                        loc.click()
                        consent_clicked = True
                        log(f'  invite: bridge -> consent approved ({sel})')
                        break
                except Exception:
                    continue
        if consent_clicked:
            break
        time.sleep(1)
    if not consent_clicked:
        log('  invite: bridge -> consent not completed')

    # wait for the token exchange + redirect back to zcode
    for _ in range(25):
        url = page.url
        if 'zcode.z.ai' in url and '/login' not in url and 'authorize' not in url:
            break
        time.sleep(1)
    time.sleep(2)
    ok = 'zcode.z.ai' in page.url and 'authorize' not in page.url
    log(f'  invite: bridge settled at {page.url[:80]}')
    return ok


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


def _captcha_param(log=print):
    """Pull a traceless-captcha param from the local Go captcha service
    (POST /api/captcha/solve {return:'param'}, auth: admin/admin). Optional —
    the in-page send is the primary path; this is a fallback for headless runs.

    Returns the param STRING, or None. (It used to return a (param, region)
    tuple, which was then set as an HTTP header and blew up with
    'Header part ((None, '')) must be of type str or bytes'.)"""
    import requests
    base = os.environ.get('ZAI_CAPTCHA_URL') or 'http://127.0.0.1:8687'
    try:
        s = requests.Session()
        r = s.post(f'{base}/api/login',
                   json={'username': 'admin', 'password': 'admin'}, timeout=10)
        tok = r.json().get('token')
        h = {'Authorization': 'Bearer ' + tok} if tok else {}
        r = s.post(f'{base}/api/captcha/solve', json={'return': 'param'},
                   headers=h, timeout=180)
        j = r.json()
        if j.get('success') and j.get('param'):
            return str(j['param'])
        log(f'  invite: captcha solve failed: {str(j)[:120]}')
    except Exception as e:
        log(f'  invite: captcha service unreachable ({base}): {type(e).__name__}')
    return None


ZCODE_IDENTITY = 'You are ZCode, an interactive coding agent'
ZCODE_HARNESS = '''You are an interactive ZCode agent that helps users with software engineering tasks.

# Harness
- Text you output outside of tool use is displayed to the user as Github-flavored markdown in a terminal.
- Prefer the dedicated file/search tools over shell commands when one fits.'''


def _norm_captcha_param(v):
    """Coerce whatever we have into an HTTP-header-safe string or None.
    Guards against the (param, region) tuple an older _captcha_param returned."""
    if isinstance(v, (tuple, list)):
        v = v[0] if v else None
    if v is None:
        return None
    s = v if isinstance(v, str) else str(v)
    return s.strip() or None


def send_message_in_page(page, zcode_jwt, log=print, captcha_param=None) -> bool:
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
                   'authorization': 'Bearer ' + a.jwt};
        if (a.cap) h['x-aliyun-captcha-verify-param'] = a.cap;        const r = await fetch(a.url, {
          method: 'POST', credentials: 'include', headers: h,
          body: JSON.stringify(a.body),
        });
        const t = await r.text();
        return {status: r.status, text: t.slice(0, 300)};
      } catch (e) { return {status: -1, text: String(e)}; }
    }"""
    try:
        res = page.evaluate(js, {'url': CLI_MESSAGES, 'jwt': zcode_jwt,
                                 'body': body, 'cap': captcha_param or ''})
    except Exception as e:
        log(f'  invite: in-page send failed to run ({type(e).__name__})')
        return False
    st = (res or {}).get('status')
    log(f"  invite: in-page message -> {st} {str((res or {}).get('text'))[:120]}")
    return st == 200


def send_one_message(zcode_jwt, log=print, captcha_param=None, retries=4):
    """Complete the send_message task via the ZCode client chat API.
    Body mirrors the native ZCode client exactly (bootstrap system blocks,
    thinking enabled, metadata.user_id).

    The client call is guarded by Aliyun traceless captcha: without a fresh
    `x-aliyun-captcha-verify-param` the API answers 405 / code 3012 "unusual
    activity". The bind-code request the browser just made carries a valid
    param, so we replay that one; if it is missing we fall back to asking the
    local captcha service (ZAI_CAPTCHA_URL), and finally to retries."""
    import requests
    from plan_check import build_headers

    if captcha_param is None:
        captcha_param = _captcha_param(log=log)
    captcha_param = _norm_captcha_param(captcha_param)

    url = 'https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages'
    last = None
    for attempt in range(1, retries + 1):
        h = build_headers(zcode_jwt)
        session_id = h['x-session-id']
        device_mid = h['x-device-mid']
        # chat shape: device lives in metadata only, never in headers
        h.pop('x-device-mid', None)
        h.pop('x-zcode-session-type', None)
        if captcha_param:
            h['x-aliyun-captcha-verify-param'] = captcha_param
        body = {
            'model': 'glm-5.3-flash',
            'max_tokens': 512,
            'stream': True,
            'messages': [{'role': 'user',
                          'content': [{'type': 'text', 'text': 'hi'}]}],
            'system': [{'type': 'text', 'text': ZCODE_IDENTITY},
                       {'type': 'text', 'text': ZCODE_HARNESS}],
            'thinking': {'type': 'enabled'},
            'output_config': {'effort': 'low'},
            'metadata': {'user_id': json.dumps(
                {'device_id': device_mid, 'account_uuid': '', 'session_id': session_id},
                separators=(',', ':'))},
        }
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
                return True
            last = f'{r.status_code} {r.text[:150]}'
            log(f'  invite: client chat -> {last}')
            # 3012 = risk control: refresh the captcha param and back off
            if r.status_code == 405 or '3012' in (r.text or ''):
                if attempt < retries:
                    fresh = _captcha_param(log=log)
                    if fresh:
                        captcha_param = fresh
                    time.sleep(4 * attempt)
                    continue
            break
        except Exception as e:
            last = f'{type(e).__name__}: {e}'
            log(f'  invite: client chat error {last}')
            time.sleep(3)
    if last:
        log(f'  invite: message not sent after {retries} attempt(s): {last}')
    return False


_NO_PROXY = {"", "0", "none", "null", "direct", "off", "false", "no"}


def _proxies():
    # ZAI_PROXY may carry a sentinel meaning 'no proxy' - the gateway sets
    # '0' for direct connections. Anything falsy or one of these tokens means
    # go direct; otherwise the value would be used as a proxy host and every
    # request dies with "Failed to resolve '0'".
    raw = (os.environ.get('ZAI_PROXY') or '').strip()
    if raw.lower() in _NO_PROXY:
        return None
    return {'http': raw, 'https': raw}


APP_HEADERS = {  # exact cli/init header set captured from the desktop app
    'content-type': 'application/json',
    'http-referer': 'https://zcode.z.ai',
    'user-agent': 'ZCode/3.15.3',
    'x-client-language': 'en-US',
    'x-client-timezone': 'Asia/Tehran',
    'x-device-mid': '58aca3b3-3239-4cb7-9c70-df3e5488fb99',
    'x-os-category': 'windows',
    'x-os-version': '10.0.26200',
    'x-platform': 'win32-x64',
    'x-release-channel': 'production',
    'x-title': 'Z Code@electron',
    'x-zcode-app-version': '3.15.3',
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

    mid = device_mid or APP_HEADERS.get('x-device-mid', '58aca3b3-3239-4cb7-9c70-df3e5488fb99')
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
        'app_version': '3.15.3',
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
        'app_version': '3.15.3',
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


def device_login(chat_token, log=print):
    """Full desktop-app-equivalent login:
    cli/init -> authorize (chat token approves) -> cli/poll ready
    -> report_login_device (app_login_success telemetry) -> zai_login.
    Returns (zcodeJWT, ok)."""
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
    r = requests.post(INIT, headers=h, json={'provider': 'zai'},
                      timeout=30, verify=_VERIFY, proxies=_proxies())
    d = (r.json().get('data') or {})
    flow_id, poll_token = d.get('flow_id'), d.get('poll_token') or poll_token
    m = _re.search(r'state=([^&]+)', d.get('authorize_url', ''))
    state = m.group(1) if m else ''
    if not flow_id:
        log('  invite: device init failed')
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
    m = _re.search(r'code=(code-[0-9a-fA-F]+)', r.json().get('redirect_url', ''))
    if not m:
        log(f'  invite: device authorize failed {r.status_code}')
        return None, False
    cb = dict(h)
    cb['x-request-id'] = str(_uuid.uuid4())
    requests.get(CALLBACK + f'?code={m.group(1)}&state={state}',
                 headers=cb, timeout=30, verify=_VERIFY, proxies=_proxies())

    zjwt = zai_tok = user_id = None
    for _ in range(30):
        r = requests.get(POLL + flow_id, headers=h,
                         timeout=30, verify=_VERIFY, proxies=_proxies())
        data = (r.json().get('data') or {})
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
        ok = zai_login(zai_tok, log=log)
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


def claim_invite(chat_token: str, invite_code: str = 'MT9GNDQ8', log=print,
                 headless: bool = False, manual_accept: bool = False,
                 zjwt: str = None, captcha_param: str = None) -> dict:
    """Full invite-claim chain. zjwt: pass a pre-minted zcodeJWT (from
    device_login run EARLIER) to skip step 4 — the proven order for fresh
    accounts is login FIRST, then bind, then message.

    captcha_param: an Aliyun traceless-captcha param captured earlier in the
    flow (e.g. the signup param) — used as the last-resort source for the
    client message, which otherwise answers 405/3012."""
    result = {'bound': False, 'message_sent': False, 'summary': None,
              'zcode_jwt': None}
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
            launch_kwargs['proxy'] = {'server': _px['https']}
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
        page.add_init_script(ANTI_DETECTION_SCRIPT)
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

        # 3. click the accept button (several phrasings seen in the UI), then
        #    wait for the bind-code POST. If nothing lands, retry the click —
        #    the button can appear before the page is interactive.
        def _click_accept() -> bool:
            for sel in ('button:has-text("Accept invitation")',
                        'button:has-text("Accept Invitation")',
                        'button:has-text("Accept")',
                        'button:has-text("接受")'):
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state='visible', timeout=5000)
                    loc.click()
                    log(f'  invite: clicked accept ({sel})')
                    return True
                except Exception:
                    continue
            try:
                page.get_by_role('button', name='Accept invitation').click(timeout=5000)
                log('  invite: clicked accept (role)')
                return True
            except Exception:
                return False

        clicked = _click_accept()
        if not clicked:
            log('  invite: no accept button found (maybe already bound)')

        if manual_accept and not bind_seen['code']:
            log('  invite: MANUAL — click "Accept invitation" in the window now')
            log('  invite: watching for the bind-code POST (up to 300s)…')

        # wait for the bind-code POST (traceless captcha runs on-page). Retry
        # the click a couple of times: a page that is still hydrating swallows it.
        deadline = 300 if manual_accept else 90
        waited = 0
        while waited < deadline and not bind_seen['code']:
            time.sleep(1)
            waited += 1
            if (not manual_accept and waited in (25, 55)
                    and _on_invite_page() and not bind_seen['code']):
                log(f'  invite: no bind-code after {waited}s — retrying the click')
                _click_accept()
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

        # 6. send the one message — from inside the page if possible (only the
        #    browser session reliably clears the Aliyun risk control), else via
        #    a raw request carrying the bind's captcha param
        if s and s['tasks'].get('referral_bind_code') and not s['tasks'].get('send_message'):
            cap = _norm_captcha_param(bind_seen.get('captcha')
                                      or bind_seen.get('verify_param')
                                      or captcha_param)
            sent = False
            if _on_invite_page() or 'zcode.z.ai' in page.url:
                sent = send_message_in_page(page, zjwt, log=log, captcha_param=cap)
            if not sent:
                sent = send_one_message(zjwt, log=log, captcha_param=cap)
            result['message_sent'] = sent

        try:
            ctx.close()
        except Exception:
            pass

    # 7. final summary & plan issuance check
    time.sleep(3)
    s = referral_summary(zjwt, log=log)
    result['summary'] = s
    result['bound'] = bool(s and s['tasks'].get('referral_bind_code'))
    if s:
        log(f"  invite: tasks={s['tasks']} progress={s['current']}/{s['target']} "
            f"issued={s['issued']}")
    return result


if __name__ == '__main__':
    tok = sys.argv[1] if len(sys.argv) > 1 else None
    code = sys.argv[2] if len(sys.argv) > 2 else 'MT9GNDQ8'
    if not tok:
        # try newest account from output
        import glob
        rows = []
        for f in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        'output', 'accounts*.txt')):
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
