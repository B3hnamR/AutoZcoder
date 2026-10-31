"""Batch cancellation and outcome bookkeeping, with no provider traffic."""
import threading
import time
from types import SimpleNamespace

import pytest
from zcode_gateway.generator import GeneratorManager, ClaimJob


def manager(tmp_path):
    return GeneratorManager(tmp_path, data_dir=tmp_path / 'data')


def wait_done(mgr):
    for _ in range(100):
        if not mgr.claim_all_status()['running']:
            return
        time.sleep(.01)
    pytest.fail('batch did not terminate')


def test_failed_then_successful_claim_batch_finishes(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    monkeypatch.setattr(mgr, 'unclaimed_accounts', lambda: [{'email':'a@example.test'}, {'email':'b@example.test'}])
    calls = []
    def start(email, mode):
        calls.append((email, mode))
        job = SimpleNamespace(email=email, done=threading.Event(), result={'claimed':email.startswith('b')}, alive=lambda:False)
        job.done.set()
        mgr.claims[email] = job
        return {'started':email}
    monkeypatch.setattr(mgr, 'start_claim_start', start)
    assert mgr.start_claim_all()['mode'] == 'direct'
    wait_done(mgr)
    status = mgr.claim_all_status()
    assert status['state'] == 'completed' and status['completed'] == 2
    assert status['succeeded'] == status['failed'] == 1
    assert all(mode == 'direct' for _,mode in calls)


def test_stop_batch_kills_current_job_and_never_starts_next(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    monkeypatch.setattr(mgr, 'unclaimed_accounts', lambda: [{'email':'a@example.test'}, {'email':'b@example.test'}])
    entered = threading.Event()
    calls=[]
    job = ClaimJob('first','a@example.test','direct','',tmp_path,'python','',data_dir=tmp_path,kind='start')
    def start(email, mode):
        calls.append(email);mgr.claims['first']=job;entered.set();return {'started':'first'}
    monkeypatch.setattr(mgr,'start_claim_start',start)
    mgr.start_claim_all()
    assert entered.wait(1)
    # Cancellation must work even if requested between registration and the
    # batch publishing its current job name.
    mgr.stop_claim_all()
    wait_done(mgr)
    assert calls == ['a@example.test']
    assert job.stopped and job.state == 'killed'
    assert mgr.claim_all_status()['state']=='cancelled'


def test_batch_exception_and_thread_start_failure_release_running(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    def fail():raise ValueError('failure fixture')
    monkeypatch.setattr(mgr,'unclaimed_accounts',fail)
    mgr.start_claim_all();wait_done(mgr)
    assert mgr.claim_all_status()['state']=='failed'
    monkeypatch.setattr(threading.Thread,'start',lambda _: (_ for _ in ()).throw(RuntimeError('start failed')))
    assert 'error' in mgr.start_claim_all()
    assert not mgr.claim_all_status()['running']


def test_claim_elapsed_stops_and_cancelled_state_survives_pump(tmp_path, monkeypatch):
    job=ClaimJob('first','a@example.test','direct','',tmp_path,'python','',data_dir=tmp_path)
    job.started=time.time()-3
    job.stopped=True
    job.proc=SimpleNamespace(stdout=iter(['CLAIM_RESULT {"claimed":false}\n']),returncode=1,wait=lambda:None)
    job._pump()
    assert job.state=='killed' and job.done.is_set()
    elapsed=job.to_public()['elapsed']
    monkeypatch.setattr(time,'time',lambda:job.finished+100)
    assert job.to_public()['elapsed']==elapsed


def test_batch_stop_api_does_not_create_manager_and_start_defaults_direct(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from zcode_gateway.app import create_app
    from zcode_gateway.config import Config
    from unittest.mock import Mock
    cfg=Config(server={'host':'127.0.0.1','admin_key':'test-admin'},
               accounts={'source':'accounts_dir','dir':str(tmp_path/'accounts')},
               store={'sqlite_path':str(tmp_path/'gateway.db')},update={'enabled':False})
    headers={'Authorization':'Bearer test-admin'}
    with TestClient(create_app(cfg)) as client:
        assert client.post('/admin/claims/claim_all_stop').status_code in (401,403)
        assert client.post('/admin/claims/claim_all_stop',headers=headers).json()['state']=='idle'
        mgr=Mock()
        mgr.start_claim_all.return_value={'started':True,'mode':'direct'}
        monkeypatch.setattr('zcode_gateway.api_dash._generator',lambda _:mgr)
        result=client.post('/admin/claims/claim_all_pending',headers=headers,json={})
        assert result.status_code==200
        mgr.start_claim_all.assert_called_once_with(mode='direct')
