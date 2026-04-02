"""Tests for the enhanced task progress display with full status breakdown.

Validates _count_subtree_by_status, _format_status_summary, and the updated
_format_task_tree summary line that shows all non-completed task stats.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.command_handler import (
    _count_subtree,
    _count_subtree_by_status,
    _format_status_summary,
    _format_task_tree,
)
from src.models import TaskStatus


def _make_task(status: TaskStatus = TaskStatus.DEFINED, **kwargs) -> MagicMock:
    """Create a mock Task with the given status."""
    t = MagicMock()
    t.status = status
    t.id = kwargs.get("id", "task-001")
    t.title = kwargs.get("title", "Test Task")
    t.parent_task_id = kwargs.get("parent_task_id", None)
    t.is_plan_subtask = kwargs.get("is_plan_subtask", False)
    t.pr_url = kwargs.get("pr_url", None)
    t.task_type = kwargs.get("task_type", None)
    return t


def _make_tree_node(status: TaskStatus, children: list[dict] | None = None, **kwargs) -> dict:
    """Create a tree node dict matching Database.get_task_tree() shape."""
    return {
        "task": _make_task(status, **kwargs),
        "children": children or [],
    }


# ---------------------------------------------------------------------------
# _count_subtree_by_status
# ---------------------------------------------------------------------------


class TestCountSubtreeByStatus:
    def test_empty_children(self):
        assert _count_subtree_by_status([]) == {}

    def test_single_completed(self):
        children = [_make_tree_node(TaskStatus.COMPLETED)]
        assert _count_subtree_by_status(children) == {"COMPLETED": 1}

    def test_mixed_statuses(self):
        children = [
            _make_tree_node(TaskStatus.COMPLETED),
            _make_tree_node(TaskStatus.IN_PROGRESS),
            _make_tree_node(TaskStatus.FAILED),
            _make_tree_node(TaskStatus.READY),
        ]
        result = _count_subtree_by_status(children)
        assert result == {
            "COMPLETED": 1,
            "IN_PROGRESS": 1,
            "FAILED": 1,
            "READY": 1,
        }

    def test_nested_children(self):
        children = [
            _make_tree_node(TaskStatus.COMPLETED, children=[
                _make_tree_node(TaskStatus.IN_PROGRESS),
                _make_tree_node(TaskStatus.COMPLETED),
            ]),
            _make_tree_node(TaskStatus.BLOCKED),
        ]
        result = _count_subtree_by_status(children)
        assert result == {
            "COMPLETED": 2,
            "IN_PROGRESS": 1,
            "BLOCKED": 1,
        }

    def test_all_same_status(self):
        children = [
            _make_tree_node(TaskStatus.READY),
            _make_tree_node(TaskStatus.READY),
            _make_tree_node(TaskStatus.READY),
        ]
        assert _count_subtree_by_status(children) == {"READY": 3}

    def test_consistent_with_count_subtree(self):
        """_count_subtree_by_status totals should match _count_subtree."""
        children = [
            _make_tree_node(TaskStatus.COMPLETED),
            _make_tree_node(TaskStatus.IN_PROGRESS, children=[
                _make_tree_node(TaskStatus.COMPLETED),
                _make_tree_node(TaskStatus.DEFINED),
            ]),
            _make_tree_node(TaskStatus.FAILED),
        ]
        completed, total = _count_subtree(children)
        by_status = _count_subtree_by_status(children)
        assert sum(by_status.values()) == total
        assert by_status.get("COMPLETED", 0) == completed


# ---------------------------------------------------------------------------
# _format_status_summary
# ---------------------------------------------------------------------------


class TestFormatStatusSummary:
    def test_all_completed(self):
        result = _format_status_summary({"COMPLETED": 5}, 5)
        assert result == "5/5 subtasks complete"

    def test_none_completed(self):
        result = _format_status_summary({"READY": 3}, 3)
        assert "0/3 subtasks complete" in result
        assert "3 ready" in result

    def test_mixed_statuses(self):
        counts = {
            "COMPLETED": 2,
            "IN_PROGRESS": 1,
            "FAILED": 1,
            "BLOCKED": 1,
        }
        result = _format_status_summary(counts, 5)
        assert result.startswith("2/5 subtasks complete")
        assert "1 in progress" in result
        assert "1 failed" in result
        assert "1 blocked" in result

    def test_order_active_before_attention(self):
        counts = {
            "COMPLETED": 1,
            "BLOCKED": 2,
            "IN_PROGRESS": 3,
        }
        result = _format_status_summary(counts, 6)
        # "in progress" should appear before "blocked" in the summary
        ip_pos = result.index("in progress")
        bl_pos = result.index("blocked")
        assert ip_pos < bl_pos

    def test_empty_counts(self):
        result = _format_status_summary({}, 0)
        assert result == "0/0 subtasks complete"

    def test_all_non_completed_statuses(self):
        counts = {
            "IN_PROGRESS": 1,
            "ASSIGNED": 1,
            "AWAITING_APPROVAL": 1,
            "WAITING_INPUT": 1,
            "PAUSED": 1,
            "FAILED": 1,
            "BLOCKED": 1,
            "READY": 1,
            "DEFINED": 1,
        }
        result = _format_status_summary(counts, 9)
        assert "0/9 subtasks complete" in result
        assert "1 in progress" in result
        assert "1 assigned" in result
        assert "1 awaiting approval" in result
        assert "1 waiting input" in result
        assert "1 paused" in result
        assert "1 failed" in result
        assert "1 blocked" in result
        assert "1 ready" in result
        assert "1 defined" in result


# ---------------------------------------------------------------------------
# _format_task_tree summary line integration
# ---------------------------------------------------------------------------


class TestFormatTaskTreeSummary:
    def test_compact_mode_shows_status_breakdown(self):
        root = _make_task(TaskStatus.IN_PROGRESS, id="root-1", title="Root Task")
        children = [
            _make_tree_node(TaskStatus.COMPLETED, id="c-1", title="Done"),
            _make_tree_node(TaskStatus.IN_PROGRESS, id="c-2", title="Working"),
            _make_tree_node(TaskStatus.FAILED, id="c-3", title="Broken"),
        ]
        result = _format_task_tree(root, children, compact=True)
        assert "1/3 subtasks complete" in result
        assert "1 in progress" in result
        assert "1 failed" in result

    def test_expanded_mode_shows_status_breakdown(self):
        root = _make_task(TaskStatus.IN_PROGRESS, id="root-1", title="Root Task")
        children = [
            _make_tree_node(TaskStatus.COMPLETED, id="c-1", title="Done"),
            _make_tree_node(TaskStatus.BLOCKED, id="c-2", title="Stuck"),
        ]
        result = _format_task_tree(root, children, compact=False)
        assert "1/2 subtasks complete" in result
        assert "1 blocked" in result

    def test_all_completed_no_extra_stats(self):
        root = _make_task(TaskStatus.COMPLETED, id="root-1", title="All Done")
        children = [
            _make_tree_node(TaskStatus.COMPLETED, id="c-1", title="Done 1"),
            _make_tree_node(TaskStatus.COMPLETED, id="c-2", title="Done 2"),
        ]
        result = _format_task_tree(root, children, compact=True)
        assert "2/2 subtasks complete" in result
        # Should NOT have any non-completed status suffix
        assert "in progress" not in result
        assert "failed" not in result

    def test_no_children_no_summary(self):
        root = _make_task(TaskStatus.READY, id="root-1", title="Standalone")
        result = _format_task_tree(root, [], compact=True)
        assert "subtasks" not in result
