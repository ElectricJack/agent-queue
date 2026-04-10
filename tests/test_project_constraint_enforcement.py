"""Tests for project constraint enforcement — Roadmap 7.2.5.

Covers all eight test cases (a)-(h) from the roadmap:
  (a) exclusive=true blocks scheduler from assigning to other agents
  (b) release_project_constraint lifts the block — scheduler resumes
  (c) max_agents={"coding": 2} allows up to 2 coding agents, blocks a third
  (d) pause_scheduling=true stops all task assignment
  (e) constraint on project A does not affect project B
  (f) set constraint on non-existent project returns clear error
  (g) multiple constraints stack correctly (exclusive + max_agents)
  (h) constraint persists across scheduler tick cycles until released

Tests exercise three layers:
  1. Scheduler.schedule() — pure-function constraint filtering
  2. CommandHandler — set/release commands with validation
  3. Orchestrator._check_constraints_before_assignment() — pre-commit guard
"""

import pytest

from src.config import AppConfig
from src.models import (
    Agent,
    AgentState,
    Project,
    ProjectConstraint,
    Task,
    TaskStatus,
)
from src.orchestrator import Orchestrator
from src.command_handler import CommandHandler
from src.scheduler import AssignAction, Scheduler, SchedulerState


# ── Helpers ─────────────────────────────────────────────────────────────


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


def make_agent(id="a-1", name="claude-1", state=AgentState.IDLE, agent_type="claude", **kw):
    return Agent(id=id, name=name, agent_type=agent_type, state=state, **kw)


def _make_state(
    projects=None,
    tasks=None,
    agents=None,
    constraints=None,
    active_agent_counts=None,
    **kw,
):
    """Build a SchedulerState with sensible defaults for constraint tests."""
    return SchedulerState(
        projects=projects or [make_project()],
        tasks=tasks or [make_task()],
        agents=agents or [make_agent()],
        project_token_usage=kw.pop("project_token_usage", {}),
        project_active_agent_counts=active_agent_counts or {},
        tasks_completed_in_window=kw.pop("tasks_completed_in_window", {}),
        project_constraints=constraints or {},
        **kw,
    )


# ── Orchestrator / DB fixtures ──────────────────────────────────────────


@pytest.fixture
async def orch(tmp_path):
    """Minimal orchestrator backed by a real SQLite database."""
    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    o = Orchestrator(config)
    await o.initialize()
    yield o
    await o.shutdown()


@pytest.fixture
async def db(orch):
    return orch.db


@pytest.fixture
async def handler(orch, tmp_path):
    """CommandHandler wired to the real orchestrator + DB."""
    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    return CommandHandler(orch, config)


async def _ensure_project(db, project_id="p-1"):
    existing = await db.get_project(project_id)
    if not existing:
        await db.create_project(Project(id=project_id, name=f"Project {project_id}"))


async def _setup(db, project_id="p-1", agent_id="a-1", task_id="t-1", agent_type="claude"):
    """Create a project, agent, and READY task for testing."""
    await _ensure_project(db, project_id)
    await db.create_agent(Agent(id=agent_id, name=f"agent-{agent_id}", agent_type=agent_type))
    await db.create_task(
        Task(
            id=task_id,
            project_id=project_id,
            title=f"Task {task_id}",
            description="test task",
            status=TaskStatus.READY,
        )
    )


async def _make_busy(db, task_id, agent_id):
    """Assign a task to an agent and move it to IN_PROGRESS."""
    await db.assign_task_to_agent(task_id, agent_id)
    await db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test")


# ═══════════════════════════════════════════════════════════════════════
# (a) exclusive=true blocks scheduler from assigning other agents
# ═══════════════════════════════════════════════════════════════════════


class TestExclusiveBlocksOtherAgents:
    """Roadmap case (a): exclusive constraint limits to one agent on the project."""

    def test_scheduler_exclusive_blocks_second_agent(self):
        """Scheduler produces no action for a second agent when exclusive=true
        and one agent is already active on the project."""
        state = _make_state(
            projects=[make_project(max_agents=3)],
            tasks=[
                make_task(id="t-1", status=TaskStatus.IN_PROGRESS),
                make_task(id="t-2"),
            ],
            agents=[
                make_agent(id="a-1", state=AgentState.BUSY),
                make_agent(id="a-2"),
            ],
            constraints={"p-1": ProjectConstraint(project_id="p-1", exclusive=True)},
            active_agent_counts={"p-1": 1},
        )
        actions = Scheduler.schedule(state)
        # exclusive overrides max_agents to 1, and project already has 1 active
        assert len(actions) == 0

    def test_scheduler_exclusive_allows_first_agent(self):
        """Scheduler allows the first agent on an exclusive project."""
        state = _make_state(
            constraints={"p-1": ProjectConstraint(project_id="p-1", exclusive=True)},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].agent_id == "a-1"

    async def test_preassign_exclusive_blocks_second_agent(self, orch, db):
        """Pre-assignment check blocks when another agent is already busy."""
        await _setup(db, agent_id="a-1", task_id="t-1")
        await _setup(db, project_id="p-1", agent_id="a-2", task_id="t-2")
        await _make_busy(db, "t-1", "a-1")
        await db.set_project_constraint(ProjectConstraint(project_id="p-1", exclusive=True))
        action = AssignAction(agent_id="a-2", task_id="t-2", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is not None
        assert "exclusive" in result

    async def test_preassign_exclusive_allows_first_agent(self, orch, db):
        """Pre-assignment check allows when no agents are active on the project."""
        await _setup(db)
        await db.set_project_constraint(ProjectConstraint(project_id="p-1", exclusive=True))
        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# (b) release_project_constraint lifts the block
# ═══════════════════════════════════════════════════════════════════════


class TestReleaseConstraintResumesScheduling:
    """Roadmap case (b): releasing a constraint lets the scheduler resume."""

    def test_scheduler_resumes_after_constraint_removed(self):
        """Scheduler assigns tasks after constraint is removed from state."""
        # With constraint: no scheduling
        state_blocked = _make_state(
            constraints={"p-1": ProjectConstraint(project_id="p-1", pause_scheduling=True)},
        )
        assert len(Scheduler.schedule(state_blocked)) == 0

        # Without constraint: scheduling resumes
        state_free = _make_state(constraints={})
        actions = Scheduler.schedule(state_free)
        assert len(actions) == 1

    async def test_command_release_removes_constraint(self, handler, db):
        """release_project_constraint command removes the DB constraint."""
        await _ensure_project(db, "p-1")
        await db.set_project_constraint(ProjectConstraint(project_id="p-1", exclusive=True))
        # Constraint exists
        assert await db.get_project_constraint("p-1") is not None

        result = await handler.execute("release_project_constraint", {"project_id": "p-1"})
        assert "error" not in result
        assert result["constraint_released"] is True

        # Constraint gone
        assert await db.get_project_constraint("p-1") is None

    async def test_preassign_allows_after_release(self, orch, db):
        """After releasing a constraint, pre-assignment check passes."""
        await _setup(db)
        await db.set_project_constraint(ProjectConstraint(project_id="p-1", pause_scheduling=True))
        # Blocked
        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")
        assert await orch._check_constraints_before_assignment(action) is not None

        # Release
        await db.delete_project_constraint("p-1")

        # Now allowed
        assert await orch._check_constraints_before_assignment(action) is None

    async def test_release_exclusive_resumes_second_agent(self, orch, db):
        """Releasing exclusive allows a second agent to be assigned."""
        await _setup(db, agent_id="a-1", task_id="t-1")
        await _setup(db, project_id="p-1", agent_id="a-2", task_id="t-2")
        await _make_busy(db, "t-1", "a-1")
        await db.set_project_constraint(ProjectConstraint(project_id="p-1", exclusive=True))

        # Blocked while exclusive is active
        action = AssignAction(agent_id="a-2", task_id="t-2", project_id="p-1")
        assert await orch._check_constraints_before_assignment(action) is not None

        # Release exclusive
        await db.delete_project_constraint("p-1")

        # Now allowed
        assert await orch._check_constraints_before_assignment(action) is None


# ═══════════════════════════════════════════════════════════════════════
# (c) max_agents={"coding": 2} allows up to 2, blocks third
# ═══════════════════════════════════════════════════════════════════════


class TestMaxAgentsByType:
    """Roadmap case (c): per-type agent limits."""

    def test_scheduler_type_limit_allows_under_cap(self):
        """Scheduler assigns when type count is below limit."""
        state = _make_state(
            constraints={
                "p-1": ProjectConstraint(project_id="p-1", max_agents_by_type={"claude": 2})
            },
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1

    def test_scheduler_type_limit_allows_second_agent(self):
        """Scheduler assigns a second agent when limit=2 and only one is active."""
        state = _make_state(
            projects=[make_project(max_agents=3)],
            tasks=[
                make_task(id="t-1", status=TaskStatus.IN_PROGRESS),
                make_task(id="t-2"),
            ],
            agents=[
                make_agent(id="a-1", state=AgentState.BUSY, agent_type="coding"),
                make_agent(id="a-2", agent_type="coding"),
            ],
            constraints={
                "p-1": ProjectConstraint(project_id="p-1", max_agents_by_type={"coding": 2})
            },
            active_agent_counts={"p-1": 1},
        )
        # a-1 is BUSY on t-1 (coding), a-2 is IDLE (coding).
        # Limit is 2, only 1 active — a-2 should be assigned.
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].agent_id == "a-2"

    def test_scheduler_type_limit_blocks_third_agent(self):
        """Scheduler blocks a third coding agent when limit=2 and two are active."""
        state = _make_state(
            projects=[make_project(max_agents=5)],
            tasks=[
                make_task(id="t-1", status=TaskStatus.IN_PROGRESS),
                make_task(id="t-2", status=TaskStatus.IN_PROGRESS),
                make_task(id="t-3"),
            ],
            agents=[
                make_agent(
                    id="a-1",
                    state=AgentState.BUSY,
                    agent_type="coding",
                    current_task_id="t-1",
                ),
                make_agent(
                    id="a-2",
                    state=AgentState.BUSY,
                    agent_type="coding",
                    current_task_id="t-2",
                ),
                make_agent(id="a-3", agent_type="coding"),
            ],
            constraints={
                "p-1": ProjectConstraint(project_id="p-1", max_agents_by_type={"coding": 2})
            },
            active_agent_counts={"p-1": 2},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_scheduler_type_limit_allows_different_type(self):
        """A 'codex' agent is not limited by a cap on 'coding' agents."""
        state = _make_state(
            projects=[make_project(max_agents=5)],
            tasks=[
                make_task(id="t-1", status=TaskStatus.IN_PROGRESS),
                make_task(id="t-2", status=TaskStatus.IN_PROGRESS),
                make_task(id="t-3"),
            ],
            agents=[
                make_agent(
                    id="a-1",
                    state=AgentState.BUSY,
                    agent_type="coding",
                    current_task_id="t-1",
                ),
                make_agent(
                    id="a-2",
                    state=AgentState.BUSY,
                    agent_type="coding",
                    current_task_id="t-2",
                ),
                make_agent(id="a-3", agent_type="codex"),  # different type
            ],
            constraints={
                "p-1": ProjectConstraint(project_id="p-1", max_agents_by_type={"coding": 2})
            },
            active_agent_counts={"p-1": 2},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].agent_id == "a-3"

    async def test_preassign_type_limit_blocks_excess(self, orch, db):
        """Pre-assignment check blocks when the type limit is reached."""
        await _setup(db, agent_id="a-1", task_id="t-1", agent_type="coding")
        await _setup(db, project_id="p-1", agent_id="a-2", task_id="t-2", agent_type="coding")
        await _setup(db, project_id="p-1", agent_id="a-3", task_id="t-3", agent_type="coding")
        await _make_busy(db, "t-1", "a-1")
        await _make_busy(db, "t-2", "a-2")
        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", max_agents_by_type={"coding": 2})
        )

        action = AssignAction(agent_id="a-3", task_id="t-3", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is not None
        assert "max_agents_by_type" in result
        assert "coding" in result

    async def test_preassign_type_limit_allows_under_cap(self, orch, db):
        """Pre-assignment check allows when under the type limit."""
        await _setup(db, agent_id="a-1", task_id="t-1", agent_type="coding")
        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", max_agents_by_type={"coding": 2})
        )

        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# (d) pause_scheduling=true stops all task assignment
# ═══════════════════════════════════════════════════════════════════════


class TestPauseScheduling:
    """Roadmap case (d): pause_scheduling blocks all assignment."""

    def test_scheduler_pause_blocks_all_agents(self):
        """Scheduler produces no actions when pause_scheduling=True."""
        state = _make_state(
            projects=[make_project(max_agents=3)],
            tasks=[make_task(id="t-1"), make_task(id="t-2")],
            agents=[make_agent(id="a-1"), make_agent(id="a-2")],
            constraints={"p-1": ProjectConstraint(project_id="p-1", pause_scheduling=True)},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    async def test_command_set_pause_scheduling(self, handler, db):
        """set_project_constraint command with pause_scheduling=True stores the constraint."""
        await _ensure_project(db, "p-1")
        result = await handler.execute(
            "set_project_constraint",
            {"project_id": "p-1", "pause_scheduling": True},
        )
        assert "error" not in result
        assert "pause_scheduling" in result["active_fields"]

        c = await db.get_project_constraint("p-1")
        assert c is not None
        assert c.pause_scheduling is True

    async def test_preassign_pause_blocks(self, orch, db):
        """Pre-assignment check blocks when pause_scheduling is active."""
        await _setup(db)
        await db.set_project_constraint(ProjectConstraint(project_id="p-1", pause_scheduling=True))
        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is not None
        assert "pause_scheduling" in result


# ═══════════════════════════════════════════════════════════════════════
# (e) constraint on project A does not affect project B
# ═══════════════════════════════════════════════════════════════════════


class TestProjectIsolation:
    """Roadmap case (e): constraints are per-project, no cross-contamination."""

    def test_scheduler_constraint_a_does_not_block_b(self):
        """Pausing project A still allows assignment on project B."""
        state = _make_state(
            projects=[
                make_project(id="p-a", name="Alpha"),
                make_project(id="p-b", name="Beta"),
            ],
            tasks=[
                make_task(id="t-a", project_id="p-a"),
                make_task(id="t-b", project_id="p-b"),
            ],
            agents=[make_agent(id="a-1"), make_agent(id="a-2")],
            constraints={"p-a": ProjectConstraint(project_id="p-a", pause_scheduling=True)},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].project_id == "p-b"

    def test_scheduler_exclusive_a_does_not_block_b(self):
        """Exclusive on project A with an active agent does not block project B."""
        state = _make_state(
            projects=[
                make_project(id="p-a", name="Alpha", max_agents=3),
                make_project(id="p-b", name="Beta", max_agents=3),
            ],
            tasks=[
                make_task(id="t-a1", project_id="p-a", status=TaskStatus.IN_PROGRESS),
                make_task(id="t-a2", project_id="p-a"),
                make_task(id="t-b", project_id="p-b"),
            ],
            agents=[
                make_agent(id="a-1", state=AgentState.BUSY),
                make_agent(id="a-2"),
                make_agent(id="a-3"),
            ],
            constraints={"p-a": ProjectConstraint(project_id="p-a", exclusive=True)},
            active_agent_counts={"p-a": 1},
        )
        actions = Scheduler.schedule(state)
        # p-a is blocked by exclusive (1 agent already), p-b is free
        task_ids = {a.task_id for a in actions}
        assert "t-b" in task_ids
        assert "t-a2" not in task_ids

    def test_scheduler_type_limit_a_does_not_block_b(self):
        """A type limit on project A does not affect project B."""
        state = _make_state(
            projects=[
                make_project(id="p-a", name="Alpha", max_agents=3),
                make_project(id="p-b", name="Beta", max_agents=3),
            ],
            tasks=[
                make_task(id="t-a1", project_id="p-a", status=TaskStatus.IN_PROGRESS),
                make_task(id="t-a2", project_id="p-a"),
                make_task(id="t-b", project_id="p-b"),
            ],
            agents=[
                make_agent(
                    id="a-1",
                    state=AgentState.BUSY,
                    agent_type="coding",
                    current_task_id="t-a1",
                ),
                make_agent(id="a-2", agent_type="coding"),
                make_agent(id="a-3", agent_type="coding"),
            ],
            constraints={
                "p-a": ProjectConstraint(project_id="p-a", max_agents_by_type={"coding": 1})
            },
            active_agent_counts={"p-a": 1},
        )
        actions = Scheduler.schedule(state)
        task_ids = {a.task_id for a in actions}
        assert "t-b" in task_ids
        assert "t-a2" not in task_ids

    async def test_preassign_constraint_a_does_not_block_b(self, orch, db):
        """Pre-assignment check for project B passes when project A is paused."""
        await _setup(db, project_id="p-a", agent_id="a-1", task_id="t-a")
        await _setup(db, project_id="p-b", agent_id="a-2", task_id="t-b")
        await db.set_project_constraint(ProjectConstraint(project_id="p-a", pause_scheduling=True))

        # p-a is blocked
        action_a = AssignAction(agent_id="a-1", task_id="t-a", project_id="p-a")
        assert await orch._check_constraints_before_assignment(action_a) is not None

        # p-b is unaffected
        action_b = AssignAction(agent_id="a-2", task_id="t-b", project_id="p-b")
        assert await orch._check_constraints_before_assignment(action_b) is None


# ═══════════════════════════════════════════════════════════════════════
# (f) set constraint on non-existent project returns clear error
# ═══════════════════════════════════════════════════════════════════════


class TestNonExistentProjectError:
    """Roadmap case (f): attempting to constrain a missing project fails cleanly."""

    async def test_set_constraint_on_missing_project_returns_error(self, handler):
        """set_project_constraint on a non-existent project returns an error dict."""
        result = await handler.execute(
            "set_project_constraint",
            {"project_id": "nonexistent-project", "exclusive": True},
        )
        assert "error" in result
        assert "nonexistent-project" in result["error"]

    async def test_release_constraint_on_missing_project_returns_error(self, handler):
        """release_project_constraint on a non-existent project returns an error dict."""
        result = await handler.execute(
            "release_project_constraint",
            {"project_id": "nonexistent-project"},
        )
        assert "error" in result
        assert "nonexistent-project" in result["error"]

    async def test_set_constraint_empty_fields_returns_error(self, handler, db):
        """set_project_constraint with no actual constraint fields returns an error."""
        await _ensure_project(db, "p-1")
        result = await handler.execute(
            "set_project_constraint",
            {"project_id": "p-1"},  # no exclusive, pause, or max_agents
        )
        assert "error" in result
        assert "At least one constraint" in result["error"]


# ═══════════════════════════════════════════════════════════════════════
# (g) multiple constraints stack correctly (exclusive + max_agents)
# ═══════════════════════════════════════════════════════════════════════


class TestConstraintStacking:
    """Roadmap case (g): combined constraints are enforced together."""

    def test_scheduler_exclusive_plus_type_limit(self):
        """Exclusive + type limit: exclusive blocks before type limit matters."""
        state = _make_state(
            projects=[make_project(max_agents=5)],
            tasks=[
                make_task(id="t-1", status=TaskStatus.IN_PROGRESS),
                make_task(id="t-2"),
            ],
            agents=[
                make_agent(id="a-1", state=AgentState.BUSY, agent_type="claude"),
                make_agent(id="a-2", agent_type="claude"),
            ],
            constraints={
                "p-1": ProjectConstraint(
                    project_id="p-1",
                    exclusive=True,
                    max_agents_by_type={"claude": 3},
                )
            },
            active_agent_counts={"p-1": 1},
        )
        # exclusive limits to 1 agent, so t-2 should not be assigned
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_scheduler_pause_plus_exclusive(self):
        """pause_scheduling takes precedence — blocks even if no agents active."""
        state = _make_state(
            constraints={
                "p-1": ProjectConstraint(
                    project_id="p-1",
                    pause_scheduling=True,
                    exclusive=True,
                )
            },
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_scheduler_pause_plus_type_limit(self):
        """pause_scheduling blocks even when type limits would allow assignment."""
        state = _make_state(
            constraints={
                "p-1": ProjectConstraint(
                    project_id="p-1",
                    pause_scheduling=True,
                    max_agents_by_type={"claude": 10},
                )
            },
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    async def test_command_stacks_constraints_incrementally(self, handler, db):
        """Setting constraint fields one at a time merges them (stacking behavior)."""
        await _ensure_project(db, "p-1")

        # First: set exclusive
        result = await handler.execute(
            "set_project_constraint",
            {"project_id": "p-1", "exclusive": True},
        )
        assert "error" not in result
        c = await db.get_project_constraint("p-1")
        assert c.exclusive is True
        assert c.pause_scheduling is False

        # Second: add pause_scheduling — should merge with existing exclusive
        result = await handler.execute(
            "set_project_constraint",
            {"project_id": "p-1", "pause_scheduling": True},
        )
        assert "error" not in result
        c = await db.get_project_constraint("p-1")
        assert c.exclusive is True  # preserved from earlier
        assert c.pause_scheduling is True

    async def test_command_release_partial_fields(self, handler, db):
        """Releasing one field keeps the other constraint fields active."""
        await _ensure_project(db, "p-1")

        # Set both exclusive and pause_scheduling
        await handler.execute(
            "set_project_constraint",
            {"project_id": "p-1", "exclusive": True, "pause_scheduling": True},
        )
        c = await db.get_project_constraint("p-1")
        assert c.exclusive is True
        assert c.pause_scheduling is True

        # Release only pause_scheduling
        result = await handler.execute(
            "release_project_constraint",
            {"project_id": "p-1", "fields": ["pause_scheduling"]},
        )
        assert "error" not in result
        c = await db.get_project_constraint("p-1")
        assert c is not None
        assert c.exclusive is True  # still active
        assert c.pause_scheduling is False  # released

    async def test_preassign_exclusive_plus_type_limit(self, orch, db):
        """Pre-assignment checks both exclusive and type limits together."""
        await _setup(db, agent_id="a-1", task_id="t-1", agent_type="claude")
        await _setup(db, project_id="p-1", agent_id="a-2", task_id="t-2", agent_type="claude")
        await _make_busy(db, "t-1", "a-1")

        await db.set_project_constraint(
            ProjectConstraint(
                project_id="p-1",
                exclusive=True,
                max_agents_by_type={"claude": 3},
            )
        )

        # Exclusive blocks even though type limit (3) would allow it
        action = AssignAction(agent_id="a-2", task_id="t-2", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is not None
        assert "exclusive" in result

    async def test_preassign_pause_plus_exclusive(self, orch, db):
        """pause_scheduling is checked first, even with exclusive set."""
        await _setup(db)
        await db.set_project_constraint(
            ProjectConstraint(
                project_id="p-1",
                pause_scheduling=True,
                exclusive=True,
            )
        )
        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is not None
        assert "pause_scheduling" in result


# ═══════════════════════════════════════════════════════════════════════
# (h) constraint persists across scheduler tick cycles until released
# ═══════════════════════════════════════════════════════════════════════


class TestConstraintPersistsAcrossTicks:
    """Roadmap case (h): constraints survive multiple scheduling rounds."""

    def test_scheduler_constraint_blocks_across_multiple_ticks(self):
        """Run the scheduler multiple times — constraint blocks each time."""
        constraint_map = {"p-1": ProjectConstraint(project_id="p-1", pause_scheduling=True)}

        for tick in range(3):
            state = _make_state(
                constraints=constraint_map,
                tasks=[make_task(id=f"t-{tick}")],
            )
            actions = Scheduler.schedule(state)
            assert len(actions) == 0, f"Tick {tick}: constraint should still block"

    def test_scheduler_constraint_blocks_until_removed(self):
        """Constraint blocks for N ticks, then removal allows assignment."""
        constraint_map = {"p-1": ProjectConstraint(project_id="p-1", pause_scheduling=True)}

        # First two ticks: blocked
        for tick in range(2):
            state = _make_state(constraints=constraint_map)
            actions = Scheduler.schedule(state)
            assert len(actions) == 0

        # Third tick: constraint removed
        state = _make_state(constraints={})
        actions = Scheduler.schedule(state)
        assert len(actions) == 1

    async def test_db_constraint_persists_across_reads(self, db):
        """Constraint stored in DB persists across multiple get calls."""
        await _ensure_project(db, "p-1")
        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", exclusive=True, pause_scheduling=True)
        )

        for _ in range(3):
            c = await db.get_project_constraint("p-1")
            assert c is not None
            assert c.exclusive is True
            assert c.pause_scheduling is True

    async def test_preassign_constraint_persists_until_released(self, orch, db):
        """Pre-assignment check blocks across multiple invocations until release."""
        await _setup(db)
        await db.set_project_constraint(ProjectConstraint(project_id="p-1", pause_scheduling=True))

        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")

        # Blocked on each check
        for _ in range(3):
            result = await orch._check_constraints_before_assignment(action)
            assert result is not None
            assert "pause_scheduling" in result

        # Release the constraint
        await db.delete_project_constraint("p-1")

        # Now allowed
        result = await orch._check_constraints_before_assignment(action)
        assert result is None

    async def test_exclusive_constraint_persists_then_releases(self, orch, db):
        """Exclusive constraint blocks multiple assignments, then release allows."""
        await _setup(db, agent_id="a-1", task_id="t-1")
        await _setup(db, project_id="p-1", agent_id="a-2", task_id="t-2")
        await _make_busy(db, "t-1", "a-1")
        await db.set_project_constraint(ProjectConstraint(project_id="p-1", exclusive=True))

        action = AssignAction(agent_id="a-2", task_id="t-2", project_id="p-1")

        # Repeated checks: still blocked
        for _ in range(3):
            result = await orch._check_constraints_before_assignment(action)
            assert result is not None

        # Release
        await db.delete_project_constraint("p-1")

        # Now allowed
        assert await orch._check_constraints_before_assignment(action) is None


# ═══════════════════════════════════════════════════════════════════════
# Additional edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestConstraintEdgeCases:
    """Additional edge cases not explicitly listed in (a)-(h) but important
    for complete coverage of the constraint enforcement system."""

    async def test_set_constraint_via_command_stores_in_db(self, handler, db):
        """Verify the command path stores a well-formed constraint in the DB."""
        await _ensure_project(db, "p-1")
        result = await handler.execute(
            "set_project_constraint",
            {
                "project_id": "p-1",
                "exclusive": True,
                "max_agents_by_type": {"claude": 2, "codex": 1},
            },
        )
        assert "error" not in result
        c = await db.get_project_constraint("p-1")
        assert c is not None
        assert c.exclusive is True
        assert c.max_agents_by_type == {"claude": 2, "codex": 1}

    async def test_list_constraints_returns_all_active(self, db):
        """list_project_constraints returns all active constraints."""
        await _ensure_project(db, "p-1")
        await _ensure_project(db, "p-2")
        await db.set_project_constraint(ProjectConstraint(project_id="p-1", exclusive=True))
        await db.set_project_constraint(ProjectConstraint(project_id="p-2", pause_scheduling=True))

        constraints = await db.list_project_constraints()
        ids = {c.project_id for c in constraints}
        assert ids == {"p-1", "p-2"}

    async def test_overwrite_constraint_replaces_previous(self, db):
        """Setting a new constraint replaces the old one entirely at DB level."""
        await _ensure_project(db, "p-1")
        await db.set_project_constraint(ProjectConstraint(project_id="p-1", exclusive=True))
        c = await db.get_project_constraint("p-1")
        assert c.exclusive is True

        # Overwrite with a different constraint
        await db.set_project_constraint(ProjectConstraint(project_id="p-1", pause_scheduling=True))
        c = await db.get_project_constraint("p-1")
        assert c.pause_scheduling is True
        assert c.exclusive is False  # replaced, not merged

    def test_scheduler_empty_constraints_dict_no_effect(self):
        """An empty constraints dict is equivalent to no constraints."""
        state = _make_state(constraints={})
        actions = Scheduler.schedule(state)
        assert len(actions) == 1

    def test_scheduler_constraint_on_nonexistent_project_harmless(self):
        """A constraint for a project not in the state is silently ignored."""
        state = _make_state(
            constraints={"p-ghost": ProjectConstraint(project_id="p-ghost", pause_scheduling=True)},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1  # p-1 is unaffected

    async def test_delete_nonexistent_constraint_returns_false(self, db):
        """Deleting a constraint that doesn't exist returns False (not an error)."""
        result = await db.delete_project_constraint("p-nonexistent")
        assert result is False
