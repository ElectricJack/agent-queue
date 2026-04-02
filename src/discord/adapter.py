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
        """Block until the Discord connection is established."""
        await self._bot.wait_until_ready()

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
        """Send a notification to the appropriate Discord channel."""
        await self._bot._send_message(text, project_id, embed=embed, view=view)

    async def create_task_thread(
        self,
        thread_name: str,
        initial_message: str,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> tuple["ThreadSendCallback", "ThreadSendCallback"] | None:
        """Create a Discord thread for task output streaming.

        Returns ``(send_to_thread, notify_main_channel)`` callback pair,
        or None if thread creation failed.
        """
        result = await self._bot._create_task_thread(
            thread_name, initial_message, project_id, task_id
        )
        return result

    async def get_thread_last_message_url(self, task_id: str) -> str | None:
        """Return a Discord jump URL to the last message in a task's thread."""
        return await self._bot.get_thread_last_message_url(task_id)

    async def edit_thread_root_message(
        self,
        task_id: str,
        content: str | None = None,
        embed: Any = None,
    ) -> None:
        """Edit the thread-root message for a task."""
        await self._bot.edit_thread_root_message(task_id, content=content, embed=embed)

    # -------------------------------------------------------------------
    # Component access
    # -------------------------------------------------------------------

    def get_command_handler(self) -> Any:
        """Return the CommandHandler wired to the Discord bot."""
        return self._bot.agent.handler

    def get_supervisor(self) -> Any:
        """Return the Supervisor wired to the Discord bot."""
        return self._bot.agent

    # -------------------------------------------------------------------
    # Health / diagnostics
    # -------------------------------------------------------------------

    def is_connected(self) -> bool:
        """Return True when the Discord bot is connected to the gateway."""
        return self._bot.is_ready() and not self._bot.is_closed()

    @property
    def platform_name(self) -> str:
        return "discord"
