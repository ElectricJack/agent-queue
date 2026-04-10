"""Tests for pre-assignment constraint enforcement in the orchestrator.

Roadmap 7.2.4: The scheduler checks constraints with a point-in-time snapshot,
but the assignment happens asynchronously.  _check_constraints_before_assignment()
re-validates constraints right before committing the assignment to the DB, closing
the temporal gap between scheduler decision and actual assignment.

These tests verify that the orchestrator correctly blocks assignments when
constraints change between the scheduler's decision and the execution of the
background task.
"""

import pytest

from src.config import AppConfig
from src.models import (
    Agent,
    Project,
    ProjectConstraint,
    Task,
    TaskStatus,
)
from src.orchestrator import Orchestrator
from src.scheduler import AssignAction


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
async def orch(tmp_path):
    """Create a minimal orchestrator with an in-memory DB for testing."""
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
    """Shorthand for the orchestrator's database."""
    return orch.db


async def _ensure_project(db, project_id="p-1"):
    """Create a project if it doesn't exist yet."""
    existing = await db.get_project(project_id)
    if not existing:
        await db.create_project(Project(id=project_id, name=f"Project {project_id}"))


async def _setup_project_agent_task(db, project_id="p-1", agent_id="a-1", task_id="t-1",
                                     agent_type="claude"):
    """Create a project (if needed), agent, and READY task for constraint testing."""
    await _ensure_project(db, project_id)
    await db.create_agent(
        Agent(id=agent_id, name=f"agent-{agent_id}", agent_type=agent_type)
    )
    await db.create_task(
        Task(
            id=task_id,
            project_id=project_id,
            title=f"Task {task_id}",
            description="test task",
            status=TaskStatus.READY,
        )
    )


# ── No constraints → assignment allowed ──────────────────────────────


class TestNoConstraints:
    async def test_no_constraint_allows_assignment(self, orch, db):
        """With no constraints, _check_constraints_before_assignment returns None."""
        await _setup_project_agent_task(db)
        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")

        result = await orch._check_constraints_before_assignment(action)
        assert result is None

    async def test_empty_constraint_allows_assignment(self, orch, db):
        """A constraint with all defaults (no active fields) allows assignment."""
        await _setup_project_agent_task(db)
        # Set a constraint with all defaults (no actual restrictions)
        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1")
        )
        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")

        result = await orch._check_constraints_before_assignment(action)
        assert result is None


# ── pause_scheduling blocks assignment ───────────────────────────────


class TestPauseSchedulingEnforcement:
    async def test_pause_scheduling_blocks_assignment(self, orch, db):
        """pause_scheduling=True prevents the assignment."""
        await _setup_project_agent_task(db)
        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", pause_scheduling=True)
        )
        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")

        result = await orch._check_constraints_before_assignment(action)
        assert result is not None
        assert "pause_scheduling" in result

    async def test_pause_scheduling_does_not_affect_other_project(self, orch, db):
        """Pausing project p-1 does not block assignment to project p-2."""
        await _setup_project_agent_task(db, project_id="p-1")
        await _setup_project_agent_task(
            db, project_id="p-2", agent_id="a-2", task_id="t-2"
        )
        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", pause_scheduling=True)
        )
        # Assignment to p-2 should be fine
        action = AssignAction(agent_id="a-2", task_id="t-2", project_id="p-2")

        result = await orch._check_constraints_before_assignment(action)
        assert result is None


# ── exclusive blocks second agent ────────────────────────────────────


class TestExclusiveEnforcement:
    async def test_exclusive_blocks_when_agent_already_active(self, orch, db):
        """exclusive=True blocks a second agent when one is already BUSY on the project."""
        await _setup_project_agent_task(db, agent_id="a-1", task_id="t-1")
        await _setup_project_agent_task(
            db, project_id="p-1", agent_id="a-2", task_id="t-2"
        )

        # Make agent a-1 busy on task t-1 (which belongs to p-1)
        await db.assign_task_to_agent("t-1", "a-1")
        await db.transition_task("t-1", TaskStatus.IN_PROGRESS, context="test")

        # Set exclusive constraint
        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", exclusive=True)
        )

        # Try to assign t-2 to a-2 — should be blocked
        action = AssignAction(agent_id="a-2", task_id="t-2", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is not None
        assert "exclusive" in result

    async def test_exclusive_allows_first_agent(self, orch, db):
        """exclusive=True allows the first agent (no one else is active)."""
        await _setup_project_agent_task(db)
        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", exclusive=True)
        )

        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is None

    async def test_exclusive_does_not_count_agents_on_other_projects(self, orch, db):
        """An agent busy on project p-2 does not block exclusive assignment on p-1."""
        # Project p-1: where we want exclusive assignment
        await _setup_project_agent_task(db, project_id="p-1", agent_id="a-1", task_id="t-1")
        # Project p-2: has a busy agent
        await _setup_project_agent_task(db, project_id="p-2", agent_id="a-2", task_id="t-2")
        await db.assign_task_to_agent("t-2", "a-2")
        await db.transition_task("t-2", TaskStatus.IN_PROGRESS, context="test")

        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", exclusive=True)
        )

        # Assignment to p-1 should be allowed (no agents active on p-1)
        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is None


# ── max_agents_by_type enforcement ───────────────────────────────────


class TestMaxAgentsByTypeEnforcement:
    async def test_type_limit_blocks_excess(self, orch, db):
        """max_agents_by_type={"claude": 1} blocks a second claude agent."""
        await _setup_project_agent_task(
            db, agent_id="a-1", task_id="t-1", agent_type="claude"
        )
        await _setup_project_agent_task(
            db, project_id="p-1", agent_id="a-2", task_id="t-2", agent_type="claude"
        )

        # a-1 is already busy on p-1
        await db.assign_task_to_agent("t-1", "a-1")
        await db.transition_task("t-1", TaskStatus.IN_PROGRESS, context="test")

        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", max_agents_by_type={"claude": 1})
        )

        # Try to assign t-2 to a-2 (also claude) — should be blocked
        action = AssignAction(agent_id="a-2", task_id="t-2", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is not None
        assert "max_agents_by_type" in result
        assert "claude" in result

    async def test_type_limit_allows_different_type(self, orch, db):
        """max_agents_by_type={"claude": 1} does not restrict codex agents."""
        await _setup_project_agent_task(
            db, agent_id="a-1", task_id="t-1", agent_type="claude"
        )
        await _setup_project_agent_task(
            db, project_id="p-1", agent_id="a-codex", task_id="t-2", agent_type="codex"
        )

        # a-1 (claude) is already busy on p-1
        await db.assign_task_to_agent("t-1", "a-1")
        await db.transition_task("t-1", TaskStatus.IN_PROGRESS, context="test")

        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", max_agents_by_type={"claude": 1})
        )

        # Try to assign t-2 to a-codex (codex type) — should be allowed
        action = AssignAction(agent_id="a-codex", task_id="t-2", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is None

    async def test_type_limit_allows_under_limit(self, orch, db):
        """max_agents_by_type={"claude": 2} allows the first claude agent."""
        await _setup_project_agent_task(
            db, agent_id="a-1", task_id="t-1", agent_type="claude"
        )

        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", max_agents_by_type={"claude": 2})
        )

        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is None

    async def test_type_limit_does_not_count_other_projects(self, orch, db):
        """Agents on other projects don't count toward the type limit."""
        # p-1: target project with type limit
        await _setup_project_agent_task(
            db, project_id="p-1", agent_id="a-1", task_id="t-1", agent_type="claude"
        )
        # p-2: has a busy claude agent
        await _setup_project_agent_task(
            db, project_id="p-2", agent_id="a-2", task_id="t-2", agent_type="claude"
        )
        await db.assign_task_to_agent("t-2", "a-2")
        await db.transition_task("t-2", TaskStatus.IN_PROGRESS, context="test")

        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", max_agents_by_type={"claude": 1})
        )

        # Assigning claude to p-1 should be fine (only p-2's claude is busy)
        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is None


# ── Constraint stacking ──────────────────────────────────────────────


class TestConstraintStackingEnforcement:
    async def test_exclusive_plus_pause(self, orch, db):
        """pause_scheduling takes precedence — blocks even if exclusive would allow."""
        await _setup_project_agent_task(db)
        await db.set_project_constraint(
            ProjectConstraint(
                project_id="p-1",
                exclusive=True,
                pause_scheduling=True,
            )
        )

        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is not None
        assert "pause_scheduling" in result

    async def test_exclusive_plus_type_limit(self, orch, db):
        """exclusive + max_agents_by_type both checked — exclusive blocks first."""
        await _setup_project_agent_task(
            db, agent_id="a-1", task_id="t-1", agent_type="claude"
        )
        await _setup_project_agent_task(
            db, project_id="p-1", agent_id="a-2", task_id="t-2", agent_type="claude"
        )

        # a-1 is already busy
        await db.assign_task_to_agent("t-1", "a-1")
        await db.transition_task("t-1", TaskStatus.IN_PROGRESS, context="test")

        await db.set_project_constraint(
            ProjectConstraint(
                project_id="p-1",
                exclusive=True,
                max_agents_by_type={"claude": 2},
            )
        )

        # exclusive blocks (1 agent already active) even though type limit allows 2
        action = AssignAction(agent_id="a-2", task_id="t-2", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is not None
        assert "exclusive" in result


# ── Constraint set after scheduler decision ──────────────────────────


class TestConstraintChangedAfterScheduling:
    """Verify that constraints set AFTER the scheduler made its decision
    are still enforced at assignment time."""

    async def test_pause_added_after_scheduler_ran(self, orch, db):
        """If pause_scheduling is set between scheduler and assignment,
        the assignment is blocked."""
        await _setup_project_agent_task(db)

        # Simulate: scheduler ran with no constraints and produced an action
        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")

        # Now a playbook sets pause_scheduling before the action executes
        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", pause_scheduling=True)
        )

        # The pre-assignment check should catch this
        result = await orch._check_constraints_before_assignment(action)
        assert result is not None
        assert "pause_scheduling" in result

    async def test_exclusive_added_while_agent_busy(self, orch, db):
        """If exclusive is set after scheduler ran and another agent is already busy,
        the second assignment is blocked."""
        await _setup_project_agent_task(db, agent_id="a-1", task_id="t-1")
        await _setup_project_agent_task(
            db, project_id="p-1", agent_id="a-2", task_id="t-2"
        )

        # a-1 gets assigned first (no constraints at scheduler time)
        await db.assign_task_to_agent("t-1", "a-1")
        await db.transition_task("t-1", TaskStatus.IN_PROGRESS, context="test")

        # Scheduler already decided to assign t-2 to a-2
        action = AssignAction(agent_id="a-2", task_id="t-2", project_id="p-1")

        # Playbook now sets exclusive
        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", exclusive=True)
        )

        # Pre-assignment check catches the new constraint
        result = await orch._check_constraints_before_assignment(action)
        assert result is not None
        assert "exclusive" in result

    async def test_constraint_released_allows_assignment(self, orch, db):
        """If a constraint is released between scheduler and assignment,
        the assignment proceeds."""
        await _setup_project_agent_task(db)

        # Constraint was active when scheduler ran (but allowed 1 agent)
        await db.set_project_constraint(
            ProjectConstraint(project_id="p-1", pause_scheduling=True)
        )

        # Playbook releases the constraint before execution
        await db.delete_project_constraint("p-1")

        action = AssignAction(agent_id="a-1", task_id="t-1", project_id="p-1")
        result = await orch._check_constraints_before_assignment(action)
        assert result is None
