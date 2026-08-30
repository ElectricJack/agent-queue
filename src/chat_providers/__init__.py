"""Chat provider abstraction for the LLM control plane.

This package is used by the ChatAgent (Discord chat interface) and the
PlaybookExecutor (automated playbook runs) -- NOT by agent execution, which
goes through the platform layer in ``src/runtimes/``.

The factory function ``create_chat_provider`` selects between Anthropic
(direct API, Vertex AI, Bedrock, or Claude Code OAuth) and Ollama
(local/self-hosted via OpenAI-compatible endpoint) based on configuration.

See specs/chat-providers/providers.md for the full specification.
"""

from __future__ import annotations

from src.config import ChatProviderConfig

from .base import ChatProvider
from .logged import LoggedChatProvider
from .types import ChatResponse, TextBlock, ToolUseBlock


def create_chat_provider(config: ChatProviderConfig) -> ChatProvider | None:
    """Create a chat provider based on configuration.

    Returns None if the provider cannot be initialized (e.g. missing credentials).
    """
    from src.llm.providers import create_provider
    from src.config import normalize_llm_provider

    provider_id = normalize_llm_provider(config.provider)
    base_url = config.base_url or ("http://localhost:11434/v1" if config.provider == "ollama" else "")
    extras = {"thinking_budget": config.thinking_budget} if provider_id == "google" else {}
    try:
        provider = create_provider(
            provider=provider_id, model=config.model, base_url=base_url,
            api_key=config.api_key, extras=extras,
        )
    except Exception:
        return None
    return provider if provider.is_configured else None


__all__ = [
    "ChatProvider",
    "ChatResponse",
    "LoggedChatProvider",
    "TextBlock",
    "ToolUseBlock",
    "create_chat_provider",
]
