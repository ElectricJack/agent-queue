---
tags: [adapters, development-guide]
---

# Adapter Development Guide

How to add a new AI agent backend to Agent Queue.

> See also: [[specs/adapters/claude|Claude adapter spec]] and [[specs/adapters/development-guide|Adapter Development Guide spec]] for detailed reference.
>
> See also: [[specs/design/agent-coordination]] for how adapters interact with coordination playbooks.

## Architecture Overview

Agent adapters are the bridge between the [[specs/orchestrator|orchestrator]] and external AI coding
agents. Each adapter implements a minimal 4-method interface that the
orchestrator calls during the task execution pipeline. The adapter is
responsible for launching the agent process, streaming its output, and
returning structured results.

```
Orchestrator                    Adapter                     Agent Process
    │                             │                              │
    ├── start(TaskContext) ──────►│                              │
    │                             ├── (prepare config) ─────────►│
    │                             │                              │
    ├── wait(on_message) ────────►│                              │
    │                             ├── (stream messages) ◄────────┤
    │   ◄── on_message("...") ───┤                              │
    │   ◄── on_message("...") ───┤                              │
    │   ◄── AgentOutput ─────────┤                              │
    │                             │                              │
    ├── stop() ──────────────────►│── (kill process) ───────────►│
    │                             │                              │
    ├── is_alive() ──────────────►│── (check process) ──────────►│
    │   ◄── bool ────────────────┤                              │
```

## Step-by-Step Guide

### 1. Create the Adapter File

Create `src/adapters/your_agent.py`:

```python
"""YourAgent adapter — wraps the YourAgent CLI/SDK for agent-queue orchestration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from src.adapters.base import AgentAdapter, MessageCallback
from src.models import AgentOutput, AgentResult, TaskContext

logger = logging.getLogger(__name__)


@dataclass
class YourAgentConfig:
    """Configuration for the YourAgent adapter.

    These values come from the AdapterFactory's base config, which can
    be overridden per-task via AgentProfiles.
    """
    model: str = "default-model"
    max_tokens: int = 200000
    # Add any agent-specific settings here


class YourAgentAdapter(AgentAdapter):
    """Adapter for YourAgent coding assistant.

    Lifecycle:
      1. ``start()`` stores the task context and prepares the agent config.
      2. ``wait()`` launches the agent process, streams output via on_message,
         and blocks until the agent finishes.
      3. ``stop()`` forcefully kills the agent process (e.g., on admin cancel).
      4. ``is_alive()`` checks if the agent subprocess is still running.
    """

    def __init__(self, config: YourAgentConfig | None = None):
        self._config = config or YourAgentConfig()
        self._task: TaskContext | None = None
        self._process = None  # subprocess handle
        self._cancelled = False

    async def start(self, task: TaskContext) -> None:
        """Prepare the adapter for task execution.

        Store the task context and reset cancellation state. The actual
        agent process is NOT launched here — that happens in wait().
        This separation allows the orchestrator to set up Discord threads
        between start() and wait().
        """
        self._task = task
        self._cancelled = False

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        """Launch the agent, stream output, and return results.

        This is where the main work happens:
        1. Build the prompt from self._task (description, criteria, context)
        2. Launch the agent subprocess or SDK call
        3. Stream progress messages via on_message callback
        4. Parse the agent's output into an AgentOutput

        The on_message callback sends text to the task's Discord thread
        for live progress updates. Call it with human-readable status
        messages (not raw API responses).
        """
        if not self._task:
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message="No task context — call start() first",
            )

        # Build the prompt from TaskContext
        prompt = self._build_prompt(self._task)

        try:
            # --- Launch your agent here ---
            # Example: subprocess, SDK call, API request, etc.
            #
            # self._process = await asyncio.create_subprocess_exec(
            #     "your-agent", "--prompt", prompt,
            #     "--workspace", self._task.checkout_path,
            #     stdout=asyncio.subprocess.PIPE,
            #     stderr=asyncio.subprocess.PIPE,
            # )

            # --- Stream output ---
            # While the agent runs, forward messages to Discord:
            #
            # async for line in self._process.stdout:
            #     text = line.decode().strip()
            #     if on_message and text:
            #         await on_message(text)

            # --- Parse results ---
            # Map the agent's exit status to AgentResult:
            #
            # if exit_code == 0:
            #     return AgentOutput(
            #         result=AgentResult.COMPLETED,
            #         summary="Task completed successfully",
            #         files_changed=["file1.py", "file2.py"],
            #         tokens_used=12345,
            #     )
            # else:
            #     return AgentOutput(
            #         result=AgentResult.FAILED,
            #         error_message=stderr_output,
            #     )

            raise NotImplementedError("Implement agent execution logic")

        except asyncio.CancelledError:
            await self.stop()
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message="Task was cancelled",
            )

    async def stop(self) -> None:
        """Forcefully terminate the agent process.

        Called by the orchestrator when an admin stops a task or when
        the daemon is shutting down. Must be safe to call multiple times
        and when no process is running.
        """
        self._cancelled = True
        if self._process is not None:
            try:
                self._process.kill()
                await self._process.wait()
            except ProcessLookupError:
                pass  # Already exited
            finally:
                self._process = None

    async def is_alive(self) -> bool:
        """Check if the agent process is still running.

        Used by the heartbeat monitor to detect dead agents. Return True
        if the agent subprocess is active, False otherwise.
        """
        if self._process is None:
            return False
        return self._process.returncode is None

    def _build_prompt(self, task: TaskContext) -> str:
        """Construct the full prompt from TaskContext fields.

        The orchestrator populates TaskContext with everything the agent
        needs. Your adapter should map these fields to whatever format
        your agent expects.
        """
        parts = [task.description]

        if task.acceptance_criteria:
            parts.append("\n## Acceptance Criteria")
            for criterion in task.acceptance_criteria:
                parts.append(f"- {criterion}")

        if task.test_commands:
            parts.append("\n## Test Commands")
            for cmd in task.test_commands:
                parts.append(f"- `{cmd}`")

        if task.attached_context:
            parts.append("\n## Additional Context")
            for ctx in task.attached_context:
                parts.append(ctx)

        return "\n".join(parts)
```

### 2. Register in the AdapterFactory

Edit `src/adapters/__init__.py` to register your new adapter type:

```python
from src.adapters.your_agent import YourAgentAdapter, YourAgentConfig

class AdapterFactory:
    def __init__(self, ...):
        # Add your agent's base config
        self._configs = {
            "claude": ClaudeAdapterConfig(),
            "your_agent": YourAgentConfig(),
        }

    def create(self, agent_type: str, profile=None) -> AgentAdapter:
        if agent_type == "claude":
            return ClaudeAdapter(config=self._configs["claude"])
        elif agent_type == "your_agent":
            return YourAgentAdapter(config=self._configs["your_agent"])
        else:
            raise ValueError(f"Unknown agent type: {agent_type}")
```

### 3. Register Agents with the New Type

Agents are stored in the database with an `agent_type` field. To use your
new adapter, register agents with your type string:

```
/add-agent name="my-agent" type="your_agent"
```

The orchestrator will automatically use your adapter when scheduling tasks
to agents of this type.

## Key Interfaces

### TaskContext (Input)

The `TaskContext` dataclass is everything the adapter receives about the task:

| Field | Type | Description |
|-------|------|-------------|
| `description` | `str` | Full task instructions |
| `task_id` | `str` | Unique task identifier (for logging) |
| `acceptance_criteria` | `list[str]` | "Done" conditions |
| `test_commands` | `list[str]` | Verification commands to run |
| `checkout_path` | `str` | Workspace directory path |
| `branch_name` | `str` | Git branch to work on |
| `attached_context` | `list[str]` | Extra context (docs, memories) |
| `mcp_servers` | `dict` | MCP server configurations |

### AgentOutput (Output)

The `AgentOutput` dataclass is what the adapter returns:

| Field | Type | Description |
|-------|------|-------------|
| `result` | `AgentResult` | Outcome enum (see below) |
| `summary` | `str` | Human-readable summary for Discord |
| `files_changed` | `list[str]` | Modified file paths |
| `tokens_used` | `int` | Token count for budget tracking |
| `error_message` | `str\|None` | Error details (on failure) |
| `question` | `str\|None` | Agent question (on WAITING_INPUT) |

### AgentResult Values

| Value | Meaning | Orchestrator Action |
|-------|---------|-------------------|
| `COMPLETED` | Agent finished successfully | Run verification, create PR |
| `FAILED` | Agent hit an error | Increment retries, may block |
| `PAUSED_TOKENS` | Token budget exhausted | Pause with resume timer |
| `PAUSED_RATE_LIMIT` | API rate limited | Pause with backoff timer |
| `WAITING_INPUT` | Agent needs human input | Post question to Discord |

### MessageCallback

```python
MessageCallback = Callable[[str], Awaitable[None]]
```

Called with human-readable progress strings during `wait()`. The orchestrator
wires this to a Discord thread for live output. Keep messages concise and
meaningful — they appear directly in the Discord thread.

## Agent Profiles

Adapters can receive per-task configuration overrides via `AgentProfile`:

- **model** — override the default model
- **allowed_tools** — restrict available tools
- **mcp_servers** — additional MCP server configs
- **system_prompt_suffix** — extra instructions

The `AdapterFactory.create()` receives the resolved profile and should
merge it with the base config. See `ClaudeAdapter` for an example of
how profile fields override defaults.

## Testing Your Adapter

1. **Unit test** the adapter in isolation with mock subprocesses
2. **Integration test** with a real agent binary and a test workspace
3. **End-to-end test** by registering an agent and creating a test task

Example test structure:

```python
import pytest
from src.adapters.your_agent import YourAgentAdapter
from src.models import TaskContext, AgentResult

@pytest.mark.asyncio
async def test_basic_execution():
    adapter = YourAgentAdapter()
    task = TaskContext(
        description="Create a hello.py file",
        checkout_path="/tmp/test-workspace",
        branch_name="test-branch",
    )
    await adapter.start(task)
    output = await adapter.wait()
    assert output.result == AgentResult.COMPLETED

@pytest.mark.asyncio
async def test_stop():
    adapter = YourAgentAdapter()
    task = TaskContext(description="Long running task")
    await adapter.start(task)
    await adapter.stop()
    assert not await adapter.is_alive()
```

## Reference: ClaudeAdapter

The existing [[specs/adapters/claude|ClaudeAdapter]] (`src/adapters/claude.py`) is the reference
implementation. Key patterns to follow:

- **Environment scrubbing**: Clear agent-specific env vars to avoid conflicts
  with nested invocations
- **Resilient parsing**: Handle unknown/unexpected message formats gracefully
- **Token tracking**: Extract and report token usage for budget management
- **Graceful shutdown**: Handle SIGTERM/SIGINT during execution
- **Profile merging**: Override model, tools, and MCP servers from profiles
