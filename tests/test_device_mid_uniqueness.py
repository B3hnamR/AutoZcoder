"""Unique per-account device_mid: shared/absent mids get flagged UNUSUAL_ACTIVITY."""

import uuid

from zcode_gateway.pool import Account, _ensure_unique_device_mids, _per_account_device_mid


def _acct(ident, token, mid):
    return Account(id=ident, access_token=token, notes={"device_mid": mid})


def test_shared_device_mid_is_replaced_for_every_account():
    shared = "234aefe2-3795-4cf2-95c6-7bd9291feb1f"
    accounts = [_acct(str(i), f"tok{i}", shared) for i in range(5)]

    _ensure_unique_device_mids(accounts)

    mids = [a.notes["device_mid"] for a in accounts]
    assert len(set(mids)) == 5
    assert shared not in mids
    for account in accounts:
        uuid.UUID(account.notes["device_mid"])  # still a valid uuid


def test_missing_device_mid_is_filled():
    accounts = [_acct("a", "tok-a", ""), _acct("b", "tok-b", None)]

    _ensure_unique_device_mids(accounts)

    assert all(a.notes["device_mid"] for a in accounts)
    assert accounts[0].notes["device_mid"] != accounts[1].notes["device_mid"]


def test_unique_device_mid_is_preserved():
    accounts = [_acct("a", "tok-a", "kept-a"), _acct("b", "tok-b", "kept-b")]

    _ensure_unique_device_mids(accounts)

    assert accounts[0].notes["device_mid"] == "kept-a"
    assert accounts[1].notes["device_mid"] == "kept-b"


def test_derivation_is_deterministic_and_generator_compatible():
    # Must match generator/plan_check.per_account_device_mid exactly so the
    # gateway and generator agree on one stable id per account.
    token = "eyJ.example.token"
    expected = str(uuid.uuid5(uuid.NAMESPACE_URL, "zcode-device:" + token))
    assert _per_account_device_mid(token) == expected
    assert _per_account_device_mid(token) == _per_account_device_mid(token)
