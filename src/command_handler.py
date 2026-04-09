"""Shared command handler for AgentQueue.

This module provides the single code path for all operational commands.
Both the Discord slash commands and the chat agent LLM tools delegate
their business logic here, keeping formatting and presentation separate.

This is the Command Pattern in action: every operation the system supports
(50+ commands) is routed through CommandHandler.execute(name, args).  The
two callers -- Discord slash commands and Supervisor LLM tool-use -- never
contain business logic themselves; they translate their inputs into a dict,
call execute(), and format the returned dict for their respective UIs.

The benefit is feature parity by construction.  A new command added here is
immediately available to both Discord and the chat agent without duplicating
any logic.

Related modules:

- ``src/tool_registry.py`` — JSON Schema definitions for every tool.
  Each tool's ``name`` maps to a ``_cmd_{name}`` method here.
- ``src/supervisor.py`` — The LLM tool-use loop that calls ``execute()``.
- ``src/discord/commands.py`` — Discord slash commands that call ``execute()``.

See ``specs/command-handler.md`` for the command reference specification.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

import logging

from src.config import AppConfig
from src.discord.embeds import STATUS_EMOJIS, progress_bar
from src.discord.notifications import classify_error
from src.models import (
    Project,
    ProjectStatus,
    RepoSourceType,
    Task,
    TaskStatus,
    TaskType,
    VerificationType,
    TASK_TYPE_VALUES,
    Workspace,
)
from src.orchestrator import Orchestrator
from src.logging_config import CorrelationContext
from src.state_machine import CyclicDependencyError, validate_dag_with_new_edge
from src.task_names import generate_task_id
from src.workspace_names import generate_workspace_id

logger = logging.getLogger(__name__)


async def _run_subprocess(
    *args: str,
    cwd: str | None = None,
    timeout: float = 30,
) -> tuple[int, str, str]:
    """Run a subprocess asynchronously.

    This is a coroutine.  Kills the process if *timeout* is exceeded.

    Args:
        *args: Command and arguments (e.g. ``"git", "status"``).
        cwd: Working directory for the subprocess.
        timeout: Maximum seconds to wait before killing the process.

    Returns:
        Tuple of ``(returncode, stdout, stderr)``.

    Raises:
        asyncio.TimeoutError: If the process exceeds *timeout*.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise
    return (
        proc.returncode,
        stdout_b.decode() if stdout_b else "",
        stderr_b.decode() if stderr_b else "",
    )


async def _run_subprocess_shell(
    command: str,
    *,
    cwd: str | None = None,
    timeout: float = 30,
) -> tuple[int, str, str]:
    """Run a shell command asynchronously via ``/bin/sh -c``.

    This is a coroutine.  Same semantics as ``_run_subprocess`` but
    accepts a single shell command string.

    Args:
        command: Shell command string.
        cwd: Working directory.
        timeout: Maximum seconds to wait.

    Returns:
        Tuple of ``(returncode, stdout, stderr)``.

    Raises:
        asyncio.TimeoutError: If the process exceeds *timeout*.
    """
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise
    return (
        proc.returncode,
        stdout_b.decode() if stdout_b else "",
        stderr_b.decode() if stderr_b else "",
    )


def _count_by(items, key_fn) -> dict[str, int]:
    """Count items by a key function, returning ``{key: count}``."""
    counts: dict[str, int] = {}
    for item in items:
        k = key_fn(item)
        counts[k] = counts.get(k, 0) + 1
    return counts


def _parse_delay(delay_str: str) -> int:
    """Parse a human-friendly delay string into seconds.

    Accepts formats like: '30s', '5m', '2h', '1d', '2h30m', '1d6h'.
    Plain integers are treated as seconds.
    """
    delay_str = delay_str.strip()
    # Plain integer → seconds
    if delay_str.isdigit():
        return int(delay_str)

    import re as _re

    total = 0
    pattern = _re.compile(r"(\d+)\s*([smhd])", _re.IGNORECASE)
    matches = pattern.findall(delay_str)
    if not matches:
        raise ValueError(
            f"Cannot parse delay '{delay_str}'. "
            "Use formats like '30s', '5m', '2h', '1d', or '2h30m'."
        )
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for value, unit in matches:
        total += int(value) * multipliers[unit.lower()]
    return total


def _format_interval(seconds: int) -> str:
    """Format an interval in seconds as a human-readable string (e.g. '2h 30m')."""
    if seconds <= 0:
        return "0s"
    parts: list[str] = []
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, secs = divmod(seconds, 60)
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs and not parts:
        parts.append(f"{secs}s")
    return " ".join(parts) if parts else "0s"


# ---------------------------------------------------------------------------
# Tree-view text formatting
# ---------------------------------------------------------------------------
# Unicode box-drawing characters for task tree rendering.  These match the
# constants in ``src/discord/embeds.py`` but are duplicated here so the
# command handler stays self-contained for formatting purposes.

_TREE_BRANCH = "├── "  # Non-last child connector
_TREE_LAST = "└── "  # Last child connector
_TREE_PIPE = "│   "  # Continuation pipe for deeper levels
_TREE_SPACE = "    "  # Blank continuation (last child's subtree)

# Discord messages cap at 2,000 characters.  We leave headroom for any
# surrounding text the caller might prepend/append (embed wrapper, header, etc).
_TREE_CHAR_BUDGET = 1800


def _status_emoji(status: TaskStatus) -> str:
    """Return the status emoji for a *TaskStatus* value.

    Falls back to ``⚪`` (white circle) for unknown statuses so the tree
    never breaks even if new statuses are added before the emoji map is
    updated.
    """
    return STATUS_EMOJIS.get(status.value, "⚪")


def _count_tree_stats(node: dict) -> tuple[int, int]:
    """Return ``(completed, total)`` counts for all descendants of *node*.

    *node* uses the ``{"task": Task, "children": [...]}`` shape produced by
    ``Database.get_task_tree()``.
    """
    total = 0
    completed = 0
    for child in node.get("children", []):
        total += 1
        if child["task"].status == TaskStatus.COMPLETED:
            completed += 1
        # Recurse into grandchildren
        sub_c, sub_t = _count_tree_stats(child)
        completed += sub_c
        total += sub_t
    return completed, total


def _collect_tree_task_ids(node: dict) -> list[str]:
    """Collect all task IDs from a tree hierarchy.

    *node* uses the ``{"task": Task, "children": [...]}`` shape produced
    by ``Database.get_task_tree()``.
    """
    ids = [node["task"].id]
    for child in node.get("children", []):
        ids.extend(_collect_tree_task_ids(child))
    return ids


def _tree_dep_annotation(task_id: str, dep_map: dict[str, dict] | None) -> str:
    """Build an inline dependency annotation for a tree node.

    Returns a string like ``" (← needs #X; blocks #Y)"`` when there
    are noteworthy dependency relationships, or the empty string when
    the task has no unmet upstream dependencies and blocks nothing.

    "needs" shows only **unmet** (non-COMPLETED) upstream dependencies
    so that fully-satisfied edges don't add noise.  "blocks" always
    lists downstream dependents regardless of their status.
    """
    if not dep_map or task_id not in dep_map:
        return ""
    info = dep_map[task_id]
    parts: list[str] = []

    # Unmet upstream dependencies (what this task still needs).
    depends_on: list[dict] = info.get("depends_on", [])
    unmet = [d for d in depends_on if d.get("status") != "COMPLETED"]
    if unmet:
        ids = ", ".join(f"#{d['id']}" for d in unmet)
        parts.append(f"needs {ids}")

    # Downstream dependents (what this task blocks).
    blocks: list[str] = info.get("blocks", [])
    if blocks:
        ids = ", ".join(f"#{bid}" for bid in blocks)
        parts.append(f"blocks {ids}")

    if not parts:
        return ""
    return f" (← {'; '.join(parts)})"


def _dep_annotation(task_id: str, dep_map: dict[str, dict] | None) -> str:
    """Build a concise dependency annotation suffix for a tree node.

    When *dep_map* is provided and contains entries for *task_id*, the
    function returns a parenthesised annotation like ``(← needs #abc)``
    or ``(← blocks #xyz)`` (or both).  Returns an empty string when
    there's nothing to annotate.

    Parameters
    ----------
    task_id:
        The ID of the task being rendered.
    dep_map:
        Mapping of ``task_id`` → ``{"depends_on": [...], "blocks": [...]}``
        as produced by :meth:`CommandHandler._build_dep_map`.  Each list
        element is a dict with at least an ``"id"`` key.  May be ``None``.
    """
    if not dep_map:
        return ""
    info = dep_map.get(task_id)
    if not info:
        return ""

    parts: list[str] = []

    depends_on = info.get("depends_on", [])
    if depends_on:
        ids = ", ".join(f"#{d['id']}" for d in depends_on)
        parts.append(f"needs {ids}")

    blocks = info.get("blocks", [])
    if blocks:
        ids = ", ".join(f"#{b['id']}" for b in blocks)
        parts.append(f"blocks {ids}")

    if not parts:
        return ""
    return " (← " + ", ".join(parts) + ")"


def _collect_tree_tasks(children: list[dict]) -> list["Task"]:
    """Recursively collect all :class:`Task` objects from tree children.

    Parameters
    ----------
    children:
        A list of ``{"task": Task, "children": [...]}`` dicts, matching
        the shape returned by ``Database.get_task_tree()``.

    Returns
    -------
    list[Task]
        Flat list of every Task in the subtree (does **not** include the
        root — the caller should prepend it if needed).
    """
    result: list[Task] = []
    for node in children:
        result.append(node["task"])
        result.extend(_collect_tree_tasks(node.get("children", [])))
    return result


def _count_subtree(children: list[dict]) -> tuple[int, int]:
    """Recursively count ``(completed, total)`` tasks in a tree node list.

    Parameters
    ----------
    children:
        A list of ``{"task": Task, "children": [...]}`` dicts, matching the
        shape returned by ``Database.get_task_tree()``.

    Returns
    -------
    tuple[int, int]
        ``(completed_count, total_count)`` across the entire subtree
        (excluding the root that *owns* these children).
    """
    completed = 0
    total = 0
    for node in children:
        task: Task = node["task"]
        total += 1
        if task.status == TaskStatus.COMPLETED:
            completed += 1
        child_completed, child_total = _count_subtree(node.get("children", []))
        completed += child_completed
        total += child_total
    return completed, total


def _count_subtree_by_status(children: list[dict]) -> dict[str, int]:
    """Recursively count tasks by status across a subtree.

    Parameters
    ----------
    children:
        A list of ``{"task": Task, "children": [...]}`` dicts, matching the
        shape returned by ``Database.get_task_tree()``.

    Returns
    -------
    dict[str, int]
        Mapping of ``TaskStatus.value`` → count for every status present
        in the subtree (excluding the root that *owns* these children).
    """
    counts: dict[str, int] = {}
    for node in children:
        task: Task = node["task"]
        status_val = task.status.value
        counts[status_val] = counts.get(status_val, 0) + 1
        child_counts = _count_subtree_by_status(node.get("children", []))
        for s, c in child_counts.items():
            counts[s] = counts.get(s, 0) + c
    return counts


def _format_status_summary(status_counts: dict[str, int], total: int) -> str:
    """Build a concise one-line summary of non-completed task status counts.

    Given a full status breakdown and total, produces a string like::

        2/5 subtasks complete · 1 in progress · 1 failed · 1 blocked

    Only non-zero, non-completed statuses are included in the suffix.

    Parameters
    ----------
    status_counts:
        Mapping of ``TaskStatus.value`` → count.
    total:
        Total number of subtasks.

    Returns
    -------
    str
        A formatted summary line (without leading indent).
    """
    completed = status_counts.get("COMPLETED", 0)
    base = f"{completed}/{total} subtasks complete"

    # Ordered list of (status_value, display_label) for non-completed statuses.
    # Order mirrors the visual priority: active work first, then needs-attention,
    # then queued/pending states.
    _NON_COMPLETED_LABELS: list[tuple[str, str]] = [
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

    parts: list[str] = []
    for status_val, label in _NON_COMPLETED_LABELS:
        count = status_counts.get(status_val, 0)
        if count > 0:
            parts.append(f"{count} {label}")

    if parts:
        return base + " · " + " · ".join(parts)
    return base


def _render_tree_node(
    task: Task,
    children: list[dict],
    *,
    depth: int,
    max_depth: int,
    prefix: str,
    is_last: bool,
    dep_map: dict[str, dict] | None = None,
) -> list[str]:
    """Render a single tree node and its descendants as lines of text.

    This is the recursive workhorse called by :func:`_format_task_tree`.
    It produces one line per visible task, using box-drawing characters to
    convey hierarchy.

    Parameters
    ----------
    task:
        The Task object for this node.
    children:
        Child tree nodes (same shape as ``get_task_tree`` output).
    depth:
        Current nesting depth (0 = root).
    max_depth:
        Maximum depth before collapsing remaining children.
    prefix:
        The box-drawing prefix inherited from the parent's formatting
        (e.g. ``"│   "`` or ``"    "``).
    is_last:
        Whether this node is the last sibling at its level.
    dep_map:
        Optional dependency mapping produced by
        :meth:`CommandHandler._build_dep_map`.  When provided, each node
        gets an inline annotation showing upstream/downstream dependencies
        (e.g. ``(← needs #abc, blocks #xyz)``).
    """
    lines: list[str] = []
    emoji = _status_emoji(task.status)
    dep_suffix = _dep_annotation(task.id, dep_map)

    # -- Format the current node's line --------------------------------------
    if depth == 0:
        # Root task: bold title + inline task id
        lines.append(f"{emoji} **{task.title}** `{task.id}`{dep_suffix}")
    else:
        connector = _TREE_LAST if is_last else _TREE_BRANCH
        lines.append(f"{prefix}{connector}{emoji} {task.title}{dep_suffix}")

    if not children:
        return lines

    # Prefix that this node's children will inherit
    if depth == 0:
        child_prefix = ""
    else:
        child_prefix = prefix + (_TREE_SPACE if is_last else _TREE_PIPE)

    # -- Depth limit: collapse the remaining subtree into a summary ----------
    if depth >= max_depth:
        completed, total = _count_subtree(children)
        noun = "subtask" if total == 1 else "subtasks"
        lines.append(f"{child_prefix}{_TREE_LAST}… ({total} more {noun}, {completed} complete)")
        return lines

    # -- Render each child recursively ---------------------------------------
    for i, child_node in enumerate(children):
        is_last_child = i == len(children) - 1
        child_lines = _render_tree_node(
            child_node["task"],
            child_node.get("children", []),
            depth=depth + 1,
            max_depth=max_depth,
            prefix=child_prefix,
            is_last=is_last_child,
            dep_map=dep_map,
        )
        lines.extend(child_lines)

    return lines


def _format_task_tree(
    root_task: Task,
    children: list[dict],
    *,
    depth: int = 0,
    max_depth: int = 4,
    compact: bool = False,
    dep_map: dict[str, dict] | None = None,
) -> str:
    """Format a task and its subtask tree as readable text with box-drawing chars.

    This is the single formatter for tree-view task display.  Both Discord
    slash commands and the chat-agent LLM tools call this to produce a
    consistent hierarchical rendering of parent/subtask relationships.

    Parameters
    ----------
    root_task:
        The root :class:`Task` object.
    children:
        List of ``{"task": Task, "children": [...]}`` dicts as returned by
        ``Database.get_task_tree()["children"]``.
    depth:
        Starting depth (normally ``0`` for a top-level call; pass a higher
        value when embedding this tree inside a larger view).
    max_depth:
        Maximum nesting depth to render before collapsing deeper levels
        into a ``… (N more subtasks)`` summary.
    compact:
        If ``True``, show only the root task header and a summary count
        line — no child tree at all.  Useful for dense list views.
    dep_map:
        Optional dependency mapping produced by
        :meth:`CommandHandler._build_dep_map`.  When provided, each tree
        node gets an inline annotation showing upstream/downstream
        dependencies (e.g. ``(← needs #abc, blocks #xyz)``).  Ignored
        in compact mode (too dense for annotations).

    Returns
    -------
    str
        A multi-line string suitable for Discord messages / embeds.
        Automatically truncated to ~1,800 characters to stay within
        Discord's 2,000-char message limit.

    Notes
    -----
    Truncation strategy:
        If the expanded tree exceeds ``_TREE_CHAR_BUDGET`` (~1,800 chars),
        the formatter progressively reduces ``max_depth`` until it fits.
        If even depth-1 is too long it falls back to compact mode.

    Examples
    --------
    Expanded::

        🟡 **Implement auth** `task-abc`
          2/5 subtasks complete
        ├── 🟢 Set up OAuth
        ├── 🟢 Create login page
        ├── 🟡 Add session management
        ├── ⚪ Write tests
        └── ⚪ Security review

    With dependency annotations (``dep_map`` provided)::

        🟡 **Implement auth** `task-abc`
          2/5 subtasks complete
        ├── 🟢 Set up OAuth
        ├── 🟢 Create login page
        ├── 🟡 Add session management (← blocks #task-xyz)
        ├── ⚪ Write tests (← needs #task-def)
        └── ⚪ Security review

    Compact::

        🟡 **Implement auth** `task-abc`
          2/5 subtasks complete
    """
    # -- Compute subtree statistics once (shared by all modes) ---------------
    if children:
        completed, total = _count_subtree(children)
        status_counts = _count_subtree_by_status(children)
        summary_line = f"  {_format_status_summary(status_counts, total)}"
    else:
        _, total = 0, 0
        status_counts = {}
        summary_line = None

    # -- Compact mode: root + summary only -----------------------------------
    # Dependency annotations are intentionally omitted in compact mode —
    # the format is too dense and the annotations would dominate the output.
    if compact:
        emoji = _status_emoji(root_task.status)
        lines = [f"{emoji} **{root_task.title}** `{root_task.id}`"]
        if summary_line:
            lines.append(summary_line)
        return "\n".join(lines)

    # -- Expanded mode: full tree with box-drawing characters ----------------
    def _build_expanded(effective_max_depth: int) -> str:
        tree_lines = _render_tree_node(
            root_task,
            children,
            depth=depth,
            max_depth=effective_max_depth,
            prefix="",
            is_last=True,
            dep_map=dep_map,
        )
        # Insert summary line right after the root header
        if summary_line:
            tree_lines.insert(1, summary_line)
        return "\n".join(tree_lines)

    result = _build_expanded(max_depth)

    # -- Truncation: progressively reduce depth, then fall back to compact ---
    if len(result) > _TREE_CHAR_BUDGET:
        for reduced_depth in range(max(max_depth - 1, 1), 0, -1):
            result = _build_expanded(reduced_depth)
            if len(result) <= _TREE_CHAR_BUDGET:
                return result

        # Even depth-1 is too long — fall back to compact mode
        return _format_task_tree(
            root_task,
            children,
            depth=depth,
            compact=True,
        )

    return result


# ---------------------------------------------------------------------------
# Dependency-aware text formatter
# ---------------------------------------------------------------------------

# Character budget for dependency display — mirrors _TREE_CHAR_BUDGET.
_DEP_MAX_CHARS = 1800


def _format_task_dep_line(entry: dict) -> str:
    """Format a single task entry with dependency annotations.

    Parameters
    ----------
    entry:
        A task dict produced by ``_cmd_list_tasks`` when
        ``show_dependencies=True``.  Expected keys: ``id``, ``title``,
        ``status``, and optionally ``depends_on`` (list of dicts with
        ``id`` and ``status``) and ``blocks`` (list of task IDs).

    Returns
    -------
    str
        One or more lines like::

            🔵 #12: Set up database [READY]
               ↳ depends on: #10 (COMPLETED ✅), #11 (IN_PROGRESS 🟡)

        If the task has no dependencies or dependents the sub-lines are
        omitted so that "clean" tasks stay compact.
    """
    status = entry.get("status", "DEFINED")
    emoji = STATUS_EMOJIS.get(status, "⚪")
    lines: list[str] = [f"{emoji} #{entry['id']}: {entry['title']} [{status}]"]

    # --- depends_on (upstream) -----------------------------------------------
    depends_on: list[dict] = entry.get("depends_on", [])
    if depends_on:
        dep_parts: list[str] = []
        for dep in depends_on:
            dep_status = dep.get("status", "DEFINED")
            dep_emoji = STATUS_EMOJIS.get(dep_status, "⚪")
            dep_parts.append(f"#{dep['id']} ({dep_status} {dep_emoji})")
        lines.append(f"   ↳ depends on: {', '.join(dep_parts)}")

    # --- blocks (downstream) -------------------------------------------------
    blocks: list[str] = entry.get("blocks", [])
    if blocks:
        block_ids = ", ".join(f"#{bid}" for bid in blocks)
        lines.append(f"   ↳ blocks: {block_ids}")

    return "\n".join(lines)


def format_dependency_list(task_list: list[dict]) -> str:
    """Format a full task list with dependency annotations.

    Iterates over *task_list* (as produced by ``_cmd_list_tasks`` with
    ``show_dependencies=True``) and returns a multi-line string where
    each task is annotated with its upstream ``depends_on`` and
    downstream ``blocks`` relationships.  Tasks with no dependency data
    are rendered as single lines without sub-annotations.

    The output is truncated to ``_DEP_MAX_CHARS`` to stay within
    Discord embed / message limits.

    Parameters
    ----------
    task_list:
        List of task entry dicts, each containing at minimum ``id``,
        ``title``, and ``status``.  When dependency data is present,
        ``depends_on`` and ``blocks`` keys are also expected.

    Returns
    -------
    str
        Formatted multi-line string ready for embed descriptions or
        plain-text messages.
    """
    if not task_list:
        return ""

    formatted_tasks: list[str] = []
    for entry in task_list:
        formatted_tasks.append(_format_task_dep_line(entry))

    result = "\n".join(formatted_tasks)

    # Truncation guard — drop tasks from the end and add a summary.
    if len(result) <= _DEP_MAX_CHARS:
        return result

    kept: list[str] = []
    used = 0
    # Reserve space for the "… (N more tasks)" indicator (~30 chars).
    budget = _DEP_MAX_CHARS - 30
    shown = 0
    for block in formatted_tasks:
        cost = len(block) + 1  # +1 for the joining newline
        if used + cost > budget:
            break
        kept.append(block)
        used += cost
        shown += 1

    remaining = len(formatted_tasks) - shown
    if remaining > 0:
        kept.append(f"… ({remaining} more task{'s' if remaining != 1 else ''})")

    return "\n".join(kept)


def _build_archive_note(
    task,
    result: dict | None,
    dependencies: set[str],
) -> str:
    """Build a markdown reference note for an archived task.

    Produces a self-contained document that preserves the task's full
    context for future reference -- everything needed to understand what
    was done, why, and how well it went.
    """
    lines: list[str] = []
    lines.append(f"# {task.title}")
    lines.append("")
    lines.append(f"**Task ID:** `{task.id}`")
    lines.append(f"**Project:** `{task.project_id}`")
    lines.append(f"**Status:** {task.status.value}")
    lines.append(f"**Priority:** {task.priority}")
    if task.task_type:
        lines.append(f"**Type:** {task.task_type.value}")
    if task.branch_name:
        lines.append(f"**Branch:** `{task.branch_name}`")
    if task.pr_url:
        lines.append(f"**PR:** {task.pr_url}")
    if task.parent_task_id:
        lines.append(f"**Parent Task:** `{task.parent_task_id}`")
    if task.is_plan_subtask:
        lines.append("**Plan Subtask:** Yes")
    if task.plan_source:
        lines.append(f"**Plan Source:** `{task.plan_source}`")
    if dependencies:
        dep_list = ", ".join(f"`{d}`" for d in sorted(dependencies))
        lines.append(f"**Dependencies:** {dep_list}")
    lines.append(f"**Archived:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # Description
    lines.append("## Description")
    lines.append("")
    lines.append(task.description)
    lines.append("")

    # Result details
    if result:
        lines.append("## Result")
        lines.append("")
        if result.get("summary"):
            lines.append(f"**Summary:** {result['summary']}")
            lines.append("")
        if result.get("files_changed"):
            lines.append("**Files Changed:**")
            for fc in result["files_changed"]:
                lines.append(f"- `{fc}`")
            lines.append("")
        if result.get("tokens_used"):
            lines.append(f"**Tokens Used:** {result['tokens_used']:,}")
            lines.append("")
        if result.get("error_message"):
            lines.append(f"**Error:** {result['error_message']}")
            lines.append("")
    else:
        lines.append("## Result")
        lines.append("")
        lines.append("_No execution result recorded._")
        lines.append("")

    return "\n".join(lines)


class CommandHandler:
    """Unified command execution layer for AgentQueue (Command Pattern).

    This is the single code path for every operation in the system.  Both
    the Discord slash commands and the Supervisor LLM tools call
    ``handler.execute(name, args)`` -- neither contains business logic.

    Convention for command methods:
        Each ``_cmd_*`` method receives a flat ``dict`` of arguments and
        returns a ``dict``.  On success the dict contains domain data
        (e.g. ``{"task": {...}}``).  On failure it contains
        ``{"error": "human-readable message"}``.  Callers never need to
        catch exceptions -- ``execute()`` wraps every call in a try/except.

    Active project context:
        ``_active_project_id`` lets callers set an implicit project scope
        so users chatting in a project's Discord channel don't have to
        pass ``project_id`` on every command.  Many ``_cmd_*`` methods
        fall back to this when no explicit project_id is provided.

    Security helpers:
        ``_validate_path`` sandboxes all file operations to the workspace
        directory or a registered repo source path -- the chat agent can
        never escape to arbitrary filesystem locations.

        ``_resolve_repo_path`` centralizes the surprisingly tricky logic
        for finding the right git checkout directory given a combination
        of project_id, workspace, and the active project fallback.
    """

    def __init__(self, orchestrator: Orchestrator, config: AppConfig):
        self.orchestrator = orchestrator
        self.config = config
        self._active_project_id: str | None = None
        # Optional callback invoked after a project is deleted.
        # Signature: callback(project_id: str) -> None
        # The Discord bot registers this to clean in-memory channel caches.
        self._on_project_deleted: Callable[[str], None] | None = None
        # Optional callback invoked after a note is written or appended.
        # Signature: async callback(project_id, note_filename, note_path) -> None
        # The Discord bot registers this to auto-refresh viewed notes.
        self.on_note_written: Callable | None = None
        # Conversation context set by the Supervisor during its chat loop.
        # When set, _cmd_create_task stores it as task_context so agents
        # have the same thread chain context that the supervisor had when
        # creating the task.
        self._current_conversation_context: str | None = None
        # When True, _cmd_create_task creates tasks as DEFINED instead of
        # READY.  Set by supervisor.break_plan_into_tasks() so plan subtasks
        # are born DEFINED, eliminating the race condition that required
        # project-wide plan processing locks.
        self._plan_subtask_creation_mode: bool = False

    @property
    def db(self):
        return self.orchestrator.db

    def set_active_project(self, project_id: str | None) -> None:
        self._active_project_id = project_id

    async def _validate_path(self, path: str) -> str | None:
        """Validate that a path resolves within an allowed directory.

        Allowed roots: workspace_dir, any registered repo source_path,
        and any registered workspace path.
        """
        real = os.path.realpath(path)
        workspace_real = os.path.realpath(self.config.workspace_dir)
        if real.startswith(workspace_real + os.sep) or real == workspace_real:
            return real
        repos = await self.db.list_repos()
        for repo in repos:
            if repo.source_path:
                repo_real = os.path.realpath(repo.source_path)
                if real.startswith(repo_real + os.sep) or real == repo_real:
                    return real
        # Also allow paths within any registered workspace
        workspaces = await self.db.list_workspaces()
        for ws in workspaces:
            ws_real = os.path.realpath(ws.workspace_path)
            if real.startswith(ws_real + os.sep) or real == ws_real:
                return real
        return None

    async def execute(self, name: str, args: dict) -> dict:
        """Execute a command by name and return a structured result dict.

        This is the single code path for all operational commands in the system.
        Both Discord slash commands and chat agent LLM tools call this method.
        """
        with CorrelationContext(command=name, component="command_handler"):
            try:
                handler = getattr(self, f"_cmd_{name}", None)
                if handler:
                    return await handler(args)

                # Fallback to plugin registry
                if (
                    hasattr(self.orchestrator, "plugin_registry")
                    and self.orchestrator.plugin_registry
                ):
                    plugin_handler = self.orchestrator.plugin_registry.get_command(name)
                    if plugin_handler:
                        plugin_name = name.split(".")[0] if "." in name else name
                        try:
                            with CorrelationContext(plugin=plugin_name):
                                result = await plugin_handler(args)
                            self.orchestrator.plugin_registry.record_success(plugin_name)
                            return result
                        except Exception as e:
                            await self.orchestrator.plugin_registry.record_failure(
                                plugin_name, str(e)
                            )
                            logger.error("Plugin command %s failed: %s", name, e, exc_info=True)
                            return {"error": f"Plugin command failed: {e}"}

                logger.warning("Unknown command requested: %s", name)
                return {"error": f"Unknown command: {name}"}
            except Exception as e:
                logger.error("Command %s failed: %s", name, e, exc_info=True)
                return {"error": str(e)}

    # -----------------------------------------------------------------------
    # System commands — config reload, status, diagnostics
    # -----------------------------------------------------------------------

    async def _cmd_reload_config(self, args: dict) -> dict:
        """Manually trigger a config hot-reload from disk.

        Returns a summary of which sections changed, which were applied,
        and which require a restart.
        """
        watcher = self.orchestrator._config_watcher
        if not watcher:
            return {"error": "Config watcher is not active (no config file path)"}
        result = await watcher.reload()
        if "error" in result:
            return {"error": f"Config reload failed: {result['error']}"}
        if not result.get("changed_sections"):
            return {"message": "No configuration changes detected."}
        parts = []
        if result.get("applied"):
            parts.append(f"Applied: {', '.join(result['applied'])}")
        if result.get("restart_required"):
            parts.append(f"Restart required: {', '.join(result['restart_required'])}")
        return {
            "message": "Config reloaded.",
            "changed_sections": result["changed_sections"],
            "applied": result.get("applied", []),
            "restart_required": result.get("restart_required", []),
            "summary": "; ".join(parts) if parts else "No changes.",
        }

    async def _cmd_claude_usage(self, args: dict) -> dict:
        """Get Claude Code usage stats from live session data.

        Computes real token usage by scanning active session JSONL files
        in ``~/.claude/projects/``.  Also reads subscription info from
        ``~/.claude/.credentials.json``.
        """
        import json as _json
        from pathlib import Path as _Path
        from datetime import datetime as _dt, timezone as _tz

        result: dict = {}

        claude_dir = _Path.home() / ".claude"
        sessions_dir = claude_dir / "sessions"
        projects_dir = claude_dir / "projects"

        # --- 1. Subscription info from credentials ---
        creds_path = claude_dir / ".credentials.json"
        if creds_path.exists():
            try:
                creds = _json.loads(creds_path.read_text())
                oauth = creds.get("claudeAiOauth", {})
                result["subscription"] = oauth.get("subscriptionType", "unknown")
                result["rate_limit_tier"] = oauth.get("rateLimitTier", "unknown")
            except Exception:
                pass

        # --- 2. Active sessions with live token usage from session JSONLs ---
        active_sessions: list[dict] = []
        if sessions_dir.exists():
            for sf in sessions_dir.iterdir():
                try:
                    sess = _json.loads(sf.read_text())
                except Exception:
                    continue
                pid = sess.get("pid")
                # Check if process is still alive
                if not pid or not os.path.exists(f"/proc/{pid}"):
                    continue
                sid = sess.get("sessionId", "")
                cwd = sess.get("cwd", "")
                started_ms = sess.get("startedAt", 0)
                started = _dt.fromtimestamp(started_ms / 1000, tz=_tz.utc) if started_ms else None

                # Scan for the session JSONL in projects/
                usage = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
                msg_count = 0
                jsonl_path = None
                for pd in projects_dir.iterdir():
                    candidate = pd / f"{sid}.jsonl"
                    if candidate.exists():
                        jsonl_path = candidate
                        break
                if jsonl_path:
                    try:
                        with open(jsonl_path) as fh:
                            for line in fh:
                                data = _json.loads(line)
                                msg = data.get("message", {})
                                if isinstance(msg, dict) and "usage" in msg:
                                    u = msg["usage"]
                                    usage["input"] += u.get("input_tokens", 0)
                                    usage["output"] += u.get("output_tokens", 0)
                                    usage["cache_read"] += u.get("cache_read_input_tokens", 0)
                                    usage["cache_create"] += u.get("cache_creation_input_tokens", 0)
                                    msg_count += 1
                    except Exception:
                        pass

                total = sum(usage.values())
                # Derive project name from cwd
                project_name = os.path.basename(cwd) if cwd else "unknown"
                active_sessions.append(
                    {
                        "session_id": sid[:12],
                        "project": project_name,
                        "cwd": cwd,
                        "started": started.strftime("%H:%M") if started else "?",
                        "messages": msg_count,
                        "usage": usage,
                        "total_tokens": total,
                    }
                )

        result["active_sessions"] = active_sessions
        result["active_session_count"] = len(active_sessions)
        result["active_total_tokens"] = sum(s["total_tokens"] for s in active_sessions)

        # --- 3. Aggregate model usage from stats-cache (cumulative) ---
        stats_path = claude_dir / "stats-cache.json"
        if stats_path.exists():
            try:
                stats = _json.loads(stats_path.read_text())
                result["total_sessions"] = stats.get("totalSessions", 0)
                result["total_messages"] = stats.get("totalMessages", 0)
                model_usage = {}
                for model, data in (stats.get("modelUsage") or {}).items():
                    short = model.replace("claude-", "").split("-202")[0]
                    inp = data.get("inputTokens", 0)
                    out = data.get("outputTokens", 0)
                    cache_read = data.get("cacheReadInputTokens", 0)
                    cache_create = data.get("cacheCreationInputTokens", 0)
                    model_usage[short] = {
                        "input": inp,
                        "output": out,
                        "cache_read": cache_read,
                        "cache_create": cache_create,
                        "total": inp + out + cache_read + cache_create,
                    }
                result["model_usage"] = model_usage
                result["stats_date"] = stats.get("lastComputedDate", "unknown")
            except Exception as e:
                result["stats_error"] = str(e)

        # --- 4. Probe rate-limit status via a minimal API call ---
        try:
            rate_limit = await self._probe_claude_rate_limit()
            result["rate_limit"] = rate_limit
        except Exception as e:
            result["rate_limit_error"] = str(e)

        return result

    async def _probe_claude_rate_limit(self) -> dict:
        """Send a minimal 1-token API request to read rate-limit headers.

        This replicates what the Claude Code CLI does internally to check
        quota status.  Uses the OAuth token from ``~/.claude/.credentials.json``
        or falls back to ``ANTHROPIC_API_KEY``.
        """
        import json as _json
        from pathlib import Path as _Path

        # Get auth token
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        auth_header = {}
        creds_path = _Path.home() / ".claude" / ".credentials.json"
        if not api_key and creds_path.exists():
            try:
                creds = _json.loads(creds_path.read_text())
                oauth = creds.get("claudeAiOauth", {})
                token = oauth.get("accessToken")
                if token:
                    auth_header = {"Authorization": f"Bearer {token}"}
            except Exception:
                pass

        if not api_key and not auth_header:
            return {"error": "No API key or OAuth token available"}

        if api_key:
            auth_header = {"x-api-key": api_key}

        import aiohttp

        headers = {
            **auth_header,
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "q"}],
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    rate_info: dict = {}
                    # Extract all unified rate-limit headers
                    for key, val in resp.headers.items():
                        lk = key.lower()
                        if "ratelimit-unified" in lk:
                            # Simplify header names
                            short_key = lk.replace("anthropic-ratelimit-unified-", "")
                            rate_info[short_key] = val

                    # Parse utilisation into a percentage
                    for k, v in list(rate_info.items()):
                        if "utilization" in k:
                            try:
                                rate_info[k + "_pct"] = f"{float(v) * 100:.1f}%"
                            except ValueError:
                                pass

                    # Parse reset timestamp
                    reset_ts = rate_info.get("reset")
                    if reset_ts:
                        try:
                            from datetime import datetime, timezone

                            reset_dt = datetime.fromtimestamp(float(reset_ts), tz=timezone.utc)
                            rate_info["reset_human"] = reset_dt.strftime("%Y-%m-%d %H:%M UTC")
                            # Time until reset
                            now = datetime.now(timezone.utc)
                            delta = reset_dt - now
                            if delta.total_seconds() > 0:
                                hours = int(delta.total_seconds() // 3600)
                                mins = int((delta.total_seconds() % 3600) // 60)
                                rate_info["resets_in"] = f"{hours}h {mins}m"
                        except (ValueError, OSError):
                            pass

                    rate_info["http_status"] = resp.status
                    return rate_info
        except Exception as e:
            return {"error": f"API probe failed: {e}"}

    # -----------------------------------------------------------------------
    # Project commands -- CRUD, pause/resume, and Discord channel management.
    # Projects are the top-level grouping: each project has its own workspace
    # directory, scheduling weight, and optional dedicated Discord channel.
    # -----------------------------------------------------------------------

    async def _cmd_get_status(self, args: dict) -> dict:
        filter_project = args.get("project_id")
        projects = await self.db.list_projects()
        tasks = await self.db.list_tasks(project_id=filter_project)

        # Build agent view from workspaces (scoped to project when provided)
        agent_details = []
        target_projects = projects
        if filter_project:
            target_projects = [p for p in projects if p.id == filter_project]
        for p in target_projects:
            workspaces = await self.db.list_workspaces(project_id=p.id)
            for ws in workspaces:
                if ws.locked_by_task_id:
                    state = "busy"
                    task = await self.db.get_task(ws.locked_by_task_id)
                    info: dict = {
                        "workspace_id": ws.id,
                        "name": ws.name or ws.id,
                        "project_id": p.id,
                        "state": state,
                    }
                    if task:
                        info["working_on"] = {
                            "task_id": task.id,
                            "title": task.title,
                            "project_id": task.project_id,
                            "status": task.status.value,
                        }
                else:
                    info = {
                        "workspace_id": ws.id,
                        "name": ws.name or ws.id,
                        "project_id": p.id,
                        "state": "idle",
                    }
                agent_details.append(info)

        in_progress = [
            {
                "id": t.id,
                "title": t.title,
                "project_id": t.project_id,
                "assigned_agent": t.assigned_agent_id,
            }
            for t in tasks
            if t.status == TaskStatus.IN_PROGRESS
        ]
        ready = [
            {"id": t.id, "title": t.title, "project_id": t.project_id}
            for t in tasks
            if t.status == TaskStatus.READY
        ]

        return {
            "projects": 1 if filter_project else len(projects),
            "agents": agent_details,
            "tasks": {
                "total": len(tasks),
                "by_status": _count_by(tasks, lambda t: t.status.value),
                "in_progress": in_progress,
                "ready_to_work": ready,
            },
            "orchestrator_paused": self.orchestrator._paused,
        }

    async def _cmd_list_projects(self, args: dict) -> dict:
        projects = await self.db.list_projects()
        result = []
        for p in projects:
            ws_path = await self.db.get_project_workspace_path(p.id)
            info = {
                "id": p.id,
                "name": p.name,
                "status": p.status.value,
                "credit_weight": p.credit_weight,
                "max_concurrent_agents": p.max_concurrent_agents,
                "workspace": ws_path,
            }
            if p.repo_url:
                info["repo_url"] = p.repo_url
            if p.discord_channel_id:
                info["discord_channel_id"] = p.discord_channel_id
            result.append(info)
        return {"projects": result}

    async def _cmd_create_project(self, args: dict) -> dict:
        project_id = args["name"].lower().replace(" ", "-")
        project = Project(
            id=project_id,
            name=args["name"],
            credit_weight=args.get("credit_weight", 1.0),
            max_concurrent_agents=args.get("max_concurrent_agents", 2),
            repo_url=args.get("repo_url", ""),
            repo_default_branch=args.get("default_branch", "main"),
        )
        await self.db.create_project(project)

        # Ensure the per-project task directory exists (vault migration Phase 1).
        project_tasks_dir = os.path.join(self.config.data_dir, "tasks", project_id)
        os.makedirs(project_tasks_dir, exist_ok=True)

        # Create vault subdirectories for the new project (vault spec §2).
        from src.vault import ensure_vault_project_dirs

        ensure_vault_project_dirs(self.config.data_dir, project_id)

        # Determine whether auto-channel creation should happen.
        # An explicit ``auto_create_channels`` arg takes precedence;
        # otherwise fall back to the per-project-channels config flag.
        explicit = args.get("auto_create_channels")
        if explicit is not None:
            should_auto_create = bool(explicit)
        else:
            ppc = self.config.discord.per_project_channels
            should_auto_create = ppc.auto_create

        return {
            "created": project_id,
            "name": project.name,
            "auto_create_channels": should_auto_create,
        }

    async def _cmd_pause_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        await self.db.update_project(pid, status=ProjectStatus.PAUSED)
        return {"paused": pid, "name": project.name}

    async def _cmd_resume_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        await self.db.update_project(pid, status=ProjectStatus.ACTIVE)
        return {"resumed": pid, "name": project.name}

    async def _cmd_edit_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        updates = {}
        if "name" in args:
            updates["name"] = args["name"]
        if "credit_weight" in args:
            updates["credit_weight"] = args["credit_weight"]
        if "max_concurrent_agents" in args:
            updates["max_concurrent_agents"] = args["max_concurrent_agents"]
        if "budget_limit" in args:
            updates["budget_limit"] = args["budget_limit"]
        if "discord_channel_id" in args:
            updates["discord_channel_id"] = args["discord_channel_id"]
        if "default_profile_id" in args:
            dpid = args["default_profile_id"]
            if dpid is not None:
                profile = await self.db.get_profile(dpid)
                if not profile:
                    return {"error": f"Profile '{dpid}' not found"}
            updates["default_profile_id"] = dpid  # None clears it
        if "repo_default_branch" in args:
            updates["repo_default_branch"] = args["repo_default_branch"]
        if not updates:
            return {
                "error": (
                    "No fields to update. Provide name, credit_weight, "
                    "max_concurrent_agents, budget_limit, discord_channel_id, "
                    "default_profile_id, or repo_default_branch."
                )
            }
        await self.db.update_project(pid, **updates)
        return {"updated": pid, "fields": list(updates.keys())}

    async def _cmd_set_project_channel(self, args: dict) -> dict:
        """Link an existing Discord channel to a project."""
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}

        channel_id = args["channel_id"]
        await self.db.update_project(pid, discord_channel_id=channel_id)

        return {
            "project_id": pid,
            "channel_id": channel_id,
            "status": "linked",
        }

    async def _cmd_set_default_branch(self, args: dict) -> dict:
        """Set (or change) a project's default branch.

        If the branch does not exist on the remote yet, it is created by
        pushing the current HEAD of the old default branch to the new name.
        """
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}

        branch = args.get("branch", "").strip()
        if not branch:
            return {"error": "branch is required"}

        old_branch = project.repo_default_branch or "main"

        # If the project has a workspace, optionally create the branch
        # on the remote when it doesn't exist yet.
        ws_path = await self.db.get_project_workspace_path(pid)
        branch_created = False
        if ws_path:
            git = self.orchestrator.git
            try:
                # Fetch latest so we know what branches exist on the remote
                await git._arun(["fetch", "origin"], cwd=ws_path)

                # Check if the branch exists on the remote
                try:
                    await git._arun(
                        ["rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
                        cwd=ws_path,
                    )
                except Exception:
                    # Branch does not exist on the remote — create it from
                    # the current default branch (or HEAD).
                    try:
                        await git._arun(
                            ["branch", branch, f"origin/{old_branch}"],
                            cwd=ws_path,
                        )
                    except Exception:
                        # If old default branch ref doesn't exist, branch from HEAD
                        await git._arun(["branch", branch, "HEAD"], cwd=ws_path)
                    await git._arun(
                        ["push", "-u", "origin", branch],
                        cwd=ws_path,
                    )
                    branch_created = True
            except Exception as exc:
                logger.warning(
                    "Could not verify/create branch %s for project %s: %s",
                    branch,
                    pid,
                    exc,
                )

        await self.db.update_project(pid, repo_default_branch=branch)

        result: dict = {
            "project_id": pid,
            "default_branch": branch,
            "previous_branch": old_branch,
            "status": "updated",
        }
        if branch_created:
            result["branch_created"] = True
        return result

    async def _cmd_set_control_interface(self, args: dict) -> dict:
        """Set a project's channel by channel *name* (string lookup).

        Resolves the channel name within the guild, then delegates to
        ``_cmd_set_project_channel``.
        Requires ``guild_channels`` to be supplied by the caller (the Discord
        command layer passes the guild's text channels so this layer stays
        Discord-import-free).
        """
        pid = args.get("project_id") or args.get("project_name")
        if not pid:
            return {"error": "project_id (or project_name) is required"}
        channel_name: str | None = args.get("channel_name")
        if not channel_name:
            return {"error": "channel_name is required"}

        # Normalise: strip leading '#' if the user included one.
        channel_name = channel_name.lstrip("#").strip()

        # --- Resolve channel name → ID ---
        # Option A: The caller already looked up the ID (Discord slash command).
        channel_id: str | None = args.get("_resolved_channel_id")

        if not channel_id:
            # Option B: guild_channels list supplied (list of {id, name} dicts).
            guild_channels = args.get("guild_channels")
            if guild_channels:
                for ch in guild_channels:
                    if ch["name"] == channel_name:
                        channel_id = str(ch["id"])
                        break
                if not channel_id:
                    return {"error": f"No text channel named '{channel_name}' found in this server"}
            else:
                return {
                    "error": (
                        "Cannot resolve channel name without guild context. "
                        "Use set_project_channel with a channel_id instead, "
                        "or invoke this command from Discord."
                    )
                }

        # Delegate to the existing set_project_channel handler.
        return await self._cmd_set_project_channel(
            {
                "project_id": pid,
                "channel_id": channel_id,
            }
        )

    async def _cmd_get_project(self, args: dict) -> dict:
        """Return full details for a single project."""
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        ws_path = await self.db.get_project_workspace_path(pid)
        usage = await self.db.get_project_token_usage(pid)
        info: dict = {
            "id": project.id,
            "name": project.name,
            "status": project.status.value,
            "repo_url": project.repo_url or "",
            "repo_default_branch": project.repo_default_branch,
            "workspace": ws_path,
            "credit_weight": project.credit_weight,
            "max_concurrent_agents": project.max_concurrent_agents,
            "total_tokens_used": project.total_tokens_used,
            "tokens_used_recent": usage,
        }
        if project.budget_limit is not None:
            info["budget_limit"] = project.budget_limit
        if project.discord_channel_id:
            info["discord_channel_id"] = project.discord_channel_id
        if project.default_profile_id:
            info["default_profile_id"] = project.default_profile_id
        return info

    async def _cmd_get_project_channels(self, args: dict) -> dict:
        """Return the Discord channel ID configured for a project."""
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        return {
            "project_id": pid,
            "channel_id": project.discord_channel_id,
        }

    async def _cmd_get_project_for_channel(self, args: dict) -> dict:
        """Reverse lookup: find which project a Discord channel belongs to.

        Scans all projects and checks ``discord_channel_id``.
        Returns the first match, or ``project_id: null`` if no project
        is linked to the channel.
        """
        channel_id = args.get("channel_id")
        if not channel_id:
            return {"error": "channel_id is required"}

        channel_id = str(channel_id)
        projects = await self.db.list_projects()
        for project in projects:
            if project.discord_channel_id == channel_id:
                return {
                    "channel_id": channel_id,
                    "project_id": project.id,
                    "project_name": project.name,
                }

        return {
            "channel_id": channel_id,
            "project_id": None,
            "project_name": None,
        }

    async def _cmd_delete_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        tasks = await self.db.list_tasks(project_id=pid, status=TaskStatus.IN_PROGRESS)
        if tasks:
            return {
                "error": f"Cannot delete: {len(tasks)} task(s) currently IN_PROGRESS. "
                "Stop them first."
            }

        # Capture channel ID before the DB cascade removes it.
        channel_ids: dict[str, str] = {}
        if project.discord_channel_id:
            channel_ids["channel"] = project.discord_channel_id

        await self.db.delete_project(pid)

        # Notify listeners (e.g. Discord bot) so they can purge in-memory
        # channel caches, notes-thread mappings, etc.
        if self._on_project_deleted:
            self._on_project_deleted(pid)

        result: dict = {"deleted": pid, "name": project.name}
        if channel_ids:
            result["channel_ids"] = channel_ids
        # Pass through the caller's archive preference so the Discord layer
        # can act on it.
        archive = args.get("archive_channels", False)
        if archive:
            result["archive_channels"] = True
        return result

    # -----------------------------------------------------------------------
    # Task commands -- CRUD plus lifecycle operations.
    # Tasks are the unit of work assigned to agents.  Beyond basic CRUD this
    # group includes stop (cancel a running task), restart (re-queue a
    # failed/completed task), skip (mark as completed without running),
    # approve (accept an AWAITING_APPROVAL task's PR), and chain-health
    # diagnostics for dependency graphs.
    # -----------------------------------------------------------------------

    # Statuses considered "finished" for the include_completed / completed_only
    # filters.  Only COMPLETED is treated as finished — FAILED and BLOCKED
    # tasks still need attention (retry/fix or dependency resolution) and
    # should be visible in the default task list so the progress breakdown
    # numbers add up correctly.
    _FINISHED_STATUSES: frozenset[TaskStatus] = frozenset(
        {
            TaskStatus.COMPLETED,
        }
    )

    async def _resolve_root_task_id(self, task_id: str) -> str:
        """Walk up the parent chain to find the topmost ancestor task ID.

        Used by tree/compact display modes to determine the root task that
        should be rendered as the tree head for a given subtask.  Includes a
        cycle guard to protect against malformed parent chains.
        """
        current_id = task_id
        seen: set[str] = set()
        while True:
            if current_id in seen:
                break  # cycle guard
            seen.add(current_id)
            task = await self.db.get_task(current_id)
            if task is None or task.parent_task_id is None:
                return current_id
            current_id = task.parent_task_id
        return current_id

    async def _build_dep_map_for_tree(
        self,
        tree_data: dict,
        base_map: dict[str, dict] | None = None,
    ) -> dict[str, dict]:
        """Build a dependency map covering every task in *tree_data*.

        Starts from *base_map* (which typically comes from the pre-fetched
        ``task_list`` entries) and fills in any tree nodes that are missing,
        so that ``_tree_dep_annotation()`` can annotate every node.

        Parameters
        ----------
        tree_data:
            A tree hierarchy dict from ``Database.get_task_tree()``.
        base_map:
            Pre-existing dependency data keyed by task ID.  Entries already
            present are reused without additional DB queries.

        Returns
        -------
        dict[str, dict]
            Mapping of ``task_id`` → ``{"depends_on": [...], "blocks": [...]}``.
        """
        result = dict(base_map) if base_map else {}
        # Find tree task IDs not already in the base map
        missing_ids = [tid for tid in _collect_tree_task_ids(tree_data) if tid not in result]
        if missing_ids:
            # Batch-fetch all missing dependency data in two queries
            batch_result = await self.db.get_dependency_map_for_tasks(missing_ids)
            result.update(batch_result)
        return result

    @staticmethod
    def format_task_with_dependencies(task: dict) -> str:
        """Format a single task dict with optional dependency annotation lines.

        Produces output like::

            🔵 #12: Set up database [READY]
               ↳ depends on: #10 (COMPLETED ✅), #11 (IN_PROGRESS 🟡)

        or::

            🟡 #14: Build API endpoints [IN_PROGRESS]
               ↳ blocks: #15, #16, #17

        Tasks with no dependencies or dependents get a single line with no
        annotation.  The status emoji for the main task is looked up from
        ``STATUS_EMOJIS``; dependency references also include their status
        emoji for quick visual scanning.

        Parameters
        ----------
        task : dict
            A task dict as returned by ``_cmd_list_tasks`` when
            ``show_dependencies=True``.  Must contain at least ``id``,
            ``title``, and ``status`` keys.  May contain ``depends_on`` and
            ``blocks`` lists (each entry: ``{id, title, status}``).

        Returns
        -------
        str
            One or more lines of formatted text.
        """
        status = task.get("status", "DEFINED")
        emoji = STATUS_EMOJIS.get(status, "⚪")
        line = f"{emoji} #{task['id']}: {task['title']} [{status}]"
        lines = [line]

        # depends_on annotation
        depends_on = task.get("depends_on", [])
        if depends_on:
            parts = []
            for dep in depends_on:
                dep_emoji = STATUS_EMOJIS.get(dep["status"], "⚪")
                parts.append(f"#{dep['id']} ({dep['status']} {dep_emoji})")
            lines.append(f"   ↳ depends on: {', '.join(parts)}")

        # blocks annotation
        blocks = task.get("blocks", [])
        if blocks:
            parts = [f"#{b['id']}" for b in blocks]
            lines.append(f"   ↳ blocks: {', '.join(parts)}")

        return "\n".join(lines)

    @staticmethod
    def format_task_list_with_dependencies(tasks: list[dict]) -> str:
        """Format a full task list with dependency annotations.

        Convenience wrapper around :meth:`format_task_with_dependencies` that
        joins all task blocks with a newline separator.

        Parameters
        ----------
        tasks : list[dict]
            Task dicts as returned by ``_cmd_list_tasks`` (the ``"tasks"``
            value) when ``show_dependencies=True``.

        Returns
        -------
        str
            Multi-line formatted text ready for display.
        """
        return "\n".join(CommandHandler.format_task_with_dependencies(t) for t in tasks)

    async def _cmd_list_tasks(self, args: dict) -> dict:
        """List tasks with configurable display mode.

        Supports three ``display_mode`` values:

        ``"flat"`` (default)
            The original flat list of task dicts — every task is an
            independent row.  This is backward-compatible with all
            existing callers.

        ``"tree"``
            Group tasks by parent and render each root task's hierarchy
            using :func:`_format_task_tree` (expanded, with box-drawing
            characters).  The response includes both the pre-formatted
            text and structured data so callers can choose how to present
            it.

        ``"compact"``
            Show only root (parent) tasks with a subtask count and
            progress bar.  Uses :func:`_format_task_tree` in compact
            mode.  Ideal for dense overview lists.

        For ``"tree"`` and ``"compact"`` modes, a ``project_id`` is
        required so we can query parent tasks.  If ``project_id`` is
        missing the method silently falls back to ``"flat"``.

        When ``show_dependencies`` is ``True``, each task dict is enriched
        with ``depends_on`` (list of upstream task IDs + statuses) and
        ``blocks`` (list of downstream dependent task IDs + statuses).

        Parameters
        ----------
        args : dict
            ``project_id`` – filter by project (optional).
            ``status`` – filter by a specific TaskStatus value (optional).
            ``display_mode`` – ``"flat"``, ``"tree"``, or ``"compact"`` (default ``"flat"``).
            ``include_completed`` – if True, include terminal tasks (default False).
            ``completed_only`` – if True, show only terminal tasks (default False).
            ``show_dependencies`` – if True, enrich each task dict with
            ``depends_on`` and ``blocks`` lists and include a pre-formatted
            ``formatted`` key with the dependency-aware text representation.
        """
        # Normalize show_all → include_completed so all downstream helpers
        # only need to check include_completed.
        if args.get("show_all") and not args.get("include_completed"):
            args = {**args, "include_completed": True}

        display_mode: str = args.get("display_mode", "flat")
        show_dependencies: bool = args.get("show_dependencies", False)

        kwargs = {}
        if "project_id" in args:
            kwargs["project_id"] = args["project_id"]

        # An explicit `status` filter takes precedence over the convenience
        # boolean flags — the caller is asking for a specific status.
        explicit_status = "status" in args
        if explicit_status:
            kwargs["status"] = TaskStatus(args["status"])

        # ── Flat mode (default / backward-compatible) ──────────────────
        # Also used as the fallback when tree/compact lack a project_id.
        if display_mode == "flat" or "project_id" not in args:
            return await self._list_tasks_flat(
                args,
                kwargs,
                explicit_status,
                show_dependencies=show_dependencies,
            )

        # ── Tree / Compact modes ───────────────────────────────────────
        return await self._list_tasks_hierarchical(
            args,
            kwargs,
            explicit_status,
            compact=(display_mode == "compact"),
            show_dependencies=show_dependencies,
        )

    # -- private helpers for _cmd_list_tasks display modes -------------------

    async def _list_tasks_flat(
        self,
        args: dict,
        db_kwargs: dict,
        explicit_status: bool,
        *,
        show_dependencies: bool = False,
    ) -> dict:
        """Flat list mode — the original ``_cmd_list_tasks`` behaviour."""
        tasks = await self.db.list_tasks(**db_kwargs)

        # Apply include_completed / completed_only filtering only when no
        # explicit status filter was provided.
        include_completed: bool = args.get("include_completed", False)
        hidden_count = 0
        if not explicit_status:
            completed_only: bool = args.get("completed_only", False)
            all_count = len(tasks)

            if completed_only:
                # Show only finished tasks.
                tasks = [t for t in tasks if t.status in self._FINISHED_STATUSES]
                hidden_count = all_count - len(tasks)
            elif not include_completed:
                # Default: hide finished tasks so the list shows active work.
                tasks = [t for t in tasks if t.status not in self._FINISHED_STATUSES]
                hidden_count = all_count - len(tasks)
            # else: include_completed=True — return everything unfiltered.

        task_dicts = [self._task_to_dict(t) for t in tasks[:200]]

        if show_dependencies:
            await self._enrich_with_dependencies(task_dicts, tasks[:200])

        result: dict = {
            "display_mode": "flat",
            "tasks": task_dicts,
            "total": len(tasks),
            "hidden_completed": hidden_count,
            "filtered": not include_completed and "status" not in args,
        }
        if show_dependencies:
            result["dependency_display"] = format_dependency_list(task_dicts)
        return result

    async def _cmd_list_active_tasks_all_projects(self, args: dict) -> dict:
        """List active (non-terminal) tasks across ALL projects, grouped by project.

        This gives a cross-project overview of everything that is currently
        queued, in-progress, or otherwise actionable.  Only COMPLETED tasks
        are excluded by default; FAILED and BLOCKED tasks are shown since
        they still need attention.  Use ``include_completed=True`` to also
        include completed tasks.

        Uses ``Database.list_active_tasks()`` for SQL-level filtering when
        showing only active tasks, avoiding the need to fetch and discard
        potentially large numbers of completed tasks.
        """
        include_completed = args.get("include_completed", False)

        if include_completed:
            # Caller wants everything -- no status filtering.
            tasks = await self.db.list_tasks()
        else:
            # SQL-level filtering excludes terminal statuses.
            tasks = await self.db.list_active_tasks()

        # Compute how many terminal tasks were hidden (for UI hints).
        hidden_completed = 0
        if not include_completed:
            status_counts = await self.db.count_tasks_by_status()
            _terminal_values = {"COMPLETED"}
            hidden_completed = sum(
                cnt for st, cnt in status_counts.items() if st in _terminal_values
            )

        # Build a task-entry dict (reused for both grouped and flat views).
        def _entry(t: Task, *, include_project: bool = False) -> dict:
            d: dict = {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority,
                "assigned_agent": t.assigned_agent_id,
                "parent_task_id": t.parent_task_id,
                "is_plan_subtask": t.is_plan_subtask,
                "task_type": t.task_type.value if t.task_type else None,
                "pr_url": t.pr_url,
                "requires_approval": t.requires_approval,
            }
            if include_project:
                d["project_id"] = t.project_id
            return d

        # Group by project_id for readability.
        by_project: dict[str, list[dict]] = {}
        for t in tasks:
            by_project.setdefault(t.project_id, []).append(_entry(t))

        # Also build a flat list (capped at 200) for simple consumers.
        flat = [_entry(t, include_project=True) for t in tasks[:200]]

        return {
            "by_project": by_project,
            "tasks": flat,
            "total": len(tasks),
            "project_count": len(by_project),
            "hidden_completed": hidden_completed,
        }

    async def _list_tasks_hierarchical(
        self,
        args: dict,
        db_kwargs: dict,
        explicit_status: bool,
        *,
        compact: bool,
        show_dependencies: bool = False,
    ) -> dict:
        """Tree or compact list mode — groups tasks by parent hierarchy.

        Fetches root (parentless) tasks for the project, then builds the
        full subtask tree for each root task using
        ``Database.get_task_tree()``.  The caller receives both
        pre-formatted text (ready for Discord) and structured data.
        """
        project_id: str = db_kwargs["project_id"]
        mode_name = "compact" if compact else "tree"

        # 1. Get all root-level tasks for the project.
        root_tasks = await self.db.get_parent_tasks(project_id)

        # 2. Apply status filtering to root tasks.
        if explicit_status:
            status_filter = TaskStatus(args["status"])
            root_tasks = [t for t in root_tasks if t.status == status_filter]
        else:
            root_tasks = self._apply_completion_filter(root_tasks, args)

        # 3. Build tree for each root and format.
        #    When show_dependencies is active we need two passes:
        #      a) collect all trees so we know every task in every subtree,
        #      b) build a dep_map for the full set, then re-format with
        #         annotations.  The first pass still stores a provisional
        #         ``formatted`` string (without annotations) so that if
        #         dep_map turns out empty the output is unchanged.
        trees: list[dict] = []
        included_roots: list[Task] = []  # Track Task objects for dependency enrichment
        # raw_trees stores (root, children) pairs for a second formatting pass
        raw_trees: list[tuple[Task, list[dict]]] = []
        total_tasks = 0

        for root in root_tasks[:200]:
            tree_data = await self.db.get_task_tree(root.id)
            if tree_data is None:
                # Shouldn't happen — root was just fetched — but be safe.
                continue

            children = tree_data.get("children", [])
            completed, subtask_total = _count_subtree(children)
            status_counts = _count_subtree_by_status(children)

            formatted = _format_task_tree(
                root,
                children,
                compact=compact,
            )

            tree_entry: dict = {
                "root": self._task_to_dict(root),
                "formatted": formatted,
                "subtask_completed": completed,
                "subtask_total": subtask_total,
                "subtask_by_status": status_counts,
            }

            # In compact mode, also include a text progress bar for
            # callers that want to display it inline.
            if compact and subtask_total > 0:
                tree_entry["progress_bar"] = progress_bar(
                    completed,
                    subtask_total,
                )

            trees.append(tree_entry)
            included_roots.append(root)
            raw_trees.append((root, children))
            # Count root + all its subtasks
            total_tasks += 1 + subtask_total

        # Enrich root task dicts with dependency info when requested.
        if show_dependencies:
            root_dicts = [entry["root"] for entry in trees]
            await self._enrich_with_dependencies(root_dicts, included_roots)

            # Build dep_map across ALL tasks in all trees and re-format
            # expanded trees with inline annotations.  Compact mode is
            # skipped — annotations are too dense for the summary format.
            if not compact:
                all_tasks: list[Task] = []
                for root, children in raw_trees:
                    all_tasks.append(root)
                    all_tasks.extend(_collect_tree_tasks(children))

                dep_map = await self._build_dep_map(all_tasks)
                if dep_map:
                    for i, (root, children) in enumerate(raw_trees):
                        trees[i]["formatted"] = _format_task_tree(
                            root,
                            children,
                            compact=False,
                            dep_map=dep_map,
                        )

        return {
            "display_mode": mode_name,
            "trees": trees,
            "total_root_tasks": len(trees),
            "total_tasks": total_tasks,
        }

    # -- shared helpers ------------------------------------------------------

    def _apply_completion_filter(
        self,
        tasks: list[Task],
        args: dict,
    ) -> list[Task]:
        """Filter a task list by the ``include_completed`` / ``completed_only``
        convenience flags.  Used by both flat and hierarchical modes.
        """
        include_completed: bool = args.get("include_completed", False)
        completed_only: bool = args.get("completed_only", False)

        if completed_only:
            return [t for t in tasks if t.status in self._FINISHED_STATUSES]
        if not include_completed:
            return [t for t in tasks if t.status not in self._FINISHED_STATUSES]
        # include_completed=True — return everything unfiltered.
        return tasks

    async def _enrich_with_dependencies(
        self,
        task_dicts: list[dict],
        tasks: list[Task],
    ) -> None:
        """Add ``depends_on`` and ``blocks`` keys to each task dict in-place.

        ``depends_on`` contains a list of upstream dependency dicts, each with
        ``id``, ``title``, and ``status``.  ``blocks`` contains a list of
        downstream dependent task IDs with the same shape.

        Uses the existing ``get_dependencies()`` and ``get_dependents()`` DB
        helpers.  Lookups are batched per-task but results are cached within
        the call to avoid redundant ``get_task()`` queries when the same
        dependency appears across multiple tasks.
        """
        # Local cache so repeated dependency IDs don't trigger extra DB reads.
        task_cache: dict[str, Task | None] = {}

        async def _resolve(task_id: str) -> dict | None:
            if task_id not in task_cache:
                task_cache[task_id] = await self.db.get_task(task_id)
            t = task_cache[task_id]
            if t is None:
                return None
            return {"id": t.id, "title": t.title, "status": t.status.value}

        for td, task in zip(task_dicts, tasks):
            # Upstream: tasks this task depends on
            dep_ids = await self.db.get_dependencies(task.id)
            if dep_ids:
                dep_details = []
                for dep_id in dep_ids:
                    resolved = await _resolve(dep_id)
                    if resolved:
                        dep_details.append(resolved)
                td["depends_on"] = dep_details
            else:
                td["depends_on"] = []

            # Downstream: tasks that depend on this task
            dependent_ids = await self.db.get_dependents(task.id)
            if dependent_ids:
                block_details = []
                for dep_id in dependent_ids:
                    resolved = await _resolve(dep_id)
                    if resolved:
                        block_details.append(resolved)
                td["blocks"] = block_details
            else:
                td["blocks"] = []

    async def _build_dep_map(
        self,
        tasks: list[Task],
    ) -> dict[str, dict]:
        """Build a dependency map for annotating tree nodes.

        Returns a dict mapping ``task_id`` → ``{"depends_on": [...], "blocks": [...]}``
        where each list element is ``{"id": str, "title": str, "status": str}``.

        Only tasks that have at least one dependency or dependent are included
        in the returned map — callers can treat a missing key as "no
        dependencies".

        This is similar to :meth:`_enrich_with_dependencies` but returns a
        standalone mapping suitable for passing to :func:`_format_task_tree`
        instead of mutating task dicts in-place.
        """
        # Local cache so repeated dependency IDs don't trigger extra DB reads.
        task_cache: dict[str, Task | None] = {}

        async def _resolve(task_id: str) -> dict | None:
            if task_id not in task_cache:
                task_cache[task_id] = await self.db.get_task(task_id)
            t = task_cache[task_id]
            if t is None:
                return None
            return {"id": t.id, "title": t.title, "status": t.status.value}

        dep_map: dict[str, dict] = {}

        for task in tasks:
            # Upstream: tasks this task depends on
            dep_ids = await self.db.get_dependencies(task.id)
            depends_on: list[dict] = []
            for dep_id in dep_ids:
                resolved = await _resolve(dep_id)
                if resolved:
                    depends_on.append(resolved)

            # Downstream: tasks that depend on this task
            dependent_ids = await self.db.get_dependents(task.id)
            blocks: list[dict] = []
            for dep_id in dependent_ids:
                resolved = await _resolve(dep_id)
                if resolved:
                    blocks.append(resolved)

            if depends_on or blocks:
                dep_map[task.id] = {
                    "depends_on": depends_on,
                    "blocks": blocks,
                }

        return dep_map

    @staticmethod
    def _task_to_dict(t: Task) -> dict:
        """Serialize a :class:`Task` to the standard dict used in list
        responses.  Centralises the field selection so flat and
        hierarchical modes stay consistent.
        """
        return {
            "id": t.id,
            "project_id": t.project_id,
            "title": t.title,
            "status": t.status.value,
            "priority": t.priority,
            "assigned_agent": t.assigned_agent_id,
            "parent_task_id": t.parent_task_id,
            "is_plan_subtask": t.is_plan_subtask,
            "task_type": t.task_type.value if t.task_type else None,
            "pr_url": t.pr_url,
            "requires_approval": t.requires_approval,
        }

    async def _cmd_get_task_tree(self, args: dict) -> dict:
        """Return the full subtask hierarchy for a single parent task.

        Fetches the task tree from the database and renders it using
        :func:`_format_task_tree`.  Returns both structured data and
        pre-formatted text so callers (Discord embeds, chat agent) can
        choose how to present it.

        Parameters (via *args*):
            task_id (str): Required.  The root task whose tree to fetch.
            compact (bool): If ``True``, render in compact mode (root +
                summary only).  Default ``False``.
            max_depth (int): Maximum nesting depth before collapsing.
                Default 4.
            show_dependencies (bool): If ``True``, annotate tree nodes with
                inline dependency arrows (e.g. ``← needs #abc``).
                Default ``False``.
        """
        task_id: str = args["task_id"]
        compact: bool = args.get("compact", False)
        max_depth: int = args.get("max_depth", 4)
        show_dependencies: bool = args.get("show_dependencies", False)

        tree_data = await self.db.get_task_tree(task_id)
        if tree_data is None:
            return {"error": f"Task '{task_id}' not found"}

        root_task: Task = tree_data["task"]
        children: list[dict] = tree_data.get("children", [])

        completed, subtask_total = _count_subtree(children)
        status_counts = _count_subtree_by_status(children)

        # Build dependency map for tree annotations when requested.
        dep_map: dict[str, dict] | None = None
        if show_dependencies and not compact:
            all_tasks = [root_task] + _collect_tree_tasks(children)
            dep_map = await self._build_dep_map(all_tasks)
            # Only pass dep_map if it actually contains entries.
            if not dep_map:
                dep_map = None

        formatted = _format_task_tree(
            root_task,
            children,
            compact=compact,
            max_depth=max_depth,
            dep_map=dep_map,
        )

        result: dict = {
            "root": self._task_to_dict(root_task),
            "formatted": formatted,
            "subtask_completed": completed,
            "subtask_total": subtask_total,
            "subtask_by_status": status_counts,
        }

        # In compact mode, include a text progress bar for inline display.
        if compact and subtask_total > 0:
            result["progress_bar"] = progress_bar(completed, subtask_total)

        return result

    async def _cmd_create_task(self, args: dict) -> dict:
        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        task_id = await generate_task_id(self.db)
        requires_approval = args.get("requires_approval", False)
        # Resolve optional task_type from string to enum.
        raw_task_type = args.get("task_type")
        task_type: TaskType | None = None
        if raw_task_type:
            if raw_task_type in TASK_TYPE_VALUES:
                task_type = TaskType(raw_task_type)
            else:
                return {
                    "error": f"Invalid task_type '{raw_task_type}'. "
                    f"Allowed: {', '.join(sorted(TASK_TYPE_VALUES))}"
                }
        # Validate optional profile_id
        profile_id = args.get("profile_id")
        if profile_id:
            profile = await self.db.get_profile(profile_id)
            if not profile:
                return {"error": f"Profile '{profile_id}' not found"}
        # Validate optional preferred_workspace_id
        preferred_workspace_id = args.get("preferred_workspace_id")
        if preferred_workspace_id:
            ws = await self.db.get_workspace(preferred_workspace_id)
            if not ws:
                return {"error": f"Workspace '{preferred_workspace_id}' not found"}
            if ws.project_id != project_id:
                return {
                    "error": f"Workspace '{preferred_workspace_id}' belongs to "
                    f"project '{ws.project_id}', not '{project_id}'"
                }
        # Validate optional attachments (list of file paths)
        attachments = args.get("attachments", [])
        if attachments:
            import os

            valid_paths = []
            for path in attachments:
                if os.path.isfile(path):
                    valid_paths.append(os.path.abspath(path))
                else:
                    return {"error": f"Attachment file not found: {path}"}
            attachments = valid_paths

        initial_status = (
            TaskStatus.DEFINED if self._plan_subtask_creation_mode else TaskStatus.READY
        )
        auto_approve_plan = args.get("auto_approve_plan", False)
        skip_verification = args.get("skip_verification", False)
        task = Task(
            id=task_id,
            project_id=project_id,
            title=args["title"],
            description=args.get("description", args["title"]),
            priority=args.get("priority", 100),
            status=initial_status,
            requires_approval=requires_approval,
            task_type=task_type,
            profile_id=profile_id,
            preferred_workspace_id=preferred_workspace_id,
            attachments=attachments,
            auto_approve_plan=auto_approve_plan,
            skip_verification=skip_verification,
        )
        await self.db.create_task(task)

        # If the supervisor set conversation context (the thread chain it was
        # responding to), store it as task_context so the executing agent gets
        # the same conversational backdrop the supervisor had.
        if self._current_conversation_context:
            await self.db.add_task_context(
                task_id,
                type="conversation_context",
                label="Conversation Thread Context",
                content=self._current_conversation_context,
            )

        result = {
            "created": task_id,
            "title": task.title,
            "project_id": task.project_id,
        }
        if requires_approval:
            result["requires_approval"] = True
        if task_type:
            result["task_type"] = task_type.value
        if profile_id:
            result["profile_id"] = profile_id
        if preferred_workspace_id:
            result["preferred_workspace_id"] = preferred_workspace_id
        if attachments:
            result["attachments"] = attachments
        if auto_approve_plan:
            result["auto_approve_plan"] = True
        if skip_verification:
            result["skip_verification"] = True

        # Cross-project warning: if project_id was implicitly inherited from
        # the active channel context (not explicitly passed by the caller),
        # check whether the task title or description mentions another known
        # project name.  This catches the common mistake of creating a task
        # for project A while chatting in project B's channel.
        if not args.get("project_id"):
            other_projects = await self.db.list_projects()
            text_to_check = f"{task.title} {task.description}".lower()
            mentioned = [
                p.id for p in other_projects if p.id != project_id and p.id.lower() in text_to_check
            ]
            if mentioned:
                result["warning"] = (
                    f"Task was assigned to '{project_id}' (from channel context) "
                    f"but its content mentions project(s): {', '.join(mentioned)}. "
                    f"If this task belongs to a different project, update it with "
                    f"edit_task(task_id='{task_id}', project_id='<correct_project>')."
                )

        return result

    async def _cmd_get_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        info = {
            "id": task.id,
            "project_id": task.project_id,
            "title": task.title,
            "description": task.description,
            "status": task.status.value,
            "priority": task.priority,
            "assigned_agent": task.assigned_agent_id,
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "requires_approval": task.requires_approval,
            "is_plan_subtask": task.is_plan_subtask,
            "task_type": task.task_type.value if task.task_type else None,
            "parent_task_id": task.parent_task_id,
            "profile_id": task.profile_id,
            "auto_approve_plan": task.auto_approve_plan,
            "skip_verification": task.skip_verification,
        }
        if task.pr_url:
            info["pr_url"] = task.pr_url

        # Dependency visualization: show what this task depends on and blocks
        deps = await self.db.get_dependencies(task.id)
        if deps:
            dep_details = []
            for dep_id in deps:
                dep_task = await self.db.get_task(dep_id)
                if dep_task:
                    dep_details.append(
                        {
                            "id": dep_task.id,
                            "title": dep_task.title,
                            "status": dep_task.status.value,
                        }
                    )
            info["depends_on"] = dep_details

        dependents = await self.db.get_dependents(task.id)
        if dependents:
            dep_details = []
            for dep_id in dependents:
                dep_task = await self.db.get_task(dep_id)
                if dep_task:
                    dep_details.append(
                        {
                            "id": dep_task.id,
                            "title": dep_task.title,
                            "status": dep_task.status.value,
                        }
                    )
            info["blocks"] = dep_details

        # Subtask info
        subtasks = await self.db.get_subtasks(task.id)
        if subtasks:
            info["subtasks"] = [
                {
                    "id": st.id,
                    "title": st.title,
                    "status": st.status.value,
                }
                for st in subtasks
            ]

        return info

    async def _cmd_task_deps(self, args: dict) -> dict:
        """Return upstream dependencies and downstream dependents for a task.

        Used by the ``/task-deps`` slash command to render a focused
        dependency view with visual status for each related task.

        Returns
        -------
        dict
            ``task_id``, ``title``, ``status``, ``depends_on`` list, and
            ``blocks`` list.  Each entry in those lists carries ``id``,
            ``title``, and ``status``.
        """
        task_id = args.get("task_id", "")
        if not task_id:
            return {"error": "task_id is required"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        # Upstream: what this task depends on
        dep_ids = await self.db.get_dependencies(task.id)
        depends_on: list[dict] = []
        for dep_id in sorted(dep_ids):
            dep_task = await self.db.get_task(dep_id)
            if dep_task:
                depends_on.append(
                    {
                        "id": dep_task.id,
                        "title": dep_task.title,
                        "status": dep_task.status.value,
                    }
                )

        # Downstream: what this task blocks
        dependent_ids = await self.db.get_dependents(task.id)
        blocks: list[dict] = []
        for dep_id in sorted(dependent_ids):
            dep_task = await self.db.get_task(dep_id)
            if dep_task:
                blocks.append(
                    {
                        "id": dep_task.id,
                        "title": dep_task.title,
                        "status": dep_task.status.value,
                    }
                )

        return {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "depends_on": depends_on,
            "blocks": blocks,
        }

    async def _cmd_get_task_dependencies(self, args: dict) -> dict:
        """Alias for ``_cmd_task_deps`` — used by the Supervisor tool.

        The ``/task-deps`` slash command uses ``task_deps`` while the
        Supervisor exposes the same data as ``get_task_dependencies``.
        Both route through the same logic.
        """
        return await self._cmd_task_deps(args)

    async def _cmd_add_dependency(self, args: dict) -> dict:
        """Add a dependency edge: *task_id* depends on *depends_on*.

        Validates both tasks exist and performs cycle detection before
        persisting the edge.  Returns the updated dependency view for the
        task so callers can confirm the new state.
        """
        task_id = args.get("task_id", "")
        depends_on = args.get("depends_on", "")
        if not task_id:
            return {"error": "task_id is required"}
        if not depends_on:
            return {"error": "depends_on is required"}
        if task_id == depends_on:
            return {"error": "A task cannot depend on itself"}

        # Verify both tasks exist.
        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        dep_task = await self.db.get_task(depends_on)
        if not dep_task:
            return {"error": f"Dependency task '{depends_on}' not found"}

        # Check for duplicate edge.
        existing = await self.db.get_dependencies(task_id)
        if depends_on in existing:
            return {
                "error": f"Dependency already exists: '{task_id}' already depends on '{depends_on}'"
            }

        # Cycle detection — build the full graph and validate.
        all_deps = await self.db.get_all_dependencies()
        try:
            validate_dag_with_new_edge(all_deps, task_id, depends_on)
        except CyclicDependencyError as exc:
            return {"error": f"Cannot add dependency: {exc}"}

        await self.db.add_dependency(task_id, depends_on)

        return {
            "ok": True,
            "task_id": task_id,
            "depends_on": depends_on,
            "task_title": task.title,
            "depends_on_title": dep_task.title,
        }

    async def _cmd_remove_dependency(self, args: dict) -> dict:
        """Remove a dependency edge: *task_id* no longer depends on *depends_on*.

        Returns a confirmation dict.  Silently succeeds if the edge does
        not exist (idempotent).
        """
        task_id = args.get("task_id", "")
        depends_on = args.get("depends_on", "")
        if not task_id:
            return {"error": "task_id is required"}
        if not depends_on:
            return {"error": "depends_on is required"}

        # Verify the task exists (the dependency target need not still exist).
        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        # Check if the dependency edge actually exists.
        existing = await self.db.get_dependencies(task_id)
        if depends_on not in existing:
            return {"error": f"No dependency found: '{task_id}' does not depend on '{depends_on}'"}

        await self.db.remove_dependency(task_id, depends_on)

        return {
            "ok": True,
            "task_id": task_id,
            "removed_dependency": depends_on,
            "task_title": task.title,
        }

    async def _cmd_edit_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}

        VERIFICATION_VALUES = frozenset(v.value for v in VerificationType)

        # Handle status change separately — uses transition_task for logging
        status_changed = False
        if "status" in args:
            new_status_raw = args["status"]
            try:
                new_status = TaskStatus(new_status_raw)
            except ValueError:
                valid = ", ".join(s.value for s in TaskStatus)
                return {"error": f"Invalid status '{new_status_raw}'. Valid: {valid}"}
            old_status = task.status.value
            await self.db.transition_task(
                args["task_id"],
                new_status,
                context="edit_task",
            )
            status_changed = True

        updates = {}
        if "project_id" in args:
            new_pid = args["project_id"]
            project = await self.db.get_project(new_pid)
            if not project:
                return {"error": f"Project '{new_pid}' not found"}
            updates["project_id"] = new_pid
        if "title" in args:
            updates["title"] = args["title"]
        if "description" in args:
            updates["description"] = args["description"]
        if "priority" in args:
            updates["priority"] = args["priority"]
        if "task_type" in args:
            raw_tt = args["task_type"]
            if raw_tt is None:
                updates["task_type"] = None  # allow clearing task_type
            elif raw_tt in TASK_TYPE_VALUES:
                updates["task_type"] = TaskType(raw_tt)
            else:
                return {
                    "error": f"Invalid task_type '{raw_tt}'. Allowed: {', '.join(sorted(TASK_TYPE_VALUES))}"
                }
        if "max_retries" in args:
            updates["max_retries"] = args["max_retries"]
        if "verification_type" in args:
            raw_vt = args["verification_type"]
            if raw_vt in VERIFICATION_VALUES:
                updates["verification_type"] = VerificationType(raw_vt)
            else:
                return {
                    "error": f"Invalid verification_type '{raw_vt}'. Allowed: {', '.join(sorted(VERIFICATION_VALUES))}"
                }
        if "profile_id" in args:
            pid = args["profile_id"]
            if pid is not None:
                profile = await self.db.get_profile(pid)
                if not profile:
                    return {"error": f"Profile '{pid}' not found"}
            updates["profile_id"] = pid  # None clears the profile
        if "auto_approve_plan" in args:
            updates["auto_approve_plan"] = bool(args["auto_approve_plan"])
        if "skip_verification" in args:
            updates["skip_verification"] = bool(args["skip_verification"])

        if updates:
            await self.db.update_task(args["task_id"], **updates)

        all_fields = list(updates.keys())
        if status_changed:
            all_fields.append("status")

        if not all_fields:
            return {
                "error": (
                    "No fields to update. Provide project_id, title, description, priority, "
                    "task_type, status, max_retries, verification_type, profile_id, "
                    "auto_approve_plan, or skip_verification."
                )
            }

        result = {"updated": args["task_id"], "fields": all_fields}
        if status_changed:
            result["old_status"] = old_status
            result["new_status"] = new_status_raw
        return result

    async def _cmd_stop_task(self, args: dict) -> dict:
        error = await self.orchestrator.stop_task(args["task_id"])
        if error:
            return {"error": error}
        return {"stopped": args["task_id"]}

    async def _cmd_restart_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if task.status == TaskStatus.IN_PROGRESS:
            return {"error": "Task is currently in progress. Stop it first."}
        old_status = task.status.value
        await self.db.transition_task(
            args["task_id"],
            TaskStatus.READY,
            context="restart_task",
            retry_count=0,
            assigned_agent_id=None,
        )
        return {
            "restarted": args["task_id"],
            "title": task.title,
            "previous_status": old_status,
        }

    async def _cmd_reopen_with_feedback(self, args: dict) -> dict:
        """Reopen a completed/failed task with feedback appended to its description.

        Used when a completed or failed task needs to be retried because issues
        were found.  The feedback is appended to the task description so the
        agent sees it on re-execution, stored as a structured task_context
        entry for programmatic access, and the task is reset to READY.

        The PR URL is cleared so the agent can create a fresh PR on the next
        execution, and retry_count is reset to 0.

        Required args: task_id, feedback (the feedback text).
        """
        task_id = args.get("task_id")
        feedback = args.get("feedback", "").strip()
        if not task_id:
            return {"error": "task_id is required"}
        if not feedback:
            return {"error": "feedback text is required"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        if task.status == TaskStatus.IN_PROGRESS:
            return {"error": "Task is currently in progress. Stop it first."}

        old_status = task.status.value

        # Append feedback to the task description so the agent sees it
        # when the task is re-executed.
        separator = "\n\n---\n**Reopen Feedback:**\n"
        updated_description = task.description + separator + feedback

        # Preserve requires_approval so the agent re-creates a PR on
        # completion (the field must survive reopen cycles).
        await self.db.transition_task(
            task_id,
            TaskStatus.READY,
            context="reopen_with_feedback",
            description=updated_description,
            retry_count=0,
            assigned_agent_id=None,
            pr_url=None,
            requires_approval=task.requires_approval,
        )

        # Store feedback as a structured task_context entry so agents and
        # tooling can access individual reopen comments programmatically.
        await self.db.add_task_context(
            task_id,
            type="reopen_feedback",
            label="Reopen Feedback",
            content=feedback,
        )

        await self.db.log_event(
            "reopen_with_feedback",
            project_id=task.project_id,
            task_id=task_id,
            payload=feedback[:500],
        )
        return {
            "reopened": task_id,
            "title": task.title,
            "previous_status": old_status,
            "status": "READY",
            "feedback_added": True,
            "requires_approval": task.requires_approval,
        }

    async def _cmd_delete_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if task.status == TaskStatus.IN_PROGRESS:
            error = await self.orchestrator.stop_task(args["task_id"])
            if error:
                return {"error": f"Could not stop task before deleting: {error}"}
        await self.db.delete_task(args["task_id"])
        return {"deleted": args["task_id"], "title": task.title}

    # -- Archive commands -----------------------------------------------------
    # Archive moves completed tasks out of the active view into the
    # ``archived_tasks`` DB table and writes markdown notes to
    # ``~/.agent-queue/archived_tasks/<project_id>/``.  Tasks can be listed,
    # inspected, restored, or permanently deleted from the archive.

    async def _cmd_archive_tasks(self, args: dict) -> dict:
        """Archive completed (and optionally failed/blocked) tasks.

        Moves matching tasks into the ``archived_tasks`` database table and
        writes a markdown reference note for each task into
        ``~/.agent-queue/archived_tasks/<project_id>/``.

        Parameters
        ----------
        args : dict
            ``project_id`` – optional project scope.  When omitted, all
            matching tasks across every project are archived.
            ``include_failed`` – if ``True``, also archive FAILED and BLOCKED
            tasks in addition to COMPLETED.  Default ``False``.
        """
        project_id = args.get("project_id")
        include_failed = args.get("include_failed", False)

        # Determine which statuses to archive.
        statuses_to_archive = [TaskStatus.COMPLETED]
        if include_failed:
            statuses_to_archive.extend([TaskStatus.FAILED, TaskStatus.BLOCKED])

        tasks_to_archive: list = []
        for status in statuses_to_archive:
            tasks_to_archive.extend(await self.db.list_tasks(project_id=project_id, status=status))

        if not tasks_to_archive:
            scope = f" in project `{project_id}`" if project_id else ""
            return {"message": f"No completed tasks to archive{scope}."}

        # Phase 1 — gather results and dependencies before any deletions.
        task_data: list[tuple] = []
        for task in tasks_to_archive:
            result = await self.db.get_task_result(task.id)
            deps = await self.db.get_dependencies(task.id)
            task_data.append((task, result, deps))

        # Phase 2 — archive each task (DB table + optional markdown note).
        archived: list[dict] = []
        for task, result, deps in task_data:
            # Write markdown note if the project has a workspace.
            archive_path = await self._write_archive_note(task, result, deps)

            # Move to the archived_tasks table.
            await self.db.archive_task(task.id)

            await self.db.log_event(
                "task_archived",
                project_id=task.project_id,
                task_id=task.id,
            )
            archived.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "status": task.status.value,
                    "archive_path": archive_path,
                }
            )

        # Determine archive_dir for the response (use first task's project).
        archive_dir = None
        for entry in archived:
            if entry["archive_path"]:
                archive_dir = os.path.dirname(entry["archive_path"])
                break

        return {
            "archived_count": len(archived),
            "archived_ids": [a["id"] for a in archived],
            "archived": archived,
            "archive_dir": archive_dir,
            "project_id": project_id,
        }

    async def _cmd_archive_task(self, args: dict) -> dict:
        """Archive a single task by ID.

        The task must be in a terminal status (COMPLETED, FAILED, or BLOCKED).
        Active (non-terminal) tasks cannot be archived — stop or complete them
        first.

        Parameters
        ----------
        args : dict
            ``task_id`` – the task to archive.
        """
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}
        if task.status not in terminal:
            return {
                "error": (
                    f"Cannot archive task in {task.status.value} status. "
                    "Only COMPLETED, FAILED, or BLOCKED tasks can be archived."
                ),
            }

        # Gather result/deps before archiving (archive deletes child rows).
        result = await self.db.get_task_result(task_id)
        deps = await self.db.get_dependencies(task_id)
        await self._write_archive_note(task, result, deps)

        success = await self.db.archive_task(task_id)
        if not success:
            return {"error": f"Failed to archive task '{task_id}'"}

        await self.db.log_event(
            "task_archived",
            project_id=task.project_id,
            task_id=task_id,
        )
        return {
            "archived": task_id,
            "title": task.title,
            "status": task.status.value,
        }

    async def _cmd_list_archived(self, args: dict) -> dict:
        """List archived tasks, optionally scoped to a project.

        Parameters
        ----------
        args : dict
            ``project_id`` – optional project scope.
            ``limit`` – max number of results (default 50).
        """
        project_id = args.get("project_id")
        limit = int(args.get("limit", 50))
        tasks = await self.db.list_archived_tasks(
            project_id=project_id,
            limit=limit,
        )
        total = await self.db.count_archived_tasks(project_id=project_id)
        return {
            "tasks": tasks,
            "count": len(tasks),
            "total": total,
            "project_id": project_id,
        }

    async def _cmd_restore_task(self, args: dict) -> dict:
        """Restore an archived task back into the active task list.

        The task is restored with status DEFINED so it enters the normal
        orchestrator lifecycle (dependency check → READY → scheduling).

        Parameters
        ----------
        args : dict
            ``task_id`` – the archived task to restore.
        """
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}

        archived = await self.db.get_archived_task(task_id)
        if not archived:
            return {"error": f"Archived task '{task_id}' not found"}

        success = await self.db.restore_archived_task(task_id)
        if not success:
            return {"error": f"Failed to restore task '{task_id}'"}

        await self.db.log_event(
            "task_restored",
            project_id=archived["project_id"],
            task_id=task_id,
        )
        return {
            "restored": task_id,
            "title": archived["title"],
            "new_status": "DEFINED",
        }

    async def _cmd_archive_settings(self, args: dict) -> dict:
        """Return the current auto-archive configuration.

        Reads from ``config.archive`` and includes the count of currently
        archived tasks and how many terminal tasks are eligible right now.
        """
        cfg = self.config.archive
        archived_count = await self.db.count_archived_tasks()

        # Count how many active terminal tasks would be archived now
        older_than_seconds = cfg.after_hours * 3600
        import time as _time

        cutoff = _time.time() - older_than_seconds
        eligible = 0
        if cfg.enabled and cfg.statuses:
            for status in cfg.statuses:
                tasks = await self.db.list_tasks(status=TaskStatus(status))
                for t in tasks:
                    # Check updated_at from DB row
                    cursor = await self.db._db.execute(
                        "SELECT updated_at FROM tasks WHERE id = ?", (t.id,)
                    )
                    row = await cursor.fetchone()
                    if row and row["updated_at"] <= cutoff:
                        eligible += 1

        return {
            "enabled": cfg.enabled,
            "after_hours": cfg.after_hours,
            "statuses": cfg.statuses,
            "archived_count": archived_count,
            "eligible_count": eligible,
        }

    async def _cmd_provide_input(self, args: dict) -> dict:
        """Provide a human reply to an agent question (WAITING_INPUT → READY).

        The agent's question is answered by appending the human's response to the
        task description so the agent sees it on re-execution.  The task is
        transitioned to READY so the scheduler picks it up in the next cycle.

        Required args: task_id, input (the human's response text).
        """
        task_id = args.get("task_id")
        input_text = args.get("input", "").strip()
        if not task_id:
            return {"error": "task_id is required"}
        if not input_text:
            return {"error": "input text is required"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        if task.status != TaskStatus.WAITING_INPUT:
            return {"error": f"Task is not waiting for input (status: {task.status.value})"}

        # Append the human reply to the task description so the agent sees it
        # when the task is re-executed.
        separator = "\n\n---\n**Human Reply:**\n"
        updated_description = task.description + separator + input_text
        await self.db.update_task(task_id, description=updated_description)

        # Transition WAITING_INPUT → READY so the scheduler re-runs the task.
        await self.db.transition_task(
            task_id,
            TaskStatus.READY,
            context="human_replied",
        )
        await self.db.log_event(
            "human_replied",
            project_id=task.project_id,
            task_id=task_id,
            payload=input_text[:500],
        )
        return {
            "task_id": task_id,
            "title": task.title,
            "status": "READY",
        }

    async def _cmd_approve_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if task.status != TaskStatus.AWAITING_APPROVAL:
            return {"error": f"Task is not awaiting approval (status: {task.status.value})"}
        await self.db.transition_task(
            args["task_id"],
            TaskStatus.COMPLETED,
            context="approve_task",
        )
        await self.db.log_event(
            "task_completed",
            project_id=task.project_id,
            task_id=task.id,
        )
        return {"approved": args["task_id"], "title": task.title}

    async def _cmd_process_task_completion(self, args: dict) -> dict:
        """Process task completion: discover plan files and archive them.

        Called by Supervisor after a task completes. Searches for plan files
        in the workspace, parses them, archives them to .claude/plans/, and
        stores plan data via task_context for later approval flow.

        Includes safety checks ported from the legacy
        ``Orchestrator._discover_and_store_plan()`` path:
        - Staleness check: plan file mtime vs task execution start time
        - skip_if_implemented: skip if branch has substantial non-plan changes
        - Deduplication: skip if tasks with the same titles already exist

        Returns {"plan_found": bool, "steps_count": int, ...}
        """
        import logging
        from src import plan_parser

        logger = logging.getLogger(__name__)

        task_id = args.get("task_id")
        workspace_path = args.get("workspace_path")

        logger.info(
            "process_task_completion: starting plan discovery for task %s (workspace=%s)",
            task_id,
            workspace_path,
        )

        config = self.orchestrator.config.auto_task

        # Check if auto_task is enabled
        if not config.enabled:
            logger.info(
                "process_task_completion: auto-task disabled, skipping plan discovery for task %s",
                task_id,
            )
            return {"plan_found": False, "reason": "Auto-task generation is disabled"}

        if not task_id or not workspace_path:
            return {"plan_found": False, "reason": "Missing required parameters"}

        # Look up the task to check if it's a subtask
        task = await self.db.get_task(task_id)
        if not task:
            return {"plan_found": False, "reason": f"Task {task_id} not found"}

        # Don't process plans for plan-generated subtasks (avoid recursion)
        if task.is_plan_subtask:
            return {"plan_found": False, "reason": "Task is already a plan subtask"}

        # skip_if_implemented: if the branch already has substantial code
        # changes beyond the plan file itself, the agent likely already
        # implemented the plan — no need to create subtasks from it.
        if config.skip_if_implemented:
            try:
                project = await self.db.get_project(task.project_id) if task.project_id else None
                default_branch = await self.orchestrator._get_default_branch(
                    project, workspace_path
                )
                if await self.orchestrator.git.ahas_non_plan_changes(
                    workspace_path, default_branch
                ):
                    logger.info(
                        "process_task_completion: skipping plan for task %s — "
                        "branch has substantial code changes beyond the plan file, "
                        "indicating the plan was already implemented",
                        task_id,
                    )
                    return {
                        "plan_found": False,
                        "reason": "Plan already implemented (non-plan changes detected)",
                    }
            except Exception as e:
                logger.debug(
                    "process_task_completion: skip_if_implemented check failed for task %s: %s",
                    task_id,
                    e,
                )

        # Find a plan file in the workspace
        plan_patterns = config.plan_file_patterns
        plan_file = plan_parser.find_plan_file(workspace_path, plan_patterns)

        if not plan_file:
            logger.info(
                "process_task_completion: no plan file found for task %s "
                "(searched patterns: %s in %s)",
                task_id,
                plan_patterns,
                workspace_path,
            )
            return {"plan_found": False, "reason": "No plan file found"}

        logger.info(
            "process_task_completion: found plan file %s for task %s — running validation checks",
            plan_file,
            task_id,
        )

        # Staleness check: compare the plan file's mtime against the
        # recorded execution start time.  If the plan file predates the
        # agent's execution start, it was written by a previous task and
        # should not be attributed to this one.
        exec_start = self.orchestrator._task_exec_start.get(task_id)
        if exec_start is not None:
            try:
                plan_mtime = os.path.getmtime(plan_file)
                # Use a 2-second tolerance for filesystem timestamp granularity
                if plan_mtime < exec_start - 2.0:
                    logger.warning(
                        "process_task_completion: ignoring stale plan file %s for task %s "
                        "(plan mtime %.0f < exec start %.0f — file "
                        "predates this task's execution)",
                        plan_file,
                        task_id,
                        plan_mtime,
                        exec_start,
                    )
                    # Archive stale plan so it's not rediscovered by future tasks
                    try:
                        plans_dir = os.path.join(workspace_path, ".claude", "plans")
                        os.makedirs(plans_dir, exist_ok=True)
                        stale_archive = os.path.join(plans_dir, f"stale-{task_id}-plan.md")
                        os.rename(plan_file, stale_archive)
                    except OSError:
                        pass
                    return {
                        "plan_found": False,
                        "reason": "Plan file is stale (predates task execution)",
                    }
            except OSError as e:
                logger.debug(
                    "process_task_completion: staleness check failed for %s: %s (proceeding)",
                    plan_file,
                    e,
                )

        # Read the plan file content
        try:
            content = plan_parser.read_plan_file(plan_file)
        except Exception as e:
            logger.warning("Failed to read plan file for task %s: %s", task_id, e)
            return {"plan_found": False, "reason": f"Failed to read plan file: {e}"}

        if not content or not content.strip():
            return {"plan_found": False, "reason": "Plan file is empty"}

        # Archive the plan file to .claude/plans/{task_id}-plan.md
        try:
            plans_dir = os.path.join(workspace_path, ".claude", "plans")
            os.makedirs(plans_dir, exist_ok=True)
            archived_path = os.path.join(plans_dir, f"{task_id}-plan.md")

            os.rename(plan_file, archived_path)
            logger.info("Archived plan file to %s", archived_path)
        except Exception as e:
            logger.warning("Failed to archive plan file for task %s: %s", task_id, e)
            archived_path = plan_file  # Use original path if archival fails

        # Store raw plan content in task_context for later use during approval.
        # The actual plan-to-task splitting is done by the supervisor LLM
        # at approval time (not by algorithmic parsing here).
        try:
            await self.db.add_task_context(
                task_id,
                type="plan_archived_path",
                label="Plan Archived Path",
                content=archived_path,
            )
            await self.db.add_task_context(
                task_id,
                type="plan_raw",
                label="Plan Raw Content",
                content=content,
            )
        except Exception as e:
            logger.warning("Failed to store plan context for task %s: %s", task_id, e)

        logger.info(
            "process_task_completion: plan stored for task %s — "
            "plan_found=True, archived=%s, content_length=%d",
            task_id,
            archived_path,
            len(content),
        )

        return {
            "plan_found": True,
            "plan_file": plan_file,
            "archived_path": archived_path,
        }

    async def _cmd_approve_plan(self, args: dict) -> dict:
        """Approve a plan and activate its draft subtasks.

        The task must be in AWAITING_PLAN_APPROVAL status.

        **Parse-first workflow (preferred):**
        If ``_cmd_process_plan`` already created draft subtasks (stored in
        ``plan_draft_subtasks`` context), approval transitions the parent to
        IN_PROGRESS — signaling that the plan is approved and subtasks are
        running.  The parent will auto-complete when all subtasks finish.

        **Legacy workflow (fallback):**
        If no draft subtasks exist (e.g. plan came through the old auto-
        detection path), the supervisor LLM is called to create tasks now,
        falling back to the algorithmic parser if the supervisor is
        unavailable.
        """
        import json as _json
        import logging

        logger = logging.getLogger(__name__)

        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if task.status != TaskStatus.AWAITING_PLAN_APPROVAL:
            return {"error": f"Task is not awaiting plan approval (status: {task.status.value})"}

        # Retrieve stored plan content
        contexts = await self.db.get_task_contexts(task.id)
        raw_ctx = next((c for c in contexts if c["type"] == "plan_raw"), None)
        if not raw_ctx:
            return {"error": "No stored plan content found for this task"}

        raw_plan = raw_ctx["content"]
        config = self.orchestrator.config.auto_task

        # ── Check for pre-created draft subtasks (parse-first workflow) ──
        draft_ctx = next((c for c in contexts if c["type"] == "plan_draft_subtasks"), None)

        created_info: list[dict] = []

        if draft_ctx:
            # Draft subtasks were already created by _cmd_process_plan.
            # They are blocked by a dependency on this parent task.
            # Transitioning the parent to IN_PROGRESS will unblock the chain
            # (plan subtask dependency checking treats IN_PROGRESS parents as met).
            created_info = _json.loads(draft_ctx["content"])
            logger.info(
                "approve_plan: found %d pre-created draft subtasks for %s",
                len(created_info),
                task.id,
            )

            # Defense-in-depth: draft_ctx should always have subtasks, but
            # guard against empty list (e.g. data corruption, partial save).
            if not created_info:
                logger.warning(
                    "approve_plan: draft subtasks context exists but is empty "
                    "for task %s — refusing to approve",
                    task.id,
                )
                return {
                    "error": (
                        "Draft subtasks context is empty — no subtasks to activate. "
                        "Please retry or use /process-plan to manually trigger "
                        "subtask creation."
                    )
                }

            # Handle downstream dependencies: any task that depends on the
            # parent task should also depend on the final subtask so
            # downstream work waits for the full chain to finish.
            if config.chain_dependencies and created_info:
                final_subtask_id = created_info[-1]["id"]
                dependents = await self.db.get_dependents(task.id)
                # Exclude draft subtasks themselves from downstream dep wiring
                draft_ids = {t["id"] for t in created_info}
                for dep_task_id in dependents:
                    if dep_task_id in draft_ids:
                        continue
                    try:
                        await self.db.add_dependency(dep_task_id, depends_on=final_subtask_id)
                    except Exception as e:
                        logger.warning(
                            "Failed to add downstream dep %s→%s: %s",
                            dep_task_id,
                            final_subtask_id,
                            e,
                        )
        else:
            # ── Legacy workflow: create subtasks on approval ──
            ws = await self.db.get_workspace_for_task(task.id)
            workspace_id = ws.id if ws else None
            if not workspace_id and task.project_id:
                workspaces = await self.db.list_workspaces(project_id=task.project_id)
                if workspaces:
                    workspace_id = workspaces[0].id

            supervisor = getattr(self.orchestrator, "_supervisor", None)

            if supervisor and supervisor.is_ready:
                _lock_project = task.project_id or ""
                created_info = await supervisor.break_plan_into_tasks(
                    raw_plan=raw_plan,
                    parent_task_id=task.id,
                    project_id=_lock_project,
                    workspace_id=workspace_id,
                    chain_dependencies=config.chain_dependencies,
                    requires_approval=(
                        task.requires_approval if config.inherit_approval else False
                    ),
                    base_priority=task.priority,
                )

                # Treat empty result as an error — the LLM call may have
                # failed silently (rate limit, timeout, etc.).
                if not created_info:
                    logger.warning(
                        "approve_plan: supervisor returned 0 subtasks for "
                        "task %s — refusing to approve",
                        task.id,
                    )
                    return {
                        "error": (
                            "Supervisor failed to create subtasks from the plan. "
                            "The plan has not been approved. Please retry or use "
                            "/process-plan to manually trigger subtask creation."
                        )
                    }

                if config.chain_dependencies:
                    final_subtask_id = created_info[-1]["id"]
                    dependents = await self.db.get_dependents(task.id)
                    for dep_task_id in dependents:
                        try:
                            await self.db.add_dependency(dep_task_id, depends_on=final_subtask_id)
                        except Exception as e:
                            logger.warning(
                                "Failed to add downstream dep %s→%s: %s",
                                dep_task_id,
                                final_subtask_id,
                                e,
                            )
            else:
                logger.error(
                    "approve_plan: supervisor unavailable — cannot create subtasks for task %s",
                    task.id,
                )
                return {"error": "Supervisor is not available. Cannot create subtasks from plan."}

        # Note: no separate channel notification here — the PlanApprovalView
        # already updates the original embed in-place to show approval status
        # and subtask count, avoiding duplicate messages.

        # Transition to IN_PROGRESS — the plan is approved and subtasks are
        # active.  The parent will auto-complete when all subtasks finish
        # (see _check_plan_parent_completion in orchestrator.py).
        # Plan subtask dependency checking treats IN_PROGRESS plan parents
        # as satisfying the dependency.
        await self.db.transition_task(
            args["task_id"],
            TaskStatus.IN_PROGRESS,
            context="plan_approved",
        )

        # Re-check DEFINED tasks so subtasks get promoted to READY
        await self.orchestrator._check_defined_tasks()

        await self.db.log_event(
            "plan_approved",
            project_id=task.project_id,
            task_id=task.id,
            payload=f"Activated {len(created_info)} subtask(s)",
        )

        # Schedule heavy cleanup work (file deletion + git commits) in the
        # background so the approve command returns immediately and the
        # Discord UI feels responsive.
        async def _background_cleanup():
            try:
                await self._cleanup_plan_files_after_approval(task)
            except Exception as e:
                logger.warning(
                    "Background plan cleanup failed for task %s: %s",
                    task.id,
                    e,
                )

        asyncio.create_task(_background_cleanup())

        return {
            "approved": args["task_id"],
            "title": task.title,
            "subtask_count": len(created_info),
            "subtasks": created_info,
        }

    async def _cleanup_plan_files_after_approval(self, task) -> None:
        """Delete ALL plan files from all workspaces after a plan is approved/deleted.

        Removes:
        1. All plan files that were enumerated during ``_cmd_process_plan``
           (stored in ``plan_all_enumerated_paths`` task context).
        2. The archived plan file (in ``.claude/plans/``).
        3. Any original plan files matching configured patterns (safety net).

        Commits the deletions so plan files don't persist on the branch
        and get picked up by other tasks running in the same workspace.
        """
        logger = logging.getLogger(__name__)
        deleted_any = False
        workspaces_with_deletions: set[str] = set()

        contexts = await self.db.get_task_contexts(task.id)

        # 1. Delete ALL enumerated plan files (from process_plan discovery)
        enumerated_ctx = next(
            (c for c in contexts if c["type"] == "plan_all_enumerated_paths"), None
        )
        if enumerated_ctx:
            import json as _json_cleanup

            try:
                enumerated_paths = _json_cleanup.loads(enumerated_ctx["content"])
            except (ValueError, TypeError):
                enumerated_paths = []
            for plan_path in enumerated_paths:
                try:
                    if os.path.isfile(plan_path):
                        os.remove(plan_path)
                        deleted_any = True
                        # Track the workspace directory for committing
                        workspaces_with_deletions.add(
                            os.path.dirname(
                                os.path.dirname(plan_path) if ".claude" in plan_path else plan_path
                            )
                        )
                        logger.info("Plan cleanup: deleted enumerated plan file %s", plan_path)
                except OSError as e:
                    logger.warning(
                        "Plan cleanup: failed to delete enumerated plan %s: %s", plan_path, e
                    )

        # 2. Delete the archived plan file if it exists
        archived_ctx = next((c for c in contexts if c["type"] == "plan_archived_path"), None)
        if archived_ctx:
            archived_path = archived_ctx["content"]
            try:
                if os.path.isfile(archived_path):
                    os.remove(archived_path)
                    deleted_any = True
                    logger.info("Plan cleanup: deleted archived plan file %s", archived_path)
            except OSError as e:
                logger.warning(
                    "Plan cleanup: failed to delete archived plan %s: %s", archived_path, e
                )

        # 3. Safety net: also check configured plan patterns in all project workspaces
        ws = await self.db.get_workspace_for_task(task.id)
        if ws and ws.workspace_path:
            workspace = ws.workspace_path
        else:
            workspace = await self.db.get_project_workspace_path(task.project_id)

        if workspace:
            import glob as _glob

            plan_patterns = self.orchestrator.config.auto_task.plan_file_patterns
            for pattern in plan_patterns:
                full_pattern = os.path.join(workspace, pattern)
                matches = _glob.glob(full_pattern)
                if not matches:
                    if os.path.isfile(full_pattern):
                        matches = [full_pattern]
                for plan_path in matches:
                    try:
                        if os.path.isfile(plan_path):
                            os.remove(plan_path)
                            deleted_any = True
                            workspaces_with_deletions.add(workspace)
                            logger.info("Plan cleanup: deleted plan file %s", plan_path)
                    except OSError as e:
                        logger.warning("Plan cleanup: failed to delete plan %s: %s", plan_path, e)

            # Also scan all project workspaces for any remaining plan files
            all_ws = await self.db.list_workspaces(project_id=task.project_id)
            for ws_entry in all_ws:
                if not ws_entry.workspace_path or not os.path.isdir(ws_entry.workspace_path):
                    continue
                from src.plan_parser import find_all_plan_files

                remaining = find_all_plan_files(ws_entry.workspace_path)
                for pf in remaining:
                    try:
                        if os.path.isfile(pf["path"]):
                            os.remove(pf["path"])
                            deleted_any = True
                            workspaces_with_deletions.add(ws_entry.workspace_path)
                            logger.info("Plan cleanup: deleted remaining plan file %s", pf["path"])
                    except OSError as e:
                        logger.warning(
                            "Plan cleanup: failed to delete remaining plan %s: %s", pf["path"], e
                        )

        # 4. Commit the deletions in each workspace that had files deleted
        if deleted_any and task.branch_name:
            # Determine which workspaces to commit in
            commit_workspaces: set[str] = set()
            if workspace:
                commit_workspaces.add(workspace)
            commit_workspaces.update(workspaces_with_deletions)

            for ws_path in commit_workspaces:
                if not os.path.isdir(ws_path):
                    continue
                try:
                    if await self.orchestrator.git.avalidate_checkout(ws_path):
                        await self.orchestrator.git.acommit_all(
                            ws_path,
                            f"chore: delete plan files after approval\n\nTask-Id: {task.id}",
                            event_bus=self.orchestrator.bus,
                            project_id=task.project_id,
                            agent_id=task.assigned_agent_id,
                        )
                        logger.info(
                            "Plan cleanup: committed plan file deletion in %s for task %s",
                            ws_path,
                            task.id,
                        )
                except Exception as e:
                    logger.warning(
                        "Plan cleanup: failed to commit deletion in %s for task %s: %s",
                        ws_path,
                        task.id,
                        e,
                    )

    async def _delete_draft_subtasks(self, parent_task_id: str) -> int:
        """Delete draft subtasks that were pre-created by ``_cmd_process_plan``.

        Reads the ``plan_draft_subtasks`` task-context entry, deletes each
        referenced task from the database, and returns the count of deleted
        tasks.  Safe to call when no drafts exist (returns 0).
        """
        import json as _json

        contexts = await self.db.get_task_contexts(parent_task_id)
        draft_ctx = next((c for c in contexts if c["type"] == "plan_draft_subtasks"), None)
        if not draft_ctx:
            return 0

        draft_tasks: list[dict] = _json.loads(draft_ctx["content"])
        deleted = 0
        for entry in draft_tasks:
            tid = entry.get("id")
            if not tid:
                continue
            try:
                await self.db.delete_task(tid)
                deleted += 1
            except Exception as e:
                logger.warning("delete_draft_subtasks: failed to delete %s: %s", tid, e)
        logger.info(
            "delete_draft_subtasks: deleted %d/%d draft subtasks for %s",
            deleted,
            len(draft_tasks),
            parent_task_id,
        )
        return deleted

    async def _cmd_reject_plan(self, args: dict) -> dict:
        """Reject a plan and reopen the task with feedback for revision.

        The task must be in AWAITING_PLAN_APPROVAL status.  The feedback is
        appended to the task description so the agent sees it on re-execution
        and can revise the plan accordingly.  Any draft subtasks created
        during ``_cmd_process_plan`` are deleted.
        """
        task_id = args["task_id"]
        feedback = args.get("feedback", "")
        if not feedback:
            return {"error": "Feedback is required when rejecting a plan"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        if task.status != TaskStatus.AWAITING_PLAN_APPROVAL:
            return {"error": f"Task is not awaiting plan approval (status: {task.status.value})"}

        # Delete any draft subtasks created during process_plan
        deleted_count = await self._delete_draft_subtasks(task_id)

        # Append feedback to description (similar to reopen_with_feedback)
        separator = "\n\n---\n\n**Plan Revision Requested:**\n"
        updated_description = task.description + separator + feedback

        await self.db.transition_task(
            task_id,
            TaskStatus.READY,
            context="plan_rejected",
            description=updated_description,
            retry_count=0,
            assigned_agent_id=None,
            pr_url=None,
        )

        # Store feedback as a structured task_context entry
        await self.db.add_task_context(
            task_id,
            type="plan_revision_feedback",
            label="Plan Revision Feedback",
            content=feedback,
        )

        await self.db.log_event(
            "plan_rejected",
            project_id=task.project_id,
            task_id=task_id,
            payload=feedback[:500],
        )
        return {
            "rejected": task_id,
            "title": task.title,
            "status": "READY",
            "feedback_added": True,
            "draft_subtasks_deleted": deleted_count,
        }

    async def _cmd_delete_plan(self, args: dict) -> dict:
        """Delete a plan and complete the task without creating subtasks.

        The task must be in AWAITING_PLAN_APPROVAL status.  The task is
        transitioned to COMPLETED and no subtasks are created.  Any draft
        subtasks created during ``_cmd_process_plan`` are deleted.
        """
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if task.status != TaskStatus.AWAITING_PLAN_APPROVAL:
            return {"error": f"Task is not awaiting plan approval (status: {task.status.value})"}

        # Delete any draft subtasks created during process_plan
        deleted_count = await self._delete_draft_subtasks(task.id)

        # Clean up plan files from the workspace
        await self._cleanup_plan_files_after_approval(task)

        await self.db.transition_task(
            args["task_id"],
            TaskStatus.COMPLETED,
            context="plan_deleted",
        )
        await self.db.log_event(
            "plan_deleted",
            project_id=task.project_id,
            task_id=task.id,
            payload=f"Plan deleted by user — {deleted_count} draft subtask(s) removed",
        )
        return {
            "deleted": args["task_id"],
            "title": task.title,
            "status": "COMPLETED",
            "draft_subtasks_deleted": deleted_count,
        }

    async def _cmd_process_plan(self, args: dict) -> dict:
        """Manually scan project workspaces for plan files and present for approval.

        This allows users to trigger plan processing via Discord when the
        supervisor misses auto-detection (e.g. the task completed without
        a plan file being detected, or a plan was dropped into a workspace
        manually).

        If a specific task_id is provided, the plan is attached to that task.
        Otherwise a new synthetic "plan" task is created to hold the plan.

        **Workflow (parse-first):**

        1. Discover the plan file in the workspace.
        2. Use the supervisor LLM to break the plan into **draft subtasks**
           (with a dependency on the parent so they stay blocked).
        3. Present the draft subtasks for user approval.
        4. On approve → parent transitions to COMPLETED, unblocking the chain.
           On reject/delete → draft subtasks are deleted.
        """
        from src.plan_parser import find_all_plan_files, read_plan_file

        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        # Scan ALL workspaces for the project, collecting every plan file
        workspaces = await self.db.list_workspaces(project_id=project_id)
        if not workspaces:
            return {"error": f"No workspaces found for project '{project_id}'"}

        # Collect all plan files across all workspaces with their timestamps
        all_plan_files: list[dict] = []  # {path, ctime, workspace}
        for ws_candidate in workspaces:
            if not ws_candidate.workspace_path or not os.path.isdir(ws_candidate.workspace_path):
                continue
            ws_plans = find_all_plan_files(ws_candidate.workspace_path)
            for pf in ws_plans:
                pf["workspace"] = ws_candidate
                all_plan_files.append(pf)

        if not all_plan_files:
            return {
                "status": "no_plans_found",
                "project_id": project_id,
                "workspaces_scanned": len(workspaces),
                "message": f"No plan files found in {len(workspaces)} workspace(s) for project '{project_id}'",
            }

        # Sort all plan files by ctime (newest first) and pick the newest
        all_plan_files.sort(key=lambda f: f["ctime"], reverse=True)
        newest = all_plan_files[0]

        # Read the newest plan file
        try:
            raw = read_plan_file(newest["path"])
            if not raw or not raw.strip():
                return {
                    "status": "no_plans_found",
                    "project_id": project_id,
                    "workspaces_scanned": len(workspaces),
                    "message": f"Newest plan file is empty: {newest['path']}",
                }
        except Exception as e:
            return {"error": f"Failed to read newest plan file {newest['path']}: {e}"}

        ws = newest["workspace"]
        plan_path = newest["path"]

        logger.info(
            "process_plan: found %d plan file(s) across %d workspace(s), "
            "picking newest: %s (ctime=%.0f)",
            len(all_plan_files),
            len(workspaces),
            plan_path,
            newest["ctime"],
        )

        # Track ALL enumerated plan file paths for cleanup later
        all_enumerated_paths = [pf["path"] for pf in all_plan_files]

        # If a task_id is provided, attach the plan to that task
        task_id = args.get("task_id")
        if task_id:
            task = await self.db.get_task(task_id)
            if not task:
                return {"error": f"Task '{task_id}' not found"}
            if task.status == TaskStatus.AWAITING_PLAN_APPROVAL:
                return {"error": f"Task '{task_id}' already has a plan awaiting approval"}
        else:
            # Create a synthetic task to hold the plan
            task_id = await generate_task_id(self.db)
            # Extract a title from the plan content (first heading or filename)
            plan_title = "Manual plan processing"
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("# "):
                    plan_title = line[2:].strip()
                    break

            task = Task(
                id=task_id,
                project_id=project_id,
                title=plan_title,
                description=f"Plan discovered via manual /process-plan command.\n\nSource: `{plan_path}`",
                priority=100,
                status=TaskStatus.AWAITING_PLAN_APPROVAL,
                task_type=TaskType.PLAN,
            )
            await self.db.create_task(task)
            logger.info(
                "process_plan: created synthetic task %s for plan in %s",
                task_id,
                plan_path,
            )

        # Archive the plan file
        archived_path = None
        try:
            plans_dir = os.path.join(ws.workspace_path, ".claude", "plans")
            os.makedirs(plans_dir, exist_ok=True)
            archived_path = os.path.join(plans_dir, f"{task_id}-plan.md")
            os.rename(plan_path, archived_path)
            logger.info("process_plan: archived plan to %s", archived_path)
        except OSError as e:
            logger.warning("process_plan: failed to archive plan %s: %s", plan_path, e)

        # Store the raw plan content as task_context
        await self.db.add_task_context(
            task_id,
            type="plan_raw",
            label="Plan Raw Content",
            content=raw,
        )
        if archived_path:
            await self.db.add_task_context(
                task_id,
                type="plan_archived_path",
                label="Plan Archived Path",
                content=archived_path,
            )

        # Store ALL enumerated plan file paths so cleanup can delete them all
        import json as _json_ctx

        await self.db.add_task_context(
            task_id,
            type="plan_all_enumerated_paths",
            label="All Enumerated Plan File Paths",
            content=_json_ctx.dumps(all_enumerated_paths),
        )

        # If we attached to an existing task, transition it to AWAITING_PLAN_APPROVAL
        if args.get("task_id"):
            await self.db.transition_task(
                task_id,
                TaskStatus.AWAITING_PLAN_APPROVAL,
                context="manual_process_plan",
            )

        await self.db.log_event(
            "plan_discovered",
            project_id=project_id,
            task_id=task_id,
            payload=f"Manual plan processing triggered — plan found at {plan_path}",
        )

        # ── Phase 1: Use supervisor LLM to break plan into draft subtasks ──
        # This happens BEFORE approval so the user sees the proper task
        # breakdown.  Draft subtasks are blocked by a dependency on the
        # parent task (which is in AWAITING_PLAN_APPROVAL), so they won't
        # execute until the plan is approved and the parent is COMPLETED.
        #
        # IMPORTANT: We hold a plan-processing lock on the project while the
        # supervisor is creating subtasks.  This prevents the orchestrator's
        # _check_defined_tasks() from promoting subtasks to READY before
        # the blocking dependency on the parent has been established.
        config = self.orchestrator.config.auto_task
        supervisor = getattr(self.orchestrator, "_supervisor", None)
        created_info: list[dict] = []

        if supervisor and supervisor.is_ready:
            workspace_id = ws.id if ws else None
            created_info = await supervisor.break_plan_into_tasks(
                raw_plan=raw,
                parent_task_id=task_id,
                project_id=project_id,
                workspace_id=workspace_id,
                chain_dependencies=config.chain_dependencies,
                requires_approval=(task.requires_approval if config.inherit_approval else False),
                base_priority=task.priority,
            )

            if created_info:
                # Add a dependency from the first subtask to the parent task.
                # Since the parent is in AWAITING_PLAN_APPROVAL (not COMPLETED),
                # this blocks the entire chain from executing until approval.
                first_subtask_id = created_info[0]["id"]
                try:
                    await self.db.add_dependency(first_subtask_id, depends_on=task_id)
                    logger.info(
                        "process_plan: added blocking dep %s → %s (parent)",
                        first_subtask_id,
                        task_id,
                    )
                except Exception as e:
                    logger.warning(
                        "process_plan: failed to add blocking dep %s → %s: %s",
                        first_subtask_id,
                        task_id,
                        e,
                    )

                # Store draft subtask IDs so approve/delete/reject can find them
                import json as _json

                await self.db.add_task_context(
                    task_id,
                    type="plan_draft_subtasks",
                    label="Draft Subtask IDs",
                    content=_json.dumps(created_info),
                )

                logger.info(
                    "process_plan: supervisor created %d draft subtasks for %s",
                    len(created_info),
                    task_id,
                )

        # Build the steps list for the approval embed from supervisor-created
        # subtasks.  If supervisor didn't create any, the embed will show the
        # raw plan content only.
        parsed_steps = [{"title": t["title"], "description": ""} for t in created_info]

        # Emit plan approval notification via the event bus.
        # The DiscordNotificationHandler resolves thread URLs at delivery time.
        try:
            from src.notifications.builder import build_task_detail
            from src.notifications.events import PlanAwaitingApprovalEvent

            current_task = task if not args.get("task_id") else await self.db.get_task(task_id)
            await self.orchestrator._emit_notify(
                "notify.plan_awaiting_approval",
                PlanAwaitingApprovalEvent(
                    task=build_task_detail(current_task),
                    subtasks=parsed_steps,
                    raw_content=raw,
                    project_id=project_id,
                ),
            )
        except Exception as e:
            logger.warning("process_plan: failed to send approval notification: %s", e)

        result = {
            "status": "plan_found",
            "task_id": task_id,
            "project_id": project_id,
            "plan_path": plan_path,
            "title": task.title,
            "phases": len(parsed_steps),
            "draft_subtasks": len(created_info),
            "total_plan_files_found": len(all_plan_files),
        }
        if len(all_plan_files) > 1:
            result["note"] = (
                f"Found {len(all_plan_files)} plan file(s) total across all workspaces. "
                f"Selected the newest one. All will be cleaned up on approve/delete."
            )
        return result

    async def _cmd_set_task_status(self, args: dict) -> dict:
        task_id = args["task_id"]
        new_status = args["status"]
        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        old_status = task.status.value
        await self.db.transition_task(task_id, TaskStatus(new_status), context="admin_set_status")
        return {
            "task_id": task_id,
            "old_status": old_status,
            "new_status": new_status,
            "title": task.title,
        }

    async def _cmd_skip_task(self, args: dict) -> dict:
        """Skip a BLOCKED/FAILED task to unblock its dependency chain."""
        error, unblocked = await self.orchestrator.skip_task(args["task_id"])
        if error:
            return {"error": error}
        return {
            "skipped": args["task_id"],
            "unblocked_count": len(unblocked),
            "unblocked": [{"id": t.id, "title": t.title} for t in unblocked],
        }

    async def _write_archive_note(
        self,
        task,
        result: dict | None,
        dependencies: set[str],
    ) -> str | None:
        """Write a markdown reference note for a task to the data directory.

        Notes are stored under ``~/.agent-queue/archived_tasks/<project_id>/``.
        Returns the file path if written, or ``None`` if the project could not
        be resolved.
        """
        project = await self.db.get_project(task.project_id)
        if not project:
            return None

        archive_dir = os.path.join(self.config.data_dir, "archived_tasks", task.project_id)
        os.makedirs(archive_dir, exist_ok=True)

        note = _build_archive_note(task, result, dependencies)
        archive_path = os.path.join(archive_dir, f"{task.id}.md")
        with open(archive_path, "w") as f:
            f.write(note)
        return archive_path

    async def _cmd_get_chain_health(self, args: dict) -> dict:
        """Check dependency chain health for a task or project."""
        task_id = args.get("task_id")
        project_id = args.get("project_id")

        if task_id:
            task = await self.db.get_task(task_id)
            if not task:
                return {"error": f"Task '{task_id}' not found"}
            if task.status != TaskStatus.BLOCKED:
                return {
                    "task_id": task_id,
                    "status": task.status.value,
                    "stuck_downstream": [],
                    "message": "Task is not blocked — no stuck chain.",
                }
            stuck = await self.orchestrator._find_stuck_downstream(task_id)
            return {
                "task_id": task_id,
                "title": task.title,
                "status": task.status.value,
                "stuck_downstream": [
                    {"id": t.id, "title": t.title, "status": t.status.value} for t in stuck
                ],
                "stuck_count": len(stuck),
            }

        # If project_id given (or fall back to active), list all blocked tasks
        # with stuck chains.
        pid = project_id or self._active_project_id
        blocked_tasks = await self.db.list_tasks(project_id=pid, status=TaskStatus.BLOCKED)
        chains = []
        for bt in blocked_tasks:
            stuck = await self.orchestrator._find_stuck_downstream(bt.id)
            if stuck:
                chains.append(
                    {
                        "blocked_task": {"id": bt.id, "title": bt.title},
                        "stuck_downstream": [{"id": t.id, "title": t.title} for t in stuck],
                        "stuck_count": len(stuck),
                    }
                )
        return {
            "project_id": pid,
            "stuck_chains": chains,
            "total_stuck_chains": len(chains),
        }

    async def _cmd_get_task_result(self, args: dict) -> dict:
        result = await self.db.get_task_result(args["task_id"])
        if not result:
            return {"error": f"No results found for task '{args['task_id']}'"}
        return result

    async def _cmd_get_task_diff(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if not task.branch_name:
            return {"error": "Task has no branch name"}

        # Resolve checkout path from workspaces (locked by this task)
        checkout_path = None
        workspaces = await self.db.list_workspaces(project_id=task.project_id)
        for ws in workspaces:
            if ws.locked_by_task_id == task.id:
                checkout_path = ws.workspace_path
                break
        # Fallback: first workspace for the project
        if not checkout_path and workspaces:
            checkout_path = workspaces[0].workspace_path
        # Legacy fallback: repo source_path
        if not checkout_path and task.repo_id:
            repo = await self.db.get_repo(task.repo_id)
            if repo and repo.source_path:
                checkout_path = repo.source_path
        if not checkout_path:
            return {"error": "Could not determine checkout path for diff"}

        project = await self.db.get_project(task.project_id)
        default_branch = project.repo_default_branch if project else "main"
        diff = await self.orchestrator.git.aget_diff(checkout_path, default_branch)
        if not diff:
            return {"diff": "(no changes)", "branch": task.branch_name}
        return {"diff": diff, "branch": task.branch_name}

    async def _cmd_get_agent_error(self, args: dict) -> dict:
        task_id = args["task_id"]
        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        result = await self.db.get_task_result(task_id)

        info = {
            "task_id": task_id,
            "title": task.title,
            "status": task.status.value,
            "retries": f"{task.retry_count} / {task.max_retries}",
        }

        if not result:
            info["message"] = "No result recorded yet for this task"
            return info

        result_value = result.get("result", "unknown")
        error_msg = result.get("error_message") or ""
        error_type, suggestion = classify_error(error_msg)

        info["result"] = result_value
        info["error_type"] = error_type
        info["error_message"] = error_msg[:2000] if error_msg else None
        info["suggested_fix"] = suggestion
        summary = result.get("summary") or ""
        if summary:
            info["agent_summary"] = summary[:1000]

        return info

    # -----------------------------------------------------------------------
    # Agent commands -- workspace-as-agent model.
    # Agents are derived from project workspaces: each workspace is an
    # agent slot.  CRUD commands (create/delete/pause/resume) are deprecated
    # and return helpful error messages pointing to workspace commands.
    # -----------------------------------------------------------------------

    async def _cmd_list_agents(self, args: dict) -> dict:
        """List agent slots derived from project workspaces.

        Requires ``project_id`` (or active project).  Each workspace is an
        agent slot: locked workspaces are "busy", unlocked are "idle".
        """
        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (or set an active project)"}

        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        workspaces = await self.db.list_workspaces(project_id=project_id)
        agent_list = []
        for ws in workspaces:
            if ws.locked_by_task_id:
                state = "busy"
                task = await self.db.get_task(ws.locked_by_task_id)
                info: dict = {
                    "workspace_id": ws.id,
                    "project_id": project_id,
                    "name": ws.name or ws.id,
                    "state": state,
                    "current_task_id": ws.locked_by_task_id,
                    "current_task_title": task.title if task else None,
                }
            else:
                info = {
                    "workspace_id": ws.id,
                    "project_id": project_id,
                    "name": ws.name or ws.id,
                    "state": "idle",
                    "current_task_id": None,
                    "current_task_title": None,
                }
            agent_list.append(info)
        return {"agents": agent_list, "project_id": project_id}

    async def _cmd_create_agent(self, args: dict) -> dict:
        """Deprecated — agents are now derived from workspaces.

        Use ``add_workspace`` to add agent capacity to a project.
        """
        return {
            "error": (
                "create_agent is no longer supported. Agents are now derived "
                "from project workspaces. Use 'add_workspace' to add agent "
                "capacity to a project."
            )
        }

    async def _cmd_edit_agent(self, args: dict) -> dict:
        """Deprecated — agents are now derived from workspaces."""
        return {
            "error": (
                "edit_agent is no longer supported. Agents are derived from "
                "project workspaces. Use workspace management commands instead."
            )
        }

    async def _cmd_add_workspace(self, args: dict) -> dict:
        """Create a workspace for a project."""
        project_id = args["project_id"]
        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        source = args.get("source", "clone")
        source_type = RepoSourceType(source)
        path = args.get("path")
        name = args.get("name")

        # Generate a human-readable workspace ID up front so it can double
        # as the checkout directory name when no explicit path is given.
        ws_id = await generate_workspace_id(self.db)

        if source_type == RepoSourceType.LINK:
            if not path:
                return {"error": "Link workspaces require a 'path' parameter"}
            # Always store as absolute path to prevent CWD-relative resolution issues
            path = os.path.realpath(path)
            if not os.path.isdir(path):
                return {"error": f"Path '{path}' does not exist or is not a directory"}
            # Reject if path is already a workspace for a different project
            all_ws = await self.db.list_workspaces()
            for ws in all_ws:
                if os.path.realpath(ws.workspace_path) == path and ws.project_id != project_id:
                    return {
                        "error": f"Path '{path}' is already a workspace for "
                        f"project '{ws.project_id}'"
                    }
        elif source_type == RepoSourceType.CLONE:
            if not path:
                # Auto-generate path under workspace_dir/{project_id}/
                # Use explicit name, or the human-readable workspace ID
                ws_dir_name = name or ws_id
                path = os.path.join(
                    self.config.workspace_dir,
                    project_id,
                    ws_dir_name,
                )
            # Always store as absolute path
            path = os.path.realpath(path)
            if project.repo_url:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                try:
                    await self.orchestrator.git.acreate_checkout(project.repo_url, path)
                except Exception as e:
                    return {"error": f"Clone failed: {e}"}
        workspace = Workspace(
            id=ws_id,
            project_id=project_id,
            workspace_path=path,
            source_type=source_type,
            name=name,
        )
        await self.db.create_workspace(workspace)

        # Auto-populate project.repo_url from git remote if not already set
        result: dict = {
            "created": ws_id,
            "project_id": project_id,
            "workspace_path": path,
            "source_type": source,
        }
        if not project.repo_url:
            try:
                remote_url = await self.orchestrator.git.aget_remote_url(path)
                if remote_url:
                    await self.db.update_project(project_id, repo_url=remote_url)
                    result["auto_detected_repo_url"] = remote_url
            except Exception:
                pass  # Non-fatal — workspace was still created successfully
        return result

    async def _cmd_list_workspaces(self, args: dict) -> dict:
        """List workspaces with lock status."""
        project_id = args.get("project_id")
        if not project_id and self._active_project_id:
            project_id = self._active_project_id
        workspaces = await self.db.list_workspaces(project_id=project_id)
        return {
            "workspaces": [
                {
                    "id": ws.id,
                    "project_id": ws.project_id,
                    "workspace_path": ws.workspace_path,
                    "source_type": ws.source_type.value,
                    "name": ws.name,
                    "locked_by_agent_id": ws.locked_by_agent_id,
                    "locked_by_task_id": ws.locked_by_task_id,
                }
                for ws in workspaces
            ]
        }

    async def _cmd_remove_workspace(self, args: dict) -> dict:
        """Delete a workspace by ID or name."""
        workspace_ref = args.get("workspace_id") or args.get("workspace")
        if not workspace_ref:
            return {"error": "workspace_id or workspace is required"}

        # Try by ID first
        ws = await self.db.get_workspace(workspace_ref)

        # If not found by ID, try by name within a project
        if not ws:
            project_id = args.get("project_id") or self._active_project_id
            if project_id:
                ws = await self.db.get_workspace_by_name(project_id, workspace_ref)

        if not ws:
            return {"error": f"Workspace '{workspace_ref}' not found"}
        if ws.locked_by_agent_id:
            return {
                "error": f"Workspace '{ws.id}' is locked by agent "
                f"'{ws.locked_by_agent_id}'. Release it first."
            }
        await self.db.delete_workspace(ws.id)
        return {
            "deleted": ws.id,
            "name": ws.name,
            "project_id": ws.project_id,
            "workspace_path": ws.workspace_path,
        }

    async def _cmd_release_workspace(self, args: dict) -> dict:
        """Admin force-release a stuck workspace lock."""
        workspace_id = args["workspace_id"]
        ws = await self.db.get_workspace(workspace_id)
        if not ws:
            return {"error": f"Workspace '{workspace_id}' not found"}
        if not ws.locked_by_agent_id:
            return {"workspace_id": workspace_id, "status": "already_unlocked"}
        await self.db.release_workspace(workspace_id)
        return {
            "workspace_id": workspace_id,
            "released_from_agent": ws.locked_by_agent_id,
            "released_from_task": ws.locked_by_task_id,
        }

    async def _cmd_find_merge_conflict_workspaces(self, args: dict) -> dict:
        """Scan project workspaces for branches with merge conflicts against main.

        For each workspace, runs ``git merge-tree`` checks on all remote
        branches to detect conflicts without touching the worktree.  Returns a
        list of workspaces that contain conflicting branches, along with
        conflict details (branch name, conflicting files, commits behind).

        This enables the chat agent to create a task with
        ``preferred_workspace_id`` so the orchestrator assigns the exact
        workspace that needs conflict resolution instead of a random one.
        """
        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        workspaces = await self.db.list_workspaces(project_id=project_id)
        if not workspaces:
            return {"error": f"No workspaces found for project '{project_id}'"}

        default_branch = project.repo_default_branch or "main"
        results: list[dict] = []

        for ws in workspaces:
            ws_path = ws.workspace_path
            if not os.path.isdir(ws_path):
                continue

            # Check if this is a valid git repository
            git_dir = os.path.join(ws_path, ".git")
            if not os.path.exists(git_dir):
                continue

            try:
                # Fetch latest remote state
                await _run_subprocess(
                    "git",
                    "fetch",
                    "origin",
                    "--prune",
                    "--quiet",
                    cwd=ws_path,
                    timeout=30,
                )

                main_ref = f"origin/{default_branch}"

                # Verify main exists
                check_rc, _, _ = await _run_subprocess(
                    "git",
                    "rev-parse",
                    main_ref,
                    cwd=ws_path,
                    timeout=10,
                )
                if check_rc != 0:
                    continue

                # Get current branch
                cb_rc, cb_stdout, _ = await _run_subprocess(
                    "git",
                    "rev-parse",
                    "--abbrev-ref",
                    "HEAD",
                    cwd=ws_path,
                    timeout=10,
                )
                current_branch = cb_stdout.strip() if cb_rc == 0 else "unknown"

                # Check for uncommitted merge conflict markers in working tree
                has_working_tree_conflict = False
                status_rc, status_stdout, _ = await _run_subprocess(
                    "git",
                    "status",
                    "--porcelain",
                    cwd=ws_path,
                    timeout=10,
                )
                if status_rc == 0:
                    for line in status_stdout.splitlines():
                        if (
                            line.startswith("UU ")
                            or line.startswith("AA ")
                            or line.startswith("DD ")
                        ):
                            has_working_tree_conflict = True
                            break

                # List remote branches and check each for merge conflicts
                br_rc, br_stdout, _ = await _run_subprocess(
                    "git",
                    "branch",
                    "-r",
                    "--list",
                    "origin/*",
                    cwd=ws_path,
                    timeout=10,
                )
                if br_rc != 0:
                    continue

                branch_conflicts: list[dict] = []

                for line in br_stdout.splitlines():
                    branch_ref = line.strip()
                    if not branch_ref:
                        continue

                    branch_name = branch_ref.removeprefix("origin/")

                    # Skip main, HEAD, and dependabot branches
                    if branch_name in (default_branch, "HEAD") or branch_name.startswith(
                        "dependabot/"
                    ):
                        continue
                    if " -> " in branch_ref:
                        continue

                    # Find merge base
                    mb_rc, mb_stdout, _ = await _run_subprocess(
                        "git",
                        "merge-base",
                        main_ref,
                        branch_ref,
                        cwd=ws_path,
                        timeout=10,
                    )
                    if mb_rc != 0:
                        continue
                    merge_base = mb_stdout.strip()

                    # Use merge-tree to check for conflicts
                    _, mt_stdout, _ = await _run_subprocess(
                        "git",
                        "merge-tree",
                        merge_base,
                        main_ref,
                        branch_ref,
                        cwd=ws_path,
                        timeout=10,
                    )
                    merge_output = mt_stdout

                    if "+<<<<<<< " in merge_output:
                        # Extract conflicting files
                        conflicting_files = []
                        for mline in merge_output.splitlines():
                            if mline.startswith("changed in both"):
                                conflicting_files.append(
                                    mline.replace("changed in both", "").strip()
                                )

                        # Extract task ID from branch name
                        if "/" in branch_name:
                            task_id_part = branch_name.split("/")[0]
                        else:
                            task_id_part = branch_name

                        # Commits behind main
                        behind_rc, behind_stdout, _ = await _run_subprocess(
                            "git",
                            "rev-list",
                            "--count",
                            f"{branch_ref}..{main_ref}",
                            cwd=ws_path,
                            timeout=10,
                        )
                        behind_count = behind_stdout.strip() if behind_rc == 0 else "?"

                        branch_conflicts.append(
                            {
                                "branch": branch_name,
                                "task_id": task_id_part,
                                "conflicting_files": conflicting_files or ["unknown"],
                                "commits_behind_main": behind_count,
                            }
                        )

                if branch_conflicts or has_working_tree_conflict:
                    results.append(
                        {
                            "workspace_id": ws.id,
                            "workspace_name": ws.name,
                            "workspace_path": ws_path,
                            "current_branch": current_branch,
                            "locked_by_task_id": ws.locked_by_task_id,
                            "locked_by_agent_id": ws.locked_by_agent_id,
                            "has_working_tree_conflict": has_working_tree_conflict,
                            "branch_conflicts": branch_conflicts,
                        }
                    )

            except (asyncio.TimeoutError, OSError) as e:
                logging.getLogger(__name__).warning(
                    "Error scanning workspace %s for conflicts: %s",
                    ws_path,
                    e,
                )
                continue

        return {
            "project_id": project_id,
            "workspaces_scanned": len(workspaces),
            "workspaces_with_conflicts": len(results),
            "conflicts": results,
        }

    async def _cmd_queue_sync_workspaces(self, args: dict) -> dict:
        """Queue a high-priority Sync Workspaces task that orchestrates a full sync workflow.

        When the orchestrator picks up this task, it will:
        1. Pause the project (prevent new tasks from being queued).
        2. Wait for ALL currently running tasks to complete.
        3. Launch a Claude Code agent task to merge all feature branches
           into the default branch across all project workspaces.
        4. Resume the project after synchronization is complete.

        This is used for periodic workspace consolidation when feature branches
        have drifted from the default branch.

        Early-out conditions (no task queued):
        - A SYNC task already exists for this project (READY/ASSIGNED/IN_PROGRESS).
        - All workspaces are already on the default branch with no feature branches.
        """
        from src.task_names import generate_task_id

        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        workspaces = await self.db.list_workspaces(project_id=project_id)
        if not workspaces:
            return {"error": f"No workspaces found for project '{project_id}'"}

        default_branch = project.repo_default_branch or "main"

        # ── Early-out: duplicate sync task ─────────────────────────────
        # If there's already a SYNC task queued or running for this project,
        # don't create another one.
        active_tasks = await self.db.list_active_tasks(
            project_id=project_id,
            exclude_statuses={TaskStatus.COMPLETED, TaskStatus.FAILED},
        )
        existing_sync = [
            t
            for t in active_tasks
            if t.task_type == TaskType.SYNC
            and t.status in (TaskStatus.READY, TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS)
        ]
        if existing_sync:
            existing_ids = ", ".join(t.id for t in existing_sync)
            return {
                "already_queued": True,
                "existing_task_ids": [t.id for t in existing_sync],
                "project_id": project_id,
                "message": (
                    f"Sync Workspaces task already exists for '{project_id}': {existing_ids}. "
                    f"Skipping duplicate."
                ),
            }

        # ── Early-out: workspaces already synced ───────────────────────
        # Check if all workspaces are already on the default branch with no
        # feature branches.  If so, there's nothing to merge — skip the sync.
        git = self.orchestrator.git
        needs_sync = False
        workspace_details = []
        for ws in workspaces:
            ws_path = ws.workspace_path
            if not os.path.isdir(ws_path):
                # Workspace path doesn't exist — can't check, assume needs sync
                needs_sync = True
                workspace_details.append(f"  - {ws_path}: directory not found")
                break
            try:
                current = await git.aget_current_branch(ws_path)
                branches = await git.alist_branches(ws_path)
                # alist_branches returns lines like "* main", "  feature-x"
                # Strip the leading "* " or "  " to get clean branch names.
                clean_branches = [b.lstrip("* ").strip() for b in branches if b.strip()]
                non_default = [b for b in clean_branches if b and b != default_branch]
                if current != default_branch or non_default:
                    needs_sync = True
                    workspace_details.append(
                        f"  - {ws_path}: branch={current}, other_branches={non_default or '(none)'}"
                    )
                    break
                workspace_details.append(f"  - {ws_path}: on {default_branch}, no feature branches")
            except Exception:
                # Git error — can't determine state, assume needs sync
                needs_sync = True
                workspace_details.append(f"  - {ws_path}: git check failed")
                break

        if not needs_sync:
            return {
                "already_synced": True,
                "project_id": project_id,
                "workspace_count": len(workspaces),
                "default_branch": default_branch,
                "message": (
                    f"All {len(workspaces)} workspace(s) for '{project_id}' are already on "
                    f"'{default_branch}' with no feature branches. No sync needed."
                ),
            }

        workspace_root = "/home/jkern/agent-queue-workspaces"

        # Build a self-contained description with all context for the sync workflow.
        workspace_paths = "\n".join(
            f"  - {ws.workspace_path} (id: {ws.id}, name: {ws.name or '—'})" for ws in workspaces
        )

        description = f"""## Sync Workspaces — {project_id}

**Task Type:** Orchestrator-managed sync workflow (handled automatically by the orchestrator)

### Workflow Steps (executed by the orchestrator, NOT the agent):
1. **Pause Project** — Prevent new tasks from being queued for `{project_id}`
2. **Wait for Active Tasks** — Monitor all running tasks and wait for completion
3. **Merge Feature Branches** — Launch a Claude Code agent to merge all feature work
4. **Cleanup & Resume** — Unlock workspaces and resume the project

### Project Details:
- **Project ID:** {project_id}
- **Default Branch:** {default_branch}
- **Workspace Root:** {workspace_root}

### Workspaces:
{workspace_paths}

### Merge Strategy:
- Merge and push one workspace at a time
- Pull updates before working on subsequent workspaces
- Preserve ALL feature work unless it's duplicated or made irrelevant by subsequent changes
- Resolve conflicts intelligently, preferring to preserve feature functionality
- Ensure each workspace ends up on `{default_branch}` with no remaining feature branches from agent-queue

### Why This Exists:
This synchronizes workspaces that have drifted from the default branch, consolidating
feature work stuck on feature branches across multiple workspaces.
"""

        task_id = await generate_task_id(self.db)
        task = Task(
            id=task_id,
            project_id=project_id,
            title=f"Sync Workspaces — {project_id}",
            description=description,
            priority=1,  # Highest priority
            status=TaskStatus.READY,
            task_type=TaskType.SYNC,
        )
        await self.db.create_task(task)

        return {
            "queued": task_id,
            "project_id": project_id,
            "title": task.title,
            "priority": 1,
            "workspace_count": len(workspaces),
            "default_branch": default_branch,
            "message": (
                f"Sync Workspaces task queued with highest priority (id: {task_id}). "
                f"When it starts, it will pause the project, wait for active tasks to "
                f"complete, merge all feature branches into '{default_branch}', then resume."
            ),
        }

    async def _cmd_pause_agent(self, args: dict) -> dict:
        """Deprecated — agents are now derived from workspaces."""
        return {
            "error": (
                "pause_agent is no longer supported. Agents are derived from "
                "project workspaces. To pause work, pause the project instead."
            )
        }

    async def _cmd_resume_agent(self, args: dict) -> dict:
        """Deprecated — agents are now derived from workspaces."""
        return {
            "error": (
                "resume_agent is no longer supported. Agents are derived from "
                "project workspaces. To resume work, resume the project instead."
            )
        }

    async def _cmd_delete_agent(self, args: dict) -> dict:
        """Deprecated — agents are now derived from workspaces."""
        return {
            "error": (
                "delete_agent is no longer supported. Agents are derived from "
                "project workspaces. Use 'remove_workspace' to remove agent "
                "capacity from a project."
            )
        }

    # -----------------------------------------------------------------------
    # Events and token usage -- observability into system activity and
    # LLM token consumption, broken down by project, task, or agent.
    # -----------------------------------------------------------------------

    async def _cmd_get_recent_events(self, args: dict) -> dict:
        limit = args.get("limit", 10)
        events = await self.db.get_recent_events(limit=limit)
        return {"events": events}

    async def _cmd_get_token_usage(self, args: dict) -> dict:
        project_id = args.get("project_id")
        task_id = args.get("task_id")

        if task_id:
            cursor = await self.db._db.execute(
                "SELECT agent_id, SUM(tokens_used) as total, COUNT(*) as entries "
                "FROM token_ledger WHERE task_id = ? GROUP BY agent_id",
                (task_id,),
            )
            rows = await cursor.fetchall()
            return {
                "task_id": task_id,
                "breakdown": [
                    {"agent_id": r["agent_id"], "tokens": r["total"], "entries": r["entries"]}
                    for r in rows
                ],
                "total": sum(r["total"] for r in rows),
            }
        elif project_id:
            cursor = await self.db._db.execute(
                "SELECT task_id, agent_id, SUM(tokens_used) as total "
                "FROM token_ledger WHERE project_id = ? "
                "GROUP BY task_id, agent_id ORDER BY total DESC",
                (project_id,),
            )
            rows = await cursor.fetchall()
            return {
                "project_id": project_id,
                "breakdown": [
                    {"task_id": r["task_id"], "agent_id": r["agent_id"], "tokens": r["total"]}
                    for r in rows
                ],
                "total": sum(r["total"] for r in rows),
            }
        else:
            cursor = await self.db._db.execute(
                "SELECT project_id, SUM(tokens_used) as total "
                "FROM token_ledger GROUP BY project_id ORDER BY total DESC",
            )
            rows = await cursor.fetchall()
            return {
                "breakdown": [{"project_id": r["project_id"], "tokens": r["total"]} for r in rows],
                "total": sum(r["total"] for r in rows),
            }

    async def _cmd_token_audit(self, args: dict) -> dict:
        days = args.get("days", 7)
        project_id = args.get("project_id")
        return await self.db.get_token_audit(days=days, project_id=project_id)

    # -----------------------------------------------------------------------
    # Git commands -- full git workflow via GitManager.
    # Two generations of git commands coexist here: the newer "git_*" set
    # (git_commit, git_push, etc.) and the older "create_branch",
    # "checkout_branch" wrappers.  Both delegate to GitManager for the
    # actual git operations.  All commands use _resolve_repo_path to find
    # the correct checkout directory before invoking git.
    # -----------------------------------------------------------------------

    # _cmd_get_git_status → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_git_commit → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_git_pull → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_git_push → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_git_create_branch → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_git_merge → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_git_create_pr → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_create_github_repo → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_generate_readme → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_git_changed_files → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_git_log → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_git_branch → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_git_checkout → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_git_diff → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_create_branch → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_checkout_branch → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_commit_changes → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_push_branch → moved to src/plugins/internal/git.py (aq-git plugin)
    # _cmd_merge_branch → moved to src/plugins/internal/git.py (aq-git plugin)
    # _git_ahead_behind, _git_stash_count, _git_diff_stat, _warn_if_in_progress
    #   → moved to src/plugins/internal/git.py (aq-git plugin)

    # -----------------------------------------------------------------------
    # Hook commands -- CRUD plus manual firing.
    # Hooks are automated routines that fire on events (e.g. task completion)
    # or on a schedule.  They gather context via shell/file/HTTP steps and
    # optionally invoke an LLM with full tool access to take corrective
    # actions (like creating fix-up tasks when tests fail).
    # -----------------------------------------------------------------------

    async def _cmd_fire_hook(self, args: dict) -> dict:
        """Internal: fire a single hook. Used by _cmd_fire_rule."""
        hook_id = args["hook_id"]
        hooks_engine = self.orchestrator.hooks
        if not hooks_engine:
            return {"error": "Hook engine is not enabled"}
        try:
            await hooks_engine.fire_hook(hook_id)
            return {"fired": hook_id, "status": "running"}
        except ValueError as e:
            return {"error": str(e)}

    # Hook CRUD commands removed — hooks are now an internal implementation
    # detail managed through rules. See _cmd_fire_rule, _cmd_rule_runs,
    # _cmd_toggle_rule, and _cmd_refresh_rules above.

    # -----------------------------------------------------------------------
    # Rule commands -- persistent autonomous behaviors stored as markdown.
    # Rules are the source of truth; active rules generate hooks.
    # -----------------------------------------------------------------------

    async def _cmd_save_rule(self, args: dict) -> dict:
        """Create or update a rule."""
        rm = getattr(self.orchestrator, "rule_manager", None)
        if not rm:
            return {"error": "Rule manager not initialized"}

        rule_id = args.get("id")
        project_id = args.get("project_id")
        rule_type = args.get("type", "passive")
        content = args.get("content", "")

        if not content:
            return {"error": "content is required"}
        if rule_type not in ("active", "passive"):
            return {"error": f"type must be 'active' or 'passive', got '{rule_type}'"}

        result = await rm.async_save_rule(
            id=rule_id,
            project_id=project_id,
            rule_type=rule_type,
            content=content,
        )
        return result

    async def _cmd_delete_rule(self, args: dict) -> dict:
        """Delete a rule and its associated hooks."""
        rm = getattr(self.orchestrator, "rule_manager", None)
        if not rm:
            return {"error": "Rule manager not initialized"}

        rule_id = args.get("id")
        if not rule_id:
            return {"error": "id is required"}

        return await rm.async_delete_rule(rule_id)

    async def _cmd_browse_rules(self, args: dict) -> dict:
        """List rules for a project (plus globals). Alias: list_rules."""
        rm = getattr(self.orchestrator, "rule_manager", None)
        if not rm:
            return {"error": "Rule manager not initialized"}

        project_id = args.get("project_id")
        rules = rm.browse_rules(project_id)

        # Enrich each rule with execution info
        all_hooks = await self.db.list_hooks()
        hooks_by_rule: dict[str, list] = {}
        for h in all_hooks:
            for rule in rules:
                prefix = f"rule-{rule['id']}-"
                if h.id.startswith(prefix):
                    hooks_by_rule.setdefault(rule["id"], []).append(h)
                    break

        for rule in rules:
            rule_hooks = hooks_by_rule.get(rule["id"], [])
            if rule_hooks:
                all_enabled = all(h.enabled for h in rule_hooks)
                all_disabled = all(not h.enabled for h in rule_hooks)
                last_runs = [h.last_triggered_at for h in rule_hooks if h.last_triggered_at]
                rule["enabled"] = (
                    all_enabled if all_enabled else (False if all_disabled else "mixed")
                )
                rule["last_run"] = max(last_runs) if last_runs else None
            else:
                rule["enabled"] = None
                rule["last_run"] = None

        return {"rules": rules}

    async def _cmd_list_rules(self, args: dict) -> dict:
        """Alias for browse_rules — primary interface name."""
        return await self._cmd_browse_rules(args)

    async def _cmd_load_rule(self, args: dict) -> dict:
        """Load full details of a specific rule, including execution info."""
        rm = getattr(self.orchestrator, "rule_manager", None)
        if not rm:
            return {"error": "Rule manager not initialized"}

        rule_id = args.get("id")
        if not rule_id:
            return {"error": "id is required"}

        loaded = rm.load_rule(rule_id)
        if not loaded:
            return {"error": f"Rule '{rule_id}' not found"}

        # Enrich with execution info from hooks
        prefix = f"rule-{rule_id}-"
        hooks = await self.db.list_hooks_by_id_prefix(prefix)
        if hooks:
            all_enabled = all(h.enabled for h in hooks)
            all_disabled = all(not h.enabled for h in hooks)
            last_runs = [h.last_triggered_at for h in hooks if h.last_triggered_at]
            loaded["execution_info"] = {
                "enabled": all_enabled if all_enabled else (False if all_disabled else "mixed"),
                "hook_count": len(hooks),
                "last_run": max(last_runs) if last_runs else None,
            }

        return loaded

    async def _cmd_refresh_hooks(self, args: dict) -> dict:
        """Backward compat alias — delegates to refresh_rules."""
        return await self._cmd_refresh_rules(args)

    async def _cmd_refresh_rules(self, args: dict) -> dict:
        """Reconcile hooks from current rule files.

        Re-reads all rule files and regenerates hooks for active rules,
        ensuring the hook engine matches the rules on disk.
        """
        rm = getattr(self.orchestrator, "rule_manager", None)
        if not rm:
            return {"error": "Rule manager not initialized"}

        stats = await rm.reconcile()
        return {
            "success": True,
            "rules_scanned": stats.get("rules_scanned", 0),
            "active_rules": stats.get("active_rules", 0),
            "hooks_regenerated": stats.get("hooks_regenerated", 0),
            "hooks_unchanged": stats.get("hooks_unchanged", 0),
            "errors": stats.get("errors", 0),
        }

    async def _cmd_fire_rule(self, args: dict) -> dict:
        """Manually trigger all hooks for a rule."""
        rm = getattr(self.orchestrator, "rule_manager", None)
        if not rm:
            return {"error": "Rule manager not initialized"}

        rule_id = args.get("id")
        if not rule_id:
            return {"error": "id is required"}

        loaded = rm.load_rule(rule_id)
        if not loaded:
            return {"error": f"Rule '{rule_id}' not found"}

        hook_ids = loaded.get("hooks", [])
        if not hook_ids:
            return {"error": f"Rule '{rule_id}' has no generated hooks"}

        hooks_engine = self.orchestrator.hooks
        if not hooks_engine:
            return {"error": "Hook engine is not enabled"}

        fired = []
        skipped = []
        for hid in hook_ids:
            try:
                await hooks_engine.fire_hook(hid)
                fired.append(hid)
            except ValueError:
                skipped.append(hid)

        return {
            "rule_id": rule_id,
            "fired": len(fired),
            "skipped": len(skipped),
            "total": len(hook_ids),
        }

    async def _cmd_rule_runs(self, args: dict) -> dict:
        """Show recent execution history for a rule's hooks."""
        rm = getattr(self.orchestrator, "rule_manager", None)
        if not rm:
            return {"error": "Rule manager not initialized"}

        rule_id = args.get("id")
        if not rule_id:
            return {"error": "id is required"}

        loaded = rm.load_rule(rule_id)
        if not loaded:
            return {"error": f"Rule '{rule_id}' not found"}

        limit = args.get("limit", 10)
        prefix = f"rule-{rule_id}-"
        runs = await self.db.list_hook_runs_by_prefix(prefix, limit=limit)

        run_list = []
        for r in runs:
            run_list.append(
                {
                    "id": r.id,
                    "hook_id": r.hook_id,
                    "project_id": r.project_id,
                    "status": r.status,
                    "trigger_reason": r.trigger_reason,
                    "tokens_used": r.tokens_used,
                    "skipped_reason": r.skipped_reason,
                    "started_at": r.started_at,
                    "completed_at": r.completed_at,
                }
            )

        return {
            "rule_id": rule_id,
            "rule_name": loaded.get("name", rule_id),
            "runs": run_list,
        }

    async def _cmd_toggle_rule(self, args: dict) -> dict:
        """Enable or disable all hooks for a rule."""
        rm = getattr(self.orchestrator, "rule_manager", None)
        if not rm:
            return {"error": "Rule manager not initialized"}

        rule_id = args.get("id")
        if not rule_id:
            return {"error": "id is required"}

        enabled = args.get("enabled")
        if enabled is None:
            return {"error": "enabled is required (true or false)"}

        loaded = rm.load_rule(rule_id)
        if not loaded:
            return {"error": f"Rule '{rule_id}' not found"}

        prefix = f"rule-{rule_id}-"
        hooks = await self.db.list_hooks_by_id_prefix(prefix)
        updated = 0
        for hook in hooks:
            if hook.enabled != enabled:
                await self.db.update_hook(hook.id, enabled=enabled)
                updated += 1

        return {
            "rule_id": rule_id,
            "action": "enabled" if enabled else "disabled",
            "hooks_updated": updated,
            "total_hooks": len(hooks),
        }

    # -----------------------------------------------------------------------
    # Notes commands -- markdown documents stored in project workspaces.
    # Notes are a lightweight knowledge base: users and hooks can write
    # specs, brainstorms, or analysis, and later turn them into tasks.
    # Stored as plain .md files under <data_dir>/vault/projects/<project_id>/notes/.
    # -----------------------------------------------------------------------

    def _get_notes_dir(self, project_id: str) -> str:
        """Return the central notes directory for a project.

        Notes live in the vault at ``vault/projects/{project_id}/notes/``.
        """
        return os.path.join(self.config.data_dir, "vault", "projects", project_id, "notes")

    def _resolve_note_path(self, notes_dir: str, title: str) -> str | None:
        """Resolve a note file path from a title, filename, or slug.

        Tries in order:
        1. Exact filename match (e.g. "keen-beacon-splitting-analysis.md")
        2. Filename without .md extension (e.g. "keen-beacon-splitting-analysis")
        3. Slugified title (e.g. "Analysis: Why keen-beacon Was Not Split" → slug)

        Returns the full file path if found, None otherwise.
        """
        # 1. Exact filename
        if title.endswith(".md"):
            fpath = os.path.join(notes_dir, title)
            if os.path.isfile(fpath):
                return fpath

        # 2. Title as filename without extension
        fpath = os.path.join(notes_dir, f"{title}.md")
        if os.path.isfile(fpath):
            return fpath

        # 3. Slugified title
        slug = self.orchestrator.git.slugify(title)
        if slug:
            fpath = os.path.join(notes_dir, f"{slug}.md")
            if os.path.isfile(fpath):
                return fpath

        return None

    # _cmd_list_notes → moved to src/plugins/internal/notes.py (aq-notes plugin)

    # _cmd_write_note → moved to src/plugins/internal/notes.py (aq-notes plugin)

    # _cmd_read_note → moved to src/plugins/internal/notes.py (aq-notes plugin)

    # _cmd_append_note → moved to src/plugins/internal/notes.py (aq-notes plugin)
    # _cmd_compare_specs_notes → moved to src/plugins/internal/notes.py (aq-notes plugin)
    # _cmd_delete_note → moved to src/plugins/internal/notes.py (aq-notes plugin)
    # _cmd_promote_note → moved to src/plugins/internal/notes.py (aq-notes plugin)
    # _trigger_note_profile_revision → moved to src/plugins/internal/notes.py (aq-notes plugin)

    # -----------------------------------------------------------------------
    # Memory commands -- semantic search, stats, and reindex for the
    # memsearch-powered project memory subsystem.  These delegate to
    # MemoryManager on the orchestrator.  When memory is not enabled or
    # memsearch is not installed, commands return informative errors.
    # -----------------------------------------------------------------------

    # _cmd_memory_search → moved to src/plugins/internal/memory.py (aq-memory plugin)
    # _cmd_memory_stats → moved to src/plugins/internal/memory.py (aq-memory plugin)
    # _cmd_memory_reindex → moved to src/plugins/internal/memory.py (aq-memory plugin)
    # _cmd_view_profile → moved to src/plugins/internal/memory.py (aq-memory plugin)
    # _cmd_edit_project_profile → moved to src/plugins/internal/memory.py (aq-memory plugin)
    # _cmd_regenerate_profile → moved to src/plugins/internal/memory.py (aq-memory plugin)
    # _cmd_compact_memory → moved to src/plugins/internal/memory.py (aq-memory plugin)

    async def _cmd_recall_topic_context(self, args: dict) -> dict:
        """Load L2 topic context on-demand when a topic emerges mid-task.

        Detects topics from the provided text (or uses explicit topic names),
        then loads matching knowledge files and memories.  Returns formatted
        context ready for injection into the agent's working memory.

        This implements roadmap 3.3.6 — agents call this tool when they
        encounter a new topic during execution rather than relying solely on
        the initial topic context loaded at task start.
        """
        memory_mgr = getattr(self.orchestrator, "memory_manager", None)
        if not memory_mgr:
            return {"error": "Memory system is not available."}

        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required."}

        text = args.get("text", "")
        topics = args.get("topics")
        exclude_topics = args.get("exclude_topics")

        if not text and not topics:
            return {"error": "Either 'text' or 'topics' must be provided."}

        result = await memory_mgr.load_topic_context_on_demand(
            project_id,
            text,
            topics=topics,
            exclude_topics=exclude_topics,
        )
        return result

    # -----------------------------------------------------------------------
    # Prompt template commands -- read-only browsing of prompt templates
    # stored in <workspace>/prompts/.  Templates use YAML frontmatter for
    # metadata and Mustache-style {{variable}} placeholders for context
    # injection.  Modifications should only be done through tasks, not
    # through these commands.
    # -----------------------------------------------------------------------

    def _get_prompt_manager(self, workspace: str):
        """Create a PromptManager for the given workspace."""
        from src.prompt_manager import PromptManager

        prompts_dir = os.path.join(workspace, "prompts")
        return PromptManager(prompts_dir)

    async def _cmd_list_prompts(self, args: dict) -> dict:
        """List all prompt templates for a project, optionally filtered."""
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {
                "error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."
            }
        pm = self._get_prompt_manager(workspace)
        templates = pm.list_templates(
            category=args.get("category"),
            tag=args.get("tag"),
        )
        return {
            "project_id": args["project_id"],
            "prompts": [t.to_dict() for t in templates],
            "categories": pm.get_categories(),
            "total": len(templates),
        }

    async def _cmd_read_prompt(self, args: dict) -> dict:
        """Read a specific prompt template's content and metadata."""
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {
                "error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."
            }
        pm = self._get_prompt_manager(workspace)
        tmpl = pm.get_template(args["name"])
        if not tmpl:
            return {"error": f"Prompt template '{args['name']}' not found"}
        result = tmpl.to_dict()
        result["content"] = tmpl.body
        return result

    async def _cmd_render_prompt(self, args: dict) -> dict:
        """Render a prompt template with variable substitution."""
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {
                "error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."
            }
        pm = self._get_prompt_manager(workspace)
        variables = args.get("variables", {})
        rendered = pm.render(args["name"], variables)
        if rendered is None:
            return {"error": f"Prompt template '{args['name']}' not found"}
        return {
            "name": args["name"],
            "rendered": rendered,
            "variables_used": variables,
        }

    # -----------------------------------------------------------------------
    # System / control commands -- orchestrator pause/resume, active project
    # switching, and daemon restart.  These affect the global state of the
    # system rather than any single project or task.
    # -----------------------------------------------------------------------

    async def _cmd_set_active_project(self, args: dict) -> dict:
        pid = args.get("project_id")
        if pid:
            project = await self.db.get_project(pid)
            if not project:
                return {"error": f"Project '{pid}' not found"}
            self._active_project_id = pid
            return {"active_project": pid, "name": project.name}
        else:
            self._active_project_id = None
            return {"active_project": None, "message": "Active project cleared"}

    async def _cmd_orchestrator_control(self, args: dict) -> dict:
        action = args["action"]
        orch = self.orchestrator
        if action == "pause":
            orch.pause()
            return {
                "status": "paused",
                "message": "Orchestrator paused — no new tasks will be scheduled",
            }
        elif action == "resume":
            orch.resume()
            return {"status": "running", "message": "Orchestrator resumed"}
        else:  # status
            running = len(orch._running_tasks)
            return {
                "status": "paused" if orch._paused else "running",
                "running_tasks": running,
            }

    async def _cmd_shutdown(self, args: dict) -> dict:
        """Shut down the bot and all running agents.

        Supports two modes:
        - graceful (default): waits for current tasks to complete before exiting
        - force: immediately stops all running agents and exits

        The process exits with code 0 (no restart) rather than SIGTERM
        which the daemon supervisor would interpret as a restart request.
        """
        reason = args.get("reason", "No reason provided")
        force = args.get("force", False)
        mode = "force" if force else "graceful"

        # Log the shutdown event
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        shutdown_msg = (
            f"🛑 **Daemon shutdown initiated** ({mode})\n"
            f"**Reason:** {reason}\n"
            f"**Time:** {timestamp}"
        )
        await self.orchestrator._emit_text_notify(shutdown_msg)

        orch = self.orchestrator

        if force:
            # Force-stop all running tasks immediately
            running_task_ids = list(orch._running_tasks.keys())
            for task_id in running_task_ids:
                try:
                    await orch.stop_task(task_id)
                except Exception as e:
                    logger.warning("Error force-stopping task %s: %s", task_id, e)
        else:
            # Graceful: pause orchestrator so no new tasks are started,
            # then wait for running tasks to finish
            orch._paused = True
            await orch.wait_for_running_tasks(timeout=300)

        # Note: bot status is set to offline by the slash command caller
        # before invoking this handler.

        # Exit without restart — use os._exit(0) after a brief delay
        # to allow the Discord response to be sent
        async def _delayed_exit():
            await asyncio.sleep(2)
            # Don't set _restart_requested — this is a full shutdown, not restart
            os._exit(0)

        asyncio.ensure_future(_delayed_exit())

        return {
            "status": "shutting_down",
            "mode": mode,
            "reason": reason,
            "timestamp": timestamp,
            "tasks_stopped": len(orch._running_tasks) if force else 0,
        }

    async def _cmd_restart_daemon(self, args: dict) -> dict:
        reason = args.get("reason", "No reason provided")
        wait_for_tasks = args.get("wait_for_tasks", False)
        orch = self.orchestrator

        running_count = len(orch._running_tasks)

        if wait_for_tasks and running_count > 0:
            # Pause orchestrator so no new tasks are scheduled, then wait
            orch._paused = True
            await orch._emit_text_notify(
                f"🔄 **Daemon restart pending** — waiting for {running_count} "
                f"running task(s) to complete\n**Reason:** {reason}"
            )
            await orch.wait_for_running_tasks(timeout=300)

        # Log the restart reason to the notification channel before restarting
        await orch._emit_text_notify(f"🔄 **Daemon restart initiated** — {reason}")
        orch._restart_requested = True
        os.kill(os.getpid(), signal.SIGTERM)
        return {
            "status": "restarting",
            "message": "Daemon restart initiated",
            "reason": reason,
            "waited_for_tasks": wait_for_tasks and running_count > 0,
        }

    async def _cmd_update_and_restart(self, args: dict) -> dict:
        """Pull the latest source from git and restart the daemon."""
        reason = args.get("reason", "No reason provided")
        wait_for_tasks = args.get("wait_for_tasks", False)
        orch = self.orchestrator
        # Determine the repo root (where this source lives)
        repo_dir = str(Path(__file__).resolve().parent.parent)

        # git pull
        pull_rc, pull_stdout, pull_stderr = await _run_subprocess(
            "git",
            "pull",
            "--ff-only",
            cwd=repo_dir,
            timeout=30,
        )
        if pull_rc != 0:
            stderr = pull_stderr.strip() or pull_stdout.strip()
            return {"error": f"git pull failed: {stderr}"}

        pull_output = pull_stdout.strip()

        # pip install -e . to pick up any dependency changes
        pip_rc, pip_stdout, pip_stderr = await _run_subprocess(
            "pip",
            "install",
            "-e",
            ".",
            cwd=repo_dir,
            timeout=120,
        )
        if pip_rc != 0:
            stderr = pip_stderr.strip() or pip_stdout.strip()
            return {"error": f"pip install failed: {stderr}"}

        running_count = len(orch._running_tasks)

        if wait_for_tasks and running_count > 0:
            # Pause orchestrator so no new tasks are scheduled, then wait
            orch._paused = True
            await orch._emit_text_notify(
                f"🔄 **Daemon update & restart pending** — waiting for {running_count} "
                f"running task(s) to complete\n**Reason:** {reason}"
            )
            await orch.wait_for_running_tasks(timeout=300)

        # Log the update/restart reason to the notification channel
        await orch._emit_text_notify(f"🔄 **Daemon update & restart initiated** — {reason}")
        # Trigger restart
        orch._restart_requested = True
        os.kill(os.getpid(), signal.SIGTERM)
        return {
            "status": "updating",
            "message": "Update pulled and daemon restart initiated",
            "pull_output": pull_output,
            "reason": reason,
            "waited_for_tasks": wait_for_tasks and running_count > 0,
        }

    # -----------------------------------------------------------------------
    # File / shell commands -- sandboxed filesystem and shell access for the
    # chat agent.  These have no Discord slash command equivalent; they exist
    # so the LLM can inspect workspace files, run diagnostic commands, and
    # search codebases.  All paths are validated through _validate_path to
    # prevent escaping the workspace sandbox.
    # -----------------------------------------------------------------------

    # _cmd_read_file → moved to src/plugins/internal/files.py (aq-files plugin)

    async def _cmd_run_command(self, args: dict) -> dict:
        command = args["command"]
        working_dir = args["working_dir"]
        timeout = min(args.get("timeout", 30), 120)

        if not os.path.isabs(working_dir):
            ws_path = await self.db.get_project_workspace_path(working_dir)
            if ws_path:
                working_dir = ws_path
            else:
                working_dir = os.path.join(self.config.workspace_dir, working_dir)

        validated = await self._validate_path(working_dir)
        if not validated:
            return {"error": "Access denied: working directory is outside allowed directories"}
        if not os.path.isdir(validated):
            return {"error": f"Directory not found: {working_dir}"}

        try:
            rc, stdout, stderr = await _run_subprocess_shell(
                command,
                cwd=validated,
                timeout=timeout,
            )
            stdout = stdout[:4000] if stdout else ""
            stderr = stderr[:2000] if stderr else ""
            return {
                "returncode": rc,
                "stdout": stdout,
                "stderr": stderr,
            }
        except asyncio.TimeoutError:
            return {"error": f"Command timed out after {timeout}s"}

    # _cmd_search_files → moved to src/plugins/internal/files.py (aq-files plugin)

    # _cmd_list_directory → moved to src/plugins/internal/files.py (aq-files plugin)
    # _cmd_write_file → moved to src/plugins/internal/files.py (aq-files plugin)
    # _cmd_edit_file → moved to src/plugins/internal/files.py (aq-files plugin)
    # _cmd_glob_files → moved to src/plugins/internal/files.py (aq-files plugin)
    # _cmd_grep → moved to src/plugins/internal/files.py (aq-files plugin)

    # -----------------------------------------------------------------------
    # Agent Profile commands -- CRUD for capability bundles that configure
    # agents with specific tools, MCP servers, and system prompt overrides.
    #
    # All mutations write to the vault markdown file first; the vault
    # watcher syncs changes to the database.  For immediate feedback, an
    # explicit sync_profile_text_to_db call is made after the file write.
    # -----------------------------------------------------------------------

    def _vault_profile_path(self, profile_id: str) -> str:
        """Return the vault file path for a profile's markdown definition."""
        return os.path.join(self.config.data_dir, "vault", "agent-types", profile_id, "profile.md")

    async def _cmd_list_profiles(self, args: dict) -> dict:
        profiles = await self.db.list_profiles()
        if not profiles:
            return {"profiles": [], "count": 0}
        return {
            "profiles": [
                {
                    "id": p.id,
                    "name": p.name,
                    "description": p.description,
                    "model": p.model or "(default)",
                    "allowed_tools": p.allowed_tools,
                    "mcp_servers": list(p.mcp_servers.keys()) if p.mcp_servers else [],
                    "has_system_prompt": bool(p.system_prompt_suffix),
                }
                for p in profiles
            ],
            "count": len(profiles),
        }

    async def _cmd_create_profile(self, args: dict) -> dict:
        from pathlib import Path

        from src.profile_parser import agent_profile_to_markdown
        from src.profile_sync import sync_profile_text_to_db
        from src.vault import copy_starter_knowledge, ensure_vault_profile_dirs

        profile_id = args.get("id", "").strip()
        name = args.get("name", "").strip()
        if not profile_id:
            return {"error": "Profile id is required"}
        if not name:
            return {"error": "Profile name is required"}

        # Check for duplicates in both vault and DB.
        vault_path = self._vault_profile_path(profile_id)
        if os.path.isfile(vault_path):
            return {"error": f"Profile '{profile_id}' already exists"}
        existing = await self.db.get_profile(profile_id)
        if existing:
            return {"error": f"Profile '{profile_id}' already exists"}

        # Create vault subdirectories for the new profile (vault spec §2).
        ensure_vault_profile_dirs(self.config.data_dir, profile_id)

        # Copy starter knowledge pack if one exists for this profile type
        # (profiles spec §4, roadmap 4.3.4).
        starter_result = copy_starter_knowledge(self.config.data_dir, profile_id)

        # Render and write the markdown profile to the vault.
        markdown = agent_profile_to_markdown(
            id=profile_id,
            name=name,
            description=args.get("description", ""),
            model=args.get("model", ""),
            permission_mode=args.get("permission_mode", ""),
            allowed_tools=args.get("allowed_tools", []),
            mcp_servers=args.get("mcp_servers", {}),
            system_prompt_suffix=args.get("system_prompt_suffix", ""),
            install=args.get("install", {}),
        )
        Path(vault_path).write_text(markdown, encoding="utf-8")

        # Immediate sync: parse the file and upsert to DB so the caller
        # gets instant confirmation.  The vault watcher will also pick up
        # the change (idempotent upsert).
        sync_result = await sync_profile_text_to_db(
            markdown, self.db, source_path=vault_path, fallback_id=profile_id
        )
        if not sync_result.success:
            return {
                "error": (f"Profile file written to vault but DB sync failed: {sync_result.errors}")
            }

        # Ensure the agent-type memory collection exists (memory scoping spec §4).
        memory_mgr = getattr(self.orchestrator, "memory_manager", None)
        if memory_mgr:
            try:
                await memory_mgr.ensure_agent_type_collection(profile_id)
            except Exception:
                logger.warning(
                    "Failed to ensure agent-type memory collection for profile '%s'",
                    profile_id,
                    exc_info=True,
                )

        result: dict = {"created": profile_id, "name": name}
        if starter_result["copied"]:
            result["starter_knowledge"] = starter_result["copied"]
        if sync_result.warnings:
            result["warnings"] = sync_result.warnings
        return result

    async def _cmd_get_profile(self, args: dict) -> dict:
        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}
        profile = await self.db.get_profile(profile_id)
        if not profile:
            return {"error": f"Profile '{profile_id}' not found"}
        return {
            "id": profile.id,
            "name": profile.name,
            "description": profile.description,
            "model": profile.model or "(default)",
            "permission_mode": profile.permission_mode or "(default)",
            "allowed_tools": profile.allowed_tools,
            "mcp_servers": profile.mcp_servers,
            "system_prompt_suffix": profile.system_prompt_suffix or "(none)",
            "install": profile.install,
        }

    async def _cmd_edit_profile(self, args: dict) -> dict:
        from pathlib import Path

        from src.profile_parser import (
            agent_profile_to_markdown,
            parse_profile,
            parsed_profile_to_agent_profile,
        )
        from src.profile_sync import sync_profile_text_to_db
        from src.vault import ensure_vault_profile_dirs

        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}

        updates: dict = {}
        for fld in (
            "name",
            "description",
            "model",
            "permission_mode",
            "allowed_tools",
            "mcp_servers",
            "system_prompt_suffix",
            "install",
        ):
            if fld in args:
                updates[fld] = args[fld]
        if not updates:
            return {
                "error": (
                    "No fields to update. Provide name, description, model, "
                    "permission_mode, allowed_tools, mcp_servers, "
                    "system_prompt_suffix, or install."
                )
            }

        # Load current state: prefer vault file, fall back to DB.
        vault_path = self._vault_profile_path(profile_id)
        current: dict = {}
        role = ""
        rules = ""
        reflection = ""

        if os.path.isfile(vault_path):
            text = Path(vault_path).read_text(encoding="utf-8")
            parsed = parse_profile(text)
            current = parsed_profile_to_agent_profile(parsed)
            role = parsed.role
            rules = parsed.rules
            reflection = parsed.reflection
        else:
            # No vault file yet — reconstruct from DB.
            profile = await self.db.get_profile(profile_id)
            if not profile:
                return {"error": f"Profile '{profile_id}' not found"}
            current = {
                "id": profile.id,
                "name": profile.name,
                "description": profile.description,
                "model": profile.model,
                "permission_mode": profile.permission_mode,
                "allowed_tools": profile.allowed_tools,
                "mcp_servers": profile.mcp_servers,
                "system_prompt_suffix": profile.system_prompt_suffix,
                "install": profile.install,
            }

        # Apply updates on top of the current state.
        merged = {**current, **updates}

        # If the user is updating system_prompt_suffix, clear the
        # individual prompt section overrides so the new suffix is used.
        if "system_prompt_suffix" in updates:
            role = ""
            rules = ""
            reflection = ""

        # Ensure vault dirs exist (handles DB-only → vault migration).
        ensure_vault_profile_dirs(self.config.data_dir, profile_id)

        # Re-render and write the updated markdown.
        markdown = agent_profile_to_markdown(
            id=merged.get("id", profile_id),
            name=merged.get("name", profile_id),
            description=merged.get("description", ""),
            model=merged.get("model", ""),
            permission_mode=merged.get("permission_mode", ""),
            allowed_tools=merged.get("allowed_tools", []),
            mcp_servers=merged.get("mcp_servers", {}),
            system_prompt_suffix=merged.get("system_prompt_suffix", ""),
            install=merged.get("install", {}),
            role=role,
            rules=rules,
            reflection=reflection,
        )
        Path(vault_path).write_text(markdown, encoding="utf-8")

        # Immediate sync to DB.
        sync_result = await sync_profile_text_to_db(
            markdown, self.db, source_path=vault_path, fallback_id=profile_id
        )

        result: dict = {"updated": profile_id, "fields": list(updates.keys())}
        if sync_result.warnings:
            result["warnings"] = sync_result.warnings
        if not sync_result.success:
            result["sync_errors"] = sync_result.errors
        return result

    async def _cmd_delete_profile(self, args: dict) -> dict:
        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}

        # Check both vault file and DB for the profile.
        vault_path = self._vault_profile_path(profile_id)
        vault_exists = os.path.isfile(vault_path)
        profile = await self.db.get_profile(profile_id)

        if not profile and not vault_exists:
            return {"error": f"Profile '{profile_id}' not found"}

        name = profile.name if profile else profile_id

        # Remove the vault markdown file (the watcher retains DB rows on
        # file deletion, so we explicitly delete from DB as well).
        if vault_exists:
            os.remove(vault_path)

        # Delete from DB (cascade-clears task/project FK references).
        if profile:
            await self.db.delete_profile(profile_id)

        return {"deleted": profile_id, "name": name}

    # --- Discovery commands ------------------------------------------------

    async def _cmd_list_available_tools(self, args: dict) -> dict:
        from src.known_tools import CLAUDE_CODE_TOOLS, KNOWN_MCP_SERVERS

        tools = [
            {"name": name, "description": desc} for name, desc in sorted(CLAUDE_CODE_TOOLS.items())
        ]
        mcp_servers = [
            {
                "name": name,
                "description": info["description"],
                "npm_package": info.get("npm_package", ""),
            }
            for name, info in sorted(KNOWN_MCP_SERVERS.items())
        ]
        return {"tools": tools, "mcp_servers": mcp_servers}

    # --- Install manifest commands -----------------------------------------

    async def _cmd_check_profile(self, args: dict) -> dict:
        import shutil
        from src.known_tools import InstallManifest

        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}
        profile = await self.db.get_profile(profile_id)
        if not profile:
            return {"error": f"Profile '{profile_id}' not found"}

        manifest = InstallManifest.from_dict(profile.install)
        issues: list[str] = []

        # Check commands via shutil.which
        for cmd in manifest.commands:
            if not shutil.which(cmd):
                issues.append(f"Command not found: {cmd}")

        # Check npm packages
        for pkg in manifest.npm:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "npm",
                    "list",
                    "-g",
                    pkg,
                    "--depth=0",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode != 0:
                    issues.append(f"npm package not installed: {pkg}")
            except FileNotFoundError:
                issues.append(f"npm not available — cannot check: {pkg}")

        # Check pip packages
        for pkg in manifest.pip:
            try:
                import importlib.metadata

                importlib.metadata.version(pkg)
            except Exception:
                issues.append(f"pip package not installed: {pkg}")

        return {
            "profile_id": profile_id,
            "valid": len(issues) == 0,
            "issues": issues,
            "manifest": manifest.to_dict(),
        }

    async def _cmd_install_profile(self, args: dict) -> dict:
        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}

        # Run check first
        check = await self._cmd_check_profile({"profile_id": profile_id})
        if "error" in check:
            return check

        profile = await self.db.get_profile(profile_id)
        from src.known_tools import InstallManifest

        manifest = InstallManifest.from_dict(profile.install)

        if manifest.is_empty:
            return {
                "profile_id": profile_id,
                "installed": [],
                "already_present": [],
                "manual": [],
                "ready": True,
            }

        return await self._install_manifest(profile_id, manifest)

    async def _install_manifest(
        self,
        profile_id: str,
        manifest: "Any",  # noqa: F821 — type was removed, keeping signature stable
    ) -> dict:
        """Shared logic for installing an InstallManifest's dependencies."""
        import shutil

        installed: list[str] = []
        already_present: list[str] = []
        manual: list[str] = []

        # Install npm packages
        for pkg in manifest.npm:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "npm",
                    "list",
                    "-g",
                    pkg,
                    "--depth=0",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode == 0:
                    already_present.append(f"npm:{pkg}")
                    continue
            except FileNotFoundError:
                manual.append(f"npm not available — install manually: {pkg}")
                continue

            try:
                proc = await asyncio.create_subprocess_exec(
                    "npm",
                    "install",
                    "-g",
                    pkg,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode == 0:
                    installed.append(f"npm:{pkg}")
                else:
                    stderr = await proc.stderr.read()
                    manual.append(f"npm install failed for {pkg}: {stderr.decode().strip()}")
            except Exception as e:
                manual.append(f"npm install failed for {pkg}: {e}")

        # Install pip packages
        for pkg in manifest.pip:
            try:
                import importlib.metadata

                importlib.metadata.version(pkg)
                already_present.append(f"pip:{pkg}")
                continue
            except Exception:
                pass

            try:
                proc = await asyncio.create_subprocess_exec(
                    "pip",
                    "install",
                    pkg,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode == 0:
                    installed.append(f"pip:{pkg}")
                else:
                    stderr = await proc.stderr.read()
                    manual.append(f"pip install failed for {pkg}: {stderr.decode().strip()}")
            except Exception as e:
                manual.append(f"pip install failed for {pkg}: {e}")

        # Check commands — can't auto-install system binaries
        for cmd in manifest.commands:
            if shutil.which(cmd):
                already_present.append(f"cmd:{cmd}")
            else:
                manual.append(f"Command not found (install manually): {cmd}")

        ready = len(manual) == 0
        return {
            "profile_id": profile_id,
            "installed": installed,
            "already_present": already_present,
            "manual": manual,
            "ready": ready,
        }

    # --- Export / import commands ------------------------------------------

    async def _cmd_export_profile(self, args: dict) -> dict:
        import yaml as _yaml

        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}
        profile = await self.db.get_profile(profile_id)
        if not profile:
            return {"error": f"Profile '{profile_id}' not found"}

        data: dict = {
            "id": profile.id,
            "name": profile.name,
        }
        if profile.description:
            data["description"] = profile.description
        if profile.model:
            data["model"] = profile.model
        if profile.permission_mode:
            data["permission_mode"] = profile.permission_mode
        if profile.allowed_tools:
            data["allowed_tools"] = profile.allowed_tools
        if profile.mcp_servers:
            data["mcp_servers"] = profile.mcp_servers
        if profile.system_prompt_suffix:
            data["system_prompt_suffix"] = profile.system_prompt_suffix
        if profile.install:
            data["install"] = profile.install

        yaml_text = f"# Agent Profile: {profile.name}\n"
        yaml_text += _yaml.dump(
            {"agent_profile": data},
            default_flow_style=False,
            sort_keys=False,
        )

        result: dict = {"yaml": yaml_text}

        # Optionally create a GitHub gist
        if args.get("create_gist"):
            import tempfile

            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".yaml",
                    delete=False,
                    prefix=f"agent-profile-{profile_id}-",
                ) as f:
                    f.write(yaml_text)
                    tmp_path = f.name

                env = {**os.environ, "GH_PROMPT_DISABLED": "1"}
                proc = await asyncio.create_subprocess_exec(
                    "gh",
                    "gist",
                    "create",
                    "--public",
                    "--desc",
                    f"Agent Profile: {profile.name}",
                    tmp_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    result["gist_url"] = stdout.decode().strip()
                else:
                    result["gist_error"] = stderr.decode().strip()
            except FileNotFoundError:
                result["gist_error"] = "gh CLI not found — install GitHub CLI"
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        return result

    async def _cmd_import_profile(self, args: dict) -> dict:
        from pathlib import Path

        import yaml as _yaml

        from src.known_tools import InstallManifest
        from src.profile_parser import agent_profile_to_markdown
        from src.profile_sync import sync_profile_text_to_db
        from src.vault import ensure_vault_profile_dirs

        source = args.get("source", "").strip()
        if not source:
            return {"error": "source is required (YAML text or gist URL)"}

        # If source looks like a URL, fetch via gh gist
        yaml_text = source
        if source.startswith("http://") or source.startswith("https://"):
            gist_id = source.rstrip("/").split("/")[-1]
            try:
                env = {**os.environ, "GH_PROMPT_DISABLED": "1"}
                proc = await asyncio.create_subprocess_exec(
                    "gh",
                    "gist",
                    "view",
                    gist_id,
                    "--raw",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    return {"error": f"Failed to fetch gist: {stderr.decode().strip()}"}
                yaml_text = stdout.decode()
            except FileNotFoundError:
                return {"error": "gh CLI not found — install GitHub CLI to import from URLs"}

        try:
            data = _yaml.safe_load(yaml_text)
        except Exception as e:
            return {"error": f"Invalid YAML: {e}"}

        if not isinstance(data, dict) or "agent_profile" not in data:
            return {"error": "YAML must contain an 'agent_profile' key"}

        pdata = data["agent_profile"]
        if not isinstance(pdata, dict):
            return {"error": "agent_profile must be a mapping"}

        profile_id = args.get("id") or pdata.get("id", "")
        if not profile_id:
            return {"error": "Profile must have an 'id' field"}

        overwrite = args.get("overwrite", False)
        vault_path = self._vault_profile_path(profile_id)
        vault_exists = os.path.isfile(vault_path)
        existing = await self.db.get_profile(profile_id)
        if (existing or vault_exists) and not overwrite:
            return {
                "error": f"Profile '{profile_id}' already exists (use overwrite=true to replace)"
            }

        profile_name = args.get("name") or pdata.get("name", profile_id)

        # Ensure vault directories.
        ensure_vault_profile_dirs(self.config.data_dir, profile_id)

        # Render imported data as vault markdown.
        markdown = agent_profile_to_markdown(
            id=profile_id,
            name=profile_name,
            description=pdata.get("description", ""),
            model=pdata.get("model", ""),
            permission_mode=pdata.get("permission_mode", ""),
            allowed_tools=pdata.get("allowed_tools", []),
            mcp_servers=pdata.get("mcp_servers", {}),
            system_prompt_suffix=pdata.get("system_prompt_suffix", ""),
            install=pdata.get("install", {}),
        )
        Path(vault_path).write_text(markdown, encoding="utf-8")

        # Immediate sync to DB.
        sync_result = await sync_profile_text_to_db(
            markdown, self.db, source_path=vault_path, fallback_id=profile_id
        )

        result: dict = {"imported": True, "name": profile_name, "id": profile_id}
        if sync_result.warnings:
            result["warnings"] = sync_result.warnings

        # Auto-install dependencies if manifest is non-empty
        install_data = pdata.get("install", {})
        manifest = InstallManifest.from_dict(install_data)
        if not manifest.is_empty:
            install_result = await self._install_manifest(profile_id, manifest)
            result["installed"] = install_result["installed"]
            result["already_present"] = install_result["already_present"]
            result["manual"] = install_result["manual"]
            result["ready"] = install_result["ready"]
        else:
            result["ready"] = True

        return result

    # -------------------------------------------------------------------
    # Tool navigation commands (Phase 3 -- tiered tool system)
    # -------------------------------------------------------------------

    async def _cmd_browse_tools(self, args: dict) -> dict:
        """List available tool categories with metadata."""
        from src.tool_registry import ToolRegistry

        registry = ToolRegistry()
        if hasattr(self.orchestrator, "plugin_registry") and self.orchestrator.plugin_registry:
            registry.set_plugin_registry(self.orchestrator.plugin_registry)
        return {"categories": registry.get_categories()}

    async def _cmd_load_tools(self, args: dict) -> dict:
        """Load a tool category's definitions for the current interaction.

        The actual schema injection happens in the chat layer (Supervisor),
        not here. This command returns the list of tool names so the chat
        layer knows which schemas to add.
        """
        from src.tool_registry import ToolRegistry

        category = args.get("category", "")
        registry = ToolRegistry()
        if hasattr(self.orchestrator, "plugin_registry") and self.orchestrator.plugin_registry:
            registry.set_plugin_registry(self.orchestrator.plugin_registry)
        names = registry.get_category_tool_names(category)
        if names is None:
            available = [c["name"] for c in registry.get_categories()]
            return {
                "error": (f"Unknown category: {category}. Available: {', '.join(available)}"),
            }
        return {
            "loaded": category,
            "tools_added": names,
            "message": (f"{len(names)} {category} tools are now available."),
        }

    async def _cmd_send_message(self, args: dict) -> dict:
        """Post a message to a Discord channel."""
        channel_id = args.get("channel_id")
        content = args.get("content")
        if not channel_id or not content:
            return {"error": "channel_id and content are required"}
        bot = getattr(self.orchestrator, "_discord_bot", None)
        if not bot:
            return {"error": "Discord bot not available"}
        try:
            channel = bot.get_channel(int(channel_id))
            if not channel:
                channel = await bot.fetch_channel(int(channel_id))
            await channel.send(content)
            return {"success": True, "channel_id": channel_id}
        except Exception as e:
            return {"error": f"Failed to send message: {e}"}

    # ------------------------------------------------------------------
    # Plugin management commands
    # ------------------------------------------------------------------

    async def _cmd_plugin_list(self, args: dict) -> dict:
        """List installed plugins."""
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        # Get all plugins from DB (includes disabled/errored)
        db_plugins = await self.db.list_plugins()
        loaded = self.orchestrator.plugin_registry.list_plugins()

        plugins = []
        for p in db_plugins:
            info = {
                "name": p["id"],
                "version": p.get("version", "?"),
                "status": p.get("status", "unknown"),
                "source_url": p.get("source_url", ""),
            }
            # Merge runtime info if loaded
            for lp in loaded:
                if lp["name"] == p["id"]:
                    info["description"] = lp.get("description", "")
                    info["commands"] = lp.get("commands", [])
                    info["tools"] = lp.get("tools", [])
                    break
            plugins.append(info)

        return {"plugins": plugins, "count": len(plugins)}

    async def _cmd_plugin_info(self, args: dict) -> dict:
        """Get detailed info for a specific plugin."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        # Try loaded plugin first
        info = self.orchestrator.plugin_registry.get_plugin(name)
        if info:
            return {"plugin": info}

        # Fall back to DB record
        db_plugin = await self.db.get_plugin(name)
        if db_plugin:
            return {"plugin": db_plugin}

        return {"error": f"Plugin '{name}' not found"}

    async def _cmd_plugin_install(self, args: dict) -> dict:
        """Install a plugin from a git repository."""
        url = args.get("url")
        if not url:
            return {"error": "url is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        branch = args.get("branch")
        name = args.get("name")

        try:
            installed_name = await self.orchestrator.plugin_registry.install_from_git(
                url,
                branch=branch,
                name=name,
            )
            return {
                "installed": installed_name,
                "message": f"Plugin '{installed_name}' installed successfully from {url}",
            }
        except Exception as e:
            return {"error": f"Installation failed: {e}"}

    async def _cmd_plugin_update(self, args: dict) -> dict:
        """Update an installed plugin."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        rev = args.get("rev")
        try:
            new_rev = await self.orchestrator.plugin_registry.update_plugin(
                name,
                rev=rev,
            )
            return {
                "updated": name,
                "rev": new_rev,
                "message": f"Plugin '{name}' updated to {new_rev[:8]}",
            }
        except Exception as e:
            return {"error": f"Update failed: {e}"}

    async def _cmd_plugin_remove(self, args: dict) -> dict:
        """Remove an installed plugin."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        try:
            await self.orchestrator.plugin_registry.remove_plugin(name)
            return {"removed": name, "message": f"Plugin '{name}' removed"}
        except Exception as e:
            return {"error": f"Removal failed: {e}"}

    async def _cmd_plugin_enable(self, args: dict) -> dict:
        """Enable a disabled plugin."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        try:
            await self.orchestrator.plugin_registry.enable_plugin(name)
            return {"enabled": name, "message": f"Plugin '{name}' enabled"}
        except Exception as e:
            return {"error": f"Enable failed: {e}"}

    async def _cmd_plugin_disable(self, args: dict) -> dict:
        """Disable a plugin (keeps it installed)."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        try:
            await self.orchestrator.plugin_registry.disable_plugin(name)
            return {"disabled": name, "message": f"Plugin '{name}' disabled"}
        except Exception as e:
            return {"error": f"Disable failed: {e}"}

    async def _cmd_plugin_reload(self, args: dict) -> dict:
        """Reload a plugin (shutdown -> load -> initialize)."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        try:
            await self.orchestrator.plugin_registry.reload_plugin(name)
            return {"reloaded": name, "message": f"Plugin '{name}' reloaded"}
        except Exception as e:
            return {"error": f"Reload failed: {e}"}

    async def _cmd_plugin_config(self, args: dict) -> dict:
        """Get or set plugin configuration."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        plugin_info = self.orchestrator.plugin_registry.get_plugin(name)
        if not plugin_info:
            # Try DB
            db_plugin = await self.db.get_plugin(name)
            if not db_plugin:
                return {"error": f"Plugin '{name}' not found"}
            import json

            try:
                config = json.loads(db_plugin.get("config", "{}"))
            except (json.JSONDecodeError, TypeError):
                config = {}
            return {"name": name, "config": config}

        loaded = self.orchestrator.plugin_registry._plugins.get(name)
        if loaded:
            config = loaded.context.get_config()
            new_config = args.get("config")
            if new_config:
                if isinstance(new_config, str):
                    import json

                    new_config = json.loads(new_config)
                await loaded.context.save_config(new_config)
                return {"name": name, "config": new_config, "message": "Config updated"}
            return {"name": name, "config": config}

        return {"error": f"Plugin '{name}' is not loaded"}

    async def _cmd_plugin_prompts(self, args: dict) -> dict:
        """List a plugin's prompt templates."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        loaded = self.orchestrator.plugin_registry._plugins.get(name)
        if not loaded:
            return {"error": f"Plugin '{name}' is not loaded"}

        prompts = loaded.context.list_prompts()
        return {"name": name, "prompts": prompts}

    async def _cmd_plugin_reset_prompts(self, args: dict) -> dict:
        """Re-copy all default prompts from plugin source."""
        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        if not hasattr(self.orchestrator, "plugin_registry"):
            return {"error": "Plugin system not initialized"}

        loaded = self.orchestrator.plugin_registry._plugins.get(name)
        if not loaded:
            return {"error": f"Plugin '{name}' is not loaded"}

        from src.plugins.loader import reset_prompts

        count = reset_prompts(loaded.install_path)
        return {"name": name, "reset_count": count, "message": f"Reset {count} prompts"}

    # Rule system commands are implemented above (Phase 2).
