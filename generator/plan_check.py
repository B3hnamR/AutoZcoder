"""Starter-plan checker for generated accounts.

Reverses the ZCode OAuth CLI flow so we can ask, per account, whether it got the
**ZCode Start Plan** (`zcode-v3-start-plan-0817`).

Header set + init bearer mirror `ZcodeGate/zcode_gateway/upstream.py:build_headers`
and `device_auth.py:_authorize` — this is the part that was missing before:
the CLI `init` requires `Authorization: Bearer <client-generated 32-byte hex>` and the
full ZCode fingerprint header block, otherwise it answers `400 {"code":3004,"invalid_flow"}`.

Chain:
    POST zcode.z.ai/api/v1/oauth/cli/init    (Bearer <poll_token>) -> flow_id, poll_token, authorize_url
    POST chat.z.ai/api/oauth/authorize       (Bearer <chat_token>) -> redirect_url?code=code-XXXX
    GET  zcode.z.ai/api/v1/oauth/cli/callback/zai?code=&state=
    GET  zcode.z.ai/api/v1/oauth/cli/poll/<flow_id> (Bearer <poll_token>) -> zcodeJWT
    GET  zcode.z.ai/api/v1/zcode-plan/billing/balance (Bearer <zcodeJWT>) -> plans[]
"""

import json
import os
import re
import secrets
import sys
import time
import uuid

import requests
import urllib3

urllib3.disable_warnings()

CLIENT_ID = "client_P8X5CMWmlaRO9gyO-KSqtg"
BRIDGE_REDIRECT = "https://zcode.z.ai/app/oauth/login?redirect=zcode%3A%2F%2Foauth%2Fcallback&app_version=3.15.3"
CALLBACK = "https://zcode.z.ai/api/v1/oauth/cli/callback/zai"
INIT = "https://zcode.z.ai/api/v1/oauth/cli/init"
POLL = "https://zcode.z.ai/api/v1/oauth/cli/poll/"
BALANCE = "https://zcode.z.ai/api/v1/zcode-plan/billing/balance?app_version=3.15.3"
AUTHORIZE = "https://chat.z.ai/api/oauth/authorize"

try:
    import certifi
    _VERIFY = certifi.where() if not os.environ.get('ZAI_INSECURE_SSL') else False
except Exception:
    _VERIFY = False


def _proxies():
    raw = os.environ.get('ZAI_PROXY')
    return {'http': raw, 'https': raw} if raw else None


STARTER_PLAN_ID = "zcode-v3-start-plan-0817"
REFERRAL_PLAN_ID = "zcode-v3-start-plan-referral"

# The 16 static headers the upstream fingerprints (upstream.py STATIC_HEADERS).
STATIC_HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "http-referer": "https://zcode.z.ai",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "mid-conversation-system-2026-04-07",
}
DEVICE_MID = "58aca3b3-3239-4cb7-9c70-df3e5488fb99"


def build_headers(access_token=None, device_mid=None, include_api_key=True):
    h = dict(STATIC_HEADERS)
    h.update({
        "user-agent": "ZCode/3.15.3 ai-sdk/provider-utils/4.0.27 runtime/node.js/24",
        "x-device-mid": device_mid or DEVICE_MID,
        "x-client-language": "en-US",
        "x-client-timezone": "Asia/Tehran",
        "x-os-category": "windows",
        "x-os-version": "10.0.26200",
        "x-platform": "win32-x64",
        "x-release-channel": "production",
        "x-title": "Z Code@electron",
        "x-zcode-agent": "glm",
        "x-zcode-app-version": "3.15.3",
        "x-zcode-session-type": "main",
        "x-query-id": str(uuid.uuid4()),
        "x-request-id": str(uuid.uuid4()),
        "x-zcode-trace-id": str(uuid.uuid4()),
        "x-session-id": str(uuid.uuid4()),
    })
    if access_token:
        tok = access_token.removeprefix("Bearer ").strip()
        h["authorization"] = "Bearer " + tok
        if include_api_key:
            h["x-api-key"] = tok
    return h


def get_zcode_jwt(chat_token, log=print):
    """chat.z.ai token -> OAuth device flow -> zcodeJWT. Returns None on failure."""
    poll_token = secrets.token_hex(32)

    # init/poll carry the self-chosen poll token; x-api-key is dropped there.
    h_init = build_headers(poll_token, include_api_key=False)
    r = requests.post(INIT, headers=h_init, json={"provider": "zai"}, timeout=30, verify=_VERIFY, proxies=_proxies())
    try:
        env = r.json()
    except Exception:
        log(f"  plan: init non-json {r.status_code} {r.text[:150]}")
        return None
    if env.get("code") != 0:
        log(f"  plan: init rejected {r.status_code} {r.text[:180]}")
        return None
    d = env.get("data") or {}
    flow_id = d.get("flow_id")
    poll_token = d.get("poll_token") or poll_token
    authorize_url = d.get("authorize_url", "")
    if not flow_id:
        log("  plan: init gave no flow_id")
        return None
    m = re.search(r"state=([^&]+)", authorize_url)
    state = m.group(1) if m else ""
    interval = int(d.get("poll_interval_sec") or 2)

    # consent with the ACCOUNT's chat.z.ai bearer token
    r = requests.post(AUTHORIZE, headers={
        "authorization": "Bearer " + chat_token,
        "content-type": "application/x-www-form-urlencoded",
        "x-region": "overseas",
        "origin": "https://chat.z.ai",
        "user-agent": "Mozilla/5.0",
    }, data={
        "client_id": CLIENT_ID,
        "redirect_uri": BRIDGE_REDIRECT,
        "state": state,
        "response_type": "code",
        "action": "approve",
    }, timeout=30, verify=_VERIFY, proxies=_proxies())
    code = None
    try:
        code = re.search(r"code=(code-[0-9a-fA-F]+)", r.json().get("redirect_url", ""))
        code = code.group(1) if code else None
    except Exception:
        pass
    if not code:
        log(f"  plan: authorize failed {r.status_code} {r.text[:180]}")
        return None

    requests.get(CALLBACK + f"?code={code}&state={state}", headers=h_init, timeout=30, verify=_VERIFY, proxies=_proxies())

    for _ in range(15):
        r = requests.get(POLL + flow_id, headers=build_headers(poll_token, include_api_key=False),
                         timeout=30, verify=_VERIFY, proxies=_proxies())
        try:
            data = r.json().get("data") or {}
        except Exception:
            data = {}
        if data.get("status") == "ready" and data.get("token"):
            return data["token"]
        time.sleep(interval)
    log("  plan: poll never became ready")
    return None


def get_plan(zcode_jwt, log=print):
    r = requests.get(BALANCE, headers=build_headers(zcode_jwt), timeout=30, verify=_VERIFY, proxies=_proxies())
    try:
        j = r.json()
    except Exception:
        log(f"  plan: balance non-json {r.status_code} {r.text[:150]}")
        return None
    data = j.get("data") or {}
    active = [p for p in (data.get("plans") or []) if p.get("status") == "active"]
    ids = [p.get("plan_id") for p in active]
    return {
        "plans": ids,
        "starter": STARTER_PLAN_ID in ids,
        "referral": REFERRAL_PLAN_ID in ids,
        "code": j.get("code"),
        "msg": j.get("msg"),
        "raw": data,
    }


def check_account_plan(chat_token, log=print):
    zjwt = get_zcode_jwt(chat_token, log=log)
    if not zjwt:
        return None
    return get_plan(zjwt, log=log)


if __name__ == "__main__":
    tok = sys.argv[1] if len(sys.argv) > 1 else None
    if not tok:
        print("usage: python plan_check.py <chat.z.ai token>")
        sys.exit(1)
    out = check_account_plan(tok)
    print(json.dumps(out, indent=1)[:2500] if out else "no plan data")


