"""SSE framing must split on real line terminators only.

A JSON payload serialized with ensure_ascii=False can carry U+2028 (LINE
SEPARATOR), U+2029 (PARAGRAPH SEPARATOR) and U+0085 (NEL) raw inside string
values. str.splitlines() treats those as line breaks, which cut the payload
mid-string: a 500 on non-streaming /v1/chat/completions, a mid-stream 502
invalid_stream, and a stream with real content reported as empty.
"""
from __future__ import annotations

import json

import pytest

from zcode_gateway.translate_anthropic import extract_stream_error, stream_had_content
from zcode_gateway.upstream import iter_sse_events

UNICODE_LINE_BREAKS = ["\u2028", "\u2029", "\u0085"]


def _sse(*events, end="\r\n\r\n"):
    return "".join("data: " + json.dumps(e, ensure_ascii=False) + end for e in events)


@pytest.mark.parametrize("sep", UNICODE_LINE_BREAKS)
def test_line_separator_inside_payload_survives_parsing(sep):
    text = f"before{sep}after"
    frames = _sse({"id": "chatcmpl-1", "choices": [{"delta": {"content": text}}]},
                  {"id": "chatcmpl-1", "choices": [{"delta": {"content": "next"}}]})
    events = iter_sse_events(frames)
    assert [e["choices"][0]["delta"]["content"] for e in events] == [text, "next"]


@pytest.mark.parametrize("sep", UNICODE_LINE_BREAKS)
def test_error_frame_message_with_line_separator_is_detected(sep):
    message = f"stream died{sep}retry once"
    error = extract_stream_error(_sse(
        {"error": {"code": "stream_initialization_failed", "message": message}}, end="\n\n"))
    assert error == {"code": "stream_initialization_failed", "message": message,
                     "request_id": None, "type": "stream_error"}


@pytest.mark.parametrize("sep", UNICODE_LINE_BREAKS)
def test_content_with_line_separator_counts_as_content(sep):
    assert stream_had_content(_sse(
        {"choices": [{"delta": {"content": f"a{sep}b"}}]}, end="\n\n"))
    # role-only chunks still carry no content
    assert not stream_had_content(_sse(
        {"choices": [{"delta": {"role": "assistant"}}]}, end="\n\n"))
