from .base import LLMError, LLMProvider, LLMResponse, ToolCall, Usage, tool_results_message

__all__ = ["LLMError", "LLMProvider", "LLMResponse", "ToolCall", "Usage", "tool_results_message", "make_provider", "PROVIDERS"]

PROVIDERS = ("anthropic", "gemini", "groq", "openrouter", "ollama", "custom")


def make_provider(name: str = "anthropic", **kwargs) -> LLMProvider:
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(**kwargs)
    if name in PROVIDERS:
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider.from_name(name, **kwargs)
    raise ValueError(f"Unknown provider {name!r}")
