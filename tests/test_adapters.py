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

