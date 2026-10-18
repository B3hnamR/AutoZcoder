"""Proxy text parsing shared by the generator and the gateway.

One parser for every input shape we accept:
  * ``ip:port``, ``scheme://ip:port``, ``user:pass@ip:port``
  * provider style ``host:port:user:pass`` (a trailing ``{note}`` is ignored)
  * CIDR ranges (up to /16), HTML tables, JSON records (ip/host + port fields)
  * ``ip:port:US:elite`` style lists, where the trailing fields are facts, not a login

The gateway imports this module too, so the frozen build never needs the
generator's third-party dependencies just to parse a pasted list.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
from urllib.parse import quote

PROTOCOLS = ("http", "socks4", "socks5")
ALL_PROTOCOLS = PROTOCOLS
SCHEME_ALIASES = {
    "http": "http", "https": "https",
    "socks4": "socks4", "socks4a": "socks4",
    "socks5": "socks5", "socks5h": "socks5",
}

# [scheme://][user:pass@]ip[/cidr]:port[:user:pass]
PROXY_RE = re.compile(
    r"(?:(?P<scheme>[a-z][a-z0-9]*)://)?"
    r"(?:(?P<user>[^\s:@/<>\"']{1,64}):(?P<pw>[^\s@/<>\"']{0,64})@)?"
    r"(?<![\d.])(?P<host>(?:\d{1,3}\.){3}\d{1,3})(?:/(?P<cidr>\d{1,2}))?:(?P<port>\d{1,5})(?!\d)"
    r"(?::(?P<u2>[^\s:@/{}<>\"']{1,200}):(?P<p2>[^\s:@/{}<>\"']{1,200}))?",
    re.IGNORECASE,
)
IP_RE = re.compile(r"[0-9a-fA-F:.]{3,45}")
HTML_CELLS_RE = re.compile(r"\s*</t[dh]\s*>\s*<t[dh][^>]*>\s*", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
MIN_CIDR_PREFIX = 16
MAX_PARSED = 200_000
JSON_HOST_KEYS = ("ip", "host", "ip_address", "address", "proxy_address", "hostname")
JSON_PORT_KEYS = ("port", "proxy_port")
JSON_PROTO_KEYS = ("protocols", "protocol", "type", "scheme", "proxy_type", "proxy_protocol")

# trailing fields that look like facts ("ip:port:US:elite"), never like a login
_NOT_A_LOGIN = {"http", "https", "socks4", "socks5", "elite", "anonymous", "transparent",
                "high", "anon", "none", "yes", "no", "true", "false", "unknown"}


def allow_loopback() -> bool:
    return os.environ.get("ZAI_ALLOW_LOOPBACK") == "1" or \
        os.environ.get("PROXYHIVE_ALLOW_LOOPBACK") == "1"


def valid_endpoint(host: str, port: int) -> bool:
    if not (0 < port < 65536):
        return False
    try:
        parts = [int(o) for o in host.split(".")]
    except ValueError:
        return False
    if len(parts) != 4 or any(p > 255 for p in parts):
        return False
    if allow_loopback():
        return True
    if host.startswith(("0.", "127.")):
        return False
    # link-local (cloud metadata lives here), multicast, reserved, broadcast
    if host.startswith("169.254") or host == "255.255.255.255":
        return False
    first = parts[0]
    if first >= 224:
        return False
    return True


def _trailing_login(m: re.Match) -> tuple[str, str] | None:
    user, pw = m["u2"], m["p2"]
    if not user:
        return None
    if m["scheme"]:                       # scheme://host:port:user:pass is unambiguous
        return user, pw
    # Provider lists also use ip:port:COUNTRY:anonymity (US:elite, de:anonymous).
    # Only the value decides: a two-letter username is still a username.
    if user.lower() in _NOT_A_LOGIN or (pw or "").lower() in _NOT_A_LOGIN:
        return None
    return user, pw


def _scheme(value: str | None, default_proto: str) -> list[str]:
    s = (value or "").lower()
    if s in SCHEME_ALIASES:
        return [SCHEME_ALIASES[s]]
    d = (default_proto or "").lower()
    if d in ("all", "any", ""):
        return list(ALL_PROTOCOLS)
    return [SCHEME_ALIASES.get(d, "http")]


def _expand_cidr(host: str, prefix: int) -> list[str]:
    if prefix < MIN_CIDR_PREFIX or prefix > 32:
        return []
    try:
        net = ipaddress.ip_network(f"{host}/{prefix}", strict=False)
    except ValueError:
        return []
    return [str(h) for h in (net.hosts() if prefix < 31 else net)]


def _json_protocols(obj: dict) -> list[str]:
    for key in JSON_PROTO_KEYS:
        val = obj.get(key)
        vals = val if isinstance(val, list) else [val]
        found = [SCHEME_ALIASES[v.lower()] for v in vals
                 if isinstance(v, str) and v.lower() in SCHEME_ALIASES]
        if found:
            return list(dict.fromkeys(found))
    return []


def _walk_json(obj, default_proto: str, out: list, seen: set, depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(obj, list):
        for item in obj:
            _walk_json(item, default_proto, out, seen, depth + 1)
    elif isinstance(obj, dict):
        host = next((obj[k] for k in JSON_HOST_KEYS if isinstance(obj.get(k), str)), None)
        port = next((obj[k] for k in JSON_PORT_KEYS if obj.get(k) not in (None, "")), None)
        if host and port is not None and re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", host.strip()):
            try:
                port_n = int(str(port).strip())
            except ValueError:
                port_n = 0
            host = host.strip()
            if valid_endpoint(host, port_n):
                protos = _json_protocols(obj) or _scheme(None, default_proto)
                user = next((obj[k] for k in ("username", "user", "login")
                             if isinstance(obj.get(k), str) and obj[k]), None)
                pw = next((obj[k] for k in ("password", "pass", "pwd")
                           if isinstance(obj.get(k), str)), "")
                for p in protos:
                    _add(out, seen, p, host, port_n, f"{user}:{pw}" if user else "")
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _walk_json(v, default_proto, out, seen, depth + 1)


def _add(out: list, seen: set, proto: str, host: str, port: int, auth: str = "") -> None:
    key = (proto, host, port)
    if key in seen or len(out) >= MAX_PARSED:
        return
    seen.add(key)
    out.append({"proto": proto, "host": host, "port": port, "auth": auth or ""})


def _scan_text(text: str, default_proto: str, out: list, seen: set) -> None:
    for m in PROXY_RE.finditer(text):
        try:
            port = int(m["port"])
        except (TypeError, ValueError):
            continue
        hosts = [m["host"]]
        if m["cidr"] is not None:
            hosts = _expand_cidr(m["host"], int(m["cidr"]))
        protos = _scheme(m["scheme"], default_proto)
        login = (m["user"], m["pw"] or "") if m["user"] else _trailing_login(m)
        auth = f"{login[0]}:{login[1]}" if login else ""
        for host in hosts:
            if not valid_endpoint(host, port):
                continue
            for p in protos:
                _add(out, seen, p, host, port, auth)


def parse_proxy_list(text: str, default_proto: str = "http") -> list[dict]:
    """Every proxy found in `text`, deduped, in source order.

    Each item: {"proto", "host", "port", "auth"} where auth is "" or "user:pass".
    """
    out: list[dict] = []
    seen: set = set()
    text = text or ""
    _scan_text(text, default_proto, out, seen)
    if "</t" in text[:3_000_000].lower():
        # HTML table: <td>1.2.3.4</td><td>8080</td>
        flat = HTML_TAG_RE.sub(" ", HTML_CELLS_RE.sub(":", text))
        _scan_text(flat, default_proto, out, seen)
    if text.lstrip()[:1] in ("[", "{"):
        try:
            _walk_json(json.loads(text), default_proto, out, seen)
        except ValueError:
            pass
    return out


def to_url(proto: str, host: str, port: int, auth: str = "") -> str:
    if auth:
        user, _, pw = auth.partition(":")
        return f"{proto}://{quote(user, safe='')}:{quote(pw, safe='')}@{host}:{port}"
    return f"{proto}://{host}:{port}"


def split_url(url: str) -> dict:
    """proto://[user:pass@]host:port -> {proto, host, port, auth}."""
    proto, _, rest = (url or "").partition("://")
    auth = ""
    if "@" in rest:
        auth, _, rest = rest.rpartition("@")
    host, _, port = rest.rpartition(":")
    try:
        port_n = int(port)
    except ValueError:
        port_n = 0
    return {"proto": proto.lower(), "host": host, "port": port_n, "auth": auth}


def redact(url: str | None) -> str | None:
    """Strip the login from a proxy URL so logs and the dashboard never show it.

    Greedy within the authority only: ``u:p@ss@host`` must lose the whole login,
    while a non-proxy URL with an ``@`` in its path must be left alone.
    """
    if not url:
        return url
    return re.sub(r"//[^/]*@", "//", url)


def parse_source_lines(text: str) -> list[tuple[str, str]]:
    """`<proto> <url>` per line (proto may be `all`)."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2 and parts[0].lower() in (*SCHEME_ALIASES, "all"):
            out.append((parts[0].lower(), parts[1]))
        elif len(parts) == 1 and parts[0].startswith("http"):
            out.append(("http", parts[0]))
    return out
