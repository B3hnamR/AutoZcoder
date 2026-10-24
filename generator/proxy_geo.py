"""Online country lookup for proxy hosts.

Nothing is downloaded or installed. The 2-letter country of a proxy's IP is asked
of free public lookup sites — ip-api.com and geojs.io in batches of up to 100,
ipwho.is and ipinfo.io one IP at a time as a last resort — and the callers keep the
answers in ``ip_country.json``. A lookup that fails simply returns less: callers
carry on with whatever their cache already holds, and a provider that errors or
rate-limits is skipped for a few minutes.
"""
from __future__ import annotations

import ipaddress
import re
import threading
import time

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
BATCH = 100                 # IPs per request for the batch providers
SINGLE_LIMIT = 40           # most single lookups per provider per call
DOWN_SECONDS = 300          # how long a failing/rate-limited provider is skipped

_lock = threading.Lock()
_down: dict = {}            # provider name -> unix time it may be tried again
_memo: dict = {}            # ip -> country, for repeated single lookups in one process
_CC = re.compile(r"^[A-Za-z]{2}$")


def valid_ip(host) -> bool:
    """A public IP literal (hostnames, private and reserved ranges can't be looked up)."""
    try:
        return ipaddress.ip_address(str(host).strip().strip("[]")).is_global
    except ValueError:
        return False


def _is_down(name: str) -> bool:
    with _lock:
        return _down.get(name, 0) > time.time()


def _mark_down(name: str, seconds: int = DOWN_SECONDS) -> None:
    with _lock:
        _down[name] = time.time() + seconds


def _headers() -> dict:
    return {"User-Agent": UA}


# ----------------------------------------------------------------------------- providers

def _ipapi_batch(ips, timeout) -> dict:
    r = requests.post("http://ip-api.com/batch?fields=status,query,countryCode",
                      json=list(ips), headers=_headers(), timeout=timeout)
    if r.status_code == 429:
        raise RuntimeError("rate limited")
    r.raise_for_status()
    return {row["query"]: row["countryCode"].upper() for row in r.json()
            if row.get("status") == "success" and _CC.match(row.get("countryCode") or "")}


def _geojs_batch(ips, timeout) -> dict:
    r = requests.get("https://get.geojs.io/v1/ip/country.json", params={"ip": ",".join(ips)},
                     headers=_headers(), timeout=timeout)
    if r.status_code == 429:
        raise RuntimeError("rate limited")
    r.raise_for_status()
    data = r.json()
    rows = data if isinstance(data, list) else [data]
    return {row["ip"]: row["country"].upper() for row in rows
            if row.get("ip") and _CC.match(row.get("country") or "")}


def _ipwho_single(ip, timeout) -> str:
    r = requests.get(f"https://ipwho.is/{ip}", params={"fields": "success,country_code"},
                     headers=_headers(), timeout=timeout)
    if r.status_code == 429:
        raise RuntimeError("rate limited")
    data = r.json()
    code = data.get("country_code") or ""
    return code.upper() if data.get("success") and _CC.match(code) else ""


def _ipinfo_single(ip, timeout) -> str:
    r = requests.get(f"https://ipinfo.io/{ip}/country", headers=_headers(), timeout=timeout)
    if r.status_code == 429:
        raise RuntimeError("rate limited")
    text = (r.text or "").strip()
    return text.upper() if r.ok and _CC.match(text) else ""


_BATCH_PROVIDERS = (("ip-api", _ipapi_batch), ("geojs", _geojs_batch))
_SINGLE_PROVIDERS = (("ipwho", _ipwho_single), ("ipinfo", _ipinfo_single))


# ----------------------------------------------------------------------------- public API

def lookup_many(ips, budget_s: float = 20.0, progress=None) -> dict:
    """{ip: 'DE'} for every IP that could be resolved within ``budget_s`` seconds.

    Batch providers go first (a thousand IPs take about ten requests), then the
    single-IP ones for whatever is left. Unresolvable IPs are simply absent."""
    todo = list(dict.fromkeys(ip for ip in (str(i).strip() for i in ips if i) if valid_ip(ip)))
    out: dict = {}
    deadline = time.time() + max(1.0, float(budget_s))

    def remaining() -> float:
        return deadline - time.time()

    for name, fn in _BATCH_PROVIDERS:
        if not todo or remaining() <= 0:
            break
        if _is_down(name):
            continue
        for i in range(0, len(todo), BATCH):
            if remaining() <= 0:
                break
            try:
                out.update(fn(todo[i:i + BATCH], timeout=min(10.0, max(2.0, remaining()))))
            except Exception:
                _mark_down(name)
                break
            if progress:
                progress(len(out), len(todo))
        todo = [ip for ip in todo if ip not in out]
    for name, fn in _SINGLE_PROVIDERS:
        if not todo or remaining() <= 0:
            break
        if _is_down(name):
            continue
        for ip in todo[:SINGLE_LIMIT]:
            if remaining() <= 0:
                break
            try:
                cc = fn(ip, timeout=min(8.0, max(2.0, remaining())))
            except Exception:
                _mark_down(name)
                break
            if cc:
                out[ip] = cc
        todo = [ip for ip in todo if ip not in out]
    return out


def country(host) -> str:
    """2-letter country of one IP ('' when unknown). Remembered for this process."""
    host = str(host or "").strip().strip("[]")
    if host in _memo:
        return _memo[host]
    cc = lookup_many([host], budget_s=6.0).get(host, "")
    if cc:
        _memo[host] = cc
    return cc


def lookup(host) -> dict:
    """{'country': 'DE'} (empty when unknown)."""
    return {"country": country(host)}
