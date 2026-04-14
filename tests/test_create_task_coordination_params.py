"""Tests for create_task coordination parameters (Roadmap 7.2.1).

Covers §5 of docs/specs/design/agent-coordination.md:
  - create_task with agent_type, affinity_agent_id, affinity_reason, workspace_mode
  - edit_task editing these fields
  - get_task returning these fields
  - Validation: invalid values, empty strings, clearing fields
  - Scheduler: affinity_agent_id preference during task assignment

These parameters enable coordination playbooks to express agent type
requirements, affinity preferences, and workspace lock modes when
creating tasks — the interface between playbooks and the scheduler.
"""

import pytest
from unittest.mock import MagicMock

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import (
    Agent,
    AgentState,
    Project,
    Task,
    TaskStatus,
    WorkspaceMode,
)
from src.orchestrator import Orchestrator
from src.scheduler import Scheduler, SchedulerState


# ── Helpers ──────────────────────────────────────────────────────────


def make_project(id="p-1", name="alpha", weight=1.0, max_agents=2, **kw):
    return Project(id=id, name=name, credit_weight=weight, max_concurrent_agents=max_agents, **kw)


def make_task(id="t-1", project_id="p-1", status=TaskStatus.READY, priority=100, **kw):
    return Task(
        id=id,
        project_id=project_id,
        title=f"Task {id}",
        description="test",
        status=status,
        priority=priority,
        **kw,
    )


def make_agent(id="a-1", name="claude-1", agent_type="claude", state=AgentState.IDLE, **kw):
    return Agent(id=id, name=name, agent_type=agent_type, state=state, **kw)


def make_state(**overrides) -> SchedulerState:
    """Build a SchedulerState with sensible defaults."""
    defaults = dict(
        projects=[make_project()],
        tasks=[make_task()],
        agents=[make_agent()],
        project_token_usage={},
        project_active_agent_counts={},
        tasks_completed_in_window={},
        project_constraints={},
    )
    defaults.update(overrides)
    return SchedulerState(**defaults)


# ── Command handler fixture ──────────────────────────────────────────


@pytest.fixture
async def db(tmp_path):
    """Create a temp database."""
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
async def handler(db, tmp_path):
    """Create a CommandHandler with a real database."""
    config = AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "test.db"),
    )
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()

    cmd = CommandHandler(orchestrator, config)

    # Create test project directly in db (command derives id from name)
    await db.create_project(Project(id="test-proj", name="Test Project"))

    # Create test agent (needed for affinity_agent_id validation)
    await db.create_agent(
        Agent(id="agent-1", name="claude-1", agent_type="claude", state=AgentState.IDLE)
    )

    return cmd


# ── Command handler tests (create, edit, get) ────────────────────────


class TestCreateTaskCoordinationParams:
    """Test create_task with agent_type, affinity, workspace_mode."""

    @pytest.mark.asyncio
    async def test_create_task_with_agent_type(self, handler, db):
        result = await handler.execute(
            "create_task",
            {"project_id": "test-proj", "title": "Review PR", "agent_type": "code-review"},
        )
        assert "error" not in result
        assert result.get("agent_type") == "code-review"

        # Verify persisted
        task = await db.get_task(result["created"])
        assert task.agent_type == "code-review"

    @pytest.mark.asyncio
    async def test_create_task_with_affinity_agent_id(self, handler, db):
        result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "Fix review feedback",
                "affinity_agent_id": "agent-1",
                "affinity_reason": "context",
            },
        )
        assert "error" not in result
        assert result.get("affinity_agent_id") == "agent-1"
        assert result.get("affinity_reason") == "context"

        task = await db.get_task(result["created"])
        assert task.affinity_agent_id == "agent-1"
        assert task.affinity_reason == "context"

    @pytest.mark.asyncio
    async def test_create_task_with_workspace_mode_exclusive(self, handler, db):
        result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "DB migration",
                "workspace_mode": "exclusive",
            },
        )
        assert "error" not in result
        assert result.get("workspace_mode") == "exclusive"

        task = await db.get_task(result["created"])
        assert task.workspace_mode == WorkspaceMode.EXCLUSIVE

    @pytest.mark.asyncio
    async def test_create_task_with_workspace_mode_branch_isolated(self, handler, db):
        result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "Explore approach A",
                "workspace_mode": "branch-isolated",
            },
        )
        assert "error" not in result
        assert result.get("workspace_mode") == "branch-isolated"

        task = await db.get_task(result["created"])
        assert task.workspace_mode == WorkspaceMode.BRANCH_ISOLATED

    @pytest.mark.asyncio
    async def test_create_task_with_all_coordination_params(self, handler, db):
        """Create a task with all coordination params at once (full playbook scenario)."""
        result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "Address review feedback on auth module",
                "agent_type": "coding",
                "affinity_agent_id": "agent-1",
                "affinity_reason": "context",
                "workspace_mode": "branch-isolated",
                "priority": 2,
            },
        )
        assert "error" not in result
        assert result.get("agent_type") == "coding"
        assert result.get("affinity_agent_id") == "agent-1"
        assert result.get("affinity_reason") == "context"
        assert result.get("workspace_mode") == "branch-isolated"

        task = await db.get_task(result["created"])
        assert task.agent_type == "coding"
        assert task.affinity_agent_id == "agent-1"
        assert task.affinity_reason == "context"
        assert task.workspace_mode == WorkspaceMode.BRANCH_ISOLATED
        assert task.priority == 2

    @pytest.mark.asyncio
    async def test_create_task_without_coordination_params(self, handler, db):
        """Tasks without coordination params should still work (backward compat)."""
        result = await handler.execute(
            "create_task",
            {"project_id": "test-proj", "title": "Simple task"},
        )
        assert "error" not in result
        assert "agent_type" not in result
        assert "affinity_agent_id" not in result
        assert "workspace_mode" not in result

        task = await db.get_task(result["created"])
        assert task.agent_type is None
        assert task.affinity_agent_id is None
        assert task.affinity_reason is None
        assert task.workspace_mode is None

    @pytest.mark.asyncio
    async def test_create_task_directory_isolated_warns(self, handler):
        """directory-isolated is accepted but triggers a warning (deferred feature)."""
        result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "Monorepo work",
                "workspace_mode": "directory-isolated",
            },
        )
        assert "error" not in result
        assert result.get("workspace_mode") == "directory-isolated"
        assert "warning" in result
        assert "not yet implemented" in result["warning"]


class TestCreateTaskValidation:
    """Test validation of coordination parameters."""

    @pytest.mark.asyncio
    async def test_invalid_agent_type_not_string(self, handler):
        result = await handler.execute(
            "create_task",
            {"project_id": "test-proj", "title": "Bad", "agent_type": 123},
        )
        assert "error" in result
        assert "agent_type must be a string" in result["error"]

    @pytest.mark.asyncio
    async def test_empty_agent_type(self, handler):
        result = await handler.execute(
            "create_task",
            {"project_id": "test-proj", "title": "Bad", "agent_type": "  "},
        )
        assert "error" in result
        assert "agent_type cannot be empty" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_affinity_agent_id(self, handler):
        result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "Bad",
                "affinity_agent_id": "nonexistent-agent",
            },
        )
        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_affinity_reason(self, handler):
        result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "Bad",
                "affinity_reason": "invalid-reason",
            },
        )
        assert "error" in result
        assert "Invalid affinity_reason" in result["error"]

    @pytest.mark.asyncio
    async def test_valid_affinity_reasons(self, handler):
        """All three valid reasons should be accepted."""
        for reason in ["context", "workspace", "type"]:
            result = await handler.execute(
                "create_task",
                {
                    "project_id": "test-proj",
                    "title": f"Task with {reason} affinity",
                    "affinity_reason": reason,
                },
            )
            assert "error" not in result, f"Failed for reason: {reason}"

    @pytest.mark.asyncio
    async def test_invalid_workspace_mode(self, handler):
        result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "Bad",
                "workspace_mode": "invalid-mode",
            },
        )
        assert "error" in result
        assert "Invalid workspace_mode" in result["error"]


class TestGetTaskCoordinationParams:
    """Test that get_task returns coordination parameters."""

    @pytest.mark.asyncio
    async def test_get_task_returns_coordination_fields(self, handler, db):
        create_result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "Feature task",
                "agent_type": "coding",
                "affinity_agent_id": "agent-1",
                "affinity_reason": "context",
                "workspace_mode": "branch-isolated",
            },
        )
        task_id = create_result["created"]

        result = await handler.execute("get_task", {"task_id": task_id})
        assert result.get("agent_type") == "coding"
        assert result.get("affinity_agent_id") == "agent-1"
        assert result.get("affinity_reason") == "context"
        assert result.get("workspace_mode") == "branch-isolated"

    @pytest.mark.asyncio
    async def test_get_task_returns_null_for_unset_fields(self, handler, db):
        create_result = await handler.execute(
            "create_task",
            {"project_id": "test-proj", "title": "Simple task"},
        )
        task_id = create_result["created"]

        result = await handler.execute("get_task", {"task_id": task_id})
        assert result.get("agent_type") is None
        assert result.get("affinity_agent_id") is None
        assert result.get("affinity_reason") is None
        assert result.get("workspace_mode") is None


class TestEditTaskCoordinationParams:
    """Test edit_task with coordination parameters."""

    @pytest.mark.asyncio
    async def test_edit_task_set_agent_type(self, handler, db):
        create_result = await handler.execute(
            "create_task",
            {"project_id": "test-proj", "title": "Task"},
        )
        task_id = create_result["created"]

        result = await handler.execute("edit_task", {"task_id": task_id, "agent_type": "qa"})
        assert "error" not in result
        assert "agent_type" in result["fields"]

        task = await db.get_task(task_id)
        assert task.agent_type == "qa"

    @pytest.mark.asyncio
    async def test_edit_task_clear_agent_type(self, handler, db):
        create_result = await handler.execute(
            "create_task",
            {"project_id": "test-proj", "title": "Task", "agent_type": "coding"},
        )
        task_id = create_result["created"]

        result = await handler.execute("edit_task", {"task_id": task_id, "agent_type": None})
        assert "error" not in result

        task = await db.get_task(task_id)
        assert task.agent_type is None

    @pytest.mark.asyncio
    async def test_edit_task_set_affinity(self, handler, db):
        create_result = await handler.execute(
            "create_task",
            {"project_id": "test-proj", "title": "Task"},
        )
        task_id = create_result["created"]

        result = await handler.execute(
            "edit_task",
            {
                "task_id": task_id,
                "affinity_agent_id": "agent-1",
                "affinity_reason": "workspace",
            },
        )
        assert "error" not in result

        task = await db.get_task(task_id)
        assert task.affinity_agent_id == "agent-1"
        assert task.affinity_reason == "workspace"

    @pytest.mark.asyncio
    async def test_edit_task_clear_affinity(self, handler, db):
        create_result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "Task",
                "affinity_agent_id": "agent-1",
                "affinity_reason": "context",
            },
        )
        task_id = create_result["created"]

        result = await handler.execute(
            "edit_task",
            {
                "task_id": task_id,
                "affinity_agent_id": None,
                "affinity_reason": None,
            },
        )
        assert "error" not in result

        task = await db.get_task(task_id)
        assert task.affinity_agent_id is None
        assert task.affinity_reason is None

    @pytest.mark.asyncio
    async def test_edit_task_set_workspace_mode(self, handler, db):
        create_result = await handler.execute(
            "create_task",
            {"project_id": "test-proj", "title": "Task"},
        )
        task_id = create_result["created"]

        result = await handler.execute(
            "edit_task", {"task_id": task_id, "workspace_mode": "branch-isolated"}
        )
        assert "error" not in result

        task = await db.get_task(task_id)
        assert task.workspace_mode == WorkspaceMode.BRANCH_ISOLATED

    @pytest.mark.asyncio
    async def test_edit_task_clear_workspace_mode(self, handler, db):
        create_result = await handler.execute(
            "create_task",
            {
                "project_id": "test-proj",
                "title": "Task",
                "workspace_mode": "exclusive",
            },
        )
        task_id = create_result["created"]

        result = await handler.execute("edit_task", {"task_id": task_id, "workspace_mode": None})
        assert "error" not in result

        task = await db.get_task(task_id)
        assert task.workspace_mode is None

    @pytest.mark.asyncio
    async def test_edit_task_invalid_affinity_agent_id(self, handler):
        create_result = await handler.execute(
            "create_task",
            {"project_id": "test-proj", "title": "Task"},
        )
        task_id = create_result["created"]

        result = await handler.execute(
            "edit_task", {"task_id": task_id, "affinity_agent_id": "bad-agent"}
        )
        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_edit_task_invalid_affinity_reason(self, handler):
        create_result = await handler.execute(
            "create_task",
            {"project_id": "test-proj", "title": "Task"},
        )
        task_id = create_result["created"]

        result = await handler.execute(
            "edit_task", {"task_id": task_id, "affinity_reason": "bad-reason"}
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_edit_task_invalid_workspace_mode(self, handler):
        create_result = await handler.execute(
            "create_task",
            {"project_id": "test-proj", "title": "Task"},
        )
        task_id = create_result["created"]

        result = await handler.execute(
            "edit_task", {"task_id": task_id, "workspace_mode": "bad-mode"}
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_edit_task_directory_isolated_warns(self, handler):
        create_result = await handler.execute(
            "create_task",
            {"project_id": "test-proj", "title": "Task"},
        )
        task_id = create_result["created"]

        result = await handler.execute(
            "edit_task", {"task_id": task_id, "workspace_mode": "directory-isolated"}
        )
        assert "error" not in result
        assert "warning" in result
        assert "not yet implemented" in result["warning"]


# ── Scheduler affinity tests ─────────────────────────────────────────


class TestSchedulerAffinityAgent:
    """Test that the scheduler prefers affinity_agent_id when assigning tasks."""

    def test_affinity_agent_preferred_when_idle(self):
        """When the affinity agent is idle, other agents defer the task to it."""
        task = make_task(
            id="t-1",
            priority=100,
            affinity_agent_id="a-2",
            affinity_reason="context",
        )
        t_other = make_task(id="t-other", priority=100)
        state = make_state(
            tasks=[task, t_other],
            agents=[
                make_agent(id="a-1", name="claude-1"),
                make_agent(id="a-2", name="claude-2"),
            ],
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 2
        # a-2 should get the affinity task, a-1 should get the other task
        a2_action = next(a for a in actions if a.agent_id == "a-2")
        a1_action = next(a for a in actions if a.agent_id == "a-1")
        assert a2_action.task_id == "t-1"
        assert a1_action.task_id == "t-other"

    def test_affinity_single_task_still_assigned(self):
        """If the only task has affinity for another agent, still assign it (no starvation)."""
        task = make_task(
            id="t-1",
            priority=100,
            affinity_agent_id="a-2",
        )
        state = make_state(
            tasks=[task],
            agents=[
                make_agent(id="a-1", name="claude-1"),
                make_agent(id="a-2", name="claude-2"),
            ],
        )
        actions = Scheduler.schedule(state)
        # Task should still be assigned (not starved)
        assert len(actions) >= 1
        task_action = next(a for a in actions if a.task_id == "t-1")
        # The first agent processed will pick it up since it's the only task
        # but the affinity agent should get it if it's processed
        assert task_action.agent_id in ("a-1", "a-2")

    def test_affinity_fallback_when_agent_busy(self):
        """When the affinity agent is busy, assign to any available agent."""
        task = make_task(
            id="t-1",
            priority=100,
            affinity_agent_id="a-2",
        )
        state = make_state(
            tasks=[task],
            agents=[
                make_agent(id="a-1", name="claude-1"),
                make_agent(id="a-2", name="claude-2", state=AgentState.BUSY),
            ],
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].agent_id == "a-1"  # fallback to available agent
        assert actions[0].task_id == "t-1"

    def test_affinity_overrides_priority_for_same_agent(self):
        """An affinity task with lower priority is picked first for the matching agent."""
        # t-high has priority 10 (higher priority, no affinity)
        # t-affinity has priority 50 (lower priority, but affinity for a-1)
        # When a-1 is being scheduled, t-affinity should be picked because
        # it has affinity for a-1
        t_high = make_task(id="t-high", priority=10)
        t_affinity = make_task(
            id="t-affinity",
            priority=50,
            affinity_agent_id="a-1",
        )
        state = make_state(
            tasks=[t_high, t_affinity],
            agents=[make_agent(id="a-1")],
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-affinity"
        assert actions[0].agent_id == "a-1"

    def test_no_affinity_uses_standard_priority(self):
        """Without affinity, tasks are assigned by priority as before."""
        t_high = make_task(id="t-high", priority=10)
        t_low = make_task(id="t-low", priority=50)
        state = make_state(
            tasks=[t_high, t_low],
            agents=[make_agent(id="a-1")],
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-high"

    def test_affinity_for_different_agent_no_boost(self):
        """Affinity for a different agent doesn't boost the task for this agent."""
        t_high = make_task(id="t-high", priority=10)
        t_affinity_other = make_task(
            id="t-affinity",
            priority=50,
            affinity_agent_id="a-OTHER",  # not the idle agent
        )
        state = make_state(
            tasks=[t_high, t_affinity_other],
            agents=[make_agent(id="a-1")],
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-high"  # standard priority wins

    def test_two_agents_one_affinity(self):
        """Two idle agents, one task with affinity — correct agent gets affinity task."""
        t_no_affinity = make_task(id="t-1", priority=10)
        t_affinity = make_task(
            id="t-2",
            priority=50,
            affinity_agent_id="a-2",
        )
        state = make_state(
            tasks=[t_no_affinity, t_affinity],
            agents=[
                make_agent(id="a-1"),
                make_agent(id="a-2"),
            ],
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 2

        a1_action = next(a for a in actions if a.agent_id == "a-1")
        a2_action = next(a for a in actions if a.agent_id == "a-2")
        # a-2 should get the affinity task
        assert a2_action.task_id == "t-2"
        # a-1 should get the other task
        assert a1_action.task_id == "t-1"

    def test_multiple_tasks_with_different_affinities(self):
        """Multiple tasks each with affinity for different agents."""
        t1 = make_task(id="t-1", priority=100, affinity_agent_id="a-1")
        t2 = make_task(id="t-2", priority=100, affinity_agent_id="a-2")
        state = make_state(
            tasks=[t1, t2],
            agents=[
                make_agent(id="a-1"),
                make_agent(id="a-2"),
            ],
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 2

        a1_action = next(a for a in actions if a.agent_id == "a-1")
        a2_action = next(a for a in actions if a.agent_id == "a-2")
        assert a1_action.task_id == "t-1"
        assert a2_action.task_id == "t-2"

    def test_affinity_with_workspace_mode_stored(self):
        """workspace_mode is stored on the task but doesn't affect scheduler assignment."""
        task = make_task(
            id="t-1",
            priority=100,
            workspace_mode=WorkspaceMode.BRANCH_ISOLATED,
        )
        state = make_state(
            tasks=[task],
            agents=[make_agent(id="a-1")],
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-1"

    def test_agent_type_matching_filters_assignment(self):
        """agent_type is a hard constraint: task only assigned to matching agent."""
        task = make_task(id="t-1", agent_type="code-review")
        state = make_state(
            tasks=[task],
            agents=[make_agent(id="a-1", agent_type="coding")],
        )
        # Task has agent_type="code-review" but agent is "coding" → no match
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_agent_type_matching_allows_matching_agent(self):
        """agent_type match → task IS assigned."""
        task = make_task(id="t-1", agent_type="coding")
        state = make_state(
            tasks=[task],
            agents=[make_agent(id="a-1", agent_type="coding")],
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-1"
        assert actions[0].agent_id == "a-1"

    def test_no_agent_type_matches_any_agent(self):
        """Tasks without agent_type are assigned to any available agent."""
        task = make_task(id="t-1")  # agent_type=None
        state = make_state(
            tasks=[task],
            agents=[make_agent(id="a-1", agent_type="code-review")],
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-1"


# ── Database round-trip tests ─────────────────────────────────────────


class TestCoordinationParamsDbRoundTrip:
    """Test that coordination params survive create → get → archive → restore."""

    @pytest.fixture
    async def testdb(self, tmp_path):
        database = Database(str(tmp_path / "roundtrip.db"))
        await database.initialize()
        # Create a project to satisfy FK
        await database.create_project(Project(id="p-1", name="test"))
        # Create agent for affinity FK validation
        await database.create_agent(Agent(id="agent-1", name="claude-1", agent_type="claude"))
        yield database
        await database.close()

    @pytest.mark.asyncio
    async def test_full_coordination_params_round_trip(self, testdb):
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Coordinated task",
            description="Full coordination",
            agent_type="coding",
            affinity_agent_id="agent-1",
            affinity_reason="context",
            workspace_mode=WorkspaceMode.BRANCH_ISOLATED,
        )
        await testdb.create_task(task)

        loaded = await testdb.get_task("t-1")
        assert loaded is not None
        assert loaded.agent_type == "coding"
        assert loaded.affinity_agent_id == "agent-1"
        assert loaded.affinity_reason == "context"
        assert loaded.workspace_mode == WorkspaceMode.BRANCH_ISOLATED

    @pytest.mark.asyncio
    async def test_null_coordination_params(self, testdb):
        task = Task(
            id="t-2",
            project_id="p-1",
            title="Simple task",
            description="No coordination",
        )
        await testdb.create_task(task)

        loaded = await testdb.get_task("t-2")
        assert loaded is not None
        assert loaded.agent_type is None
        assert loaded.affinity_agent_id is None
        assert loaded.affinity_reason is None
        assert loaded.workspace_mode is None

    @pytest.mark.asyncio
    async def test_update_coordination_params(self, testdb):
        task = Task(
            id="t-3",
            project_id="p-1",
            title="Updatable task",
            description="test",
        )
        await testdb.create_task(task)

        await testdb.update_task(
            "t-3",
            agent_type="qa",
            affinity_agent_id="agent-1",
            affinity_reason="workspace",
            workspace_mode=WorkspaceMode.EXCLUSIVE,
        )

        loaded = await testdb.get_task("t-3")
        assert loaded.agent_type == "qa"
        assert loaded.affinity_agent_id == "agent-1"
        assert loaded.affinity_reason == "workspace"
        assert loaded.workspace_mode == WorkspaceMode.EXCLUSIVE

    @pytest.mark.asyncio
    async def test_archive_preserves_coordination_params(self, testdb):
        task = Task(
            id="t-4",
            project_id="p-1",
            title="Archivable task",
            description="test",
            status=TaskStatus.COMPLETED,
            agent_type="code-review",
            affinity_agent_id="agent-1",
            affinity_reason="type",
            workspace_mode=WorkspaceMode.BRANCH_ISOLATED,
        )
        await testdb.create_task(task)
        await testdb.archive_task("t-4")

        archived = await testdb.get_archived_task("t-4")
        assert archived is not None
        assert archived["agent_type"] == "code-review"
        assert archived["affinity_agent_id"] == "agent-1"
        assert archived["affinity_reason"] == "type"
        assert archived["workspace_mode"] == "branch-isolated"

    @pytest.mark.asyncio
    async def test_restore_preserves_coordination_params(self, testdb):
        task = Task(
            id="t-5",
            project_id="p-1",
            title="Restorable task",
            description="test",
            status=TaskStatus.COMPLETED,
            agent_type="coding",
            affinity_agent_id="agent-1",
            affinity_reason="context",
            workspace_mode=WorkspaceMode.EXCLUSIVE,
        )
        await testdb.create_task(task)
        await testdb.archive_task("t-5")
        await testdb.restore_archived_task("t-5")

        restored = await testdb.get_task("t-5")
        assert restored is not None
        assert restored.agent_type == "coding"
        assert restored.affinity_agent_id == "agent-1"
        assert restored.affinity_reason == "context"
        assert restored.workspace_mode == WorkspaceMode.EXCLUSIVE

    @pytest.mark.asyncio
    async def test_workspace_mode_enum_values(self, testdb):
        """All WorkspaceMode values can be round-tripped through the database."""
        for i, mode in enumerate(WorkspaceMode):
            tid = f"t-mode-{i}"
            task = Task(
                id=tid,
                project_id="p-1",
                title=f"Task with {mode.value}",
                description="test",
                workspace_mode=mode,
            )
            await testdb.create_task(task)
            loaded = await testdb.get_task(tid)
            assert loaded.workspace_mode == mode, f"Round-trip failed for {mode}"
