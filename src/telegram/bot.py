"""Telegram bot — core message handling and chat routing for AgentQueue.

Mirrors ``src/discord/bot.py`` but uses the ``python-telegram-bot`` library
(async-native).  Key differences from Discord:

- **Forum topics** replace Discord threads for per-task output streaming.
  When ``use_topics`` is enabled and the chat is a supergroup with topics,
  each task gets its own topic.  Otherwise, reply chains are used.
- **Inline keyboards** replace Discord button views for interactive actions.
- **MarkdownV2** replaces Discord embeds for rich formatting.
- **Chat IDs** (integers) replace Discord channel objects for routing.

Message flow::

    Telegram update -> _handle_message routing -> _build_message_history
    -> Supervisor.chat() -> tool-use loop -> send_message -> Telegram reply
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from src.telegram.commands import register_commands
from src.telegram.notifications import (
    escape_markdown,
    format_embed_as_text,
    split_message,
)
from src.telegram.views import (
    disable_keyboard_after_action,
    notification_actions_keyboard,
    parse_callback_data,
)

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

# Buffer settings (mirroring Discord bot)
MAX_HISTORY_MESSAGES = 50
BUFFER_IDLE_TIMEOUT = 3600  # Drop idle chat buffers after 1 hour


@dataclass(slots=True)
class CachedMessage:
    """Lightweight representation of a Telegram message for local buffering."""

    message_id: int
    author_name: str
    is_bot: bool
    content: str
    created_at: float  # UTC timestamp
    chat_id: int = 0


class TelegramBot:
    """Telegram bot that bridges user interaction to the AgentQueue orchestrator.

    Responsibilities:
    - Connects to Telegram Bot API via long polling (``Application.run_polling``)
    - Routes incoming messages to ``Supervisor.chat()``
    - Maintains per-chat message buffers and conversation history
    - Authorizes users by Telegram user ID
    - Per-project chat routing (similar to Discord's per-project channels)
    - Creates forum topics for task output streaming (or uses reply chains)
    """

    def __init__(self, config: "AppConfig", orchestrator: "Orchestrator") -> None:
        self.config = config
        self.orchestrator = orchestrator

        # Lazy imports — python-telegram-bot may not be installed
        from telegram.ext import Application

        self._application: Application = (
            Application.builder().token(config.telegram.bot_token).build()
        )

        # Supervisor and CommandHandler (created lazily after start)
        from src.supervisor import Supervisor

        self._supervisor = Supervisor(orchestrator, config, llm_logger=orchestrator.llm_logger)
        self.handler = self._supervisor.handler
        self.supervisor = self._supervisor

        # Per-project chat routing: project_id -> chat_id
        self._project_chats: dict[str, int] = {}
        # Reverse lookup: chat_id -> project_id
        self._chat_to_project: dict[int, str] = {}

        # Main chat ID from config
        self._main_chat_id: int = int(config.telegram.chat_id) if config.telegram.chat_id else 0

        # Per-chat message buffers (mirrors Discord's _channel_buffers)
        self._chat_buffers: dict[int, collections.deque[CachedMessage]] = {}
        self._buffer_last_access: dict[int, float] = {}

        # Per-chat locks to serialize concurrent LLM calls
        self._chat_locks: dict[int, asyncio.Lock] = {}

        # Topic tracking for task threads: topic_message_id -> task_id
        self._task_topics: dict[int, str] = {}
        # task_id -> topic_message_id (for reuse)
        self._task_topic_ids: dict[str, int] = {}

        # Ready event — set once the bot is connected
        self._ready_event = asyncio.Event()

        # Polling task handle
        self._polling_task: asyncio.Task | None = None

        # Register command handlers
        register_commands(self._application, self.handler)

        # Register message handler for natural language routing
        self._register_message_handler()

    def _register_message_handler(self) -> None:
        """Register the catch-all message handler for non-command messages."""
        from telegram.ext import MessageHandler, filters

        self._application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_message,
            )
        )

        # Register callback query handler for inline keyboard buttons
        from telegram.ext import CallbackQueryHandler

        self._application.add_handler(CallbackQueryHandler(self._handle_callback_query))

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    async def start(self) -> None:
        """Start the bot — initialize the Application and begin polling."""
        await self._application.initialize()
        await self._application.start()

        # Start polling in a background task (non-blocking)
        updater = self._application.updater
        if updater:
            self._polling_task = asyncio.create_task(
                updater.start_polling(drop_pending_updates=True)
            )

        # Resolve per-project chats from config
        self._resolve_project_chats()

        # Initialize the Supervisor's LLM client
        try:
            if self._supervisor.initialize():
                logger.info("Telegram bot: Supervisor ready (model: %s)", self._supervisor.model)
            else:
                logger.warning("Telegram bot: No LLM credentials found")
        except Exception as e:
            logger.warning("Telegram bot: Could not initialize LLM client: %s", e)

        # Wire orchestrator callbacks
        self.orchestrator.set_notify_callback(self.send_notification)
        self.orchestrator.set_create_thread_callback(self.create_task_topic)
        self.orchestrator.set_command_handler(self.handler)
        self.orchestrator.set_supervisor(self._supervisor)

        # Wire HookEngine supervisor
        if hasattr(self.orchestrator, "hooks") and self.orchestrator.hooks:
            self.orchestrator.hooks.set_supervisor(self._supervisor)

        self._ready_event.set()
        logger.info("Telegram bot started (chat_id: %s)", self._main_chat_id)

    async def wait_until_ready(self) -> None:
        """Block until the bot is connected and ready."""
        await self._ready_event.wait()

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        logger.info("Telegram bot stopping...")
        updater = self._application.updater
        if updater and updater.running:
            await updater.stop()
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        await self._application.stop()
        await self._application.shutdown()
        logger.info("Telegram bot stopped")

    # -------------------------------------------------------------------
    # Chat routing
    # -------------------------------------------------------------------

    def _resolve_project_chats(self) -> None:
        """Populate per-project chat routing from config."""
        for project_id, chat_id_str in self.config.telegram.per_project_chats.items():
            chat_id = int(chat_id_str)
            self._project_chats[project_id] = chat_id
            self._chat_to_project[chat_id] = project_id

    def update_project_chat(self, project_id: str, chat_id: int) -> None:
        """Update the chat routing for a project at runtime."""
        old_chat = self._project_chats.get(project_id)
        if old_chat is not None:
            self._chat_to_project.pop(old_chat, None)
        self._project_chats[project_id] = chat_id
        self._chat_to_project[chat_id] = project_id

    def clear_project_chats(self, project_id: str) -> None:
        """Remove chat routing for a deleted project."""
        chat_id = self._project_chats.pop(project_id, None)
        if chat_id is not None:
            self._chat_to_project.pop(chat_id, None)

    def _get_chat_id(self, project_id: str | None = None) -> int:
        """Resolve the target chat ID for a project (or the main chat)."""
        if project_id and project_id in self._project_chats:
            return self._project_chats[project_id]
        return self._main_chat_id

    # -------------------------------------------------------------------
    # Authorization
    # -------------------------------------------------------------------

    def _is_authorized(self, user_id: int | str) -> bool:
        """Check if a Telegram user is authorized to interact with the bot."""
        authorized = self.config.telegram.authorized_users
        if not authorized:
            return True  # No restriction if list is empty
        return str(user_id) in authorized

    # -------------------------------------------------------------------
    # Message handling
    # -------------------------------------------------------------------

    async def _handle_message(self, update, context) -> None:
        """Route an incoming non-command message to the Supervisor.

        This is the Telegram equivalent of Discord's ``on_message`` handler.
        Natural language messages are passed to ``Supervisor.chat()`` which
        invokes tools and returns a text response.
        """
        message = update.effective_message
        if not message or not message.text:
            return

        user = update.effective_user
        if not user:
            return

        # Authorization check
        if not self._is_authorized(user.id):
            return

        chat_id = message.chat_id

        # Determine project context from chat routing
        project_id = self._chat_to_project.get(chat_id)

        # Buffer the incoming message
        self._append_to_buffer(
            chat_id,
            CachedMessage(
                message_id=message.message_id,
                author_name=user.first_name or str(user.id),
                is_bot=False,
                content=message.text,
                created_at=time.time(),
                chat_id=chat_id,
            ),
        )

        # Serialize per-chat to avoid concurrent LLM calls
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            await self._process_chat_message(
                chat_id=chat_id,
                project_id=project_id,
                user_name=user.first_name or str(user.id),
            )

    async def _process_chat_message(
        self,
        chat_id: int,
        project_id: str | None,
        user_name: str,
    ) -> None:
        """Build message history and call Supervisor.chat().

        Mirrors Discord bot's message handling: builds a history from the
        local buffer, passes it to the Supervisor, and sends the response.
        """
        if not self._supervisor._provider:
            await self._send_text(
                chat_id,
                escape_markdown("No LLM provider configured. Set ANTHROPIC_API_KEY."),
            )
            return

        # Build message history from buffer
        history = self._build_message_history(chat_id)
        if not history:
            return

        # Set active project context on the handler
        if project_id:
            self.handler._active_project_id = project_id

        structlog.contextvars.bind_contextvars(
            platform="telegram",
            telegram_user=user_name,
            chat_id=str(chat_id),
        )
        try:
            response = await self._supervisor.chat(history)
            if response:
                await self._send_long_text(chat_id, response)
                # Buffer the bot's response
                self._append_to_buffer(
                    chat_id,
                    CachedMessage(
                        message_id=0,
                        author_name="AgentQueue",
                        is_bot=True,
                        content=response,
                        created_at=time.time(),
                        chat_id=chat_id,
                    ),
                )
        except Exception as e:
            logger.error("Supervisor error: %s", e, exc_info=True)
            await self._send_text(
                chat_id,
                escape_markdown(f"Error processing message: {e}"),
            )

    def _build_message_history(self, chat_id: int) -> list[dict[str, str]]:
        """Convert the local message buffer to a Supervisor-compatible history.

        Returns a list of ``{"role": "user"|"assistant", "content": str}``
        dicts, matching the format Supervisor.chat() expects.
        """
        buf = self._chat_buffers.get(chat_id)
        if not buf:
            return []

        self._buffer_last_access[chat_id] = time.time()
        history: list[dict[str, str]] = []
        for msg in buf:
            role = "assistant" if msg.is_bot else "user"
            content = msg.content
            if not msg.is_bot:
                content = f"[{msg.author_name}]: {content}"
            history.append({"role": role, "content": content})
        return history

    def _append_to_buffer(self, chat_id: int, msg: CachedMessage) -> None:
        """Add a message to the per-chat buffer."""
        buf = self._chat_buffers.setdefault(chat_id, collections.deque(maxlen=MAX_HISTORY_MESSAGES))
        buf.append(msg)
        self._buffer_last_access[chat_id] = time.time()

    # -------------------------------------------------------------------
    # Sending messages
    # -------------------------------------------------------------------

    async def _send_text(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "MarkdownV2",
        reply_to_message_id: int | None = None,
        reply_markup: Any = None,
    ) -> Any:
        """Send a text message to a Telegram chat.

        Returns the sent ``telegram.Message`` object.
        """
        bot = self._application.bot
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_to_message_id:
            kwargs["reply_to_message_id"] = reply_to_message_id
        if reply_markup:
            kwargs["reply_markup"] = reply_markup
        try:
            return await bot.send_message(**kwargs)
        except Exception as e:
            # Fallback: try without parse_mode in case of formatting errors
            logger.warning("MarkdownV2 send failed (%s), retrying as plain text", e)
            kwargs.pop("parse_mode", None)
            kwargs["text"] = text  # send as-is
            return await bot.send_message(**kwargs)

    async def _send_long_text(self, chat_id: int, text: str) -> None:
        """Send a potentially long message, splitting if necessary."""
        # For plain text responses from the Supervisor, send without parse_mode
        # to avoid MarkdownV2 escaping issues with LLM output
        chunks = split_message(text)
        for chunk in chunks:
            await self._application.bot.send_message(chat_id=chat_id, text=chunk)

    # -------------------------------------------------------------------
    # Notification sending (orchestrator callback)
    # -------------------------------------------------------------------

    async def send_notification(
        self,
        text: str,
        project_id: str | None = None,
        *,
        embed: Any = None,
        view: Any = None,
        notification: Any = None,
    ) -> Any:
        """Send a notification message to the appropriate chat.

        This is the callback registered with the orchestrator via
        ``set_notify_callback``.  It handles:
        - Plain text messages
        - Discord-style embeds (converted to MarkdownV2 text)
        - RichNotification objects (from the messaging abstraction)
        - Interactive views (converted to inline keyboards)
        """
        chat_id = self._get_chat_id(project_id)
        if not chat_id:
            logger.warning("No chat ID configured for notification (project=%s)", project_id)
            return None

        reply_markup = None

        # Convert embed to text if present
        if embed is not None:
            text = self._convert_embed_to_text(embed)

        # Convert RichNotification if present
        if notification is not None:
            text = self._convert_notification_to_text(notification)
            reply_markup = self._convert_notification_actions(notification)

        # Convert Discord-style view to inline keyboard
        if view is not None and reply_markup is None:
            reply_markup = self._convert_view_to_keyboard(view)

        return await self._send_text(
            chat_id,
            text or escape_markdown("(empty notification)"),
            reply_markup=reply_markup,
        )

    def _convert_embed_to_text(self, embed: Any) -> str:
        """Convert a Discord Embed (or dict representation) to MarkdownV2 text."""
        if hasattr(embed, "title"):
            # discord.Embed object
            title = str(embed.title or "")
            description = str(embed.description or "")
            fields = []
            if hasattr(embed, "fields"):
                for f in embed.fields:
                    fields.append((str(f.name), str(f.value)))
            footer = ""
            if hasattr(embed, "footer") and embed.footer:
                footer = str(getattr(embed.footer, "text", ""))
            url = str(embed.url or "") if hasattr(embed, "url") else ""
            return format_embed_as_text(title, description, fields, footer, url)
        # Fallback for dict-like embeds
        if isinstance(embed, dict):
            return format_embed_as_text(
                embed.get("title", ""),
                embed.get("description", ""),
                [(f["name"], f["value"]) for f in embed.get("fields", [])],
                embed.get("footer", {}).get("text", ""),
                embed.get("url", ""),
            )
        return escape_markdown(str(embed))

    def _convert_notification_to_text(self, notification: Any) -> str:
        """Convert a RichNotification to MarkdownV2 text."""
        title = getattr(notification, "title", "")
        description = getattr(notification, "description", "")
        fields = getattr(notification, "fields", None) or []
        footer = getattr(notification, "footer", "")
        url = getattr(notification, "url", "")
        return format_embed_as_text(title, description, [(f[0], f[1]) for f in fields], footer, url)

    def _convert_notification_actions(self, notification: Any) -> Any:
        """Convert RichNotification actions to an InlineKeyboardMarkup."""
        actions = getattr(notification, "actions", None)
        if not actions:
            return None
        return notification_actions_keyboard(actions)

    def _convert_view_to_keyboard(self, view: Any) -> Any:
        """Best-effort conversion of a Discord View to an inline keyboard.

        Discord Views contain ``Button`` children — we extract their labels
        and custom_ids to build inline keyboard buttons.
        """
        if not hasattr(view, "children"):
            return None

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        buttons = []
        for child in view.children:
            if hasattr(child, "label") and hasattr(child, "custom_id"):
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=child.label or "Action",
                            callback_data=child.custom_id or "unknown",
                        )
                    ]
                )
        return InlineKeyboardMarkup(buttons) if buttons else None

    # -------------------------------------------------------------------
    # Callback query handling (inline keyboard button presses)
    # -------------------------------------------------------------------

    async def _handle_callback_query(self, update, context) -> None:
        """Handle inline keyboard button presses.

        Button callback_data uses the format ``"action:key=val,key2=val2"``
        produced by ``src.telegram.views._make_callback_data``.  The action
        is routed to ``CommandHandler.execute()`` and the result is shown
        by editing the original message (removing the keyboard).

        Special pseudo-actions:
        - ``agent_reply_prompt`` — tells the user to reply to the question
          message; no command is executed.
        """
        query = update.callback_query
        if not query:
            return

        user = query.from_user
        if not self._is_authorized(user.id):
            await query.answer("Unauthorized.", show_alert=True)
            return

        callback_data = query.data
        if not callback_data:
            await query.answer()
            return

        cmd_name, args = parse_callback_data(callback_data)

        # --- Special pseudo-actions that don't map to CommandHandler ---

        if cmd_name == "agent_reply_prompt":
            # Prompt the user to reply to the message with their answer
            await query.answer(
                "Reply to this message with your answer for the agent.",
                show_alert=True,
            )
            return

        # --- Standard command routing ---

        await query.answer()  # Acknowledge the button press

        try:
            result = await self.handler.execute(cmd_name, args)
            if "error" in result:
                result_text = f"\u274c Error: {result['error']}"
            else:
                # Build a human-friendly summary from the action name
                friendly = cmd_name.replace("_", " ").title()
                result_text = f"\u2705 {friendly}"
            await disable_keyboard_after_action(query, result_text)
        except Exception as e:
            logger.error("Callback query error: %s", e, exc_info=True)
            await disable_keyboard_after_action(query, f"\u274c Error: {e}")

    # -------------------------------------------------------------------
    # Task topics (thread equivalent)
    # -------------------------------------------------------------------

    async def create_task_topic(
        self,
        thread_name: str,
        initial_message: str,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> tuple | None:
        """Create a forum topic or reply chain for task output streaming.

        This is the Telegram equivalent of Discord's ``_create_task_thread``.
        Returns ``(send_to_thread, notify_main_channel)`` callback pair,
        or ``None`` if the chat isn't configured.

        When ``use_topics`` is enabled and the chat is a supergroup with
        forum topics, creates a dedicated topic.  Otherwise, sends a root
        message and uses reply chains for grouping.
        """
        chat_id = self._get_chat_id(project_id)
        if not chat_id:
            logger.warning("Cannot create topic: no chat configured")
            return None

        bot = self._application.bot

        # Check if we can reuse an existing topic for this task
        if task_id and task_id in self._task_topic_ids:
            existing_topic_id = self._task_topic_ids[task_id]
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"Task resumed — agent is working on your feedback.\n{initial_message}",
                    message_thread_id=existing_topic_id,
                )
                logger.info("Reusing topic %s for task %s", existing_topic_id, task_id)
                return self._make_topic_callbacks(chat_id, existing_topic_id)
            except Exception as e:
                logger.warning("Could not reuse topic for task %s: %s", task_id, e)

        # Try to create a forum topic if use_topics is enabled
        if self.config.telegram.use_topics:
            try:
                topic = await bot.create_forum_topic(
                    chat_id=chat_id,
                    name=thread_name[:128],  # Telegram topic name limit
                )
                topic_id = topic.message_thread_id
                logger.info("Created forum topic %s: %s", topic_id, thread_name)

                # Track the topic
                if task_id:
                    self._task_topics[topic_id] = task_id
                    self._task_topic_ids[task_id] = topic_id

                return self._make_topic_callbacks(chat_id, topic_id)
            except Exception as e:
                logger.warning(
                    "Forum topic creation failed (chat may not support topics): %s. "
                    "Falling back to reply chains.",
                    e,
                )

        # Fallback: reply chain mode
        # Send a root message, then use replies for streaming output
        root_msg = await bot.send_message(
            chat_id=chat_id,
            text=f"*Agent working:* {escape_markdown(thread_name)}",
            parse_mode="MarkdownV2",
        )

        if task_id:
            self._task_topics[root_msg.message_id] = task_id
            self._task_topic_ids[task_id] = root_msg.message_id

        return self._make_reply_callbacks(chat_id, root_msg.message_id)

    def _make_topic_callbacks(self, chat_id: int, topic_id: int) -> tuple:
        """Create send callbacks for a forum topic.

        Returns ``(send_to_thread, notify_main_channel)``.
        """
        bot = self._application.bot

        async def send_to_thread(text: str) -> None:
            try:
                for chunk in split_message(text):
                    await bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        message_thread_id=topic_id,
                    )
            except Exception as e:
                logger.error("Topic send error: %s", e)

        async def notify_main_channel(text: str, **kwargs) -> None:
            """Send a notification to the main chat (outside the topic)."""
            try:
                embed = kwargs.get("embed")
                if embed is not None:
                    text = self._convert_embed_to_text(embed)
                for chunk in split_message(text):
                    await bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                    )
            except Exception as e:
                logger.error("Main channel notify error: %s", e)

        return send_to_thread, notify_main_channel

    def _make_reply_callbacks(self, chat_id: int, root_message_id: int) -> tuple:
        """Create send callbacks for a reply chain (non-topic fallback).

        Returns ``(send_to_thread, notify_main_channel)``.
        """
        bot = self._application.bot

        async def send_to_thread(text: str) -> None:
            try:
                for chunk in split_message(text):
                    await bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        reply_to_message_id=root_message_id,
                    )
            except Exception as e:
                logger.error("Reply send error: %s", e)

        async def notify_main_channel(text: str, **kwargs) -> None:
            """Send a notification as a reply to the root message."""
            try:
                embed = kwargs.get("embed")
                if embed is not None:
                    text = self._convert_embed_to_text(embed)
                for chunk in split_message(text):
                    await bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        reply_to_message_id=root_message_id,
                    )
            except Exception as e:
                logger.error("Main channel notify error: %s", e)
                # Fallback: send without reply
                try:
                    await bot.send_message(chat_id=chat_id, text=text)
                except Exception as e2:
                    logger.error("Fallback notify error: %s", e2)

        return send_to_thread, notify_main_channel
