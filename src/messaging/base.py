"""Abstract messaging platform adapter.

``MessagingAdapter`` defines the contract that both Discord and Telegram
transports must implement.  The orchestrator and ``main.py`` interact only
through this ABC — never importing platform-specific types directly.

The adapter handles lifecycle management (start/stop/readiness) and provides
access to the ``CommandHandler`` and ``Supervisor`` instances wired into the
transport.  Notification delivery is handled separately by event bus handlers
(e.g. ``DiscordNotificationHandler``) that subscribe to ``notify.*`` events.

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
    # Messaging (optional)
    # -------------------------------------------------------------------
    # Primary notification delivery is handled by event bus handlers
    # (e.g. DiscordNotificationHandler).  These methods are retained for
    # backward compatibility and direct use by platform-specific code.

    async def send_message(
        self,
        text: str,
        project_id: str | None = None,
        *,
        embed: Any = None,
        view: Any = None,
    ) -> None:
        """Send a notification message to the appropriate channel/chat.

        Override in platform-specific adapters.  Default is a no-op.
        """

    async def create_task_thread(
        self,
        thread_name: str,
        initial_message: str,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> tuple["ThreadSendCallback", "ThreadSendCallback"] | None:
        """Create a thread/topic for task output streaming.

        Override in platform-specific adapters.  Default returns None.
        """
        return None

    async def get_thread_last_message_url(self, task_id: str) -> str | None:
        """Return a jump URL to the last message in a task's thread.

        Override in platform-specific adapters.  Default returns None.
        """
        return None

    async def edit_thread_root_message(
        self,
        task_id: str,
        content: str | None = None,
        embed: Any = None,
    ) -> None:
        """Edit the thread-root message (e.g. "Agent working: ...").

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
