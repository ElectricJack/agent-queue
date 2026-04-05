"""Discord slash commands for AgentQueue.

All commands delegate their business logic to the shared ``CommandHandler``,
ensuring feature parity with the chat agent LLM tools.  This file is
intentionally a thin formatting layer: each slash command calls
``handler.execute(name, args)`` and only handles Discord-specific presentation
(ephemeral replies, embeds, file attachments, autocomplete).  No business
logic lives here -- see ``src/command_handler.py`` for that.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from src.discord.embeds import (
    STATUS_COLORS,
    STATUS_EMOJIS,
    TYPE_TAGS,
    EmbedStyle,
    make_embed,
    success_embed,
    error_embed,
    warning_embed,
    info_embed,
    status_embed,
    truncate,
    progress_bar,
    LIMIT_FIELD_VALUE,
)
from src.discord.project_wizard import ProjectInfoModal

logger = logging.getLogger(__name__)


async def _send_error(
    interaction,
    message: str,
    *,
    followup: bool = False,
    ephemeral: bool = True,
):
    """Send an error response as a rich embed."""
    embed = error_embed("Error", description=message)
    if followup:
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


async def _send_success(
    interaction,
    title: str,
    *,
    description: str | None = None,
    fields: list[tuple[str, str, bool]] | None = None,
    followup: bool = False,
    ephemeral: bool = False,
    result: dict | None = None,
):
    """Send a success response as a rich green embed."""
    if result and result.get("warning"):
        warning_text = f"\u26a0\ufe0f {result['warning']}"
        description = f"{description}\n\n{warning_text}" if description else warning_text
    embed = success_embed(title, description=description, fields=fields)
    if followup:
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


_NOTES_PER_PAGE = 20  # Max note buttons (4 rows × 5 buttons)


async def _send_long_interaction(
    text: str,
    send_func,
    *,
    filename: str = "response.md",
    **send_kwargs,
) -> None:
    """Send a long message via a Discord interaction, splitting or attaching as needed.

    Works with both ``interaction.response.send_message`` and
    ``interaction.followup.send``.  Passes through extra *send_kwargs*
    (e.g. ``ephemeral=True``, ``view=...``) to the first chunk only so
    that Discord UI elements are attached correctly.

    Behaviour:
    - ≤2000 chars → send as-is.
    - 2001–6000 chars → split at line boundaries into ≤2000-char chunks.
    - >6000 chars → attach as a file with a short preview.
    """
    if len(text) <= 2000:
        await send_func(text, **send_kwargs)
        return

    # Very long content → attach as file with a short preview
    if len(text) > 6000:
        preview_end = text.find("\n\n", 0, 500)
        if preview_end == -1:
            preview_end = min(300, len(text))
        preview = text[:preview_end].rstrip()

        file = discord.File(
            fp=io.BytesIO(text.encode("utf-8")),
            filename=filename,
        )
        preview_msg = f"{preview}\n\n*Full response attached ({len(text):,} chars)*"
        # Ensure preview itself fits Discord limit
        if len(preview_msg) > 2000:
            preview_msg = preview_msg[:1997] + "…"
        await send_func(preview_msg, file=file, **send_kwargs)
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
        if i == 0:
            # First chunk carries the extra kwargs (view, ephemeral, etc.)
            await send_func(chunk, **send_kwargs)
        else:
            # Subsequent chunks are plain follow-ups.  If the send_func is
            # ``interaction.response.send_message``, the response has already
            # been consumed so we can't call it again.  However, in practice
            # the caller always uses ``followup.send`` for long lists, which
            # *can* be called multiple times.  For the rare case where the
            # first call was ``interaction.response``, we just skip the extra
            # kwargs (view etc.) because Discord only allows one response.
            try:
                await send_func(chunk)
            except discord.errors.InteractionResponded:
                # Response already sent – silently drop overflow rather than crash.
                break


class _NoteDismissButton(discord.ui.Button):
    """Dismiss button that deletes the note-viewing message."""

    def __init__(self, project_id: str, note_slug: str, bot) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Dismiss",
            custom_id=f"notes:{project_id}:dismiss:{note_slug}",
        )
        self._bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = self._bot
        if bot:
            thread_id = interaction.channel_id
            note_viewers = getattr(bot, "_note_viewers", {})
            if thread_id in note_viewers:
                fname = f"{self.custom_id.split(':')[3]}.md"
                note_viewers[thread_id].pop(fname, None)
        await interaction.message.delete()


class _NoteDeleteButton(discord.ui.Button):
    """Delete button that deletes the actual note file and removes the message."""

    def __init__(self, project_id: str, note_slug: str, handler, bot) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Delete",
            custom_id=f"notes:{project_id}:delete:{note_slug}",
        )
        self._project_id = project_id
        self._slug = note_slug
        self._handler = handler
        self._bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = self._handler
        if not handler:
            await interaction.response.send_message("Not available.", ephemeral=True)
            return

        title = self._slug.replace("-", " ").title()
        await interaction.response.defer(ephemeral=True)

        result = await handler.execute(
            "delete_note",
            {
                "project_id": self._project_id,
                "title": title,
            },
        )
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        # Clean up note_viewers tracking
        bot = self._bot
        fname = f"{self._slug}.md"
        if bot:
            thread_id = interaction.channel_id
            note_viewers = getattr(bot, "_note_viewers", {})
            if thread_id in note_viewers:
                note_viewers[thread_id].pop(fname, None)

        # Delete the note-viewing message
        try:
            await interaction.message.delete()
        except Exception:
            pass

        # Send ephemeral confirmation
        await _send_success(
            interaction,
            "Note Deleted",
            description=f"Note **{title}** deleted.",
            followup=True,
            ephemeral=True,
        )

        # Auto-refresh notes TOC if available
        if bot:
            await self._refresh_toc(bot, interaction.channel_id)

    async def _refresh_toc(self, bot, thread_id: int) -> None:
        """Try to refresh the notes TOC message after deletion."""
        toc_messages = getattr(bot, "_notes_toc_messages", {})
        toc_msg_id = toc_messages.get(thread_id)
        if not toc_msg_id:
            return
        try:
            handler = self._handler
            result = await handler.execute(
                "list_notes",
                {
                    "project_id": self._project_id,
                },
            )
            if "error" in result:
                return
            notes = result.get("notes", [])
            thread = bot.get_channel(thread_id)
            if not thread:
                return
            toc_msg = await thread.fetch_message(toc_msg_id)
            view = NotesView(
                self._project_id,
                notes,
                handler=handler,
                bot=bot,
            )
            await toc_msg.edit(
                content=view.build_content(),
                view=view,
            )
        except Exception:
            pass


class _NotePlanButton(discord.ui.Button):
    """Button that creates a task to plan/implement what's described in the note."""

    def __init__(self, project_id: str, note_slug: str, handler, bot) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Plan Implementation",
            custom_id=f"notes:{project_id}:plan:{note_slug}",
        )
        self._project_id = project_id
        self._slug = note_slug
        self._handler = handler
        self._bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = self._handler
        if not handler:
            await interaction.response.send_message("Not available.", ephemeral=True)
            return

        title = self._slug.replace("-", " ").title()
        await interaction.response.defer(ephemeral=True)

        # Read the note content
        result = await handler.execute(
            "read_note",
            {
                "project_id": self._project_id,
                "title": title,
            },
        )
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        note_content = result["content"]
        note_title = result.get("title", title)

        # Create a task to plan/implement what's described in the note
        task_title = f"Plan implementation: {note_title}"
        task_description = (
            f"Plan and implement what is described in the following note.\n\n"
            f"## Note: {note_title}\n\n{note_content}"
        )

        task_result = await handler.execute(
            "create_task",
            {
                "project_id": self._project_id,
                "title": task_title,
                "description": task_description,
            },
        )
        if "error" in task_result:
            await _send_error(interaction, task_result["error"], followup=True)
            return

        task_id = task_result["created"]
        await _send_success(
            interaction,
            "Task Created",
            description=(
                f"Created task **{task_id}** to plan implementation of note **{note_title}**."
            ),
            followup=True,
            ephemeral=True,
        )


class NoteContentView(discord.ui.View):
    """View attached to a note content message with Dismiss, Delete, and Plan buttons."""

    def __init__(self, project_id: str, note_slug: str, handler=None, bot=None) -> None:
        super().__init__(timeout=None)
        self.add_item(_NotePlanButton(project_id, note_slug, handler, bot))
        self.add_item(_NoteDismissButton(project_id, note_slug, bot))
        self.add_item(_NoteDeleteButton(project_id, note_slug, handler, bot))


class _NoteViewButton(discord.ui.Button):
    """Button that displays a note's content when clicked."""

    def __init__(self, project_id: str, slug: str, label: str, handler, bot) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            custom_id=f"notes:{project_id}:view:{slug}",
        )
        self._project_id = project_id
        self._slug = slug
        self._handler = handler
        self._bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = self._handler
        bot = self._bot
        if not handler:
            await interaction.response.send_message("Not available.", ephemeral=True)
            return
        title = self._slug.replace("-", " ").title()
        await interaction.response.defer()
        result = await handler.execute(
            "read_note",
            {
                "project_id": self._project_id,
                "title": title,
            },
        )
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        content = result["content"]
        fname = f"{self._slug}.md"
        thread_id = interaction.channel_id

        # Delete previous view of the same note if any
        if bot:
            note_viewers = getattr(bot, "_note_viewers", {})
            if thread_id in note_viewers and fname in note_viewers[thread_id]:
                try:
                    old_msg = await interaction.channel.fetch_message(
                        note_viewers[thread_id][fname]
                    )
                    await old_msg.delete()
                except Exception:
                    pass

        # Send note content with dismiss and delete buttons
        dismiss_view = NoteContentView(self._project_id, self._slug, handler=handler, bot=bot)
        if len(content) <= 1900:
            msg = await interaction.followup.send(
                f"### 📄 {result['title']}\n```md\n{content}\n```",
                view=dismiss_view,
                wait=True,
            )
        else:
            file = discord.File(
                fp=io.BytesIO(content.encode("utf-8")),
                filename=fname,
            )
            preview = content[:300].rstrip()
            msg = await interaction.followup.send(
                f"### 📄 {result['title']}\n{preview}\n\n"
                f"*Full content attached ({len(content):,} chars)*",
                file=file,
                view=dismiss_view,
                wait=True,
            )

        # Track the viewing message
        if bot:
            note_viewers = getattr(bot, "_note_viewers", {})
            note_viewers.setdefault(thread_id, {})[fname] = msg.id


class _NotesRefreshButton(discord.ui.Button):
    """Refreshes the notes TOC by re-fetching the note list."""

    def __init__(self, project_id: str, handler, bot) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Refresh",
            custom_id=f"notes:{project_id}:refresh",
            row=4,
        )
        self._project_id = project_id
        self._handler = handler
        self._bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = self._handler
        if not handler:
            await interaction.response.send_message("Not available.", ephemeral=True)
            return
        await interaction.response.defer()
        result = await handler.execute(
            "list_notes",
            {
                "project_id": self._project_id,
            },
        )
        if "error" not in result:
            parent_view: NotesView = self.view
            parent_view.notes = result.get("notes", [])
            parent_view.total_pages = max(
                1, (len(parent_view.notes) + _NOTES_PER_PAGE - 1) // _NOTES_PER_PAGE
            )
            if parent_view.page >= parent_view.total_pages:
                parent_view.page = parent_view.total_pages - 1
            parent_view._rebuild_components()
            await interaction.edit_original_response(
                content=parent_view.build_content(),
                view=parent_view,
            )


class _NotesCloseButton(discord.ui.Button):
    """Archives the notes thread and cleans up tracking state."""

    def __init__(self, project_id: str, bot) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Close Thread",
            custom_id=f"notes:{project_id}:close",
            row=4,
        )
        self._project_id = project_id
        self._bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = self._bot
        if not bot:
            await interaction.response.send_message("Not available.", ephemeral=True)
            return
        await interaction.response.defer()

        # Find the thread for this project — it may not be interaction.channel
        # (the TOC message lives in the parent channel, not the thread).
        thread = None
        if isinstance(interaction.channel, discord.Thread):
            thread = interaction.channel
        else:
            # Look up the thread from bot tracking
            for tid, pid in list(bot._notes_threads.items()):
                if pid == self._project_id:
                    thread = bot.get_channel(tid)
                    if isinstance(thread, discord.Thread):
                        break
                    thread = None

        if thread and isinstance(thread, discord.Thread):
            # Clean up tracking
            note_viewers = getattr(bot, "_note_viewers", {})
            note_viewers.pop(thread.id, None)
            toc_messages = getattr(bot, "_notes_toc_messages", {})
            toc_messages.pop(thread.id, None)
            # Unregister and archive
            bot._notes_threads.pop(thread.id, None)
            await bot._save_notes_threads()
            await thread.edit(archived=True)
            # Delete the TOC message (the /notes response with buttons)
            try:
                await interaction.message.delete()
            except Exception:
                pass


class _NotesPageButton(discord.ui.Button):
    """Paginates the notes TOC forward or backward."""

    def __init__(self, project_id: str, direction: str, disabled: bool) -> None:
        label = "◀ Prev" if direction == "prev" else "Next ▶"
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"notes:{project_id}:page:{direction}",
            disabled=disabled,
            row=4,
        )
        self._direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        parent_view: NotesView = self.view
        if self._direction == "prev" and parent_view.page > 0:
            parent_view.page -= 1
        elif self._direction == "next" and parent_view.page < parent_view.total_pages - 1:
            parent_view.page += 1
        parent_view._rebuild_components()
        await interaction.response.edit_message(
            content=parent_view.build_content(),
            view=parent_view,
        )


class NotesView(discord.ui.View):
    """Interactive table-of-contents for project notes with per-note buttons."""

    def __init__(
        self,
        project_id: str,
        notes: list[dict],
        page: int = 0,
        handler=None,
        bot=None,
    ) -> None:
        super().__init__(timeout=None)
        self.project_id = project_id
        self.notes = notes
        self.page = page
        self._handler = handler
        self._bot = bot
        self.total_pages = max(1, (len(notes) + _NOTES_PER_PAGE - 1) // _NOTES_PER_PAGE)
        self._rebuild_components()

    def _rebuild_components(self) -> None:
        self.clear_items()
        start = self.page * _NOTES_PER_PAGE
        page_notes = self.notes[start : start + _NOTES_PER_PAGE]

        # Rows 1-4: note buttons (up to 5 per row, 4 rows = 20 max)
        for note in page_notes:
            slug = note["name"][:-3] if note["name"].endswith(".md") else note["name"]
            label = note["title"]
            if len(label) > 72:
                label = label[:69] + "..."
            self.add_item(
                _NoteViewButton(
                    self.project_id,
                    slug,
                    label,
                    self._handler,
                    self._bot,
                )
            )

        # Row 5: control buttons
        if self.total_pages > 1:
            self.add_item(
                _NotesPageButton(
                    self.project_id,
                    "prev",
                    disabled=(self.page == 0),
                )
            )
            self.add_item(
                _NotesPageButton(
                    self.project_id,
                    "next",
                    disabled=(self.page >= self.total_pages - 1),
                )
            )
        self.add_item(_NotesRefreshButton(self.project_id, self._handler, self._bot))
        self.add_item(_NotesCloseButton(self.project_id, self._bot))

    def build_content(self) -> str:
        if not self.notes:
            return "## 📝 Notes\n_No notes yet._ Use the chat thread to create some!"
        lines = ["## 📝 Notes"]
        start = self.page * _NOTES_PER_PAGE
        for i, note in enumerate(self.notes[start : start + _NOTES_PER_PAGE], start=1):
            size_kb = note["size_bytes"] / 1024
            lines.append(f"**{i}.** {note['title']} (`{note['name']}`, {size_kb:.1f} KB)")
        if self.total_pages > 1:
            lines.append(f"\n_Page {self.page + 1}/{self.total_pages}_")
        return "\n".join(lines)


def _split_display_text(text: str, limit: int) -> list[str]:
    """Split *text* into chunks of at most *limit* characters.

    Splits on line boundaries so individual task lines are never broken
    mid-line.  If the text starts or ends with a code-block fence (````` ```
    `````) each chunk is wrapped in its own fence pair so Discord renders
    them correctly.

    Returns a list with at least one element.
    """
    is_code_block = text.lstrip().startswith("```")
    # Determine the fence marker (e.g. "```\n" / "\n```")
    fence_open = ""
    fence_close = ""
    inner = text
    if is_code_block:
        # Extract the opening fence (e.g. "```\n" or "```py\n")
        first_nl = text.index("\n")
        fence_open = text[: first_nl + 1]
        # The closing fence is always "```" possibly preceded by a newline
        fence_close = "\n```"
        # Strip outer fences so we can split the inner content
        inner = text[first_nl + 1 :]
        if inner.rstrip().endswith("```"):
            inner = inner.rstrip()[:-3].rstrip("\n")

    # Budget per chunk: subtract fence overhead
    overhead = len(fence_open) + len(fence_close)
    chunk_limit = limit - overhead

    lines = inner.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in lines:
        # +1 for the newline character
        line_cost = len(line) + 1
        if current and current_len + line_cost > chunk_limit:
            body = "\n".join(current)
            chunks.append(f"{fence_open}{body}{fence_close}" if is_code_block else body)
            current = []
            current_len = 0
        current.append(line)
        current_len += line_cost

    if current:
        body = "\n".join(current)
        chunks.append(f"{fence_open}{body}{fence_close}" if is_code_block else body)

    return chunks or [text]


# ---------------------------------------------------------------------------
# Interactive menu view — persistent control panel
# ---------------------------------------------------------------------------


class _AddTaskMenuModal(discord.ui.Modal, title="Add New Task"):
    """Modal dialog for adding a task via the interactive menu."""

    task_description = discord.ui.TextInput(
        label="Task description",
        style=discord.TextStyle.long,
        placeholder="Describe what the task should do…",
        required=True,
        max_length=2000,
    )

    def __init__(self, project_id: str, handler) -> None:
        super().__init__()
        self._project_id = project_id
        self._handler = handler

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        description = self.task_description.value
        title = description[:100]
        result = await self._handler.execute(
            "create_task",
            {
                "project_id": self._project_id,
                "title": title,
                "description": description,
            },
        )
        if "error" in result:
            await interaction.followup.send(
                f"Could not create task: {result['error']}", ephemeral=True
            )
        else:
            task_id = result.get("created") or result.get("task_id", "?")
            await interaction.followup.send(
                f"✅ Task `{task_id}` created in project `{self._project_id}`.",
                ephemeral=True,
            )


class MenuView(discord.ui.View):
    """Persistent interactive control panel for agent-queue.

    Provides clickable buttons for common actions — useful for mobile usage
    and remote control without typing slash commands.  The view never times
    out so the message stays interactive as long as the bot is running.
    """

    def __init__(self, handler, bot) -> None:
        super().__init__(timeout=None)
        self._handler = handler
        self._bot = bot

    # --- Row 1: Status & Task Info ---

    @discord.ui.button(
        label="Status",
        style=discord.ButtonStyle.primary,
        emoji="📊",
        row=0,
    )
    async def status_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("get_status", {})
        tasks = result.get("tasks", {})
        by_status = tasks.get("by_status", {})
        total = tasks.get("total", 0)
        completed = by_status.get("COMPLETED", 0)
        in_progress = by_status.get("IN_PROGRESS", 0)
        failed = by_status.get("FAILED", 0)
        assigned = by_status.get("ASSIGNED", 0)
        active = in_progress + assigned
        ready = by_status.get("READY", 0)
        pending = by_status.get("DEFINED", 0)
        blocked = by_status.get("BLOCKED", 0)

        lines = []
        if result.get("orchestrator_paused"):
            lines.append("⏸ **Orchestrator is PAUSED**")
        lines.append("## System Status")

        if total > 0:
            bar = progress_bar(completed, total, width=12)
            lines.append(f"**Progress:** {bar}")

        parts = []
        if active:
            parts.append(f"{active} active")
        if ready:
            parts.append(f"{ready} ready")
        if pending:
            parts.append(f"{pending} pending")
        if completed:
            parts.append(f"{completed} completed")
        if failed:
            parts.append(f"{failed} failed")
        if blocked:
            parts.append(f"{blocked} blocked")
        lines.append(f"**Tasks:** {total} total — " + ", ".join(parts))

        agents = result.get("agents", [])
        if agents:
            lines.append("\n**Agents:**")
            for a in agents:
                working_on = a.get("working_on")
                if working_on:
                    lines.append(
                        f"• **{a['name']}** ({a['state']}) → "
                        f"**{working_on['project_id']}** / "
                        f"`{working_on['task_id']}` — {working_on['title']}"
                    )
                else:
                    lines.append(f"• **{a['name']}** ({a['state']})")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @discord.ui.button(
        label="Active Tasks",
        style=discord.ButtonStyle.primary,
        emoji="📋",
        row=0,
    )
    async def active_tasks_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute(
            "list_active_tasks_all_projects",
            {"include_completed": False},
        )
        by_project = result.get("by_project", {})
        total = result.get("total", 0)

        if total == 0:
            await interaction.followup.send("No active tasks across any project.", ephemeral=True)
            return

        lines = [f"**{total} active tasks:**"]
        for project_id in sorted(by_project.keys()):
            project_tasks = by_project[project_id]
            lines.append(f"\n`{project_id}` ({len(project_tasks)}):")
            for t in project_tasks[:8]:
                emoji = STATUS_EMOJIS.get(t["status"], "⚪")
                title = t["title"][:55] + "…" if len(t["title"]) > 55 else t["title"]
                agent = f" ← `{t['assigned_agent']}`" if t.get("assigned_agent") else ""
                lines.append(f"{emoji} **{title}** `{t['id']}`{agent}")
            if len(project_tasks) > 8:
                lines.append(f"_...and {len(project_tasks) - 8} more_")

        msg = "\n".join(lines)
        await _send_long_interaction(msg, interaction.followup.send, ephemeral=True)

    @discord.ui.button(
        label="All Tasks",
        style=discord.ButtonStyle.primary,
        emoji="🌳",
        row=0,
    )
    async def all_tasks_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Show all tasks across all projects in tree view format."""
        await interaction.response.defer(ephemeral=True)

        # Get all projects so we can fetch tree views per project.
        proj_result = await self._handler.execute("list_projects", {})
        projects = proj_result.get("projects", [])
        if not projects:
            await interaction.followup.send("No projects configured.", ephemeral=True)
            return

        lines: list[str] = []
        grand_total = 0

        for proj in projects:
            pid = proj["id"]
            result = await self._handler.execute(
                "list_tasks",
                {
                    "project_id": pid,
                    "display_mode": "tree",
                    "include_completed": False,
                },
            )

            trees = result.get("trees", [])
            total_tasks = result.get("total_tasks", 0)
            if not trees:
                continue

            grand_total += total_tasks
            lines.append(f"\n**📁 {proj.get('name', pid)}** (`{pid}`) — {total_tasks} tasks")

            for tree in trees:
                formatted = tree.get("formatted", "")
                if formatted:
                    lines.append(formatted)

        if not lines:
            await interaction.followup.send("No active tasks across any project.", ephemeral=True)
            return

        header = f"## 🌳 All Tasks — Tree View ({grand_total} total)"
        msg = header + "\n" + "\n".join(lines)
        await _send_long_interaction(msg, interaction.followup.send, ephemeral=True)

    @discord.ui.button(
        label="Projects",
        style=discord.ButtonStyle.secondary,
        emoji="📁",
        row=0,
    )
    async def projects_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("list_projects", {})
        projects = result.get("projects", [])
        if not projects:
            await interaction.followup.send("No projects configured.", ephemeral=True)
            return
        lines = ["**Projects:**"]
        for p in projects:
            status = p.get("status", "?")
            lines.append(f"• **{p['name']}** (`{p['id']}`) — {status}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @discord.ui.button(
        label="Agents",
        style=discord.ButtonStyle.secondary,
        emoji="🤖",
        row=0,
    )
    async def agents_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        # list_agents requires project_id — use active project from handler
        result = await self._handler.execute("list_agents", {})
        if "error" in result:
            await interaction.followup.send(f"⚠️ {result['error']}", ephemeral=True)
            return
        agents = result.get("agents", [])
        if not agents:
            await interaction.followup.send(
                "No agent slots (workspaces) configured.", ephemeral=True
            )
            return
        lines = [f"**Agent Slots** (project: `{result.get('project_id', '?')}`)"]
        for a in agents:
            task_info = ""
            if a.get("current_task_id"):
                title = a.get("current_task_title", "")
                task_info = f" → `{a['current_task_id']}`" + (f" — {title}" if title else "")
            state_emoji = "🔴" if a["state"] == "busy" else "🟢"
            lines.append(f"• {state_emoji} **{a['name']}** ({a['state']}){task_info}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # --- Row 2: Actions ---

    @discord.ui.button(
        label="Notes",
        style=discord.ButtonStyle.secondary,
        emoji="📝",
        row=1,
    )
    async def notes_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        project_id = self._bot.get_project_for_channel(interaction.channel_id)
        if not project_id:
            project_id = getattr(self._handler, "_active_project_id", None)
        if not project_id:
            await interaction.response.send_message(
                "No project context — use this from a project channel.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("list_notes", {"project_id": project_id})
        if "error" in result:
            await interaction.followup.send(
                f"Could not list notes: {result['error']}", ephemeral=True
            )
            return
        notes = result.get("notes", [])
        if not notes:
            await interaction.followup.send(f"No notes in project `{project_id}`.", ephemeral=True)
            return
        lines = [f"**Notes for `{project_id}`:**"]
        for n in notes[:20]:
            lines.append(f"• {n.get('title', n.get('filename', '?'))}")
        if len(notes) > 20:
            lines.append(f"_...and {len(notes) - 20} more_")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @discord.ui.button(
        label="Add Task",
        style=discord.ButtonStyle.success,
        emoji="➕",
        row=1,
    )
    async def add_task_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        project_id = self._bot.get_project_for_channel(interaction.channel_id) or getattr(
            self._handler, "_active_project_id", None
        )
        if not project_id:
            await interaction.response.send_message(
                "No project context — use this from a project channel.",
                ephemeral=True,
            )
            return
        modal = _AddTaskMenuModal(project_id, self._handler)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Restart Task",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        row=1,
    )
    async def restart_task_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Show failed/blocked tasks and let user pick one to restart."""
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute(
            "list_active_tasks_all_projects",
            {"include_completed": False},
        )
        by_project = result.get("by_project", {})
        restartable = []
        for tasks in by_project.values():
            for t in tasks:
                if t["status"] in ("FAILED", "BLOCKED", "PAUSED"):
                    restartable.append(t)

        if not restartable:
            await interaction.followup.send(
                "No failed/blocked/paused tasks to restart.", ephemeral=True
            )
            return

        lines = ["**Restartable tasks** (use `/restart-task <id>`):"]
        for t in restartable[:15]:
            emoji = STATUS_EMOJIS.get(t["status"], "⚪")
            lines.append(f"{emoji} `{t['id']}` — {t['title'][:60]}")
        if len(restartable) > 15:
            lines.append(f"_...and {len(restartable) - 15} more_")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @discord.ui.button(
        label="Toggle Orchestrator",
        style=discord.ButtonStyle.secondary,
        emoji="⏯️",
        row=1,
    )
    async def toggle_orchestrator_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        # Check current state
        status_result = await self._handler.execute("orchestrator_control", {"action": "status"})
        is_paused = status_result.get("status") == "paused"
        # Toggle
        action = "resume" if is_paused else "pause"
        await self._handler.execute("orchestrator_control", {"action": action})
        if action == "pause":
            await interaction.followup.send(
                "⏸ Orchestrator **paused** — no new tasks will be scheduled.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "▶ Orchestrator **resumed** — task scheduling is active.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Rules",
        style=discord.ButtonStyle.secondary,
        emoji="📏",
        row=2,
    )
    async def rules_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Show all rules with an interactive browsing view."""
        await interaction.response.defer(ephemeral=True)
        project_id = self._bot.get_project_for_channel(interaction.channel_id) or getattr(
            self._handler, "_active_project_id", None
        )
        result = await self._handler.execute(
            "browse_rules", {"project_id": project_id} if project_id else {}
        )
        rules = result.get("rules", [])
        if not rules:
            await interaction.followup.send("No rules configured.", ephemeral=True)
            return
        view = RulesListView(rules, self._handler)
        msg = view.build_content()
        await _send_long_interaction(msg, interaction.followup.send, view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# Rules list view with View buttons
# ---------------------------------------------------------------------------

_RULES_PER_PAGE = 10


class _RuleViewButton(discord.ui.Button):
    """Button to view a rule's full content."""

    def __init__(self, rule: dict, handler, *, row: int = 0) -> None:
        rule_type = rule.get("type", "passive")
        emoji = "⚡" if rule_type == "active" else "📖"
        label = (rule.get("name") or rule.get("id", "?"))[:40]
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            emoji=emoji,
            row=row,
        )
        self._rule = rule
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        rule_id = self._rule.get("id")
        result = await self._handler.execute("load_rule", {"id": rule_id})
        if "error" in result:
            await interaction.response.send_message(
                f"⚠️ {result['error']}",
                ephemeral=True,
            )
            return
        view = _RuleContentView(result, self._rule, self._handler)
        await interaction.response.send_message(
            view.build_content(),
            ephemeral=True,
            view=view,
        )


class _RuleContentView(discord.ui.View):
    """Rule content preview with delete action."""

    def __init__(self, result: dict, rule: dict, handler) -> None:
        super().__init__(timeout=300)
        self._result = result
        self._rule = rule
        self._handler = handler
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        fire_btn = discord.ui.Button(
            label="Fire",
            style=discord.ButtonStyle.primary,
            emoji="🔥",
            row=0,
        )
        fire_btn.callback = self._fire_callback
        self.add_item(fire_btn)

        toggle_btn = discord.ui.Button(
            label="Toggle",
            style=discord.ButtonStyle.secondary,
            emoji="⏯️",
            row=0,
        )
        toggle_btn.callback = self._toggle_callback
        self.add_item(toggle_btn)

        delete_btn = discord.ui.Button(
            label="Delete Rule",
            style=discord.ButtonStyle.danger,
            emoji="🗑️",
            row=0,
        )
        delete_btn.callback = self._delete_callback
        self.add_item(delete_btn)

        close_btn = discord.ui.Button(
            label="Close",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        close_btn.callback = self._close_callback
        self.add_item(close_btn)

    def build_content(self) -> str:
        rule_id = self._rule.get("id")
        result = self._result
        rule_type = result.get("type", "passive")
        type_label = "⚡ Active" if rule_type == "active" else "📖 Passive"
        scope = result.get("project_id") or "global"
        hooks = result.get("hooks", [])
        content = result.get("content", "")
        if len(content) > 1500:
            content = content[:1500] + "\n\n_...truncated_"

        lines = [
            f"## 📏 Rule: {self._rule.get('name', rule_id)}",
            f"**ID:** `{rule_id}`",
            f"**Type:** {type_label}",
            f"**Scope:** `{scope}`",
            f"**Hooks:** {len(hooks)}",
            f"**Updated:** {result.get('updated', 'unknown')}",
        ]

        # Show execution info if available
        exec_info = result.get("execution_info", {})
        if exec_info:
            enabled = exec_info.get("enabled")
            if enabled is False:
                lines.append("**Status:** ⏸️ Disabled")
            elif enabled == "mixed":
                lines.append("**Status:** ⚠️ Mixed (some hooks disabled)")
            else:
                lines.append("**Status:** ✅ Enabled")
            hook_count = exec_info.get("hook_count")
            if hook_count is not None:
                lines.append(f"**Active hooks:** {hook_count}")
            last_run = exec_info.get("last_run")
            if last_run:
                lines.append(f"**Last run:** {last_run}")

        lines.append("")
        lines.append(content)
        return "\n".join(lines)

    async def _fire_callback(self, interaction: discord.Interaction) -> None:
        """Fire all hooks for this rule."""
        rule_id = self._rule.get("id")
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("fire_rule", {"id": rule_id})
        if "error" in result:
            await interaction.followup.send(f"\u274c {result['error']}", ephemeral=True)
            return
        fired = result.get("fired", 0)
        skipped = result.get("skipped", 0)
        msg = f"\ud83d\udd25 Rule `{rule_id}` triggered \u2014 **{fired}** hook(s) fired"
        if skipped:
            msg += f", {skipped} already running"
        await interaction.followup.send(msg, ephemeral=True)

    async def _toggle_callback(self, interaction: discord.Interaction) -> None:
        """Toggle rule enabled state."""
        rule_id = self._rule.get("id")
        # Determine current state and flip it
        exec_info = self._result.get("execution_info", {})
        current_enabled = exec_info.get("enabled", True)
        # If mixed or True, disable; if False, enable
        new_enabled = current_enabled is False
        result = await self._handler.execute(
            "toggle_rule", {"id": rule_id, "enabled": new_enabled}
        )
        if "error" in result:
            await interaction.response.send_message(
                f"\u274c {result['error']}", ephemeral=True
            )
            return
        action = result.get("action", "toggled")
        updated = result.get("hooks_updated", 0)
        total = result.get("total_hooks", 0)
        # Update local state so rebuild reflects it
        if "execution_info" not in self._result:
            self._result["execution_info"] = {}
        self._result["execution_info"]["enabled"] = new_enabled
        self._rebuild()
        await interaction.response.edit_message(
            content=self.build_content(),
            view=self,
        )

    async def _delete_callback(self, interaction: discord.Interaction) -> None:
        """Show confirmation before deleting the rule and its hooks."""
        confirm_view = discord.ui.View(timeout=60)
        yes_btn = discord.ui.Button(
            label="Yes, delete it",
            style=discord.ButtonStyle.danger,
        )
        no_btn = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
        )

        async def _confirm(confirm_interaction: discord.Interaction) -> None:
            rule_id = self._rule.get("id")
            result = await self._handler.execute("delete_rule", {"id": rule_id})
            if "error" in result:
                await confirm_interaction.response.edit_message(
                    content=f"❌ {result['error']}",
                    view=None,
                )
                return
            hooks_removed = result.get("hooks_removed", [])
            rule_name = self._rule.get("name", rule_id)
            msg = f"🗑️ Rule **{rule_name}** deleted"
            if hooks_removed:
                msg += f" ({len(hooks_removed)} hook(s) removed)"
            msg += "."
            await confirm_interaction.response.edit_message(
                content=msg,
                view=None,
            )

        async def _cancel(cancel_interaction: discord.Interaction) -> None:
            self._rebuild()
            await cancel_interaction.response.edit_message(
                content=self.build_content(),
                view=self,
            )

        yes_btn.callback = _confirm
        no_btn.callback = _cancel
        confirm_view.add_item(yes_btn)
        confirm_view.add_item(no_btn)

        rule_name = self._rule.get("name", self._rule.get("id"))
        hooks = self._result.get("hooks", [])
        warning = f"⚠️ Are you sure you want to delete rule **{rule_name}**?"
        if hooks:
            warning += f"\n\nThis will also remove **{len(hooks)}** generated hook(s)."
        warning += "\n\nThis cannot be undone."
        await interaction.response.edit_message(
            content=warning,
            view=confirm_view,
        )

    async def _close_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="Rule view closed.",
            view=None,
        )


class RulesListView(discord.ui.View):
    """Interactive rules list with per-rule View buttons and pagination."""

    def __init__(self, rules: list[dict], handler, *, page: int = 0) -> None:
        super().__init__(timeout=300)
        self._rules = rules
        self._handler = handler
        self.page = page
        self.total_pages = max(1, (len(rules) + _RULES_PER_PAGE - 1) // _RULES_PER_PAGE)
        self._rebuild_components()

    def _rebuild_components(self) -> None:
        self.clear_items()
        start = self.page * _RULES_PER_PAGE
        page_rules = self._rules[start : start + _RULES_PER_PAGE]

        # Add view buttons — 3 per row, up to 4 rows
        for i, r in enumerate(page_rules):
            row = i // 3
            if row > 3:
                break  # Reserve row 4 for nav buttons
            self.add_item(_RuleViewButton(r, self._handler, row=row))

        # Navigation row (row 4)
        if self.total_pages > 1:
            prev_btn = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="◀ Prev",
                custom_id="rules:page:prev",
                disabled=(self.page == 0),
                row=4,
            )
            prev_btn.callback = self._prev_page
            self.add_item(prev_btn)

            next_btn = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="Next ▶",
                custom_id="rules:page:next",
                disabled=(self.page >= self.total_pages - 1),
                row=4,
            )
            next_btn.callback = self._next_page
            self.add_item(next_btn)

    def build_embed(self) -> discord.Embed:
        """Build an embed with table-formatted rules list."""
        if not self._rules:
            return make_embed(
                EmbedStyle.INFO,
                "Rules",
                description="No rules configured.",
            )

        start = self.page * _RULES_PER_PAGE
        page_rules = self._rules[start : start + _RULES_PER_PAGE]

        # Collect row data
        rows: list[tuple[str, str, str, str, str, str]] = []
        for r in page_rules:
            name = r.get("name") or r.get("id", "?")
            rule_type = r.get("type", "passive")
            scope = r.get("project_id") or "global"
            hooks = str(r.get("hook_count", 0))

            enabled = r.get("enabled")
            if enabled is False:
                status = "off"
            elif enabled == "mixed":
                status = "mixed"
            else:
                status = "on"

            last_run = r.get("last_run") or ""
            rows.append((name, rule_type, scope, hooks, status, last_run))

        has_last_run = any(r[5] for r in rows)

        # Compute column widths (capped to keep table compact)
        name_w = min(max(4, *(len(r[0]) for r in rows)), 20)
        type_w = 7  # "passive" is the longest
        scope_w = min(max(5, *(len(r[2]) for r in rows)), 14)
        hooks_w = 5
        status_w = 6

        # Build header + separator
        cols = [
            f"{'Name':<{name_w}}",
            f"{'Type':<{type_w}}",
            f"{'Scope':<{scope_w}}",
            f"{'Hooks':>{hooks_w}}",
            f"{'Status':<{status_w}}",
        ]
        if has_last_run:
            cols.append("Last Run")
        header = "  ".join(cols)
        sep = "─" * len(header)

        # Build data rows
        lines = [header, sep]
        for name, rule_type, scope, hooks, status, last_run in rows:
            t_name = name[:name_w] if len(name) > name_w else name
            t_scope = scope[:scope_w] if len(scope) > scope_w else scope
            parts = [
                f"{t_name:<{name_w}}",
                f"{rule_type:<{type_w}}",
                f"{t_scope:<{scope_w}}",
                f"{hooks:>{hooks_w}}",
                f"{status:<{status_w}}",
            ]
            if has_last_run:
                parts.append(last_run or "—")
            lines.append("  ".join(parts))

        table_text = "```\n" + "\n".join(lines) + "\n```"

        # Legend for status values
        legend = "⚡ active · 📖 passive · ✅ on · ⏸️ off · ⚠️ mixed"

        # Footer
        footer_parts = []
        if self.total_pages > 1:
            footer_parts.append(f"Page {self.page + 1}/{self.total_pages}")
        footer_parts.append("AgentQueue")

        return make_embed(
            EmbedStyle.INFO,
            f"Rules ({len(self._rules)})",
            description=f"{table_text}\n{legend}",
            footer=" · ".join(footer_parts),
        )

    async def _prev_page(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._rebuild_components()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            content=None,
            view=self,
        )

    async def _next_page(self, interaction: discord.Interaction) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        self._rebuild_components()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            content=None,
            view=self,
        )


async def _async_git_output(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: int = 10,
) -> str:
    """Run a git command asynchronously and return stripped stdout.

    Uses ``asyncio.create_subprocess_exec`` to avoid blocking the event loop
    (unlike ``asyncio.to_thread(subprocess.run, ...)`` which consumes a thread-
    pool slot).  Raises on non-zero exit or timeout.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ""
    return stdout.decode(errors="replace").strip()


def setup_commands(bot: commands.Bot) -> None:
    """Register all slash commands on the bot."""

    # Shortcut — the shared command handler is owned by the Supervisor.
    # Every slash command calls `handler.execute(name, args)` for its
    # business logic and only handles Discord-specific formatting here.
    handler = bot.agent.handler

    # ---------------------------------------------------------------------------
    # Channel→project resolution helper
    # ---------------------------------------------------------------------------

    async def _resolve_project_from_context(
        interaction: discord.Interaction,
        project_id: str | None,
    ) -> str | None:
        """Resolve *project_id*, falling back to the channel→project mapping.

        If *project_id* was explicitly supplied by the user it is returned
        unchanged.  Otherwise the interaction's channel is checked against
        the bot's reverse channel-to-project lookup so that commands run
        inside a project-specific channel automatically inherit that project.

        For threads inside a project channel, the parent channel is also
        checked so that slash commands invoked from threads still resolve
        to the correct project.
        """
        if project_id is not None:
            return project_id
        # Direct channel lookup
        result = bot.get_project_for_channel(interaction.channel_id)
        if result:
            return result
        # If the interaction is in a thread, check the parent channel
        channel = interaction.channel
        parent_id = getattr(channel, "parent_id", None)
        if parent_id:
            return bot.get_project_for_channel(parent_id)
        return None

    _NO_PROJECT_MSG = (
        "Could not determine project — please provide `project_id` "
        "or run this command from a project channel."
    )

    # ---------------------------------------------------------------------------
    # Shared task-detail helper
    # ---------------------------------------------------------------------------

    async def _format_task_detail(task_id: str) -> str | None:
        """Fetch and format full task details. Returns None if task not found."""
        result = await handler.execute("get_task", {"task_id": task_id})
        if "error" in result:
            return None

        # Build type tag prefix
        tags = []
        if result.get("is_plan_subtask"):
            tags.append(TYPE_TAGS["plan_subtask"])
        if result.get("subtasks"):
            tags.append(TYPE_TAGS["has_subtasks"])
        if result.get("pr_url"):
            tags.append(TYPE_TAGS["has_pr"])
        if result.get("requires_approval"):
            tags.append(TYPE_TAGS["approval_required"])
        tag_str = " ".join(tags) + " " if tags else ""

        status = result["status"]
        emoji = STATUS_EMOJIS.get(status, "⚪")

        lines = [
            f"## {tag_str}Task `{result['id']}`",
            f"**Title:** {result['title']}",
            f"**Status:** {emoji} {status}",
            f"**Project:** `{result['project_id']}`",
            f"**Priority:** {result['priority']}",
        ]
        if result.get("assigned_agent"):
            lines.append(f"**Agent:** {result['assigned_agent']}")
        if result.get("retry_count"):
            lines.append(f"**Retries:** {result['retry_count']} / {result['max_retries']}")
        if result.get("parent_task_id"):
            lines.append(f"**Parent Task:** `{result['parent_task_id']}`")
        if result.get("pr_url"):
            lines.append(f"**PR:** {result['pr_url']}")

        # Dependency visualization
        depends_on = result.get("depends_on", [])
        if depends_on:
            lines.append("\n**Depends On:**")
            for dep in depends_on:
                dep_emoji = STATUS_EMOJIS.get(dep["status"], "⚪")
                lines.append(f"  {dep_emoji} `{dep['id']}` — {dep['title']} ({dep['status']})")

        blocks = result.get("blocks", [])
        if blocks:
            lines.append("\n**Blocks:**")
            for blk in blocks:
                blk_emoji = STATUS_EMOJIS.get(blk["status"], "⚪")
                lines.append(f"  {blk_emoji} `{blk['id']}` — {blk['title']} ({blk['status']})")

        # Subtask tree view
        subtasks = result.get("subtasks", [])
        if subtasks:
            completed = sum(1 for st in subtasks if st["status"] == "COMPLETED")
            bar = progress_bar(completed, len(subtasks), width=8)
            lines.append(f"\n**Subtasks:** {bar}")
            for i, st in enumerate(subtasks):
                is_last = i == len(subtasks) - 1
                st_emoji = STATUS_EMOJIS.get(st["status"], "⚪")
                connector = "└── " if is_last else "├── "
                lines.append(f"  {connector}{st_emoji} `{st['id']}` — {st['title']}")

        desc = result.get("description", "")
        if desc:
            desc = desc if len(desc) <= 800 else desc[:800] + "..."
            lines.append(f"\n**Description:**\n{desc}")
        return "\n".join(lines)

    # ---------------------------------------------------------------------------
    # Interactive UI components for task lists
    # ---------------------------------------------------------------------------

    _STATUS_ORDER = [
        "IN_PROGRESS",
        "ASSIGNED",
        "READY",
        "DEFINED",
        "PAUSED",
        "WAITING_INPUT",
        "AWAITING_APPROVAL",
        "AWAITING_PLAN_APPROVAL",
        "FAILED",
        "BLOCKED",
        "COMPLETED",
    ]
    _STATUS_DISPLAY: dict[str, str] = {
        "DEFINED": "Defined",
        "READY": "Ready",
        "ASSIGNED": "Assigned",
        "IN_PROGRESS": "In Progress",
        "COMPLETED": "Completed",
        "PAUSED": "Paused",
        "WAITING_INPUT": "Waiting Input",
        "FAILED": "Failed",
        "BLOCKED": "Blocked",
        "AWAITING_APPROVAL": "Awaiting Approval",
        "AWAITING_PLAN_APPROVAL": "Awaiting Plan Approval",
    }
    # Sections expanded by default (active/actionable states)
    _DEFAULT_EXPANDED = {
        "IN_PROGRESS",
        "ASSIGNED",
        "READY",
        "FAILED",
        "BLOCKED",
        "PAUSED",
        "WAITING_INPUT",
        "AWAITING_APPROVAL",
        "AWAITING_PLAN_APPROVAL",
    }
    _MAX_TASKS_PER_SECTION = 15

    class StatusToggleButton(discord.ui.Button):
        """Toggles a status section between expanded and collapsed."""

        def __init__(self, status: str, count: int, is_expanded: bool) -> None:
            emoji = _STATUS_EMOJIS.get(status, "⚪")
            display = _STATUS_DISPLAY.get(status, status)
            label = f"{display} ({count})"
            style = discord.ButtonStyle.primary if is_expanded else discord.ButtonStyle.secondary
            super().__init__(style=style, label=label, emoji=emoji)
            self.status = status

        async def callback(self, interaction: discord.Interaction) -> None:
            view: TaskReportView = self.view
            if self.status in view.expanded:
                view.expanded.discard(self.status)
            else:
                view.expanded.add(self.status)
            view._rebuild_components()
            content = view.build_content()
            await interaction.response.edit_message(content=content, view=view)

    class TaskDetailSelect(discord.ui.Select):
        """Dropdown to pick a task and view its full details."""

        def __init__(self, options: list[discord.SelectOption]) -> None:
            super().__init__(
                placeholder="Select a task for details...",
                options=options,
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            task_id = self.values[0]
            await interaction.response.defer(ephemeral=True)
            detail = await _format_task_detail(task_id)
            if detail is None:
                await interaction.followup.send(f"Task `{task_id}` not found.", ephemeral=True)
            else:
                await interaction.followup.send(detail, ephemeral=True)

    class _TasksRefreshButton(discord.ui.Button):
        """Refreshes the tasks report by re-fetching task data."""

        def __init__(self, handler, project_id: str | None) -> None:
            super().__init__(
                style=discord.ButtonStyle.secondary,
                label="Refresh",
                emoji="🔄",
                row=4,
            )
            self._handler = handler
            self._project_id = project_id

        async def callback(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            args: dict = {
                "include_completed": True,
                "display_mode": "flat",
            }
            if self._project_id:
                args["project_id"] = self._project_id
            result = await self._handler.execute("list_tasks", args)
            if "error" in result:
                return
            tasks = result.get("tasks", [])
            view: TaskReportView = self.view
            # Rebuild task groupings
            tasks_by_status: dict[str, list] = {}
            for t in tasks:
                tasks_by_status.setdefault(t["status"], []).append(t)
            view.tasks_by_status = tasks_by_status
            view.total = result.get("total", len(tasks))
            view._all_tasks = tasks
            # Refresh thread URLs
            view._thread_urls = bot.get_task_thread_urls()
            # Refresh header lines if this is a status view
            if view._show_agent_header:
                status_result = await self._handler.execute("get_status", {})
                header: list[str] = []
                if status_result.get("orchestrator_paused"):
                    header.append("⏸ **Orchestrator is PAUSED** — scheduling suspended")
                agents = status_result.get("agents", [])
                if agents:
                    header.append("**Agents:**")
                    for a in agents:
                        working_on = a.get("working_on")
                        if working_on:
                            header.append(
                                f"• **{a['name']}** ({a['state']}) → "
                                f"**{working_on['project_id']}** / "
                                f"`{working_on['task_id']}` — {working_on['title']}"
                            )
                        else:
                            header.append(f"• **{a['name']}** ({a['state']})")
                view._header_lines = header
            # Rebuild subtask maps
            view._subtask_ids = set()
            view._subtask_map = {}
            for t in tasks:
                pid = t.get("parent_task_id")
                if pid:
                    view._subtask_ids.add(t["id"])
                    view._subtask_map.setdefault(pid, []).append(t)
            view._rebuild_components()
            await interaction.edit_original_response(
                content=view.build_content(),
                view=view,
            )

    # StatusReportView removed — /status now uses TaskReportView with
    # agent info in header_lines for a consolidated view.

    class TaskReportView(discord.ui.View):
        """Grouped task report with collapsible status sections and detail select.

        Displays tasks grouped by status with tree-view for parent/subtask
        relationships and type tags for quick identification.
        """

        def __init__(
            self,
            tasks_by_status: dict[str, list],
            total: int,
            *,
            all_tasks: list | None = None,
            handler=None,
            project_id: str | None = None,
            thread_urls: dict[str, str] | None = None,
            header_lines: list[str] | None = None,
        ) -> None:
            super().__init__(timeout=600)
            self.tasks_by_status = tasks_by_status
            self.total = total
            self._handler = handler
            self._project_id = project_id
            self._thread_urls: dict[str, str] = thread_urls or {}
            self._header_lines: list[str] = header_lines or []
            self._show_agent_header: bool = header_lines is not None
            # Build parent→subtask lookup for tree view
            self._all_tasks = all_tasks or []
            self._subtask_ids: set[str] = set()
            self._subtask_map: dict[str, list] = {}  # parent_id → [child tasks]
            for t in self._all_tasks:
                pid = t.get("parent_task_id")
                if pid:
                    self._subtask_ids.add(t["id"])
                    self._subtask_map.setdefault(pid, []).append(t)
            self.expanded: set[str] = set()
            for status in _DEFAULT_EXPANDED:
                if status in tasks_by_status:
                    self.expanded.add(status)
            # If nothing expanded, expand the first non-empty section
            if not self.expanded:
                for status in _STATUS_ORDER:
                    if status in tasks_by_status:
                        self.expanded.add(status)
                        break
            self._rebuild_components()

        def _get_type_tag(self, task: dict) -> str:
            """Return a type tag emoji based on task properties."""
            tags = []
            if task.get("is_plan_subtask"):
                tags.append(TYPE_TAGS["plan_subtask"])
            if task["id"] in self._subtask_map:
                tags.append(TYPE_TAGS["has_subtasks"])
            if task.get("pr_url"):
                tags.append(TYPE_TAGS["has_pr"])
            return "".join(tags)

        def _rebuild_components(self) -> None:
            self.clear_items()
            # Toggle buttons for each status that has tasks
            for status in _STATUS_ORDER:
                if status not in self.tasks_by_status:
                    continue
                count = len(self.tasks_by_status[status])
                is_expanded = status in self.expanded
                self.add_item(StatusToggleButton(status, count, is_expanded))
            # Select dropdown with tasks from expanded sections
            options: list[discord.SelectOption] = []
            for status in _STATUS_ORDER:
                if status not in self.expanded:
                    continue
                for t in self.tasks_by_status.get(status, []):
                    if len(options) >= 25:
                        break
                    title = t["title"]
                    tag = self._get_type_tag(t)
                    if tag:
                        title = f"{tag} {title}"
                    if len(title) > 95:
                        title = title[:92] + "..."
                    options.append(
                        discord.SelectOption(
                            label=title,
                            value=t["id"],
                            description=t["id"],
                        )
                    )
                if len(options) >= 25:
                    break
            if options:
                self.add_item(TaskDetailSelect(options))
            # Add refresh button if handler is available
            if self._handler is not None:
                self.add_item(_TasksRefreshButton(self._handler, self._project_id))

        def _format_task_line(self, t: dict, *, show_children: bool = True) -> list[str]:
            """Format a task with optional tree-view subtasks."""
            tag = self._get_type_tag(t)
            tag_str = f"{tag} " if tag else ""
            # Add thread link for active/completed tasks
            thread_url = self._thread_urls.get(t["id"])
            thread_link = f" — [thread]({thread_url})" if thread_url else ""
            lines = [f"{tag_str}**{t['title']}** `{t['id']}`{thread_link}"]

            # Show inline subtask count if parent has children
            children = self._subtask_map.get(t["id"], [])
            if children and show_children:
                completed = sum(1 for c in children if c.get("status") == "COMPLETED")
                total = len(children)
                if total <= 4:
                    # Show individual subtasks in tree view
                    for i, child in enumerate(children):
                        is_last = i == total - 1
                        child_emoji = _STATUS_EMOJIS.get(child.get("status", ""), "⚪")
                        connector = "└── " if is_last else "├── "
                        child_tag = (
                            TYPE_TAGS["plan_subtask"] + " " if child.get("is_plan_subtask") else ""
                        )
                        lines.append(
                            f"  {connector}{child_emoji} {child_tag}{child['title']} `{child['id']}`"
                        )
                else:
                    # Compact subtask summary
                    bar = progress_bar(completed, total, width=6)
                    lines.append(f"  └── {total} subtasks: {bar}")
            return lines

        def build_content(self) -> str:
            lines: list[str] = []
            # Add optional header (e.g. agent status, orchestrator state)
            if self._header_lines:
                lines.extend(self._header_lines)
                lines.append("")
            # Add progress summary at top — use _all_tasks for accurate totals
            # since tasks_by_status may be filtered (e.g. completed hidden).
            if self._all_tasks:
                all_count = len(self._all_tasks)
                completed_count = sum(1 for t in self._all_tasks if t.get("status") == "COMPLETED")
            else:
                all_count = sum(len(v) for v in self.tasks_by_status.values())
                completed_count = len(self.tasks_by_status.get("COMPLETED", []))
            if all_count > 0:
                bar = progress_bar(completed_count, all_count, width=10)
                lines.append(f"**Progress:** {bar}")
                # Show counts for all non-completed statuses that have tasks.
                # Ordered by visual priority: active work → needs attention → queued.
                _STAT_LABELS: list[tuple[str, str]] = [
                    ("IN_PROGRESS", "In Progress"),
                    ("ASSIGNED", "Assigned"),
                    ("AWAITING_APPROVAL", "Awaiting Approval"),
                    ("AWAITING_PLAN_APPROVAL", "Awaiting Plan Approval"),
                    ("WAITING_INPUT", "Waiting Input"),
                    ("PAUSED", "Paused"),
                    ("FAILED", "Failed"),
                    ("BLOCKED", "Blocked"),
                    ("READY", "Ready"),
                    ("DEFINED", "Defined"),
                ]
                stat_parts: list[str] = []
                for status_val, label in _STAT_LABELS:
                    cnt = len(self.tasks_by_status.get(status_val, []))
                    if cnt > 0:
                        emoji = _STATUS_EMOJIS.get(status_val, "⚪")
                        stat_parts.append(f"{emoji} {cnt} {label}")
                if stat_parts:
                    lines.append(" · ".join(stat_parts))
                lines.append("")

            for status in _STATUS_ORDER:
                if status not in self.tasks_by_status:
                    continue
                tasks = self.tasks_by_status[status]
                emoji = _STATUS_EMOJIS.get(status, "⚪")
                display = _STATUS_DISPLAY.get(status, status)
                count = len(tasks)
                if status in self.expanded:
                    lines.append(f"### {emoji} {display} ({count})")
                    shown = tasks[:_MAX_TASKS_PER_SECTION]
                    for t in shown:
                        # Skip subtasks that are shown under their parent
                        if t["id"] in self._subtask_ids:
                            # Only skip if parent is in the same expanded section
                            parent_id = t.get("parent_task_id")
                            parent_in_section = any(
                                pt["id"] == parent_id for pt in self.tasks_by_status.get(status, [])
                            )
                            if parent_in_section:
                                continue
                        task_lines = self._format_task_line(t)
                        lines.extend(task_lines)
                    if count > _MAX_TASKS_PER_SECTION:
                        lines.append(f"_...and {count - _MAX_TASKS_PER_SECTION} more_")
                    lines.append("")
                else:
                    lines.append(f"{emoji} **{display}** ({count})")
            content = "\n".join(lines)
            # Trim if over Discord's 2000-char message limit.
            # Progressively reduce the per-section cap until content fits.
            if len(content) > 1950:
                for cap in (8, 4, 2):
                    lines = []
                    for status in _STATUS_ORDER:
                        if status not in self.tasks_by_status:
                            continue
                        tasks = self.tasks_by_status[status]
                        emoji = _STATUS_EMOJIS.get(status, "⚪")
                        display = _STATUS_DISPLAY.get(status, status)
                        count = len(tasks)
                        if status in self.expanded:
                            lines.append(f"### {emoji} {display} ({count})")
                            for t in tasks[:cap]:
                                tag = self._get_type_tag(t)
                                tag_str = f"{tag} " if tag else ""
                                lines.append(f"{tag_str}**{t['title']}** `{t['id']}`")
                            if count > cap:
                                lines.append(f"_...and {count - cap} more_")
                            lines.append("")
                        else:
                            lines.append(f"{emoji} **{display}** ({count})")
                    content = "\n".join(lines)
                    if len(content) <= 1950:
                        break
                # Final safety net: hard-truncate if still over limit
                if len(content) > 1950:
                    content = content[:1947] + "..."
            return content

    # ---------------------------------------------------------------------------
    # Shared formatting helpers
    # ---------------------------------------------------------------------------

    # Status visual mappings -- canonical definitions live in
    # src/discord/embeds.py; local aliases for backward compatibility
    # with the many references inside this function's nested closures.
    _STATUS_COLORS = STATUS_COLORS
    _STATUS_EMOJIS = STATUS_EMOJIS

    async def _send_long(interaction, text: str, *, followup: bool = False):
        """Send a potentially long response, splitting or attaching as file."""
        send = interaction.followup.send if followup else interaction.response.send_message
        if len(text) <= 2000:
            await send(text)
        elif len(text) <= 6000:
            chunks, current = [], ""
            for line in text.split("\n"):
                candidate = current + ("\n" if current else "") + line
                if len(candidate) > 2000:
                    if current:
                        chunks.append(current)
                    current = line
                else:
                    current = candidate
            if current:
                chunks.append(current)
            for chunk in chunks:
                await interaction.followup.send(chunk)
        else:
            file = discord.File(
                fp=io.BytesIO(text.encode("utf-8")),
                filename="response.md",
            )
            preview = text[:300].rstrip() + "\n\n*Full output attached.*"
            await send(preview, file=file)

    async def _send_error(
        interaction,
        message: str,
        *,
        followup: bool = False,
        ephemeral: bool = True,
    ):
        """Send an error response as a rich embed.

        Replaces the plain-text ``f"Error: {msg}"`` pattern with a consistent
        red embed, improving visual clarity in the Discord channel.
        """
        embed = error_embed("Error", description=message)
        if followup:
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    async def _send_success(
        interaction,
        title: str,
        *,
        description: str | None = None,
        fields: list[tuple[str, str, bool]] | None = None,
        followup: bool = False,
        ephemeral: bool = False,
        result: dict | None = None,
    ):
        """Send a success response as a rich green embed.

        If *result* contains a ``"warning"`` key the warning text is appended
        to the embed *description* so it remains visible.
        """
        if result and result.get("warning"):
            warning_text = f"⚠️ {result['warning']}"
            description = f"{description}\n\n{warning_text}" if description else warning_text
        embed = success_embed(title, description=description, fields=fields)
        if followup:
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    async def _send_info(
        interaction,
        title: str,
        *,
        description: str | None = None,
        fields: list[tuple[str, str, bool]] | None = None,
        followup: bool = False,
        ephemeral: bool = False,
    ):
        """Send an informational response as a rich blue embed."""
        embed = info_embed(title, description=description, fields=fields)
        if followup:
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    async def _send_warning(
        interaction,
        title: str,
        *,
        description: str | None = None,
        fields: list[tuple[str, str, bool]] | None = None,
        followup: bool = False,
        ephemeral: bool = False,
    ):
        """Send a warning response as a rich amber embed."""
        embed = warning_embed(title, description=description, fields=fields)
        if followup:
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    def _with_warning(msg: str, result: dict) -> str:
        """Append an in-progress task warning to *msg* if present in *result*.

        .. deprecated::
            Prefer passing ``result=result`` to ``_send_success()`` instead.
        """
        warning = result.get("warning")
        if warning:
            return f"{msg}\n\n⚠️ {warning}"
        return msg

    # ===================================================================
    # SYSTEM / STATUS COMMANDS
    # ===================================================================

    @bot.tree.command(name="status", description="Show system status overview")
    async def status_command(interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            # Resolve project from channel context (like /tasks does)
            project_id = await _resolve_project_from_context(interaction, None)

            # Get agent/orchestrator status for the header (scoped to project)
            status_args: dict = {}
            if project_id:
                status_args["project_id"] = project_id
            status_result = await handler.execute("get_status", status_args)

            # Build header lines from status info
            header_lines: list[str] = []
            if status_result.get("orchestrator_paused"):
                header_lines.append("⏸ **Orchestrator is PAUSED** — scheduling suspended")

            agents = status_result.get("agents", [])
            if agents:
                header_lines.append("**Agents:**")
                for a in agents:
                    working_on = a.get("working_on")
                    if working_on:
                        header_lines.append(
                            f"• **{a['name']}** ({a['state']}) → "
                            f"**{working_on['project_id']}** / "
                            f"`{working_on['task_id']}` — {working_on['title']}"
                        )
                    else:
                        header_lines.append(f"• **{a['name']}** ({a['state']})")

            # Fetch tasks using the same backend as /tasks
            args: dict = {
                "include_completed": True,
                "display_mode": "flat",
            }
            if project_id:
                args["project_id"] = project_id

            result = await handler.execute("list_tasks", args)

            if "error" in result:
                await interaction.followup.send(
                    embed=error_embed("Error", description=result["error"]),
                )
                return

            tasks = result.get("tasks", [])

            # Get thread URLs from the bot
            thread_urls = bot.get_task_thread_urls()

            if not tasks:
                # Still show header even if no tasks
                content = "\n".join(header_lines) if header_lines else "No tasks found."
                await interaction.followup.send(content)
                return

            # Group tasks by status for the interactive report view
            tasks_by_status: dict[str, list] = {}
            for t in tasks:
                tasks_by_status.setdefault(t["status"], []).append(t)
            total = result.get("total", len(tasks))
            view_widget = TaskReportView(
                tasks_by_status,
                total,
                all_tasks=tasks,
                handler=handler,
                project_id=project_id,
                thread_urls=thread_urls,
                header_lines=header_lines,
            )
            content = view_widget.build_content()
            await interaction.followup.send(content, view=view_widget)
        except Exception as e:
            logger.error("Error in /status: %s", e, exc_info=True)
            try:
                await interaction.followup.send(
                    embed=error_embed("Error", description=f"Failed to get status: {e}"),
                )
            except Exception:
                pass

    @bot.tree.command(name="projects", description="List all projects")
    async def projects_command(interaction: discord.Interaction):
        result = await handler.execute("list_projects", {})
        projects = result.get("projects", [])
        if not projects:
            await _send_info(interaction, "No Projects", description="No projects configured.")
            return
        lines = []
        for p in projects:
            line = f"• **{p['name']}** (`{p['id']}`) — {p['status']}, weight={p['credit_weight']}"
            if p.get("discord_channel_id"):
                line += f" | <#{p['discord_channel_id']}>"
            lines.append(line)
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="agents", description="List agent slots for a project")
    @app_commands.describe(project_id="Project ID (optional if active project is set)")
    async def agents_command(
        interaction: discord.Interaction,
        project_id: str | None = None,
    ):
        args: dict = {}
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("list_agents", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        agents = result.get("agents", [])
        if not agents:
            await _send_info(
                interaction,
                "No Agent Slots",
                description=f"No workspaces configured for project `{result.get('project_id', '?')}`.",
            )
            return
        lines = [f"**Agent Slots** — project `{result.get('project_id', '?')}`"]
        for a in agents:
            task_info = ""
            if a.get("current_task_id"):
                title = a.get("current_task_title", "")
                task_info = f" → `{a['current_task_id']}`" + (f" — {title}" if title else "")
            state_emoji = "🔴" if a["state"] == "busy" else "🟢"
            lines.append(f"• {state_emoji} **{a['name']}** ({a['state']}){task_info}")
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(
        name="usage",
        description="Show Claude Code usage — active sessions, tokens, rate limits",
    )
    async def usage_command(interaction: discord.Interaction):
        await interaction.response.defer()
        result = await handler.execute("claude_usage", {})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        lines = ["## 📊 Claude Code Usage"]

        # Subscription info
        sub = result.get("subscription", "unknown")
        tier = result.get("rate_limit_tier", "unknown")
        lines.append(f"**Plan:** {sub} — **Tier:** `{tier}`")

        # Rate-limit status (from API probe)
        rl = result.get("rate_limit", {})
        if rl and "error" not in rl:
            status = rl.get("status", "unknown")
            status_emoji = {"allowed": "🟢", "allowed_warning": "🟡", "rejected": "🔴"}.get(
                status, "⚪"
            )
            lines.append(f"\n### Rate Limit: {status_emoji} {status}")

            for k, v in sorted(rl.items()):
                if k.endswith("_pct"):
                    claim = k.replace("_pct", "").replace("-", " ").title()
                    try:
                        pct_val = float(v.rstrip("%"))
                        bar = progress_bar(int(pct_val), 100, width=12)
                        lines.append(f"{bar} **{v}** — {claim}")
                    except ValueError:
                        lines.append(f"• **{claim}:** {v}")

            reset_human = rl.get("reset_human")
            resets_in = rl.get("resets_in")
            if reset_human:
                reset_line = f"⏰ **Resets:** {reset_human}"
                if resets_in:
                    reset_line += f" (in {resets_in})"
                lines.append(reset_line)
        elif rl_err := result.get("rate_limit_error"):
            lines.append(f"\n⚠️ Rate limit probe failed: {rl_err}")

        # Active sessions with live token counts
        sessions = result.get("active_sessions", [])
        if sessions:
            total_active = result.get("active_total_tokens", 0)
            lines.append(f"\n### Active Sessions ({len(sessions)}) — {total_active:,} tokens total")
            for s in sorted(sessions, key=lambda x: -x["total_tokens"]):
                u = s["usage"]
                lines.append(
                    f"• **{s['project']}** (since {s['started']}) — "
                    f"{s['total_tokens']:,} tokens "
                    f"({u['input']:,} in · {u['output']:,} out · "
                    f"{u['cache_read']:,} cache · {u['cache_create']:,} create)"
                )

        # Cumulative model usage from stats-cache
        mu = result.get("model_usage", {})
        if mu:
            stats_date = result.get("stats_date", "?")
            lines.append(f"\n### All-Time Token Usage (as of {stats_date})")
            for model, data in sorted(mu.items(), key=lambda x: -x[1]["total"]):
                total = data["total"]
                lines.append(
                    f"• **{model}:** {total:,} total "
                    f"({data['input']:,} in · {data['output']:,} out · "
                    f"{data['cache_read']:,} cache-read)"
                )

        if stats_err := result.get("stats_error"):
            lines.append(f"\n⚠️ Stats: {stats_err}")

        msg = "\n".join(lines)
        await _send_long_interaction(msg, interaction.followup.send)

    @bot.tree.command(name="events", description="Show recent system events")
    @app_commands.describe(limit="Number of events to show (default 10)")
    async def events_command(interaction: discord.Interaction, limit: int = 10):
        result = await handler.execute("get_recent_events", {"limit": limit})
        events = result.get("events", [])
        if not events:
            await _send_info(interaction, "No Events", description="No recent events.")
            return
        lines = ["## Recent Events"]
        for evt in events:
            ts = evt.get("timestamp", "")
            etype = evt.get("event_type", "unknown")
            project = evt.get("project_id", "")
            task = evt.get("task_id", "")
            parts = [f"**{etype}**"]
            if project:
                parts.append(f"project=`{project}`")
            if task:
                parts.append(f"task=`{task}`")
            if ts:
                parts.append(f"at {ts}")
            lines.append(f"• {' — '.join(parts)}")
        msg = "\n".join(lines)
        await _send_long_interaction(msg, interaction.response.send_message)

    # ===================================================================
    # PROJECT COMMANDS
    # ===================================================================

    @bot.tree.command(
        name="new-project",
        description="Create a new project with an interactive wizard",
    )
    async def new_project_command(interaction: discord.Interaction):
        """Launch the interactive project-creation wizard.

        Opens a modal to collect project name, description, tech stack,
        and branch, then guides the user through repo setup and workspace
        selection before creating everything automatically.
        """
        modal = ProjectInfoModal(handler, bot)
        await interaction.response.send_modal(modal)

    @bot.tree.command(name="edit-project", description="Edit a project's settings")
    @app_commands.describe(
        project_id="Project ID",
        name="New name (optional)",
        credit_weight="New scheduling weight (optional)",
        max_concurrent_agents="New max agents (optional)",
        budget_limit="Token budget limit (optional, 0 to clear)",
        channel="Discord channel to link to this project (optional)",
    )
    async def edit_project_command(
        interaction: discord.Interaction,
        project_id: str,
        name: str | None = None,
        credit_weight: float | None = None,
        max_concurrent_agents: int | None = None,
        budget_limit: int | None = None,
        channel: discord.TextChannel | None = None,
    ):
        args: dict = {"project_id": project_id}
        if name is not None:
            args["name"] = name
        if credit_weight is not None:
            args["credit_weight"] = credit_weight
        if max_concurrent_agents is not None:
            args["max_concurrent_agents"] = max_concurrent_agents
        if budget_limit is not None:
            args["budget_limit"] = budget_limit if budget_limit > 0 else None
        if channel is not None:
            args["discord_channel_id"] = str(channel.id)
        result = await handler.execute("edit_project", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        fields = ", ".join(result.get("fields", []))
        desc = f"Project `{project_id}` updated: {fields}"
        if channel is not None:
            desc += f"\nChannel: {channel.mention}"
            bot.update_project_channel(project_id, channel)
        await _send_success(
            interaction,
            "Project Updated",
            description=desc,
        )

    @bot.tree.command(
        name="set-default-branch",
        description="Set the default branch for a project (creates it if needed)",
    )
    @app_commands.describe(
        project_id="Project ID",
        branch="Branch name to use as default (e.g. dev, main, master)",
    )
    async def set_default_branch_command(
        interaction: discord.Interaction,
        project_id: str,
        branch: str,
    ):
        await interaction.response.defer()
        result = await handler.execute(
            "set_default_branch",
            {"project_id": project_id, "branch": branch},
        )
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        desc = (
            f"Project `{project_id}` default branch set to `{result['default_branch']}`"
            f"\n(was `{result['previous_branch']}`)"
        )
        if result.get("branch_created"):
            desc += f"\n\n🌿 Branch `{branch}` was created on the remote."
        await _send_success(
            interaction,
            "Default Branch Updated",
            description=desc,
            followup=True,
        )

    @bot.tree.command(name="delete-project", description="Delete a project and all its data")
    @app_commands.describe(
        project_id="Project ID to delete",
        archive_channels="Archive the project's Discord channels instead of leaving them (default: False)",
    )
    @app_commands.choices(
        archive_channels=[
            app_commands.Choice(name="Yes – archive channels", value=1),
            app_commands.Choice(name="No – leave channels as-is", value=0),
        ]
    )
    async def delete_project_command(
        interaction: discord.Interaction,
        project_id: str,
        archive_channels: app_commands.Choice[int] | None = None,
    ):
        do_archive = archive_channels is not None and archive_channels.value == 1
        result = await handler.execute(
            "delete_project",
            {"project_id": project_id, "archive_channels": do_archive},
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return

        # The command handler's _on_project_deleted callback already cleared
        # the bot's in-memory caches.  Now handle optional channel archival.
        archived: list[str] = []
        if do_archive and result.get("channel_ids"):
            guild = interaction.guild
            if guild:
                for ch_type, ch_id in result["channel_ids"].items():
                    channel = guild.get_channel(int(ch_id))
                    if channel and isinstance(channel, discord.TextChannel):
                        try:
                            # Archive by moving to read-only: deny Send Messages
                            # for @everyone while preserving history.
                            overwrite = channel.overwrites_for(guild.default_role)
                            overwrite.send_messages = False
                            await channel.set_permissions(
                                guild.default_role,
                                overwrite=overwrite,
                                reason=f"Archived: project {project_id} deleted",
                            )
                            await channel.edit(
                                name=f"archived-{channel.name}",
                                reason=f"Archived: project {project_id} deleted",
                            )
                            archived.append(f"#{channel.name} ({ch_type})")
                        except discord.Forbidden:
                            from src.discord.rate_guard import get_tracker

                            get_tracker().record(403)
                            archived.append(
                                f"#{channel.name} ({ch_type}) — no permission to archive"
                            )
                        except discord.HTTPException as exc:
                            from src.discord.rate_guard import get_tracker

                            if exc.status in (401, 429):
                                get_tracker().record(exc.status)
                            archived.append(
                                f"#{channel.name} ({ch_type}) — archive failed (HTTP {exc.status})"
                            )

        desc = f"Project **{result.get('name', project_id)}** (`{project_id}`) deleted."
        if archived:
            desc += "\n📦 Archived channels: " + ", ".join(archived)
        elif do_archive:
            desc += "\n*(No linked channels to archive.)*"
        await _send_success(interaction, "Project Deleted", description=desc)

    # -------------------------------------------------------------------
    # CHANNEL MANAGEMENT COMMANDS
    # -------------------------------------------------------------------
    # Note: /set-channel and /set-control-interface have been removed.
    # Use /edit-project with the channel parameter instead.

    @bot.tree.command(
        name="create-channel",
        description="Create a new Discord channel for a project",
    )
    @app_commands.describe(
        project_id="Project ID",
        channel_name="Name for the new channel (defaults to project ID)",
        category="Category to create the channel in (optional)",
    )
    async def create_channel_command(
        interaction: discord.Interaction,
        project_id: str,
        channel_name: str | None = None,
        category: discord.CategoryChannel | None = None,
    ):
        await interaction.response.defer()

        # Validate project exists (direct lookup)
        project_check = await handler.execute("get_project_channels", {"project_id": project_id})
        if "error" in project_check:
            await _send_error(interaction, f"Project `{project_id}` not found.", followup=True)
            return

        name = channel_name or project_id
        # Create the Discord channel
        guild = interaction.guild
        if not guild:
            await _send_error(interaction, "Not in a guild.", followup=True)
            return

        topic = f"Agent Queue channel for project: {project_id}"

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
                name=name,
                category=category,
                topic=topic,
                overwrites=overwrites,
                reason=f"AgentQueue: channel for project {project_id}",
            )
        except discord.Forbidden:
            await _send_error(
                interaction, "Bot lacks permission to create channels.", followup=True
            )
            return
        except discord.HTTPException as e:
            await _send_error(interaction, f"Error creating channel: {e}", followup=True)
            return

        # Link it to the project in the database
        result = await handler.execute(
            "set_project_channel",
            {
                "project_id": project_id,
                "channel_id": str(new_channel.id),
            },
        )
        if "error" in result:
            await _send_error(
                interaction, f"Channel created but linking failed: {result['error']}", followup=True
            )
            return

        # Update the bot's channel cache immediately
        bot.update_project_channel(project_id, new_channel)
        await _send_success(
            interaction,
            "Channel Created",
            description=f"Created {new_channel.mention} as channel for project `{project_id}`",
            followup=True,
        )

    @bot.tree.command(
        name="channel-map",
        description="Show all project-to-channel mappings",
    )
    async def channel_map_command(interaction: discord.Interaction):
        result = await handler.execute("list_projects", {})
        projects = result.get("projects", [])
        if not projects:
            await _send_info(interaction, "No Projects", description="No projects configured.")
            return

        # Build a channel-centric view: collect all channel assignments
        lines = ["**Channel Map**\n"]
        assigned = []
        unassigned = []

        for p in projects:
            channel_id = p.get("discord_channel_id")
            if channel_id:
                assigned.append(f"**{p['name']}** (`{p['id']}`) → <#{channel_id}>")
            else:
                unassigned.append(f"`{p['id']}`")

        if assigned:
            lines.append("**Projects with dedicated channels:**")
            for entry in assigned:
                lines.append(f"• {entry}")
        else:
            lines.append("_No projects have dedicated channels yet._")

        if unassigned:
            lines.append(f"\n**Using global channels:** {', '.join(unassigned)}")

        lines.append("\n_Use `/set-channel` or `/create-channel` to assign project channels._")
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="pause", description="Pause a project")
    async def pause_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute("pause_project", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_info(
            interaction,
            "Project Paused",
            description=f"Project **{result.get('name', project_id)}** is now paused.",
        )

    @bot.tree.command(name="resume", description="Resume a paused project")
    async def resume_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute("resume_project", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction,
            "Project Resumed",
            description=f"Project **{result.get('name', project_id)}** is now active.",
        )

    @bot.tree.command(
        name="set-project", description="Set or clear the active project for the chat agent"
    )
    @app_commands.describe(project_id="Project ID to set as active (leave empty to clear)")
    async def set_project_command(interaction: discord.Interaction, project_id: str | None = None):
        args = {"project_id": project_id} if project_id else {}
        result = await handler.execute("set_active_project", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        if result.get("active_project"):
            await _send_success(
                interaction,
                "Active Project Set",
                description=f"Active project set to **{result.get('name', '')}** (`{result['active_project']}`)",
            )
        else:
            await _send_info(
                interaction,
                "Active Project Cleared",
                description="No active project is set for the chat agent.",
            )

    # ===================================================================
    # TASK COMMANDS
    # ===================================================================

    @bot.tree.command(name="tasks", description="List tasks for a project")
    async def tasks_command(
        interaction: discord.Interaction,
    ):
        await interaction.response.defer()
        try:
            project_id = await _resolve_project_from_context(interaction, None)

            args: dict = {
                "include_completed": True,
                "display_mode": "flat",
            }
            if project_id:
                args["project_id"] = project_id

            result = await handler.execute("list_tasks", args)

            if "error" in result:
                await interaction.followup.send(
                    embed=error_embed("Error", description=result["error"]),
                )
                return

            tasks = result.get("tasks", [])
            if not tasks:
                desc = "No tasks found"
                if project_id:
                    desc += f" for project `{project_id}`."
                else:
                    desc += "."
                await interaction.followup.send(
                    embed=info_embed("No Tasks", description=desc),
                )
                return

            # Get thread URLs from the bot
            thread_urls = bot.get_task_thread_urls()

            # Group tasks by status for the interactive report view
            tasks_by_status: dict[str, list] = {}
            for t in tasks:
                tasks_by_status.setdefault(t["status"], []).append(t)
            total = result.get("total", len(tasks))
            view_widget = TaskReportView(
                tasks_by_status,
                total,
                all_tasks=tasks,
                handler=handler,
                project_id=project_id,
                thread_urls=thread_urls,
            )
            content = view_widget.build_content()
            await interaction.followup.send(content, view=view_widget)
        except Exception as e:
            logger.error("Error in /tasks: %s", e, exc_info=True)
            try:
                await interaction.followup.send(
                    embed=error_embed("Error", description=f"Failed to list tasks: {e}"),
                )
            except Exception:
                pass  # interaction may have expired

    @bot.tree.command(
        name="active-tasks",
        description="List active tasks across ALL projects",
    )
    @app_commands.describe(
        show_completed="Include completed/failed/blocked tasks (default: hide)",
    )
    async def active_tasks_command(
        interaction: discord.Interaction,
        show_completed: bool = False,
    ):
        await interaction.response.defer()
        result = await handler.execute(
            "list_active_tasks_all_projects",
            {"include_completed": show_completed},
        )

        by_project: dict[str, list] = result.get("by_project", {})
        total = result.get("total", 0)
        hidden = result.get("hidden_completed", 0)

        if total == 0:
            desc = (
                "No active tasks across any project."
                if not show_completed
                else "No tasks found across any project."
            )
            if hidden > 0:
                desc += (
                    f"\n\n_{hidden} completed/failed task(s) hidden — "
                    f"use `/active-tasks show_completed:True` to view._"
                )
            await interaction.followup.send(
                embed=info_embed("No Tasks", description=desc),
            )
            return

        # Build embed with one field per project (up to 25 field limit).
        label = "tasks" if show_completed else "active tasks"
        description = f"**{total} {label}** across **{len(by_project)} project(s)**"
        if hidden > 0:
            description += f"\n_{hidden} completed/failed task(s) hidden_"

        # Build per-project fields. Each project gets one embed field.
        # Cap tasks per project to fit within 1024-char field value limit.
        fields: list[tuple[str, str, bool]] = []
        max_tasks_per_field = 12  # keep field values well under 1024 chars
        for project_id in sorted(by_project.keys()):
            project_tasks = by_project[project_id]
            task_lines: list[str] = []
            for t in project_tasks[:max_tasks_per_field]:
                emoji = STATUS_EMOJIS.get(t["status"], "\u26aa")
                title = t["title"]
                if len(title) > 60:
                    title = title[:57] + "..."
                agent_info = ""
                if t.get("assigned_agent"):
                    agent_info = f" \u2190 `{t['assigned_agent']}`"
                task_lines.append(f"{emoji} **{title}** `{t['id']}`{agent_info}")
            if len(project_tasks) > max_tasks_per_field:
                task_lines.append(f"_...and {len(project_tasks) - max_tasks_per_field} more_")
            field_value = "\n".join(task_lines)
            # Truncate field value to Discord's limit as a safety net
            field_value = truncate(field_value, LIMIT_FIELD_VALUE)
            fields.append(
                (
                    f"`{project_id}` ({len(project_tasks)})",
                    field_value,
                    False,  # not inline — each project gets a full-width field
                )
            )

        embed = info_embed(
            "Active Tasks — All Projects",
            description=description,
            fields=fields,
        )
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="task", description="Show full details of a task")
    @app_commands.describe(task_id="Task ID")
    async def task_command(interaction: discord.Interaction, task_id: str):
        detail = await _format_task_detail(task_id)
        if detail is None:
            await interaction.response.send_message(
                embed=error_embed("Task Not Found", description=f"Task `{task_id}` was not found."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(detail)

    @bot.tree.command(name="add-task", description="Add a task manually to the active project")
    @app_commands.describe(description="What the task should do")
    async def add_task_command(interaction: discord.Interaction, description: str):
        project_id = (
            bot.get_project_for_channel(interaction.channel_id) or handler._active_project_id
        )
        if not project_id:
            await interaction.response.send_message(
                "No project context — use this in a project channel or set an active project.",
                ephemeral=True,
            )
            return
        title = description[:100] if len(description) > 100 else description
        result = await handler.execute(
            "create_task",
            {
                "project_id": project_id,
                "title": title,
                "description": description,
            },
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        task_id = result["created"]
        desc_preview = truncate(description, LIMIT_FIELD_VALUE)
        embed = success_embed(
            "Task Added",
            fields=[
                ("ID", f"`{task_id}`", True),
                ("Project", f"`{result['project_id']}`", True),
                ("Status", "🔵 READY", True),
                ("Description", desc_preview, False),
            ],
        )
        await interaction.response.send_message(embed=embed)
        # Track the message so the orchestrator can delete it when the task starts
        try:
            msg = await interaction.original_response()
            handler.orchestrator._task_added_messages[task_id] = msg
        except Exception:
            pass  # Non-critical — message just won't be auto-deleted

    @bot.tree.command(name="edit-task", description="Edit a task's properties")
    @app_commands.describe(
        task_id="Task ID",
        title="New title (optional)",
        description="New description (optional)",
        priority="New priority (optional)",
        task_type="New task type (optional)",
        status="New status — admin override (optional)",
        max_retries="Max retry attempts (optional)",
        verification_type="How to verify task output (optional)",
    )
    @app_commands.choices(
        task_type=[
            app_commands.Choice(name="feature", value="feature"),
            app_commands.Choice(name="bugfix", value="bugfix"),
            app_commands.Choice(name="refactor", value="refactor"),
            app_commands.Choice(name="test", value="test"),
            app_commands.Choice(name="docs", value="docs"),
            app_commands.Choice(name="chore", value="chore"),
            app_commands.Choice(name="research", value="research"),
            app_commands.Choice(name="plan", value="plan"),
        ],
        status=[
            app_commands.Choice(name="DEFINED", value="DEFINED"),
            app_commands.Choice(name="READY", value="READY"),
            app_commands.Choice(name="IN_PROGRESS", value="IN_PROGRESS"),
            app_commands.Choice(name="COMPLETED", value="COMPLETED"),
            app_commands.Choice(name="FAILED", value="FAILED"),
            app_commands.Choice(name="BLOCKED", value="BLOCKED"),
        ],
        verification_type=[
            app_commands.Choice(name="auto_test", value="auto_test"),
            app_commands.Choice(name="qa_agent", value="qa_agent"),
            app_commands.Choice(name="human", value="human"),
        ],
    )
    async def edit_task_command(
        interaction: discord.Interaction,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        priority: int | None = None,
        task_type: app_commands.Choice[str] | None = None,
        status: app_commands.Choice[str] | None = None,
        max_retries: int | None = None,
        verification_type: app_commands.Choice[str] | None = None,
    ):
        args: dict = {"task_id": task_id}
        if title is not None:
            args["title"] = title
        if description is not None:
            args["description"] = description
        if priority is not None:
            args["priority"] = priority
        if task_type is not None:
            args["task_type"] = task_type.value
        if status is not None:
            args["status"] = status.value
        if max_retries is not None:
            args["max_retries"] = max_retries
        if verification_type is not None:
            args["verification_type"] = verification_type.value
        result = await handler.execute("edit_task", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        fields = ", ".join(result.get("fields", []))
        desc = f"Task `{task_id}` updated: {fields}"
        if result.get("old_status"):
            from src.discord.embeds import STATUS_EMOJIS

            old_emoji = STATUS_EMOJIS.get(result["old_status"], "")
            new_emoji = STATUS_EMOJIS.get(result["new_status"], "")
            desc += (
                f"\n{old_emoji} **{result['old_status']}** → {new_emoji} **{result['new_status']}**"
            )
        await _send_success(
            interaction,
            "Task Updated",
            description=desc,
        )

    @bot.tree.command(name="stop-task", description="Stop a task that is currently in progress")
    @app_commands.describe(task_id="Task ID to stop")
    async def stop_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("stop_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction, "Task Stopped", description=f"Task `{task_id}` has been stopped."
        )

    @bot.tree.command(
        name="restart-task", description="Reset a task back to READY for re-execution"
    )
    @app_commands.describe(task_id="Task ID to restart")
    async def restart_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("restart_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction,
            "Task Restarted",
            description=f"Task `{task_id}` restarted ({result.get('previous_status', '?')} → READY)",
        )

    @bot.tree.command(
        name="reopen-with-feedback",
        description="Reopen a completed/failed task with feedback for rework",
    )
    @app_commands.describe(
        task_id="Task ID to reopen",
        feedback="QA feedback explaining what went wrong or needs fixing",
    )
    async def reopen_with_feedback_command(
        interaction: discord.Interaction,
        task_id: str,
        feedback: str,
    ):
        result = await handler.execute(
            "reopen_with_feedback",
            {"task_id": task_id, "feedback": feedback},
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        prev = result.get("previous_status", "?")
        title = result.get("title", "")
        await _send_success(
            interaction,
            "Task Reopened with Feedback",
            description=(
                f"Task `{task_id}` ({title}) reopened ({prev} → READY).\n\n"
                f"**Feedback added:**\n{feedback[:500]}"
            ),
        )

    @bot.tree.command(name="delete-task", description="Delete a task")
    @app_commands.describe(task_id="Task ID to delete")
    async def delete_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("delete_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction,
            "Task Deleted",
            description=f"Task `{task_id}` ({result.get('title', '')}) deleted.",
        )

    @bot.tree.command(
        name="archive-tasks",
        description="Archive completed tasks (DB + markdown notes in workspace)",
    )
    @app_commands.describe(
        project_id="Project to archive completed tasks from (optional — omit for all projects)",
        include_failed="Also archive FAILED and BLOCKED tasks (default: false)",
    )
    async def archive_tasks_command(
        interaction: discord.Interaction,
        project_id: str | None = None,
        include_failed: bool = False,
    ):
        args: dict = {"include_failed": include_failed}
        if project_id:
            args["project_id"] = project_id
        await interaction.response.defer()
        result = await handler.execute("archive_tasks", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        if "message" in result:
            await _send_info(
                interaction,
                "Nothing to Archive",
                description=result["message"],
                followup=True,
            )
            return
        count = result.get("archived_count", 0)
        scope = f" from `{project_id}`" if project_id else ""
        archive_dir = result.get("archive_dir")
        desc = f"Archived **{count}** task{'s' if count != 1 else ''}{scope}."
        if archive_dir:
            desc += f"\nNotes written to `{archive_dir}`"
        await _send_success(
            interaction,
            "Tasks Archived",
            description=desc,
            followup=True,
        )

    @bot.tree.command(
        name="archive-task",
        description="Archive a single completed/failed/blocked task",
    )
    @app_commands.describe(task_id="Task ID to archive")
    async def archive_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("archive_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction,
            "Task Archived",
            description=(
                f"Task `{task_id}` ({result.get('title', '')}) archived "
                f"(was {result.get('status', '?')})."
            ),
        )

    @bot.tree.command(
        name="list-archived",
        description="View archived tasks",
    )
    @app_commands.describe(
        project_id="Filter by project (optional)",
        limit="Max tasks to show (default 25)",
    )
    async def list_archived_command(
        interaction: discord.Interaction,
        project_id: str | None = None,
        limit: int = 25,
    ):
        args: dict = {"limit": limit}
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("list_archived", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        tasks = result.get("tasks", [])
        total = result.get("total", 0)
        if not tasks:
            scope = f" in project `{project_id}`" if project_id else ""
            await _send_info(
                interaction,
                "No Archived Tasks",
                description=f"No archived tasks found{scope}.",
            )
            return
        lines = []
        for t in tasks:
            status = t.get("status", "?")
            title = t.get("title", "")
            tid = t.get("id", "?")
            lines.append(f"• `{tid}` — {title} ({status})")
        body = "\n".join(lines)
        showing = (
            f"Showing {len(tasks)} of {total}"
            if total > len(tasks)
            else f"{len(tasks)} task{'s' if len(tasks) != 1 else ''}"
        )
        scope = f" in `{project_id}`" if project_id else ""
        await _send_info(
            interaction,
            f"Archived Tasks{scope}",
            description=f"{showing}\n\n{body}",
        )

    @bot.tree.command(
        name="restore-task",
        description="Restore an archived task back to active status",
    )
    @app_commands.describe(task_id="Archived task ID to restore")
    async def restore_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("restore_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction,
            "Task Restored",
            description=(
                f"Task `{task_id}` ({result.get('title', '')}) restored with status DEFINED."
            ),
        )

    @bot.tree.command(
        name="archive-settings",
        description="View auto-archive configuration and status",
    )
    async def archive_settings_command(interaction: discord.Interaction):
        result = await handler.execute("archive_settings", {})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        enabled = "✅ Enabled" if result["enabled"] else "❌ Disabled"
        hours = result["after_hours"]
        statuses = ", ".join(result["statuses"]) if result["statuses"] else "None"
        archived = result["archived_count"]
        eligible = result["eligible_count"]
        desc = (
            f"**Auto-Archive:** {enabled}\n"
            f"**Archive After:** {hours} hours\n"
            f"**Eligible Statuses:** {statuses}\n\n"
            f"**Currently Archived:** {archived} task{'s' if archived != 1 else ''}\n"
            f"**Eligible Now:** {eligible} task{'s' if eligible != 1 else ''} "
            f"ready to be auto-archived"
        )
        await _send_info(interaction, "Archive Settings", description=desc)

    @bot.tree.command(name="approve-task", description="Approve a task that is awaiting approval")
    @app_commands.describe(task_id="Task ID to approve")
    async def approve_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("approve_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction,
            "Task Approved",
            description=f"Task `{task_id}` ({result.get('title', '')}) approved and completed.",
        )

    @bot.tree.command(
        name="skip-task",
        description="Skip a blocked/failed task to unblock its dependency chain",
    )
    @app_commands.describe(task_id="Task ID to skip")
    async def skip_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("skip_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        unblocked_count = result.get("unblocked_count", 0)
        desc = f"Task `{task_id}` skipped (marked COMPLETED)."
        if unblocked_count:
            unblocked_list = ", ".join(f"`{t['id']}`" for t in result.get("unblocked", []))
            desc += f"\n{unblocked_count} task(s) unblocked: {unblocked_list}"
        await _send_success(interaction, "Task Skipped", description=desc)

    @bot.tree.command(
        name="chain-health",
        description="Check dependency chain health for stuck tasks",
    )
    @app_commands.describe(
        task_id="(Optional) Check a specific blocked task",
    )
    async def chain_health_command(
        interaction: discord.Interaction,
        task_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        args = {}
        if task_id:
            args["task_id"] = task_id
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("get_chain_health", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return

        if "task_id" in result:
            stuck = result.get("stuck_downstream", [])
            if not stuck:
                await _send_success(
                    interaction,
                    "Chain Healthy",
                    description=f"No stuck downstream tasks for `{result['task_id']}`.",
                )
            else:
                lines = [
                    f"**Stuck Chain:** `{result['task_id']}` — {result.get('title', '')}",
                    f"{len(stuck)} downstream task(s) stuck:",
                ]
                for t in stuck[:15]:
                    lines.append(f"  • `{t['id']}` — {t['title']} ({t['status']})")
                if len(stuck) > 15:
                    lines.append(f"  … and {len(stuck) - 15} more")
                await _send_warning(
                    interaction,
                    "Stuck Chain Detected",
                    description="\n".join(lines),
                )
        else:
            chains = result.get("stuck_chains", [])
            if not chains:
                scope = (
                    f" in project `{result.get('project_id')}`" if result.get("project_id") else ""
                )
                await _send_success(
                    interaction,
                    "Chains Healthy",
                    description=f"No stuck dependency chains{scope}.",
                )
            else:
                lines = [f"**{len(chains)} stuck chain(s):**"]
                for chain in chains[:10]:
                    bt = chain["blocked_task"]
                    lines.append(
                        f"  • `{bt['id']}` — {bt['title']} → {chain['stuck_count']} stuck task(s)"
                    )
                if len(chains) > 10:
                    lines.append(f"  … and {len(chains) - 10} more chains")
                await _send_warning(
                    interaction,
                    "Stuck Chains Found",
                    description="\n".join(lines),
                )

    # Note: /set-status has been removed. Use /edit-task with the status
    # parameter instead.

    @bot.tree.command(name="task-result", description="Show the results/output of a completed task")
    @app_commands.describe(task_id="Task ID to inspect")
    async def task_result_command(interaction: discord.Interaction, task_id: str):
        await interaction.response.defer(ephemeral=True)
        result = await handler.execute("get_task_result", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        fields: list[tuple[str, str, bool]] = [
            ("Result", result.get("result", "unknown"), True),
        ]
        summary = result.get("summary") or ""
        if summary:
            fields.append(("Summary", truncate(summary, LIMIT_FIELD_VALUE), False))
        files = result.get("files_changed") or []
        if files:
            file_list = "\n".join(f"• `{f}`" for f in files[:20])
            if len(files) > 20:
                file_list += f"\n_...and {len(files) - 20} more_"
            fields.append(("Files Changed", truncate(file_list, LIMIT_FIELD_VALUE), False))
        tokens = result.get("tokens_used", 0)
        if tokens:
            fields.append(("Tokens Used", f"{tokens:,}", True))
        error_msg = result.get("error_message") or ""
        if error_msg:
            snippet = truncate(error_msg, 500)
            fields.append(("Error", f"```\n{snippet}\n```", False))
        if result.get("result") == "completed":
            embed = success_embed(f"Task Result: {task_id}", fields=fields)
        else:
            embed = error_embed(f"Task Result: {task_id}", fields=fields)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(name="task-diff", description="Show the git diff for a task's branch")
    @app_commands.describe(task_id="Task ID")
    async def task_diff_command(interaction: discord.Interaction, task_id: str):
        await interaction.response.defer(ephemeral=True)
        result = await handler.execute("get_task_diff", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        diff = result.get("diff", "(no changes)")
        branch = result.get("branch", "?")
        header = f"**Branch:** `{branch}`\n"
        if len(diff) > 1800:
            file = discord.File(
                fp=io.BytesIO(diff.encode("utf-8")),
                filename=f"diff-{task_id}.patch",
            )
            await interaction.followup.send(
                f"{header}*Diff attached ({len(diff):,} chars)*",
                file=file,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"{header}```diff\n{diff}\n```",
                ephemeral=True,
            )

    @bot.tree.command(
        name="task-deps",
        description="Show dependency graph for a task (what it needs and blocks)",
    )
    @app_commands.describe(task_id="Task ID to inspect")
    async def task_deps_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("task_deps", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return

        status = result["status"]
        title = result["title"]
        task_emoji = STATUS_EMOJIS.get(status, "⚪")

        depends_on: list[dict] = result.get("depends_on", [])
        blocks: list[dict] = result.get("blocks", [])

        # Build embed fields
        fields: list[tuple[str, str, bool]] = [
            ("Task", f"{task_emoji} `{task_id}` — {title}", False),
            ("Status", f"{task_emoji} {status}", True),
        ]

        # Upstream: what this task needs
        if depends_on:
            dep_lines = []
            for dep in depends_on:
                emoji = STATUS_EMOJIS.get(dep["status"], "⚪")
                dep_lines.append(f"{emoji} `{dep['id']}` — {dep['title']} ({dep['status']})")
            fields.append(("Depends On", truncate("\n".join(dep_lines), LIMIT_FIELD_VALUE), False))
        else:
            fields.append(("Depends On", "_No upstream dependencies_", False))

        # Downstream: what this task blocks
        if blocks:
            blk_lines = []
            for blk in blocks:
                emoji = STATUS_EMOJIS.get(blk["status"], "⚪")
                blk_lines.append(f"{emoji} `{blk['id']}` — {blk['title']} ({blk['status']})")
            fields.append(("Blocks", truncate("\n".join(blk_lines), LIMIT_FIELD_VALUE), False))
        else:
            fields.append(("Blocks", "_No downstream dependents_", False))

        embed = status_embed(
            status,
            f"Dependencies: {task_id}",
            fields=fields,
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(
        name="agent-error",
        description="Show the last error recorded for a task",
    )
    @app_commands.describe(task_id="Task ID to inspect")
    async def agent_error_command(interaction: discord.Interaction, task_id: str):
        await interaction.response.defer(ephemeral=True)
        result = await handler.execute("get_agent_error", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        fields: list[tuple[str, str, bool]] = [
            ("Task", result.get("title", ""), False),
            ("Status", result.get("status", ""), True),
            ("Retries", result.get("retries", ""), True),
        ]

        if result.get("message"):
            embed = error_embed(
                f"Agent Error Report: {task_id}",
                description=f"_{result['message']}_",
                fields=fields,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        fields.append(("Result", result.get("result", "unknown"), True))
        fields.append(("Error Type", f"**{result.get('error_type', 'unknown')}**", False))
        error_msg = result.get("error_message") or ""
        if error_msg:
            snippet = truncate(error_msg, 990)
            fields.append(("Error Detail", f"```\n{snippet}\n```", False))
        else:
            fields.append(("Error Detail", "_No error message recorded._", False))
        fields.append(("Suggested Fix", result.get("suggested_fix", "Review the logs"), False))
        summary = result.get("agent_summary") or ""
        if summary:
            fields.append(("Agent Summary", truncate(summary, 500), False))
        embed = error_embed(f"Agent Error Report: {task_id}", fields=fields)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ===================================================================
    # AGENT COMMANDS
    # ===================================================================

    @bot.tree.command(name="create-agent", description="Register a new agent")
    @app_commands.describe(
        name="Agent display name (leave empty for auto-generated creative name)",
    )
    async def create_agent_command(
        interaction: discord.Interaction,
        name: str | None = None,
    ):
        args: dict = {}
        if name:
            args["name"] = name
        result = await handler.execute("create_agent", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        agent_fields: list[tuple[str, str, bool]] = [
            ("Name", result.get("name", name), True),
            ("ID", f"`{result['created']}`", True),
            ("State", result.get("state", "IDLE"), True),
        ]
        embed = success_embed("Agent Registered", fields=agent_fields)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(
        name="pause-agent",
        description="Pause an agent so it stops receiving new tasks",
    )
    @app_commands.describe(agent_id="Agent ID to pause")
    async def pause_agent_command(
        interaction: discord.Interaction,
        agent_id: str,
    ):
        result = await handler.execute("pause_agent", {"agent_id": agent_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        desc = f"Agent `{agent_id}` is now paused."
        if result.get("note"):
            desc += f"\n{result['note']}"
        embed = success_embed("Agent Paused", description=desc)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(
        name="resume-agent",
        description="Resume a paused agent so it can receive tasks again",
    )
    @app_commands.describe(agent_id="Agent ID to resume")
    async def resume_agent_command(
        interaction: discord.Interaction,
        agent_id: str,
    ):
        result = await handler.execute("resume_agent", {"agent_id": agent_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        embed = success_embed(
            "Agent Resumed",
            description=f"Agent `{agent_id}` is now IDLE and ready to receive tasks.",
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="edit-agent", description="Edit an agent's properties")
    @app_commands.describe(
        agent_id="Agent ID",
        name="New display name (optional)",
        agent_type="New agent type (optional)",
    )
    @app_commands.choices(
        agent_type=[
            app_commands.Choice(name="claude", value="claude"),
            app_commands.Choice(name="codex", value="codex"),
            app_commands.Choice(name="cursor", value="cursor"),
            app_commands.Choice(name="aider", value="aider"),
        ]
    )
    async def edit_agent_command(
        interaction: discord.Interaction,
        agent_id: str,
        name: str | None = None,
        agent_type: app_commands.Choice[str] | None = None,
    ):
        args: dict = {"agent_id": agent_id}
        if name is not None:
            args["name"] = name
        if agent_type is not None:
            args["agent_type"] = agent_type.value
        result = await handler.execute("edit_agent", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        fields = ", ".join(result.get("fields", []))
        await _send_success(
            interaction,
            "Agent Updated",
            description=f"Agent `{agent_id}` updated: {fields}",
        )

    @bot.tree.command(
        name="delete-agent",
        description="Delete an agent and its workspace mappings",
    )
    @app_commands.describe(agent_id="Agent ID to delete")
    async def delete_agent_command(
        interaction: discord.Interaction,
        agent_id: str,
    ):
        result = await handler.execute("delete_agent", {"agent_id": agent_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        embed = success_embed(
            "Agent Deleted",
            description=f"Agent `{result['name']}` (`{agent_id}`) has been removed.",
        )
        await interaction.response.send_message(embed=embed)

    # ===================================================================
    # WORKSPACE COMMANDS
    # ===================================================================

    @bot.tree.command(name="workspaces", description="List project workspaces")
    async def workspaces_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        args = {}
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("list_workspaces", args)
        workspaces = result.get("workspaces", [])
        if not workspaces:
            await _send_info(
                interaction,
                "No Workspaces",
                description="No workspaces registered. Use `/add-workspace` to add one.",
            )
            return
        lines = ["## Workspaces"]
        for ws in workspaces:
            lock_info = ""
            if ws.get("locked_by_agent_id"):
                lock_info = f" 🔒 agent=`{ws['locked_by_agent_id']}`"
                if ws.get("locked_by_task_id"):
                    lock_info += f" task=`{ws['locked_by_task_id']}`"
            name_str = f" **{ws['name']}**" if ws.get("name") else ""
            lines.append(
                f"• `{ws['id']}`{name_str} ({ws['source_type']}) — "
                f"`{ws['workspace_path']}` project=`{ws['project_id']}`{lock_info}"
            )
        msg = "\n".join(lines)
        await _send_long_interaction(msg, interaction.response.send_message)

    @bot.tree.command(name="add-workspace", description="Add a workspace directory for a project")
    @app_commands.describe(
        source="How to set up the workspace",
        path="Directory path (required for link, auto-generated for clone)",
        name="Workspace name (optional)",
    )
    @app_commands.choices(
        source=[
            app_commands.Choice(name="clone", value="clone"),
            app_commands.Choice(name="link", value="link"),
        ]
    )
    async def add_workspace_command(
        interaction: discord.Interaction,
        source: app_commands.Choice[str],
        path: str | None = None,
        name: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        args: dict = {
            "project_id": project_id,
            "source": source.value,
        }
        if path:
            args["path"] = path
        if name:
            args["name"] = name
        result = await handler.execute("add_workspace", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        embed = success_embed(
            "Workspace Added",
            fields=[
                ("ID", f"`{result['created']}`", True),
                ("Source", source.value, True),
                ("Project", f"`{project_id}`", True),
                ("Path", f"`{result.get('workspace_path', '')}`", False),
            ],
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(
        name="release-workspace",
        description="Force-release a stuck workspace lock",
    )
    @app_commands.describe(workspace_id="Workspace ID to release")
    async def release_workspace_command(
        interaction: discord.Interaction,
        workspace_id: str,
    ):
        result = await handler.execute("release_workspace", {"workspace_id": workspace_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction,
            "Workspace Released",
            description=f"Workspace `{workspace_id}` lock has been released.",
        )

    @bot.tree.command(
        name="remove-workspace",
        description="Delete a workspace from a project (must not be locked)",
    )
    @app_commands.describe(workspace_id="Workspace ID or name to delete")
    async def remove_workspace_command(
        interaction: discord.Interaction,
        workspace_id: str,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        args: dict = {"workspace_id": workspace_id}
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("remove_workspace", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        embed = success_embed(
            "Workspace Deleted",
            fields=[
                ("ID", f"`{result['deleted']}`", True),
                ("Name", result.get("name") or "—", True),
                ("Project", f"`{result.get('project_id', '')}`", True),
                ("Path", f"`{result.get('workspace_path', '')}`", False),
            ],
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(
        name="sync-workspaces",
        description="Queue a sync task to merge all feature branches and synchronize workspaces",
    )
    async def sync_workspaces_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute("queue_sync_workspaces", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return

        embed = success_embed(
            "Sync Workspaces Queued",
            fields=[
                ("Task ID", f"`{result['queued']}`", True),
                ("Project", f"`{result['project_id']}`", True),
                ("Priority", str(result.get("priority", 1)), True),
                ("Workspaces", str(result.get("workspace_count", 0)), True),
                ("Default Branch", f"`{result.get('default_branch', 'main')}`", True),
            ],
        )
        embed.description = result.get("message", "")
        await interaction.response.send_message(embed=embed)

    # ===================================================================
    # GIT COMMANDS
    # ===================================================================

    @bot.tree.command(
        name="git-status",
        description="Show the git status of a project's repository",
    )
    async def git_status_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        result = await handler.execute("get_git_status", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        project_name = result.get("project_name", project_id)
        repo_statuses = result.get("repos", [])

        # ----- Build compact summary for the channel -----
        summary_lines: list[str] = []
        for rs in repo_statuses:
            ws_label = rs.get("workspace_name") or rs.get("workspace_id") or "?"
            if "error" in rs:
                summary_lines.append(f"⚠️ **{ws_label}** — {rs['error']}")
                continue

            branch = rs.get("branch", "?")
            status_raw = rs.get("status", "(clean)")
            is_clean = status_raw.strip() in ("(clean)", "") or (
                "nothing to commit" in status_raw and "working tree clean" in status_raw
            )

            # Build compact one-line status indicators
            indicators: list[str] = []
            if is_clean:
                indicators.append("✅ clean")
            else:
                # Count changed/untracked files from porcelain-like output
                changed = sum(
                    1
                    for line in status_raw.splitlines()
                    if line.strip()
                    and not line.startswith("On branch")
                    and not line.startswith("Your branch")
                    and not line.startswith("nothing to commit")
                    and not line.startswith("Changes")
                    and not line.startswith("Untracked")
                    and not line.startswith("  (use")
                    and not line.startswith("no changes")
                )
                if changed:
                    indicators.append(f"📝 {changed} changed")
                else:
                    indicators.append("📝 dirty")

            ahead = rs.get("ahead", 0)
            behind = rs.get("behind", 0)
            if ahead:
                indicators.append(f"⬆ {ahead}")
            if behind:
                indicators.append(f"⬇ {behind}")

            stash_count = rs.get("stash_count", 0)
            if stash_count:
                indicators.append(f"📦 {stash_count} stash")

            agent_id = rs.get("locked_by_agent_id")
            task_title = rs.get("current_task_title")
            if agent_id:
                task_hint = f" — *{task_title}*" if task_title else ""
                indicators.append(f"🔒 {agent_id}{task_hint}")

            status_str = " · ".join(indicators)
            summary_lines.append(f"**{ws_label}** `{branch}` — {status_str}")

        summary = (
            f"## 📊 Git Status: {project_name}\n"
            + "\n".join(summary_lines)
            + "\n\n*See thread for full details ↓*"
        )
        await interaction.followup.send(summary)

        # ----- Create thread with detailed per-workspace breakdown -----
        msg = await interaction.original_response()
        thread = await msg.create_thread(
            name=f"Git Status: {project_name}"[:100],
            auto_archive_duration=60,
        )

        for rs in repo_statuses:
            ws_label = rs.get("workspace_name") or rs.get("workspace_id") or "?"
            if "error" in rs:
                await thread.send(f"### ⚠️ Workspace: `{ws_label}`\n{rs['error']}")
                continue

            ws_name = rs.get("workspace_name")
            ws_id = rs.get("workspace_id") or "?"
            ws_header = f"`{ws_name}` (`{ws_id}`)" if ws_name else f"`{ws_id}`"
            lines: list[str] = [f"### 📁 Workspace: {ws_header}"]

            if rs.get("path"):
                lines.append(f"**Path:** `{rs['path']}`")
            if rs.get("branch"):
                branch_line = f"**Branch:** `{rs['branch']}`"
                ahead = rs.get("ahead", 0)
                behind = rs.get("behind", 0)
                if ahead or behind:
                    parts = []
                    if ahead:
                        parts.append(f"{ahead} ahead")
                    if behind:
                        parts.append(f"{behind} behind")
                    branch_line += f" ({', '.join(parts)})"
                lines.append(branch_line)

            # Lock / active-task info
            agent_id = rs.get("locked_by_agent_id")
            if agent_id:
                task_title = rs.get("current_task_title")
                lock_line = f"**Agent:** `{agent_id}`"
                if task_title:
                    lock_line += f" — *{task_title}*"
                lines.append(lock_line)

            stash_count = rs.get("stash_count", 0)
            if stash_count:
                lines.append(f"**Stashes:** {stash_count}")

            # Working-tree status
            status_output = rs.get("status", "(clean)")
            lines.append(f"\n**Working tree:**\n```\n{status_output}\n```")

            # Diff stat (files changed vs default branch)
            diff_stat = rs.get("diff_stat", "")
            if diff_stat:
                lines.append(f"**Diff vs default branch:**\n```\n{diff_stat}\n```")

            # Recent commits
            if rs.get("recent_commits"):
                lines.append(f"**Recent commits:**\n```\n{rs['recent_commits']}\n```")

            detail_msg = "\n".join(lines)
            # Split if too long for Discord
            if len(detail_msg) <= 2000:
                await thread.send(detail_msg)
            else:
                chunks, current = [], ""
                for line in detail_msg.split("\n"):
                    candidate = current + ("\n" if current else "") + line
                    if len(candidate) > 2000:
                        if current:
                            chunks.append(current)
                        current = line
                    else:
                        current = candidate
                if current:
                    chunks.append(current)
                for chunk in chunks:
                    await thread.send(chunk)

    # -------------------------------------------------------------------
    # GIT MANAGEMENT COMMANDS
    # -------------------------------------------------------------------
    # Project-based git commands that auto-detect the project from the
    # Discord channel.  These call the newer project-oriented handlers
    # (create_branch, checkout_branch, commit_changes, push_branch,
    # merge_branch, git_branch) which resolve repos via project_id.

    @bot.tree.command(
        name="git-branches",
        description="List branches or create a new branch in a project's repository",
    )
    @app_commands.describe(
        name="New branch name to create (omit to list branches)",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def git_branches_command(
        interaction: discord.Interaction,
        name: str | None = None,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"project_id": project_id}
        if name:
            args["name"] = name
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("git_branch", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        if "created" in result:
            await _send_success(
                interaction,
                "Branch Created",
                description=f"Created and switched to branch `{result['created']}` on `{project_id}`",
                followup=True,
            )
        else:
            current = result.get("current_branch", "?")
            branches = result.get("branches", [])
            branch_list = "\n".join(branches) if branches else "(no branches)"
            text = f"## Branches: `{project_id}`\n**Current:** `{current}`\n```\n{branch_list}\n```"
            await _send_long(interaction, text, followup=True)

    @bot.tree.command(
        name="git-checkout",
        description="Switch to an existing branch in a project's repository",
    )
    @app_commands.describe(
        branch_name="Branch name to switch to",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def git_checkout_command(
        interaction: discord.Interaction,
        branch_name: str,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"project_id": project_id, "branch_name": branch_name}
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("checkout_branch", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        await _send_success(
            interaction,
            "Branch Switched",
            description=f"Switched to branch `{result['branch']}` on `{project_id}`",
            followup=True,
            result=result,
        )

    @bot.tree.command(
        name="project-commit",
        description="Stage all changes and commit in a project's repository",
    )
    @app_commands.describe(
        message="Commit message",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def project_commit_command(
        interaction: discord.Interaction,
        message: str,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"project_id": project_id, "message": message}
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("commit_changes", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        if result.get("status") == "committed":
            repo_label = project_id
            await _send_success(
                interaction,
                "Changes Committed",
                description=f"Committed in `{repo_label}` on `{project_id}`: {message}",
                followup=True,
                result=result,
            )
        else:
            await _send_info(
                interaction,
                "Nothing to Commit",
                description=f"Working tree clean on `{project_id}`.",
                followup=True,
            )

    @bot.tree.command(
        name="project-push",
        description="Push a branch to origin in a project's repository",
    )
    @app_commands.describe(
        branch_name="Branch to push (defaults to current branch)",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def project_push_command(
        interaction: discord.Interaction,
        branch_name: str | None = None,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"project_id": project_id}
        if branch_name:
            args["branch_name"] = branch_name
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("push_branch", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        pushed_branch = result.get("branch", "?")
        await _send_success(
            interaction,
            "Branch Pushed",
            description=f"Pushed `{pushed_branch}` to origin on `{project_id}`",
            followup=True,
        )

    @bot.tree.command(
        name="project-merge",
        description="Merge a branch into the default branch in a project's repository",
    )
    @app_commands.describe(
        branch_name="Branch to merge",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def project_merge_command(
        interaction: discord.Interaction,
        branch_name: str,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"project_id": project_id, "branch_name": branch_name}
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("merge_branch", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        target = result.get("target", "main")
        if result.get("status") == "conflict":
            await _send_warning(
                interaction,
                "Merge Conflict",
                description=(
                    f"`{branch_name}` could not be merged into `{target}` "
                    f"on `{project_id}`. Merge was aborted."
                ),
                followup=True,
            )
        else:
            await _send_success(
                interaction,
                "Branch Merged",
                description=f"Merged `{branch_name}` into `{target}` on `{project_id}`",
                followup=True,
                result=result,
            )

    @bot.tree.command(
        name="project-create-branch",
        description="Create and switch to a new branch in a project's repository",
    )
    @app_commands.describe(
        branch_name="Name for the new branch",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def project_create_branch_command(
        interaction: discord.Interaction,
        branch_name: str,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"project_id": project_id, "branch_name": branch_name}
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("create_branch", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        await _send_success(
            interaction,
            "Branch Created",
            description=f"Created and switched to branch `{result['branch']}` on `{project_id}`",
            followup=True,
        )

    @bot.tree.command(
        name="create-branch",
        description="Create a new git branch in a project's repo",
    )
    @app_commands.describe(
        branch_name="Name for the new branch",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def create_branch_command(
        interaction: discord.Interaction,
        branch_name: str,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id, "branch_name": branch_name}
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("create_branch", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction,
            "Branch Created",
            description=f"Branch `{branch_name}` created in `{project_id}`",
        )

    @bot.tree.command(
        name="checkout-branch",
        description="Switch to an existing git branch",
    )
    @app_commands.describe(
        branch_name="Branch name to check out",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def checkout_branch_command(
        interaction: discord.Interaction,
        branch_name: str,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id, "branch_name": branch_name}
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("checkout_branch", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction,
            "Branch Switched",
            description=f"Switched to branch `{branch_name}` in `{project_id}`",
            result=result,
        )

    @bot.tree.command(
        name="commit",
        description="Stage all changes and commit",
    )
    @app_commands.describe(
        message="Commit message",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def commit_command(
        interaction: discord.Interaction,
        message: str,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id, "message": message}
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("commit_changes", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        if result.get("status") == "nothing_to_commit":
            await _send_info(
                interaction,
                "Nothing to Commit",
                description=f"Working tree clean in `{project_id}`.",
            )
            return
        await _send_success(
            interaction,
            "Changes Committed",
            description=f"Committed in `{project_id}`: {message}",
            result=result,
        )

    @bot.tree.command(
        name="push",
        description="Push a branch to the remote",
    )
    @app_commands.describe(
        branch_name="Branch to push (optional — pushes current branch)",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def push_command(
        interaction: discord.Interaction,
        branch_name: str | None = None,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id}
        if branch_name:
            args["branch_name"] = branch_name
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("push_branch", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        pushed_branch = result.get("branch", branch_name or "current")
        await _send_success(
            interaction,
            "Branch Pushed",
            description=f"Pushed `{pushed_branch}` in `{project_id}`",
        )

    @bot.tree.command(
        name="merge",
        description="Merge a branch into the default branch",
    )
    @app_commands.describe(
        branch_name="Branch to merge",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def merge_command(
        interaction: discord.Interaction,
        branch_name: str,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id, "branch_name": branch_name}
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("merge_branch", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        if result.get("status") == "conflict":
            await _send_warning(
                interaction,
                "Merge Conflict",
                description=(
                    f"Merge conflict: `{branch_name}` → `{result.get('target', 'main')}` "
                    f"in `{project_id}`. Merge was aborted."
                ),
            )
            return
        await _send_success(
            interaction,
            "Branch Merged",
            description=f"Merged `{branch_name}` → `{result.get('target', 'main')}` in `{project_id}`",
            result=result,
        )

    @bot.tree.command(
        name="git-commit",
        description="Stage all changes and commit in a repository",
    )
    @app_commands.describe(
        message="Commit message",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def git_commit_command(
        interaction: discord.Interaction,
        message: str,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        # Set active project from channel context so _resolve_repo_path can
        # infer the repository even when project_id is not in the args dict.
        if project_id:
            handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"message": message}
        if project_id:
            args["project_id"] = project_id
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("git_commit", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        label = project_id or "repo"
        if result.get("committed"):
            await _send_success(
                interaction,
                "Changes Committed",
                description=f"Committed in `{label}`: {message}",
                followup=True,
            )
        else:
            await _send_info(
                interaction,
                "Nothing to Commit",
                description=f"Working tree clean in `{label}`.",
                followup=True,
            )

    @bot.tree.command(
        name="git-pull",
        description="Pull (fetch + merge) a branch from remote origin",
    )
    @app_commands.describe(
        branch="Branch name to pull (defaults to current branch)",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def git_pull_command(
        interaction: discord.Interaction,
        branch: str | None = None,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if project_id:
            handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {}
        if project_id:
            args["project_id"] = project_id
        if branch:
            args["branch"] = branch
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("git_pull", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        label = project_id or "repo"
        await _send_success(
            interaction,
            "Branch Pulled",
            description=f"Pulled `{result['pulled']}` in `{label}`",
            followup=True,
        )

    @bot.tree.command(
        name="git-push",
        description="Push a branch to remote origin",
    )
    @app_commands.describe(
        branch="Branch name to push (defaults to current branch)",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def git_push_command(
        interaction: discord.Interaction,
        branch: str | None = None,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if project_id:
            handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {}
        if project_id:
            args["project_id"] = project_id
        if branch:
            args["branch"] = branch
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("git_push", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        label = project_id or "repo"
        await _send_success(
            interaction,
            "Branch Pushed",
            description=f"Pushed `{result['pushed']}` in `{label}`",
            followup=True,
        )

    @bot.tree.command(
        name="git-branch",
        description="Create and switch to a new git branch",
    )
    @app_commands.describe(
        branch_name="Name for the new branch",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def git_branch_command(
        interaction: discord.Interaction,
        branch_name: str,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if project_id:
            handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"branch_name": branch_name}
        if project_id:
            args["project_id"] = project_id
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("git_create_branch", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        label = project_id or "repo"
        await _send_success(
            interaction,
            "Branch Created",
            description=f"Created and switched to branch `{branch_name}` in `{label}`",
            followup=True,
        )

    @bot.tree.command(
        name="git-merge",
        description="Merge a branch into the default branch",
    )
    @app_commands.describe(
        branch_name="Branch to merge",
        default_branch="Target branch (defaults to repo's default branch)",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def git_merge_command(
        interaction: discord.Interaction,
        branch_name: str,
        default_branch: str | None = None,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if project_id:
            handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"branch_name": branch_name}
        if project_id:
            args["project_id"] = project_id
        if default_branch:
            args["default_branch"] = default_branch
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("git_merge", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        label = project_id or "repo"
        if result.get("merged"):
            await _send_success(
                interaction,
                "Branch Merged",
                description=f"Merged `{branch_name}` into `{result['into']}` in `{label}`",
                followup=True,
            )
        else:
            await _send_warning(
                interaction,
                "Merge Conflict",
                description=(
                    f"`{branch_name}` could not be merged into "
                    f"`{result.get('into', 'default')}` in `{label}`. Merge was aborted."
                ),
                followup=True,
            )

    @bot.tree.command(
        name="git-pr",
        description="Create a GitHub pull request",
    )
    @app_commands.describe(
        title="PR title",
        body="PR description (optional)",
        branch="Head branch (defaults to current)",
        base="Base branch (defaults to repo default)",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def git_pr_command(
        interaction: discord.Interaction,
        title: str,
        body: str = "",
        branch: str | None = None,
        base: str | None = None,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if project_id:
            handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"title": title, "body": body}
        if project_id:
            args["project_id"] = project_id
        if workspace:
            args["workspace"] = workspace
        if branch:
            args["branch"] = branch
        if base:
            args["base"] = base
        result = await handler.execute("git_create_pr", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        pr_url = result.get("pr_url", "")
        await _send_success(
            interaction,
            "Pull Request Created",
            description=(
                f"[View PR]({pr_url})\n"
                f"**Branch:** `{result.get('branch', '?')}` → `{result.get('base', '?')}`"
            ),
            followup=True,
        )

    @bot.tree.command(
        name="git-files",
        description="List files changed compared to a base branch",
    )
    @app_commands.describe(
        base_branch="Branch to compare against (defaults to repo default)",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def git_files_command(
        interaction: discord.Interaction,
        base_branch: str | None = None,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if project_id:
            handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {}
        if project_id:
            args["project_id"] = project_id
        if base_branch:
            args["base_branch"] = base_branch
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("git_changed_files", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        label = project_id or "repo"
        files = result.get("files", [])
        count = result.get("count", 0)
        base = result.get("base_branch", "main")
        if not files:
            await _send_info(
                interaction,
                "No Changes",
                description=f"No files changed in `{label}` vs `{base}`",
                followup=True,
            )
            return
        file_list = "\n".join(f"• `{f}`" for f in files[:50])
        if count > 50:
            file_list += f"\n_...and {count - 50} more_"
        msg = f"## Changed Files: `{label}` vs `{base}`\n**{count} file(s) changed:**\n{file_list}"
        await _send_long(interaction, msg, followup=True)

    @bot.tree.command(
        name="git-log",
        description="Show recent git commits",
    )
    @app_commands.describe(
        count="Number of commits to show (default 10)",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def git_log_command(
        interaction: discord.Interaction,
        count: int = 10,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id, "count": count}
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("git_log", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        branch = result.get("branch", "?")
        log = result.get("log", "(no commits)")
        repo_label = project_id
        msg = f"## Git Log: `{repo_label}` (branch: `{branch}`)\n```\n{log}\n```"
        await _send_long(interaction, msg, followup=False)

    @bot.tree.command(
        name="git-diff",
        description="Show git diff for a project's repo",
    )
    @app_commands.describe(
        base_branch="Base branch to diff against (optional — shows working tree diff)",
        workspace="Workspace ID or name (optional — defaults to first workspace)",
    )
    async def git_diff_command(
        interaction: discord.Interaction,
        base_branch: str | None = None,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id}
        if base_branch:
            args["base_branch"] = base_branch
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("git_diff", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return

        diff = result.get("diff", "(no changes)")
        base_label = result.get("base_branch", "working tree")
        repo_label = project_id
        header = f"**Repo:** `{repo_label}` | **Diff against:** `{base_label}`\n"

        if len(diff) > 1800:
            await interaction.response.defer()
            await interaction.followup.send(
                content=f"{header}*Diff attached ({len(diff):,} chars)*",
                file=discord.File(
                    fp=io.BytesIO(diff.encode("utf-8")),
                    filename=f"diff-{project_id}.patch",
                ),
            )
        else:
            await interaction.response.send_message(f"{header}```diff\n{diff}\n```")

    # ---- /create-rule slash command with modal ----

    class _CreateRuleModal(discord.ui.Modal, title="Create Automation Rule"):
        """Modal for quickly creating a new rule."""

        rule_name = discord.ui.TextInput(
            label="Rule name",
            placeholder="e.g. post-failure-analysis",
            required=True,
            max_length=100,
        )
        rule_type_input = discord.ui.TextInput(
            label="Type (active or passive)",
            placeholder="active",
            required=True,
            max_length=10,
            default="active",
        )
        trigger_input = discord.ui.TextInput(
            label="Trigger (for active rules)",
            placeholder="e.g. 'Check every 5 minutes' or 'When task.completed'",
            required=False,
            max_length=200,
        )
        logic_input = discord.ui.TextInput(
            label="Logic / prompt",
            style=discord.TextStyle.long,
            placeholder="What should this rule do when triggered?",
            required=True,
            max_length=2000,
        )

        def __init__(self, project_id: str) -> None:
            super().__init__()
            self._project_id = project_id

        async def on_submit(self, interaction: discord.Interaction) -> None:
            name = self.rule_name.value.strip()
            rule_type = self.rule_type_input.value.strip().lower()
            if rule_type not in ("active", "passive"):
                await interaction.response.send_message(
                    "Type must be 'active' or 'passive'.",
                    ephemeral=True,
                )
                return

            # Build rule markdown content
            lines = [f"# {name}"]
            trigger_text = self.trigger_input.value.strip()
            if trigger_text:
                lines.append(f"\n## Trigger\n\n{trigger_text}")
            lines.append(f"\n## Logic\n\n{self.logic_input.value}")
            content = "\n".join(lines)

            result = await handler.execute(
                "save_rule",
                {
                    "project_id": self._project_id,
                    "type": rule_type,
                    "content": content,
                },
            )
            if "error" in result:
                await interaction.response.send_message(
                    f"Error creating rule: {result['error']}",
                    ephemeral=True,
                )
                return

            rule_id = result.get("id", "unknown")
            hooks = result.get("hooks", [])
            hook_msg = f" ({len(hooks)} hook(s) generated)" if hooks else ""
            await interaction.response.send_message(
                f"Rule **{name}** (`{rule_id}`) created{hook_msg}!",
                ephemeral=True,
            )

    @bot.tree.command(
        name="create-rule",
        description="Create a new automation rule (replaces create-hook)",
    )
    async def create_rule_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.send_modal(_CreateRuleModal(project_id))

    # ===================================================================
    # RULES COMMANDS
    # ===================================================================

    @bot.tree.command(name="rules", description="Browse rules for a project")
    @app_commands.describe(project_id="Project ID (optional if active project is set)")
    async def rules_command(interaction: discord.Interaction, project_id: str | None = None):
        project_id = await _resolve_project_from_context(interaction, project_id)
        args = {}
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("list_rules", args)
        rules = result.get("rules", [])
        if not rules:
            await _send_info(
                interaction,
                "No Rules",
                description="No rules configured for this scope.",
            )
            return
        view = RulesListView(rules, handler)
        embed = view.build_embed()
        await interaction.response.send_message(embed=embed, view=view)

    @bot.tree.command(name="rule", description="View full details of a rule")
    @app_commands.describe(rule_id="Rule ID")
    async def rule_command(interaction: discord.Interaction, rule_id: str):
        result = await handler.execute("load_rule", {"id": rule_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        rule_type = result.get("type", "passive")
        type_label = "⚡ Active" if rule_type == "active" else "📖 Passive"
        scope = result.get("project_id") or "global"
        hooks = result.get("hooks", [])
        content = result.get("content", "")
        if len(content) > 1500:
            content = content[:1500] + "\n\n_...truncated_"

        lines = [
            f"## 📏 Rule: {rule_id}",
            f"**Type:** {type_label}",
            f"**Scope:** `{scope}`",
            f"**Hooks:** {len(hooks)}",
            f"**Created:** {result.get('created', 'unknown')}",
            f"**Updated:** {result.get('updated', 'unknown')}",
            "",
            content,
        ]
        await _send_long_interaction(
            "\n".join(lines),
            interaction.response.send_message,
        )

    @bot.tree.command(name="delete-rule", description="Delete a rule and its hooks")
    @app_commands.describe(rule_id="Rule ID to delete")
    async def delete_rule_command(interaction: discord.Interaction, rule_id: str):
        result = await handler.execute("delete_rule", {"id": rule_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        hooks_removed = result.get("hooks_removed", [])
        await _send_success(
            interaction,
            "Rule Deleted",
            description=(
                f"Rule `{rule_id}` deleted"
                + (f" ({len(hooks_removed)} hook(s) removed)." if hooks_removed else ".")
            ),
        )

    @bot.tree.command(name="fire-rule", description="Manually trigger all hooks for a rule")
    @app_commands.describe(rule_id="Rule ID to fire")
    async def fire_rule_command(interaction: discord.Interaction, rule_id: str):
        await interaction.response.defer(ephemeral=True)
        result = await handler.execute("fire_rule", {"id": rule_id})
        if "error" in result:
            await interaction.followup.send(
                embed=error_embed(title="Error", description=result["error"]),
                ephemeral=True,
            )
            return
        fired = result.get("fired", 0)
        skipped = result.get("skipped", 0)
        msg = f"Rule `{rule_id}` triggered — **{fired}** hook(s) fired"
        if skipped:
            msg += f", {skipped} already running"
        await interaction.followup.send(
            embed=success_embed(title="Rule Fired", description=msg),
            ephemeral=True,
        )

    @bot.tree.command(name="rule-runs", description="Show execution history for a rule")
    @app_commands.describe(rule_id="Rule ID", limit="Number of runs to show (default 10)")
    async def rule_runs_command(
        interaction: discord.Interaction, rule_id: str, limit: int = 10
    ):
        result = await handler.execute("rule_runs", {"id": rule_id, "limit": limit})
        if "error" in result:
            await interaction.response.send_message(
                embed=error_embed(title="Error", description=result["error"]),
                ephemeral=True,
            )
            return
        runs = result.get("runs", [])
        rule_name = result.get("rule_name", rule_id)
        if not runs:
            await interaction.response.send_message(
                f"No execution history for rule `{rule_id}`.",
                ephemeral=True,
            )
            return
        lines = [f"**Execution history for {rule_name}** (`{rule_id}`)\n"]
        for r in runs[:15]:
            status_icon = {
                "completed": "\u2705",
                "failed": "\u274c",
                "skipped": "\u23ed\ufe0f",
                "running": "\ud83d\udd04",
            }.get(r.get("status", ""), "\u2753")
            tokens = r.get("tokens_used", 0)
            reason = r.get("trigger_reason", "")
            skip = r.get("skipped_reason")
            line = f"{status_icon} {reason}"
            if tokens:
                line += f" \u2022 {tokens} tokens"
            if skip:
                line += f" \u2022 skip: {skip}"
            lines.append(line)
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @bot.tree.command(name="toggle-rule", description="Enable or disable a rule")
    @app_commands.describe(rule_id="Rule ID", enabled="True to enable, False to disable")
    async def toggle_rule_command(
        interaction: discord.Interaction, rule_id: str, enabled: bool
    ):
        result = await handler.execute("toggle_rule", {"id": rule_id, "enabled": enabled})
        if "error" in result:
            await interaction.response.send_message(
                embed=error_embed(title="Error", description=result["error"]),
                ephemeral=True,
            )
            return
        action = result.get("action", "toggled")
        updated = result.get("hooks_updated", 0)
        total = result.get("total_hooks", 0)
        await interaction.response.send_message(
            embed=success_embed(
                title=f"Rule {action.title()}",
                description=f"Rule `{rule_id}` {action} — {updated}/{total} hooks updated",
            ),
            ephemeral=True,
        )

    @bot.tree.command(name="refresh-rules", description="Reconcile rules and regenerate hooks")
    async def refresh_rules_command(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        result = await handler.execute("refresh_rules", {})
        if "error" in result:
            await interaction.followup.send(
                embed=error_embed(title="Error", description=result["error"]),
                ephemeral=True,
            )
            return
        lines = [
            f"Rules scanned: **{result.get('rules_scanned', 0)}**",
            f"Active rules: **{result.get('active_rules', 0)}**",
            f"Hooks regenerated: **{result.get('hooks_regenerated', 0)}**",
            f"Hooks unchanged: **{result.get('hooks_unchanged', 0)}**",
        ]
        errors = result.get("errors", 0)
        if errors:
            lines.append(f"Errors: **{errors}**")
        await interaction.followup.send(
            embed=success_embed(title="Rules Refreshed", description="\n".join(lines)),
            ephemeral=True,
        )

    # ===================================================================
    # NOTES COMMANDS
    # ===================================================================

    @bot.tree.command(name="notes", description="View and manage notes for a project")
    async def notes_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute("list_notes", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return

        # Look up project name for the header
        projects_result = await handler.execute("list_projects", {})
        project_name = project_id
        for p in projects_result.get("projects", []):
            if p["id"] == project_id:
                project_name = p["name"]
                break

        notes = result.get("notes", [])
        view = NotesView(project_id, notes, handler=handler, bot=bot)
        await interaction.response.send_message(
            view.build_content(),
            view=view,
        )

        # Create a thread for interactive note management
        msg = await interaction.original_response()
        thread = await msg.create_thread(
            name=f"Notes: {project_name}"[:100],
            auto_archive_duration=1440,
        )
        await thread.send(
            f"💬 Type ideas and I'll organize them into notes for **{project_name}**.\n"
            f"Click a note button above to view its content."
        )
        await bot.register_notes_thread(thread.id, project_id)

        # Track TOC message for view persistence
        toc_messages = getattr(bot, "_notes_toc_messages", {})
        toc_messages[thread.id] = msg.id
        bot._notes_toc_messages = toc_messages
        await bot._save_notes_threads()

    @bot.tree.command(name="write-note", description="Create or update a project note")
    @app_commands.describe(
        title="Note title",
        content="Note content (markdown)",
    )
    async def write_note_command(
        interaction: discord.Interaction,
        title: str,
        content: str,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute(
            "write_note",
            {
                "project_id": project_id,
                "title": title,
                "content": content,
            },
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        status = result.get("status", "created")
        await _send_success(
            interaction,
            f"Note {status.title()}",
            description=f"Note **{title}** {status} in `{project_id}`",
        )

    @bot.tree.command(name="delete-note", description="Delete a project note")
    @app_commands.describe(
        title="Note title",
    )
    async def delete_note_command(
        interaction: discord.Interaction,
        title: str,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute(
            "delete_note",
            {
                "project_id": project_id,
                "title": title,
            },
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction,
            "Note Deleted",
            description=f"Note **{title}** deleted from `{project_id}`",
        )

    # ===================================================================
    # MEMORY COMMANDS
    # ===================================================================

    @bot.tree.command(name="memory-stats", description="Show memory index statistics for a project")
    @app_commands.describe(
        project="Project ID (defaults to the project linked to this channel)",
    )
    async def memory_stats_command(
        interaction: discord.Interaction,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return

        result = await handler.execute("memory_stats", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return

        enabled = result.get("enabled", False)
        available = result.get("available", False)

        if not enabled:
            await _send_info(
                interaction,
                "Memory Disabled",
                description=(
                    f"Memory is **not enabled** for `{project_id}`.\n"
                    f"memsearch installed: {'Yes' if available else 'No'}\n\n"
                    "Set `memory.enabled = true` in your config to enable."
                ),
            )
            return

        if not available:
            await _send_warning(
                interaction,
                "Memory Unavailable",
                description=(
                    f"Memory is enabled for `{project_id}` but the "
                    "`memsearch` package is not installed."
                ),
            )
            return

        fields = [
            ("Collection", f"`{result.get('collection', 'N/A')}`", True),
            ("Embedding Provider", f"`{result.get('embedding_provider', 'N/A')}`", True),
            ("Milvus URI", f"`{result.get('milvus_uri', 'N/A')}`", False),
            ("Auto Recall", "Enabled" if result.get("auto_recall") else "Disabled", True),
            ("Auto Remember", "Enabled" if result.get("auto_remember") else "Disabled", True),
            ("Recall Top-K", str(result.get("recall_top_k", "N/A")), True),
        ]

        await _send_success(
            interaction,
            f"Memory Stats — {project_id}",
            description="Memory subsystem is **active** and operational.",
            fields=fields,
        )

    @bot.tree.command(name="memory-search", description="Semantic search across project memory")
    @app_commands.describe(
        query="Semantic search query",
        project="Project ID (defaults to the project linked to this channel)",
        top_k="Number of results to return (default: 5)",
    )
    async def memory_search_command(
        interaction: discord.Interaction,
        query: str,
        project: str | None = None,
        top_k: int = 5,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return

        await interaction.response.defer()

        result = await handler.execute(
            "memory_search",
            {
                "project_id": project_id,
                "query": query,
                "top_k": top_k,
            },
        )
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        results = result.get("results", [])
        count = result.get("count", 0)

        if count == 0:
            await _send_info(
                interaction,
                "No Results",
                description=f"No memories matched your query in `{project_id}`.\n\n**Query:** {query}",
                followup=True,
            )
            return

        # Build result entries for the embed description
        desc_parts = [f"**Query:** {query}", f"**Project:** `{project_id}`", ""]
        for r in results:
            score = r.get("score", 0)
            source = r.get("source", "unknown")
            heading = r.get("heading", "")
            content = r.get("content", "")

            # Truncate content preview
            preview = content.replace("\n", " ").strip()
            if len(preview) > 200:
                preview = preview[:197] + "..."

            # Format source — show just the filename
            source_short = source.rsplit("/", 1)[-1] if "/" in source else source

            rank = r.get("rank", "?")
            score_pct = f"{score * 100:.1f}%" if isinstance(score, (int, float)) else "N/A"
            entry = f"**{rank}.** `{source_short}` — {score_pct}\n"
            if heading:
                entry += f"> **{heading}**\n"
            entry += f"> {preview}"
            desc_parts.append(entry)

        description = "\n\n".join(desc_parts)
        # Truncate to Discord limit if needed
        if len(description) > 4000:
            description = description[:3997] + "..."

        embed = info_embed(
            f"Memory Search — {count} result{'s' if count != 1 else ''}",
            description=description,
        )
        await interaction.followup.send(embed=embed)

    # ===================================================================
    # SYSTEM CONTROL COMMANDS
    # ===================================================================

    @bot.tree.command(name="orchestrator", description="Pause or resume task scheduling")
    @app_commands.describe(action="pause | resume | status")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="pause", value="pause"),
            app_commands.Choice(name="resume", value="resume"),
            app_commands.Choice(name="status", value="status"),
        ]
    )
    async def orchestrator_command(
        interaction: discord.Interaction, action: app_commands.Choice[str]
    ):
        result = await handler.execute("orchestrator_control", {"action": action.value})
        if action.value == "pause":
            await _send_info(
                interaction,
                "Orchestrator Paused",
                description="No new tasks will be scheduled.",
            )
        elif action.value == "resume":
            await _send_success(
                interaction,
                "Orchestrator Resumed",
                description="Task scheduling is now active.",
            )
        else:
            running = result.get("running_tasks", 0)
            is_paused = result.get("status") == "paused"
            state = "⏸ PAUSED" if is_paused else "▶ RUNNING"
            embed_fn = _send_info if is_paused else _send_success
            await embed_fn(
                interaction,
                f"Orchestrator {state}",
                description=f"**Running tasks:** {running}",
            )

    @bot.tree.command(name="restart", description="Restart the agent-queue daemon")
    @app_commands.describe(
        reason="Why are you restarting? (required)",
        wait_for_tasks="Wait for running tasks to complete before restarting (default: False)",
    )
    async def restart_command(
        interaction: discord.Interaction,
        reason: str,
        wait_for_tasks: bool = False,
    ):
        user_name = interaction.user.display_name
        full_reason = f"User {user_name} requested a restart: {reason}"

        # Gather git info for the restart message
        git_info_parts: list[str] = []
        try:
            short_hash = await _async_git_output(["rev-parse", "--short", "HEAD"], timeout=5)
            if short_hash:
                git_info_parts.append(f"commit `{short_hash}`")
        except Exception:
            pass
        try:
            await _async_git_output(["fetch", "--quiet"], timeout=15)
            behind = await _async_git_output(["rev-list", "--count", "HEAD..@{u}"], timeout=5)
            if behind and int(behind) > 0:
                git_info_parts.append(
                    f"**{behind}** commit{'s' if int(behind) != 1 else ''} behind origin"
                )
        except Exception:
            pass

        running_count = len(handler.orchestrator._running_tasks)

        if wait_for_tasks and running_count > 0:
            desc = (
                f"Agent-queue daemon will restart after {running_count} "
                f"running task(s) complete…\n**Reason:** {full_reason}"
            )
        else:
            desc = f"Agent-queue daemon is restarting…\n**Reason:** {full_reason}"
        if git_info_parts:
            desc += "\n" + " · ".join(git_info_parts)

        await _send_warning(interaction, "Restarting", description=desc)
        await handler.execute(
            "restart_daemon", {"reason": full_reason, "wait_for_tasks": wait_for_tasks}
        )

    @bot.tree.command(
        name="shutdown",
        description="Shut down the bot and all running agents",
    )
    @app_commands.describe(
        reason="Why are you shutting down? (required)",
        force="Force-stop all running agents immediately (default: graceful)",
    )
    async def shutdown_command(
        interaction: discord.Interaction,
        reason: str,
        force: bool = False,
    ):
        user_name = interaction.user.display_name
        full_reason = f"User {user_name} requested shutdown: {reason}"
        mode = "force" if force else "graceful"

        # Count running tasks for the confirmation message
        running_count = len(handler.orchestrator._running_tasks)

        # Build description
        desc_parts = [
            f"Agent-queue daemon is shutting down ({mode})…",
            f"**Reason:** {full_reason}",
        ]
        if running_count > 0:
            if force:
                desc_parts.append(f"⚠️ **{running_count}** running task(s) will be force-stopped.")
            else:
                desc_parts.append(
                    f"⏳ Waiting for **{running_count}** running task(s) to complete…"
                )
        else:
            desc_parts.append("No tasks currently running.")

        await _send_warning(interaction, "Shutting Down", description="\n".join(desc_parts))

        # Set bot status to invisible/offline before shutting down
        try:
            await bot.change_presence(status=discord.Status.invisible)
        except Exception:
            pass

        await handler.execute("shutdown", {"reason": full_reason, "force": force})

    @bot.tree.command(
        name="update",
        description="Pull latest source, install deps, and restart the daemon",
    )
    @app_commands.describe(
        reason="Why are you updating? (optional, auto-filled if omitted)",
        wait_for_tasks="Wait for running tasks to complete before restarting (default: False)",
    )
    async def update_command(
        interaction: discord.Interaction,
        reason: str | None = None,
        wait_for_tasks: bool = False,
    ):
        user_name = interaction.user.display_name
        full_reason = f"User {user_name} requested an update" + (f": {reason}" if reason else "")

        await interaction.response.defer(ephemeral=False)

        # Show current commit before pulling
        repo_dir = str(Path(__file__).resolve().parent.parent.parent)
        before_hash = ""
        try:
            before_hash = await _async_git_output(
                ["rev-parse", "--short", "HEAD"],
                cwd=repo_dir,
                timeout=5,
            )
        except Exception:
            pass

        result = await handler.execute(
            "update_and_restart",
            {"reason": full_reason, "wait_for_tasks": wait_for_tasks},
        )

        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        pull_output = result.get("pull_output", "")
        after_hash = ""
        try:
            after_hash = await _async_git_output(
                ["rev-parse", "--short", "HEAD"],
                cwd=repo_dir,
                timeout=5,
            )
        except Exception:
            pass

        desc_parts = ["Pulled latest changes and restarting…"]
        if before_hash and after_hash and before_hash != after_hash:
            desc_parts.append(f"`{before_hash}` → `{after_hash}`")
        elif before_hash and after_hash:
            desc_parts.append(f"Already up to date at `{after_hash}`")
        if pull_output and "Already up to date" not in pull_output:
            # Show abbreviated pull output
            lines = pull_output.splitlines()
            if len(lines) > 6:
                lines = lines[:6] + [f"… and {len(lines) - 6} more lines"]
            desc_parts.append("```\n" + "\n".join(lines) + "\n```")

        await _send_warning(
            interaction,
            "Updating & Restarting",
            description="\n".join(desc_parts),
            followup=True,
        )

    # ===================================================================
    # CHANNEL MANAGEMENT
    # ===================================================================

    @bot.tree.command(
        name="clear",
        description="Clear messages from the current channel",
    )
    @app_commands.describe(
        count="Number of messages to delete (default: all, max: 1000)",
    )
    async def clear_command(
        interaction: discord.Interaction,
        count: int | None = None,
    ):
        channel = interaction.channel

        # Validate the channel supports bulk deletion
        if not hasattr(channel, "purge"):
            await _send_error(
                interaction,
                "This command can only be used in text channels.",
            )
            return

        # Check bot permissions
        bot_member = interaction.guild.me if interaction.guild else None
        if bot_member:
            perms = channel.permissions_for(bot_member)
            if not perms.manage_messages:
                await _send_error(
                    interaction,
                    "I need the **Manage Messages** permission to clear messages.",
                )
                return

        limit = min(count, 1000) if count is not None else 1000

        await interaction.response.defer(ephemeral=True)

        try:
            deleted = await channel.purge(limit=limit)
            await _send_success(
                interaction,
                "Channel Cleared",
                description=f"Deleted **{len(deleted)}** message{'s' if len(deleted) != 1 else ''}.",
                followup=True,
                ephemeral=True,
            )
        except discord.Forbidden:
            await _send_error(
                interaction,
                "Missing permissions to delete messages in this channel.",
                followup=True,
            )
        except discord.HTTPException as exc:
            await _send_error(
                interaction,
                f"Failed to clear messages: {exc}",
                followup=True,
            )

    # ===================================================================
    # FILE BROWSER & EDITOR
    # ===================================================================

    def _format_file_size(size: int) -> str:
        """Format bytes into a human-readable string."""
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        else:
            return f"{size / (1024 * 1024):.1f} MB"

    class _FileBrowserView(discord.ui.View):
        """Interactive view for browsing repository files and directories."""

        def __init__(
            self,
            handler,
            project_id: str,
            current_path: str,
            directories: list[str],
            files: list[dict],
            workspace_path: str,
            workspace_name: str = "",
        ):
            super().__init__(timeout=300)
            self._handler = handler
            self._project_id = project_id
            self._current_path = current_path
            # Always resolve to absolute path to prevent CWD-relative issues
            self._workspace_path = (
                os.path.realpath(workspace_path) if workspace_path else workspace_path
            )
            self._workspace_name = workspace_name
            self._directories = directories
            self._files = files
            self._dir_page = 0
            self._file_page = 0
            self._items_per_page = 20
            self._build_buttons()

        def _build_buttons(self):
            self.clear_items()

            # Parent directory button (row 0)
            if self._current_path and self._current_path != "/":
                parent = discord.ui.Button(
                    label="⬆ Parent Directory",
                    style=discord.ButtonStyle.secondary,
                    row=0,
                )
                parent.callback = self._go_parent
                self.add_item(parent)

            # Directory select menu (row 1) — show up to 25 dirs per page
            dir_start = self._dir_page * self._items_per_page
            dir_end = dir_start + self._items_per_page
            page_dirs = self._directories[dir_start:dir_end]
            if page_dirs:
                dir_select = discord.ui.Select(
                    placeholder=f"📁 Navigate to directory... (page {self._dir_page + 1})",
                    options=[
                        discord.SelectOption(label=d[:100], value=d[:100], emoji="📁")
                        for d in page_dirs
                    ],
                    row=1,
                )
                dir_select.callback = self._navigate_dir
                self.add_item(dir_select)

            # File select menu (row 2) — show up to 25 files per page
            file_start = self._file_page * self._items_per_page
            file_end = file_start + self._items_per_page
            page_files = self._files[file_start:file_end]
            if page_files:
                file_select = discord.ui.Select(
                    placeholder=f"📄 View/edit file... (page {self._file_page + 1})",
                    options=[
                        discord.SelectOption(
                            label=f["name"][:100],
                            value=f["name"][:100],
                            description=_format_file_size(f.get("size", 0)),
                            emoji="📄",
                        )
                        for f in page_files
                    ],
                    row=2,
                )
                file_select.callback = self._view_file
                self.add_item(file_select)

            # Pagination buttons (row 3)
            total_dir_pages = max(1, -(-len(self._directories) // self._items_per_page))
            total_file_pages = max(1, -(-len(self._files) // self._items_per_page))

            if total_dir_pages > 1:
                if self._dir_page > 0:
                    prev_dir = discord.ui.Button(
                        label="◀ Prev Dirs",
                        style=discord.ButtonStyle.secondary,
                        row=3,
                    )
                    prev_dir.callback = self._prev_dir_page
                    self.add_item(prev_dir)
                if self._dir_page < total_dir_pages - 1:
                    next_dir = discord.ui.Button(
                        label="Next Dirs ▶",
                        style=discord.ButtonStyle.secondary,
                        row=3,
                    )
                    next_dir.callback = self._next_dir_page
                    self.add_item(next_dir)

            if total_file_pages > 1:
                if self._file_page > 0:
                    prev_file = discord.ui.Button(
                        label="◀ Prev Files",
                        style=discord.ButtonStyle.secondary,
                        row=3,
                    )
                    prev_file.callback = self._prev_file_page
                    self.add_item(prev_file)
                if self._file_page < total_file_pages - 1:
                    next_file = discord.ui.Button(
                        label="Next Files ▶",
                        style=discord.ButtonStyle.secondary,
                        row=3,
                    )
                    next_file.callback = self._next_file_page
                    self.add_item(next_file)

        def _build_embed(self) -> discord.Embed:
            path_display = self._current_path or "/"
            ws_label = f"\n**Workspace:** `{self._workspace_name}`" if self._workspace_name else ""
            embed = discord.Embed(
                title=f"📂 {path_display}",
                description=f"**Project:** `{self._project_id}`{ws_label}",
                color=0x3498DB,
            )
            dir_count = len(self._directories)
            file_count = len(self._files)
            embed.add_field(
                name="Contents",
                value=f"📁 {dir_count} director{'y' if dir_count == 1 else 'ies'}, "
                f"📄 {file_count} file{'s' if file_count != 1 else ''}",
                inline=False,
            )
            # Show the resolved workspace path for transparency
            if self._workspace_path:
                embed.set_footer(text=self._workspace_path)
            return embed

        async def _refresh(self, interaction: discord.Interaction):
            await interaction.response.defer()

            # Use the command handler for consistent workspace resolution.
            args: dict = {
                "project_id": self._project_id,
                "path": self._current_path or "",
            }
            if self._workspace_name:
                args["workspace"] = self._workspace_name

            result = await self._handler.execute("list_directory", args)

            if "error" in result:
                await interaction.edit_original_response(
                    content=f"❌ {result['error']}",
                    embed=None,
                    view=None,
                )
                return
            # Update workspace path from the resolved result to stay in sync
            if result.get("workspace_path"):
                self._workspace_path = result["workspace_path"]
            self._directories = result["directories"]
            self._files = result["files"]
            self._dir_page = 0
            self._file_page = 0
            self._build_buttons()
            await interaction.edit_original_response(
                content=None,
                embed=self._build_embed(),
                view=self,
            )

        async def _go_parent(self, interaction: discord.Interaction):
            if self._current_path and self._current_path != "/":
                parts = self._current_path.rstrip("/").rsplit("/", 1)
                self._current_path = parts[0] if len(parts) > 1 else ""
            else:
                self._current_path = ""
            await self._refresh(interaction)

        async def _navigate_dir(self, interaction: discord.Interaction):
            selected = interaction.data["values"][0]
            if self._current_path and self._current_path != "/":
                self._current_path = f"{self._current_path}/{selected}"
            else:
                self._current_path = selected
            await self._refresh(interaction)

        async def _view_file(self, interaction: discord.Interaction):
            selected = interaction.data["values"][0]
            if self._current_path and self._current_path != "/":
                file_rel = f"{self._current_path}/{selected}"
            else:
                file_rel = selected
            file_full = f"{self._workspace_path}/{file_rel}"

            # Get file size synchronously — fast stat call, no need for thread.
            try:
                file_size = os.path.getsize(os.path.realpath(file_full))
            except OSError:
                file_size = 0

            # Respond immediately with file info and action buttons.
            # No file I/O happens here — just metadata we already have.
            embed = discord.Embed(
                title=f"📄 {selected}",
                color=0x3498DB,
            )
            embed.add_field(name="Path", value=f"`{file_rel}`", inline=False)
            embed.add_field(name="Size", value=_format_file_size(file_size), inline=True)

            view = _FileInfoView(
                handler=self._handler,
                file_path=file_full,
                file_rel=file_rel,
                project_id=self._project_id,
            )
            await interaction.response.send_message(
                embed=embed,
                view=view,
                ephemeral=True,
            )

        async def _prev_dir_page(self, interaction: discord.Interaction):
            self._dir_page = max(0, self._dir_page - 1)
            self._build_buttons()
            await interaction.response.defer()
            await interaction.edit_original_response(embed=self._build_embed(), view=self)

        async def _next_dir_page(self, interaction: discord.Interaction):
            self._dir_page += 1
            self._build_buttons()
            await interaction.response.defer()
            await interaction.edit_original_response(embed=self._build_embed(), view=self)

        async def _prev_file_page(self, interaction: discord.Interaction):
            self._file_page = max(0, self._file_page - 1)
            self._build_buttons()
            await interaction.response.defer()
            await interaction.edit_original_response(embed=self._build_embed(), view=self)

        async def _next_file_page(self, interaction: discord.Interaction):
            self._file_page += 1
            self._build_buttons()
            await interaction.response.defer()
            await interaction.edit_original_response(embed=self._build_embed(), view=self)

    class _FileInfoView(discord.ui.View):
        """View shown when a file is selected — offers View Content and Edit buttons."""

        def __init__(self, handler, file_path: str, file_rel: str, project_id: str):
            super().__init__(timeout=300)
            self._handler = handler
            self._file_path = file_path
            self._file_rel = file_rel
            self._project_id = project_id

        @discord.ui.button(label="👁️ View Content", style=discord.ButtonStyle.secondary)
        async def view_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.defer(ephemeral=True)

            def _read_file_sync():
                real = os.path.realpath(self._file_path)
                if not os.path.isfile(real):
                    return {"error": f"File not found: {self._file_rel}"}
                try:
                    with open(real, "r") as f:
                        content = f.read(64_000)  # ~64 KB max
                    return {"content": content}
                except UnicodeDecodeError:
                    return {"error": "Binary file — cannot display contents"}
                except OSError as exc:
                    return {"error": str(exc)}

            result = await asyncio.to_thread(_read_file_sync)

            if "error" in result:
                await interaction.followup.send(
                    embed=error_embed("Error", description=result["error"]),
                    ephemeral=True,
                )
                return

            content = result["content"]
            # Determine file extension for syntax highlighting
            ext = os.path.splitext(self._file_rel)[1].lstrip(".")

            # Send as a file attachment — works for any size, no truncation issues
            buf = io.BytesIO(content.encode("utf-8"))
            filename = os.path.basename(self._file_rel)
            file = discord.File(buf, filename=filename)

            # Also include a short inline preview
            preview = content[:1800]
            if len(content) > 1800:
                preview += "\n… (full content attached above)"
            lang = ext if ext else ""
            await interaction.followup.send(
                f"### 📄 `{self._file_rel}`\n```{lang}\n{preview}\n```",
                file=file,
                ephemeral=True,
            )

        @discord.ui.button(label="✏️ Edit File", style=discord.ButtonStyle.primary)
        async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            # Read the current content for the modal directly — skip handler overhead
            def _read_for_edit():
                real = os.path.realpath(self._file_path)
                if not os.path.isfile(real):
                    return {"error": f"File not found: {self._file_rel}"}
                try:
                    with open(real, "r") as f:
                        content = f.read(4000)  # Modal limit is 4000 chars
                    return {"content": content}
                except UnicodeDecodeError:
                    return {"error": "Binary file — cannot edit"}
                except OSError as exc:
                    return {"error": str(exc)}

            result = await asyncio.to_thread(_read_for_edit)

            if "error" in result:
                await interaction.response.send_message(
                    embed=error_embed("Error", description=result["error"]),
                    ephemeral=True,
                )
                return
            content = result.get("content", "")
            modal = _FileEditModal(
                handler=self._handler,
                file_path=self._file_path,
                file_rel=self._file_rel,
                current_content=content,
            )
            await interaction.response.send_modal(modal)

    class _FileEditModal(discord.ui.Modal, title="Edit File"):
        """Modal dialog for editing a text file's contents."""

        content_input = discord.ui.TextInput(
            label="File Content",
            style=discord.TextStyle.long,
            required=True,
            max_length=4000,
        )

        def __init__(self, handler, file_path: str, file_rel: str, current_content: str):
            super().__init__()
            self._handler = handler
            self._file_path = file_path
            self._file_rel = file_rel
            self.content_input.default = current_content
            self.title = f"Edit: {file_rel[-40:]}" if len(file_rel) > 45 else f"Edit: {file_rel}"

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            new_content = self.content_input.value
            result = await self._handler.execute(
                "write_file",
                {"path": self._file_path, "content": new_content},
            )
            if "error" in result:
                await interaction.followup.send(
                    embed=error_embed("Save Failed", description=result["error"]),
                    ephemeral=True,
                )
                return
            written = result.get("written", 0)
            await interaction.followup.send(
                embed=success_embed(
                    "File Saved ✅",
                    description=f"**`{self._file_rel}`** saved successfully.\n"
                    f"Wrote {written:,} characters.",
                ),
                ephemeral=True,
            )

    @bot.tree.command(
        name="browse",
        description="Browse project repository files and directories",
    )
    @app_commands.describe(
        path="Subdirectory to start browsing from (default: root)",
        workspace="Workspace name or ID to browse (default: first workspace)",
    )
    async def browse_command(
        interaction: discord.Interaction,
        path: str | None = None,
        workspace: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        await interaction.response.defer()

        args: dict = {"project_id": project_id}
        if path:
            args["path"] = path
        if workspace:
            args["workspace"] = workspace
        result = await handler.execute("list_directory", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        view = _FileBrowserView(
            handler=handler,
            project_id=project_id,
            current_path=result["path"] if result["path"] != "/" else "",
            directories=result["directories"],
            files=result["files"],
            workspace_path=result["workspace_path"],
            workspace_name=result.get("workspace_name", ""),
        )
        await interaction.followup.send(embed=view._build_embed(), view=view)

    @browse_command.autocomplete("workspace")
    async def _browse_workspace_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            return []
        workspaces = await handler.db.list_workspaces(project_id)
        choices = []
        for ws in workspaces:
            label = ws.name or ws.id
            if current and current.lower() not in label.lower():
                continue
            locked = " 🔒" if ws.locked_by_agent_id else ""
            choices.append(app_commands.Choice(name=f"{label}{locked}", value=ws.name or ws.id))
            if len(choices) >= 25:
                break
        return choices

    @bot.tree.command(
        name="edit-file",
        description="Open a text editor dialog for any file in the project",
    )
    @app_commands.describe(
        path="Relative file path within the project workspace (e.g. src/main.py)",
    )
    async def edit_file_command(
        interaction: discord.Interaction,
        path: str,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)

        ws_path = await handler.db.get_project_workspace_path(project_id)
        if not ws_path:
            await _send_error(interaction, f"Project '{project_id}' has no workspaces.")
            return

        file_full = f"{ws_path}/{path}"
        result = await handler.execute(
            "read_file",
            {"path": file_full, "max_lines": 4000},
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return

        content = result.get("content", "")
        if len(content) > 4000:
            content = content[:4000]

        modal = _FileEditModal(
            handler=handler,
            file_path=file_full,
            file_rel=path,
            current_content=content,
        )
        await interaction.response.send_modal(modal)

    # ===================================================================
    # PLAN PROCESSING
    # ===================================================================

    @bot.tree.command(
        name="process-plan",
        description="Scan project workspaces for plan files and present for approval",
    )
    @app_commands.describe(
        project_id="Project to scan (uses channel's project if omitted)",
        task_id="Existing task to attach the plan to (creates new task if omitted)",
    )
    async def process_plan_command(
        interaction: discord.Interaction,
        project_id: str | None = None,
        task_id: str | None = None,
    ):
        await interaction.response.defer()

        resolved_project = await _resolve_project_from_context(interaction, project_id)
        if not resolved_project:
            await _send_error(interaction, _NO_PROJECT_MSG, followup=True)
            return

        cmd_args: dict = {"project_id": resolved_project}
        if task_id:
            cmd_args["task_id"] = task_id

        result = await handler.execute("process_plan", cmd_args)

        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        status = result.get("status")
        if status == "no_plans_found":
            await _send_info(
                interaction,
                "No Plans Found",
                description=(
                    f"Scanned {result.get('workspaces_scanned', 0)} workspace(s) "
                    f"for project `{resolved_project}` — no plan files found.\n\n"
                    "Plan files are expected at: `.claude/plan.md`, `plan.md`, "
                    "`docs/plan.md`, `docs/plans/*.md`, or `plans/*.md`."
                ),
                followup=True,
            )
            return

        # Plan found and queued for approval
        desc_lines = [
            "📋 Found plan in workspace and queued for approval.",
            "",
            f"**Task:** `{result['task_id']}`",
            f"**Title:** {result.get('title', 'N/A')}",
            f"**Source:** `{result.get('plan_path', 'N/A')}`",
        ]
        if result.get("additional_plans"):
            desc_lines.append(
                f"\n⚠️ {result['additional_plans']} additional plan file(s) found. "
                f"Run `/process-plan` again to process them."
            )

        await _send_success(
            interaction,
            "Plan Queued for Approval",
            description="\n".join(desc_lines),
            followup=True,
        )

    # ===================================================================
    # PLUGIN MANAGEMENT
    # ===================================================================

    @bot.tree.command(name="plugin-list", description="List all installed plugins")
    async def plugin_list_command(interaction: discord.Interaction):
        await interaction.response.defer()
        result = await handler.execute("plugin_list", {})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        plugins = result.get("plugins", [])
        if not plugins:
            await _send_info(
                interaction,
                "No Plugins",
                description="No plugins are currently installed.",
                followup=True,
            )
            return
        lines = []
        for p in plugins:
            status = "✅" if p.get("enabled") else "❌"
            version = p.get("version", "unknown")
            lines.append(
                f"{status} **{p['name']}** `v{version}` — {p.get('description', 'No description')}"
            )
        await _send_success(
            interaction,
            f"Installed Plugins ({len(plugins)})",
            description="\n".join(lines),
            followup=True,
        )

    @bot.tree.command(
        name="plugin-install",
        description="Install a plugin from a git repository",
    )
    @app_commands.describe(
        url="Git repository URL for the plugin",
        branch="Git branch to install from (optional)",
    )
    async def plugin_install_command(
        interaction: discord.Interaction,
        url: str,
        branch: str | None = None,
    ):
        await interaction.response.defer()
        args: dict = {"url": url}
        if branch:
            args["branch"] = branch
        result = await handler.execute("plugin_install", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        name = result.get("name", "unknown")
        version = result.get("version", "unknown")
        await _send_success(
            interaction,
            "Plugin Installed",
            description=f"**{name}** `v{version}` has been installed successfully.",
            followup=True,
            result=result,
        )

    @bot.tree.command(name="plugin-update", description="Update an installed plugin")
    @app_commands.describe(name="Name of the plugin to update")
    async def plugin_update_command(interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        result = await handler.execute("plugin_update", {"name": name})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        old_ver = result.get("old_version", "unknown")
        new_ver = result.get("version", "unknown")
        desc = f"**{name}** updated from `v{old_ver}` to `v{new_ver}`."
        await _send_success(
            interaction,
            "Plugin Updated",
            description=desc,
            followup=True,
            result=result,
        )

    @bot.tree.command(name="plugin-remove", description="Remove an installed plugin")
    @app_commands.describe(name="Name of the plugin to remove")
    async def plugin_remove_command(interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        result = await handler.execute("plugin_remove", {"name": name})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        await _send_success(
            interaction,
            "Plugin Removed",
            description=f"**{name}** has been removed.",
            followup=True,
            result=result,
        )

    @bot.tree.command(
        name="plugin-toggle",
        description="Enable or disable an installed plugin",
    )
    @app_commands.describe(name="Name of the plugin to toggle")
    async def plugin_toggle_command(interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        result = await handler.execute("plugin_toggle", {"name": name})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        enabled = result.get("enabled", False)
        state = "enabled" if enabled else "disabled"
        emoji = "✅" if enabled else "❌"
        await _send_success(
            interaction,
            "Plugin Toggled",
            description=f"{emoji} **{name}** is now **{state}**.",
            followup=True,
            result=result,
        )

    @bot.tree.command(name="plugin-info", description="Show detailed plugin information")
    @app_commands.describe(name="Name of the plugin")
    async def plugin_info_command(interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        result = await handler.execute("plugin_info", {"name": name})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        plugin = result.get("plugin", result)
        enabled = "Enabled" if plugin.get("enabled") else "Disabled"
        fields = [
            ("Version", f"`{plugin.get('version', 'unknown')}`", True),
            ("Status", enabled, True),
            ("Author", plugin.get("author", "unknown"), True),
        ]
        if plugin.get("url"):
            fields.append(("Repository", plugin["url"], False))
        hooks = plugin.get("hooks", [])
        if hooks:
            fields.append(("Hooks", ", ".join(f"`{h}`" for h in hooks), False))
        commands_list = plugin.get("commands", [])
        if commands_list:
            fields.append(("Commands", ", ".join(f"`{c}`" for c in commands_list), False))
        await _send_info(
            interaction,
            f"Plugin: {plugin.get('name', name)}",
            description=plugin.get("description", "No description available."),
            fields=fields,
            followup=True,
        )

    @bot.tree.command(
        name="plugin-config",
        description="Show configuration for a plugin",
    )
    @app_commands.describe(name="Name of the plugin")
    async def plugin_config_command(interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        result = await handler.execute("plugin_config", {"name": name})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        config = result.get("config", {})
        if not config:
            await _send_info(
                interaction,
                f"Plugin Config: {name}",
                description="This plugin has no configuration.",
                followup=True,
            )
            return
        lines = []
        for key, value in config.items():
            lines.append(f"**{key}:** `{value}`")
        await _send_info(
            interaction,
            f"Plugin Config: {name}",
            description="\n".join(lines),
            followup=True,
        )

    @bot.tree.command(name="plugin-reload", description="Reload a plugin")
    @app_commands.describe(name="Name of the plugin to reload")
    async def plugin_reload_command(interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        result = await handler.execute("plugin_reload", {"name": name})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        await _send_success(
            interaction,
            "Plugin Reloaded",
            description=f"**{name}** has been reloaded successfully.",
            followup=True,
            result=result,
        )

    # ===================================================================
    # INTERACTIVE MENU
    # ===================================================================

    @bot.tree.command(
        name="menu",
        description="Show an interactive control panel with clickable buttons",
    )
    async def menu_command(interaction: discord.Interaction):
        # Build a quick status summary for the menu message
        result = await handler.execute("get_status", {})
        tasks = result.get("tasks", {})
        by_status = tasks.get("by_status", {})
        total = tasks.get("total", 0)
        completed = by_status.get("COMPLETED", 0)
        in_progress = by_status.get("IN_PROGRESS", 0) + by_status.get("ASSIGNED", 0)
        failed = by_status.get("FAILED", 0)
        blocked = by_status.get("BLOCKED", 0)
        ready = by_status.get("READY", 0)

        agents = result.get("agents", [])
        busy_count = sum(1 for a in agents if a.get("state") == "BUSY")

        lines = ["## 🎛️ Agent Queue — Control Panel"]
        if result.get("orchestrator_paused"):
            lines.append("⏸ **Orchestrator is PAUSED**")
        else:
            lines.append("▶ Orchestrator running")

        bar = progress_bar(completed, total, width=12) if total > 0 else "—"
        lines.append(f"**Progress:** {bar}")
        lines.append(
            f"📊 {total} tasks — {in_progress} active, {ready} ready, "
            f"{failed} failed, {blocked} blocked"
        )
        lines.append(f"🤖 {len(agents)} agents — {busy_count} busy")
        lines.append("\n_Use the buttons below to view details and take actions._")

        view = MenuView(handler=handler, bot=bot)
        await interaction.response.send_message("\n".join(lines), view=view)
