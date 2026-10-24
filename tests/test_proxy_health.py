"""The proxy tiles and the "Proxy pool" card must agree, and must read the
fields pool.json really has (the card used to read keys that do not exist and
reported every proxy as unverified)."""
import json
import time

from fastapi.testclient import TestClient

from zcode_gateway.app import create_app
from zcode_gateway.config import Config
from zcode_gateway.generator import GeneratorManager, classify_proxy_entry

ADMIN = {"Authorization": "Bearer adm-test"}
NOW = int(time.time())


def entry(url, **kw):
    base = {"url": url, "proto": url.split("://")[0], "latency": 0.0, "fails": 0, "checks": 0,
            "last_ok": 0, "last_check": 0, "country": ""}
    base.update(kw)
    return base


POOL = {
    # working: latest check passed
    "socks5://1.1.1.1:1080": entry("socks5://1.1.1.1:1080", latency=0.8, checks=3, last_ok=NOW - 60,
                                   last_check=NOW - 60, last_result="ok", country="DE"),
    "http://2.2.2.2:80": entry("http://2.2.2.2:80", latency=0.3, checks=2, last_ok=NOW - 30,
                               last_check=NOW - 30, last_result="ok", country="US"),
    # failing: passed once, latest check failed
    "http://3.3.3.3:80": entry("http://3.3.3.3:80", fails=1, checks=6, last_ok=NOW - 5000,
                               last_check=NOW - 10, last_result="fail", last_reason="timeout"),
    # failing: checked, never passed
    "socks5://4.4.4.4:1080": entry("socks5://4.4.4.4:1080", fails=2, checks=2, last_check=NOW - 20,
                                   last_result="fail", last_reason="refused"),
    # never checked (imported / scraped): fails == 0 used to be counted as "healthy"
    "socks5://5.5.5.5:1080": entry("socks5://5.5.5.5:1080", vouched=True),
    "socks5://6.6.6.6:1080": entry("socks5://6.6.6.6:1080"),
    # used up and bad
    "socks5://7.7.7.7:1080": entry("socks5://7.7.7.7:1080", last_ok=NOW - 90, last_check=NOW - 90,
                                   last_result="ok"),
    "http://8.8.8.8:80": entry("http://8.8.8.8:80", last_ok=NOW - 90, last_check=NOW - 90, last_result="ok"),
}
USED = {"socks5://7.7.7.7:1080"}
BAD = {"http://8.8.8.8:80"}


def test_each_proxy_lands_in_exactly_one_state():
    got = {u: classify_proxy_entry(e, u, BAD, USED) for u, e in POOL.items()}
    assert got == {
        "socks5://1.1.1.1:1080": "working", "http://2.2.2.2:80": "working",
        "http://3.3.3.3:80": "failing", "socks5://4.4.4.4:1080": "failing",
        "socks5://5.5.5.5:1080": "new", "socks5://6.6.6.6:1080": "new",
        "socks5://7.7.7.7:1080": "used", "http://8.8.8.8:80": "bad",
    }


def test_too_many_failures_stop_a_passing_proxy_counting_as_working():
    e = entry("http://9.9.9.9:80", fails=9, checks=20, last_ok=NOW, last_check=NOW, last_result="ok")
    assert classify_proxy_entry(e, "http://9.9.9.9:80", set(), set()) == "failing"


def _write_pool(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "pool.json").write_text(json.dumps(POOL), encoding="utf-8")
    (data_dir / "proxies_used.txt").write_text("\n".join(USED), encoding="utf-8")
    (data_dir / "proxies_bad.txt").write_text("\n".join(BAD), encoding="utf-8")


def test_checker_tiles_add_up_to_the_pool(tmp_path):
    _write_pool(tmp_path)
    mgr = GeneratorManager(tmp_path, data_dir=tmp_path)
    mgr.checker.refresh_stats()
    s = mgr.checker.stats
    assert s["pool_json"] == 8
    assert (s["healthy"], s["failing"], s["unverified"]) == (2, 2, 2)
    assert s["usable"] == s["healthy"] == 2
    assert s["healthy"] + s["failing"] + s["unverified"] + s["used"] + s["bad"] == s["pool_json"]


def test_pool_card_endpoint_reads_the_real_fields(tmp_path):
    cfg = Config(server={"host": "127.0.0.1", "port": 8788, "admin_key": "adm-test",
                         "client_keys": [{"key": "cli-test", "name": "local"}]},
                 accounts={"source": "accounts_dir", "dir": str(tmp_path / "accounts")},
                 store={"sqlite_path": str(tmp_path / "gw.db")},
                 update={"enabled": False}, logging={"capture": False})
    _write_pool(tmp_path / "generator-data")
    with TestClient(create_app(cfg)) as c:
        body = c.get("/admin/dash/proxies/health", headers=ADMIN).json()
    k = body["kpis"]
    assert body["available"] is True and k["total"] == 8
    assert (k["working"], k["failing"], k["unverified"], k["used"], k["bad"]) == (2, 2, 2, 1, 1)
    assert k["healthy"] == k["working"] and k["socks"] == 5 and k["http"] == 3
    assert k["working"] + k["failing"] + k["unverified"] + k["used"] + k["bad"] == k["total"]

    # working first, fastest first; then failing, never checked, used, bad
    order = [p["status"] for p in body["proxies"]]
    assert order == ["working", "working", "failing", "failing", "new", "new", "used", "bad"]
    assert [p["url"] for p in body["proxies"][:2]] == ["http://2.2.2.2:80", "socks5://1.1.1.1:1080"]
    fail = next(p for p in body["proxies"] if p["url"] == "http://3.3.3.3:80")
    assert fail["reason"] == "timeout" and fail["fails"] == 1 and fail["checks"] == 6
    assert fail["checked_at"] == NOW - 10                       # real last_check, not a missing key
    assert body["countries"]["DE"] == 1 and body["countries"]["??"] == 6 and body["countries"]["US"] == 1
