"""Commands package — mixin-based CommandHandler composition.

The CommandHandler class is composed from domain-specific mixin modules.
Import it directly from this package::

    from src.commands import CommandHandler

Module-level helper functions (tree formatting, time parsing, etc.) remain
in ``handler.py`` and can be imported from there.

Load the handler only when requested so contract consumers such as the CLI
do not import the daemon or database layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.commands.handler import CommandHandler

__all__ = ["CommandHandler"]


def __getattr__(name: str):
    if name == "CommandHandler":
        from src.commands.handler import CommandHandler

        globals()[name] = CommandHandler
        return CommandHandler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
