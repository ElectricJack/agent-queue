"""Minimal read-only slash commands for the interim in-process Discord bot.

M0 messaging strip (docs/specs/design/messaging-rework.md §4.5): the 122
mirrored command handlers in the deleted ``src/discord/commands.py`` are
replaced by exactly six navigation/read-only slash commands.  Every
mutation now flows through gate buttons, thread replies, supervisor chat,
or the dashboard — never a slash command.

| Command | Backs onto | Why it survives |
|---|---|---|
| ``/status`` | ``system_status`` | Highest-frequency glance; one embed. |
| ``/tasks`` | ``list_tasks`` | Orientation before replying in a thread. |
| ``/explain`` | ``task_explain`` | "Why isn't X running" support question. |
| ``/peek`` | ``session_peek`` | See the live pane without leaving Discord. |
| ``/gates`` | ``gate_list`` | What is waiting on a human right now. |
| ``/attach`` | ``session_attach_command`` | Bridge to the real terminal. |

``task_explain``, ``session_peek``, ``gate_list``, and
``session_attach_command`` are M1 prerequisites owned by other
workstreams (implementation plan §6, M1) and may not exist in
``CommandHandler`` yet — ``handler.execute()`` degrades gracefully to an
``{"error": ...}`` dict for an unknown command name rather than raising,
so these slash commands are safe to register now: they will start
returning real data as the other lanes land, with no change needed here.
Their result formatting is therefore schema-agnostic (pretty-printed
JSON) rather than hand-tuned to a shape that doesn't exist yet.

This whole module is interim — it is superseded by
``packages/aq-discord/aq_discord/slash.py`` (calling ``DaemonClient``
instead of an in-process ``CommandHandler``) at M3.
"""

from __future__ import annotations

import io
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.discord.embeds import error_embed, info_embed

logger = logging.getLogger(__name__)

_MAX_INLINE = 1900  # leave headroom under Discord's 2000-char message limit


def _resolve_project_from_context(
    bot: commands.Bot,
    interaction: discord.Interaction,
    project_id: str | None,
) -> str | None:
    """Resolve *project_id*, falling back to the channel→project mapping.

    Mirrors the helper that used to live in ``src/discord/commands.py`` so
    running a survivor command inside a project channel still infers the
    right project without the caller spelling it out.
    """
    if project_id is not None:
        return project_id
    result = bot.get_project_for_channel(interaction.channel_id)
    if result:
        return result
    channel = interaction.channel
    parent_id = getattr(channel, "parent_id", None)
    if parent_id:
        return bot.get_project_for_channel(parent_id)
    return None


async def _reply_json(interaction: discord.Interaction, result: dict, *, filename: str) -> None:
    """Pretty-print a command result as JSON, falling back to a file attachment.

    Schema-agnostic on purpose — several of the six commands don't exist
    in ``CommandHandler`` yet (see module docstring), so this can't assume
    field names.  ``/tasks`` gets bespoke formatting below because
    ``list_tasks`` is already implemented and its shape is well known.
    """
    text = json.dumps(result, indent=2, default=str, ensure_ascii=False)
    if len(text) <= _MAX_INLINE:
        await interaction.followup.send(f"```json\n{text}\n```")
        return
    file = discord.File(fp=io.BytesIO(text.encode("utf-8")), filename=filename)
    await interaction.followup.send(
        f"Result attached ({len(text):,} chars — too large to inline).", file=file
    )


async def _execute(
    interaction: discord.Interaction,
    handler,
    command: str,
    args: dict,
) -> dict | None:
    """Run *command* via the shared ``CommandHandler`` and reply on error.

    Returns the result dict on success, or ``None`` after already sending
    an error reply (callers should return immediately in that case).
    """
    await interaction.response.defer()
    try:
        result = await handler.execute(command, args)
    except Exception as e:
        logger.error("Slash command /%s (%s) failed: %s", command, args, e, exc_info=True)
        await interaction.followup.send(embed=error_embed("Error", description=str(e)))
        return None
    if isinstance(result, dict) and "error" in result:
        await interaction.followup.send(embed=error_embed("Error", description=str(result["error"])))
        return None
    return result


def setup_commands(bot: commands.Bot) -> None:
    """Register the six survivor slash commands on the bot's command tree."""

    # The shared command handler is owned by the Supervisor. Every slash
    # command calls handler.execute(name, args) for its business logic and
    # only handles Discord-specific formatting here — same convention the
    # deleted commands.py followed.
    handler = bot.agent.handler

    @bot.tree.command(name="status", description="Show system status overview")
    @app_commands.describe(project="Project ID (optional if the channel has one)")
    async def status_command(interaction: discord.Interaction, project: str | None = None) -> None:
        project_id = _resolve_project_from_context(bot, interaction, project)
        args: dict = {"project_id": project_id} if project_id else {}
        result = await _execute(interaction, handler, "system_status", args)
        if result is None:
            return
        await _reply_json(interaction, result, filename="status.json")

    @bot.tree.command(name="tasks", description="List tasks for a project")
    @app_commands.describe(
        project="Project ID (optional if the channel has one)",
        status="Filter by task status (optional)",
    )
    async def tasks_command(
        interaction: discord.Interaction,
        project: str | None = None,
        status: str | None = None,
    ) -> None:
        project_id = _resolve_project_from_context(bot, interaction, project)
        args: dict = {"display_mode": "flat", "include_completed": True}
        if project_id:
            args["project_id"] = project_id
        if status:
            args["status"] = status
        result = await _execute(interaction, handler, "list_tasks", args)
        if result is None:
            return
        tasks = result.get("tasks", [])
        if not tasks:
            desc = "No tasks found" + (f" for project `{project_id}`." if project_id else ".")
            await interaction.followup.send(embed=info_embed("No Tasks", description=desc))
            return
        shown = tasks[:25]
        lines = [
            f"`{t.get('id', '?')}` **{t.get('status', '?')}** — {t.get('title', '(untitled)')}"
            for t in shown
        ]
        if len(tasks) > len(shown):
            lines.append(f"_...and {len(tasks) - len(shown)} more — use the dashboard for the full list._")
        content = "\n".join(lines)
        if len(content) > _MAX_INLINE:
            content = content[: _MAX_INLINE - 1] + "…"
        await interaction.followup.send(content)

    @bot.tree.command(name="explain", description="Explain why a task is (or isn't) running")
    @app_commands.describe(task="Task ID")
    async def explain_command(interaction: discord.Interaction, task: str) -> None:
        result = await _execute(interaction, handler, "task_explain", {"task_id": task})
        if result is None:
            return
        await _reply_json(interaction, result, filename="explain.json")

    @bot.tree.command(name="peek", description="See the live output pane for a running task")
    @app_commands.describe(task="Task ID")
    async def peek_command(interaction: discord.Interaction, task: str) -> None:
        result = await _execute(interaction, handler, "session_peek", {"task_id": task})
        if result is None:
            return
        await _reply_json(interaction, result, filename="peek.json")

    @bot.tree.command(name="gates", description="List gates waiting on a human")
    @app_commands.describe(project="Project ID (optional if the channel has one)")
    async def gates_command(interaction: discord.Interaction, project: str | None = None) -> None:
        project_id = _resolve_project_from_context(bot, interaction, project)
        args: dict = {"project_id": project_id} if project_id else {}
        result = await _execute(interaction, handler, "gate_list", args)
        if result is None:
            return
        await _reply_json(interaction, result, filename="gates.json")

    @bot.tree.command(name="attach", description="Print the tmux attach command for a task's session")
    @app_commands.describe(task="Task ID")
    async def attach_command(interaction: discord.Interaction, task: str) -> None:
        result = await _execute(interaction, handler, "session_attach_command", {"task_id": task})
        if result is None:
            return
        await _reply_json(interaction, result, filename="attach.json")
