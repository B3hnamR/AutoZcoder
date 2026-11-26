"""Auto Generate browser checks with every request intercepted; no live API."""
from pathlib import Path
from urllib.parse import urlsplit

import pytest


ASSETS = Path(__file__).resolve().parents[1] / "zcode_gateway/dashboard/assets"
CONFIG = {"mode": "goal", "target": 20, "attempt_limit": 100, "concurrency": 1,
          "cooldown_seconds": 60, "ip_source": "direct", "ip_sources": [], "ip_mode": "spread",
          "ip_pick": "", "captcha": "ai", "timeout_seconds": 900, "failure_limit": 5}
EXITS = [{"key": "psiphon:DE", "label": "Psiphon Germany (DE)"},
         {"key": "surfshark:nl-ams", "label": "Surfshark Netherlands (Amsterdam)"}]


def test_auto_generate_mocked_browser(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    config = dict(CONFIG)
    status = {"state": "idle", "qualified": 0, "no_plan": 0, "slots": []}
    workers = []
    calls, errors, unexpected = [], [], []
    fail = {"get": True, "save": False}

    def handle(route):
        request = route.request
        path = urlsplit(request.url).path
        if path.startswith("/admin/dash/generator/auto"):
            calls.append((request.method, path, request.post_data_json if request.method != "GET" else None))
            if request.method == "GET" and fail["get"]:
                route.fulfill(status=503, json={"detail": "Fixture offline"})
                return
            if request.method == "PUT":
                if fail["save"]:
                    route.fulfill(json={"error": "Fixture save rejected"})
                    return
                config.update(request.post_data_json)
            if request.method == "POST":
                action = path.rsplit("/", 1)[1]
                status["state"] = {"start": "running", "pause": "paused", "resume": "running", "stop": "stopped"}[action]
                if action == "start":
                    status.update(id="fixture-session", config=dict(config))
            route.fulfill(json={"config": config, "status": status, "workers": workers,
                                "ip_exits": EXITS, "pool_available": 0, "pin_set": False})
        elif path.startswith("/admin/dash/generator/workers/"):
            calls.append((request.method, path, request.post_data_json if request.method != "GET" else None))
            route.fulfill(json={"log": "full log", "password": "pw", "removed": "x", "cleared": 1})
        elif path == "/admin/dash/generator/stop":
            calls.append((request.method, path, request.post_data_json))
            workers[0]["state"] = "killed"
            route.fulfill(json={"stopped": workers[0]["name"]})
        elif path.startswith("/assets/"):
            target = ASSETS / path.removeprefix("/assets/")
            route.fulfill(body=target.read_text(encoding="utf-8"),
                          content_type="text/css" if path.endswith(".css") else "text/javascript")
        elif path == "/":
            route.fulfill(body='''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/assets/css/tokens.css"><link rel="stylesheet" href="/assets/css/base.css">
<link rel="stylesheet" href="/assets/css/components.css"><main id="root" style="padding:16px"></main>
<script type="module">import {createAutoGenerate} from '/assets/js/views/auto-generate.js';
window.controller = new AbortController();
window.view = createAutoGenerate({signal:controller.signal}); document.querySelector('#root').append(view.node);</script>''',
                          content_type="text/html")
        else:
            unexpected.append(request.url)
            route.abort()

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch()
        except playwright.Error as exc:
            pytest.skip("Installed Chromium unavailable: " + str(exc))
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", handle)
        page.goto("http://auto.test/")
        save = page.get_by_role("button", name="Save config", exact=True)
        start = page.get_by_role("button", name="Auto Generate", exact=True)
        page.get_by_text("Configuration not loaded. Refresh status to retry.", exact=True).wait_for()
        assert save.is_disabled() and start.is_disabled()
        fail["get"] = False
        page.get_by_role("button", name="Refresh status", exact=True).click()
        page.wait_for_function("document.querySelector('.auto-note').textContent.includes('Review the saved profile')")
        assert start.is_disabled()
        mode = page.get_by_label("Run mode", exact=True)
        if page.evaluate("CSS.supports('appearance: base-select') && CSS.supports('selector(::picker(select))')"):
            assert mode.evaluate("node => getComputedStyle(node).appearance") == "base-select"
            mode.click()
            page.wait_for_function("document.querySelector('.auto-generate select').matches(':open')")
            assert mode.evaluate("node => getComputedStyle(node, '::picker(select)').backgroundColor") != "rgba(0, 0, 0, 0)"
            page.keyboard.press("Escape")
            assert mode.input_value() == "goal"
        page.get_by_text("Browser and retry settings", exact=True).click()
        assert page.get_by_label("Captcha solver", exact=True).input_value() == "ai"
        page.get_by_label("Captcha solver", exact=True).select_option("mint")
        display = page.get_by_label("Browser display", exact=True)
        assert display.input_value() == "background"  # Older saved profiles have no display field.
        assert display.locator("option").all_text_contents() == ["Background (invisible)", "Desktop window"]
        assert "native headless Chromium" in page.get_by_text("Windows background uses", exact=False).inner_text()
        goal = page.get_by_label("Qualified success goal", exact=True)
        goal.fill("25")
        goal.focus()
        config["target"] = 99  # Poll must not overwrite in-progress edits or steal focus.
        page.evaluate("view.refresh()")
        assert goal.input_value() == "25"
        assert goal.evaluate("node => node === document.activeElement")
        page.get_by_label("Maximum attempts", exact=True).fill("24")
        save.click()
        assert not [call for call in calls if call[0] == "PUT"]
        page.get_by_label("Maximum attempts", exact=True).fill("100")
        save.click()
        page.wait_for_function("!document.querySelector('.auto-actions .btn--primary').disabled")
        assert calls[-1][0] == "PUT"
        assert calls[-1][2]["target"] == 25
        assert calls[-1][2]["browser_display"] == "background"
        assert not [call for call in calls if call[0] == "POST"]
        config.pop("browser_display")
        page.evaluate("view.refresh()")
        assert start.is_enabled()  # Omitted legacy field still means background.
        config["cooldown_seconds"] = 30
        page.evaluate("view.refresh()")
        assert start.is_disabled()  # A different dashboard's save is not silently used.
        assert page.get_by_label("Cooldown (seconds)", exact=True).input_value() == "60"
        page.get_by_label("Cooldown (seconds)", exact=True).fill("10")
        assert start.is_disabled()
        fail["save"] = True
        save.click()
        page.get_by_role("alert").wait_for()
        assert "Fixture save rejected" in page.get_by_role("alert").inner_text()
        assert start.is_disabled()
        fail["save"] = False
        save.click()
        page.wait_for_function("!document.querySelector('.auto-actions .btn--primary').disabled")
        start.click()
        pause = page.get_by_role("button", name="Pause", exact=True)
        pause.wait_for()
        assert goal.is_disabled()
        assert display.is_disabled()
        assert status["config"]["browser_display"] == "background"
        assert "Background (invisible)" in page.locator(".auto-session > .auto-note").first.inner_text()
        assert "0 / 25 new qualified" in page.locator(".auto-progress").inner_text()
        config["target"] = 200
        config["browser_display"] = "desktop"
        status.update(qualified=3, no_plan=2, reason="<img src=x onerror=alert(1)>",
                      events=[{"id":1,"ts":1793457000,"level":"warn","worker":"auto-fixture",
                               "message":"Attempt 1 finished: launch failed. <img src=x onerror=alert(1)>"}],
                      active=1, cooling=1, remaining_cooldown_seconds=4,
                      slots=[{"id": 1, "state": "cooling", "remaining_cooldown_seconds": 4}])
        page.evaluate("view.refresh()")
        assert "3 / 25" in page.locator(".auto-progress").inner_text()
        assert "Background (invisible)" in page.locator(".auto-session > .auto-note").first.inner_text()
        assert display.input_value() == "background"
        assert page.locator(".auto-generate img").count() == 0
        assert page.locator(".auto-count").filter(has_text="No plan").inner_text() == "No plan\n2"
        assert 'launch failed' in page.get_by_role('region',name='Auto Generate live session log').inner_text()
        assert page.get_by_role('checkbox',name='Follow latest').is_checked()
        page.get_by_role('checkbox',name='Follow latest').uncheck()
        assert page.get_by_role('button',name='Copy log',exact=True).is_enabled()
        pause.click()
        resume = page.get_by_role("button", name="Resume", exact=True)
        resume.wait_for()
        resume.click()
        pause.wait_for()
        status["state"] = "interrupted"
        before = len([call for call in calls if call[0] == "POST"])
        page.evaluate("view.refresh()")
        resume.wait_for()
        assert len([call for call in calls if call[0] == "POST"]) == before
        assert "Session interrupted" in page.locator(".auto-session").inner_text()
        page.screenshot(path=str(tmp_path / "auto-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert save.evaluate("node => node.getBoundingClientRect().height >= 44")
        page.screenshot(path=str(tmp_path / "auto-mobile.png"), full_page=True)
        fail["get"] = True
        page.evaluate("view.refresh()")
        page.get_by_text("Auto Generate status unavailable:", exact=False).wait_for()
        assert resume.is_disabled()
        fail["get"] = False
        page.get_by_role("button", name="Refresh status", exact=True).click()
        page.wait_for_function("!Array.from(document.querySelectorAll('button')).find(n => n.textContent === 'Resume').disabled")
        page.get_by_role("button", name="Stop session", exact=True).click()
        page.wait_for_function("!document.querySelector('[name=mode]').disabled")
        page.get_by_label("Run mode", exact=True).select_option("attempts")
        source = page.get_by_role("checkbox", name="Proxy pool (0)", exact=True)
        modeRow = page.locator(".ip-field .ip-line").nth(1)
        assert modeRow.is_visible() is False
        source.check()
        display.select_option("desktop")
        assert not goal.is_visible()
        page.get_by_role("checkbox", name="Psiphon", exact=True).check()
        page.get_by_role("checkbox", name="Surfshark", exact=True).check()
        page.get_by_label("Concurrent browsers", exact=True).fill("4")
        assert modeRow.is_visible()
        page.get_by_role("radio", name="Stay on one").check()
        pick = page.get_by_label("Exit in use", exact=True)
        assert pick.locator("option").all_text_contents() == [
            "Psiphon Germany (DE)", "Surfshark Netherlands (Amsterdam)"]
        pick.select_option("surfshark:nl-ams")
        # Unchecking the only remaining source must keep it selected.
        for name in ("Proxy pool (0)", "Psiphon", "My network"):
            page.get_by_role("checkbox", name=name, exact=True).click()
        last = page.get_by_role("checkbox", name="Surfshark", exact=True)
        last.click()
        assert last.is_checked()
        for name in ("Proxy pool (0)", "Psiphon"):
            page.get_by_role("checkbox", name=name, exact=True).check()
        save.click()
        page.wait_for_function("!document.querySelector('.auto-actions .btn--primary').disabled")
        assert calls[-1][0] == "PUT"
        assert calls[-1][2]["browser_display"] == "desktop"
        assert sorted(calls[-1][2]["ip_sources"]) == ["pool", "psiphon", "surfshark"]
        assert calls[-1][2]["ip_mode"] == "single"
        assert calls[-1][2]["ip_pick"] == "surfshark:nl-ams"
        assert calls[-1][2]["captcha"] == "mint"
        assert calls[-1][2]["concurrency"] == 4
        assert page.get_by_label("Concurrent browsers", exact=True).input_value() == "4"
        start.click()
        pause.wait_for()
        assert "attempts launched" in page.locator(".auto-progress").inner_text()
        assert sorted(status["config"]["ip_sources"]) == ["pool", "psiphon", "surfshark"]
        assert status["config"]["browser_display"] == "desktop"
        assert "Desktop window" in page.locator(".auto-session > .auto-note").first.inner_text()
        assert "stay on one" in page.locator(".auto-session > .auto-note").first.inner_text()
        assert "Mint token" in page.locator(".auto-session > .auto-note").first.inner_text()
        assert status["config"]["concurrency"] == 4

        workers.append({"name": "auto-abc123", "state": "running", "captcha": "mint",
                        "route_label": "Psiphon · Germany (DE)", "created": 1, "has_log": False,
                        "has_password": False, "starter": True, "start_plan": False,
                        "email": "a@b.test", "lines": ["Captcha solved"]})
        page.evaluate("view.refresh()")
        card = page.locator(".auto-workers-list .job")
        card.wait_for()
        assert "auto-abc123" in card.inner_text() and "Captcha solved" in card.inner_text()
        assert "Psiphon · Germany (DE)" in card.inner_text()
        assert card.get_by_text("Starter", exact=True).is_visible()
        assert "a@b.test" in card.inner_text()
        workers[0].update(state="exited", has_log=True, has_password=True, starter=False)
        page.evaluate("view.refresh()")
        assert card.get_by_text("No plan", exact=True).is_visible()
        card.get_by_role("button", name="Copy log", exact=True).click()
        assert any(path.endswith("/workers/details/log") for _, path, _ in calls)
        assert card.get_by_role("button", name="Kill", exact=True).count() == 0
        workers.append({"name": "auto-def456", "state": "exited", "created": 2, "has_log": False,
                        "has_password": False, "lines": []})
        page.evaluate("view.refresh()")
        page.locator(".auto-workers-list .job").nth(1).locator('button[title="Remove from list"]').click()
        assert any(path.endswith("/workers/remove") for _, path, _ in calls)
        assert not page.get_by_text("No session events yet.", exact=True).is_visible()
        before = len(calls)
        page.evaluate("controller.abort(); view.refresh(); view.destroy()")
        assert len(calls) == before
        assert not errors
        assert not unexpected
        browser.close()


@pytest.mark.parametrize("saved_display", ["auto", "headless"])
def test_windows_manual_display_is_separate(saved_display):
    playwright = pytest.importorskip("playwright.sync_api")
    calls, errors, unexpected = [], [], []

    def handle(route):
        request = route.request
        path = urlsplit(request.url).path
        if path == "/admin/dash/capabilities":
            route.fulfill(json={"os": "windows", "features": {"visible_browser": True, "manual_captcha": True}})
        elif path == "/admin/dash/generator/status":
            route.fulfill(json={"options": {"captcha": "ai", "browser_display": saved_display},
                                "workers": [], "claims": []})
        elif path == "/admin/dash/generator/auto":
            if request.method != "GET":
                unexpected.append(request.url)
            route.fulfill(json={"config": CONFIG, "status": {"state": "idle"}})
        elif path == "/admin/dash/generator/options" and request.method == "POST":
            calls.append(request.post_data_json)
            route.fulfill(json={"ok": True})
        elif path in {"/admin/dash/generator/mail", "/admin/claims/claim_all_status"}:
            route.fulfill(json={})
        elif path.startswith("/assets/"):
            target = ASSETS / path.removeprefix("/assets/")
            route.fulfill(body=target.read_text(encoding="utf-8"),
                          content_type="text/css" if path.endswith(".css") else "text/javascript")
        elif path == "/":
            route.fulfill(body='''<!doctype html><main id="root"></main>
<script type="module">import {load} from '/assets/js/core/caps.js';
import {mount} from '/assets/js/views/generator.js';
await load(); window.controller = new AbortController();
window.view = await mount({root:document.querySelector('#root'),signal:controller.signal});</script>''',
                          content_type="text/html")
        else:
            unexpected.append(request.url)
            route.abort()

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch()
        except playwright.Error as exc:
            pytest.skip("Installed Chromium unavailable: " + str(exc))
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", handle)
        page.goto("http://auto.test/")
        display = page.locator("#generator-display")
        display.wait_for()
        assert display.is_enabled()
        assert display.input_value() == saved_display
        assert display.locator("option").all_text_contents() == ["Desktop window", "Native headless (background)"]
        assert page.locator(".auto-generate").count() == 0
        display.select_option("headless")
        page.wait_for_function("document.querySelector('.text-ok.text-xs').hidden === false")
        assert calls[-1]["browser_display"] == "headless"
        captcha = page.get_by_text("Captcha", exact=True).locator("..").locator("select")
        with page.expect_response(lambda response: urlsplit(response.url).path == "/admin/dash/generator/options"):
            captcha.select_option("manual")
        assert display.is_disabled()
        assert display.input_value() == "auto"
        assert calls[-1]["browser_display"] == "auto"
        assert calls[-1]["captcha"] == "manual"
        captcha.select_option("ai")
        assert display.is_enabled()
        assert not errors
        assert not unexpected
        page.evaluate("controller.abort(); view.destroy()")
        browser.close()
