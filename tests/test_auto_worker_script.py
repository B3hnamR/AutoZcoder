"""Run worker control flow from its AST with no browser or network imports."""
import ast
from contextlib import contextmanager
import io
import json
import logging
import os
import re
import sys
import threading
import types
import traceback
import urllib.parse
from pathlib import Path
from unittest.mock import Mock

import pytest


SOURCE = Path(__file__).resolve().parents[1] / 'generator' / 'main.py'
PREFIX = 'AUTO_GENERATE_RESULT '


@pytest.fixture
def worker(monkeypatch, tmp_path):
    for key in tuple(os.environ):
        if key.startswith('ZAI_'):
            monkeypatch.delenv(key)
    monkeypatch.setenv('ZAI_AUTO_GENERATE', '1')
    monkeypatch.setenv('ZAI_ROUTE_POLICY', 'direct')
    monkeypatch.setenv('ZAI_PROXY_RETRIES', '0')
    monkeypatch.setenv('ZAI_WORKER_ID', 'run-unique-worker-1')
    functions = {
        'env_on', 'env_flag', '_auto_result', '_publish_auto_result',
        '_save_auto_route_metadata', '_account_profile', '_main', 'main', 'set_proxy', 'pw_proxy',
        '_redact_proxy',
    }
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    module = ast.Module(
        body=[node for node in tree.body
              if isinstance(node, ast.FunctionDef) and node.name in functions],
        type_ignores=[],
    )
    mail = Mock(email='owned@example.test', password='PRIVATE_PASSWORD')
    mail.create_account.return_value = True
    mail.get_token.return_value = True
    plan = types.ModuleType('plan_check')
    plan.check_account_plan = Mock(return_value={'starter': False, 'plans': []})
    claim = types.ModuleType('claim_start')
    claim.claim_start = Mock(return_value={'claimed': True, 'plan_id': 'start-plan'})
    fp = types.ModuleType('fingerprint')
    fp.exit_ip_geo = Mock(return_value=('', ''))
    monkeypatch.setitem(sys.modules, 'plan_check', plan)
    monkeypatch.setitem(sys.modules, 'claim_start', claim)
    monkeypatch.setitem(sys.modules, 'fingerprint', fp)
    ns = {
        'os': os, 're': re, 'json': json,
        'sys': types.SimpleNamespace(argv=['main.py', '999']),
        'time': types.SimpleNamespace(time=lambda: 1234, sleep=Mock()),
        'CONFIG': {'count': 20, 'proxy_retries': 9},
        'CURRENT_PROXY': None, 'OUT_SUFFIX': '-owned',
        'OUTPUT_DIR': str(tmp_path), 'RESULTS_DIR': str(tmp_path),
        'PROFILES_DIR': str(tmp_path / 'profiles'),
        'ACC_FILE': str(tmp_path / 'accounts.txt'),
        'PLANS_FILE': str(tmp_path / 'plans.jsonl'),
        'STARTER_FILE': str(tmp_path / 'starter.txt'),
        '_log_file': str(tmp_path / 'worker.log'),
        'logger': Mock(spec=logging.Logger),
        'timing': types.SimpleNamespace(summary=lambda: 'mock timing'),
        'kill_stale_browsers': Mock(),
        'CatchAllMail': Mock(return_value=mail),
        'signup': Mock(return_value='PRIVATE_TOKEN'),
        'pick_verified_proxy': Mock(return_value='socks5://proxy.test:1080'),
        '_pool_has_live_proxy': Mock(return_value=True),
        '_secret_filter': Mock(),
        'generate_human_name': Mock(return_value='Fixture User'),
        '_enter_signup_form': Mock(),
    }
    exec(compile(module, str(SOURCE), 'exec'), ns)
    return types.SimpleNamespace(ns=ns, mail=mail, plan=plan, claim=claim)


def run(worker, capsys):
    summary = worker.ns['main']()
    lines = [line for line in capsys.readouterr().out.splitlines()
             if line.startswith(PREFIX)]
    assert len(lines) == 1
    result = json.loads(lines[0][len(PREFIX):])
    assert result == summary
    assert set(result) <= {'category', 'message', 'email'}
    assert 'PRIVATE' not in lines[0]
    worker.ns['kill_stale_browsers'].assert_not_called()
    return result


@pytest.mark.parametrize('signup_result,category', [
    (None, 'failed'),
    ('CAPTCHA_NOT_LOADED', 'captcha_failed'),
    ('CAPTCHA_FAILED', 'captcha_failed'),
    ('BROWSER_FAILED', 'browser_failed'),
    ('MAIL_TIMEOUT', 'mail_timeout'),
    ('SIGNUP_FINISH_FAILED', 'finish_signup_failed'),
    ('SIGNUP_UNVERIFIED', 'mail_error'),
    ('SIGNUP_IP_LIMITED', 'rate_limited'),
    ({'needs_verification': True}, 'mail_error'),
    ('PRIVATE_TOKEN', 'generated'),
])
def test_auto_forces_one_signup_and_one_email(worker, monkeypatch, capsys,
                                             signup_result, category):
    monkeypatch.setenv('ZAI_PROXY_RETRIES', '27')
    worker.ns['signup'].return_value = signup_result
    assert run(worker, capsys)['category'] == category
    worker.ns['signup'].assert_called_once()
    worker.ns['CatchAllMail'].assert_called_once()
    worker.ns['_pool_has_live_proxy'].assert_not_called()
    worker.ns['pick_verified_proxy'].assert_not_called()


def test_direct_overrides_inherited_proxy_and_pool(worker, monkeypatch, capsys):
    worker.ns['CURRENT_PROXY'] = 'socks5://inherited.test:1080'
    monkeypatch.setenv('ZAI_PROXY', worker.ns['CURRENT_PROXY'])
    monkeypatch.setenv('ZAI_USE_POOL', '1')
    assert run(worker, capsys)['category'] == 'generated'
    assert worker.ns['CURRENT_PROXY'] is None
    assert 'ZAI_PROXY' not in os.environ
    worker.ns['pick_verified_proxy'].assert_not_called()
    worker.claim.claim_start.assert_called_once_with(
        'PRIVATE_TOKEN', log=worker.ns['logger'].info, force_direct=True)


def test_proxy_picks_route_before_mail_and_uses_claim_route(worker, monkeypatch, capsys):
    monkeypatch.setenv('ZAI_ROUTE_POLICY', 'proxy')
    worker.ns['pick_verified_proxy'].side_effect = lambda: (
        worker.ns['CatchAllMail'].assert_not_called() or 'socks5://proxy.test:1080')
    observed = []
    worker.ns['signup'].side_effect = lambda *args: (
        observed.append(os.environ.get('ZAI_PROXY')) or 'PRIVATE_TOKEN')
    assert run(worker, capsys)['category'] == 'generated'
    assert observed == ['socks5://proxy.test:1080']
    assert os.environ['ZAI_PROXY'] == 'socks5://proxy.test:1080'
    worker.claim.claim_start.assert_called_once_with(
        'PRIVATE_TOKEN', log=worker.ns['logger'].info, force_direct=False)


def test_proxy_plancheck_failure_does_not_retry_direct(worker, monkeypatch, capsys):
    monkeypatch.setenv('ZAI_ROUTE_POLICY', 'proxy')
    worker.plan.check_account_plan.side_effect = RuntimeError('PRIVATE_TOKEN')
    assert run(worker, capsys)['category'] == 'generated'
    worker.plan.check_account_plan.assert_called_once()
    assert os.environ['ZAI_PROXY'] == 'socks5://proxy.test:1080'
    worker.claim.claim_start.assert_called_once()


def test_pool_empty_returns_before_allocating_email(worker, monkeypatch, capsys):
    monkeypatch.setenv('ZAI_ROUTE_POLICY', 'proxy')
    worker.ns['pick_verified_proxy'].return_value = None
    result = run(worker, capsys)
    assert result['category'] == 'pool_empty'
    assert 'email' not in result
    worker.ns['CatchAllMail'].assert_not_called()
    worker.ns['signup'].assert_not_called()


def test_invalid_proxy_cannot_silently_launch_direct(worker, monkeypatch, capsys):
    monkeypatch.setenv('ZAI_ROUTE_POLICY', 'proxy')
    worker.ns['CURRENT_PROXY'] = 'not-a-proxy'
    assert run(worker, capsys)['category'] == 'pool_empty'
    worker.ns['CatchAllMail'].assert_not_called()
    worker.ns['signup'].assert_not_called()


@pytest.mark.parametrize('stage,has_email', [('create_account', False), ('get_token', True)])
def test_mail_errors_are_structured(worker, capsys, stage, has_email):
    getattr(worker.mail, stage).return_value = False
    result = run(worker, capsys)
    assert result['category'] == 'mail_error'
    assert ('email' in result) == has_email
    worker.ns['signup'].assert_not_called()


@pytest.mark.parametrize('starter,claimed,category', [
    (False, False, 'no_plan'), (True, False, 'generated'),
    (False, True, 'generated'),
])
def test_summary_uses_collected_plan_evidence(worker, capsys, starter, claimed, category):
    worker.plan.check_account_plan.return_value = {'starter': starter, 'plans': []}
    worker.claim.claim_start.return_value = {'claimed': claimed}
    assert run(worker, capsys)['category'] == category


@pytest.mark.parametrize('where,has_email', [
    ('CatchAllMail', False), ('signup', True), ('timing', True),
])
def test_exception_always_emits_safe_failure(worker, capsys, where, has_email):
    error = RuntimeError('PRIVATE_PASSWORD PRIVATE_TOKEN')
    if where == 'timing':
        worker.ns['timing'].summary = Mock(side_effect=error)
    else:
        worker.ns[where].side_effect = error
    result = run(worker, capsys)
    assert result['category'] == 'failed'
    assert ('email' in result) == has_email


def test_invalid_policy_fails_closed(worker, monkeypatch, capsys):
    monkeypatch.setenv('ZAI_ROUTE_POLICY', 'unexpected')
    assert run(worker, capsys)['category'] == 'failed'
    worker.ns['signup'].assert_not_called()
    worker.ns['CatchAllMail'].assert_not_called()


def test_profile_is_worker_owned_and_manual_path_unchanged(worker, monkeypatch):
    profile = worker.ns['_account_profile']
    first = profile('same@example.test')
    assert 'run-unique-worker-1' in first
    monkeypatch.setenv('ZAI_WORKER_ID', 'run-unique-worker-2')
    assert first != profile('same@example.test')
    monkeypatch.setenv('ZAI_WORKER_ID', '../bad/path')
    sanitized = profile('same@example.test')
    assert Path(sanitized).parent == Path(worker.ns['PROFILES_DIR'])
    monkeypatch.setenv('ZAI_WORKER_ID', '.._bad_path')
    assert profile('same@example.test') != sanitized
    monkeypatch.delenv('ZAI_AUTO_GENERATE')
    assert profile('same@example.test') == os.path.join(
        worker.ns['PROFILES_DIR'], '.pw-profile-same')


def test_manual_keeps_cleanup_retries_count_and_no_result(worker, monkeypatch, capsys):
    monkeypatch.delenv('ZAI_AUTO_GENERATE')
    worker.ns['sys'].argv = ['main.py', '2']
    worker.ns['CONFIG']['proxy_retries'] = 1
    monkeypatch.setenv('ZAI_PROXY_RETRIES', '1')
    worker.ns['signup'].side_effect = [None, 'PRIVATE_TOKEN', 'PRIVATE_TOKEN']
    assert worker.ns['main']() is None
    assert PREFIX not in capsys.readouterr().out
    worker.ns['kill_stale_browsers'].assert_called_once()
    assert worker.ns['signup'].call_count == 3
    assert worker.ns['CatchAllMail'].call_count == 3
    assert all(not c.kwargs['force_direct'] for c in worker.claim.claim_start.call_args_list)


def test_manual_keeps_plancheck_direct_retry(worker, monkeypatch, capsys):
    monkeypatch.delenv('ZAI_AUTO_GENERATE')
    worker.ns['sys'].argv = ['main.py', '1']
    worker.ns['CURRENT_PROXY'] = 'socks5://chosen.test:1080'
    monkeypatch.setenv('ZAI_PROXY', worker.ns['CURRENT_PROXY'])
    routes = []

    def check(*args, **kwargs):
        routes.append(os.environ.get('ZAI_PROXY'))
        if len(routes) == 1:
            raise RuntimeError('mock transport failure')
        return {'starter': False}

    worker.plan.check_account_plan.side_effect = check
    worker.ns['main']()
    assert PREFIX not in capsys.readouterr().out
    assert routes == ['socks5://chosen.test:1080', None]
    assert os.environ['ZAI_PROXY'] == 'socks5://chosen.test:1080'


def test_finish_failure_never_enters_success_or_signup_retry(worker, monkeypatch, capsys):
    monkeypatch.delenv('ZAI_AUTO_GENERATE')
    monkeypatch.setenv('ZAI_PROXY_RETRIES', '5')
    worker.ns['sys'].argv = ['main.py', '1']
    worker.ns['signup'].return_value = 'SIGNUP_FINISH_FAILED'
    worker.ns['main']()
    assert PREFIX not in capsys.readouterr().out
    worker.ns['signup'].assert_called_once()
    worker.plan.check_account_plan.assert_not_called()
    worker.claim.claim_start.assert_not_called()
    assert not Path(worker.ns['ACC_FILE']).exists()


@pytest.fixture
def signup_flow(worker, monkeypatch):
    names = {'signup', '_save_recovery_account', 'jwt_payload', 'token_email', 'is_guest_token'}
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    module = ast.Module(body=[node for node in tree.body
                             if isinstance(node, ast.FunctionDef) and node.name in names],
                        type_ignores=[])
    page = Mock()
    page.locator.return_value.count.return_value = 1
    page.locator.return_value.first.is_visible.return_value = True
    context = Mock()
    context.new_page.return_value = page
    context.cookies.return_value = []
    browser = Mock()
    browser.chromium.launch_persistent_context.return_value = context
    playwright = Mock()
    playwright.__enter__ = Mock(return_value=browser)
    playwright.__exit__ = Mock(return_value=False)
    posts = []
    responses = {'verify_email': {'status': 200, 'body': '{"success":true}'},
                 'finish_signup': {'status': 500, 'body': '{"detail":"Internal error"}'}}

    def evaluate(script, arg=None):
        if arg:
            stage = arg['path'].rsplit('/', 1)[-1]
            posts.append(stage)
            return responses[stage]
        if 'localStorage' in script:
            return None
        return 'captcha-material'

    def on_response(event, callback):
        callback(types.SimpleNamespace(
            url='https://chat.z.ai/api/v1/auths/signup', status=200,
            request=types.SimpleNamespace(method='POST'),
            text=lambda: '{"success":true}', headers={}))

    page.evaluate.side_effect = evaluate
    page.on.side_effect = on_response
    ns = worker.ns
    ns.update({
        'Path': Path, 'shutil': types.SimpleNamespace(rmtree=Mock()),
        'sync_playwright': Mock(return_value=playwright),
        'fingerprint': types.SimpleNamespace(
            make_identity=Mock(return_value={
                'chrome_major': 140, 'viewport': {'width': 800, 'height': 600},
                'hardware_concurrency': 4, 'device_memory': 8, 'country': 'US',
                'timezone_id': 'UTC', 'locale': 'en-US', 'user_agent': 'fixture'}),
            base_script=Mock(return_value=''), noise_script=Mock(return_value='')),
        'SELECTORS': {name: name for name in (
            'continue_with_email', 'email_input', 'password_input', 'signup_button',
            'name_input', 'create_account_button', 'captcha_start', 'slider')},
        'BROWSER_ARGS': [], 'CAPTCHA_VERIFY_HOOK': '', 'AVATAR_DATA_URI': 'fixture',
        'CaptchaSolver': Mock(return_value=Mock(solve=Mock(return_value='SOLVED'))),
        'take_screenshot': Mock(), '_rm_profile': Mock(), 'notify_gateway': Mock(),
        '_enter_signup_form': Mock(return_value=page.locator.return_value),
        '_secret_filter': Mock(),
        'timing': types.SimpleNamespace(start=Mock(), end=Mock()),
    })
    worker.mail.get_verification_link.return_value = (
        'https://chat.z.ai/auth/verify_email?token=verify-abcdef')
    exec(compile(module, str(SOURCE), 'exec'), ns)
    return types.SimpleNamespace(ns=ns, worker=worker, page=page, context=context,
                                 responses=responses, posts=posts, browser=browser)


def test_finish_500_preserves_verified_credentials_without_extra_requests(signup_flow):
    flow = signup_flow
    assert flow.ns['signup']('owned@example.test', 'PRIVATE_PASSWORD', flow.worker.mail) == 'SIGNUP_FINISH_FAILED'
    assert flow.posts == ['verify_email', 'finish_signup']
    recovery = Path(flow.ns['OUTPUT_DIR']) / 'recovery_accounts.txt'
    assert recovery.read_text(encoding='utf-8') == (
        'owned@example.test:PRIVATE_PASSWORD:finish_failed|verified|http_status=500\n')
    assert not Path(flow.ns['ACC_FILE']).exists()
    if os.name != 'nt':
        assert recovery.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('status,body', [
    (200, '{"success":true}'), (500, 'not json'),
    (500, '{"user":{"token":"not-a-session"}}'),
])
def test_no_account_token_is_never_reported_as_success(signup_flow, status, body):
    flow = signup_flow
    flow.responses['finish_signup'] = {'status': status, 'body': body}
    assert flow.ns['signup']('owned@example.test', 'PRIVATE_PASSWORD', flow.worker.mail) == 'SIGNUP_FINISH_FAILED'
    assert flow.posts == ['verify_email', 'finish_signup']


def test_finish_error_survives_unavailable_browser_storage_and_cleanup(signup_flow):
    flow = signup_flow
    evaluate = flow.page.evaluate.side_effect

    def broken_storage(script, arg=None):
        if 'localStorage' in script:
            raise RuntimeError('fixture storage unavailable')
        return evaluate(script, arg)

    flow.page.evaluate.side_effect = broken_storage
    flow.context.cookies.side_effect = RuntimeError('fixture cookies unavailable')
    flow.context.close.side_effect = RuntimeError('fixture browser already closed')
    assert flow.ns['signup']('owned@example.test', 'PRIVATE_PASSWORD', flow.worker.mail) == 'SIGNUP_FINISH_FAILED'
    assert (Path(flow.ns['OUTPUT_DIR']) / 'recovery_accounts.txt').exists()
    assert flow.posts == ['verify_email', 'finish_signup']


def test_finish_rate_limit_keeps_existing_category_and_recovery(signup_flow):
    flow = signup_flow
    flow.responses['finish_signup'] = {
        'status': 429, 'body': json.dumps({'detail': {
            'code': 'SIGNUP_RATE_LIMITED', 'retry_after_seconds': 3600, 'scope': 'signup:ip'}})}
    assert flow.ns['signup']('owned@example.test', 'PRIVATE_PASSWORD', flow.worker.mail) == 'SIGNUP_IP_LIMITED'
    assert 'finish_blocked|retry_after=3600|scope=signup:ip' in (
        Path(flow.ns['OUTPUT_DIR']) / 'recovery_accounts.txt').read_text()


def test_finish_non200_with_matching_session_still_returns_token(signup_flow):
    import base64

    flow = signup_flow
    payload = base64.urlsafe_b64encode(json.dumps({'email': 'owned@example.test'}).encode()).decode().rstrip('=')
    token = f'eyJhbGciOiJub25lIn0.{payload}.fixture'
    flow.context.cookies.return_value = [{'name': 'token', 'value': token}]
    assert flow.ns['signup']('owned@example.test', 'PRIVATE_PASSWORD', flow.worker.mail) == token
    assert not (Path(flow.ns['OUTPUT_DIR']) / 'recovery_accounts.txt').exists()
    assert flow.posts == ['verify_email', 'finish_signup']


@pytest.mark.parametrize('auto,expected', [(True, 'CAPTCHA_FAILED'), (False, None)])
def test_solver_failure_keeps_manual_behavior(signup_flow, monkeypatch, auto, expected):
    flow = signup_flow
    monkeypatch.setenv('ZAI_AUTO_GENERATE', '1' if auto else '0')
    flow.page.on.side_effect = None
    flow.ns['CaptchaSolver'].return_value.solve.return_value = None
    assert flow.ns['signup']('owned@example.test', 'PRIVATE_PASSWORD', flow.worker.mail) == expected
    assert flow.posts == []


@pytest.mark.parametrize('auto,expected', [(True, 'BROWSER_FAILED'), (False, None)])
def test_browser_launch_failure_keeps_manual_behavior(signup_flow, monkeypatch, auto, expected):
    flow = signup_flow
    monkeypatch.setenv('ZAI_AUTO_GENERATE', '1' if auto else '0')
    flow.browser.chromium.launch_persistent_context.side_effect = RuntimeError('fixture failure')
    assert flow.ns['signup']('owned@example.test', 'PRIVATE_PASSWORD', flow.worker.mail) == expected
    assert flow.posts == []


@pytest.mark.parametrize('auto,expected', [(True, 'MAIL_TIMEOUT'), (False, 'SIGNUP_UNVERIFIED')])
def test_mail_timeout_keeps_recovery_and_manual_behavior(signup_flow, monkeypatch, auto, expected):
    flow = signup_flow
    monkeypatch.setenv('ZAI_AUTO_GENERATE', '1' if auto else '0')
    flow.worker.mail.get_verification_link.return_value = None
    assert flow.ns['signup']('owned@example.test', 'PRIVATE_PASSWORD', flow.worker.mail) == expected
    assert flow.posts == []
    assert 'unverified|no_link' in (Path(flow.ns['OUTPUT_DIR']) / 'recovery_accounts.txt').read_text()


def test_log_filter_sanitizes_real_records_on_both_handlers(monkeypatch, tmp_path):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    module = ast.Module(body=[node for node in tree.body
                             if isinstance(node, ast.ClassDef)
                             and node.name in ('_SecretFilter', '_SafeConsoleHandler')],
                        type_ignores=[])
    ns = {'logging': logging, 'os': os, 're': re, 'urllib': urllib, 'traceback': traceback}
    exec(compile(module, str(SOURCE), 'exec'), ns)
    secret_filter = ns['_SecretFilter']()
    secret_filter.remember('GeneratedPassword!')
    secret_filter.remember('runtime-inbox-secret')
    monkeypatch.setenv('ZAI_INBOX_SECRET', 'env secret/unsafe')
    stream = io.StringIO()
    path = tmp_path / 'worker.log'
    handlers = [ns['_SafeConsoleHandler'](stream), logging.FileHandler(path, encoding='utf-8')]
    try:
        try:
            raise RuntimeError('runtime-inbox-secret https://mail.test/inbox?k=trace-query')
        except RuntimeError:
            exc = sys.exc_info()
        for handler in handlers:
            handler.addFilter(secret_filter)
            # Separate records prove each handler sanitizes independently.
            record = logging.LogRecord('urllib3.connectionpool', logging.DEBUG, 'fixture', 1,
                                       'Password: %s https://mail.test/inbox?k=raw-secret&to=email '
                                       'verify?token=verify-abcdef Authorization: Bearer opaque-bearer '
                                       'eyJhbGciOiJub25lIn0.eyJlbWFpbCI6IngifQ.sig '
                                       'env%%20secret%%2Funsafe {"token":"body-secret"}',
                                       ('GeneratedPassword!',), exc)
            record.stack_info = 'secret=stack-secret'
            handler.handle(record)
            handler.flush()
    finally:
        for handler in handlers:
            handler.close()
    for output in (stream.getvalue(), path.read_text(encoding='utf-8')):
        assert '[REDACTED]' in output
        assert 'RuntimeError' in output
        for secret in ('GeneratedPassword!', 'runtime-inbox-secret', 'raw-secret',
                       'verify-abcdef', 'opaque-bearer', 'eyJhbGci', 'env%20secret%2Funsafe',
                       'body-secret', 'trace-query', 'stack-secret'):
            assert secret not in output
    attachments = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr == 'addFilter']
    assert attachments


def test_signup_uses_owned_profile_helper():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    signup = next(node for node in tree.body
                  if isinstance(node, ast.FunctionDef) and node.name == 'signup')
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == '_account_profile' for node in ast.walk(signup))


@pytest.mark.parametrize('policy', ['direct', 'proxy'])
def test_private_route_written_before_terminal_result(worker, monkeypatch, capsys,
                                                      tmp_path, policy):
    directory = tmp_path / 'auto-private'
    directory.mkdir()
    path = directory / 'worker.json'
    monkeypatch.setenv('ZAI_AUTO_ROUTE_FILE', str(path))
    monkeypatch.setenv('ZAI_ROUTE_POLICY', policy)
    proxy = 'socks5://PRIVATE_USER:PRIVATE_PASSWORD@proxy.test:1080'
    if policy == 'proxy':
        worker.ns['pick_verified_proxy'].return_value = proxy
    publish = worker.ns['_publish_auto_result']
    observed = []

    def terminal(result):
        observed.append(json.loads(path.read_text(encoding='utf-8')))
        publish(result)

    worker.ns['_publish_auto_result'] = terminal
    result = run(worker, capsys)
    assert result['category'] == 'generated'
    assert observed == [{'proxy': proxy if policy == 'proxy' else ''}]
    assert list(directory.iterdir()) == [path]
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600


def test_absent_route_file_env_never_touches_disk(worker, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('metadata must not touch the filesystem without opt-in')

    import tempfile
    monkeypatch.setattr(tempfile, 'mkstemp', forbidden)
    assert run(worker, capsys)['category'] == 'generated'


def test_pool_empty_retains_failure_diagnostic_without_route_file(worker, monkeypatch,
                                                                 capsys, tmp_path):
    directory = tmp_path / 'auto-private'
    directory.mkdir()
    path = directory / 'worker.json'
    monkeypatch.setenv('ZAI_AUTO_ROUTE_FILE', str(path))
    monkeypatch.setenv('ZAI_ROUTE_POLICY', 'proxy')
    worker.ns['pick_verified_proxy'].return_value = None
    assert run(worker, capsys)['category'] == 'pool_empty'
    assert not path.exists()
    worker.ns['signup'].assert_not_called()


def test_manual_never_writes_auto_route_file(worker, monkeypatch, capsys, tmp_path):
    monkeypatch.delenv('ZAI_AUTO_GENERATE')
    monkeypatch.setenv('ZAI_AUTO_ROUTE_FILE', str(tmp_path / 'auto-private' / 'worker.json'))
    worker.ns['sys'].argv = ['main.py', '1']
    worker.ns['main']()
    assert PREFIX not in capsys.readouterr().out
    assert not (tmp_path / 'auto-private').exists()


@pytest.mark.parametrize('destination', ['relative', 'outside', 'missing'])
def test_invalid_route_metadata_path_fails_safely(worker, monkeypatch, capsys,
                                                tmp_path, destination):
    paths = {
        'relative': 'auto-private/worker.json',
        'outside': str(tmp_path / 'worker.json'),
        'missing': str(tmp_path / 'auto-private' / 'worker.json'),
    }
    monkeypatch.setenv('ZAI_AUTO_ROUTE_FILE', paths[destination])
    assert run(worker, capsys)['category'] == 'failed'
    assert not (tmp_path / 'worker.json').exists()
    assert not (tmp_path / 'auto-private').exists()


def test_route_write_failure_cleans_temp_and_preserves_previous(worker, monkeypatch,
                                                               capsys, tmp_path):
    directory = tmp_path / 'auto-private'
    directory.mkdir()
    path = directory / 'worker.json'
    path.write_text('{"proxy":"previous"}', encoding='utf-8')
    monkeypatch.setenv('ZAI_AUTO_ROUTE_FILE', str(path))
    replace = Mock(side_effect=OSError('PRIVATE_PASSWORD'))
    monkeypatch.setattr(os, 'replace', replace)
    assert run(worker, capsys)['category'] == 'failed'
    assert json.loads(path.read_text(encoding='utf-8')) == {'proxy': 'previous'}
    assert list(directory.iterdir()) == [path]


@pytest.mark.parametrize('policy,proxy', [
    ('direct', 'socks5://proxy.test:1080'), ('proxy', None),
    ('unexpected', None), ('proxy', 'not-a-proxy'),
    ('proxy', 'http://PRIVATE_PASSWORD@proxy.test:invalid'),
    ('proxy', 'file://proxy.test:1234'),
])
def test_route_metadata_rejects_policy_mismatch(worker, monkeypatch, tmp_path, policy, proxy):
    directory = tmp_path / 'auto-private'
    directory.mkdir()
    monkeypatch.setenv('ZAI_AUTO_ROUTE_FILE', str(directory / 'worker.json'))
    monkeypatch.setenv('ZAI_ROUTE_POLICY', policy)
    worker.ns['CURRENT_PROXY'] = proxy
    with pytest.raises(ValueError, match='^Invalid private worker route$'):
        worker.ns['_save_auto_route_metadata']()
    assert list(directory.iterdir()) == []


@pytest.fixture
def routes():
    from urllib.parse import urlsplit

    source = SOURCE.with_name('plan_check.py')
    tree = ast.parse(source.read_text(encoding='utf-8'))
    module = ast.Module(body=[
        node for node in tree.body if isinstance(node, ast.FunctionDef)
        and node.name in {'_proxies', 'request_route', 'direct_requests', '_session'}
    ], type_ignores=[])
    ns = {'contextmanager': contextmanager, 'urlsplit': urlsplit, 'os': os,
          '_tls': threading.local(),
          '_NO_PROXY': {'', '0', 'none', 'null', 'direct', 'off', 'false', 'no'},
          'requests': types.SimpleNamespace(Session=Mock(return_value=types.SimpleNamespace()))}
    exec(compile(module, str(source), 'exec'), ns)
    return ns


def test_request_route_restores_nested_routes_without_env_mutation(routes, monkeypatch):
    monkeypatch.setenv('ZAI_PROXY', 'http://ambient.test:8080')
    before = dict(os.environ)
    explicit = 'socks5://PRIVATE_USER:PRIVATE_PASSWORD@proxy.test:1080'
    with routes['request_route'](explicit):
        assert routes['_proxies']() == {'http': explicit, 'https': explicit}
        with routes['request_route']():
            assert routes['_proxies']() is None
        assert routes['_proxies']()['http'] == explicit
        with routes['direct_requests'](True):
            assert routes['_proxies']() is None
        assert routes['_proxies']()['https'] == explicit
    assert routes['_proxies']()['http'] == 'http://ambient.test:8080'
    assert dict(os.environ) == before
    assert routes['_session']().trust_env is False


def test_request_route_restores_on_exception(routes, monkeypatch):
    monkeypatch.setenv('ZAI_PROXY', 'http://ambient.test:8080')
    with pytest.raises(RuntimeError):
        with routes['request_route'](''):
            assert routes['_proxies']() is None
            raise RuntimeError('mock failure')
    assert routes['_proxies']()['http'] == 'http://ambient.test:8080'


@pytest.mark.parametrize('invalid', [
    '0', 'direct', 'garbage', 'http://proxy.test', 'file://proxy.test:99',
    'http://proxy.test:bad', 'http://proxy.test:8080?secret=yes',
    'http://proxy.test:8080/route', 123, [], {},
])
def test_request_route_invalid_proxy_fails_closed(routes, invalid):
    with pytest.raises(ValueError, match='^Invalid explicit request route$'):
        with routes['request_route'](invalid):
            pytest.fail('invalid route must not enter scope')
    assert not hasattr(routes['_tls'], 'request_proxy')


def test_request_routes_are_thread_local(routes, monkeypatch):
    monkeypatch.setenv('ZAI_PROXY', 'http://ambient.test:8080')
    barrier = threading.Barrier(3)
    results = []

    def worker_route(proxy):
        with routes['request_route'](proxy):
            barrier.wait(timeout=5)
            results.append(routes['_proxies']())
            barrier.wait(timeout=5)

    direct = threading.Thread(target=worker_route, args=(None,))
    proxied = threading.Thread(target=worker_route, args=('socks5://proxy.test:1080',))
    direct.start()
    proxied.start()
    barrier.wait(timeout=5)
    assert routes['_proxies']()['http'] == 'http://ambient.test:8080'
    barrier.wait(timeout=5)
    direct.join(timeout=5)
    proxied.join(timeout=5)
    assert not direct.is_alive() and not proxied.is_alive()
    assert None in results
    assert {'http': 'socks5://proxy.test:1080', 'https': 'socks5://proxy.test:1080'} in results
