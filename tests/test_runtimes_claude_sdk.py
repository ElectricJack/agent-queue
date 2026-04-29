"""Tests for ClaudeSDKRuntime._build_prompt() L0/L1 injection.

Migrated from tests/test_adapters.py::TestClaudeAdapterL0L1Injection
as part of the platforms refactor (Task 4).
"""

from src.models import TaskContext


class TestClaudeSDKRuntimeL0L1Injection:
    """Verify ClaudeSDKRuntime._build_prompt() injects L0 and L1 from TaskContext."""

    def _make_platform(self):
        from src.runtimes.claude_sdk import ClaudeSDKRuntime

        return ClaudeSDKRuntime(profile=None)

    def test_build_prompt_injects_l0_role(self):
        platform = self._make_platform()
        platform._task = TaskContext(
            description="## Task\nFix the bug.",
            l0_role="You are a backend developer.",
        )
        prompt = platform._build_prompt()

        assert "You are a backend developer." in prompt
        assert "Fix the bug." in prompt
        # L0 role appears before description
        role_pos = prompt.index("You are a backend developer.")
        task_pos = prompt.index("Fix the bug.")
        assert role_pos < task_pos

    def test_build_prompt_injects_l1_facts(self):
        platform = self._make_platform()
        platform._task = TaskContext(
            description="## Task\nFix the bug.",
            l1_facts="## Critical Facts\n- tech_stack: Python\n- test_command: pytest",
        )
        prompt = platform._build_prompt()

        assert "Critical Facts" in prompt
        assert "tech_stack: Python" in prompt
        assert "Fix the bug." in prompt
        # L1 facts appear before description
        facts_pos = prompt.index("Critical Facts")
        task_pos = prompt.index("Fix the bug.")
        assert facts_pos < task_pos

    def test_build_prompt_l0_l1_ordering(self):
        """L0 role appears before L1 facts, both before description."""
        platform = self._make_platform()
        platform._task = TaskContext(
            description="## Task\nFix the bug.",
            l0_role="You are a QA agent.",
            l1_facts="## Critical Facts\n- lang: Python",
        )
        prompt = platform._build_prompt()

        role_pos = prompt.index("You are a QA agent.")
        facts_pos = prompt.index("Critical Facts")
        task_pos = prompt.index("Fix the bug.")
        assert role_pos < facts_pos < task_pos

    def test_build_prompt_without_l0_l1(self):
        """Prompt still works when L0 and L1 are empty (backward compat)."""
        platform = self._make_platform()
        platform._task = TaskContext(description="## Task\nFix the bug.")
        prompt = platform._build_prompt()

        assert "Fix the bug." in prompt
        # No L0/L1 markers
        assert "## Role" not in prompt

    def test_build_prompt_l0_l1_with_extras(self):
        """L0/L1 coexist with acceptance criteria and other TaskContext fields."""
        platform = self._make_platform()
        platform._task = TaskContext(
            description="## Task\nFix the bug.",
            l0_role="You are a security auditor.",
            l1_facts="## Critical Facts\n- auth: JWT",
            acceptance_criteria=["Login works", "Errors shown"],
            test_commands=["pytest tests/"],
        )
        prompt = platform._build_prompt()

        # All sections present
        assert "You are a security auditor." in prompt
        assert "Critical Facts" in prompt
        assert "Fix the bug." in prompt
        assert "Acceptance Criteria" in prompt
        assert "pytest tests/" in prompt

        # L0 → L1 → description → extras
        role_pos = prompt.index("You are a security auditor.")
        facts_pos = prompt.index("Critical Facts")
        task_pos = prompt.index("Fix the bug.")
        criteria_pos = prompt.index("Acceptance Criteria")
        assert role_pos < facts_pos < task_pos < criteria_pos

    def test_build_prompt_l0_only_no_l1(self):
        """L0 role injected even when L1 facts are absent."""
        platform = self._make_platform()
        platform._task = TaskContext(
            description="## Task\nDo the thing.",
            l0_role="You are a coding agent.",
        )
        prompt = platform._build_prompt()

        assert "You are a coding agent." in prompt
        assert "Critical Facts" not in prompt
        role_pos = prompt.index("You are a coding agent.")
        task_pos = prompt.index("Do the thing.")
        assert role_pos < task_pos

    def test_build_prompt_l1_only_no_l0(self):
        """L1 facts injected even when L0 role is absent."""
        platform = self._make_platform()
        platform._task = TaskContext(
            description="## Task\nDo the thing.",
            l1_facts="## Critical Facts\n- deploy: staging",
        )
        prompt = platform._build_prompt()

        assert "Critical Facts" in prompt
        assert "deploy: staging" in prompt
        facts_pos = prompt.index("Critical Facts")
        task_pos = prompt.index("Do the thing.")
        assert facts_pos < task_pos
