---
tags: [spec, logging, llm, observability]
---

# LLM Logging Spec

## Source Files
- `src/llm_logger.py`
- `src/chat_providers/logged.py`

**Related config:** `LLMLoggingConfig` in `src/config.py` (see [[specs/config]])
**Related models:** `ChatProvider` in `src/chat_providers/base.py` (see [[chat-providers/base|Chat Providers]])

## 1. Overview

The LLM logging system captures every interaction with language models — both ChatProvider calls (Discord chat, hooks, plan parsing, summarization) and Claude Code agent sessions (task execution). The goal is prompt optimization: understanding what context is provided, how long responses take, and what outputs look like.

Logs are written as JSONL files (one JSON object per line) organized by date under `logs/llm/`. The system is off by default and enabled via configuration. When disabled, no files are created and all logging calls are no-ops.

Two components work together:
- **`LLMLogger`** — writes JSONL entries and manages retention.
- **`LoggedChatProvider`** — wraps any `ChatProvider` with transparent timing and logging (decorator pattern).

Agent sessions are logged separately via a direct `LLMLogger` call in the Claude adapter, since agent execution does not go through `ChatProvider`.

## 2. Log Directory Structure

```
logs/llm/
  2026-02-25/
    chat_provider.jsonl    # All ChatProvider.create_message() calls
    claude_agent.jsonl     # Claude Code agent task sessions
  2026-02-26/
    chat_provider.jsonl
    claude_agent.jsonl
```

- Date directories use UTC dates in `YYYY-MM-DD` format.
- Directories and files are created on first write (no pre-creation on startup).
- The `logs/` directory is git-ignored.

## 3. LLMLogger

### 3.1 Constructor

```python
LLMLogger(base_dir: str = "logs/llm", enabled: bool = True, retention_days: int = 30)
```

The orchestrator creates the singleton instance from `LLMLoggingConfig` at startup. All other components receive the logger via dependency injection.

### 3.2 `log_chat_provider_call()`

Logs a single `ChatProvider.create_message()` invocation. Called by `LoggedChatProvider` in its `finally` block, so both successes and errors are captured.

**Parameters (all keyword-only):**

| Parameter | Type | Description |
|-----------|------|-------------|
| `caller` | `str` | Call site identity (e.g. `"chat_agent.chat"`, `"hook_engine"`, `"plan_parser"`, `"chat_agent.summarize"`) |
| `model` | `str` | Model name from the provider |
| `provider` | `str` | Provider class name (e.g. `"AnthropicChatProvider"`, `"OllamaChatProvider"`) |
| `messages` | `list[dict]` | The message history sent to the LLM |
| `system` | `str` | System prompt |
| `tools` | `list[dict] \| None` | Tool definitions (logged as names only) |
| `max_tokens` | `int` | Max tokens parameter |
| `response` | `ChatResponse \| None` | The response object (logged as text_parts + tool use name/input keys) |
| `error` | `str \| None` | Error message if the call failed |
| `duration_ms` | `int` | Wall-clock duration in milliseconds |

**JSONL entry fields:**

```json
{
  "timestamp": "2026-02-25T14:30:00.123456+00:00",
  "caller": "chat_agent.chat",
  "model": "claude-sonnet-4-20250514",
  "provider": "AnthropicChatProvider",
  "duration_ms": 1500,
  "input": {
    "system_prompt_length": 4200,
    "message_count": 5,
    "messages": [
      {"role": "user", "content_length": 120, "content_preview": "What is..."},
      {"role": "assistant", "content_length": 85, "content_preview": "The answer..."}
    ],
    "tool_names": ["create_task", "list_tasks", "get_status"],
    "max_tokens": 1024
  },
  "output": {
    "text_parts": ["Here is the status..."],
    "tool_uses": [{"name": "get_status", "input_keys": []}]
  },
  "error": null
}
```

**Data reduction rules:**
- Tool definitions: only tool names are logged (schemas are large and static).
- Tool use inputs: only the keys of the input dict are logged (values may contain sensitive data or large payloads).
- Messages: each message is summarized as role + content length + first 150 characters of content. List-type content (tool results) is logged as type and count only.
- Response text parts are logged in full.

### 3.3 `log_agent_session()`

Logs a Claude Code agent task session. Called by the Claude adapter's `wait()` method after each execution completes (success, failure, or pause).

**Parameters (all keyword-only):**

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | `str` | Task ID |
| `session_id` | `str \| None` | Claude Code session ID |
| `model` | `str` | Model ID or `"(default)"` if not explicitly set |
| `prompt` | `str` | The assembled prompt sent to the agent |
| `config_summary` | `dict \| None` | Adapter config: `allowed_tools`, `permission_mode`, `cwd` |
| `output` | `AgentOutput \| None` | The agent output (result, summary, tokens, files, error) |
| `duration_ms` | `int` | Wall-clock duration in milliseconds |

**JSONL entry fields:**

```json
{
  "timestamp": "2026-02-25T14:35:00.654321+00:00",
  "task_id": "keen-fox",
  "session_id": "sess-abc123",
  "model": "(default)",
  "duration_ms": 45000,
  "input": {
    "prompt_length": 2400,
    "prompt_preview": "## System Context\n- Workspace directory: /home/...",
    "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
    "permission_mode": "acceptEdits",
    "cwd": "/home/dev/workspaces/my-project"
  },
  "output": {
    "result": "AgentResult.COMPLETED",
    "summary": "Implemented the feature and added tests.",
    "tokens_used": 12500,
    "files_changed": ["src/foo.py", "tests/test_foo.py"]
  }
}
```

The `prompt_preview` is truncated to 300 characters. Error messages in the output are truncated to 500 characters.

### 3.4 `cleanup_old_logs()`

```python
cleanup_old_logs() -> int
```

Deletes date directories older than `retention_days`. Returns the number of directories removed. Only directories matching the `YYYY-MM-DD` format (10-character names) and lexicographically less than the cutoff date are removed. Non-date directories (e.g. a stray `temp/` folder) are left untouched.

Called by the orchestrator approximately once per hour (tracked via `_last_log_cleanup` timestamp in the main loop). Failures are logged to stdout but do not interrupt the orchestrator cycle.

### 3.5 `_append()`

Internal method that writes a single JSON line to a date-organized file. Creates the date directory if it does not exist. Uses `json.dumps(default=str)` for safe serialization of non-JSON-native types (datetimes, enums, etc.).

### 3.6 Disabled Behavior

When `enabled=False`, `log_chat_provider_call()` and `log_agent_session()` return immediately without writing anything. No directories or files are created. `cleanup_old_logs()` still operates (returns 0 if the directory does not exist).

## 4. LoggedChatProvider

```python
LoggedChatProvider(inner: ChatProvider, logger: LLMLogger, caller: str = "unknown")
```

A `ChatProvider` subclass that wraps any provider with transparent logging. Follows the decorator pattern — all calls are delegated to the inner provider, with timing and logging added around `create_message()`.

### 4.1 `create_message()`

1. Records `start = time.monotonic()`.
2. Delegates to `inner.create_message()` with identical arguments.
3. In a `finally` block (runs on both success and exception):
   - Computes `duration_ms` from the monotonic clock.
   - Calls `logger.log_chat_provider_call()` with the caller, model, provider class name, all inputs, the response (or `None`), any error message, and the duration.
4. Returns the response from the inner provider, or re-raises the exception.

### 4.2 `model_name`

Property that delegates to `inner.model_name`.

### 4.3 `_caller` Attribute

The `caller` string can be changed at runtime to tag different call sites. For example, `ChatAgent` sets `_caller = "chat_agent.summarize"` before summarization calls and restores the previous value afterward.

## 5. Integration Points

### 5.1 Orchestrator (`src/orchestrator.py`)

- Creates the `LLMLogger` instance in `__init__` from `config.llm_logging`.
- Wraps the plan parser's `_chat_provider` with `LoggedChatProvider(caller="plan_parser")` when logging is enabled.
- Runs `cleanup_old_logs()` approximately once per hour in the main loop (step 6, after hook engine tick).
- Exposes `self.llm_logger` for other components to reference.

### 5.2 ChatAgent (`src/chat_agent.py`)

- Accepts optional `LLMLogger` in constructor.
- In `initialize()`: wraps the provider with `LoggedChatProvider(caller="chat_agent.chat")` when a logger is present and enabled.
- In `summarize()`: temporarily sets `_caller` to `"chat_agent.summarize"` for the duration of the call, restoring the previous value in a `finally` block.

### 5.3 Claude Adapter (`src/adapters/claude.py`)

- Accepts optional `llm_logger` in constructor.
- In `wait()`: records `start_time` at entry, calls `_log_session()` before every return path (success, cancellation, exception, CLI error, zero-token failure).
- The `_log_session()` helper computes duration and calls `llm_logger.log_agent_session()`.

### 5.4 AdapterFactory (`src/adapters/__init__.py`)

- Accepts optional `llm_logger` in constructor, passes it through to adapters on `create()`.

### 5.5 HookEngine (`src/hooks.py`)

- In `_invoke_llm()`: wraps the hook's ChatAgent provider with `LoggedChatProvider(caller="hook_engine")` when the orchestrator's logger is enabled.

### 5.6 Discord Bot (`src/discord/bot.py`)

- Passes `orchestrator.llm_logger` to `ChatAgent` on construction.

### 5.7 Main (`src/main.py`)

- Creates the orchestrator first, then creates `AdapterFactory` with the orchestrator's `llm_logger`.

## 6. Configuration

```yaml
llm_logging:
  enabled: true          # default: false
  retention_days: 14     # default: 30
```

| YAML key | Type | Default | Description |
|----------|------|---------|-------------|
| `enabled` | `bool` | `false` | When false, all logging is a no-op |
| `retention_days` | `int` | `30` | Date directories older than this are deleted by periodic cleanup |

Represented by `LLMLoggingConfig` dataclass in `src/config.py`, nested under `AppConfig.llm_logging`.

## 7. Reading Logs

```bash
# Live tail chat provider calls
tail -f logs/llm/$(date +%Y-%m-%d)/chat_provider.jsonl | jq .

# Find slow calls (> 5 seconds)
jq 'select(.duration_ms > 5000)' logs/llm/*/chat_provider.jsonl

# See all hook LLM calls
jq 'select(.caller == "hook_engine")' logs/llm/*/chat_provider.jsonl

# Agent session summary
jq '{task: .task_id, tokens: .output.tokens_used, duration_s: (.duration_ms/1000)}' \
  logs/llm/*/claude_agent.jsonl
```
