"""Tool abstraction shared by every capability the agent can use."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..agent.context import RunContext

MAX_OUTPUT_CHARS = 8000


@dataclass
class ToolResult:
    ok: bool
    output: str
    # transient: environment hiccup, may succeed later
    # invalid:   bad input / validation error, change the input
    # blocked:   policy or human said no, do not work around it
    # fatal:     cannot continue with this tool
    error_kind: str | None = None
    # True when repeating the exact call cannot cause a duplicate side effect.
    safe_to_retry: bool = False
    data: dict[str, Any] = field(default_factory=dict)
    terminal: bool = False  # the agent declared it is done

    def render(self) -> str:
        text = self.output
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + f"\n... [truncated {len(self.output) - MAX_OUTPUT_CHARS} chars]"
        if not self.ok:
            text = f"ERROR ({self.error_kind or 'error'}): {text}"
        return text


class Tool:
    name: str = ""
    description: str = ""
    input_schema: dict = {"type": "object", "properties": {}}
    # read tools never change the outside world; the verifier only gets these
    read_only: bool = True

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    def run(self, ctx: "RunContext", **kwargs) -> ToolResult:  # pragma: no cover - interface
        raise NotImplementedError


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.add(t)

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[dict]:
        return [t.spec() for t in self._tools.values()]

    def subset(self, names: list[str]) -> "ToolRegistry":
        return ToolRegistry([self._tools[n] for n in names if n in self._tools])


def obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}
