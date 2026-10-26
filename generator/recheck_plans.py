"""Re-check the plan of every account in the ledgers (post-claim_settle).

Why this exists: `plans*.jsonl` was written from a checker call that ran at
signup time. The Start Plan is granted asynchronously and the old header set
(`x-zcode-app-version: 3.15.3`) could also miss plans. This tool re-reads the
balance for each account now and rewrites the ledger, so accounts the gateway
skipped as "no plan" get a second chance.

    python recheck_plans.py [--limit N] [--escalate] [--direct]

  --limit N     only the first N accounts (default: all)
  --escalate    when the balance shows nothing, run the full desktop
                device_login (OAuth + app_login telemetry + api.z.ai login)
                once and re-read — this is the sequence the real client uses,
                and it is the only thing known to flip a late grant.
  --direct      ignore ZAI_PROXY for the whole run

Writes (under $ZAI_OUTPUT_DIR/results or ./output/results):
  plans_recheck.jsonl   one line per account: plans, entitlement, ts
  startplan.txt         email:password:chat_token for accounts with a plan
"""
import glob
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plan_check  # noqa: E402
from plan_check import get_zcode_jwt, get_plan, is_start_plan  # noqa: E402


def _out_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.environ.get('ZAI_OUTPUT_DIR') or os.path.join(here, 'output')
    return out


def _results_dir() -> str:
    base = _out_dir()
    for cand in (os.path.join(base, 'results'), base):
        if os.path.isdir(cand):
            return cand
    return base


def collect(limit: int = 0):
    seen, rows = set(), []
    for base in (_results_dir(), os.path.join(_results_dir(), '..')):
        for path in sorted(glob.glob(os.path.join(base, 'account*.txt'))
                           + glob.glob(os.path.join(base, 'starter*.txt'))
                           + glob.glob(os.path.join(base, 'startplan*.txt'))):
            try:
                lines = open(path, encoding='utf-8', errors='ignore').read().splitlines()
            except OSError:
                continue
            for ln in lines:
                ln = ln.strip()
                if ln.count(':') < 2:
                    continue
                parts = ln.split(':')
                email, token = parts[0].strip().lower(), parts[-1].strip()
                if not token.startswith('eyJ') or email in seen:
                    continue
                seen.add(email)
                rows.append((email, parts[1], token))
                if limit and len(rows) >= limit:
                    return rows
    return rows


def main():
    args = sys.argv[1:]
    limit = 0
    if '--limit' in args:
        i = args.index('--limit')
        limit = int(args[i + 1]) if i + 1 < len(args) else 0
    escalate = '--escalate' in args
    if '--direct' in args:
        os.environ.pop('ZAI_PROXY', None)

    email_only = ''
    if '--email' in args:
        i = args.index('--email')
        email_only = (args[i + 1] if i + 1 < len(args) else '').lower()

    rows = [r for r in collect(limit=0) if not email_only or r[0] == email_only]
    if limit:
        rows = rows[:limit]
    out = _results_dir()
    plans_path = os.path.join(out, 'plans_recheck.jsonl')
    startplan_path = os.path.join(out, 'startplan.txt')
    log = lambda m: print(m, flush=True)

    got = 0
    for n, (email, _password, token) in enumerate(rows, 1):
        log(f"[{n}/{len(rows)}] {email}")
        zjwt = get_zcode_jwt(token, log=lambda *a, **k: None)
        if not zjwt:
            log('  login failed')
            record = {'email': email, 'plans': None, 'error': 'login'}
        else:
            plan = get_plan(zjwt, log=lambda *a, **k: None) or {}
            ids = plan.get('plans') or []
            if not ids and escalate:
                log('  no plan — escalating to the full desktop device_login')
                try:
                    from invite_claim import device_login
                    zjwt2, _ = device_login(token, log=lambda *a, **k: None)
                    if zjwt2:
                        zjwt = zjwt2
                        time.sleep(2)
                        plan = get_plan(zjwt, log=lambda *a, **k: None) or {}
                        ids = plan.get('plans') or []
                except Exception as e:
                    log(f'  escalate failed ({type(e).__name__}: {e})')
            has_start = any(is_start_plan(p) for p in ids)
            got += 1 if has_start else 0
            record = {'email': email, 'plans': ids, 'starter': has_start,
                      'referral': plan.get('referral'),
                      'campaign': plan.get('campaign'),
                      'entitlements': plan.get('entitlements'),
                      'ts': int(time.time())}
            if has_start:
                with open(startplan_path, 'a', encoding='utf-8') as f:
                    f.write(f"{email}:{_password}:{token}\n")
            log(f"  -> {ids}")
        with open(plans_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    log(f"\n{got}/{len(rows)} accounts currently hold a Start Plan")
    log(f"ledger: {plans_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
