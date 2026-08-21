"""FastAPI dependencies for the agent-queue REST API.

Provides access to the shared Orchestrator and CommandHandler instances
via FastAPI's dependency injection system.  These are set during the
app's lifespan by the daemon startup code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from src.api.auth import SessionTokenStore
    from src.commands.handler import CommandHandler
    from src.orchestrator import Orchestrator

# Module-level state — set by the lifespan context manager in app.py.
_orchestrator: "Orchestrator | None" = None
_command_handler: "CommandHandler | None" = None
_health_provider: Callable[[], Awaitable[dict[str, Any]]] | None = None
_plan_content_provider: Callable[[str], Awaitable[str | None]] | None = None
_started_at: float | None = None
_base_url: str = ""

# aq-surface Phase S2: session-scoped API auth.  Populated by
# :func:`src.api.app.create_app` at startup; ``TokenAuthMiddleware`` reads
# both fields on every request.  A ``None`` store means "no auth wired"
# and effectively degrades to LOCAL_SCOPE for everyone.
_token_store: "SessionTokenStore | None" = None
_require_session_token: bool = False


def get_token_store() -> "SessionTokenStore":
    assert _token_store is not None, "SessionTokenStore not initialized"
    return _token_store


def get_command_handler() -> CommandHandler:
    """FastAPI dependency that returns the shared CommandHandler.

    Prefers the orchestrator's *current* handler over the snapshot taken at
    ``create_app()`` time: ``src/main.py`` lets the messaging adapter replace
    the daemon-wide handler after startup (Discord owns its own, complete
    with the ``_on_project_created`` / ``_on_project_deleted`` callbacks), and
    the HTTP API must follow that swap rather than keep serving a stale
    instance.  Falls back to the snapshot when no orchestrator is wired (e.g.
    router-level unit tests).
    """
    live = getattr(_orchestrator, "_command_handler", None) if _orchestrator is not None else None
    if live is not None:
        return live
    assert _command_handler is not None, "CommandHandler not initialized"
    return _command_handler


def get_orchestrator() -> Orchestrator:
    """FastAPI dependency that returns the shared Orchestrator."""
    assert _orchestrator is not None, "Orchestrator not initialized"
    return _orchestrator
