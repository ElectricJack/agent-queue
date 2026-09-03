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

    @property
    def reports_usage(self) -> bool:
        """Whether this adapter can return provider token counts before a call.

        The conservative default makes new adapters ineligible for a hard
        total-token budget until they opt in deliberately.
        """
        return False

    async def is_model_loaded(self) -> bool:
        return True
