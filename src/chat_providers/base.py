"""Common interface (ABC) for all LLM chat providers.

Every provider must implement ``create_message`` (send messages and receive a
normalized ``ChatResponse``) and expose a ``model_name`` property.  This
abstraction lets the rest of the codebase swap between Anthropic, Ollama, or
future providers without any changes to calling code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .types import ChatResponse


class ChatProvider(ABC):
    @abstractmethod
    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse: ...

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    async def is_model_loaded(self) -> bool:
        """Check whether the model is ready to serve requests.

        Returns ``True`` by default (most providers are always ready).
        Ollama overrides this to probe ``/api/ps`` for cold-start detection.
        """
        return True
