"""ZCode's captured Anthropic wire contract and OpenAI adapter."""
from __future__ import annotations

import copy
import json
import time
import uuid
from .zcode_bootstrap import bootstrap_system


def reasoning(payload: dict, default_effort: str = 'high') -> dict:
    thinking = payload.get('thinking')
    effort = (payload.get('output_config') or {}).get('effort')
    effort = effort or payload.get('reasoning_effort') or payload.get('reasoningEffort')
    effort = effort or (payload.get('reasoning') or {}).get('effort')
    disabled = (payload.get('enable_thinking') is False
                or (payload.get('reasoning') or {}).get('enabled') is False
                or (isinstance(thinking, dict) and thinking.get('type') == 'disabled')
                or effort in ('none', 'off', 'disabled'))
    if disabled:
        return {'thinking': {'type': 'disabled'}}
    mapped = {'minimal': 'low', 'low': 'low', 'medium': 'high', 'high': 'high',
              'xhigh': 'max', 'extra': 'max', 'max': 'max'}.get(effort or default_effort)
    if mapped is None:
        raise ValueError('reasoning effort must be low, medium, high, max, or none')
    return {'thinking': {'type': 'enabled'}, 'output_config': {'effort': mapped}}


def content_blocks(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    if not isinstance(content, list):
        raise ValueError('message content must be a string or a list of blocks')
    out = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError('content blocks must be objects')
        kind = part.get('type')
        if kind in ('text', 'input_text'):
            out.append({'type': 'text', 'text': part.get('text', '')})
        elif kind == 'image_url':
            image = part.get('image_url')
            url = image.get('url') if isinstance(image, dict) else image
            if not isinstance(url, str):
                raise ValueError('image_url.url must be a string')
            if url.startswith('data:'):
                header, sep, data = url.partition(',')
                if not sep or ';base64' not in header:
                    raise ValueError('images must use a base64 data URL')
                source = {'type': 'base64', 'media_type': header[5:].split(';')[0], 'data': data}
            else:
                source = {'type': 'url', 'url': url}
            out.append({'type': 'image', 'source': source})
        elif kind in ('image', 'tool_use', 'tool_result', 'thinking', 'redacted_thinking'):
            out.append(copy.deepcopy(part))
        else:
            raise ValueError(f'unsupported content block type: {kind}')
    return out


def _strip_image_blocks(messages) -> None:
    """Drop image blocks (and empty image-only tool_result contents) from an
    Anthropic-shape message list, in place. Used for non-vision models: the
    upstream would 400 the whole request; the text proceeds instead."""
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get('content')
        if not isinstance(content, list):
            continue
        message['content'] = [b for b in content
                              if not (isinstance(b, dict) and b.get('type') == 'image')]


def _strip_image_blocks_system(system) -> None:
    pass  # system is always text-only; images there already 400 in content_blocks


def build_body(payload: dict, default_max_tokens=128000, model_caps=None,
               default_effort='high') -> dict:
    """Native requests preserve blocks, caching, signatures and metadata exactly.

    `model_caps` (optional): the registry's per-model capability dict — when
    present, image blocks are stripped for non-vision models (the request
    proceeds text-only instead of a guaranteed upstream 400) and max_tokens is
    clamped to the model's real output cap."""
    vision = None
    if isinstance(model_caps, dict):
        vision = bool(model_caps.get('vision'))
    if payload.get('_anthropic'):
        body = copy.deepcopy(payload['_anthropic'])
        body['model'] = payload['model']
        # capability gate applies to the native path too: an Anthropic client
        # can send image blocks to a text-only model just like an OpenAI one
        if vision is False:
            _strip_image_blocks(body.get('messages') or [])
            _strip_image_blocks_system(body.get('system'))
    else:
        body = {'model': payload['model'], 'messages': []}
        system = []
        for message in payload.get('messages', []):
            role = message.get('role')
            blocks = content_blocks(message.get('content'))
            if role in ('system', 'developer'):
                if any(block.get('type') != 'text' for block in blocks):
                    raise ValueError('system messages must contain text')
                system.extend(blocks)
                continue
            if role == 'tool':
                blocks = [{'type': 'tool_result', 'tool_use_id': message.get('tool_call_id'),
                           'content': blocks}]
                role = 'user'
            elif role == 'assistant':
                for call in message.get('tool_calls') or []:
                    function = call.get('function') or {}
                    try:
                        args = function.get('arguments') or '{}'
                        args = json.loads(args) if isinstance(args, str) else args
                    except (ValueError, TypeError) as exc:
                        raise ValueError('tool call arguments must be valid JSON') from exc
                    blocks.append({'type': 'tool_use', 'id': call.get('id'),
                                   'name': function.get('name'), 'input': args})
            if role not in ('user', 'assistant'):
                raise ValueError(f'unsupported message role: {role}')
            if vision is False:
                blocks = [b for b in blocks if b.get('type') != 'image']
            # Anthropic expects tool results in user messages and alternating turns.
            if body['messages'] and body['messages'][-1]['role'] == role:
                body['messages'][-1]['content'].extend(blocks)
            else:
                body['messages'].append({'role': role, 'content': blocks})
        if system:
            body['system'] = system
        if payload.get('tools'):
            body['tools'] = []
            for tool in payload['tools']:
                if tool.get('type') != 'function':
                    raise ValueError('only function tools are supported')
                fn = tool['function']
                body['tools'].append({'name': fn['name'], 'description': fn.get('description', ''),
                                      'input_schema': fn.get('parameters') or {'type': 'object'}})
            choice = payload.get('tool_choice')
            if choice is None:
                # explicit JSON null (or absent) — let the upstream default.
                # The old code skipped setting the key entirely and the
                # parallel_tool_calls branch below then crashed on KeyError.
                body['tool_choice'] = {'type': 'auto'} if payload.get('parallel_tool_calls') is False else None
                if body['tool_choice'] is None:
                    body.pop('tool_choice', None)
            elif isinstance(choice, str):
                body['tool_choice'] = {'type': {'required': 'any'}.get(choice, choice)}
            elif isinstance(choice, dict):
                body['tool_choice'] = {'type': 'tool', 'name': choice['function']['name']}
            if payload.get('parallel_tool_calls') is False and body.get('tool_choice') is not None:
                body['tool_choice']['disable_parallel_tool_use'] = True
        for key in ('temperature', 'top_p', 'top_k', 'metadata'):
            if payload.get(key) is not None:
                body[key] = copy.deepcopy(payload[key])
        if payload.get('stop') is not None:
            stop = payload['stop']
            body['stop_sequences'] = [stop] if isinstance(stop, str) else stop
        if payload.get('response_format') is not None:
            raise ValueError('response_format is not supported by the captured ZCode API')
        body.update(reasoning(payload, default_effort))
    limit = body.get('max_tokens')
    if limit is None:
        limit = payload.get('max_tokens')
    if limit is None:
        limit = payload.get('max_completion_tokens')
    if limit is None:
        limit = default_max_tokens
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError('max_tokens must be a positive integer')
    # clamp to the model's real output cap so a client asking for 500k on a
    # 128k model gets 128k instead of an upstream rejection
    if isinstance(model_caps, dict):
        cap = int(model_caps.get('max_output_tokens', 0) or 0)
        if cap > 0:
            limit = min(limit, cap)
    body['max_tokens'] = limit
    body['stream'] = True
    if 'thinking' not in body:
        body.update(reasoning(body, default_effort))
    if (body.get('thinking') or {}).get('type') == 'disabled':
        raise ValueError('ZCode GLM models always use reasoning. Use reasoning_effort low, high, or max; disabling reasoning is not supported.')
    body['system'] = bootstrap_system(body.get('system'))
    return body


def usage_openai(usage: dict) -> dict:
    cache = int(usage.get('cache_read_input_tokens') or 0)
    prompt = int(usage.get('input_tokens') or 0) + cache + int(usage.get('cache_creation_input_tokens') or 0)
    output = int(usage.get('output_tokens') or 0)
    return {'prompt_tokens': prompt, 'completion_tokens': output, 'total_tokens': prompt + output,
            'prompt_tokens_details': {'cached_tokens': cache}}


class MessageAccumulator:
    def __init__(self):
        self.message = {'id': '', 'type': 'message', 'role': 'assistant', 'content': [],
                        'stop_reason': None, 'stop_sequence': None, 'usage': {}}
        self.blocks = {}
        self.partial_json = {}
        self.stopped = False

    def feed(self, event: dict):
        kind = event.get('type')
        if kind == 'message_start':
            self.message.update(copy.deepcopy(event['message']))
        elif kind == 'content_block_start':
            self.blocks[event['index']] = copy.deepcopy(event['content_block'])
        elif kind == 'content_block_delta':
            index, delta = event['index'], event['delta']
            block = self.blocks.get(index)
            if block is None:
                raise ValueError('delta without content block')
            dtype = delta.get('type')
            key = {'text_delta': 'text', 'thinking_delta': 'thinking', 'signature_delta': 'signature'}.get(dtype)
            if key:
                block[key] = block.get(key, '') + delta.get(key, '')
            elif dtype == 'input_json_delta':
                self.partial_json[index] = self.partial_json.get(index, '') + delta.get('partial_json', '')
        elif kind == 'message_delta':
            self.message.update(event.get('delta') or {})
            self.message['usage'].update(event.get('usage') or {})
        elif kind == 'message_stop':
            self.stopped = True

    def finish(self):
        for index, partial in self.partial_json.items():
            self.blocks[index]['input'] = json.loads(partial or '{}')
        self.message['content'] = [self.blocks[index] for index in sorted(self.blocks)]
        return self.message


class OpenAITranslator:
    """Track Anthropic block indices separately from OpenAI tool-call indices."""
    def __init__(self, model: str):
        self.model, self.id = model, 'chatcmpl-' + uuid.uuid4().hex[:24]
        self.created = int(time.time())
        self.tools = {}
        self.usage = {}

    def chunk(self, delta, finish=None):
        return {'id': self.id, 'object': 'chat.completion.chunk', 'created': self.created,
                'model': self.model, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}

    def feed(self, event):
        kind = event.get('type')
        if kind == 'message_start':
            msg = event['message']
            self.id = 'chatcmpl-' + msg['id']
            self.usage.update(msg.get('usage') or {})
            return [self.chunk({'role': 'assistant', 'content': ''})]
        if kind == 'content_block_start':
            block = event['content_block']
            if block.get('type') == 'tool_use':
                idx = len(self.tools)
                self.tools[event['index']] = idx
                initial = block.get('input')
                return [self.chunk({'tool_calls': [{'index': idx, 'id': block['id'], 'type': 'function',
                         'function': {'name': block['name'], 'arguments': json.dumps(initial) if initial else ''}}]})]
            if block.get('text'):
                return [self.chunk({'content': block['text']})]
            if block.get('thinking'):
                return [self.chunk({'reasoning_content': block['thinking'], 'reasoning': block['thinking']})]
        if kind == 'content_block_delta':
            delta = event['delta']
            if delta['type'] == 'text_delta':
                return [self.chunk({'content': delta.get('text', '')})]
            if delta['type'] == 'thinking_delta':
                value = delta.get('thinking', '')
                return [self.chunk({'reasoning_content': value, 'reasoning': value})]
            if delta['type'] == 'input_json_delta':
                return [self.chunk({'tool_calls': [{'index': self.tools[event['index']],
                         'function': {'arguments': delta.get('partial_json', '')}}]})]
        if kind == 'message_delta':
            self.usage.update(event.get('usage') or {})
            stop = (event.get('delta') or {}).get('stop_reason')
            finish = {'end_turn': 'stop', 'stop_sequence': 'stop', 'max_tokens': 'length',
                      'tool_use': 'tool_calls', 'refusal': 'content_filter'}.get(stop, 'stop')
            return [self.chunk({}, finish)] if stop else []
        if kind == 'message_stop':
            chunk = self.chunk({})
            chunk['choices'] = []
            chunk['usage'] = usage_openai(self.usage)
            return [chunk]
        return []
