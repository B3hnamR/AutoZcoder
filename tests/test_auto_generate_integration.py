import concurrent.futures
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from zcode_gateway.app import create_app
from zcode_gateway.config import Config
from zcode_gateway.generator import GeneratorManager, GenWorker


def test_registration_ledger_parallel_updates_keep_all_entries(tmp_path):
    mgr = GeneratorManager(tmp_path)
    mgr.mark_registered('existing@example.test')
    barrier = threading.Barrier(3)
    def add(i):
        barrier.wait()
        mgr.mark_registered(f'worker{i}@example.test')
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(add, range(3)))
    assert mgr.registered_emails() == {'existing@example.test', *(f'worker{i}@example.test' for i in range(3))}
    assert not list(mgr.data_dir.glob('.registered-*'))


def test_auto_worker_done_waits_for_registration_ack_and_uses_private_route(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def register(info):
        assert info['registration_proxy'] == 'socks5://user:pass@127.0.0.1:1080'
        entered.set()
        assert release.wait(3)
        return {'qualified': True, 'account_id': 'new-identity', 'category': 'qualified'}
    mgr = GeneratorManager(tmp_path, on_account=register)
    route = tmp_path/'route.json'
    route.write_text(json.dumps({'proxy':'socks5://user:pass@127.0.0.1:1080'}))
    env = {'ZAI_OUTPUT_DIR':str(tmp_path), 'ZAI_OUT_SUFFIX':'auto-test', 'ZAI_AUTO_GENERATE':'1',
           'ZAI_ROUTE_POLICY':'proxy', 'ZAI_AUTO_ROUTE_FILE':str(route)}
    worker = GenWorker('test', [], env, tmp_path, on_exit=mgr._worker_done)
    worker.starter=True
    worker.script_result={'category':'generated'}
    worker.account_file.parent.mkdir(exist_ok=True)
    worker.account_file.write_text('new@example.test:password:eyJ.fake.token\n')
    thread=threading.Thread(target=worker._finish)
    thread.start()
    assert entered.wait(2)
    assert not worker.done.is_set()
    release.set();thread.join(timeout=3)
    assert worker.done.is_set() and worker.result['qualified']
    assert not route.exists()
    assert 'user:pass' not in json.dumps(worker.to_public())


def test_cancelled_worker_cannot_register_late_result(tmp_path):
    callback=Mock()
    mgr=GeneratorManager(tmp_path,on_account=callback)
    worker=GenWorker('test',[],{'ZAI_AUTO_GENERATE':'1','ZAI_OUTPUT_DIR':str(tmp_path)},tmp_path,on_exit=mgr._worker_done)
    worker.stopped=True
    worker._finish()
    assert worker.done.is_set() and not worker.result['qualified']
    callback.assert_not_called()


def test_real_manager_auto_launcher_must_not_reject_its_own_session(tmp_path, monkeypatch):
    """Regression: manual-only guard accidentally lived inside _auto_launch."""
    mgr = GeneratorManager(tmp_path)
    mgr._auto_runtime_snapshot = SimpleNamespace(_worker_env=lambda name: {'ZAI_OUTPUT_DIR': str(tmp_path)})
    monkeypatch.setattr(mgr.auto_generate, 'status', lambda: {'status': 'running'})
    starts = []
    monkeypatch.setattr(GenWorker, 'start', lambda worker: starts.append(worker))
    worker = mgr._auto_launch(mgr.auto_generate.config(), 'testsession-1')
    assert isinstance(worker, GenWorker)
    assert starts == [worker] and mgr.workers[worker.name] is worker
    assert worker.env['ZAI_AUTO_RUN_ID'] == 'testsession'
    assert mgr.start_workers(1)['error'].startswith('Stop the Auto Generate')


def test_manual_guard_rechecks_after_display_setup(tmp_path, monkeypatch):
    mgr = GeneratorManager(tmp_path)
    states = iter([{'status': 'idle'}, {'status': 'preparing'}])
    monkeypatch.setattr(mgr.auto_generate, 'status', lambda: next(states))
    monkeypatch.setattr(mgr, '_ensure_display', lambda **kw: '')
    monkeypatch.setattr(GenWorker, 'start', lambda _: pytest.fail('manual worker launched during auto preflight'))
    assert 'error' in mgr.start_workers(1)
    assert not mgr.workers


def test_controller_through_actual_manager_launcher_completes_one_fake_child(tmp_path, monkeypatch):
    import time
    mgr = GeneratorManager(tmp_path)
    mgr._auto_runtime_snapshot = SimpleNamespace(_worker_env=lambda name: {'ZAI_OUTPUT_DIR': str(tmp_path)})
    mgr.auto_generate._preflight = lambda _: None
    # Emulate process output and exit only. No browser, provider, or signup.
    def popen(*args, **kwargs):
        return SimpleNamespace(stdout=iter(['AUTO_GENERATE_RESULT {"category":"no_plan","message":"fixture"}\n']),
                               returncode=0, wait=lambda **_: 0, poll=lambda: 0)
    monkeypatch.setattr('zcode_gateway.generator.subprocess.Popen', popen)
    mgr.auto_generate.start({**mgr.auto_generate.config(), 'mode': 'attempts',
                             'attempt_limit': 1, 'cooldown_seconds': 0})
    deadline = time.monotonic() + 3
    while mgr.auto_generate.status()['status'] != 'completed' and time.monotonic() < deadline:
        threading.Event().wait(.01)
    status = mgr.auto_generate.status()
    assert status['status'] == 'completed'
    assert status['categories'] == {'no_plan': 1}
    assert len(mgr.workers) == 1
    assert any(event['worker'].startswith('auto-') for event in status['events'] if event['worker'])
    mgr.shutdown()


def test_auto_preflight_checks_real_prerequisites_without_altering_manual_options(tmp_path, monkeypatch):
    mgr=GeneratorManager(tmp_path)
    mgr.env_setup.check=lambda:{'all_ok':True}
    mgr.mail_cfg={'active':'example.test','inbox_url':'https://worker.invalid/inbox','inbox_secret':'dummy'}
    mgr._verify_inbox=lambda *a,**kw:{'ok':True}
    mgr.auto_ai_ready=lambda:True
    mgr.captcha='manual';mgr.captcha_strategy='auto';mgr.ip_source='pin'
    mgr.pin_proxy='http://saved.invalid:8080'
    monkeypatch.setattr('zcode_gateway.generator.sys.platform','win32')
    cfg=mgr.auto_generate.config()
    mgr._auto_preflight(cfg)
    snapshot=mgr._auto_runtime_snapshot
    assert (snapshot.captcha,snapshot.captcha_strategy,snapshot.ip_source,snapshot.retries)==('ai','ai','direct',0)
    assert snapshot.browser_display == 'headless'
    assert mgr.captcha=='manual' and mgr.ip_source=='pin'
    mgr._pool_has_candidates=lambda:False
    with pytest.raises(ValueError,match='No proxy'):
        mgr._auto_preflight({**cfg,'ip_source':'proxy'})


@pytest.mark.parametrize('mode,headless', [('background','1'),('desktop','0')])
def test_windows_auto_saved_display_controls_actual_launch_environment(tmp_path,monkeypatch,mode,headless):
    mgr=GeneratorManager(tmp_path)
    monkeypatch.setattr('zcode_gateway.generator.sys.platform','win32')
    mgr.env_setup.check=lambda:{'all_ok':True}
    mgr.mail_cfg={'active':'example.test','inbox_url':'https://worker.invalid/inbox','inbox_secret':'dummy'}
    mgr._verify_inbox=lambda *a,**kw:{'ok':True}
    mgr.auto_ai_ready=lambda:True
    config={**mgr.auto_generate.config(),'browser_display':mode}
    mgr._auto_preflight(config)
    calls=[]
    monkeypatch.setattr(GenWorker,'start',lambda worker:calls.append(worker))
    worker=mgr._auto_launch(config,'session-1')
    assert worker.env['ZAI_HEADLESS']==headless
    assert worker.env['ZAI_MANUAL']=='0'
    assert worker.env['ZAI_CAPTCHA_STRATEGY']=='ai'
    assert calls==[worker]
    assert mgr.browser_display=='auto'  # manual preferences untouched


def test_windows_manual_solver_never_uses_headless(tmp_path,monkeypatch):
    mgr=GeneratorManager(tmp_path)
    monkeypatch.setattr('zcode_gateway.generator.sys.platform','win32')
    mgr.set_options(browser_display='headless',captcha='manual')
    assert mgr._worker_env('manual')['ZAI_HEADLESS']=='0'


def test_auto_nonzero_exit_preserves_explicit_failed_stage(tmp_path):
    mgr=GeneratorManager(tmp_path)
    worker=GenWorker('test',[],{'ZAI_AUTO_GENERATE':'1','ZAI_OUTPUT_DIR':str(tmp_path)},tmp_path,on_exit=mgr._worker_done)
    worker.exit_code=1
    worker.script_result={'category':'finish_signup_failed','message':'Provider rejected final signup (HTTP 500).'}
    worker._finish()
    assert worker.result['category']=='finish_signup_failed'
    assert worker.done.is_set() and not worker.result['qualified']


def test_auto_admin_api_save_does_not_start_and_caps_concurrency(tmp_path,monkeypatch):
    cfg=Config(server={'host':'127.0.0.1','admin_key':'dummy-admin'},
               accounts={'source':'accounts_dir','dir':str(tmp_path/'accounts')},
               store={'sqlite_path':str(tmp_path/'gateway.db')},update={'enabled':False})
    mgr=GeneratorManager(tmp_path)
    monkeypatch.setattr('zcode_gateway.api_dash._generator',lambda _:mgr)
    headers={'Authorization':'Bearer dummy-admin'}
    with TestClient(create_app(cfg)) as client:
        assert client.get('/admin/dash/generator/auto').status_code in (401,403)
        response=client.put('/admin/dash/generator/auto',headers=headers,
            json={**mgr.auto_generate.config(),'target':20,'concurrency':3,'cooldown_seconds':10})
        assert response.status_code==200
        assert response.json()['status']['state']=='idle'
        assert response.json()['config']['cooldown_seconds']==10
        assert client.put('/admin/dash/generator/auto',headers=headers,
            json={**mgr.auto_generate.config(),'concurrency':4}).status_code==400
        assert client.post('/admin/dash/generator/auto/unknown',headers=headers).status_code==404
        assert client.post('/admin/dash/generator/auto/stop',headers=headers).status_code==200
        assert client.post('/admin/dash/generator/auto/resume',headers=headers).status_code==409


@pytest.mark.parametrize('active,quota,expected', [(True,10,True),(False,10,False),(True,0,False)])
def test_auto_registration_requires_verified_start_plan_and_usable_quota(tmp_path,monkeypatch,active,quota,expected):
    import base64,time,contextlib,sys
    from zcode_gateway import api_dash
    gen=tmp_path/'scripts';gen.mkdir();(gen/'main.py').write_text('')
    cfg=Config(server={'host':'127.0.0.1','admin_key':'dummy-admin'},
               accounts={'source':'accounts_dir','dir':str(tmp_path/'accounts')},
               store={'sqlite_path':str(tmp_path/'gateway.db')},update={'enabled':False},
               generator={'zcodeauto_dir':str(gen)})
    ident='new-account-fixture'
    jwt='eyJ.'+base64.urlsafe_b64encode(json.dumps({'user_id':ident}).encode()).decode().rstrip('=')+'.sig'
    now=time.time()
    raw={'plans':[{'user_plan_id':'p','plan_id':'zcode-v3-start-plan-test','status':'active' if active else 'expired',
                   'starts_at':now-1,'ends_at':now+3600}],
         'balances':[{'user_plan_id':'p','show_name':'GLM-5.3-Flash','remaining_units':quota,'total_units':10,
                      'period_end':now+3600}]}
    monkeypatch.setitem(sys.modules,'plan_check',SimpleNamespace(
        get_zcode_jwt=lambda *_a,**_kw:jwt,
        get_plan=lambda _: {'code':0,'plans':['zcode-v3-start-plan-test'] if active else ['unrelated'], 'raw':raw},
        is_start_plan=lambda p:p=='zcode-v3-start-plan-test',request_route=lambda _:contextlib.nullcontext()))
    monkeypatch.setattr(api_dash,'_referral_needs_finish',lambda _:False)
    with TestClient(create_app(cfg)) as client:
        client.get('/admin/dash/generator/mail',headers={'Authorization':'Bearer dummy-admin'})
        mgr=client.app.state.app_state.generator
        result=mgr.on_account({'email':'new@example.test','chat_token':'eyJ.fixture.sig','auto_generate':True,
                               'route_policy':'direct','registration_proxy':''})
        assert result['qualified'] is expected
        if expected:
            assert result['account_id']==ident
            assert 'new@example.test' in mgr.registered_emails()
            rows=client.get('/admin/accounts',headers={'Authorization':'Bearer dummy-admin'}).json()['accounts']
            assert rows[0]['quotas']['GLM-5.3-Flash']['remaining']==10
            duplicate=mgr.on_account({'email':'new@example.test','chat_token':'eyJ.fixture.sig','auto_generate':True})
            assert duplicate['category']=='duplicate'
