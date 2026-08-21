"""Minimal read-only slash commands for the interim in-process Discord bot.

M0 messaging strip (docs/specs/design/messaging-rework.md §4.5): the 122
mirrored command handlers in the deleted ``src/discord/commands.py`` are
replaced by at most six navigation/read-only slash commands.  Every
mutation now flows through gate buttons, thread replies, supervisor chat,
or the dashboard — never a slash command.

| Command | Backs onto | Why it survives |
|---|---|---|
| ``/status`` | ``get_status`` + ``list_tasks`` | Highest-frequency glance. |
| ``/tasks`` | ``list_tasks`` | Orientation before replying in a thread. |
| ``/explain`` | ``explain_task`` | "Why isn't X running" support question. |
| ``/peek`` | ``session_peek`` | See the live pane without leaving Discord. |
| ``/gates`` | ``gate_list`` | What is waiting on a human right now. |
| ``/attach`` | ``session_attach`` | Bridge to the real terminal. |

**Registration is conditional.**  Every backing command now exists —
``session_peek``/``session_attach`` landed with the session-runtime lane and
``explain_task``/``gate_list`` with the work-graph lane (note the naming:
[[design/messaging-rework]] says ``task_explain`` and
``session_attach_command``; what shipped is ``_cmd_explain_task`` and
``_cmd_session_attach`` returning ``{"attach_command": str}``).
Registering a slash command without its backend would
publish autocompleting commands to the guild that can only ever answer
``Unknown command: ...`` — so :func:`setup_commands` resolves each backing
command name first (built-in ``_cmd_{name}`` method, then the plugin
registry — exactly the two lookups ``CommandHandler.execute`` performs) and
only registers the slash command when every backend it needs resolves.
Each command therefore appears on its own, on the next daemon restart after
its owning wave lands, with no change needed here.

``/status`` and ``/tasks`` keep the pre-strip wiring, per
docs/specs/implementation/messaging-rework.md §1 ("until a prerequisite
lands, the interim in-process bot (M0) keeps the old wiring for that
feature").  The four not-yet-backed commands format their results
schema-agnostically (pretty-printed JSON) because their result shapes do
not exist yet.

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

#: Slash command name → the ``CommandHandler`` command names it needs.
#: A slash command is registered only when *every* listed backend resolves.
SLASH_COMMAND_BACKENDS: dict[str, tuple[str, ...]] = {
    "status": ("get_status", "list_tasks"),
    "tasks": ("list_tasks",),
    "explain": ("explain_task",),
    "peek": ("session_peek",),
    "gates": ("gate_list",),
    "attach": ("session_attach",),
}

#: Backing commands known to be unimplemented today, each owned by a later
#: wave of the framework overhaul (implementation plan §6, M1).  This set is
#: an expiring allowlist: ``tests/test_slash_commands.py`` fails when a
#: backend is missing *and* absent from here, so a wave that renames its
#: command (``gates_list`` vs ``gate_list``) breaks the build instead of
#: silently un-registering a slash command.  Entries must be deleted as the
#: owning wave lands.
PENDING_BACKENDS: frozenset[str] = frozenset()


def command_resolves(handler, name: str) -> bool:
    """True when ``handler.execute(name, ...)`` would reach a real handler.

    Mirrors ``CommandHandler.execute`` exactly: a built-in ``_cmd_{name}``
    method first, then the plugin registry.  ``PluginContext.register_command``
    registers each plugin command under both a bare and a plugin-qualified
    name, and ``execute`` looks up whatever name it was given, so a single
    ``get_command(name)`` probe matches its behaviour for both shapes.
    """
    if handler is None:
        return False
    if getattr(handler, f"_cmd_{name}", None) is not None:
        return True
    orchestrator = getattr(handler, "orchestrator", None)
    registry = getattr(orchestrator, "plugin_registry", None)
    if registry is None:
        return False
    try:
        return registry.get_command(name) is not None
    except Exception:  # a half-initialised registry must not break bot startup
        logger.debug("Plugin registry lookup for %r failed", name, exc_info=True)
        return False


def available_slash_commands(handler) -> dict[str, list[str]]:
    """Return ``{slash_name: [missing backends]}`` for all six commands.

    An empty list means every backend resolved and the command is safe to
    register.  Used by :func:`setup_commands` and by the contract test.
    """
    return {
        slash_name: [b for b in backends if not command_resolves(handler, b)]
        for slash_name, backends in SLASH_COMMAND_BACKENDS.items()
    }


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

    Schema-agnostic on purpose — the commands using it land in later waves
    (see module docstring), so this can't assume field names.  ``/status``
    and ``/tasks`` get bespoke formatting below because their backends are
    already implemented and their shapes are well known.
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
    *,
    defer: bool = True,
) -> dict | None:
    """Run *command* via the shared ``CommandHandler`` and reply on error.

    Returns the result dict on success, or ``None`` after already sending
    an error reply (callers should return immediately in that case).  Pass
    ``defer=False`` for the second call of a two-call command — the
    interaction may only be deferred once.
    """
    if defer:
        await interaction.response.defer()
    try:
        result = await handler.execute(command, args)
    except Exception as e:
        logger.error("Slash command /%s (%s) failed: %s", command, args, e, exc_info=True)
        await interaction.followup.send(embed=error_embed("Error", description=str(e)))
        return None
    if isinstance(result, dict) and "error" in result:
        await interaction.followup.send(
            embed=error_embed("Error", description=str(result["error"]))
        )
        return None
    return result


def _format_status(status_result: dict, tasks: list[dict]) -> str:
    """Render the ``/status`` summary — the pre-strip content, minus the view.

    The interactive ``TaskReportView`` went with ``src/discord/commands.py``
    in the M0 strip; the same numbers are rendered as plain text here.
    """
    lines: list[str] = []
    if status_result.get("orchestrator_paused"):
        lines.append("⏸ **Orchestrator is PAUSED** — scheduling suspended")

    task_stats = status_result.get("tasks", {})
    by_status: dict = task_stats.get("by_status", {}) or {}
    total = task_stats.get("total", 0)
    lines.append(f"## System Status\n**Projects:** {status_result.get('projects', 0)}")
    if by_status:
        counts = ", ".join(f"{v} {k.lower()}" for k, v in sorted(by_status.items()))
        lines.append(f"**Tasks:** {total} total — {counts}")
    else:
        lines.append(f"**Tasks:** {total} total")

    if tasks:
        shown = tasks[:15]
        lines.append("")
        lines.append(f"**Active ({len(tasks)}):**")
        lines += [
            f"`{t.get('id', '?')}` **{t.get('status', '?')}** — {t.get('title', '(untitled)')}"
            for t in shown
        ]
        if len(tasks) > len(shown):
            lines.append(f"_...and {len(tasks) - len(shown)} more — see the dashboard._")
    else:
        lines.append("")
        lines.append("_No active tasks._")

    content = "\n".join(lines)
    if len(content) > _MAX_INLINE:
        content = content[: _MAX_INLINE - 1] + "…"
    return content


def setup_commands(bot: commands.Bot) -> list[str]:
    """Register the survivor slash commands whose backends actually exist.

    Returns the list of registered slash command names (sorted), so callers
    and tests can assert what was published to the command tree.  A command
    whose backing ``CommandHandler`` command is missing is skipped and logged
    rather than registered — an unregistered command is honest, a registered
    one that can only answer ``Unknown command`` is not.
    """

    # The shared command handler is owned by the Supervisor. Every slash
    # command calls handler.execute(name, args) for its business logic and
    # only handles Discord-specific formatting here — same convention the
    # deleted commands.py followed.
    handler = bot.agent.handler

    missing_by_command = available_slash_commands(handler)
    registered: list[str] = []

    def _wanted(slash_name: str) -> bool:
        missing = missing_by_command[slash_name]
        if missing:
            logger.info(
                "Slash command /%s not registered — backing command(s) not available yet: %s",
                slash_name,
                ", ".join(missing),
            )
            return False
        registered.append(slash_name)
        return True

    if _wanted("status"):

        @bot.tree.command(name="status", description="Show system status overview")
        @app_commands.describe(project="Project ID (optional if the channel has one)")
        async def status_command(
            interaction: discord.Interaction, project: str | None = None
        ) -> None:
            project_id = _resolve_project_from_context(bot, interaction, project)
            args: dict = {"project_id": project_id} if project_id else {}
            status_result = await _execute(interaction, handler, "get_status", dict(args))
            if status_result is None:
                return
            list_args: dict = {"include_completed": False, "display_mode": "flat"}
            if project_id:
                list_args["project_id"] = project_id
            task_result = await _execute(interaction, handler, "list_tasks", list_args, defer=False)
            if task_result is None:
                return
            await interaction.followup.send(
                _format_status(status_result, task_result.get("tasks", []))
            )

    if _wanted("tasks"):

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
                lines.append(
                    f"_...and {len(tasks) - len(shown)} more — "
                    "use the dashboard for the full list._"
                )
            content = "\n".join(lines)
            if len(content) > _MAX_INLINE:
                content = content[: _MAX_INLINE - 1] + "…"
            await interaction.followup.send(content)

    if _wanted("explain"):

        @bot.tree.command(name="explain", description="Explain why a task is (or isn't) running")
        @app_commands.describe(task="Task ID")
        async def explain_command(interaction: discord.Interaction, task: str) -> None:
            result = await _execute(interaction, handler, "explain_task", {"task_id": task})
            if result is None:
                return
            await _reply_json(interaction, result, filename="explain.json")

    if _wanted("peek"):

        @bot.tree.command(name="peek", description="See the live output pane for a running task")
        @app_commands.describe(task="Task ID")
        async def peek_command(interaction: discord.Interaction, task: str) -> None:
            result = await _execute(interaction, handler, "session_peek", {"task_id": task})
            if result is None:
                return
            await _reply_json(interaction, result, filename="peek.json")

    if _wanted("gates"):

        @bot.tree.command(name="gates", description="List gates waiting on a human")
        @app_commands.describe(project="Project ID (optional if the channel has one)")
        async def gates_command(
            interaction: discord.Interaction, project: str | None = None
        ) -> None:
            project_id = _resolve_project_from_context(bot, interaction, project)
            args: dict = {"project_id": project_id} if project_id else {}
            result = await _execute(interaction, handler, "gate_list", args)
            if result is None:
                return
            await _reply_json(interaction, result, filename="gates.json")

    if _wanted("attach"):

        @bot.tree.command(
            name="attach", description="Print the tmux attach command for a task's session"
        )
        @app_commands.describe(task="Task ID")
        async def attach_command(interaction: discord.Interaction, task: str) -> None:
            result = await _execute(interaction, handler, "session_attach", {"task_id": task})
            if result is None:
                return
            await _reply_json(interaction, result, filename="attach.json")

    logger.info(
        "Slash commands registered: %s",
        ", ".join(f"/{n}" for n in sorted(registered)) or "(none)",
    )
    return sorted(registered)
