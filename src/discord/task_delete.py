"""One operator delete action using the daemon's shared preview and execution."""

from __future__ import annotations

import asyncio
import io

import discord
from discord import app_commands

from src.integration.task_deletion import deletion_actor


def is_delete_operator(bot, interaction):
    """Check the configured destination and operator identity on every press."""
    config = bot.config.discord
    if str(interaction.channel_id) != config.channel_id:
        return False
    if config.guild_id and str(interaction.guild_id) != config.guild_id:
        return False
    if not bot._is_authorized(interaction.user.id):
        return False
    return str(interaction.user.id) in config.authorized_users or bool(
        getattr(getattr(interaction.user, "guild_permissions", None), "administrator", False)
    )


def preview_text(plan):
    """Keep exact ids/tips accessible even when Discord's message limit is hit."""
    lines = ["Delete selected tasks? Running sessions and claims will stop. Audit history remains."]
    lines += [
        f"Task {row['id']}: {row['title']} ({row['status']})" for row in plan.get("tasks", [])
    ]
    if plan.get("reparented_tasks"):
        lines.append("Children kept: " + ", ".join(plan["reparented_tasks"]))
    for branch in plan.get("branch_details", []):
        lines.append(
            f"{branch['action'].title()} branch {branch['branch']}: remote "
            f"{branch.get('remote_sha') or branch['remote_state']}"
        )
        lines += [
            f"Local {ref['path']}: {ref.get('sha') or ref.get('tracking_sha')}"
            for ref in branch.get("local_refs", [])
        ]
        lines += [f"Worktree {checkout['path']}" for checkout in branch.get("worktrees", [])]
    lines += [
        f"Stop session {session['session_id']} ({session['task_id']})"
        for session in plan.get("sessions", [])
    ]
    lines += ["Warning: " + notice["message"] for notice in plan.get("warnings", [])]
    return "\n".join(lines)


class DeleteTaskView(discord.ui.View):
    """Scope and branch choices update the preview before one final confirmation."""

    def __init__(self, bot, task_id, user_id, *, reason=""):
        super().__init__(timeout=300)
        self.bot, self.task_id, self.user_id = bot, task_id, user_id
        self.recursive, self.branches, self.reason = False, "keep", reason
        self.plan = None
        self.finished = False
        self.lock = asyncio.Lock()
        self.confirm.disabled = True

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id or not is_delete_operator(self.bot, interaction):
            await interaction.response.send_message(
                "Only the requesting operator can confirm.", ephemeral=True
            )
            return False
        return True

    def args(self):
        return {
            "task_id": self.task_id,
            "recursive": self.recursive,
            "branches": self.branches,
            "stop_running": True,
            "reason": self.reason,
        }

    async def refresh(self):
        self.confirm.disabled = True
        self.plan = await self.bot.handler.execute(
            "delete_task", {**self.args(), "confirmed": False}
        )
        self.confirm.disabled = not self.plan.get("success", False)
        return self.content()

    def content(self):
        if not self.plan or not self.plan.get("success"):
            return (self.plan or {}).get("error", "Loading deletion preview…")
        content = preview_text(self.plan)
        if len(content) > 1900:
            return content[:1750] + "\nThe full resource preview is attached."
        return content

    def response_args(self):
        content = preview_text(self.plan) if self.plan and self.plan.get("success") else ""
        attachments = (
            [discord.File(io.BytesIO(content.encode()), filename="deletion-preview.txt")]
            if len(content) > 1900
            else []
        )
        return {
            "content": self.content(),
            "view": self,
            "attachments": attachments,
            "allowed_mentions": discord.AllowedMentions.none(),
        }

    @discord.ui.select(
        placeholder="Scope: this task only",
        row=0,
        options=[
            discord.SelectOption(label="This task only; keep children", value="task", default=True),
            discord.SelectOption(label="This task and all descendants", value="recursive"),
        ],
    )
    async def scope(self, interaction, selection):
        await interaction.response.defer()
        async with self.lock:
            if self.finished:
                return
            self.recursive = selection.values[0] == "recursive"
            for option in selection.options:
                option.default = option.value == selection.values[0]
            await self.refresh()
            await interaction.edit_original_response(**self.response_args())

    @discord.ui.select(
        placeholder="Branches: keep",
        row=1,
        options=[
            discord.SelectOption(label="Keep branches and worktrees", value="keep", default=True),
            discord.SelectOption(
                label="Delete local/remote branches and worktrees", value="delete"
            ),
        ],
    )
    async def branch_choice(self, interaction, selection):
        await interaction.response.defer()
        async with self.lock:
            if self.finished:
                return
            self.branches = selection.values[0]
            for option in selection.options:
                option.default = option.value == self.branches
            await self.refresh()
            await interaction.edit_original_response(**self.response_args())

    @discord.ui.button(label="Delete selected tasks", style=discord.ButtonStyle.danger, row=2)
    async def confirm(self, interaction, button):
        await interaction.response.defer()
        async with self.lock:
            if self.finished or self.confirm.disabled or not self.plan:
                return
            self.finished = True
            token = deletion_actor.set(f"human:discord:{interaction.user.id}")
            try:
                result = await self.bot.handler.execute(
                    "delete_task", {**self.args(), "confirmed": True}
                )
            except Exception as exc:
                # Inspect the task before retrying an ambiguous execution error.
                result = {"success": False, "error": str(exc)}
            finally:
                deletion_actor.reset(token)
            if result.get("success"):
                content = f"Deleted {', '.join(result['task_ids'])}. Audit: {result['audit_id']}."
                content += "\n" + "\n".join(w["message"] for w in result.get("warnings", []))
            else:
                content = result.get("error", "Deletion failed. Check task status before retrying.")
            await interaction.edit_original_response(
                content=content[:1900], view=None, allowed_mentions=discord.AllowedMentions.none()
            )
            self.stop()

    @discord.ui.button(label="Cancel", row=2)
    async def cancel(self, interaction, button):
        await interaction.response.defer()
        async with self.lock:
            self.finished = True
            await interaction.edit_original_response(content="Deletion cancelled.", view=None)
            self.stop()


def register_task_delete(bot):
    @app_commands.command(
        name="delete-task", description="Preview and delete a task with scope and branch choices"
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(task_id="Task to delete", reason="Optional reason retained in the audit")
    async def delete_task(interaction: discord.Interaction, task_id: str, reason: str = ""):
        if not is_delete_operator(bot, interaction):
            await interaction.response.send_message(
                "Task deletion requires an operator in the configured AQ channel.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        view = DeleteTaskView(bot, task_id, interaction.user.id, reason=reason)
        await view.refresh()
        await interaction.edit_original_response(**view.response_args())

    bot.tree.add_command(delete_task)
