"""Local-only registration races and snapshot preservation checks."""

import base64
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from zcode_gateway import accounts_dir
from zcode_gateway import accounts_dir_bridge as bridge


def _token(account_id, exp=1792000000):
    payload = base64.urlsafe_b64encode(json.dumps({
        "user_id": account_id, "exp": exp,
    }).encode()).decode().rstrip("=")
    return f"test.{payload}.not-a-real-signature"


def _register_together(directory, identities, emails):
    barrier = threading.Barrier(3)

    def register(index):
        barrier.wait(timeout=5)
        # Both path spellings must share the same directory lock.
        folder = directory if index == 0 else directory / "unused" / ".."
        return bridge.mint_and_register(folder, emails[index], _token(identities[index]))

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(register, index) for index in range(3)]
        return [future.result(timeout=10) for future in futures]


def test_three_ids_with_same_sanitized_name(tmp_path):
    identities = ["account-one", "account-two", "account-three"]
    registered = _register_together(
        tmp_path, identities, ["same?name", "same:name", "same/name"],
    )
    assert len({account.notes["file"].casefold() for account in registered}) == 3
    assert len(list(tmp_path.glob("*.txt"))) == 3
    for account in registered:
        path = accounts_dir.snapshot_path(tmp_path, account.notes["file"])
        fields = accounts_dir.parse_snapshot(path)
        assert fields["account_id"] == account.id
        assert fields["access_token"] == _token(account.id)
        assert fields["source"] == "ZCODEAUTO generator (device flow)"
        assert account.source == "generator"


def test_same_id_is_idempotent_even_when_email_changes(tmp_path):
    registered = _register_together(
        tmp_path, ["one-account"] * 3, ["first@example.test", "second@example.test", "third@example.test"],
    )
    assert len({account.notes["file"] for account in registered}) == 1
    again = bridge.mint_and_register(tmp_path, "renamed@example.test", _token("one-account", 1792000100))
    assert again.notes["file"] == registered[0].notes["file"]
    assert len(list(tmp_path.glob("*.txt"))) == 1
    fields = accounts_dir.parse_snapshot(tmp_path / again.notes["file"])
    assert fields["email"] == "renamed@example.test"
    assert fields["access_token"] == again.access_token
    assert fields["expires_at_ms"] == "1792000100000"


def test_existing_snapshot_casing_and_metadata_are_preserved(tmp_path):
    path = tmp_path / "Existing.TXT"
    original = {
        "account_id": "existing", "email": "old@example.test", "name": "Display Name",
        "source": "Manual JWT import", "imported_jwt": "true", "device_mid": "saved-device",
        "plan": "saved-plan", "refresh_token": "synthetic-refresh", "provider_slot": "slot-2",
        "app_config_active": "true", "workos_user_id": "workos-test", "balance_usd": "12.34",
        "access_token": _token("existing"),
    }
    accounts_dir.write_snapshot(path, original)
    account = bridge.mint_and_register(tmp_path, "new@example.test", _token("existing", 1792000200))
    assert account.notes["file"] == "Existing.TXT"
    assert account.notes["name"] == "Display Name"
    assert account.notes["plan"] == "saved-plan"
    assert account.notes["device_mid"] == "saved-device"
    assert account.notes["imported_jwt"] is True
    assert account.refresh_token == "synthetic-refresh"
    assert list(tmp_path.iterdir()) == [path]
    fields = accounts_dir.parse_snapshot(path)
    for key in ("name", "imported_jwt", "device_mid", "plan", "refresh_token", "provider_slot",
                "app_config_active", "workos_user_id", "balance_usd"):
        assert fields[key] == original[key]
    updated = bridge.mint_and_register(
        tmp_path, "new@example.test", _token("existing"), device_mid="new-device", plan="new-plan",
    )
    assert updated.notes["device_mid"] == "new-device"
    assert updated.notes["plan"] == "new-plan"


def test_case_insensitive_filename_collision(tmp_path):
    existing = tmp_path / "SHARED@EXAMPLE.TEST.TXT"
    accounts_dir.write_snapshot(existing, {"account_id": "first", "access_token": _token("first")})
    before = existing.read_bytes()
    account = bridge.mint_and_register(tmp_path, "shared@example.test", _token("second"))
    assert account.notes["file"].casefold() != existing.name.casefold()
    assert existing.read_bytes() == before
    assert accounts_dir.parse_snapshot(tmp_path / account.notes["file"])["account_id"] == "second"


def test_distinct_directories_do_not_serialize_writes(tmp_path, monkeypatch):
    barrier = threading.Barrier(3)
    write_snapshot = bridge.write_snapshot

    def write_together(path, fields):
        barrier.wait(timeout=5)
        write_snapshot(path, fields)

    monkeypatch.setattr(bridge, "write_snapshot", write_together)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(
            bridge.mint_and_register, tmp_path / str(index), "local@example.test", _token(str(index)),
        ) for index in range(3)]
        registered = [future.result(timeout=10) for future in futures]
    assert {account.id for account in registered} == {"0", "1", "2"}


@pytest.mark.parametrize("email", ["CON", "nul.txt", "COM1", "../unsafe:name?", "." * 200, "x" * 400])
def test_windows_safe_names(tmp_path, email):
    account = bridge.mint_and_register(tmp_path, email, _token("windows-safe"))
    name = account.notes["file"]
    assert name == accounts_dir.snapshot_filename(email, "windows-safe", {})
    assert accounts_dir.snapshot_path(tmp_path, name).is_file()
    assert not any(character in name for character in '<>:"/\\|?*')
    assert len(name) <= 180


@pytest.mark.parametrize("field", ["account_id", "email", "device_mid", "plan", "zcode_jwt"])
@pytest.mark.parametrize("newline", ["\r", "\n", "\r\n"])
def test_snapshot_newline_injection_rejected(tmp_path, field, newline):
    kwargs = {"email": "local@example.test", "zcode_jwt": _token("safe"), "device_mid": "local-device"}
    injected = f"bad{newline}access_token: injected"
    if field == "account_id":
        kwargs["zcode_jwt"] = _token(injected)
    else:
        kwargs[field] = injected
    with pytest.raises(ValueError, match="single-line"):
        bridge.mint_and_register(tmp_path, **kwargs)
    assert not list(tmp_path.iterdir())


def test_failed_replace_preserves_snapshot_and_cleans_tempfile(tmp_path, monkeypatch):
    account = bridge.mint_and_register(tmp_path, "local@example.test", _token("stable"))
    path = tmp_path / account.notes["file"]
    before = path.read_bytes()
    replace = os.replace
    temporary_paths = []

    def fail_replace(source, destination):
        temporary_paths.append(source)
        assert source.parent == path.parent
        assert source.name.startswith(".snapshot-")
        if os.name != "nt":
            assert stat.S_IMODE(source.stat().st_mode) == 0o600
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(accounts_dir.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        bridge.mint_and_register(tmp_path, "changed@example.test", _token("stable", 1792000300))
    assert path.read_bytes() == before
    assert temporary_paths and all(not temporary.exists() for temporary in temporary_paths)
    assert list(tmp_path.iterdir()) == [path]
    monkeypatch.setattr(accounts_dir.os, "replace", replace)
    retried = bridge.mint_and_register(tmp_path, "changed@example.test", _token("stable", 1792000300))
    assert retried.notes["file"] == path.name
    assert accounts_dir.parse_snapshot(path)["access_token"] == retried.access_token


def test_compatibility_wrappers_use_shared_helpers(tmp_path):
    name = bridge._snapshot_name("CON", "wrapper-account", {"ACCOUNT_CON.TXT"})
    assert name.casefold() != "account_con.txt"
    path = tmp_path / name
    bridge._write_snapshot(path, {"account_id": "wrapper-account", "access_token": "synthetic"})
    assert accounts_dir.parse_snapshot(path)["account_id"] == "wrapper-account"
