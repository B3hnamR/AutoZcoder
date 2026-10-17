"""Invite-link signup: open a zcode.z.ai invite URL, let the human drive the
entire signup manually, capture the session token when it lands, plan-check,
save.

Usage:
    python invite_signup.py                       # default invite URL
    python invite_signup.py <url>                 # custom invite/auth URL

Token capture is adaptive: watches /api/v1/auths/* responses on ANY host,
cookies, and localStorage. A non-guest JWT with an @guest.com-free email claim
is the finish signal.
"""
import io
import json
import os
import re
import sys
import time
from pathlib import Path

# Windows console is cp1252 by default; force UTF-8 so ✓/→ in output never crash us
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from playwright.sync_api import sync_playwright

from utils import ANTI_DETECTION_SCRIPT, BROWSER_ARGS
from config import CONFIG

OUT_SUFFIX = os.environ.get('ZAI_OUT_SUFFIX', 'inv')
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           '.pw-profile' + OUT_SUFFIX)
ACC_FILE = f'output/accounts{OUT_SUFFIX}.txt'
PLANS_FILE = f'output/plans{OUT_SUFFIX}.jsonl'
STARTER_FILE = f'output/starter{OUT_SUFFIX}.txt'

DEFAULT_URL = 'https://zcode.z.ai/invite/MT9GNDQ8'

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def jwt_payload(token: str):
    try:
        import base64 as _b64
        part = token.split('.')[1]
        part += '=' * ((4 - len(part) % 4) % 4)
        return json.loads(_b64.urlsafe_b64decode(part))
    except Exception:
        return None


def is_guest(token: str) -> bool:
    p = jwt_payload(token) or {}
    em = p.get('email') or ''
    return em.endswith('@guest.com') or em.startswith('guest-') or not em


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f'invite flow -> {url}')
    print('the browser is YOURS: fill the form, solve the captcha, finish signup.')
    print('I grab the token the moment it lands, then plan-check + save.\n')

    captured = {'token': None, 'email': None}

    def try_token(tok: str) -> bool:
        if not tok or len(tok) < 50 or is_guest(tok):
            return False
        if captured['token']:
            return True
        p = jwt_payload(tok) or {}
        captured['token'] = tok
        captured['email'] = p.get('email')
        print(f'[+] TOKEN CAPTURED  email={captured["email"]}')
        return True

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            args=BROWSER_ARGS,
            viewport={'width': 1280, 'height': 800},
            user_agent=UA,
            locale='en-US',
            timezone_id='America/New_York',
        )
        page = ctx.new_page()
        page.add_init_script(ANTI_DETECTION_SCRIPT)

        def on_response(resp):
            try:
                u = resp.url
                if '/api/v1/auths/' in u:
                    print(f'    [auth] {resp.request.method} {u.split("?")[0]} -> {resp.status}')
                    sc = resp.headers.get('set-cookie') or ''
                    m = re.search(r'token=([^;]+)', sc)
                    if m:
                        try_token(m.group(1))
                    if resp.status == 200:
                        try:
                            body = resp.json()
                            tok = (body.get('user') or {}).get('token')
                            if tok:
                                try_token(tok)
                        except Exception:
                            pass
            except Exception:
                pass

        page.on('response', on_response)
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
        except Exception as e:
            print(f'[!] load issue ({str(e)[:80]}) — continuing anyway')

        deadline = time.time() + 900   # 15 min of human time
        poll = 0
        while time.time() < deadline and not captured['token']:
            poll += 1
            try:
                for fr in ctx.pages:
                    try:
                        lt = fr.evaluate(
                            "() => localStorage.getItem('token') || null")
                        if lt:
                            try_token(lt)
                        if captured['token']:
                            break
                    except Exception:
                        pass
                if not captured['token']:
                    for c in ctx.cookies():
                        if c.get('name') == 'token':
                            try_token(c.get('value', ''))
            except Exception:
                pass
            if poll % 20 == 0:
                print('    waiting for signup to finish...')
            time.sleep(1)

        if captured['token']:
            tok = captured['token']
            email = captured['email'] or 'unknown'
            print('\n[+] saving…')
            Path('output').mkdir(exist_ok=True)
            with open(ACC_FILE, 'a', encoding='utf-8') as f:
                f.write(f'{email}:manual-invite:{tok}\n')

            plan = None
            try:
                from plan_check import check_account_plan
                plan = check_account_plan(tok, log=print)
            except Exception as e:
                print(f'[!] plan check failed: {e}')
            starter = bool(plan and plan.get('starter'))
            with open(PLANS_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'email': email, 'starter': starter,
                                    'plans': (plan or {}).get('plans'),
                                    'ts': int(time.time())}) + '\n')
            if starter:
                with open(STARTER_FILE, 'a', encoding='utf-8') as f:
                    f.write(f'{email}:manual-invite:{tok}\n')
            print(f'[✓] done — starter={starter} plans={(plan or {}).get("plans")}')
        else:
            print('[x] no token captured in 15 min — giving up')

        try:
            ctx.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
