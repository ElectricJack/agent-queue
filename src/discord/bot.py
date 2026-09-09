"""Simplified Discord gateway: escalation replies only.

Discord no longer owns task controls, worker input, project chat, execution
threads, lifecycle notifications, or slash commands. Outbound escalation and
digest delivery live in their dedicated durable transports. The gateway's one
inbound responsibility is to authenticate a reply in a known escalation
thread and pass it to ``escalation_reply`` for the owning supervisor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from src.config import AppConfig
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
            )
            self._escalation_intake_impl = (handler, intake)
            return intake
        return cached[1]

    async def _reconcile_escalation(self, escalation_id: str) -> None:
        service = getattr(self.orchestrator, "escalation_delivery", None)
        if service is not None:
            await service.reconcile(escalation_id)

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

    async def wait_until_cutover_complete(self) -> None:
        await self._cutover_complete.wait()

    async def on_message(self, message: discord.Message) -> None:
        """Accept only known escalation-thread replies; ignore all chatter."""
        if message.author == self.user or not self._is_authorized(message.author.id):
            return
        ready = getattr(self, "_cutover_complete", None)
        if ready is not None and not ready.is_set():
            return
        report = getattr(self, "_cutover_report", None)
        if report is None or report.status != "complete":
            return
        bot_user_id = getattr(self.user, "id", None)
        await self._escalation_intake().handle(message, bot_user_id=bot_user_id)


__all__ = ["AgentQueueBot"]
