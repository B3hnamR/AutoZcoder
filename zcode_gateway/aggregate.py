import json, time
from typing import Any
from .upstream import iter_sse_events

def aggregate_openai_stream(text: str, model: str, fallback_id: str) -> dict:
    """Collapse a captured SSE stream into a single chat.completion object."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    refusal: str | None = None
    finish_reason: str | None = None
    completion_id = fallback_id
    created = int(time.time())
    usage: dict = {}
    upstream_error: str | None = None

    for event in iter_sse_events(text):
        if not isinstance(event, dict):
            continue
        completion_id = event.get("id", completion_id)
        created = event.get("created", created)
        if event.get("model"):
            model = event["model"]
        if event.get("usage"):
            usage = event["usage"]
        if event.get("error"):
            upstream_error = str(event["error"])
        for choice in (event.get("choices") or []):
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            # Reasoning models stream their thinking in a `reasoning` delta
            # (captured 2026-09-19, zcode-free/kimi-k3). Collect it so
            # non-streaming callers receive it instead of losing it.
            if delta.get("reasoning"):
                reasoning_parts.append(delta["reasoning"])
            # Providers signal a policy refusal with a `refusal` delta, not
            # `content`. Dropping it leaves the caller with an empty answer and
            # only a `content_filter` code, so they invent their own message.
            if delta.get("refusal"):
                refusal = delta["refusal"]
            for call in (delta.get("tool_calls") or []):
                idx = call.get("index", 0)
                slot = tool_calls.setdefault(idx, {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    message: dict[str, Any] = {"role": "assistant",
                               "content": "".join(content_parts) or None}
    if reasoning_parts:
        # OpenRouter-style additive field: clients that understand reasoning
        # read it, everyone else ignores it
        message["reasoning"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
    if refusal:
        # OpenAI's message object has a `refusal` field for exactly this
        message["refusal"] = refusal
        if finish_reason is None:
            finish_reason = "content_filter"
    if upstream_error and not content_parts and not tool_calls:
        message["content"] = upstream_error

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason or "stop",
        }],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0,
                           "total_tokens": 0},
    }
