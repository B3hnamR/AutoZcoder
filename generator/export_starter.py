"""Export generated Starter accounts into the ZcodeGate accounts/ folder.

The gateway loads `dist/accounts/*.txt` snapshots (see ZcodeGate/zcode_gateway/
accounts_dir.py). Those files hold the **zcodeJWT** (HS256, `user_id` claim) — the
token the gateway uses against zcode.z.ai — NOT the chat.z.ai token we capture at
signup. So for each generated account we run the OAuth CLI flow to mint its zcodeJWT,
confirm the plan, and only then write a snapshot for the Starter ones.

Usage:
    python export_starter.py            # scan gen/output/*.txt, export Starter accounts
    python export_starter.py --all      # export every account that has any plan
"""

import base64
import datetime
import glob
import json
import os
import re
import sys

from plan_check import get_zcode_jwt, get_plan, is_start_plan, DEVICE_MID

HERE = os.path.dirname(os.path.abspath(__file__))
# destination for exported snapshots; override per machine (or copy the
# output manually) — the old hardcoded personal path broke on any other box
GATEWAY_ACCOUNTS = os.environ.get(
    "ZCODE_GATEWAY_ACCOUNTS",
    os.path.join(os.path.dirname(HERE), "accounts"))

# mirrors ZcodeGate accounts_dir.write_snapshot field order
ORDER = [
    "captured_at", "source", "provider_slot", "app_config_active",
    "", "account_id", "zcode_user_id", "workos_user_id", "session_id",
    "email", "name", "device_mid",
    "", "token_prefix", "expires_at", "expires_at_ms", "token_lifetime_s",
    "issuer", "client_id",
    "", "balance_micro", "balance_usd", "plan",
    "", "access_token", "refresh_token",
]


def write_snapshot(path, fields):
    lines = ["# Zcode account snapshot"]
    for key in ORDER:
        if key == "":
            lines.append("")
            continue
        if key in fields:
            lines.append(f"{key + ':':<18}{fields[key]}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def safe_name(email):
    label = re.sub(r"[^A-Za-z0-9._@-]+", "_", email or "account").strip("._")
    return f"{label}.txt"


def jwt_payload(tok):
    try:
        p = tok.split('.')[1]
        p += '=' * ((4 - len(p) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def collect_accounts():
    """Current result ledgers, with a legacy-flat fallback under the same root."""
    seen, out = set(), []
    root = os.environ.get('ZAI_OUTPUT_DIR', 'output')
    paths = sorted(glob.glob(os.path.join(root, 'results', 'accounts*.txt')))
    paths += sorted(glob.glob(os.path.join(root, 'accounts*.txt')))
    for path in paths:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line or line.count(':') < 1:
                    continue
                parts = line.split(':')
                if len(parts) < 3:
                    continue
                email = parts[0].strip().lower()
                token = parts[-1]
                if not token.startswith('eyJ') or email in seen:
                    continue
                seen.add(email)
                out.append((email, token))
    return out


def main():
    export_all = '--all' in sys.argv
    accounts = collect_accounts()
    print(f"candidate accounts: {len(accounts)}")
    os.makedirs(GATEWAY_ACCOUNTS, exist_ok=True)
    exported = 0
    for email, chat_token in accounts:
        print(f"\n=== {email} ===")
        zjwt = get_zcode_jwt(chat_token, log=lambda *a: print('   ', *a))
        if not zjwt:
            print("    no zcodeJWT — skip")
            continue
        plan = get_plan(zjwt)
        if not plan:
            print("    no plan data — skip")
            continue
        starter = plan.get('starter')
        referral = plan.get('referral')
        print(f"    plans={plan.get('plans')} starter={starter} referral={referral}")
        if not (starter or referral or export_all):
            continue
        payload = jwt_payload(zjwt)
        ident = payload.get('user_id') or payload.get('sub') or email
        plans_list = plan.get('plans') or []
        chosen_plan = next((p for p in plans_list if is_start_plan(p)),
                           plans_list[0] if plans_list else '')
        fields = {
            'captured_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'source': 'ZCODEAUTO chat.z.ai signup (OAuth CLI)',
            'account_id': ident,
            'email': email,
            'name': '',
            'device_mid': DEVICE_MID,
            'plan': chosen_plan,
            'access_token': zjwt,
        }
        if payload.get('exp'):
            fields['expires_at'] = str(payload['exp'])
            fields['expires_at_ms'] = str(int(payload['exp']) * 1000)
        else:
            fields['expires_at_ms'] = '0'
        # drop empty values so the file matches ZcodeGate's own output exactly
        fields = {k: v for k, v in fields.items() if v != ''}
        dest = os.path.join(GATEWAY_ACCOUNTS, safe_name(email))
        write_snapshot(dest, fields)
        exported += 1
        print(f"    -> wrote {dest}")
    print(f"\nexported {exported} account(s) to {GATEWAY_ACCOUNTS}")


if __name__ == '__main__':
    main()
