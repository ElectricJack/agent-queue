"""LLMClient — the direct LLM path: one ``complete()`` and one ``run_tools()``
(spec §3.2).  Owned by the orchestrator; consumers receive it, never build one."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from src.config import LLMConfig
from src.intelligence_classes import IntelligenceClass
from src.llm.providers import create_provider
from src.llm.providers.base import LLMProvider
from src.llm.spec import LLMCallSpec, ResolvedCall, resolve_call
from src.llm.types import ChatResponse, serialize_canonical
from src.llm_logger import LLMLogger

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: ChatResponse | None = None

    @classmethod
    def from_chat_response(cls, resp: ChatResponse) -> "LLMResponse":
        return cls(
            text="\n".join(resp.text_parts),
            tool_calls=[
                ToolCall(id=t.id, name=t.name, args=dict(t.input or {})) for t in resp.tool_uses
            ],
            raw=resp,
        )


def _as_messages(messages: list[dict] | str) -> list[dict]:
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    return list(messages)


class LLMClient:
    def __init__(
        self,
        config: LLMConfig,
        *,
        classes_loader: Callable[[], dict[str, IntelligenceClass]],
        llm_logger: LLMLogger | None = None,
        provider_factory: Callable[..., LLMProvider] = create_provider,
    ):
        self._config = config
        self._classes_loader = classes_loader
        self._logger = llm_logger
        self._factory = provider_factory
        self._providers: dict[tuple, LLMProvider] = {}

    @classmethod
    def with_provider(
        cls,
        provider: LLMProvider,
        *,
        config: LLMConfig | None = None,
        llm_logger: LLMLogger | None = None,
    ) -> "LLMClient":
        """A client whose every resolution yields *provider* (tests, dry runs)."""
        return cls(
            config or LLMConfig(),
            classes_loader=dict,
            llm_logger=llm_logger,
            provider_factory=lambda **_kw: provider,
        )

    @property
    def config(self) -> LLMConfig:
        return self._config

    # -- resolution --------------------------------------------------------

    def resolve(self, spec: LLMCallSpec) -> ResolvedCall:
        return resolve_call(spec, self._config, self._classes_loader())

    def _provider_for(self, resolved: ResolvedCall) -> LLMProvider:
        key = resolved.cache_key
        provider = self._providers.get(key)
        if provider is None:
            provider = self._factory(
                provider=resolved.provider,
                model=resolved.model,
                base_url=resolved.base_url,
                api_key=resolved.api_key,
                extras=dict(resolved.extras),
            )
            self._providers[key] = provider
        return provider

    def is_configured(self, spec: LLMCallSpec = LLMCallSpec()) -> bool:
        try:
            return bool(self._provider_for(self.resolve(spec)).is_configured)
        except Exception as exc:  # missing SDK, missing creds, bad id
            logger.debug("llm: not configured: %s", exc)
            return False

    async def is_model_loaded(self, spec: LLMCallSpec = LLMCallSpec()) -> bool:
        return await self._provider_for(self.resolve(spec)).is_model_loaded()

    # -- calls ---------------------------------------------------------------

    async def complete(
        self,
        messages: list[dict] | str,
        *,
        system: str = "",
        spec: LLMCallSpec = LLMCallSpec(),
    ) -> LLMResponse:
        resolved = self.resolve(spec)
        resp = await self._create_message(
            resolved, messages=_as_messages(messages), system=system, tools=None
        )
        return LLMResponse.from_chat_response(resp)

    async def _create_message(
        self,
        resolved: ResolvedCall,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None,
    ) -> ChatResponse:
        provider = self._provider_for(resolved)
        start = time.monotonic()
        response: ChatResponse | None = None
        error: str | None = None
        try:
            response = await provider.create_message(
                messages=messages, system=system, tools=tools, max_tokens=resolved.max_tokens
            )
            return response
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            if self._logger is not None:
                self._logger.log_llm_call(
                    caller=resolved.caller,
                    model=provider.model_name,
                    provider=type(provider).__name__,
                    messages=serialize_canonical(messages),
                    system=system,
                    tools=tools,
                    max_tokens=resolved.max_tokens,
                    response=response,
                    error=error,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
