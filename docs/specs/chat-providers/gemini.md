---
tags: [spec, chat-providers, gemini, llm]
---

# Gemini Provider — Specification

Implements [[base|ChatProvider]] interface.

Defined in `src/chat_providers/gemini.py`. Class: `GeminiChatProvider`.

Uses the `google-genai` SDK to communicate with Google's Gemini models. Like the Ollama provider, the main complexity is format conversion: the rest of the codebase uses Anthropic-style tool definitions and message structures, so this module translates between the two formats on every request and response.

## Initialization

```python
def __init__(
    self,
    model: str = "gemini-2.5-flash",
    api_key: str = "",
):
```

Constructs a `google.genai.Client` with the resolved API key.

**API key resolution order:**

1. The `api_key` constructor argument (from `ChatProviderConfig.api_key`)
2. `GEMINI_API_KEY` environment variable
3. `GOOGLE_API_KEY` environment variable
4. Empty string (may still work if Application Default Credentials are configured)

## Message Creation

```python
async def create_message(
    self,
    *,
    messages: list[dict],
    system: str,
    tools: list[dict] | None = None,
    max_tokens: int = 1024,
) -> ChatResponse:
```

Steps:

1. Build a `GenerateContentConfig` with `system_instruction=system` and `max_output_tokens=max_tokens`.
2. If `tools` is provided, convert via `_convert_tools` and attach to the config.
3. Convert `messages` to Gemini `Content` objects via `_convert_messages`.
4. Call `self._client.aio.models.generate_content(model=..., contents=..., config=...)`.
5. Parse the response via `_parse_response`.

## Tool Definition Conversion

Static method `_convert_tools(anthropic_tools) -> list`.

Converts Anthropic-format tool definitions to a list containing a single `types.Tool` with all function declarations. Each tool becomes a `FunctionDeclaration` with:

- `name`: tool name
- `description`: tool description (defaults to `""`)
- `parameters`: the `input_schema` recursively converted to a `types.Schema` object via `_convert_schema`. Set to `None` if the schema has no `properties`.

## Message Format Conversion

Static method `_convert_messages(messages) -> list`.

Converts Anthropic-format conversation history to Gemini `Content` objects. The system prompt is not included here — it is passed via `GenerateContentConfig.system_instruction`.

**Pre-processing:** Before converting, the method builds a `tool_use_id → function_name` mapping from assistant messages. This is needed because Gemini's `FunctionResponse` requires the function name (not the opaque tool call ID used by Anthropic).

**Per-message conversion rules:**

| Incoming role | Incoming content type | Output |
|---|---|---|
| `"user"` | `list` | Iterate items. `tool_result` dicts become `Part.from_function_response(name=..., response=...)` where the name is looked up from the pre-built ID-to-name map and the content is JSON-parsed (falling back to `{"result": raw}` on parse failure). `text` dicts become `Part.from_text(...)`. Other items are stringified as text parts. All parts are combined into a single `Content(role="user")`. |
| `"assistant"` | `list` | Items with a `.text` attribute become `Part.from_text(...)`. Items with `.name` and `.input` attributes become `Part.from_function_call(name=..., args=...)`. Combined into a single `Content(role="model")`. |
| `"assistant"` | non-list | Single `Content(role="model")` with one text part. |
| any other role | non-list | Single `Content(role="user")` with one text part. |

Note: Gemini uses `"model"` instead of `"assistant"` for assistant messages.

## Response Parsing

Static method `_parse_response(response) -> ChatResponse`.

Inspects `response.candidates[0].content.parts`:

- Parts with `.text` become `TextBlock(text=...)`.
- Parts with `.function_call` become `ToolUseBlock(id=<random-8-char-uuid>, name=fc.name, input=dict(fc.args))`. Gemini does not provide tool call IDs, so a random 8-character UUID prefix is generated.

**Empty / missing response guards** (returns a `ChatResponse` with a single empty `TextBlock` in each case):

- If `response.candidates` is empty or falsy.
- If the first candidate's `.content` is `None` or its `.content.parts` is `None`/empty (e.g., safety-filtered responses where Gemini omits content entirely).
- If none of the parts matched as text or function_call, so the assembled `content` list is empty after iteration.

## Schema Conversion Helper

Module-level function `_convert_schema(schema: dict) -> types.Schema`.

Recursively converts a JSON Schema dict to a Gemini `types.Schema` object. This is necessary because Gemini does not accept raw JSON Schema — it requires its own `Schema` type.

**Type mapping:**

| JSON Schema type | Gemini type |
|---|---|
| `"string"` | `"STRING"` |
| `"number"` | `"NUMBER"` |
| `"integer"` | `"INTEGER"` |
| `"boolean"` | `"BOOLEAN"` |
| `"array"` | `"ARRAY"` |
| `"object"` | `"OBJECT"` |

**Special handling:**

- **Union types:** JSON Schema allows `type` to be a list (e.g., `["string", "null"]`). Gemini doesn't support unions, so the first non-`"null"` type is selected.
- **Enum values:** Gemini requires enum values to be strings. `None` values are filtered out from enum lists.
- **Object properties:** Recursively converted. `required` fields are passed through.
- **Array items:** The `items` schema is recursively converted.
- **Description:** Preserved when present.
