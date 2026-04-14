"""Backward-compatibility shim — imports from src.commands.handler.

All code has been moved to ``src/commands/``.  This module re-exports
the public API so existing ``from src.command_handler import ...``
statements continue to work without modification.
"""

from __future__ import annotations

# Re-export everything from the new location
from src.commands.handler import *  # noqa: F401,F403
from src.commands.handler import (  # noqa: F401 — explicit re-exports for type checkers
    CommandHandler,
    _build_archive_note,
    _collect_tree_task_ids,
    _collect_tree_tasks,
    _count_by,
    _count_subtree,
    _count_subtree_by_status,
    _count_tree_stats,
    _dep_annotation,
    _format_interval,
    _format_status_summary,
    _format_task_dep_line,
    _format_task_tree,
    _LEVEL_PRIORITY,
    _parse_delay,
    _parse_relative_time,
    _render_tree_node,
    _run_subprocess,
    _run_subprocess_shell,
    _status_emoji,
    _tail_log_lines,
    _tree_dep_annotation,
    format_dependency_list,
)
