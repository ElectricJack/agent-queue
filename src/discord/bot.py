"""Simplified Discord gateway with opt-in supervisor conversations.

Discord no longer owns task controls, worker input, project chat, execution
threads, lifecycle notifications, or slash commands. Outbound escalation and
digest delivery live in their dedicated durable transports. Escalation replies
route first; enabled conversations pass verified input through the command boundary.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from src.config import AppConfig
from src.discord.intake_diagnostics import IgnoreCounter
from src.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class AgentQueueBot(commands.Bot):
    """One-channel Discord adapter with no direct work mutation surface."""

    def __init__(self, config: AppConfig, orchestrator: Orchestrator):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.orchestrator = orchestrator

        from src.discord.rate_guard import configure_tracker

        self._rate_tracker = configure_tracker(
            warn=config.discord.rate_guard_warn,
            critical=config.discord.rate_guard_critical,
            halt=config.discord.rate_guard_halt,
        )
        self._guild: discord.Guild | None = None
        self._escalation_intake_impl: tuple[Any, Any] | None = None
        self._inbound_router_impl: tuple[Any, Any, Any] | None = None
        self._conversation_backfill_impl: tuple[Any, Any] | None = None
        # Outlives any one intake adapter: a handler swap rebuilds the adapter,
        # not the counts ``digest_status`` reports as its ``intake`` block.
        self._intake_diagnostics = IgnoreCounter()
        self._cutover_complete = asyncio.Event()
        self._cutover_report: Any = None

    @property
    def handler(self):
        """The daemon-wide command boundary wired by ``main.py``."""
        return self.orchestrator._command_handler

    def _is_authorized(self, user_id: int | str) -> bool:
        allowed = self.config.discord.authorized_users
        return not allowed or str(user_id) in allowed

    def _escalation_intake(self):
        """Build the narrow inbound adapter lazily against the current handler."""
        from src.discord.escalation_intake import DiscordEscalationIntake

        handler = self.handler
        cached = self._escalation_intake_impl
        if cached is None or cached[0] is not handler:
            intake = DiscordEscalationIntake(
                handler,
                self.config,
                reconcile=self._reconcile_escalation,
                on_ignore=self._intake_diagnostics.record,
            )
            self._escalation_intake_impl = (handler, intake)
            return intake
        return cached[1]

    async def _reconcile_escalation(self, escalation_id: str) -> None:
        service = getattr(self.orchestrator, "escalation_delivery", None)
        if service is not None:
            await service.reconcile(escalation_id)

    def _inbound_router(self):
        """Build lazily; rebuilding the handler also rebuilds both inbound adapters."""
        from src.discord.inbound import DiscordInboundRouter

        handler = self.handler
        intake = self._escalation_intake()
        cached = getattr(self, "_inbound_router_impl", None)
        if cached is None or cached[0] is not handler or cached[1] is not intake:
            diagnostics = getattr(self, "_intake_diagnostics", None)
            if diagnostics is None:
                diagnostics = self._intake_diagnostics = IgnoreCounter()
            router = DiscordInboundRouter(
                bot=self,
                handler=handler,
                config=self.config,
                escalation_intake=intake,
                diagnostics=diagnostics,
                cutover_status=lambda: getattr(
                    getattr(self, "_cutover_report", None), "status", None
                ),
                outbox_bound=lambda: bool(
                    getattr(getattr(self.orchestrator, "conversation_outbox", None), "bound", False)
                ),
            )
            self._inbound_router_impl = (handler, intake, router)
            return router
        return cached[2]

    def _conversation_backfill(self):
        """Keep one recovery pass lock and refresh it when the router changes."""
        from src.conversations.backfill import ConversationBackfill
        from src.discord.escalation_transport import DiscordEscalationTransport

        router = self._inbound_router()
        cached = getattr(self, "_conversation_backfill_impl", None)
        if cached is None or cached[0] is not router:
            backfill = ConversationBackfill(
                db=self.handler.db,
                router=router,
                transport=DiscordEscalationTransport(self, self.config),
                config=self.config,
            )
            self._conversation_backfill_impl = (router, backfill)
            return backfill
        return cached[1]

    async def _run_conversation_backfill(self) -> None:
        try:
            result = await self._conversation_backfill().run(
                bot_user_id=getattr(self.user, "id", None)
            )
            logger.info("Discord conversation backfill: %s", result)
        except Exception:
            logger.warning("Discord conversation backfill failed", exc_info=True)

    async def setup_hook(self) -> None:
        """Keep plugin commands, retire only the six former AQ commands, sync."""
        registry = getattr(self.orchestrator, "plugin_registry", None)
        if registry:
            for command in registry.get_discord_commands():
                try:
                    self.tree.add_command(command)
                except Exception:
                    logger.warning("Failed to register plugin Discord command", exc_info=True)

        from src.discord.slash_commands import unregister_retired_commands

        retired = unregister_retired_commands(self.tree)
        if retired:
            logger.info("Retired Discord slash commands: %s", ", ".join(retired))

        original_check = self.tree.interaction_check

        async def _authorized(interaction: discord.Interaction) -> bool:
            if not self._is_authorized(interaction.user.id):
                await interaction.response.send_message(
                    "You don't have permission to use this command.", ephemeral=True
                )
                return False
            return await original_check(interaction)

        self.tree.interaction_check = _authorized

        async def _on_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ) -> None:
            original = getattr(error, "original", error)
            logger.error("Discord application command failed: %r", original, exc_info=True)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(str(original), ephemeral=True)
                else:
                    await interaction.response.send_message(str(original), ephemeral=True)
            except Exception:
                pass

        self.tree.on_error = _on_error

        try:
            if self.config.discord.guild_id:
                guild = discord.Object(id=int(self.config.discord.guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
            else:
                synced = await self.tree.sync()
            logger.info("Synced %d non-retired Discord command(s)", len(synced))
        except Exception:
            logger.error("Failed to sync Discord command retirement", exc_info=True)

    async def on_ready(self) -> None:
        """Finish the idempotent migration before inbound routing becomes live."""
        self.orchestrator._discord_bot = self
        if self.config.discord.guild_id:
            self._guild = self.get_guild(int(self.config.discord.guild_id))
        try:
            from src.discord.cutover import run_discord_cutover

            self._cutover_report = await run_discord_cutover(self)
            logger.info(self._cutover_report.summary())
        except Exception as exc:
            logger.exception("Discord cutover inventory failed")
            from src.discord.cutover import CutoverReport

            self._cutover_report = CutoverReport(
                status="failed", conflicts=(f"migration failed: {exc}",)
            )
        finally:
            self._cutover_complete.set()
        await self._run_conversation_backfill()

    async def on_resumed(self) -> None:
        """A resumed gateway can have missed messages just like a fresh READY."""
        await self._run_conversation_backfill()

    async def wait_until_cutover_complete(self) -> None:
        await self._cutover_complete.wait()

    async def on_message(self, message: discord.Message) -> None:
        """Route authenticated messages only after the Discord cutover completes."""
        if message.author == self.user or not self._is_authorized(message.author.id):
            return
        ready = getattr(self, "_cutover_complete", None)
        if ready is not None and not ready.is_set():
            return
        report = getattr(self, "_cutover_report", None)
        if report is None or report.status != "complete":
            return
        bot_user_id = getattr(self.user, "id", None)
        await self._inbound_router().route(message, bot_user_id=bot_user_id)


__all__ = ["AgentQueueBot"]
