"""FastAPI dependencies for the agent-queue REST API.

Provides access to the shared Orchestrator and CommandHandler instances
via FastAPI's dependency injection system.  These are set during the
app's lifespan by the daemon startup code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from src.commands.handler import CommandHandler
    from src.orchestrator import Orchestrator

# Module-level state — set by the lifespan context manager in app.py.
_orchestrator: Orchestrator | None = None
_command_handler: CommandHandler | None = None
_health_provider: Callable[[], Awaitable[dict[str, Any]]] | None = None
_plan_content_provider: Callable[[str], Awaitable[str | None]] | None = None
_started_at: float | None = None
_base_url: str = ""


def get_command_handler() -> CommandHandler:
    """FastAPI dependency that returns the shared CommandHandler."""
    assert _command_handler is not None, "CommandHandler not initialized"
    return _command_handler


def get_orchestrator() -> Orchestrator:
    """FastAPI dependency that returns the shared Orchestrator."""
    assert _orchestrator is not None, "Orchestrator not initialized"
    return _orchestrator
