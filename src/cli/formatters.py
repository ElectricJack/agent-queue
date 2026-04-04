"""Rich formatters for tasks, agents, hooks, projects, and generic responses.

Each formatter returns Rich renderables (Table, Panel, Group, etc.)
that the CLI command layer simply prints via ``console.print()``.

Plugin-specific formatters live in their respective plugin modules
(src/plugins/internal/*.py).  This file contains formatters for
built-in CommandHandler commands and generic reusable formatters.
"""

from __future__ import annotations

import json
import time

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from typing import Any

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
    tasks: list[Any],
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
            type_icon = TASK_TYPE_ICONS.get(task.task_type, "")

        pri_text = Text(str(task.priority), style=priority_style(task.priority))

        row = [task.id]
        if show_project:
            row.append(task.project_id)
        row.extend(
            [
                _status_text(task.status),
                pri_text,
                type_icon,
                _truncate(task.title, 50),
                task.assigned_agent_id or "—",
            ]
        )
        table.add_row(*row)

    if not tasks:
        cols = 7 if show_project else 6
        table.add_row(*["" for _ in range(cols)])

    return table


def format_task_detail(
    task: Any,
    deps_on: list[str] | None = None,
    dependents: list[str] | None = None,
    subtask_stats: tuple[int, int] | None = None,
) -> Panel:
    """Format a single task as a detailed Rich panel."""
    status = task.status or ""
    status_icon = STATUS_ICONS.get(status, "⚪")
    status_style = STATUS_STYLES.get(status, "white")

    # Build content sections
    lines: list[str | Text] = []

    # Header line
    lines.append(Text(f"{status_icon} {status}", style=status_style))
    lines.append("")

    # Core fields
    fields = [
        ("Project", task.project_id),
        ("Priority", str(task.priority)),
        ("Type", task.task_type if task.task_type else "—"),
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
        prog_line.append("  Subtasks: ", style="bold cyan")
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
        type_tag = f" {TASK_TYPE_ICONS.get(task.task_type, '')} {task.task_type}"

    return Panel(
        content,
        title=f"[bold bright_white]{task.title}[/] [dim]({task.id}){type_tag}[/]",
        border_style=status_style,
        padding=(1, 2),
    )


# ---------------------------------------------------------------------------
# Agent formatters
# ---------------------------------------------------------------------------


def format_agent_table(agents: list[Any]) -> Table:
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
        state = (agent.state or "").upper()
        state_icon = AGENT_STATE_ICONS.get(state, "❓")
        state_style = AGENT_STATE_STYLES.get(state, "white")
        state_text = Text(f"{state_icon} {state}", style=state_style)

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


def format_hook_table(hooks: list[Any]) -> Table:
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
            trigger_type = (
                trigger.get("type", "unknown") if isinstance(trigger, dict) else str(trigger)
            )
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


def format_hook_run_table(runs: list[Any]) -> Table:
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


def format_project_table(projects: list[Any]) -> Table:
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
        status = (project.status or "").upper()
        status_style = "green" if status == "ACTIVE" else "dim"
        status_text = Text(status, style=status_style)

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
    projects: list[Any],
    agents: list[Any],
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
    lines.append(
        Text(f"  Total: {total}  Active: {active}  Completed: {completed}  Failed: {failed}")
    )
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
    busy_agents = sum(1 for a in agents if (a.state or "").upper() == "BUSY")
    idle_agents = sum(1 for a in agents if (a.state or "").upper() == "IDLE")
    lines.append(Text("🤖 Agents", style="bold bright_white"))
    lines.append(Text(f"  Total: {len(agents)}  Busy: {busy_agents}  Idle: {idle_agents}"))
    lines.append("")

    # Project summary
    active_projects = sum(1 for p in projects if (p.status or "").upper() == "ACTIVE")
    lines.append(Text("📁 Projects", style="bold bright_white"))
    lines.append(Text(f"  Total: {len(projects)}  Active: {active_projects}"))

    return Panel(
        Group(*lines),
        title="[bold bright_white]AgentQueue Status[/]",
        border_style="bright_blue",
        padding=(1, 2),
    )


# ---------------------------------------------------------------------------
# Generic formatters — reusable across many commands
# ---------------------------------------------------------------------------


def format_confirmation(data: dict) -> Text:
    """Format a status confirmation response (created/deleted/updated/etc.).

    Detects the action from common keys like 'created', 'deleted',
    'archived', 'updated', 'paused', 'resumed', etc.
    """
    text = Text()
    # Detect the action and primary ID
    action_keys = [
        "created", "deleted", "archived", "updated", "paused", "resumed",
        "restored", "skipped", "rejected", "approved", "reopened", "fired",
    ]
    action = None
    primary_id = None
    for key in action_keys:
        val = data.get(key)
        if val is not None:
            action = key
            primary_id = val
            break

    if action:
        text.append("✅ ", style="bold")
        text.append(f"{action.capitalize()}", style="bold green")
        if isinstance(primary_id, str):
            text.append(f" {primary_id}", style="bold bright_cyan")
    elif data.get("status"):
        text.append("✅ ", style="bold")
        text.append(data["status"], style="bold green")
    elif data.get("message"):
        text.append("✅ ", style="bold")
        text.append(data["message"], style="white")
    elif data.get("ok") or data.get("success"):
        text.append("✅ Done", style="bold green")

    # Show title if present
    title = data.get("title") or data.get("name")
    if title and title != primary_id:
        text.append(f" — {title}", style="white")

    # Show extra detail fields
    detail_keys = [
        "new_status", "old_status", "status", "fields", "message",
        "subtask_count", "unblocked_count", "draft_subtasks_deleted",
        "feedback_added", "archived_count", "warning", "note",
    ]
    for key in detail_keys:
        val = data.get(key)
        if val is not None and key not in ("status",) if action else True:
            if isinstance(val, bool):
                if val:
                    text.append(f"\n  {key}", style="dim")
            elif isinstance(val, list):
                text.append(f"\n  {key}: {len(val)} item(s)", style="dim")
            else:
                text.append(f"\n  {key}: {val}", style="dim")

    return text


def format_entity_detail(data: dict, title_key: str = "name") -> Panel:
    """Format a single entity as a key-value panel."""
    title = data.get(title_key) or data.get("id") or data.get("title") or ""
    lines: list[Text] = []
    skip = {"error"}
    for key, val in data.items():
        if key in skip or val is None:
            continue
        line = Text()
        line.append(f"  {key}: ", style="dim")
        if isinstance(val, (list, dict)):
            val_str = json.dumps(val, indent=2) if len(str(val)) > 80 else str(val)
            line.append(val_str, style="white")
        elif isinstance(val, bool):
            line.append("yes" if val else "no", style="green" if val else "red")
        else:
            line.append(str(val), style="white")
        lines.append(line)
    return Panel(
        Group(*lines) if lines else Text("  (empty)", style="dim"),
        title=f"[bold bright_white]{title}[/]",
        border_style="bright_cyan",
        padding=(0, 1),
    )


def format_text_content(data: dict) -> Panel:
    """Format a response that contains a text content field."""
    content = (
        data.get("content")
        or data.get("formatted")
        or data.get("rendered")
        or data.get("log")
        or data.get("diff")
        or data.get("output")
        or ""
    )
    title = data.get("name") or data.get("title") or data.get("task_id") or ""
    return Panel(
        Text(content),
        title=f"[bold bright_white]{title}[/]",
        border_style="bright_cyan",
        padding=(0, 1),
    )


def format_key_value(data: dict) -> Group:
    """Format a flat dict as aligned key-value pairs."""
    lines: list[Text] = []
    for key, val in data.items():
        if val is None:
            continue
        line = Text()
        line.append(f"  {key}: ", style="dim")
        if isinstance(val, bool):
            line.append("yes" if val else "no", style="green" if val else "red")
        elif isinstance(val, (int, float)):
            line.append(f"{val:,}", style="bright_cyan")
        elif isinstance(val, list):
            line.append(f"{len(val)} item(s)", style="white")
        else:
            line.append(_truncate(str(val), 100), style="white")
        lines.append(line)
    return Group(*lines)


# ---------------------------------------------------------------------------
# Task extra formatters
# ---------------------------------------------------------------------------


def format_task_tree(data: dict) -> Panel:
    """Format get_task_tree response."""
    formatted = data.get("formatted", "")
    progress = data.get("progress_bar", "")
    done = data.get("subtask_completed", 0)
    total = data.get("subtask_total", 0)
    header = Text()
    if progress:
        header.append(f"{progress}  ", style="white")
    header.append(f"{done}/{total} complete", style="dim")
    return Panel(
        Group(header, Text(formatted)),
        title="[bold bright_white]Task Tree[/]",
        border_style="bright_cyan",
        padding=(0, 1),
    )


def format_task_deps(data: dict) -> Group:
    """Format task_deps / get_task_dependencies response."""
    lines: list[Text] = []
    tid = data.get("task_id", "")
    title = data.get("title", "")
    status = data.get("status", "")
    header = Text()
    header.append(f"  {tid}", style="bold bright_cyan")
    if title:
        header.append(f" — {title}", style="white")
    if status:
        icon = STATUS_ICONS.get(status, "⚪")
        header.append(f"  {icon} {status}", style="dim")
    lines.append(header)

    deps = data.get("depends_on", [])
    if deps:
        lines.append(Text("  Depends on:", style="bold"))
        for d in deps:
            did = d.get("id", d) if isinstance(d, dict) else str(d)
            dtitle = d.get("title", "") if isinstance(d, dict) else ""
            dstatus = d.get("status", "") if isinstance(d, dict) else ""
            line = Text()
            line.append(f"    ← {did}", style="bright_cyan")
            if dtitle:
                line.append(f" {dtitle}", style="white")
            if dstatus:
                line.append(f"  ({dstatus})", style="dim")
            lines.append(line)

    blocks = data.get("blocks", [])
    if blocks:
        lines.append(Text("  Blocks:", style="bold"))
        for b in blocks:
            bid = b.get("id", b) if isinstance(b, dict) else str(b)
            btitle = b.get("title", "") if isinstance(b, dict) else ""
            bstatus = b.get("status", "") if isinstance(b, dict) else ""
            line = Text()
            line.append(f"    → {bid}", style="bright_cyan")
            if btitle:
                line.append(f" {btitle}", style="white")
            if bstatus:
                line.append(f"  ({bstatus})", style="dim")
            lines.append(line)

    if not deps and not blocks:
        lines.append(Text("  No dependencies.", style="dim"))
    return Group(*lines)


def format_archived_tasks(data: dict) -> Table:
    """Format list_archived response as a table."""
    tasks = data.get("tasks", [])
    table = Table(
        title=f"Archived Tasks ({data.get('count', len(tasks))} of {data.get('total', '?')})",
        title_style="bold bright_white",
        border_style="bright_black",
        expand=True,
    )
    table.add_column("ID", style="bold bright_cyan", no_wrap=True, max_width=20)
    table.add_column("Title", ratio=1)
    table.add_column("Status", style="dim")
    for t in tasks:
        if isinstance(t, dict):
            table.add_row(t.get("id", ""), _truncate(t.get("title", ""), 50), t.get("status", ""))
        else:
            table.add_row(str(t), "", "")
    return table


def format_chain_health(data: dict) -> Group:
    """Format get_chain_health response."""
    lines: list[Text] = []
    if "stuck_chains" in data:
        # Project-level
        total = data.get("total_stuck_chains", 0)
        header = Text()
        header.append(f"  {data.get('project_id', '')}", style="bold")
        header.append(f" — {total} stuck chain(s)", style="yellow" if total else "green")
        lines.append(header)
        for chain in data.get("stuck_chains", []):
            line = Text()
            line.append(f"    {chain.get('task_id', '')}", style="bright_cyan")
            line.append(f" {chain.get('title', '')}", style="white")
            lines.append(line)
    else:
        # Task-level
        header = Text()
        header.append(f"  {data.get('task_id', '')}", style="bold bright_cyan")
        if data.get("title"):
            header.append(f" — {data['title']}", style="white")
        header.append(f"  ({data.get('status', '')})", style="dim")
        lines.append(header)
        stuck = data.get("stuck_downstream", [])
        if stuck:
            lines.append(Text(f"  {len(stuck)} stuck downstream:", style="yellow"))
            for s in stuck:
                line = Text()
                line.append(f"    {s.get('id', '')}", style="bright_cyan")
                line.append(f" {s.get('title', '')}", style="white")
                lines.append(line)
        else:
            lines.append(Text("  No stuck tasks downstream.", style="green"))
    return Group(*lines)


# ---------------------------------------------------------------------------
# Profile formatters
# ---------------------------------------------------------------------------


def format_profile_list(data: dict) -> Table:
    """Format list_profiles response as a table."""
    profiles = data.get("profiles", [])
    table = Table(
        title=f"Agent Profiles ({data.get('count', len(profiles))})",
        title_style="bold bright_white",
        border_style="bright_black",
        expand=True,
    )
    table.add_column("ID", style="bold bright_cyan", no_wrap=True)
    table.add_column("Name", style="white", ratio=1)
    table.add_column("Model", style="dim")
    table.add_column("Tools", justify="right", style="dim")
    table.add_column("MCP", justify="right", style="dim")
    for p in profiles:
        tools = p.get("allowed_tools", [])
        mcp = p.get("mcp_servers", [])
        table.add_row(
            p.get("id", ""),
            p.get("name", ""),
            p.get("model", "—"),
            str(len(tools)),
            str(len(mcp)),
        )
    return table


def format_profile_detail(data: dict) -> Panel:
    """Format get_profile response as a panel."""
    lines: list[Text] = []
    for key in ("id", "name", "description", "model", "permission_mode"):
        val = data.get(key)
        if val:
            line = Text()
            line.append(f"  {key}: ", style="dim")
            line.append(str(val), style="white")
            lines.append(line)

    tools = data.get("allowed_tools", [])
    if tools:
        line = Text()
        line.append("  tools: ", style="dim")
        line.append(", ".join(tools), style="bright_cyan")
        lines.append(line)

    mcp = data.get("mcp_servers", {})
    if mcp:
        line = Text()
        line.append("  mcp_servers: ", style="dim")
        names = list(mcp.keys()) if isinstance(mcp, dict) else [str(s) for s in mcp]
        line.append(", ".join(names), style="bright_cyan")
        lines.append(line)

    suffix = data.get("system_prompt_suffix", "")
    if suffix:
        lines.append(Text(f"  system_prompt_suffix: {_truncate(suffix, 80)}", style="dim"))

    return Panel(
        Group(*lines) if lines else Text("  (empty profile)", style="dim"),
        title=f"[bold bright_white]{data.get('name', data.get('id', 'Profile'))}[/]",
        border_style="bright_cyan",
        padding=(0, 1),
    )


def format_available_tools(data: dict) -> Group:
    """Format list_available_tools response."""
    tools = data.get("tools", [])
    mcp = data.get("mcp_servers", [])
    parts: list[Any] = []

    if tools:
        table = Table(
            title="Tools", title_style="bold bright_white",
            border_style="bright_black", expand=True,
        )
        table.add_column("Name", style="bold bright_cyan")
        table.add_column("Description", style="white", ratio=1)
        for t in tools:
            table.add_row(t.get("name", ""), _truncate(t.get("description", ""), 60))
        parts.append(table)

    if mcp:
        mcp_table = Table(
            title="MCP Servers", title_style="bold bright_white",
            border_style="bright_black", expand=True,
        )
        mcp_table.add_column("Name", style="bold bright_cyan")
        mcp_table.add_column("Package", style="dim")
        mcp_table.add_column("Description", style="white", ratio=1)
        for s in mcp:
            mcp_table.add_row(
                s.get("name", ""), s.get("npm_package", ""),
                _truncate(s.get("description", ""), 50),
            )
        parts.append(mcp_table)

    return Group(*parts) if parts else Group(Text("  No tools available.", style="dim"))


# ---------------------------------------------------------------------------
# Hook/rule formatters
# ---------------------------------------------------------------------------


def format_rule_list(data: dict) -> Table:
    """Format browse_rules / list_rules response as a table."""
    rules = data.get("rules", [])
    table = Table(
        title="Rules", title_style="bold bright_white",
        border_style="bright_black", expand=True,
    )
    table.add_column("ID", style="bold bright_cyan", no_wrap=True, max_width=20)
    table.add_column("Name", style="white", ratio=1)
    table.add_column("Active", justify="center")
    table.add_column("Project", style="dim", max_width=16)
    for r in rules:
        active = "✅" if r.get("active", r.get("enabled", True)) else "❌"
        table.add_row(
            r.get("id", ""),
            _truncate(r.get("name", r.get("title", "")), 40),
            active,
            r.get("project_id", "—"),
        )
    return table


def format_schedule_list(data: dict) -> Table:
    """Format hook_schedules / list_scheduled response as a table."""
    hooks = data.get("hooks", data.get("scheduled", []))
    table = Table(
        title="Scheduled Hooks", title_style="bold bright_white",
        border_style="bright_black", expand=True,
    )
    table.add_column("ID", style="bold bright_cyan", no_wrap=True, max_width=16)
    table.add_column("Name", style="white", ratio=1)
    table.add_column("Schedule", style="dim")
    table.add_column("Next Run", style="dim")
    for h in hooks:
        table.add_row(
            h.get("hook_id", h.get("id", "")),
            h.get("name", ""),
            h.get("schedule", h.get("interval", "—")),
            h.get("next_run", h.get("fire_at", "—")),
        )
    return table


# ---------------------------------------------------------------------------
# System formatters
# ---------------------------------------------------------------------------


def format_event_list(data: dict) -> Table:
    """Format get_recent_events response as a table."""
    events = data.get("events", [])
    table = Table(
        title="Recent Events", title_style="bold bright_white",
        border_style="bright_black", expand=True,
    )
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Type", style="bold bright_cyan")
    table.add_column("Detail", style="white", ratio=1)
    for e in events:
        ts = e.get("timestamp", e.get("created_at"))
        time_str = _relative_time(ts) if isinstance(ts, (int, float)) else str(ts or "—")
        detail = e.get("message", e.get("detail", e.get("data", "")))
        if isinstance(detail, dict):
            detail = json.dumps(detail)
        table.add_row(time_str, e.get("type", e.get("event_type", "")), _truncate(str(detail), 60))
    return table


def format_token_usage(data: dict) -> Group:
    """Format get_token_usage response."""
    total = data.get("total", 0)
    breakdown = data.get("breakdown", [])
    lines: list[Text] = []
    header = Text()
    header.append("  Total tokens: ", style="dim")
    header.append(f"{total:,}", style="bold bright_cyan")
    lines.append(header)

    if breakdown:
        table = Table(border_style="bright_black", expand=True, show_header=True)
        # Detect columns from first entry
        sample = breakdown[0] if breakdown else {}
        for col in sample:
            style = "bold bright_cyan" if col in ("task_id", "project_id", "agent_id") else "white"
            table.add_column(col, style=style)
        for row in breakdown:
            table.add_row(*[
                f"{v:,}" if isinstance(v, (int, float)) else str(v or "")
                for v in row.values()
            ])
        return Group(header, table)
    return Group(*lines)


def format_prompt_list(data: dict) -> Table:
    """Format list_prompts response as a table."""
    prompts = data.get("prompts", [])
    table = Table(
        title=f"Prompts ({data.get('total', len(prompts))})",
        title_style="bold bright_white",
        border_style="bright_black",
        expand=True,
    )
    table.add_column("Name", style="bold bright_cyan")
    table.add_column("Category", style="dim")
    table.add_column("Description", style="white", ratio=1)
    for p in prompts:
        if isinstance(p, dict):
            table.add_row(
                p.get("name", ""),
                p.get("category", "—"),
                _truncate(p.get("description", ""), 50),
            )
        else:
            table.add_row(str(p), "", "")
    return table


def format_workspace_list(data: dict) -> Table:
    """Format list_workspaces response as a table."""
    workspaces = data.get("workspaces", [])
    table = Table(
        title="Workspaces", title_style="bold bright_white",
        border_style="bright_black", expand=True,
    )
    table.add_column("ID", style="bold bright_cyan", no_wrap=True, max_width=20)
    table.add_column("Name", style="white")
    table.add_column("Path", style="dim", ratio=1)
    table.add_column("Type", style="dim")
    table.add_column("Lock", style="dim")
    for ws in workspaces:
        lock = ""
        if ws.get("locked_by_task_id"):
            lock = f"🔒 {ws['locked_by_task_id']}"
        table.add_row(
            ws.get("id", ""),
            ws.get("name", "—"),
            ws.get("workspace_path", ""),
            ws.get("source_type", ""),
            lock,
        )
    return table


def format_active_tasks_all(data: dict) -> Group:
    """Format list_active_tasks_all_projects response."""
    by_project = data.get("by_project", {})
    total = data.get("total", 0)
    parts: list[Any] = []
    header = Text()
    header.append(f"  {total} active task(s)", style="bold")
    header.append(f" across {data.get('project_count', len(by_project))} project(s)", style="dim")
    parts.append(header)

    for proj_id, tasks in by_project.items():
        table = Table(
            title=proj_id, title_style="bold bright_magenta",
            border_style="bright_black", expand=True,
        )
        table.add_column("ID", style="bold bright_cyan", no_wrap=True, max_width=20)
        table.add_column("Status", max_width=18)
        table.add_column("Title", ratio=1)
        for t in tasks:
            status = t.get("status", "")
            icon = STATUS_ICONS.get(status, "⚪")
            table.add_row(t.get("id", ""), f"{icon} {status}", _truncate(t.get("title", ""), 40))
        parts.append(table)

    return Group(*parts)
