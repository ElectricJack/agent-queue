"""Notification formatting for Discord messages about task lifecycle events.

**String formatters** (``format_*``) produce human-readable markdown strings
that are easy to unit test without a live Discord connection and are used for
logging and plain-text fallback.

**Embed formatters** (``format_*_embed``) produce ``discord.Embed`` objects for
rich Discord presentation — color-coded by severity, with structured fields for
task metadata.  The orchestrator passes both versions through the notification
callback so the bot can choose the appropriate format.

**Interactive views** (``TaskFailedView``, ``TaskApprovalView``,
``AgentQuestionView``) attach action buttons
to notification embeds so users can retry, skip, approve tasks, or reply to
agent questions directly from Discord without memorizing slash commands.

``classify_error`` pattern-matches raw error messages against known failure modes
and returns an actionable fix suggestion -- this turns opaque stack traces into
guidance the user can act on immediately from Discord.
"""

from __future__ import annotations

import discord

from src.discord.embeds import (
    success_embed,
    error_embed,
    warning_embed,
    info_embed,
    critical_embed,
    status_embed,
    make_embed,
    truncate,
    EmbedStyle,
    TASK_TYPE_EMOJIS,
    LIMIT_FIELD_VALUE,
    LIMIT_DESCRIPTION,
)
from src.models import Task, Agent, AgentOutput, TaskStatus, Workspace

# ---------------------------------------------------------------------------
# Server lifecycle notifications
# ---------------------------------------------------------------------------


def format_server_started() -> str:
    """Plain-text message indicating the server is back online."""
    return (
        "✅ **AgentQueue is back online** — the server has started and is ready to process tasks."
    )


def format_server_started_embed() -> discord.Embed:
    """Rich embed announcing the server is back online.

    Uses a green success embed to clearly signal that the system is
    operational and ready to accept work.
    """
    return success_embed(
        "Server Online",
        description=(
            "AgentQueue has started and is ready to process tasks.\n\n"
            "All systems are operational — commands, notifications, and "
            "task orchestration are now available."
        ),
    )


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

    Falls back to a generic label when no pattern matches.
    """
    if not error_message:
        return "Unknown error", "Check daemon logs for details."
    lowered = error_message.lower()
    for keyword, label, suggestion in _ERROR_PATTERNS:
        if keyword.lower() in lowered:
            return label, suggestion
    return "Unexpected error", "Check daemon logs (`~/.agent-queue/daemon.log`) for full details."


def format_task_started(task: Task, agent: Agent, workspace: Workspace | None = None) -> str:
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


def format_task_completed(task: Task, agent: Agent, output: AgentOutput) -> str:
    lines = [
        f"**Task Completed:** `{task.id}` — {task.title}",
        f"Project: `{task.project_id}` | Agent: {agent.name}",
        f"Tokens used: {output.tokens_used:,}",
    ]
    if output.summary:
        lines.append(f"Summary: {output.summary}")
    if output.files_changed:
        lines.append(f"Files changed: {', '.join(output.files_changed)}")
    return "\n".join(lines)


def format_task_failed(task: Task, agent: Agent, output: AgentOutput) -> str:
    error_type, suggestion = classify_error(output.error_message)
    lines = [
        f"**Task Failed:** `{task.id}` — {task.title}",
        f"Project: `{task.project_id}` | Agent: {agent.name} | Retry: {task.retry_count}/{task.max_retries}",
        f"Error type: **{error_type}**",
    ]
    if output.error_message:
        # Show first 300 chars of the error — enough to diagnose without flooding Discord
        snippet = output.error_message[:300]
        if len(output.error_message) > 300:
            snippet += "…"
        lines.append(f"```\n{snippet}\n```")
    lines.append(f"💡 {suggestion}")
    lines.append(f"_Use `/agent-error {task.id}` for the full error log._")
    return "\n".join(lines)


def format_task_blocked(task: Task, last_error: str | None = None) -> str:
    lines = [
        f"**Task Blocked:** `{task.id}` — {task.title}",
        f"Project: `{task.project_id}` | Max retries ({task.max_retries}) exhausted. Manual intervention required.",
    ]
    if last_error:
        error_type, suggestion = classify_error(last_error)
        lines.append(f"Last error type: **{error_type}**")
        lines.append(f"💡 {suggestion}")
    lines.append(f"_Use `/agent-error {task.id}` to inspect the last error._")
    return "\n".join(lines)


def format_pr_created(task: Task, pr_url: str) -> str:
    return (
        f"**PR Created:** `{task.id}` — {task.title}\n"
        f"Project: `{task.project_id}`\n"
        f"Review and merge to complete: {pr_url}\n"
        f"Status: AWAITING_APPROVAL"
    )


def format_agent_question(task: Task, agent: Agent, question: str) -> str:
    return (
        f"**Agent Question:** `{task.id}` — {task.title}\n"
        f"Project: `{task.project_id}` | Agent: {agent.name}\n"
        f"> {question[:500]}"
    )


def format_chain_stuck(
    blocked_task: Task,
    stuck_tasks: list[Task],
) -> str:
    """Format a notification about downstream tasks stuck because of a blocked task."""
    task_list = ", ".join(f"`{t.id}`" for t in stuck_tasks[:5])
    if len(stuck_tasks) > 5:
        task_list += f" +{len(stuck_tasks) - 5} more"
    return (
        f"⛓️ **Chain Stuck:** `{blocked_task.id}` BLOCKED → "
        f"{len(stuck_tasks)} stuck: {task_list}\n"
        f"`/skip-task {blocked_task.id}` or `/restart-task {blocked_task.id}`"
    )


def format_stuck_defined_task(
    task: Task,
    blocking_deps: list[tuple[str, str, str]],
    stuck_hours: float,
) -> str:
    """Format a notification for a DEFINED task stuck waiting on dependencies."""
    if blocking_deps:
        blockers = ", ".join(
            f"`{dep_id}` ({dep_status})" for dep_id, _, dep_status in blocking_deps[:3]
        )
        if len(blocking_deps) > 3:
            blockers += f" +{len(blocking_deps) - 3} more"
        return (
            f"⏳ **Stuck:** `{task.id}` — {task.title} "
            f"(DEFINED {stuck_hours:.1f}h, blocked by {blockers})\n"
            f"`/skip-task` or `/restart-task` the blocker to unblock"
        )
    return (
        f"⏳ **Stuck:** `{task.id}` — {task.title} "
        f"(DEFINED {stuck_hours:.1f}h, no unmet deps found — possible bug)"
    )


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


def format_budget_warning(project_name: str, usage: int, limit: int) -> str:
    pct = (usage / limit * 100) if limit > 0 else 0
    return (
        f"**Budget Warning:** Project **{project_name}** at {pct:.0f}% "
        f"({usage:,} / {limit:,} tokens)"
    )


def format_plan_generated(
    parent_task: Task,
    generated_tasks: list[Task],
    *,
    workspace_path: str | None = None,
    chained: bool = True,
) -> str:
    """Format a plain-text notification for auto-generated plan subtasks.

    Returns a human-readable markdown string listing all tasks created
    from a plan file, suitable for logging and fallback display.
    """
    count = len(generated_tasks)
    lines = [
        f"📋 **Plan Generated — {count} Task{'s' if count != 1 else ''} Created**",
        f"Parent: `{parent_task.id}` — {parent_task.title}",
        f"Project: `{parent_task.project_id}`",
    ]
    if workspace_path:
        lines.append(f"Workspace: `{workspace_path}`")
    if chained and count > 1:
        chain_str = " → ".join(f"`{t.id}`" for t in generated_tasks)
        lines.append(f"Chain: {chain_str}")
    lines.append("")
    for idx, t in enumerate(generated_tasks, 1):
        type_emoji = ""
        if t.task_type:
            type_emoji = TASK_TYPE_EMOJIS.get(t.task_type.value, "") + " "
        lines.append(f"**{idx}.** {type_emoji}`{t.id}` — {t.title} (priority: {t.priority})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rich embed formatters
# ---------------------------------------------------------------------------
# Each function mirrors a string formatter above and returns a
# ``discord.Embed`` for richer Discord presentation.  The string versions
# are preserved for logging, testing, and fallback.


def format_task_started_embed(
    task: Task, agent: Agent, workspace: Workspace | None = None
) -> discord.Embed:
    """Rich embed version of :func:`format_task_started`.

    Uses the IN_PROGRESS status color (amber) to visually indicate that
    the task is now actively being worked on by an agent.
    """
    fields: list[tuple[str, str, bool]] = [
        ("Task ID", f"`{task.id}`", True),
        ("Project", f"`{task.project_id}`", True),
        ("Agent", agent.name, True),
        ("Status", "\U0001f7e1 IN_PROGRESS", True),
    ]
    if workspace:
        label = workspace.name or workspace.workspace_path
        fields.append(("Workspace", f"`{label}`", True))
    if task.branch_name:
        fields.append(("Branch", f"`{task.branch_name}`", True))
    return status_embed(
        TaskStatus.IN_PROGRESS.value,
        f"Task Started — {task.title}",
        fields=fields,
    )


def format_task_completed_embed(
    task: Task,
    agent: Agent,
    output: AgentOutput,
) -> discord.Embed:
    """Rich embed version of :func:`format_task_completed`.

    The summary is placed in the embed *description* (4096 char limit)
    rather than a field (1024 char limit) so that longer agent summaries
    are not truncated.
    """
    # Use the embed description for the summary (4096 chars vs 1024 for fields)
    description = truncate(output.summary, LIMIT_DESCRIPTION) if output.summary else None

    fields: list[tuple[str, str, bool]] = [
        ("Task ID", f"`{task.id}`", True),
        ("Project", f"`{task.project_id}`", True),
        ("Agent", agent.name, True),
        ("Tokens Used", f"{output.tokens_used:,}", True),
    ]
    if output.files_changed:
        files_text = ", ".join(f"`{f}`" for f in output.files_changed)
        fields.append(
            (
                "Files Changed",
                truncate(files_text, LIMIT_FIELD_VALUE),
                False,
            )
        )
    return success_embed(f"Task Completed — {task.title}", description=description, fields=fields)


def format_task_failed_embed(
    task: Task,
    agent: Agent,
    output: AgentOutput,
) -> discord.Embed:
    """Rich embed version of :func:`format_task_failed`.

    The error detail is placed in the embed *description* (4096 char limit)
    rather than a field (1024 char limit) so that longer error messages
    are not truncated as aggressively.
    """
    error_type, suggestion = classify_error(output.error_message)

    # Use the embed description for error detail (4096 chars vs 1024 for fields)
    description: str | None = None
    if output.error_message:
        # Reserve space for code-block fences (```\n ... \n```)
        max_error_len = LIMIT_DESCRIPTION - 8
        snippet = output.error_message[:max_error_len]
        if len(output.error_message) > max_error_len:
            snippet += "\u2026"
        description = f"```\n{snippet}\n```"

    fields: list[tuple[str, str, bool]] = [
        ("Task ID", f"`{task.id}`", True),
        ("Project", f"`{task.project_id}`", True),
        ("Agent", agent.name, True),
        ("Retries", f"{task.retry_count}/{task.max_retries}", True),
        ("Error Type", f"**{error_type}**", True),
    ]
    fields.append(("Suggestion", f"\U0001f4a1 {suggestion}", False))
    fields.append(
        (
            "Next Step",
            f"Use `/agent-error {task.id}` for the full error log.",
            False,
        )
    )
    return error_embed(f"Task Failed — {task.title}", description=description, fields=fields)


def format_task_blocked_embed(
    task: Task,
    last_error: str | None = None,
) -> discord.Embed:
    """Rich embed version of :func:`format_task_blocked`."""
    fields: list[tuple[str, str, bool]] = [
        ("Task ID", f"`{task.id}`", True),
        ("Project", f"`{task.project_id}`", True),
        ("Status", f"Max retries ({task.max_retries}) exhausted", False),
    ]
    if last_error:
        error_type, suggestion = classify_error(last_error)
        fields.append(("Last Error Type", f"**{error_type}**", True))
        fields.append(("Suggestion", f"\U0001f4a1 {suggestion}", False))
    fields.append(
        (
            "Action Required",
            f"Use `/agent-error {task.id}` to inspect the last error.",
            False,
        )
    )
    return critical_embed(f"Task Blocked — {task.title}", fields=fields)


def format_pr_created_embed(task: Task, pr_url: str) -> discord.Embed:
    """Rich embed version of :func:`format_pr_created`."""
    fields: list[tuple[str, str, bool]] = [
        ("Task ID", f"`{task.id}`", True),
        ("Project", f"`{task.project_id}`", True),
        ("Status", "AWAITING_APPROVAL", True),
        ("Pull Request", f"[Review and merge to complete]({pr_url})", False),
    ]
    return info_embed(f"PR Created — {task.title}", fields=fields, url=pr_url)


def format_agent_question_embed(
    task: Task,
    agent: Agent,
    question: str,
) -> discord.Embed:
    """Rich embed version of :func:`format_agent_question`."""
    fields: list[tuple[str, str, bool]] = [
        ("Task ID", f"`{task.id}`", True),
        ("Project", f"`{task.project_id}`", True),
        ("Agent", agent.name, True),
        ("Question", f"> {truncate(question, LIMIT_FIELD_VALUE - 2)}", False),
    ]
    return warning_embed(f"Agent Question — {task.title}", fields=fields)


def format_chain_stuck_embed(
    blocked_task: Task,
    stuck_tasks: list[Task],
) -> discord.Embed:
    """Rich embed version of :func:`format_chain_stuck`."""
    task_list = "\n".join(f"\u2022 `{t.id}` \u2014 {t.title}" for t in stuck_tasks[:10])
    if len(stuck_tasks) > 10:
        task_list += f"\n+{len(stuck_tasks) - 10} more"
    fields: list[tuple[str, str, bool]] = [
        ("Blocked Task", f"`{blocked_task.id}` \u2014 {blocked_task.title}", False),
        ("Project", f"`{blocked_task.project_id}`", True),
        ("Affected Tasks", str(len(stuck_tasks)), True),
        ("Downstream Tasks", truncate(task_list, LIMIT_FIELD_VALUE), False),
        (
            "Actions",
            f"`/skip-task {blocked_task.id}` or `/restart-task {blocked_task.id}`",
            False,
        ),
    ]
    return critical_embed(
        "Chain Stuck",
        description=(
            f"Task `{blocked_task.id}` is BLOCKED, preventing "
            f"{len(stuck_tasks)} downstream task(s) from running."
        ),
        fields=fields,
    )


def format_stuck_defined_task_embed(
    task: Task,
    blocking_deps: list[tuple[str, str, str]],
    stuck_hours: float,
) -> discord.Embed:
    """Rich embed version of :func:`format_stuck_defined_task`."""
    fields: list[tuple[str, str, bool]] = [
        ("Task ID", f"`{task.id}`", True),
        ("Project", f"`{task.project_id}`", True),
        ("Stuck Duration", f"{stuck_hours:.1f} hours", True),
    ]
    if blocking_deps:
        blockers = "\n".join(
            f"\u2022 `{dep_id}` ({dep_status})" for dep_id, _, dep_status in blocking_deps[:5]
        )
        if len(blocking_deps) > 5:
            blockers += f"\n+{len(blocking_deps) - 5} more"
        fields.append(
            (
                "Blocking Dependencies",
                truncate(blockers, LIMIT_FIELD_VALUE),
                False,
            )
        )
        fields.append(
            (
                "Actions",
                "`/skip-task` or `/restart-task` the blocker to unblock",
                False,
            )
        )
    else:
        fields.append(
            (
                "Note",
                "No unmet dependencies found \u2014 possible bug",
                False,
            )
        )
    return warning_embed(
        f"Task Stuck — {task.title}",
        description=f"DEFINED for {stuck_hours:.1f}h, waiting on dependencies.",
        fields=fields,
    )


def format_failed_blocked_report_embed(
    failed_tasks: list[Task],
    blocked_tasks: list[Task],
) -> discord.Embed:
    """Rich embed version of :func:`format_failed_blocked_report`.

    Uses a critical (dark red) embed with structured fields grouping tasks
    by status, giving operators an at-a-glance view of everything needing
    manual intervention.
    """
    total = len(failed_tasks) + len(blocked_tasks)
    description = (
        f"{total} task{'s' if total != 1 else ''} "
        f"currently {'require' if total != 1 else 'requires'} manual intervention."
    )

    fields: list[tuple[str, str, bool]] = [
        ("Failed", str(len(failed_tasks)), True),
        ("Blocked", str(len(blocked_tasks)), True),
    ]

    if failed_tasks:
        task_lines = "\n".join(
            f"\u2022 `{t.id}` \u2014 {t.title} ({t.retry_count}/{t.max_retries})"
            for t in failed_tasks[:8]
        )
        if len(failed_tasks) > 8:
            task_lines += f"\n+{len(failed_tasks) - 8} more"
        fields.append(("Failed Tasks", truncate(task_lines, LIMIT_FIELD_VALUE), False))

    if blocked_tasks:
        task_lines = "\n".join(f"\u2022 `{t.id}` \u2014 {t.title}" for t in blocked_tasks[:8])
        if len(blocked_tasks) > 8:
            task_lines += f"\n+{len(blocked_tasks) - 8} more"
        fields.append(("Blocked Tasks", truncate(task_lines, LIMIT_FIELD_VALUE), False))

    fields.append(
        (
            "Actions",
            "`/restart-task <id>` to retry \u2022 `/skip-task <id>` to unblock dependents",
            False,
        )
    )
    return critical_embed("Attention Required", description=description, fields=fields)


def format_budget_warning_embed(
    project_name: str,
    usage: int,
    limit: int,
) -> discord.Embed:
    """Rich embed version of :func:`format_budget_warning`.

    The embed color shifts from amber to orange to red as the budget
    utilization increases, providing an at-a-glance severity indicator.
    """
    pct = (usage / limit * 100) if limit > 0 else 0
    remaining = max(0, limit - usage)

    # Dynamic color: amber → orange → red as budget depletes
    if pct >= 95:
        color = 0xE74C3C  # Red
    elif pct >= 80:
        color = 0xE67E22  # Orange
    else:
        color = 0xF39C12  # Amber

    fields: list[tuple[str, str, bool]] = [
        ("Project", f"**{project_name}**", True),
        ("Used", f"{usage:,} tokens", True),
        ("Limit", f"{limit:,} tokens", True),
        ("Remaining", f"{remaining:,} tokens ({100 - pct:.0f}%)", True),
    ]
    return warning_embed(
        f"Budget Warning — {pct:.0f}% Used",
        fields=fields,
        color_override=color,
    )


def format_plan_generated_embed(
    parent_task: Task,
    generated_tasks: list[Task],
    *,
    workspace_path: str | None = None,
    chained: bool = True,
) -> discord.Embed:
    """Rich embed for auto-generated plan subtasks.

    Builds a visually structured embed that displays all relevant metadata
    for each auto-generated task: title, priority, project, workspace,
    task type, and dependency chain — making it easy to scan at a glance.

    Parameters
    ----------
    parent_task:
        The task whose completion produced the plan file.
    generated_tasks:
        The list of newly created subtasks from the plan.
    workspace_path:
        The workspace directory used by the parent task (shown when available).
    chained:
        Whether tasks are chained in a sequential dependency order.
    """
    count = len(generated_tasks)
    plural = "s" if count != 1 else ""

    # --- Description block ---------------------------------------------------
    desc_lines: list[str] = [
        f"Task `{parent_task.id}` completed with an implementation plan.",
        f"**{count}** subtask{plural} {'have' if count != 1 else 'has'} been "
        f"created{' and chained for sequential execution' if chained and count > 1 else ''}.",
    ]

    # Show dependency chain as a compact arrow diagram
    if chained and count > 1:
        chain_ids = " → ".join(f"`{t.id}`" for t in generated_tasks)
        desc_lines.append(f"\n**Execution order:** {chain_ids}")

    description = "\n".join(desc_lines)

    # --- Header fields -------------------------------------------------------
    fields: list[tuple[str, str, bool]] = [
        ("Parent Task", f"`{parent_task.id}`\n{truncate(parent_task.title, 80)}", True),
        ("Project", f"`{parent_task.project_id}`", True),
    ]

    if workspace_path:
        # Show only the last 2 path components to keep it readable
        short_path = workspace_path
        parts = workspace_path.replace("\\", "/").rstrip("/").split("/")
        if len(parts) > 2:
            short_path = "…/" + "/".join(parts[-2:])
        fields.append(("Workspace", f"`{short_path}`", True))

    # --- Separator -----------------------------------------------------------
    fields.append(("─── Subtasks ───", "\u200b", False))  # zero-width space value

    # --- Per-task fields -----------------------------------------------------
    for idx, t in enumerate(generated_tasks, 1):
        # Build the field name with step number and optional type emoji
        type_emoji = ""
        if t.task_type:
            type_emoji = TASK_TYPE_EMOJIS.get(t.task_type.value, "")
            if type_emoji:
                type_emoji += " "

        field_name = f"{type_emoji}Step {idx}/{count}: {truncate(t.title, 80)}"

        # Build the field value with key metadata
        detail_parts: list[str] = [f"**ID:** `{t.id}`"]

        detail_parts.append(f"**Priority:** {t.priority}")

        if t.task_type:
            detail_parts.append(
                f"**Type:** {TASK_TYPE_EMOJIS.get(t.task_type.value, '')} `{t.task_type.value}`"
            )

        if t.requires_approval:
            detail_parts.append("🔒 **Requires approval**")

        # Show dependency info for chained tasks (except the first)
        if chained and idx > 1:
            prev = generated_tasks[idx - 2]
            detail_parts.append(f"⏳ Depends on `{prev.id}`")

        # Show a snippet of the description if available
        if t.description:
            # Extract first meaningful line (skip headers/blanks)
            snippet = _extract_description_snippet(t.description, max_len=120)
            if snippet:
                detail_parts.append(f"*{snippet}*")

        field_value = "\n".join(detail_parts)
        fields.append((field_name, truncate(field_value, 1024), False))

    # --- Build the embed with plan-generation color (teal/cyan) --------------
    # Use a distinctive teal color (0x1ABC9C) to stand out from standard
    # status notifications (success=green, error=red, etc.)
    _PLAN_GENERATED_COLOR = 0x1ABC9C

    embed = make_embed(
        EmbedStyle.INFO,
        f"Plan Generated — {count} New Task{plural}",
        description=truncate(description, LIMIT_DESCRIPTION),
        fields=fields,
        color_override=_PLAN_GENERATED_COLOR,
    )

    return embed


def _extract_description_snippet(description: str, *, max_len: int = 120) -> str:
    """Extract a short, meaningful snippet from a task description.

    Skips markdown headers, blank lines, and common boilerplate prefixes
    to find the first substantive line of content.
    """
    for line in description.split("\n"):
        stripped = line.strip()
        # Skip blank lines, headers, horizontal rules, and boilerplate
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("---") or stripped.startswith("==="):
            continue
        if stripped.startswith("Parent task:") or stripped.startswith("Plan context:"):
            continue
        # Found a substantive line
        if len(stripped) > max_len:
            return stripped[: max_len - 1] + "…"
        return stripped
    return ""


# ---------------------------------------------------------------------------
# Merge / push notification formatters
# ---------------------------------------------------------------------------


def format_merge_conflict(task: Task, branch_name: str, default_branch: str) -> str:
    """Plain-text notification for a merge conflict during sync-and-merge."""
    return (
        f"**Merge Conflict:** Task `{task.id}` — {task.title}\n"
        f"Project: `{task.project_id}`\n"
        f"Branch `{branch_name}` has conflicts with `{default_branch}`.\n"
        f"Manual resolution needed — check out the branch locally and resolve conflicts."
    )


def format_merge_conflict_embed(
    task: Task,
    branch_name: str,
    default_branch: str,
) -> discord.Embed:
    """Rich embed for a merge conflict during sync-and-merge."""
    fields: list[tuple[str, str, bool]] = [
        ("Task ID", f"`{task.id}`", True),
        ("Project", f"`{task.project_id}`", True),
        ("Branch", f"`{branch_name}`", True),
        ("Target", f"`{default_branch}`", True),
        (
            "Action Required",
            f"Branch `{branch_name}` has conflicts with `{default_branch}`.\n"
            f"Check out the branch locally and resolve conflicts manually.",
            False,
        ),
    ]
    return error_embed(f"Merge Conflict — {task.title}", fields=fields)


def format_push_failed(
    task: Task,
    default_branch: str,
    error_detail: str,
) -> str:
    """Plain-text notification for a push failure after retries."""
    return (
        f"**Push Failed:** Task `{task.id}` — {task.title}\n"
        f"Project: `{task.project_id}`\n"
        f"Could not push `{default_branch}` after retries. "
        f"The workspace may be diverged and require manual intervention.\n"
        f"```\n{error_detail[:300]}\n```"
    )


def format_push_failed_embed(
    task: Task,
    default_branch: str,
    error_detail: str,
) -> discord.Embed:
    """Rich embed for a push failure after retries."""
    snippet = error_detail[:300]
    if len(error_detail) > 300:
        snippet += "…"
    fields: list[tuple[str, str, bool]] = [
        ("Task ID", f"`{task.id}`", True),
        ("Project", f"`{task.project_id}`", True),
        ("Branch", f"`{default_branch}`", True),
        ("Error Detail", f"```\n{snippet}\n```", False),
        (
            "Action Required",
            "Push failed after retries. The workspace may be diverged.\n"
            "Inspect the workspace and push manually, or restart the task.",
            False,
        ),
    ]
    return warning_embed(f"Push Failed — {task.title}", fields=fields)


# ---------------------------------------------------------------------------
# Interactive action views for notification embeds
# ---------------------------------------------------------------------------


def _split_message(text: str, *, limit: int = 1900) -> list[str]:
    """Split *text* into chunks that fit within Discord's message limit.

    Tries to break on newline boundaries for readability.  Falls back to
    hard-splitting at *limit* when a single line exceeds it.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            # If the single line itself exceeds the limit, hard-split it.
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text]


class TaskStartedView(discord.ui.View):
    """Action buttons attached to task-started notifications.

    Provides a one-click "Stop Task" button and a "View Context" button.
    Stop Task cancels the running task. View Context opens an ephemeral
    message showing the full task description and all context passed to the
    agent — useful for reviewing exactly what the agent was told to do.
    """

    def __init__(
        self,
        task_id: str,
        handler=None,
        task_description: str = "",
        task_contexts: list[dict] | None = None,
    ) -> None:
        super().__init__(timeout=86400)  # 24 hours
        self.task_id = task_id
        self._handler = handler
        self._task_description = task_description
        self._task_contexts = task_contexts or []

    @discord.ui.button(
        label="View Context",
        style=discord.ButtonStyle.secondary,
        emoji="📋",
    )
    async def view_context_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Show the full task description and attached context."""
        parts: list[str] = []
        parts.append(f"## Task `{self.task_id}`\n")

        desc = self._task_description.strip() if self._task_description else ""
        if desc:
            parts.append(f"### Description\n{desc}\n")
        else:
            parts.append("*No description available.*\n")

        # Show attached context entries (e.g. additional context, docs,
        # reopen feedback, etc.) that were passed to the agent.
        if self._task_contexts:
            for ctx_entry in self._task_contexts:
                label = ctx_entry.get("label") or ctx_entry.get("type") or "Context"
                content = ctx_entry.get("content", "")
                if content:
                    parts.append(f"### {label}\n{content}\n")

        full_text = "\n".join(parts)

        # Discord ephemeral messages are capped at 2000 chars; split into
        # multiple follow-ups if needed.
        chunks = _split_message(full_text, limit=1900)
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    @discord.ui.button(
        label="Stop Task",
        style=discord.ButtonStyle.danger,
        emoji="⏹️",
    )
    async def stop_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("stop_task", {"task_id": self.task_id})
        if "error" in result:
            await interaction.followup.send(f"Could not stop: {result['error']}", ephemeral=True)
        else:
            await interaction.followup.send(
                f"⏹️ Task `{self.task_id}` stopped.",
                ephemeral=True,
            )
            # Disable button after action
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


class TaskFailedView(discord.ui.View):
    """Action buttons attached to failed task notifications.

    Provides one-click Retry and Skip buttons so the user doesn't
    have to remember ``/restart-task`` or ``/skip-task`` slash commands.
    The handler is passed at creation time and called when buttons are pressed.
    """

    def __init__(self, task_id: str, handler=None) -> None:
        super().__init__(timeout=3600)  # 1 hour
        self.task_id = task_id
        self._handler = handler

    @discord.ui.button(
        label="Retry Task",
        style=discord.ButtonStyle.primary,
        emoji="🔄",
    )
    async def retry_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("restart_task", {"task_id": self.task_id})
        if "error" in result:
            await interaction.followup.send(f"Could not restart: {result['error']}", ephemeral=True)
        else:
            prev = result.get("previous_status", "?")
            await interaction.followup.send(
                f"🔄 Task `{self.task_id}` restarted ({prev} → READY)",
                ephemeral=True,
            )
            # Disable buttons after action
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    @discord.ui.button(
        label="Skip Task",
        style=discord.ButtonStyle.secondary,
        emoji="⏭️",
    )
    async def skip_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("skip_task", {"task_id": self.task_id})
        if "error" in result:
            await interaction.followup.send(f"Could not skip: {result['error']}", ephemeral=True)
        else:
            unblocked = result.get("unblocked_count", 0)
            msg = f"⏭️ Task `{self.task_id}` skipped."
            if unblocked:
                msg += f" {unblocked} task(s) unblocked."
            await interaction.followup.send(msg, ephemeral=True)
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    @discord.ui.button(
        label="View Error",
        style=discord.ButtonStyle.secondary,
        emoji="🔍",
    )
    async def view_error_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("get_agent_error", {"task_id": self.task_id})
        if "error" in result:
            await interaction.followup.send(
                f"Could not fetch error: {result['error']}", ephemeral=True
            )
        else:
            error_msg = result.get("error_message") or "No error message recorded."
            snippet = error_msg[:1800]
            if len(error_msg) > 1800:
                snippet += "\n… _(truncated)_"
            await interaction.followup.send(
                f"**Error for `{self.task_id}`:**\n```\n{snippet}\n```",
                ephemeral=True,
            )


class TaskApprovalView(discord.ui.View):
    """Action buttons attached to PR-created / awaiting-approval notifications.

    Provides one-click Approve and Restart buttons for tasks in
    AWAITING_APPROVAL status.
    """

    def __init__(self, task_id: str, handler=None) -> None:
        super().__init__(timeout=86400)  # 24 hours
        self.task_id = task_id
        self._handler = handler

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        emoji="✅",
    )
    async def approve_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("approve_task", {"task_id": self.task_id})
        if "error" in result:
            await interaction.followup.send(f"Could not approve: {result['error']}", ephemeral=True)
        else:
            await interaction.followup.send(
                f"✅ Task `{self.task_id}` approved and completed.",
                ephemeral=True,
            )
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    @discord.ui.button(
        label="Restart",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
    )
    async def restart_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("restart_task", {"task_id": self.task_id})
        if "error" in result:
            await interaction.followup.send(f"Could not restart: {result['error']}", ephemeral=True)
        else:
            await interaction.followup.send(
                f"🔄 Task `{self.task_id}` restarted → READY",
                ephemeral=True,
            )
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


class TaskBlockedView(discord.ui.View):
    """Action buttons for blocked task notifications.

    Provides Restart and Skip buttons for tasks that have exhausted retries.
    """

    def __init__(self, task_id: str, handler=None) -> None:
        super().__init__(timeout=86400)  # 24 hours
        self.task_id = task_id
        self._handler = handler

    @discord.ui.button(
        label="Restart Task",
        style=discord.ButtonStyle.primary,
        emoji="🔄",
    )
    async def restart_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("restart_task", {"task_id": self.task_id})
        if "error" in result:
            await interaction.followup.send(f"Could not restart: {result['error']}", ephemeral=True)
        else:
            await interaction.followup.send(
                f"🔄 Task `{self.task_id}` restarted → READY",
                ephemeral=True,
            )
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    @discord.ui.button(
        label="Skip Task",
        style=discord.ButtonStyle.secondary,
        emoji="⏭️",
    )
    async def skip_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("skip_task", {"task_id": self.task_id})
        if "error" in result:
            await interaction.followup.send(f"Could not skip: {result['error']}", ephemeral=True)
        else:
            unblocked = result.get("unblocked_count", 0)
            msg = f"⏭️ Task `{self.task_id}` skipped."
            if unblocked:
                msg += f" {unblocked} task(s) unblocked."
            await interaction.followup.send(msg, ephemeral=True)
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


class AgentQuestionModal(discord.ui.Modal, title="Reply to Agent"):
    """Modal dialog for typing a reply to an agent question.

    Opened when the user clicks the "Reply" button on an agent question
    notification.  On submit, calls ``CommandHandler.execute("provide_input", ...)``
    to transition the task from WAITING_INPUT → READY with the user's reply
    appended to the task description.
    """

    answer = discord.ui.TextInput(
        label="Your reply",
        style=discord.TextStyle.long,
        placeholder="Type your answer to the agent's question…",
        required=True,
        max_length=2000,
    )

    def __init__(self, task_id: str, handler=None) -> None:
        super().__init__()
        self.task_id = task_id
        self._handler = handler

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute(
            "provide_input",
            {"task_id": self.task_id, "input": self.answer.value},
        )
        if "error" in result:
            await interaction.followup.send(
                f"Could not submit reply: {result['error']}", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"💬 Reply sent for task `{self.task_id}` — task re-queued.",
                ephemeral=True,
            )


class AgentQuestionView(discord.ui.View):
    """Action buttons attached to agent question notifications.

    Provides a one-click "Reply" button that opens a modal text input so the
    user can answer the agent's question without memorizing slash commands.
    A secondary "Skip Task" button allows skipping the task entirely.
    """

    def __init__(self, task_id: str, handler=None) -> None:
        super().__init__(timeout=86400)  # 24 hours
        self.task_id = task_id
        self._handler = handler

    @discord.ui.button(
        label="Reply",
        style=discord.ButtonStyle.primary,
        emoji="💬",
    )
    async def reply_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        modal = AgentQuestionModal(self.task_id, handler=self._handler)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Skip Task",
        style=discord.ButtonStyle.secondary,
        emoji="⏭️",
    )
    async def skip_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("skip_task", {"task_id": self.task_id})
        if "error" in result:
            await interaction.followup.send(f"Could not skip: {result['error']}", ephemeral=True)
        else:
            unblocked = result.get("unblocked_count", 0)
            msg = f"⏭️ Task `{self.task_id}` skipped."
            if unblocked:
                msg += f" {unblocked} task(s) unblocked."
            await interaction.followup.send(msg, ephemeral=True)
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


# ---------------------------------------------------------------------------
# Plan approval views
# ---------------------------------------------------------------------------


class PlanChangesModal(discord.ui.Modal, title="Request Plan Changes"):
    """Modal dialog for providing feedback to revise a plan.

    Opens a text input where the user can type their requested changes.
    On submit, the feedback is forwarded via
    ``CommandHandler.execute("reject_plan", …)`` to reopen the task
    with the feedback appended.
    """

    feedback_input = discord.ui.TextInput(
        label="What changes do you want?",
        style=discord.TextStyle.long,
        placeholder="Describe the changes you'd like to the plan…",
        required=True,
        max_length=2000,
    )

    def __init__(self, task_id: str, handler=None, plan_message=None) -> None:
        super().__init__()
        self.task_id = task_id
        self._handler = handler
        self._plan_message = plan_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute(
            "reject_plan",
            {"task_id": self.task_id, "feedback": self.feedback_input.value},
        )
        if "error" in result:
            await interaction.followup.send(
                f"Could not request changes: {result['error']}", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"✏️ Changes requested for plan `{self.task_id}`. Task reopened with feedback.",
                ephemeral=True,
            )
            # Delete the plan approval message from the channel
            if self._plan_message is not None:
                try:
                    await self._plan_message.delete()
                except Exception:
                    pass


class PlanApprovalView(discord.ui.View):
    """Action buttons attached to plan-awaiting-approval notifications.

    Provides Approve, Request Changes, and Delete Plan buttons for tasks
    in AWAITING_PLAN_APPROVAL status.
    """

    def __init__(self, task_id: str, handler=None) -> None:
        super().__init__(timeout=86400 * 7)  # 7 days
        self.task_id = task_id
        self._handler = handler

    @discord.ui.button(
        label="Approve Plan",
        style=discord.ButtonStyle.success,
        emoji="✅",
    )
    async def approve_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        # Immediately update the message to remove buttons and show processing
        # state — this makes the interaction feel responsive even if the
        # backend work takes a few seconds.
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed is not None:
            embed.title = "⏳ Approving Plan..."
            embed.color = 0xF1C40F  # yellow/pending
        try:
            await interaction.message.edit(embed=embed, view=None)
        except Exception:
            pass  # Best-effort; the final update below will fix it

        result = await self._handler.execute("approve_plan", {"task_id": self.task_id})
        if "error" in result:
            await interaction.followup.send(
                f"Could not approve plan: {result['error']}", ephemeral=True
            )
            # Restore the buttons on error so user can retry
            if embed is not None:
                embed.title = "📋 Plan Awaiting Approval"
                embed.color = 0x3498DB  # blue
            try:
                await interaction.message.edit(embed=embed, view=self)
            except Exception:
                pass
        else:
            count = result.get("subtask_count", 0)
            await interaction.followup.send(
                f"✅ Plan approved for `{self.task_id}`. {count} subtask(s) created.",
                ephemeral=True,
            )
            # Final update with the actual subtask count
            if embed is not None:
                embed.title = f"✅ Plan Approved — {count} Subtask(s) Created"
                embed.color = 0x2ECC71  # green
            try:
                await interaction.message.edit(embed=embed, view=None)
            except Exception:
                pass  # Already removed buttons in the immediate update

    @discord.ui.button(
        label="Request Changes",
        style=discord.ButtonStyle.primary,
        emoji="✏️",
    )
    async def request_changes_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        modal = PlanChangesModal(
            self.task_id,
            handler=self._handler,
            plan_message=interaction.message,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Delete Plan",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
    )
    async def delete_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("delete_plan", {"task_id": self.task_id})
        if "error" in result:
            await interaction.followup.send(
                f"Could not delete plan: {result['error']}", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"🗑️ Plan deleted for `{self.task_id}`. Task completed without subtasks.",
                ephemeral=True,
            )
            # Delete the plan approval message from the channel
            try:
                await interaction.message.delete()
            except Exception:
                # Fallback: disable buttons if deletion fails
                for child in self.children:
                    child.disabled = True
                await interaction.message.edit(view=self)


def format_plan_approval_embed(
    task: Task,
    steps_json: str = "[]",
    raw_content: str = "",
    plan_url: str = "",
    parsed_steps: list[dict] | None = None,
    thread_url: str = "",
) -> discord.Embed:
    """Rich embed showing a plan awaiting user approval.

    Shows a high-level summary with links to the full plan — either a
    Discord thread jump URL (preferred, since the agent's final messages
    contain the complete plan summary) or a browser link to the health
    server's rendered plan page.
    """
    # --- Build description: summary + links ---
    desc_lines = [
        f"Task `{task.id}` generated a **{len(parsed_steps or [])}-phase implementation plan**.",
    ]

    # Extract a one-line summary from the plan title (first # heading)
    if raw_content:
        import re as _re

        title_match = _re.match(r"^#\s+(.+)$", raw_content.strip(), _re.MULTILINE)
        if title_match:
            desc_lines.append(f"> {title_match.group(1).strip()}")

    desc_lines.append("")

    # Link to the thread where the agent posted the full plan summary
    if thread_url:
        desc_lines.append(f"\U0001f4ac [**View Plan Summary in Thread**]({thread_url})")
    if plan_url:
        desc_lines.append(f"\U0001f4c4 [**View Full Plan**]({plan_url})")
    if thread_url or plan_url:
        desc_lines.append("")

    description = "\n".join(desc_lines)

    fields: list[tuple[str, str, bool]] = [
        ("Task", f"`{task.id}`\n{truncate(task.title, 80)}", True),
        ("Project", f"`{task.project_id}`", True),
    ]

    # --- List the tasks that will be generated ---
    if parsed_steps:
        task_list_lines = []
        for i, step in enumerate(parsed_steps, 1):
            title = step.get("title", "Untitled")
            # Truncate long titles
            if len(title) > 80:
                title = title[:77] + "..."
            task_list_lines.append(f"`{i}.` {title}")

        task_list = "\n".join(task_list_lines)
        fields.append(
            (
                f"─── Subtasks ({len(parsed_steps)}) ───",
                truncate(task_list, LIMIT_FIELD_VALUE),
                False,
            )
        )
    elif not thread_url and raw_content:
        # Fallback: no thread URL and no parsed steps — show a brief preview
        preview = raw_content.strip()
        if len(preview) > 400:
            cut = preview[:400].rfind("\n")
            if cut > 200:
                preview = preview[:cut] + "\n..."
            else:
                preview = preview[:400] + "..."
        fields.append(("Preview", truncate(f"```md\n{preview}\n```", LIMIT_FIELD_VALUE), False))

    # Link to full plan in browser (only if not already in description)
    if plan_url and not parsed_steps and not thread_url:
        fields.append(("Full Plan", f"[View in browser]({plan_url})", False))

    _PLAN_APPROVAL_COLOR = 0xF39C12  # amber/orange to indicate "needs attention"

    embed = make_embed(
        EmbedStyle.INFO,
        "Plan Awaiting Approval",
        description=truncate(description, LIMIT_DESCRIPTION),
        fields=fields,
        color_override=_PLAN_APPROVAL_COLOR,
    )

    return embed


# ---------------------------------------------------------------------------
# Playbook human-in-the-loop notifications (roadmap 5.4.2)
# ---------------------------------------------------------------------------


def format_playbook_paused(
    *,
    playbook_id: str,
    run_id: str,
    node_id: str,
) -> str:
    """Plain-text fallback for a playbook pausing at a wait_for_human node."""
    return (
        f"⏸️ **Playbook paused for human review:** `{playbook_id}` "
        f"(run `{run_id}`) at node `{node_id}`\n"
        f"Use `/resume-playbook {run_id}` to provide your input."
    )


def format_playbook_paused_embed(
    *,
    playbook_id: str,
    run_id: str,
    node_id: str,
    last_response: str = "",
    running_seconds: float = 0.0,
    tokens_used: int = 0,
) -> "discord.Embed":
    """Rich embed for a playbook paused at a ``wait_for_human`` node.

    Displays the accumulated context summary (the last assistant response)
    so the human reviewer can understand what the playbook has done and
    make an informed decision without having to look up additional details.

    See ``docs/specs/design/playbooks.md`` Section 9 — Human-in-the-Loop.
    """
    # --- Description: context summary ---
    desc_lines = [
        f"Playbook `{playbook_id}` has paused at node `{node_id}` and is awaiting human review.",
        "",
    ]

    if last_response:
        # Show the context summary (the last assistant message)
        context_preview = last_response
        if len(context_preview) > 1800:
            # Truncate at a newline boundary for readability
            cut = context_preview[:1800].rfind("\n")
            if cut > 600:
                context_preview = context_preview[:cut] + "\n…"
            else:
                context_preview = context_preview[:1800] + "…"
        desc_lines.append("**Context Summary:**")
        desc_lines.append(f"```\n{context_preview}\n```")
    else:
        desc_lines.append("_No context summary available._")

    description = "\n".join(desc_lines)

    # --- Fields ---
    fields: list[tuple[str, str, bool]] = [
        ("Playbook", f"`{playbook_id}`", True),
        ("Run ID", f"`{run_id}`", True),
        ("Paused at Node", f"`{node_id}`", True),
    ]

    if running_seconds > 0:
        if running_seconds >= 60:
            mins = int(running_seconds // 60)
            secs = int(running_seconds % 60)
            duration_str = f"{mins}m {secs}s"
        else:
            duration_str = f"{running_seconds:.1f}s"
        fields.append(("Running Time", duration_str, True))

    if tokens_used > 0:
        fields.append(("Tokens Used", f"{tokens_used:,}", True))

    fields.append(
        (
            "Resume",
            f"Use `/resume-playbook {run_id}` or click the button below.",
            False,
        )
    )

    _PAUSED_COLOR = 0x9B59B6  # purple — stands out as "needs human attention"

    embed = make_embed(
        EmbedStyle.WARNING,
        "⏸️ Playbook Awaiting Human Review",
        description=truncate(description, LIMIT_DESCRIPTION),
        fields=fields,
        color_override=_PAUSED_COLOR,
    )

    return embed


def format_playbook_timed_out(
    *,
    playbook_id: str,
    run_id: str,
    node_id: str,
    transitioned_to: str | None = None,
) -> str:
    """Plain-text message for a playbook pause timeout (roadmap 5.4.7 case f)."""
    if transitioned_to:
        return (
            f"⏰ **Playbook Timeout** — `{playbook_id}` "
            f"(run `{run_id}`) timed out at node `{node_id}` "
            f"and transitioned to `{transitioned_to}`."
        )
    return (
        f"⏰ **Playbook Timeout** — `{playbook_id}` (run `{run_id}`) timed out at node `{node_id}`."
    )


def format_playbook_timed_out_embed(
    *,
    playbook_id: str,
    run_id: str,
    node_id: str,
    timeout_seconds: int = 0,
    waited_seconds: float = 0.0,
    tokens_used: int = 0,
    transitioned_to: str | None = None,
) -> "discord.Embed":
    """Rich embed for a playbook pause timeout notification.

    Mirrors :func:`format_playbook_paused_embed` and routes to the same
    channel so the human reviewer sees timeout context alongside the
    original pause notification (roadmap 5.4.7 case f).
    """
    if transitioned_to:
        description = (
            f"Playbook `{playbook_id}` timed out at node `{node_id}` "
            f"and execution has continued at node `{transitioned_to}`."
        )
    else:
        description = (
            f"Playbook `{playbook_id}` timed out at node `{node_id}`. "
            f"The run has been marked as **timed_out**."
        )

    fields: list[tuple[str, str, bool]] = [
        ("Playbook", f"`{playbook_id}`", True),
        ("Run ID", f"`{run_id}`", True),
        ("Timed Out at Node", f"`{node_id}`", True),
    ]

    if timeout_seconds > 0:
        if timeout_seconds >= 3600:
            hours = timeout_seconds / 3600
            timeout_str = f"{hours:.1f}h"
        elif timeout_seconds >= 60:
            mins = timeout_seconds // 60
            timeout_str = f"{mins}m"
        else:
            timeout_str = f"{timeout_seconds}s"
        fields.append(("Timeout", timeout_str, True))

    if waited_seconds > 0:
        if waited_seconds >= 3600:
            waited_str = f"{waited_seconds / 3600:.1f}h"
        elif waited_seconds >= 60:
            waited_str = f"{int(waited_seconds // 60)}m {int(waited_seconds % 60)}s"
        else:
            waited_str = f"{waited_seconds:.1f}s"
        fields.append(("Waited", waited_str, True))

    if tokens_used > 0:
        fields.append(("Tokens Used", f"{tokens_used:,}", True))

    if transitioned_to:
        fields.append(("Transitioned To", f"`{transitioned_to}`", False))

    _TIMEOUT_COLOR = 0xE67E22  # orange — attention, but not as urgent as red

    embed = make_embed(
        EmbedStyle.WARNING,
        "⏰ Playbook Pause Timeout",
        description=truncate(description, LIMIT_DESCRIPTION),
        fields=fields,
        color_override=_TIMEOUT_COLOR,
    )

    return embed


# ---------------------------------------------------------------------------
# Playbook run lifecycle (start / complete / fail) — routed per scope
# ---------------------------------------------------------------------------


def format_playbook_started(*, playbook_id: str, run_id: str) -> str:
    """Plain-text line for a playbook run start."""
    return f"▶️ **Playbook Started** — `{playbook_id}` (run `{run_id}`)"


def format_playbook_started_embed(
    *,
    playbook_id: str,
    run_id: str,
    trigger_event_type: str = "",
    scope: str = "system",
) -> "discord.Embed":
    """Rich embed for a playbook run start."""
    fields: list[tuple[str, str, bool]] = [
        ("Playbook", f"`{playbook_id}`", True),
        ("Run ID", f"`{run_id}`", True),
        ("Scope", f"`{scope}`", True),
    ]
    if trigger_event_type:
        fields.append(("Trigger", f"`{trigger_event_type}`", False))

    return make_embed(
        EmbedStyle.INFO,
        "▶️ Playbook Started",
        description=f"Playbook `{playbook_id}` is now running.",
        fields=fields,
    )


def format_playbook_completed(
    *, playbook_id: str, run_id: str, duration_seconds: float = 0.0
) -> str:
    """Plain-text line for a playbook run completion."""
    if duration_seconds > 0:
        return (
            f"✅ **Playbook Completed** — `{playbook_id}` "
            f"(run `{run_id}`) in {_fmt_duration(duration_seconds)}"
        )
    return f"✅ **Playbook Completed** — `{playbook_id}` (run `{run_id}`)"


def format_playbook_completed_embed(
    *,
    playbook_id: str,
    run_id: str,
    duration_seconds: float = 0.0,
    tokens_used: int = 0,
    node_count: int = 0,
    final_context: str | None = None,
) -> "discord.Embed":
    """Rich embed for a playbook run completion."""
    fields: list[tuple[str, str, bool]] = [
        ("Playbook", f"`{playbook_id}`", True),
        ("Run ID", f"`{run_id}`", True),
    ]
    if duration_seconds > 0:
        fields.append(("Duration", _fmt_duration(duration_seconds), True))
    if node_count > 0:
        fields.append(("Nodes", str(node_count), True))
    if tokens_used > 0:
        fields.append(("Tokens", f"{tokens_used:,}", True))

    description = f"Playbook `{playbook_id}` completed successfully."
    if final_context:
        snippet = truncate(final_context, 500)
        description = f"{description}\n\n{snippet}"

    return make_embed(
        EmbedStyle.SUCCESS,
        "✅ Playbook Completed",
        description=truncate(description, LIMIT_DESCRIPTION),
        fields=fields,
    )


def format_playbook_run_failed(
    *, playbook_id: str, run_id: str, failed_at_node: str
) -> str:
    """Plain-text line for a playbook run failure."""
    return (
        f"❌ **Playbook Failed** — `{playbook_id}` (run `{run_id}`) "
        f"at node `{failed_at_node}`"
    )


def format_playbook_run_failed_embed(
    *,
    playbook_id: str,
    run_id: str,
    failed_at_node: str,
    error: str = "",
    duration_seconds: float = 0.0,
    tokens_used: int = 0,
) -> "discord.Embed":
    """Rich embed for a playbook run failure."""
    fields: list[tuple[str, str, bool]] = [
        ("Playbook", f"`{playbook_id}`", True),
        ("Run ID", f"`{run_id}`", True),
        ("Failed at Node", f"`{failed_at_node}`", True),
    ]
    if duration_seconds > 0:
        fields.append(("Duration", _fmt_duration(duration_seconds), True))
    if tokens_used > 0:
        fields.append(("Tokens", f"{tokens_used:,}", True))

    description = f"Playbook `{playbook_id}` failed at node `{failed_at_node}`."
    if error:
        description = f"{description}\n\n```\n{truncate(error, 1500)}\n```"

    return make_embed(
        EmbedStyle.ERROR,
        "❌ Playbook Failed",
        description=truncate(description, LIMIT_DESCRIPTION),
        fields=fields,
    )


def _fmt_duration(seconds: float) -> str:
    """Human-friendly duration formatter shared by the lifecycle embeds."""
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{seconds:.1f}s"


class PlaybookResumeModal(discord.ui.Modal, title="Resume Playbook"):
    """Modal dialog for providing human input to resume a paused playbook run.

    Opens when the user clicks the "Resume" button on a playbook-paused
    notification.  On submit, calls ``CommandHandler.execute("resume_playbook", ...)``
    to transition the run from PAUSED → RUNNING with the human's decision.
    """

    human_input = discord.ui.TextInput(
        label="Your decision / input",
        style=discord.TextStyle.long,
        placeholder="Provide your review decision or instructions for the playbook…",
        required=True,
        max_length=2000,
    )

    def __init__(self, run_id: str, handler=None) -> None:
        super().__init__()
        self.run_id = run_id
        self._handler = handler

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute(
            "resume_playbook",
            {"run_id": self.run_id, "human_input": self.human_input.value},
        )
        if "error" in result:
            await interaction.followup.send(
                f"Could not resume playbook: {result['error']}", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"▶️ Playbook run `{self.run_id}` resumed with your input.",
                ephemeral=True,
            )


class PlaybookResumeView(discord.ui.View):
    """Action buttons attached to playbook-paused notifications.

    Provides a "Resume" button that opens a modal for the human to enter
    their review decision, and a "List Runs" informational hint.
    """

    def __init__(self, run_id: str, handler=None) -> None:
        super().__init__(timeout=86400)  # 24 hours (matches pause timeout)
        self.run_id = run_id
        self._handler = handler
        self._message: discord.Message | None = None

    def set_message(self, message: discord.Message | None) -> None:
        """Store the Discord message this view is attached to.

        Called by the notification handler after ``_send_message`` returns
        so the view can later edit the message (e.g. disable buttons after
        the run resumes).
        """
        self._message = message

    @discord.ui.button(
        label="Resume Playbook",
        style=discord.ButtonStyle.success,
        emoji="▶️",
    )
    async def resume_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        modal = PlaybookResumeModal(self.run_id, handler=self._handler)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="List Paused Runs",
        style=discord.ButtonStyle.secondary,
        emoji="📋",
    )
    async def list_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._handler:
            await interaction.response.send_message("Handler not available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute(
            "list_playbook_runs", {"status": "paused", "limit": 10}
        )
        if "error" in result:
            await interaction.followup.send(
                f"Could not list runs: {result['error']}",
                ephemeral=True,
            )
        else:
            runs = result.get("runs", [])
            if not runs:
                await interaction.followup.send("No paused playbook runs found.", ephemeral=True)
            else:
                lines = [f"**Paused Playbook Runs** ({len(runs)}):"]
                for r in runs:
                    lines.append(
                        f"• `{r.get('run_id', '?')}` — "
                        f"{r.get('playbook_id', '?')} at `{r.get('current_node', '?')}`"
                    )
                await interaction.followup.send("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------
# Chat Analyzer suggestion notifications
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
