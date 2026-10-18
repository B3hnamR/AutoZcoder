"""Model-cap parks (UNUSUAL_ACTIVITY / GEO_BLOCKED) survive restarts."""
import asyncio
import time

import pytest

from zcode_gateway.config import PoolConfig
from zcode_gateway.pool import Account, PoolManager


def _mgr(tmp_path, accounts):
    m = PoolManager(PoolConfig(), accounts)
    m.set_caps_path(tmp_path / "model-caps.json")
    return m


@pytest.mark.asyncio
async def test_cap_persists_across_restart(tmp_path):
    acc = Account(id="acc-1", email="a@x.io", access_token="t")
    m1 = _mgr(tmp_path, [acc])
    await m1.cap_model(acc, "GLM-5.3", 900,
                       reason="UNUSUAL_ACTIVITY", message="blocked")
    assert (tmp_path / "model-caps.json").exists()

    # "restart": fresh manager, same account ids, same file
    acc2 = Account(id="acc-1", email="a@x.io", access_token="t")
    m2 = _mgr(tmp_path, [acc2])
    m2.load_caps()
    assert acc2.is_model_capped("GLM-5.3")
    info = acc2.model_cap_info["GLM-5.3"]
    assert info["code"] == "UNUSUAL_ACTIVITY"


@pytest.mark.asyncio
async def test_expired_cap_not_restored(tmp_path):
    acc = Account(id="acc-1", email="a@x.io", access_token="t")
    m1 = _mgr(tmp_path, [acc])
    await m1.cap_model(acc, "GLM-5.3", 1.0, reason="GEO_BLOCKED", message="451")
    # force-expire it on disk
    import json
    p = tmp_path / "model-caps.json"
    data = json.loads(p.read_text())
    data["acc-1"]["GLM-5.3"]["release_at_epoch"] = time.time() - 10
    p.write_text(json.dumps(data))

    acc2 = Account(id="acc-1", email="a@x.io", access_token="t")
    m2 = _mgr(tmp_path, [acc2])
    m2.load_caps()
    assert not acc2.is_model_capped("GLM-5.3")


@pytest.mark.asyncio
async def test_other_model_not_blocked(tmp_path):
    """A parked model must not block the account's other models."""
    acc = Account(id="acc-1", email="a@x.io", access_token="t")
    m1 = _mgr(tmp_path, [acc])
    await m1.cap_model(acc, "GLM-5.3", 900, reason="UNUSUAL_ACTIVITY")

    acc2 = Account(id="acc-1", email="a@x.io", access_token="t")
    m2 = _mgr(tmp_path, [acc2])
    m2.load_caps()
    assert acc2.is_model_capped("GLM-5.3")
    assert not acc2.is_model_capped("GLM-5.3-Flash")


@pytest.mark.asyncio
async def test_removed_account_pruned(tmp_path):
    acc = Account(id="gone", email="a@x.io", access_token="t")
    m1 = _mgr(tmp_path, [acc])
    await m1.cap_model(acc, "GLM-5.3", 900, reason="UNUSUAL_ACTIVITY")

    acc2 = Account(id="other", email="b@x.io", access_token="t")
    m2 = _mgr(tmp_path, [acc2])
    m2.load_caps()
    import json
    data = json.loads((tmp_path / "model-caps.json").read_text())
    assert "gone" not in data
