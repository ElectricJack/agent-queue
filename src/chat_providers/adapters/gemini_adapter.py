"""Adapter for converting Anthropic-format requests to Google Gemini format.

All functions use lazy imports for ``google.genai.types`` so the module
can be imported even when the ``google-genai`` SDK is not installed.
"""

from __future__ import annotations

import json
import uuid

from ..types import ChatResponse, TextBlock, ToolUseBlock


# ── Tool definition conversion ────────────────────────────────────────


def convert_tools(anthropic_tools: list[dict]) -> list:
    """Convert Anthropic-format tool defs to Gemini FunctionDeclarations."""
    from google.genai import types

    declarations = []
    for tool in anthropic_tools:
        schema = tool.get("input_schema", {})
        declarations.append(
            types.FunctionDeclaration(
                name=tool["name"],
                description=tool.get("description", ""),
                parameters=_convert_schema(schema) if schema.get("properties") else None,
            )
        )
    return [types.Tool(function_declarations=declarations)]


# ── Message conversion ────────────────────────────────────────────────


def convert_messages(messages: list[dict]) -> list:
    """Convert Anthropic-format messages to Gemini Content objects."""
    from google.genai import types

    # Build tool_use_id -> function name map from assistant messages
    # so we can resolve tool_result blocks to function names (Gemini
    # requires the function name, not the opaque call ID).
    id_to_name: dict[str, str] = {}
    for msg in messages:
        if msg["role"] == "assistant" and isinstance(msg["content"], list):
            for item in msg["content"]:
                if hasattr(item, "id") and hasattr(item, "name"):
                    id_to_name[item.id] = item.name

    result = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if role == "user" and isinstance(content, list):
            # Tool result blocks
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    raw = item.get("content", "")
                    if isinstance(raw, str):
                        try:
                            parsed = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            parsed = {"result": raw}
                    else:
                        parsed = raw if isinstance(raw, dict) else {"result": raw}
                    tool_id = item.get("tool_use_id", "unknown")
                    parts.append(
                        types.Part.from_function_response(
                            name=id_to_name.get(tool_id, tool_id),
                            response=parsed,
                        )
                    )
                elif isinstance(item, dict) and item.get("type") == "text":
                    parts.append(types.Part.from_text(text=item["text"]))
                else:
                    parts.append(types.Part.from_text(text=str(item)))
            if parts:
                result.append(types.Content(role="user", parts=parts))

        elif role == "assistant" and isinstance(content, list):
            # ToolUseBlock dataclasses from the supervisor's message history
            parts = []
            for item in content:
                if hasattr(item, "text"):
                    parts.append(types.Part.from_text(text=item.text))
                elif hasattr(item, "name") and hasattr(item, "input"):
                    parts.append(
                        types.Part.from_function_call(
                            name=item.name,
                            args=item.input,
                        )
                    )
            if parts:
                result.append(types.Content(role="model", parts=parts))

        elif role == "assistant":
            result.append(
                types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=content if content else "")],
                )
            )
        else:
            result.append(
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=content if content else "")],
                )
            )

    return result


# ── Response parsing ──────────────────────────────────────────────────


def parse_response(response: object) -> ChatResponse:
    """Convert a Gemini response into our normalized ChatResponse."""
    content: list[TextBlock | ToolUseBlock] = []

    if not response.candidates:
        return ChatResponse(content=[TextBlock(text="")])

    candidate = response.candidates[0]
    if not candidate.content or not candidate.content.parts:
        return ChatResponse(content=[TextBlock(text="")])

    for part in candidate.content.parts:
        if part.text:
            content.append(TextBlock(text=part.text))
        elif part.function_call:
            fc = part.function_call
            args = dict(fc.args) if fc.args else {}
            content.append(
                ToolUseBlock(
                    id=str(uuid.uuid4())[:8],
                    name=fc.name,
                    input=args,
                )
            )

    return ChatResponse(content=content or [TextBlock(text="")])


# ── Schema conversion helper ─────────────────────────────────────────


def _convert_schema(schema: dict):
    """Recursively convert a JSON Schema dict to a Gemini types.Schema."""
    from google.genai import types

    _TYPE_MAP = {
        "string": "STRING",
        "number": "NUMBER",
        "integer": "INTEGER",
        "boolean": "BOOLEAN",
        "array": "ARRAY",
        "object": "OBJECT",
    }

    schema_type = schema.get("type", "object")
    # JSON Schema allows type to be a list for unions, e.g. ["string", "null"].
    # Gemini doesn't support union types, so pick the first non-null type.
    if isinstance(schema_type, list):
        non_null = [t for t in schema_type if t != "null"]
        schema_type = non_null[0] if non_null else "string"
    kwargs: dict = {
        "type": _TYPE_MAP.get(schema_type, "OBJECT"),
    }

    if "description" in schema:
        kwargs["description"] = schema["description"]

    if "enum" in schema:
        kwargs["enum"] = [v for v in schema["enum"] if v is not None]

    if schema_type == "object" and "properties" in schema:
        kwargs["properties"] = {
            name: _convert_schema(prop) for name, prop in schema["properties"].items()
        }
        if "required" in schema:
            kwargs["required"] = schema["required"]

    if schema_type == "array" and "items" in schema:
        kwargs["items"] = _convert_schema(schema["items"])

    return types.Schema(**kwargs)
