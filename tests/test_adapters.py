from src.adapters.base import AgentAdapter
from src.models import TaskContext, AgentOutput, AgentResult


class MockAdapter(AgentAdapter):
    def __init__(self, result=AgentResult.COMPLETED, tokens=1000):
        self._result = result
        self._tokens = tokens
        self.started = False
        self.stopped = False

    async def start(self, task: TaskContext) -> None:
        self.started = True

    async def wait(self) -> AgentOutput:
        return AgentOutput(
            result=self._result,
            summary="Did the thing",
            tokens_used=self._tokens,
        )

    async def stop(self) -> None:
        self.stopped = True

    async def is_alive(self) -> bool:
        return self.started and not self.stopped


class TestMockAdapter:
    async def test_lifecycle(self):
        adapter = MockAdapter()
        ctx = TaskContext(description="test task")
        await adapter.start(ctx)
        assert adapter.started
        assert await adapter.is_alive()
        output = await adapter.wait()
        assert output.result == AgentResult.COMPLETED
        assert output.tokens_used == 1000
        await adapter.stop()
        assert adapter.stopped

    async def test_failed_result(self):
        adapter = MockAdapter(result=AgentResult.FAILED)
        ctx = TaskContext(description="test")
        await adapter.start(ctx)
        output = await adapter.wait()
        assert output.result == AgentResult.FAILED

    async def test_paused_result(self):
        adapter = MockAdapter(result=AgentResult.PAUSED_RATE_LIMIT)
        ctx = TaskContext(description="test")
        await adapter.start(ctx)
        output = await adapter.wait()
        assert output.result == AgentResult.PAUSED_RATE_LIMIT


# ------------------------------------------------------------------
# TaskContext L0/L1 fields
# ------------------------------------------------------------------


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


# ------------------------------------------------------------------
# ClaudeAdapter._build_prompt() L0/L1 injection
# ------------------------------------------------------------------


class TestClaudeAdapterL0L1Injection:
    """Verify ClaudeAdapter._build_prompt() injects L0 and L1 from TaskContext."""

    def _make_adapter(self):
        from src.adapters.claude import ClaudeAdapter

        return ClaudeAdapter()

    def test_build_prompt_injects_l0_role(self):
        adapter = self._make_adapter()
        adapter._task = TaskContext(
            description="## Task\nFix the bug.",
            l0_role="You are a backend developer.",
        )
        prompt = adapter._build_prompt()

        assert "You are a backend developer." in prompt
        assert "Fix the bug." in prompt
        # L0 role appears before description
        role_pos = prompt.index("You are a backend developer.")
        task_pos = prompt.index("Fix the bug.")
        assert role_pos < task_pos

    def test_build_prompt_injects_l1_facts(self):
        adapter = self._make_adapter()
        adapter._task = TaskContext(
            description="## Task\nFix the bug.",
            l1_facts="## Critical Facts\n- tech_stack: Python\n- test_command: pytest",
        )
        prompt = adapter._build_prompt()

        assert "Critical Facts" in prompt
        assert "tech_stack: Python" in prompt
        assert "Fix the bug." in prompt
        # L1 facts appear before description
        facts_pos = prompt.index("Critical Facts")
        task_pos = prompt.index("Fix the bug.")
        assert facts_pos < task_pos

    def test_build_prompt_l0_l1_ordering(self):
        """L0 role appears before L1 facts, both before description."""
        adapter = self._make_adapter()
        adapter._task = TaskContext(
            description="## Task\nFix the bug.",
            l0_role="You are a QA agent.",
            l1_facts="## Critical Facts\n- lang: Python",
        )
        prompt = adapter._build_prompt()

        role_pos = prompt.index("You are a QA agent.")
        facts_pos = prompt.index("Critical Facts")
        task_pos = prompt.index("Fix the bug.")
        assert role_pos < facts_pos < task_pos

    def test_build_prompt_without_l0_l1(self):
        """Prompt still works when L0 and L1 are empty (backward compat)."""
        adapter = self._make_adapter()
        adapter._task = TaskContext(description="## Task\nFix the bug.")
        prompt = adapter._build_prompt()

        assert "Fix the bug." in prompt
        # No L0/L1 markers
        assert "## Role" not in prompt

    def test_build_prompt_l0_l1_with_extras(self):
        """L0/L1 coexist with acceptance criteria and other TaskContext fields."""
        adapter = self._make_adapter()
        adapter._task = TaskContext(
            description="## Task\nFix the bug.",
            l0_role="You are a security auditor.",
            l1_facts="## Critical Facts\n- auth: JWT",
            acceptance_criteria=["Login works", "Errors shown"],
            test_commands=["pytest tests/"],
        )
        prompt = adapter._build_prompt()

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
        adapter = self._make_adapter()
        adapter._task = TaskContext(
            description="## Task\nDo the thing.",
            l0_role="You are a coding agent.",
        )
        prompt = adapter._build_prompt()

        assert "You are a coding agent." in prompt
        assert "Critical Facts" not in prompt
        role_pos = prompt.index("You are a coding agent.")
        task_pos = prompt.index("Do the thing.")
        assert role_pos < task_pos

    def test_build_prompt_l1_only_no_l0(self):
        """L1 facts injected even when L0 role is absent."""
        adapter = self._make_adapter()
        adapter._task = TaskContext(
            description="## Task\nDo the thing.",
            l1_facts="## Critical Facts\n- deploy: staging",
        )
        prompt = adapter._build_prompt()

        assert "Critical Facts" in prompt
        assert "deploy: staging" in prompt
        facts_pos = prompt.index("Critical Facts")
        task_pos = prompt.index("Do the thing.")
        assert facts_pos < task_pos
