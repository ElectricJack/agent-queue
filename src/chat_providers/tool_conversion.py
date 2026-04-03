"""Convert Anthropic tool definitions to OpenAI function-calling format.

This exists solely to bridge the format gap for the Ollama provider, which
speaks OpenAI's API.  The rest of the codebase defines tools in Anthropic
format (``name``, ``description``, ``input_schema``); this module maps them
to OpenAI format (``type: "function"``, ``function.parameters``).
"""

from __future__ import annotations


def anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic-format tool definitions to OpenAI function-calling format.

    Anthropic format:
        {"name": ..., "description": ..., "input_schema": {...}}

    OpenAI format:
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
    """
    result = []
    for tool in tools:
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
