"""Checks the real SDK request/response path against a mocked HTTP transport (no API key needed)."""

import json

import anthropic
import httpx2 as httpx

from taskpilot.llm.anthropic_provider import AnthropicProvider
from taskpilot.llm.base import tool_results_message


def make(reply: dict, seen: list):
    def handler(request: httpx.Request):
        seen.append(json.loads(request.content))
        base = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}
        return httpx.Response(200, json={**base, **reply})

    client = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return AnthropicProvider(client=client)


def test_tool_call_round_trip():
    seen = []
    p = make({"stop_reason": "tool_use", "content": [
        {"type": "text", "text": "Opening mail."},
        {"type": "tool_use", "id": "toolu_1", "name": "browser_open", "input": {"url": "/mail"}}]}, seen)
    tools = [{"name": "browser_open", "description": "d", "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}}}]
    r = p.complete(system="sys", messages=[{"role": "user", "content": "go"}], tools=tools)
    assert r.tool_calls[0].name == "browser_open" and r.tool_calls[0].input == {"url": "/mail"}
    body = seen[0]
    assert body["model"] == "claude-opus-5-5" and body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "high" and body["fallbacks"] == "default"
    # the assistant turn can be appended back as-is, followed by tool results
    msgs = [{"role": "user", "content": "go"}, {"role": "assistant", "content": r.assistant_content},
            tool_results_message([("toolu_1", "page", False)])]
    p.complete(system="sys", messages=msgs, tools=tools)
    assert seen[1]["messages"][1]["content"][1]["type"] == "tool_use"
    assert seen[1]["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"


def test_structured_output():
    seen = []
    p = make({"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"lessons": []}'}]}, seen)
    out, usage = p.structured(system="s", prompt="p", schema={"type": "object"})
    assert out == {"lessons": []} and usage.calls == 1
    assert seen[0]["output_config"]["format"]["type"] == "json_schema"
