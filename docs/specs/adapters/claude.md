---
tags: [spec, adapters, claude-code]
---

# Agent Adapter System — Claude Code Adapter

## 1. Overview

The adapter subsystem provides a pluggable interface between the [[specs/orchestrator]] and AI coding agents. All agent-specific behaviour is isolated behind a common abstract base class (`AgentAdapter`). This allows the orchestrator to drive any supported agent type through the same four-method contract without knowing which agent is running underneath.

Currently one concrete implementation exists: `ClaudeAdapter`, which runs Claude Code via the `claude_agent_sdk` Python package. The `AdapterFactory` class handles instantiation by agent-type string.

---

## Source Files

- `src/adapters/base.py` — abstract interface and `MessageCallback` type alias
- `src/adapters/__init__.py` — `AdapterFactory`
- `src/adapters/claude.py` — `ClaudeAdapter`, `ClaudeAdapterConfig`, `_resilient_query`

---

## 2. AgentAdapter Interface

Defined in `src/adapters/base.py`.

```python
class AgentAdapter(ABC):
    async def start(self, task: TaskContext) -> None: ...
    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput: ...
    async def stop(self) -> None: ...
    async def is_alive(self) -> bool: ...
```

### MessageCallback

```python
MessageCallback = Callable[[str], Awaitable[None]]
```

An async callable that accepts a single string. The orchestrator supplies this to `wait()` so that agent output can be forwarded to Discord in real time as messages arrive.

### start(task: TaskContext) -> None

Prepares the adapter to run the given task. Stores `TaskContext` internally and resets any cancellation state. Does not launch the underlying process; actual execution begins inside `wait()`.

`TaskContext` fields used by the adapter:

| Field | Type | Purpose |
|-------|------|---------|
| `description` | `str` | Primary task prompt (always included) |
| `acceptance_criteria` | `list[str]` | Appended as a "## Acceptance Criteria" section |
| `test_commands` | `list[str]` | Appended as a "## Test Commands" section |
| `attached_context` | `list[str]` | Appended as a "## Additional Context" section |
| `checkout_path` | `str` | Working directory passed to the Claude CLI subprocess |
| `mcp_servers` | `dict` | MCP server configurations (name → config dict) forwarded to SDK options |

### wait(on_message) -> AgentOutput

Launches the agent session and blocks until the agent terminates or is cancelled. Streams intermediate messages to `on_message` if provided. Returns an `AgentOutput` describing the final outcome.

`AgentOutput` fields:

| Field | Type | Meaning |
|-------|------|---------|
| `result` | `AgentResult` | Outcome enum value |
| `summary` | `str` | Concatenated result text from the agent |
| `tokens_used` | `int` | Total input + output tokens consumed |
| `error_message` | `str \| None` | Set on failure paths |

### stop() -> None

Sets the internal `asyncio.Event` cancellation flag. The `wait()` loop checks this flag on every incoming message and returns an `AgentOutput(result=FAILED, summary="Cancelled")` immediately when it is set.

### is_alive() -> bool

Returns `True` when `start()` has been called and `stop()` has not. Specifically: `self._task is not None and not self._cancel_event.is_set()`. Does not check whether the underlying subprocess is still running.

---

## 3. AdapterFactory

Defined in `src/adapters/__init__.py`.

```python
class AdapterFactory:
    def __init__(self, claude_config: ClaudeAdapterConfig | None = None): ...
    def create(self, agent_type: str) -> AgentAdapter: ...
```

`create()` inspects the `agent_type` string:

- `"claude"` — returns `ClaudeAdapter(self._claude_config)`
- Anything else — raises `ValueError("Unknown agent type: <type>")`

A single `AdapterFactory` instance is constructed at startup (by the orchestrator) with a pre-built `ClaudeAdapterConfig`. Individual `ClaudeAdapter` instances are created fresh for each task execution via `factory.create("claude")`.

---

## 4. ClaudeAdapter

Defined in `src/adapters/claude.py`.

### 4.1 Configuration (ClaudeAdapterConfig)

```python
@dataclass
class ClaudeAdapterConfig:
    model: str = ""
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=lambda: [
        "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    ])
```

| Field | Default | Meaning |
|-------|---------|---------|
| `model` | `""` (empty) | Model identifier passed to `ClaudeAgentOptions.model`. When empty the field is not set, allowing Claude Code to pick its own default. |
| `permission_mode` | `"acceptEdits"` | Passed directly to `ClaudeAgentOptions.permission_mode`. Controls how Claude Code handles file-edit permission prompts. |
| `allowed_tools` | `["Read","Write","Edit","Bash","Glob","Grep"]` | List of tool names the agent is permitted to call. Passed to `ClaudeAgentOptions.allowed_tools`. |

### 4.2 SDK Integration

`wait()` constructs a `ClaudeAgentOptions` object and drives the session through `_resilient_query()`:

```python
options = ClaudeAgentOptions(
    allowed_tools=self._config.allowed_tools,
    permission_mode=self._config.permission_mode,
    cwd=self._task.checkout_path or None,
    cli_path=system_claude,      # system `claude` binary preferred over bundled
)
if self._config.model:
    options.model = self._config.model
if self._task.mcp_servers:
    options.mcp_servers = self._task.mcp_servers
```

The `cli_path` is resolved with `shutil.which("claude")`. When the system binary is found it is preferred over the SDK's bundled binary because the system binary carries user credentials (either from `claude login` or `ANTHROPIC_API_KEY`). If `shutil.which` returns `None`, `cli_path=None` causes the SDK to fall back to its own bundled binary.

Before calling `_resilient_query`, `wait()` removes the `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` environment variables from the current process environment. These markers would otherwise signal to the SDK that it is already running inside a Claude Code environment. `_resilient_query` then sets `CLAUDE_CODE_ENTRYPOINT` to `"sdk-py"` as part of its own initialisation, which is the correct value for an SDK-launched session.

SDK type references (`AssistantMessage`, `ResultMessage`, etc.) are loaded lazily inside `wait()` and stored in module-level globals so that `_extract_message_text()` can use `isinstance` checks without circular imports.

If the `claude_agent_sdk` package is not installed, the `ImportError` is caught at the outermost level of `wait()` and returned as `AgentOutput(result=FAILED, error_message="Claude Agent SDK not available: ...")`.

### 4.3 Resilient Query (_resilient_query)

The standard `claude_agent_sdk.query()` async generator will crash its entire iteration if the SDK's message parser encounters an unknown message type (e.g. `rate_limit_event`), raising `MessageParseError` and terminating the stream.

`_resilient_query` works around this by bypassing the public `query()` function and driving the SDK internals directly:

1. Creates an `InternalClient`. Applies the `can_use_tool` guard (see below) and produces `configured_options`.
2. Creates a `SubprocessCLITransport(prompt=prompt, options=configured_options)` and calls `transport.connect()`.
3. Extracts any `"sdk"`-type MCP servers from `configured_options.mcp_servers` into a separate `sdk_mcp_servers` dict. Converts `configured_options.agents` (if set) into a plain dict and converts `configured_options.hooks` via `client._convert_hooks_to_internal_format()`.
4. Builds a `Query(transport=transport, is_streaming_mode=True, can_use_tool=..., hooks=..., sdk_mcp_servers=..., agents=...)` object.
5. Calls `query_obj.start()` and `query_obj.initialize()`.
6. If `prompt` is a plain `str`, writes it as a JSON `user` message directly to the transport and calls `transport.end_input()`. If `prompt` is an `AsyncIterable`, schedules `query_obj.stream_input(prompt)` as a background task in the query's task group.
7. Iterates `query_obj.receive_messages()` (raw dicts). For each dict, calls `parse_message(data)`. If `MessageParseError` is raised, the message is **silently skipped** with a `print` log line; iteration continues.
8. A `finally` block calls `await query_obj.close()` to clean up the session regardless of how iteration ends.

This means a `rate_limit_event` or any other unrecognised message type no longer terminates the stream.

**MCP server pre-processing inside `_resilient_query`:** If `options.mcp_servers` is a dict and any entry has `"type": "sdk"`, its `"instance"` value is extracted into `sdk_mcp_servers` and passed to the `Query` constructor. Non-SDK MCP servers remain in the options and are handled by the CLI transport.

**`can_use_tool` guard:** If `options.can_use_tool` is set, `_resilient_query` enforces two constraints: (1) the prompt must be an `AsyncIterable`, not a plain string — if it is a string, `ValueError("can_use_tool requires streaming mode")` is raised; (2) `options.permission_prompt_tool_name` must not also be set — if it is, `ValueError("can_use_tool and permission_prompt_tool_name are mutually exclusive")` is raised. When both constraints pass, `permission_prompt_tool_name` is set to `"stdio"` on a copied options object.

### 4.4 Prompt Construction (_build_prompt)

`_build_prompt()` assembles the `TaskContext` fields into a single markdown string passed to `_resilient_query` as the prompt:

```
<task.description>

## Acceptance Criteria       (omitted if list is empty)
- <criterion 1>
- <criterion 2>

## Test Commands             (omitted if list is empty)
- `<command 1>`

## Additional Context        (omitted if list is empty)
- <context item 1>
```

The description is always the first element. The three optional sections are appended in order only when their respective `TaskContext` list is non-empty.

### 4.5 Message Processing

As `_resilient_query` yields parsed SDK message objects, `wait()` processes each one in this order:

**Cancellation check (first):** At the top of the loop body, before any other processing, `wait()` checks `self._cancel_event.is_set()`. If the event is set it immediately returns `AgentOutput(result=FAILED, summary="Cancelled", error_message="Agent was stopped")`.

**Session initialisation:** A `SystemMessage` with `subtype == "init"` carries the session ID inside its `data` dict. The adapter extracts and stores this as `self._session_id` for logging.

**Streaming to Discord:** For every message, `_extract_message_text(message)` is called. If it returns a non-empty string and `on_message` is set, `await on_message(text)` forwards it to the caller (typically a Discord thread writer in the orchestrator).

**Result and token accumulation:** When a `ResultMessage` arrives:
- If `message.is_error` is `True`, the error subtype and result text are recorded in `cli_error` and normal result accumulation is skipped.
- Otherwise, `message.result` (if non-empty) is appended to `summary_parts`.
- `message.usage` dict is read for `input_tokens` and `output_tokens`; both are added to `tokens_used`.

For any other message type that has a `result` attribute with a truthy value (i.e. non-`ResultMessage` objects), that result text is also appended to `summary_parts` as a fallback.

#### _extract_message_text detail

| Message type | Extracted content |
|--------------|-------------------|
| `AssistantMessage` | Iterates `content` blocks: `ThinkingBlock` → `*thinking:* <first 500 chars>`; `TextBlock` → raw text; `ToolUseBlock` → `**[ToolName: `arg`]**` with path/command detail for known tools; `ToolResultBlock` → fenced code block with first 300 chars, prefixed `**Error:**` if `is_error` |
| `UserMessage` | If `tool_use_result.content` is a non-empty string under 300 chars, wraps it in a fenced code block |
| `ResultMessage` | Formats result text, cost in USD, and token counts |
| All others | Returns `None` (not forwarded) |

### 4.6 Result Mapping

After the `_resilient_query` loop exits, `wait()` determines the final `AgentResult`:

| Condition | AgentResult | Notes |
|-----------|-------------|-------|
| `self._cancel_event` set during iteration | `FAILED` | Checked on every message; returns immediately |
| Unhandled exception with "token" or "quota" in message | `PAUSED_TOKENS` | Agent will be re-queued after token refill |
| Any other unhandled exception | `FAILED` | Full traceback included in `error_message` |
| `cli_error` is set (CLI reported `is_error=True`) | `FAILED` | `error_message` contains `subtype: result` text |
| `tokens_used == 0` and `summary_parts` empty | `FAILED` | Sentinel for silent auth/rate-limit/CLI-crash failures |
| Normal completion | `COMPLETED` | `summary` = joined `summary_parts` or `"Completed"` |

Note: `PAUSED_RATE_LIMIT` is defined in `AgentResult` but is not produced by `ClaudeAdapter` directly. Rate limit events from the SDK appear as unrecognised message types that `_resilient_query` silently skips; rate limit recovery is handled at the orchestrator level.

### 4.7 Cancellation

Cancellation is cooperative, not preemptive. There is no process kill.

```python
async def stop(self) -> None:
    self._cancel_event.set()
```

`stop()` simply sets an `asyncio.Event`. The running `wait()` loop checks `self._cancel_event.is_set()` at the start of each iteration, before any message processing. The next message received after `stop()` is called will trigger the early return. If the agent subprocess is producing no output, the cancellation may not take effect until the subprocess produces its next message or terminates.

`is_alive()` reflects the cancellation state synchronously:

```python
async def is_alive(self) -> bool:
    return self._task is not None and not self._cancel_event.is_set()
```

It returns `False` as soon as `stop()` is called, even if `wait()` has not yet returned.

### 4.8 MCP Server Configuration

MCP servers are passed through from `TaskContext.mcp_servers` (a `dict` mapping server name to config dict) to `ClaudeAgentOptions.mcp_servers`. The `_resilient_query` function performs additional pre-processing:

- Entries with `"type": "sdk"` have their `"instance"` extracted and placed in a separate `sdk_mcp_servers` dict passed to the `Query` constructor. These are SDK-native in-process MCP servers.
- All other entries remain in `options.mcp_servers` and are passed to `SubprocessCLITransport` for the CLI subprocess to handle.

If `TaskContext.mcp_servers` is empty (the default), no MCP configuration is applied to the session.

> **Future evolution:** See [[design/agent-coordination]] for how coordination playbooks manage agent assignment and affinity.
