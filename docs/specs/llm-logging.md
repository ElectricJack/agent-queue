---
tags: [spec, logging, llm, observability]
---

# LLM Logging Spec

<!-- aq:historical -->
> **Design record — not current documentation.** A spec states the behaviour
> intended when it was approved; it is written before the code and is not
> revised to track it. Where this page and the code disagree, the code is right.
> Start at [the documentation home](../README.md) for what AQ does today, and
> see [historical material](../history/README.md) for how this material is
> organised.

## Source Files
- `src/llm_logger.py`
- `src/llm/client.py` (the direct LLM path — `LLMClient`)

**Related config:** `LLMLoggingConfig` in `src/config.py` (see [specs/config](config.md))
**Related spec:** `docs/superpowers/specs/2026-08-30-llm-direct-path-design.md`

Current implementation notes below were checked on 2026-10-22. The original
Claude-platform session integration has been retired.

## 1. Overview

The current LLM logging system captures direct API attempts through
`LLMClient.complete()` (API playbook steps, plugin `invoke_llm`, the reference-stub
enricher, `aq vault rebuild-index --with-summaries`). It does not capture headless
`transport: cli` calls or tmux coding-agent sessions. The goal is
prompt optimization: understanding what context is provided, how long
responses take, and what outputs look like.

Logs are written as JSONL files (one JSON object per line) organized by date
under `logs/llm/`. Logging is enabled by default (`llm_logging.enabled: true`).
When disabled, no files are created and all logging calls are no-ops.

`LLMLogger` is the single component: it writes JSONL entries and manages
retention. `LLMClient.complete()` times each call itself and calls
`LLMLogger.log_llm_call()` directly in a `finally` block — there is no
decorator/wrapper type; logging is inline in the client, not a separate
provider subclass.

`LLMLogger.log_agent_session()` remains as a legacy helper with no production
caller. Its `claude_agent.jsonl` and per-task files describe the retired integration,
not output to expect from current tmux sessions. Session token accounting comes
from transcript ingestion; see [usage accounting](../reference/usage-accounting.md).
Availability refusals happen before an SDK attempt and are not logged here.

## 2. Log Directory Structure

```
logs/llm/
  2026-02-25/
    llm.jsonl              # Each attempted SDK call
    claude_agent.jsonl     # Legacy helper output; not produced by current sessions
    prompt_analytics.jsonl # Periodic aggregated metrics (see §3.4)
    tasks/
      <task_id>.jsonl       # Per-task copy of that task's agent-session entries
  2026-02-26/
    llm.jsonl
    claude_agent.jsonl     # Legacy helper output only
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
| `provider` | `str` | Adapter class name supplied by the client (e.g. `AnthropicProvider`) |
| `messages` | `list[dict]` | The message history sent to the LLM |
| `system` | `str` | System prompt |
| `tools` | `list[dict] \| None` | Full tool definitions and a separate name summary |
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
  "provider": "AnthropicProvider",
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
- Messages, system prompts and tool schemas are logged in full (no truncation). Logs are git-ignored and logging can be disabled with `llm_logging.enabled: false`.
- JSONL token fields are **estimates** (roughly 1 token ≈ 4 characters). `LLMResponse` and `LLMRunResult` separately expose provider-reported `TokenUsage` for budget enforcement; those counts are not written to the token ledger by this logger.
- A `prompt_fingerprint` (md5 of the first 100 chars of the system prompt + sorted tool names) tags entries by prompt template for A/B comparison.

### 3.3 `log_agent_session()`

Legacy helper for a Claude Code agent task session; there is no production caller
in the current session runtime. If explicitly invoked, it also writes a copy to
`tasks/<task_id>.jsonl` when `task_id` is provided.

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
  paths for SDK attempts are logged. Gate refusals happen before this block.

### 4.3 Coding-agent sessions and headless CLI calls

The retired Claude platform logging integration is no longer wired into production.
Tmux sessions are accounted for by `src/sessions/transcripts/watcher.py` instead.
`src/llm/cli.py` reports usage to its playbook step but does not call this logger.

## 5. Configuration

```yaml
llm_logging:
  enabled: true          # default: true
  retention_days: 30     # default: 30
```

| YAML key | Type | Default | Description |
|----------|------|---------|-------------|
| `enabled` | `bool` | `true` | When false, all logging is a no-op |
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

# Legacy agent-session files, if retained from an older installation
jq '{task: .task_id, tokens: .output.tokens_used, duration_s: (.duration_ms/1000)}' \
  logs/llm/*/claude_agent.jsonl
```
