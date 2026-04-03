"""Test providers for the chat agent evaluation framework.

ScriptedProvider returns pre-queued responses for deterministic testing.
RecordingProvider wraps a real provider and records all calls + responses.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from src.chat_providers.base import ChatProvider
from src.chat_providers.types import ChatResponse, TextBlock, ToolUseBlock


@dataclass
class ProviderCall:
    """Records a single create_message invocation."""

    messages: list[dict]
    system: str
    tools: list[dict] | None
    max_tokens: int
    response: ChatResponse
    timestamp: float = field(default_factory=time.time)


class ScriptedProvider(ChatProvider):
    """Returns pre-queued ChatResponse objects in FIFO order.

    Used for deterministic tests that verify chat loop mechanics without
    calling a real LLM.
    """

    def __init__(self):
        self._queue: list[ChatResponse] = []
        self._calls: list[ProviderCall] = []

    @property
    def model_name(self) -> str:
        return "scripted-test"

    def add_response(self, response: ChatResponse) -> None:
        """Queue a raw ChatResponse."""
        self._queue.append(response)

    def add_text(self, text: str) -> None:
        """Queue a text-only response."""
        self._queue.append(ChatResponse(content=[TextBlock(text=text)]))

    def add_tool_call(self, name: str, args: dict | None = None) -> None:
        """Queue a response containing a single tool use block."""
        self._queue.append(
            ChatResponse(
                content=[
                    ToolUseBlock(id=f"toolu_{uuid.uuid4().hex[:12]}", name=name, input=args or {}),
                ]
            )
        )

    def add_tool_calls(self, calls: list[tuple[str, dict]]) -> None:
        """Queue a response containing multiple tool use blocks."""
        blocks = [
            ToolUseBlock(id=f"toolu_{uuid.uuid4().hex[:12]}", name=name, input=args)
            for name, args in calls
        ]
        self._queue.append(ChatResponse(content=blocks))

    def add_reply(self, message: str) -> None:
        """Queue a reply_to_user tool call response.

        The supervisor requires reply_to_user to deliver responses after
        tool use.  This is the standard way to end a tool-use sequence.
        """
        self._queue.append(
            ChatResponse(
                content=[
                    ToolUseBlock(
                        id=f"toolu_{uuid.uuid4().hex[:12]}",
                        name="reply_to_user",
                        input={"message": message},
                    ),
                ]
            )
        )

    def add_tool_then_text(self, name: str, args: dict, text: str) -> None:
        """Queue a tool call followed by a reply_to_user delivery.

        This queues TWO responses: the tool call (which triggers execution),
        then a reply_to_user call to deliver the text as the final response.
        """
        self.add_tool_call(name, args)
        self.add_reply(text)

    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        if not self._queue:
            # Default: return empty text to end the loop
            response = ChatResponse(content=[TextBlock(text="(no more scripted responses)")])
        else:
            response = self._queue.pop(0)

        import copy

        self._calls.append(
            ProviderCall(
                messages=copy.deepcopy(messages),
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                response=response,
            )
        )
        return response

    @property
    def calls(self) -> list[ProviderCall]:
        return self._calls

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def reset(self) -> None:
        self._queue.clear()
        self._calls.clear()


class RecordingProvider(ChatProvider):
    """Wraps a real ChatProvider, recording all calls + responses + latency.

    Used for integration eval tests that measure LLM tool selection accuracy.
    """

    def __init__(self, inner: ChatProvider):
        self._inner = inner
        self._calls: list[ProviderCall] = []
        self._latencies: list[float] = []

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
        response = await self._inner.create_message(
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
        )
        elapsed = time.monotonic() - start
        self._latencies.append(elapsed)

        self._calls.append(
            ProviderCall(
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                response=response,
            )
        )
        return response

    @property
    def calls(self) -> list[ProviderCall]:
        return self._calls

    @property
    def latencies(self) -> list[float]:
        return self._latencies

    @property
    def total_latency(self) -> float:
        return sum(self._latencies)

    def reset(self) -> None:
        self._calls.clear()
        self._latencies.clear()
