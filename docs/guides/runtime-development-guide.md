# Platform Development Guide

> **Consolidated:** See [[platform-development]] for the
> full step-by-step guide with code examples, interface documentation, and testing
> instructions.

How to add a new agent runtime to the Agent Queue system.

## Architecture Overview

```
Orchestrator ──► Platform ──► Agent Process
    │               │                  │
    ├── start()    launch subprocess   │
    ├── wait()     stream output ◄─────┤
    ├── is_alive() check process       │
    └── stop()     kill process        │
```

## The Platform Interface

Located in `src/runtimes/base.py`:

```python
class Platform(ABC):
    name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]]

    @abstractmethod
    async def start(self, task: TaskContext) -> None:
        """Launch the agent process with the given task."""

    @abstractmethod
    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        """Wait for the agent to finish and return results."""

    @abstractmethod
    async def stop(self) -> None:
        """Forcefully stop the agent."""

    @abstractmethod
    async def is_alive(self) -> bool:
        """Check if the agent process is still running."""
```

## Step-by-Step: Adding a New Platform

### 1. Create the Platform File

Create `src/runtimes/your_agent.py` implementing `Runtime`.

### 2. Register in PlatformRegistry

Add your runtime to `default_registry()` in `src/runtimes/__init__.py`:

```python
# src/runtimes/__init__.py
def default_registry() -> PlatformRegistry:
    from src.platforms.claude_sdk import ClaudeSDKPlatform
    from src.platforms.your_agent import YourAgentPlatform  # add this
    return PlatformRegistry(platforms={
        ClaudeSDKPlatform.name: ClaudeSDKPlatform,
        YourAgentPlatform.name: YourAgentPlatform,
    })
```

### 3. Register an Agent

```
/add-agent name:my-agent type:your_agent
```

## TaskContext — What Your Platform Receives

| Field | Type | Description |
|-------|------|-------------|
| `description` | `str` | Full task description (markdown) |
| `task_id` | `str` | Unique task identifier |
| `acceptance_criteria` | `list[str]` | Success conditions |
| `test_commands` | `list[str]` | Verification commands |
| `checkout_path` | `str` | Absolute path to git worktree |
| `branch_name` | `str` | Git branch for this task |
| `attached_context` | `list[str]` | Additional context |
| `mcp_servers` | `dict` | MCP server configurations |

## AgentOutput — What Your Platform Returns

| Field | Type | Description |
|-------|------|-------------|
| `result` | `AgentResult` | COMPLETED, FAILED, PAUSED_TOKENS, PAUSED_RATE_LIMIT, or WAITING_INPUT |
| `summary` | `str` | Human-readable summary |
| `files_changed` | `list[str]` | Modified file paths |
| `tokens_used` | `int` | Token count for budget tracking |
| `error_message` | `str \| None` | Error details on failure |
| `question` | `str \| None` | Question when WAITING_INPUT |

## AgentResult Effects

| Result | Orchestrator Action |
|--------|-------------------|
| `COMPLETED` | Task → COMPLETED (or AWAITING_APPROVAL / AWAITING_PLAN_APPROVAL / BLOCKED depending on outcome) |
| `FAILED` | Task → FAILED, increment retry_count |
| `PAUSED_TOKENS` | Task → PAUSED with resume_after |
| `PAUSED_RATE_LIMIT` | Task → PAUSED with backoff |
| `WAITING_INPUT` | Task → WAITING_INPUT, notify Discord |

## MessageCallback

Stream real-time output to Discord via the `on_message` callback.
Keep messages under 2000 chars, batch rapid updates.

## Reference

See `src/runtimes/claude_sdk.py` (~600 lines) for a complete implementation
including environment scrubbing, resilient streaming, rate limit detection,
and graceful shutdown.
