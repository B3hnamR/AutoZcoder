"""Offline JS quota contract tests; no gateway or credential-backed requests."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
VIEWS = ROOT / "zcode_gateway/dashboard/assets/js/views"
HELPER = VIEWS / "model-quota.js"
NODE = shutil.which("node")


def aggregate(accounts, now=1000):
    if not NODE:
        pytest.skip("Node is required to exercise the shipped JS helper")
    script = """
import {readFileSync} from 'node:fs';
const source = readFileSync(process.argv[1], 'utf8');
const {aggregateModelQuotas} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const {accounts, now} = JSON.parse(readFileSync(0, 'utf8'));
const before = JSON.stringify(accounts);
const result = aggregateModelQuotas(accounts, now);
if (before !== JSON.stringify(accounts)) throw new Error('helper mutated input');
console.log(JSON.stringify(result));
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script, str(HELPER)],
        input=json.dumps({"accounts": accounts, "now": now}), text=True,
        capture_output=True, timeout=15, check=True,
    )
    return json.loads(result.stdout)


def account(remaining=100, **fields):
    return {"id": "account", "state": "ready", "has_plan": True,
            "balance_checked_at": 900,
            "quotas": {"GLM-5.3": {"remaining": remaining, "starts_at": 800,
                                    "resets_at": 2000}}, **fields}


def model(result, name="GLM-5.3"):
    return next(item for item in result["models"] if item["model"] == name)


def test_total_keeps_positive_capped_dead_cooling_and_no_plan_quotas():
    result = aggregate([
        account(100), account(200, state="dead"), account(300, state="cooling"),
        account(400, has_plan=False), account(500, model_caps={"glm-5.3": {"release_in_s": 20}}),
        account(600, state="exhausted"),
        account(700, plans=[{"status": "active", "ends_at": 999}]),
    ])
    quota = model(result)
    assert quota["remaining"] == 2800
    assert quota["routable_remaining"] == 100
    assert quota["eligible"] == 1
    assert quota["known"] == 7
    assert quota["capped"] == 1


@pytest.mark.parametrize("bucket,known,pending,expired", [
    ({"remaining": 50, "starts_at": 1000, "resets_at": 1001}, 1, 0, 0),
    ({"remaining": 50, "starts_at": 1001, "resets_at": 2000}, 0, 1, 0),
    ({"remaining": 50, "starts_at": 800, "resets_at": 1000}, 0, 0, 1),
    ({"remaining": 50, "resets_at": 999}, 0, 0, 1),
    ({"remaining": 50}, 1, 0, 0),
    ({"remaining": 50, "starts_at": 0, "resets_at": 0}, 1, 0, 0),
    ({"remaining": 50, "resets_at": "invalid"}, 0, 0, 0),
])
def test_active_window_boundaries(bucket, known, pending, expired):
    quota = model(aggregate([account(quotas={"GLM-5.3": bucket})]))
    assert quota["known"] == known
    assert quota["remaining"] == (50 if known else 0)
    assert quota["pending"] == pending
    assert quota["expired"] == expired
    assert quota["stale"] == expired


@pytest.mark.parametrize("remaining,known", [
    (0, 1), ("25", 1), (None, 0), ("", 0), (True, 0), (-1, 0), ("NaN", 0),
])
def test_unknown_is_not_zero_or_coerced_null(remaining, known):
    quota = model(aggregate([account(remaining)]))
    assert quota["known"] == known
    assert quota["accounts"][0]["remaining"] == (float(remaining) if known else None)


def test_checked_empty_unchecked_and_failed_checks_are_distinct():
    result = aggregate([
        account(quotas={}),
        account(balance_checked_at=0, quotas={}),
        account(50, quota_check_error="Timeout"),
        account(balance_checked_at=0, quotas={}, quota_check_error="HTTP 401"),
    ])
    quota = model(result)
    assert (result["checked"], result["unchecked"], result["errors"], result["stale"]) == (2, 2, 2, 2)
    assert (quota["known"], quota["unknown"], quota["remaining"]) == (1, 3, 50)
    assert quota["routable_remaining"] == 50  # retained active snapshot, explicitly stale
    assert quota["accounts"][0]["checked"]
    assert not quota["accounts"][1]["checked"]


def test_canonical_case_names_and_namespaces_do_not_alias():
    result = aggregate([account(quotas={
        "GLM-5.3": None, "glm-5.3": {"remaining": 10},
        "gLm-5.3-fLaSh": {"remaining": 20},
        "zai/glm-5.3": {"remaining": 30},
    })])
    assert len(result["models"]) == 3
    assert model(result)["remaining"] == 10
    assert model(result, "GLM-5.3-Flash")["remaining"] == 20
    assert model(result, "zai/glm-5.3")["remaining"] == 30


def test_caps_normalize_case_and_expired_caps_do_not_block():
    result = aggregate([
        account(10, model_caps={"glm-5.3": {"release_at": 999}}, capped_models=["GLM-5.3"]),
        account(20, model_caps={"GLM-5.3": 1001}),
        account(30, capped_models=["gLm-5.3"]),
        account(40, model_caps={"GLM-5.3": {"release_in_s": 0}}),
    ])
    quota = model(result)
    assert quota["remaining"] == 100
    assert quota["routable_remaining"] == 50
    assert quota["capped"] == 2


def test_empty_pool_has_named_unknown_models_not_fake_eligibility():
    result = aggregate([])
    assert result["total_accounts"] == 0
    assert [item["model"] for item in result["models"]] == ["GLM-5.3", "GLM-5.3-Flash"]
    assert all(item["known"] == item["eligible"] == 0 for item in result["models"])


def test_ui_retains_catalog_sync_and_uses_public_accounts_read():
    source = (VIEWS / "models.js").read_text(encoding="utf-8")
    assert 'api.post("/admin/models/catalog/refresh"' in source
    assert 'api.get("/admin/accounts", { signal })' in source
    assert "aggregateModelQuotas" in source
    assert "Unknown buckets are not zero" in source


def test_models_browser_mocked_api_desktop_mobile_and_actions(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    import time
    from urllib.parse import urlsplit

    now = time.time()
    accounts = [account(1000 + index, id="fixture-" + str(index),
                        email="fixture-" + str(index) + "@example.invalid") for index in range(52)]
    for item in accounts:
        item["quotas"]["GLM-5.3"].update(starts_at=now - 100, resets_at=now + 3600)
        item["balance_checked_at"] = now - 60
    accounts[-1].update(state="dead", quota_check_error="Fixture timeout")
    models = [{"model": name, "available_on": 51, "total_accounts": 52, "overall": "partial",
               "accounts": [{"account_id": item["id"], "status": "available" if index < 51 else "unavailable",
                             "reason": "Fixture eligible" if index < 51 else "Fixture dead"}
                            for index, item in enumerate(accounts)]}
              for name in ("GLM-5.3", "GLM-5.3-Flash")]
    posts = []
    unexpected = []
    errors = []
    assets = VIEWS.parents[1]

    def handle(route):
        request = route.request
        path = urlsplit(request.url).path
        if request.method == "POST":
            posts.append(path)
            route.fulfill(json={"error": None})
        elif path == "/admin/models/availability":
            route.fulfill(json={"models": models, "generated_at": now,
                                "catalog": {"source": "snapshot", "error": "Fixture offline"}})
        elif path == "/admin/accounts":
            route.fulfill(json={"accounts": accounts})
        elif path.startswith("/assets/"):
            target = assets / path.removeprefix("/assets/")
            route.fulfill(body=target.read_text(encoding="utf-8"),
                          content_type="text/css" if path.endswith(".css") else "text/javascript")
        elif path == "/":
            route.fulfill(body='''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/assets/css/tokens.css"><link rel="stylesheet" href="/assets/css/base.css">
<link rel="stylesheet" href="/assets/css/components.css"><main id="root" style="padding:16px"></main>
<script type="module">import {mount} from '/assets/js/views/models.js';
window.view = await mount({root:document.querySelector('#root'),signal:new AbortController().signal});</script>''',
                          content_type="text/html")
        else:
            unexpected.append(request.url)
            route.abort()

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch()
        except playwright.Error as exc:
            pytest.skip("Installed Chromium unavailable: " + str(exc))
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", handle)
        page.goto("http://models.test/")
        page.wait_for_function("window.view !== undefined")
        assert page.locator(".model-entry").count() == 2
        assert page.locator(".model-entry").first.inner_text().find("51 / 52") >= 0
        assert "Unknown" in page.locator(".model-entry").nth(1).inner_text()
        assert "53,326" in page.locator(".model-entry").first.inner_text()
        assert "52,275" in page.locator(".model-entry").first.inner_text()
        page.screenshot(path=str(tmp_path / "models-desktop.png"), full_page=True)
        search = page.get_by_role("searchbox")
        search.fill("flash")
        assert page.locator(".model-entry:visible").count() == 1
        search.fill("fixture-51")
        assert page.locator(".model-entry:visible").count() == 2
        search.fill("does-not-match")
        assert page.locator("[data-nomatch]").is_visible()
        search.fill("")
        page.locator("summary").first.focus()
        page.keyboard.press("Enter")
        assert page.locator("details").first.get_attribute("open") is not None
        assert page.get_by_text("Fixture timeout", exact=True).first.is_visible()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator(".model-account-grid").first.evaluate("node => node.getBoundingClientRect().width >= 760")
        assert page.locator(".model-account-table").first.evaluate("node => node.getBoundingClientRect().height <= 480")
        page.screenshot(path=str(tmp_path / "models-mobile.png"), full_page=True)
        page.get_by_role("button", name="Sync models", exact=True).click()
        page.wait_for_function("document.querySelector('.model-action-status').textContent === 'Model catalogue synced.'")
        page.get_by_role("button", name="Refresh quotas", exact=True).click()
        page.wait_for_function("document.querySelector('.model-action-status').textContent === 'All account quotas refreshed.'")
        assert posts.count("/admin/models/catalog/refresh") == 1
        assert len([path for path in posts if path.endswith("/balance")]) == 52
        assert not errors
        assert not unexpected
        browser.close()
