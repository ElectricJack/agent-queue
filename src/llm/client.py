"""LLMClient — the direct LLM path: one ``complete()`` and one ``run_tools()``
(spec §3.2).  Owned by the orchestrator; consumers receive it, never build one."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from src.config import LLMConfig
from src.intelligence_classes import IntelligenceClass
from src.llm.providers import create_provider
from src.llm.providers.base import LLMProvider
from src.llm.spec import LLMCallSpec, ResolvedCall, resolve_call
from src.llm.types import ChatResponse, TokenUsage, serialize_canonical
from src.llm_logger import LLMLogger

logger = logging.getLogger(__name__)


@dataclass
class LLMRunResult:
    text: str
    transcript: list[dict]
    turns: int
    stopped_by: str  # "done" | "max_turns" | "cancelled" | "interrupted"
    tool_calls_made: list[str] = field(default_factory=list)
    usage: TokenUsage | None = None


ProgressCallback = Callable[[str, str | None], Awaitable[None]]
ToolExecutor = Callable[[str, dict], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class LLMToolTurn:
    """One tool-loop boundary, safe for a durable caller to receipt.

    Raw tool results stay in ``transcript_delta`` for the private run snapshot;
    operator-visible receipts use only ``tool_call_ids`` and ``results_digest``.
    An interrupted partial turn deliberately carries no delta.
    """

    kind: Literal["tool_turn", "interrupted"]
    turn_index: int
    tool_call_ids: tuple[str, ...]
    results_digest: str
    usage: TokenUsage
    transcript_delta: tuple[dict, ...] = ()
    principal: Any | None = None


ToolTurnCallback = Callable[[LLMToolTurn], Awaitable[None]]


class LLMToolTurnBoundaryError(RuntimeError):
    """A durable turn callback failed; this is not a provider failure."""


def _json_safe(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str)
    except Exception:
        return json.dumps({"result": str(obj)})


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
    usage: TokenUsage | None = None

    @classmethod
    def from_chat_response(cls, resp: ChatResponse) -> "LLMResponse":
        return cls(
            text="\n".join(resp.text_parts),
            tool_calls=[
                ToolCall(id=t.id, name=t.name, args=dict(t.input or {})) for t in resp.tool_uses
            ],
            raw=resp,
            usage=resp.usage,
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

    async def run_tools(
        self,
        messages: list[dict] | str,
        tools: list[dict],
        execute: ToolExecutor,
        *,
        system: str = "",
        spec: LLMCallSpec = LLMCallSpec(),
        max_turns: int = 25,
        on_progress: ProgressCallback | None = None,
        on_tool_turn: ToolTurnCallback | None = None,
        initial_turn_index: int = 0,
        cancel_event: asyncio.Event | None = None,
        timeout_seconds: float | None = None,
    ) -> LLMRunResult:
        """Caller-supplied tool loop.  Tool errors become tool results; the loop
        ends when the model answers without tool calls, on ``max_turns``, or on
        ``cancel_event``."""
        resolved = self.resolve(spec)
        transcript = _as_messages(messages)
        offered = {t["name"] for t in tools}
        made: list[str] = []
        turns = 0
        usage: TokenUsage | None = None
        deadline = (
            asyncio.get_running_loop().time() + timeout_seconds
            if timeout_seconds is not None
            else None
        )

        async def _progress(kind: str, detail: str | None = None) -> None:
            if on_progress is not None:
                await on_progress(kind, detail)

        async def _turn_boundary(turn: LLMToolTurn) -> None:
            if on_tool_turn is not None:
                try:
                    await on_tool_turn(turn)
                except Exception as exc:
                    raise LLMToolTurnBoundaryError(
                        "durable LLM tool-turn callback failed"
                    ) from exc

        async def _within_deadline(operation: Callable[[], Awaitable[Any]]) -> Any:
            if deadline is None:
                return await operation()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                return await operation()

        def _results_digest(results: list[dict]) -> str:
            payload = json.dumps(
                results,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
            return hashlib.sha256(payload.encode()).hexdigest()

        while True:
            if cancel_event is not None and cancel_event.is_set():
                await _progress("cancelled")
                return LLMRunResult(
                    "", transcript, turns, "cancelled", made, usage or TokenUsage()
                )
            if turns >= max_turns:
                return LLMRunResult(
                    "", transcript, turns, "max_turns", made, usage or TokenUsage()
                )

            await _progress("thinking", None if turns == 0 else f"round {turns + 1}")
            try:
                resp = await _within_deadline(
                    lambda: self._create_message(
                        resolved,
                        messages=transcript,
                        system=system,
                        tools=tools or None,
                    )
                )
            except asyncio.CancelledError:
                if on_tool_turn is None:
                    raise
                await _turn_boundary(
                    LLMToolTurn(
                        kind="interrupted",
                        turn_index=initial_turn_index + turns,
                        tool_call_ids=(),
                        results_digest=_results_digest([]),
                        usage=TokenUsage(),
                    )
                )
                return LLMRunResult(
                    "", transcript, turns, "interrupted", made, usage or TokenUsage()
                )
            turns += 1
            call_usage = resp.usage or TokenUsage()
            usage = call_usage if usage is None else usage + call_usage

            if not resp.has_tool_use:
                await _progress("responding")
                text = "\n".join(resp.text_parts).strip()
                transcript.append({"role": "assistant", "content": text})
                return LLMRunResult(text, transcript, turns, "done", made, usage)

            transcript.append({"role": "assistant", "content": resp.tool_uses})
            results: list[dict] = []
            for call in resp.tool_uses:
                await _progress("tool_use", call.name)
                made.append(call.name)
                if call.name not in offered:
                    result: Any = {
                        "success": False,
                        "error": f"Tool '{call.name}' is not available in this call",
                    }
                else:
                    try:
                        result = await _within_deadline(
                            lambda: execute(call.name, dict(call.input or {}))
                        )
                    except asyncio.CancelledError:
                        if on_tool_turn is None:
                            raise
                        await _turn_boundary(
                            LLMToolTurn(
                                kind="interrupted",
                                turn_index=initial_turn_index + turns - 1,
                                tool_call_ids=tuple(item.id for item in resp.tool_uses),
                                results_digest=_results_digest(results),
                                usage=call_usage,
                            )
                        )
                        return LLMRunResult(
                            "", transcript, turns, "interrupted", made, usage
                        )
                    except Exception as exc:
                        logger.warning(
                            "llm.run_tools: tool %s raised: %s", call.name, exc
                        )
                        result = {"success": False, "error": str(exc)}
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": _json_safe(result),
                    }
                )
            result_message = {"role": "user", "content": results}
            transcript.append(result_message)
            assistant_message = serialize_canonical(
                [{"role": "assistant", "content": resp.tool_uses}]
            )[0]
            await _turn_boundary(
                LLMToolTurn(
                    kind="tool_turn",
                    turn_index=initial_turn_index + turns - 1,
                    tool_call_ids=tuple(call.id for call in resp.tool_uses),
                    results_digest=_results_digest(results),
                    usage=call_usage,
                    transcript_delta=(assistant_message, result_message),
                )
            )

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
