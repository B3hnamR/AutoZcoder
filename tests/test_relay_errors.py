"""Relay deploy: transient network failures are retried, errors are explained in
plain language, and relays can be deleted in bulk."""
import requests
from fastapi.testclient import TestClient

from zcode_gateway import neterr, relay_deploy
from zcode_gateway.app import create_app
from zcode_gateway.config import Config
from zcode_gateway.relay_deploy import RelayDeployError
from zcode_gateway.relay_manager import RelayManager

ADMIN = {"Authorization": "Bearer adm-test"}

# the exact failure from the dashboard screenshot
VERCEL_SSL_EOF = (
    "HTTPSConnectionPool(host='api.vercel.com', port=443): Max retries exceeded with url: "
    "/v13/deployments/dpl_EXAMPLE0000000000000000000 (Caused by SSLError(SSLEOFError(8, "
    "'[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1032)')))")


class Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data or {}, text
        self.ok = 200 <= status < 300
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._data


# --------------------------------------------------------------------------- #
# plain-language messages
# --------------------------------------------------------------------------- #

def test_ssl_eof_is_explained_not_dumped():
    msg = neterr.describe_exception(requests.exceptions.SSLError(VERCEL_SSL_EOF))
    assert "api.vercel.com" in msg and "cut off" in msg and "try again" in msg
    assert "HTTPSConnectionPool" not in msg and "_ssl.c" not in msg
    assert neterr.is_transient(requests.exceptions.SSLError(VERCEL_SSL_EOF))


def test_other_network_failures_get_their_own_advice():
    dns = requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='api.netlify.com', port=443): Max retries exceeded (Caused by "
        "NameResolutionError(\"Failed to resolve 'api.netlify.com' ([Errno 11001] getaddrinfo failed)\"))")
    assert "DNS" in neterr.describe_exception(dns) and "api.netlify.com" in neterr.describe_exception(dns)
    cert = requests.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    assert "certificate" in neterr.describe_exception(cert, "api.deno.com")
    assert not neterr.is_transient(cert)                  # an untrusted certificate is not a blip
    rto = requests.exceptions.ReadTimeout("HTTPSConnectionPool(host='x.vercel.app', port=443): Read timed out.")
    assert "did not answer in time" in neterr.describe_exception(rto)


def test_http_errors_say_what_to_do():
    assert "rejected the API token" in neterr.describe_http_error("Vercel", 403, '{"error":{"message":"Not authorized"}}', "create deployments")
    assert "Not authorized" in neterr.describe_http_error("Vercel", 403, '{"error":{"message":"Not authorized"}}', "x")
    assert "rate-limiting" in neterr.describe_http_error("Netlify", 429, "")
    assert "server problem" in neterr.describe_http_error("Deno Deploy", 503, "")
    assert "already" in neterr.describe_http_error("Cloudflare", 409, "")
    assert "Cloudflare" in neterr.describe_http_error("Cloudflare", 401, '{"errors":[{"message":"Invalid token"}]}')
    assert "Invalid token" in neterr.describe_http_error("Cloudflare", 401, '{"errors":[{"message":"Invalid token"}]}')


# --------------------------------------------------------------------------- #
# retries
# --------------------------------------------------------------------------- #

def test_call_retries_a_transient_failure_then_succeeds(monkeypatch):
    seq = [requests.exceptions.SSLError(VERCEL_SSL_EOF), requests.exceptions.SSLError(VERCEL_SSL_EOF), Resp()]
    calls = []

    def get(url, **kw):
        calls.append(url)
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(relay_deploy.requests, "get", get)
    monkeypatch.setattr(relay_deploy.time, "sleep", lambda s: None)
    assert relay_deploy._call("get", "https://api.vercel.com/x").ok
    assert len(calls) == 3


def test_call_gives_up_with_a_readable_error(monkeypatch):
    monkeypatch.setattr(relay_deploy.requests, "get",
                        lambda url, **kw: (_ for _ in ()).throw(requests.exceptions.SSLError(VERCEL_SSL_EOF)))
    monkeypatch.setattr(relay_deploy.time, "sleep", lambda s: None)
    try:
        relay_deploy._call("get", "https://api.vercel.com/x", attempts=2)
        assert False, "should have raised"
    except RelayDeployError as exc:
        assert "api.vercel.com" in exc.user_message and "HTTPSConnectionPool" not in exc.user_message
        assert "SSLError" in exc.detail                    # the raw cause is kept for the log


def test_post_is_not_retried_after_a_read_timeout(monkeypatch):
    calls = []

    def post(url, **kw):
        calls.append(1)
        raise requests.exceptions.ReadTimeout("Read timed out")

    monkeypatch.setattr(relay_deploy.requests, "post", post)
    monkeypatch.setattr(relay_deploy.time, "sleep", lambda s: None)
    try:
        relay_deploy._call("post", "https://api.vercel.com/v13/deployments")
        assert False
    except RelayDeployError:
        pass
    assert len(calls) == 1          # the server may already have created the deployment


def test_gateway_hiccup_502_is_retried(monkeypatch):
    seq = [Resp(502), Resp(503), Resp(200)]
    monkeypatch.setattr(relay_deploy.requests, "get", lambda url, **kw: seq.pop(0))
    monkeypatch.setattr(relay_deploy.time, "sleep", lambda s: None)
    assert relay_deploy._call("get", "https://api.netlify.com/x").status_code == 200


def test_vercel_deploy_survives_failed_status_checks(monkeypatch):
    """The reported failure: the deployment was created, then ONE status check hit a
    dropped TLS connection and the whole deploy was reported as failed."""
    monkeypatch.setattr(relay_deploy.time, "sleep", lambda s: None)
    monkeypatch.setattr(relay_deploy.requests, "post",
                        lambda url, **kw: Resp(200, {"id": "dpl_1", "projectId": "prj_1"}))
    monkeypatch.setattr(relay_deploy.requests, "patch", lambda url, **kw: Resp(200))
    polls = []

    def get(url, **kw):
        polls.append(1)
        if len(polls) <= 5:                                   # several polls fail outright
            raise requests.exceptions.SSLError(VERCEL_SSL_EOF)
        return Resp(200, {"readyState": "READY", "url": "relay-abc.vercel.app"})

    monkeypatch.setattr(relay_deploy.requests, "get", get)
    out = relay_deploy.deploy_vercel("tok", "relay-abc")
    assert out["url"] == "https://relay-abc.vercel.app" and len(polls) == 6


def test_bad_token_is_explained_for_every_provider(monkeypatch):
    monkeypatch.setattr(relay_deploy.time, "sleep", lambda s: None)
    denied = Resp(401, text='{"error":{"message":"Invalid token"}}')
    for fn, args, verb, provider in [
        (relay_deploy.deploy_vercel, ("bad",), "post", "Vercel"),
        (relay_deploy.deploy_deno, ("bad", "org.deno.net"), "post", "Deno Deploy"),
        (relay_deploy.deploy_netlify, ("bad",), "post", "Netlify"),
        (relay_deploy.deploy_cloudflare, ("acc", "bad"), "put", "Cloudflare"),
    ]:
        monkeypatch.setattr(relay_deploy.requests, verb, lambda url, **kw: denied)
        try:
            fn(*args)
            assert False, provider
        except RelayDeployError as exc:
            assert provider in exc.user_message and "rejected the API token" in exc.user_message


def test_ping_failures_are_plain_language(monkeypatch):
    monkeypatch.setattr(relay_deploy.requests, "get", lambda url, **kw: Resp(404, text="nope"))
    assert "Nothing is deployed" in relay_deploy.ping_relay("https://r.example")["error"]
    monkeypatch.setattr(relay_deploy.requests, "get",
                        lambda url, **kw: (_ for _ in ()).throw(requests.exceptions.ReadTimeout(
                            "HTTPSConnectionPool(host='v.vercel.app', port=443): Read timed out.")))
    err = relay_deploy.ping_relay("https://v.vercel.app")["error"]
    assert "did not answer in time" in err and "HTTPSConnectionPool" not in err


# --------------------------------------------------------------------------- #
# endpoint + bulk delete
# --------------------------------------------------------------------------- #

def _app(tmp_path):
    return create_app(Config(
        server={"host": "127.0.0.1", "port": 8788, "admin_key": "adm-test",
                "client_keys": [{"key": "cli-test", "name": "local"}]},
        accounts={"source": "accounts_dir", "dir": str(tmp_path / "accounts")},
        store={"sqlite_path": str(tmp_path / "gw.db")},
        update={"enabled": False}, logging={"capture": False}))


def test_deploy_endpoint_returns_the_readable_message(tmp_path, monkeypatch):
    def boom(vercel_token, project_name=""):
        raise RelayDeployError("Vercel rejected the API token (HTTP 403).", detail="raw")

    monkeypatch.setattr("zcode_gateway.relay_manager.deploy_vercel", boom)
    with TestClient(_app(tmp_path)) as c:
        r = c.post("/admin/dash/relays/deploy", headers=ADMIN, json={"type": "vercel", "token": "x"})
        assert r.status_code == 400 and r.json()["detail"] == "Vercel rejected the API token (HTTP 403)."


def test_bulk_delete_by_ids_and_all(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        ids = []
        for n in range(5):
            r = c.post("/admin/dash/relays/add", headers=ADMIN,
                       json={"name": f"r{n}", "url": f"https://r{n}.example.app", "type": "vercel"})
            ids.append(r.json()["relay"]["id"])
        settings = c.get("/admin/dash/relays", headers=ADMIN).json()
        assert settings["active_id"] == ids[0]

        # delete three, including the active one
        r = c.post("/admin/dash/relays/delete", headers=ADMIN, json={"ids": [ids[0], ids[2], ids[3], "nope"]})
        assert r.status_code == 200 and r.json()["deleted"] == 3
        left = c.get("/admin/dash/relays", headers=ADMIN).json()
        assert [x["id"] for x in left["relays"]] == [ids[1], ids[4]]
        assert left["active_id"] == ids[1]                      # active moved to a surviving relay

        # nothing matches -> 0, no error
        assert c.post("/admin/dash/relays/delete", headers=ADMIN, json={"ids": ["zzz"]}).json()["deleted"] == 0
        # empty request is refused
        assert c.post("/admin/dash/relays/delete", headers=ADMIN, json={}).status_code == 400
        assert c.post("/admin/dash/relays/delete", headers=ADMIN, json={"ids": []}).status_code == 400

        # delete all
        r = c.post("/admin/dash/relays/delete", headers=ADMIN, json={"all": True})
        assert r.json()["deleted"] == 2
        left = c.get("/admin/dash/relays", headers=ADMIN).json()
        assert left["relays"] == [] and left["active_id"] == "" and left["active_url"] == ""


def test_bulk_delete_keeps_cooldown_state_of_survivors(tmp_path):
    mgr = RelayManager(tmp_path)
    a = mgr.add_relay("a", "https://a.example.app", "vercel")
    b = mgr.add_relay("b", "https://b.example.app", "vercel")
    mgr.mark_relay_failed("https://a.example.app")
    mgr.mark_relay_failed("https://b.example.app")
    assert mgr.delete_relays([a["id"]]) == 1
    assert "https://b.example.app" in mgr._failed and "https://a.example.app" not in mgr._failed
