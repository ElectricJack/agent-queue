"""Runtime ABC -- the contract between the orchestrator and any AI agent.

The interface is intentionally minimal: ``start``, ``wait``, ``stop``,
``is_alive``.  Two ClassVars (``name``, ``capabilities``) declare the
platform's identity in the registry and the typed feature set it
supports; phase 2 of the platforms refactor consumes these for profile
validation.

Lifecycle:
  1. ``start(task)`` receives a :class:`TaskContext` with workspace,
     description, criteria, and attached context.
  2. ``wait(on_message)`` blocks until the agent finishes; while running
     it streams progress via :data:`MessageCallback`.
  3. ``stop()`` cooperatively cancels the agent.
  4. ``is_alive()`` lets the heartbeat monitor detect dead agents.

Profile binding stays at construction (each platform's ``__init__``
takes ``profile: AgentProfile``), preserving the shape of the legacy
``AgentAdapter``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Awaitable, Callable, ClassVar

from src.models import AgentOutput, TaskContext


# Callback invoked with each human-readable message chunk as the agent works.
# The orchestrator typically wires this to a Discord thread for live output.
MessageCallback = Callable[[str], Awaitable[None]]


class Capability(StrEnum):
    """Typed declaration of features a platform supports.

    Platforms list their capabilities at the class level
    (``capabilities: ClassVar[frozenset[Capability]]``).  Phase 2 of the
    platforms refactor consumes these for sync-time profile validation
    and runtime field filtering.  In phase 1 the enum is declared and
    populated but not yet read by any consumer.
    """

    MCP = "mcp"
    PLAN_MODE = "plan_mode"
    RESUME = "resume"
    STREAMING_JSON = "streaming_json"
    HOOKS = "hooks"
    SKILLS = "skills"
    MEMORY_MD = "memory_md"
    THINKING = "thinking"
    PERMISSION_CALLBACKS = "permission_callbacks"


class Runtime(ABC):
    """Base class for AI agent platforms (e.g. ClaudeSDK, ClaudeCLI, CodexCLI)."""

    name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]]
    # Whether this platform needs a per-task workspace (repo checkout/branch).
    # Defaults to True for backward compatibility — the Claude/Codex platforms
    # inherit the default; the in-process Supervisor platform overrides to
    # False because supervisor tasks are tool-call-only and have no cwd.
    requires_workspace: ClassVar[bool] = True

    @abstractmethod
    async def start(self, task: TaskContext) -> None:
        """Launch the agent process with the given task."""

    @abstractmethod
    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        """Wait for the agent to finish and return results."""

    @abstractmethod
    async def stop(self) -> None:
        """Cooperatively stop the agent."""

    @abstractmethod
    async def is_alive(self) -> bool:
        """Check if the agent process is still running."""
