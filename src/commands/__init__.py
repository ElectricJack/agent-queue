"""Commands package — mixin-based CommandHandler composition.

The CommandHandler class is composed from domain-specific mixin modules.
Import it directly from this package::

    from src.commands import CommandHandler

Module-level helper functions (tree formatting, time parsing, etc.) remain
in ``handler.py`` and can be imported from there.
"""

from __future__ import annotations

from src.commands.handler import CommandHandler

__all__ = ["CommandHandler"]
