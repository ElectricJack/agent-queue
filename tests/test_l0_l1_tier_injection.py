"""Tests for L0 + L1 tier injection — Roadmap 3.3.7.

Verifies that L0 Identity (~50 tokens) and L1 Critical Facts (~200 tokens)
are correctly computed by the orchestrator and injected into the adapter's
system prompt at the correct tier positions.

Test cases from docs/specs/design/roadmap.md §3.3.7:
  (a) Task context includes Role from profile (L0)
  (b) Task context includes project + agent-type facts (L1)
  (c) Combined L0+L1 ≈ 250 tokens baseline
  (d) L0 absent if no profile (graceful degradation)
  (e) L1 absent if no facts.md (no error)
  (f) L0+L1 in system prompt section (not user message)
  (g) Agent with profile but no project still gets L0 + agent-type L1
"""

import pytest
from unittest.mock import AsyncMock

from src.adapters.base import AgentAdapter
from src.adapters.claude import ClaudeAdapter
from src.config import AppConfig
from src.models import (
    Agent,
    AgentOutput,
    AgentProfile,
    AgentResult,
    Project,
    RepoSourceType,
    Task,
    TaskContext,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator


# -- Realistic L0/L1 content for token budget tests ----------------------

L0_ROLE_REALISTIC = (
    "You are a senior software engineering agent. You write, modify, and debug "
    "production-quality Python code within an async project workspace. Follow "
    "the project's conventions, run tests before committing."
)  # ~50 tokens at 4 chars/token ≈ 200 chars

L1_FACTS_REALISTIC = (
    "## Critical Facts\n"
    "- tech_stack: Python 3.12, asyncio, SQLAlchemy Core, FastAPI, discord.py\n"
    "- test_framework: pytest with pytest-asyncio in auto mode, ruff for linting\n"
    "- linter: ruff format and lint (line-length 100, target py312, pre-commit hooks)\n"
    "- default_branch: main (protected, requires PR review before merge)\n"
    "- deploy_target: staging environment via Docker Compose with PostgreSQL\n"
    "- database: SQLite with aiosqlite for dev, PostgreSQL with asyncpg for prod\n"
    "- ci_cd: GitHub Actions for tests, linting, and documentation deployment\n"
    "- api_style: RESTful JSON endpoints with FastAPI OpenAPI documentation\n"
    "- auth: JWT bearer tokens with refresh token rotation\n"
    "- logging: structlog with structured JSON and correlation IDs"
)  # ~200 tokens at 4 chars/token ≈ 800 chars


# -- Test helpers --------------------------------------------------------


class CapturingMockAdapter(AgentAdapter):
    """MockAdapter that captures the TaskContext passed to start()."""

    def __init__(self):
        self.captured_ctx: TaskContext | None = None

    async def start(self, task: TaskContext) -> None:
        self.captured_ctx = task

    async def wait(self, on_message=None) -> AgentOutput:
        return AgentOutput(result=AgentResult.COMPLETED, summary="Done", tokens_used=100)

    async def stop(self) -> None:
        pass

    async def is_alive(self) -> bool:
        return True


class CapturingMockAdapterFactory:
    """Factory that creates CapturingMockAdapters and records them."""

    def __init__(self):
        self.adapters: list[CapturingMockAdapter] = []

    def create(self, agent_type: str, profile=None) -> AgentAdapter:
        adapter = CapturingMockAdapter()
        self.adapters.append(adapter)
        return adapter

    @property
    def last_ctx(self) -> TaskContext | None:
        """Return the TaskContext captured by the most recently created adapter."""
        if self.adapters:
            return self.adapters[-1].captured_ctx
        return None


async def _setup_project_and_agent(
    db,
    project_id: str = "p-1",
    profile: AgentProfile | None = None,
):
    """Create project, workspace, and agent. Optionally set a default profile.

    Profile is created first to satisfy FK constraints.
    """
    # Profile must exist before project references it
    if profile:
        await db.create_profile(profile)

    project = Project(
        id=project_id,
        name="test-project",
        default_profile_id=profile.id if profile else None,
    )
    await db.create_project(project)
    await db.create_workspace(
        Workspace(
            id=f"ws-{project_id}",
            project_id=project_id,
            workspace_path="/tmp/test-l0l1-workspace",
            source_type=RepoSourceType.LINK,
        )
    )
    await db.create_agent(Agent(id="a-1", name="claude-1", agent_type="claude"))


# -- Fixtures -----------------------------------------------------------


@pytest.fixture
async def orch_env(tmp_path):
    """Create orchestrator with capturing adapter factory."""
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    factory = CapturingMockAdapterFactory()
    o = Orchestrator(config, adapter_factory=factory)
    await o.initialize()
    yield o, factory
    await o.wait_for_running_tasks(timeout=10)
    await o.shutdown()


# ======================================================================
# (a) Every task context includes Role from profile (L0, ~50 tokens)
# ======================================================================


class TestL0RoleFromProfile:
    """(a) Every task context includes the ## Role section from the agent's profile."""

    async def test_l0_role_populated_from_profile_suffix(self, orch_env):
        """Profile's system_prompt_suffix flows through to TaskContext.l0_role."""
        orch, factory = orch_env

        profile = AgentProfile(
            id="coding",
            name="Coding Agent",
            system_prompt_suffix="You are a senior backend developer.",
        )
        await _setup_project_and_agent(orch.db, profile=profile)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test L0",
                description="Do something",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        ctx = factory.last_ctx
        assert ctx is not None, "Adapter was never started — TaskContext not captured"
        assert ctx.l0_role == "You are a senior backend developer."

    async def test_l0_role_stripped_of_whitespace(self, orch_env):
        """Leading/trailing whitespace in system_prompt_suffix is stripped."""
        orch, factory = orch_env

        profile = AgentProfile(
            id="qa",
            name="QA Agent",
            system_prompt_suffix="  \n  You are a QA specialist.  \n  ",
        )
        await _setup_project_and_agent(orch.db, profile=profile)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test L0 strip",
                description="Do something",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        ctx = factory.last_ctx
        assert ctx is not None
        assert ctx.l0_role == "You are a QA specialist."

    async def test_l0_role_from_task_level_profile(self, orch_env):
        """Profile set via task.profile_id also provides L0 role."""
        orch, factory = orch_env

        profile = AgentProfile(
            id="reviewer",
            name="Code Reviewer",
            system_prompt_suffix="You review PRs for correctness and style.",
        )
        # Project has no default profile; the task has one explicitly
        await _setup_project_and_agent(orch.db, profile=profile)
        # Override: remove default_profile_id from project
        await orch.db.update_project("p-1", default_profile_id=None)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test task-level L0",
                description="Review the code",
                status=TaskStatus.READY,
                profile_id="reviewer",  # task-level profile
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        ctx = factory.last_ctx
        assert ctx is not None
        assert ctx.l0_role == "You review PRs for correctness and style."


# ======================================================================
# (b) Every task context includes project + agent-type facts (L1)
# ======================================================================


class TestL1FactsFromMemory:
    """(b) Every task context includes project + agent-type facts.md KV entries."""

    async def test_l1_facts_populated_from_memory_service(self, orch_env):
        """Memory service's load_l1_facts() result flows to TaskContext.l1_facts."""
        orch, factory = orch_env

        mock_mem = AsyncMock()
        mock_mem.load_l1_facts = AsyncMock(return_value=L1_FACTS_REALISTIC)
        orch._memory_v2_service = mock_mem

        profile = AgentProfile(
            id="coding",
            name="Coding Agent",
            system_prompt_suffix="You are a coding agent.",
        )
        await _setup_project_and_agent(orch.db, profile=profile)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test L1",
                description="Build it",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        ctx = factory.last_ctx
        assert ctx is not None
        assert "Critical Facts" in ctx.l1_facts
        assert "tech_stack: Python 3.12" in ctx.l1_facts

    async def test_l1_facts_called_with_project_and_agent_type(self, orch_env):
        """load_l1_facts receives correct project_id and agent_type."""
        orch, factory = orch_env

        mock_mem = AsyncMock()
        mock_mem.load_l1_facts = AsyncMock(return_value="## Critical Facts\n- key: value")
        orch._memory_v2_service = mock_mem

        profile = AgentProfile(
            id="web-developer",
            name="Web Developer",
            system_prompt_suffix="You build web apps.",
        )
        await _setup_project_and_agent(orch.db, profile=profile)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Test L1 params",
                description="Build it",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        mock_mem.load_l1_facts.assert_called_once_with(
            project_id="p-1",
            agent_type="web-developer",
        )


# ======================================================================
# (c) Combined L0+L1 ≈ 250 tokens baseline (verify within tolerance)
# ======================================================================


class TestL0L1TokenBudget:
    """(c) Combined L0+L1 is approximately 250 tokens baseline."""

    # Token estimation: 1 token ≈ 4 characters (same as prompt_builder.py)
    CHARS_PER_TOKEN = 4

    def test_l0_role_within_50_token_budget(self):
        """L0 role text is approximately 50 tokens."""
        tokens = len(L0_ROLE_REALISTIC) / self.CHARS_PER_TOKEN
        assert 30 <= tokens <= 80, (
            f"L0 should be ~50 tokens, got {tokens:.0f} ({len(L0_ROLE_REALISTIC)} chars)"
        )

    def test_l1_facts_within_200_token_budget(self):
        """L1 facts text is approximately 200 tokens."""
        tokens = len(L1_FACTS_REALISTIC) / self.CHARS_PER_TOKEN
        assert 120 <= tokens <= 280, (
            f"L1 should be ~200 tokens, got {tokens:.0f} ({len(L1_FACTS_REALISTIC)} chars)"
        )

    def test_combined_l0_l1_approximately_250_tokens(self):
        """Combined L0+L1 is within tolerance of 250-token baseline."""
        l0_tokens = len(L0_ROLE_REALISTIC) / self.CHARS_PER_TOKEN
        l1_tokens = len(L1_FACTS_REALISTIC) / self.CHARS_PER_TOKEN
        combined = l0_tokens + l1_tokens

        # 250 tokens ±50% tolerance (150–375)
        assert 150 <= combined <= 375, (
            f"Combined L0+L1 should be ~250 tokens, got {combined:.0f} "
            f"(L0={l0_tokens:.0f}, L1={l1_tokens:.0f})"
        )

    def test_combined_prompt_stays_within_budget(self):
        """L0+L1 injected through PromptBuilder stays within ~250 tokens."""
        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        builder.set_l0_role(L0_ROLE_REALISTIC)
        builder.set_l1_facts(L1_FACTS_REALISTIC)
        prompt = builder.build_task_prompt()

        # The prompt includes the L0 and L1 content plus "---" separators.
        # Verify the overhead from separators is minimal.
        prompt_tokens = len(prompt) / self.CHARS_PER_TOKEN
        raw_tokens = (len(L0_ROLE_REALISTIC) + len(L1_FACTS_REALISTIC)) / self.CHARS_PER_TOKEN

        # Separator overhead should be < 10 tokens
        overhead = prompt_tokens - raw_tokens
        assert overhead < 10, f"Separator overhead is {overhead:.0f} tokens, expected < 10"


# ======================================================================
# (d) L0 absent if agent has no profile (graceful degradation)
# ======================================================================


class TestL0GracefulDegradation:
    """(d) L0 is absent if agent has no profile.md (graceful degradation)."""

    async def test_l0_empty_when_no_profile(self, orch_env):
        """No profile → l0_role is empty string."""
        orch, factory = orch_env

        # No profile configured at all
        await _setup_project_and_agent(orch.db)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="No profile",
                description="Do something",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        ctx = factory.last_ctx
        assert ctx is not None
        assert ctx.l0_role == ""

    async def test_l0_empty_when_profile_has_no_suffix(self, orch_env):
        """Profile exists but system_prompt_suffix is empty → l0_role is empty."""
        orch, factory = orch_env

        profile = AgentProfile(
            id="bare",
            name="Bare Profile",
            system_prompt_suffix="",  # explicitly empty
        )
        await _setup_project_and_agent(orch.db, profile=profile)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Empty suffix",
                description="Do something",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        ctx = factory.last_ctx
        assert ctx is not None
        assert ctx.l0_role == ""

    async def test_task_completes_without_l0(self, orch_env):
        """Task completes successfully even without L0 role."""
        orch, factory = orch_env

        await _setup_project_and_agent(orch.db)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="No L0 completion",
                description="Work without role",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.COMPLETED


# ======================================================================
# (e) L1 absent if no facts.md exists for the scope (no error)
# ======================================================================


class TestL1GracefulDegradation:
    """(e) L1 is absent if no facts.md exists for the scope (no error)."""

    async def test_l1_empty_when_memory_returns_empty(self, orch_env):
        """Memory service returns empty string → l1_facts is empty."""
        orch, factory = orch_env

        mock_mem = AsyncMock()
        mock_mem.load_l1_facts = AsyncMock(return_value="")
        orch._memory_v2_service = mock_mem

        await _setup_project_and_agent(orch.db)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="No facts",
                description="Do something",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        ctx = factory.last_ctx
        assert ctx is not None
        assert ctx.l1_facts == ""

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.COMPLETED

    async def test_l1_empty_when_no_memory_service(self, orch_env):
        """No memory service configured → l1_facts is empty, no error."""
        orch, factory = orch_env

        # Ensure no memory service is set (default state)
        orch._memory_v2_service = None

        await _setup_project_and_agent(orch.db)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="No mem service",
                description="Do something",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        ctx = factory.last_ctx
        assert ctx is not None
        assert ctx.l1_facts == ""

    async def test_l1_graceful_on_memory_service_exception(self, orch_env):
        """Memory service throws → l1_facts is empty, task still completes."""
        orch, factory = orch_env

        mock_mem = AsyncMock()
        mock_mem.load_l1_facts = AsyncMock(side_effect=RuntimeError("memsearch unavailable"))
        orch._memory_v2_service = mock_mem

        await _setup_project_and_agent(orch.db)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Mem error",
                description="Do something",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        ctx = factory.last_ctx
        assert ctx is not None
        assert ctx.l1_facts == ""

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.COMPLETED


# ======================================================================
# (f) L0+L1 content appears in system prompt section (not user message)
# ======================================================================


class TestL0L1InSystemPrompt:
    """(f) L0+L1 content appears in the system prompt section (not user message)."""

    def test_l0_l1_present_in_adapter_system_prompt(self):
        """L0 and L1 appear in ClaudeAdapter._build_prompt() output."""
        adapter = ClaudeAdapter()
        adapter._task = TaskContext(
            description="## Task\nImplement the feature.",
            l0_role=L0_ROLE_REALISTIC,
            l1_facts=L1_FACTS_REALISTIC,
        )
        prompt = adapter._build_prompt()

        assert L0_ROLE_REALISTIC in prompt
        assert "Critical Facts" in prompt
        assert "tech_stack: Python 3.12" in prompt

    def test_l0_before_l1_before_description_in_prompt(self):
        """System prompt ordering: L0 → L1 → description."""
        adapter = ClaudeAdapter()
        adapter._task = TaskContext(
            description="## Task\nImplement the feature.",
            l0_role="You are a QA agent.",
            l1_facts="## Critical Facts\n- lang: Python",
        )
        prompt = adapter._build_prompt()

        role_pos = prompt.index("You are a QA agent.")
        facts_pos = prompt.index("Critical Facts")
        task_pos = prompt.index("Implement the feature.")
        assert role_pos < facts_pos < task_pos

    def test_l0_l1_assembled_via_prompt_builder(self):
        """L0+L1 are injected through PromptBuilder's layered assembly."""
        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        builder.set_l0_role("You are a coding agent.")
        builder.set_l1_facts("## Critical Facts\n- stack: Python")
        builder.add_context("description", "## Task\nFix the bug.")

        system_prompt, tools = builder.build()

        # L0 and L1 are in the system prompt string
        assert "You are a coding agent." in system_prompt
        assert "Critical Facts" in system_prompt
        assert "Fix the bug." in system_prompt

        # Correct ordering in system prompt
        role_pos = system_prompt.index("You are a coding agent.")
        facts_pos = system_prompt.index("Critical Facts")
        task_pos = system_prompt.index("Fix the bug.")
        assert role_pos < facts_pos < task_pos

    def test_l0_l1_with_all_task_context_extras(self):
        """L0+L1 coexist with acceptance criteria and test commands in prompt."""
        adapter = ClaudeAdapter()
        adapter._task = TaskContext(
            description="## Task\nBuild the API.",
            l0_role="You are an API developer.",
            l1_facts="## Critical Facts\n- framework: FastAPI",
            acceptance_criteria=["Endpoint returns 200", "Tests pass"],
            test_commands=["pytest tests/test_api.py"],
        )
        prompt = adapter._build_prompt()

        # All sections present
        assert "You are an API developer." in prompt
        assert "Critical Facts" in prompt
        assert "Build the API." in prompt
        assert "Acceptance Criteria" in prompt
        assert "pytest tests/test_api.py" in prompt

        # Ordering: L0 → L1 → description → extras
        role_pos = prompt.index("You are an API developer.")
        facts_pos = prompt.index("Critical Facts")
        task_pos = prompt.index("Build the API.")
        criteria_pos = prompt.index("Acceptance Criteria")
        assert role_pos < facts_pos < task_pos < criteria_pos


# ======================================================================
# (g) Agent with profile but no project still gets L0 + agent-type L1
# ======================================================================


class TestL0L1ProfileWithoutProjectFacts:
    """(g) Agent with profile but no project-level facts still gets L0 + agent-type L1."""

    async def test_l0_from_profile_l1_from_agent_type_scope(self, orch_env):
        """Profile provides L0; agent-type scope provides L1 (no project facts)."""
        orch, factory = orch_env

        # Mock memory service returns only agent-type facts
        # (simulating: project has no facts.md, agent-type does)
        agent_type_facts = "## Critical Facts\n- default_model: claude-sonnet\n- code_style: PEP 8"
        mock_mem = AsyncMock()
        mock_mem.load_l1_facts = AsyncMock(return_value=agent_type_facts)
        orch._memory_v2_service = mock_mem

        profile = AgentProfile(
            id="coding",
            name="Coding Agent",
            system_prompt_suffix="You are a full-stack developer.",
        )
        await _setup_project_and_agent(orch.db, profile=profile)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Profile+AgentType L1",
                description="Do something",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        ctx = factory.last_ctx
        assert ctx is not None

        # L0 from profile
        assert ctx.l0_role == "You are a full-stack developer."

        # L1 from agent-type scope
        assert "Critical Facts" in ctx.l1_facts
        assert "default_model: claude-sonnet" in ctx.l1_facts

    async def test_memory_service_called_with_agent_type(self, orch_env):
        """load_l1_facts is called with agent_type=profile.id."""
        orch, factory = orch_env

        mock_mem = AsyncMock()
        mock_mem.load_l1_facts = AsyncMock(return_value="")
        orch._memory_v2_service = mock_mem

        profile = AgentProfile(
            id="reviewer",
            name="Code Reviewer",
            system_prompt_suffix="You review code.",
        )
        await _setup_project_and_agent(orch.db, profile=profile)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Check agent_type param",
                description="Do something",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        # Verify agent_type comes from profile.id
        mock_mem.load_l1_facts.assert_called_once_with(
            project_id="p-1",
            agent_type="reviewer",
        )

    async def test_agent_type_none_when_no_profile(self, orch_env):
        """Without a profile, agent_type=None is passed to load_l1_facts."""
        orch, factory = orch_env

        mock_mem = AsyncMock()
        mock_mem.load_l1_facts = AsyncMock(return_value="")
        orch._memory_v2_service = mock_mem

        # No profile
        await _setup_project_and_agent(orch.db)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="No profile agent_type",
                description="Do something",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        mock_mem.load_l1_facts.assert_called_once_with(
            project_id="p-1",
            agent_type=None,
        )

    async def test_both_l0_and_l1_in_final_prompt(self, orch_env):
        """When both L0 and L1 are present, both appear in the adapter prompt."""
        orch, factory = orch_env

        facts = "## Critical Facts\n- key: value"
        mock_mem = AsyncMock()
        mock_mem.load_l1_facts = AsyncMock(return_value=facts)
        # Execution path also calls load_l1_guidance and load_l2_context —
        # return empty string so AsyncMock auto-attrs don't leak coroutines
        # or MagicMocks into the assembled prompt.
        mock_mem.load_l1_guidance = AsyncMock(return_value="")
        mock_mem.load_l2_context = AsyncMock(return_value="")
        orch._memory_v2_service = mock_mem

        profile = AgentProfile(
            id="coding",
            name="Coding Agent",
            system_prompt_suffix="You are a senior engineer.",
        )
        await _setup_project_and_agent(orch.db, profile=profile)

        await orch.db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="Both L0 L1",
                description="Do something",
                status=TaskStatus.READY,
            )
        )

        await orch.run_one_cycle()
        await orch.wait_for_running_tasks()

        ctx = factory.last_ctx
        assert ctx is not None
        assert ctx.l0_role == "You are a senior engineer."
        assert ctx.l1_facts == facts

        # Verify both would appear in the adapter's built prompt
        adapter = ClaudeAdapter()
        adapter._task = ctx
        prompt = adapter._build_prompt()

        assert "You are a senior engineer." in prompt
        assert "Critical Facts" in prompt
        assert "key: value" in prompt
