"""Suite-wide guard: tests must never call the live Zcode API.

The gateway's background loops (balance poll, token refresh) start with the
app lifespan; a test that boots the real app with accounts would otherwise
send real requests to api.zcode.bot. This fixture blocks DNS for that host and
fails the offending test at teardown, so an accidental live call cannot go
unnoticed.
"""

from __future__ import annotations

import socket

import pytest

_REAL_GETADDRINFO = socket.getaddrinfo


@pytest.fixture(autouse=True)
def _no_live_zcode_api(monkeypatch):
    attempts: list[str] = []

    def guarded(host, *args, **kwargs):
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        if isinstance(name, str) and (
                name == "z.ai" or name.endswith(".z.ai") or name == "api.workos.com"):
            attempts.append(name)
            raise OSError(f"test guard: refusing live call to {name}")
        return _REAL_GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)
    yield
    if attempts:
        pytest.fail(
            f"test attempted a live network call to {attempts[0]} — "
            "stub the client or disable the background poller")


@pytest.fixture(autouse=True)
def _no_real_claim_proxy_fallback(monkeypatch):
    """claim500 retries blocked claims through verified POOL proxies (live probes
    against z.ai). Tests must never touch a real pool: they opt in explicitly."""
    monkeypatch.setenv("ZAI_CLAIM500_PROXY_FALLBACK", "0")


@pytest.fixture(autouse=True)
def _no_real_country_lookups(monkeypatch):
    """Recording a proxy success looks its country up online (proxy_geo). Tests
    must never reach those sites: switch every provider off by default; the
    tests that exercise the lookup install their own fakes."""
    import sys
    geo = sys.modules.get("proxy_geo")
    if geo is not None:
        monkeypatch.setattr(geo, "_BATCH_PROVIDERS", ())
        monkeypatch.setattr(geo, "_SINGLE_PROVIDERS", ())
    yield


@pytest.fixture(autouse=True)
def _no_real_captcha_mint(monkeypatch):
    """The claim path auto-discovers tools/captcha-mint/captcha-mint.exe (a
    live Aliyun solve). Tests must never spawn it; opt in explicitly."""
    monkeypatch.setenv("ZAI_CAPTCHA_MINT", "0")
    monkeypatch.delenv("ZAI_CAPTCHA_CMD", raising=False)
    monkeypatch.delenv("ZAI_CAPTCHA_PARAM", raising=False)
