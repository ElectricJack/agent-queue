---
tags: [spec, adapters, development-guide]
---

# Adapter Development Guide

How to add a new agent adapter to agent-queue so that a third-party AI coding
agent can be orchestrated alongside (or instead of) Claude Code.

---

## 1. Architecture Overview

The orchestrator never talks to a specific agent implementation directly. All
agent backends sit behind the `AgentAdapter` abstract base class defined in
`src/adapters/base.py`. The orchestrator interacts with adapters exclusively
through four async methods — `start`, `wait`, `stop`, `is_alive` — and uses
`AdapterFactory` (in `src/adapters/__init__.py`) to instantiate them by type
string.

```
Orchestrator
  │
  ├─ AdapterFactory.create("claude")  → ClaudeAdapter
  ├─ AdapterFactory.create("my-agent") → MyAgentAdapter   ← you add this
  │
  └─ AgentAdapter ABC (start / wait / stop / is_alive)
```

**Canonical example:** `src/adapters/claude.py` — read this file end-to-end
before writing your own adapter. It demonstrates every pattern described below.
See also `docs/specs/adapters/claude.md` for the full behavioral spec.

---

## 2. The AgentAdapter ABC

```python
# src/adapters/base.py

MessageCallback = Callable[[str], Awaitable[None]]

class AgentAdapter(ABC):
    @abstractmethod
    async def start(self, task: TaskContext) -> None: ...

    @abstractmethod
    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def is_alive(self) -> bool: ...
```

### Method Contract

| Method | Called by | Must do |
|--------|-----------|---------|
| `start(task)` | Orchestrator, once per task execution | Store task context, reset internal state. Do **not** launch the agent process here. |
| `wait(on_message)` | Orchestrator, immediately after `start` | Launch the agent, block until it finishes, stream progress via `on_message`, return `AgentOutput`. |
| `stop()` | Orchestrator on cancellation or timeout | Signal the agent to stop. Cooperative — `wait()` checks for the signal. |
| `is_alive()` | Heartbeat monitor, periodically | Return `True` if `start()` has been called and the agent has not been stopped or finished. |

### Why `start` does not launch the agent

Separating `start` from `wait` lets the orchestrator set up the Discord thread
(wiring `on_message`) between the two calls. If you launch the process in
`start`, early output would be lost before `wait` is called.

---

## 3. Key Data Types

All types are defined in `src/models.py`.

### TaskContext — what your adapter receives

```python
@dataclass
class TaskContext:
    description: str                              # The task prompt (always present)
    task_id: str = ""                             # Unique task identifier
    acceptance_criteria: list[str] = field(...)    # What "done" looks like
    test_commands: list[str] = field(...)          # Commands to verify the work
    checkout_path: str = ""                       # Git worktree / working directory
    branch_name: str = ""                         # Git branch for this task
    attached_context: list[str] = field(...)       # Extra context strings
    mcp_servers: dict[str, dict] = field(...)      # MCP server configurations
```

Your adapter should:

- Use `description` as the primary prompt.
- Append `acceptance_criteria`, `test_commands`, and `attached_context` into your
  agent's prompt format (see `ClaudeAdapter._build_prompt()` for an example).
- Set the working directory to `checkout_path` when launching the agent process.
- Forward `mcp_servers` if your agent supports the Model Context Protocol.

### AgentOutput — what your adapter returns

```python
@dataclass
class AgentOutput:
    result: AgentResult        # Required: the outcome enum
    summary: str = ""          # Human-readable summary of what the agent did
    files_changed: list[str] = field(...)  # Paths modified (for PR decisions)
    tokens_used: int = 0       # Total tokens consumed (input + output)
    error_message: str | None = None       # Set on failure paths
    question: str | None = None            # Set when result is WAITING_INPUT
```

### AgentResult — the outcome enum

| Value | Meaning | Orchestrator action |
|-------|---------|---------------------|
| `COMPLETED` | Agent finished successfully | Transition task to COMPLETED (or AWAITING_APPROVAL / AWAITING_PLAN_APPROVAL / BLOCKED depending on outcome) |
| `FAILED` | Agent crashed or could not complete | Retry or transition to FAILED |
| `PAUSED_TOKENS` | Token budget / quota exhausted | Pause task, auto-resume later |
| `PAUSED_RATE_LIMIT` | Rate-limited by the API | Pause task, auto-resume later |
| `WAITING_INPUT` | Agent has a question for a human | Transition to WAITING_INPUT, notify Discord |

### MessageCallback — streaming output

```python
MessageCallback = Callable[[str], Awaitable[None]]
```

The orchestrator passes an `on_message` callback wired to a Discord thread. Call
it with human-readable markdown strings as your agent produces output. Keep
messages concise — Discord has a 2000-char message limit, and the orchestrator
may split or truncate. Typical patterns:

- Forward the agent's "thinking" output (truncated).
- Forward tool invocations as short one-liners (e.g., `-# Read · src/main.py`).
- Forward final results with token/cost summaries.

---

## 4. Step-by-Step: Creating a New Adapter

### 4.1 Create the adapter module

Create `src/adapters/my_agent.py`:

```python
"""My Agent adapter — runs tasks via the My Agent CLI/SDK."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from src.adapters.base import AgentAdapter, MessageCallback
from src.models import AgentOutput, AgentResult, TaskContext

logger = logging.getLogger(__name__)


@dataclass
class MyAgentConfig:
    """Configuration for the My Agent adapter."""
    model: str = "default-model"
    api_key: str = ""
    timeout_seconds: int = 600
    # Add agent-specific config fields here


class MyAgentAdapter(AgentAdapter):
    """AgentAdapter implementation for My Agent."""

    def __init__(self, config: MyAgentConfig | None = None, llm_logger=None):
        self._config = config or MyAgentConfig()
        self._task: TaskContext | None = None
        self._cancel_event = asyncio.Event()
        self._process = None  # Track your subprocess/session here
        self._llm_logger = llm_logger

    async def start(self, task: TaskContext) -> None:
        """Store task context; do NOT launch the agent process yet."""
        self._task = task
        self._cancel_event.clear()
        logger.info("MyAgent adapter starting for task %s", task.task_id)

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        """Launch the agent, stream output, return result."""
        try:
            prompt = self._build_prompt()
            tokens_used = 0
            summary_parts = []

            # --- Launch your agent here ---
            # Example: subprocess, SDK call, HTTP API, etc.
            # self._process = await self._launch_agent(prompt)

            # --- Stream output ---
            # async for chunk in self._iter_agent_output():
            #     # Check for cancellation on every iteration
            #     if self._cancel_event.is_set():
            #         return AgentOutput(
            #             result=AgentResult.FAILED,
            #             summary="Cancelled",
            #             error_message="Agent was stopped",
            #         )
            #
            #     # Forward to Discord
            #     if on_message and chunk.text:
            #         await on_message(chunk.text)
            #
            #     # Accumulate results
            #     if chunk.is_final:
            #         summary_parts.append(chunk.text)
            #     tokens_used += chunk.tokens

            return AgentOutput(
                result=AgentResult.COMPLETED,
                summary="\n".join(summary_parts) or "Completed",
                tokens_used=tokens_used,
            )

        except Exception as e:
            logger.exception("MyAgent adapter error")
            # Map specific errors to appropriate AgentResult values
            if self._is_token_error(e):
                return AgentOutput(
                    result=AgentResult.PAUSED_TOKENS,
                    error_message=str(e),
                )
            if self._is_rate_limit_error(e):
                return AgentOutput(
                    result=AgentResult.PAUSED_RATE_LIMIT,
                    error_message=str(e),
                )
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message=str(e),
            )

    async def stop(self) -> None:
        """Signal the agent to stop."""
        self._cancel_event.set()
        # Also kill the subprocess if applicable:
        # if self._process:
        #     self._process.terminate()

    async def is_alive(self) -> bool:
        """Return True if the agent is running."""
        return self._task is not None and not self._cancel_event.is_set()

    def _build_prompt(self) -> str:
        """Assemble TaskContext into a prompt string."""
        parts = [self._task.description]
        if self._task.acceptance_criteria:
            parts.append("\n## Acceptance Criteria")
            for c in self._task.acceptance_criteria:
                parts.append(f"- {c}")
        if self._task.test_commands:
            parts.append("\n## Test Commands")
            for cmd in self._task.test_commands:
                parts.append(f"- `{cmd}`")
        if self._task.attached_context:
            parts.append("\n## Additional Context")
            for ctx in self._task.attached_context:
                parts.append(f"- {ctx}")
        return "\n".join(parts)

    def _is_token_error(self, e: Exception) -> bool:
        msg = str(e).lower()
        return "token" in msg or "quota" in msg

    def _is_rate_limit_error(self, e: Exception) -> bool:
        msg = str(e).lower()
        return "rate limit" in msg or "429" in msg
```

### 4.2 Register in AdapterFactory

Edit `src/adapters/__init__.py` to import your adapter and add a branch to
`create()`:

```python
from src.adapters.my_agent import MyAgentAdapter, MyAgentConfig

class AdapterFactory:
    def __init__(self, claude_config=None, my_agent_config=None, llm_logger=None):
        self._claude_config = claude_config or ClaudeAdapterConfig()
        self._my_agent_config = my_agent_config or MyAgentConfig()
        self._llm_logger = llm_logger

    def create(self, agent_type: str, profile=None) -> AgentAdapter:
        if agent_type == "claude":
            config = self._config_for_profile(profile)
            return ClaudeAdapter(config, llm_logger=self._llm_logger)
        if agent_type == "my-agent":
            return MyAgentAdapter(self._my_agent_config, llm_logger=self._llm_logger)
        raise ValueError(f"Unknown agent type: {agent_type}")
```

### 4.3 Add configuration

If your adapter needs config values from `config.yaml`, add a new section to
`AppConfig` in `src/config.py` and wire it through to `AdapterFactory` in
`src/main.py` or `src/orchestrator.py`.

### 4.4 Write tests

See [Section 7: Testing Strategy](#7-testing-strategy) below.

---

## 5. Common Pitfalls

### 5.1 Heartbeat / is_alive

The orchestrator's heartbeat monitor calls `is_alive()` periodically. If it
returns `False` while a task is `IN_PROGRESS`, the orchestrator considers the
agent dead and may restart or fail the task.

**Pitfall:** If your adapter returns `False` from `is_alive()` before `wait()`
has finished, the orchestrator will race against your adapter. Make sure
`is_alive()` returns `True` for the entire duration between `start()` and the
completion of `wait()`.

**Pattern:** Use a simple flag approach like `ClaudeAdapter`:

```python
async def is_alive(self) -> bool:
    return self._task is not None and not self._cancel_event.is_set()
```

### 5.2 Token Counting

The orchestrator uses `AgentOutput.tokens_used` for budget tracking and
scheduling decisions. Always populate this field, even with an estimate.

**Pitfall:** Returning `tokens_used=0` with no summary causes `ClaudeAdapter`
to treat the run as a failure (silent crash detection). If your agent genuinely
uses 0 tokens sometimes, include a non-empty summary to avoid this pattern.

**Pitfall:** Don't count only output tokens. The field represents total tokens
(input + output). If your agent's API provides both, sum them.

### 5.3 Error Classification

Map errors to the correct `AgentResult` so the orchestrator can handle them
appropriately:

| Error type | Return as | Why |
|------------|-----------|-----|
| Token/quota exhausted | `PAUSED_TOKENS` | Orchestrator pauses the task and auto-resumes after the quota resets |
| Rate limited (HTTP 429) | `PAUSED_RATE_LIMIT` | Same as above, with a shorter backoff |
| Agent needs human input | `WAITING_INPUT` (set `question` field) | Orchestrator notifies Discord and waits for reply |
| Transient failure | `FAILED` | Orchestrator retries up to `max_retries` |
| Permanent failure | `FAILED` | After max retries, task transitions to FAILED |

**Pitfall:** Returning `FAILED` for rate limits wastes retry attempts. Return
`PAUSED_RATE_LIMIT` instead so the orchestrator waits rather than retrying
immediately.

### 5.4 Cancellation Must Be Cooperative

`stop()` is called from outside the `wait()` coroutine (typically from a
different task in the event loop). You cannot forcefully abort `wait()` from
`stop()` — you can only signal it.

**Pattern:** Set an `asyncio.Event` in `stop()`, check it on every iteration of
your message loop in `wait()`. If your agent blocks for long periods without
producing output, also check the event with a timeout:

```python
# Inside wait(), if the agent may block:
try:
    result = await asyncio.wait_for(
        self._get_next_message(),
        timeout=5.0,
    )
except asyncio.TimeoutError:
    if self._cancel_event.is_set():
        return AgentOutput(result=AgentResult.FAILED, ...)
    continue  # No message yet, keep waiting
```

### 5.5 Environment Isolation

If your agent runs as a subprocess, be careful about environment variable
leakage. The orchestrator process may have variables set by other components
(Discord bot token, database paths, etc.) that could confuse your agent.

**Pattern:** Build an explicit environment dict for your subprocess rather than
inheriting the full `os.environ`. See how `ClaudeAdapter` strips `CLAUDECODE`
and `CLAUDE_CODE_ENTRYPOINT` before launching.

### 5.6 Working Directory

Always set the subprocess working directory to `task.checkout_path`. This is a
git worktree prepared by the orchestrator specifically for this task. If
`checkout_path` is empty, fall back to the project's workspace directory or
raise an error — never run in the orchestrator's own working directory.

### 5.7 Streaming vs. Batched Output

The `on_message` callback is optional. Your adapter must work correctly whether
or not a callback is provided. Never assume `on_message` is set — always guard
calls:

```python
if on_message and text:
    await on_message(text)
```

---

## 6. Agent Profiles

The orchestrator supports `AgentProfile` objects that override adapter config on
a per-task basis (model, permission mode, allowed tools, MCP servers, etc.). Your
adapter should accept profile overrides through `AdapterFactory._config_for_profile`.

See `AgentProfile` in `src/models.py` for the full field list:

```python
@dataclass
class AgentProfile:
    id: str
    name: str
    description: str = ""
    model: str = ""
    permission_mode: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    system_prompt_suffix: str = ""
    install: dict = field(default_factory=dict)
```

When profile fields are empty, fall through to the adapter's base config
defaults.

---

## 7. Logging Integration

### 7.1 Structured logging with correlation context

The system uses structured JSON logging with per-task correlation IDs (see
`src/logging_config.py`). Your adapter should use the standard `logging` module
— correlation fields (`task_id`, `project_id`, `cycle_id`) are automatically
injected into every log record by the `CorrelationFilter`.

```python
import logging
from src.logging_config import get_correlation_context

logger = logging.getLogger(__name__)

async def start(self, task: TaskContext) -> None:
    self._task = task
    self._cancel_event.clear()
    # Correlation context is set by the orchestrator before calling start().
    # Just use the standard logger — fields are injected automatically.
    ctx = get_correlation_context()
    logger.info(
        "MyAgent adapter starting for task %s",
        ctx.get("task_id", task.task_id),
    )
```

You can also call `get_correlation_context()` to access the current task/project
IDs explicitly (e.g., for passing to external APIs or custom log calls).

### 7.2 LLM session logging

The `AdapterFactory` passes an optional `llm_logger` (an `LLMLogger` instance
from `src/llm_logger.py`) to each adapter. If present, your adapter should call
`llm_logger.log_agent_session()` after each execution to record prompt, output,
duration, and token usage for analytics.

```python
import time

async def wait(self, on_message=None) -> AgentOutput:
    start_time = time.monotonic()
    prompt = self._build_prompt()
    # ... run agent ...
    output = AgentOutput(result=AgentResult.COMPLETED, ...)

    # Log the session
    if self._llm_logger:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        self._llm_logger.log_agent_session(
            task_id=self._task.task_id,
            session_id=self._session_id,  # from your agent's session
            model=self._config.model or "(default)",
            prompt=prompt,
            config_summary={
                "allowed_tools": self._config.allowed_tools,
                # Include agent-specific config relevant for debugging
            },
            output=output,
            duration_ms=duration_ms,
        )
    return output
```

Log the session on **all** code paths — success, failure, cancellation, and
pause — so that token usage is tracked even for failed runs. See
`ClaudeAdapter._log_session()` for the reference pattern.

---

## 8. Testing Strategy

### Unit tests (required)

Create `tests/test_my_agent_adapter.py`:

```python
import asyncio
import pytest
from src.adapters.my_agent import MyAgentAdapter, MyAgentConfig
from src.models import AgentOutput, AgentResult, TaskContext


def make_task(**overrides) -> TaskContext:
    """Helper to create a TaskContext with sensible defaults."""
    defaults = dict(
        description="Implement the foo feature",
        task_id="test-task-1",
        checkout_path="/tmp/test-workspace",
    )
    defaults.update(overrides)
    return TaskContext(**defaults)


@pytest.mark.asyncio
async def test_start_stores_task():
    adapter = MyAgentAdapter()
    task = make_task()
    await adapter.start(task)
    assert await adapter.is_alive()


@pytest.mark.asyncio
async def test_stop_cancels():
    adapter = MyAgentAdapter()
    await adapter.start(make_task())
    await adapter.stop()
    assert not await adapter.is_alive()


@pytest.mark.asyncio
async def test_wait_returns_agent_output():
    adapter = MyAgentAdapter()
    await adapter.start(make_task())
    result = await adapter.wait()
    assert isinstance(result, AgentOutput)
    assert result.result in AgentResult


@pytest.mark.asyncio
async def test_on_message_callback_called():
    """Verify that wait() streams output through the callback."""
    messages = []
    adapter = MyAgentAdapter()
    await adapter.start(make_task())
    await adapter.wait(on_message=lambda text: messages.append(text))
    # Assert messages were received (specifics depend on your agent)


@pytest.mark.asyncio
async def test_cancellation_during_wait():
    """Verify that stop() causes wait() to return FAILED."""
    adapter = MyAgentAdapter()
    await adapter.start(make_task())

    async def cancel_after_delay():
        await asyncio.sleep(0.1)
        await adapter.stop()

    asyncio.create_task(cancel_after_delay())
    result = await adapter.wait()
    assert result.result == AgentResult.FAILED
```

### Integration tests (recommended)

If your agent has a sandbox or dry-run mode, write an integration test that runs
a trivial task (e.g., "create a file called hello.txt") and verifies:

- `AgentOutput.result` is `COMPLETED`
- `AgentOutput.tokens_used` > 0
- The file was actually created in the `checkout_path`

---

## 9. Checklist

Before submitting your adapter:

- [ ] Subclasses `AgentAdapter` from `src/adapters/base.py`
- [ ] Implements all four abstract methods (`start`, `wait`, `stop`, `is_alive`)
- [ ] `start()` stores context but does NOT launch the agent
- [ ] `wait()` checks `_cancel_event` on every loop iteration
- [ ] `wait()` streams output via `on_message` when provided
- [ ] Returns correct `AgentResult` values (especially `PAUSED_*` for rate limits)
- [ ] Populates `tokens_used` in `AgentOutput`
- [ ] Registered in `AdapterFactory.create()` with a unique type string
- [ ] Has a `*Config` dataclass for adapter-specific settings
- [ ] Has unit tests covering start/stop/wait/cancellation
- [ ] Sets working directory to `task.checkout_path`
- [ ] Handles `ImportError` gracefully if the agent SDK is optional
- [ ] Logs session to `llm_logger` on all code paths (success, failure, cancel)
- [ ] Uses standard `logging` module (correlation context is auto-injected)
