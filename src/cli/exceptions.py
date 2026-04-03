"""CLI-specific exception types."""

from __future__ import annotations


class DaemonNotRunningError(Exception):
    """Raised when the CLI cannot connect to the agent-queue daemon."""

    def __init__(self, url: str, cause: Exception | None = None):
        self.url = url
        self.cause = cause
        super().__init__(
            f"Cannot connect to agent-queue daemon at {url}. "
            "Is it running? Start with 'agent-queue' or check your config."
        )


class CommandError(Exception):
    """Raised when CommandHandler returns an error response."""

    def __init__(self, command: str, message: str):
        self.command = command
        super().__init__(f"Command '{command}' failed: {message}")
