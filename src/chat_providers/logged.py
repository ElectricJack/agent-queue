"""Logging decorator for ChatProvider implementations.

``LoggedChatProvider`` wraps any ``ChatProvider`` and transparently logs
every ``create_message()`` call — including timing, inputs, outputs, and
errors — via an ``LLMLogger`` instance.  The caller tag (e.g.
"supervisor.chat", "supervisor.summarize") can be set per-call via the
:data:`caller_override` ContextVar so concurrent users don't stomp each
other's tags.

Usage::

    provider = create_chat_provider(config)
    logged = LoggedChatProvider(provider, logger, caller="supervisor.chat")
    response = await logged.create_message(messages=..., system=...)

The wrapper is intentionally thin: it delegates everything to the inner
provider and only adds timing + logging in a ``finally`` block so that
both successful responses and exceptions are captured.
"""

from __future__ import annotations

import contextvars
import time

from src.llm_logger import LLMLogger

from .base import ChatProvider
from .types import ChatResponse, serialize_canonical


# Per-asyncio-task caller override. When set, takes precedence over the
# instance's static ``_caller``; this lets concurrent ``Supervisor`` paths
# (chat / summarize / break_plan / observe) tag their own log entries
# without racing on a shared attribute.
caller_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "caller_override", default=None
)


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
            # Per-call ContextVar wins over the instance's static caller —
            # safe under concurrent Supervisor entry points.
            effective_caller = caller_override.get() or self._caller
            self._logger.log_chat_provider_call(
                caller=effective_caller,
                model=self._inner.model_name,
                provider=provider_name,
                messages=serialize_canonical(messages),
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                response=response,
                error=error,
                duration_ms=duration_ms,
            )
