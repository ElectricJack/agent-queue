"""Platform-neutral text formatters for notification events.

Every formatter here is a pure function: no Discord client, no gateway, no
bot instance.  They render the markdown body a notification consumer posts and
the strings the daemon logs, so the wording is testable without a transport.

This module holds the lifecycle formatters that outlived the retired Discord
notification path (``src/discord/notifications.py``, deleted per the
Discord-as-a-chat-extension spec §9 row 12).  The ``discord.Embed`` variants
that module carried went with it: nothing builds them any more.
"""

from __future__ import annotations

from src.models import Agent, Task, Workspace

# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------

# Maps error subtype/keyword → (short label, fix suggestion)
_ERROR_PATTERNS: list[tuple[str, str, str]] = [
    # (keyword to search in lowercased error, label, suggestion)
    (
        "error_max_structured_output_retries",
        "Structured-output failure",
        "The model couldn't produce valid JSON output after 7 tries. "
        "Simplify the task description or remove JSON-schema constraints.",
    ),
    (
        "auth",
        "Authentication error",
        "Check that ANTHROPIC_API_KEY (or claude login) is valid and not expired.",
    ),
    (
        "authentication",
        "Authentication error",
        "Check that ANTHROPIC_API_KEY (or claude login) is valid and not expired.",
    ),
    (
        "rate_limit",
        "Rate-limit",
        "The API rate limit was hit. The task will be retried automatically.",
    ),
    (
        "rate limit",
        "Rate-limit",
        "The API rate limit was hit. The task will be retried automatically.",
    ),
    (
        "429",
        "Rate-limit",
        "The API rate limit was hit. The task will be retried automatically.",
    ),
    (
        "quota",
        "Token quota exhausted",
        "Daily or session token quota exceeded. Wait for quota reset or increase limits.",
    ),
    (
        "token",
        "Token limit",
        "The context window or token budget was exceeded. Break the task into smaller pieces.",
    ),
    (
        "timeout",
        "Timeout",
        "The agent exceeded the stuck-timeout. Increase stuck_timeout_seconds or simplify the task.",
    ),
    (
        "config",
        "Configuration error",
        "A config value is invalid. Check model name, allowed_tools, and MCP server settings.",
    ),
    (
        "mcp",
        "MCP server error",
        "An MCP server failed. Verify MCP server configs in the task context.",
    ),
    (
        "permission",
        "Permission denied",
        "The agent couldn't access a file or directory. Check workspace permissions.",
    ),
    (
        "cancelled",
        "Cancelled",
        "The task was stopped manually.",
    ),
]


def classify_error(error_message: str | None) -> tuple[str, str]:
    """Return (error_type_label, fix_suggestion) for a given error message.

    Falls back to a generic label when no pattern matches.  Platform-agnostic:
    dashboard and log consumers read the same pair.
    """
    if not error_message:
        return "Unknown error", "Check daemon logs for details."
    lowered = error_message.lower()
    for keyword, label, suggestion in _ERROR_PATTERNS:
        if keyword.lower() in lowered:
            return label, suggestion
    return "Unexpected error", "Check daemon logs (`~/.agent-queue/daemon.log`) for full details."


# ---------------------------------------------------------------------------
# Task lifecycle formatters
# ---------------------------------------------------------------------------


def format_task_started(task: Task, agent: Agent, workspace: Workspace | None = None) -> str:
    """Plain-text body for the task-started notification.

    Carried as ``TaskThreadOpenEvent.initial_message``, so the thread's opening
    line reads the same on every transport.
    """
    lines = [
        f"**Task Started:** `{task.id}` — {task.title}",
        f"Project: `{task.project_id}` | Agent: {agent.name}",
    ]
    if workspace:
        label = workspace.name or workspace.workspace_path
        lines.append(f"Workspace: `{label}`")
    if task.branch_name:
        lines.append(f"Branch: `{task.branch_name}`")
    lines.append("Status: IN_PROGRESS")
    return "\n".join(lines)


def format_failed_blocked_report(
    failed_tasks: list[Task],
    blocked_tasks: list[Task],
) -> str:
    """Format a periodic summary of all tasks currently in FAILED or BLOCKED status.

    Produces a concise markdown message listing tasks that need attention,
    grouped by status, with actionable commands for each.
    """
    total = len(failed_tasks) + len(blocked_tasks)
    lines = [
        f"📊 **Attention Required — {total} task{'s' if total != 1 else ''} "
        f"need{'s' if total == 1 else ''} intervention**",
    ]

    if failed_tasks:
        lines.append(f"\n**Failed ({len(failed_tasks)}):**")
        for t in failed_tasks[:10]:
            lines.append(
                f"• `{t.id}` — {t.title} "
                f"(project: `{t.project_id}`, retries: {t.retry_count}/{t.max_retries})"
            )
        if len(failed_tasks) > 10:
            lines.append(f"  +{len(failed_tasks) - 10} more")

    if blocked_tasks:
        lines.append(f"\n**Blocked ({len(blocked_tasks)}):**")
        for t in blocked_tasks[:10]:
            lines.append(f"• `{t.id}` — {t.title} (project: `{t.project_id}`)")
        if len(blocked_tasks) > 10:
            lines.append(f"  +{len(blocked_tasks) - 10} more")

    lines.append("\n_Use `/restart-task` to retry or `/skip-task` to unblock dependents._")
    return "\n".join(lines)
