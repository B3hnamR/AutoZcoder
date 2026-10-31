"""JWT import uses only a stub provider; test tokens have no real signature."""
import base64
import json
import stat
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
from zcode_gateway.config import Config, ServerConfig
from zcode_gateway.accounts_dir import load_from_accounts_dir, parse_snapshot
from zcode_gateway.pool import load_from_pool_file

IDENT = "99aa3247-908c-433c-874f-616aa15d6e02"
HEADERS = {"Authorization": "Bearer test-admin"}


def jwt(**extra):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return part({"alg": "HS256"}) + "." + part({"sub": IDENT, **extra}) + ".dummy-signature"


def config(tmp_path):
    cfg = Config(server=ServerConfig(host="127.0.0.1", admin_key="test-admin"))
    cfg.accounts.source = "accounts_dir"
    cfg.accounts.dir = str(tmp_path / "accounts")
    cfg.accounts.pool_file = str(tmp_path / "pool.json")
    cfg.store.sqlite_path = str(tmp_path / "gateway.db")
    cfg.update.enabled = False
    return cfg


def provider(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr("zcode_gateway.api_admin.httpx.AsyncClient",
                        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))


def test_import_persists_validated_token_and_copy_fetches_current(tmp_path, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        assert request.url.path.endswith("/zcode-plan/billing/balance")
        return httpx.Response(200, json={"code": 0, "data": {"plans": [], "balances": []}})

    cfg = config(tmp_path)
    with TestClient(create_app(cfg)) as client:
        provider(monkeypatch, handler)
        token = jwt(iat=123)
        response = client.post("/admin/accounts/import-jwt", headers=HEADERS,
                               json={"token": "Bearer " + token, "email": "test@example.test"})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert token not in response.text
        assert seen == ["Bearer " + token]
        rows = load_from_accounts_dir(cfg.accounts.dir)
        assert len(rows) == 1 and rows[0].id == IDENT
        assert response.json()["account"]["imported_jwt"] is True
        assert rows[0].to_public()["imported_jwt"] is True
        assert rows[0].access_token == token and rows[0].expires_at == 0
        path = next(Path(cfg.accounts.dir).glob("*.txt"))
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0 or __import__('os').name == 'nt'
        rotated = jwt(iat=456)
        response = client.post("/admin/accounts/import-jwt", headers=HEADERS, json={"token": rotated})
        assert response.status_code == 200 and response.json()["updated"] is True
        assert len(list(Path(cfg.accounts.dir).glob("*.txt"))) == 1
        assert parse_snapshot(path)["email"] == "test@example.test"
        assert parse_snapshot(path)["access_token"] == rotated
        assert parse_snapshot(path)["imported_jwt"] == "true"
        copy = client.get(f"/admin/accounts/{IDENT}/token", headers=HEADERS)
        assert copy.json()["access_token"] == rotated
        assert "no-store" in copy.headers["cache-control"]
        assert rotated not in client.get("/admin/accounts", headers=HEADERS).text
        assert client.get(f"/admin/accounts/{IDENT}/token").status_code in (401, 403)


@pytest.mark.parametrize("token", [None, "junk", jwt(exp=1), jwt(exp=True), jwt(sub="invalid"), jwt(exp="NaN")])
def test_import_rejects_malformed_before_provider_or_persistence(tmp_path, monkeypatch, token):
    with TestClient(create_app(config(tmp_path))) as client:
        provider(monkeypatch, lambda _: pytest.fail("malformed JWT reached provider"))
        response = client.post("/admin/accounts/import-jwt", headers=HEADERS, json={"token": token})
        assert response.status_code == 400
        assert not list(tmp_path.rglob("*.txt"))


@pytest.mark.parametrize("status,payload,expected", [
    (401, {}, 400), (403, {}, 400), (200, {"code": 1005}, 400),
    (200, [], 400), (500, {}, 502),
])
def test_rejected_or_unavailable_provider_cannot_import(tmp_path, monkeypatch, status, payload, expected):
    with TestClient(create_app(config(tmp_path))) as client:
        provider(monkeypatch, lambda _: httpx.Response(status, json=payload))
        token = jwt()
        response = client.post("/admin/accounts/import-jwt", headers=HEADERS, json={"token": token})
        assert response.status_code == expected
        assert token not in response.text
        assert not list(tmp_path.rglob("*.txt"))
        assert client.get("/admin/accounts", headers=HEADERS).json()["accounts"] == []


def test_import_admin_only_and_pool_file_persistence(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cfg.accounts.source = "pool_file"
    with TestClient(create_app(cfg)) as client:
        assert client.post("/admin/accounts/import-jwt", json={"token": jwt()}).status_code in (401, 403)
        provider(monkeypatch, lambda _: httpx.Response(200, json={"code": 0, "data": {"plans": [], "balances": []}}))
        response = client.post("/admin/accounts/import-jwt", headers=HEADERS, json={"token": jwt()})
        assert response.status_code == 200
        saved = json.loads(Path(cfg.accounts.pool_file).read_text())
        assert saved["accounts"][0]["access_token"] == jwt()
        assert saved["accounts"][0]["imported_jwt"] is True
        assert load_from_pool_file(cfg.accounts.pool_file)[0].to_public()["imported_jwt"] is True


def test_failed_snapshot_write_preserves_existing_token(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    with TestClient(create_app(cfg)) as client:
        provider(monkeypatch, lambda _: httpx.Response(200, json={"code": 0, "data": {"plans": [], "balances": []}}))
        first = jwt(iat=100)
        assert client.post("/admin/accounts/import-jwt", headers=HEADERS, json={"token": first}).status_code == 200
        snapshot = next(Path(cfg.accounts.dir).glob("*.txt"))
        original = snapshot.read_bytes()

        def fail(*_args, **_kwargs):
            raise OSError("private path")

        monkeypatch.setattr("zcode_gateway.accounts_dir.write_snapshot", fail)
        response = client.post("/admin/accounts/import-jwt", headers=HEADERS, json={"token": jwt(iat=200)})
        assert response.status_code == 500
        assert "private path" not in response.text
        assert snapshot.read_bytes() == original
        assert client.get(f"/admin/accounts/{IDENT}/token", headers=HEADERS).json()["access_token"] == first


def test_import_rejects_conflicting_claims_and_bad_labels(tmp_path, monkeypatch):
    with TestClient(create_app(config(tmp_path))) as client:
        provider(monkeypatch, lambda _: pytest.fail("invalid import reached provider"))
        for body in ({"token": jwt(user_id="00000000-0000-0000-0000-000000000000")},
                     {"token": jwt(), "email": "bad\nsource:other"},
                     {"token": jwt(exp=1e100)},
                     {"token": jwt(), "email": "not-an-email"}):
            assert client.post("/admin/accounts/import-jwt", headers=HEADERS, json=body).status_code == 400


def test_reimport_reuses_pooled_account_preserving_caps_and_inflight(tmp_path, monkeypatch):
    from zcode_gateway.pool import Account
    from zcode_gateway.api_admin import import_jwt_account
    from types import SimpleNamespace
    import asyncio

    async def scenario():
        from zcode_gateway.pool import PoolManager
        cfg = config(tmp_path)
        existing = Account(id=IDENT, email="old@example.test", access_token="old")
        existing.in_flight = 2
        existing.model_caps["GLM-5.3"] = 9999999999
        pool = PoolManager(cfg.pool, [existing])
        tokens = SimpleNamespace(_lock_for=lambda _: lock, _scan_at=123)
        lock = asyncio.Lock()
        state = SimpleNamespace(cfg=cfg, pool=pool, tokens=tokens)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gw=state)))
        monkeypatch.setattr("zcode_gateway.api_admin.get_state", lambda _: state)
        provider(monkeypatch, lambda _: httpx.Response(200, json={"code": 0, "data": {"plans": [], "balances": []}}))
        await import_jwt_account(request, {"token": jwt()}, "test-admin")
        assert await pool.find(IDENT) is existing
        assert existing.in_flight == 2 and "GLM-5.3" in existing.model_caps
        assert existing.to_public()["imported_jwt"] is True
        assert tokens._scan_at == 0
        await pool.aclose()

    asyncio.run(scenario())


def test_account_export_import_roundtrip_between_installations(tmp_path, monkeypatch):
    from zcode_gateway.accounts_dir import write_snapshot
    from unittest.mock import AsyncMock
    monkeypatch.setattr("zcode_gateway.tokens.TokenManager.check_account", AsyncMock())
    source = config(tmp_path / "sender")
    write_snapshot(Path(source.accounts.dir) / "original.txt", {
        "account_id": IDENT, "email": "owner@example.test", "name": "Owner Label",
        "device_mid": "device-example", "access_token": jwt(), "refresh_token": "not-for-export",
        "expires_at_ms": "0",
    })
    with TestClient(create_app(source)) as sender:
        assert sender.get(f"/admin/accounts/{IDENT}/export").status_code in (401, 403)
        response = sender.get(f"/admin/accounts/{IDENT}/export", headers=HEADERS)
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
        bundle = response.json()
        assert bundle == {"format": "autozcoder-account", "version": 1, "account_id": IDENT,
                          "email": "owner@example.test", "name": "Owner Label",
                          "device_mid": "device-example", "access_token": jwt(), "expires_at_ms": 0}
        assert "refresh_token" not in bundle
        assert source.server.admin_key not in response.text

    receiver = config(tmp_path / "receiver")
    with TestClient(create_app(receiver)) as friend:
        def handler(request):
            assert request.headers.get("authorization") == "Bearer " + jwt()
            return httpx.Response(200, json={"code": 0, "data": {"plans": [], "balances": []}})
        provider(monkeypatch, handler)
        result = friend.post("/admin/accounts/import-account", headers=HEADERS,
                             json={"token": json.dumps(bundle)})
        assert result.status_code == 200, result.text
        account = result.json()["account"]
        assert account["email"] == "owner@example.test" and account["imported_jwt"]
        assert jwt() not in result.text
        loaded = load_from_accounts_dir(receiver.accounts.dir)[0]
        assert loaded.notes["device_mid"] == "device-example"
        assert loaded.notes["name"] == "Owner Label"
        assert loaded.notes["imported_jwt"]
        assert loaded.access_token == jwt()


@pytest.mark.parametrize("changes", [
    {"version": 2}, {"version": True}, {"format": "other"},
    {"account_id": "00000000-0000-0000-0000-000000000000"},
    {"device_mid": "bad\naccess_token:injected"}, {"name": ["bad"]},
    {"access_token": None},
])
def test_invalid_transfer_never_reaches_provider(tmp_path, monkeypatch, changes):
    bundle = {"format": "autozcoder-account", "version": 1, "account_id": IDENT,
              "access_token": jwt(), **changes}
    with TestClient(create_app(config(tmp_path))) as client:
        provider(monkeypatch, lambda _: pytest.fail("Invalid export reached provider"))
        result = client.post("/admin/accounts/import-account", headers=HEADERS,
                             json={"token": json.dumps(bundle)})
        assert result.status_code == 400
        assert not list(tmp_path.rglob("*.txt"))
