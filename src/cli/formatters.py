"""Rich formatters for tasks, agents, hooks, and system status.

Each formatter returns Rich renderables (Table, Panel, Group, etc.)
that the CLI command layer simply prints via ``console.print()``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from src.models import (
    Agent,
    Hook,
    HookRun,
    Project,
    ProjectStatus,
    Task,
    TaskStatus,
    Workspace,
)

from .styles import (
    AGENT_STATE_ICONS,
    AGENT_STATE_STYLES,
    STATUS_ICONS,
    STATUS_STYLES,
    TASK_TYPE_ICONS,
    priority_style,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _relative_time(ts: float | None) -> str:
    """Format a Unix timestamp as a human-readable relative time."""
    if not ts:
        return "—"
    delta = time.time() - ts
    if delta < 0:
        return "in the future"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def _truncate(text: str, max_len: int = 60) -> str:
    """Truncate text with ellipsis if too long."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _status_text(status: str) -> Text:
    """Create a styled Text object for a task status."""
    icon = STATUS_ICONS.get(status, "⚪")
    style = STATUS_STYLES.get(status, "white")
    return Text(f"{icon} {status}", style=style)


# ---------------------------------------------------------------------------
# Task formatters
# ---------------------------------------------------------------------------


def format_task_table(
    tasks: list[Task],
    title: str = "Tasks",
    show_project: bool = True,
) -> Table:
    """Format a list of tasks as a Rich table."""
    table = Table(
        title=title,
        title_style="bold bright_white",
        border_style="bright_black",
        show_lines=False,
        pad_edge=True,
        expand=True,
    )

    table.add_column("ID", style="bold bright_cyan", no_wrap=True, max_width=20)
    if show_project:
        table.add_column("Project", style="bold bright_magenta", max_width=16)
    table.add_column("Status", no_wrap=True, max_width=22)
    table.add_column("Pri", justify="right", max_width=5)
    table.add_column("Type", max_width=6)
    table.add_column("Title", ratio=1)
    table.add_column("Agent", style="dim", max_width=14)

    for task in tasks:
        type_icon = ""
        if task.task_type:
            type_icon = TASK_TYPE_ICONS.get(task.task_type.value, "")

        pri_text = Text(str(task.priority), style=priority_style(task.priority))

        row = [task.id]
        if show_project:
            row.append(task.project_id)
        row.extend([
            _status_text(task.status.value),
            pri_text,
            type_icon,
            _truncate(task.title, 50),
            task.assigned_agent_id or "—",
        ])
        table.add_row(*row)

    if not tasks:
        cols = 7 if show_project else 6
        table.add_row(*["" for _ in range(cols)])

    return table


def format_task_detail(
    task: Task,
    deps_on: list[str] | None = None,
    dependents: list[str] | None = None,
    subtask_stats: tuple[int, int] | None = None,
) -> Panel:
    """Format a single task as a detailed Rich panel."""
    status_icon = STATUS_ICONS.get(task.status.value, "⚪")
    status_style = STATUS_STYLES.get(task.status.value, "white")

    # Build content sections
    lines: list[str | Text] = []

    # Header line
    lines.append(Text(f"{status_icon} {task.status.value}", style=status_style))
    lines.append("")

    # Core fields
    fields = [
        ("Project", task.project_id),
        ("Priority", str(task.priority)),
        ("Type", task.task_type.value if task.task_type else "—"),
        ("Agent", task.assigned_agent_id or "—"),
        ("Branch", task.branch_name or "—"),
        ("Approval", "Required" if task.requires_approval else "No"),
    ]

    if task.pr_url:
        fields.append(("PR", task.pr_url))
    if task.parent_task_id:
        fields.append(("Parent", task.parent_task_id))

    for label, value in fields:
        line = Text()
        line.append(f"  {label}: ", style="bold cyan")
        line.append(value, style="white")
        lines.append(line)

    # Dependencies
    if deps_on:
        lines.append("")
        dep_line = Text()
        dep_line.append("  Depends on: ", style="bold cyan")
        dep_line.append(", ".join(deps_on), style="bright_cyan")
        lines.append(dep_line)

    if dependents:
        dep_line = Text()
        dep_line.append("  Blocks: ", style="bold cyan")
        dep_line.append(", ".join(dependents), style="bright_yellow")
        lines.append(dep_line)

    # Subtask progress bar
    if subtask_stats and subtask_stats[1] > 0:
        completed, total = subtask_stats
        lines.append("")
        prog_line = Text()
        prog_line.append(f"  Subtasks: ", style="bold cyan")
        prog_line.append(f"{completed}/{total} completed", style="white")
        lines.append(prog_line)

    # Description
    lines.append("")
    lines.append(Text("  Description:", style="bold cyan"))
    desc = task.description or "(no description)"
    for desc_line in desc.split("\n"):
        lines.append(Text(f"    {desc_line}", style="white"))

    content = Group(*lines)

    type_tag = ""
    if task.task_type:
        type_tag = f" {TASK_TYPE_ICONS.get(task.task_type.value, '')} {task.task_type.value}"

    return Panel(
        content,
        title=f"[bold bright_white]{task.title}[/] [dim]({task.id}){type_tag}[/]",
        border_style=status_style,
        padding=(1, 2),
    )


# ---------------------------------------------------------------------------
# Agent formatters
# ---------------------------------------------------------------------------


def format_agent_table(agents: list[Agent]) -> Table:
    """Format a list of agents as a Rich table."""
    table = Table(
        title="Agents",
        title_style="bold bright_white",
        border_style="bright_black",
        expand=True,
    )

    table.add_column("ID", style="bold bright_cyan", no_wrap=True)
    table.add_column("Name", style="white")
    table.add_column("Type", style="dim")
    table.add_column("State", no_wrap=True)
    table.add_column("Current Task", style="bright_cyan")
    table.add_column("Heartbeat", style="dim")
    table.add_column("Tokens", justify="right", style="dim")

    for agent in agents:
        state_icon = AGENT_STATE_ICONS.get(agent.state.value, "❓")
        state_style = AGENT_STATE_STYLES.get(agent.state.value, "white")
        state_text = Text(f"{state_icon} {agent.state.value}", style=state_style)

        tokens = f"{agent.session_tokens_used:,}" if agent.session_tokens_used else "—"

        table.add_row(
            agent.id,
            agent.name,
            agent.agent_type,
            state_text,
            agent.current_task_id or "—",
            _relative_time(agent.last_heartbeat),
            tokens,
        )

    return table


# ---------------------------------------------------------------------------
# Hook formatters
# ---------------------------------------------------------------------------


def format_hook_table(hooks: list[Hook]) -> Table:
    """Format hooks as a Rich table."""
    table = Table(
        title="Hooks",
        title_style="bold bright_white",
        border_style="bright_black",
        expand=True,
    )

    table.add_column("ID", style="bold bright_cyan", no_wrap=True, max_width=16)
    table.add_column("Name", style="white")
    table.add_column("Project", style="bold bright_magenta", max_width=16)
    table.add_column("Enabled", justify="center")
    table.add_column("Trigger", style="dim")
    table.add_column("Last Fired", style="dim")
    table.add_column("Cooldown", justify="right", style="dim")

    for hook in hooks:
        enabled = Text("✅", style="green") if hook.enabled else Text("❌", style="red")

        # Parse trigger for display
        try:
            trigger = json.loads(hook.trigger) if isinstance(hook.trigger, str) else hook.trigger
            trigger_type = trigger.get("type", "unknown") if isinstance(trigger, dict) else str(trigger)
        except (json.JSONDecodeError, TypeError):
            trigger_type = str(hook.trigger)[:20]

        cooldown = f"{hook.cooldown_seconds}s" if hook.cooldown_seconds else "—"

        table.add_row(
            hook.id[:16],
            hook.name,
            hook.project_id,
            enabled,
            trigger_type,
            _relative_time(hook.last_triggered_at),
            cooldown,
        )

    return table


def format_hook_run_table(runs: list[HookRun]) -> Table:
    """Format hook execution history."""
    table = Table(
        title="Hook Runs",
        title_style="bold bright_white",
        border_style="bright_black",
        expand=True,
    )

    table.add_column("ID", style="dim", no_wrap=True, max_width=12)
    table.add_column("Status", no_wrap=True)
    table.add_column("Trigger", style="dim")
    table.add_column("Tokens", justify="right")
    table.add_column("Started", style="dim")

    status_map = {
        "completed": ("✅", "green"),
        "failed": ("❌", "red"),
        "running": ("⚡", "yellow"),
        "skipped": ("⏭️", "dim"),
    }

    for run in runs:
        icon, style = status_map.get(run.status, ("❓", "white"))
        status_text = Text(f"{icon} {run.status}", style=style)

        table.add_row(
            run.id[:12],
            status_text,
            run.trigger_reason,
            f"{run.tokens_used:,}" if run.tokens_used else "—",
            _relative_time(run.started_at),
        )

    return table


# ---------------------------------------------------------------------------
# Project formatters
# ---------------------------------------------------------------------------


def format_project_table(projects: list[Project]) -> Table:
    """Format projects as a Rich table."""
    table = Table(
        title="Projects",
        title_style="bold bright_white",
        border_style="bright_black",
        expand=True,
    )

    table.add_column("ID", style="bold bright_magenta", no_wrap=True)
    table.add_column("Name", style="white")
    table.add_column("Status", no_wrap=True)
    table.add_column("Channel", style="cyan", no_wrap=True)
    table.add_column("Agents", justify="right")
    table.add_column("Tokens Used", justify="right", style="dim")

    for project in projects:
        status_style = "green" if project.status == ProjectStatus.ACTIVE else "dim"
        status_text = Text(project.status.value, style=status_style)

        table.add_row(
            project.id,
            project.name,
            status_text,
            project.discord_channel_id or "—",
            str(project.max_concurrent_agents),
            f"{project.total_tokens_used:,}" if project.total_tokens_used else "—",
        )

    return table


# ---------------------------------------------------------------------------
# System status formatter
# ---------------------------------------------------------------------------


def format_status_overview(
    projects: list[Project],
    agents: list[Agent],
    task_counts: dict[str, int],
) -> Panel:
    """Format a system status overview panel."""
    lines: list[str | Text] = []

    # Task summary
    total = sum(task_counts.values())
    active_statuses = {"READY", "ASSIGNED", "IN_PROGRESS", "WAITING_INPUT", "VERIFYING"}
    active = sum(v for k, v in task_counts.items() if k in active_statuses)
    completed = task_counts.get("COMPLETED", 0)
    failed = task_counts.get("FAILED", 0)

    lines.append(Text("📊 Task Summary", style="bold bright_white"))
    lines.append(Text(f"  Total: {total}  Active: {active}  Completed: {completed}  Failed: {failed}"))
    lines.append("")

    # Status breakdown
    if task_counts:
        for status, count in sorted(task_counts.items(), key=lambda x: -x[1]):
            if count == 0:
                continue
            icon = STATUS_ICONS.get(status, "⚪")
            style = STATUS_STYLES.get(status, "white")
            line = Text()
            line.append(f"  {icon} {status}: ", style=style)
            line.append(str(count))
            lines.append(line)
        lines.append("")

    # Agent summary
    busy_agents = sum(1 for a in agents if a.state.value == "BUSY")
    idle_agents = sum(1 for a in agents if a.state.value == "IDLE")
    lines.append(Text("🤖 Agents", style="bold bright_white"))
    lines.append(Text(f"  Total: {len(agents)}  Busy: {busy_agents}  Idle: {idle_agents}"))
    lines.append("")

    # Project summary
    active_projects = sum(1 for p in projects if p.status == ProjectStatus.ACTIVE)
    lines.append(Text("📁 Projects", style="bold bright_white"))
    lines.append(Text(f"  Total: {len(projects)}  Active: {active_projects}"))

    return Panel(
        Group(*lines),
        title="[bold bright_white]AgentQueue Status[/]",
        border_style="bright_blue",
        padding=(1, 2),
    )
