import logging, os
# silence PIL/pngjs chunk-stream debug spam
logging.getLogger('PIL').setLevel(logging.WARNING)
os.environ.setdefault('PYTHONWARNINGS', 'ignore')

import time
import random
import string
import json
import subprocess
import logging
import sys
import re
import html
from pathlib import Path
from playwright.sync_api import sync_playwright

from utils import ANTI_DETECTION_SCRIPT, BROWSER_ARGS, SELECTORS, take_screenshot, timing
from captcha_solver import CaptchaSolver
from config import CONFIG

# Where results land. ZAI_OUTPUT_DIR lets the gateway redirect worker output
# into its own portable folder; default stays ZCODEAUTO's output/.
OUTPUT_DIR = os.environ.get('ZAI_OUTPUT_DIR', 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)   # log handler writes inside OUTPUT_DIR

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%H:%M:%S'))

# The gateway sets ZAI_OUTPUT_DIR per worker; CONFIG['log_file'] is a
# repo-relative default that ignores it. Honor the env var so redirected
# workers never hit FileNotFoundError from a nonexistent output/ dir.
_log_file = os.environ.get('ZAI_LOG_FILE') or os.path.join(OUTPUT_DIR, 'output.txt')
os.makedirs(os.path.dirname(_log_file) or '.', exist_ok=True)
main_log_handler = logging.FileHandler(_log_file, encoding='utf-8')
main_log_handler.setLevel(logging.DEBUG)
main_log_handler.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%H:%M:%S'))

root_logger.handlers = [console_handler, main_log_handler]

logger = logging.getLogger(__name__)


FIRST_NAMES = [
    'James', 'Mary', 'Robert', 'Patricia', 'John', 'Jennifer', 'Michael', 'Linda',
    'David', 'Elizabeth', 'William', 'Barbara', 'Richard', 'Susan', 'Joseph', 'Jessica',
    'Thomas', 'Sarah', 'Charles', 'Karen', 'Christopher', 'Nancy', 'Daniel', 'Margaret',
    'Matthew', 'Lisa', 'Anthony', 'Betty', 'Mark', 'Sandra', 'Donald', 'Ashley',
    'Steven', 'Kimberly', 'Paul', 'Emily', 'Andrew', 'Donna', 'Joshua', 'Michelle',
    'Kenneth', 'Carol', 'Kevin', 'Amanda', 'Brian', 'Dorothy', 'George', 'Melissa',
    'Edward', 'Deborah', 'Ronald', 'Stephanie', 'Timothy', 'Rebecca', 'Jason', 'Sharon',
    'Jeffrey', 'Laura', 'Ryan', 'Cynthia', 'Jacob', 'Kathleen', 'Gary', 'Amy',
    'Nicholas', 'Shirley', 'Eric', 'Angela', 'Jonathan', 'Helen', 'Stephen', 'Anna',
    'Larry', 'Brenda', 'Justin', 'Pamela', 'Scott', 'Nicole', 'Brandon', 'Emma',
    'Benjamin', 'Samantha', 'Samuel', 'Katherine', 'Gregory', 'Christine', 'Frank', 'Debra',
    'Alexander', 'Rachel', 'Raymond', 'Catherine', 'Patrick', 'Carolyn', 'Jack', 'Janet',
    'Dennis', 'Ruth', 'Jerry', 'Maria', 'Tyler', 'Heather', 'Aaron', 'Diane',
    'Henry', 'Virginia', 'Douglas', 'Julie', 'Jose', 'Joyce', 'Peter', 'Victoria',
    'Adam', 'Olivia', 'Nathan', 'Kelly', 'Zachary', 'Christina', 'Walter', 'Lauren',
    'Kyle', 'Joan', 'Harold', 'Evelyn', 'Carl', 'Judith', 'Arthur', 'Andrea',
    'Gerald', 'Hannah', 'Roger', 'Megan', 'Keith', 'Cheryl', 'Jeremy', 'Jacqueline',
    'Terry', 'Martha', 'Lawrence', 'Gloria', 'Sean', 'Teresa', 'Christian', 'Ann',
    'Ethan', 'Sara', 'Austin', 'Madison', 'Joe', 'Frances', 'Albert', 'Kathryn',
    'Jesse', 'Janice', 'Willie', 'Jean', 'Billy', 'Abigail', 'Bryan', 'Alice',
    'Bruce', 'Judy', 'Jordan', 'Sophia', 'Dylan', 'Grace', 'Noah', 'Denise',
    'Alan', 'Amber', 'Ralph', 'Doris', 'Gabriel', 'Marilyn', 'Logan', 'Danielle',
    'Wayne', 'Beverly', 'Vincent', 'Isabella', 'Eugene', 'Theresa', 'Randy', 'Diana',
    'Harry', 'Natalie', 'Philip', 'Brittany', 'Louis', 'Charlotte', 'Bobby', 'Marie',
    'Johnny', 'Kayla', 'Mason', 'Alexis', 'Eduardo', 'Lori',
]

LAST_NAMES = [
    'Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis',
    'Rodriguez', 'Martinez', 'Hernandez', 'Lopez', 'Gonzalez', 'Wilson', 'Anderson',
    'Thomas', 'Taylor', 'Moore', 'Jackson', 'Martin', 'Lee', 'Perez', 'Thompson',
    'White', 'Harris', 'Sanchez', 'Clark', 'Ramirez', 'Lewis', 'Robinson', 'Walker',
    'Young', 'Allen', 'King', 'Wright', 'Scott', 'Torres', 'Nguyen', 'Hill',
    'Flores', 'Green', 'Adams', 'Nelson', 'Baker', 'Hall', 'Rivera', 'Campbell',
    'Mitchell', 'Carter', 'Roberts', 'Gomez', 'Phillips', 'Evans', 'Turner', 'Diaz',
    'Parker', 'Cruz', 'Edwards', 'Collins', 'Reyes', 'Stewart', 'Morris', 'Morales',
    'Murphy', 'Cook', 'Rogers', 'Gutierrez', 'Ortiz', 'Morgan', 'Cooper', 'Peterson',
    'Bailey', 'Reed', 'Kelly', 'Howard', 'Ramos', 'Kim', 'Cox', 'Ward',
    'Richardson', 'Watson', 'Brooks', 'Chavez', 'Wood', 'James', 'Bennett', 'Gray',
    'Mendoza', 'Ruiz', 'Hughes', 'Price', 'Alvarez', 'Castillo', 'Sanders', 'Patel',
    'Myers', 'Long', 'Ross', 'Foster', 'Jimenez', 'Powell', 'Jenkins', 'Perry',
    'Russell', 'Sullivan', 'Bell', 'Coleman', 'Butler', 'Henderson', 'Barnes', 'Gonzales',
    'Fisher', 'Vasquez', 'Simmons', 'Romero', 'Jordan', 'Patterson', 'Alexander', 'Hamilton',
    'Graham', 'Reynolds', 'Griffin', 'Wallace', 'Moreno', 'West', 'Cole', 'Hayes',
    'Bryant', 'Herrera', 'Gibson', 'Ellis', 'Tran', 'Medina', 'Aguilar', 'Stevens',
    'Murray', 'Ford', 'Castro', 'Marshall', 'Owens', 'Harrison', 'Fernandez', 'Mcdonald',
    'Woods', 'Washington', 'Kennedy', 'Wells', 'Vargas', 'Henry', 'Chen', 'Freeman',
    'Webb', 'Tucker', 'Guzman', 'Burns', 'Crawford', 'Olson', 'Simpson', 'Porter',
    'Hunter', 'Gordon', 'Mendez', 'Silva', 'Shaw', 'Snyder', 'Mills', 'Pearson',
]


def generate_human_name() -> str:
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    return f"{first} {last}"


def jwt_payload(token: str):
    """Decode a JWT payload without verifying. Returns dict or None."""
    try:
        import base64 as _b64
        part = token.split('.')[1]
        part += '=' * ((4 - len(part) % 4) % 4)
        return json.loads(_b64.urlsafe_b64decode(part))
    except Exception:
        return None


def token_email(token: str):
    p = jwt_payload(token)
    return (p or {}).get('email')


def is_guest_token(token: str) -> bool:
    """chat.z.ai mints a guest JWT on first load; localStorage keeps it after signup.
    Treating that as the account token causes a false success."""
    em = token_email(token) or ''
    return em.endswith('@guest.com') or em.startswith('guest-')


# The app sends a data: URI avatar in finish_signup (not "/user.png"); match it.
AVATAR_DATA_URI = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                   "AAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

# Per-process output suffix so parallel runs (ZAI_OUT_SUFFIX=1,2,3) don't clobber.
OUT_SUFFIX = os.environ.get('ZAI_OUT_SUFFIX', '')
ACC_FILE = os.path.join(OUTPUT_DIR, f'accounts{OUT_SUFFIX}.txt')
PLANS_FILE = os.path.join(OUTPUT_DIR, f'plans{OUT_SUFFIX}.jsonl')
STARTER_FILE = os.path.join(OUTPUT_DIR, f'starter{OUT_SUFFIX}.txt')

# Dedicated chromium profile per process: lets us identify (and kill) browsers left
# behind by a killed run without ever touching the user's own Chrome.
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           '.pw-profile' + OUT_SUFFIX)


def env_on(name):
    """Routing flags: '1' = on; '0'/''/absent = off (the panel sends explicit
    '0' to defeat parent-shell inheritance)."""
    return os.environ.get(name, '').strip() == '1'


def _env_proxy():
    v = os.environ.get('ZAI_PROXY', '').strip()
    return v if v and v != '0' else None


CURRENT_PROXY = _env_proxy() or (None if env_on('ZAI_USE_POOL') or os.environ.get('ZAI_PROXY') == '0' else CONFIG.get('proxy'))


def set_proxy(url):
    """Point the whole flow (browser + API calls) at a new exit IP."""
    global CURRENT_PROXY
    CURRENT_PROXY = url or None
    if url:
        os.environ['ZAI_PROXY'] = url   # plan_check / requests read this
    else:
        os.environ.pop('ZAI_PROXY', None)


def pick_verified_proxy():
    """Verify-on-pick: grab a pool entry and re-test it before use.
    ZAI_SOCKS_ONLY=1: socks4/5 entries only (residential-style — they pass the
    Starter gate; HTTP datacenter ranges fail it)."""
    try:
        import proxy_pool
        return proxy_pool.pick_verified(socks=env_on('ZAI_SOCKS_ONLY'))
    except Exception as e:
        logger.warning(f"proxy pick failed: {e}")
        return None


def pw_proxy():
    """Playwright proxy dict from the current proxy, or None for direct."""
    raw = CURRENT_PROXY
    if not raw:
        return None
    from urllib.parse import urlparse
    u = urlparse(raw)
    if not u.hostname or not u.port:
        return None
    d = {'server': f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        d['username'] = u.username
        d['password'] = u.password or ''
    return d


def req_proxies():
    """requests-style proxies dict for API calls (same exit IP as the browser)."""
    if not CURRENT_PROXY:
        return None
    return {'http': CURRENT_PROXY, 'https': CURRENT_PROXY}


def kill_stale_browsers():
    """Kill chromium processes left over from a previous killed run.

    Only touches processes whose command line contains the EXACT argument
    `--user-data-dir=<our profile dir>` as a whole token — a substring/wildcard
    match would let .pw-profilew1 also match .pw-profilew10/w11 and kill other
    workers' browsers. CommandLineToArgvW-style parsing via a regex that
    requires the argument to END right after the path (quote or whitespace).
    """
    pat = re.escape(PROFILE_DIR) + r'("|\s|$)'
    try:
        ps = ("Get-CimInstance Win32_Process | "
              "Where-Object { $_.Name -match 'chrome|chromium|msedge' -and "
              f"$_.CommandLine -match '--user-data-dir={pat}' }} | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
              "-ErrorAction SilentlyContinue }")
        subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                       capture_output=True, timeout=25,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception:
        pass


# Aliyun's VerifyCaptchaV3 response carries the signed material the app puts into
# `captcha_verify_param`:
#   {"Result":{"securityToken":"...","VerifyCode":"T001","VerifyResult":true,"certifyId":"..."}}
# and the app's field decodes to base64({certifyId, sceneId, isSign:true, securityToken}).
# `#aliyunCaptcha-verify-param` is EMPTY in this mode, so build it from the network.
CAPTCHA_VERIFY_HOOK = r"""
(function () {
  var SCENE = '36qgs6xb';
  function b64(s) { try { return btoa(unescape(encodeURIComponent(s))); } catch (e) { return null; } }
  function capture(txt) {
    try {
      if (!txt || txt.indexOf('securityToken') === -1) return;
      var j = JSON.parse(txt);
      var r = j && j.Result;
      // Only a VERDICT of true is success material. Aliyun can return a
      // securityToken on failed verifies too — stashing that produced
      // false "success:hook" signals after a failed slide.
      if (r && r.securityToken && (r.VerifyResult === true || r.VerifyCode === 'T001')) {
        window.__verifyResp = r;
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
  var ox = XMLHttpRequest.prototype.open;
  var os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function () { this.__u = arguments[1]; return ox.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    var self = this;
    this.addEventListener('load', function () {
      try { if (self.responseType === '' || self.responseType === 'text') capture(self.responseText); } catch (e) {}
    });
    return os.apply(this, arguments);
  };
})();
"""


class MailTM:

    def __init__(self):
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.base_url = 'https://api.mail.tm'
        self.session = requests.Session()
        self.session.verify = False  # mail.tm cert chain not in certifi bundle on this box
        self.session.headers.update({
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        })
        self.account = None
        self.token = None
        self.email = None
        self.password = None

    def get_domain(self):
        try:
            resp = self.session.get(f'{self.base_url}/domains', verify=False)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    first = data[0]
                    if isinstance(first, str):
                        return first
                    elif isinstance(first, dict):
                        return first.get('domain') or first.get('name')
                elif isinstance(data, dict):
                    member = data.get('hydra:member', [])
                    if member and len(member) > 0:
                        first = member[0]
                        if isinstance(first, str):
                            return first
                        elif isinstance(first, dict):
                            return first.get('domain') or first.get('name')
        except Exception as e:
            logger.warning(f"Error getting domains: {e}")

        return None

    def create_account(self):
        domain = self.get_domain()
        if not domain:
            logger.error("Could not fetch mail.tm domain")
            return False

        username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        self.email = f"{username}@{domain}"
        self.password = ''.join(random.choices(string.ascii_letters + string.digits + '!@#$%^&*', k=16))

        payload = {
            'address': self.email,
            'password': self.password
        }

        try:
            resp = self.session.post(f'{self.base_url}/accounts', json=payload, verify=False)
            if resp.status_code in [200, 201]:
                self.account = resp.json()
                logger.info(f"Created mail.tm account: {self.email}")
                return True
            else:
                logger.warning(f"Failed to create account on {domain}")
                return False
        except Exception as e:
            logger.error(f"Error creating mail.tm account: {e}")
            return False

    def get_token(self):
        if not self.email or not self.password:
            return None

        payload = {
            'address': self.email,
            'password': self.password
        }

        try:
            resp = self.session.post(f'{self.base_url}/token', json=payload, verify=False)
            if resp.status_code == 200:
                data = resp.json()
                self.token = data.get('token')
                self.session.headers.update({
                    'Authorization': f'Bearer {self.token}'
                })
                logger.info(f"Got mail.tm token")
                return self.token
            else:
                logger.error(f"Failed to get mail.tm token: {resp.status_code}")
                return None
        except Exception as e:
            logger.error(f"Error getting mail.tm token: {e}")
            return None

    def get_messages(self):
        if not self.token:
            return None
        try:
            resp = self.session.get(f'{self.base_url}/messages', verify=False)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict) and data.get('hydra:member'):
                    return data['hydra:member']
                return data
            return None
        except:
            return None

    def get_message(self, message_id):
        if not self.token:
            return None
        try:
            resp = self.session.get(f'{self.base_url}/messages/{message_id}', verify=False)
            return resp.json() if resp.status_code == 200 else None
        except:
            return None

    def get_verification_link(self, timeout=60):
        logger.info(f"Waiting for verification email (timeout: {timeout}s)...")

        start_time = time.time()
        while time.time() - start_time < timeout:
            messages = self.get_messages()

            if messages and isinstance(messages, list):
                for msg in messages:
                    subject = msg.get('subject', '').lower()
                    if 'verify' in subject or 'verification' in subject or 'confirm' in subject:
                        logger.info(f"Found verification email: {msg.get('subject')}")

                        full_msg = self.get_message(msg.get('id'))
                        if full_msg:
                            html_content = full_msg.get('html', [''])[0] if full_msg.get('html') else ''
                            if not html_content:
                                html_content = full_msg.get('htmlBody', [''])[0] if full_msg.get('htmlBody') else ''
                            text = full_msg.get('text', [''])[0] if full_msg.get('text') else ''
                            if not text:
                                text = full_msg.get('plainBody', [''])[0] if full_msg.get('plainBody') else ''

                            patterns = [
                                r'https://chat\.z\.ai/auth/verify_email\?[^\s"\']+',
                                r'https://chat\.z\.ai/verify[^\s"\']+',
                                r'https://[^\s"\']*verify[^\s"\']+',
                                r'http[s]?://[^\s"\']*verify[^\s"\']+',
                                r'http[s]?://[^\s"\']*confirm[^\s"\']+'
                            ]

                            for pattern in patterns:
                                matches = re.findall(pattern, html_content + text)
                                if matches:
                                    logger.info(f"Found verification link")
                                    return html.unescape(matches[0])
            time.sleep(2)

        logger.warning("Timeout waiting for verification email")
        return None


def env_flag(name):
    """Env override with explicit parsing: '0'/'false'/'no' = False,
    '1'/'true'/'yes' = True, absent = None (fall back to CONFIG)."""
    v = os.environ.get(name)
    if v is None:
        return None
    return v.strip().lower() in ('1', 'true', 'yes', 'on')


def signup(email, password, mail=None):
    logger.info(f"Starting: {email}")
    timing.start('total_signup')

    try:
        with sync_playwright() as p:
            # Persistent context (playwright refuses --user-data-dir via launch()).
            # Using our own profile dir lets kill_stale_browsers() reap orphans.
            is_manual = env_flag('ZAI_MANUAL')
            if is_manual:
                is_headless = False
            elif 'ZAI_HEADLESS' in os.environ:
                is_headless = env_on('ZAI_HEADLESS')
            else:
                is_headless = CONFIG.get('headless', False)

            context = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=is_headless,
                args=BROWSER_ARGS,
                proxy=pw_proxy(),
                viewport={'width': CONFIG['viewport_width'], 'height': CONFIG['viewport_height']},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York',
            )
            browser = context   # `browser.close()` below closes the persistent context

            page = context.new_page()
            page.add_init_script(ANTI_DETECTION_SCRIPT)
            page.add_init_script(CAPTCHA_VERIFY_HOOK)

            # Capture the REAL auth token from the signup response's set-cookie.
            # Reading localStorage.token is wrong: it still holds the guest session
            # JWT minted on page load, which produced a false "account created".
            captured = {'token': None, 'status': None, 'body': None, 'calls': []}

            def _capture_auth(resp):
                try:
                    url = resp.url
                    if '/api/v1/auths' in url:
                        path = url.split('?')[0]
                        try:
                            method = resp.request.method
                        except Exception:
                            method = '?'
                        captured['calls'].append((method, path, resp.status))
                        logger.info(f"    [auth] {method} {path} -> {resp.status}")
                        # The app auto-submits /auths/signup the moment the captcha passes.
                        # That is the ONLY reliable success signal — the DOM check can time
                        # out while the app has already moved on. Tell the solver to stop.
                        if method == 'POST' and path.endswith('/auths/signup'):
                            try:
                                page.evaluate("window.__signupFired = true")
                            except Exception:
                                pass
                    if re.search(r'/api/v1/auths/(finish_)?signup', url):
                        captured['status'] = resp.status
                        try:
                            captured['body'] = resp.text()[:500]
                        except Exception:
                            pass
                        sc = resp.headers.get('set-cookie') or ''
                        m = re.search(r'token=([^;]+)', sc)
                        if m:
                            captured['token'] = m.group(1)
                            logger.info(f"Captured signup token (status={resp.status})")
                except Exception:
                    pass

            page.on('response', _capture_auth)

            try:
                timing.start('page_load')
                loaded = False
                for _ in range(3):
                    try:
                        # SPA often never reaches 'load'; domcontentloaded is reliable.
                        page.goto('https://chat.z.ai/auth', wait_until='domcontentloaded',
                                  timeout=60000)
                        loaded = True
                        break
                    except Exception as e:
                        msg = str(e)
                        logger.warning(f"auth page load retry: {msg[:80]}")
                        if 'ERR_CERT' in msg:
                            # The proxy is MITM-ing TLS (its own cert). Never acceptable:
                            # it could read/alter the session. Blacklist it and rotate.
                            logger.warning("  TLS cert error via proxy (MITM) — blacklisting")
                            try:
                                import proxy_pool
                                proxy_pool.mark_bad(CURRENT_PROXY, 'tls-mitm')
                            except Exception:
                                pass
                            break
                        time.sleep(2)
                if not loaded:
                    raise RuntimeError("could not load chat.z.ai/auth")
                timing.end('page_load')
                time.sleep(1.5)

                # Wait for each control to actually BE there before touching it. A slow
                # proxy renders the form late; clicking a not-yet-present button does
                # nothing (or times out) and kills the run before the captcha appears.

                def _click_when_ready(sel, timeout=45000):
                    el = page.locator(sel).first
                    el.wait_for(state='visible', timeout=timeout)
                    try:
                        el.click(timeout=timeout)
                    except Exception:
                        el.click(timeout=timeout, force=True)

                def _fill_when_ready(sel, value, timeout=45000):
                    el = page.locator(sel).first
                    el.wait_for(state='visible', timeout=timeout)
                    el.fill(value)

                _click_when_ready(SELECTORS['continue_with_email'])
                time.sleep(0.8)

                _fill_when_ready(SELECTORS['email_input'], email)
                _fill_when_ready(SELECTORS['password_input'], password)
                _click_when_ready(SELECTORS['signup_button'])

                name = generate_human_name()
                logger.info(f"Using name: {name}")
                try:
                    _fill_when_ready(SELECTORS['name_input'], name, timeout=20000)
                except Exception:
                    page.locator('input[type="text"]').first.fill(name)

                # "Create Account" must be present before we click — it appears late on
                # a slow IP, and clicking early is what broke the captcha trigger.
                _click_when_ready(SELECTORS['create_account_button'], timeout=45000)
                time.sleep(1.5)

                # Trigger the captcha. The element's id/text varies by SDK build (it
                # reads "Click to start verification" here) and on a slow proxy it
                # renders LATE — so wait up to 25s for it rather than assuming.
                started = False
                trigger_sels = (SELECTORS['captcha_start'],
                                '#aliyunCaptcha-captcha-wrapper',
                                '#aliyunCaptcha-captcha-img',
                                'text=Click to start verification',
                                '[id^="aliyunCaptcha-captcha"]')
                deadline = time.time() + 25
                while time.time() < deadline and not started:
                    for sel in trigger_sels:
                        try:
                            el = page.locator(sel)
                            if el.count() > 0 and el.first.is_visible():
                                el.first.click(timeout=3000)
                                started = True
                                logger.info(f"Captcha trigger clicked ({sel})")
                                break
                        except Exception:
                            continue
                    if not started:
                        time.sleep(1)
                if not started:
                    logger.info("Captcha start not auto-clicked (already open, or click manually)")

                slider_ok = True
                try:
                    page.locator(SELECTORS['slider']).wait_for(state='visible', timeout=8000)
                except Exception:
                    slider_ok = False

                _manual_env = env_flag('ZAI_MANUAL')
                manual = (CONFIG.get('manual_captcha') if _manual_env is None
                          else _manual_env)

                if not slider_ok and not manual:
                    # The widget never rendered (page shows "Please complete the
                    # verification first"). In practice this is an IP/edge block: Aliyun
                    # refuses to serve the challenge. Grinding 15 attempts is pointless
                    # and only burns risk score — skip this run.
                    logger.warning("Captcha widget did not load (IP-blocked?) — skipping run")
                    take_screenshot(page, "captcha_not_loaded")
                    browser.close()
                    return 'CAPTCHA_NOT_LOADED'

                solver = CaptchaSolver(logger, CONFIG)
                if manual:
                    # Human solves it in the visible window; we just wait for the pass
                    # and then continue the API flow automatically.
                    result = 'MANUAL_OK' if solver.wait_for_manual_solve(page) else None
                else:
                    result = solver.solve(page)

                if result == 'CAPTCHA_NOT_LOADED':
                    logger.warning("Captcha stopped serving challenges (IP-blocked) — skipping run")
                    take_screenshot(page, "captcha_not_loaded")
                    browser.close()
                    return 'CAPTCHA_NOT_LOADED'

                if not result:
                    # The solver can time out on the DOM check even though the app already
                    # accepted the captcha and submitted signup (observed: slide 09:42:36,
                    # signup POST 09:42:53). If we saw a 200 signup POST, the account
                    # exists — continue instead of bailing.
                    app_ok = any(m == 'POST' and p.endswith('/auths/signup') and s == 200
                                 for (m, p, s) in captured['calls'])
                    if app_ok:
                        logger.info("Captcha check timed out, but signup POST=200 — continuing")
                    else:
                        logger.error("Captcha failed")
                        take_screenshot(page, "captcha_failed")
                        browser.close()
                        return None

                if isinstance(result, str) and len(result) > 20:
                    logger.info(f"Captcha solved, got verification token")
                    time.sleep(1)

                time.sleep(1)

                # --- Direct-API signup -------------------------------------------------
                # Driving the UI submit is unreliable from automation (the app-owned
                # captcha callback never fired the POST for us). The captured manual
                # flow is plain JSON over the same session, so we call it directly:
                #   POST /api/v1/auths/signup       {name,email,password,profile_image_url,
                #                                    sso_redirect,captcha_verify_param}
                #   POST /api/v1/auths/verify_email  {username,email,token}
                #   POST /api/v1/auths/finish_signup {username,email,token,password,...}
                captcha_param = None
                try:
                    captcha_param = page.evaluate(
                        "() => window.__verifyParam"
                        " || window.__captchaParam"
                        " || (document.querySelector('#aliyunCaptcha-verify-param')||{}).value"
                        " || null;")
                except Exception:
                    pass
                if not captcha_param and isinstance(result, str) and len(result) > 40:
                    captcha_param = result
                logger.info(f"captcha_verify_param: {'captured' if captcha_param else 'MISSING'}")

                def _page_post(path, payload):
                    return page.evaluate("""
                        async (a) => {
                            try {
                                const r = await fetch(a.path, {
                                    method: 'POST',
                                    headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify(a.body),
                                });
                                let t = '';
                                try { t = await r.text(); } catch (e) {}
                                return { status: r.status, body: t };
                            } catch (e) { return { status: -1, body: String(e) }; }
                        }
                    """, {'path': path, 'body': payload})

                username = name
                signup_status = None
                app_fired = any(m == 'POST' and p.endswith('/auths/signup')
                                for (m, p, s) in captured['calls'])
                if app_fired:
                    # The app already submitted on captcha pass. Re-POSTing would double
                    # submit and trip the 45s per-email rate limit.
                    signup_status = captured['status'] or 200
                    logger.info(f"App already submitted signup (status={signup_status}) "
                                f"— not re-POSTing")
                elif captcha_param:
                    r = _page_post('/api/v1/auths/signup', {
                        'name': name,
                        'email': email,
                        'password': password,
                        'profile_image_url': '/user.png',
                        'sso_redirect': None,
                        'captcha_verify_param': captcha_param,
                    })
                    signup_status = r['status']
                    logger.info(f"signup: status={r['status']} body={str(r['body'])[:300]}")
                else:
                    logger.warning("No captcha_verify_param — cannot POST signup")

                # --- Direct-API verify + finish --------------------------------------
                verify_token = None
                if signup_status in (200, 201) and mail:
                    logger.info("Waiting for the verification email...")
                    link = mail.get_verification_link(timeout=90)
                    if link:
                        logger.info(f"verify link: {link[:100]}...")
                        m = re.search(r'token=(verify-[0-9a-fA-F]+)', link)
                        if m:
                            verify_token = m.group(1)
                    else:
                        logger.warning("No verification email arrived")
                elif signup_status not in (200, 201):
                    logger.warning(f"signup not OK (status={signup_status}) — skipping verify")

                finish_token = None
                if verify_token:
                    r = _page_post('/api/v1/auths/verify_email',
                                   {'username': username, 'email': email, 'token': verify_token})
                    logger.info(f"verify_email: status={r['status']} body={str(r['body'])[:200]}")
                    # finish_signup returns the session token in its body:
                    #   {"success":true,"user":{"token":"eyJ...","email":"..."}}
                    # The manual capture used a data: URI avatar, so match that.
                    r = _page_post('/api/v1/auths/finish_signup',
                                   {'username': username, 'email': email, 'token': verify_token,
                                    'password': password,
                                    'profile_image_url': AVATAR_DATA_URI,
                                    'sso_redirect': None})
                    logger.info(f"finish_signup: status={r['status']} body={str(r['body'])[:200]}")
                    try:
                        fj = json.loads(r['body'])
                        finish_token = (fj.get('user') or {}).get('token')
                        if finish_token:
                            logger.info(f"finish_signup issued token "
                                        f"(email={token_email(finish_token)})")
                    except Exception:
                        pass

                # --- Extract the real session token --------------------------------
                # finish_signup returns it directly; fall back to a non-guest cookie or
                # localStorage (which may still hold the pre-signup guest JWT). The
                # fallback token's email MUST match the account we just created — a
                # persistent profile can hold a previous identity's session.
                token = finish_token
                if not token:
                    for c in context.cookies('https://chat.z.ai'):
                        if c.get('name') == 'token':
                            v = c.get('value', '')
                            if v and not is_guest_token(v) and token_email(v) == email:
                                token = v
                if not token:
                    lt = page.evaluate(
                        "() => localStorage.getItem('token') || sessionStorage.getItem('token')")
                    if lt and not is_guest_token(lt) and token_email(lt) == email:
                        token = lt

                if token:
                    logger.info(f"Account token email={token_email(token)}")
                    browser.close()
                    return token

                logger.warning("No non-guest token obtained — account not confirmed")
                logger.info(f"    auth calls seen: {captured['calls']}")
                browser.close()
                return None

            except Exception as e:
                logger.error(f"Error: {e}")
                take_screenshot(page, "error")
                try:
                    browser.close()
                except:
                    pass
                return None

    except Exception as e:
        logger.error(f"Browser launch error: {e}")
        return None


class CatchAllMail:
    """Temp addresses on a REAL domain (Cloudflare Email Routing catch-all -> Gmail).

    z.ai rejects disposable domains ("Please use a permanent email address"), and it
    rate-limits per email for 45s. A catch-all on a real domain fixes both: every
    account gets a fresh address, and the domain looks permanent.

    Reading: polls the destination Gmail over IMAP for the z.ai verify mail.
    Creds come from env (ZAI_GMAIL_USER / ZAI_GMAIL_APP_PW) or gen/gmail.json.
    """

    def __init__(self, domain=None, logger_instance=None):
        self.logger = logger_instance or logging.getLogger(__name__)
        self.domain = (os.environ.get('ZAI_MAIL_DOMAIN') or domain
                       or CONFIG.get('mail_domain', 'behnamrjd.me'))
        self.user = os.environ.get('ZAI_GMAIL_USER')
        self.app_pw = os.environ.get('ZAI_GMAIL_APP_PW')
        self.inbox_url = os.environ.get('ZAI_INBOX_URL') or CONFIG.get('mail_inbox_url')
        self.inbox_secret = os.environ.get('ZAI_INBOX_SECRET') or CONFIG.get('mail_inbox_secret')
        for fname in ('mail.json', 'gmail.json'):
            creds = Path(__file__).with_name(fname)
            if not creds.exists():
                continue
            try:
                j = json.loads(creds.read_text())
                self.user = self.user or j.get('user')
                self.app_pw = self.app_pw or j.get('app_pw')
                self.inbox_url = self.inbox_url or j.get('inbox_url')
                self.inbox_secret = self.inbox_secret or j.get('inbox_secret')
            except Exception:
                pass
        self.email = None
        self.password = None

    def _pick_domain(self) -> str:
        """Rotation across the user's configured domains (ZAI_MAIL_DOMAIN wins)."""
        domains = [d.strip() for d in CONFIG.get('mail_domains') or [] if d.strip()]
        if self.domain not in domains:
            domains.insert(0, self.domain)
        domains = [d for d in domains if d]
        return random.choice(domains) if domains else self.domain

    def create_account(self) -> bool:
        local = 'zai' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        self.email = f"{local}@{self._pick_domain()}"
        self.password = ''.join(random.choices(
            string.ascii_letters + string.digits + "!@#$%*", k=16))
        return True

    def get_token(self) -> bool:
        return True

    def _find_link(self):
        import imaplib
        import email as _email
        M = imaplib.IMAP4_SSL('imap.gmail.com')
        try:
            M.login(self.user, self.app_pw)
            M.select('INBOX')
            typ, data = M.search(None, 'UNSEEN')
            for i in reversed(data[0].split()):
                try:
                    # BODY.PEEK: do NOT set the Seen flag — a parallel worker
                    # polling the same inbox must still see this message.
                    typ, md = M.fetch(i, '(BODY.PEEK[])')
                    msg = _email.message_from_bytes(md[0][1])
                except Exception:
                    continue
                hdrs = ' '.join(str(msg.get(h, '')) for h in
                                ('To', 'Delivered-To', 'X-Forwarded-To', 'X-Original-To'))
                body = ''
                try:
                    for part in (msg.walk() if msg.is_multipart() else [msg]):
                        if part.get_content_type() in ('text/html', 'text/plain'):
                            body += (part.get_payload(decode=True) or b'').decode('utf-8', 'ignore')
                except Exception:
                    pass
                # recipient check FIRST, before touching anything — a message for
                # another worker's address must stay untouched and unread
                if self.email and (self.email in hdrs or self.email in body):
                    m = re.search(r'https?://[^\s"\'<>]*verify_email[^\s"\'<>]*', body)
                    if m:
                        return m.group(0).replace('&amp;', '&')
        finally:
            try:
                M.logout()
            except Exception:
                pass
        return None

    def _find_link_http(self):
        """Poll the Cloudflare Email Worker's /inbox endpoint."""
        import requests
        import urllib3
        urllib3.disable_warnings()
        try:
            import certifi
            _verify = certifi.where()  # never MITM-able: this request carries inbox_secret
        except Exception:
            _verify = True
        url = self.inbox_url + ('&' if '?' in self.inbox_url else '?') + 'to=' + self.email
        if self.inbox_secret:
            url += '&k=' + self.inbox_secret
        try:
            r = requests.get(url, timeout=15, verify=_verify)
            j = r.json()
        except Exception as e:
            self.logger.debug(f"inbox poll error: {e}")
            return None
        if isinstance(j, dict) and j.get('link'):
            return j['link']
        return None

    def get_verification_link(self, timeout: int = 90):
        if self.inbox_url:
            deadline = time.time() + timeout
            while time.time() < deadline:
                link = self._find_link_http()
                if link:
                    return link
                time.sleep(4)
            return None
        if not (self.user and self.app_pw):
            self.logger.warning("No inbox reader configured (mail_inbox_url or "
                                "ZAI_GMAIL_APP_PW / gen/gmail.json)")
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                link = self._find_link()
                if link:
                    return link
            except Exception as e:
                self.logger.debug(f"IMAP error: {e}")
            time.sleep(5)
        return None


def main():
    global IS_HEADLESS
    print("\n" + "="*50)
    print(f"Z.ai Auto Signup ({CONFIG.get('mail_domain', 'behnamrjd.me')} catch-all)")
    print("="*50)

    kill_stale_browsers()   # clear windows orphaned by a previously killed run

    # Headless flag resolved once, in main()'s scope: signup() uses it for the
    # browser and the invite-claim stage reuses it (was a NameError before —
    # the exception was swallowed and the claim stage silently never ran).
    if env_flag('ZAI_MANUAL'):
        IS_HEADLESS = False
    elif 'ZAI_HEADLESS' in os.environ:
        IS_HEADLESS = env_on('ZAI_HEADLESS')
    else:
        IS_HEADLESS = CONFIG.get('headless', False)

    # ZAI_USE_POOL=1 -> auto-pick a verified proxy from output/proxies.txt at startup.
    if env_on('ZAI_USE_POOL') and not CURRENT_PROXY:
        picked = pick_verified_proxy()
        if picked:
            set_proxy(picked)
            logger.info(f"Using pool proxy: {picked}")
        else:
            # Never silently substitute the user's chosen route: a pool run that
            # falls back to direct burns the home IP AND hides the reason.
            logger.error("POOL MODE: no live pool proxy found — aborting "
                         "(run proxy_pool.py verify, or switch IP source)")
            return

    mail = CatchAllMail(logger_instance=logger)
    if not mail.create_account():
        print("Failed to create mail account")
        return

    email = mail.email
    password = mail.password

    if not mail.get_token():
        print("Failed to init mail reader")
        return

    count = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() \
        else CONFIG.get('count', 1)

    logger.info(f"Email: {email}")
    # full password no longer logged — output.txt is a shared log and ends up
    # in captures/HARs; the password is already in ACC_FILE for the operator
    logger.info(f"Password: {'*' * len(password)}")
    logger.info(f"Count: {count}")
    logger.info(f"Headless: {CONFIG.get('headless', False)}")
    logger.info("="*50)

    success = 0
    verification_needed = 0
    starter_count = 0
    skipped = 0

    # Retry count + routing mode resolved ONCE, before the loop — the panel
    # sends ZAI_PROXY_RETRIES and it must govern the retry loop below.
    _retries_env = os.environ.get('ZAI_PROXY_RETRIES')
    if _retries_env and _retries_env.isdigit():
        CONFIG['proxy_retries'] = int(_retries_env)
    # Pinned proxy (ZAI_PROXY set explicitly by the user/panel) never rotates:
    # the user chose that exact route; rotating would silently replace it.
    pinned_mode = bool(CURRENT_PROXY) and not env_on('ZAI_USE_POOL')

    for i in range(count):
        logger.info(f"\nAccount {i+1}/{count}")

        # Fresh exit IP per account — the Starter gate is IP-based, so each signup
        # must come from a different proxy. Never in pinned mode.
        if env_on('ZAI_USE_POOL') and (i > 0 or not CURRENT_PROXY):
            newp = pick_verified_proxy()
            if newp:
                set_proxy(newp)
                logger.info(f"Account {i+1} proxy: {newp}")
            else:
                logger.error(f"Account {i+1}: POOL MODE — no live proxy, aborting "
                             f"(run proxy_pool.py verify)")
                break

        if count > 1 and i > 0:
            mail = CatchAllMail(logger_instance=logger)
            if not mail.create_account():
                logger.error("Failed to create new mail address")
                continue
            email = mail.email
            password = mail.password
            logger.info(f"Email: {email}")

        # Auto-retry: a failure (dead proxy, blocked IP, widget-not-loaded) retries
        # with a fresh email. Pool mode ROTATES to a new proxy each retry; pinned
        # and direct modes retry on the SAME route (retries must stay enabled —
        # disabling them on pin was a bug: the route is chosen, not the effort).
        max_proxy_retries = CONFIG.get('proxy_retries', 3)
        rotate = bool(CURRENT_PROXY) and not pinned_mode   # pool: new IP per retry
        result = None
        for pr in range(max_proxy_retries + 1):
            result = signup(email, password, mail)
            retryable = (result is None) or (result == 'CAPTCHA_NOT_LOADED')
            if not retryable or pr >= max_proxy_retries:
                break
            newp = None
            if rotate:
                newp = pick_verified_proxy()
                if not newp:
                    logger.warning("No live proxy available — stopping retries")
                    break
                set_proxy(newp)
            mail = CatchAllMail(logger_instance=logger)
            if not mail.create_account():
                break
            email, password = mail.email, mail.password
            route = f"proxy={newp}" if rotate else f"same route ({CURRENT_PROXY or 'direct'})"
            logger.info(f"  retry {pr + 1}/{max_proxy_retries} — "
                        f"{route} email={email}")

        if result == 'CAPTCHA_NOT_LOADED':
            skipped += 1
            logger.warning(f"Account {i+1} SKIPPED — captcha widget never loaded "
                           f"(IP-blocked). Change IP and retry.")
            if i < count - 1:
                time.sleep(2)
            continue

        if result:
            if isinstance(result, dict) and result.get('needs_verification'):
                verification_needed += 1
                with open(ACC_FILE, 'a') as f:
                    f.write(f"{email}:{password}:VERIFICATION_NEEDED\n")
                logger.info(f"Account {i+1} needs verification: {email}")
            else:
                success += 1
                token = result if isinstance(result, str) else "SUCCESS"
                token_display = token[:50] + "..." if len(token) > 50 else token

                # Plan check: a created account is only worth keeping if it actually
                # got the Starter plan (granted to only some signups).
                plan, starter = None, False
                if isinstance(result, str):
                    try:
                        from plan_check import check_account_plan
                        try:
                            plan = check_account_plan(token, log=logger.info)
                        except Exception as e:
                            # The plan is tied to the ACCOUNT, not the IP, so if the
                            # proxy died mid-check just retry directly.
                            logger.warning(f"plan check via proxy failed ({type(e).__name__}) "
                                           f"— retrying direct")
                            saved = os.environ.pop('ZAI_PROXY', None)
                            try:
                                plan = check_account_plan(token, log=logger.info)
                            finally:
                                if saved:
                                    os.environ['ZAI_PROXY'] = saved
                        starter = bool(plan and plan.get('starter'))
                        if plan:
                            logger.info(f"Plan: {plan.get('plans')} starter={starter}")
                    except Exception as e:
                        logger.warning(f"plan check failed: {e}")

                with open(ACC_FILE, 'a') as f:
                    f.write(f"{email}:{password}:{token}\n")

                # Invite-referral Global Build (100M GLM-5.3-Flash):
                # device login FIRST, then bind, then message (proven order).
                # Only claim accounts that actually got Starter — client chat
                # needs a plan, and the bind is a one-shot per account.
                # ZAI_INVITE_CODE set to empty string = explicitly OFF; absent
                # or non-empty = use it (fall back to CONFIG).
                if 'ZAI_INVITE_CODE' in os.environ:
                    invite_code = os.environ.get('ZAI_INVITE_CODE', '')
                else:
                    invite_code = CONFIG.get('invite_code')
                global_build = False
                if invite_code and starter and isinstance(result, str):
                    try:
                        from invite_claim import device_login, claim_invite
                        zjwt, _ = device_login(token, log=logger.info)
                        if zjwt:
                            inv = claim_invite(token, invite_code, log=logger.info,
                                               zjwt=zjwt, headless=IS_HEADLESS,
                                               captcha_param=captcha_param)
                            s = inv.get('summary') or {}
                            global_build = bool(s.get('issued'))
                    except Exception as e:
                        logger.warning(f"invite claim failed: {e}")

                with open(PLANS_FILE, 'a') as f:
                    f.write(json.dumps({
                        'email': email, 'starter': starter,
                        'plans': plan.get('plans') if plan else None,
                        'global_build': global_build,
                        'ts': int(time.time()),
                    }) + "\n")
                if starter:
                    starter_count += 1
                    with open(STARTER_FILE, 'a') as f:
                        f.write(f"{email}:{password}:{token}\n")
                if global_build:
                    with open(os.path.join(OUTPUT_DIR, 'starter100m.txt'), 'a', encoding='utf-8') as f:
                        f.write(f"{email}:{password}:{token}\n")
                    logger.info(f"Account {i+1} 100M Global Build active -> {STARTER_FILE}100m")

                logger.info(f"Account {i+1} created with token: {token_display}")

        if i < count - 1:
            time.sleep(2)

    logger.info(f"\n{success} fully created ({starter_count} with Starter plan), "
                f"{verification_needed} need verification")
    print(f"\n{success} fully created")
    print(f"{starter_count} with Starter plan  ->  {STARTER_FILE}")
    if skipped:
        print(f"{skipped} skipped (captcha widget never loaded — IP-blocked)")
    print(f"{verification_needed} need email verification")
    print(f"{ACC_FILE}")
    print(f"{_log_file}")
    print(timing.summary())


if __name__ == "__main__":
    main()
