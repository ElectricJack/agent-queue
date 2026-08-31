---
tags: [spec, logging, llm, observability]
---

# LLM Logging Spec

## Source Files
- `src/llm_logger.py`
- `src/llm/client.py` (the direct LLM path — `LLMClient`)

**Related config:** `LLMLoggingConfig` in `src/config.py` (see [[specs/config]])
**Related spec:** `docs/superpowers/specs/2026-08-30-llm-direct-path-design.md`

## 1. Overview

The LLM logging system captures every interaction with language models — both
direct-path `LLMClient.complete()` calls (playbook nodes/transitions, plugin
`invoke_llm`, the reference-stub enricher, `aq vault rebuild-index
--with-summaries`) and Claude Code agent sessions (task execution). The goal is
prompt optimization: understanding what context is provided, how long
responses take, and what outputs look like.

Logs are written as JSONL files (one JSON object per line) organized by date
under `logs/llm/`. The system is off by default and enabled via
configuration. When disabled, no files are created and all logging calls are
no-ops.

`LLMLogger` is the single component: it writes JSONL entries and manages
retention. `LLMClient.complete()` times each call itself and calls
`LLMLogger.log_llm_call()` directly in a `finally` block — there is no
decorator/wrapper type; logging is inline in the client, not a separate
provider subclass.

Agent sessions are logged separately via a direct `LLMLogger.log_agent_session()`
call in the Claude platform, since agent execution does not go through
`LLMClient`.

## 2. Log Directory Structure

```
logs/llm/
  2026-02-25/
    llm.jsonl              # Every LLMClient.complete() call
    claude_agent.jsonl     # Claude Code agent task sessions
    prompt_analytics.jsonl # Periodic aggregated metrics (see §5)
    tasks/
      <task_id>.jsonl       # Per-task copy of that task's agent-session entries
  2026-02-26/
    llm.jsonl
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

The orchestrator creates the singleton instance from `LLMLoggingConfig` at
startup. All other components receive the logger via dependency injection
(`LLMClient(..., llm_logger=...)`, adapter constructors, etc.).

### 3.2 `log_llm_call()`

Logs a single direct-path LLM call. Called by `LLMClient.complete()` in a
`finally` block, so both successes and errors are captured. Writes to
`llm.jsonl` (internally this is `_log_provider_call(filename="llm.jsonl", ...)`).

**Parameters (all keyword-only):**

| Parameter | Type | Description |
|-----------|------|-------------|
| `caller` | `str` | Call site identity — the `caller` on the resolved `LLMCallSpec` (e.g. `"playbook.node"`, `"playbook.transition"`, `"plugin.invoke_llm"`, `"stub_enricher"`, `"vault.summarize"`) |
| `model` | `str` | Model name from the provider |
| `provider` | `str` | Provider id (e.g. `"anthropic"`, `"google"`, `"openai"`) |
| `messages` | `list[dict]` | The message history sent to the LLM |
| `system` | `str` | System prompt |
| `tools` | `list[dict] \| None` | Tool definitions (logged as names only) |
| `max_tokens` | `int` | Max tokens parameter |
| `response` | `Any \| None` | The response object (logged as text_parts + tool use name/input) |
| `error` | `str \| None` | Error message if the call failed |
| `duration_ms` | `int` | Wall-clock duration in milliseconds |

**JSONL entry fields:**

```json
{
  "timestamp": "2026-08-30T14:30:00.123456+00:00",
  "caller": "playbook.transition",
  "model": "claude-sonnet-4-5-20250929",
  "provider": "anthropic",
  "duration_ms": 1500,
  "prompt_fingerprint": "3f9a1c2b4d5e",
  "input": {
    "system": "...",
    "messages": [{"role": "user", "content": "..."}],
    "tools": [],
    "tool_names": [],
    "max_tokens": 1024,
    "input_tokens_est": 340
  },
  "output": {
    "text_parts": ["Here is the status..."],
    "output_tokens_est": 42
  },
  "error": null
}
```

**Data reduction rules:**
- Tool definitions: only tool names are logged in the `tool_names` summary field (the full `tools` array is also logged as-given, schemas included).
- Messages and system prompt are logged in full (no truncation) — `llm.jsonl` is off by default and git-ignored precisely because entries carry full prompts.
- Token counts are **estimates only** (roughly 1 token ≈ 4 characters) — there is no provider-reported `usage` field on `LLMResponse`/`LLMRunResult`; see the direct-path spec's "Deviations applied during implementation" §3.
- A `prompt_fingerprint` (md5 of the first 100 chars of the system prompt + sorted tool names) tags entries by prompt template for A/B comparison.

### 3.3 `log_agent_session()`

Logs a Claude Code agent task session. Called by the Claude adapter's `wait()`
method after each execution completes (success, failure, or pause). Also
writes a copy to `tasks/<task_id>.jsonl` when `task_id` is provided.

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
| `transcript` | `list[dict] \| None` | Optional ordered list of structured turn records |

The `prompt` is logged in full (`prompt_length` + `prompt`). Error messages in
the output are truncated to 500 characters.

### 3.4 `flush_analytics()` / `get_analytics_summary()`

`LLMLogger` maintains an in-memory `PromptAnalytics` aggregator (per
`caller:provider:model` key: call count, estimated input/output tokens,
total duration, error count). `flush_analytics()` writes a summary entry to
`prompt_analytics.jsonl` and resets the aggregator; `get_analytics_summary()`
reads it without flushing (for health checks / dashboards).

### 3.5 `cleanup_old_logs()`

```python
cleanup_old_logs() -> int
```

Deletes date directories older than `retention_days`. Returns the number of
directories removed. Only directories matching the `YYYY-MM-DD` format
(10-character names) and lexicographically less than the cutoff date are
removed. Non-date directories (e.g. a stray `temp/` folder) are left
untouched.

Called by the orchestrator approximately once per hour (tracked via
`_last_log_cleanup` timestamp in the main loop). Failures are logged to
stdout but do not interrupt the orchestrator cycle.

### 3.6 `_append()`

Internal method that writes a single JSON line to a date-organized file.
Creates the date directory if it does not exist. Uses
`json.dumps(default=str)` for safe serialization of non-JSON-native types
(datetimes, enums, etc.).

### 3.7 Disabled Behavior

When `enabled=False`, `log_llm_call()` and `log_agent_session()` return
immediately without writing anything. No directories or files are created.
`cleanup_old_logs()` still operates (returns 0 if the directory does not
exist).

## 4. Integration Points

### 4.1 Orchestrator (`src/orchestrator.py`)

- Creates the `LLMLogger` instance in `__init__` from `config.llm_logging`.
- Constructs `orchestrator.llm` (`LLMClient`) with this logger.
- Runs `cleanup_old_logs()` approximately once per hour in the main loop
  (housekeeping phase, after the timer-service tick).
- Exposes `self.llm_logger` for other components to reference.

### 4.2 `LLMClient` (`src/llm/client.py`)

- Accepts optional `llm_logger` in its constructor.
- `complete()` times the call, resolves the `LLMCallSpec` (which supplies
  `caller`), delegates to the provider adapter, and calls
  `llm_logger.log_llm_call()` in a `finally` block — both success and error
  paths are logged.

### 4.3 Claude Platform (`src/runtimes/claude.py` and session adapters)

- Accepts optional `llm_logger` in constructor.
- In `wait()`: records `start_time` at entry, calls `_log_session()` before
  every return path (success, cancellation, exception, CLI error, zero-token
  failure).
- The `_log_session()` helper computes duration and calls
  `llm_logger.log_agent_session()`.

### 4.4 Main (`src/main.py`)

- Creates the orchestrator first (which owns `llm_logger` and `llm`), then
  wires the same logger into session-adapter construction.

## 5. Configuration

```yaml
llm_logging:
  enabled: true          # default: false
  retention_days: 14     # default: 30
```

| YAML key | Type | Default | Description |
|----------|------|---------|-------------|
| `enabled` | `bool` | `false` | When false, all logging is a no-op |
| `retention_days` | `int` | `30` | Date directories older than this are deleted by periodic cleanup |

Represented by `LLMLoggingConfig` dataclass in `src/config.py`, nested under
`AppConfig.llm_logging`.

## 6. Reading Logs

```bash
# Live tail direct-path LLM calls
tail -f logs/llm/$(date +%Y-%m-%d)/llm.jsonl | jq .

# Find slow calls (> 5 seconds)
jq 'select(.duration_ms > 5000)' logs/llm/*/llm.jsonl

# Find all calls from one call site
jq 'select(.caller == "playbook.transition")' logs/llm/*/llm.jsonl

# Agent session summary
jq '{task: .task_id, tokens: .output.tokens_used, duration_s: (.duration_ms/1000)}' \
  logs/llm/*/claude_agent.jsonl
```
