"""Claim the ZCode Start Plan from the billing preview (pure HTTP).

Replaces the two dead campaigns:
  * `claim100.py` — the invite Global Build 100M (3/3 task reward, withdrawn)
  * `claim500.py` — the 1024 Dev Day 500M giveaway (expired 2026-10-26 01:00 UTC)

Since the signup grant was removed, a fresh account no longer *receives* the
Start Plan. It is offered in the billing preview instead, and must be granted:

  1. chat token -> device_login (OAuth CLI -> zcodeJWT)   [invite_claim.py]
  2. POST /api/v1/event/report  app_launch + app_daily_active
     (the preview answers an empty list until the account reported activity)
  3. GET  /api/v1/zcode-plan/billing/preview?app_version=3.16.3
     (also version-gated: <3.16.1 answers [])
  4. POST /api/v1/zcode-plan/billing/claim {"plan_id": ...}
     (0 = claimed, 1003 = already claimed -> idempotent success)
  5. GET  /api/v1/zcode-plan/billing/balance -> an active start-plan id

If what the account is offered turns out to be `zcode-v3-start-plan-referral`,
the account must first be bound to an invite (browser + Aliyun captcha); this
script then still claims whatever the preview lists once the bind has landed.

No browser, no captcha, no Playwright in the normal path.

Usage:
    python claim_start.py <email> [--direct | --proxy socks5://ip:port] [--plan PLAN_ID]

Prints one final JSON line starting with `CLAIM_RESULT ` for the gateway
(same contract as claim100.py / claim500.py).
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
)  # per_account_device_mid / _proxies come from plan_check now
from plan_check import (          # noqa: E402
    APP_VERSION,
    PLATFORM,
    claim_plan,
    get_plan,
    is_start_plan,
    per_account_device_mid,
    preview_plans,
    report_app_activity,
)

FALLBACK_PROXIES = 2


def _captcha_config(log) -> dict:
    """`data.configs.captcha` from /client/configs — {enabled, prefix, sceneId, region}."""
    try:
        import requests
        r = requests.get(
            "https://zcode.z.ai/api/v1/client/configs",
            params={"app_version": APP_VERSION, "platform": PLATFORM},
            timeout=15, verify=_VERIFY,
        )
        cfg = ((r.json() or {}).get("data") or {}).get("configs", {}).get("captcha") or {}
        if cfg.get("enabled") and cfg.get("sceneId"):
            return cfg
        log(f"  claim_start: captcha config disabled/empty ({str(cfg)[:120]})")
    except Exception as e:
        log(f"  claim_start: captcha config failed ({type(e).__name__}: {e})")
    return {}


def _find_mint_exe() -> str:
    """Locate the bundled captcha-mint sidecar (frozen or source tree)."""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands.append(os.path.join(meipass, "captcha-mint", "captcha-mint.exe"))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    cands += [
        os.path.join(exe_dir, "captcha-mint", "captcha-mint.exe"),
        os.path.join(os.path.dirname(here), "tools", "captcha-mint", "captcha-mint.exe"),
        os.path.join(here, "captcha-mint.exe"),
    ]
    for p in cands:
        if os.path.isfile(p):
            return p
    return ""


def _captcha_token(log):
    """Mint an Aliyun captcha verify param for the claim.

    Priority:
      1. ZAI_CAPTCHA_PARAM (+ ZAI_CAPTCHA_REGION) — a pre-solved token.
      2. ZAI_CAPTCHA_CMD — a sidecar that prints `<param> <region>` on stdout
         (e.g. the Bun/happy-dom solver in zcode-api: captcha-happy.ts).
    Returns ('', '') when neither is available; the claim then answers 3007.
    """
    param = (os.environ.get("ZAI_CAPTCHA_PARAM") or "").strip()
    if param:
        return param, (os.environ.get("ZAI_CAPTCHA_REGION") or "sgp").strip()
    cmd = (os.environ.get("ZAI_CAPTCHA_CMD") or "").strip()
    if not cmd and os.environ.get("ZAI_CAPTCHA_MINT", "1").strip() != "0":
        mint = _find_mint_exe()
        if mint:
            cmd = f'"{mint}"'
    if cmd:
        import shlex
        import subprocess
        cfg = _captcha_config(log)
        scene = cfg.get("sceneId", "")
        region = cfg.get("region", "sgp")
        prefix = cfg.get("prefix", "")
        env = dict(os.environ, ZAI_CAPTCHA_SCENE=scene, ZAI_CAPTCHA_REGION=region,
                   ZAI_CAPTCHA_PREFIX=prefix, ZAI_APP_VERSION=APP_VERSION)
        try:
            bare = cmd.strip().strip('"')
            argv = [bare] if os.path.isfile(bare) else shlex.split(cmd)
            out = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=120, env=env)
            parts = (out.stdout or "").strip().split()
            if len(parts) >= 1 and len(parts[0]) > 20:
                return parts[0], (parts[1] if len(parts) > 1 else region)
            log(f"  claim_start: captcha sidecar gave no token "
                f"(rc={out.returncode} {((out.stderr or out.stdout) or '')[:160]})")
        except Exception as e:
            log(f"  claim_start: captcha sidecar failed ({type(e).__name__}: {e})")
    return "", ""


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
            if not ((name.startswith('accountsg') or name.startswith('accounts')
                     or name.startswith('starter') or name.startswith('startplan'))
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


def _is_route_problem(code, msg) -> bool:
    """Could a different IP change this answer? Only a block (3012/405
    "unusual activity") or a failed/garbled request can; a definitive business
    answer (1001 gone, 1005 limit) cannot, so retrying through pool proxies
    only burns single-use proxies."""
    if code is None or code in (3012, 405, -1):
        return True
    text = str(msg or '').lower()
    return any(k in text for k in ('unusual activity', 'blocked', 'request failed', 'non-json'))


def _redact(proxy: str) -> str:
    return re.sub(r'//[^/@]*@', '//', proxy or '')


def _verified_pool_proxy(log) -> str:
    """A live, UNUSED proxy from the shared pool, verified against z.ai right
    now, or '' if none passes."""
    try:
        import proxy_pool
        out = os.environ.get('ZAI_OUTPUT_DIR')
        if out:
            proxy_pool.set_output_dir(out)
        for socks in (True, False):
            picked = proxy_pool.pick_verified(tries=10, timeout=8, socks=socks)
            if picked:
                return picked
    except Exception as e:
        log(f'  claim_start: no pool proxy available ({type(e).__name__}: {e})')
    return ''


def _plan_buckets(data: dict, plan_id: str) -> list:
    return [
        {'show_name': b.get('show_name'), 'total': b.get('total_units'),
         'remaining': b.get('remaining_units')}
        for b in (data.get('balances') or [])
        if b.get('plan_id') == plan_id
    ]


def _run_chain(token: str, plan_id: str, log, force_direct: bool,
               zjwt=None, claim_attempts: int = 2, dry_run: bool = False):
    """One pass of the whole chain on the CURRENT route.

    dry_run stops after the preview: it proves whether the account is *offered*
    a plan without spending the one-shot claim."""
    res = {'plan_id': plan_id or '', 'zjwt': False, 'claimed': False,
           'already_claimed': False, 'needs_captcha': False,
           'plans': [], 'preview': [], 'balance': None,
           'route_problem': True}

    for attempt in (1, 2):      # the device flow crosses 4 hosts: one blip must not end the run
        if zjwt:
            break
        try:
            zjwt, _ok = device_login(token, log=log, force_direct=force_direct)
        except Exception as e:
            log(f'  claim_start: device login failed ({type(e).__name__}: {e})')
            zjwt = None
        if zjwt:
            break
        if attempt == 1:
            log('  claim_start: retrying device login once')
            time.sleep(3)
    res['zjwt'] = bool(zjwt)
    if not zjwt:
        log('  claim_start: no zjwt — aborting')
        return res, None

    # already granted? balance is the only truth (claims are idempotent)
    plan = get_plan(zjwt, log=log)
    if plan:
        res['plans'] = plan.get('plans') or []
    if plan and plan.get('starter'):
        log(f"  claim_start: start plan already active ({plan.get('start_plans')}) — nothing to do")
        res['already_claimed'] = True
        res['claimed'] = True
        return res, zjwt

    # the preview stays empty without recent app telemetry
    report_app_activity(zjwt, log=log)

    offer = preview_plans(zjwt, log=log)
    res['preview'] = [p['plan_id'] for p in offer]

    candidates = []
    if plan_id:
        candidates.append(plan_id)
    for p in offer:
        pid = p.get('plan_id')
        if pid and pid not in candidates:
            candidates.append(pid)
    if dry_run:
        log(f"  claim_start: dry run — would claim {candidates}")
        res['dry_run'] = True
        res['candidates'] = candidates
        res['error'] = '' if candidates else 'no claimable plan in preview'
        res['route_problem'] = False
        return res, zjwt
    if not candidates:
        log('  claim_start: nothing is claimable from the preview')
        res['error'] = 'no claimable plan in preview'
        res['route_problem'] = False
        return res, zjwt

    last_code = None
    last_msg = ''
    fresh = False
    unavailable = False
    for pid in candidates:
        for attempt in range(1, claim_attempts + 1):
            # a token is single-use: mint a fresh one for every attempt
            vparam, vregion = _captcha_token(log)
            if not vparam:
                log('  claim_start: no captcha token available — the claim will '
                    'answer 3007 (set ZAI_CAPTCHA_PARAM or ZAI_CAPTCHA_CMD)')
            j = claim_plan(zjwt, pid, verify_param=vparam, region=vregion, log=log)
            code, msg = j.get('code'), j.get('msg', '')
            last_code = code
            last_msg = f'{pid}: {code} {msg}'
            log(f'  claim_start: claim {pid} attempt {attempt} -> {code} {msg}')
            if code == 0:
                fresh = True
                break
            if code == 1003 or 'already' in str(msg).lower():
                fresh = True          # idempotent success; balance proves it
                break
            if code == 3007:
                res['needs_captcha'] = True
                break
            if code == 1001:
                break                 # this plan is gone — try the next offer
            if code == 1005:
                unavailable = True
                break
            time.sleep(2)
        if fresh or res['needs_captcha']:
            break

    plan = get_plan(zjwt, log=log) or {}
    res['plans'] = plan.get('plans') or []
    entitled_ids = set(res['plans']) | {
        e.get('plan_id') for e in (plan.get('entitlements') or []) if e.get('plan_id')
    }
    entitled = any(is_start_plan(pid) for pid in entitled_ids)
    # `fresh` alone is not proof: 1003 means "already claimed", but the plan
    # may still be pre-effective (entitlement with effective_at), so the
    # balance/entitlement list is what decides.
    res['claimed'] = bool(plan.get('starter')) or (fresh and entitled)
    res['already_claimed'] = bool(plan.get('starter')) and not fresh
    res['start_plans'] = plan.get('start_plans') or []
    res['entitlements'] = plan.get('entitlements') or []
    if res['claimed']:
        pid = (res['start_plans'] or [plan_id])[0] if (res['start_plans'] or plan_id) else ''
        res['plan_id'] = pid
        buckets = _plan_buckets(plan.get('raw') or {}, pid)
        if buckets:
            res['balance'] = {'buckets': buckets}
        return res, zjwt

    res['code'] = last_code
    res['error'] = last_msg or 'claim did not land'
    if last_code == 1003:
        res['error'] = ('the server says this plan was already claimed, but it is '
                        'not in balance/entitlements yet — re-check after the '
                        'effective_at time')
    res['route_problem'] = _is_route_problem(last_code, last_msg)
    if res['needs_captcha']:
        res['error'] = ('the claim endpoint asked for the Aliyun captcha '
                        '(3007) — this account needs the in-page/browser mint')
        res['route_problem'] = False
    if unavailable:
        res['unavailable'] = True
        res['route_problem'] = False
        res['error'] = ('nothing was granted: the server answered 1005 '
                        '(quota/limit) for every offered plan')
    return res, zjwt


def claim_start(token: str, log=print, force_direct: bool = False,
                plan_id: str = '', proxy_fallback=None,
                dry_run: bool = False) -> dict:
    """Full Start Plan claim chain. Returns a dict for CLAIM_RESULT."""
    if proxy_fallback is None:
        proxy_fallback = os.environ.get('ZAI_CLAIM_START_PROXY_FALLBACK', '1').strip() != '0'
    res, zjwt = _run_chain(token, plan_id, log, force_direct, dry_run=dry_run)
    if res['claimed'] or not proxy_fallback:
        return res
    if not res.get('route_problem', True) or res.get('needs_captcha'):
        return res
    saved = os.environ.get('ZAI_PROXY')
    try:
        for n in range(1, FALLBACK_PROXIES + 1):
            proxy = _verified_pool_proxy(log)
            if not proxy:
                log('  claim_start: no verified pool proxy available to retry with')
                break
            log(f'  claim_start: first route failed — retrying through verified pool proxy '
                f'{n}/{FALLBACK_PROXIES} ({_redact(proxy)})')
            os.environ['ZAI_PROXY'] = proxy
            res2, zjwt = _run_chain(token, plan_id, log, force_direct=False,
                                    zjwt=zjwt, claim_attempts=1, dry_run=dry_run)
            if res2['claimed']:
                res2['via_proxy'] = _redact(proxy)
                return res2
            if not res2.get('route_problem', True) or res2.get('needs_captcha'):
                return res2
            res = res2 if res2.get('zjwt') or not res.get('zjwt') else res
    finally:
        if saved is None:
            os.environ.pop('ZAI_PROXY', None)
        else:
            os.environ['ZAI_PROXY'] = saved
    return res


def main():
    args = list(sys.argv[1:])
    email = args[0] if args and not args[0].startswith('--') else ''
    mode = 'direct' if '--direct' in args else 'proxy'
    proxy = ''
    if '--proxy' in args:
        i = args.index('--proxy')
        if i + 1 < len(args):
            proxy = args[i + 1]
            mode = 'proxy'
    plan_id = os.environ.get('ZAI_START_PLAN_ID', '')
    if '--plan' in args:
        i = args.index('--plan')
        if i + 1 < len(args):
            plan_id = args[i + 1]
    if not email:
        print("usage: claim_start.py <email> [--direct | --proxy URL] [--plan PLAN_ID]")
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
    log(f"claim_start: {email} mode={mode}"
        + (f" plan={plan_id}" if plan_id else "")
        + (f" proxy={proxy[:40]}…" if proxy and mode == 'proxy' else ""))
    try:
        res = claim_start(token, log=log, force_direct=(mode == 'direct'),
                          plan_id=plan_id, dry_run=('--dry-run' in args))
    except Exception as e:
        log(f"claim_start: crashed: {type(e).__name__}: {e}")
        res = {'zjwt': False, 'claimed': False, 'error': f'{type(e).__name__}: {e}'}
    res['email'] = email
    print('CLAIM_RESULT ' + json.dumps(res, ensure_ascii=False))
    return 0 if res.get('claimed') else 1


if __name__ == '__main__':
    sys.exit(main())
