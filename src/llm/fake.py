"""FakeProvider — scripted responses for tests and dry runs."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from src.llm.providers.base import LLMProvider
from src.llm.types import ChatResponse, TextBlock, ToolUseBlock


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

    def add_response(self, response: ChatResponse) -> None:
        self._queue.append(response)

    def add_text(self, text: str) -> None:
        self._queue.append(ChatResponse(content=[TextBlock(text=text)]))

    def add_tool_call(self, name: str, args: dict | None = None) -> None:
        self._queue.append(
            ChatResponse(
                content=[ToolUseBlock(id=f"toolu_{uuid.uuid4().hex[:12]}", name=name, input=args or {})]
            )
        )

    def add_tool_calls(self, calls: list[tuple[str, dict]]) -> None:
        self._queue.append(
            ChatResponse(
                content=[
                    ToolUseBlock(id=f"toolu_{uuid.uuid4().hex[:12]}", name=n, input=a)
                    for n, a in calls
                ]
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
