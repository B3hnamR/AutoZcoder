import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi import HTTPException
from zcode_gateway import api_dash
from zcode_gateway.config import Config, load_config
from zcode_gateway.pool import Account, PoolManager
from fastapi.testclient import TestClient
from zcode_gateway.app import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize('values',[
    {'pool.acquire_wait_seconds':-1}, {'pool.acquire_wait_seconds':'nan'},
    {'update.interval_hours':-1}, {'models.pacing.max_gap_ms':-1},
    {'logging.capture':'typo'}, {'pool.max_in_flight_per_account':0},
])
async def test_full_settings_validation_rejects_before_persist(tmp_path,monkeypatch,values):
    cfg=Config();path=tmp_path/'config.yaml'
    path.write_text(yaml.safe_dump(cfg.model_dump(mode='json')))
    cfg._config_file=path
    state=SimpleNamespace(cfg=cfg)
    monkeypatch.setattr(api_dash,'get_state',lambda _:state)
    old=path.read_bytes()
    with pytest.raises(HTTPException) as error:
        await api_dash.dash_settings_put(None,{'values':values},'admin')
    assert error.value.status_code==400
    assert path.read_bytes()==old


def test_tls_paths_are_relative_to_config_directory(tmp_path):
    p=tmp_path/'config.yaml'
    p.write_text('server:\n  admin_key: dummy-test-admin\n  tls_cert: certs/fullchain.pem\n  tls_key: certs/privkey.pem\n')
    cfg=load_config(p)
    assert Path(cfg.server.tls_cert)==tmp_path/'certs/fullchain.pem'
    assert Path(cfg.server.tls_key)==tmp_path/'certs/privkey.pem'


def test_start_qualification_only_uses_own_current_balance():
    now=time.time()
    def plan(owner,name):return {'user_plan_id':owner,'plan_id':name,'status':'active','starts_at':now-1,'ends_at':now+60}
    def bucket(owner,remaining):return {'user_plan_id':owner,'show_name':'GLM-5.3','remaining_units':remaining,'total_units':100,'period_end':now+60}
    raw={'plans':[plan('start','zcode-v3-start-plan-fixture'),plan('paid','paid-fixture')],
         'balances':[bucket('start',0),bucket('paid',100)]}
    assert not api_dash._qualifying_start_balance(raw,now)
    raw['balances'][0]['remaining_units']=5
    assert api_dash._qualifying_start_balance(raw,now)
    raw['plans'][0]['starts_at']=now+10
    assert not api_dash._qualifying_start_balance(raw,now)
    raw['plans'][0].update(starts_at=now-1,plan_id='zcode-v3-start-plan-500m')
    assert not api_dash._qualifying_start_balance(raw,now)


@pytest.mark.asyncio
async def test_login_probe_updates_canonical_and_initializing_flow_reused(tmp_path,monkeypatch):
    from zcode_gateway import device_auth
    canonical=Account(id='same',access_token='old')
    pool=PoolManager(Config().pool,[canonical]);probed=[]
    cfg=Config(accounts={'source':'accounts_dir','dir':str(tmp_path)})
    state=SimpleNamespace(cfg=cfg,pool=pool,tokens=SimpleNamespace(check_account=AsyncMock(side_effect=lambda account:probed.append(account))))
    monkeypatch.setattr(api_dash,'get_state',lambda _:state)
    gate=asyncio.Event();created=[]
    class Login:
        def __init__(self,*args,**kwargs):
            self.state='starting';self._ready=asyncio.Event();self._ready.set();self.callback=kwargs['on_account'];created.append(self)
        async def run(self):await gate.wait()
        def status(self):return {'state':self.state}
    monkeypatch.setattr(device_auth,'DeviceLogin',Login)
    await api_dash.login_start(None,'admin')
    await api_dash.login_start(None,'admin')
    assert len(created)==1
    await created[0].callback(Account(id='same',access_token='new'))
    assert probed==[canonical] and canonical.access_token=='new'
    gate.set();await created[0]._task;await pool.aclose()


def test_update_health_nonce_is_local_and_not_cacheable(tmp_path,monkeypatch):
    monkeypatch.setenv('ZCODE_GATEWAY_UPDATE_NONCE','a'*32)
    cfg=Config(server={'host':'127.0.0.1','admin_key':'dummy-admin','client_keys':[{'key':'fixture-client-key','name':'fixture'}]},
               accounts={'source':'accounts_dir','dir':str(tmp_path/'accounts')},
               store={'sqlite_path':str(tmp_path/'usage.db')},update={'enabled':False})
    with TestClient(create_app(cfg),client=('127.0.0.1',12345),base_url='http://127.0.0.1:8788') as client:
        response=client.get('/health')
        assert response.status_code==200
        assert response.headers['X-AutoZCoder-Update']=='a'*32
        assert response.headers['Cache-Control']=='no-store'
    cfg.server.host='0.0.0.0';cfg.server.expose_ops=True
    with TestClient(create_app(cfg),client=('198.51.100.3',12345),base_url='http://127.0.0.1:8788') as client:
        assert 'X-AutoZCoder-Update' not in client.get('/health').headers
        reply=client.get('/health',headers={'X-AutoZCoder-Update':'a'*32})
        assert reply.status_code==200 and reply.headers['X-AutoZCoder-Update']=='a'*32


def test_expired_bucket_cannot_be_restored_from_entitlement_for_goal():
    from zcode_gateway.quota import parse_balance
    now=time.time()
    raw={'plans':[{'user_plan_id':'s','plan_id':'zcode-v3-start-plan-test','status':'active',
                   'starts_at':now-100,'ends_at':now+100,
                   'entitlements':[{'entitlement_id':'e','show_name':'GLM-5.3',
                                    'grant_units':100,'effective_at':now-50}]}],
         'balances':[{'user_plan_id':'s','entitlement_id':'e','show_name':'GLM-5.3',
                      'remaining_units':100,'total_units':100,'expires_at':now-1,'period_end':now-1}]}
    assert not api_dash._qualifying_start_balance(raw,now)
    _,quotas=parse_balance(raw,now)
    assert not quotas
