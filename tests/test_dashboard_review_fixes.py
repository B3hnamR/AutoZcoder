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
<link rel="stylesheet" href="/assets/css/layout.css">
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
             const ctx = {root, signal:controller.signal};
             window.view = await module.mount(ctx);
           };
           window.mountProvider = async provider => {
             const module = await import('/assets/js/views/generator/vpn.js');
             const root = document.querySelector('#root');
             root.replaceChildren();
             window.view = await module.mountProvider({root, signal:controller.signal}, provider);
           };
        }""")
        yield page, overrides, defaults, calls
        page.evaluate("window.controller?.abort(); window.view?.destroy?.()")
        assert not errors
        assert not unexpected
        browser.close()


def mount(page, name):
    page.evaluate("name => mountView(name)", name)


def test_dashboard_sidebar_opens_separate_generator_and_route_pages(dashboard):
    page, _, defaults, calls = dashboard
    defaults.update({
        "/admin/dash/server": {"version": "fixture"},
        "/admin/dash/capabilities": {},
        "/admin/dash/zcode-status": {},
        "/admin/dash/notifications": {"events": []},
        "/admin/dash/update/status": {},
        "/admin/dash/generator/vpn/status": {"psiphon": {}, "surfshark": {}},
    })
    defaults["/admin/dash/generator/status"]["options"] = {
        "custom_ai_url": "https://judge.invalid/v1", "custom_ai_key_set": True,
    }
    shell = ASSETS.parent.joinpath("index.html").read_text(encoding="utf-8").replace("/dash/assets/", "/assets/")
    page.route("**/shell", lambda route: route.fulfill(body=shell, content_type="text/html"))
    page.goto("http://dashboard.test/shell#/proxy")
    title = page.locator("#view-title")
    nav = page.locator("#nav")
    nav.get_by_role("link", name="Proxy pool", exact=True).wait_for()
    assert title.inner_text() == "Proxy pool"
    nav.get_by_role("link", name="Psiphon", exact=True).click()
    page.get_by_role("region", name="Psiphon", exact=True).wait_for()
    assert page.get_by_role("region", name="Surfshark", exact=True).count() == 0
    nav.get_by_role("link", name="Surfshark", exact=True).click()
    page.get_by_role("region", name="Surfshark", exact=True).wait_for()
    assert page.get_by_role("region", name="Psiphon", exact=True).count() == 0
    nav.get_by_text("Generator", exact=True).click()
    nav.get_by_role("link", name="Auto Generate", exact=True).click()
    endpoint = page.get_by_label("Endpoint URL", exact=True)
    page.wait_for_function("document.querySelector('input[type=url]')?.value.includes('judge.invalid')")
    assert title.inner_text() == "Auto Generate"
    assert endpoint.input_value() == "https://judge.invalid/v1"
    endpoint.fill("https://other.invalid/v1")
    page.get_by_role("button", name="Save AI judge", exact=True).click()
    page.get_by_text("AI judge settings saved", exact=True).first.wait_for()
    assert ("POST", "/admin/dash/generator/options", {
        "custom_ai_url": "https://other.invalid/v1", "custom_ai_model": "",
    }) in calls
    page.set_viewport_size({"width": 375, "height": 812})
    page.locator("#nav-toggle").click()
    nav.get_by_role("link", name="Email setup", exact=True).click()
    page.get_by_text("Connect a Cloudflare domain", exact=False).wait_for()
    assert title.inner_text() == "Email setup"
    assert page.locator("#nav-toggle").get_attribute("aria-expanded") == "false"
    assert nav.get_by_role("link", name="Email setup", exact=True).get_attribute("aria-current") == "page"
    page.goto("http://dashboard.test/shell#/generator")
    page.get_by_role("button", name="Start manual signup", exact=True).wait_for()
    assert page.get_by_label("Endpoint URL", exact=True).count() == 0


def test_accounts_empty_pool_does_not_show_a_filter_message(dashboard):
    """An empty pool shows its own empty state, not "No accounts match that filter"."""
    page, overrides, _, _ = dashboard
    mount(page, "accounts")
    page.get_by_text("No accounts yet").wait_for()
    assert not page.get_by_role("button", name="Claim all pending").count()
    assert not page.locator("[data-nomatch]").is_visible()

    overrides["/admin/accounts"] = lambda route: route.fulfill(
        json={"accounts": [{"id": "a1", "email": "a@b.test", "state": "ready"}]})
    page.evaluate("view.refresh()")
    page.get_by_text("a@b.test").wait_for()
    assert not page.locator("[data-nomatch]").is_visible()
    page.locator("input[type=search]").fill("zzz-no-hit")
    assert page.locator("[data-nomatch]").is_visible()


def test_ip_sources_allow_several_providers_and_a_shared_mode(dashboard):
    page, overrides, defaults, calls = dashboard
    defaults["/admin/dash/generator/status"]["options"] = {
        "ip_source": "pool", "ip_sources": ["pool"], "ip_mode": "spread",
        "ip_exits": [{"key": "psiphon:DE", "label": "Psiphon \u00b7 Germany (DE)"}],
        "pool_available": 3, "types_counts": {"all": 3, "socks": 3},
    }
    mount(page, "generator")
    pool = page.get_by_role("checkbox", name="Proxy pool (3)")
    pool.wait_for()
    assert pool.is_checked()
    assert page.get_by_label("Proxy types", exact=True).is_visible()
    assert not page.locator("#generator-pin").is_visible()
    # mode only concerns the bundled networks, not the pool
    mode = page.locator(".ip-line", has_text="Mode")
    assert mode.is_hidden()
    with page.expect_response(lambda response: urlsplit(response.url).path == "/admin/dash/generator/options"):
        page.get_by_role("checkbox", name="Pinned proxy", exact=True).check()
    page.wait_for_timeout(50)
    assert page.locator("#generator-pin").is_visible()
    with page.expect_response(lambda response: urlsplit(response.url).path == "/admin/dash/generator/options"):
        page.get_by_role("checkbox", name="Pinned proxy", exact=True).uncheck()
    page.wait_for_timeout(50)
    assert not page.locator("#generator-pin").is_visible()

    with page.expect_response(lambda response: urlsplit(response.url).path == "/admin/dash/generator/options"):
        page.get_by_role("checkbox", name="Psiphon", exact=True).check()
        page.get_by_role("radio", name="Stay on one").check()
    page.wait_for_timeout(50)
    assert mode.is_visible()
    assert page.get_by_role("radio", name="Random").is_visible()
    pick = page.get_by_label("Exit in use", exact=True)
    pick.wait_for()
    assert pick.input_value() == "psiphon:DE"
    body = [c for c in calls if c[1] == "/admin/dash/generator/options"][-1][2]
    assert body["ip_sources"] == ["pool", "psiphon"]
    assert body["ip_mode"] == "single" and body["ip_pick"] == "psiphon:DE"
    assert body["ip_source"] == "pool"

    with page.expect_response(lambda response: urlsplit(response.url).path == "/admin/dash/generator/options"):
        page.get_by_role("checkbox", name="Psiphon", exact=True).uncheck()
    page.wait_for_timeout(50)
    body = [c for c in calls if c[1] == "/admin/dash/generator/options"][-1][2]
    assert body["ip_sources"] == ["pool"]
    assert mode.is_hidden()
    assert not page.get_by_label("Exit in use", exact=True).is_visible()

    # layout: sources and mode are full-width rows, never a squeezed column
    field = page.locator(".ip-field")
    assert field.evaluate("node => node.scrollWidth <= node.clientWidth + 1")
    rows = page.locator(".ip-line .chip").evaluate_all(
        "nodes => [...new Set(nodes.map(n => Math.round(n.getBoundingClientRect().top)))].length")
    assert rows == 2
    page.set_viewport_size({"width": 1280, "height": 900})
    page.set_viewport_size({"width": 375, "height": 900})
    assert field.evaluate("node => node.scrollWidth <= node.clientWidth + 1")


def test_long_select_value_stays_within_control_and_menu_shows_full_label(dashboard):
    page, _, defaults, _ = dashboard
    defaults["/admin/dash/capabilities"] = {"os": "windows"}
    defaults["/admin/dash/generator/status"]["options"] = {"captcha": "mint", "browser_display": "headless"}
    page.evaluate("import('/assets/js/core/caps.js').then(m => m.load())")
    page.set_viewport_size({"width": 675, "height": 900})
    mount(page, "generator")
    for name, value in [("Captcha", "mint"), ("Browser display", "headless")]:
        select = page.get_by_role("combobox", name=name, exact=True)
        assert select.input_value() == value
        assert select.evaluate("n => n.scrollHeight <= n.clientHeight && n.scrollWidth <= n.clientWidth")
        if select.evaluate("n => getComputedStyle(n).appearance === 'base-select'"):
            full_label = select.locator("option:checked").inner_text()
            assert select.locator("selectedcontent").text_content() == full_label
            select.focus()
            page.keyboard.press("Space")
            assert select.locator("option:checked").is_visible()
            assert select.locator("option:checked").inner_text() == full_label
            page.keyboard.press("Escape")
            assert select.input_value() == value


def test_ip_picker_refreshes_without_saving_a_fallback_or_clearing_the_last_source(dashboard):
    page, _, defaults, calls = dashboard
    options = {"ip_sources": ["psiphon"], "ip_mode": "single", "ip_pick": "psiphon:DE",
               "ip_exits": [{"key": "direct", "label": "My network"}]}
    defaults["/admin/dash/generator/status"]["options"] = options
    mount(page, "generator")
    pick = page.get_by_label("Exit in use", exact=True)
    assert pick.is_disabled()
    source = page.get_by_role("checkbox", name="Psiphon", exact=True)
    source.click()
    page.get_by_text("Keep at least one IP source selected", exact=True).wait_for()
    assert source.is_checked()

    options["ip_exits"].append({"key": "psiphon:NL", "label": "Netherlands"})
    page.evaluate("view.refresh()")
    assert pick.locator("option").all_text_contents() == ["First connected VPN exit", "Netherlands"]
    assert pick.is_enabled()
    options["ip_exits"].append({"key": "psiphon:DE", "label": "Germany"})
    page.evaluate("view.refresh()")
    assert pick.input_value() == "psiphon:DE"
    if pick.evaluate("n => getComputedStyle(n).appearance === 'base-select'"):
        assert pick.locator("selectedcontent").text_content() == "Germany"
    assert all(method != "POST" or not path.endswith("/generator/options") for method, path, _ in calls)



def test_generator_start_points_to_email_setup_before_launch(dashboard):
    page, _, _, calls = dashboard
    mount(page, "generator")
    manual = page.get_by_text("Manual signup", exact=True).locator("xpath=ancestor::*[contains(@class, 'card')][2]")
    start = page.get_by_role("button", name="Start manual signup", exact=True)
    assert manual.get_by_role("button", name="Start manual signup", exact=True).is_visible()
    assert page.locator(".auto-generate").count() == 0
    start.click()
    page.get_by_text("Finish Email setup before starting.", exact=False).first.wait_for()
    assert page.evaluate("location.hash") == "#/generator/mail"
    assert not any(path == "/admin/dash/generator/start" for _, path, _ in calls)


def test_accounts_actions_reload_visible_results(dashboard):
    page, overrides, defaults, calls = dashboard
    accounts = [{"id": "first", "email": "first@example.test", "state": "ready"}]
    defaults["/admin/accounts"] = {"accounts": accounts}
    defaults["/admin/accounts/first/balance"] = {"quota_refreshed": True}

    def reload_pool(route):
        accounts.append({"id": "second", "email": "second@example.test", "state": "ready"})
        route.fulfill(json={"loaded": 2, "added": 1, "updated": 0})

    overrides["/admin/pool/reload"] = reload_pool
    mount(page, "accounts")
    page.get_by_role("button", name="Refresh quotas", exact=True).click()
    page.get_by_text("All quotas refreshed", exact=True).first.wait_for()
    assert ("POST", "/admin/accounts/first/balance", {}) in calls
    page.get_by_role("button", name="Reload pool", exact=True).click()
    page.get_by_text("second@example.test", exact=True).wait_for()


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

def test_generator_ip_source_card_lists_bundled_exits(dashboard):
    """The IP source card renders Psiphon regions and the Surfshark key form."""
    page, overrides, defaults, calls = dashboard
    vpn = {
        "psiphon": {
            "installed": True, "problem": "", "busy": "", "lastError": "",
            "running": True, "live": 1,
            "config": {"enabled": True, "autoStart": False, "regions": ["NL"],
                       "mode": "spread", "pinned": ""},
            "available": ["DE", "NL"],
            "regionNames": {"DE": "Germany", "NL": "Netherlands"},
            "regions": [{"region": "NL", "state": "ready", "port": 47100,
                         "exitIp": "203.0.113.7", "location": "NL", "since": 0, "error": ""}],
        },
        "surfshark": {
            "installed": True, "problem": "", "busy": "", "lastError": "",
            "running": False, "live": 0,
            "config": {"enabled": True, "autoStart": False, "regions": [],
                       "mode": "spread", "pinned": ""},
            "servers": [], "keys": {"saved": False, "publicKey": ""},
            "updatedAt": 0, "regions": [],
        },
    }
    defaults["/admin/dash/generator/vpn/status"] = vpn
    overrides["/admin/dash/generator/vpn/psiphon/renew"] = lambda route: route.fulfill(json=vpn)
    page.add_style_tag(url="/assets/css/layout.css")
    page.evaluate("mountProvider('psiphon')")
    panel = page.locator(".vpn-card")
    psiphon = panel.get_by_role("region", name="Psiphon", exact=True)
    surfshark = panel.get_by_role("region", name="Surfshark", exact=True)
    assert page.get_by_text("1 of 1 connected").is_visible()
    assert page.get_by_text("203.0.113.7").is_visible()
    assert page.get_by_text("Netherlands").is_visible()
    assert surfshark.count() == 0
    with page.expect_response(lambda response: response.url.endswith("/psiphon/renew")):
        page.get_by_role("button", name="New address").click()
    renew = [c for c in calls if c[1] == "/admin/dash/generator/vpn/psiphon/renew"]
    assert renew and renew[-1][2] == {"region": "NL"}

    overrides["/admin/dash/generator/vpn/psiphon/settings"] = lambda route: route.fulfill(
        status=400, json={"detail": "Test region save rejected"})
    psiphon.get_by_role("checkbox", name="DE", exact=True).check()
    psiphon.get_by_role("button", name="Save regions").click()
    page.get_by_text("Test region save rejected", exact=False).first.wait_for()
    assert psiphon.get_by_role("checkbox", name="DE", exact=True).is_checked()
    assert psiphon.get_by_role("button", name="Save regions").is_visible()

    page.evaluate("mountProvider('surfshark')")
    page.get_by_text("Surfshark key pair").wait_for()
    assert page.get_by_label("Private key", exact=True).get_attribute("type") == "password"
    assert psiphon.count() == 0
    page.set_viewport_size({"width": 375, "height": 812})
    assert panel.evaluate("node => node.scrollWidth <= node.clientWidth")
    page.get_by_text("Public key", exact=True).click()
    assert page.get_by_label("Public key", exact=True).evaluate("node => node === document.activeElement")
    page.keyboard.type("dummy")
    page.evaluate("view.refresh()")
    assert page.get_by_label("Public key", exact=True).evaluate("node => node === document.activeElement")
    assert page.get_by_label("Public key", exact=True).input_value() == "dummy"
    overrides["/admin/dash/generator/vpn/surfshark/keys"] = lambda route: route.fulfill(
        status=400, json={"detail": "Test key save rejected"})
    private = page.get_by_label("Private key", exact=True)
    private.fill("dummy-private")
    page.get_by_role("button", name="Save key pair").click()
    page.get_by_text("Test key save rejected", exact=False).first.wait_for()
    assert private.input_value() == "dummy-private"
    assert page.get_by_label("Public key", exact=True).input_value() == "dummy"

    vpn["surfshark"]["keys"]["saved"] = True
    vpn["surfshark"]["servers"] = [
        {"id": "nl-ams", "country": "Netherlands", "countryCode": "NL", "location": "Amsterdam"},
        {"id": "de-ber", "country": "Germany", "countryCode": "DE", "location": "Berlin"},
    ]
    page.evaluate("view.refresh()")
    search = page.get_by_role("searchbox", name="Search locations")
    search.click()
    page.keyboard.type("Berlin")
    assert search.input_value() == "Berlin"
    assert search.evaluate("node => node === document.activeElement")
    assert not page.get_by_role("region", name="Surfshark").get_by_text("Amsterdam").is_visible()

    search.fill("")
    vpn["surfshark"]["servers"] += [
        {"id": f"us-test-{i}", "country": "United States", "countryCode": "US", "location": f"City {i}"}
        for i in range(30)
    ]
    vpn["surfshark"]["regions"] = [
        {"region": f"us-test-{i}", "place": f"United States · City {i}", "state": "ready"}
        for i in range(12)
    ]
    page.evaluate("view.refresh()")
    for width in (375, 1280):
        page.set_viewport_size({"width": width, "height": 812})
        assert panel.evaluate("node => node.scrollWidth <= node.clientWidth")
        for name in ("Available locations", "Location status"):
            scroll = surfshark.get_by_role("region", name=name, exact=True)
            assert scroll.evaluate("node => node.scrollHeight > node.clientHeight")
            scroll.focus()
            position = scroll.evaluate("node => { node.scrollTop = 80; return node.scrollTop; }")
            assert position > 0
            page.evaluate("view.refresh()")
            assert scroll.evaluate("(node, position) => node.scrollTop === position && node === document.activeElement", position)


def test_relay_card_offers_connected_vpn_exits_as_relays(dashboard):
    page, _, defaults, calls = dashboard
    state = {
        "enabled": True, "strategy": "single", "models": ["GLM-5.4"],
        "vpn_ids": [], "relays": [],
        "vpn_exits": [{"key": "psiphon:DE", "label": "Psiphon · Germany (DE)",
                       "url": "socks5://127.0.0.1:47190"}],
    }
    defaults["/admin/dash/relays"] = state

    def save(route):
        body = route.request.post_data_json
        calls.append(("POST", "/admin/dash/relays/settings", body))
        state["vpn_ids"] = body.get("vpn_ids") or []
        route.fulfill(json={"ok": True, "settings": state})

    page.route("**/admin/dash/relays/settings", save)
    page.evaluate("""async () => {
      const module = await import('/assets/js/views/proxy/relays.js');
      const card = module.createRelaysCard();
      document.querySelector('#root').replaceChildren(card.node);
      window.card = card;
      await card.refresh();
    }""")
    chip = page.get_by_role("checkbox", name="Psiphon · Germany (DE)", exact=True)
    chip.check()
    page.wait_for_function("document.querySelector('.relay-vpns input:checked') !== null")
    assert chip.is_checked()
    last = [entry[2] for entry in calls if entry[1].endswith("/relays/settings")][-1]
    assert last["vpn_ids"] == ["psiphon:DE"]
    assert last["models"] == ["GLM-5.4"]

    chip.uncheck()
    page.wait_for_function("document.querySelector('.relay-vpns input:checked') === null")
    last = [entry[2] for entry in calls if entry[1].endswith("/relays/settings")][-1]
    assert last["vpn_ids"] == []

    state["vpn_exits"] = []
    page.evaluate("window.card.refresh()")
    assert page.get_by_text("No connected VPN exits", exact=False).is_visible()
