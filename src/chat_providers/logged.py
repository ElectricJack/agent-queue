"""Logging decorator for ChatProvider implementations.

``LoggedChatProvider`` wraps any ``ChatProvider`` and transparently logs
every ``create_message()`` call — including timing, inputs, outputs, and
errors — via an ``LLMLogger`` instance.  The caller can set the ``caller``
attribute to tag log entries with the call site (e.g. "supervisor.chat",
"playbook_executor", "plan_parser").

Usage::

    provider = create_chat_provider(config)
    logged = LoggedChatProvider(provider, logger, caller="supervisor.chat")
    response = await logged.create_message(messages=..., system=...)

The wrapper is intentionally thin: it delegates everything to the inner
provider and only adds timing + logging in a ``finally`` block so that
both successful responses and exceptions are captured.
"""

from __future__ import annotations

import time

from src.llm_logger import LLMLogger

from .base import ChatProvider
from .types import ChatResponse


class LoggedChatProvider(ChatProvider):
    """ChatProvider wrapper that logs every create_message() call."""

    def __init__(
        self,
        inner: ChatProvider,
        logger: LLMLogger,
        caller: str = "unknown",
    ):
        self._inner = inner
        self._logger = logger
        self._caller = caller

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    async def is_model_loaded(self) -> bool:
        return await self._inner.is_model_loaded()

    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        start = time.monotonic()
        response = None
        error = None

        try:
            response = await self._inner.create_message(
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
            )
            return response
        except Exception as e:
            error = str(e)
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            # Determine provider name from inner class
            provider_name = type(self._inner).__name__
            self._logger.log_chat_provider_call(
                caller=self._caller,
                model=self._inner.model_name,
                provider=provider_name,
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                response=response,
                error=error,
                duration_ms=duration_ms,
            )
