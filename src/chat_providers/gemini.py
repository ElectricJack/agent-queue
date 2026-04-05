"""Google Gemini chat provider using the ``google-genai`` SDK.

Supports both the Gemini API (API key auth) and Vertex AI.  Like the
Ollama provider, the main complexity is format conversion: the rest of
the codebase uses Anthropic-style tool definitions and message
structures, so this module translates between the two formats on every
request and response.
"""

from __future__ import annotations

import json
import os
import uuid

from .base import ChatProvider
from .types import ChatResponse, TextBlock, ToolUseBlock


class GeminiChatProvider(ChatProvider):
    """Chat provider using Google's Gemini models via google-genai SDK."""

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        api_key: str = "",
    ):
        from google import genai

        resolved_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
        self._client = genai.Client(api_key=resolved_key)
        self._model = str(model) if model else "gemini-2.5-flash"

    @property
    def model_name(self) -> str:
        return self._model

    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
        )
        if tools:
            config.tools = self._convert_tools(tools)

        gemini_contents = self._convert_messages(messages)

        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=gemini_contents,
            config=config,
        )

        return self._parse_response(response)

    # ── Tool definition conversion ────────────────────────────────────

    @staticmethod
    def _convert_tools(anthropic_tools: list[dict]) -> list:
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

    # ── Message conversion ────────────────────────────────────────────

    @staticmethod
    def _convert_messages(messages: list[dict]) -> list:
        """Convert Anthropic-format messages to Gemini Content objects."""
        from google.genai import types

        # Build tool_use_id → function name map from assistant messages
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
                        # Parse JSON string results into dicts for FunctionResponse
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

    # ── Response parsing ──────────────────────────────────────────────

    @staticmethod
    def _parse_response(response) -> ChatResponse:
        """Convert a Gemini response into our normalized ChatResponse."""
        content: list[TextBlock | ToolUseBlock] = []

        if not response.candidates:
            return ChatResponse(content=[TextBlock(text="")])

        for part in response.candidates[0].content.parts:
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

        return ChatResponse(content=content)


# ── Schema conversion helper ──────────────────────────────────────────

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
        # Gemini requires enum values to be strings — filter out None
        kwargs["enum"] = [v for v in schema["enum"] if v is not None]

    if schema_type == "object" and "properties" in schema:
        kwargs["properties"] = {
            name: _convert_schema(prop)
            for name, prop in schema["properties"].items()
        }
        if "required" in schema:
            kwargs["required"] = schema["required"]

    if schema_type == "array" and "items" in schema:
        kwargs["items"] = _convert_schema(schema["items"])

    return types.Schema(**kwargs)
