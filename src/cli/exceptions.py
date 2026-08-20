"""CLI-specific exception types.

Each type maps to one of the ``aq-surface`` §4.1 exit codes:
:class:`CommandError` → 1, :class:`DaemonNotRunningError` → 3,
:class:`ScopeDeniedError` → 4.  ``src/cli/app.py::_handle_errors`` owns
that mapping.
"""

from __future__ import annotations

from typing import Any


class DaemonNotRunningError(Exception):
    """Raised when the CLI cannot connect to the agent-queue daemon."""

    #: aq-surface §4.1 error code and exit status.
    code = "daemon_unreachable"
    exit_code = 3

    def __init__(self, url: str, cause: Exception | None = None):
        self.url = url
        self.cause = cause
        super().__init__(
            f"Cannot connect to agent-queue daemon at {url}. "
            "Is it running? Start with 'agent-queue' or check your config."
        )


class CommandError(Exception):
    """Raised when CommandHandler returns an error response.

    ``details`` carries everything the command returned *besides* the
    ``error`` string — most importantly ``create_task_graph``'s structured
    ``errors``/``warnings`` finding lists, which the CLI renders so an author
    sees which rules failed rather than just a count.
    """

    code = "command_error"
    exit_code = 1

    def __init__(self, command: str, message: str, details: dict[str, Any] | None = None):
        self.command = command
        self.detail_message = message
        self.details = details or {}
        super().__init__(f"Command '{command}' failed: {message}")


class ScopeDeniedError(CommandError):
    """Raised when the daemon refuses a command on auth/scope grounds (HTTP 401/403)."""

    code = "out_of_scope"
    exit_code = 4
