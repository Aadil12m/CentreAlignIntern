"""A deterministic provider for tests, demos without an API key, and replays.

``policy`` is a function that receives the system prompt and the conversation so far and returns the
next assistant turn as a list of blocks: ``("text", "...")`` or
``("tool", name, {input})``. Because it sees real tool results it can react to
the environment, which lets the test suite drive the real harness, the real
browser and the real simulated company end to end.
"""

from __future__ import annotations

import itertools
from typing import Callable

from .base import LLMProvider, LLMResponse, ToolCall, Usage

Turn = list[tuple]


class ScriptedProvider(LLMProvider):
    name = "scripted"

    def __init__(self, policy: Callable[[str, list[dict]], Turn], structured_fn: Callable[[str, dict], dict] | None = None):
        self.policy = policy
        self.structured_fn = structured_fn or (lambda prompt, schema: {})
        self._ids = itertools.count(1)

    def complete(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        blocks, calls, text = [], [], ""
        for item in self.policy(system, messages):
            if item[0] == "text":
                text += item[1]
                blocks.append({"type": "text", "text": item[1]})
            else:
                call = ToolCall(f"toolu_{next(self._ids)}", item[1], item[2])
                calls.append(call)
                blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.input})
        stop = "tool_use" if calls else "end_turn"
        return LLMResponse(text, calls, stop, blocks, Usage(calls=1))

    def structured(self, *, system: str, prompt: str, schema: dict) -> tuple[dict, Usage]:
        return self.structured_fn(prompt, schema), Usage(calls=1)


def last_tool_result(messages: list[dict]) -> str:
    """Convenience for policies: text of the most recent tool result."""
    for msg in reversed(messages):
        if msg["role"] == "user" and isinstance(msg["content"], list):
            for block in msg["content"]:
                if block.get("type") == "tool_result":
                    return block["content"]
    return ""
