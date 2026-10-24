"""Plain-language descriptions of network and API failures.

Deploy tools and the dashboard used to surface raw library text such as
"HTTPSConnectionPool(host='api.vercel.com', port=443): Max retries exceeded
with url: /v13/deployments/dpl_... (Caused by SSLError(SSLEOFError(8, ...)))".
These helpers say what actually happened and what to do about it.
"""
from __future__ import annotations

import re


def _chain_text(exc: BaseException) -> str:
    """str() of the exception and everything it wraps (cause/context chain)."""
    parts, seen, cur = [], set(), exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        parts.append(f"{type(cur).__name__} {cur}")
        cur = cur.__cause__ or cur.__context__
    return " | ".join(parts)


def host_of(exc: BaseException) -> str:
    m = re.search(r"host='([^']+)'", str(exc)) or re.search(r"https?://([^/\s:'\"]+)", str(exc))
    return m.group(1) if m else ""


def is_transient(exc: BaseException) -> bool:
    """A network blip worth retrying (not a bad token, not a refused request)."""
    hay = _chain_text(exc).lower()
    if "certificate_verify_failed" in hay or "certificate verify failed" in hay:
        return False                      # retrying will not make an untrusted certificate trusted
    return any(k in hay for k in (
        "eof occurred", "unexpected_eof", "sslerror", "connectionreset", "connection reset",
        "connection aborted", "remotedisconnected", "timed out", "timeout", "temporarily",
        "connectionerror", "protocolerror", "chunkedencodingerror", "name resolution",
        "getaddrinfo"))


def describe_exception(exc: BaseException, target: str = "") -> str:
    """One readable sentence for a failed network call."""
    hay = _chain_text(exc).lower()
    where = target or host_of(exc) or "the server"
    again = " This is usually temporary - try again."
    if any(k in hay for k in ("nameresolutionerror", "getaddrinfo", "name or service not known",
                              "errno 11001", "no address associated")):
        return (f"Could not look up {where} (DNS failed). Check your internet connection, "
                "DNS settings or VPN." + again)
    if "certificate_verify_failed" in hay or "certificate verify failed" in hay:
        return (f"The security certificate of {where} was not trusted. An antivirus or proxy "
                "that re-signs HTTPS traffic is the usual cause.")
    if any(k in hay for k in ("eof occurred", "unexpected_eof", "sslerror", "ssl:")):
        return (f"The secure connection to {where} was cut off while it was being set up "
                "(network or ISP interference)." + again)
    if "proxyerror" in hay or "proxy" in hay and "tunnel" in hay:
        return f"The proxy in between refused or dropped the connection to {where}."
    if "readtimeout" in hay or "read timed out" in hay:
        return f"{where} accepted the connection but did not answer in time." + again
    if "connecttimeout" in hay or "timed out" in hay:
        return f"Timed out connecting to {where}. Check your connection or VPN." + again
    if any(k in hay for k in ("connectionreset", "connection reset", "connection aborted",
                              "remotedisconnected", "remote end closed", "protocolerror",
                              "chunkedencodingerror", "incompleteread")):
        return f"{where} dropped the connection mid-request." + again
    if "connection refused" in hay or "connectionrefused" in hay or "errno 111" in hay or "winerror 10061" in hay:
        return f"{where} refused the connection (nothing is listening there)."
    text = str(exc).strip() or type(exc).__name__
    return f"{type(exc).__name__}: {text[:200]}"


def _message_from_body(body: str) -> str:
    """Best-effort provider message out of a JSON error body."""
    import json
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return (body or "").strip()[:160]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:200]
        if isinstance(err, str) and err:
            return err[:200]
        errs = data.get("errors")
        if isinstance(errs, list) and errs:
            first = errs[0]
            msg = first.get("message") if isinstance(first, dict) else first
            if msg:
                return str(msg)[:200]
        for k in ("message", "detail", "msg"):
            if data.get(k):
                return str(data[k])[:200]
    return ""


def describe_http_error(provider: str, status: int, body: str = "", doing: str = "complete the request") -> str:
    """One readable sentence for a non-2xx answer from a provider API."""
    detail = _message_from_body(body)
    tail = f" ({detail})" if detail else ""
    if status in (401, 403):
        return (f"{provider} rejected the API token (HTTP {status}). Check that it is correct, "
                f"has not expired or been revoked, and is allowed to {doing}.{tail}")
    if status == 402:
        return f"{provider} says this needs a paid plan or has hit a plan limit (HTTP 402).{tail}"
    if status == 404:
        return (f"{provider} could not find what the request pointed at (HTTP 404) - "
                f"check the account/organization id and the names you entered.{tail}")
    if status in (409, 422):
        return (f"{provider} refused it as a conflict (HTTP {status}) - the name is probably "
                f"already taken or invalid; pick a different one.{tail}")
    if status == 429:
        return f"{provider} is rate-limiting this token (HTTP 429). Wait a minute and try again.{tail}"
    if status >= 500:
        return (f"{provider} had a server problem (HTTP {status}). This is usually temporary - "
                f"try again in a minute.{tail}")
    return f"{provider} could not {doing} (HTTP {status}).{tail}"


def friendly(exc: BaseException) -> str:
    """Best sentence for ANY exception reaching an API boundary."""
    msg = getattr(exc, "user_message", None)
    if msg:
        return str(msg)
    try:
        import requests
        if isinstance(exc, requests.RequestException):
            return describe_exception(exc)
    except ImportError:
        pass
    try:
        import httpx
        if isinstance(exc, httpx.HTTPError):
            return describe_exception(exc)
    except ImportError:
        pass
    if isinstance(exc, (ValueError,)):
        return str(exc)
    if isinstance(exc, OSError) and not isinstance(exc, (FileNotFoundError, PermissionError)):
        return describe_exception(exc)
    return f"{type(exc).__name__}: {str(exc)[:240]}" if str(exc) else type(exc).__name__
