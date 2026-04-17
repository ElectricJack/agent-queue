"""Adapter for converting Anthropic-format requests to OpenAI function-calling format.

Used by the Ollama provider (which speaks OpenAI's API) and any future
providers that use the OpenAI-compatible endpoint format.
"""

from __future__ import annotations

import json
import uuid

from ..types import ChatResponse, TextBlock, ToolUseBlock


def convert_tools(anthropic_tools: list[dict]) -> list[dict]:
    """Convert Anthropic-format tool definitions to OpenAI function-calling format.

    Anthropic format:
        {"name": ..., "description": ..., "input_schema": {...}}

    OpenAI format:
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
    """
    result = []
    for tool in anthropic_tools:
        result.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return result


def convert_messages(messages: list[dict], system: str) -> list[dict]:
    """Convert Anthropic-format messages to OpenAI format.

    - System prompt becomes the first message with role ``"system"``.
    - Tool results become ``{"role": "tool", "tool_call_id": ..., "content": ...}``.
    - Assistant tool uses become ``tool_calls`` list on the assistant message.
    """
    result: list[dict] = [{"role": "system", "content": system}]

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if role == "user" and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    result.append(
                        {
                            "role": "tool",
                            "tool_call_id": item["tool_use_id"],
                            "content": item.get("content", ""),
                        }
                    )
                elif isinstance(item, dict) and item.get("type") == "text":
                    result.append({"role": "user", "content": item["text"]})
                else:
                    result.append({"role": "user", "content": str(item)})

        elif role == "assistant" and isinstance(content, list):
            text_parts = []
            tool_calls = []
            for item in content:
                if hasattr(item, "text"):
                    text_parts.append(item.text)
                elif hasattr(item, "name") and hasattr(item, "input"):
                    tool_calls.append(
                        {
                            "id": item.id,
                            "type": "function",
                            "function": {
                                "name": item.name,
                                "arguments": json.dumps(item.input),
                            },
                        }
                    )

            assistant_msg: dict = {"role": "assistant"}
            if text_parts:
                assistant_msg["content"] = "\n".join(text_parts)
            else:
                assistant_msg["content"] = None
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            result.append(assistant_msg)

        else:
            result.append({"role": role, "content": content})

    return result


def parse_response(response: object) -> ChatResponse:
    """Parse an OpenAI-format chat completion response into a ChatResponse."""
    choice = response.choices[0]
    content: list[TextBlock | ToolUseBlock] = []

    if choice.message.content:
        content.append(TextBlock(text=choice.message.content))

    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            args = tc.function.arguments
            if isinstance(args, str):
                args = json.loads(args)
            content.append(
                ToolUseBlock(
                    id=tc.id or str(uuid.uuid4())[:8],
                    name=tc.function.name,
                    input=args,
                )
            )

    return ChatResponse(content=content)
