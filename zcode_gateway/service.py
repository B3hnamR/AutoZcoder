"""Shared pool, persistence and dashboard with a native ZCode transport."""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass

import httpx
from .aggregate import aggregate_openai_stream
from .pool import Account, AccountState
from .breaker import EdgeBlockBreaker
from .model_retry import is_model_overload, overload_delay, retry_after_seconds, wait_for_retry
from .zcode_protocol import MessageAccumulator, OpenAITranslator, usage_openai
from .upstream import (build_headers, build_upstream_body, iter_sse_events, sse_frame,
                       parse_upstream_error, anthropic_error, openai_error, TransportError,
                       is_block_code)
from .registry import model_caps

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
    # re-opens the same request on a fresh upstream stream (set by open_stream);
    # used to retry a stream that died before any content reached the client
    reopen: object = None

@dataclass
class CallResult:
    account: Account
    status: int
    raw: bytes
    duration_ms: float
    upstream_headers: dict


def _snapshot_payload(payload):
    """Copy of a request payload that survives open_stream mutating it (the
    account pin is popped out of payload['_anthropic'] on each attempt)."""
    snap = dict(payload)
    if isinstance(snap.get('_anthropic'), dict):
        snap['_anthropic'] = dict(snap['_anthropic'])
    return snap


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
        # IP-wide CDN edge-block breaker: shared by every account (one egress
        # IP, one block). Replaces the old behaviour where an HTML 403 was
        # mislabelled ENTITLEMENT_ERROR and parked accounts one by one until
        # the pool drained for a condition no account caused.
        self.breaker = EdgeBlockBreaker()

        def _on_edge_block(count: int) -> None:
            try:
                self.pool.notify('edge_block',
                                 f"upstream edge block #{count} — requests paused, "
                                 f"probing to resume automatically",
                                 level='error')
            except Exception:
                pass
        self.breaker.on_block = _on_edge_block

    def _build(self, payload, variant='anthropic', model=None):
        caps = model_caps(self.registry.resolve(model or payload.get('model') or '')) \
            if hasattr(self, 'registry') and self.registry is not None else None
        return build_upstream_body(payload, variant, self.cfg.models.default_max_tokens,
                                   model_caps=caps,
                                   default_effort=getattr(self.cfg.models, 'default_effort', 'high'))

    async def _edge_probe(self):
        """Cheap GET against the upstream host for the breaker's prober.

        Any HTTP answer that is NOT the CDN's HTML block page means the edge
        is no longer intercepting (a 404/401 JSON from the API is fine — the
        point is the request reached the API at all). Returns (status, body).
        """
        base = self.cfg.upstream.base_url.rstrip('/')
        resp = await self.client._client.get(base + '/zcode-plan/billing/balance',
                                             timeout=10)
        return resp.status_code, resp.text
    async def _close(self, handle):
        if handle.closed: return
        handle.closed = True
        try:
            if handle.events is not None: await handle.events.aclose()
        finally:
            try:
                await handle.resp.aclose()
            finally:
                # Shield the release: on a client disconnect Starlette cancels
                # the scope this runs in, and an await on the pool lock here
                # would leak the in-flight slot FOREVER (cap reached after a
                # few disconnects). Counter hygiene must not be cancellable.
                try:
                    await asyncio.shield(self.pool.release(handle.account))
                except asyncio.CancelledError:
                    # the shield's outer await was cancelled even though the
                    # release itself completed — re-raise after the fact
                    raise
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
        elif code in ('11113', '1005') or status == 402:
            # 1005 is the SAME "exceed quota limit" rejection under an HTTP
            # 502 (captured live 2026-10-21: 10x/10min on a plan-less
            # account) — treating it as a generic 5xx only cooled the
            # account 30s and the router picked it again, forever.
            # Duration depends on what the quota data says:
            #   remaining <= 0 + resets_at -> genuinely spent bucket: park
            #     until the reset (correct, could be hours)
            #   remaining > 0 -> the upstream's answer CONTRADICTS the ledger:
            #     either in-flight usage drained it since the last 60s poll,
            #     or (seen live across whole clusters 2026-10-22) the exit IP
            #     is risk-shaped and z.ai mislabels the rejection. Parking
            #     until resets_at here would sideline a 105M-token account
            #     for a day on ONE transient error — short park instead, the
            #     next balance poll auto-clears it when the ledger is right.
            #   no bucket data -> strike toward EXHAUSTED (plan-less churner)
            quota = account.notes.get('quotas', {}).get(model, {})
            remaining = int(quota.get('remaining', 0) or 0)
            if remaining > 0:
                await self.pool.cap_model(account, model, 600,
                                          reason='11113' if code == '11113' else '1005',
                                          message=message)
                return
            seconds = max(1, quota.get('resets_at', 0) - time.time()) if quota else 0
            if seconds <= 1:
                # no bucket data to schedule around: if the account HAS no
                # entitlement (plans empty, quotas empty), parking one model
                # still leaves it churning on the other — strike toward
                # EXHAUSTED; the balance loop restores it the moment real
                # quota appears.
                account.quota_strikes = getattr(account, 'quota_strikes', 0) + 1
                if account.quota_strikes >= 3:
                    account.state = AccountState.EXHAUSTED
                    account.last_error = 'repeated quota rejection with no visible buckets'
                    try:
                        self.pool.notify('quota',
                                         f"account {account.id} EXHAUSTED — repeated "
                                         f"quota rejections, no visible plan/buckets",
                                         level='warning')
                    except Exception:
                        pass
                    return
                seconds = 900      # park the model past the churn window
            await self.pool.cap_model(account, model, seconds,
                                      reason='11113' if code == '11113' else '1005',
                                      message=message)
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

    # A stream that dies BEFORE any content reached the client (only
    # message_start/ping so far) is re-opened this many times, transparently:
    # the client cannot tell, and nothing is ever replayed to it.
    STREAM_RETRIES = 2

    def _relay_label(self, resp) -> str:
        url = getattr(resp, 'relay_url', '') or ''
        if not url:
            return 'direct'
        name = ''
        try:
            rm = getattr(self.client, 'relay_manager', None)
            if rm is not None:
                for r in rm.get_settings().get('relays') or []:
                    if str(r.get('url', '')).rstrip('/') == url.rstrip('/'):
                        name = str(r.get('name') or '')
                        break
        except Exception:
            pass
        host = urllib.parse.urlparse(url).netloc or url
        return f'{name} ({host})' if name else host

    @staticmethod
    def _describe_failure(exc) -> str:
        if isinstance(exc, UpstreamFailure):
            if exc.code == 'truncated_stream':
                return 'connection closed cleanly before message_stop'
            return f'{exc.code}: {exc.message}'
        text = f'{type(exc).__name__}: {exc}'.rstrip(': ')
        inner = exc.__cause__ or exc.__context__
        if inner is not None and inner is not exc:
            text += f' (caused by {type(inner).__name__}: {inner})'.replace(': )', ')')
        return text

    @staticmethod
    def _retryable(exc) -> bool:
        # transport-level only: a dead/cut connection, or a clean EOF with no
        # message_stop. Upstream error events and parse errors are not retried.
        return isinstance(exc, httpx.HTTPError) or (
            isinstance(exc, UpstreamFailure) and exc.code == 'truncated_stream')

    def _log_stream_failure(self, handle, exc, *, committed, events, retry):
        acct = handle.account
        who = str(acct.id)[:12] + (f' <{acct.email}>' if getattr(acct, 'email', '') else '')
        log.warning('ZCode stream failed mid-flight: model=%s account=%s relay=%s after %.1fs, '
                    '%d event(s), content_sent=%s - %s%s',
                    handle.body.get('model'), who, self._relay_label(handle.resp),
                    time.time() - handle.started, events, committed,
                    self._describe_failure(exc),
                    ' - retrying transparently' if retry else '')

    async def _after_stream_failure(self, handle, exc, *, committed, events, retries_left):
        """Log a mid-stream failure; return a freshly opened handle when the
        failure is retryable and nothing has been sent to the client."""
        will_retry = (not committed) and retries_left > 0 and self._retryable(exc)
        self._log_stream_failure(handle, exc, committed=committed, events=events, retry=will_retry)
        if not will_retry:
            return None
        reopen = getattr(handle, 'reopen', None)
        if reopen is None:
            return None
        # free this attempt's in-flight slot BEFORE re-opening: with a low
        # per-account cap the retry would otherwise wait on the slot we hold
        await self._close(handle)
        await asyncio.sleep(0.15)
        try:
            return await reopen()
        except asyncio.CancelledError:
            raise
        except Exception as exc2:
            log.warning('ZCode stream retry could not open a new upstream stream (%s) - '
                        'reporting the original failure', self._describe_failure(exc2))
            return None

    async def open_stream(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous', is_disconnected=None):
        snapshot = _snapshot_payload(payload)
        handle = await self._open_stream(payload, model=model, variant=variant, dialect=dialect,
                                         client_key=client_key, is_disconnected=is_disconnected)

        async def reopen():
            return await self.open_stream(_snapshot_payload(snapshot), model=model, variant=variant,
                                          dialect=dialect, client_key=client_key)
        handle.reopen = reopen
        return handle

    async def _open_stream(self, payload, *, model, variant='anthropic', dialect='openai', client_key='anonymous', is_disconnected=None):
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
                # Try the next model in the fallback chain for every error the
                # account-parking path already handled (its own codes) AND the
                # upstream's real rate-limit code — `rate_limit_error` was
                # missing from this list, so a 429 on the paid model never
                # fell back to the free one (measured: 0 retries on 429).
                if exc.code not in ('11113', '1005', 'rate_limit_error', 'MODEL_CAPPED', 'MODEL_PARKED',
                                    'INFERENCE_CAP_ERROR', 'ENTITLEMENT_ERROR',
                                    'UNUSUAL_ACTIVITY', '3012', '405', 'HTTP_405',
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
        # fail fast while the egress IP is edge-blocked: every account would
        # return the same HTML 403, and trying parks them one by one
        blocked = self.breaker.check()
        if blocked is not None:
            raise UpstreamFailure(blocked['status'], blocked['code'],
                                  blocked['message'],
                                  retry_after=blocked['retry_in_s'])
        try: body = self._build(payload, variant, model=model)
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
            elif isinstance(body.get('metadata'), dict) and 'user_id' not in body['metadata']:
                # A client-supplied Anthropic `metadata` field used to skip the
                # device binding entirely — every such request went upstream
                # with no device id (an "unusual activity" signal). Merge it.
                device_mid = account.notes.get('device_mid') or self.cfg.upstream.fingerprint.device_mid
                body['metadata'] = {**body['metadata'],
                    'user_id': json.dumps({'device_id': device_mid,
                        'account_uuid': '', 'session_id': headers['x-session-id']}, separators=(',', ':'))}
            resp, handle, transferred = None, None, False
            try:
                if account.expires_in() <= 0 and not await self.tokens.refresh(account):
                    # Expired token: strike-and-cool (like upstream 401s), not
                    # an instant permanent kill — an expired token whose file
                    # simply hasn't rotated yet used to DEAD the account with
                    # no recovery path (enable() -> next request kills again).
                    await self.pool.kill_after_strikes(account, 'ZCode token expired; sign in again')
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
                        status = 429 if code in ('11113', '1005') else 502
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
                                status = 429 if code in ('11113', '1005') else 502
                    if status != 200:
                        # IP-wide CDN edge block (HTML body, not API JSON)?
                        # Detect BEFORE _apply_error — this is not an account
                        # problem, and parking the account for it is the bug
                        # the breaker exists to prevent.
                        if self.breaker.note_edge_block(raw):
                            await resp.aclose()
                            self.breaker.ensure_prober(self._edge_probe)
                            raise UpstreamFailure(
                                503, 'waf_block',
                                'upstream edge block (IP-wide): requests '
                                'rejected for every account; retry shortly',
                                retry_after=35)
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
                # a clean stream through the CDN is evidence any prior edge
                # block has lifted
                self.breaker.note_success()
                # An immediate SSE error must reach the client before HTTP 200 is committed.
                while True:
                    first = await anext(handle.events, None)
                    if first is None:
                        raise UpstreamFailure(502, 'empty_stream', 'ZCode returned an empty event stream')
                    if first.get('type') == 'ping': continue
                    # Two error shapes reach this loop:
                    #   1) Anthropic event form:  {"type":"error","error":{...}}
                    #   2) OpenAI-shaped frame (captured 2026-09-16):
                    #      {"error":{"code":"stream_initialization_failed", ...}}
                    # The second has no "type" key — checking only the first
                    # form masked the real code/message into a generic 502
                    # invalid_stream and never cooled/parked the account.
                    if first.get('type') == 'error' or (first.get('type') is None and 'error' in first):
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
                # A block that arrived THROUGH a relay is the relay's exit-IP
                # problem: UpstreamClient already rotated+cooled every relay
                # it could. Do NOT cool the account (the same request through
                # the next relay on the next attempt is fine) and do NOT burn
                # attempts rotating accounts — every account uses the same
                # relay set, so the next attempt would go DIRECT (all relays
                # are cooling) and burn the home IP. Fail the request now
                # with the real code; relays uncool in 60s.
                from .upstream import UpstreamBlockThroughRelay
                if isinstance(exc, UpstreamBlockThroughRelay):
                    raise UpstreamFailure(
                        405, '3012',
                        'relay exit IP(s) flagged (unusual activity): all relays '
                        'cooled ~60s — retry shortly; a different relay '
                        'rotation is not needed, it happens automatically'
                    ) from exc
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
        committed = False       # has anything beyond the prelude been handed to the client?
        prelude = []            # message_start/ping held back until real content shows up,
                                # so an early failure can be retried without the client noticing
        seen = 0
        retries_left = self.STREAM_RETRIES

        def emit(event):
            kind = event.get('type')
            if kind == 'message_start': usage.update(event['message'].get('usage') or {})
            if kind == 'message_delta': usage.update(event.get('usage') or {})
            if anthropic: return [_sse_event(kind, event)]
            return [sse_frame(chunk) for chunk in translator.feed(event)]

        async def events():
            yield handle.first
            async for event in handle.events: yield event
        try:
            while True:                     # one pass per upstream attempt
                try:
                    async for event in events():
                        kind = event.get('type')
                        seen += 1
                        if self.capture.enabled and capture_size < 200000:
                            text = json.dumps(event, ensure_ascii=False)
                            captured.append(text)
                            capture_size += len(text)
                        if not committed:
                            if kind in ('message_start', 'ping'):
                                prelude.append(event)
                                continue
                            committed = True
                            for held in prelude:
                                for out in emit(held): yield out
                            prelude.clear()
                        # OpenAI-shaped mid-stream error frame: {"error":{...}} with
                        # no "type" key (captured 2026-09-16). Without this branch it
                        # fell through to the Anthropic passthrough as `event: None`
                        # — not in the Anthropic SSE vocabulary — and the account was
                        # never parked for the failure.
                        if kind == 'error' or (kind is None and 'error' in event):
                            info = event.get('error') or event
                            code, message = str(info.get('code') or info.get('type') or 'api_error'), info.get('message', 'ZCode stream failed')
                            status = 529 if is_model_overload(502, code) else (429 if code in ('11113', 'rate_limit_error') else 502)
                            await self._apply_error(handle.account, model, status, code, message)
                            self._log_stream_failure(handle, UpstreamFailure(status, code, message),
                                                     committed=committed, events=seen, retry=False)
                            yield _sse_event('error', event) if anthropic else sse_frame(openai_error(status, code, message))
                            if not anthropic: yield b'data: [DONE]\n\n'
                            return
                        for out in emit(event): yield out
                        if kind == 'message_stop':
                            stopped = True
                            if not anthropic: yield b'data: [DONE]\n\n'
                            await self.pool.on_success(handle.account)
                            break
                    if not stopped:
                        raise UpstreamFailure(502, 'truncated_stream', 'ZCode stream ended before message_stop')
                    break
                except (httpx.HTTPError, ValueError, KeyError, UpstreamFailure) as exc:
                    fresh = await self._after_stream_failure(
                        handle, exc, committed=committed, events=seen, retries_left=retries_left)
                    if fresh is None:
                        raise               # reported to the client below, as before
                    retries_left -= 1
                    # the aborted attempt still counts as an upstream failure
                    self._record(handle.account, handle.headers, handle.body, 502, handle.resp.headers,
                                 '\n'.join(captured), (time.time() - handle.started) * 1000,
                                 client_key, dialect, model, usage)
                    old, handle = handle, fresh
                    await self._close(old)
                    model = handle.body['model']
                    translator = OpenAITranslator(model)
                    usage, captured, capture_size, prelude, seen = {}, deque(), 0, [], 0
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
        retries_left = self.STREAM_RETRIES
        fresh = None
        while True:
            if fresh is not None:
                handle, fresh = fresh, None
            model = handle.body['model']
            accumulator, translator, chunks = MessageAccumulator(), OpenAITranslator(model), []
            status, seen = 200, 0
            try:
                accumulator.feed(handle.first)
                if dialect != 'anthropic': chunks.extend(sse_frame(c) for c in translator.feed(handle.first))
                async for event in handle.events:
                    seen += 1
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
                # non-streaming: nothing has reached the client, so a cut or
                # truncated stream is retried whatever it had produced so far
                fresh = await self._after_stream_failure(
                    handle, exc, committed=False, events=seen, retries_left=retries_left)
                if fresh is None:
                    raise
                retries_left -= 1
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                self._record(handle.account, handle.headers, handle.body, 502, handle.resp.headers,
                             '{"error":"malformed stream"}', (time.time() - handle.started)*1000,
                             client_key, dialect, model, accumulator.message.get('usage') or {})
                fresh = await self._after_stream_failure(
                    handle, exc, committed=False, events=seen, retries_left=retries_left)
                if fresh is None:
                    raise UpstreamFailure(502, 'invalid_stream', 'ZCode returned an incomplete or malformed response') from exc
                retries_left -= 1
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
