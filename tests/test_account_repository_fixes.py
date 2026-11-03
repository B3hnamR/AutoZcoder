"""Offline repository transactions against real temporary credential files."""
import asyncio
import base64
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from zcode_gateway import accounts_dir, accounts_dir_bridge, device_auth, token_utils, zcode_accounts
from zcode_gateway.config import Fingerprint
from zcode_gateway.pool import Account


def jwt(ident, iat=1, exp=None):
    claims = {"sub": ident, "iat": iat}
    if exp is not None:
        claims["exp"] = exp
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{payload}.offline"


def test_bridge_device_and_import_share_directory_transaction(tmp_path, monkeypatch):
    barrier = threading.Barrier(3)
    monkeypatch.setattr(zcode_accounts, "read_credentials", lambda _: [{
        "account_id": "imported", "email": "shared@example.test", "access_token": jwt("imported"),
    }])

    def bridge():
        barrier.wait(timeout=5)
        return accounts_dir_bridge.mint_and_register(tmp_path, "shared@example.test", jwt("bridge"))

    def device():
        barrier.wait(timeout=5)
        login = device_auth.DeviceLogin(tmp_path / "unused" / "..", client=object())
        return asyncio.run(login._register({"token": jwt("device"), "user": {"email": "shared@example.test"}}))

    def imported():
        barrier.wait(timeout=5)
        return zcode_accounts.import_credentials("offline", tmp_path)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(worker) for worker in (bridge, device, imported)]
        for future in futures:
            future.result(timeout=15)
    loaded = accounts_dir.load_from_accounts_dir(tmp_path)
    assert {a.id for a in loaded} == {"bridge", "device", "imported"}
    assert len({a.notes["file"].casefold() for a in loaded}) == 3
    assert len(list(tmp_path.glob("*.txt"))) == 3


def test_same_identity_email_change_reuses_owner_preserves_metadata(tmp_path, monkeypatch):
    original = tmp_path / "Original.TXT"
    accounts_dir.write_snapshot(original, {
        "account_id": "owner", "email": "old@example.test", "access_token": jwt("owner"),
        "refresh_token": "refresh", "name": "Owner", "plan": "saved-plan", "custom_note": "retained",
        "source": "Manual JWT import", "device_mid": "saved-device", "expires_at": "old-expiry",
    })
    login = device_auth.DeviceLogin(tmp_path, client=object(), fingerprint=Fingerprint(device_mid=""))
    account = asyncio.run(login._register({"token": jwt("owner", 2), "user": {"email": "new@example.test"}}))
    assert login.snapshot_path == original
    assert account.notes["file"] == original.name
    assert account.refresh_token == "refresh"
    assert account.notes["name"] == "Owner"
    assert account.notes["plan"] == "saved-plan"
    assert account.notes["device_mid"] == "saved-device"
    monkeypatch.setattr(zcode_accounts, "read_credentials", lambda _: [{
        "account_id": "owner", "email": "third@example.test", "access_token": jwt("owner", 3),
    }])
    assert zcode_accounts.import_credentials("offline", tmp_path)["files"] == [original.name]
    fields = accounts_dir.parse_snapshot(original)
    assert fields["custom_note"] == "retained"
    assert fields["imported_jwt"] == "true"
    assert "expires_at" not in fields
    assert len(list(tmp_path.iterdir())) == 1


def test_concurrent_bridge_device_same_id_merge_without_losing_metadata(tmp_path):
    barrier = threading.Barrier(2)
    original = accounts_dir.save_account_snapshot(tmp_path, {
        "account_id": "same", "email": "original@example.test", "access_token": jwt("same"),
        "refresh_token": "original-refresh", "custom_note": "original-note",
    })

    def bridge():
        barrier.wait(timeout=5)
        return accounts_dir_bridge.mint_and_register(tmp_path, "bridge@example.test", jwt("same", 2), plan="new-plan")

    def device():
        barrier.wait(timeout=5)
        login = device_auth.DeviceLogin(tmp_path, client=object())
        return asyncio.run(login._register({"token": jwt("same", 3), "user": {
            "email": "device@example.test", "name": "Device Name",
        }}))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for worker in (bridge, device)]
        results = [future.result(timeout=10) for future in futures]
    assert {account.notes["file"] for account in results} == {original.name}
    fields = accounts_dir.parse_snapshot(original)
    assert fields["name"] == "Device Name"
    assert fields["plan"] == "new-plan"
    assert fields["refresh_token"] == "original-refresh"
    assert fields["custom_note"] == "original-note"
    assert list(tmp_path.iterdir()) == [original]


@pytest.mark.parametrize("captured", [True, False])
def test_duplicate_load_prefers_fresh_credential_not_expiry(tmp_path, captured):
    old = {"account_id": "same", "access_token": jwt("same", 1, 9999999999), "expires_at_ms": "9999999999000"}
    new = {"account_id": "same", "access_token": jwt("same", 2), "expires_at_ms": "0"}
    if captured:
        old["captured_at"] = "2026-01-01T00:00:00Z"
        new["captured_at"] = "2026-02-01T00:00:00Z"
    accounts_dir.write_snapshot(tmp_path / "old.txt", old)
    accounts_dir.write_snapshot(tmp_path / "new.txt", new)
    os.utime(tmp_path / "old.txt", (100, 100))
    os.utime(tmp_path / "new.txt", (200, 200))
    account, = accounts_dir.load_from_accounts_dir(tmp_path)
    assert account.access_token == new["access_token"]
    assert account.expires_at == 0


def test_duplicate_issuance_breaks_capture_tie_before_mtime(tmp_path):
    for name, issued, expiry, modified in [("new.txt", 2, 0, 100), ("old.txt", 1, 9999999999000, 200)]:
        path = tmp_path / name
        accounts_dir.write_snapshot(path, {
            "account_id": "same", "access_token": jwt("same", issued), "expires_at_ms": str(expiry),
            "captured_at": "2026-02-01T00:00:00Z",
        })
        os.utime(path, (modified, modified))
    account, = accounts_dir.load_from_accounts_dir(tmp_path)
    assert account.access_token == jwt("same", 2)


def test_newer_capture_remains_authoritative_over_larger_old_expiry(tmp_path):
    accounts_dir.write_snapshot(tmp_path / "old.txt", {
        "account_id": "same", "access_token": jwt("same", 1), "expires_at_ms": "9999999999000",
        "captured_at": "2026-01-01T00:00:00Z",
    })
    accounts_dir.write_snapshot(tmp_path / "new.txt", {
        "account_id": "same", "access_token": jwt("same", 2), "expires_at_ms": "100",
        "captured_at": "2026-02-01T00:00:00Z",
    })
    account, = accounts_dir.load_from_accounts_dir(tmp_path)
    assert account.expires_at == 100


def test_nested_transactions_and_direct_writer_use_same_lock(tmp_path):
    attempted, finished = threading.Event(), threading.Event()

    def blocked_writer():
        attempted.set()
        accounts_dir.write_snapshot(tmp_path / "other.txt", {"access_token": "other"})
        finished.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with accounts_dir.snapshot_transaction(tmp_path):
            accounts_dir.save_account_snapshot(tmp_path, {"account_id": "one", "access_token": "first"})
            future = executor.submit(blocked_writer)
            assert attempted.wait(timeout=5)
            assert not finished.wait(timeout=0.05)
        future.result(timeout=5)
    assert finished.is_set()


def test_pool_concurrent_real_writes_have_private_unique_temps(tmp_path, monkeypatch):
    barrier = threading.Barrier(6)
    cfg = SimpleNamespace(accounts=SimpleNamespace(pool_file=str(tmp_path / "pool.json")))
    observed = []
    replace = os.replace

    def inspect_replace(source, target):
        source = Path(source)
        assert source.parent == tmp_path
        if os.name != "nt":
            assert stat.S_IMODE(source.stat().st_mode) == 0o600
        observed.append(source.name)
        json.loads(source.read_text(encoding="utf-8"))
        replace(source, target)

    monkeypatch.setattr(token_utils.os, "replace", inspect_replace)

    def write(index):
        barrier.wait(timeout=5)
        token_utils.persist_pool_snapshot(cfg, [Account(id=str(index), access_token="offline" * 1000)])

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(write, index) for index in range(6)]
        for future in futures:
            future.result(timeout=10)
    payload = json.loads((tmp_path / "pool.json").read_text(encoding="utf-8"))
    assert len(payload["accounts"]) == 1
    assert len(observed) == len(set(observed)) == 6
    assert not list(tmp_path.glob(".pool-*.tmp"))


def test_pool_failed_replace_preserves_file_and_removes_private_temp(tmp_path, monkeypatch):
    path = tmp_path / "pool.json"
    token_utils._atomic_write(path, '{"old": true}')
    predictable = tmp_path / "pool.json.tmp"
    predictable.write_text("do not overwrite", encoding="utf-8")

    def fail(*_):
        raise OSError("offline replace failure")

    monkeypatch.setattr(token_utils.os, "replace", fail)
    with pytest.raises(OSError):
        token_utils._atomic_write(path, '{"new": true}')
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert predictable.read_text(encoding="utf-8") == "do not overwrite"
    assert not list(tmp_path.glob(".pool-*.tmp"))


def test_snapshot_symlinks_never_supply_owner_or_credentials(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "credential.txt"
    accounts_dir.write_snapshot(target, {"account_id": "outside", "access_token": "secret"})
    folder = tmp_path / "accounts"
    folder.mkdir()
    link = folder / "same@example.test.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    assert accounts_dir.parse_snapshot(link) == {}
    assert accounts_dir.load_from_accounts_dir(folder) == []
    saved = accounts_dir.save_account_snapshot(folder, {
        "account_id": "inside", "email": "same@example.test", "access_token": "inside-token",
    })
    assert saved != link
    assert accounts_dir.parse_snapshot(target)["access_token"] == "secret"


def test_device_flow_is_starting_before_task_runs(tmp_path):
    login = device_auth.DeviceLogin(tmp_path, client=object())
    assert login.state == "starting"


def test_snapshot_load_retains_durable_registration_receipt(tmp_path):
    accounts_dir.save_account_snapshot(tmp_path, {
        "account_id": "same", "access_token": jwt("same"),
        "auto_run_id": "offline-run", "auto_attempt_id": "offline-attempt",
    })
    account, = accounts_dir.load_from_accounts_dir(tmp_path)
    assert account.notes["auto_run_id"] == "offline-run"
    assert account.notes["auto_attempt_id"] == "offline-attempt"


@pytest.mark.parametrize("ident", ["", "\nowner", None, 42])
def test_save_requires_verified_single_line_owner(tmp_path, ident):
    with pytest.raises(ValueError):
        accounts_dir.save_account_snapshot(tmp_path, {"account_id": ident, "access_token": "offline"})
    assert not list(tmp_path.iterdir())
