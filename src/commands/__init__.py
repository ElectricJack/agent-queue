"""Commands package — mixin-based CommandHandler composition.

The CommandHandler class is composed from domain-specific mixin modules.
Import it directly from this package::

    from src.commands import CommandHandler

Module-level helper functions (tree formatting, time parsing, etc.) remain
in ``handler.py`` and can be imported from there.

The package imports ``CommandHandler`` **lazily** (PEP 562 module
``__getattr__``): merely importing one of the contract submodules
(``src.commands.contracts.knowledge_protection``, ``…knowledge``,
``…records``) — which the tool/CLI surface does to build its closed schemas —
no longer drags in ``handler.py`` and, through it, the full SQLAlchemy/
asyncpg database layer.  The import chain that paid for it on every ``aq``
invocation (``src.cli.app`` → … → ``src.tools.definitions`` →
``src.commands.contracts.*`` → this package → ``handler`` → ``src.database``)
is broken here: the heavy module is loaded only when ``CommandHandler`` is
actually referenced.
"""

from __future__ import annotations

__all__ = ["CommandHandler"]


def __getattr__(name: str):
    if name == "CommandHandler":
        from src.commands.handler import CommandHandler

        globals()["CommandHandler"] = CommandHandler
        return CommandHandler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
