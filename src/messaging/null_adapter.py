"""No-op messaging adapter for ``messaging_platform: "none"``.

Lets the daemon boot and schedule with no chat platform attached at all —
CLI, MCP, and the HTTP API remain fully functional.  This is the target
steady state after M4 (cutover to the out-of-process ``aq-discord``); it is
also useful today for headless deployments and tests that don't want to spin
up a Discord bot.

``NullMessagingAdapter`` does not own a ``CommandHandler`` or ``Supervisor``
of its own — ``get_command_handler()`` / ``get_supervisor()`` return
``None``.  Callers (``src/main.py``) must fall back to constructing their
own when the active adapter doesn't provide them; see the ``main.py``
docstring notes on the M0 decoupling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.messaging.base import MessagingAdapter

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.orchestrator import Orchestrator


class NullMessagingAdapter(MessagingAdapter):
    """No-op ``MessagingAdapter`` — connects to nothing, sends nothing.

    Usage::

        adapter = NullMessagingAdapter(config, orchestrator)
        await adapter.start()
        await adapter.wait_until_ready()   # returns immediately
        ...
        await adapter.close()
    """

    def __init__(self, config: "AppConfig", orchestrator: "Orchestrator") -> None:
        self._config = config
        self._orchestrator = orchestrator

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    async def start(self) -> None:
        """No-op — there is nothing to connect to."""

    async def wait_until_ready(self) -> None:
        """No-op — the adapter is always "ready"."""

    async def close(self) -> None:
        """No-op — there is nothing to disconnect."""

    # -------------------------------------------------------------------
    # Component access
    # -------------------------------------------------------------------

    def get_command_handler(self) -> Any:
        """Return ``None`` — this adapter owns no ``CommandHandler``.

        Callers must fall back to a daemon-owned instance (see
        ``src/main.py``).
        """
        return None

    def get_supervisor(self) -> Any:
        """Return ``None`` — this adapter owns no ``Supervisor``.

        Callers must fall back to a daemon-owned instance (see
        ``src/main.py``).
        """
        return None

    # -------------------------------------------------------------------
    # Health / diagnostics
    # -------------------------------------------------------------------

    def is_connected(self) -> bool:
        """Always ``True`` — there is no connection to lose."""
        return True

    @property
    def platform_name(self) -> str:
        return "none"
