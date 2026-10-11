"""Claude via the official Anthropic Python SDK."""

from __future__ import annotations

import json
import os

import anthropic

from .base import LLMError, LLMProvider, LLMResponse, ToolCall, Usage

DEFAULT_MODEL = "claude-opus-5-5"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str | None = None, effort: str = "high", max_tokens: int = 16000, client=None):
        self.model = model or os.environ.get("TASKPILOT_MODEL", DEFAULT_MODEL)
        self.effort = effort
        self.max_tokens = max_tokens
        # The SDK already retries 408/409/429/5xx and connection errors with backoff.
        self.client = client or anthropic.Anthropic(max_retries=4)

    def _create(self, **kwargs):
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                thinking={"type": "adaptive"},
                cache_control={"type": "ephemeral"},
                # Server-side fallback if a safety classifier declines a request.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **kwargs,
            )
        except TypeError as e:  # raised by the SDK when no credentials are configured
            raise LLMError(f"No Anthropic credentials: set ANTHROPIC_API_KEY ({e})") from e
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic authentication failed: set ANTHROPIC_API_KEY") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"Anthropic rejected the request: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Could not reach the Anthropic API: {e}") from e
        if resp.stop_reason == "refusal":
            raise LLMError("The model declined this request (stop_reason=refusal).")
        return resp

    @staticmethod
    def _usage(resp) -> Usage:
        u = resp.usage
        return Usage(u.input_tokens, u.output_tokens, getattr(u, "cache_read_input_tokens", 0) or 0, 1)

    def complete(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        resp = self._create(
            system=system,
            messages=messages,
            tools=tools,
            output_config={"effort": self.effort},
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        return LLMResponse(text, calls, resp.stop_reason, resp.content, self._usage(resp))

    def structured(self, *, system: str, prompt: str, schema: dict) -> tuple[dict, Usage]:
        resp = self._create(
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return json.loads(text), self._usage(resp)
        except json.JSONDecodeError as e:
            raise LLMError(f"Structured output was not valid JSON: {text[:200]}") from e
