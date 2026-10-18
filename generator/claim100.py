"""Standalone 100M Global Build claim for a single account.

Used by the gateway's Accounts tab ("⚡ Claim 100M · proxy / · direct").

    python claim100.py <email> [--direct | --proxy socks5://ip:port]

Finds `email:password:chat_token` in the generator's account ledgers
(`accountsg*.txt`, `accounts*.txt`), then runs the full 100M chain:
device_login -> OAuth bridge -> invite bind -> one client message.
Prints one final JSON line starting with `CLAIM_RESULT ` for the gateway.
"""

import json
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CONFIG          # noqa: E402


def _find_token(email: str) -> str:
    tok = ''
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.environ.get('ZAI_OUTPUT_DIR') or os.path.join(here, 'output')
    roots = [out_dir, here]
    if out_dir:
        parent = os.path.dirname(os.path.abspath(out_dir))
        roots.extend([parent, os.path.join(parent, 'accounts'), os.path.join(parent, 'generator-data')])
    seen = set()
    for root in roots:
        if not os.path.isdir(root) or root in seen:
            continue
        seen.add(root)
        # Check single-file snapshots like email.txt
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
    if not email:
        print("usage: claim100.py <email> [--direct | --proxy URL]")
        return 2

    import invite_claim as ic
    token = _find_token(email)
    if not token:
        print(f"no chat token found for {email} in the account ledgers")
        return 1

    if proxy:
        os.environ['ZAI_PROXY'] = proxy
    elif mode == 'direct':
        os.environ.pop('ZAI_PROXY', None)
    os.environ.setdefault('ZAI_INVITE_CODE', CONFIG.get('invite_code') or '')
    code = os.environ.get('ZAI_INVITE_CODE', '').strip()
    if not code:
        print("no invite code: set claim 100M / invite code in the generator options")
        return 1

    log = lambda m: print(m, flush=True)
    log(f"claim100: {email} mode={mode}"
        + (f" proxy={proxy[:40]}…" if proxy and mode == 'proxy' else ""))
    try:
        zjwt, ok = ic.device_login(token, log=log, force_direct=False)
        log(f"claim100: zjwt {'ok' if zjwt else 'FAILED'}")
    except Exception as e:
        log(f"claim100: device_login crashed: {type(e).__name__}: {e}")
        zjwt, ok = None, False
    res = {'email': email, 'zjwt': bool(zjwt), 'summary': None, 'summary_before': None}
    if zjwt:
        try:
            res['summary_before'] = ic.referral_summary(zjwt, log=lambda *a, **k: None)
        except Exception as e:
            log(f"claim100: summary_before failed: {type(e).__name__}: {e}")
            res['summary_before'] = None
        before = res['summary_before'] or {}
        tasks = before.get('tasks') or {}
        # Only trust "already issued" when we have positive evidence: an issued
        # flag OR a non-empty task map that is fully done. An empty/failed
        # summary used to count as "already claimed" and silently skip.
        if before.get('issued') or (tasks and all(tasks.values())):
            log("claim100: global build already issued — nothing to do")
        else:
            if not before:
                log("claim100: pre-summary unavailable — proceeding with claim")
            inv = None
            try:
                inv = ic.claim_invite(token, code, log=log, zjwt=zjwt,
                                      headless=False, force_direct=False)
            except Exception as e:
                log(f"claim100: claim crashed: {type(e).__name__}: {e}")
            if inv is not None:
                # keep the full result for the gateway, but never print the
                # live credential into logs (IC-H1)
                redacted = dict(inv)
                if inv.get('zcode_jwt'):
                    redacted['zcode_jwt'] = inv['zcode_jwt'][:12] + '…(hidden)'
                res['claim'] = redacted
                res['_claim_jwt'] = inv.get('zcode_jwt') or ''
    time.sleep(1)
    if zjwt:
        try:
            res['summary'] = ic.referral_summary(zjwt, log=lambda *a, **k: None)
        except Exception as e:
            log(f"claim100: final summary failed: {type(e).__name__}: {e}")
    print('CLAIM_RESULT ' + json.dumps(res, ensure_ascii=False))
    s = res.get('summary') or {}
    return 0 if s.get('issued') else 1


if __name__ == '__main__':
    sys.exit(main())
