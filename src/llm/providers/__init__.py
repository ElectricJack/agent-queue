"""Provider adapters for the direct LLM path.  Only ``create_provider`` is
used outside this package; nothing else constructs an adapter."""

from __future__ import annotations

from .base import LLMProvider


def create_provider(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    extras: dict,
) -> LLMProvider:
    """Build one adapter.  ``extras`` is the intelligence-class slice minus ``model``
    (``thinking``, ``thinking_budget``, ``reasoning_effort``); unknown keys are ignored."""
    if provider == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(
            model=model,
            api_key=api_key,
            thinking_budget=_anthropic_budget(extras.get("thinking")),
        )
    if provider == "google":
        from .google import GoogleProvider

        return GoogleProvider(
            model=model or "gemini-2.5-flash",
            api_key=api_key,
            thinking_budget=int(extras.get("thinking_budget", 8192)),
        )
    if provider == "openai":
        from .openai import OpenAIProvider

        return OpenAIProvider(
            model=model,
            base_url=base_url,
            api_key=api_key,
            reasoning_effort=str(extras.get("reasoning_effort", "") or ""),
        )
    raise ValueError(f"unknown llm provider {provider!r}")


_THINKING_BUDGETS = {"off": 0, "low": 1024, "medium": 4096, "high": 16000}


def _anthropic_budget(level) -> int:
    if isinstance(level, int):
        return level
    return _THINKING_BUDGETS.get(str(level or "off"), 0)


__all__ = ["LLMProvider", "create_provider"]
