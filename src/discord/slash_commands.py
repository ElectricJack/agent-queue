"""Retirement list for the six former Discord slash commands.

The dashboard owns these read/control surfaces. The bot keeps this narrow list
only so startup can remove stale local registrations before syncing. Plugin
commands and commands owned by other Discord applications are not enumerated
or cleared.
"""

from __future__ import annotations

from typing import Any

RETIRED_SLASH_COMMANDS = frozenset({"status", "tasks", "explain", "peek", "gates", "attach"})


def unregister_retired_commands(tree: Any) -> tuple[str, ...]:
    """Remove only Agent Queue's six retired names from a command tree."""
    removed: list[str] = []
    for name in sorted(RETIRED_SLASH_COMMANDS):
        if tree.remove_command(name) is not None:
            removed.append(name)
    return tuple(removed)


__all__ = ["RETIRED_SLASH_COMMANDS", "unregister_retired_commands"]
