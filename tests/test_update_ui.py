"""Updater browser contracts. Every page request is intercepted, including assets."""
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import pytest


ASSETS = Path(__file__).resolve().parents[1] / "zcode_gateway/dashboard/assets"
RELEASE = {
    "checked_at": 1, "current": "0.3.0", "latest": "0.4.0",
    "update_available": True, "updater_phase": "idle", "downloaded": False,
    "release_url": "https://github.com/example/gateway/releases/tag/v0.4.0",
    "notes": "A fixture release. <img src=x onerror=alert(1)>",
}


@pytest.fixture
def updater_page():
    playwright = pytest.importorskip("playwright.sync_api")
    state = {
        "status": deepcopy(RELEASE), "os": "windows", "calls": [],
        "errors": [], "unexpected": [], "pending": {}, "hold": set(),
        "fail": {},
    }

    def handle(route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path
        if url.hostname != "update.test":
            state["unexpected"].append(request.url)
            route.abort()
        elif path == "/admin/dash/capabilities":
            route.fulfill(json={"os": state["os"], "features": {
                "self_update": state["os"] == "windows"}})
        elif path.startswith("/admin/dash/update/"):
            action = path.rsplit("/", 1)[1]
            state["calls"].append((request.method, action))
            if action in state["hold"]:
                state["pending"][action] = route
            elif action in state["fail"]:
                route.fulfill(status=409, json={"detail": state["fail"][action]})
            elif action in ("status", "check"):
                route.fulfill(json=state["status"])
            else:
                route.fulfill(json={"downloaded": True} if action == "download" else {"applying": True})
        elif path.startswith("/assets/"):
            target = ASSETS / path.removeprefix("/assets/")
            route.fulfill(body=target.read_text(encoding="utf-8"),
                          content_type="text/css" if path.endswith(".css") else "text/javascript")
        elif path == "/":
            route.fulfill(content_type="text/html", body='''<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/assets/css/tokens.css">
<link rel="stylesheet" href="/assets/css/base.css">
<link rel="stylesheet" href="/assets/css/components.css">
<button id="updates">Updates</button>
<script type="module">
import * as updates from '/assets/js/core/updates.js';
import * as caps from '/assets/js/core/caps.js';
window.updates = updates;
await caps.load();
updates.init(document.querySelector('#updates'));
window.booted = true;
</script>''')
        else:
            state["unexpected"].append(request.url)
            route.abort()

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch()
        except playwright.Error as exc:
            pytest.skip("Installed Chromium unavailable: " + str(exc))
        page = browser.new_page(viewport={"width": 1280, "height": 900}, service_workers="block")
        page.on("pageerror", lambda error: state["errors"].append(str(error)))
        page.route("**/*", handle)
        page.clock.install()
        yield page, state
        assert not state["errors"]
        assert not state["unexpected"]
        browser.close()


def boot(page):
    page.goto("http://update.test/")
    page.wait_for_function("window.booted === true")
    page.evaluate("updates.poll()")
    page.locator("#updates").click()


def test_older_published_release_never_looks_like_downgrade_offer(updater_page):
    page,state=updater_page
    state['status'].update(current='2.0.0',latest='1.2.2',update_available=False,release_available=False)
    boot(page)
    assert 'Installed 2.0.0 | Latest published 1.2.2' in page.locator('.update-card').inner_text()
    assert '2.0.0 -> 1.2.2' not in page.locator('.update-card').inner_text()
    assert 'No downgrade will be offered' in page.get_by_role('status').inner_text()
    assert not page.get_by_role('button',name='Download update',exact=True).is_visible()
    assert not page.get_by_role('button',name='Restart to apply',exact=True).is_visible()
    assert not page.locator('.update-card__notes').is_visible()
    assert ('POST','download') not in state['calls'] and ('POST','apply') not in state['calls']


def test_download_requires_explicit_restart_and_retry_keeps_download(updater_page, tmp_path):
    page, state = updater_page
    boot(page)
    assert "does not stop work" in page.locator(".field__help").inner_text()
    assert "Active work blocks restart" in page.locator(".field__help").inner_text()
    assert page.get_by_role("status").get_attribute("aria-live") == "polite"
    assert page.locator(".update-card img").count() == 0
    page.get_by_role("button", name="Download update", exact=True).click()
    restart = page.get_by_role("button", name="Restart to apply", exact=True)
    restart.wait_for()
    assert restart.is_enabled()
    assert ("POST", "apply") not in state["calls"]
    state["fail"]["apply"] = "Active workers must finish first"
    restart.click()
    page.get_by_role("status").filter(has_text="Restart not started").wait_for()
    assert restart.is_enabled()
    assert "download is ready" in page.get_by_role("status").inner_text()
    assert not page.get_by_role("button", name="Download update", exact=True).is_visible()
    # A forced check can return release metadata without download state.
    state["status"].pop("updater_phase")
    state["status"].pop("downloaded")
    assert page.evaluate("updates.checkNow()") == state["status"]
    assert restart.is_enabled()
    state["status"].update(updater_phase="ready", downloaded=True)
    page.evaluate("updates.poll()")
    assert restart.is_enabled()
    page.screenshot(path=str(tmp_path / "update-desktop.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / "update-mobile.png"))
    state["fail"].clear()
    restart.click()
    page.get_by_role("status").filter(has_text="The app will relaunch").wait_for()
    assert restart.is_disabled()
    assert state["calls"].count(("POST", "download")) == 1
    assert state["calls"].count(("POST", "apply")) == 2


def test_close_reopen_does_not_duplicate_download_or_apply(updater_page):
    page, state = updater_page
    state["hold"].add("download")
    boot(page)
    page.get_by_role("button", name="Download update", exact=True).click()
    page.wait_for_function("document.querySelector('.update-card').getAttribute('aria-busy') === 'true'")
    page.get_by_role("button", name="Close", exact=True).click()
    page.locator("#updates").click()
    assert page.get_by_role("button", name="Download update", exact=True).is_disabled()
    assert state["calls"].count(("POST", "download")) == 1
    state["pending"].pop("download").fulfill(json={"downloaded": True})
    restart = page.get_by_role("button", name="Restart to apply", exact=True)
    restart.wait_for()
    state["hold"].add("apply")
    restart.click()
    page.get_by_role("button", name="Close", exact=True).click()
    page.locator("#updates").click()
    assert restart.is_disabled()
    assert page.get_by_role("button", name="Check now", exact=True).is_disabled()
    assert state["calls"].count(("POST", "apply")) == 1
    state["pending"].pop("apply").fulfill(json={"applying": True})


def test_failed_download_can_retry_and_stale_poll_cannot_reset_ready(updater_page):
    page, state = updater_page
    boot(page)
    state["fail"]["download"] = "Fixture download rejected"
    download = page.get_by_role("button", name="Download update", exact=True)
    download.click()
    page.get_by_role("status").filter(has_text="Download failed").wait_for()
    assert download.is_enabled()
    state["fail"].clear()
    state["hold"].add("status")
    page.evaluate("void updates.poll()")
    page.wait_for_function("true")
    download.click()
    restart = page.get_by_role("button", name="Restart to apply", exact=True)
    restart.wait_for()
    state["hold"].remove("status")
    state["status"].update(updater_phase="ready", downloaded=True)
    state["pending"].pop("status").fulfill(json=RELEASE)
    page.evaluate("updates.poll()")
    assert restart.is_enabled()
    assert not download.is_visible()


def test_check_now_is_single_flight_across_card_reopen(updater_page):
    page, state = updater_page
    state["hold"].add("check")
    boot(page)
    page.get_by_role("button", name="Check now", exact=True).click()
    page.get_by_role("button", name="Close", exact=True).click()
    page.locator("#updates").click()
    assert page.get_by_role("button", name="Check now", exact=True).is_disabled()
    page.evaluate("void updates.checkNow()")
    assert state["calls"].count(("POST", "check")) == 1
    state["pending"].pop("check").fulfill(json=RELEASE)


@pytest.mark.parametrize("phase,downloaded", [("downloading", False), ("ready", True), ("applying", True), ("idle", True)])
def test_backend_phase_restores_controls_after_reload(updater_page, phase, downloaded):
    page, state = updater_page
    state["status"].update(updater_phase=phase, downloaded=downloaded)
    boot(page)
    if phase == "downloading":
        assert page.get_by_role("button", name="Download update", exact=True).is_disabled()
    else:
        restart = page.get_by_role("button", name="Restart to apply", exact=True)
        assert restart.is_visible()
        assert restart.is_disabled() == (phase == "applying")
    assert not [call for call in state["calls"] if call[0] == "POST"]


def test_check_now_without_available_release_and_failed_check_preserves_result(updater_page):
    page, state = updater_page
    state["status"] = {"checked_at": None, "update_available": False}
    boot(page)
    assert page.locator("#updates").is_visible()
    assert page.get_by_role("dialog", name="Updates", exact=True).is_visible()
    assert not page.get_by_role("button", name="Download update", exact=True).is_visible()
    state["status"] = deepcopy(RELEASE)
    page.evaluate("updates.checkNow()")
    assert page.get_by_role("button", name="Download update", exact=True).is_enabled()
    state["status"] = {"checked_at": 2, "error": "ConnectError", "update_available": False}
    page.evaluate("updates.checkNow()")
    assert "Check again to retry" in page.get_by_role("status").inner_text()
    assert page.get_by_role("button", name="Download update", exact=True).is_enabled()
    state["fail"]["check"] = "Fixture unavailable"
    page.evaluate("updates.checkNow()")
    assert "Fixture unavailable" in page.get_by_role("status").inner_text()
    assert page.get_by_role("button", name="Check now", exact=True).is_enabled()


def test_init_is_idempotent_and_polling_is_hidden_aware(updater_page):
    page, state = updater_page
    boot(page)
    page.evaluate("updates.init(document.querySelector('#updates')); updates.init(document.querySelector('#updates'))")
    page.locator("#updates").click()
    assert not page.get_by_role("dialog").count()  # Exactly one toggle listener.
    before = state["calls"].count(("GET", "status"))
    page.clock.run_for(60_000)
    page.wait_for_function("true")
    assert state["calls"].count(("GET", "status")) == before + 1
    page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, value:true})")
    page.evaluate("updates.poll()")
    assert state["calls"].count(("GET", "status")) == before + 1
    page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, value:false})")
    state["status"].update(updater_phase="downloading")
    page.evaluate("updates.poll()")
    page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, value:true})")
    page.evaluate("updates.poll()")
    assert state["calls"].count(("GET", "status")) == before + 3


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://github.com/example", "https://github.com.evil.test/", "https://user:password@github.com/example", "https://example.test/"])
def test_release_links_reject_unsafe_urls_and_linux_never_downloads(updater_page, url):
    page, state = updater_page
    state["os"] = "linux"
    state["status"]["release_url"] = url
    boot(page)
    assert "autozcoder update" in page.locator(".field__help").inner_text()
    assert not page.get_by_role("button", name="Download update", exact=True).is_visible()
    assert not page.get_by_role("button", name="Restart to apply", exact=True).is_visible()
    assert not page.get_by_role("link", name="Release notes", exact=True).is_visible()
    assert page.locator(".update-card a").get_attribute("href") is None
    assert not [call for call in state["calls"] if call[1] in ("download", "apply")]
