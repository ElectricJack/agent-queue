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

_RULES_PER_PAGE = 6


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


class _EditRuleModal(discord.ui.Modal, title="Edit Rule"):
    """Modal for editing an existing rule's content and type."""

    rule_type_input = discord.ui.TextInput(
        label="Type (active or passive)",
        placeholder="active",
        required=True,
        max_length=10,
    )
    content_input = discord.ui.TextInput(
        label="Content (markdown)",
        style=discord.TextStyle.long,
        placeholder="# Rule Name\n\n## Trigger\n...\n\n## Logic\n...",
        required=True,
        max_length=4000,
    )

    def __init__(
        self,
        rule_id: str,
        project_id: str | None,
        current_type: str,
        current_content: str,
        handler,
        parent_view: "_RuleContentView",
    ) -> None:
        super().__init__()
        self._rule_id = rule_id
        self._project_id = project_id
        self._handler = handler
        self._parent_view = parent_view
        self.rule_type_input.default = current_type
        self.content_input.default = current_content[:4000] if current_content else ""

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rule_type = self.rule_type_input.value.strip().lower()
        if rule_type not in ("active", "passive"):
            await interaction.response.send_message(
                "Type must be 'active' or 'passive'.",
                ephemeral=True,
            )
            return

        # Defer immediately — save_rule may trigger hook generation for active
        # rules (DB operations) and the 3-second Discord timeout is too tight.
        # Using thinking=False sends DEFERRED_UPDATE_MESSAGE so we can later
        # edit the parent message that holds the rule content view.
        await interaction.response.defer()

        content = self.content_input.value
        result = await self._handler.execute(
            "save_rule",
            {
                "id": self._rule_id,
                "project_id": self._project_id,
                "type": rule_type,
                "content": content,
            },
        )
        if "error" in result:
            await interaction.followup.send(
                f"❌ Error saving rule: {result['error']}",
                ephemeral=True,
            )
            return

        # Reload the rule to refresh the preview
        loaded = await self._handler.execute("load_rule", {"id": self._rule_id})
        if "error" not in loaded:
            self._parent_view._result = loaded
            # Update rule summary dict with new name/type
            self._parent_view._rule["type"] = loaded.get("type", rule_type)
            if loaded.get("name"):
                self._parent_view._rule["name"] = loaded["name"]
            self._parent_view._rebuild()
            try:
                await interaction.edit_original_response(
                    content=self._parent_view.build_content(),
                    view=self._parent_view,
                )
                return
            except Exception:
                pass  # Fall through to followup message below

        hooks = result.get("hooks_generated", [])
        hook_msg = f" ({len(hooks)} hook(s) regenerated)" if hooks else ""
        await interaction.followup.send(
            f"✅ Rule `{self._rule_id}` updated{hook_msg}.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        try:
            msg = f"❌ Error editing rule: {error}"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass  # interaction may have expired


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

        edit_btn = discord.ui.Button(
            label="Edit",
            style=discord.ButtonStyle.secondary,
            emoji="✏️",
            row=0,
        )
        edit_btn.callback = self._edit_callback
        self.add_item(edit_btn)

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
        result = await self._handler.execute("toggle_rule", {"id": rule_id, "enabled": new_enabled})
        if "error" in result:
            await interaction.response.send_message(f"\u274c {result['error']}", ephemeral=True)
            return
        result.get("action", "toggled")
        result.get("hooks_updated", 0)
        result.get("total_hooks", 0)
        # Update local state so rebuild reflects it
        if "execution_info" not in self._result:
            self._result["execution_info"] = {}
        self._result["execution_info"]["enabled"] = new_enabled
        self._rebuild()
        await interaction.response.edit_message(
            content=self.build_content(),
            view=self,
        )

    async def _edit_callback(self, interaction: discord.Interaction) -> None:
        """Open a modal to edit the rule content."""
        rule_id = self._rule.get("id")
        project_id = self._result.get("project_id")
        current_type = self._result.get("type", "passive")
        current_content = self._result.get("content", "")
        modal = _EditRuleModal(
            rule_id=rule_id,
            project_id=project_id,
            current_type=current_type,
            current_content=current_content,
            handler=self._handler,
            parent_view=self,
        )
        await interaction.response.send_modal(modal)

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
        """Build an embed with a markdown table of rules."""
        if not self._rules:
            return make_embed(
                EmbedStyle.INFO,
                "Rules",
                description="No rules configured.",
            )

        start = self.page * _RULES_PER_PAGE
        page_rules = self._rules[start : start + _RULES_PER_PAGE]

        # Status emoji mapping
        _status_emoji = {True: "\u2705", False: "\u23f8\ufe0f", "mixed": "\u26a0\ufe0f"}

        # Build markdown table
        rows: list[str] = []
        rows.append("| Rule | Scope | Status | Description |")
        rows.append("|------|-------|--------|-------------|")

        for r in page_rules:
            name = r.get("name") or r.get("id", "?")
            rule_type = r.get("type", "passive")
            scope = r.get("project_id") or "global"
            summary = r.get("summary", "")
            enabled = r.get("enabled")
            hook_count = r.get("hook_count", 0)

            # Type emoji prefix
            type_icon = "\u26a1" if rule_type == "active" else "\U0001f4d6"

            # Status cell
            status_icon = _status_emoji.get(enabled, "\u2796")
            hook_note = f" · {hook_count}h" if hook_count else ""
            status_cell = f"{status_icon}{hook_note}"

            # Truncate description for table cell
            desc = summary[:100].rstrip() if summary else "*No description*"
            if summary and len(summary) > 100:
                desc += "\u2026"
            # Escape pipes in description to avoid breaking markdown table
            desc = desc.replace("|", "\\|")

            rows.append(f"| {type_icon} **{name}** | `{scope}` | {status_cell} | {desc} |")

        body = "\n".join(rows)

        # Footer
        footer_parts = []
        if self.total_pages > 1:
            footer_parts.append(f"Page {self.page + 1}/{self.total_pages}")
        footer_parts.append("AgentQueue")

        return make_embed(
            EmbedStyle.INFO,
            f"Rules ({len(self._rules)})",
            description=body,
            footer=" \u00b7 ".join(footer_parts),
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
            # Refresh header lines if this is a status view (orchestrator state only)
            if view._show_agent_header:
                status_result = await self._handler.execute("get_status", {})
                header: list[str] = []
                if status_result.get("orchestrator_paused"):
                    header.append("⏸ **Orchestrator is PAUSED** — scheduling suspended")
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
    # orchestrator state in header_lines for a consolidated view.

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

            # Get orchestrator status for the header (scoped to project)
            status_args: dict = {}
            if project_id:
                status_args["project_id"] = project_id
            status_result = await handler.execute("get_status", status_args)

            # Build header lines — orchestrator state only (no agent info)
            header_lines: list[str] = []
            if status_result.get("orchestrator_paused"):
                header_lines.append("⏸ **Orchestrator is PAUSED** — scheduling suspended")

            # Fetch active tasks only — status focuses on current work
            args: dict = {
                "include_completed": False,
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

    @bot.tree.command(name="channel-map", description="Show all project-to-channel mappings")
    async def channel_map_command(interaction: discord.Interaction):
        result = await handler.execute("list_projects", {})
        projects = result.get("projects", [])
        if not projects:
            await _send_info(interaction, "No Projects", description="No projects configured.")
            return
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

    @bot.tree.command(name="set-default-branch", description="Set the default branch for a project")
    @app_commands.describe(
        project_id="Project ID",
        branch="Branch name to use as default (e.g. dev, main)",
    )
    async def set_default_branch_command(
        interaction: discord.Interaction,
        project_id: str,
        branch: str,
    ):
        await interaction.response.defer()
        result = await handler.execute(
            "set_default_branch", {"project_id": project_id, "branch": branch}
        )
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        desc = (
            f"Project `{project_id}` default branch set to `{result['default_branch']}`"
            f"\n(was `{result['previous_branch']}`)"
        )
        if result.get("branch_created"):
            desc += f"\n\nBranch `{branch}` was created on the remote."
        await _send_success(
            interaction, "Default Branch Updated",
            description=desc, followup=True,
        )

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

    @bot.tree.command(name="process-plan", description="Scan workspaces for plan.md files and process them")
    @app_commands.describe(project="Project ID (defaults to channel's project)")
    async def process_plan_command(
        interaction: discord.Interaction,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        result = await handler.execute("process_plan", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        await _send_success(
            interaction, "Plan Processed",
            description=result.get("message", "Plan processing complete."),
            followup=True, result=result,
        )

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

        if result.get("already_queued") or result.get("already_synced"):
            await _send_info(
                interaction,
                "Sync Workspaces — No Action Needed",
                description=result.get("message", "Already up to date."),
            )
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
    # PLAYBOOK COMMANDS
    # ===================================================================

    @bot.tree.command(name="playbook-list", description="List all playbooks across scopes")
    @app_commands.describe(
        scope="Filter by scope type (system, project, agent-type)",
    )
    async def playbook_list_command(
        interaction: discord.Interaction,
        scope: str | None = None,
    ):
        await interaction.response.defer()
        args: dict = {}
        if scope:
            args["scope"] = scope
        result = await handler.execute("list_playbooks", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        playbooks = result.get("playbooks", [])
        count = result.get("count", len(playbooks))

        if count == 0:
            await _send_info(
                interaction,
                "No Playbooks",
                description="No compiled playbooks found. Use `/playbook-compile` to compile one.",
                followup=True,
            )
            return

        desc_parts = []
        for p in playbooks:
            pid = p.get("id", "?")
            p_scope = p.get("scope", "?")
            triggers = ", ".join(
                t if isinstance(t, str) else t.get("event_type", "?")
                for t in p.get("triggers", [])
            )
            status_icon = "🟢" if p.get("status") == "active" else "⚠️"
            cooldown = p.get("cooldown_seconds", "—")
            last_run = p.get("last_run", "never")
            desc_parts.append(
                f"{status_icon} **{pid}** (`{p_scope}`)\n"
                f"> Triggers: `{triggers}`\n"
                f"> Cooldown: {cooldown}s · Last run: {last_run}"
            )

        description = "\n\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed(
            f"Playbooks — {count} found",
            description=description,
        )
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="playbook-compile", description="Compile a playbook markdown file")
    @app_commands.describe(
        path="Absolute path to the playbook .md file",
        force="Force recompilation even if source is unchanged",
    )
    async def playbook_compile_command(
        interaction: discord.Interaction,
        path: str,
        force: bool = False,
    ):
        await interaction.response.defer()
        args: dict = {"path": path}
        if force:
            args["force"] = True
        result = await handler.execute("compile_playbook", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        playbook_id = result.get("playbook_id", "?")
        version = result.get("version", "?")
        nodes = result.get("node_count", "?")
        await _send_success(
            interaction,
            "Playbook Compiled",
            fields=[
                ("ID", f"`{playbook_id}`", True),
                ("Version", str(version), True),
                ("Nodes", str(nodes), True),
            ],
            followup=True,
            result=result,
        )

    @bot.tree.command(name="playbook-graph", description="Show a playbook's compiled graph")
    @app_commands.describe(
        playbook_id="The playbook identifier to render",
        format="Output format: ascii or mermaid",
        show_prompts="Include prompt previews in node labels",
    )
    @app_commands.choices(
        format=[
            app_commands.Choice(name="ascii", value="ascii"),
            app_commands.Choice(name="mermaid", value="mermaid"),
        ]
    )
    async def playbook_graph_command(
        interaction: discord.Interaction,
        playbook_id: str,
        format: app_commands.Choice[str] | None = None,
        show_prompts: bool = False,
    ):
        await interaction.response.defer()
        args: dict = {"playbook_id": playbook_id, "show_prompts": show_prompts}
        if format:
            args["format"] = format.value
        result = await handler.execute("show_playbook_graph", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        graph_text = result.get("graph", result.get("output", str(result)))
        await _send_long_interaction(
            f"```\n{graph_text}\n```",
            interaction.followup.send,
            filename=f"{playbook_id}-graph.txt",
        )

    @bot.tree.command(name="playbook-dry-run", description="Simulate a playbook without side effects")
    @app_commands.describe(
        playbook_id="The compiled playbook ID to simulate",
    )
    async def playbook_dry_run_command(
        interaction: discord.Interaction,
        playbook_id: str,
    ):
        await interaction.response.defer()
        result = await handler.execute("dry_run_playbook", {"playbook_id": playbook_id})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        nodes_visited = result.get("nodes_visited", [])
        final_status = result.get("status", "?")
        tokens = result.get("total_tokens", "?")

        desc_parts = [f"**Status:** {final_status}", f"**Tokens:** {tokens}", ""]
        for node in nodes_visited:
            name = node if isinstance(node, str) else node.get("node_id", "?")
            desc_parts.append(f"→ `{name}`")

        await _send_info(
            interaction,
            f"Dry Run — {playbook_id}",
            description="\n".join(desc_parts),
            followup=True,
        )

    @bot.tree.command(name="playbook-run", description="Manually trigger a playbook run")
    @app_commands.describe(
        playbook_id="The compiled playbook ID to execute",
    )
    async def playbook_run_command(
        interaction: discord.Interaction,
        playbook_id: str,
    ):
        await interaction.response.defer()
        result = await handler.execute(
            "run_playbook", {"playbook_id": playbook_id, "event": {"type": "manual"}}
        )
        if "error" in result and "run_id" not in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        run_id = result.get("run_id", "?")
        status = result.get("status", "?")
        tokens = result.get("tokens_used", 0)
        node_count = result.get("node_count", 0)

        desc_parts = [
            f"**Run ID:** `{run_id}`",
            f"**Status:** {status}",
            f"**Nodes visited:** {node_count}",
            f"**Tokens used:** {tokens}",
        ]
        if result.get("error"):
            desc_parts.append(f"**Error:** {result['error']}")

        await _send_info(
            interaction,
            f"Playbook Run — {playbook_id}",
            description="\n".join(desc_parts),
            followup=True,
        )

    @bot.tree.command(name="playbook-runs", description="List recent playbook runs")
    @app_commands.describe(
        playbook_id="Filter to a specific playbook",
        status="Filter by run status",
        limit="Max results (default 20)",
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name="running", value="running"),
            app_commands.Choice(name="paused", value="paused"),
            app_commands.Choice(name="completed", value="completed"),
            app_commands.Choice(name="failed", value="failed"),
            app_commands.Choice(name="timed_out", value="timed_out"),
        ]
    )
    async def playbook_runs_command(
        interaction: discord.Interaction,
        playbook_id: str | None = None,
        status: app_commands.Choice[str] | None = None,
        limit: int = 20,
    ):
        await interaction.response.defer()
        args: dict = {"limit": limit}
        if playbook_id:
            args["playbook_id"] = playbook_id
        if status:
            args["status"] = status.value
        result = await handler.execute("list_playbook_runs", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        runs = result.get("runs", [])
        if not runs:
            await _send_info(
                interaction,
                "No Playbook Runs",
                description="No runs match the given filters.",
                followup=True,
            )
            return

        status_icons = {
            "running": "▶️", "paused": "⏸", "completed": "✅",
            "failed": "❌", "timed_out": "⏰",
        }
        desc_parts = []
        for r in runs:
            icon = status_icons.get(r.get("status", ""), "⚪")
            rid = r.get("run_id", "?")[:12]
            pid = r.get("playbook_id", "?")
            run_status = r.get("status", "?")
            path = " → ".join(r.get("node_path", [])) if r.get("node_path") else "—"
            desc_parts.append(
                f"{icon} `{rid}…` **{pid}** — {run_status}\n> Path: {path}"
            )

        description = "\n\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed(f"Playbook Runs — {len(runs)} found", description=description)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="playbook-inspect", description="Inspect a playbook run in detail")
    @app_commands.describe(run_id="The playbook run ID to inspect")
    async def playbook_inspect_command(
        interaction: discord.Interaction,
        run_id: str,
    ):
        await interaction.response.defer()
        result = await handler.execute("inspect_playbook_run", {"run_id": run_id})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        playbook_id = result.get("playbook_id", "?")
        run_status = result.get("status", "?")
        tokens = result.get("total_tokens", "?")
        node_trace = result.get("node_trace", [])

        desc_parts = [
            f"**Playbook:** `{playbook_id}`",
            f"**Status:** {run_status}",
            f"**Total tokens:** {tokens}",
            "",
        ]
        for node in node_trace:
            name = node.get("node_id", "?") if isinstance(node, dict) else str(node)
            node_tokens = node.get("tokens", "") if isinstance(node, dict) else ""
            token_str = f" ({node_tokens} tokens)" if node_tokens else ""
            desc_parts.append(f"→ `{name}`{token_str}")

        description = "\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed(f"Run {run_id[:16]}…", description=description)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="playbook-resume", description="Resume a paused playbook run")
    @app_commands.describe(
        run_id="The paused playbook run ID",
        human_input="Your review decision or feedback",
    )
    async def playbook_resume_command(
        interaction: discord.Interaction,
        run_id: str,
        human_input: str,
    ):
        await interaction.response.defer()
        result = await handler.execute(
            "resume_playbook", {"run_id": run_id, "human_input": human_input}
        )
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        await _send_success(
            interaction,
            "Playbook Resumed",
            description=f"Run `{run_id[:16]}…` has been resumed with your input.",
            followup=True,
            result=result,
        )

    @bot.tree.command(name="playbook-health", description="Playbook health metrics")
    @app_commands.describe(
        playbook_id="Filter to a specific playbook (omit for all)",
        limit="Max runs to analyse (default 200)",
    )
    async def playbook_health_command(
        interaction: discord.Interaction,
        playbook_id: str | None = None,
        limit: int = 200,
    ):
        await interaction.response.defer()
        args: dict = {"limit": limit}
        if playbook_id:
            args["playbook_id"] = playbook_id
        result = await handler.execute("playbook_health", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        # Format health metrics
        playbooks = result.get("playbooks", [result] if "playbook_id" in result else [])
        if not playbooks:
            await _send_info(
                interaction, "No Health Data",
                description="No playbook run data available yet.",
                followup=True,
            )
            return

        desc_parts = []
        for p in playbooks:
            pid = p.get("playbook_id", "?")
            total_runs = p.get("total_runs", 0)
            success_rate = p.get("success_rate", 0)
            avg_duration = p.get("avg_duration_seconds", 0)
            avg_tokens = p.get("avg_tokens", 0)
            desc_parts.append(
                f"**{pid}**\n"
                f"> Runs: {total_runs} · Success: {success_rate:.0%}\n"
                f"> Avg duration: {avg_duration:.1f}s · Avg tokens: {avg_tokens:.0f}"
            )

        description = "\n\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed("Playbook Health", description=description)
        await interaction.followup.send(embed=embed)

    # ===================================================================
    # MEMORY COMMANDS (NEW)
    # ===================================================================

    @bot.tree.command(name="memory-save", description="Save an insight to project memory")
    @app_commands.describe(
        content="The insight or learning to save",
        project="Project ID (defaults to channel's project)",
        topic="Topic for filtering (e.g. 'authentication', 'testing')",
        tags="Comma-separated tags (e.g. 'insight,auth,important')",
    )
    async def memory_save_command(
        interaction: discord.Interaction,
        content: str,
        project: str | None = None,
        topic: str | None = None,
        tags: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return

        args: dict = {"project_id": project_id, "content": content}
        if topic:
            args["topic"] = topic
        if tags:
            args["tags"] = [t.strip() for t in tags.split(",")]
        result = await handler.execute("memory_save", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return

        preview = content[:100] + "..." if len(content) > 100 else content
        await _send_success(
            interaction,
            "Memory Saved",
            description=f"Saved to `{project_id}` memory:\n> {preview}",
            result=result,
        )

    @bot.tree.command(name="memory-list", description="Browse memories in a scope")
    @app_commands.describe(
        project="Project ID (defaults to channel's project)",
        topic="Filter by topic",
        tag="Filter by tag",
        limit="Max entries (default 20)",
    )
    async def memory_list_command(
        interaction: discord.Interaction,
        project: str | None = None,
        topic: str | None = None,
        tag: str | None = None,
        limit: int = 20,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return

        await interaction.response.defer()
        args: dict = {"project_id": project_id, "limit": limit}
        if topic:
            args["topic"] = topic
        if tag:
            args["tag"] = tag
        result = await handler.execute("memory_list", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        entries = result.get("entries", [])
        if not entries:
            await _send_info(
                interaction, "No Memories",
                description=f"No memories found in `{project_id}`.",
                followup=True,
            )
            return

        desc_parts = [f"**Project:** `{project_id}` — {len(entries)} entries\n"]
        for e in entries:
            heading = e.get("heading", "")
            entry_topic = e.get("topic", "")
            preview = e.get("content", "")[:120].replace("\n", " ")
            topic_tag = f" `#{entry_topic}`" if entry_topic else ""
            label = heading or preview
            desc_parts.append(f"• **{label}**{topic_tag}")

        description = "\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed(f"Memories — {project_id}", description=description)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="memory-get", description="Retrieve information from memory (KV + semantic)")
    @app_commands.describe(
        query="What to retrieve",
        project="Project ID (defaults to channel's project)",
        topic="Topic filter for semantic search",
    )
    async def memory_get_command(
        interaction: discord.Interaction,
        query: str,
        project: str | None = None,
        topic: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return

        await interaction.response.defer()
        args: dict = {"query": query, "project_id": project_id}
        if topic:
            args["topic"] = topic
        result = await handler.execute("memory_get", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        # memory_get returns either KV result or semantic results
        kv_value = result.get("value")
        if kv_value is not None:
            await _send_success(
                interaction,
                f"Memory: {query}",
                description=f"```\n{kv_value}\n```",
                followup=True,
            )
            return

        results = result.get("results", [])
        if not results:
            await _send_info(
                interaction, "Not Found",
                description=f"No memories matched `{query}` in `{project_id}`.",
                followup=True,
            )
            return

        desc_parts = [f"**Query:** {query}\n"]
        for r in results:
            score = r.get("score", 0)
            content = r.get("content", "").replace("\n", " ")[:150]
            source = r.get("source", "")
            source_short = source.rsplit("/", 1)[-1] if "/" in source else source
            score_pct = f"{score * 100:.1f}%" if isinstance(score, (int, float)) else ""
            desc_parts.append(f"• `{source_short}` ({score_pct})\n> {content}")

        description = "\n\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed(f"Memory Results — {len(results)} found", description=description)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="memory-health", description="Memory health metrics for a project")
    @app_commands.describe(
        project="Project ID (defaults to channel's project)",
    )
    async def memory_health_command(
        interaction: discord.Interaction,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return

        await interaction.response.defer()
        result = await handler.execute("memory_health", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        fields = []
        for key in ("total_entries", "collection_size", "stale_count",
                     "growth_rate", "retrieval_hit_rate", "contradictions"):
            val = result.get(key)
            if val is not None:
                label = key.replace("_", " ").title()
                fields.append((label, str(val), True))

        await _send_info(
            interaction,
            f"Memory Health — {project_id}",
            fields=fields if fields else [("Status", "No data available", False)],
            followup=True,
        )

    @bot.tree.command(name="memory-fact-get", description="Get a temporal fact's current value")
    @app_commands.describe(
        key="Fact key (e.g. 'deploy_branch')",
        project="Project ID (defaults to channel's project)",
    )
    async def memory_fact_get_command(
        interaction: discord.Interaction,
        key: str,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return

        result = await handler.execute(
            "memory_fact_get", {"project_id": project_id, "key": key}
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return

        value = result.get("value")
        if value is None:
            await _send_info(
                interaction,
                f"Fact: {key}",
                description=f"No value set for `{key}` in `{project_id}`.",
            )
        else:
            await _send_success(
                interaction,
                f"Fact: {key}",
                description=f"```\n{value}\n```",
                fields=[("Project", f"`{project_id}`", True)],
            )

    @bot.tree.command(name="memory-fact-set", description="Set a temporal fact")
    @app_commands.describe(
        key="Fact key (e.g. 'deploy_branch')",
        value="New value for the fact",
        project="Project ID (defaults to channel's project)",
    )
    async def memory_fact_set_command(
        interaction: discord.Interaction,
        key: str,
        value: str,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return

        result = await handler.execute(
            "memory_fact_set",
            {"project_id": project_id, "key": key, "value": value},
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return

        await _send_success(
            interaction,
            "Fact Updated",
            description=f"`{key}` = `{value}` in `{project_id}`",
            result=result,
        )

    @bot.tree.command(name="memory-fact-list", description="List temporal facts for a project")
    @app_commands.describe(
        project="Project ID (defaults to channel's project)",
    )
    async def memory_fact_list_command(
        interaction: discord.Interaction,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return

        await interaction.response.defer()
        result = await handler.execute(
            "memory_fact_list", {"project_id": project_id}
        )
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        facts = result.get("facts", [])
        if not facts:
            await _send_info(
                interaction, "No Facts",
                description=f"No temporal facts in `{project_id}`.",
                followup=True,
            )
            return

        desc_parts = []
        for f in facts:
            k = f.get("key", "?")
            v = f.get("value", "?")
            desc_parts.append(f"`{k}` = {v}")

        description = "\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed(f"Facts — {project_id} ({len(facts)})", description=description)
        await interaction.followup.send(embed=embed)

    # ===================================================================
    # ADDITIONAL MEMORY COMMANDS
    # ===================================================================

    @bot.tree.command(name="memory-recall", description="Smart retrieval — KV exact match then semantic")
    @app_commands.describe(
        query="Search query (used as KV key and semantic query)",
        project="Project ID (defaults to channel's project)",
        topic="Topic filter for semantic fallback",
    )
    async def memory_recall_command(
        interaction: discord.Interaction,
        query: str,
        project: str | None = None,
        topic: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        args: dict = {"query": query, "project_id": project_id}
        if topic:
            args["topic"] = topic
        result = await handler.execute("memory_recall", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        # Format similarly to memory-get
        kv_value = result.get("value")
        if kv_value is not None:
            await _send_success(
                interaction, f"Recall: {query}",
                description=f"**KV match:**\n```\n{kv_value}\n```",
                followup=True,
            )
            return
        results = result.get("results", [])
        if not results:
            await _send_info(interaction, "Not Found",
                             description=f"No results for `{query}`.", followup=True)
            return
        desc_parts = []
        for r in results:
            content = r.get("content", "").replace("\n", " ")[:150]
            score = r.get("score", 0)
            desc_parts.append(f"• ({score * 100:.0f}%) {content}")
        description = "\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed(f"Recall — {len(results)} results", description=description)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="memory-kv-get", description="Exact key-value lookup")
    @app_commands.describe(
        namespace="KV namespace (e.g. 'project', 'conventions')",
        key="The key to look up",
        project="Project ID (defaults to channel's project)",
    )
    async def memory_kv_get_command(
        interaction: discord.Interaction,
        namespace: str,
        key: str,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute(
            "memory_kv_get", {"project_id": project_id, "namespace": namespace, "key": key}
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        value = result.get("value")
        if value is None:
            await _send_info(interaction, "Not Found",
                             description=f"`{namespace}/{key}` not set in `{project_id}`.")
        else:
            await _send_success(interaction, f"{namespace}/{key}",
                                description=f"```\n{value}\n```")

    @bot.tree.command(name="memory-kv-set", description="Store a key-value pair")
    @app_commands.describe(
        namespace="KV namespace (e.g. 'project', 'conventions')",
        key="The key to set",
        value="The value to store",
        project="Project ID (defaults to channel's project)",
    )
    async def memory_kv_set_command(
        interaction: discord.Interaction,
        namespace: str,
        key: str,
        value: str,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute(
            "memory_kv_set",
            {"project_id": project_id, "namespace": namespace, "key": key, "value": value},
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(interaction, "KV Stored",
                            description=f"`{namespace}/{key}` = `{value}`", result=result)

    @bot.tree.command(name="memory-kv-list", description="List key-value entries in a namespace")
    @app_commands.describe(
        namespace="KV namespace to list",
        project="Project ID (defaults to channel's project)",
    )
    async def memory_kv_list_command(
        interaction: discord.Interaction,
        namespace: str,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        result = await handler.execute(
            "memory_kv_list", {"project_id": project_id, "namespace": namespace}
        )
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        entries = result.get("entries", [])
        if not entries:
            await _send_info(interaction, "Empty Namespace",
                             description=f"No entries in `{namespace}`.", followup=True)
            return
        desc_parts = [f"**Namespace:** `{namespace}` in `{project_id}`\n"]
        for e in entries:
            k = e.get("key", "?")
            v = str(e.get("value", ""))[:80]
            desc_parts.append(f"`{k}` = {v}")
        description = "\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed(f"KV Entries — {len(entries)}", description=description)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="memory-fact-history", description="Full history of a temporal fact")
    @app_commands.describe(
        key="Temporal fact key",
        project="Project ID (defaults to channel's project)",
    )
    async def memory_fact_history_command(
        interaction: discord.Interaction,
        key: str,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        result = await handler.execute(
            "memory_fact_history", {"project_id": project_id, "key": key}
        )
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        history = result.get("history", [])
        if not history:
            await _send_info(interaction, f"No History: {key}",
                             description="No history found.", followup=True)
            return
        desc_parts = [f"**Fact:** `{key}` in `{project_id}`\n"]
        for h in history:
            val = h.get("value", "?")
            valid_from = h.get("valid_from", "?")
            valid_to = h.get("valid_to", "current")
            desc_parts.append(f"• `{val}` — {valid_from} → {valid_to}")
        description = "\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed(f"Fact History — {key}", description=description)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="memory-tag-search", description="Cross-scope tag search")
    @app_commands.describe(
        tag="Tag to search for across all scopes",
        limit="Max results (default 10)",
    )
    async def memory_tag_search_command(
        interaction: discord.Interaction,
        tag: str,
        limit: int = 10,
    ):
        await interaction.response.defer()
        result = await handler.execute("memory_search_by_tag", {"tag": tag, "limit": limit})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        results = result.get("results", [])
        if not results:
            await _send_info(interaction, f"No Results for #{tag}",
                             description="No entries found with that tag.", followup=True)
            return
        desc_parts = []
        for r in results:
            scope = r.get("scope", "?")
            content = r.get("content", "").replace("\n", " ")[:120]
            desc_parts.append(f"• `{scope}` — {content}")
        description = "\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed(f"Tag: #{tag} — {len(results)} results", description=description)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="memory-stale", description="Find stale memory documents")
    @app_commands.describe(
        project="Project ID (defaults to channel's project)",
        stale_days="Days without retrieval to consider stale (default 30)",
        limit="Max results (default 50)",
    )
    async def memory_stale_command(
        interaction: discord.Interaction,
        project: str | None = None,
        stale_days: int = 30,
        limit: int = 50,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        result = await handler.execute(
            "memory_stale",
            {"project_id": project_id, "stale_days": stale_days, "limit": limit},
        )
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        entries = result.get("entries", [])
        if not entries:
            await _send_success(interaction, "No Stale Memories",
                                description=f"All memories in `{project_id}` are fresh!",
                                followup=True)
            return
        desc_parts = [f"**{len(entries)}** stale entries (>{stale_days} days)\n"]
        for e in entries:
            heading = e.get("heading", e.get("chunk_hash", "?")[:12])
            days = e.get("days_stale", "?")
            desc_parts.append(f"• **{heading}** — {days} days stale")
        description = "\n".join(desc_parts[:30])
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = warning_embed(f"Stale Memories — {project_id}", description=description)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="memory-reindex", description="Reindex vault files into memory")
    @app_commands.describe(
        project="Project ID (defaults to channel's project)",
        full="Drop and rebuild from scratch (default: incremental)",
    )
    async def memory_reindex_command(
        interaction: discord.Interaction,
        project: str | None = None,
        full: bool = False,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        args: dict = {"project_id": project_id}
        if full:
            args["full"] = True
        result = await handler.execute("memory_reindex", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        indexed = result.get("indexed", 0)
        await _send_success(
            interaction, "Reindex Complete",
            description=f"Indexed **{indexed}** entries for `{project_id}`.",
            followup=True, result=result,
        )

    @bot.tree.command(name="memory-compact", description="Compact and summarize older memories")
    @app_commands.describe(
        project="Project ID (defaults to channel's project)",
    )
    async def memory_compact_command(
        interaction: discord.Interaction,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        result = await handler.execute("compact_memory", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        await _send_success(
            interaction, "Memory Compacted",
            description=f"Compaction complete for `{project_id}`.",
            followup=True, result=result,
        )

    @bot.tree.command(name="memory-consolidate", description="Run knowledge consolidation")
    @app_commands.describe(
        project="Project ID (defaults to channel's project)",
        mode="Consolidation mode",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="daily", value="daily"),
            app_commands.Choice(name="deep", value="deep"),
            app_commands.Choice(name="bootstrap", value="bootstrap"),
        ]
    )
    async def memory_consolidate_command(
        interaction: discord.Interaction,
        project: str | None = None,
        mode: app_commands.Choice[str] | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        args: dict = {"project_id": project_id}
        if mode:
            args["mode"] = mode.value
        result = await handler.execute("consolidate", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        await _send_success(
            interaction, "Consolidation Complete",
            description=f"Knowledge consolidation ({mode.value if mode else 'daily'}) "
                        f"complete for `{project_id}`.",
            followup=True, result=result,
        )

    @bot.tree.command(name="project-profile", description="View project memory profile")
    @app_commands.describe(
        project="Project ID (defaults to channel's project)",
    )
    async def view_profile_command(
        interaction: discord.Interaction,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        result = await handler.execute("view_profile", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        content = result.get("content", result.get("profile", str(result)))
        await _send_long_interaction(
            content, interaction.followup.send,
            filename=f"{project_id}-profile.md",
        )

    @bot.tree.command(name="project-factsheet", description="View or update project factsheet")
    @app_commands.describe(
        project="Project ID (defaults to channel's project)",
        action="View or update",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="view", value="view"),
            app_commands.Choice(name="update", value="update"),
        ]
    )
    async def project_factsheet_command(
        interaction: discord.Interaction,
        project: str | None = None,
        action: app_commands.Choice[str] | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        args: dict = {"project_id": project_id}
        if action:
            args["action"] = action.value
        result = await handler.execute("project_factsheet", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        content = result.get("content", result.get("factsheet", str(result)))
        await _send_long_interaction(
            content, interaction.followup.send,
            filename=f"{project_id}-factsheet.yaml",
        )

    @bot.tree.command(name="project-knowledge", description="Read organized topic knowledge")
    @app_commands.describe(
        project="Project ID (defaults to channel's project)",
        action="Read a topic or list available topics",
        topic="Topic to read (e.g. 'architecture', 'testing')",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="read", value="read"),
            app_commands.Choice(name="list", value="list"),
        ]
    )
    async def project_knowledge_command(
        interaction: discord.Interaction,
        project: str | None = None,
        action: app_commands.Choice[str] | None = None,
        topic: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        args: dict = {"project_id": project_id}
        if action:
            args["action"] = action.value
        if topic:
            args["topic"] = topic
        result = await handler.execute("project_knowledge", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        content = result.get("content", result.get("knowledge", str(result)))
        if isinstance(content, list):
            content = "\n".join(f"• {t}" for t in content)
        await _send_long_interaction(
            content, interaction.followup.send,
            filename=f"{project_id}-knowledge.md",
        )

    # ===================================================================
    # ADDITIONAL TASK COMMANDS
    # ===================================================================

    @bot.tree.command(name="task-tree", description="Subtask hierarchy for a parent task")
    @app_commands.describe(task_id="Root/parent task ID")
    async def task_tree_command(
        interaction: discord.Interaction,
        task_id: str,
    ):
        await interaction.response.defer()
        result = await handler.execute("get_task_tree", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        tree_text = result.get("tree", result.get("output", str(result)))
        await _send_long_interaction(
            f"```\n{tree_text}\n```", interaction.followup.send,
            filename=f"{task_id}-tree.txt",
        )

    @bot.tree.command(name="add-dep", description="Add a dependency between tasks")
    @app_commands.describe(
        task_id="Task that should wait (downstream)",
        depends_on="Task that must complete first (upstream)",
    )
    async def add_dep_command(
        interaction: discord.Interaction,
        task_id: str,
        depends_on: str,
    ):
        result = await handler.execute(
            "add_dependency", {"task_id": task_id, "depends_on": depends_on}
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction, "Dependency Added",
            description=f"`{task_id}` now depends on `{depends_on}`.",
            result=result,
        )

    @bot.tree.command(name="remove-dep", description="Remove a dependency between tasks")
    @app_commands.describe(
        task_id="Downstream task to unlink",
        depends_on="Upstream task to remove",
    )
    async def remove_dep_command(
        interaction: discord.Interaction,
        task_id: str,
        depends_on: str,
    ):
        result = await handler.execute(
            "remove_dependency", {"task_id": task_id, "depends_on": depends_on}
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction, "Dependency Removed",
            description=f"`{task_id}` no longer depends on `{depends_on}`.",
            result=result,
        )

    @bot.tree.command(name="reopen-task", description="Reopen a task with feedback")
    @app_commands.describe(
        task_id="Task ID to reopen",
        feedback="What needs to be fixed or changed",
    )
    async def reopen_task_command(
        interaction: discord.Interaction,
        task_id: str,
        feedback: str,
    ):
        result = await handler.execute(
            "reopen_with_feedback", {"task_id": task_id, "feedback": feedback}
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction, "Task Reopened",
            description=f"`{task_id}` reopened with feedback.",
            result=result,
        )

    @bot.tree.command(name="approve-plan", description="Approve a task plan and create subtasks")
    @app_commands.describe(task_id="Task ID whose plan to approve")
    async def approve_plan_command(
        interaction: discord.Interaction,
        task_id: str,
    ):
        result = await handler.execute("approve_plan", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        subtasks = result.get("subtasks_created", result.get("subtasks", 0))
        await _send_success(
            interaction, "Plan Approved",
            description=f"Plan for `{task_id}` approved. {subtasks} subtask(s) created.",
            result=result,
        )

    @bot.tree.command(name="reject-plan", description="Reject a task plan with feedback")
    @app_commands.describe(
        task_id="Task ID whose plan to reject",
        feedback="What changes are needed",
    )
    async def reject_plan_command(
        interaction: discord.Interaction,
        task_id: str,
        feedback: str,
    ):
        result = await handler.execute(
            "reject_plan", {"task_id": task_id, "feedback": feedback}
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction, "Plan Rejected",
            description=f"Plan for `{task_id}` rejected. Task reopened with feedback.",
            result=result,
        )

    @bot.tree.command(name="set-status", description="Set a task's status directly (admin)")
    @app_commands.describe(
        task_id="Task ID",
        status="New status",
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name="DEFINED", value="DEFINED"),
            app_commands.Choice(name="READY", value="READY"),
            app_commands.Choice(name="COMPLETED", value="COMPLETED"),
            app_commands.Choice(name="FAILED", value="FAILED"),
            app_commands.Choice(name="BLOCKED", value="BLOCKED"),
        ]
    )
    async def set_status_command(
        interaction: discord.Interaction,
        task_id: str,
        status: app_commands.Choice[str],
    ):
        result = await handler.execute(
            "set_task_status", {"task_id": task_id, "status": status.value}
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction, "Status Updated",
            description=f"`{task_id}` → **{status.value}**",
            result=result,
        )

    @bot.tree.command(name="all-tasks", description="Active tasks across all projects")
    async def all_tasks_command(interaction: discord.Interaction):
        await interaction.response.defer()
        result = await handler.execute("list_active_tasks_all_projects", {})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        projects = result.get("projects", {})
        if not projects:
            await _send_info(interaction, "No Active Tasks",
                             description="No active tasks across any project.", followup=True)
            return
        desc_parts = []
        for proj_id, tasks in projects.items():
            task_lines = []
            for t in tasks[:5]:
                tid = t.get("id", "?") if isinstance(t, dict) else str(t)
                tstatus = t.get("status", "") if isinstance(t, dict) else ""
                ttitle = t.get("title", "") if isinstance(t, dict) else ""
                emoji = STATUS_EMOJIS.get(tstatus, "⚪")
                task_lines.append(f"  {emoji} `{tid}` {ttitle[:40]}")
            extra = f" (+{len(tasks) - 5} more)" if len(tasks) > 5 else ""
            desc_parts.append(f"**{proj_id}** — {len(tasks)} task(s){extra}\n"
                              + "\n".join(task_lines))
        description = "\n\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed("Active Tasks — All Projects", description=description)
        await interaction.followup.send(embed=embed)

    # ===================================================================
    # AGENT PROFILE COMMANDS
    # ===================================================================

    @bot.tree.command(name="profiles", description="List all agent profiles")
    async def list_profiles_command(interaction: discord.Interaction):
        await interaction.response.defer()
        result = await handler.execute("list_profiles", {})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        profiles = result.get("profiles", [])
        if not profiles:
            await _send_info(interaction, "No Profiles",
                             description="No agent profiles configured.", followup=True)
            return
        desc_parts = []
        for p in profiles:
            pid = p.get("id", "?") if isinstance(p, dict) else str(p)
            pname = p.get("name", pid) if isinstance(p, dict) else pid
            model = p.get("model", "") if isinstance(p, dict) else ""
            model_str = f" · `{model}`" if model else ""
            desc_parts.append(f"• **{pname}** (`{pid}`){model_str}")
        description = "\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed(f"Agent Profiles — {len(profiles)}", description=description)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="profile", description="View agent profile details")
    @app_commands.describe(profile_id="Profile ID to view")
    async def get_profile_command(
        interaction: discord.Interaction,
        profile_id: str,
    ):
        await interaction.response.defer()
        result = await handler.execute("get_profile", {"profile_id": profile_id})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        name = result.get("name", profile_id)
        model = result.get("model", "default")
        tools = result.get("allowed_tools", [])
        desc = result.get("description", "")
        fields = [
            ("ID", f"`{profile_id}`", True),
            ("Model", f"`{model}`", True),
        ]
        if tools:
            tools_str = ", ".join(f"`{t}`" for t in tools[:10])
            if len(tools) > 10:
                tools_str += f" +{len(tools) - 10} more"
            fields.append(("Tools", tools_str, False))
        await _send_info(
            interaction, f"Profile: {name}",
            description=desc or None,
            fields=fields, followup=True,
        )

    # ===================================================================
    # PROJECT CONSTRAINT COMMANDS
    # ===================================================================

    @bot.tree.command(name="set-constraint", description="Set scheduling constraint on a project")
    @app_commands.describe(
        project="Project ID (defaults to channel's project)",
        exclusive="Only one agent at a time",
        pause_scheduling="Pause all scheduling for this project",
    )
    async def set_constraint_command(
        interaction: discord.Interaction,
        project: str | None = None,
        exclusive: bool = False,
        pause_scheduling: bool = False,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        args: dict = {"project_id": project_id}
        if exclusive:
            args["exclusive"] = True
        if pause_scheduling:
            args["pause_scheduling"] = True
        result = await handler.execute("set_project_constraint", args)
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        constraints = []
        if exclusive:
            constraints.append("exclusive")
        if pause_scheduling:
            constraints.append("paused")
        await _send_success(
            interaction, "Constraint Set",
            description=f"`{project_id}`: {', '.join(constraints) or 'updated'}",
            result=result,
        )

    @bot.tree.command(name="release-constraint", description="Release scheduling constraint")
    @app_commands.describe(
        project="Project ID (defaults to channel's project)",
    )
    async def release_constraint_command(
        interaction: discord.Interaction,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute(
            "release_project_constraint", {"project_id": project_id}
        )
        if "error" in result:
            await _send_error(interaction, result["error"])
            return
        await _send_success(
            interaction, "Constraint Released",
            description=f"Constraints on `{project_id}` have been released.",
            result=result,
        )

    @bot.tree.command(name="token-audit", description="Token usage audit")
    @app_commands.describe(
        days="Number of days to audit (default 7)",
        project="Filter to a specific project",
    )
    async def token_audit_command(
        interaction: discord.Interaction,
        days: int = 7,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        await interaction.response.defer()
        args: dict = {"days": days}
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("token_audit", args)
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        # Format audit data
        total = result.get("total_tokens", 0)
        by_project = result.get("by_project", {})
        desc_parts = [f"**Period:** {days} days", f"**Total tokens:** {total:,}\n"]
        if by_project:
            for pid, count in sorted(by_project.items(), key=lambda x: -x[1]):
                pct = (count / total * 100) if total else 0
                desc_parts.append(f"• `{pid}`: {count:,} ({pct:.1f}%)")
        description = "\n".join(desc_parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = info_embed("Token Audit", description=description)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="recover-workflow", description="Recover an orphaned workflow")
    @app_commands.describe(workflow_id="The workflow ID to recover")
    async def recover_workflow_command(
        interaction: discord.Interaction,
        workflow_id: str,
    ):
        await interaction.response.defer()
        result = await handler.execute("recover_workflow", {"workflow_id": workflow_id})
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        await _send_success(
            interaction, "Workflow Recovered",
            description=f"Workflow `{workflow_id}` has been recovered.",
            followup=True, result=result,
        )

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

    @bot.tree.command(name="shutdown", description="Shut down the agent-queue daemon")
    async def shutdown_command(interaction: discord.Interaction):
        await _send_warning(
            interaction, "Shutting Down",
            description="Agent-queue daemon is shutting down...",
        )
        await handler.execute("shutdown", {})

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

    @bot.tree.command(name="edit-file", description="Edit a file in a project workspace")
    @app_commands.describe(
        path="File path relative to the workspace root",
        content="New file content",
        project="Project ID (defaults to channel's project)",
    )
    async def edit_file_command(
        interaction: discord.Interaction,
        path: str,
        content: str,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        result = await handler.execute(
            "edit_file", {"project_id": project_id, "path": path, "content": content}
        )
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return
        await _send_success(
            interaction, "File Updated",
            description=f"Updated `{path}` in `{project_id}`.",
            followup=True, result=result,
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
        lines.append("\n_Use the buttons below to view details and take actions._")

        view = MenuView(handler=handler, bot=bot)
        await interaction.response.send_message("\n".join(lines), view=view)
