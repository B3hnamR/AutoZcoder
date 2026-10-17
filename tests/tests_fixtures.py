"""Offline ZCode wire fixtures. No network or real credentials."""
import json


def frame(event):
    return ('event: '+event['type']+'\r\ndata: '+json.dumps(event,ensure_ascii=False)+'\r\n\r\n').encode()


def sample_events(tools=False):
    events = [
        {'type':'message_start','message':{'id':'msg_test','type':'message','role':'assistant','model':'glm-5.3','content':[], 'usage':{'input_tokens':3,'output_tokens':0},'stop_reason':None,'stop_sequence':None}},
        {'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':'','signature':''}},
        {'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'think 😀'}},
        {'type':'content_block_delta','index':0,'delta':{'type':'signature_delta','signature':'opaque_signature'}},
        {'type':'content_block_stop','index':0},
        {'type':'content_block_start','index':1,'content_block':{'type':'text','text':''}},
        {'type':'content_block_delta','index':1,'delta':{'type':'text_delta','text':'hello'}},
        {'type':'content_block_stop','index':1},
    ]
    if tools:
        for index, name in [(2,'read'), (3,'write')]:
            events.extend([
                {'type':'content_block_start','index':index,'content_block':{'type':'tool_use','id':f'tool_{index}','name':name,'input':{}}},
                {'type':'content_block_delta','index':index,'delta':{'type':'input_json_delta','partial_json':'{"path":'}},
                {'type':'content_block_delta','index':index,'delta':{'type':'input_json_delta','partial_json':'"test.txt"}'}},
                {'type':'content_block_stop','index':index},
            ])
    events.extend([
        {'type':'message_delta','delta':{'stop_reason':'tool_use' if tools else 'end_turn','stop_sequence':None},'usage':{'output_tokens':1}},
        {'type':'message_stop'}])
    return events


def sample_sse(tools=False): return b''.join(frame(e) for e in sample_events(tools))

class FakeResponse:
    def __init__(self, body, status=200, split=7):
        self.body, self.status_code, self.split = body, status, split
        self.headers, self.closed = {'content-type':'text/event-stream'}, False
    async def aread(self): return self.body
    async def aiter_bytes(self):
        for i in range(0,len(self.body),self.split): yield self.body[i:i+self.split]
    async def aclose(self): self.closed=True

class FakeClient:
    def __init__(self, body=None, responses=None):
        self.pending = list(responses or [FakeResponse(body or sample_sse())])
        self.responses, self.calls = [], []
    async def send(self, headers, body, stream):
        self.calls.append((headers, body))
        response = self.pending.pop(0)
        self.responses.append(response)
        return response
