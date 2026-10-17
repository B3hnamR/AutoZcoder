"""Anthropic <-> internal translation tests."""

from __future__ import annotations

import json

from zcode_gateway.translate_anthropic import (
    AnthropicStreamTranslator,
    request_to_openai,
    response_to_anthropic,
    tool_choice_to_openai,
    tools_to_openai,
)

# --------------------------------------------------------------------------- #
# request
# --------------------------------------------------------------------------- #


def test_system_prompt_and_simple_text():
    req = {
        "model": "anthropic/claude-opus-5",
        "max_tokens": 100,
        "system": "be terse",
        "messages": [{"role": "user", "content": "hello"}],
    }
    out = request_to_openai(req, "anthropic/claude-opus-5")
    assert out["messages"][0] == {"role": "system", "content": "be terse"}
    assert out["messages"][1] == {"role": "user", "content": "hello"}
    assert out["max_tokens"] == 100
    assert out["model"] == "anthropic/claude-opus-5"


def test_system_as_block_list():
    req = {
        "model": "m", "max_tokens": 10,
        "system": [{"type": "text", "text": "one"},
                   {"type": "text", "text": "two"}],
        "messages": [{"role": "user", "content": "x"}],
    }
    out = request_to_openai(req, "m")
    assert out["messages"][0]["content"] == "one\n\ntwo"


def test_image_block_becomes_data_uri():
    req = {
        "model": "m", "max_tokens": 10,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png",
                                         "data": "AAAA"}},
        ]}],
    }
    out = request_to_openai(req, "m")
    content = out["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[1]["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_assistant_tool_use_becomes_tool_calls():
    req = {
        "model": "m", "max_tokens": 10,
        "messages": [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                 "input": {"city": "Berlin"}},
            ]},
        ],
    }
    out = request_to_openai(req, "m")
    assistant = out["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "checking"
    call = assistant["tool_calls"][0]
    assert call["id"] == "toolu_1"
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Berlin"}


def test_tool_result_becomes_tool_message():
    req = {
        "model": "m", "max_tokens": 10,
        "messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": "18C sunny"},
                {"type": "text", "text": "thanks"},
            ]},
        ],
    }
    out = request_to_openai(req, "m")
    assert out["messages"][0]["role"] == "tool"
    assert out["messages"][0]["tool_call_id"] == "toolu_1"
    assert out["messages"][0]["content"] == "18C sunny"
    assert out["messages"][1] == {"role": "user", "content": "thanks"}


def test_tools_conversion():
    tools = [{"name": "get_weather", "description": "d",
              "input_schema": {"type": "object",
                               "properties": {"city": {"type": "string"}}}}]
    out = tools_to_openai(tools)
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "get_weather"
    assert out[0]["function"]["parameters"]["properties"]["city"]["type"] == "string"


def test_tool_choice_mapping():
    assert tool_choice_to_openai({"type": "auto"}) == "auto"
    assert tool_choice_to_openai({"type": "any"}) == "required"
    assert tool_choice_to_openai({"type": "none"}) == "none"
    assert tool_choice_to_openai({"type": "tool", "name": "f"}) == {
        "type": "function", "function": {"name": "f"}}


def test_stop_sequences_and_temperature():
    req = {"model": "m", "max_tokens": 5, "messages": [],
           "stop_sequences": ["STOP"], "temperature": 0.3, "top_p": 0.9}
    out = request_to_openai(req, "m")
    assert out["stop"] == ["STOP"]
    assert out["temperature"] == 0.3
    assert out["top_p"] == 0.9


# --------------------------------------------------------------------------- #
# response
# --------------------------------------------------------------------------- #


def test_response_to_anthropic_text():
    openai_resp = {
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hi there"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }
    out = response_to_anthropic(openai_resp, "anthropic/claude-opus-5")
    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["content"] == [{"type": "text", "text": "hi there"}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 11, "output_tokens": 3}


def test_response_to_anthropic_tool_use():
    openai_resp = {
        "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "get_weather",
                                         "arguments": '{"city":"Berlin"}'}}],
        }}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    }
    out = response_to_anthropic(openai_resp, "m")
    assert out["stop_reason"] == "tool_use"
    block = out["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "get_weather"
    assert block["input"] == {"city": "Berlin"}


# --------------------------------------------------------------------------- #
# streaming
# --------------------------------------------------------------------------- #


def _text(chunk: dict) -> str:
    return b"".join(chunk).decode()


def test_stream_translator_text():
    tr = AnthropicStreamTranslator("anthropic/claude-opus-5", "msg_1")
    events = []
    events += tr.feed({"choices": [{"delta": {"role": "assistant"},
                                    "finish_reason": None}]})
    events += tr.feed({"choices": [{"delta": {"content": "Hel"},
                                    "finish_reason": None}]})
    events += tr.feed({"choices": [{"delta": {"content": "lo"},
                                    "finish_reason": None}]})
    events += tr.feed({"choices": [{"delta": {}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
    events += tr.finish()

    text = _text(events)
    assert "event: message_start" in text
    assert '"type": "text_delta", "text": "Hel"' in text
    assert '"type": "text_delta", "text": "lo"' in text
    assert "event: content_block_stop" in text
    assert "event: message_delta" in text
    assert '"stop_reason": "end_turn"' in text
    assert "event: message_stop" in text


def test_stream_translator_tool_calls():
    tr = AnthropicStreamTranslator("m", "msg_2")
    events = []
    events += tr.feed({"choices": [{"delta": {
        "tool_calls": [{"index": 0, "id": "call_a",
                        "function": {"name": "f", "arguments": '{"a"'}}],
    }, "finish_reason": None}]})
    events += tr.feed({"choices": [{"delta": {
        "tool_calls": [{"index": 0, "function": {"arguments": ":1}"}}],
    }, "finish_reason": "tool_calls"}]})
    events += tr.finish()

    text = _text(events)
    assert '"type": "tool_use", "id": "call_a", "name": "f"' in text
    assert '"partial_json": "{\\"a\\""' in text
    assert '"partial_json": ":1}"' in text
    assert '"stop_reason": "tool_use"' in text
