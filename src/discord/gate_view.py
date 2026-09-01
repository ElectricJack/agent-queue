"""In-process Discord view for work-graph gates (Wave 4).

Renders ``gate.created`` events as an embed with Approve / Deny buttons.
Button callbacks route through the shared ``CommandHandler`` to
``gate_resolve``, tagging ``resolved_by`` with the clicking user's
Discord id.

Follows the ``TaskFailedView`` pattern in ``src/discord/notifications.py``:
short-lived (24h timeout), disable-on-success, ephemeral confirmations.

MVP scope (see docs/superpowers/plans/2026-08-21-wave4-discord-e2e.md):
- Two fixed buttons (Approve / Deny).  Custom option lists are follow-up.
- No cross-restart persistence.  A bot restart drops the ``View`` state;
  the ``gate.resolved`` handler will silently no-op if the message
  isn't tracked, and users fall back to ``/gates`` + ``aq gate resolve``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import discord

logger = logging.getLogger(__name__)

_APPROVE_EMOJI = "✅"
_DENY_EMOJI = "🚫"


class GateView(discord.ui.View):
    """Approve / Deny buttons attached to a ``gate.created`` embed.

    Both buttons call ``handler.execute("gate_resolve", ...)`` with
    ``resolved_by=f"discord:{interaction.user.id}"`` so the audit trail
    identifies the Discord user who clicked.
    """

    def __init__(
        self,
        gate_id: str,
        *,
        handler: Any | None = None,
        bot: Any | None = None,
        on_timeout_evict: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(timeout=86400)  # 24h — matches TaskFailedView
        self.gate_id = gate_id
        self._handler = handler
        self._bot = bot
        self._on_timeout_evict = on_timeout_evict

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Gate button clicks behind the bot's authorization allowlist.

        If no ``bot`` was passed (e.g. legacy tests or missing wiring), the
        check is a no-op and returns True so we don't regress existing
        behaviour. Otherwise unauthorized clickers get an ephemeral message
        and the button callback is skipped.
        """
        bot = self._bot
        if bot is None or not hasattr(bot, "_is_authorized"):
            return True
        try:
            if bot._is_authorized(interaction.user.id):
                return True
        except Exception:
            logger.exception(
                "GateView.interaction_check: authorization lookup failed for gate %s",
                self.gate_id,
            )
            return False
        try:
            await interaction.response.send_message(
                "You are not authorized to resolve gates.",
                ephemeral=True,
            )
        except Exception:
            logger.debug(
                "GateView.interaction_check: failed to send ephemeral rejection",
                exc_info=True,
            )
        return False

    async def on_timeout(self) -> None:
        """Release any handler-owned tracking for this gate.

        The notification handler stores the posted ``discord.Message`` in a
        dict keyed by gate id so ``gate.resolved`` can edit it.  If no
        resolution ever arrives, the entry would leak forever — so on the
        view's 24h timeout we invoke the eviction callback the handler
        wired in.  Uses ``pop(..., None)``-style semantics so a subsequent
        ``gate.resolved`` (or a second timeout) is safe.
        """
        cb = self._on_timeout_evict
        if cb is None:
            return
        try:
            cb(self.gate_id)
        except Exception:
            logger.debug(
                "GateView.on_timeout: eviction callback raised for %s",
                self.gate_id,
                exc_info=True,
            )

    async def _resolve(
        self,
        interaction: discord.Interaction,
        resolution: str,
    ) -> None:
        if self._handler is None:
            await interaction.response.send_message(
                "Handler not available.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute(
            "gate_resolve",
            {
                "gate_id": self.gate_id,
                "resolved_by": f"discord:{interaction.user.id}",
                "resolution": resolution,
            },
        )
        if isinstance(result, dict) and "error" in result:
            await interaction.followup.send(
                f"Could not resolve gate: {result['error']}", ephemeral=True
            )
            return
        unblocked = 0
        if isinstance(result, dict):
            unblocked = len(result.get("unblocked_task_ids") or [])
        emoji = _APPROVE_EMOJI if resolution == "approve" else _DENY_EMOJI
        suffix = f" — {unblocked} task(s) unblocked." if unblocked else ""
        await interaction.followup.send(
            f"{emoji} Gate `{self.gate_id}` resolved ({resolution}).{suffix}",
            ephemeral=True,
        )
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except (discord.NotFound, discord.HTTPException):
            pass

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        emoji=_APPROVE_EMOJI,
    )
    async def approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._resolve(interaction, "approve")

    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        emoji=_DENY_EMOJI,
    )
    async def deny(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._resolve(interaction, "deny")


def build_gate_embed(data: dict) -> discord.Embed:
    """Build the embed posted for a ``gate.created`` event.

    ``data`` is the raw event payload; only ``gate_id``, ``gate_type``,
    ``project_id``, and ``title`` are required.  Optional fields are
    added as embed fields when present.
    """
    title = data.get("title") or "(untitled gate)"
    question = (data.get("question") or "").strip() or "Awaiting approval."
    embed = discord.Embed(
        title=f"⏸ Gate: {title}",
        description=question,
        color=discord.Color.gold(),
    )
    embed.add_field(name="Gate ID", value=f"`{data.get('gate_id', '?')}`", inline=True)
    embed.add_field(name="Type", value=str(data.get("gate_type", "?")), inline=True)
    embed.add_field(name="Project", value=str(data.get("project_id", "?")), inline=True)
    await_id = data.get("await_id")
    if await_id:
        embed.add_field(name="Awaits", value=str(await_id), inline=True)
    waiters = data.get("waiter_task_ids") or []
    if waiters:
        shown = ", ".join(f"`{w}`" for w in waiters[:10])
        if len(waiters) > 10:
            shown += f" (+{len(waiters) - 10} more)"
        embed.add_field(name="Waiter tasks", value=shown, inline=False)
    timeout_at = data.get("timeout_at")
    if timeout_at:
        embed.set_footer(text=f"Times out at {timeout_at}")
    return embed
