"""Only local HTML fixtures: never submit provider credentials or signup."""
import ast
import logging
from pathlib import Path
import re

import pytest


@pytest.fixture
def enter_form():
    path = Path(__file__).resolve().parents[1] / 'generator/main.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_enter_signup_form')
    ns = {'re': re, 'logger': logging.getLogger('fixture')}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), ns)
    return ns['_enter_signup_form']


@pytest.mark.parametrize('tag,ignored', [('button', 0), ('a', 0), ('button', 1)])
def test_waits_for_actual_signup_mode_and_retries_only_toggle(enter_form, tag, ignored):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except playwright.Error:
            pytest.skip('Local Chromium unavailable')
        page = browser.new_page()
        page.route('**/*', lambda _: pytest.fail('Local form attempted a network request'))
        page.set_content(f'''<input type="email"><input type="password">
          <button id="submit">Sign in</button><{tag} id="toggle">Sign up</{tag}>
          <script>window.clicks=0;window.submits=0;
          document.querySelector('#submit').onclick=()=>window.submits++;
          document.querySelector('#toggle').onclick=()=>{{
            window.clicks++;
            if(window.clicks<={ignored})return;
            setTimeout(()=>{{document.querySelector('#toggle').remove();
              document.body.insertAdjacentHTML('beforeend','<input name="name" placeholder="Enter Your Full Name">');}},30);
          }};</script>''')
        field = enter_form(page, timeout=300)
        field.fill('Local test')
        assert field.input_value() == 'Local test'
        assert page.evaluate('window.clicks') == ignored + 1
        assert page.evaluate('window.submits') == 0
        browser.close()


def test_existing_form_does_not_click_or_toggle_back(enter_form):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except playwright.Error:
            pytest.skip('Local Chromium unavailable')
        page=browser.new_page()
        page.set_content('<input name="name"><button onclick="throw Error()">Sign up</button>')
        enter_form(page, timeout=300).fill('Ready')
        assert page.locator('input').input_value()=='Ready'
        browser.close()


def test_stuck_login_reports_transition_not_missing_generic_text(enter_form):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except playwright.Error:
            pytest.skip('Local Chromium unavailable')
        page=browser.new_page()
        page.set_content('<input type="email"><input type="password"><button>Sign up</button>')
        with pytest.raises(RuntimeError, match='Signup form did not appear'):
            enter_form(page, timeout=200)
        browser.close()
