"""Anthropic Messages API <-> internal (OpenAI-shaped) translation.

Covers: system prompts, text/image/tool_use/tool_result content blocks, tools,
tool_choice, stop sequences, and full SSE event translation in both directions.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# request: anthropic -> openai
# --------------------------------------------------------------------------- #

_OPENAI_TO_ANTHROPIC_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    # a provider refusal is its own stop reason in the Anthropic API
    "content_filter": "refusal",
    "refusal": "refusal",
}


def _blocks(content: Any) -> list[dict]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _system_text(system: Any) -> str | None:
    if not system:
        return None
    if isinstance(system, str):
        return system
    parts = [b.get("text", "") for b in _blocks(system) if b.get("type") == "text"]
    joined = "\n\n".join(p for p in parts if p)
    return joined or None


def _image_url(block: dict) -> str | None:
    src = block.get("source") or {}
    if src.get("type") == "base64":
        media = src.get("media_type", "image/png")
        return f"data:{media};base64,{src.get('data', '')}"
    if src.get("type") == "url":
        return src.get("url")
    return None


def messages_to_openai(messages: Iterable[dict]) -> list[dict]:
    """Convert Anthropic messages into OpenAI chat messages."""
    out: list[dict] = []

    for msg in messages:
        if not isinstance(msg, dict):
            # a bare string/list message used to raise AttributeError deep in
            # translation and surface as a 500; make it a protocol error
            raise ValueError("'messages' must be a list of message objects")
        role = msg.get("role")
        blocks = _blocks(msg.get("content"))

        if role == "assistant":
            texts: list[str] = []
            tool_calls: list[dict] = []
            for b in blocks:
                if b.get("type") == "text":
                    text = b.get("text", "")
                    if isinstance(text, str):
                        texts.append(text)
                elif b.get("type") == "tool_use":
                    tool_calls.append({
                        "id": b.get("id") or f"call_{uuid.uuid4().hex[:16]}",
                        "type": "function",
                        "function": {
                            "name": b.get("name", ""),
                            "arguments": json.dumps(b.get("input", {}),
                                                    ensure_ascii=False),
                        },
                    })
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(t for t in texts if t) or None,
            }
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
            continue

        # user role: may carry tool_result blocks AND text/image blocks
        tool_results: list[dict] = []
        parts: list[dict] = []
        for b in blocks:
            if b.get("type") == "tool_result":
                inner = b.get("content")
                if isinstance(inner, list):
                    text = "\n".join(x.get("text", "") for x in inner
                                     if isinstance(x, dict) and x.get("type") == "text")
                else:
                    text = inner if isinstance(inner, str) else json.dumps(inner)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id", ""),
                    "content": text or "",
                })
            elif b.get("type") == "text":
                text = b.get("text", "")
                if isinstance(text, str):
                    parts.append({"type": "text", "text": text})
            elif b.get("type") == "image":
                url = _image_url(b)
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})

        out.extend(tool_results)

        if parts:
            # collapse to a plain string when it is text only (max compatibility)
            if all(p["type"] == "text" for p in parts):
                out.append({"role": "user",
                            "content": "\n".join(p["text"] for p in parts)})
            else:
                out.append({"role": "user", "content": parts})

    return out


def tools_to_openai(tools: Any) -> list[dict] | None:
    if not tools:
        return None
    out = []
    for t in tools:
        if "function" in t:                       # already OpenAI-shaped
            out.append(t)
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object",
                                                        "properties": {}},
            },
        })
    return out


def tool_choice_to_openai(tc: Any) -> Any:
    if tc is None:
        return "auto"
    if isinstance(tc, str):
        return tc
    kind = tc.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool" and tc.get("name"):
        return {"type": "function", "function": {"name": tc["name"]}}
    return "auto"


def request_to_openai(req: dict, upstream_model: str,
                      default_max_tokens: int = 32000) -> dict:
    """Anthropic Messages request -> internal OpenAI-shaped payload."""
    messages: list[dict] = []
    sys_text = _system_text(req.get("system"))
    if sys_text:
        messages.append({"role": "system", "content": sys_text})
    messages.extend(messages_to_openai(req.get("messages") or []))

    # explicit max_tokens is forwarded as-is (0 no longer silently becomes the
    # 32000 default); only an absent value falls back to the configured default
    raw_max_tokens = req.get("max_tokens")
    if raw_max_tokens is None:
        max_tokens = default_max_tokens
    else:
        try:
            max_tokens = int(raw_max_tokens)
        except (TypeError, ValueError):
            raise ValueError("'max_tokens' must be an integer")

    payload: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "stream": bool(req.get("stream", False)),
        "max_tokens": max_tokens,
    }

    tools = tools_to_openai(req.get("tools"))
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice_to_openai(req.get("tool_choice"))

    if req.get("stop_sequences"):
        payload["stop"] = req["stop_sequences"]
    if req.get("temperature") is not None:
        payload["temperature"] = req["temperature"]
    if req.get("top_p") is not None:
        payload["top_p"] = req["top_p"]
    # Anthropic-specific, but OpenRouter-style upstreams accept it; carrying it
    # is better than silently dropping a sampling parameter the client asked for
    if req.get("top_k") is not None:
        payload["top_k"] = req["top_k"]

    # reasoning hint carried by client extensions (Zcode uses reasoning_effort)
    extra = req.get("metadata") or {}
    if isinstance(extra, dict) and extra.get("reasoning_effort"):
        payload["reasoning_effort"] = extra["reasoning_effort"]

    return payload


# --------------------------------------------------------------------------- #
# response: openai -> anthropic
# --------------------------------------------------------------------------- #


def _map_stop_reason(finish: str | None) -> str | None:
    if finish is None:
        return None
    return _OPENAI_TO_ANTHROPIC_STOP.get(finish, "end_turn")


def response_to_anthropic(full: dict, model: str,
                          msg_id: str | None = None) -> dict:
    """Non-streaming OpenAI chat.completion -> Anthropic Messages response."""
    choice = (full.get("choices") or [{}])[0]
    message = choice.get("message") or {}

    content: list[dict] = []
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": text})

    # A refusal carries the reason; surface it as text so the caller is not left
    # with an empty message and no explanation.
    refusal = message.get("refusal")
    if refusal and not text:
        content.append({"type": "text", "text": refusal})

    for call in (message.get("tool_calls") or []):
        fn = call.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            parsed = {"_raw": raw_args}
        content.append({
            "type": "tool_use",
            "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:20]}",
            "name": fn.get("name", ""),
            "input": parsed,
        })

    if not content:
        content = [{"type": "text", "text": ""}]

    usage = full.get("usage") or {}
    return {
        "id": msg_id or full.get("id") or f"msg_{uuid.uuid4().hex[:20]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _map_stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# --------------------------------------------------------------------------- #
# streaming: openai SSE chunks -> anthropic SSE events
# --------------------------------------------------------------------------- #


def _evt(name: str, data: dict) -> bytes:
    return (f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            ).encode("utf-8")


class AnthropicStreamTranslator:
    """Consumes OpenAI chat.completion.chunk dicts, emits Anthropic SSE events."""

    def __init__(self, model: str, msg_id: str | None = None) -> None:
        self.model = model
        self.msg_id = msg_id or f"msg_{uuid.uuid4().hex[:24]}"
        self.sent_message_start = False
        self.text_block_open = False
        self.next_index = 0
        # openai tool_call index -> (anthropic block index, id, name)
        self.tool_blocks: dict[int, tuple[int, str, str]] = {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.finish_reason: str | None = None

    # -- helpers ---------------------------------------------------------- #

    def _start(self) -> bytes:
        self.sent_message_start = True
        return _evt("message_start", {
            "type": "message_start",
            "message": {
                "id": self.msg_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

    def _open_text_block(self) -> bytes:
        self.text_block_open = True
        idx = self.next_index
        self.next_index += 1
        return _evt("content_block_start", {
            "type": "content_block_start",
            "index": idx,
            "content_block": {"type": "text", "text": ""},
        })

    # -- main ------------------------------------------------------------- #

    def feed(self, chunk: dict) -> list[bytes]:
        out: list[bytes] = []

        if not self.sent_message_start:
            out.append(self._start())

        usage = chunk.get("usage") or {}
        if usage:
            self.input_tokens = usage.get("prompt_tokens", self.input_tokens)
            self.output_tokens = usage.get("completion_tokens", self.output_tokens)

        for choice in (chunk.get("choices") or []):
            delta = choice.get("delta") or {}

            # a refusal arrives in its own field, not in `content`
            text = delta.get("content") or delta.get("refusal")
            if text:
                if not self.text_block_open:
                    out.append(self._open_text_block())
                out.append(_evt("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self.next_index - 1,
                    "delta": {"type": "text_delta", "text": text},
                }))

            for call in (delta.get("tool_calls") or []):
                oai_index = call.get("index", 0)
                fn = call.get("function") or {}

                if oai_index not in self.tool_blocks:
                    if self.text_block_open:
                        out.append(_evt("content_block_stop", {
                            "type": "content_block_stop",
                            "index": self.next_index - 1,
                        }))
                        self.text_block_open = False
                    block_index = self.next_index
                    self.next_index += 1
                    tool_id = call.get("id") or f"toolu_{uuid.uuid4().hex[:20]}"
                    name = fn.get("name") or ""
                    self.tool_blocks[oai_index] = (block_index, tool_id, name)
                    out.append(_evt("content_block_start", {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": name,
                            "input": {},
                        },
                    }))

                args = fn.get("arguments")
                if args:
                    block_index, _, _ = self.tool_blocks[oai_index]
                    out.append(_evt("content_block_delta", {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    }))

            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

        return out

    def finish(self) -> list[bytes]:
        out: list[bytes] = []
        if not self.sent_message_start:
            out.append(self._start())

        if self.text_block_open:
            out.append(_evt("content_block_stop", {
                "type": "content_block_stop",
                "index": self.next_index - 1,
            }))
            self.text_block_open = False

        for _, (block_index, _, _) in sorted(self.tool_blocks.items(),
                                             key=lambda kv: kv[1][0]):
            out.append(_evt("content_block_stop", {
                "type": "content_block_stop",
                "index": block_index,
            }))

        out.append(_evt("message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": _map_stop_reason(self.finish_reason) or "end_turn",
                "stop_sequence": None,
            },
            # OpenAI streams only reveal prompt_tokens at the end, so
            # message_start always said 0; carry the real input count here so
            # clients can recover totals (Anthropic's own field is
            # output_tokens; the extra key is additive and harmless).
            "usage": {"input_tokens": self.input_tokens,
                      "output_tokens": self.output_tokens},
        }))
        out.append(_evt("message_stop", {"type": "message_stop"}))
        return out


# --------------------------------------------------------------------------- #
# helpers for consumers
# --------------------------------------------------------------------------- #


def extract_openai_usage(chunks: list[dict]) -> dict:
    """Pull the final usage block out of a list of OpenAI chunks."""
    for chunk in reversed(chunks):
        usage = chunk.get("usage")
        if usage:
            return usage
    return {}


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def openai_completion_shell(model: str, msg_id: str | None = None) -> dict:
    return {
        "id": msg_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [],
    }

def extract_stream_error(text: str) -> dict | None:
    """Find an error frame inside an HTTP 200 SSE stream.

    Captured 2026-09-16: the upstream can answer 200 and *then* send
        data: {"error":{"code":"stream_initialization_failed","message":"..."}}
    An error inside a success stream must be surfaced as an error, never folded
    into assistant content.
    """
    import json as _json

    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            obj = _json.loads(payload)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("error"):
            err = obj["error"]
            if isinstance(err, dict):
                return {"code": str(err.get("code") or "stream_error"),
                        "message": str(err.get("message") or err),
                        "request_id": err.get("request_id"),
                        "type": err.get("type") or "stream_error"}
            return {"code": "stream_error", "message": str(err),
                    "request_id": None, "type": "stream_error"}
    return None


def stream_had_content(text: str) -> bool:
    """True if the stream carried any assistant text or tool call."""
    import json as _json

    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("data:") or "[DONE]" in line:
            continue
        try:
            obj = _json.loads(line[5:].strip())
        except Exception:
            continue
        for choice in (obj.get("choices") or []):
            delta = choice.get("delta") or {}
            if delta.get("content") or delta.get("tool_calls"):
                return True
    return False
