"""Discord messaging adapter — wraps AgentQueueBot to implement MessagingAdapter.

This is the thin adapter layer that the orchestrator and ``main.py`` interact
with.  All Discord-specific logic lives in ``AgentQueueBot``; this class simply
delegates the ``MessagingAdapter`` methods to the underlying bot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.messaging.base import MessagingAdapter

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.messaging.types import ThreadSendCallback
    from src.orchestrator import Orchestrator


class DiscordMessagingAdapter(MessagingAdapter):
    """Adapter that wraps ``AgentQueueBot`` to implement ``MessagingAdapter``.

    Usage::

        adapter = DiscordMessagingAdapter(config, orchestrator)
        await adapter.start()
        await adapter.wait_until_ready()
        # ... orchestrator runs ...
        await adapter.close()
    """

    def __init__(self, config: "AppConfig", orchestrator: "Orchestrator") -> None:
        from src.discord.bot import AgentQueueBot

        self._bot = AgentQueueBot(config, orchestrator)
        self._config = config

    @property
    def bot(self) -> Any:
        """Direct access to the underlying bot for Discord-specific needs."""
        return self._bot

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to Discord gateway and begin listening."""
        await self._bot.start(self._config.discord.bot_token)

    async def wait_until_ready(self) -> None:
        """Block until the gateway is ready and the one-way cutover finished."""
        await self._bot.wait_until_ready()
        await self._bot.wait_until_cutover_complete()

    async def close(self) -> None:
        """Disconnect from Discord gracefully."""
        await self._bot.close()

    # -------------------------------------------------------------------
    # Messaging
    # -------------------------------------------------------------------

    async def send_message(
        self,
        text: str,
        project_id: str | None = None,
        *,
        embed: Any = None,
        view: Any = None,
    ) -> None:
        """Retired legacy notification callback; durable transports own sends."""
        return None

    async def create_task_thread(
        self,
        thread_name: str,
        initial_message: str,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> tuple["ThreadSendCallback", "ThreadSendCallback"] | None:
        """Task execution threads are retired; historical threads stay intact."""
        return None

    async def get_thread_last_message_url(self, task_id: str) -> str | None:
        """Task execution threads are no longer a navigation surface."""
        return None

    async def edit_thread_root_message(
        self,
        task_id: str,
        content: str | None = None,
        embed: Any = None,
    ) -> None:
        """Retired legacy callback; old thread roots are kept read-only."""
        return None

    # -------------------------------------------------------------------
    # Component access
    # -------------------------------------------------------------------

    def get_command_handler(self) -> Any:
        """Return the daemon-wide CommandHandler exposed by the bot.

        Wired by ``main.py`` before transports start; the bot no longer
        owns a private Supervisor after the chat cutover.
        """
        return self._bot.handler

    def get_supervisor(self) -> Any:
        """No adapter-owned Supervisor after the cutover.

        The daemon-wide Supervisor is wired directly in ``main.py``; the
        adapter has nothing to override.  Returning ``None`` keeps the
        ``MessagingAdapter`` ABC satisfied.
        """
        return None

    # -------------------------------------------------------------------
    # Health / diagnostics
    # -------------------------------------------------------------------

    def is_connected(self) -> bool:
        """Return True when the Discord bot is connected to the gateway."""
        return self._bot.is_ready() and not self._bot.is_closed()

    @property
    def platform_name(self) -> str:
        return "discord"
