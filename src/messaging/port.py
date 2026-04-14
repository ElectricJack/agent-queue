"""Abstract messaging transport contract.

``MessagingPort`` defines the interface that any messaging platform (Discord,
Telegram, etc.) must implement to integrate with the Agent Queue orchestrator.

``RichNotification`` and ``NotificationAction`` are platform-neutral data
containers that describe rich messages — the transport converts them into
its native format (Discord embeds, Telegram HTML, etc.).

Design notes:

- One transport per deployment.  The config selects ``messaging: discord``
  or ``messaging: telegram``; both implement this ABC.
- The orchestrator and main.py talk only to this interface — no platform
  imports leak outside ``src/discord/`` or ``src/telegram/``.
- Interactive actions (buttons, inline keyboards) are described via
  ``NotificationAction`` and rendered by each transport into its native
  widget.  Action callbacks route to ``CommandHandler.execute()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.messaging.types import ThreadSendCallback

if TYPE_CHECKING:
    from src.commands.handler import CommandHandler
    from src.supervisor import Supervisor


# ---------------------------------------------------------------------------
# Notification colors — semantic names that each transport maps to its
# own palette (Discord embed sidebar, Telegram header emoji, etc.)
# ---------------------------------------------------------------------------

NOTIFICATION_COLORS = {
    "default",
    "success",
    "error",
    "warning",
    "info",
    "critical",
}


@dataclass
class NotificationAction:
    """A button/action attached to a notification.

    Each transport renders this into its native interactive widget:
    - Discord: ``discord.ui.Button`` inside a ``discord.ui.View``
    - Telegram: ``InlineKeyboardButton`` in an ``InlineKeyboardMarkup``

    The ``action_id`` is passed to ``CommandHandler.execute()`` when the
    user clicks.  Extra ``args`` are forwarded as keyword arguments.

    Attributes
    ----------
    label:
        Human-readable button text.
    action_id:
        Maps to a CommandHandler command (e.g. ``"retry_task"``).
    style:
        Visual style hint — ``"primary"``, ``"secondary"``, or ``"danger"``.
    args:
        Extra arguments forwarded to the command handler.
    """

    label: str
    action_id: str
    style: str = "primary"  # "primary", "danger", "secondary"
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class RichNotification:
    """Platform-neutral rich notification.

    Captures all the information needed to render a rich message on any
    messaging platform.  Each transport converts this into its native
    format:

    - **Discord**: ``discord.Embed`` with color sidebar, fields, footer,
      and attached ``discord.ui.View`` for action buttons.
    - **Telegram**: HTML-formatted message with inline keyboard buttons.

    The ``fields`` list mirrors Discord embed fields: each tuple is
    ``(name, value, inline)`` where ``inline`` hints that the field
    should render side-by-side with adjacent inline fields.

    Attributes
    ----------
    title:
        Bold header text.
    description:
        Main body text (supports markdown in most transports).
    color:
        Semantic color name — one of ``NOTIFICATION_COLORS``.
    fields:
        Structured key-value pairs: ``(name, value, inline)``.
    footer:
        Small text at the bottom of the notification.
    url:
        Optional URL to link in the title.
    actions:
        Interactive buttons/actions attached to the message.
    """

    title: str
    description: str = ""
    color: str = "default"  # "success", "error", "warning", "info", "critical"
    fields: list[tuple[str, str, bool]] = field(default_factory=list)
    footer: str = ""
    url: str = ""
    actions: list[NotificationAction] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.color not in NOTIFICATION_COLORS:
            raise ValueError(
                f"Invalid notification color {self.color!r}. "
                f"Must be one of: {', '.join(sorted(NOTIFICATION_COLORS))}"
            )


class MessagingPort(ABC):
    """Abstract messaging transport contract.

    Both ``DiscordTransport`` and ``TelegramTransport`` implement this
    interface.  The orchestrator and ``main.py`` interact only through
    this ABC — never importing platform-specific types directly.

    Lifecycle::

        transport = create_transport(config)
        transport.set_command_handler(handler)
        transport.set_supervisor(supervisor)
        await transport.start()
        await transport.wait_until_ready()
        ...
        await transport.stop()
    """

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """Connect to the messaging platform and begin processing events."""

    @abstractmethod
    async def stop(self) -> None:
        """Disconnect cleanly and release resources."""

    @abstractmethod
    async def wait_until_ready(self) -> None:
        """Block until the transport is fully connected and operational."""

    # -----------------------------------------------------------------------
    # Messaging
    # -----------------------------------------------------------------------

    @abstractmethod
    async def send_message(
        self,
        text: str,
        project_id: str | None = None,
        *,
        notification: RichNotification | None = None,
    ) -> Any:
        """Send a message to a channel.

        Parameters
        ----------
        text:
            Plain-text fallback (always provided).
        project_id:
            Route to a project-specific channel/topic if set.
        notification:
            Rich notification to render in the transport's native format.
            When ``None``, the transport sends ``text`` as a plain message.

        Returns
        -------
        Any
            Platform-specific message object (for tracking / deletion).
        """

    @abstractmethod
    async def create_thread(
        self,
        thread_name: str,
        initial_message: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> tuple[ThreadSendCallback, ThreadSendCallback] | None:
        """Create a thread/topic for streaming agent output.

        Returns a ``(send_to_thread, notify_main_channel)`` pair, or
        ``None`` if thread creation is not supported or failed.

        Parameters
        ----------
        thread_name:
            Display name for the thread/topic.
        initial_message:
            Optional first message posted to the thread.
        project_id:
            Project context for routing.
        task_id:
            Task context for routing and tracking.
        """

    # -----------------------------------------------------------------------
    # Wiring — connect the transport to orchestrator components
    # -----------------------------------------------------------------------

    @abstractmethod
    def set_command_handler(self, handler: "CommandHandler") -> None:
        """Provide the unified command handler for interactive actions."""

    @abstractmethod
    def set_supervisor(self, supervisor: "Supervisor") -> None:
        """Provide the supervisor for conversational message handling."""

    # -----------------------------------------------------------------------
    # Notify / thread callbacks for orchestrator wiring
    # -----------------------------------------------------------------------

    @abstractmethod
    def get_notify_callback(self) -> Any:
        """Return a ``NotifyCallback`` for the orchestrator to call.

        The returned callable must accept ``(text, project_id, **kwargs)``
        where kwargs may include ``notification=RichNotification`` or
        legacy ``embed=``/``view=`` during migration.
        """

    @abstractmethod
    def get_create_thread_callback(self) -> Any:
        """Return a ``CreateThreadCallback`` for the orchestrator to call.

        The returned callable must accept
        ``(thread_name, initial_message, project_id, task_id)``
        and return ``(send_to_thread, notify_main)`` or ``None``.
        """
