"""Abstract messaging platform adapter.

``MessagingAdapter`` defines the contract that both Discord and Telegram
transports must implement.  The orchestrator and ``main.py`` interact only
through this ABC — never importing platform-specific types directly.

This differs from ``MessagingPort`` (the lower-level transport contract) in
that it bundles higher-level orchestrator-facing concerns: task thread
creation that returns callback pairs, and access to the ``CommandHandler``
and ``Supervisor`` instances wired into the transport.

Lifecycle::

    adapter = create_messaging_adapter(config, orchestrator)
    await adapter.start()
    await adapter.wait_until_ready()
    ...
    await adapter.close()
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.messaging.types import ThreadSendCallback


class MessagingAdapter(ABC):
    """Abstract messaging platform adapter.

    Both ``DiscordMessagingAdapter`` and ``TelegramMessagingAdapter`` implement
    this interface.  The orchestrator and ``main.py`` create an adapter via the
    ``create_messaging_adapter()`` factory and interact only through this ABC.
    """

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """Connect to the platform and begin listening for messages."""

    @abstractmethod
    async def wait_until_ready(self) -> None:
        """Block until the platform connection is established and ready."""

    @abstractmethod
    async def close(self) -> None:
        """Disconnect from the platform gracefully."""

    # -------------------------------------------------------------------
    # Messaging
    # -------------------------------------------------------------------

    @abstractmethod
    async def send_message(
        self,
        text: str,
        project_id: str | None = None,
        *,
        embed: Any = None,
        view: Any = None,
    ) -> None:
        """Send a notification message to the appropriate channel/chat.

        Parameters
        ----------
        text:
            Plain-text message content.
        project_id:
            Route to a project-specific channel/chat when set.
        embed:
            Platform-specific rich embed (Discord Embed, etc.).
        view:
            Platform-specific interactive view (Discord View, etc.).
        """

    @abstractmethod
    async def create_task_thread(
        self,
        thread_name: str,
        initial_message: str,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> tuple["ThreadSendCallback", "ThreadSendCallback"] | None:
        """Create a thread/topic for task output streaming.

        Parameters
        ----------
        thread_name:
            Display name for the thread/topic.
        initial_message:
            First message to post in the thread.
        project_id:
            Route to a project-specific channel when set.
        task_id:
            The task ID — used to reuse existing threads for reopened tasks.

        Returns
        -------
        tuple[ThreadSendCallback, ThreadSendCallback] | None
            ``(send_to_thread, notify_main_channel)`` callback pair,
            or None if thread creation failed.
        """

    async def get_thread_last_message_url(self, task_id: str) -> str | None:
        """Return a jump URL to the last message in a task's thread.

        Override in platform-specific adapters.  Default returns None
        (no thread URL available).
        """
        return None

    async def edit_thread_root_message(
        self,
        task_id: str,
        content: str | None = None,
        embed: Any = None,
    ) -> None:
        """Edit the thread-root message (e.g. "Agent working: ...").

        Used to update the root message when a task completes or fails,
        changing it from "Agent working" to a completion/failure status.

        Override in platform-specific adapters.  Default is a no-op.
        """

    # -------------------------------------------------------------------
    # Component access
    # -------------------------------------------------------------------

    @abstractmethod
    def get_command_handler(self) -> Any:
        """Return the ``CommandHandler`` instance wired to this adapter."""

    @abstractmethod
    def get_supervisor(self) -> Any:
        """Return the ``Supervisor`` instance wired to this adapter."""

    # -------------------------------------------------------------------
    # Health / diagnostics
    # -------------------------------------------------------------------

    @abstractmethod
    def is_connected(self) -> bool:
        """Return ``True`` when the platform connection is alive and ready.

        Used by the health check endpoint to report messaging connectivity.
        """

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Short lowercase platform identifier (e.g. ``"discord"``, ``"telegram"``)."""
