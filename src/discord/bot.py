"""Discord integration layer -- connects the orchestrator to Discord via discord.py.

AgentQueueBot extends ``commands.Bot`` with:
- LLM-powered chat (via Supervisor) that lets users interact with the orchestrator
  through natural language
- Per-project channel routing so each project's notifications land in the right place
- Thread-based task output streaming (one Discord thread per agent execution)

This is the interim M0 in-process bot (messaging rework
docs/specs/design/messaging-rework.md §4.6 / implementation plan §3.2).  It
keeps channel routing, task-thread streaming, gate/approval views (see
``notification_handler.py`` / ``notifications.py``), project-channel chat,
and a minimal set of read-only slash commands (``slash_commands.py``).
Notes threads, channel summarization, the local message-history buffer, and
the chat-observer/suggestion wiring were removed — replies are stateless
per-message (no multi-turn history is threaded through ``Supervisor.chat()``
from here).  This whole module is superseded by the out-of-process
``aq-discord`` package at M2-M4.

Key design decision: the bot maintains in-memory channel caches
(``_project_channels``, ``_channel_to_project``) for O(1) message routing.
On startup, these are populated from the database via ``_resolve_project_channels``,
and kept in sync at runtime when channels are created, reassigned, or deleted.

Message flow::

    Discord message -> on_message routing -> Supervisor.chat()
    -> tool-use loop -> _send_long_message -> Discord reply

Scope decision (Wave 4, docs/superpowers/plans/2026-08-21-wave4-discord-e2e.md):
the ``on_message`` handler routes to the supervisor session
(``message_send`` with ``to_id=f"supervisor-{project_id}"``) ONLY when the
message arrives in a per-project channel AND
``supervisor_session_routing_enabled(config)`` is True.  The global bot
channel intentionally continues to call ``Supervisor.chat()`` in-process
— it is not tied to any single project's supervisor session, and the
cross-project routing helper needs the legacy tool-call loop.  Do not
try to migrate the global channel to the message-queue path without
first designing a "system-wide" supervisor session or a router.

See specs/discord/discord.md for the full specification.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import discord
import structlog
from discord import app_commands
from discord.ext import commands

from src.runtimes.supervisor import Supervisor
from src.config import AppConfig
from src.discord.notifications import format_server_started, format_server_started_embed
from src.models import TaskStatus
from src.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


def supervisor_session_routing_enabled(config: Any) -> bool:
    """Return True when project chat should ride the message queue.

    Phase 4 cutover (supervisor-agent.md §9 row 2, §10):  when
    ``supervisor_agent.enabled`` is set and ``legacy_chat`` is false, the
    Discord chat path enqueues a ``message.send`` for ``session:supervisor-<pid>``
    instead of invoking :meth:`Supervisor.chat` in-process.  Both flags must
    agree — flipping ``enabled`` on while ``legacy_chat`` is still true keeps
    the legacy behaviour so the rollout can be staged safely.
    """
    sa = getattr(config, "supervisor_agent", None)
    if sa is None:
        return False
    return bool(getattr(sa, "enabled", False)) and not bool(getattr(sa, "legacy_chat", True))


class AgentQueueBot(commands.Bot):
    """Discord bot that bridges user interaction to the AgentQueue orchestrator.

    Responsibilities:
    - Registers slash commands and an authorization guard on startup
    - Resolves per-project Discord channels from the database for fast routing
    - Sets orchestrator callbacks for notifications and thread creation
    - Handles incoming messages: routes them through Supervisor for LLM responses,
      serializing concurrent requests per channel to avoid duplicate processing
    """

    def __init__(self, config: AppConfig, orchestrator: Orchestrator):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.orchestrator = orchestrator
        self.agent = Supervisor(orchestrator, config, llm_logger=orchestrator.llm_logger)
        # Register a callback so that project deletions (from any caller)
        # automatically purge the bot's in-memory channel caches.
        self.agent.handler._on_project_deleted = self.clear_project_channels
        # Register a callback so that project creations (from any caller —
        # supervisor, CLI, API) auto-create a dedicated Discord channel.
        self.agent.handler._on_project_created = self._on_project_created
        # HookEngine removed (playbooks spec §13 Phase 3).
        # Discord invalid-request rate guard — tracks 401/403/429 responses
        # in a 10-minute sliding window to prevent Cloudflare IP bans.
        from src.discord.rate_guard import configure_tracker

        self._rate_tracker = configure_tracker(
            warn=config.discord.rate_guard_warn,
            critical=config.discord.rate_guard_critical,
            halt=config.discord.rate_guard_halt,
        )
        self._channel: discord.TextChannel | None = None
        # Per-project channel cache: project_id -> channel
        self._project_channels: dict[str, discord.TextChannel] = {}
        # Reverse lookup: channel_id -> project_id
        self._channel_to_project: dict[int, str] = {}
        self._processed_messages: set[int] = set()
        self._channel_locks: dict[
            int, asyncio.Lock
        ] = {}  # prevent concurrent LLM calls per channel
        self._boot_time: float | None = None
        self._guild: discord.Guild | None = None
        # Task thread tracking — maps thread_id ↔ task_id so the bot can
        # detect when a user types into a task thread and route the message
        # appropriately (reopen completed tasks, acknowledge in-progress ones).
        # IDs of transient "thinking" indicator messages that should not
        # appear in conversation history.  Populated when the thinking msg
        # is sent, cleared when it is deleted or replaced by the final reply.
        self._thinking_msg_ids: set[int] = set()
        self._task_threads: dict[int, str] = {}  # thread_id -> task_id
        self._task_thread_objects: dict[str, discord.Thread] = {}  # task_id -> Thread
        self._task_root_messages: dict[str, discord.Message] = {}  # task_id -> root msg
        # RuleManager removed (playbooks spec §13 Phase 3).
        # Notes threads, chat-observer/suggestion wiring, and the local
        # message-history buffer + summarization were removed in the M0
        # messaging strip (docs/specs/design/messaging-rework.md §4.6).

    def update_project_channel(self, project_id: str, channel: discord.TextChannel) -> None:
        """Update the cached channel for a project at runtime.

        Called after ``/set-channel`` or ``/create-channel`` commands so the
        bot immediately routes to the new channel without requiring a restart.
        Also maintains the reverse ``_channel_to_project`` mapping.
        """
        # Remove stale reverse entry for old channel, if any
        old_ch = self._project_channels.get(project_id)
        if old_ch is not None and old_ch.id != channel.id:
            self._channel_to_project.pop(old_ch.id, None)
        self._project_channels[project_id] = channel
        # Always update the reverse mapping
        self._channel_to_project[channel.id] = project_id

    def clear_project_channels(self, project_id: str) -> None:
        """Remove all cached channels for a project.

        Called after project deletion to keep the in-memory cache in sync
        with the database and avoid stale routing entries.  Cleans up:
        - ``_project_channels``
        - ``_channel_locks`` for the project's channel
        """
        # Collect Discord channel IDs before removing them from the cache so we
        # can clean up secondary caches keyed by channel ID.
        stale_channel_ids: set[int] = set()
        ch = self._project_channels.pop(project_id, None)
        if ch is not None:
            stale_channel_ids.add(ch.id)
            self._channel_to_project.pop(ch.id, None)

        # Purge channel-level caches keyed by Discord channel ID.
        for cid in stale_channel_ids:
            self._channel_locks.pop(cid, None)

    async def _on_project_created(self, project_id: str, auto_create_channels: bool) -> None:
        """Auto-create a Discord channel for a newly created project.

        Registered as the ``_on_project_created`` callback on the
        CommandHandler so that channel creation happens regardless of
        whether the project was created via the wizard, supervisor, CLI,
        or API.
        """
        if not auto_create_channels:
            return

        guild = getattr(self, "_guild", None)
        if guild is None:
            logger.debug("No guild available — skipping channel creation for %s", project_id)
            return

        ppc = self.config.discord.per_project_channels
        channel_name = ppc.naming_convention.format(project_id=project_id)

        # Look up optional category
        target_category = None
        if ppc.category_name:
            target_category = discord.utils.get(guild.categories, name=ppc.category_name)

        # Make the channel private: deny @everyone, allow the bot
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
            ),
        }

        try:
            new_channel = await guild.create_text_channel(
                name=channel_name,
                category=target_category,
                topic=f"Agent Queue channel for project: {project_id}",
                overwrites=overwrites,
                reason=f"AgentQueue: channel for project {project_id}",
            )

            # Link channel to project in the database
            await self.agent.handler.execute(
                "set_project_channel",
                {"project_id": project_id, "channel_id": str(new_channel.id)},
            )

            # Update bot's in-memory channel cache
            self.update_project_channel(project_id, new_channel)
            logger.info("Created Discord channel %s for project %s", channel_name, project_id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Failed to create Discord channel for %s: %s", project_id, exc)

    def get_project_for_channel(self, channel_id: int) -> str | None:
        """Return the project_id associated with a Discord channel, or ``None``.

        Performs an O(1) lookup against the reverse mapping that covers
        both notification and control channels for every project.
        """
        return self._channel_to_project.get(channel_id)

    def _is_authorized(self, user_id: int | str) -> bool:
        """Check whether a Discord user is allowed to interact with the bot.

        If ``authorized_users`` is empty (the default), all users are
        permitted.  Otherwise only the listed user IDs may use commands
        or send messages to the bot.
        """
        allowed = self.config.discord.authorized_users
        if not allowed:
            return True  # no restriction configured
        return str(user_id) in allowed

    async def setup_hook(self) -> None:
        from src.discord.slash_commands import setup_commands

        setup_commands(self)

        # Register Discord commands from loaded plugins.
        if hasattr(self, "orchestrator") and self.orchestrator:
            registry = getattr(self.orchestrator, "plugin_registry", None)
            if registry:
                for cmd in registry.get_discord_commands():
                    try:
                        self.tree.add_command(cmd)
                    except Exception as e:
                        logger.warning("Failed to register plugin Discord command: %s", e)

        # Log how many commands were registered on the tree.
        registered = self.tree.get_commands()
        cmd_names = sorted(c.name for c in registered)
        logger.info("Registered %d slash commands on the command tree", len(registered))

        # Global authorization guard for all slash commands.
        # Runs before every interaction — unauthorized users receive an
        # ephemeral rejection and the command is silently dropped.
        original_interaction_check = self.tree.interaction_check

        async def _auth_interaction_check(interaction: discord.Interaction) -> bool:
            if not self._is_authorized(interaction.user.id):
                await interaction.response.send_message(
                    "You don't have permission to use this command.",
                    ephemeral=True,
                )
                return False
            return await original_interaction_check(interaction)

        self.tree.interaction_check = _auth_interaction_check

        # Global error handler for all slash commands.
        # If a command throws an unhandled exception after calling defer(),
        # Discord shows "thinking" forever because no followup is sent.
        # This handler catches those errors and sends an error followup.
        async def _on_tree_error(
            interaction: discord.Interaction,
            error: app_commands.AppCommandError,
        ) -> None:
            original = getattr(error, "original", error)
            logger.error(
                "Slash command error in /%s: %r",
                interaction.command.name if interaction.command else "?",
                original,
                exc_info=True,
            )
            try:
                msg = f"An unexpected error occurred: {original}"
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass  # interaction may have expired

        self.tree.on_error = _on_tree_error

        # Sync the command tree with Discord so slash commands appear.
        # Guild-scoped sync is instant; global sync can take up to an hour.
        if self.config.discord.guild_id:
            guild = discord.Object(id=int(self.config.discord.guild_id))
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
                logger.info(
                    "Synced %d slash commands to guild %s",
                    len(synced),
                    self.config.discord.guild_id,
                )
            except Exception as e:
                logger.error("Failed to sync commands to guild: %s", e)
                logger.warning("Commands will NOT appear in Discord until sync succeeds.")
                logger.error("Registered commands were: %s", cmd_names)
        else:
            # No guild_id configured — sync globally (takes up to 1 hour to propagate).
            try:
                synced = await self.tree.sync()
                logger.info(
                    "Synced %d slash commands globally (no guild_id configured)", len(synced)
                )
            except Exception as e:
                logger.error("Failed to sync commands globally: %s", e)

    async def on_ready(self) -> None:
        logger.info(
            "Discord bot connected as %s (guild: %s)", self.user, self.config.discord.guild_id
        )
        self._boot_time = discord.utils.utcnow().timestamp()

        # Make bot accessible to CommandHandler for tools like send_message
        self.orchestrator._discord_bot = self

        # Cache channels
        if self.config.discord.guild_id:
            guild = self.get_guild(int(self.config.discord.guild_id))
            self._guild = guild
            if guild:
                channel_name = self.config.discord.channels.get("channel", "agent-queue")
                for ch in guild.text_channels:
                    if ch.name == channel_name:
                        self._channel = ch

                if self._channel:
                    logger.info("Bot channel: #%s", self._channel.name)
                else:
                    logger.warning("Bot channel '%s' not found", channel_name)

                # Resolve per-project channels from database
                await self._resolve_project_channels()

                # Wire orchestrator references for command handling and
                # supervisor delegation.  Notification delivery is handled
                # by DiscordNotificationHandler (subscribes to EventBus
                # notify.* events) — no callback wiring needed here.
                if self._channel or self._project_channels:
                    if not self.orchestrator._command_handler:
                        self.orchestrator.set_command_handler(self.agent.handler)
                        self.orchestrator.set_supervisor(self.agent)

        # Initialize LLM client via Supervisor
        try:
            if self.agent.initialize():
                logger.info("Chat agent ready (model: %s)", self.agent.model)
            else:
                logger.warning(
                    "No LLM credentials found — set ANTHROPIC_API_KEY or run `claude login`"
                )
        except Exception as e:
            logger.error("Could not initialize LLM client: %s", e)

        # RuleManager removed (playbooks spec §13 Phase 3).
        # Playbook compilation is handled by the VaultWatcher.

        # Notify all transports that the server is back online via the
        # event bus.  The Discord notification handler subscribes to this
        # event and formats the message + embed.  Also send directly as a
        # fallback in case the notification handler isn't subscribed yet.
        try:
            from src.notifications.events import SystemOnlineEvent

            await self.orchestrator.bus.emit(
                "notify.system_online",
                SystemOnlineEvent().model_dump(mode="json"),
            )
        except Exception:
            # Fallback: send directly if event bus emit fails
            await self._send_message(
                format_server_started(),
                embed=format_server_started_embed(),
            )

    class ThinkingView(discord.ui.View):
        """Attached to the thinking indicator message with a Cancel button.

        When clicked, calls ``Supervisor.cancel()`` to immediately terminate
        the response loop.  The view disables itself after use.
        """

        def __init__(self, supervisor: Supervisor):
            super().__init__(timeout=300)  # 5-minute timeout
            self._supervisor = supervisor
            self._cancelled = False

        @discord.ui.button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            emoji="✖️",
        )
        async def cancel_button(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ) -> None:
            if self._cancelled:
                await interaction.response.send_message(
                    "Already cancelled.",
                    ephemeral=True,
                )
                return
            self._cancelled = True
            self._supervisor.cancel()
            button.disabled = True
            button.label = "Cancelled"
            try:
                await interaction.response.edit_message(
                    content="🚫 Cancelling...",
                    view=self,
                )
            except Exception:
                try:
                    await interaction.response.send_message(
                        "Cancelling...",
                        ephemeral=True,
                    )
                except Exception:
                    pass

    async def _delete_thinking_msg(self, msg: discord.Message | None) -> None:
        """Silently delete a thinking indicator message.

        Also removes the message from ``_thinking_msg_ids``.

        Fail-open: if the message was already deleted or any Discord error
        occurs, we swallow the exception so the main response flow is never
        interrupted.
        """
        if msg is None:
            return
        self._thinking_msg_ids.discard(msg.id)
        try:
            await msg.delete()
        except discord.NotFound:
            pass  # Already deleted externally
        except Exception:
            pass  # Fail-open on cleanup

    async def _safe_api_call(
        self,
        coro,
        *,
        critical: bool = True,
        context: str = "",
    ):
        """Execute a Discord API coroutine with rate-guard protection.

        Checks the invalid-request tracker before calling and records any
        error responses (401/403/429) after.  Non-critical calls are
        silently dropped when the tracker reaches the critical threshold;
        all calls are blocked at the halt threshold.

        Returns the coroutine's result on success, or ``None`` when the
        call is suppressed or fails with a trackable error.
        """
        tracker = self._rate_tracker
        if not tracker.should_allow(critical=critical):
            if context:
                logging.getLogger(__name__).debug(
                    "Rate guard blocked %s API call: %s (state=%s, count=%d)",
                    "critical" if critical else "non-critical",
                    context,
                    tracker.state,
                    tracker.count,
                )
            return None
        try:
            return await coro
        except discord.NotFound:
            raise  # 404 doesn't count toward invalid request limit
        except discord.Forbidden as exc:
            tracker.record(403)
            logging.getLogger(__name__).warning(
                "Discord 403 Forbidden%s: %s",
                f" ({context})" if context else "",
                exc,
            )
            return None
        except discord.HTTPException as exc:
            if exc.status in (401, 429):
                tracker.record(exc.status)
            logging.getLogger(__name__).warning(
                "Discord HTTP %d%s: %s",
                exc.status,
                f" ({context})" if context else "",
                exc,
            )
            return None

    async def _send_long_message(
        self,
        channel: discord.abc.Messageable,
        text: str,
        *,
        reply_to: discord.Message | None = None,
        filename: str = "response.md",
    ) -> None:
        """Send a message, handling Discord's 2000-char limit.

        Short messages are sent normally. Long messages are split at line
        boundaries when possible, falling back to a file attachment for
        very long content (>6000 chars).
        """
        if len(text) <= 2000:
            if reply_to:
                await self._safe_api_call(
                    reply_to.reply(text),
                    critical=False,
                    context="send_long_message reply",
                )
            else:
                await self._safe_api_call(
                    channel.send(text),
                    critical=False,
                    context="send_long_message",
                )
            return

        # Very long content → attach as file with a short preview
        if len(text) > 6000:
            # Find a reasonable preview (first paragraph or first 300 chars)
            preview_end = text.find("\n\n", 0, 500)
            if preview_end == -1:
                preview_end = min(300, len(text))
            preview = text[:preview_end].rstrip()

            file = discord.File(
                fp=__import__("io").BytesIO(text.encode("utf-8")),
                filename=filename,
            )
            msg = f"{preview}\n\n*Full response attached ({len(text):,} chars)*"
            if reply_to:
                await self._safe_api_call(
                    reply_to.reply(msg, file=file),
                    critical=False,
                    context="send_long_message file reply",
                )
            else:
                await self._safe_api_call(
                    channel.send(msg, file=file),
                    critical=False,
                    context="send_long_message file",
                )
            return

        # Medium-length content → split into multiple messages at line boundaries
        chunks: list[str] = []
        current = ""
        for line in text.split("\n"):
            candidate = current + ("\n" if current else "") + line
            if len(candidate) > 2000:
                if current:
                    chunks.append(current)
                # If a single line exceeds 2000, hard-split it
                while len(line) > 2000:
                    chunks.append(line[:2000])
                    line = line[2000:]
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks):
            if i == 0 and reply_to:
                await self._safe_api_call(
                    reply_to.reply(chunk),
                    critical=False,
                    context="send_long_message chunk",
                )
            else:
                await self._safe_api_call(
                    channel.send(chunk),
                    critical=False,
                    context="send_long_message chunk",
                )

    async def _resolve_project_channels(self) -> None:
        """Resolve per-project Discord channels from the database.

        Projects may have ``discord_channel_id`` set.  This method looks up
        the corresponding :class:`discord.TextChannel` objects and caches
        them for fast routing.

        When a stored channel ID no longer resolves to a guild channel
        (e.g. the Discord channel was deleted), the stale ID is nullified
        in the database to prevent orphaned references from accumulating.
        """
        if not self._guild:
            return

        projects = await self.orchestrator.db.list_projects()
        for project in projects:
            if project.discord_channel_id:
                ch = self._guild.get_channel(int(project.discord_channel_id))
                if ch and isinstance(ch, discord.TextChannel):
                    self._project_channels[project.id] = ch
                    self._channel_to_project[ch.id] = project.id
                    logger.info("Project '%s' channel: #%s", project.id, ch.name)
                else:
                    logger.warning(
                        "Project '%s' channel %s not found in guild — clearing stale ID",
                        project.id,
                        project.discord_channel_id,
                    )
                    await self.orchestrator.db.update_project(project.id, discord_channel_id=None)

    def _get_channel(self, project_id: str | None = None) -> discord.TextChannel | None:
        """Return the channel for a project, falling back to the global channel."""
        if project_id and project_id in self._project_channels:
            return self._project_channels[project_id]
        return self._channel

    def _is_global_channel(self, channel: discord.TextChannel, project_id: str | None) -> bool:
        """Return True if *channel* is the global fallback (not a per-project channel)."""
        return (
            project_id is not None
            and channel == self._channel
            and project_id not in self._project_channels
        )

    @staticmethod
    def _prepend_project_tag(text: str, project_id: str) -> str:
        """Prepend a ``[project-id]`` tag to a message for global-channel context."""
        return f"[`{project_id}`] {text}"

    async def _send_message(
        self,
        text: str,
        project_id: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> discord.Message | None:
        """Send a message to the project's channel (or the global channel).

        When *embed* is provided the message is sent as a rich embed; the
        plain *text* is kept as a fallback (and remains useful for logging).
        When *view* is provided, interactive buttons are attached to the embed.
        When the message is routed to the **global** channel (because the
        project has no dedicated channel), a ``[project-id]`` tag is prepended
        to the text version so users can tell which project it belongs to.

        Returns the sent ``discord.Message`` (or ``None`` if no channel was
        available) so callers can track/delete it later.
        """
        channel = self._get_channel(project_id)
        if channel:
            if project_id and self._is_global_channel(channel, project_id):
                text = self._prepend_project_tag(text, project_id)
            if embed is not None:
                kwargs = {"embed": embed}
                if view is not None:
                    kwargs["view"] = view
                return await self._safe_api_call(
                    channel.send(**kwargs),
                    critical=True,
                    context="send_message embed",
                )
            else:
                return await self._send_long_message(channel, text)
        return None

    async def _create_task_thread(
        self,
        thread_name: str,
        initial_message: str,
        project_id: str | None = None,
        task_id: str | None = None,
    ):
        """Create a Discord thread for streaming agent output.

        Returns a tuple of two async callbacks:
          ``(send_to_thread, notify_main_channel)``

        The two-callback design separates concerns:
        - ``send_to_thread`` streams verbose agent output into the thread,
          keeping it contained and out of the main channel.
        - ``notify_main_channel`` posts a brief completion/failure message as
          a reply to the thread-root message, so the notification appears in
          the main channel feed and is visually linked to the thread.

        If *task_id* is provided and an existing thread for that task is already
        tracked (e.g. from a previous run that was reopened via thread feedback),
        the existing thread is reused instead of creating a new one.

        Returns ``None`` if no notifications channel is available (the
        orchestrator checks for this before calling).
        """
        channel = self._get_channel(project_id)
        if not channel:
            logger.warning("Cannot create thread: no channel configured")
            return None

        # Reuse an existing thread if one was already created for this task
        # (e.g. reopened via thread feedback).
        existing_thread = self._task_thread_objects.get(task_id) if task_id else None
        if existing_thread:
            try:
                # Verify the thread is still accessible
                await existing_thread.send(
                    f"🔄 **Task resumed** — agent is working on your feedback.\n{initial_message}"
                )
                logger.info("Reusing existing thread %d for task %s", existing_thread.id, task_id)
                thread = existing_thread

                async def send_to_thread(text: str) -> None:
                    try:
                        await self._send_long_message(thread, text)
                    except Exception as e:
                        logger.error("Thread send error: %s", e)

                async def notify_main_channel(
                    text: str, *, embed: discord.Embed | None = None
                ) -> None:
                    """No-op for reopened tasks — avoid duplicating messages in the thread.

                    The thread already receives result info from thread_send
                    (failure details, PR info, etc.) and the completion embed
                    via the orchestrator's _post helper.  Sending the brief
                    notification here as well would duplicate the information.
                    """
                    pass

                return send_to_thread, notify_main_channel
            except Exception as e:
                logger.error("Could not reuse thread for task %s: %s, creating new one", task_id, e)
                # Fall through to create a new thread

        # When routing to the global channel, prepend a project tag so users
        # can tell which project the thread belongs to.
        is_global = self._is_global_channel(channel, project_id)
        display_name = thread_name
        if is_global and project_id:
            display_name = f"[{project_id}] {thread_name}"

        logger.info(
            "Creating thread: %s%s → #%s (%s)",
            thread_name,
            f" (project: {project_id})" if project_id else "",
            channel.name,
            "global" if is_global else "per-project",
        )
        # Create the thread-root message in the notifications channel, then open
        # a thread on it so all streaming output stays inside the thread.
        try:
            msg = await self._safe_api_call(
                channel.send(f"**Agent working:** {display_name}"),
                critical=True,
                context="create_task_thread root",
            )
            if msg is None:
                return None
            thread = await self._safe_api_call(
                msg.create_thread(name=display_name[:100]),
                critical=True,
                context="create_task_thread thread",
            )
            if thread is None:
                return None
        except Exception as e:
            logger.error("Thread creation failed: %s", e)
            return None
        logger.info("Thread created: %d", thread.id)

        # Track the thread ↔ task mapping for thread message detection
        if task_id:
            self._task_threads[thread.id] = task_id
            self._task_thread_objects[task_id] = thread
            self._task_root_messages[task_id] = msg

        async def send_to_thread(text: str) -> None:
            try:
                await self._send_long_message(thread, text)
            except Exception as e:
                logger.error("Thread send error: %s", e)

        async def notify_main_channel(text: str, *, embed: discord.Embed | None = None) -> None:
            """Reply to the thread-root message with a brief notification.

            When an embed is provided, only the embed is sent to avoid
            redundant plain-text + embed duplication.  The *text* is kept
            as a fallback when no embed is available.
            """
            try:
                if embed is not None:
                    await msg.reply(embed=embed)
                else:
                    await msg.reply(text)
            except Exception as e:
                logger.error("Main channel notify error: %s", e)
                # Fallback: plain message in the notifications channel
                try:
                    if embed is not None:
                        await channel.send(embed=embed)
                    else:
                        await channel.send(text)
                except Exception as e2:
                    logger.error("Fallback notify error: %s", e2)

        return send_to_thread, notify_main_channel

    def get_task_thread_urls(self) -> dict[str, str]:
        """Return a mapping of task_id → thread jump_url for all tracked tasks.

        Uses the thread's jump_url property which links to the thread itself
        (not a specific message).  This is fast — no API calls required.
        """
        urls: dict[str, str] = {}
        for task_id, thread in self._task_thread_objects.items():
            try:
                urls[task_id] = thread.jump_url
            except Exception:
                pass
        return urls

    async def get_thread_last_message_url(self, task_id: str) -> str | None:
        """Return a Discord jump URL to the last message in a task's thread.

        Used by the plan approval embed to link directly to the agent's final
        output (which contains the full plan summary) instead of showing a
        truncated preview.
        """
        thread = self._task_thread_objects.get(task_id)
        if not thread:
            return None
        try:
            # Fetch the most recent message in the thread
            messages = [msg async for msg in thread.history(limit=1)]
            if messages:
                return messages[0].jump_url
        except Exception as e:
            logger.error("Could not get last message URL for task %s: %s", task_id, e)
        return None

    async def edit_thread_root_message(
        self,
        task_id: str,
        content: str | None = None,
        embed: discord.Embed | None = None,
    ) -> None:
        """Edit the thread-root message for a task.

        Used to update the "Agent working: ..." message to reflect task
        completion or failure status.
        """
        root_msg = self._task_root_messages.get(task_id)
        if not root_msg:
            return
        try:
            kwargs: dict[str, Any] = {}
            if content is not None:
                kwargs["content"] = content
            if embed is not None:
                kwargs["embed"] = embed
            if kwargs:
                await root_msg.edit(**kwargs)
        except Exception as e:
            logger.error("Could not edit thread root for task %s: %s", task_id, e)

    async def _handle_task_thread_message(self, message: discord.Message, task_id: str) -> None:
        """Handle a user message posted in a task's Discord thread.

        When a user types into a task thread:
        - **Completed/failed/blocked task**: Reopen the task with the user's
          message as feedback and let the agent address it on re-execution.
          The status change happens silently (no main channel notification).
        - **In-progress task**: Acknowledge that the message was received;
          it will be stored as context for the agent.
        - **Other statuses** (READY, ASSIGNED, etc.): Acknowledge that the
          message was stored as feedback.
        """
        feedback = message.content.strip()
        if not feedback:
            return

        try:
            task = await self.orchestrator.db.get_task(task_id)
        except Exception as e:
            logger.error("Task thread message: could not fetch task %s: %s", task_id, e)
            return

        if not task:
            return

        terminal_statuses = {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.AWAITING_PLAN_APPROVAL,
        }

        if task.status in terminal_statuses:
            # Reopen the task with the user's feedback
            try:
                result = await self.agent.handler.execute(
                    "reopen_with_feedback",
                    {"task_id": task_id, "feedback": feedback},
                )
                if result.get("error"):
                    await message.reply(f"⚠️ Could not reopen task: {result['error']}")
                else:
                    await message.reply(
                        "📝 Task **reopened** with your feedback. An agent will pick it up shortly."
                    )
            except Exception as e:
                logger.error("Task thread reopen failed for %s: %s", task_id, e)
                await message.reply(f"⚠️ Error reopening task: {e}")

        elif task.status == TaskStatus.IN_PROGRESS:
            # Task is actively running — try to inject the message into the
            # live session so the agent sees it immediately.
            injected = False
            if task.assigned_agent_id:
                adapter = self.orchestrator._adapters.get(task.assigned_agent_id)
                if adapter and hasattr(adapter, "inject_message"):
                    try:
                        await adapter.inject_message(feedback)
                        injected = True
                        await message.add_reaction("👍")
                    except Exception as e:
                        logger.warning("inject_message failed for task %s: %s", task_id, e)

            if not injected:
                # Fallback: save as context for re-execution
                try:
                    separator = "\n\n---\n**Thread Feedback:**\n"
                    updated_desc = task.description + separator + feedback
                    await self.orchestrator.db.update_task(task_id, description=updated_desc)
                    await self.orchestrator.db.add_task_context(
                        task_id,
                        type="thread_feedback",
                        label="User Feedback (from thread)",
                        content=feedback,
                    )
                    await message.reply(
                        "💬 Message received. The agent is currently working — "
                        "your feedback has been saved and will be included if "
                        "the task is re-executed."
                    )
                except Exception as e:
                    logger.error("Task thread context save failed for %s: %s", task_id, e)
                    await message.reply(f"⚠️ Error saving feedback: {e}")

        else:
            # READY, ASSIGNED, DEFINED, PAUSED, WAITING_INPUT, etc.
            # Append to description so the agent sees it when the task runs.
            try:
                separator = "\n\n---\n**Thread Feedback:**\n"
                updated_desc = task.description + separator + feedback
                await self.orchestrator.db.update_task(task_id, description=updated_desc)
                await self.orchestrator.db.add_task_context(
                    task_id,
                    type="thread_feedback",
                    label="User Feedback (from thread)",
                    content=feedback,
                )
                await message.reply(
                    "💬 Feedback saved — the agent will see this when the task runs."
                )
            except Exception as e:
                logger.error("Task thread context save failed for %s: %s", task_id, e)

    # ------------------------------------------------------------------
    # Local message buffer
    # ------------------------------------------------------------------

    async def _download_attachments(self, message: discord.Message) -> list[str]:
        """Download image/file attachments from a Discord message to local disk.

        Returns a list of absolute paths to the downloaded files.  Files are
        stored under ``<data_dir>/attachments/<message_id>/`` with their
        original filenames (sanitized).  Only image content types are
        downloaded — other attachment types are skipped.

        Failures are non-fatal: a failed download is silently skipped.
        """
        IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
        paths: list[str] = []
        if not message.attachments:
            return paths

        attachments_dir = Path(self.config.data_dir) / "attachments" / str(message.id)
        attachments_dir.mkdir(parents=True, exist_ok=True)

        for att in message.attachments:
            # Only download images — skip other file types
            if (
                att.content_type
                and att.content_type.split(";")[0].strip() not in IMAGE_CONTENT_TYPES
            ):
                continue
            # Even if content_type is missing, allow common image extensions
            if not att.content_type:
                ext = os.path.splitext(att.filename)[1].lower()
                if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    continue

            # Sanitize filename: keep only alphanumeric, dots, dashes, underscores
            safe_name = "".join(c if c.isalnum() or c in ".-_" else "_" for c in att.filename)
            dest = attachments_dir / safe_name
            try:
                await att.save(dest)
                paths.append(str(dest.resolve()))
            except Exception as e:
                logger.error("Failed to download attachment %s: %s", att.filename, e)

        return paths

    async def on_message(self, message: discord.Message) -> None:
        """Route incoming Discord messages to the Supervisor for LLM processing.

        Routing logic (a message is handled if ANY of these match):
        1. Posted in the global bot channel (configured in config.yaml)
        2. Posted in a per-project channel (O(1) reverse lookup via _channel_to_project)
        3. The bot is @mentioned anywhere in the guild

        For project channels, implicit project context is injected into the
        prompt so the LLM defaults to the right project without requiring
        the user to specify it every time.

        Each message is handled statelessly — no local conversation-history
        buffer is kept (removed in the M0 messaging strip; see the module
        docstring). ``Supervisor.chat()`` is called with ``history=None``.

        Concurrency: a per-channel asyncio.Lock serializes LLM calls to
        prevent duplicate or interleaved responses when messages arrive faster
        than the LLM can respond.
        """
        # Ignore own messages
        if message.author == self.user:
            return

        # Authorization guard — silently ignore unauthorized users
        if not self._is_authorized(message.author.id):
            return

        # Dedup guard — prevent processing the same message twice
        if message.id in self._processed_messages:
            return
        self._processed_messages.add(message.id)
        # Keep the set from growing unbounded
        if len(self._processed_messages) > 200:
            self._processed_messages = set(list(self._processed_messages)[-100:])

        # Skip messages created before the bot started (prevents reprocessing after restart)
        if self._boot_time and message.created_at.timestamp() < self._boot_time:
            return

        # Check if this is a task thread — if so, handle the message as
        # feedback on the task (reopen if completed, acknowledge if in-progress).
        task_thread_id = self._task_threads.get(message.channel.id)
        if task_thread_id:
            await self._handle_task_thread_message(message, task_thread_id)
            return

        # Only respond in the global bot channel, per-project channels
        # (when mentioned), or when mentioned elsewhere.
        is_bot_channel = self._channel and message.channel.id == self._channel.id
        # Check if this is a per-project channel (O(1) reverse lookup)
        project_channel_id: str | None = self._channel_to_project.get(message.channel.id)

        is_mentioned = self.user in message.mentions

        if not is_bot_channel and project_channel_id is None and not is_mentioned:
            return

        # In project channels, require an @mention so collaborators can
        # chat freely without triggering the bot on every message.
        if project_channel_id and not is_bot_channel and not is_mentioned:
            return

        # Download any image attachments from the message.  This makes
        # local file paths available for task creation later.
        downloaded_paths: list[str] = []
        if message.attachments:
            downloaded_paths = await self._download_attachments(message)

        # Strip the bot mention from the message text
        text = message.content
        if self.user:
            text = text.replace(f"<@{self.user.id}>", "").strip()

        # Append attachment info so the LLM knows about uploaded images
        if downloaded_paths:
            attachment_lines = "\n".join(f"  - `{p}`" for p in downloaded_paths)
            text += (
                f"\n\n[User attached {len(downloaded_paths)} image(s), "
                f"downloaded to local paths:\n{attachment_lines}\n"
                f"When creating a task that needs these images, pass these "
                f"paths in the `attachments` parameter of create_task.]"
            )

        if not text:
            if downloaded_paths:
                text = (
                    f"[User sent {len(downloaded_paths)} image(s) with no text. "
                    f"Downloaded to:\n" + "\n".join(f"  - `{p}`" for p in downloaded_paths) + "\n"
                    "Ask the user what they'd like to do with these images.]"
                )
            else:
                await message.reply("How can I help? Ask me about status, projects, or tasks.")
                return

        # Serialize LLM processing per channel to avoid duplicate/concurrent responses
        lock = self._channel_locks.setdefault(message.channel.id, asyncio.Lock())
        async with lock:
            structlog.contextvars.bind_contextvars(
                platform="discord",
                discord_user=message.author.display_name,
                channel_id=str(message.channel.id),
            )
            # Notify on cold model loads (Ollama first-call latency)
            try:
                if not await self.agent.is_model_loaded():
                    await message.channel.send("\u23f3 Loading model, this may take a moment...")
            except Exception:
                pass  # fail-open — skip notification silently

            thinking_msg: discord.Message | None = None
            thinking_view: AgentQueueBot.ThinkingView | None = None
            tool_names_used: list[str] = []
            response: str = ""
            async with message.channel.typing():
                try:
                    if not self.agent.is_ready:
                        await message.reply(
                            "LLM not configured — I can only respond to slash commands. "
                            "Set `ANTHROPIC_API_KEY` or run `claude login`."
                        )
                        return

                    # Build context dict for system prompt injection (not prepended to user message)
                    user_text = text
                    llm_context: dict[str, str] = {}
                    if project_channel_id and not is_bot_channel:
                        other_projects = [
                            pid for pid in self._project_channels if pid != project_channel_id
                        ]
                        cross_project_hint = ""
                        if other_projects:
                            names = ", ".join(f"`{p}`" for p in sorted(other_projects))
                            cross_project_hint = (
                                f" Other known projects: {names}. "
                                f"If the user's request is clearly about a "
                                f"different project, set project_id explicitly."
                            )
                        llm_context["channel_context"] = (
                            f"This is the channel for project "
                            f"`{project_channel_id}`. Default to using "
                            f"project_id='{project_channel_id}' for all project-scoped "
                            f"commands.{cross_project_hint}"
                        )

                    # Include the replied-to message in the user request when
                    # Discord has pre-resolved the reference.  There is no
                    # local history buffer to fall back on (removed in the
                    # M0 messaging strip), so replies to messages outside
                    # Discord's resolution window are not quoted.
                    if (
                        message.reference
                        and message.reference.resolved
                        and isinstance(message.reference.resolved, discord.Message)
                    ):
                        resolved = message.reference.resolved
                        ref_author = (
                            "AgentQueue"
                            if resolved.author == self.user
                            else resolved.author.display_name
                        )
                        user_text = (
                            f'[Replying to {ref_author}: '
                            f'"{resolved.content}"]\n{user_text}'
                        )

                    # Set active project from channel context so that git
                    # commands (and other project-scoped tools) automatically
                    # infer the correct repository without the LLM needing to
                    # explicitly pass project_id in every tool call.
                    prev_active = self.agent._active_project_id
                    if project_channel_id and not is_bot_channel:
                        self.agent.set_active_project(project_channel_id)

                    # No local conversation-history buffer (removed in the
                    # M0 messaging strip) — each message is a stateless
                    # single-turn call into the Supervisor.
                    history = None

                    use_supervisor_session = (
                        supervisor_session_routing_enabled(self.config)
                        and project_channel_id is not None
                    )

                    if use_supervisor_session:
                        # Supervisor-session path: no ThinkingView (its Cancel
                        # button routes to the in-process Supervisor, wrong on
                        # this path).  Acknowledge the enqueue with a 📬 reaction
                        # once message_send succeeds; on error, post a visible
                        # error reply.  The real answer arrives asynchronously
                        # via ``message.sent`` → notification handler.
                        send_result = await self.agent.handler.execute(
                            "message_send",
                            {
                                "project_id": project_channel_id,
                                "to_kind": "session",
                                "to_id": f"supervisor-{project_channel_id}",
                                "from_kind": "user",
                                "from_id": f"discord:{message.author.id}",
                                "body": user_text,
                                "thread_id": f"discord:{message.channel.id}",
                            },
                        )
                        if isinstance(send_result, dict) and "error" in send_result:
                            await self._send_long_message(
                                message.channel,
                                f"**Message queue error:** {send_result['error']}",
                                reply_to=message,
                            )
                        else:
                            try:
                                await self._safe_api_call(
                                    message.add_reaction("\U0001f4ec"),
                                    critical=False,
                                    context="on_message ack reaction",
                                )
                            except Exception:
                                pass  # fail-open
                        response = ""  # nothing more to render on this path
                        # Skip legacy branch entirely.
                        return

                    # Legacy Supervisor.chat() path — keep ThinkingView + progress UI.
                    # Send a thinking indicator that updates as the agent works.
                    # This gives users real-time feedback on what the agent is
                    # doing: thinking, calling tools, or composing a reply.
                    # Includes a Cancel button to abort the supervisor mid-thought.
                    thinking_view = self.ThinkingView(self.agent)
                    thinking_msg = await message.reply(
                        "💭 Thinking...",
                        view=thinking_view,
                    )
                    self._thinking_msg_ids.add(thinking_msg.id)

                    async def _on_progress(event: str, detail: str | None) -> None:
                        """Update the thinking indicator as the agent progresses.

                        Events:
                        - "thinking" (detail=None): initial LLM call
                        - "thinking" (detail="round N"): subsequent LLM round
                        - "tool_use" (detail=tool_name): a tool is being called
                        - "responding": LLM is producing final text response
                        - "cancelled": supervisor was cancelled via button
                        """
                        nonlocal thinking_msg
                        if thinking_msg is None:
                            return
                        try:
                            if event == "cancelled":
                                thinking_view.stop()
                                await thinking_msg.edit(
                                    content="🚫 Cancelled.",
                                    view=None,
                                )
                                return
                            elif event == "thinking" and not detail:
                                # Initial thinking — already showing "Thinking..."
                                pass
                            elif event == "thinking" and detail:
                                # Subsequent LLM round after tool use
                                steps = " → ".join(f"`{t}`" for t in tool_names_used)
                                await thinking_msg.edit(content=f"💭 Thinking... {steps} → 💭")
                            elif event == "tool_use" and detail:
                                tool_names_used.append(detail)
                                steps = " → ".join(f"`{t}`" for t in tool_names_used)
                                await thinking_msg.edit(content=f"🔧 Working... {steps}")
                            elif event == "responding":
                                steps = " → ".join(f"`{t}`" for t in tool_names_used)
                                if steps:
                                    await thinking_msg.edit(
                                        content=f"✅ {steps} → composing reply..."
                                    )
                                else:
                                    await thinking_msg.edit(content="✍️ Composing reply...")
                        except discord.NotFound:
                            thinking_msg = None  # Deleted externally; stop updating
                        except Exception:
                            pass  # Fail-open — don't break the chat flow

                    try:
                        response = await self.agent.chat(
                            user_text,
                            message.author.display_name,
                            history=history,
                            on_progress=_on_progress,
                            context=llm_context or None,
                        )
                    except Exception as e:
                        _is_auth_error = False
                        try:
                            import anthropic

                            _is_auth_error = isinstance(e, anthropic.AuthenticationError)
                        except ModuleNotFoundError:
                            pass

                        if _is_auth_error:
                            # Token may have been refreshed — reload and retry once
                            logger.warning("Auth error, reloading credentials: %s", e)
                            if self.agent.reload_credentials():
                                response = await self.agent.chat(
                                    user_text,
                                    message.author.display_name,
                                    history=history,
                                    on_progress=_on_progress,
                                    context=llm_context or None,
                                )
                            else:
                                response = (
                                    "Authentication failed. Run `claude login` "
                                    "or set `ANTHROPIC_API_KEY`."
                                )
                        else:
                            raise

                    # Post-response cleanup — only meaningful on the legacy path
                    # (the supervisor-session branch returns before reaching here).
                    if thinking_view is not None:
                        thinking_view.stop()

                    if response == "Cancelled.":
                        if thinking_msg:
                            try:
                                await thinking_msg.edit(
                                    content="🚫 Cancelled.",
                                    view=None,
                                )
                            except Exception:
                                await self._delete_thinking_msg(thinking_msg)
                        thinking_msg = None
                    else:
                        await self._delete_thinking_msg(thinking_msg)
                        thinking_msg = None

                        if response:
                            await self._send_long_message(
                                message.channel, response, reply_to=message
                            )
                        else:
                            try:
                                await message.add_reaction("\U0001f4ec")  # 📬
                            except Exception:
                                pass  # fail-open
                except Exception as e:
                    if thinking_view is not None:
                        thinking_view.stop()
                    await self._delete_thinking_msg(thinking_msg)
                    thinking_msg = None
                    logger.error("LLM error", exc_info=True)
                    await message.reply(f"**LLM error:** {e}")
                finally:
                    # Restore previous active project to avoid leaking
                    # channel-specific context across concurrent requests.
                    self.agent.set_active_project(prev_active)

