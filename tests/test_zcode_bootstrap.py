import copy

import pytest

from zcode_gateway.zcode_bootstrap import ZCODE_IDENTITY, ZCODE_HARNESS, bootstrap_system
from zcode_gateway.zcode_protocol import build_body


def test_openai_bootstrap_keeps_caller_instruction_and_tools():
    payload={'model':'GLM-5.3-Flash', 'messages':[
        {'role':'system','content':'Answer in Persian.'},
        {'role':'user','content':'hi'}],
        'tools':[{'type':'function','function':{'name':'weather','parameters':{'type':'object'}}}]}
    original=copy.deepcopy(payload)
    body=build_body(payload)
    assert [b['text'] for b in body['system']]==[ZCODE_IDENTITY,ZCODE_HARNESS,'Answer in Persian.']
    assert body['messages']==[{'role':'user','content':[{'type':'text','text':'hi'}]}]
    assert [t['name'] for t in body['tools']]==['weather']
    assert payload==original


def test_existing_native_bootstrap_and_cache_markers_preserved():
    blocks=[{'type':'text','text':ZCODE_IDENTITY,'cache_control':{'type':'ephemeral'}},
            {'type':'text','text':ZCODE_HARNESS,'cache_control':{'type':'ephemeral'}},
            {'type':'text','text':'Caller context','cache_control':{'type':'ephemeral'}}]
    assert bootstrap_system(blocks)==blocks
    assert bootstrap_system(bootstrap_system(blocks))==blocks


def test_plain_native_system_kept_after_bootstrap():
    body=build_body({'model':'GLM-5.3','_anthropic':{'system':'Caller context','messages':[{'role':'user','content':'hi'}]}})
    assert body['system'][-1]=={'type':'text','text':'Caller context'}
    assert all('cache_control' not in block for block in body['system'])
    assert 'tools' not in body


@pytest.mark.parametrize('payload',[{'reasoning_effort':'none'},{'thinking':{'type':'disabled'}},
                                  {'enable_thinking':False},{'reasoning':{'enabled':False}}])
def test_unsupported_disabled_reasoning_has_clear_error(payload):
    with pytest.raises(ValueError,match='always use reasoning'):
        build_body({'model':'GLM-5.3-Flash','messages':[{'role':'user','content':'hi'}],**payload})
