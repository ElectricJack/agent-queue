"""Common interface for the direct-LLM provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.llm.types import ChatResponse


class LLMProvider(ABC):
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

    @property
    def is_configured(self) -> bool:
        """False when credentials are missing; the client reports it without calling."""
        return True

    async def is_model_loaded(self) -> bool:
        return True
