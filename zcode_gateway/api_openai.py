"""OpenAI-compatible surface: /v1/chat/completions, /v1/completions, /v1/models."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .deps import client_key, get_state
from .service import NoAccountsAvailable, UpstreamFailure, aggregate_openai_stream
from .translate_anthropic import extract_stream_error, stream_had_content
from .upstream import openai_error

router = APIRouter(tags=["openai"])

SSE_HEADERS = {
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "x-accel-buffering": "no",
}


def _error_response(status: int, code: str, message: str,
                    extra: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content=openai_error(status, code, message, extra))


def _valid_messages(messages: Any) -> bool:
    """Structural check so a malformed body fails as 400 instead of a 500 from
    deep inside translation (e.g. {"messages": "hi"}) or being forwarded."""
    return (isinstance(messages, list) and bool(messages)
            and all(isinstance(m, dict) and isinstance(m.get("role"), str)
                    for m in messages))


@router.get("/v1/models")
async def list_models(request: Request, key: str = Depends(client_key)) -> dict:
    state = get_state(request)
    if state.catalog is not None:
        await state.catalog.refresh_if_due()
    return {"object": "list", "data": state.registry.catalogue()}


@router.get("/models")
async def list_models_root(request: Request, key: str = Depends(client_key)) -> dict:
    return await list_models(request, key)


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    body: dict[str, Any] = Body(...),
    key: str = Depends(client_key),
):
    state = get_state(request)

    if body.get('model') is not None and not isinstance(body['model'], str):
        return _error_response(400, 'invalid_request_error', 'model must be a string')

    # Reject an empty message list up front. Forwarding it makes the upstream
    # answer 200 with an SSE error frame (captured: stream_initialization_failed
    # "At least one message is required"), which is a confusing way to learn that
    # the request itself was malformed.
    if not _valid_messages(body.get("messages")):
        return _error_response(400, "invalid_request_error",
                               "'messages' is required and must be a non-empty "
                               "list of message objects")

    requested = body.get("model")
    upstream_model = state.registry.resolve(requested, "openai")
    variant = state.registry.variant_for(upstream_model)

    payload = dict(body)
    payload["model"] = upstream_model

    wants_stream = bool(body.get("stream", False))

    try:
        if wants_stream:
            # Open upstream BEFORE returning StreamingResponse: once headers are
            # sent a status code can no longer be changed, so any failure here
            # would reach the client as a connection reset instead of an error.
            handle = await state.service.open_stream(
                payload, model=upstream_model, variant=variant,
                dialect="openai", client_key=key, is_disconnected=request.is_disconnected,
            )
            generator = state.service.stream_from(
                handle, model=upstream_model, dialect="openai",
                client_key=key, anthropic=False,
            )
            return StreamingResponse(generator, media_type="text/event-stream",
                                     headers=SSE_HEADERS,
                                     background=BackgroundTask(state.service._close, handle))

        result = await state.service.complete(
            payload, model=upstream_model, variant=variant,
            dialect="openai", client_key=key, is_disconnected=request.is_disconnected,
        )
        text = result.raw.decode("utf-8", "replace")

        # A 200 stream can still carry an error frame. If it produced no content,
        # report the failure instead of returning the error text as an answer.
        stream_err = extract_stream_error(text)
        if stream_err and not stream_had_content(text):
            return _error_response(
                502, stream_err["code"],
                stream_err["message"],
                {"request_id": stream_err.get("request_id")} if stream_err.get("request_id") else None)

        fallback = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        completion = aggregate_openai_stream(text, upstream_model, fallback)
        return JSONResponse(completion)

    except NoAccountsAvailable:
        return _error_response(503, "no_accounts_available",
                               "no usable Zcode account in the pool")
    except UpstreamFailure as exc:
        return _error_response(exc.status, exc.code, exc.message, exc.extra())


@router.post("/v1/completions")
async def completions(
    request: Request,
    body: dict[str, Any] = Body(...),
    key: str = Depends(client_key),
):
    """Legacy text-completion shim — reuses the chat path."""
    prompt = body.get("prompt")
    if isinstance(prompt, list):
        prompt = "\n".join(str(p) for p in prompt)

    chat_body = {
        "model": body.get("model"),
        "messages": [{"role": "user", "content": prompt or ""}],
        "stream": bool(body.get("stream", False)),
        "max_tokens": body.get("max_tokens"),
    }
    response = await chat_completions(request, chat_body, key)

    if isinstance(response, StreamingResponse):
        # Legacy clients expect text_completion chunks (choices[].text), not
        # chat.completion.chunk frames (choices[].delta) — reshape on the fly.
        return StreamingResponse(
            _legacy_completion_stream(response.body_iterator,
                                      body.get("model") or ""),
            media_type="text/event-stream", headers=SSE_HEADERS)

    # reshape chat.completion -> text_completion
    payload = json.loads(response.body.decode("utf-8"))
    if "error" in payload:
        return response
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {
        "id": payload.get("id", f"cmpl-{uuid.uuid4().hex[:24]}"),
        "object": "text_completion",
        "created": payload.get("created", int(time.time())),
        "model": payload.get("model"),
        "choices": [{
            "index": 0,
            "text": message.get("content") or "",
            "finish_reason": choice.get("finish_reason", "stop"),
        }],
        "usage": payload.get("usage", {}),
    }


async def _legacy_completion_stream(source: Any, model: str) -> Any:
    """chat.completion.chunk SSE -> text_completion SSE, line by line.

    Buffers across chunk boundaries: upstream chunks split at arbitrary byte
    positions, so a JSON frame may span two chunks.
    """
    buf = ""

    def _translate(line: str) -> bytes | None:
        line = line.strip()
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if payload == "[DONE]":
            return b"data: [DONE]\n\n"
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(chunk, dict):
            return None
        legacy: dict[str, Any] = {
            "id": chunk.get("id") or f"cmpl-{uuid.uuid4().hex[:24]}",
            "object": "text_completion",
            "created": chunk.get("created", int(time.time())),
            "model": chunk.get("model") or model,
            "choices": [
                {
                    "index": c.get("index", 0),
                    "text": ((c.get("delta") or {}).get("content")
                             or (c.get("delta") or {}).get("refusal") or ""),
                    "finish_reason": c.get("finish_reason"),
                }
                for c in (chunk.get("choices") or [])
            ],
        }
        if chunk.get("usage"):
            legacy["usage"] = chunk["usage"]
        return f"data: {json.dumps(legacy, ensure_ascii=False)}\n\n".encode("utf-8")

    try:
        async for raw in source:
            buf += raw.decode("utf-8", "replace")
            *lines, buf = buf.split("\n")
            for line in lines:
                out = _translate(line)
                if out is not None:
                    yield out
        for line in buf.splitlines():        # flush any final partial line
            out = _translate(line)
            if out is not None:
                yield out
    finally:
        # an abandoned legacy stream must close the inner generator, not wait
        # for GC: the inner generator owns the upstream connection and the
        # pool reservation
        aclose = getattr(source, "aclose", None)
        if aclose is not None:
            await aclose()
