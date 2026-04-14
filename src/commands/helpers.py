"""Shared helper functions for command handler and mixins.

Module-level utilities for time parsing, subprocess execution,
tree-view formatting, and archive note building.
"""

from __future__ import annotations

import asyncio
import datetime
import time

from src.discord.embeds import STATUS_EMOJIS
from src.models import Task, TaskStatus

# ── Log / event time helpers ──────────────────────────────────────────

_RELATIVE_TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_relative_time(value: str) -> float:
    """Parse a relative time string into a Unix epoch timestamp.

    Accepts formats like ``"5m"``, ``"1h"``, ``"2d"``, ``"30s"``.
    Returns the Unix timestamp corresponding to *now - delta*.
    """
    if not value:
        return 0.0
    unit = value[-1].lower()
    multiplier = _RELATIVE_TIME_UNITS.get(unit)
    if multiplier is None:
        raise ValueError(f"Unknown time unit '{unit}'. Use s, m, h, or d.")
    try:
        amount = int(value[:-1])
    except ValueError:
        raise ValueError(f"Invalid number in '{value}'")
    return time.time() - (amount * multiplier)


# ── Log-level priority for JSONL filtering ────────────────────────────

_LEVEL_PRIORITY = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "critical": 50,
}


def _tail_log_lines(filepath: str, max_scan: int = 1000) -> list[str]:
    """Read the last *max_scan* lines from a file efficiently.

    Reads from the end of the file in chunks to avoid loading the entire
    file into memory.  Returns lines in chronological order (oldest first).
    """
    try:
        with open(filepath, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []

            chunk_size = min(8192, size)
            lines: list[bytes] = []
            pos = size

            while len(lines) <= max_scan and pos > 0:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size)
                chunk_lines = chunk.split(b"\n")

                if lines:
                    # Merge partial line from previous chunk
                    chunk_lines[-1] = chunk_lines[-1] + lines[0]
                    lines = chunk_lines + lines[1:]
                else:
                    lines = chunk_lines

            decoded = [raw.decode("utf-8", errors="replace") for raw in lines if raw.strip()]
            return decoded[-max_scan:]
    except FileNotFoundError:
        return []


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

