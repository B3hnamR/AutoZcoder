import logging, os
import shutil
import urllib.parse
# silence PIL/pngjs chunk-stream debug spam
logging.getLogger('PIL').setLevel(logging.WARNING)
logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
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
import traceback
from pathlib import Path
from playwright.sync_api import sync_playwright

from utils import BROWSER_ARGS, SELECTORS, take_screenshot, timing
import fingerprint
from captcha_solver import CaptchaSolver
from config import CONFIG

# Where results land. ZAI_OUTPUT_DIR lets the gateway redirect worker output
# into its portable folder; source-checkout defaults come from CONFIG.
OUTPUT_DIR = os.environ.get('ZAI_OUTPUT_DIR') or CONFIG.get('output_dir', 'output')
os.makedirs(OUTPUT_DIR, exist_ok=True)   # log handler writes inside OUTPUT_DIR

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)

class _SecretFilter(logging.Filter):
    """Sanitize the rendered record, including third-party args and tracebacks."""

    def __init__(self):
        super().__init__()
        self.secrets = set()

    def remember(self, value):
        if isinstance(value, str) and value:
            self.secrets.add(value)

    def sanitize(self, text):
        secrets = self.secrets | {
            value for key, value in os.environ.items() if value
            and re.search(r'(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APP_PW)', key, re.I)
        }
        for secret in sorted(secrets, key=len, reverse=True):
            for value in {secret, urllib.parse.quote(secret, safe=''),
                          urllib.parse.quote_plus(secret)}:
                text = text.replace(value, '[REDACTED]')
        # Relative request URLs are logged by HTTP clients as well as full URLs.
        text = re.sub(r'([?&](?:amp;)?[^\s?&#=\'"<>]+)=([^\s&#\'"<>]*)',
                      r'\1=[REDACTED]', text)
        text = re.sub(r'(?i)(\bbearer\s+)[^\s\'",;<>]+', r'\1[REDACTED]', text)
        text = re.sub(r'\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*)?',
                      '[REDACTED]', text)
        text = re.sub(
            r'(?i)([\'\"]?(?:password|passwd|token|access_token|refresh_token|'
            r'securityToken|captcha_verify_param|api_key|secret)[\'\"]?\s*[:=]\s*)'
            r'(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|[^\s,;}]+)',
            r'\1[REDACTED]', text)
        return re.sub(r'(://)[^\s/@]+@', r'\1[REDACTED]@', text)

    def filter(self, record):
        record.msg = self.sanitize(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = self.sanitize(''.join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.sanitize(record.exc_text)
        if record.stack_info:
            record.stack_info = self.sanitize(record.stack_info)
        return True


_secret_filter = _SecretFilter()

# stdout under the packaged exe is a cp1252 pipe: z.ai's Chinese error
# messages (SIGNUP_RATE_LIMITED etc.) crashed the handler with
# UnicodeEncodeError and spewed tracebacks between retries. Reconfigure the
# stream to tolerate anything (errors are replaced, logging never dies).
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

class _SafeConsoleHandler(logging.StreamHandler):
    """StreamHandler that can never crash on unencodable text."""

    def emit(self, record):
        try:
            msg = self.format(record)
            try:
                self.stream.write(msg + self.terminator)
            except UnicodeEncodeError:
                # last resort: ASCII with escapes — a mangled line beats a
                # dead logger and a lost message
                self.stream.write(msg.encode('ascii', 'backslashreplace')
                                      .decode('ascii') + self.terminator)
        except Exception:
            self.handleError(record)

console_handler = _SafeConsoleHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%H:%M:%S'))

# The gateway sets ZAI_LOG_FILE per worker (a dedicated file under
# <data>/logs/workers/); standalone runs keep the shared output.txt but under
# the sorted logs/ folder.
_log_file = (os.environ.get('ZAI_LOG_FILE')
             or os.path.join(OUTPUT_DIR, 'logs', 'output.txt'))
os.makedirs(os.path.dirname(_log_file) or '.', exist_ok=True)
main_log_handler = logging.FileHandler(_log_file, encoding='utf-8')
main_log_handler.setLevel(logging.DEBUG)
main_log_handler.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%H:%M:%S'))

root_logger.handlers = [console_handler, main_log_handler]
for _handler in root_logger.handlers:
    _handler.addFilter(_secret_filter)

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
# Sorted layout (v1.0.5): results/ (accounts+plans+starter), profiles/ (chromium),
# screenshots/ (debug png), logs/ (worker solve logs) — all under OUTPUT_DIR.
OUT_SUFFIX = os.environ.get('ZAI_OUT_SUFFIX', '')
RESULTS_DIR = os.path.join(OUTPUT_DIR, 'results')
PROFILES_DIR = os.path.join(OUTPUT_DIR, 'profiles')
SHOTS_DIR = os.path.join(OUTPUT_DIR, 'screenshots')
LOGS_DIR = os.path.join(OUTPUT_DIR, 'logs')
for _d in (RESULTS_DIR, PROFILES_DIR, SHOTS_DIR, LOGS_DIR):
    os.makedirs(_d, exist_ok=True)
ACC_FILE = os.path.join(RESULTS_DIR, f'accounts{OUT_SUFFIX}.txt')
PLANS_FILE = os.path.join(RESULTS_DIR, f'plans{OUT_SUFFIX}.jsonl')
STARTER_FILE = os.path.join(RESULTS_DIR, f'starter{OUT_SUFFIX}.txt')

def _save_recovery_account(email, password, state):
    """Credentials belong only in the private recovery file, never the logs."""
    path = Path(OUTPUT_DIR) / 'recovery_accounts.txt'
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        if os.name != 'nt':
            os.fchmod(stream.fileno(), 0o600)
        stream.write(f'{email}:{password}:{state}\n')

# Dedicated chromium profile per process: lets us identify (and kill) browsers left
# behind by a killed run without ever touching the user's own Chrome.
PROFILE_DIR = os.path.join(PROFILES_DIR, '.pw-profile' + OUT_SUFFIX)
LAST_CAPTCHA_PARAM = None


def env_on(name):
    """Routing flags: '1' = on; '0'/''/absent = off (the panel sends explicit
    '0' to defeat parent-shell inheritance)."""
    return os.environ.get(name, '').strip() == '1'


def _auto_result(category='failed', email=None):
    """Diagnostics only; the manager verifies billing and registration itself."""
    messages = {
        'mail_error': 'Mail setup or verification failed.',
        'mail_timeout': 'Account created, but no verification email arrived.',
        'captcha_failed': 'Captcha verification did not complete.',
        'browser_failed': 'Browser setup or signup page interaction failed.',
        'finish_signup_failed': 'Signup finalization failed without an account token; see private recovery output.',
        'pool_empty': 'No verified proxy is available.',
        'rate_limited': 'Signup was rate limited.',
        'no_plan': 'Account created without a confirmed Start Plan.',
        'generated': 'Account created with a confirmed Start Plan.',
        'failed': 'Worker did not complete account generation.',
    }
    if category not in messages:
        category = 'failed'
    result = {'category': category, 'message': messages[category]}
    if email:
        result['email'] = email
    return result


def _publish_auto_result(result):
    # Rebuild from the allowlist: never serialize tokens, passwords or exceptions.
    safe = _auto_result(result.get('category'), result.get('email'))
    print('AUTO_GENERATE_RESULT ' + json.dumps(safe, ensure_ascii=True), flush=True)


def _save_auto_route_metadata():
    """Write credentials only to the parent's expected private handoff file."""
    target = os.environ.get('ZAI_AUTO_ROUTE_FILE')
    if not target:
        return
    from pathlib import Path
    import tempfile
    from urllib.parse import urlsplit

    path = Path(target)
    if (not path.is_absolute() or path.parent.name != 'auto-private'
            or not path.parent.is_dir() or path.parent.is_symlink() or path.is_symlink()):
        raise ValueError('Invalid private route metadata path')
    policy = os.environ.get('ZAI_ROUTE_POLICY', '').strip().lower()
    proxy = CURRENT_PROXY or ''
    valid = policy == 'direct' and not proxy
    if policy == 'proxy' and isinstance(proxy, str):
        try:
            parsed = urlsplit(proxy)
            valid = (parsed.scheme in ('http', 'https', 'socks4', 'socks4a', 'socks5', 'socks5h')
                     and parsed.hostname and parsed.port
                     and not any(c.isspace() for c in proxy)
                     and parsed.path in ('', '/') and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
    if not valid:
        raise ValueError('Invalid private worker route')
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        # mkstemp creates mode 0600 on POSIX; Windows inherits the private
        # directory ACL configured by the parent.
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'proxy': proxy}, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _account_profile(email):
    name = re.sub(r'[^a-zA-Z0-9]', '_', email.split('@')[0])[:24]
    if env_on('ZAI_AUTO_GENERATE'):
        import hashlib
        worker = os.environ.get('ZAI_WORKER_ID') or str(os.getpid())
        # Keep a readable worker prefix without allowing path characters or
        # sanitized/truncated worker IDs to collide.
        prefix = re.sub(r'[^a-zA-Z0-9_-]', '_', worker)[:40]
        digest = hashlib.sha256(worker.encode('utf-8')).hexdigest()[:16]
        name = f'{prefix}-{digest}-{name}'
    return os.path.join(PROFILES_DIR, '.pw-profile-' + name)


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


def _redact_proxy(url):
    """Never log a proxy's login (user:pass@) — logs are shared and end up in captures."""
    return re.sub(r'//[^/@]*@', '//', url) if url else url


def notify_gateway(category: str, message: str, level: str = 'info') -> None:
    """Push an event to the gateway's notification bell (fire-and-forget).

    Workers are subprocesses; the only channel back is HTTP with the per-run
    internal token. Never raises, never blocks the signup flow for long."""
    try:
        import requests
        base = os.environ.get('ZAI_GATEWAY_URL') or 'http://127.0.0.1:8788'
        internal = os.environ.get('ZAI_GATEWAY_INTERNAL_TOKEN', '')
        if not internal:
            return
        requests.post(f'{base}/admin/dash/notifications/emit',
                      json={'category': category, 'message': message,
                            'level': level},
                      headers={'Authorization': f'Bearer {internal}'},
                      timeout=4)
    except Exception:
        pass


def _pool_has_live_proxy():
    """True when the pool has any entry that could serve as a fresh exit IP.
    Used by the SIGNUP_IP_LIMITED logic: retrying a poisoned route without
    one is pure email-burning. An entry with a recent ok check counts;
    pick_verified re-tests before use anyway."""
    try:
        import proxy_pool
        pool = proxy_pool._load_pool_map()
        if not pool:
            return False
        now = time.time()
        for e in pool.values():
            last_ok = float(e.get('last_ok') or 0)
            if last_ok <= 0 or now - last_ok < 3600:
                return True     # never checked (worth a pick) or recently ok
        return False
    except Exception:
        return False


def pick_verified_proxy():
    """Verify-on-pick: grab a pool entry and re-test it before use.
    ZAI_SOCKS_ONLY=1: socks4/5 entries only (residential-style — they pass the
    Starter gate; HTTP datacenter ranges fail it).
    ZAI_PROXY_TYPES: comma-separated scheme filter (http,https,socks4,socks5)
    — overrides/refines ZAI_SOCKS_ONLY ('all' or empty = every type).
    ZAI_PROXY_COUNTRY: filter by 2-letter country code (e.g. US)."""
    try:
        import proxy_pool
        types = [t.strip().lower() for t in
                 os.environ.get('ZAI_PROXY_TYPES', '').split(',') if t.strip()]
        if types == ['all']:
            types = []
        return proxy_pool.pick_verified(
            socks=env_on('ZAI_SOCKS_ONLY') and not types,
            country=os.environ.get('ZAI_PROXY_COUNTRY'),
            types=types or None,
        )
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

    Profiles are now PER-ACCOUNT (.pw-profile-<email-token> under OUTPUT_DIR),
    so match on the shared prefix instead of one worker's exact dir — scoped
    to OUTPUT_DIR so the user's own Chrome and other apps are never touched.
    """
    pat = re.escape(os.path.join(OUTPUT_DIR, '.pw-profile')) + r'[^"]*("|\s|$)'
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
# `#aliyunCaptcha-verify-param` is EMPTY in this mode.
#
# This hook is a FALLBACK. The authoritative token is the one the app itself
# puts into `POST /api/v1/auths/signup`, which signup() reads from the
# intercepted request body. The scene id is fetched per run from
# `/api/v1/client/configs` (see fetch_captcha_config); it is never hardcoded,
# because a rotated scene produces a token the server rejects.
#
# Accept both response casings and both success codes: the SDK answers either
# `Result`/`VerifyResult: true` or `result`/`VerifyCode: "T006"` depending on
# whether the challenge ran interactive or traceless.
def _captcha_verify_hook(scene: str) -> str:
    return r"""
(function () {
  var SCENE = __SCENE__;
  var PASS_CODES = ['T001', 'T006'];
  function b64(s) { try { return btoa(unescape(encodeURIComponent(s))); } catch (e) { return null; } }
  function capture(txt) {
    try {
      if (!txt || txt.indexOf('securityToken') === -1) return;
      var j = JSON.parse(txt);
      var r = j && (j.Result || j.result);
      if (!r || !r.securityToken) return;
      var code = r.VerifyCode || r.verifyCode || '';
      // Only a VERDICT of true is success material. Aliyun can return a
      // securityToken on failed verifies too — stashing that produced
      // false "success:hook" signals after a failed slide.
      if (r.VerifyResult === true || PASS_CODES.indexOf(code) !== -1) {
        window.__verifyResp = r;
        if (!SCENE || !r.certifyId) return;
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
""".replace('__SCENE__', json.dumps(scene or ''))


def _signup_payload(name, email, password, captcha_param):
    """JSON body for POST /api/v1/auths/signup, matching the web client.

    The avatar must be the same data URI that finish_signup uses; the relative
    `/user.png` path the client never sends is not accepted everywhere.
    """
    return {
        'name': name,
        'email': email,
        'password': password,
        'profile_image_url': AVATAR_DATA_URI,
        'sso_redirect': None,
        'captcha_verify_param': captcha_param,
    }


def fetch_captcha_config():
    """Live Aliyun scene/prefix/region from `/api/v1/client/configs`.

    Reuses claim_start's transport so the request leaves through the same exit
    IP as the signup. Returns {} when unavailable; the caller then trusts only
    the app's own captured parameter.
    """
    try:
        from claim_start import _captcha_config
        return _captcha_config(logger.info) or {}
    except Exception as e:
        logger.warning(f"captcha config unavailable ({type(e).__name__}) — "
                       f"relying on the app's own verify param")
        return {}


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


def _rm_profile(profile_dir):
    """Wipe a per-account browser profile. Keeps the generator-data dir from
    accumulating one chromium profile per signup; virgin storage per account
    is the point of per-account profiles in the first place."""
    if profile_dir:
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception:
            pass


def _enter_signup_form(page, timeout=20000):
    """Confirm the SPA switched modes; click completion is not a transition."""
    name = page.locator('input[name="name"]:visible, input[autocomplete="name"]:visible, '
                        'input[placeholder="Enter Your Full Name"]:visible').first
    if name.count() and name.is_visible():
        return name
    for attempt in range(2):
        # Deployments render this toggle as either a button or a link. Exact
        # role/name matching avoids accidentally submitting the Sign in form.
        toggle = page.locator('button:visible, a:visible').filter(
            has_text=re.compile(r'^\s*sign\s*up\s*$', re.I)).first
        toggle.wait_for(state='visible', timeout=timeout)
        toggle.click(timeout=timeout)
        try:
            name.wait_for(state='visible', timeout=timeout)
            return name
        except Exception:
            if attempt:
                raise RuntimeError('Signup form did not appear after Sign up; still on login or page transition failed') from None
            logger.warning('Sign up click did not reveal the signup form; retrying the transition once')


def signup(email, password, mail=None):
    _secret_filter.remember(password)
    logger.info(f"Starting: {email}")
    timing.start('total_signup')

    try:
        with sync_playwright() as p:
            # Persistent context (playwright refuses --user-data-dir via launch()).
            # Profile dir is PER-ACCOUNT (virgin storage for every signup) and
            # removed on exit; kill_stale_browsers() still reaps orphans via
            # the exact-arg command-line match below.
            is_manual = env_flag('ZAI_MANUAL')
            if is_manual:
                is_headless = False
            elif 'ZAI_HEADLESS' in os.environ:
                is_headless = env_on('ZAI_HEADLESS')
            else:
                is_headless = CONFIG.get('headless', False)

            ident = fingerprint.make_identity(
                proxy_url=(CURRENT_PROXY if CURRENT_PROXY and CURRENT_PROXY != '0' else None))
            logger.info(f"    [fp] {ident['chrome_major']} · {ident['viewport']['width']}x{ident['viewport']['height']} · "
                        f"{ident['hardware_concurrency']}c/{ident['device_memory']}G · "
                        f"cc={ident['country']} · tz={ident['timezone_id']} · locale={ident['locale']}")

            # The Aliyun scene is server-issued and rotates. Read it through the
            # same route as the browser before the page loads, so the fallback
            # token is built for the scene the widget will actually use.
            captcha_cfg = fetch_captcha_config()
            captcha_scene = str(captcha_cfg.get('sceneId') or '')
            logger.info(f"    [captcha] scene={captcha_scene or 'unavailable'} "
                        f"region={captcha_cfg.get('region') or '?'}")

            account_profile = _account_profile(email)
            try:
                shutil.rmtree(account_profile, ignore_errors=True)
            except Exception:
                pass

            context = p.chromium.launch_persistent_context(
                account_profile,
                headless=is_headless,
                args=BROWSER_ARGS,
                proxy=pw_proxy(),
                viewport=ident['viewport'],
                user_agent=ident['user_agent'],
                locale=ident['locale'],
                timezone_id=ident['timezone_id'],
            )
            browser = context   # `browser.close()` below closes the persistent context

            page = context.new_page()
            page.add_init_script(fingerprint.base_script(ident))
            page.add_init_script(fingerprint.noise_script(ident))
            page.add_init_script(_captcha_verify_hook(captcha_scene))

            # Capture the REAL auth token from the signup response's set-cookie.
            # Reading localStorage.token is wrong: it still holds the guest session
            # JWT minted on page load, which produced a false "account created".
            # Also keep the request body of the app's own signup POST: the
            # `captcha_verify_param` in it is the authoritative, single-use token.
            captured = {'token': None, 'status': None, 'body': None, 'calls': [],
                        'verify_param': None, 'device_id': None}

            def _capture_auth(resp):
                try:
                    url = resp.url
                    if '/api/v1/auths' in url:
                        path = url.split('?')[0]
                        try:
                            method = resp.request.method
                        except Exception:
                            method = '?'
                        try:
                            did = (resp.request.headers or {}).get('x-device-id')
                            if did and not captured['device_id']:
                                captured['device_id'] = did
                        except Exception:
                            pass
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
                            try:
                                body = resp.request.post_data_json or {}
                            except Exception:
                                body = {}
                            token = str((body or {}).get('captcha_verify_param') or '')
                            if token:
                                captured['verify_param'] = token
                                logger.info("    [captcha] captured the app's "
                                            "captcha_verify_param")
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
                name_field = _enter_signup_form(page)

                name = generate_human_name()
                logger.info(f"Using name: {name}")
                name_field.fill(name)
                # Changing SPA mode can remount/reset the credential controls.
                _fill_when_ready(SELECTORS['email_input'], email)
                _fill_when_ready(SELECTORS['password_input'], password)

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

                # strategy: ai | classic | auto (manual handled above).
                # Mirror into CONFIG so CaptchaSolver/DistanceCalculator see it.
                _strategy = os.environ.get('ZAI_CAPTCHA_STRATEGY', '').lower()
                if _strategy in ('ai', 'classic', 'auto'):
                    CONFIG['captcha_strategy'] = _strategy
                strategy = CONFIG.get('captcha_strategy', 'auto')
                if not manual:
                    logger.info(f"Captcha strategy: {strategy}")

                if not slider_ok and not manual:
                    # The widget never rendered (page shows "Please complete the
                    # verification first"). In practice this is an IP/edge block: Aliyun
                    # refuses to serve the challenge. Grinding 15 attempts is pointless
                    # and only burns risk score — skip this run.
                    logger.warning("Captcha widget did not load (IP-blocked?) — skipping run")
                    take_screenshot(page, "captcha_not_loaded")
                    browser.close()
                    _rm_profile(account_profile)
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
                    _rm_profile(account_profile)
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
                        _rm_profile(account_profile)
                        return 'CAPTCHA_FAILED' if env_on('ZAI_AUTO_GENERATE') else None

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
                def _signup_fired():
                    return any(m == 'POST' and p.endswith('/auths/signup')
                               for (m, p, s) in captured['calls'])

                # Preference: the app's own request body > our hook's rebuilt
                # token > the element value. A rebuilt token is only as good as
                # the scene it was built for, so it never beats the real one.
                captcha_param = captured['verify_param']
                if not captcha_param:
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

                # A token in hand is not permission to submit: the SPA posts its
                # own signup ~15s after the slide. Posting while we have a token
                # produced two signup POSTs for one email (observed live), which
                # trips the per-email rate limit and can half-create the account.
                # Always give the app its window first; only POST if it never fires.
                if not _signup_fired():
                    deadline = time.time() + 20
                    logger.info("Waiting up to 20s for the app's own signup POST")
                    while time.time() < deadline:
                        if _signup_fired():
                            break
                        if not captcha_param:
                            captcha_param = captured['verify_param']
                        time.sleep(0.25)
                    captcha_param = captcha_param or captured['verify_param']

                global LAST_CAPTCHA_PARAM
                LAST_CAPTCHA_PARAM = captcha_param
                logger.info(f"captcha_verify_param: {'captured' if captcha_param else 'MISSING'}")

                def _page_post(path, payload, extra_headers=None):
                    return page.evaluate("""
                        async (a) => {
                            try {
                                const r = await fetch(a.path, {
                                    method: 'POST',
                                    headers: Object.assign(
                                        {'Content-Type': 'application/json'}, a.headers || {}),
                                    body: JSON.stringify(a.body),
                                });
                                let t = '';
                                try { t = await r.text(); } catch (e) {}
                                return { status: r.status, body: t };
                            } catch (e) { return { status: -1, body: String(e) }; }
                        }
                    """, {'path': path, 'body': payload, 'headers': extra_headers or {}})

                # The app's own requests carry a device id; a bare fetch does not.
                # Reuse the value the app already sent on this session.
                device_id = captured['device_id']
                signup_headers = {'x-region': os.environ.get('ZAI_X_REGION', 'overseas')}
                if device_id:
                    signup_headers['x-device-id'] = device_id
                finish_headers = dict(signup_headers)

                username = name
                signup_status = None
                app_fired = _signup_fired()
                if app_fired:
                    # The app already submitted on captcha pass. Re-POSTing would double
                    # submit and trip the 45s per-email rate limit.
                    signup_status = captured['status'] or 200
                    logger.info(f"App already submitted signup (status={signup_status}) "
                                f"— not re-POSTing")
                elif captcha_param:
                    r = _page_post('/api/v1/auths/signup',
                                   _signup_payload(name, email, password, captcha_param),
                                   signup_headers)
                    signup_status = r['status']
                    logger.info(f"signup: status={r['status']} body={str(r['body'])[:300]}")
                else:
                    logger.warning("No captcha_verify_param — cannot POST signup")

                # --- Direct-API verify + finish --------------------------------------
                verify_token = None
                verify_link_bad = False
                if signup_status in (200, 201) and mail:
                    logger.info("Waiting for the verification email...")
                    link = mail.get_verification_link(timeout=90)
                    if link:
                        logger.info("Verification link received")
                        # capture the FULL token value (any chars until & or
                        # end) — the old hex-only regex silently TRUNCATED
                        # tokens containing other characters, and the
                        # truncated token 400'd at verify_email while the
                        # account already existed on z.ai (unrecoverable: the
                        # password was never written anywhere).
                        m = re.search(r'[?&]token=([^&\s"\'<>]+)', link)
                        if m:
                            verify_token = m.group(1)
                            _secret_filter.remember(verify_token)
                            if not re.fullmatch(r'verify-[0-9a-fA-F]+', verify_token):
                                logger.warning(
                                    f"verify token has an unexpected shape "
                                    f"(len={len(verify_token)}) — trying it as-is")
                        else:
                            verify_link_bad = True
                            logger.error(
                                "verification link carries no token= parameter "
                                "— link shape changed?")
                    else:
                        logger.warning("No verification email arrived")
                elif signup_status not in (200, 201):
                    logger.warning(f"signup not OK (status={signup_status}) — skipping verify")

                if signup_status in (200, 201) and not verify_token:
                    # The account EXISTS on z.ai but we cannot finish it
                    # (no mail / bad link). Retrying burns a fresh email AND
                    # proxy while the created account is orphaned forever —
                    # its password exists nowhere. Persist credentials for
                    # manual recovery and make this a HARD failure.
                    try:
                        _save_recovery_account(email, password,
                                               'unverified|bad_link' if verify_link_bad
                                               else 'unverified|no_link')
                        logger.error(
                            f"account created but unverified — credentials "
                            f"appended to recovery_accounts.txt for manual recovery; "
                            f"NOT retrying (a retry cannot verify this account)")
                    except OSError:
                        logger.error("account created but unverified, and writing "
                                     "the recovery file failed", exc_info=True)
                    notify_gateway('generator',
                                   f"signup unverified (no verify mail) — account "
                                   f"saved to recovery_accounts.txt",
                                   level='warning')
                    browser.close()
                    _rm_profile(account_profile)
                    return ('MAIL_TIMEOUT' if mail and not verify_link_bad
                            and env_on('ZAI_AUTO_GENERATE') else 'SIGNUP_UNVERIFIED')

                finish_token = None
                finish_status = None
                verify_status = None
                finish_rate_limited = None   # (retry_after_s, scope) when IP-blocked
                if verify_token:
                    # verify_email deliberately omits x-device-id, matching the
                    # app: the token, not the device, authorizes this step.
                    r = _page_post('/api/v1/auths/verify_email',
                                   {'username': username, 'email': email, 'token': verify_token},
                                   {'x-region': os.environ.get('ZAI_X_REGION', 'overseas')})
                    verify_status = r['status']
                    logger.info(f"verify_email: status={r['status']} body={str(r['body'])[:200]}")
                    # finish_signup returns the session token in its body:
                    #   {"success":true,"user":{"token":"eyJ...","email":"..."}}
                    # The manual capture used a data: URI avatar, so match that.
                    r = _page_post('/api/v1/auths/finish_signup',
                                   {'username': username, 'email': email, 'token': verify_token,
                                    'password': password,
                                    'profile_image_url': AVATAR_DATA_URI,
                                    'sso_redirect': None},
                                   finish_headers)
                    finish_status = r['status']
                    logger.info(f"finish_signup: status={r['status']} body={str(r['body'])[:200]}")
                    try:
                        fj = json.loads(r['body'])
                        finish_token = (fj.get('user') or {}).get('token')
                        if finish_token:
                            logger.info(f"finish_signup issued token "
                                        f"(email={token_email(finish_token)})")
                        # 429 SIGNUP_RATE_LIMITED: the body carries the whole story —
                        # {"detail":{"code":"SIGNUP_RATE_LIMITED","retriable":true,
                        #  "retry_after_seconds":36505,"scope":"signup:ip","message":"…"}}
                        # Captured live 2026-10-22: the worker retried the SAME direct
                        # route 5 times against a 10-HOUR IP lockout, burning an email
                        # + captcha + verify cycle per retry for nothing.
                        det = (fj.get('detail') or {})
                        if r['status'] == 429 and det.get('code') == 'SIGNUP_RATE_LIMITED':
                            finish_rate_limited = (
                                int(det.get('retry_after_seconds') or 0),
                                str(det.get('scope') or ''))
                    except Exception:
                        pass

                # --- Extract the real session token --------------------------------
                # finish_signup returns it directly; fall back to a non-guest cookie or
                # localStorage (which may still hold the pre-signup guest JWT). The
                # fallback token's email MUST match the account we just created — a
                # persistent profile can hold a previous identity's session.
                token = finish_token
                if token and (is_guest_token(token) or token_email(token) != email):
                    token = None
                if not token:
                    try:
                        for c in context.cookies('https://chat.z.ai'):
                            if c.get('name') == 'token':
                                v = c.get('value', '')
                                if v and not is_guest_token(v) and token_email(v) == email:
                                    token = v
                    except Exception:
                        logger.debug('Could not read fallback session cookies')
                if not token:
                    try:
                        lt = page.evaluate(
                            "() => localStorage.getItem('token') || sessionStorage.getItem('token')")
                        if lt and not is_guest_token(lt) and token_email(lt) == email:
                            token = lt
                    except Exception:
                        logger.debug('Could not read fallback browser session storage')

                if token:
                    logger.info(f"Account token email={token_email(token)}")
                    browser.close()
                    _rm_profile(account_profile)
                    return token

                logger.warning("No non-guest token obtained — account not confirmed")
                logger.info(f"    auth calls seen: {captured['calls']}")
                try:
                    browser.close()
                except Exception:
                    pass
                _rm_profile(account_profile)
                if finish_rate_limited:
                    # The account EXISTS and its email is VERIFIED — only the final
                    # finish_signup call was IP-rate-limited. This is recoverable:
                    # re-running finish_signup with these credentials after the
                    # window (or from a different IP) completes the account.
                    retry_after_s, scope = finish_rate_limited
                    try:
                        _save_recovery_account(
                            email, password, f'finish_blocked|retry_after={retry_after_s}'
                            f'|scope={scope or "unknown"}')
                    except OSError:
                        logger.error("writing the recovery file failed", exc_info=True)
                    logger.error(
                        f"finish_signup IP-rate-limited (scope={scope or 'unknown'}, "
                        f"retry in ~{retry_after_s // 3600}h) — account saved to "
                        f"recovery_accounts.txt; NOT retrying on this route")
                    notify_gateway('generator',
                                   f"signup IP-rate-limited (scope={scope or 'unknown'}, "
                                   f"~{retry_after_s // 3600}h lockout) — account saved "
                                   f"to recovery, route switched or stopped",
                                   level='warning')
                    return 'SIGNUP_IP_LIMITED'
                if finish_status is not None:
                    try:
                        verification = ('verified' if verify_status == 200
                                        else 'verification_unconfirmed')
                        _save_recovery_account(email, password,
                                               f'finish_failed|{verification}'
                                               f'|http_status={finish_status}')
                    except OSError:
                        logger.error('Writing the recovery file failed', exc_info=True)
                    logger.error('finish_signup failed (HTTP %s), no account token; '
                                 'credentials saved to recovery_accounts.txt if writable; '
                                 'NOT retrying', finish_status)
                    return 'SIGNUP_FINISH_FAILED'
                return None

            except Exception as e:
                logger.error(f"Error: {e}")
                take_screenshot(page, "error")
                try:
                    browser.close()
                except:
                    pass
                _rm_profile(account_profile)
                return 'BROWSER_FAILED' if env_on('ZAI_AUTO_GENERATE') else None

    except Exception as e:
        logger.error(f"Browser launch error: {e}")
        return 'BROWSER_FAILED' if env_on('ZAI_AUTO_GENERATE') else None


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
                       or CONFIG.get('mail_domain', ''))
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
        for value in (self.app_pw, self.inbox_secret):
            _secret_filter.remember(value)

    def _pick_domain(self) -> str:
        """Rotation across the user's configured domains (ZAI_MAIL_DOMAIN wins)."""
        domains = [d.strip() for d in CONFIG.get('mail_domains') or [] if d.strip()]
        if self.domain not in domains:
            domains.insert(0, self.domain)
        domains = [d for d in domains if d]
        return random.choice(domains) if domains else self.domain

    def create_account(self) -> bool:
        if not self.domain:
            self.logger.error(
                "No mail domain configured — add your own domain in the "
                "dashboard's Email tab (or set ZAI_MAIL_DOMAIN). Refusing "
                "to sign up: without a domain you own, the verification "
                "mail goes nowhere and the account is burned.")
            return False
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
        url = self.inbox_url + ('&' if '?' in self.inbox_url else '?') + 'to=' + urllib.parse.quote(self.email, safe='')
        if self.inbox_secret:
            url += '&k=' + urllib.parse.quote(self.inbox_secret, safe='')
        ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')
        try:
            r = requests.get(url, timeout=15, verify=_verify, headers={'User-Agent': ua})
            if r.status_code != 200:
                # 401 = wrong secret, 503 = INBOX_SECRET not set on the worker,
                # 404 = wrong URL — polling longer will never fix any of these,
                # and silence made every signup spin the full 90 s timeout.
                self.logger.error(
                    f"inbox poll got HTTP {r.status_code}: {str(r.text)[:120]}")
                return None
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


def _main(auto_result=None):
    global IS_HEADLESS
    auto = auto_result is not None
    route_policy = os.environ.get('ZAI_ROUTE_POLICY', '').strip().lower() if auto else ''
    print("\n" + "="*50)
    print(f"Z.ai Auto Signup ({CONFIG.get('mail_domain') or 'no mail domain set'} catch-all)")
    print("="*50)

    if not auto:
        kill_stale_browsers()   # legacy cleanup must never touch auto siblings

    if auto:
        if route_policy not in ('direct', 'proxy'):
            logger.error('AUTO MODE: invalid route policy')
            return auto_result
        if route_policy == 'direct':
            set_proxy(None)
        elif CURRENT_PROXY:
            set_proxy(CURRENT_PROXY)

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
    use_pool = route_policy == 'proxy' if auto else env_on('ZAI_USE_POOL')
    if use_pool and not CURRENT_PROXY:
        picked = pick_verified_proxy()
        if picked:
            set_proxy(picked)
            logger.info(f"Using pool proxy: {_redact_proxy(picked)}")
        else:
            # Never silently substitute the user's chosen route: a pool run that
            # falls back to direct burns the home IP AND hides the reason.
            logger.error("POOL MODE: no live pool proxy found — aborting "
                         "(run proxy_pool.py verify, or switch IP source)")
            if auto:
                auto_result.update(_auto_result('pool_empty'))
            return auto_result

    if auto and route_policy == 'proxy' and not pw_proxy():
        logger.error('AUTO MODE: selected proxy is not a valid browser route')
        auto_result.update(_auto_result('pool_empty'))
        return auto_result

    # A proxy pinned in config.yaml lives only in CURRENT_PROXY; the API helpers
    # (plan_check/claim_start/captcha config) read ZAI_PROXY. Without this the
    # browser signs up through the proxy and every following claim goes direct
    # from the home IP — two identities for one account.
    if CURRENT_PROXY and not os.environ.get('ZAI_PROXY'):
        set_proxy(CURRENT_PROXY)
        logger.info(f"Using pinned proxy: {_redact_proxy(CURRENT_PROXY)}")

    mail = CatchAllMail(logger_instance=logger)
    if not mail.create_account():
        print("Failed to create mail account")
        if auto:
            auto_result.update(_auto_result('mail_error'))
        return auto_result

    email = mail.email
    password = mail.password
    _secret_filter.remember(password)
    if auto:
        auto_result.update(_auto_result(email=email))

    if not mail.get_token():
        print("Failed to init mail reader")
        if auto:
            auto_result.update(_auto_result('mail_error', email))
        return auto_result

    count = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() \
        else CONFIG.get('count', 1)
    if auto:
        count = 1

    logger.info(f"Email: {email}")
    # full password no longer logged — output.txt is a shared log and ends up
    # in captures/HARs; the password is already in ACC_FILE for the operator
    logger.info('Password: [REDACTED]')
    logger.info(f"Count: {count}")
    logger.info(f"Headless: {IS_HEADLESS}")
    logger.info("="*50)

    success = 0
    verification_needed = 0
    starter_count = 0
    skipped = 0
    unverified = 0
    ip_limited = 0
    finish_failed = 0

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
        if use_pool and (i > 0 or not CURRENT_PROXY):
            newp = pick_verified_proxy()
            if newp:
                set_proxy(newp)
                logger.info(f"Account {i+1} proxy: {_redact_proxy(newp)}")
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
        max_proxy_retries = 0 if auto else CONFIG.get('proxy_retries', 3)
        rotate = bool(CURRENT_PROXY) and not pinned_mode   # pool: new IP per retry
        result = None
        for pr in range(max_proxy_retries + 1):
            result = signup(email, password, mail)
            # SIGNUP_UNVERIFIED is a HARD failure (account exists, cannot be
            # completed) — never retry it with a fresh email/proxy
            if result in ('SIGNUP_UNVERIFIED', 'SIGNUP_FINISH_FAILED', 'MAIL_TIMEOUT') or pr >= max_proxy_retries:
                break
            # SIGNUP_IP_LIMITED: the account exists (saved to recovery) but the
            # ROUTE is poisoned for the lockout window. Retrying the same route
            # burns an email+captcha per attempt for nothing; only continue
            # when a pool proxy can give a different exit IP.
            if result == 'SIGNUP_IP_LIMITED':
                if CURRENT_PROXY and not pinned_mode:
                    pass              # pool route: the normal rotation below handles it
                elif _pool_has_live_proxy():
                    # let the single rotation below do the pick+set (picking
                    # here too verified a proxy and then threw it away)
                    rotate = True
                    logger.info("  IP rate-limited on direct — switching to a "
                                "pool proxy for the next attempt")
                else:
                    logger.error("  IP rate-limited and no live pool proxy — "
                                 "stopping (retries would burn emails for nothing)")
                    break
            elif result is not None and result != 'CAPTCHA_NOT_LOADED':
                break   # success (token) — done
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
            route = (f"proxy={_redact_proxy(newp)}" if rotate
                     else f"same route ({_redact_proxy(CURRENT_PROXY) or 'direct'})")
            logger.info(f"  retry {pr + 1}/{max_proxy_retries} — "
                        f"{route} email={email}")

        if result == 'CAPTCHA_NOT_LOADED':
            if auto:
                auto_result.update(_auto_result('captcha_failed', email))
            skipped += 1
            logger.warning(f"Account {i+1} SKIPPED — captcha widget never loaded "
                            f"(IP-blocked). Change IP and retry.")
            if i < count - 1:
                time.sleep(2)
            continue

        if result in ('CAPTCHA_FAILED', 'BROWSER_FAILED'):
            if auto:
                auto_result.update(_auto_result(
                    'captcha_failed' if result == 'CAPTCHA_FAILED' else 'browser_failed', email))
            continue

        if result == 'SIGNUP_FINISH_FAILED':
            if auto:
                auto_result.update(_auto_result('finish_signup_failed', email))
            finish_failed += 1
            logger.error('Account %s signup finalization failed; see private '
                         'recovery_accounts.txt (no signup retry)', i + 1)
            continue

        if result == 'MAIL_TIMEOUT':
            if auto:
                auto_result.update(_auto_result('mail_timeout', email))
            unverified += 1
            continue

        if result == 'SIGNUP_UNVERIFIED':
            if auto:
                auto_result.update(_auto_result('mail_error', email))
            # hard failure: the account exists but could not be verified —
            # credentials were already appended to recovery_accounts.txt
            # inside signup(); do NOT burn another email+proxy on it
            unverified += 1
            logger.error(f"Account {i+1} created but UNVERIFIED — see "
                         f"recovery_accounts.txt (no retry possible)")
            continue

        if result == 'SIGNUP_IP_LIMITED':
            if auto:
                auto_result.update(_auto_result('rate_limited', email))
            # account exists + verified, finish_signup was IP-rate-limited;
            # credentials were appended to recovery_accounts.txt by signup().
            ip_limited += 1
            logger.error(f"Account {i+1} created but finish_signup was "
                         f"IP-rate-limited — saved to recovery_accounts.txt; "
                         f"route {'rotated' if rotate else 'has no alternative'}")
            continue

        if result:
            if isinstance(result, dict) and result.get('needs_verification'):
                if auto:
                    auto_result.update(_auto_result('mail_error', email))
                verification_needed += 1
                with open(ACC_FILE, 'a') as f:
                    f.write(f"{email}:{password}:VERIFICATION_NEEDED\n")
                logger.info(f"Account {i+1} needs verification: {email}")
            else:
                success += 1
                token = result if isinstance(result, str) else "SUCCESS"

                # Plan check: a created account is only worth keeping if it actually
                # got the Starter plan (granted to only some signups).
                plan, starter = None, False
                if isinstance(result, str):
                    try:
                        from plan_check import check_account_plan
                        try:
                            plan = check_account_plan(token, log=logger.info)
                        except Exception as e:
                            if auto:
                                raise   # strict auto routes never retry directly
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

                # Start Plan claim (billing preview + marketing feed -> billing/claim).
                # The signup grant was removed: a fresh account is NOT given a
                # plan, it is only *offered* one, and the offer is invisible to
                # clients older than app_version 3.16.1. The same pass also
                # picks up Global/Weekend Build cards from /marketing/touch, so
                # an account that already holds a Start Plan is still worth
                # running. It runs for EVERY created account — gating it on
                # `starter` (the old behaviour) made it unreachable once the
                # signup grant disappeared. ZAI_CLAIM_START=0 disables it.
                claim_start_on = os.environ.get('ZAI_CLAIM_START', '1').strip().lower() \
                    not in ('0', 'false', 'no', 'off')
                # Legacy campaigns (withdrawn 2026-10): 100M invite Global Build
                # and the 1024 500M giveaway. Off unless explicitly requested.
                claim500_on = os.environ.get('ZAI_CLAIM500', '').strip().lower() in ('1', 'true', 'yes', 'on')
                if 'ZAI_INVITE_CODE' in os.environ:
                    invite_code = os.environ.get('ZAI_INVITE_CODE', '')
                else:
                    invite_code = CONFIG.get('invite_code')
                global_build = False
                giveaway500 = False
                start_plan = False
                start_plan_id = ''
                if claim_start_on and isinstance(result, str):
                    try:
                        from claim_start import claim_start as run_claim_start
                        rc = run_claim_start(token, log=logger.info,
                                             force_direct=auto and route_policy == 'direct',
                                             **({'proxy_fallback': False} if auto else {}))
                        start_plan = bool(rc.get('claimed'))
                        start_plan_id = rc.get('plan_id') or ''
                        if start_plan and rc.get('already_claimed'):
                            logger.info(f"Start Plan already active for {email} "
                                        f"({start_plan_id or 'start plan'})")
                        elif start_plan:
                            logger.info(f"Start Plan claimed for {email} "
                                        f"({start_plan_id or 'best offer'})")
                        elif rc.get('needs_captcha'):
                            logger.warning(f"Start Plan for {email} needs the "
                                           f"Aliyun captcha (3007) — not claimed")
                        else:
                            logger.info(f"Start Plan not granted for {email}: "
                                        f"{rc.get('error') or 'nothing claimable'}")
                    except Exception as e:
                        logger.warning(f"Start Plan claim failed: {e}")
                elif claim500_on and isinstance(result, str):
                    try:
                        from claim500 import claim500 as run_claim500
                        r500 = run_claim500(token, 'zcode-v3-start-plan-1024',
                                            log=logger.info, force_direct=False)
                        giveaway500 = bool(r500.get('claimed'))
                        if giveaway500:
                            logger.info(f"500M giveaway claimed for {email}")
                        elif r500.get('unavailable'):
                            logger.warning(f"500M giveaway unavailable right now - {r500.get('error')}")
                    except Exception as e:
                        logger.warning(f"500M claim failed: {e}")
                elif invite_code and starter and isinstance(result, str):
                    try:
                        from invite_claim import device_login, claim_invite
                        zjwt, _ = device_login(token, log=logger.info,
                                               force_direct=False)
                        if zjwt:
                            # claim from the SAME exit IP as signup — the whole
                            # 100M flow is risk-scored, and a direct-IP claim
                            # after a proxy signup reads as unusual activity.
                            # No captcha_param reuse: the signup's certifyId is
                            # single-use (F008) and would read as a bad token —
                            # the claim mints its own params in-page.
                            inv = claim_invite(token, invite_code, log=logger.info,
                                               zjwt=zjwt, headless=IS_HEADLESS,
                                               force_direct=False)
                            s = inv.get('summary') or {}
                            global_build = bool(s.get('issued')) or bool(
                                s.get('current') and s.get('target') and s['current'] >= s['target']
                            )
                    except Exception as e:
                        logger.warning(f"invite claim failed: {e}")

                # a claimed Start Plan makes the account poolable exactly like
                # the old signup-granted Starter did
                starter = starter or start_plan
                if auto:
                    auto_result.update(_auto_result('generated' if starter else 'no_plan', email))

                # Cohort forensics: which Start Plan variant a signup gets
                # (0817 vs nt-1026) does not follow a simple country rule, so
                # record the signup exit country/zone next to the result and
                # correlate later. exit_ip_geo caches per proxy host, so this
                # normally costs nothing.
                signup_cc, signup_tz = '', ''
                try:
                    from fingerprint import exit_ip_geo
                    _cc, _tz = exit_ip_geo(
                        CURRENT_PROXY if CURRENT_PROXY and CURRENT_PROXY != '0' else None)
                    signup_cc, signup_tz = _cc or '', _tz or ''
                except Exception:
                    pass

                with open(PLANS_FILE, 'a') as f:
                    f.write(json.dumps({
                        'email': email, 'starter': starter,
                        'start_plan': start_plan, 'start_plan_id': start_plan_id,
                        'plans': plan.get('plans') if plan else None,
                        'global_build': global_build,
                        'giveaway500': giveaway500,
                        'signup_cc': signup_cc, 'signup_tz': signup_tz,
                        'ts': int(time.time()),
                    }) + "\n")
                if starter:
                    starter_count += 1
                    with open(STARTER_FILE, 'a', encoding='utf-8') as f:
                        f.write(f"{email}:{password}:{token}\n")
                    if start_plan:
                        with open(os.path.join(RESULTS_DIR, f'startplan{OUT_SUFFIX}.txt'),
                                  'a', encoding='utf-8') as f:
                            f.write(f"{email}:{password}:{token}\n")
                        logger.info(f"Account {i+1} Start Plan claimed -> "
                                    f"startplan{OUT_SUFFIX}.txt")
                if global_build:
                    with open(os.path.join(OUTPUT_DIR, 'starter100m.txt'), 'a', encoding='utf-8') as f:
                        f.write(f"{email}:{password}:{token}\n")
                    logger.info(f"Account {i+1} 100M Global Build active -> {STARTER_FILE}100m")
                if giveaway500:
                    with open(os.path.join(OUTPUT_DIR, 'accounts500m.txt'), 'a', encoding='utf-8') as f:
                        f.write(f"{email}:{password}:{token}\n")
                    logger.info(f"Account {i+1} 500M giveaway claimed -> accounts500m.txt")

                logger.info(f"Account {i+1} created with an account token (not logged)")

        if i < count - 1:
            time.sleep(2)

    logger.info(f"\n{success} fully created ({starter_count} with Starter plan), "
                f"{verification_needed} need verification")
    print(f"\n{success} fully created")
    print(f"{starter_count} with Starter plan  ->  {STARTER_FILE}")
    if skipped:
        print(f"{skipped} skipped (captcha widget never loaded — IP-blocked)")
    if unverified or ip_limited or finish_failed:
        print(f"{unverified + ip_limited + finish_failed} recoverable in recovery_accounts.txt "
              f"({unverified} unverified, {ip_limited} finish_signup IP-limited, "
              f"{finish_failed} finish_signup failed)")
    print(f"{verification_needed} need email verification")
    print(f"{ACC_FILE}")
    print(f"{_log_file}")
    print(timing.summary())
    return auto_result


def main():
    if not env_on('ZAI_AUTO_GENERATE'):
        return _main()
    result = _auto_result()
    try:
        _main(result)
    except Exception:
        result.update(_auto_result(email=result.get('email')))
        logger.error('AUTO MODE: worker failed; see the diagnostic result')
    finally:
        try:
            _save_auto_route_metadata()
        except Exception:
            if result.get('category') in ('generated', 'no_plan'):
                result.update(_auto_result(email=result.get('email')))
            logger.error('AUTO MODE: private route metadata could not be saved')
        _publish_auto_result(result)
    return result


if __name__ == "__main__":
    main()
