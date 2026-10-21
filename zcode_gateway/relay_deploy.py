"""Edge relay deployers and testing for AutoZCoder / ZcodeGateway.

Deploys serverless edge worker relays to Cloudflare Workers, Vercel Edge,
Deno Deploy, and Netlify Functions. All four relays implement the shared
9Router specification:
    x-relay-target: https://zcode.z.ai
    x-relay-path:   /api/v1/zcode-plan/anthropic/v1/messages
"""
from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from typing import Any

import urllib3

urllib3.disable_warnings()

# ---------------------------------------------------------------------------
# HTTP session: verify TLS against the Windows system cert store.
#
# A frozen PyInstaller build has no certifi bundle next to it, and some AV /
# corporate proxies re-sign TLS chains — both surface as
# "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate" on the
# deploy APIs (api.netlify.com etc). truststore makes requests use the OS
# trust store instead, which is what browsers do.
# ---------------------------------------------------------------------------
try:
    import truststore
    import requests

    truststore.inject_into_ssl()
    _DEPLOY_SESSION = requests.Session()
except Exception:  # truststore not installed or not applicable
    import requests

    _DEPLOY_SESSION = requests.Session()
    try:
        import certifi
        _DEPLOY_SESSION.verify = certifi.where()
    except Exception:
        pass

# back-compat module-level names (deploy code calls requests.* directly)
get = _DEPLOY_SESSION.get
post = _DEPLOY_SESSION.post
put = _DEPLOY_SESSION.put
patch = _DEPLOY_SESSION.patch
delete = _DEPLOY_SESSION.delete

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# ---------------------------------------------------------------------------
# Worker Codes
# ---------------------------------------------------------------------------

CLOUDFLARE_WORKER_CODE = """export default {
  async fetch(request, env, ctx) {
    const target = request.headers.get("x-relay-target");
    const relayPath = request.headers.get("x-relay-path") || "/";
    if (!target) {
      return new Response(JSON.stringify({ error: "Missing x-relay-target header" }), {
        status: 400,
        headers: { "content-type": "application/json" },
      });
    }
    const targetUrl = target.replace(/\\/$/, "") + relayPath;
    const newRequestInit = {
      method: request.method,
      headers: new Headers(request.headers),
    };
    if (request.method !== "GET" && request.method !== "HEAD") {
      newRequestInit.body = request.body;
      newRequestInit.duplex = "half";
    }
    newRequestInit.headers.delete("x-relay-target");
    newRequestInit.headers.delete("x-relay-path");
    newRequestInit.headers.delete("host");
    try {
      const response = await fetch(targetUrl, newRequestInit);
      return new Response(response.body, {
        status: response.status,
        headers: response.headers,
      });
    } catch (error) {
      return new Response(JSON.stringify({ error: error.message }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    }
  },
};
"""

VERCEL_RELAY_CODE = """export const config = { runtime: "edge" };

export default async function handler(req) {
  const target = req.headers.get("x-relay-target");
  const relayPath = req.headers.get("x-relay-path") || "/";
  if (!target) {
    return new Response(JSON.stringify({ error: "Missing x-relay-target header" }), {
      status: 400,
      headers: { "content-type": "application/json" },
    });
  }
  const targetUrl = target.replace(/\\/$/, "") + relayPath;
  const rawHeaders = {};
  for (const [k, v] of req.headers.entries()) rawHeaders[k] = v;
  delete rawHeaders["x-relay-target"];
  delete rawHeaders["x-relay-path"];
  delete rawHeaders["host"];
  const response = await fetch(targetUrl, {
    method: req.method,
    headers: rawHeaders,
    body: req.method !== "GET" && req.method !== "HEAD" ? req.body : undefined,
    duplex: "half",
  });
  return new Response(response.body, {
    status: response.status,
    headers: response.headers,
  });
}
"""

DENO_RELAY_CODE = """Deno.serve(async (request) => {
  const target = request.headers.get("x-relay-target");
  const relayPath = request.headers.get("x-relay-path") || "/";
  if (!target) {
    return new Response(JSON.stringify({ error: "Missing x-relay-target header" }), {
      status: 400,
      headers: { "content-type": "application/json" },
    });
  }
  const targetUrl = target.replace(/\\/$/, "") + relayPath;
  const newHeaders = new Headers(request.headers);
  newHeaders.delete("x-relay-target");
  newHeaders.delete("x-relay-path");
  newHeaders.delete("host");
  const init = {
    method: request.method,
    headers: newHeaders,
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = request.body;
    init.duplex = "half";
  }
  try {
    const response = await fetch(targetUrl, init);
    return new Response(response.body, {
      status: response.status,
      headers: response.headers,
    });
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 502,
      headers: { "content-type": "application/json" },
    });
  }
});
"""

NETLIFY_RELAY_CODE = """exports.handler = async (event) => {
  const headers = {};
  for (const [k, v] of Object.entries(event.headers || {})) {
    headers[k.toLowerCase()] = v;
  }
  const target = headers["x-relay-target"];
  const relayPath = headers["x-relay-path"] || "/";
  if (!target) {
    return {
      statusCode: 400,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ error: "Missing x-relay-target header" }),
    };
  }
  const targetUrl = target.replace(/\\/$/, "") + relayPath;
  const forwardHeaders = { ...event.headers };
  for (const k of Object.keys(forwardHeaders)) {
    const lower = k.toLowerCase();
    if (lower === "x-relay-target" || lower === "x-relay-path" || lower === "host") {
      delete forwardHeaders[k];
    }
  }
  try {
    const upstream = await fetch(targetUrl, {
      method: event.httpMethod,
      headers: forwardHeaders,
      body: event.httpMethod !== "GET" && event.httpMethod !== "HEAD" && event.body
        ? event.isBase64Encoded
          ? Buffer.from(event.body, "base64")
          : event.body
        : undefined,
    });
    const responseHeaders = {};
    upstream.headers.forEach((value, key) => {
      responseHeaders[key] = value;
    });
    const contentType = upstream.headers.get("content-type") || "";
    const isText = new RegExp("^(text/|[^;]*json|[^;]*event-stream|[^;]*javascript|[^;]*xml|[^;]*urlencoded)", "i").test(contentType);
    const rawBody = await upstream.arrayBuffer();
    const responseBody = isText
      ? Buffer.from(rawBody).toString("utf8")
      : Buffer.from(rawBody).toString("base64");
    return {
      statusCode: upstream.status,
      headers: responseHeaders,
      body: responseBody,
      isBase64Encoded: !isText,
    };
  } catch (error) {
    return {
      statusCode: 502,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ error: error.message || "Relay fetch failed" }),
    };
  }
};
"""

NETLIFY_INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>AutoZCoder Relay</title></head>
<body><p>AutoZCoder relay function lives at <code>/.netlify/functions/relay</code>.</p></body></html>
"""


# ---------------------------------------------------------------------------
# Cloudflare Deploy
# ---------------------------------------------------------------------------

def deploy_cloudflare(account_id: str, api_token: str, project_name: str = "") -> dict[str, Any]:
    account_id = (account_id or "").strip()
    api_token = (api_token or "").strip()
    project_name = (project_name or "").strip() or f"cloudflare-relay-{int(time.time())}"

    if not account_id or not api_token:
        raise ValueError("Cloudflare Account ID and API Token are required")

    script_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{project_name}"
    headers = {"Authorization": f"Bearer {api_token}", "User-Agent": UA}

    # 1. Upload Worker Script via multipart/form-data
    metadata = {
        "main_module": "index.js",
        "compatibility_date": "2024-03-20",
        "observability": {"enabled": True},
    }
    files = {
        "index.js": ("index.js", CLOUDFLARE_WORKER_CODE.encode("utf-8"), "application/javascript+module"),
        "metadata": ("metadata.json", json.dumps(metadata).encode("utf-8"), "application/json"),
    }
    upload_res = requests.put(script_url, headers=headers, files=files, timeout=45)
    if not upload_res.ok:
        err = upload_res.json() if "application/json" in upload_res.headers.get("content-type", "") else {}
        msg = (err.get("errors") or [{}])[0].get("message") or f"Cloudflare upload failed ({upload_res.status_code})"
        raise RuntimeError(msg)

    # 2. Enable workers.dev subdomain — per script. Enabling is per-script,
    # not account-wide: a failed enable used to produce a stored-but-dead
    # URL. Check the response and refuse to return a URL that isn't live.
    subdomain_url = f"{script_url}/subdomain"
    enable_res = requests.post(subdomain_url, headers=headers, json={"enabled": True}, timeout=30)
    enabled = False
    if enable_res.ok:
        try:
            enabled = bool((enable_res.json().get("result") or {}).get("enabled"))
        except ValueError:
            enabled = False
    if not enabled:
        raise RuntimeError(
            "Worker deployed but enabling its workers.dev route failed "
            f"(HTTP {enable_res.status_code}). Enable it in the Cloudflare "
            "dashboard (Workers → your worker → Settings → Domains & Routes)."
        )

    # 3. Retrieve account workers.dev subdomain
    account_subdomain_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/subdomain"
    sub_res = requests.get(account_subdomain_url, headers=headers, timeout=30)
    subdomain = ""
    if sub_res.ok:
        data = sub_res.json().get("result") or {}
        subdomain = data.get("subdomain") or ""

    if not subdomain:
        raise RuntimeError(
            "Worker deployed but failed to retrieve workers.dev subdomain. "
            "Make sure you have set up a workers.dev subdomain in your Cloudflare Dashboard."
        )

    deploy_url = f"https://{project_name}.{subdomain}.workers.dev"
    return {
        "ok": True,
        "name": project_name,
        "type": "cloudflare",
        "url": deploy_url,
    }


# ---------------------------------------------------------------------------
# Vercel Deploy
# ---------------------------------------------------------------------------

def deploy_vercel(vercel_token: str, project_name: str = "") -> dict[str, Any]:
    vercel_token = (vercel_token or "").strip()
    project_name = (project_name or "").strip() or f"vercel-relay-{int(time.time())}"

    if not vercel_token:
        raise ValueError("Vercel API token is required")

    headers = {
        "Authorization": f"Bearer {vercel_token}",
        "Content-Type": "application/json",
        "User-Agent": UA,
    }

    # 1. Create deployment
    payload = {
        "name": project_name,
        "files": [
            {"file": "api/relay.js", "data": VERCEL_RELAY_CODE},
            {"file": "package.json", "data": json.dumps({"name": project_name, "version": "1.0.0"})},
            {"file": "vercel.json", "data": json.dumps({"rewrites": [{"source": "/(.*)", "destination": "/api/relay"}]})},
        ],
        "projectSettings": {"framework": None},
        "target": "production",
    }
    res = requests.post("https://api.vercel.com/v13/deployments", headers=headers, json=payload, timeout=45)
    if not res.ok:
        err = res.json() if "application/json" in res.headers.get("content-type", "") else {}
        raise RuntimeError(err.get("error", {}).get("message") or f"Vercel deployment failed ({res.status_code})")

    deployment = res.json()
    deployment_id = deployment.get("id") or deployment.get("uid")
    project_id = deployment.get("projectId") or project_name

    # 2. Disable SSO/deployment protection so the relay is publicly callable
    try:
        requests.patch(f"https://api.vercel.com/v9/projects/{project_id}", headers=headers, json={"ssoProtection": None}, timeout=15)
    except Exception:
        pass

    # 3. Poll readiness (up to 120s)
    deadline = time.time() + 120
    ready_url = ""
    while time.time() < deadline:
        poll = requests.get(f"https://api.vercel.com/v13/deployments/{deployment_id}", headers=headers, timeout=20)
        if poll.ok:
            data = poll.json()
            state = data.get("readyState")
            if state == "READY":
                ready_url = data.get("url")
                break
            if state in ("ERROR", "CANCELED"):
                raise RuntimeError(f"Vercel deployment failed with state: {state}")
        time.sleep(3)

    if not ready_url:
        raise RuntimeError("Vercel deployment timed out waiting for READY status")

    deploy_url = f"https://{ready_url}"
    return {
        "ok": True,
        "name": project_name,
        "type": "vercel",
        "url": deploy_url,
    }


# ---------------------------------------------------------------------------
# Deno Deploy
# ---------------------------------------------------------------------------

def deploy_deno(deno_token: str, org_domain: str, app_name: str = "") -> dict[str, Any]:
    deno_token = (deno_token or "").strip()
    org_domain = (org_domain or "").strip()
    app_name = (app_name or "").strip() or f"deno-relay-{int(time.time())}"

    if not deno_token:
        raise ValueError("Deno Deploy API token is required")
    if not org_domain:
        raise ValueError("Organization domain is required (e.g. your-org.deno.net)")

    headers = {
        "Authorization": f"Bearer {deno_token}",
        "Content-Type": "application/json",
        "User-Agent": UA,
    }

    # 1. Create app
    app_res = requests.post("https://api.deno.com/v2/apps", headers=headers, json={
        "slug": app_name,
        "labels": {"custom.kind": "autozcoder-relay"},
        "config": {"install": "deno install", "runtime": {"type": "dynamic", "entrypoint": "main.ts"}},
    }, timeout=30)

    if not app_res.ok:
        if app_res.status_code == 409:
            raise RuntimeError(f'App "{app_name}" already exists. Choose a different name.')
        raise RuntimeError(f"Failed to create Deno app ({app_res.status_code}): {app_res.text[:120]}")

    app_data = app_res.json()
    app_id = app_data.get("id")

    # 2. Deploy script asset
    deploy_res = requests.post(f"https://api.deno.com/v2/apps/{app_id}/deploy", headers=headers, json={
        "assets": {
            "main.ts": {
                "kind": "file",
                "content": DENO_RELAY_CODE,
                "encoding": "utf-8",
            }
        }
    }, timeout=45)

    if not deploy_res.ok:
        try:
            requests.delete(f"https://api.deno.com/v2/apps/{app_id}", headers=headers, timeout=10)
        except Exception:
            pass
        raise RuntimeError(f"Deno deployment failed ({deploy_res.status_code}): {deploy_res.text[:120]}")

    revision = deploy_res.json()
    revision_id = revision.get("id")

    # 3. Poll revision status (up to 60s)
    deadline = time.time() + 60
    succeeded = False
    while time.time() < deadline:
        rev_res = requests.get(f"https://api.deno.com/v2/revisions/{revision_id}", headers=headers, timeout=15)
        if rev_res.ok:
            st = rev_res.json().get("status")
            if st == "succeeded":
                succeeded = True
                break
            if st not in ("queued", "building"):
                break
        time.sleep(2)

    if not succeeded:
        try:
            requests.delete(f"https://api.deno.com/v2/apps/{app_id}", headers=headers, timeout=10)
        except Exception:
            pass
        raise RuntimeError("Deno deployment timed out or failed to reach succeeded state")

    org_slug = org_domain.split(".")[0].strip()
    deploy_url = f"https://{app_name}.{org_slug}.deno.net"
    return {
        "ok": True,
        "name": app_name,
        "type": "deno",
        "url": deploy_url,
    }


# ---------------------------------------------------------------------------
# Netlify Deploy
# ---------------------------------------------------------------------------

def _build_netlify_zip() -> tuple[bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("relay.js", NETLIFY_RELAY_CODE)
    zip_bytes = buf.getvalue()
    sha256 = hashlib.sha256(zip_bytes).hexdigest()
    return zip_bytes, sha256


def deploy_netlify(netlify_token: str, site_name: str = "") -> dict[str, Any]:
    netlify_token = (netlify_token or "").strip()
    site_name = (site_name or "").strip().lower() or f"netlify-relay-{int(time.time())}"
    # Netlify site names: lowercase alphanumerics + hyphens
    site_name = "".join(c if c.isalnum() or c == "-" else "-" for c in site_name).strip("-")

    if not netlify_token:
        raise ValueError("Netlify API token is required")

    headers = {
        "Authorization": f"Bearer {netlify_token}",
        "Content-Type": "application/json",
        "User-Agent": UA,
    }

    # 1. Create site
    site_res = requests.post("https://api.netlify.com/api/v1/sites", headers=headers, json={"name": site_name}, timeout=30)
    if not site_res.ok:
        if site_res.status_code == 422:
            raise RuntimeError(f'Site name "{site_name}" is taken — choose a different name.')
        err = site_res.json() if "application/json" in site_res.headers.get("content-type", "") else {}
        raise RuntimeError(err.get("message") or f"Failed to create Netlify site ({site_res.status_code})")

    site = site_res.json()
    site_id = site.get("id") or site.get("site_id")
    site_url = site.get("ssl_url") or site.get("url")

    # 2. Prepare digest bundle
    index_bytes = NETLIFY_INDEX_HTML.encode("utf-8")
    index_sha1 = hashlib.sha1(index_bytes).hexdigest()
    zip_bytes, fn_sha256 = _build_netlify_zip()

    deploy_res = requests.post(f"https://api.netlify.com/api/v1/sites/{site_id}/deploys", headers=headers, json={
        "files": {"/index.html": index_sha1},
        "functions": {"relay": fn_sha256},
    }, timeout=30)

    if not deploy_res.ok:
        raise RuntimeError(f"Failed to create Netlify deploy ({deploy_res.status_code}): {deploy_res.text[:120]}")

    deploy_data = deploy_res.json()
    deploy_id = deploy_data.get("id")
    required_files = set(deploy_data.get("required") or [])
    required_functions = set(deploy_data.get("required_functions") or [])

    # 3. Upload files asked for. Netlify's `required` lists FILE PATHS and
    # `required_functions` lists FUNCTION NAMES — comparing them against the
    # SHA digests we computed (old code) always failed both membership tests,
    # so NOTHING was ever uploaded and every deploy timed out.
    oct_headers = {"Authorization": f"Bearer {netlify_token}", "Content-Type": "application/octet-stream", "User-Agent": UA}
    if "/index.html" in required_files or index_sha1 in required_files:
        requests.put(f"https://api.netlify.com/api/v1/deploys/{deploy_id}/files/index.html", headers=oct_headers, data=index_bytes, timeout=30)

    if "relay" in required_functions or fn_sha256 in required_functions:
        requests.put(f"https://api.netlify.com/api/v1/deploys/{deploy_id}/functions/relay?runtime=js", headers=oct_headers, data=zip_bytes, timeout=45)

    # 4. Poll until ready (up to 90s)
    deadline = time.time() + 90
    ready = False
    while time.time() < deadline:
        st_res = requests.get(f"https://api.netlify.com/api/v1/deploys/{deploy_id}", headers=headers, timeout=15)
        if st_res.ok and st_res.json().get("state") == "ready":
            ready = True
            break
        time.sleep(3)

    if not ready:
        raise RuntimeError("Netlify deployment timed out waiting for ready state")

    deploy_url = f"{site_url.rstrip('/')}/.netlify/functions/relay"
    return {
        "ok": True,
        "name": site_name,
        "type": "netlify",
        "url": deploy_url,
    }


# ---------------------------------------------------------------------------
# Test Relay Connectivity
# ---------------------------------------------------------------------------

def ping_relay(relay_url: str, timeout: float = 12.0) -> dict[str, Any]:
    relay_url = (relay_url or "").strip()
    if not relay_url or not relay_url.startswith("http"):
        return {"ok": False, "status": 400, "error": "Invalid relay URL"}

    headers = {
        "x-relay-target": "https://httpbin.org",
        "x-relay-path": "/get",
        "User-Agent": UA,
    }
    t0 = time.time()
    try:
        r = requests.get(relay_url, headers=headers, timeout=timeout, verify=False)
        elapsed_ms = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            return {"ok": True, "status": 200, "elapsed_ms": elapsed_ms}
        return {"ok": False, "status": r.status_code, "error": f"HTTP {r.status_code} ({r.text[:80]})", "elapsed_ms": elapsed_ms}
    except Exception as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        return {"ok": False, "status": 500, "error": f"{type(e).__name__}: {e}", "elapsed_ms": elapsed_ms}


# Backward-compatible alias
test_relay = ping_relay
