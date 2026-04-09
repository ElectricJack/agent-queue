"""Telegram-specific message formatting using MarkdownV2.

Mirrors ``src/discord/notifications.py`` but produces Telegram-compatible
output.  Telegram uses MarkdownV2 which requires escaping special characters.

Functions here return plain strings — the bot layer calls
``telegram.Bot.send_message(parse_mode="MarkdownV2")`` with the result.

Telegram message limit is 4096 characters per message (vs Discord's 2000).
"""

from __future__ import annotations

import re
from typing import Any

# Characters that must be escaped in MarkdownV2 (outside code blocks)
_ESCAPE_CHARS = r"_*[]()~`>#+-=|{}.!"


def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2.

    Characters inside inline code or code blocks should NOT be escaped —
    callers are responsible for wrapping code sections before calling this
    on the surrounding text.
    """
    return re.sub(r"([" + re.escape(_ESCAPE_CHARS) + r"])", r"\\\1", text)


def bold(text: str) -> str:
    """Wrap text in MarkdownV2 bold markers, escaping inner content."""
    return f"*{escape_markdown(text)}*"


def italic(text: str) -> str:
    """Wrap text in MarkdownV2 italic markers, escaping inner content."""
    return f"_{escape_markdown(text)}_"


def code(text: str) -> str:
    """Wrap text in inline code (no escaping needed inside backticks)."""
    return f"`{text}`"


def code_block(text: str, language: str = "") -> str:
    """Wrap text in a fenced code block."""
    return f"```{language}\n{text}\n```"


def link(text: str, url: str) -> str:
    """Create a MarkdownV2 inline link."""
    return f"[{escape_markdown(text)}]({url})"


# ---------------------------------------------------------------------------
# Message size management
# ---------------------------------------------------------------------------

TELEGRAM_MESSAGE_LIMIT = 4096


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split a long message into chunks that fit within Telegram's limit.

    Tries to split on newlines first; falls back to hard splitting if a
    single line exceeds the limit.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # If a single line is too long, hard-split it
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# Notification formatters (parallel to Discord's format_* functions)
# ---------------------------------------------------------------------------


def format_server_started() -> str:
    """Plain-text message indicating the server is back online."""
    return escape_markdown(
        "AgentQueue is back online — the server has started and is ready to process tasks."
    )


def format_task_started(task: Any, project: Any) -> str:
    """Format a task-started notification for Telegram."""
    title = getattr(task, "title", None) or getattr(task, "id", "unknown")
    project_name = getattr(project, "name", None) or getattr(project, "id", "unknown")
    lines = [
        bold("Task Started"),
        f"{bold('Task:')} {escape_markdown(str(title))}",
        f"{bold('Project:')} {escape_markdown(str(project_name))}",
    ]
    if hasattr(task, "task_type") and task.task_type:
        lines.append(f"{bold('Type:')} {escape_markdown(task.task_type)}")
    return "\n".join(lines)


def format_task_completed(task: Any, project: Any, summary: str = "") -> str:
    """Format a task-completed notification for Telegram."""
    title = getattr(task, "title", None) or getattr(task, "id", "unknown")
    project_name = getattr(project, "name", None) or getattr(project, "id", "unknown")
    lines = [
        bold("Task Completed"),
        f"{bold('Task:')} {escape_markdown(str(title))}",
        f"{bold('Project:')} {escape_markdown(str(project_name))}",
    ]
    if summary:
        lines.append(f"\n{escape_markdown(summary)}")
    return "\n".join(lines)


def format_task_failed(task: Any, project: Any, error: str = "") -> str:
    """Format a task-failed notification for Telegram."""
    title = getattr(task, "title", None) or getattr(task, "id", "unknown")
    project_name = getattr(project, "name", None) or getattr(project, "id", "unknown")
    lines = [
        bold("Task Failed"),
        f"{bold('Task:')} {escape_markdown(str(title))}",
        f"{bold('Project:')} {escape_markdown(str(project_name))}",
    ]
    if error:
        lines.append(f"\n{bold('Error:')} {escape_markdown(error)}")
    return "\n".join(lines)


def format_embed_as_text(
    title: str,
    description: str = "",
    fields: list[tuple[str, str]] | None = None,
    footer: str = "",
    url: str = "",
) -> str:
    """Convert a Discord-style embed into Telegram MarkdownV2 text.

    This is the primary bridge for notifications that arrive as rich embeds
    from the orchestrator — they get flattened into a readable text block.
    """
    parts: list[str] = []

    if url:
        parts.append(link(title, url))
    else:
        parts.append(bold(title))

    if description:
        parts.append(escape_markdown(description))

    if fields:
        parts.append("")  # blank line before fields
        for name, value in fields:
            parts.append(f"{bold(name)}: {escape_markdown(value)}")

    if footer:
        parts.append(f"\n{italic(footer)}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Inline keyboard helpers
# ---------------------------------------------------------------------------


def make_inline_keyboard(
    buttons: list[tuple[str, str]],
) -> list[list[dict[str, str]]]:
    """Build an inline keyboard layout for Telegram.

    Parameters
    ----------
    buttons:
        List of (label, callback_data) tuples.

    Returns
    -------
    list[list[dict]]
        Row-major button layout suitable for ``InlineKeyboardMarkup``.
        Each button is on its own row for readability.
    """
    return [[{"text": label, "callback_data": data}] for label, data in buttons]


# ---------------------------------------------------------------------------
# Playbook human-in-the-loop notifications (roadmap 5.4.2)
# ---------------------------------------------------------------------------


def format_playbook_paused(
    *,
    playbook_id: str,
    run_id: str,
    node_id: str,
    last_response: str = "",
    running_seconds: float = 0.0,
    tokens_used: int = 0,
) -> str:
    """Format a playbook-paused notification for Telegram MarkdownV2.

    Includes the accumulated context summary so the human reviewer
    can make an informed decision from their phone.

    See ``docs/specs/design/playbooks.md`` Section 9 — Human-in-the-Loop.
    Roadmap 5.4.2.
    """
    lines: list[str] = [
        bold("Playbook Awaiting Human Review"),
        "",
        f"{bold('Playbook:')} {code(playbook_id)}",
        f"{bold('Run ID:')} {code(run_id)}",
        f"{bold('Paused at:')} {code(node_id)}",
    ]

    if running_seconds > 0:
        if running_seconds >= 60:
            mins = int(running_seconds // 60)
            secs = int(running_seconds % 60)
            duration_str = f"{mins}m {secs}s"
        else:
            duration_str = f"{running_seconds:.1f}s"
        lines.append(f"{bold('Running time:')} {escape_markdown(duration_str)}")

    if tokens_used > 0:
        lines.append(f"{bold('Tokens used:')} {escape_markdown(f'{tokens_used:,}')}")

    if last_response:
        lines.append("")
        lines.append(bold("Context Summary:"))
        # Cap the context for Telegram's 4096 char limit, leaving room for
        # the rest of the message (~500 chars overhead)
        context_preview = last_response
        max_context = 3000
        if len(context_preview) > max_context:
            cut = context_preview[:max_context].rfind("\n")
            if cut > 1000:
                context_preview = context_preview[:cut] + "\n…"
            else:
                context_preview = context_preview[:max_context] + "…"
        lines.append(code_block(context_preview))
    else:
        lines.append("")
        lines.append(italic("No context summary available."))

    lines.append("")
    lines.append(escape_markdown(f"Use /resume-playbook {run_id} to provide your input."))

    return "\n".join(lines)
