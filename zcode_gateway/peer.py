"""Peer (client IP) classification for the ops/admin gates.

The subtlety this module exists for: on the recommended VPS topology the
gateway binds 127.0.0.1 and a same-box reverse proxy (Caddy/nginx) forwards
internet traffic to it. Every TCP connection then arrives from 127.0.0.1, so
a naive loopback check on the connection treats the whole internet as local
— /health, /metrics and /admin/* would be public despite
expose_ops/allow_remote_admin being off.

The fix relies on uvicorn's ProxyHeadersMiddleware, which is always in the
request path when running via zcode_gateway.main (proxy_headers defaults on,
trusted_hosts=127.0.0.1): for a connection from a trusted loopback proxy
carrying X-Forwarded-For, uvicorn rewrites request.client to the first
untrusted hop — the actual visitor. So:

  direct local client        client 127.0.0.1, no XFF        -> local
  proxy, external visitor    client = visitor IP             -> remote
  proxy, local visitor       client 127.0.0.1 (XFF walked)   -> local

request.client alone is therefore sufficient and trustworthy in production.
The raw X-Forwarded-For header is deliberately NOT re-read here: without the
middleware in the path it is trivially spoofable, and with the middleware it
has already been applied. A proxy that does not send X-Forwarded-For is
indistinguishable from a local process — the deployment docs require the
proxy to pass it through (Caddy does by default).

Tests: FastAPI's TestClient uses the synthetic peer hostname "testclient"
(no TCP ever happens). It is classified as LOCAL so tests model the common
same-machine case by default; a test that needs a remote peer passes
TestClient(app, client=("203.0.113.9", 50000)) explicitly.
"""

from __future__ import annotations

from fastapi import Request

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_TESTCLIENT_HOST = "testclient"


def peer_host(request: Request) -> str:
    """The best-known source IP of the request (proxy-corrected)."""
    client = request.client
    return (client.host if client else "") or ""


def peer_is_local(request: Request) -> bool:
    """True when the request originates from this machine.

    See the module docstring for why request.client (and not the raw
    X-Forwarded-For header) is the authority: uvicorn has already applied
    proxy correction for trusted loopback proxies.
    """
    return peer_host(request) in LOOPBACK_HOSTS or peer_host(request) == _TESTCLIENT_HOST
