import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from zcode_gateway.app import create_app
from zcode_gateway.config import Config, ClientKey, Fingerprint
from zcode_gateway.credential_cipher import decrypt_aes_gcm
from zcode_gateway.pool import Account, AccountState, PoolManager, Router
from zcode_gateway.quota import parse_balance
from zcode_gateway.registry import Registry
from zcode_gateway.service import ChatService, UpstreamFailure, NoAccountsAvailable
from zcode_gateway.store import Store, JsonlCapture
from zcode_gateway.tokens import TokenManager
from zcode_gateway.upstream import build_headers, iter_sse_events
from zcode_gateway.zcode_protocol import build_body, OpenAITranslator, MessageAccumulator, usage_openai
from zcode_gateway.device_auth import DeviceLogin
from tests_fixtures import sample_sse, sample_events, FakeClient, FakeResponse, frame

@pytest.fixture
def cfg(tmp_path):
    config=Config()
    config.accounts.dir=str(tmp_path/'accounts')
    config.accounts.pool_file=str(tmp_path/'pool.json')
    config.store.sqlite_path=str(tmp_path/'gateway.db')
    config.logging.capture_dir=str(tmp_path/'logs')
    config.logging.capture=False
    config.pool.balance_poll_seconds=0
    config.pool.acquire_wait_seconds=0
    config.server.admin_key='test-admin'
    config.server.client_keys=[ClientKey(key='test-client')]
    return config

@pytest.fixture
def service(cfg):
    pool=PoolManager(cfg.pool,[Account(id='one',access_token='test-token')])
    store=Store(cfg.store.sqlite_path)
    class FakeTokens:
        async def refresh(self, account, force=False): return False
    svc=ChatService(cfg,pool,FakeTokens(),FakeClient(),Registry(),store,JsonlCapture(cfg.logging.capture_dir,False))
    yield svc
    store.close()

@pytest.mark.parametrize('effort,expected', [('minimal','low'),('low','low'),('medium','high'),('high','high'),('xhigh','max'),('max','max')])
def test_effort_mapping(effort,expected):
    body=build_body({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}],'reasoning_effort':effort})
    assert body['output_config']['effort']==expected
    assert body['thinking']=={'type':'enabled'}
    assert body['max_tokens']==128000
    assert 'stream_options' not in body

def test_native_blocks_preserved():
    native={'model':'glm-5.3','messages':[{'role':'assistant','content':[
        {'type':'thinking','thinking':'previous','signature':'opaque'},
        {'type':'tool_use','id':'call','name':'read','input':{'path':'a'}}]},
        {'role':'user','content':[{'type':'tool_result','tool_use_id':'call','content':'result'}]}],
        'system':[{'type':'text','text':'system','cache_control':{'type':'ephemeral'}}],
        'thinking':{'type':'enabled'},'output_config':{'effort':'high'},'metadata':{'user_id':'opaque'}}
    body=build_body({'model':'GLM-5.3','_anthropic':native})
    assert body['messages']==native['messages']
    assert body['system'][2:]==native['system']
    assert body['metadata']==native['metadata']
    assert native['model']=='glm-5.3'

def test_tools_images_and_stop():
    body=build_body({'model':'GLM-5.3','messages':[
        {'role':'system','content':'system'},
        {'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,AAAA'}}]},
        {'role':'assistant','content':None,'tool_calls':[{'id':'call','type':'function','function':{'name':'read','arguments':'{"path":"a"}'}}]},
        {'role':'tool','tool_call_id':'call','content':'one'},
        {'role':'tool','tool_call_id':'other','content':'two'}],
        'tools':[{'type':'function','function':{'name':'read','parameters':{'type':'object'}}}],
        'tool_choice':'required','parallel_tool_calls':False,'stop':'END'})
    assert body['system'][2]['text']=='system'
    assert body['messages'][0]['content'][0]['source']['type']=='base64'
    assert body['messages'][1]['content'][0]['input']=={'path':'a'}
    assert len(body['messages'][2]['content'])==2
    assert body['tool_choice']=={'type':'any','disable_parallel_tool_use':True}
    assert body['stop_sequences']==['END']

def test_zcode_headers():
    headers=build_headers('Bearer fake-token',Fingerprint(device_mid='test-device'))
    assert headers['authorization']=='Bearer fake-token'
    assert headers['x-api-key']=='fake-token'
    assert headers['anthropic-beta']=='mid-conversation-system-2026-04-07'
    assert headers['x-device-mid']=='test-device'
    assert headers['http-referer']=='https://zcode.z.ai'
    assert 'x-device-mid' not in build_headers('fake-token',Fingerprint(device_mid='test-device'),include_device_mid=False)
    assert headers['x-zcode-app-version']=='3.14.4'
    assert 'x-core-version' not in headers

def test_multiline_sse():
    assert iter_sse_events('event: ping\r\ndata: {\r\ndata: "type":"ping"\r\ndata: }\r\n\r\n')==[{'type':'ping'}]

@pytest.mark.parametrize('limit', [0, -1, False, '32'])
def test_invalid_token_limits_rejected(limit):
    with pytest.raises(ValueError):
        build_body({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}],'max_tokens':limit})

@pytest.mark.parametrize('fixture', sorted((Path(__file__).parent/'fixtures').glob('*.json')))
def test_six_capture_variants(fixture):
    captured=json.loads(fixture.read_text(encoding='utf-8'))
    native=MessageAccumulator()
    translator=OpenAITranslator(captured['model'])
    chunks=[]
    for event in captured['events']:
        native.feed(event)
        chunks.extend(translator.feed(event))
    result=native.finish()
    assert native.stopped
    assert result['stop_reason']=='end_turn'
    assert any(block['type']=='text' for block in result['content'])
    assert chunks[-1]['usage']==usage_openai(result['usage'])
    assert build_body({'model':captured['model'],'messages':[{'role':'user','content':'hi'}],
                       'reasoning_effort':captured['effort']})['output_config']['effort']==captured['effort']

@pytest.mark.asyncio
async def test_native_complete_and_usage(service):
    service.client=FakeClient(sample_sse(tools=True))
    result=await service.complete({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3',dialect='anthropic')
    message=json.loads(result.raw)
    assert message['content'][0]['signature']=='opaque_signature'
    assert message['content'][2]['input']=={'path':'test.txt'}
    assert message['content'][3]['name']=='write'
    assert message['stop_reason']=='tool_use'
    assert service.client.responses[0].closed
    assert (await service.pool.all())[0].in_flight==0
    assert service.store.summary()['total_tokens']==4

@pytest.mark.asyncio
async def test_openai_stream_tools_and_reasoning(service):
    service.client=FakeClient(sample_sse(tools=True))
    handle=await service.open_stream({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3')
    raw=b''.join([x async for x in service.stream_from(handle,model='GLM-5.3')])
    events=iter_sse_events(raw.decode())
    calls=[call for e in events if isinstance(e,dict) for ch in e.get('choices',[]) for call in ch['delta'].get('tool_calls',[])]
    assert {c['index'] for c in calls}=={0,1}
    assert 'reasoning_content' in raw.decode()
    assert events[-1]=='[DONE]'
    assert (await service.pool.all())[0].in_flight==0

@pytest.mark.asyncio
@pytest.mark.parametrize('status,code', [(401,'auth'),(402,'11113'),(429,'rate_limit_error'),(500,'2007')])
async def test_failover(service,status,code):
    second=Account(id='two',access_token='second-token')
    await service.pool.upsert(second)
    service.client=FakeClient(responses=[FakeResponse(json.dumps({'code':code,'msg':'rejected'}).encode(),status),FakeResponse(sample_sse())])
    result=await service.complete({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3')
    assert result.account.id=='two'
    assert all(a.in_flight==0 for a in await service.pool.all())
    assert all(r.closed for r in service.client.responses)
    assert second.state==AccountState.READY

@pytest.mark.asyncio
async def test_client_error_does_not_rotate(service):
    await service.pool.upsert(Account(id='two',access_token='token2'))
    service.client=FakeClient(responses=[FakeResponse(b'{"error":{"type":"invalid_request_error","message":"bad body"}}',400)])
    with pytest.raises(UpstreamFailure) as err:
        await service.open_stream({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3')
    assert err.value.status==400
    assert len(service.client.calls)==1
    assert all(a.in_flight==0 for a in await service.pool.all())

@pytest.mark.asyncio
async def test_initial_sse_error_rotates_before_response(service):
    await service.pool.upsert(Account(id='two',access_token='token2'))
    service.client=FakeClient(responses=[FakeResponse(frame({'type':'error','error':{'code':11113,'message':'Insufficient balance'}})),FakeResponse(sample_sse())])
    result=await service.complete({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3')
    assert result.account.id=='two'
    first=await service.pool.find('one')
    assert first.is_model_capped('GLM-5.3')
    assert not first.is_model_capped('GLM-5.3-Flash')

@pytest.mark.asyncio
async def test_optional_model_fallback_only_on_model_caps(service):
    service.cfg.models.auto_free_fallback.enabled=True
    service.cfg.models.auto_free_fallback.chain=['GLM-5.3','GLM-5.3-Flash']
    account=await service.pool.find('one')
    await service.pool.cap_model(account,'GLM-5.3',30,reason='MODEL_CAPPED',message='quota exhausted')
    result=await service.complete({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3')
    assert service.client.calls[0][1]['model']=='GLM-5.3-Flash'
    assert result.status==200
    assert b'GLM-5.3-Flash' in result.raw

@pytest.mark.asyncio
async def test_http200_business_quota_error_rotates(service):
    await service.pool.upsert(Account(id='two',access_token='second-token'))
    failed=FakeResponse(b'{"code":11113,"msg":"Insufficient balance"}',200)
    failed.headers['content-type']='application/json'
    service.client=FakeClient(responses=[failed,FakeResponse(sample_sse())])
    result=await service.complete({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3')
    assert result.account.id=='two'
    assert (await service.pool.find('one')).is_model_capped('GLM-5.3')


def unusual_activity_response():
    response=FakeResponse(b'{"error":{"message":"request has been blocked due to unusual activity.","type":"api_error","code":"3012"}}',405)
    response.headers['content-type']='application/json'
    return response


@pytest.mark.asyncio
async def test_unusual_activity_parks_model_and_rotates_accounts(service):
    await service.pool.upsert(Account(id='two',access_token='second-token'))
    service.client=FakeClient(responses=[unusual_activity_response(),FakeResponse(sample_sse())])
    result=await service.complete({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3')
    assert result.account.id=='two'
    first=await service.pool.find('one')
    # parked for the blocked model only; the account stays healthy and READY
    assert first.is_model_capped('GLM-5.3')
    assert not first.is_model_capped('GLM-5.3-Flash')
    assert first.state is AccountState.READY
    assert all(a.in_flight==0 for a in await service.pool.all())


@pytest.mark.asyncio
async def test_unusual_activity_all_accounts_park_and_report_real_error(service):
    await service.pool.upsert(Account(id='two',access_token='second-token'))
    service.client=FakeClient(responses=[unusual_activity_response(),unusual_activity_response()])
    with pytest.raises(UpstreamFailure) as err:
        await service.complete({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3')
    assert err.value.status==405 and err.value.code=='3012'
    assert len(service.client.calls)==2
    accounts=await service.pool.all()
    assert all(a.is_model_capped('GLM-5.3') for a in accounts)
    assert all(not a.is_model_capped('GLM-5.3-Flash') for a in accounts)
    assert all(a.state is AccountState.READY and a.in_flight==0 for a in accounts)


@pytest.mark.asyncio
async def test_unusual_activity_drives_model_fallback(service):
    service.cfg.models.auto_free_fallback.enabled=True
    service.cfg.models.auto_free_fallback.chain=['GLM-5.3','GLM-5.3-Flash']
    service.client=FakeClient(responses=[unusual_activity_response(),FakeResponse(sample_sse())])
    result=await service.complete({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3')
    assert result.status==200
    assert service.client.calls[0][1]['model']=='GLM-5.3'
    assert service.client.calls[1][1]['model']=='GLM-5.3-Flash'

def test_import_all_coding_plan_jwts_excludes_api_keys(tmp_path):
    import base64
    from zcode_gateway.zcode_accounts import read_credentials, import_credentials
    def jwt(ident):
        claims=base64.urlsafe_b64encode(json.dumps({'user_id':ident,'sub':ident,'iat':1}).encode()).decode().rstrip('=')
        return 'eyJhbGciOiJIUzI1NiJ9.'+claims+'.fake_signature'
    path=tmp_path/'credentials.json'
    path.write_text(json.dumps({'zcodejwttoken':jwt('individual'),
        'oauth:zai:user_info':json.dumps({'email':'test@example.com','name':'Test'}),
        'oauth:zai:access_token':'oauth-secret-not-for-import',
        'account-provider:coding-plan:account:team:api-key':jwt('team'),
        'account-provider:coding-plan:account:individual:api-key':jwt('individual'),
        'account-provider:coding-plan:account:custom:api-key':'api-key-value'}),encoding='utf-8')
    accounts=read_credentials(path)
    assert {a['account_id'] for a in accounts}=={'individual','team'}
    assert next(a for a in accounts if a['account_id']=='individual')['email']=='test@example.com'
    assert next(a for a in accounts if a['account_id']=='team')['email']==''
    result=import_credentials(path,tmp_path/'out')
    assert result['written']==2
    for file in (tmp_path/'out').glob('*.txt'):
        assert 'oauth-secret-not-for-import' not in file.read_text()

@pytest.mark.asyncio
async def test_metadata_uses_account_device_identity(service):
    account=await service.pool.find('one')
    account.notes['device_mid']='account-device'
    await service.complete({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3')
    headers,body=service.client.calls[0]
    metadata=json.loads(body['metadata']['user_id'])
    assert metadata['device_id']=='account-device'
    assert metadata['session_id']==headers['x-session-id']
    assert headers['http-referer']=='https://zcode.z.ai'
    assert 'x-device-mid' not in headers

@pytest.mark.asyncio
async def test_mid_stream_error_not_replayed(service):
    raw=frame(sample_events()[0])+frame(sample_events()[5])+frame(sample_events()[6])+frame({'type':'error','error':{'type':'api_error','message':'oops'}})
    service.client=FakeClient(raw)
    h=await service.open_stream({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3')
    out=b''.join([x async for x in service.stream_from(h,model='GLM-5.3')])
    assert b'hello' in out and b'oops' in out
    assert len(service.client.calls)==1
    assert h.closed and h.account.in_flight==0
    assert service.store.summary()['ok']==0

@pytest.mark.asyncio
async def test_truncated_nonstream_fails(service):
    service.client=FakeClient(frame(sample_events()[0]))
    with pytest.raises(UpstreamFailure) as err:
        await service.complete({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3')
    assert err.value.code=='truncated_stream'
    assert service.client.responses[0].closed
    assert (await service.pool.all())[0].in_flight==0

@pytest.mark.asyncio
async def test_early_client_disconnect_releases(service):
    h=await service.open_stream({'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}]},model='GLM-5.3')
    generator=service.stream_from(h,model='GLM-5.3')
    await anext(generator)
    await generator.aclose()
    assert h.account.in_flight==0 and h.closed


def test_quota_buckets_plan_expiry_and_model_routing(cfg):
    now=time.time()
    data={'plans':[{'user_plan_id':'p','status':'active','starts_at':now-1,'ends_at':now+100}],
          'balances':[{'user_plan_id':'p','show_name':'GLM-5.3','remaining_units':0,'total_units':10,'used_units':10,'period_end':now+30},
                      {'user_plan_id':'p','show_name':'GLM-5.3-Flash','remaining_units':8,'total_units':10,'used_units':2,'period_end':now+30}]}
    plans,quotas=parse_balance(data,now)
    account=Account(id='one',access_token='test',notes={'plans':plans,'quotas':quotas})
    router=Router(cfg.pool)
    assert not router.candidates([account],set(),model='GLM-5.3')
    assert router.candidates([account],set(),model='GLM-5.3-Flash')==[account]
    assert account.model_eligible('GLM-5.3',now+31)
    assert not account.model_eligible('GLM-5.3-Flash',now+101)


def test_quota_aware_uses_requested_model(cfg):
    cfg.pool.strategy='quota_aware'
    one=Account(id='one',access_token='a',notes={'quotas':{'GLM-5.3':{'remaining':1}}})
    two=Account(id='two',access_token='b',notes={'quotas':{'GLM-5.3':{'remaining':9}}})
    router=Router(cfg.pool)
    assert router.acquire([one,two],set(),model='GLM-5.3') is two


def test_drain_first_reuses_one_account_until_it_is_capped(cfg):
    cfg.pool.strategy='drain_first'
    one=Account(id='one',access_token='a',notes={'quotas':{'GLM-5.3':{'remaining':5}}})
    two=Account(id='two',access_token='b',notes={'quotas':{'GLM-5.3':{'remaining':100}}})
    router=Router(cfg.pool)
    # the account nearest exhaustion is drained first and stays selected
    assert router.acquire([one,two],set(),model='GLM-5.3') is one
    assert router.acquire([one,two],set(),model='GLM-5.3') is one
    # once it is excluded (capped/exhausted), the next account takes over
    assert router.acquire([one,two],{'one'},model='GLM-5.3') is two


def test_drain_first_unknown_quota_keeps_pool_order(cfg):
    cfg.pool.strategy='drain_first'
    one=Account(id='one',access_token='a')
    two=Account(id='two',access_token='b')
    router=Router(cfg.pool)
    assert router.acquire([one,two],set(),model='GLM-5.3') is one


def test_aes256_gcm_vector_and_tamper():
    key,iv=bytes(32),bytes(12)
    cipher=bytes.fromhex('cea7403d4d606b6e074ec5d3baf39d18')
    tag=bytes.fromhex('d0d1c8a799996bf0265b98b5d48ab919')
    assert decrypt_aes_gcm(key,iv,cipher,tag)==bytes(16)
    with pytest.raises(ValueError):decrypt_aes_gcm(key,iv,cipher,bytes(16))

@pytest.mark.asyncio
async def test_oauth_headers_poll_and_snapshot(tmp_path):
    calls=[]
    def route(req):
        calls.append(req)
        if req.url.path.endswith('/init'):
            token=req.headers['authorization'].removeprefix('Bearer ')
            assert len(token)==64
            assert json.loads(req.content)=={'provider':'zai'}
            return httpx.Response(200,json={'code':0,'data':{'flow_id':'flow','poll_token':token,'authorize_url':'https://chat.z.ai/api/oauth/authorize?state=test','expires_at':time.time()+10,'poll_interval_sec':1}})
        assert req.headers['authorization']==calls[0].headers['authorization']
        assert 'x-device-mid' in req.headers
        return httpx.Response(200,json={'code':0,'data':{'status':'ready','token':'fake-jwt','user':{'user_id':'one','email':'test@example.com','name':'Test'}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
        accounts=[]
        async def added(a):accounts.append(a)
        login=DeviceLogin(tmp_path,client=client,on_account=added)
        await login.run()
    assert login.state=='done'
    assert accounts[0].expires_at==0
    assert login.snapshot_path.is_file()
    assert 'fake-jwt' not in json.dumps(login.status())
    assert login._poll_token is None

@pytest.mark.asyncio
async def test_login_cancel_pending(tmp_path):
    login=DeviceLogin(tmp_path)
    login._expires_at=time.time()+10
    login.cancel()
    assert await login._poll() is None
    assert login.state=='cancelled'
    await login.client.aclose()


def test_api_native_and_openai_end_to_end(cfg):
    cfg.models.aliases={'glm-5.3':'GLM-5.3','glm-5.3-flash':'GLM-5.3-Flash'}
    with TestClient(create_app(cfg)) as client:
        state=client.app.state.app_state
        state.pool._accounts['one']=Account(id='one',access_token='fake')
        state.service.client=FakeClient(responses=[FakeResponse(sample_sse()),FakeResponse(sample_sse()),FakeResponse(sample_sse())])
        headers={'Authorization':'Bearer test-client'}
        body={'model':'glm-5.3','messages':[{'role':'user','content':'hi'}]}
        native=client.post('/v1/messages',json=body,headers=headers)
        assert native.status_code==200 and native.json()['content'][0]['signature']=='opaque_signature'
        openai=client.post('/v1/chat/completions',json=body,headers=headers)
        assert openai.status_code==200
        assert openai.json()['choices'][0]['message']['content']=='hello'
        assert openai.json()['choices'][0]['message']['reasoning']=='think 😀'
        streamed=client.post('/v1/messages',json={**body,'stream':True},headers=headers)
        assert 'event: message_stop' in streamed.text
        assert state.pool._accounts['one'].in_flight==0
        assert client.post('/v1/chat/completions',json=body).status_code==401
        models=client.get('/v1/models',headers=headers).json()['data']
        assert {m['id'] for m in models}=={'GLM-5.3','GLM-5.3-Flash'}
        assert len(models)==2


def test_distinct_custom_alias_still_listed():
    registry=Registry(aliases={'my-model':'GLM-5.3','glm-5.3':'GLM-5.3'})
    assert {m['id'] for m in registry.catalogue()}=={'GLM-5.3','GLM-5.3-Flash','my-model'}
    assert registry.resolve('my-model')=='GLM-5.3'
    assert registry.resolve('glm-5.3')=='GLM-5.3'
    assert registry.resolve('glm-5.3-flash')=='GLM-5.3-Flash'


def busy_response(status=529):
    return FakeResponse(b'{"error":{"code":"1305","message":"model traffic too high"}}', status)


@pytest.mark.asyncio
async def test_overload_waits_and_reuses_single_account_without_blocking_flash(service, monkeypatch):
    delays = []
    async def wait(delay, disconnected=None):
        delays.append(delay)
        account = (await service.pool.all())[0]
        assert account.state is AccountState.READY
        assert account.in_flight == 0
        assert service.client.responses[-1].closed
        # A different model remains routable during the main model's backoff.
        flash = await service.pool.acquire(model='GLM-5.3-Flash')
        assert flash is account
        await service.pool.release(flash)
    monkeypatch.setattr('zcode_gateway.service.wait_for_retry', wait)
    monkeypatch.setattr('zcode_gateway.model_retry.random.uniform', lambda a, b: 1)
    service.client = FakeClient(responses=[busy_response() for _ in range(8)] + [FakeResponse(sample_sse())])
    result = await service.complete({'messages':[{'role':'user','content':'hi'}]}, model='GLM-5.3')
    assert result.status == 200
    assert delays == [2, 4, 8, 16, 32, 60, 60, 60]
    assert len(service.client.calls) == 9
    assert all(r.closed for r in service.client.responses)
    assert (await service.pool.all())[0].in_flight == 0


@pytest.mark.asyncio
async def test_overload_rotates_accounts_after_wait_without_cooling(service, monkeypatch):
    service.cfg.pool.strategy = 'least_in_flight'
    await service.pool.upsert(Account(id='two', access_token='token-two'))
    waits = []
    async def wait(delay, disconnected=None): waits.append(delay)
    monkeypatch.setattr('zcode_gateway.service.wait_for_retry', wait)
    service.client = FakeClient(responses=[busy_response(), busy_response(), busy_response(), FakeResponse(sample_sse())])
    await service.complete({'messages':[{'role':'user','content':'hi'}]}, model='GLM-5.3')
    assert len(waits) == 3
    assert [h['x-api-key'] for h, b in service.client.calls] == ['test-token', 'token-two', 'test-token', 'token-two']
    assert all(a.state is AccountState.READY and a.in_flight == 0 and not a.model_caps for a in await service.pool.all())


@pytest.mark.asyncio
async def test_persistent_overload_has_bounded_attempts_and_preserves_real_error(service, monkeypatch):
    waits = []
    async def wait(delay, disconnected=None): waits.append(delay)
    monkeypatch.setattr('zcode_gateway.service.wait_for_retry', wait)
    service.client = FakeClient(responses=[busy_response() for _ in range(11)])
    with pytest.raises(UpstreamFailure) as err:
        await service.open_stream({'messages':[{'role':'user','content':'hi'}]}, model='GLM-5.3')
    assert err.value.status == 529 and err.value.code == '1305'
    assert err.value.message == 'model traffic too high'
    assert len(waits) == 10 and len(service.client.calls) == 11
    assert all(r.closed for r in service.client.responses)
    assert all(a.in_flight == 0 and a.state is AccountState.READY for a in await service.pool.all())


@pytest.mark.asyncio
async def test_initial_sse_overload_retries_before_headers(service, monkeypatch):
    async def wait(delay, disconnected=None): pass
    monkeypatch.setattr('zcode_gateway.service.wait_for_retry', wait)
    service.client = FakeClient(responses=[FakeResponse(frame({'type':'error','error':{'type':'overloaded_error','message':'busy'}})), FakeResponse(sample_sse())])
    result = await service.complete({'messages':[{'role':'user','content':'hi'}]}, model='GLM-5.3')
    assert result.status == 200 and len(service.client.calls) == 2
    assert all(r.closed for r in service.client.responses)


@pytest.mark.asyncio
async def test_overload_after_output_is_not_replayed(service):
    raw = b''.join(frame(e) for e in sample_events()[:7]) + frame({'type':'error','error':{'code':'1305','message':'busy'}})
    service.client = FakeClient(raw)
    handle = await service.open_stream({'messages':[{'role':'user','content':'hi'}]}, model='GLM-5.3')
    result = b''.join([part async for part in service.stream_from(handle, model='GLM-5.3')])
    assert b'hello' in result and b'1305' in result
    assert len(service.client.calls) == 1
    assert service.client.responses[0].closed
    assert handle.account.state is AccountState.READY and handle.account.in_flight == 0


@pytest.mark.asyncio
async def test_overload_respects_retry_after(service, monkeypatch):
    waits = []
    async def wait(delay, disconnected=None): waits.append(delay)
    monkeypatch.setattr('zcode_gateway.service.wait_for_retry', wait)
    response = busy_response()
    response.headers['retry-after'] = '7'
    service.client = FakeClient(responses=[response, FakeResponse(sample_sse())])
    await service.complete({'messages':[{'role':'user','content':'hi'}]}, model='GLM-5.3')
    assert waits == [7]


@pytest.mark.asyncio
async def test_cancel_during_overload_backoff_releases_reservation(service, monkeypatch):
    from zcode_gateway.model_retry import wait_for_retry
    async def disconnected(): return True
    async def wait(delay, ignored=None): await wait_for_retry(delay, disconnected)
    monkeypatch.setattr('zcode_gateway.service.wait_for_retry', wait)
    service.client = FakeClient(responses=[busy_response()])
    with pytest.raises(asyncio.CancelledError):
        await service.open_stream({'messages':[{'role':'user','content':'hi'}]}, model='GLM-5.3')
    assert len(service.client.calls) == 1
    assert service.client.responses[0].closed
    assert all(a.in_flight == 0 and a.state is AccountState.READY for a in await service.pool.all())


@pytest.mark.parametrize('path', ['/v1/chat/completions', '/v1/messages'])
@pytest.mark.parametrize('stream', [False, True])
def test_api_retries_busy_model_in_both_dialects(cfg, path, stream):
    cfg.upstream.overload_base_delay_seconds = 0
    with TestClient(create_app(cfg)) as client:
        state = client.app.state.app_state
        state.pool._accounts['one'] = Account(id='one', access_token='fake')
        state.service.client = FakeClient(responses=[busy_response(), FakeResponse(sample_sse())])
        response = client.post(path, headers={'Authorization':'Bearer test-client'},
            json={'model':'GLM-5.3','messages':[{'role':'user','content':'hi'}],'stream':stream})
        assert response.status_code == 200
        assert len(state.service.client.calls) == 2
        assert state.pool._accounts['one'].in_flight == 0
