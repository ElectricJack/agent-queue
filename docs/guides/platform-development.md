---
tags: [platforms, development-guide]
---

# Platform Development Guide

How to add a new AI agent backend to Agent Queue.

> See also: [[specs/platforms/claude_sdk|Claude SDK platform spec]] and [[specs/platforms/development-guide|Platform Development Guide spec]] for detailed reference.
>
> See also: [[specs/design/agent-coordination]] for how platforms interact with coordination playbooks.

## Architecture Overview

Agent platforms are the bridge between the [[specs/orchestrator|orchestrator]] and external AI coding
agents. Each platform implements a minimal 4-method interface that the
orchestrator calls during the task execution pipeline. The platform is
responsible for launching the agent process, streaming its output, and
returning structured results.

```
Orchestrator                    Platform                    Agent Process
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

### 1. Create the Platform File

Create `src/platforms/your_agent.py`:

```python
"""YourAgent platform — wraps the YourAgent CLI/SDK for agent-queue orchestration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import ClassVar

from src.platforms.base import Platform, Capability, MessageCallback
from src.models import AgentOutput, AgentResult, TaskContext

logger = logging.getLogger(__name__)


@dataclass
class YourAgentConfig:
    """Configuration for the YourAgent platform.

    Profile-bound construction happens in _config_from_profile(); see
    ClaudeSDKPlatform for the canonical pattern.
    """
    model: str = "default-model"
    max_tokens: int = 200000
    # Add any agent-specific settings here


class YourAgentPlatform(Platform):
    """Platform for YourAgent coding assistant.

    Lifecycle:
      1. ``start()`` stores the task context and prepares the agent config.
      2. ``wait()`` launches the agent process, streams output via on_message,
         and blocks until the agent finishes.
      3. ``stop()`` signals cancellation via an asyncio.Event (cooperative).
      4. ``is_alive()`` checks whether the agent is still running.
    """

    name: ClassVar[str] = "your_agent"
    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    def __init__(self, profile=None, llm_logger=None):
        self._config = self._config_from_profile(profile)
        self._task: TaskContext | None = None
        self._cancel_event = asyncio.Event()
        self._process = None  # subprocess handle
        self._llm_logger = llm_logger

    @staticmethod
    def _config_from_profile(profile) -> YourAgentConfig:
        """Translate an AgentProfile into a YourAgentConfig.

        Fields left empty in the profile fall through to base config defaults.
        See ClaudeSDKPlatform._config_from_profile() for the canonical pattern.
        """
        base = YourAgentConfig()
        if profile is None:
            return base
        return YourAgentConfig(
            model=profile.model or base.model,
            # Map other profile fields as appropriate
        )

    async def start(self, task: TaskContext) -> None:
        """Prepare the platform for task execution.

        Store the task context and reset cancellation state. The actual
        agent process is NOT launched here — that happens in wait().
        This separation allows the orchestrator to set up Discord threads
        between start() and wait().
        """
        self._task = task
        self._cancel_event.clear()

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
            # While the agent runs, check cancellation and forward messages:
            #
            # async for line in self._process.stdout:
            #     if self._cancel_event.is_set():
            #         break
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
        """Signal the agent to stop.

        Sets the cancel event (cooperative cancellation). If you hold a
        subprocess reference, also terminate it. Must be safe to call
        multiple times and when no process is running.
        """
        self._cancel_event.set()
        if self._process is not None:
            try:
                self._process.kill()
                await self._process.wait()
            except ProcessLookupError:
                pass  # Already exited
            finally:
                self._process = None

    async def is_alive(self) -> bool:
        """Check if the agent is still running.

        Used by the heartbeat monitor to detect dead agents. Must return
        True for the entire duration between start() and wait() finishing.
        """
        return self._task is not None and not self._cancel_event.is_set()

    def _build_prompt(self, task: TaskContext) -> str:
        """Construct the full prompt from TaskContext fields.

        The orchestrator populates TaskContext with everything the agent
        needs. Your platform should map these fields to whatever format
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

### 2. Register in PlatformRegistry

Edit `src/platforms/__init__.py` to add your platform to `default_registry()`:

```python
# src/platforms/__init__.py
def default_registry() -> PlatformRegistry:
    from src.platforms.claude_sdk import ClaudeSDKPlatform
    from src.platforms.your_agent import YourAgentPlatform  # add this
    return PlatformRegistry(platforms={
        ClaudeSDKPlatform.name: ClaudeSDKPlatform,
        YourAgentPlatform.name: YourAgentPlatform,
    })
```

`PlatformRegistry` is a simple dict-based lookup — no factory class needed.
Instantiation happens via `PlatformRegistry.create(name, profile, llm_logger)`,
which calls `cls(profile=profile, llm_logger=llm_logger)` on the registered class.

### 3. Register Agents with the New Type

Agents are stored in the database with an `agent_type` field. To use your
new platform, register agents with your type string:

```
/add-agent name="my-agent" type="your_agent"
```

The orchestrator will automatically use your platform when scheduling tasks
to agents of this type.

## Key Interfaces

### TaskContext (Input)

The `TaskContext` dataclass is everything the platform receives about the task:

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

The `AgentOutput` dataclass is what the platform returns:

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

Platforms receive per-task configuration overrides via `AgentProfile`. The
profile is passed at construction time; profile→config translation belongs in
a `_config_from_profile` static method on your platform class (see
`ClaudeSDKPlatform._config_from_profile()` for the canonical example):

```python
@staticmethod
def _config_from_profile(profile) -> YourAgentConfig:
    base = YourAgentConfig()
    if profile is None:
        return base
    return YourAgentConfig(
        model=profile.model or base.model,
        # Map other profile fields here
    )
```

Profile fields available for override include:
- **model** — override the default model
- **allowed_tools** — restrict available tools
- **mcp_servers** — additional MCP server configs
- **system_prompt_suffix** — extra instructions
- **permission_mode** — tool permission mode

When a profile field is empty, fall through to the platform's base config
defaults.

## Testing Your Platform

1. **Unit test** the platform in isolation with mock subprocesses
2. **Integration test** with a real agent binary and a test workspace
3. **End-to-end test** by registering an agent and creating a test task

Example test structure:

```python
import pytest
from src.platforms.your_agent import YourAgentPlatform
from src.models import TaskContext, AgentResult

@pytest.mark.asyncio
async def test_basic_execution():
    platform = YourAgentPlatform()
    task = TaskContext(
        description="Create a hello.py file",
        checkout_path="/tmp/test-workspace",
        branch_name="test-branch",
    )
    await platform.start(task)
    output = await platform.wait()
    assert output.result == AgentResult.COMPLETED

@pytest.mark.asyncio
async def test_stop():
    platform = YourAgentPlatform()
    task = TaskContext(description="Long running task")
    await platform.start(task)
    await platform.stop()
    assert not await platform.is_alive()
```

## Reference: ClaudeSDKPlatform

The existing [[specs/platforms/claude_sdk|ClaudeSDKPlatform]] (`src/platforms/claude_sdk.py`) is the reference
implementation. Key patterns to follow:

- **Profile-bound construction**: `__init__(self, profile=None, llm_logger=None)` with
  profile→config translation in `_config_from_profile()` static method
- **ClassVar declarations**: `name: ClassVar[str]` and `capabilities: ClassVar[frozenset[Capability]]`
  are required by the `Platform` ABC
- **Cooperative cancellation**: Use `asyncio.Event` for `_cancel_event`; check it on
  every loop iteration in `wait()`
- **Environment scrubbing**: Clear agent-specific env vars to avoid conflicts
  with nested invocations
- **Resilient parsing**: Handle unknown/unexpected message formats gracefully
- **Token tracking**: Extract and report token usage for budget management
- **Graceful shutdown**: Handle SIGTERM/SIGINT during execution
- **Profile merging**: Override model, tools, and MCP servers from profiles
