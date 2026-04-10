"""Normalized response types that abstract away provider-specific API formats.

``ChatResponse`` wraps a list of content blocks (``TextBlock`` for text,
``ToolUseBlock`` for tool calls).  Each provider converts its native response
format into these types so that ChatAgent and PlaybookExecutor can process responses
uniformly regardless of which LLM backend produced them.
"""

from __future__ import annotations

from dataclasses import dataclass


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

    @property
    def text_parts(self) -> list[str]:
        return [block.text for block in self.content if isinstance(block, TextBlock)]

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [block for block in self.content if isinstance(block, ToolUseBlock)]

    @property
    def has_tool_use(self) -> bool:
        return any(isinstance(block, ToolUseBlock) for block in self.content)
