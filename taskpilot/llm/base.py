"""Provider-neutral interface the agent talks to.

The agent never imports a vendor SDK directly. Conversation history is kept in
the Anthropic Messages shape (a list of {"role", "content"} dicts with
tool_use / tool_result blocks) because it is a clean, explicit format; another
provider would translate to and from it inside its own adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.calls += other.calls


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    # Exactly what must be appended to history as the assistant turn. Kept
    # opaque so provider-specific blocks (e.g. thinking) survive round trips.
    assistant_content: Any
    usage: Usage = field(default_factory=Usage)


class LLMError(RuntimeError):
    pass


class LLMProvider(ABC):
    name: str = "provider"

    @abstractmethod
    def complete(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """One model turn that may call tools."""

    @abstractmethod
    def structured(self, *, system: str, prompt: str, schema: dict) -> tuple[dict, Usage]:
        """One model turn constrained to return JSON matching ``schema``."""


def tool_results_message(results: list[tuple[str, str, bool]]) -> dict:
    """Build the user turn carrying every tool result of one assistant turn."""
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": is_error}
            for tid, text, is_error in results
        ],
    }
