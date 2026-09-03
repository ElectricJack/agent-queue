"""FakeProvider — scripted responses for tests and dry runs."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from src.llm.providers.base import LLMProvider
from src.llm.types import ChatResponse, TextBlock, TokenUsage, ToolUseBlock


@dataclass
class RecordedCall:
    messages: list[dict]
    system: str
    tools: list[dict] | None
    max_tokens: int
    timestamp: float = field(default_factory=time.time)


class FakeProvider(LLMProvider):
    """Returns queued ``ChatResponse`` objects FIFO and records every call."""

    def __init__(self, model_name: str = "fake"):
        self._queue: list[ChatResponse] = []
        self.calls: list[RecordedCall] = []
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def reports_usage(self) -> bool:
        """Expose the scripted provider capability for hard-budget tests."""
        return bool(self._queue) and all(
            response.usage is not None and response.usage.reported
            for response in self._queue
        )

    def add_response(self, response: ChatResponse, *, usage: TokenUsage | None = None) -> None:
        if usage is not None:
            response.usage = usage
        self._queue.append(response)

    def add_text(self, text: str, *, usage: TokenUsage | None = None) -> None:
        self._queue.append(ChatResponse(content=[TextBlock(text=text)], usage=usage))

    def add_tool_call(
        self, name: str, args: dict | None = None, *, usage: TokenUsage | None = None
    ) -> None:
        self._queue.append(
            ChatResponse(
                content=[ToolUseBlock(id=f"toolu_{uuid.uuid4().hex[:12]}", name=name, input=args or {})],
                usage=usage,
            )
        )

    def add_tool_calls(self, calls: list[tuple[str, dict]], *, usage: TokenUsage | None = None) -> None:
        self._queue.append(
            ChatResponse(
                content=[
                    ToolUseBlock(id=f"toolu_{uuid.uuid4().hex[:12]}", name=n, input=a)
                    for n, a in calls
                ],
                usage=usage,
            )
        )

    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        self.calls.append(RecordedCall(list(messages), system, tools, max_tokens))
        if not self._queue:
            raise RuntimeError("FakeProvider: no scripted response left")
        return self._queue.pop(0)
