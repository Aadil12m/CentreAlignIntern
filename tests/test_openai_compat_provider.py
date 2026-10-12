"""The OpenAI-compatible adapter against a fake endpoint (no network, no key), including a full agent run through it."""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from taskpilot.agent.human import ScriptedHuman
from taskpilot.agent.runner import Agent, AgentConfig
from taskpilot.cli import require_credentials
from taskpilot.llm import LLMError, make_provider, tool_results_message
from taskpilot.llm.openai_compat import OpenAICompatProvider, to_openai_messages
from taskpilot.tools import ToolRegistry, ToolResult
from taskpilot.tools.base import Tool, obj
from taskpilot.tools.workspace import Finish

COMPANY = Path(__file__).resolve().parent.parent / "company"
TOOLS = [{"name": "browser_open", "description": "d",
          "input_schema": {"type": "object", "properties": {"url": {"type": "string", "default": "/"}}, "required": ["url"]}}]


def chat(content=None, tool_calls=None, finish="stop", usage=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    body = {"choices": [{"index": 0, "message": msg, "finish_reason": finish}]}
    if usage:
        body["usage"] = usage
    return body


def call(name, args, id="call_1"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": json.dumps(args) if isinstance(args, dict) else args}}


def make(replies, **kw):
    """Replies are consumed in order: an exception is raised, a (status, headers, body) tuple is returned, a dict is a 200."""
    seen, queue, sleeps = [], list(replies), []

    def transport(url, headers, payload, timeout):
        seen.append((url, headers, payload))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, hdrs, body = item if isinstance(item, tuple) else (200, {}, item)
        return status, hdrs, body if isinstance(body, str) else json.dumps(body)

    p = OpenAICompatProvider("some-model", "https://example.test/v1/", api_key="k", transport=transport, sleep=sleeps.append, **kw)
    return p, seen, sleeps


# ------------------------------------------------------------------ translation

def test_history_is_translated_to_chat_completions():
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "text", "text": "Opening."},
                                          {"type": "tool_use", "id": "c1", "name": "browser_open", "input": {"url": "/mail"}},
                                          {"type": "tool_use", "id": "c2", "name": "browser_open", "input": {"url": "/erp"}}]},
        tool_results_message([("c1", "mail page", False), ("c2", "ERROR (invalid): nope", True)]),
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c3", "name": "browser_open", "input": {}}]},
        {"role": "user", "content": "[independent verification FAILED]"},
    ]
    out = to_openai_messages("sys", history)
    assert out[0] == {"role": "system", "content": "sys"} and out[1] == {"role": "user", "content": "go"}
    assert out[2]["content"] == "Opening." and [c["id"] for c in out[2]["tool_calls"]] == ["c1", "c2"]
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == {"url": "/mail"}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "mail page"}
    assert out[4] == {"role": "tool", "tool_call_id": "c2", "content": "ERROR (invalid): nope"}
    assert out[5]["content"] is None and out[5]["tool_calls"][0]["id"] == "c3"  # a tool-only turn has no text
    assert out[6] == {"role": "user", "content": "[independent verification FAILED]"}


def test_user_text_next_to_tool_results_comes_after_them():
    msg = {"role": "user", "content": [{"type": "text", "text": "also this"},
                                       {"type": "tool_result", "tool_use_id": "c1", "content": "r", "is_error": False}]}
    out = to_openai_messages("s", [msg])
    assert [m["role"] for m in out[1:]] == ["tool", "user"]


# ------------------------------------------------------------------ complete()

def test_tool_call_round_trip_and_request_shape():
    p, seen, _ = make([chat("Opening mail.", [call("browser_open", {"url": "/mail"})], "tool_calls",
                            {"prompt_tokens": 100, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 40}})])
    r = p.complete(system="sys", messages=[{"role": "user", "content": "go"}], tools=TOOLS)
    assert r.text == "Opening mail." and r.stop_reason == "tool_use"
    assert [(c.id, c.name, c.input) for c in r.tool_calls] == [("call_1", "browser_open", {"url": "/mail"})]
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cache_read_tokens, r.usage.calls) == (60, 7, 40, 1)

    url, headers, payload = seen[0]
    assert url == "https://example.test/v1/chat/completions"  # trailing slash on the base URL is tolerated
    assert headers["Authorization"] == "Bearer k" and headers["User-Agent"].startswith("taskpilot")
    assert payload["model"] == "some-model" and payload["max_tokens"] == 8000
    fn = payload["tools"][0]["function"]
    assert fn["name"] == "browser_open" and fn["parameters"]["required"] == ["url"]
    assert "default" not in fn["parameters"]["properties"]["url"]  # some endpoints reject schema defaults

    # the assistant turn can be appended back as-is and is sent in the right shape
    history = [{"role": "user", "content": "go"}, {"role": "assistant", "content": r.assistant_content},
               tool_results_message([("call_1", "page", False)])]
    sent = to_openai_messages("sys", history)
    assert sent[2]["tool_calls"][0]["id"] == "call_1" and sent[3]["tool_call_id"] == "call_1"


def test_extra_content_on_tool_calls_is_sent_back_unchanged():
    """Gemini 3 puts a thought signature in extra_content and answers 400 if the next request leaves it out."""
    sig = {"google": {"thought_signature": "abc123=="}}
    p, _, _ = make([chat(None, [{**call("browser_open", {"url": "/mail"}, id="c1"), "extra_content": sig},
                                call("browser_open", {"url": "/erp"}, id="c2")], "tool_calls")])
    r = p.complete(system="s", messages=[{"role": "user", "content": "go"}], tools=TOOLS)
    sent = to_openai_messages("s", [{"role": "user", "content": "go"}, {"role": "assistant", "content": r.assistant_content}])
    assert sent[2]["tool_calls"][0]["extra_content"] == sig
    assert "extra_content" not in sent[2]["tool_calls"][1]  # calls without one stay as they were


def test_a_property_named_default_survives_schema_cleaning():
    p, seen, _ = make([chat("ok")])
    schema = {"type": "object", "properties": {"default": {"type": "string", "default": "x"}}}
    tools = [{"name": "t", "description": "d", "input_schema": schema}]
    p.complete(system="s", messages=[{"role": "user", "content": "go"}], tools=tools)
    assert seen[0][2]["tools"][0]["function"]["parameters"]["properties"] == {"default": {"type": "string"}}


def test_no_tools_key_when_there_are_no_tools_and_no_auth_header_without_a_key():
    p, seen, _ = make([chat("hi")])
    p.api_key = None
    p.complete(system="s", messages=[{"role": "user", "content": "go"}], tools=[])
    assert "tools" not in seen[0][2] and "Authorization" not in seen[0][1]


def test_missing_and_repeated_tool_call_ids_are_replaced():
    p, _, _ = make([chat(None, [call("a", {}, id=""), call("b", {}, id="x"), call("c", {}, id="x")], "tool_calls")])
    r = p.complete(system="s", messages=[{"role": "user", "content": "go"}], tools=TOOLS)
    ids = [c.id for c in r.tool_calls]
    assert len(set(ids)) == 3 and ids[1] == "x"
    assert [b["id"] for b in r.assistant_content if b["type"] == "tool_use"] == ids


def test_broken_tool_arguments_become_an_empty_call_the_tool_can_reject():
    p, _, _ = make([chat(None, [call("browser_open", '{"url": "/ma')], "length")])
    r = p.complete(system="s", messages=[{"role": "user", "content": "go"}], tools=TOOLS)
    assert r.tool_calls[0].input == {}


def test_stop_reasons_think_blocks_and_empty_replies():
    p, _, _ = make([chat("<think>hmm</think>Done.", finish="stop"), chat("cut", finish="length"), chat(None)])
    msgs = [{"role": "user", "content": "go"}]
    a, b, c = (p.complete(system="s", messages=msgs, tools=[]) for _ in range(3))
    assert (a.text, a.stop_reason, b.stop_reason) == ("Done.", "end_turn", "max_tokens")
    assert c.tool_calls == [] and c.assistant_content[0]["text"]  # never an empty assistant turn


def test_content_filter_is_reported_like_a_refusal():
    p, _, _ = make([chat("", finish="content_filter")])
    with pytest.raises(LLMError, match="declined"):
        p.complete(system="s", messages=[{"role": "user", "content": "go"}], tools=[])


# ------------------------------------------------------------------ HTTP behaviour

def test_rate_limits_are_retried_honouring_retry_after():
    p, seen, sleeps = make([(429, {"Retry-After": "7"}, "slow down"), (503, {}, "busy"), chat("ok")])
    assert p.complete(system="s", messages=[{"role": "user", "content": "go"}], tools=[]).text == "ok"
    assert len(seen) == 3 and sleeps == [7.0, 2]


def test_connection_errors_are_retried_then_reported():
    p, seen, sleeps = make([OSError("reset"), chat("ok")])
    assert p.complete(system="s", messages=[{"role": "user", "content": "go"}], tools=[]).text == "ok" and len(sleeps) == 1
    p, _, _ = make([OSError("down")] * 3, max_retries=2)
    with pytest.raises(LLMError, match="Could not reach"):
        p.complete(system="s", messages=[{"role": "user", "content": "go"}], tools=[])


def test_persistent_server_errors_give_up_with_the_body():
    p, seen, _ = make([(500, {}, "boom")] * 3, max_retries=2)
    with pytest.raises(LLMError, match="500: boom"):
        p.complete(system="s", messages=[{"role": "user", "content": "go"}], tools=[])
    assert len(seen) == 3


def test_bad_credentials_and_bad_requests_are_not_retried():
    p, seen, _ = make([(401, {}, "no")])
    with pytest.raises(LLMError, match="API key"):
        p.complete(system="s", messages=[{"role": "user", "content": "go"}], tools=[])
    p, seen, _ = make([(400, {}, "bad model")])
    with pytest.raises(LLMError, match="rejected"):
        p.complete(system="s", messages=[{"role": "user", "content": "go"}], tools=[])
    assert len(seen) == 1


def test_non_json_and_choiceless_bodies_are_errors():
    for body in ("<html>nope</html>", {"error": {"message": "quota"}}):
        p, _, _ = make([(200, {}, body)])
        with pytest.raises(LLMError):
            p.complete(system="s", messages=[{"role": "user", "content": "go"}], tools=[])


def test_default_transport_works_over_a_real_socket():
    """The urllib transport the CLI really uses, against a local HTTP server: headers, body, and a 429 that is retried."""
    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got.append((self.path, self.headers.get("Authorization"), self.headers.get("User-Agent"), body))
            if len(got) == 1:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                out = b"slow down"
            else:
                self.send_response(200)
                out = json.dumps(chat("pong", usage={"prompt_tokens": 3, "completion_tokens": 1})).encode()
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        p = make_provider("custom", model="m", base_url=f"http://127.0.0.1:{server.server_port}/v1", effort="high")
        p.sleep = lambda s: None
        r = p.complete(system="s", messages=[{"role": "user", "content": "ping"}], tools=[])
    finally:
        server.shutdown()
        server.server_close()
    assert r.text == "pong" and r.usage.output_tokens == 1 and len(got) == 2
    assert got[1][0] == "/v1/chat/completions" and got[1][2].startswith("taskpilot")
    assert got[1][1] is None and got[1][3]["messages"][-1] == {"role": "user", "content": "ping"}  # local endpoint: no key sent


# ------------------------------------------------------------------ structured()

SCHEMA = {"type": "object", "properties": {"lessons": {"type": "array"}}, "required": ["lessons"]}


def test_structured_output_uses_json_schema_and_embeds_the_schema_in_the_prompt():
    p, seen, _ = make([chat('{"lessons": []}', usage={"prompt_tokens": 5, "completion_tokens": 2})])
    out, usage = p.structured(system="S", prompt="P", schema=SCHEMA)
    assert out == {"lessons": []} and usage.calls == 1 and usage.input_tokens == 5
    payload = seen[0][2]
    assert payload["response_format"]["type"] == "json_schema" and payload["response_format"]["json_schema"]["schema"] == SCHEMA
    assert payload["messages"][0]["content"].startswith("S") and '"lessons"' in payload["messages"][0]["content"]


def test_structured_output_degrades_to_what_the_server_accepts_and_remembers_it():
    p, seen, _ = make([(400, {}, "no json_schema"), (400, {}, "no json_object"), chat('{"lessons": []}'), chat('{"lessons": [1]}')])
    assert p.structured(system="S", prompt="P", schema=SCHEMA)[0] == {"lessons": []}
    assert [("response_format" in s[2]) for s in seen] == [True, True, False]
    assert p.structured(system="S", prompt="P", schema=SCHEMA)[0] == {"lessons": [1]}
    assert len(seen) == 4 and "response_format" not in seen[3][2]  # no more wasted requests


def test_structured_output_tolerates_fences_and_chatter():
    p, _, _ = make([chat('```json\n{"lessons": []}\n```'), chat('<think>x</think>Sure! {"lessons": ["a"]} Hope that helps.')])
    assert p.structured(system="S", prompt="P", schema=SCHEMA)[0] == {"lessons": []}
    assert p.structured(system="S", prompt="P", schema=SCHEMA)[0] == {"lessons": ["a"]}


def test_structured_output_gets_one_corrective_retry_then_fails():
    p, seen, _ = make([chat("not json"), chat('{"other": 1}'), ])
    with pytest.raises(LLMError, match="missing required keys: lessons"):
        p.structured(system="S", prompt="P", schema=SCHEMA)
    assert "rejected" in seen[1][2]["messages"][-1]["content"] and len(seen) == 2
    p, _, _ = make([chat("not json"), chat('{"lessons": []}', usage={"prompt_tokens": 3, "completion_tokens": 1})])
    out, usage = p.structured(system="S", prompt="P", schema=SCHEMA)
    assert out == {"lessons": []} and usage.calls == 2  # both attempts are counted


# ------------------------------------------------------------------ provider resolution

def clean_env(monkeypatch):
    for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "TASKPILOT_LLM_API_KEY", "TASKPILOT_LLM_MODEL",
              "TASKPILOT_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def test_presets_resolve_endpoint_and_key(monkeypatch):
    clean_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gk")
    p = make_provider("groq", model="m", effort="high")
    assert p.base_url == "https://api.groq.com/openai/v1" and p.api_key == "gk" and p.model == "m"
    assert make_provider("ollama", model="llama", effort="low").api_key is None  # local, no key needed
    assert make_provider("gemini", model="m", api_key="x").base_url.startswith("https://generativelanguage.googleapis.com")


def test_missing_key_model_or_endpoint_is_a_clear_error(monkeypatch):
    clean_env(monkeypatch)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        make_provider("gemini", model="m")
    with pytest.raises(LLMError, match="needs a model"):
        make_provider("ollama")
    with pytest.raises(LLMError, match="needs an endpoint"):
        make_provider("custom", model="m")
    monkeypatch.setenv("TASKPILOT_LLM_MODEL", "from-env")
    assert make_provider("ollama").model == "from-env"
    p = make_provider("custom", base_url="http://127.0.0.1:9/v1")  # a local custom endpoint needs no key
    assert p.base_url == "http://127.0.0.1:9/v1"


def test_the_anthropic_model_variable_does_not_leak_into_other_providers(monkeypatch):
    clean_env(monkeypatch)
    monkeypatch.setenv("TASKPILOT_MODEL", "claude-opus-5-5")
    with pytest.raises(LLMError, match="needs a model"):
        make_provider("ollama")


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError):
        make_provider("nope")


def test_cli_credential_check_is_provider_aware(monkeypatch, capsys):
    clean_env(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    def ns(provider, model=None):
        return argparse.Namespace(provider=provider, model=model, effort="high", llm_base_url=None)

    assert not require_credentials(ns("anthropic")) and "--provider" in capsys.readouterr().err  # points at the alternative
    assert not require_credentials(ns("groq", "m")) and "GROQ_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert require_credentials(ns("groq", "m"))
    assert not require_credentials()  # a call without args still means anthropic, which has no key here
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert require_credentials() and require_credentials(ns("anthropic"))
    capsys.readouterr()


# ------------------------------------------------------------------ the real agent loop on top of the adapter

class Echo(Tool):
    name = "echo"
    input_schema = obj({"text": {"type": "string"}}, ["text"])

    def run(self, ctx, text):
        return ToolResult(True, f"echo:{text}")


def fill(schema):
    kinds = {"string": "x", "array": [], "boolean": False, "object": {}}
    return {k: kinds[v["type"]] for k, v in schema["properties"].items()}


def test_full_agent_run_through_the_adapter(tmp_path):
    """Understand -> tool call -> tool result -> finish -> independent verifier, all over the Chat Completions format."""
    seen = []

    def server(payload):
        seen.append(payload)
        msgs = payload["messages"]
        if payload.get("response_format", {}).get("type") == "json_schema":
            return chat(json.dumps(fill(payload["response_format"]["json_schema"]["schema"])))
        turn = sum(m["role"] == "assistant" for m in msgs)
        if msgs[0]["content"].startswith("You are an independent verifier"):
            return chat(None, [call("report_verdict", {"passed": True, "criteria": [], "problems": []}, "v1")], "tool_calls")
        if turn == 0:
            return chat("Let me check.", [call("echo", {"text": "hi"}, "e1")], "tool_calls")
        return chat(None, [call("finish", {"status": "completed", "summary": "done", "evidence": []}, "f1")], "tool_calls")

    p, _, _ = make([])
    p.transport = lambda url, headers, payload, timeout: (200, {}, json.dumps(server(payload)))  # a tiny stand-in "model"
    agent = Agent(p, base_url="http://127.0.0.1:1", company_dir=COMPANY, human=ScriptedHuman(), runs_dir=tmp_path,
                  registry=ToolRegistry([Echo(), Finish()]), config=AgentConfig(echo=False, reflect=False))
    r = agent.run("say hi")
    assert r.status == "completed" and r.verification["passed"] and r.steps == 2 and r.usage["calls"] >= 4

    executor_turns = [s for s in seen if "response_format" not in s and not s["messages"][0]["content"].startswith("You are an indep")]
    second = executor_turns[1]["messages"]  # what the model saw on its second turn
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "e1"
    assert second[-1] == {"role": "tool", "tool_call_id": "e1", "content": "echo:hi"}
