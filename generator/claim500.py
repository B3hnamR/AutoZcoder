"""Standalone 500M "1024 Dev Day" claim for a single account.

Chain (captured via MITM 2026-10-24, verified live 2026-10-24):

  1. chat token -> device_login (CLI OAuth -> zcodeJWT)  [invite_claim.py]
  2. GET /api/v1/marketing/touch?seq=0 -> discover campaign_id + plan_id
  3. POST /api/v1/marketing/touch/action {"campaign_id":..., "action_type":"confirm"}
  4. POST /api/v1/zcode-plan/billing/claim {"plan_id":...}
     (Bearer zjwt + x-device-mid — NO captcha header needed, verified:
      the endpoint answers code=0 with plain app headers)
  5. GET /api/v1/zcode-plan/billing/balance -> the 300M+200M buckets exist

No browser, no captcha, no Playwright. Pure HTTP, ~5s per account.

Prints one final JSON line starting with `CLAIM_RESULT ` for the gateway
(same contract as claim100.py).

Usage:
    python claim500.py <email> [--direct | --proxy socks5://ip:port] [--plan PLAN_ID]
"""
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from invite_claim import (        # noqa: E402
    _VERIFY,
    device_login,
    per_account_device_mid,
    _proxies,
)

BALANCE = 'https://zcode.z.ai/api/v1/zcode-plan/billing/balance?app_version=3.16.3'
CLAIM = 'https://zcode.z.ai/api/v1/zcode-plan/billing/claim'
TOUCH = 'https://zcode.z.ai/api/v1/marketing/touch/action'
TOUCH_FEED = 'https://zcode.z.ai/api/v1/marketing/touch?seq=0'

# Fallback plan id (10/24/2026 campaign). The touch feed discovery wins.
DEFAULT_PLAN_ID = os.environ.get('ZAI_PLAN_500', 'zcode-v3-start-plan-1024')
# '' = auto-discover from the /marketing/touch feed.
CAMPAIGN_ID = os.environ.get('ZAI_CAMPAIGN_ID', '')

CLAIM_HEADERS_BASE = {
    'content-type': 'application/json',
    'http-referer': 'https://zcode.z.ai',
    'user-agent': 'ZCode/3.16.3',
    'x-client-language': 'en-US',
    'x-client-timezone': 'Asia/Tehran',
    'x-os-category': 'windows',
    'x-os-version': 'Windows 11 Pro',
    'x-platform': 'win32-x64',
    'x-release-channel': 'production',
    'x-title': 'Z Code@electron',
    'x-zcode-app-version': '3.16.3',
    'accept': '*/*',
}


def _find_token(email: str) -> str:
    tok = ''
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.environ.get('ZAI_OUTPUT_DIR') or os.path.join(here, 'output')
    roots = [out_dir, here]
    if out_dir:
        parent = os.path.dirname(os.path.abspath(out_dir))
        roots.extend([os.path.join(out_dir, 'results'), parent,
                      os.path.join(parent, 'accounts'),
                      os.path.join(parent, 'generator-data'),
                      os.path.join(parent, 'generator-data', 'results')])
    seen = set()
    for root in roots:
        if not os.path.isdir(root) or root in seen:
            continue
        seen.add(root)
        direct_file = os.path.join(root, f"{email}.txt")
        if os.path.isfile(direct_file):
            try:
                for line in open(direct_file, 'r', encoding='utf-8', errors='ignore'):
                    line = line.strip()
                    if line.startswith(email + ':'):
                        parts = line.split(':')
                        if len(parts) >= 3 and parts[2]:
                            return parts[2]
            except OSError:
                pass
        for name in os.listdir(root):
            if not ((name.startswith('accountsg') or name.startswith('accounts') or name.startswith('starter'))
                    and name.endswith('.txt')):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith(email + ':'):
                            parts = line.split(':')
                            if len(parts) >= 3 and parts[2]:
                                return parts[2]
            except OSError:
                continue
    return tok


def _headers(zjwt: str) -> dict:
    h = dict(CLAIM_HEADERS_BASE)
    h['authorization'] = 'Bearer ' + zjwt
    h['x-device-mid'] = per_account_device_mid(zjwt)
    h['x-request-id'] = str(__import__('uuid').uuid4())
    return h


def discover_campaign(zjwt: str, log=print) -> tuple:
    """Pull the live campaign_id + plan_id from the touch feed."""
    import requests
    try:
        r = requests.get(TOUCH_FEED, headers=_headers(zjwt), timeout=30,
                         verify=_VERIFY, proxies=_proxies())
        try:
            j = r.json()
        except Exception:
            j = {}
        for d in ((j.get('data') or {}).get('deliveries') or []):
            for b in ((d.get('banner') or {}).get('buttons') or []):
                args = (b.get('action') or {}).get('args') or {}
                if args.get('plan_id'):
                    cid = d.get('campaign_id') or ''
                    pid = args.get('plan_id')
                    log(f'  claim500: campaign {cid} -> plan {pid}')
                    return cid, pid
    except Exception as e:
        log(f'  claim500: campaign discovery failed ({type(e).__name__})')
    return '', ''


def marketing_touch(zjwt: str, campaign_id: str, log=print) -> bool:
    """Confirm the giveaway campaign touch (mirrors the client; the claim
    itself works without it)."""
    import requests
    if not campaign_id:
        log('  claim500: no campaign id — skipping touch')
        return False
    try:
        r = requests.post(TOUCH, headers=_headers(zjwt),
                          json={'campaign_id': campaign_id, 'action_type': 'confirm'},
                          timeout=30, verify=_VERIFY, proxies=_proxies())
        ok = False
        try:
            ok = r.status_code == 200 and r.json().get('code') == 0
        except Exception:
            pass
        log(f'  claim500: marketing_touch -> {r.status_code} '
            f'({"confirmed" if ok else r.text[:100]})')
        return ok
    except Exception as e:
        log(f'  claim500: marketing_touch failed ({type(e).__name__}: {e})')
        return False


def billing_claim(zjwt: str, plan_id: str, log=print) -> dict:
    """POST /billing/claim. Returns the parsed envelope (never None)."""
    import requests
    try:
        r = requests.post(CLAIM, headers=_headers(zjwt), json={'plan_id': plan_id},
                          timeout=30, verify=_VERIFY, proxies=_proxies())
    except requests.RequestException as e:
        # a network blip is a failed attempt the caller retries, not a crash
        return {'code': -1, 'msg': f'request failed: {type(e).__name__}'}
    try:
        return r.json()
    except Exception:
        return {'code': -1, 'msg': f'non-json {r.status_code} {r.text[:150]}'}


def billing_balance(zjwt: str, log=print) -> dict:
    import requests
    try:
        r = requests.get(BALANCE, headers=_headers(zjwt), timeout=30,
                         verify=_VERIFY, proxies=_proxies())
    except requests.RequestException as e:
        log(f'  claim500: balance request failed ({type(e).__name__})')
        return {}
    try:
        return (r.json().get('data') or {})
    except Exception:
        log(f'  claim500: balance non-json {r.status_code}')
        return {}


def plan_in_balance(data: dict, plan_id: str) -> bool:
    for p in (data.get('plans') or []):
        if p.get('plan_id') == plan_id and p.get('status') == 'active':
            return True
    return False


FALLBACK_PROXIES = 2          # how many verified pool proxies to try after the first route fails


def _is_route_problem(code, msg) -> bool:
    """Could a different IP / connection change this answer? Only a block
    (3012 / 405 "unusual activity") or a failed/garbled request can; a definitive
    business answer such as 1005 "exceed quota limit" cannot, so retrying it
    through pool proxies only burns single-use proxies."""
    if code is None or code in (3012, 405, -1):
        return True
    text = str(msg or '').lower()
    return any(k in text for k in ('unusual activity', 'blocked', 'request failed', 'non-json'))


def _redact(proxy: str) -> str:
    return re.sub(r'//[^/@]*@', '//', proxy or '')


def _verified_pool_proxy(log) -> str:
    """A live, UNUSED proxy from the shared pool, verified against z.ai right
    now (pick_verified probes before handing one out), or '' if none passes."""
    try:
        import proxy_pool
        out = os.environ.get('ZAI_OUTPUT_DIR')
        if out:
            proxy_pool.set_output_dir(out)
        for socks in (True, False):       # socks first: they pass the account gates best
            picked = proxy_pool.pick_verified(tries=10, timeout=8, socks=socks)
            if picked:
                return picked
    except Exception as e:
        log(f'  claim500: no pool proxy available ({type(e).__name__}: {e})')
    return ''


def _run_chain(token: str, plan_id: str, log, force_direct: bool, zjwt=None,
               claim_attempts: int = 3):
    """One pass of the whole chain on the CURRENT route. Returns (result, zjwt)."""
    res = {'plan_id': plan_id, 'zjwt': False, 'claimed': False,
           'already_claimed': False, 'balance': None, 'route_problem': True}
    for attempt in (1, 2):      # the device flow crosses 4 hosts: one blip must not end the run
        if zjwt:
            break
        try:
            zjwt, _ok = device_login(token, log=log, force_direct=force_direct)
        except Exception as e:
            log(f'  claim500: device login failed ({type(e).__name__}: {e})')
            zjwt = None
        if zjwt:
            break
        if attempt == 1:
            log('  claim500: retrying device login once')
            time.sleep(3)
    res['zjwt'] = bool(zjwt)
    if not zjwt:
        log('  claim500: no zjwt — aborting')
        return res, None

    # already have it? the balance is the only truth (idempotent claims)
    data = billing_balance(zjwt, log=log)
    res['balance'] = {
        'plans': [p.get('plan_id') for p in (data.get('plans') or [])
                  if p.get('status') == 'active'],
    }
    if plan_in_balance(data, plan_id):
        log(f'  claim500: {plan_id} already active — nothing to do')
        res['already_claimed'] = True
        res['claimed'] = True
        return res, zjwt

    cid, feed_pid = discover_campaign(zjwt, log=log)
    if feed_pid:
        plan_id = feed_pid
        res['plan_id'] = plan_id
        data = billing_balance(zjwt, log=log)
        if plan_in_balance(data, plan_id):
            log(f'  claim500: {plan_id} already active — nothing to do')
            res['already_claimed'] = True
            res['claimed'] = True
            return res, zjwt
    marketing_touch(zjwt, cid or CAMPAIGN_ID, log=log)

    last_msg = ''
    last_code = None
    fresh = False
    unavailable = False
    for attempt in range(1, claim_attempts + 1):
        j = billing_claim(zjwt, plan_id, log=log)
        code, msg = j.get('code'), j.get('msg', '')
        last_code = code
        last_msg = f'{code} {msg}'
        log(f'  claim500: claim attempt {attempt} -> {last_msg}')
        if code == 0:
            fresh = True
            break
        if code == 1005:
            # "exceed quota limit": the giveaway's own limit. A definitive answer,
            # not a route problem: asking again or from another IP changes nothing.
            unavailable = True
            break
        if code == 1003 or 'already' in str(msg).lower():
            # looks like "the claim landed earlier" — but the balance below is
            # the only truth; a code/message match alone must not mark success
            break
        time.sleep(2)

    data = billing_balance(zjwt, log=log)
    in_balance = plan_in_balance(data, plan_id)
    res['claimed'] = fresh or in_balance
    res['already_claimed'] = in_balance and not fresh
    buckets = [
        {'show_name': b.get('show_name'), 'total': b.get('total_units'),
         'remaining': b.get('remaining_units')}
        for b in (data.get('balances') or [])
        if b.get('plan_id') == plan_id
    ]
    if buckets:
        res['balance'] = res['balance'] or {}
        res['balance']['buckets'] = buckets
    if not res['claimed']:
        res['error'] = last_msg or 'claim did not land'
        res['code'] = last_code
        res['route_problem'] = _is_route_problem(last_code, last_msg)
        if unavailable:
            offered = bool(cid or feed_pid)
            res['unavailable'] = True
            res['route_problem'] = False
            res['error'] = ('The 500M giveaway is not available right now: the server answered '
                            '1005 "exceed quota limit"'
                            + ('' if offered else ' and is not offering any campaign')
                            + '. It has probably ended or run out.')
            log('  claim500: ' + res['error'])
    return res, zjwt


def claim500(token: str, plan_id: str, log=print, force_direct: bool = False,
             proxy_fallback=None) -> dict:
    """Full 500M claim chain. Returns a dict for CLAIM_RESULT.

    If the first route fails (typically 3012 "unusual activity" on a flagged IP,
    or a flaky connection), the chain is retried through up to FALLBACK_PROXIES
    live proxies from the pool — each one verified right before it is used.
    Set ZAI_CLAIM500_PROXY_FALLBACK=0 to disable."""
    if proxy_fallback is None:
        proxy_fallback = os.environ.get('ZAI_CLAIM500_PROXY_FALLBACK', '1').strip() != '0'
    res, zjwt = _run_chain(token, plan_id, log, force_direct)
    if res['claimed'] or not proxy_fallback:
        return res
    if res.get('unavailable'):
        log('  claim500: not retrying through proxies - another route cannot change that answer')
        return res
    if not res.get('route_problem', True):
        log(f"  claim500: the refusal ({res.get('error')}) is not an IP block or a network "
            'problem - not retrying through proxies')
        return res
    saved = os.environ.get('ZAI_PROXY')
    try:
        for n in range(1, FALLBACK_PROXIES + 1):
            proxy = _verified_pool_proxy(log)
            if not proxy:
                log('  claim500: no verified pool proxy available to retry with')
                break
            log(f'  claim500: first route failed — retrying through verified pool proxy '
                f'{n}/{FALLBACK_PROXIES} ({_redact(proxy)})')
            os.environ['ZAI_PROXY'] = proxy
            res2, zjwt = _run_chain(token, plan_id, log, force_direct=False, zjwt=zjwt,
                                    claim_attempts=2)
            if res2['claimed']:
                res2['via_proxy'] = _redact(proxy)
                return res2
            if res2.get('unavailable') or not res2.get('route_problem', True):
                return res2              # a clean route got a definitive answer: stop here
            res = res2 if res2.get('zjwt') or not res.get('zjwt') else res
    finally:
        if saved is None:
            os.environ.pop('ZAI_PROXY', None)
        else:
            os.environ['ZAI_PROXY'] = saved
    return res


def main():
    args = [a for a in sys.argv[1:]]
    email = args[0] if args and not args[0].startswith('--') else ''
    mode = 'direct' if '--direct' in args else 'proxy'
    proxy = ''
    if '--proxy' in args:
        i = args.index('--proxy')
        if i + 1 < len(args):
            proxy = args[i + 1]
            mode = 'proxy'
    plan_id = DEFAULT_PLAN_ID
    if '--plan' in args:
        i = args.index('--plan')
        if i + 1 < len(args):
            plan_id = args[i + 1]
    if not email:
        print("usage: claim500.py <email> [--direct | --proxy URL] [--plan PLAN_ID]")
        return 2

    token = _find_token(email)
    if not token:
        print(f"no chat token found for {email} in the account ledgers")
        return 1

    if proxy:
        os.environ['ZAI_PROXY'] = proxy
    elif mode == 'direct':
        os.environ.pop('ZAI_PROXY', None)

    log = lambda m: print(m, flush=True)
    log(f"claim500: {email} mode={mode} plan={plan_id}"
        + (f" proxy={proxy[:40]}…" if proxy and mode == 'proxy' else ""))
    try:
        res = claim500(token, plan_id, log=log, force_direct=(mode == 'direct'))
    except Exception as e:
        log(f"claim500: crashed: {type(e).__name__}: {e}")
        res = {'email': email, 'plan_id': plan_id, 'zjwt': False,
               'claimed': False, 'error': f'{type(e).__name__}: {e}'}
    res['email'] = email
    print('CLAIM_RESULT ' + json.dumps(res, ensure_ascii=False))
    return 0 if res.get('claimed') else 1


if __name__ == '__main__':
    sys.exit(main())
