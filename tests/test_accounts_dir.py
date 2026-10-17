"""accounts/ folder loader tests (the snapshots written by capture_account.py)."""

from __future__ import annotations

from pathlib import Path

from zcode_gateway.accounts_dir import (
    load_from_accounts_dir,
    parse_snapshot,
    write_snapshot,
)

SNAPSHOT = """\
# Zcode account snapshot
captured_at:      2026-09-16T15:58:52+00:00
source:           C:\\Users\\x\\.zcode\\data\\settings\\providers.json
provider_slot:    zcode
app_config_active: false

# identity
account_id:       usr-TEST0000000000000000000B
zcode_user_id:    usr-TEST0000000000000000000B
workos_user_id:   user_TEST000000000000000000B
session_id:       session_TEST000000000000000001
email:            someone@example.com
name:             Someone Example

# token
token_prefix:     workos:
expires_at:       2026-09-16T16:37:36+00:00
expires_at_ms:    1789576656000
token_lifetime_s: 3600
issuer:           https://api.workos.com/user_management/client_x
client_id:        client_x

# billing
balance_micro:    499670
balance_usd:      0.499670
plan:             no plan history found for user

# credentials
access_token:     workos:TEST_ACCESS_TOKEN
refresh_token:    TEST_REFRESH_TOKEN

# how to use
#   header        Authorization: Bearer <access_token>
"""


def test_parse_snapshot_reads_all_fields():
    p = Path("/tmp/x.txt")
    fields = parse_snapshot.__wrapped__(p) if hasattr(parse_snapshot, "__wrapped__") else None
    # write a real file instead of relying on internals
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "a.txt"
        f.write_text(SNAPSHOT, encoding="utf-8")
        fields = parse_snapshot(f)
    assert fields["account_id"] == "usr-TEST0000000000000000000B"
    assert fields["email"] == "someone@example.com"
    assert fields["access_token"].startswith("workos:")
    assert fields["refresh_token"] == "TEST_REFRESH_TOKEN"
    assert fields["expires_at_ms"] == "1789576656000"
    assert fields["balance_usd"] == "0.499670"


def test_parse_skips_comments_and_blanks():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "a.txt"
        f.write_text(SNAPSHOT, encoding="utf-8")
        fields = parse_snapshot(f)
    assert "# identity" not in fields
    assert "how to use" not in fields
    assert len(fields) > 8


def test_load_from_dir(tmp_path):
    (tmp_path / "one.txt").write_text(SNAPSHOT, encoding="utf-8")
    accounts = load_from_accounts_dir(tmp_path)
    assert len(accounts) == 1
    a = accounts[0]
    assert a.id == "usr-TEST0000000000000000000B"
    assert a.email == "someone@example.com"
    assert a.expires_at == 1789576656000
    assert a.refresh_token == "TEST_REFRESH_TOKEN"
    assert a.source == "accounts/one.txt"
    # the extra context is preserved for the GUI
    assert a.notes["balance_usd"] == "0.499670"
    assert a.notes["file"] == "one.txt"


def test_load_skips_files_without_a_token(tmp_path):
    (tmp_path / "good.txt").write_text(SNAPSHOT, encoding="utf-8")
    (tmp_path / "bad.txt").write_text("account_id: usr-x\nemail: y\n", encoding="utf-8")
    accounts = load_from_accounts_dir(tmp_path)
    assert len(accounts) == 1


def test_load_dedupes_by_account_id_keeping_newest(tmp_path):
    older = SNAPSHOT.replace("1789576656000", "1789000000000")
    (tmp_path / "older.txt").write_text(older, encoding="utf-8")
    (tmp_path / "newer.txt").write_text(SNAPSHOT, encoding="utf-8")
    accounts = load_from_accounts_dir(tmp_path)
    assert len(accounts) == 1
    assert accounts[0].expires_at == 1789576656000


def test_load_sorts_by_expiry_desc(tmp_path):
    a = SNAPSHOT.replace("1789576656000", "1789570000000")
    b = SNAPSHOT.replace("1789576656000", "1789600000000").replace(
        "usr-TEST0000000000000000000B", "usr-OTHER")
    (tmp_path / "a.txt").write_text(a, encoding="utf-8")
    (tmp_path / "b.txt").write_text(b, encoding="utf-8")
    accounts = load_from_accounts_dir(tmp_path)
    assert [x.expires_at for x in accounts] == [1789600000000, 1789570000000]


def test_missing_dir_returns_empty(tmp_path):
    assert load_from_accounts_dir(tmp_path / "nope") == []


def test_write_snapshot_round_trips(tmp_path):
    fields = parse_snapshot_file(tmp_path)
    out = tmp_path / "written.txt"
    write_snapshot(out, fields)
    again = parse_snapshot(out)
    for key in ("account_id", "email", "access_token", "refresh_token",
                "expires_at_ms"):
        assert again[key] == fields[key], key


def parse_snapshot_file(tmp_path) -> dict:
    f = tmp_path / "src.txt"
    f.write_text(SNAPSHOT, encoding="utf-8")
    return parse_snapshot(f)
