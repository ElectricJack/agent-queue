---
tags: [spec, chat-providers, ollama, llm]
---

# Ollama Provider — Specification

Implements [[chat-providers/base|ChatProvider]] interface.

Defined in `src/chat_providers/ollama.py`. Class: `OllamaChatProvider`.

## Initialization

```python
def __init__(
    self,
    model: str = "qwen2.5:32b-instruct-q3_K_M",
    base_url: str = "http://localhost:11434/v1",
    keep_alive: str = "1h",
    num_ctx: int = 0,
):
```

Constructs an `openai.AsyncOpenAI` client pointed at the given `base_url`. Ollama's API does not require authentication, so the `api_key` is set to the literal string `"ollama"` to satisfy the SDK's required-parameter check.

**Additional initialization:**

- `keep_alive` controls how long Ollama keeps the model loaded in memory after the last request. Accepts Go-style duration strings (`"1h"`, `"30m"`, `"5s"`, `"1h30m"`), bare integers (seconds), `"-1"` (infinite), or `"0"` (immediate unload). Parsed into seconds by `_parse_duration()`.
- `num_ctx` sets the context window size for Ollama. `0` means use the model's default.
- `_last_request_at` tracks the monotonic timestamp of the last successful response, used by `is_model_loaded()` to skip network probes.
- The Ollama API root (for non-OpenAI endpoints like `/api/ps`) is derived by stripping the `/v1` suffix from `base_url`.

## Model Loading Check

```python
async def is_model_loaded(self) -> bool:
```

Checks whether the configured model is currently loaded in Ollama's memory.

**Fast path:** If a successful response was received within 90% of the `keep_alive` window, returns `True` immediately without a network call — the model is guaranteed to still be loaded.

**Slow path:** Hits Ollama's `/api/ps` endpoint to list running models. Compares base model names (stripping the tag/version suffix after `:`). Returns `True` if found, `False` if not (cold start or timed-out model).

**Fail-open:** Returns `True` on any error, so callers never block on a failed probe.

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

1. Convert the incoming `messages` + `system` to OpenAI format via `_convert_messages`.
2. Build kwargs: `model`, `max_tokens`, `messages`, and an `extra_body` dict containing `keep_alive` and (when `num_ctx > 0`) `options.num_ctx` for Ollama-specific settings.
3. If `tools` is provided, convert via `anthropic_tools_to_openai` and add as `tools` kwarg.
4. Call `self._client.chat.completions.create(**kwargs)`.
5. Record the current monotonic time in `_last_request_at` so `is_model_loaded()` can skip probes while the model is warm.
6. Inspect `resp.choices[0]`:
   - If `choice.message.content` is non-empty, append a `TextBlock`.
   - If `choice.message.tool_calls` is non-empty, iterate and append one `ToolUseBlock` per tool call. The `arguments` field of each function call may arrive as a JSON string; if so it is parsed with `json.loads`. The tool call `id` is used as-is; if absent or falsy a random 8-character UUID prefix is generated via `uuid.uuid4()`.
6. Return `ChatResponse(content=content)`.

## Message Format Conversion

Static method `_convert_messages(messages, system) -> list[dict]`.

Converts Anthropic-format conversation history to OpenAI chat format.

**Output always starts with:**

```python
{"role": "system", "content": system}
```

**Per-message conversion rules:**

| Incoming role | Incoming content type | Output |
|---|---|---|
| `"user"` | `list` | Iterate items. Each `dict` with `type == "tool_result"` becomes a `{"role": "tool", "tool_call_id": item["tool_use_id"], "content": item.get("content", "")}` message. Each `dict` with `type == "text"` becomes a `{"role": "user", "content": item["text"]}` message. Any other item is stringified and emitted as `{"role": "user", "content": str(item)}`. |
| `"assistant"` | `list` | Items are split into text parts (objects with a `.text` attribute) and tool calls (objects with `.name` and `.input` attributes). A single assistant message is assembled: `content` is the newline-joined text parts, or `None` if there are none. `tool_calls` is added only when tool call objects were found, serialized as OpenAI function call dicts with `arguments` JSON-encoded from `.input`. |
| any role | non-list | Pass through as `{"role": role, "content": content}` unchanged. |

Note: list-type `"user"` content containing `tool_result` blocks produces multiple separate messages in the output list, one per block, rather than a single batched message.

---

## Tool Conversion Utility

Defined in `src/chat_providers/tool_conversion.py`.

```python
def anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
```

Converts a list of tool definitions from Anthropic format to OpenAI function-calling format.

**Anthropic input format (per tool):**

```json
{
  "name": "tool_name",
  "description": "What the tool does",
  "input_schema": {
    "type": "object",
    "properties": { ... },
    "required": [...]
  }
}
```

**OpenAI output format (per tool):**

```json
{
  "type": "function",
  "function": {
    "name": "tool_name",
    "description": "What the tool does",
    "parameters": {
      "type": "object",
      "properties": { ... },
      "required": [...]
    }
  }
}
```

The mapping is:
- `input_schema` -> `parameters` (the dict is used as-is, not deep-copied)
- `description` defaults to `""` when absent
- `input_schema` defaults to `{"type": "object", "properties": {}}` when absent

This function is called by `OllamaChatProvider.create_message` before passing tools to the OpenAI SDK. It is not used by `AnthropicChatProvider` or `GeminiChatProvider`, which have their own format requirements.
