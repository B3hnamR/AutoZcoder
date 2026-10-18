"""Proxy tooling: parsing, scoring, prefilter, second chance, claim, geo."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

GEN = Path(__file__).resolve().parent.parent / "generator"
if str(GEN) not in sys.path:
    sys.path.insert(0, str(GEN))

import proxy_parse as pp          # noqa: E402
import proxy_geo as geo           # noqa: E402
import proxy_pool                 # noqa: E402


@pytest.fixture()
def pool_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        proxy_pool.set_output_dir(d)
        proxy_pool.POOL_FILE = os.path.join(d, "pool.json")
        yield Path(d)
        proxy_pool.set_output_dir(GEN / "output")


# --------------------------------------------------------------------------- parser

def test_parse_text_forms():
    text = "\n".join([
        "1.2.3.4:8080",
        "socks5://5.6.7.8:1080",
        "user:pass@9.9.9.9:3128",
        "socks4://u:p@9.9.9.8:1080",
        "1.1.1.1:80:US:elite",            # facts, not a login
        "http://7.7.7.7:80:user:pw",      # provider login
    ])
    got = {(e["proto"], e["host"], e["port"], e["auth"])
           for e in pp.parse_proxy_list(text, "all")}
    assert ("http", "1.2.3.4", 8080, "") in got
    assert ("socks5", "5.6.7.8", 1080, "") in got
    assert ("http", "9.9.9.9", 3128, "user:pass") in got
    assert ("socks4", "9.9.9.8", 1080, "u:p") in got
    assert ("http", "1.1.1.1", 80, "") in got
    assert ("http", "7.7.7.7", 80, "user:pw") in got
    assert not any(e[3] == "US:elite" for e in got)


def test_parse_json_and_html():
    doc = json.dumps({"data": [{"ip": "2.2.2.2", "port": "81", "protocols": ["socks5"],
                                "username": "u", "password": "p"}]})
    assert [(e["proto"], e["host"], e["port"], e["auth"])
            for e in pp.parse_proxy_list(doc)] == [("socks5", "2.2.2.2", 81, "u:p")]
    html = "<table><tr><td>3.3.3.3</td><td>8080</td></tr></table>"
    assert pp.parse_proxy_list(html) == [
        {"proto": "http", "host": "3.3.3.3", "port": 8080, "auth": ""}]


def test_parse_cidr_expands():
    got = pp.parse_proxy_list("10.20.30.0/30:8080", "socks5")
    assert [e["host"] for e in got] == ["10.20.30.1", "10.20.30.2"]
    assert all(e["proto"] == "socks5" for e in got)


def test_url_roundtrip_and_redaction():
    url = pp.to_url("socks5", "8.8.8.8", 1080, "u:p@w")
    assert url.startswith("socks5://u:p%40w@8.8.8.8:1080")
    assert pp.redact(url) == "socks5://8.8.8.8:1080"
    s = pp.split_url("http://9.9.9.9:80")
    assert s == {"proto": "http", "host": "9.9.9.9", "port": 80, "auth": ""}


def test_redact_strips_password_containing_at():
    """Regression: the login may contain '@'; nothing of it may survive."""
    assert pp.redact("http://u:p@ss@1.2.3.4:8080") == "http://1.2.3.4:8080"
    # a non-proxy URL with an @ in its path must not be mangled
    assert pp.redact("http://host/path?mail=a@b") == "http://host/path?mail=a@b"


def test_two_letter_usernames_are_credentials():
    """Regression: ip:port:jo:hunter2 must keep its login; US:elite is facts."""
    login = pp.parse_proxy_list("1.2.3.4:8080:jo:hunter2")
    assert login == [{"proto": "http", "host": "1.2.3.4", "port": 8080,
                      "auth": "jo:hunter2"}]
    facts = pp.parse_proxy_list("1.2.3.4:8080:US:elite")
    assert facts == [{"proto": "http", "host": "1.2.3.4", "port": 8080, "auth": ""}]


def test_endpoint_rejects_metadata_and_multicast():
    assert not pp.valid_endpoint("169.254.169.254", 80)
    assert not pp.valid_endpoint("224.0.0.1", 80)
    assert not pp.valid_endpoint("255.255.255.255", 80)
    assert pp.valid_endpoint("10.0.0.1", 80)      # LAN proxies stay allowed


def test_loopback_rejected_by_default(monkeypatch):
    monkeypatch.delenv("ZAI_ALLOW_LOOPBACK", raising=False)
    monkeypatch.delenv("PROXYHIVE_ALLOW_LOOPBACK", raising=False)
    assert pp.parse_proxy_list("127.0.0.1:8080") == []
    monkeypatch.setenv("ZAI_ALLOW_LOOPBACK", "1")
    assert pp.parse_proxy_list("127.0.0.1:8080")


# --------------------------------------------------------------------------- pool scoring

def test_pool_scores_and_exports(pool_dir):
    n = proxy_pool.import_proxies("socks5://1.1.1.1:1080\nhttp://2.2.2.2:8080\n",
                                  into_good=True)
    assert n == 2
    # imported = pending, not usable until a real check passes
    assert proxy_pool.load_good() == []
    proxy_pool.record_successes([("socks5://1.1.1.1:1080", 0.6, ""),
                                 ("http://2.2.2.2:8080", 0.9, "")])
    assert set(proxy_pool.load_good()) == {"socks5://1.1.1.1:1080", "http://2.2.2.2:8080"}

    proxy_pool.add_failure("socks5://1.1.1.1:1080", "timeout")
    pool = proxy_pool.load_pool()
    assert pool["socks5://1.1.1.1:1080"]["fails"] == 1
    assert "socks5://1.1.1.1:1080" not in proxy_pool.load_good()   # latest check failed
    proxy_pool.add_success("socks5://1.1.1.1:1080", 0.4)
    assert proxy_pool.load_pool()["socks5://1.1.1.1:1080"]["fails"] == 0
    assert proxy_pool.load_pool()["socks5://1.1.1.1:1080"]["latency"] == 0.4
    assert "socks5://1.1.1.1:1080" in proxy_pool.load_good()

    for _ in range(4):
        proxy_pool.add_failure("http://2.2.2.2:8080", "reset")
    assert "http://2.2.2.2:8080" not in proxy_pool.load_good()   # over max_fails
    assert json.loads((pool_dir / "pool.json").read_text())["http://2.2.2.2:8080"]["fails"] == 4


def test_export_merge_never_truncates(pool_dir):
    proxy_pool.record_successes([("socks5://1.1.1.1:1080", 1.0, "")])
    proxy_pool.record_successes([("socks5://2.2.2.2:1080", 0.5, "")])
    good = proxy_pool.load_good()
    assert set(good) == {"socks5://1.1.1.1:1080", "socks5://2.2.2.2:1080"}
    assert good[0] == "socks5://2.2.2.2:1080"                    # fastest first


def test_seed_from_legacy_text_files(pool_dir):
    (pool_dir / "proxies.txt").write_text("socks5://1.2.3.4:1080\n", encoding="utf-8")
    (pool_dir / "proxies_evergood.txt").write_text("http://5.6.7.8:8080\n", encoding="utf-8")
    assert set(proxy_pool.load_good()) == {"socks5://1.2.3.4:1080", "http://5.6.7.8:8080"}
    assert (pool_dir / "pool.json").exists()


def test_claim_and_used_ledger(pool_dir):
    proxy_pool.add_success("socks5://1.1.1.1:1080", 0.2)
    assert proxy_pool.claim_used("socks5://1.1.1.1:1080") is True
    assert proxy_pool.claim_used("socks5://1.1.1.1:1080") is False
    assert proxy_pool.load_good() == []


def test_clear_bad_and_used(pool_dir):
    proxy_pool.add_success("http://1.1.1.1:80", 0.3)
    proxy_pool.mark_bad("http://1.1.1.1:80", "tls-mitm")
    assert proxy_pool.load_good() == []
    assert proxy_pool.clear_bad() == 1
    assert proxy_pool.load_good() == ["http://1.1.1.1:80"]
    proxy_pool.mark_used("http://1.1.1.1:80", "test")
    assert proxy_pool.load_good() == []
    assert proxy_pool.clear_used() == 1
    assert proxy_pool.load_good() == ["http://1.1.1.1:80"]


def test_prune_drops_stale_unhealthy(pool_dir):
    proxy_pool.add_success("http://1.1.1.1:80", 0.3)
    pool = proxy_pool.load_pool()
    pool["http://1.1.1.1:80"].update(fails=9, last_ok=int(time.time()) - 40 * 86400)
    (pool_dir / "pool.json").write_text(json.dumps(pool), encoding="utf-8")
    res = proxy_pool.prune(days=14)
    assert res["removed"] == 1


def test_success_clears_dead_mark(pool_dir, monkeypatch):
    """A flaky proxy that passes again must not stay banished for the window."""
    monkeypatch.setenv("PROXY_DEAD_HOURS", "24")
    url = "socks5://5.5.5.5:1080"
    proxy_pool.add_success(url, 0.5)
    proxy_pool._dead_mark_many("px", [url, "socks5://6.6.6.6:1080"])
    assert url in proxy_pool.dead_set(24)[1]
    proxy_pool.record_successes([(url, 0.4, "")])
    assert url not in proxy_pool.dead_set(24)[1]


def test_clear_bad_repairs_expired_rows(pool_dir):
    """Regression: an expired blacklist row must still have its score reset."""
    url = "socks5://6.6.6.6:1080"
    proxy_pool.add_success(url, 0.5)
    pool = proxy_pool.load_pool()
    pool[url]["fails"] = 5
    (pool_dir / "pool.json").write_text(json.dumps(pool), encoding="utf-8")
    (pool_dir / "proxies_bad.txt").write_text(
        f"{url}\t{int(time.time()) - 40 * 86400}\ttls-mitm\n", encoding="utf-8")
    assert proxy_pool.load_good() == []
    assert proxy_pool.clear_bad() == 1
    assert proxy_pool.load_good() == [url]
    assert proxy_pool.load_pool()[url]["fails"] == 0


def test_import_is_vouched_not_verified(pool_dir):
    """into_good means pending, not 'passed verification'."""
    n = proxy_pool.import_proxies("7.7.7.7:8080", into_good=True)
    assert n == 1
    entry = proxy_pool.load_pool()["http://7.7.7.7:8080"]
    assert entry["fails"] == 0
    assert entry["last_ok"] == 0
    assert entry.get("vouched") is True
    assert entry["url"] not in (pool_dir / "proxies_evergood.txt").read_text()
    assert "http://7.7.7.7:8080" not in proxy_pool.load_good()   # pending, not usable
    # a real pass promotes it to verified and makes it usable
    proxy_pool.record_successes([(entry["url"], 0.8, "")])
    entry = proxy_pool.load_pool()["http://7.7.7.7:8080"]
    assert entry["last_ok"] > 0 and "vouched" not in entry
    assert "http://7.7.7.7:8080" in proxy_pool.load_good()


def test_lock_heartbeat_prevents_stealing(pool_dir):
    """A live holder past the stale age must not lose its lock."""
    path = str(pool_dir / ".t.lock")
    outer = proxy_pool._FileLock(path, stale=0.6)
    with outer:
        time.sleep(1.1)                      # > stale, heartbeat should touch it
        inner = proxy_pool._FileLock(path, timeout=0.1, stale=0.6)
        with inner:
            assert inner.held is False       # refused (not stolen) and timed out
        assert os.path.exists(path)
    assert not os.path.exists(path)


def test_force_direct_does_not_strip_env(monkeypatch):
    """Regression: claim_invite must restore direct mode and keep ZAI_PROXY."""
    import invite_claim as ic
    proxy = "socks5://1.1.1.1:1080"
    monkeypatch.setenv("ZAI_PROXY", proxy)

    @ic._direct_proxy_while
    def probe(force_direct=False):
        return ic._proxies()

    assert probe(force_direct=False) == {"http": proxy, "https": proxy}
    assert probe(force_direct=True) is None
    assert os.environ["ZAI_PROXY"] == proxy
    assert ic._FORCE_DIRECT is False

    @ic._direct_proxy_while
    def boom(force_direct=False):
        raise RuntimeError("launch failed")

    with pytest.raises(RuntimeError):
        boom(force_direct=True)
    assert os.environ["ZAI_PROXY"] == proxy
    assert ic._FORCE_DIRECT is False


def test_device_login_can_run_direct():
    """The 100M OAuth pre-step must be able to skip the signup proxy."""
    import inspect

    import invite_claim as ic
    assert "force_direct" in inspect.signature(ic.device_login).parameters
    assert hasattr(ic.device_login, "__wrapped__")   # decorated with the guard


# --------------------------------------------------------------------------- classification

def test_classify_error_local_limit_and_tls():
    e = OSError(10055, "no buffer space")
    e.errno = 10055
    assert proxy_pool.classify_error(e) == "local_limit"
    assert proxy_pool.classify_error(TimeoutError("slow")) == "timeout"
    import ssl as _ssl
    assert proxy_pool.classify_error(_ssl.SSLError("certificate verify failed")) == "tls"
    assert proxy_pool.classify_error(__import__("requests").exceptions.ProxyError("407 auth")) == "proxy_auth"


# --------------------------------------------------------------------------- prefilter

def test_probe_endpoint_open_and_closed(monkeypatch):
    monkeypatch.setenv("ZAI_ALLOW_LOOPBACK", "1")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        ok, why = proxy_pool.probe_endpoint("127.0.0.1", port, 1.0)
        assert ok and why == ""
    finally:
        srv.close()
    ok, why = proxy_pool.probe_endpoint("127.0.0.1", 1, 0.3)
    assert not ok and why in ("tcp_refused", "tcp_timeout", "tcp_unreachable", "local_limit")


# --------------------------------------------------------------------------- verify pipeline

def test_verify_second_chance_and_reasons(pool_dir, monkeypatch):
    calls = {}

    def fake_probe(url, timeout=10):
        calls[url] = calls.get(url, 0) + 1
        if url == "http://1.1.1.1:80":
            return (None, "timeout") if calls[url] == 1 else (0.5, "ok")
        if url == "http://2.2.2.2:80":
            return (0.3, "ok")
        return (None, "tls")

    monkeypatch.setattr(proxy_pool, "probe_detailed", fake_probe)
    opts = proxy_pool.resolve_scan_options(preset="fast", prefilter=False,
                                           retry_failed=True, timeout=1, concurrency=4)
    proxies = ["http://1.1.1.1:80", "http://2.2.2.2:80", "http://3.3.3.3:80"]
    alive, reasons, failures = proxy_pool._verify_list(proxies, opts, "verifying", quiet=True)
    assert {u for _, u in alive} == {"http://1.1.1.1:80", "http://2.2.2.2:80"}
    assert calls["http://1.1.1.1:80"] == 2              # retried once
    assert calls["http://3.3.3.3:80"] == 1              # TLS is not retried
    assert reasons["tls"] == 1
    assert set(proxy_pool.load_good()) == {"http://1.1.1.1:80", "http://2.2.2.2:80"}


def test_pick_verified_claims_and_filters(pool_dir, monkeypatch):
    proxy_pool.add_success("socks5://1.1.1.1:1080", 0.2)
    proxy_pool.add_success("socks5://2.2.2.2:1080", 0.9)
    monkeypatch.setattr(proxy_pool, "probe_detailed",
                        lambda url, timeout=10: (0.4, "ok"))
    picked = proxy_pool.pick_verified(tries=5, timeout=1, types=("socks5",))
    assert picked == "socks5://1.1.1.1:1080"            # healthiest + fastest first
    assert picked in proxy_pool.load_used()
    second = proxy_pool.pick_verified(tries=5, timeout=1, types=("socks5",))
    assert second == "socks5://2.2.2.2:1080"


# --------------------------------------------------------------------------- geo

def test_geo_index_lookup():
    rows = [(167772160, 167772415, "US|United States"),
            (167772416, 167772671, "DE|Germany")]
    idx = geo._Index.from_rows(rows)
    assert idx.lookup(geo.ip_to_int("10.0.0.5")) == "US|United States"
    assert idx.lookup(geo.ip_to_int("10.0.0.200")) == "US|United States"
    assert idx.lookup(geo.ip_to_int("10.0.1.5")) == "DE|Germany"
    assert idx.lookup(geo.ip_to_int("10.0.9.5")) == ""
    assert geo.ip_to_int("not-an-ip") is None


def test_country_csv_format(tmp_path):
    """DB-IP Lite ships dotted ranges and (currently) three columns."""
    import gzip
    path = tmp_path / "country.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("1.0.0.0,1.0.0.255,AU\n1.0.1.0,1.0.3.255,CN\n")
    idx = geo._load_country(str(path))
    assert idx.lookup(geo.ip_to_int("1.0.0.5")) == "AU|"
    assert idx.lookup(geo.ip_to_int("1.0.2.5")) == "CN|"
    assert idx.lookup(geo.ip_to_int("9.9.9.9")) == ""


def test_asn_csv_format(tmp_path):
    import gzip
    path = tmp_path / "asn.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write('1.0.0.0,1.0.0.255,13335,"Cloudflare, Inc."\n')
    idx = geo._load_asn(str(path))
    assert idx.lookup(geo.ip_to_int("1.0.0.1")) == "13335 Cloudflare, Inc."
