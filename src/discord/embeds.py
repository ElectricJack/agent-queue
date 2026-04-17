"""Centralized Discord embed factory for AgentQueue.

This module is the single source of truth for all embed creation, semantic
colors, task-status visual mappings, and text-safety utilities.  Every embed
sent by the bot -- whether from a slash command, a lifecycle notification, or
the chat agent -- should be built through the helpers defined here.

Design principles
-----------------
* **Auto-truncation** -- every text property is silently capped to its Discord
  API limit so callers never have to worry about ``HTTPException: 400``.
* **Consistent branding** -- all embeds carry an "AgentQueue" footer and a UTC
  timestamp unless explicitly suppressed.
* **Pure functions** -- nothing in this module touches the Discord gateway or
  requires a bot instance, making everything trivially unit-testable.
* **Semantic styling** -- callers pick an ``EmbedStyle`` (SUCCESS, ERROR, ...)
  and the factory resolves it to a color + icon.  Status-specific embeds use
  the task ``STATUS_COLORS`` / ``STATUS_EMOJIS`` dicts instead.

Discord API embed limits (enforced by ``check_embed_size``):

    ============  ================
    Property      Character limit
    ============  ================
    Title         256
    Description   4 096
    Field name    256
    Field value   1 024
    Footer text   2 048
    Author name   256
    Fields/embed  25
    **Total**     **6 000**
    ============  ================
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Sequence

import discord

from src.models import TaskStatus

# ---------------------------------------------------------------------------
# Discord API hard limits
# ---------------------------------------------------------------------------

LIMIT_TITLE = 256
LIMIT_DESCRIPTION = 4096
LIMIT_FIELD_NAME = 256
LIMIT_FIELD_VALUE = 1024
LIMIT_FOOTER_TEXT = 2048
LIMIT_AUTHOR_NAME = 256
LIMIT_FIELDS_PER_EMBED = 25
LIMIT_TOTAL_CHARS = 6000

# ---------------------------------------------------------------------------
# Semantic embed styles
# ---------------------------------------------------------------------------


class EmbedStyle(Enum):
    """High-level visual styles with associated color and icon."""

    SUCCESS = (0x2ECC71, "\u2705")  # Green  / white check mark
    ERROR = (0xE74C3C, "\u274c")  # Red    / cross mark
    WARNING = (0xF39C12, "\u26a0\ufe0f")  # Amber  / warning sign
    INFO = (0x3498DB, "\u2139\ufe0f")  # Blue   / information
    CRITICAL = (0x992D22, "\U0001f6a8")  # Dark red / rotating light

    def __init__(self, color: int, icon: str) -> None:
        self.color = color
        self.icon = icon


# ---------------------------------------------------------------------------
# Task-status visual mappings (previously scoped inside setup_commands())
# ---------------------------------------------------------------------------

STATUS_COLORS: dict[str, int] = {
    TaskStatus.DEFINED.value: 0x95A5A6,  # Gray
    TaskStatus.READY.value: 0x3498DB,  # Blue
    TaskStatus.ASSIGNED.value: 0x9B59B6,  # Purple
    TaskStatus.IN_PROGRESS.value: 0xF39C12,  # Amber
    TaskStatus.WAITING_INPUT.value: 0x1ABC9C,  # Teal
    TaskStatus.PAUSED.value: 0x7F8C8D,  # Dark gray
    TaskStatus.AWAITING_APPROVAL.value: 0xE67E22,  # Orange
    TaskStatus.AWAITING_PLAN_APPROVAL.value: 0xF39C12,  # Amber
    TaskStatus.COMPLETED.value: 0x2ECC71,  # Green
    TaskStatus.FAILED.value: 0xE74C3C,  # Red
    TaskStatus.BLOCKED.value: 0x992D22,  # Dark red
}

STATUS_EMOJIS: dict[str, str] = {
    TaskStatus.DEFINED.value: "\u26aa",  # white circle
    TaskStatus.READY.value: "\U0001f535",  # blue circle
    TaskStatus.ASSIGNED.value: "\U0001f4cb",  # clipboard
    TaskStatus.IN_PROGRESS.value: "\U0001f7e1",  # yellow circle
    TaskStatus.WAITING_INPUT.value: "\U0001f4ac",  # speech balloon
    TaskStatus.PAUSED.value: "\u23f8\ufe0f",  # pause button
    TaskStatus.AWAITING_APPROVAL.value: "\u231b",  # hourglass
    TaskStatus.AWAITING_PLAN_APPROVAL.value: "\U0001f4cb",  # clipboard
    TaskStatus.COMPLETED.value: "\U0001f7e2",  # green circle
    TaskStatus.FAILED.value: "\U0001f534",  # red circle
    TaskStatus.BLOCKED.value: "\u26d4",  # no entry
}

_DEFAULT_FOOTER = "AgentQueue"
_ELLIPSIS = "\u2026"

# ---------------------------------------------------------------------------
# Type tags for task display
# ---------------------------------------------------------------------------

# Maps task properties to display tags shown before task titles in list views.
# These help users quickly identify what kind of work item they're looking at.
TYPE_TAGS: dict[str, str] = {
    "plan_subtask": "📋",  # Auto-generated from a plan
    "has_subtasks": "📦",  # Parent task with children
    "has_pr": "🔗",  # Has an associated pull request
    "approval_required": "🔒",  # Requires human approval
}

# Maps TaskType enum values to display emojis for visual task categorization.
TASK_TYPE_EMOJIS: dict[str, str] = {
    "feature": "✨",
    "bugfix": "🐛",
    "refactor": "♻️",
    "test": "🧪",
    "docs": "📝",
    "chore": "🔧",
    "research": "🔍",
    "plan": "📋",
}


# ---------------------------------------------------------------------------
# Progress bar rendering
# ---------------------------------------------------------------------------


def progress_bar(
    completed: int,
    total: int,
    *,
    width: int = 10,
    filled: str = "█",
    empty: str = "░",
) -> str:
    """Render a text-based progress bar for Discord display.

    Parameters
    ----------
    completed:
        Number of completed items.
    total:
        Total number of items.  If zero, returns a bar showing 0%.
    width:
        Number of characters in the bar (default 10).
    filled:
        Character for completed portions (default ``█``).
    empty:
        Character for remaining portions (default ``░``).

    Returns
    -------
    str
        e.g. ``████░░░░░░ 40% (4/10)``

    >>> progress_bar(4, 10)
    '████░░░░░░ 40% (4/10)'
    >>> progress_bar(0, 0)
    '░░░░░░░░░░ 0% (0/0)'
    """
    if total <= 0:
        pct = 0.0
    else:
        pct = completed / total * 100
    fill_count = round(pct / 100 * width)
    bar = filled * fill_count + empty * (width - fill_count)
    return f"{bar} {pct:.0f}% ({completed}/{total})"


# ---------------------------------------------------------------------------
# Tree view helpers
# ---------------------------------------------------------------------------

# Unicode box-drawing characters for tree rendering
TREE_BRANCH = "├── "  # Non-last child
TREE_LAST = "└── "  # Last child
TREE_PIPE = "│   "  # Continuation pipe
TREE_SPACE = "    "  # No continuation


def format_tree_task(
    title: str,
    task_id: str,
    *,
    is_last: bool = True,
    depth: int = 0,
    prefix: str = "",
    type_tag: str = "",
) -> str:
    """Format a single task line in tree-view style.

    Parameters
    ----------
    title:
        The task title.
    task_id:
        The task identifier to show inline.
    is_last:
        Whether this is the last sibling at this level.
    depth:
        Nesting depth (0 = root task).
    prefix:
        The continuation prefix built by the parent formatter.
    type_tag:
        Optional type emoji tag to prepend (e.g. ``"📋"``).

    Returns
    -------
    str
        A formatted line like ``├── 📋 **Task Title** `task-id```
    """
    if depth == 0:
        tag = f"{type_tag} " if type_tag else ""
        return f"{tag}**{title}** `{task_id}`"
    connector = TREE_LAST if is_last else TREE_BRANCH
    tag = f"{type_tag} " if type_tag else ""
    return f"{prefix}{connector}{tag}**{title}** `{task_id}`"


# ---------------------------------------------------------------------------
# Text-safety utilities
# ---------------------------------------------------------------------------


def truncate(text: str | None, max_len: int, *, suffix: str = _ELLIPSIS) -> str:
    """Safely truncate *text* to at most *max_len* characters.

    If *text* is ``None`` or empty the empty string is returned.  When
    truncation is necessary, *suffix* (default ``"\\u2026"``) is appended
    so the total length equals *max_len*.

    >>> truncate("hello world", 5)
    'hell\\u2026'
    >>> truncate(None, 100)
    ''
    """
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= len(suffix):
        return suffix[:max_len]
    return text[: max_len - len(suffix)] + suffix


def unix_timestamp(dt: datetime | None = None, style: str = "R") -> str:
    """Format *dt* as a Discord dynamic timestamp string.

    Parameters
    ----------
    dt:
        The datetime to format.  Defaults to ``datetime.now(tz=timezone.utc)``.
    style:
        One of the Discord timestamp styles:
        ``t`` (short time), ``T`` (long time), ``d`` (short date),
        ``D`` (long date), ``f`` (short date/time), ``F`` (long date/time),
        ``R`` (relative -- the default).

    Returns
    -------
    str
        e.g. ``<t:1700000000:R>``
    """
    if dt is None:
        dt = datetime.now(tz=timezone.utc)
    return f"<t:{math.floor(dt.timestamp())}:{style}>"


# ---------------------------------------------------------------------------
# Embed size guard
# ---------------------------------------------------------------------------


def _embed_char_count(embed: discord.Embed) -> int:
    """Return the total character count Discord uses to enforce the 6 000 limit."""
    total = 0
    if embed.title:
        total += len(embed.title)
    if embed.description:
        total += len(embed.description)
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author and embed.author.name:
        total += len(embed.author.name)
    for field in embed.fields:
        total += len(field.name or "")
        total += len(field.value or "")
    return total


def check_embed_size(embed: discord.Embed) -> tuple[bool, int]:
    """Validate that *embed* respects the 6 000 total-character limit.

    Returns
    -------
    tuple[bool, int]
        ``(is_valid, total_chars)`` where *is_valid* is ``True`` when
        the embed is within limits.
    """
    total = _embed_char_count(embed)
    return (total <= LIMIT_TOTAL_CHARS, total)


# ---------------------------------------------------------------------------
# Core factory
# ---------------------------------------------------------------------------


def make_embed(
    style: EmbedStyle,
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    footer: str | None = _DEFAULT_FOOTER,
    timestamp: datetime | None | bool = True,
    color_override: int | None = None,
    url: str | None = None,
) -> discord.Embed:
    """Build a ``discord.Embed`` with automatic truncation and branding.

    Parameters
    ----------
    style:
        Semantic style that determines the embed color and title icon.
    title:
        Embed title (auto-truncated to 256 chars).  The style's icon is
        prepended automatically.
    description:
        Optional body text (auto-truncated to 4 096 chars).
    fields:
        Optional sequence of ``(name, value, inline)`` tuples.  Each name
        and value is auto-truncated to its Discord limit.  At most 25
        fields are kept.
    footer:
        Footer text.  Defaults to ``"AgentQueue"``.  Pass ``None`` to omit.
    timestamp:
        * ``True`` (default) -- use ``datetime.now(tz=timezone.utc)``
        * A ``datetime`` instance -- use that value
        * ``None`` or ``False`` -- omit the timestamp
    color_override:
        If provided, overrides the color from *style*.
    url:
        Optional URL attached to the embed title.

    Returns
    -------
    discord.Embed
    """
    icon = style.icon
    color = color_override if color_override is not None else style.color

    full_title = f"{icon} {truncate(title, LIMIT_TITLE - len(icon) - 1)}"

    embed = discord.Embed(
        title=full_title,
        description=truncate(description, LIMIT_DESCRIPTION) if description else None,
        color=color,
        url=url,
    )

    # Timestamp
    if timestamp is True:
        embed.timestamp = datetime.now(tz=timezone.utc)
    elif isinstance(timestamp, datetime):
        embed.timestamp = timestamp
    # else: omit

    # Footer
    if footer is not None:
        embed.set_footer(text=truncate(footer, LIMIT_FOOTER_TEXT))

    # Fields
    if fields:
        for name, value, inline in fields[:LIMIT_FIELDS_PER_EMBED]:
            embed.add_field(
                name=truncate(name, LIMIT_FIELD_NAME),
                value=truncate(value, LIMIT_FIELD_VALUE),
                inline=inline,
            )

    return embed


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


def success_embed(
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    **kwargs,
) -> discord.Embed:
    """Green success embed."""
    return make_embed(EmbedStyle.SUCCESS, title, description=description, fields=fields, **kwargs)


def error_embed(
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    **kwargs,
) -> discord.Embed:
    """Red error embed."""
    return make_embed(EmbedStyle.ERROR, title, description=description, fields=fields, **kwargs)


def warning_embed(
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    **kwargs,
) -> discord.Embed:
    """Amber warning embed."""
    return make_embed(EmbedStyle.WARNING, title, description=description, fields=fields, **kwargs)


def info_embed(
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    **kwargs,
) -> discord.Embed:
    """Blue info embed."""
    return make_embed(EmbedStyle.INFO, title, description=description, fields=fields, **kwargs)


def critical_embed(
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    **kwargs,
) -> discord.Embed:
    """Dark-red critical embed."""
    return make_embed(EmbedStyle.CRITICAL, title, description=description, fields=fields, **kwargs)


def status_embed(
    status: str,
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    **kwargs,
) -> discord.Embed:
    """Build an embed whose color and title icon match a task *status*.

    *status* should be one of the ``TaskStatus`` ``.value`` strings (e.g.
    ``"IN_PROGRESS"``).  Unknown statuses fall back to gray / white-circle.

    Example::

        embed = status_embed("COMPLETED", "Task Finished", description="All done.")
    """
    color = STATUS_COLORS.get(status, 0x95A5A6)
    emoji = STATUS_EMOJIS.get(status, "\u26aa")

    full_title = f"{emoji} {truncate(title, LIMIT_TITLE - 3)}"

    embed = discord.Embed(
        title=full_title,
        description=truncate(description, LIMIT_DESCRIPTION) if description else None,
        color=color,
    )

    # Timestamp -- same logic as make_embed
    ts = kwargs.pop("timestamp", True)
    if ts is True:
        embed.timestamp = datetime.now(tz=timezone.utc)
    elif isinstance(ts, datetime):
        embed.timestamp = ts

    # Footer
    footer = kwargs.pop("footer", _DEFAULT_FOOTER)
    if footer is not None:
        embed.set_footer(text=truncate(footer, LIMIT_FOOTER_TEXT))

    # URL
    url = kwargs.pop("url", None)
    if url is not None:
        embed.url = url

    # Fields
    if fields:
        for name, value, inline in fields[:LIMIT_FIELDS_PER_EMBED]:
            embed.add_field(
                name=truncate(name, LIMIT_FIELD_NAME),
                value=truncate(value, LIMIT_FIELD_VALUE),
                inline=inline,
            )

    return embed


# ---------------------------------------------------------------------------
# Tree view embed builder
# ---------------------------------------------------------------------------

# Overhead for the code block fences (``` + newline on each end)
_CODE_BLOCK_OVERHEAD = len("```\n") + len("\n```")

# Reserve space for embed fields, footer, and title so the description
# doesn't consume the entire 6,000-char total budget.
_DESCRIPTION_BUDGET = LIMIT_DESCRIPTION - _CODE_BLOCK_OVERHEAD


def tree_view_embed(
    trees: list[dict],
    *,
    total_root_tasks: int = 0,
    total_tasks: int = 0,
    display_mode: str = "tree",
    show_completed: bool = False,
    project_name: str | None = None,
) -> list[discord.Embed]:
    """Build one or more embeds that render a task tree (or compact list).

    The tree body is placed inside a code block in the embed *description*
    for monospace alignment of box-drawing characters.  A summary line and
    optional metadata are rendered as embed *fields*.

    Parameters
    ----------
    trees:
        List of tree entry dicts as returned by the command handler's
        hierarchical list mode.  Each entry has at minimum::

            {
                "root": {<task dict>},
                "formatted": "<pre-rendered tree text>",
                "subtask_completed": int,
                "subtask_total": int,
                "progress_bar": str | None,   # compact mode only
            }

    total_root_tasks:
        Number of root-level tasks (used in the summary field).
    total_tasks:
        Total tasks including subtasks (used in the summary field).
    display_mode:
        ``"tree"`` for full hierarchical view or ``"compact"`` for root-only
        summaries.  Controls code-block wrapping and title.
    show_completed:
        Whether completed tasks are included.  Used to show a hint when the
        list is empty.
    project_name:
        Optional project name shown in the embed footer.

    Returns
    -------
    list[discord.Embed]
        One embed per page.  Callers should send the first as the
        interaction response and subsequent ones via ``followup.send()``.
        An empty *trees* list returns a single informational embed.
    """
    # ── Empty state ──────────────────────────────────────────────────
    if not trees:
        hint = ""
        if not show_completed:
            hint = " Use `/tasks show_completed:True` to include completed."
        return [
            info_embed(
                "No Tasks",
                description=f"No tasks found for this project.{hint}",
            )
        ]

    is_tree = display_mode == "tree"
    mode_label = "Tree View" if is_tree else "Compact View"

    # ── Collect formatted blocks ─────────────────────────────────────
    blocks: list[str] = []
    for entry in trees:
        formatted: str = entry.get("formatted", "")
        # In compact mode, append the progress bar inline if available
        bar = entry.get("progress_bar")
        if bar and not is_tree:
            formatted += f"\n  {bar}"
        blocks.append(formatted)

    # ── Build summary field value ────────────────────────────────────
    summary_parts: list[str] = []
    summary_parts.append(f"**{total_root_tasks}** root task(s)")
    summary_parts.append(f"**{total_tasks}** total")

    # Aggregate completion stats across all trees
    agg_completed = sum(e.get("subtask_completed", 0) for e in trees)
    agg_subtotal = sum(e.get("subtask_total", 0) for e in trees)
    if agg_subtotal > 0:
        pct = agg_completed / agg_subtotal * 100
        summary_parts.append(f"{agg_completed}/{agg_subtotal} subtasks complete ({pct:.0f}%)")

    # Aggregate subtask status breakdown across all trees for non-completed stats
    agg_by_status: dict[str, int] = {}
    for entry in trees:
        for st, cnt in entry.get("subtask_by_status", {}).items():
            agg_by_status[st] = agg_by_status.get(st, 0) + cnt
    # Build a concise status breakdown line for non-completed subtask statuses
    _SUBTASK_STAT_ORDER: list[tuple[str, str]] = [
        ("IN_PROGRESS", "in progress"),
        ("ASSIGNED", "assigned"),
        ("AWAITING_APPROVAL", "awaiting approval"),
        ("AWAITING_PLAN_APPROVAL", "awaiting plan approval"),
        ("WAITING_INPUT", "waiting input"),
        ("PAUSED", "paused"),
        ("FAILED", "failed"),
        ("BLOCKED", "blocked"),
        ("READY", "ready"),
        ("DEFINED", "defined"),
    ]
    subtask_stat_parts: list[str] = []
    for st_val, label in _SUBTASK_STAT_ORDER:
        cnt = agg_by_status.get(st_val, 0)
        if cnt > 0:
            emoji = STATUS_EMOJIS.get(st_val, "\u26aa")
            subtask_stat_parts.append(f"{emoji} {cnt} {label}")
    if subtask_stat_parts:
        summary_parts.append(" \u00b7 ".join(subtask_stat_parts))

    summary_value = " \u00b7 ".join(summary_parts)

    # ── Build metadata field (status breakdown) ──────────────────────
    status_counts: dict[str, int] = {}
    for entry in trees:
        root = entry.get("root", {})
        st = root.get("status", "DEFINED")
        status_counts[st] = status_counts.get(st, 0) + 1

    if status_counts:
        status_lines: list[str] = []
        for st, count in status_counts.items():
            emoji = STATUS_EMOJIS.get(st, "\u26aa")
            label = st.replace("_", " ").title()
            status_lines.append(f"{emoji} {label}: **{count}**")
        metadata_value = " \u00b7 ".join(status_lines)
    else:
        metadata_value = ""

    # ── Separator between blocks ─────────────────────────────────────
    separator = "\n\n" if is_tree else "\n"

    # ── Paginate into embeds respecting description limit ────────────
    # Each embed's description holds a code block (tree mode) or plain
    # text (compact mode).  When the body exceeds the budget we split
    # across multiple embeds.

    pages: list[str] = []
    current_page_blocks: list[str] = []
    current_len = 0

    for block in blocks:
        block_len = len(block) + len(separator)  # cost of adding this block
        if current_page_blocks and (current_len + block_len) > _DESCRIPTION_BUDGET:
            # Flush current page
            pages.append(separator.join(current_page_blocks))
            current_page_blocks = [block]
            current_len = len(block)
        else:
            current_page_blocks.append(block)
            current_len += block_len

    # Flush final page
    if current_page_blocks:
        pages.append(separator.join(current_page_blocks))

    # ── Build embed(s) ───────────────────────────────────────────────
    embeds: list[discord.Embed] = []
    total_pages = len(pages)

    for idx, page_body in enumerate(pages):
        is_first = idx == 0
        is_last_page = idx == total_pages - 1
        page_num = idx + 1

        # Wrap tree mode in a code block for monospace alignment
        if is_tree:
            description = f"```\n{truncate(page_body, _DESCRIPTION_BUDGET)}\n```"
        else:
            description = truncate(page_body, LIMIT_DESCRIPTION)

        # Title: include page number when paginated
        if total_pages > 1:
            title = f"{mode_label} (Page {page_num}/{total_pages})"
        else:
            title = mode_label

        embed = make_embed(
            EmbedStyle.INFO,
            title,
            description=description,
        )

        # Fields only on the first page to avoid clutter on continuations
        if is_first:
            embed.add_field(
                name="Summary",
                value=truncate(summary_value, LIMIT_FIELD_VALUE),
                inline=False,
            )
            if metadata_value:
                embed.add_field(
                    name="Status Breakdown",
                    value=truncate(metadata_value, LIMIT_FIELD_VALUE),
                    inline=False,
                )

        # Show hint on last page when completed tasks are hidden
        if is_last_page and not show_completed:
            embed.add_field(
                name="\U0001f4a1 Tip",
                value="Use `/tasks show_completed:True` to include completed tasks.",
                inline=False,
            )

        # Project name in footer (with page info if paginated)
        footer_parts: list[str] = [_DEFAULT_FOOTER]
        if project_name:
            footer_parts.append(project_name)
        if total_pages > 1:
            footer_parts.append(f"Page {page_num}/{total_pages}")
        embed.set_footer(text=" \u00b7 ".join(footer_parts))

        embeds.append(embed)

    return embeds
