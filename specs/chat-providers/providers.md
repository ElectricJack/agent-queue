# Chat Providers — Specification

## 1. Overview

The `src/chat_providers/` package provides a thin, uniform abstraction over LLM providers used for chat interactions. It is consumed exclusively by `ChatAgent` (`src/chat_agent.py`) to drive the Discord-facing conversational interface.

The abstraction serves two purposes:

1. Allow `ChatAgent` to call a single `create_message` method regardless of which backend is configured, receiving a normalized response object it can inspect without knowing provider-specific details.
2. Keep provider-specific credential detection, format conversion, and SDK calls isolated from the rest of the codebase.

The package contains eight modules:

| Module | Responsibility |
|---|---|
| `types.py` | Normalized response dataclasses (`TextBlock`, `ToolUseBlock`, `ChatResponse`) |
| `base.py` | Abstract base class `ChatProvider` |
| `anthropic.py` | Anthropic-backed implementation (direct API, Vertex AI, Bedrock, OAuth) |
| `ollama.py` | Ollama-backed implementation (OpenAI-compatible `/v1` endpoint) |
| `gemini.py` | Google Gemini-backed implementation (Gemini API or Vertex AI via `google-genai` SDK) |
| `logged.py` | `LoggedChatProvider` decorator that wraps any provider with timing and logging |
| `tool_conversion.py` | Utility to convert Anthropic tool schemas to OpenAI function-calling format |
| `__init__.py` | Public surface and `create_chat_provider` factory |

---

## Source Files

- `src/chat_providers/base.py`
- `src/chat_providers/types.py`
- `src/chat_providers/tool_conversion.py`
- `src/chat_providers/__init__.py`
- `src/chat_providers/anthropic.py`
- `src/chat_providers/ollama.py`
- `src/chat_providers/gemini.py`
- `src/chat_providers/logged.py`

---

## 2. Response Types

Defined in `src/chat_providers/types.py`. All types are plain dataclasses with no external dependencies.

### TextBlock

Represents a plain-text segment within a model response.

```python
@dataclass
class TextBlock:
    text: str
```

### ToolUseBlock

Represents a single tool invocation requested by the model.

```python
@dataclass
class ToolUseBlock:
    id: str      # Unique identifier for this tool call (used to match tool results)
    name: str    # Name of the tool being invoked
    input: dict  # Parsed arguments for the tool call
```

### ChatResponse

The single return type from every `create_message` call. Contains an ordered list of content blocks that may be any mix of `TextBlock` and `ToolUseBlock`.

```python
@dataclass
class ChatResponse:
    content: list[TextBlock | ToolUseBlock]
```

**Helper properties:**

| Property | Type | Description |
|---|---|---|
| `text_parts` | `list[str]` | Extracts the `.text` value from every `TextBlock` in `content`, in order. |
| `tool_uses` | `list[ToolUseBlock]` | Filters `content` to only `ToolUseBlock` entries. |
| `has_tool_use` | `bool` | `True` when at least one `ToolUseBlock` is present in `content`. |

All three properties are computed on access with no caching; `content` is the single source of truth.

---

## 3. ChatProvider Interface

Defined in `src/chat_providers/base.py`.

```python
class ChatProvider(ABC):
    @abstractmethod
    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse: ...

    @property
    @abstractmethod
    def model_name(self) -> str: ...
```

All parameters to `create_message` are keyword-only.

**Parameter conventions:**

| Parameter | Description |
|---|---|
| `messages` | Conversation history in Anthropic message format. Each entry is a dict with at least `"role"` and `"content"` keys. Content may be a plain string or a list of typed content blocks (e.g. `tool_result`). |
| `system` | The system prompt string passed directly to the model. |
| `tools` | Optional list of tool definitions in Anthropic format (`name`, `description`, `input_schema`). Providers that do not use Anthropic's native format are responsible for converting this before passing to their SDK. |
| `max_tokens` | Maximum tokens to generate. Defaults to `1024`. |

**`model_name` property:**

Returns the string model identifier that will appear in any logging or metadata. Set during `__init__` and never mutated afterward.

---

## 4. Factory

Defined in `src/chat_providers/__init__.py`.

```python
def create_chat_provider(config: ChatProviderConfig) -> ChatProvider | None:
```

Accepts a `ChatProviderConfig` dataclass (from `src/config.py`) and returns an initialized provider, or `None` if initialization fails.

**Routing logic:**

1. If `config.provider == "ollama"`, construct and return an `OllamaChatProvider`. This never returns `None`; it always succeeds because Ollama needs no credentials.
   - Default model: `"qwen3.5:35b"`
   - Default base URL: `"http://localhost:11434/v1"`
   - Default keep_alive: `"1h"`
   - Default num_ctx: `0` (model default)
   - All defaults are overridden by the corresponding `config` fields when set.

2. If `config.provider == "gemini"`, construct and return a `GeminiChatProvider`. This never returns `None`.
   - Default model: `"gemini-2.5-flash"`
   - API key: `config.api_key` (falls back to `GEMINI_API_KEY` or `GOOGLE_API_KEY` env vars inside the provider).

3. For any other `config.provider` value (including the default, `"anthropic"`), construct an `AnthropicChatProvider`. If `provider.is_configured` is `False` (no credentials were found during `__init__`), return `None`.

`create_chat_provider` returning `None` signals to `ChatAgent` that no LLM-backed chat interface is available; `ChatAgent` must handle this gracefully.

**Public exports from `__init__.py`:**

```
ChatProvider, ChatResponse, LoggedChatProvider, TextBlock, ToolUseBlock, create_chat_provider
```

---

## 5. Anthropic Provider

Defined in `src/chat_providers/anthropic.py`. Class: `AnthropicChatProvider`.

### Credential Detection Priority

`__init__` probes four credential sources in strict priority order, stopping at the first that succeeds. The source that is selected determines the SDK client class that is instantiated.

**Priority 1 — Vertex AI**

Checked when either `GOOGLE_CLOUD_PROJECT` or `ANTHROPIC_VERTEX_PROJECT_ID` is set in the environment.

- Client class: `anthropic.AsyncAnthropicVertex`
- Region is resolved from (in order): `GOOGLE_CLOUD_LOCATION`, `CLOUD_ML_REGION`, fallback `"us-east5"`.
- Default model when `config.model` is empty: `"claude-sonnet-4@20250514"` (note the `@` versioning convention used by Vertex).

**Priority 2 — Bedrock**

Checked when either `AWS_REGION` or `AWS_DEFAULT_REGION` is set.

- Client class: `anthropic.AsyncAnthropicBedrock` (no additional constructor arguments; uses standard AWS credential chain).
- Default model when `config.model` is empty: `"claude-sonnet-4-20250514"`.

**Priority 3 — API Key**

Checked when `ANTHROPIC_API_KEY` is set.

- Client class: `anthropic.AsyncAnthropic(api_key=...)`
- Default model when `config.model` is empty: `"claude-sonnet-4-20250514"`.

**Priority 4 — OAuth Token**

Checked last by calling `_load_claude_oauth_token()`.

- Client class: `anthropic.AsyncAnthropic(auth_token=...)`
- Default model when `config.model` is empty: `"claude-sonnet-4-20250514"`.

If none of the four sources yield a client, `self._client` remains `None`.

### OAuth Token Loading

Module-level function `_load_claude_oauth_token() -> str | None`.

Searches for a credentials file in `~/.claude/` under two candidate names, tried in order:

1. `.credentials.json`
2. `credentials.json`

For the first file that exists, it reads and JSON-parses the content, then navigates:

```
root -> "claudeAiOauth" -> "accessToken"
```

If an `accessToken` is found, it also checks `"expiresAt"` (a millisecond Unix timestamp). If `expiresAt` is present and less than the current time in milliseconds, a warning is printed to stdout but the token is still returned and used. Read errors are caught and printed as warnings; the function returns `None` in that case.

Returns the access token string, or `None` if no valid token was found.

### is_configured Flag

```python
@property
def is_configured(self) -> bool:
    return self._client is not None
```

`False` means none of the four credential sources succeeded. The factory uses this to decide whether to return `None`.

### Message Creation

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

Delegates directly to `self._client.messages.create(...)` with the following kwargs:

- `model`: `self._model`
- `max_tokens`: as passed
- `system`: as passed
- `messages`: as passed (already in Anthropic format; no conversion needed)
- `tools`: included in kwargs only when the `tools` argument is not `None` and non-empty

The raw SDK response `resp.content` is a list of block objects. Each block is inspected by its `.type` attribute:

- `"text"` -> `TextBlock(text=block.text)`
- `"tool_use"` -> `ToolUseBlock(id=block.id, name=block.name, input=block.input)`
- Any other block type is silently dropped.

Returns a `ChatResponse` with the assembled `content` list.

---

## 6. Ollama Provider

Defined in `src/chat_providers/ollama.py`. Class: `OllamaChatProvider`.

### Initialization

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

### Model Loading Check

```python
async def is_model_loaded(self) -> bool:
```

Checks whether the configured model is currently loaded in Ollama's memory.

**Fast path:** If a successful response was received within 90% of the `keep_alive` window, returns `True` immediately without a network call — the model is guaranteed to still be loaded.

**Slow path:** Hits Ollama's `/api/ps` endpoint to list running models. Compares base model names (stripping the tag/version suffix after `:`). Returns `True` if found, `False` if not (cold start or timed-out model).

**Fail-open:** Returns `True` on any error, so callers never block on a failed probe.

### Message Creation

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

### Message Format Conversion

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

## 7. Tool Conversion Utility

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

---

## 8. Gemini Provider

Defined in `src/chat_providers/gemini.py`. Class: `GeminiChatProvider`.

Uses the `google-genai` SDK to communicate with Google's Gemini models. Like the Ollama provider, the main complexity is format conversion: the rest of the codebase uses Anthropic-style tool definitions and message structures, so this module translates between the two formats on every request and response.

### Initialization

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

### Message Creation

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

### Tool Definition Conversion

Static method `_convert_tools(anthropic_tools) -> list`.

Converts Anthropic-format tool definitions to a list containing a single `types.Tool` with all function declarations. Each tool becomes a `FunctionDeclaration` with:

- `name`: tool name
- `description`: tool description (defaults to `""`)
- `parameters`: the `input_schema` recursively converted to a `types.Schema` object via `_convert_schema`. Set to `None` if the schema has no `properties`.

### Message Format Conversion

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

### Response Parsing

Static method `_parse_response(response) -> ChatResponse`.

Inspects `response.candidates[0].content.parts`:

- Parts with `.text` become `TextBlock(text=...)`.
- Parts with `.function_call` become `ToolUseBlock(id=<random-8-char-uuid>, name=fc.name, input=dict(fc.args))`. Gemini does not provide tool call IDs, so a random 8-character UUID prefix is generated.

**Empty / missing response guards** (returns a `ChatResponse` with a single empty `TextBlock` in each case):

- If `response.candidates` is empty or falsy.
- If the first candidate's `.content` is `None` or its `.content.parts` is `None`/empty (e.g., safety-filtered responses where Gemini omits content entirely).
- If none of the parts matched as text or function_call, so the assembled `content` list is empty after iteration.

### Schema Conversion Helper

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

---

## 9. LoggedChatProvider

Defined in `src/chat_providers/logged.py`. Class: `LoggedChatProvider`.

A decorator (wrapper) that wraps any `ChatProvider` instance with timing and LLM logging. It delegates all `create_message` calls to the wrapped provider while recording request/response data to the `LLMLogger` subsystem.

This is used by the system to transparently add logging to whichever provider is active, without the provider needing to know about logging concerns.
