"""HTTP-level auth + rate-limit wiring, and the non-stream complete() path.

These were gaps: every TestClient test disabled require_client_key, so the
401/403/429 wiring through deps.py could have regressed invisibly; and the
non-streaming ChatService.complete() happy path had no direct test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
from zcode_gateway.config import ClientKey, Config
from zcode_gateway.pool import Account, PoolManager
from zcode_gateway.config import PoolConfig
from zcode_gateway.registry import Registry
from zcode_gateway.service import ChatService
from zcode_gateway.store import JsonlCapture, Store


def _secured_app(tmp_path) -> Config:
    cfg = Config()
    cfg.server.require_client_key = True
    cfg.server.admin_key = "adm-test"
    cfg.server.client_keys = [ClientKey(key="ck-test", name="t", rpm=0)]
    cfg.accounts.source = "pool_file"
    cfg.accounts.pool_file = str(tmp_path / "empty.json")
    (tmp_path / "empty.json").write_text('{"accounts": []}', encoding="utf-8")
    cfg.store.sqlite_path = str(tmp_path / "g.db")
    cfg.logging.capture_dir = str(tmp_path / "logs")
    return cfg


def test_missing_client_key_is_401(tmp_path):
    app = create_app(_secured_app(tmp_path))
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions",
                        json={"model": "m", "messages": []})
    assert r.status_code == 401


def test_wrong_client_key_is_401(tmp_path):
    app = create_app(_secured_app(tmp_path))
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": "Bearer nope"},
                        json={"model": "m", "messages": []})
    assert r.status_code == 401


def test_wrong_admin_key_is_403(tmp_path):
    app = create_app(_secured_app(tmp_path))
    with TestClient(app) as client:
        r = client.get("/admin/pool/state",
                       headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 403


def test_right_keys_pass_auth(tmp_path):
    # auth must pass with the right keys (the pool itself is empty -> 503,
    # which proves we got PAST auth to routing)
    app = create_app(_secured_app(tmp_path))
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": "Bearer ck-test"},
                        json={"model": "zcode-free/solar-pro4",
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code != 401
        a = client.get("/admin/pool/state",
                       headers={"Authorization": "Bearer adm-test"})
        assert a.status_code == 200


def test_rpm_limit_returns_429_with_retry_after(tmp_path):
    cfg = _secured_app(tmp_path)
    cfg.server.client_keys = [ClientKey(key="ck-rl", name="t", rpm=1)]
    app = create_app(cfg)
    with TestClient(app) as client:
        h = {"Authorization": "Bearer ck-rl"}
        first = client.post("/v1/chat/completions", headers=h,
                            json={"model": "zcode-free/solar-pro4",
                                  "messages": [{"role": "user", "content": "hi"}]})
        second = client.post("/v1/chat/completions", headers=h,
                             json={"model": "zcode-free/solar-pro4",
                                   "messages": [{"role": "user", "content": "hi"}]})
    assert first.status_code != 429           # first request is allowed through
    assert second.status_code == 429          # second within the window is not
    assert "retry-after" in {k.lower() for k in second.headers}


# --------------------------------------------------------------------------- #
# ChatService.complete(): the non-stream happy path
# --------------------------------------------------------------------------- #


class _Resp:
    status_code = 200

    def __init__(self, body: bytes) -> None:
        self.headers = {"content-type": "text/event-stream"}
        self._body = body
        self.closed = False

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_complete_aggregates_stream_and_releases(tmp_path):
    cfg = Config()
    cfg.store.sqlite_path = str(tmp_path / "g.db")
    cfg.logging.capture_dir = str(tmp_path / "logs")
    account = Account(id="a1", access_token="t")
    pool = PoolManager(PoolConfig(), [account])
    svc = ChatService(cfg, pool, tokens=None, client=None,
                      registry=Registry(), store=Store(cfg.store.sqlite_path),
                      capture=JsonlCapture(cfg.logging.capture_dir, enabled=False))

    from tests_fixtures import sample_sse, FakeClient
    resp_client = FakeClient(sample_sse())
    svc.client = resp_client
    result = await svc.complete({"model": "GLM-5.3", "messages": [{"role":"user","content":"hi"}]},
                                model="GLM-5.3", variant="anthropic",
                                dialect="openai", client_key="k")
    resp = resp_client.responses[0]

    assert result.status == 200
    assert b"hello" in result.raw
    assert resp.closed                       # the stream was closed
    assert account.in_flight == 0            # the reservation was released
    # recorded as a non-stream exchange with usage tokens
    summary = svc.store.summary()
    assert summary["requests"] == 1
    assert summary["total_tokens"] == 4
