---
tags: [spec, chat-providers, anthropic, llm]
---

# Anthropic Provider — Specification

Implements [[base|ChatProvider]] interface. See also: [[supervisor]].

Defined in `src/chat_providers/anthropic.py`. Class: `AnthropicChatProvider`.

## Credential Detection Priority

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

## OAuth Token Loading

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

## is_configured Flag

```python
@property
def is_configured(self) -> bool:
    return self._client is not None
```

`False` means none of the four credential sources succeeded. The factory uses this to decide whether to return `None`.

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
