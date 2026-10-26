"""Starter-plan checker + billing-preview/claim transport for generated accounts.

Reverses the ZCode OAuth CLI flow so we can ask, per account, whether it got the
**ZCode Start Plan**, and (since the signup grant was removed) claim it from the
billing preview.

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

Start Plan (2026-10+) is no longer granted at signup. It is listed by:
    GET  /api/v1/zcode-plan/billing/preview?app_version=<APP_VERSION>
and granted by:
    POST /api/v1/zcode-plan/billing/claim   {"plan_id": "..."}
`preview` answers an EMPTY list unless the request presents app_version >= 3.16.1
(the client gates the gift list on its own version), and it also stays empty
until the account has reported app activity (`app_launch` / `app_daily_active`).
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
CALLBACK = "https://zcode.z.ai/api/v1/oauth/cli/callback/zai"
INIT = "https://zcode.z.ai/api/v1/oauth/cli/init"
POLL = "https://zcode.z.ai/api/v1/oauth/cli/poll/"
AUTHORIZE = "https://chat.z.ai/api/oauth/authorize"

# The gift/preview list is version-gated: 3.14.3..3.16.0 answer [], 3.16.1+
# name the plan. 3.16.3 is the client build this repo tracks.
APP_VERSION = (os.environ.get("ZAI_APP_VERSION") or "3.16.3").strip() or "3.16.3"
BRIDGE_REDIRECT = (
    "https://zcode.z.ai/app/oauth/login?redirect=zcode%3A%2F%2Foauth%2Fcallback"
    f"&app_version={APP_VERSION}"
)
BALANCE = f"https://zcode.z.ai/api/v1/zcode-plan/billing/balance?app_version={APP_VERSION}"
CURRENT = f"https://zcode.z.ai/api/v1/zcode-plan/billing/current?app_version={APP_VERSION}"
# `platform` is not decoration: the claim gateway answers biz 3001 (parameter
# error) when the request carries a device mid but no platform, and the
# upstream client always sends `${process.platform}-${arch}`.
PLATFORM = (os.environ.get("ZAI_PLATFORM") or "win32-x64").strip() or "win32-x64"
PREVIEW = (f"https://zcode.z.ai/api/v1/zcode-plan/billing/preview"
           f"?app_version={APP_VERSION}&platform={PLATFORM}")
CLAIM = "https://zcode.z.ai/api/v1/zcode-plan/billing/claim"
EVENT = "https://zcode.z.ai/api/v1/event/report"
TOUCH = "https://zcode.z.ai/api/v1/marketing/touch"

try:
    import certifi
    _VERIFY = certifi.where() if not os.environ.get('ZAI_INSECURE_SSL') else False
except Exception:
    _VERIFY = False


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


# --- plan ids ---------------------------------------------------------------
# Every Start Plan variant shares the prefix; the id suffix changes with each
# campaign (`-0817`, `-referral`, ...). The 1024 Dev Day giveaway is a
# different, expiring bucket and must not be mistaken for a Start Plan.
START_PLAN_PREFIX = "zcode-v3-start-plan-"
REFERRAL_PLAN_ID = "zcode-v3-start-plan-referral"
CAMPAIGN_PLAN_IDS = frozenset({
    "zcode-v3-start-plan-1024",
    "zcode-v3-start-plan-500m",
})

# Kept for callers/tests that still import the historical constant.
STARTER_PLAN_ID = "zcode-v3-start-plan-0817"


def is_start_plan(plan_id: str, include_campaign: bool = False) -> bool:
    """True for a Start Plan / Global Build id of any campaign generation."""
    pid = (plan_id or "").strip()
    if not pid.startswith(START_PLAN_PREFIX):
        return False
    if pid in CAMPAIGN_PLAN_IDS and not include_campaign:
        return False
    return True


def is_campaign_plan(plan_id: str) -> bool:
    return (plan_id or "").strip() in CAMPAIGN_PLAN_IDS


# The 16 static headers the upstream fingerprints (upstream.py STATIC_HEADERS).
STATIC_HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "http-referer": "https://zcode.z.ai",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "mid-conversation-system-2026-04-07",
}
# Stable PER-ACCOUNT device id: every account using one shared hardcoded
# x-device-mid is a direct cross-account correlation signal for risk control.
# uuid5 of the chat token keeps the id deterministic per account (the gateway
# plan-checker and the message flow then report a consistent device).
DEVICE_MID = "58aca3b3-3239-4cb7-9c70-df3e5488fb99"   # legacy fallback only


def per_account_device_mid(token: str) -> str:
    import uuid as _u
    if not token:
        return DEVICE_MID
    return str(_u.uuid5(_u.NAMESPACE_URL, 'zcode-device:' + token))


def build_headers(access_token=None, device_mid=None, include_api_key=True):
    h = dict(STATIC_HEADERS)
    h.update({
        "user-agent": f"ZCode/{APP_VERSION} ai-sdk/provider-utils/4.0.27 runtime/node.js/24",
        "x-device-mid": device_mid or per_account_device_mid(access_token),
        "x-client-language": "en-US",
        "x-client-timezone": "Asia/Tehran",
        "x-os-category": "windows",
        "x-os-version": "10.0.26200",
        "x-platform": "win32-x64",
        "x-release-channel": "production",
        "x-title": "Z Code@electron",
        "x-zcode-agent": "glm",
        "x-zcode-app-version": APP_VERSION,
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


def _active_ids(data: dict) -> list:
    return [p.get("plan_id") for p in (data.get("plans") or [])
            if p.get("status") == "active" and p.get("plan_id")]


def get_plan(zcode_jwt, log=print):
    """Read balance + entitlements. <=63M-token plans may appear only in
    `plans[].entitlements[]` (with `effective_at`) until they go live."""
    r = requests.get(BALANCE, headers=build_headers(zcode_jwt), timeout=30, verify=_VERIFY, proxies=_proxies())
    try:
        j = r.json()
    except Exception:
        log(f"  plan: balance non-json {r.status_code} {r.text[:150]}")
        return None
    data = j.get("data") or {}
    ids = _active_ids(data)
    entitlements = []
    for p in (data.get("plans") or []):
        for ent in (p.get("entitlements") or []):
            entitlements.append({
                "plan_id": ent.get("plan_id") or p.get("plan_id"),
                "name": ent.get("name") or ent.get("show_name") or p.get("name"),
                "effective_at": ent.get("effective_at"),
                "expires_at": ent.get("expires_at") or p.get("ends_at"),
            })
    return {
        "plans": ids,
        "starter": any(is_start_plan(pid) for pid in ids),
        "referral": REFERRAL_PLAN_ID in ids,
        "campaign": [pid for pid in ids if is_campaign_plan(pid)],
        "start_plans": [pid for pid in ids if is_start_plan(pid)],
        "entitlements": entitlements,
        "code": j.get("code"),
        "msg": j.get("msg"),
        "raw": data,
    }


def get_current(zcode_jwt, log=print):
    """Raw `/billing/current` data (plan list with entitlements/expiry)."""
    try:
        r = requests.get(CURRENT, headers=build_headers(zcode_jwt), timeout=30,
                         verify=_VERIFY, proxies=_proxies())
        return (r.json() or {}).get("data") or {}
    except Exception as e:
        log(f"  plan: current failed ({type(e).__name__})")
        return {}


# --- activity / preview / claim --------------------------------------------

def jwt_user_id(token: str) -> str:
    """`user_id` straight out of the (unverified) JWT payload.

    `/event/report` bodies carry it as a flat field — captured on 2026-10-24
    alongside `app_launch` — and the client never signs the payload."""
    try:
        import base64
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        return payload.get("user_id") or payload.get("sub") or ""
    except Exception:
        return ""


def _plan_events(zjwt):
    """The client's launch telemetry. `preview` answers empty until these land.

    Shape mirrors a real 3.16.3 capture: a FLAT object (not `{events:[...]}`)
    with `user_id`, and `marketing_params` as a JSON *string*. Override with
    ZAI_ACTIVITY_EVENTS (JSON list) if a capture shows a different shape.
    """
    override = os.environ.get("ZAI_ACTIVITY_EVENTS", "").strip()
    if override:
        try:
            events = json.loads(override)
            if isinstance(events, list):
                return events
        except Exception:
            pass
    mid = per_account_device_mid(zjwt)
    base = {
        "client_timezone": "Asia/Tehran",
        "client_language": "en-US",
        "event_text": "",
        "user_id": jwt_user_id(zjwt),
        "screen_resolution": "3440x1440",
        "app_version": APP_VERSION,
        "device_os_category": "windows",
        "device_os_version": "Windows 11 Pro",
        "device_mid": mid,
        "mac_id": "",
        "marketing_params": '{"utm_source":"z.ai"}',
    }
    return [
        dict(base, event_id=str(uuid.uuid4()), element_name="app_launch",
             event_region="app", event_type="view", event_extra_detail={}),
        dict(base, event_id=str(uuid.uuid4()), element_name="app_daily_active",
             event_region="app", event_type="view", event_extra_detail={}),
    ]


def report_app_activity(zcode_jwt, log=print) -> bool:
    """Emit launch/daily-active telemetry. Best effort: a failure only means
    `preview` may still be empty, so the caller logs and moves on."""
    if not zcode_jwt:
        return False
    h = build_headers(zcode_jwt)
    ok = False
    for body in _plan_events(zcode_jwt):
        try:
            r = requests.post(EVENT, headers=h, json=body, timeout=30,
                              verify=_VERIFY, proxies=_proxies())
            try:
                j = r.json()
            except Exception:
                j = {}
            good = r.status_code == 200 and j.get("code") in (0, 200)
            ok = ok or good
            log(f"  plan: {body.get('element_name')} -> {r.status_code}"
                + ("" if good else f" {str(j or r.text)[:120]}"))
        except Exception as e:
            log(f"  plan: {body.get('element_name')} failed ({type(e).__name__})")
    return ok


def _walk_plan_ids(node, out):
    """Collect every `{plan_id, name}`-ish dict in an unknown response shape.

    The preview envelope has moved between `data.plans[]`, campaign
    `deliveries[].banner.buttons[].action.args` and flat items; a recursive
    scan survives all of them without pinning one shape.
    """
    if isinstance(node, dict):
        pid = node.get("plan_id") or node.get("planId")
        if isinstance(pid, str) and pid.startswith("zcode-"):
            out.append({"plan_id": pid,
                        "name": node.get("name") or node.get("show_name") or "",
                        "claimable": node.get("claimable", node.get("status", True))})
        for v in node.values():
            _walk_plan_ids(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_plan_ids(v, out)


def preview_plans(zcode_jwt, log=print) -> list:
    """Plans the account is currently eligible to claim (possibly empty)."""
    try:
        r = requests.get(PREVIEW, headers=build_headers(zcode_jwt), timeout=30,
                         verify=_VERIFY, proxies=_proxies())
    except Exception as e:
        log(f"  plan: preview failed ({type(e).__name__})")
        return []
    try:
        j = r.json()
    except Exception:
        log(f"  plan: preview non-json {r.status_code} {r.text[:120]}")
        return []
    found, seen = [], set()
    _walk_plan_ids(j, found)
    plans = []
    for p in found:
        if p["plan_id"] in seen:
            continue
        seen.add(p["plan_id"])
        plans.append(p)
    log(f"  plan: preview {r.status_code} code={j.get('code')} plans={[p['plan_id'] for p in plans]}")
    return plans


def claim_plan(zcode_jwt, plan_id, verify_param="", region="", log=print) -> dict:
    """POST /billing/claim. Returns the envelope (never None).

    `x-aliyun-captcha-verify-param` is mandatory on a real grant: a 3.16.3
    capture shows the client sending it plus `...-region` (sgp) and the
    minimal claim header set. Without it the gateway answers 3007 even for a
    plan that is currently offered.

    Codes: 0 claimed · 1003 already claimed (treat as success) ·
    3007 captcha demanded · 1001 plan gone · 1005 quota/limit.
    """
    h = build_headers(zcode_jwt)
    h["x-platform"] = PLATFORM
    h["accept"] = "*/*"
    if verify_param:
        h["x-aliyun-captcha-verify-param"] = verify_param
        if region:
            h["x-aliyun-captcha-verify-region"] = region
    try:
        r = requests.post(CLAIM, headers=h,
                          json={"plan_id": plan_id}, timeout=30,
                          verify=_VERIFY, proxies=_proxies())
    except requests.RequestException as e:
        return {"code": -1, "msg": f"request failed: {type(e).__name__}"}
    try:
        return r.json()
    except Exception:
        return {"code": -1, "msg": f"non-json {r.status_code} {r.text[:150]}"}


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
