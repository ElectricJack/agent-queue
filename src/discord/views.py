"""Standalone Discord UI views and embed formatters.

This module contains reusable Discord UI components that can be imported
and used across the application without circular dependencies.
"""

from __future__ import annotations

import discord

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUGGESTION_TYPE_EMOJIS = {
    "answer": "\U0001f4a1",  # 💡
    "task": "\U0001f4cb",  # 📋
    "context": "\U0001f4ce",  # 📎
    "warning": "\u26a0\ufe0f",  # ⚠️
}

_SUGGESTION_TYPE_COLORS = {
    "answer": 0x2ECC71,  # green
    "task": 0x3498DB,  # blue
    "context": 0xF1C40F,  # yellow
    "warning": 0xE74C3C,  # red
}

# Grey color for dismissed suggestions
_DISMISSED_COLOR = 0x95A5A6

# Discord embed description limit
LIMIT_DESCRIPTION = 4096


def truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, adding ellipsis if needed."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def format_suggestion_embed(
    suggestion_type: str,
    text: str,
    project_id: str,
    confidence: float,
) -> discord.Embed:
    """Create a rich embed for a chat analyzer suggestion.

    Color-coded by suggestion type: green for answer, blue for task,
    yellow for context, red for warning. Includes a confidence indicator
    in the footer alongside the project identifier.
    """
    emoji = _SUGGESTION_TYPE_EMOJIS.get(suggestion_type, "\U0001f4ac")
    color = _SUGGESTION_TYPE_COLORS.get(suggestion_type, 0x95A5A6)
    type_label = suggestion_type.capitalize()

    embed = discord.Embed(
        title=f"{emoji} Suggestion: {type_label}",
        description=truncate(text, LIMIT_DESCRIPTION),
        color=color,
    )

    # Confidence bar: visual indicator using filled/empty blocks
    filled = round(confidence * 5)
    bar = "\u2588" * filled + "\u2591" * (5 - filled)
    embed.set_footer(
        text=(f"Chat Analyzer \u2022 {project_id} \u2022 Confidence: {bar} {confidence:.0%}")
    )
    return embed


class SuggestionView(discord.ui.View):
    """Accept/Dismiss buttons for chat analyzer suggestions.

    Uses custom_id encoding (``analyzer_accept:<id>`` / ``analyzer_dismiss:<id>``)
    so the buttons persist across bot restarts. The view has no timeout — it
    remains interactive until explicitly resolved or the message is deleted.

    On Accept — behaviour depends on suggestion type:
    - answer: posts the suggestion text as a regular bot message
    - task: creates a task via CommandHandler
    - context: posts an informational embed with the relevant context
    - warning: acknowledges and optionally offers to create a task

    On Dismiss:
    - Updates DB status to "dismissed"
    - Records dismissal timestamp for cooldown logic
    - Edits the original message to show "Suggestion dismissed" (greyed out)
    """

    def __init__(
        self,
        suggestion_id: int,
        suggestion_type: str,
        suggestion_text: str,
        project_id: str,
        task_title: str = "",
        handler=None,
        db=None,
    ) -> None:
        # timeout=None for persistence across bot restarts
        super().__init__(timeout=None)
        self.suggestion_id = suggestion_id
        self.suggestion_type = suggestion_type
        self.suggestion_text = suggestion_text
        self.project_id = project_id
        self.task_title = task_title
        self._handler = handler
        self._db = db

        # Add buttons with custom_id encoding the suggestion_id
        accept_btn = discord.ui.Button(
            label="Accept",
            style=discord.ButtonStyle.success,
            emoji="\u2705",
            custom_id=f"analyzer_accept:{suggestion_id}",
        )
        accept_btn.callback = self._on_accept
        self.add_item(accept_btn)

        dismiss_btn = discord.ui.Button(
            label="Dismiss",
            style=discord.ButtonStyle.secondary,
            emoji="\u274c",
            custom_id=f"analyzer_dismiss:{suggestion_id}",
        )
        dismiss_btn.callback = self._on_dismiss
        self.add_item(dismiss_btn)

    async def _on_accept(self, interaction: discord.Interaction) -> None:
        """Handle the Accept button press."""
        await interaction.response.defer(ephemeral=True)

        # Record acceptance in the database
        if self._db:
            try:
                await self._db.resolve_chat_analyzer_suggestion(self.suggestion_id, "accepted")
            except Exception:
                pass

        # Execute the suggestion based on type
        try:
            if self.suggestion_type == "answer":
                await interaction.channel.send(
                    f"\U0001f4a1 **Suggested answer:**\n{self.suggestion_text}"
                )
                await interaction.followup.send("Suggestion accepted!", ephemeral=True)

            elif self.suggestion_type == "task" and self._handler:
                title = self.task_title or self.suggestion_text[:80]
                result = await self._handler.execute(
                    "create_task",
                    {
                        "project_id": self.project_id,
                        "title": title,
                        "description": self.suggestion_text,
                    },
                )
                if "error" in result:
                    await interaction.followup.send(
                        f"Could not create task: {result['error']}", ephemeral=True
                    )
                else:
                    task_id = result.get("task_id", "?")
                    await interaction.followup.send(
                        f"\U0001f4cb Task `{task_id}` created: {title}", ephemeral=True
                    )

            elif self.suggestion_type == "context":
                # Post an informational embed with the context
                context_embed = discord.Embed(
                    title="\U0001f4ce Relevant Context",
                    description=truncate(self.suggestion_text, LIMIT_DESCRIPTION),
                    color=_SUGGESTION_TYPE_COLORS["context"],
                )
                context_embed.set_footer(text=f"Project: {self.project_id}")
                await interaction.channel.send(embed=context_embed)
                await interaction.followup.send("Context posted!", ephemeral=True)

            elif self.suggestion_type == "warning":
                await interaction.channel.send(
                    f"\u26a0\ufe0f **Heads up:**\n{self.suggestion_text}"
                )
                await interaction.followup.send(
                    "Warning acknowledged! Use `/create-task` if you'd like to "
                    "create a task to address this.",
                    ephemeral=True,
                )

            else:
                await interaction.followup.send("Suggestion accepted.", ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Could not post suggestion (Discord error).",
                ephemeral=True,
            )

        # Update embed to show accepted state and disable buttons
        try:
            if interaction.message and interaction.message.embeds:
                embed = interaction.message.embeds[0]
                embed.title = "\u2705 Suggestion Accepted"
                for child in self.children:
                    child.disabled = True
                await interaction.message.edit(embed=embed, view=self)
            else:
                for child in self.children:
                    child.disabled = True
                await interaction.message.edit(view=self)
        except (discord.NotFound, discord.HTTPException):
            pass  # Message may have been deleted

    async def _on_dismiss(self, interaction: discord.Interaction) -> None:
        """Handle the Dismiss button press."""
        await interaction.response.defer(ephemeral=True)

        # Record dismissal in the database (timestamp used for cooldown logic)
        if self._db:
            try:
                await self._db.resolve_chat_analyzer_suggestion(self.suggestion_id, "dismissed")
            except Exception:
                pass

        await interaction.followup.send("Suggestion dismissed.", ephemeral=True)

        # Grey out the embed and disable buttons to show dismissed state
        try:
            if interaction.message and interaction.message.embeds:
                embed = interaction.message.embeds[0]
                embed.title = "\u274c Suggestion Dismissed"
                embed.color = _DISMISSED_COLOR
                for child in self.children:
                    child.disabled = True
                await interaction.message.edit(embed=embed, view=self)
            else:
                for child in self.children:
                    child.disabled = True
                await interaction.message.edit(view=self)
        except (discord.NotFound, discord.HTTPException):
            pass  # Message may have been deleted
