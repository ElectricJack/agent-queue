"""RecordingCommandHandler — wraps the real CommandHandler and records every call.

Does NOT stub anything — delegates to the real handler with real SQLite DB.
Tests verify tool selection AND command execution end-to-end.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.orchestrator import Orchestrator


@dataclass
class CommandCall:
    """Records a single execute() invocation."""

    name: str
    args: dict
    result: dict
    timestamp: float
    duration: float


class RecordingCommandHandler(CommandHandler):
    """CommandHandler subclass that records every execute() call.

    Dangerous commands (restart_daemon, shutdown, etc.) are intercepted and
    return a fake success result instead of actually executing — the real
    ``restart_daemon`` sends SIGTERM to the current process, which would
    kill the eval runner.
    """

    # Commands that must NOT be delegated to the real handler during eval.
    _BLOCKED_COMMANDS: set[str] = {
        "restart_daemon",
        "run_shell_command",
    }

    def __init__(self, orchestrator: Orchestrator, config: AppConfig):
        super().__init__(orchestrator, config)
        self._calls: list[CommandCall] = []

    async def execute(self, name: str, args: dict) -> dict:
        start = time.monotonic()
        if name in self._BLOCKED_COMMANDS:
            result = {"success": True, "blocked_in_eval": True}
        else:
            result = await super().execute(name, args)
        duration = time.monotonic() - start

        self._calls.append(
            CommandCall(
                name=name,
                args=dict(args),
                result=result,
                timestamp=time.time(),
                duration=duration,
            )
        )
        return result

    @property
    def calls(self) -> list[CommandCall]:
        return self._calls

    @property
    def tool_names_called(self) -> list[str]:
        """Return ordered list of tool names that were called."""
        return [c.name for c in self._calls]

    def calls_for(self, name: str) -> list[CommandCall]:
        """Return all calls for a specific tool name."""
        return [c for c in self._calls if c.name == name]

    def was_called(self, name: str) -> bool:
        """Check if a tool was called at least once."""
        return any(c.name == name for c in self._calls)

    def reset(self) -> None:
        """Clear all recorded calls."""
        self._calls.clear()
