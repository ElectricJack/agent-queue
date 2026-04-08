---
tags: [spec, chat-providers, base, llm]
---

# Chat Providers — Base Specification

See also: [[chat-providers/anthropic]], [[chat-providers/ollama]], [[chat-providers/gemini]], [[chat-providers/logged]], [[specs/supervisor]]

## 1. Overview

The `src/chat_providers/` package provides a thin, uniform abstraction over LLM providers used for chat interactions. It is consumed exclusively by the [[specs/supervisor]] (`src/supervisor.py`) to drive the conversational interface.

The abstraction serves two purposes:

1. Allow the [[specs/supervisor]] to call a single `create_message` method regardless of which backend is configured, receiving a normalized response object it can inspect without knowing provider-specific details.
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

`create_chat_provider` returning `None` signals to the [[specs/supervisor]] that no LLM-backed chat interface is available; it must handle this gracefully.

**Public exports from `__init__.py`:**

```
ChatProvider, ChatResponse, LoggedChatProvider, TextBlock, ToolUseBlock, create_chat_provider
```
