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
from src.llm.spec import (
    LLMCallSpec,
    NoFallbackRoute,
    ResolvedCall,
    resolve_call,
    resolve_fallback_call,
)
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
    last_usage: TokenUsage | None = None


ProgressCallback = Callable[[str, str | None], Awaitable[None]]
ToolExecutor = Callable[[str, dict], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class LLMToolTurn:
    """One tool-loop boundary, safe for a durable caller to receipt.

    Raw tool results stay in ``transcript_delta`` for the private run snapshot;
    operator-visible receipts use only ``tool_call_ids`` and ``results_digest``.
    An interrupted partial turn deliberately carries no delta.
    """

    kind: Literal["tool_turn", "llm_call", "interrupted"]
    turn_index: int
    tool_call_ids: tuple[str, ...]
    results_digest: str
    usage: TokenUsage
    transcript_delta: tuple[dict, ...] = ()
    principal: Any | None = None


ToolTurnCallback = Callable[[LLMToolTurn], Awaitable[None]]


class LLMToolTurnBoundaryError(RuntimeError):
    """A durable turn callback failed; this is not a provider failure."""

    def __init__(self, message: str, turn: LLMToolTurn):
        super().__init__(message)
        self.turn = turn


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
        #: Called with ``(signal, detail)`` after every provider call:
        #: ``("ok", {})`` or :func:`classify_llm_error`'s verdict.  The
        #: orchestrator wires it to provider availability's ``llm`` key
        #: (provider-failover D13a).  Must be cheap and must not raise.
        self.on_outcome: Callable[[str, dict], None] | None = None
        #: Returns why direct-path calls must not be made right now, or
        #: ``None``.  The orchestrator wires it to provider availability's
        #: ``llm`` key: while that is unavailable every call is made with
        #: ``llm.fallback`` when one is configured and can serve it, and
        #: otherwise fails fast with :class:`ProviderUnavailableError`
        #: (provider-failover D13a).
        self.availability_gate: Callable[[], str | None] | None = None
        # Whether the last gated call went to the fallback; only so the
        # switch in each direction is logged once, not on every call.
        self._serving_fallback = False

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

    def cli_settings(self, class_id: str, harness: str) -> tuple[str, str, str]:
        """Resolve a profile's logged-in CLI model without API credentials."""
        from src.intelligence_classes import resolve_class
        from src.profiles.intelligence import provider_for_harness

        provider = provider_for_harness(harness)
        cls = self._classes_loader().get(class_id)
        if not provider or cls is None:
            raise LookupError("CLI harness or intelligence class unavailable")
        setting = resolve_class(cls, "codex") if harness == "codex" else {}
        if not setting:
            setting = resolve_class(cls, provider)
        model = str(setting.get("model") or "")
        if not model:
            raise LookupError("intelligence class has no model for CLI harness")
        effort = str(setting.get("reasoning_effort") or setting.get("thinking") or "")
        return provider, model, effort

    def resolve_current(self, spec: LLMCallSpec) -> ResolvedCall:
        """What a call on *spec* made right now would run on.

        The ``llm.fallback`` resolution while the primary credential is
        unavailable and the fallback can serve *spec*; otherwise the primary
        one.  Makes no call and logs no switch -- it is for naming the model
        in a receipt, not for routing.
        """
        resolved = self.resolve(spec)
        if self._block_reason() and self._config.fallback is not None:
            try:
                return resolve_fallback_call(spec, self._config, self._classes_loader())
            except NoFallbackRoute:
                pass
        return resolved

    def _block_reason(self) -> str | None:
        gate = self.availability_gate
        if gate is None:
            return None
        try:
            return gate()
        except Exception:  # a broken gate must never block the direct path
            logger.debug("llm: availability gate failed", exc_info=True)
            return None

    def _resolve_gated(self, spec: LLMCallSpec, resolved: ResolvedCall) -> ResolvedCall:
        """The resolution a call made right now uses (provider-failover D13a).

        *resolved* while the primary credential is available; otherwise the
        call resolved against ``llm.fallback``.  Raises
        :class:`ProviderUnavailableError` when there is no fallback or it
        cannot serve this call, without calling any vendor.
        """
        blocked = self._block_reason()
        if not blocked:
            if self._serving_fallback:
                self._serving_fallback = False
                logger.info("llm: primary credential is back; direct-path calls use it again")
            return resolved
        from src.llm.providers.errors import ProviderUnavailableError

        try:
            fallback = resolve_fallback_call(spec, self._config, self._classes_loader())
        except NoFallbackRoute as exc:
            if self._config.fallback is None:
                raise ProviderUnavailableError(blocked) from None
            raise ProviderUnavailableError(f"{blocked}; {exc}") from None
        if not self._serving_fallback:
            self._serving_fallback = True
            logger.warning(
                "llm: %s — direct-path calls use llm.fallback (provider %s)",
                blocked,
                fallback.provider,
            )
        return fallback

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
            resolved, spec=spec, messages=_as_messages(messages), system=system, tools=None
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
                        "durable LLM tool-turn callback failed", turn
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
                        spec=spec,
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
                return LLMRunResult(
                    text, transcript, turns, "done", made, usage, call_usage
                )

            transcript.append({"role": "assistant", "content": resp.tool_uses})
            results: list[dict] = []
            for call in resp.tool_uses:
                if cancel_event is not None and cancel_event.is_set():
                    await _progress("cancelled")
                    return LLMRunResult(
                        "", transcript, turns, "cancelled", made, usage, call_usage
                    )
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
                    except TimeoutError:
                        raise
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
                            "", transcript, turns, "interrupted", made, usage, call_usage
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

    def _report_outcome(self, signal: str, detail: dict) -> None:
        hook = self.on_outcome
        if hook is None:
            return
        try:
            hook(signal, detail)
        except Exception:  # evidence must never break an LLM call
            logger.debug("llm: outcome hook failed", exc_info=True)

    async def _create_message(
        self,
        resolved: ResolvedCall,
        *,
        spec: LLMCallSpec,
        messages: list[dict],
        system: str,
        tools: list[dict] | None,
    ) -> ChatResponse:
        # Re-evaluated on every call, so a tool loop that outlives an outage
        # (or starts inside one) moves between the credentials turn by turn.
        resolved = self._resolve_gated(spec, resolved)
        # Only the primary's calls are ``llm`` evidence: the fallback's say
        # nothing about the primary credential, and a success there must not
        # read as the primary's recovery.
        primary = resolved.credential == "llm"
        provider = self._provider_for(resolved)
        start = time.monotonic()
        response: ChatResponse | None = None
        error: str | None = None
        try:
            response = await provider.create_message(
                messages=messages, system=system, tools=tools, max_tokens=resolved.max_tokens
            )
            if primary:
                self._report_outcome("ok", {})
            return response
        except Exception as exc:
            error = str(exc)
            if primary:
                from src.llm.providers.errors import classify_llm_error

                signal, detail = classify_llm_error(exc)
                if signal is not None:
                    self._report_outcome(signal, detail)
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
