"""Dashboard lifecycle regressions, with all browser traffic intercepted locally."""
from pathlib import Path
from urllib.parse import urlsplit

import pytest


ASSETS = Path(__file__).resolve().parents[1] / "zcode_gateway/dashboard/assets"


@pytest.fixture
def dashboard():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch()
        except playwright.Error as exc:
            pytest.skip("Installed Chromium unavailable: " + str(exc))
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        errors, unexpected, calls = [], [], []
        overrides = {}
        defaults = {
            "/admin/accounts": {"accounts": []},
            "/admin/accounts/campaign_window": {"active": False, "gate": False},
            "/admin/pool/state": {},
            "/admin/claims/claim_all_status": {},
            "/admin/dash/proxy/socks_ready": {},
            "/admin/dash/relays": {"relays": [], "settings": {}},
            "/admin/dash/generator/proxies": {"stats": {}, "lines": []},
            "/admin/dash/generator/mail": {},
            "/admin/dash/generator/status": {"options": {}, "workers": [], "claims": []},
            "/admin/dash/generator/auto": {"config": {}, "status": {"state": "idle"}},
            "/admin/dash/generator/options": {"ok": True},
            "/admin/dash/auth/login/start": {"verification_url": "https://approval.invalid/login"},
            "/admin/dash/auth/login/status": {"state": "waiting"},
            "/admin/dash/auth/login/cancel": {"ok": True},
            "/admin/dash/logs": {"records": []},
        }

        def handle(route):
            request = route.request
            path = urlsplit(request.url).path
            if path.startswith("/assets/"):
                target = ASSETS / path.removeprefix("/assets/")
                route.fulfill(body=target.read_text(encoding="utf-8"), content_type=(
                    "text/css" if path.endswith(".css") else "text/javascript"))
            elif path == "/":
                route.fulfill(content_type="text/html", body="""<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/assets/css/tokens.css">
<link rel="stylesheet" href="/assets/css/base.css">
<link rel="stylesheet" href="/assets/css/components.css">
<main id="root" style="padding:16px"></main>""")
            else:
                calls.append((request.method, path, request.post_data_json
                              if request.method != "GET" else None))
                if path in overrides:
                    overrides[path](route)
                elif path in defaults:
                    route.fulfill(json=defaults[path])
                else:
                    unexpected.append(request.url)
                    route.abort()

        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", handle)
        page.goto("http://dashboard.test/")
        page.evaluate("""() => {
          window.popups = [];
          window.open = (...args) => { popups.push(args); return null; };
          window.controller = new AbortController();
          window.mountView = async name => {
            const module = await import('/assets/js/views/' + name + '.js');
            const root = document.querySelector('#root');
            window.view = name === 'generator/mail' ? module.createMailCard()
              : await module.mount({root, signal:controller.signal});
            if (name === 'generator/mail') { root.append(view.node); await view.refresh(); }
          };
        }""")
        yield page, overrides, defaults, calls
        page.evaluate("controller.abort(); window.view?.destroy?.()")
        assert not errors
        assert not unexpected
        browser.close()


def mount(page, name):
    page.evaluate("name => mountView(name)", name)


@pytest.mark.parametrize("status", [0, 401, 500, 503])
def test_proxy_only_404_shows_locator_and_recovers(dashboard, status):
    page, overrides, _, _ = dashboard
    path = "/admin/dash/generator/proxies"
    overrides[path] = lambda route: route.abort() if status == 0 else route.fulfill(
        status=status, json={"detail": "fixture unavailable"})
    mount(page, "proxy")
    assert "Proxy status unavailable" in page.locator("#root").inner_text()
    assert page.get_by_text("Generator folder", exact=True).count() == 0
    del overrides[path]
    page.evaluate("view.refresh()")
    page.get_by_text("Proxy checker", exact=True).wait_for()
    field = page.locator("input[type=number]").first
    field.fill("12")
    overrides[path] = lambda route: route.fulfill(status=503, json={"detail": "offline"})
    page.evaluate("view.refresh()")
    assert field.input_value() == "12"
    assert "Proxy status unavailable" in page.locator("#root").inner_text()


def test_proxy_404_locator_and_aborted_initial_mount_cannot_replace_new_root(dashboard):
    page, overrides, _, calls = dashboard
    path = "/admin/dash/generator/proxies"
    overrides[path] = lambda route: route.fulfill(status=404, json={"detail": "missing scripts"})
    mount(page, "proxy")
    assert "missing scripts" in page.locator("#root").inner_text()
    assert page.locator("#root input[type=text]").count() > 0
    held = []
    overrides[path] = lambda route: held.append(route)
    with page.expect_request(lambda request: urlsplit(request.url).path == path):
        page.evaluate("void (window.pendingMount = mountView('proxy'))")
    page.wait_for_timeout(50)
    assert held
    page.evaluate("controller.abort(); document.querySelector('#root').textContent = 'Next page'")
    held.pop().fulfill(json={"stats": {}})
    page.evaluate("pendingMount")
    assert page.locator("#root").inner_text() == "Next page"
    before = len(calls)
    page.evaluate("view.refresh()")
    assert len(calls) == before


def test_mail_status_failure_keeps_form_and_secrets_through_recovery(dashboard):
    page, overrides, defaults, _ = dashboard
    path = "/admin/dash/generator/mail"
    overrides[path] = lambda route: route.fulfill(status=503, json={"detail": "offline"})
    mount(page, "generator/mail")
    page.get_by_text("Advanced / manual recovery", exact=True).click()
    values = {"mail-new-domain": "typed.example", "mail-cf-token": "private-token",
              "mail-cf-account": "account", "mail-inbox-secret": "private-secret",
              "mail-inbox-url": "https://typed.example/inbox"}
    for field, value in values.items():
        page.locator("#" + field).fill(value)
    page.evaluate("window.originalToken = document.querySelector('#mail-cf-token')")
    page.evaluate("view.refresh()")
    assert page.get_by_role("alert").is_visible()
    defaults[path] = {"inbox_url": "https://server.example/inbox", "cf_token_set": True}
    del overrides[path]
    page.evaluate("view.refresh()")
    assert not page.get_by_role("alert").is_visible()
    assert page.evaluate("originalToken === document.querySelector('#mail-cf-token')")
    for field, value in values.items():
        assert page.locator("#" + field).input_value() == value
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("result,clears,message", [
    ({"ok": False, "error": "validation rejected"}, False, "Token was not saved"),
    ({"ok": False, "saved": False, "error": "disk rejected"}, False, "Token was not saved"),
    ({"ok": False, "saved": True, "error": "token saved, verification failed"}, True, "verification failed"),
    ({"ok": True, "saved": True, "verified": False}, True, "verification is pending"),
    ({"ok": True, "saved": True, "verified": True}, True, "Token saved"),
])
def test_mail_credentials_outcomes(dashboard, result, clears, message):
    page, overrides, _, calls = dashboard
    mount(page, "generator/mail")
    page.get_by_text("Advanced / manual recovery", exact=True).click()
    token = page.locator("#mail-cf-token")
    token.fill("private-token")
    overrides["/admin/dash/generator/mail/cf-credentials"] = lambda route: route.fulfill(json=result)
    page.get_by_role("button", name="Save token only", exact=True).click()
    page.wait_for_function("text => document.querySelector('#root').textContent.includes(text)", arg=message)
    assert token.input_value() == ("" if clears else "private-token")
    assert [c for c in calls if c[0] == "POST"][-1][2]["cf_token"] == "private-token"


def test_mail_remove_failure_and_success_do_not_clear_new_token(dashboard):
    page, overrides, defaults, _ = dashboard
    defaults["/admin/dash/generator/mail"] = {"cf_token_set": True}
    mount(page, "generator/mail")
    page.get_by_text("Advanced / manual recovery", exact=True).click()
    page.locator("#mail-cf-token").fill("new-unsaved-token")
    path = "/admin/dash/generator/mail/cf-credentials"
    overrides[path] = lambda route: route.fulfill(json={"ok": False, "error": "busy"})
    page.get_by_role("button", name="Remove saved token", exact=True).click()
    page.get_by_text("Token was not removed: busy", exact=True).first.wait_for()
    assert page.locator("#mail-cf-token").input_value() == "new-unsaved-token"
    defaults["/admin/dash/generator/mail"] = {"cf_token_set": False}
    overrides[path] = lambda route: route.fulfill(json={"ok": True})
    page.get_by_role("button", name="Remove saved token", exact=True).click()
    page.wait_for_function("!Array.from(document.querySelectorAll('button')).some(b => b.textContent === 'Remove saved token')")
    assert page.locator("#mail-cf-token").input_value() == "new-unsaved-token"


def test_mail_transport_failure_and_late_save_preserve_unsaved_token(dashboard):
    page, overrides, _, _ = dashboard
    mount(page, "generator/mail")
    page.get_by_text("Advanced / manual recovery", exact=True).click()
    token = page.locator("#mail-cf-token")
    token.fill("first-token")
    path = "/admin/dash/generator/mail/cf-credentials"
    overrides[path] = lambda route: route.fulfill(status=503, json={"detail": "offline"})
    page.get_by_role("button", name="Save token only", exact=True).click()
    page.get_by_text("Token was not saved: offline", exact=True).first.wait_for()
    assert token.input_value() == "first-token"
    held = []
    overrides[path] = lambda route: held.append(route)
    page.get_by_role("button", name="Save token only", exact=True).click()
    page.wait_for_timeout(50)
    token.fill("replacement-not-submitted")
    held.pop().fulfill(json={"ok": True, "saved": True})
    page.get_by_text("Token saved", exact=True).first.wait_for()
    assert token.input_value() == "replacement-not-submitted"


@pytest.mark.parametrize("navigate", [False, True])
@pytest.mark.parametrize("pending_status", [False, True])
def test_login_cancel_or_navigation_stops_pending_requests(dashboard, navigate, pending_status):
    page, overrides, _, calls = dashboard
    page.clock.install()
    held = []
    path = "/admin/dash/auth/login/" + ("status" if pending_status else "start")
    overrides[path] = lambda route: held.append(route)
    mount(page, "accounts")
    page.get_by_role("button", name="Login with Zcode", exact=True).first.click()
    if pending_status:
        page.wait_for_function("popups.length === 1")
        page.clock.run_for(1500)
    page.wait_for_timeout(50)
    assert held
    if navigate:
        page.evaluate("controller.abort(); document.querySelector('#root').textContent = 'Next page'")
    else:
        page.get_by_role("button", name="Cancel", exact=True).click()
    held.pop().fulfill(json={"state": "done", "verification_url": "https://approval.invalid/late"})
    page.clock.run_for(6000)
    assert page.get_by_role("dialog").count() == 0
    assert page.evaluate("popups.length") == int(pending_status)
    assert len([c for c in calls if c[1].endswith("/status") and "/auth/" in c[1]]) == int(pending_status)
    assert len([c for c in calls if c[1].endswith("/cancel")]) == 1
    assert page.get_by_text("Account added", exact=True).count() == 0


def test_generator_destroy_flushes_captured_snapshots_in_order(dashboard):
    page, overrides, _, calls = dashboard
    page.clock.install()
    held = []
    path = "/admin/dash/generator/options"
    overrides[path] = lambda route: held.append(route)
    mount(page, "generator")
    retries = page.get_by_text("Retries", exact=True).locator("..").locator("input")
    retries.fill("3")
    page.clock.run_for(601)
    page.wait_for_timeout(50)
    assert len(held) == 1
    retries.fill("7")
    page.evaluate("controller.abort(); view.destroy(); document.querySelector('#root').textContent = 'Next page'")
    assert len([c for c in calls if c[1] == path]) == 1
    held.pop(0).fulfill(json={"ok": True})
    page.wait_for_function("true")
    page.wait_for_timeout(50)
    assert len(held) == 1
    assert [c[2]["retries"] for c in calls if c[1] == path] == [3, 7]
    held.pop().fulfill(status=503, json={"detail": "offline"})
    page.clock.run_for(2000)
    assert page.locator("#root").inner_text() == "Next page"


def test_generator_autosave_and_offline_status_feedback_persist(dashboard):
    page, overrides, _, _ = dashboard
    page.clock.install()
    mount(page, "generator")
    retries = page.get_by_text("Retries", exact=True).locator("..").locator("input")
    retries.fill("2")
    page.clock.run_for(601)
    page.get_by_text("Saved", exact=True).wait_for()
    overrides["/admin/dash/generator/options"] = lambda route: route.fulfill(json={"ok": False, "error": "rejected"})
    retries.fill("4")
    page.clock.run_for(601)
    page.get_by_text("Options not saved. Edit to retry.", exact=True).wait_for()
    overrides["/admin/dash/generator/status"] = lambda route: route.fulfill(status=503, json={"detail": "offline"})
    page.evaluate("view.refresh()")
    page.clock.run_for(6000)
    assert page.get_by_text("Options not saved. Edit to retry.", exact=True).is_visible()
    assert "Gateway temporarily unavailable" in page.locator("#root").inner_text()
    assert retries.input_value() == "4"
    del overrides["/admin/dash/generator/options"]
    del overrides["/admin/dash/generator/status"]
    retries.fill("5")
    page.clock.run_for(601)
    page.get_by_text("Saved", exact=True).wait_for()
    page.evaluate("view.refresh()")
    assert "Gateway temporarily unavailable" not in page.locator("#root").inner_text()


def test_generator_dirty_warning_until_save_and_pending_load_navigation(dashboard):
    page, overrides, _, _ = dashboard
    page.clock.install()
    mount(page, "generator")
    retries = page.get_by_text("Retries", exact=True).locator("..").locator("input")
    retries.fill("8")
    dispatch = """() => {
      const event = new Event('beforeunload', {cancelable:true});
      window.dispatchEvent(event); return event.defaultPrevented;
    }"""
    assert page.evaluate(dispatch)
    page.clock.run_for(601)
    page.get_by_text("Saved", exact=True).wait_for()
    assert not page.evaluate(dispatch)
    held = []
    overrides["/admin/dash/generator/status"] = lambda route: held.append(route)
    page.evaluate("void (window.pendingRefresh = view.refresh())")
    page.wait_for_timeout(50)
    page.evaluate("controller.abort(); view.destroy(); document.querySelector('#root').textContent = 'Next page'")
    held.pop().fulfill(status=404, json={"detail": "late missing scripts"})
    page.evaluate("pendingRefresh")
    assert page.locator("#root").inner_text() == "Next page"


def test_logs_clear_watermark_survives_polls_filters_and_duplicate_text(dashboard):
    page, _, defaults, _ = dashboard
    page.clock.install(time=1793727000000)
    path = "/admin/dash/logs"

    def record(ts, message):
        return {"ts": ts, "time": "17:30:00", "level": "INFO", "logger": "fixture", "message": message}

    old = record(1793726999, "repeated message")
    defaults[path] = {"records": [old]}
    mount(page, "logs")
    page.get_by_role("button", name="Clear", exact=True).click()
    page.evaluate("view.refresh()")
    assert page.locator(".logpane__line").count() == 0
    defaults[path] = {"records": [old, record(1793727001, "repeated message")]}
    page.evaluate("view.refresh()")
    assert page.locator(".logpane__line").count() == 1
    page.get_by_label("Filter by level").select_option("INFO")
    page.wait_for_function("document.querySelectorAll('.logpane__line').length === 1")
    page.get_by_label("Search logs").fill("repeated")
    page.clock.run_for(301)
    assert page.locator(".logpane__line").count() == 1
    page.get_by_role("button", name="Clear", exact=True).click()
    page.evaluate("view.refresh()")
    assert page.locator(".logpane__line").count() == 0
    defaults[path]["records"].append(record(1793727002, "latest record"))
    page.evaluate("view.refresh()")
    assert page.locator(".logpane__line").count() == 1
    assert "latest record" in page.locator(".logpane").inner_text()
