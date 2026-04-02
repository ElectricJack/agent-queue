"""Chat provider abstraction for the LLM control plane.

This package is used by the ChatAgent (Discord chat interface) and the
HookEngine (automated event-driven hooks) -- NOT by agent execution, which
goes through the separate AgentAdapter layer in ``src/adapters/``.

The factory function ``create_chat_provider`` selects between Anthropic
(direct API, Vertex AI, Bedrock, or Claude Code OAuth) and Ollama
(local/self-hosted via OpenAI-compatible endpoint) based on configuration.

See specs/chat-providers/providers.md for the full specification.
"""
from __future__ import annotations

from src.config import ChatProviderConfig

from .anthropic import AnthropicChatProvider
from .base import ChatProvider
from .logged import LoggedChatProvider
from .types import ChatResponse, TextBlock, ToolUseBlock


def create_chat_provider(config: ChatProviderConfig) -> ChatProvider | None:
    """Create a chat provider based on configuration.

    Returns None if the provider cannot be initialized (e.g. missing credentials).
    """
    if config.provider == "ollama":
        from .ollama import OllamaChatProvider

        return OllamaChatProvider(
            model=config.model or "qwen3.5:35b",
            base_url=config.base_url or "http://localhost:11434/v1",
            keep_alive=config.keep_alive or "1h",
            num_ctx=config.num_ctx or 0,
        )

    # Default: anthropic
    provider = AnthropicChatProvider(model=config.model)
    if not provider.is_configured:
        return None
    return provider


__all__ = [
    "ChatProvider",
    "ChatResponse",
    "LoggedChatProvider",
    "TextBlock",
    "ToolUseBlock",
    "create_chat_provider",
]
