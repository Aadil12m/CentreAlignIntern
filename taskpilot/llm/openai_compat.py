"""Any OpenAI-compatible chat endpoint (Gemini, Groq, OpenRouter, Ollama, ...), over plain HTTP.

The agent keeps its history in the Anthropic Messages shape (see base.py). This adapter translates it to the
Chat Completions format on every call and translates the reply back, so the runner never knows the difference.
It uses only the standard library, so it adds no dependency; the HTTP call is injectable for tests.

``effort`` is accepted for interface parity and ignored: there is no portable way to ask for it.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import urlparse

from .base import LLMError, LLMProvider, LLMResponse, ToolCall, Usage

# name -> (base URL, env var that holds the key). Model names change too often to hard-code: pass --model.
PRESETS: dict[str, tuple[str, str | None]] = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
    "custom": ("", None),  # any other endpoint: --llm-base-url or TASKPILOT_LLM_BASE_URL
}

Transport = Callable[[str, dict, dict, float], tuple[int, dict, str]]

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_STRUCTURED_FORMATS = ("json_schema", "json_object", None)  # tried in order; the first one the server accepts sticks


class RequestRejected(LLMError):
    """The server answered 4xx (other than auth or rate limit): the request itself is not acceptable."""


def urllib_transport(url: str, headers: dict, payload: dict, timeout: float) -> tuple[int, dict, str]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def _is_local(url: str) -> bool:
    return (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _clean_schema(node):
    """Drop ``default`` hints, which some endpoints reject in tool schemas. A property *named* default is kept."""
    if isinstance(node, list):
        return [_clean_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k == "default":
            continue
        out[k] = {name: _clean_schema(s) for name, s in v.items()} if k == "properties" and isinstance(v, dict) else _clean_schema(v)
    return out


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if m["role"] == "assistant":
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
            calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                     for b in blocks if b.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": _text_of(blocks) or (None if calls else "")}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        elif isinstance(content, str):
            out.append({"role": "user", "content": content})
        else:
            # Tool results must directly follow the assistant turn that made the calls, so they go first.
            for b in content:
                if b.get("type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": _text_of(b["content"])})
            text = _text_of(content)
            if text:
                out.append({"role": "user", "content": text})
    return out


def _parse_arguments(raw) -> dict:
    """A model that emits broken JSON gets an empty call, which the tool rejects, so it sees the error and retries."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _extract_json(text: str) -> dict:
    text = _FENCE.sub("", _THINK.sub("", text).strip()).strip()
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


class OpenAICompatProvider(LLMProvider):
    name = "openai-compat"

    def __init__(self, model: str, base_url: str, api_key: str | None = None, effort: str = "high", max_tokens: int = 8000,
                 transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep,
                 max_retries: int = 6, timeout: float = 180):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.max_retries = max_retries
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._structured_mode = 0

    @classmethod
    def from_name(cls, name: str, model: str | None = None, base_url: str | None = None, api_key: str | None = None, **kwargs):
        preset_url, key_env = PRESETS[name]
        url = base_url or preset_url or os.environ.get("TASKPILOT_LLM_BASE_URL", "")
        if not url:
            raise LLMError("Provider 'custom' needs an endpoint: pass --llm-base-url or set TASKPILOT_LLM_BASE_URL.")
        model = model or os.environ.get("TASKPILOT_LLM_MODEL")
        if not model:
            raise LLMError(f"Provider {name!r} needs a model: pass --model or set TASKPILOT_LLM_MODEL "
                           "(see the provider's docs for the exact name).")
        key = api_key or (os.environ.get(key_env) if key_env else None) or os.environ.get("TASKPILOT_LLM_API_KEY")
        if not key and not (name == "ollama" or _is_local(url)):
            raise LLMError(f"No API key for {name!r}: set {key_env or 'TASKPILOT_LLM_API_KEY'}.")
        return cls(model=model, base_url=url, api_key=key, **kwargs)

    # ------------------------------------------------------------------ HTTP
    def _post(self, payload: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json", "User-Agent": "taskpilot/0.1"}  # some CDNs block the default urllib agent
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        for attempt in range(self.max_retries + 1):
            last = attempt == self.max_retries
            try:
                status, resp_headers, body = self.transport(url, headers, payload, self.timeout)
            except OSError as e:  # connection errors and timeouts
                if last:
                    raise LLMError(f"Could not reach {url}: {e}") from e
                self.sleep(min(2 ** attempt, 30))
                continue
            if status == 429 or status >= 500:
                if last:
                    raise LLMError(f"{url} kept answering {status}: {body[:300]}")
                retry_after = {k.lower(): v for k, v in resp_headers.items()}.get("retry-after", "")
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = 0.0
                self.sleep(min(max(wait, 2 ** attempt), 60))
                continue
            if status in (401, 403):
                raise LLMError(f"{url} refused the credentials ({status}): check the API key. {body[:200]}")
            if status >= 400:
                raise RequestRejected(f"{url} rejected the request ({status}): {body[:500]}")
            try:
                data = json.loads(body)
            except ValueError as e:
                raise LLMError(f"{url} returned something that is not JSON: {body[:200]}") from e
            if not data.get("choices"):
                raise LLMError(f"{url} returned no choices: {body[:300]}")
            return data
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _usage(data: dict) -> Usage:
        u = data.get("usage") or {}
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        return Usage(max((u.get("prompt_tokens") or 0) - cached, 0), u.get("completion_tokens") or 0, cached, 1)

    # ------------------------------------------------------------------ interface
    def complete(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        payload = {"model": self.model, "max_tokens": self.max_tokens, "messages": to_openai_messages(system, messages)}
        if tools:
            payload["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                  "parameters": _clean_schema(t["input_schema"])}}
                                for t in tools]
        data = self._post(payload)
        choice = data["choices"][0]
        if choice.get("finish_reason") == "content_filter":
            raise LLMError("The model declined this request (finish_reason=content_filter).")
        msg = choice.get("message") or {}
        text = _THINK.sub("", _text_of(msg.get("content") or "")).strip()

        calls, seen = [], set()
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            call_id = tc.get("id")
            if not call_id or call_id in seen:  # some servers send empty or repeated ids
                call_id = f"tp_call_{next(self._ids)}"
            seen.add(call_id)
            calls.append(ToolCall(call_id, fn.get("name", ""), _parse_arguments(fn.get("arguments"))))

        blocks = ([{"type": "text", "text": text}] if text else []) + [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
        if not blocks:
            blocks = [{"type": "text", "text": "(empty response)"}]  # an empty assistant turn is rejected on the next call
        stop = "tool_use" if calls else {"length": "max_tokens"}.get(choice.get("finish_reason"), "end_turn")
        return LLMResponse(text, calls, stop, blocks, self._usage(data))

    def structured(self, *, system: str, prompt: str, schema: dict) -> tuple[dict, Usage]:
        instruction = ("Reply with exactly one JSON object that matches this JSON Schema. No prose, no code fences.\n"
                       + json.dumps(schema))
        messages = [{"role": "system", "content": f"{system}\n\n{instruction}"}, {"role": "user", "content": prompt}]
        usage = Usage()
        problem = ""
        for _ in range(2):  # one corrective retry: weaker models often need it
            data = self._structured_call(messages, schema)
            usage.add(self._usage(data))
            text = _text_of((data["choices"][0].get("message") or {}).get("content") or "")
            try:
                value = _extract_json(text)
                missing = [k for k in schema.get("required", []) if k not in value]
                if not missing:
                    return value, usage
                problem = f"the object is missing required keys: {', '.join(missing)}"
            except ValueError as e:
                problem = f"it was not valid JSON ({e})"
            messages += [{"role": "assistant", "content": text or "(empty)"},
                         {"role": "user", "content": f"That reply was rejected: {problem}. Reply again with only the JSON object."}]
        raise LLMError(f"Structured output failed: {problem}")

    def _structured_call(self, messages: list[dict], schema: dict) -> dict:
        while True:
            payload = {"model": self.model, "max_tokens": self.max_tokens, "messages": messages}
            kind = _STRUCTURED_FORMATS[self._structured_mode]
            if kind == "json_schema":
                payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}}
            elif kind == "json_object":
                payload["response_format"] = {"type": "json_object"}
            try:
                return self._post(payload)
            except RequestRejected:
                if self._structured_mode + 1 >= len(_STRUCTURED_FORMATS):
                    raise
                self._structured_mode += 1  # this server does not take that response_format; remember and degrade
