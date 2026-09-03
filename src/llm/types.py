"""Normalized response types that abstract away provider-specific API formats.

``ChatResponse`` wraps a list of content blocks (``TextBlock`` for text,
``ToolUseBlock`` for tool calls).  Each provider converts its native response
format into these types so that ChatAgent and PlaybookExecutor can process responses
uniformly regardless of which LLM backend produced them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Counts returned by a provider for one request or a sequence of turns.

    ``reported`` is deliberately separate from the numeric fields: zero is a
    valid reported count, while a missing count must fail a hard budget closed.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    reported: bool = False

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reported=self.reported and other.reported,
        )


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ChatResponse:
    content: list[TextBlock | ToolUseBlock]
    usage: TokenUsage | None = None

    @property
    def text_parts(self) -> list[str]:
        return [block.text for block in self.content if isinstance(block, TextBlock)]

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [block for block in self.content if isinstance(block, ToolUseBlock)]

    @property
    def has_tool_use(self) -> bool:
        return any(isinstance(block, ToolUseBlock) for block in self.content)


def serialize_canonical(messages: list[dict]) -> list[dict]:
    """Convert messages containing dataclass instances to JSON-serializable dicts.

    The supervisor stores ``ToolUseBlock`` and ``TextBlock`` dataclass instances
    directly in the message history.  These are not JSON-serializable and render
    as opaque ``repr()`` strings when passed through ``json.dumps(default=str)``.

    This function walks the messages list and replaces any dataclass instances
    with plain dicts matching the Anthropic API wire format, so the result can
    be faithfully serialized to JSON for logging.
    """
    result = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            serialized = []
            for item in content:
                if isinstance(item, ToolUseBlock):
                    serialized.append({
                        "type": "tool_use",
                        "id": item.id,
                        "name": item.name,
                        "input": item.input,
                    })
                elif isinstance(item, TextBlock):
                    serialized.append({"type": "text", "text": item.text})
                else:
                    serialized.append(item)
            result.append({**msg, "content": serialized})
        else:
            result.append(msg)
    return result
