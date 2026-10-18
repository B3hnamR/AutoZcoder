"""Shared pool, persistence and dashboard with a native ZCode transport."""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass

import httpx
from .aggregate import aggregate_openai_stream
from .pool import Account
from .model_retry import is_model_overload, overload_delay, retry_after_seconds, wait_for_retry
from .zcode_protocol import MessageAccumulator, OpenAITranslator, usage_openai
from .upstream import (build_headers, build_upstream_body, iter_sse_events, sse_frame,
                       parse_upstream_error, anthropic_error, openai_error, TransportError,
                       is_block_code)

log = logging.getLogger('zcode_gateway.service')

class NoAccountsAvailable(Exception): pass

class UpstreamFailure(Exception):
    def __init__(self, status, code, message, release_at=None, release_in_s=None, details=None, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.message = status, str(code), message
        self.release_at, self.release_in_s, self.details = release_at, release_in_s, details or {}
        self.retry_after = retry_after
    def extra(self):
        out = dict(self.details)
        if self.release_at: out['release_at'] = self.release_at
        if self.release_in_s is not None: out['release_in_s'] = self.release_in_s
        return out

@dataclass
class StreamHandle:
    account: Account
    headers: dict
    body: dict
    resp: object
    variant: str
    started: float
    events: object = None
    first: dict | None = None
    closed: bool = False

@dataclass
class CallResult:
    account: Account
    status: int
    raw: bytes
    duration_ms: float
    upstream_headers: dict


def _split_sse_frames(pending):
    frames = []
    while True:
        match = re.search(rb'\r\n\r\n|\n\n|\r\r', pending)
        if not match: return frames, pending
        frames.append(pending[:match.start()])
        pending = pending[match.end():]


def _sse_event(name, data):
    return (f'event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n').encode()


async def upstream_events(resp):
    pending = b''
    async for chunk in resp.aiter_bytes():
        pending += chunk
        frames, pending = _split_sse_frames(pending)
        if len(pending) > 4 * 1024 * 1024:
            raise UpstreamFailure(502, 'invalid_stream', 'upstream SSE frame exceeded 4 MB')
        for frame in frames:
            for event in iter_sse_events(frame.decode('utf-8', 'replace')):
                if isinstance(event, dict): yield event
    if pending.strip():
        for event in iter_sse_events(pending.decode('utf-8', 'replace')):
            if isinstance(event, dict): yield event


class ChatService:
    def __init__(self, cfg, pool, tokens, client, registry, store, capture):
        self.cfg, self.pool, self.tokens, self.client = cfg, pool, tokens, client
        self.registry, self.store, self.capture = registry, store, capture

    def _build(self, payload, variant='anthropic'):
        return build_upstream_body(payload, variant, self.cfg.models.default_max_tokens)

    async def _close(self, handle):
        if handle.closed: return
        handle.closed = True
        try:
            if handle.events is not None: await handle.events.aclose()
        finally:
            try: await handle.resp.aclose()
            finally: await self.pool.release(handle.account)

    async def _apply_error(self, account, model, status, code, message, headers=None):
        if is_model_overload(status, code):
            # A busy model is not evidence of a bad account or spent quota.
            # Keep this account available for other models and delayed retries.
            return
        # autopsy trail: keep the last errors for the dashboard "why" view
        try:
            account.error_history.append(
                {"ts": time.time(), "status": status, "code": str(code),
                 "message": str(message)[:200], "model": model})
        except Exception:
            pass
        if status == 401 or code == 'authentication_error':
            await self.pool.kill_after_strikes(account, 'ZCode token revoked; sign in again')
        elif status == 405 or is_block_code(code, status):
            # 405/3012 "unusual activity": not a quota and not a bad account.
            # Park only this model on this account and let the caller rotate to
            # the next account; the account keeps serving every other model.
            await self.pool.cap_model(account, model,
                                      self.cfg.pool.blocked_model_cooldown_seconds,
                                      reason='UNUSUAL_ACTIVITY', message=message)
        elif code == '11113' or status == 402:
            quota = account.notes.get('quotas', {}).get(model, {})
            seconds = max(1, quota.get('resets_at', 0) - time.time())
            if seconds <= 1: seconds = self.cfg.pool.cooldown_seconds
            await self.pool.cap_model(account, model, seconds, reason='11113', message=message)
        elif status == 429 or code == 'rate_limit_error':
            try: seconds = min(3600, max(1, float((headers or {}).get('retry-after', self.cfg.pool.cooldown_seconds))))
            except ValueError: seconds = self.cfg.pool.cooldown_seconds
            await self.pool.cap_model(account, model, seconds, reason='MODEL_CAPPED', message=message)
        elif status == 403:
            await self.pool.cap_model(account, model, 300, reason='ENTITLEMENT_ERROR', message=message)
        elif status == 451:
            # Geo/legal block tied to this account's egress context: park the
            # model on THIS account only and let the caller rotate to another
            # account (different IP) — the account itself stays healthy.
            await self.pool.cap_model(account, model, 600,
                                      reason='GEO_BLOCKED', message=message or 'HTTP 451')
        elif status >= 500:
            await self.pool.cool(account, reason='upstream_error')

    async def open_stream(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous', is_disconnected=None):
        fallback = self.cfg.models.auto_free_fallback
        chain = list(dict.fromkeys(self.registry.resolve(m, dialect) for m in fallback.chain))
        candidates = chain[chain.index(model):] if fallback.enabled and model in chain else [model]
        attempted = []
        for candidate in candidates:
            attempted.append(candidate)
            try:
                return await self._open_model_stream({**payload, 'model': candidate}, model=candidate,
                    variant=variant, dialect=dialect, client_key=client_key, is_disconnected=is_disconnected)
            except UpstreamFailure as exc:
                if exc.code not in ('11113', 'MODEL_CAPPED', 'MODEL_PARKED', 'INFERENCE_CAP_ERROR',
                                    'ENTITLEMENT_ERROR', 'UNUSUAL_ACTIVITY', '3012', '405', 'HTTP_405',
                                    'HTTP_451', 'http_451'):
                    raise
                if candidate == candidates[-1]:
                    exc.details['attempted_models'] = attempted
                    raise

    async def _open_model_stream(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous', is_disconnected=None):
        # Only retry before a StreamHandle is returned. Never replay output or tools.
        attempts = self.cfg.upstream.overload_max_attempts
        for attempt in range(attempts):
            if is_disconnected is not None and await is_disconnected():
                raise asyncio.CancelledError()
            try:
                return await self._open_model_stream_once(payload, model=model, variant=variant,
                    dialect=dialect, client_key=client_key)
            except UpstreamFailure as exc:
                if not is_model_overload(exc.status, exc.code):
                    raise
                delay = overload_delay(self.cfg.upstream, attempt + 1, exc.retry_after)
                if attempt + 1 == attempts:
                    exc.release_in_s = max(1, int(delay + .999))
                    raise
                log.info('ZCode model %s overloaded (%s); retry %s/%s in %.2fs',
                         model, exc.code, attempt + 1, attempts - 1, delay)
                await wait_for_retry(delay, is_disconnected)

    async def _open_model_stream_once(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous'):
        if not self.registry.knows(model):
            raise UpstreamFailure(400, 'unknown_model', f'unknown ZCode model: {model}')
        try: body = self._build(payload, variant)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise UpstreamFailure(400, 'invalid_request_error', str(exc)) from exc
        if not body.get('messages'):
            raise UpstreamFailure(400, 'invalid_request_error', 'at least one user or assistant message is required')
        supplied_metadata = 'metadata' in body
        target_account = (
            payload.get("_anthropic", {}).pop("account", None)
            or payload.get("_anthropic", {}).pop("account_id", None)
            or payload.get("account")
            or payload.get("account_id")
        )
        tried, last = set(), None
        for attempt in range(max(1, self.cfg.upstream.max_attempts)):
            account = await self.pool.acquire(tried, model=model, only_id=target_account)
            if account is None:
                if last: raise last
                reason = await self.pool.unavailable_reason(model)
                if reason:
                    raise UpstreamFailure(reason['status'], reason['code'], reason['message'],
                                          reason.get('release_at'), reason.get('release_in_s'))
                raise NoAccountsAvailable()
            headers = build_headers(account.access_token, self.cfg.upstream.fingerprint,
                                    device_mid=account.notes.get('device_mid'), include_device_mid=False)
            if not supplied_metadata:
                device_mid = account.notes.get('device_mid') or self.cfg.upstream.fingerprint.device_mid
                body['metadata'] = {'user_id': json.dumps({'device_id': device_mid,
                    'account_uuid': '', 'session_id': headers['x-session-id']}, separators=(',', ':'))}
            resp, handle, transferred = None, None, False
            try:
                if account.expires_in() <= 0 and not await self.tokens.refresh(account):
                    await self.pool.kill(account, 'ZCode token expired; sign in again')
                    last = UpstreamFailure(401, 'authentication_error', 'ZCode token expired; sign in again')
                    tried.add(account.id)
                    continue
                headers = build_headers(account.access_token, self.cfg.upstream.fingerprint,
                                        task_id=headers['x-session-id'], device_mid=account.notes.get('device_mid'),
                                        include_device_mid=False)
                resp = await self.client.send(headers, body, stream=True)
                if resp.status_code != 200 or 'application/json' in resp.headers.get('content-type', ''):
                    raw = await resp.aread()
                    info = parse_upstream_error(resp.status_code, raw)
                    status, code, message = resp.status_code, str(info['code']), info['message']
                    if status == 200:
                        status = 429 if code == '11113' else 502
                    if status == 401 and await self.tokens.refresh(account, force=True):
                        # Retry a newly imported token once, without a second pool reservation.
                        await resp.aclose()
                        headers = build_headers(account.access_token, self.cfg.upstream.fingerprint,
                                                task_id=headers['x-session-id'], device_mid=account.notes.get('device_mid'),
                                                include_device_mid=False)
                        resp = await self.client.send(headers, body, stream=True)
                        if resp.status_code == 200 and 'application/json' not in resp.headers.get('content-type', ''):
                            status = 200
                        else:
                            raw = await resp.aread()
                            info = parse_upstream_error(resp.status_code, raw)
                            status, code, message = resp.status_code, str(info['code']), info['message']
                            if status == 200:
                                status = 429 if code == '11113' else 502
                    if status != 200:
                        await self._apply_error(account, model, status, code, message, resp.headers)
                        last = UpstreamFailure(429 if code == '11113' else status, code, message,
                                               retry_after=retry_after_seconds(resp.headers))
                        self._record(account, headers, body, last.status, resp.headers,
                                     raw.decode('utf-8', 'replace'), 0, client_key, dialect, model, {})
                        if is_model_overload(status, code): raise last
                        if (status < 500 and status not in (401, 402, 403, 405, 429, 451)
                                and code != '11113' and not is_block_code(code, status)):
                            raise last
                        tried.add(account.id)
                        continue
                handle = StreamHandle(account, headers, body, resp, variant, time.time())
                handle.events = upstream_events(resp)
                # An immediate SSE error must reach the client before HTTP 200 is committed.
                while True:
                    first = await anext(handle.events, None)
                    if first is None:
                        raise UpstreamFailure(502, 'empty_stream', 'ZCode returned an empty event stream')
                    if first.get('type') == 'ping': continue
                    if first.get('type') == 'error':
                        info = first.get('error') or first
                        code, message = str(info.get('code') or info.get('type') or 'api_error'), info.get('message', 'ZCode stream failed')
                        status = 529 if is_model_overload(502, code) else (429 if code in ('11113', 'rate_limit_error') else (405 if is_block_code(code) else 502))
                        await self._apply_error(account, model, status, code, message)
                        last = UpstreamFailure(status, code, message)
                        if is_model_overload(status, code): raise last
                        tried.add(account.id)
                        break
                    if first.get('type') != 'message_start':
                        raise UpstreamFailure(502, 'invalid_stream', 'expected ZCode message_start event')
                    handle.first = first
                    result = handle
                    transferred = True
                    handle, resp = None, None
                    return result
            except (TransportError, httpx.HTTPError) as exc:
                await self.pool.cool(account, reason='transport_error')
                last = UpstreamFailure(502, 'transport_error', 'could not reach ZCode')
                tried.add(account.id)
            finally:
                if handle is not None:
                    await self._close(handle)
                elif resp is not None:
                    try: await resp.aclose()
                    finally: await self.pool.release(account)
                elif not transferred:
                    await self.pool.release(account)
            if attempt + 1 < self.cfg.upstream.max_attempts:
                await asyncio.sleep(0.1 * (attempt + 1))
        raise last or NoAccountsAvailable()

    async def stream(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous', anthropic=False):
        handle = await self.open_stream(payload, model=model, variant=variant, dialect=dialect, client_key=client_key)
        async for frame in self.stream_from(handle, model=model, dialect=dialect,
                                            client_key=client_key, anthropic=anthropic):
            yield frame

    async def stream_from(self, handle, *, model, dialect='openai', client_key='anonymous', anthropic=False):
        model = handle.body['model']
        translator = OpenAITranslator(model)
        usage, stopped, status = {}, False, 200
        captured = deque()
        capture_size = 0
        async def events():
            yield handle.first
            async for event in handle.events: yield event
        try:
            async for event in events():
                kind = event.get('type')
                if self.capture.enabled and capture_size < 200000:
                    text = json.dumps(event, ensure_ascii=False)
                    captured.append(text)
                    capture_size += len(text)
                if kind == 'error':
                    info = event.get('error') or event
                    code, message = str(info.get('code') or info.get('type') or 'api_error'), info.get('message', 'ZCode stream failed')
                    status = 529 if is_model_overload(502, code) else (429 if code in ('11113', 'rate_limit_error') else 502)
                    await self._apply_error(handle.account, model, status, code, message)
                    yield _sse_event('error', event) if anthropic else sse_frame(openai_error(status, code, message))
                    if not anthropic: yield b'data: [DONE]\n\n'
                    return
                if kind == 'message_start': usage.update(event['message'].get('usage') or {})
                if kind == 'message_delta': usage.update(event.get('usage') or {})
                if anthropic:
                    yield _sse_event(kind, event)
                else:
                    for chunk in translator.feed(event): yield sse_frame(chunk)
                if kind == 'message_stop':
                    stopped = True
                    if not anthropic: yield b'data: [DONE]\n\n'
                    await self.pool.on_success(handle.account)
                    break
            if not stopped:
                raise UpstreamFailure(502, 'truncated_stream', 'ZCode stream ended before message_stop')
        except asyncio.CancelledError:
            status = 499
            raise
        except (httpx.HTTPError, ValueError, KeyError, UpstreamFailure) as exc:
            status = 502
            code = getattr(exc, 'code', 'stream_error')
            message = getattr(exc, 'message', 'ZCode stream interrupted')
            error = anthropic_error(status, code, message) if anthropic else openai_error(status, code, message)
            yield _sse_event('error', error) if anthropic else sse_frame(error)
            if not anthropic: yield b'data: [DONE]\n\n'
        finally:
            await self._close(handle)
            self._record(handle.account, handle.headers, handle.body, status, handle.resp.headers,
                         '\n'.join(captured), (time.time() - handle.started) * 1000,
                         client_key, dialect, model, usage)

    async def complete(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous', is_disconnected=None):
        handle = await self.open_stream(payload, model=model, variant=variant, dialect=dialect, client_key=client_key,
                                        is_disconnected=is_disconnected)
        model = handle.body['model']
        accumulator, translator, chunks = MessageAccumulator(), OpenAITranslator(model), []
        status = 200
        try:
            accumulator.feed(handle.first)
            if dialect != 'anthropic': chunks.extend(sse_frame(c) for c in translator.feed(handle.first))
            async for event in handle.events:
                if event.get('type') == 'error':
                    info = event.get('error') or event
                    code = str(info.get('code') or info.get('type') or 'api_error')
                    status = 529 if is_model_overload(502, code) else (429 if code in ('11113', 'rate_limit_error') else (405 if is_block_code(code) else 502))
                    message = info.get('message', 'ZCode stream failed')
                    await self._apply_error(handle.account, model, status, code, message)
                    raise UpstreamFailure(status, code, message)
                accumulator.feed(event)
                if dialect != 'anthropic': chunks.extend(sse_frame(c) for c in translator.feed(event))
                if event.get('type') == 'message_stop': break
            if not accumulator.stopped:
                raise UpstreamFailure(502, 'truncated_stream', 'ZCode stream ended before message_stop')
            message = accumulator.finish()
            raw = json.dumps(message, ensure_ascii=False).encode() if dialect == 'anthropic' else b''.join(chunks)
            await self.pool.on_success(handle.account)
            self._record(handle.account, handle.headers, handle.body, 200, handle.resp.headers,
                         json.dumps(message, ensure_ascii=False), (time.time() - handle.started) * 1000,
                         client_key, dialect, model, message['usage'])
            return CallResult(handle.account, 200, raw, (time.time()-handle.started)*1000, handle.resp.headers)
        except UpstreamFailure as exc:
            self._record(handle.account, handle.headers, handle.body, exc.status, handle.resp.headers,
                         json.dumps({'error': exc.message}), (time.time() - handle.started)*1000,
                         client_key, dialect, model, accumulator.message.get('usage') or {})
            raise
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            self._record(handle.account, handle.headers, handle.body, 502, handle.resp.headers,
                         '{"error":"malformed stream"}', (time.time() - handle.started)*1000,
                         client_key, dialect, model, accumulator.message.get('usage') or {})
            raise UpstreamFailure(502, 'invalid_stream', 'ZCode returned an incomplete or malformed response') from exc
        finally:
            await self._close(handle)

    def _record(self, account, headers, body, status, response_headers, response, duration, key, dialect, model, usage):
        converted = usage_openai(usage)
        rec = dict(account_id=account.id, client_key=key, dialect=dialect, model=model,
                   variant='anthropic', stream=int(body.get('stream', True)), status=status,
                   duration_ms=duration, prompt_tokens=converted['prompt_tokens'],
                   completion_tokens=converted['completion_tokens'], total_tokens=converted['total_tokens'],
                   error_code=None if status == 200 else f'http_{status}')
        cap = dict(request_headers=headers, request_body=body, status=status,
                   response_headers=dict(response_headers), response_body=response)
        # Disk I/O off the event loop: SQLite/JSONL writes (and AV scans on
        # those files) used to stall every concurrent stream while they ran.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.run_in_executor(None, self._write_record, rec, cap)
        else:
            self._write_record(rec, cap)

    def _write_record(self, rec, cap):
        try:
            self.store.record(**rec)
            self.capture.write(**cap)
        except Exception:
            log.debug("store/capture write failed", exc_info=True)
