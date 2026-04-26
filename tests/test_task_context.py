"""Tests for TaskContext model fields (L0 role, L1 facts).

Moved here from the legacy tests/test_adapters.py during the platforms
refactor — these tests are about TaskContext, not about adapters.
"""

from __future__ import annotations

from src.models import TaskContext


class TestTaskContextL0L1Fields:
    """Verify TaskContext carries L0 role and L1 facts as first-class fields."""

    def test_task_context_has_l0_role_field(self):
        ctx = TaskContext(description="test", l0_role="You are a coding agent.")
        assert ctx.l0_role == "You are a coding agent."

    def test_task_context_has_l1_facts_field(self):
        ctx = TaskContext(
            description="test",
            l1_facts="## Critical Facts\n- tech_stack: Python",
        )
        assert ctx.l1_facts == "## Critical Facts\n- tech_stack: Python"

    def test_task_context_l0_l1_defaults_empty(self):
        ctx = TaskContext(description="test")
        assert ctx.l0_role == ""
        assert ctx.l1_facts == ""

    def test_task_context_l0_l1_with_all_fields(self):
        ctx = TaskContext(
            description="Fix the bug.",
            task_id="t-1",
            l0_role="You are a QA agent.",
            l1_facts="## Critical Facts\n- test_command: pytest",
            checkout_path="/home/user/project",
            branch_name="feat/fix",
        )
        assert ctx.l0_role == "You are a QA agent."
        assert ctx.l1_facts == "## Critical Facts\n- test_command: pytest"
        assert ctx.description == "Fix the bug."
