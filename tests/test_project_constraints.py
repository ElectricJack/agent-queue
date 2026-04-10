"""Tests for project constraint scheduling behavior.

Covers all test cases from Roadmap 7.2.2:
  (a) exclusive=true blocks all but one agent on the project
  (b) release_project_constraint lifts the block
  (c) max_agents_by_type allows up to N agents of a specific type
  (d) pause_scheduling=true stops all task assignment
  (e) constraint on project A does not affect project B
  (f) constraint on non-existent project returns error
  (g) multiple constraints stack correctly (exclusive + max_agents)
  (h) constraint persists across scheduler tick cycles until released
"""

import pytest

from src.models import (
    Agent,
    AgentState,
    Project,
    ProjectConstraint,
    Task,
    TaskStatus,
)
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


# ── (a) exclusive=true blocks scheduler from assigning multiple agents ──


class TestExclusiveConstraint:
    def test_exclusive_blocks_second_agent(self):
        """With exclusive=true and one agent already active, no more agents are assigned."""
        state = make_state(
            projects=[make_project(max_agents=4)],
            tasks=[make_task(id="t-1"), make_task(id="t-2")],
            agents=[make_agent(id="a-1"), make_agent(id="a-2")],
            project_active_agent_counts={"p-1": 1},
            project_constraints={"p-1": ProjectConstraint(project_id="p-1", exclusive=True)},
        )
        actions = Scheduler.schedule(state)
        # exclusive caps at 1, already at 1 → no new assignments
        assert len(actions) == 0

    def test_exclusive_allows_first_agent(self):
        """With exclusive=true and no agents active, one agent is assigned."""
        state = make_state(
            projects=[make_project(max_agents=4)],
            tasks=[make_task()],
            agents=[make_agent()],
            project_constraints={"p-1": ProjectConstraint(project_id="p-1", exclusive=True)},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1

    def test_exclusive_caps_within_round(self):
        """With two idle agents and exclusive=true, only one gets assigned."""
        state = make_state(
            projects=[make_project(max_agents=4)],
            tasks=[make_task(id="t-1"), make_task(id="t-2")],
            agents=[make_agent(id="a-1"), make_agent(id="a-2")],
            project_constraints={"p-1": ProjectConstraint(project_id="p-1", exclusive=True)},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1


# ── (b) release_project_constraint lifts the block ──


class TestReleaseConstraint:
    def test_no_constraint_allows_normal_scheduling(self):
        """Without constraints, scheduling proceeds normally."""
        state = make_state(
            projects=[make_project(max_agents=3)],
            tasks=[make_task(id="t-1"), make_task(id="t-2"), make_task(id="t-3")],
            agents=[
                make_agent(id="a-1"),
                make_agent(id="a-2"),
                make_agent(id="a-3"),
            ],
            project_constraints={},  # no constraints → normal behavior
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 3

    def test_releasing_exclusive_restores_concurrency(self):
        """After removing exclusive constraint, max_concurrent_agents applies again."""
        # First: with exclusive
        state_constrained = make_state(
            projects=[make_project(max_agents=3)],
            tasks=[make_task(id="t-1"), make_task(id="t-2")],
            agents=[make_agent(id="a-1"), make_agent(id="a-2")],
            project_constraints={"p-1": ProjectConstraint(project_id="p-1", exclusive=True)},
        )
        actions_constrained = Scheduler.schedule(state_constrained)
        assert len(actions_constrained) == 1

        # Second: constraint released (empty dict)
        state_released = make_state(
            projects=[make_project(max_agents=3)],
            tasks=[make_task(id="t-1"), make_task(id="t-2")],
            agents=[make_agent(id="a-1"), make_agent(id="a-2")],
            project_constraints={},
        )
        actions_released = Scheduler.schedule(state_released)
        assert len(actions_released) == 2


# ── (c) max_agents_by_type allows up to N agents of a type ──


class TestMaxAgentsByType:
    def test_type_limit_blocks_excess_agents(self):
        """max_agents_by_type={"claude": 1} blocks a second claude agent."""
        state = make_state(
            projects=[make_project(max_agents=4)],
            tasks=[
                make_task(id="t-existing", status=TaskStatus.IN_PROGRESS),
                make_task(id="t-1"),
            ],
            agents=[
                # a-busy is already working (BUSY) on this project
                make_agent(
                    id="a-busy",
                    agent_type="claude",
                    state=AgentState.BUSY,
                    current_task_id="t-existing",
                ),
                # a-idle wants work
                make_agent(id="a-idle", agent_type="claude"),
            ],
            project_active_agent_counts={"p-1": 1},
            project_constraints={
                "p-1": ProjectConstraint(project_id="p-1", max_agents_by_type={"claude": 1})
            },
        )
        actions = Scheduler.schedule(state)
        # The idle claude agent should NOT be assigned because 1 claude is
        # already active and the limit is 1.
        assert len(actions) == 0

    def test_type_limit_allows_different_type(self):
        """max_agents_by_type={"claude": 1} does not restrict codex agents."""
        state = make_state(
            projects=[make_project(max_agents=4)],
            tasks=[
                make_task(id="t-existing", status=TaskStatus.IN_PROGRESS),
                make_task(id="t-1"),
            ],
            agents=[
                make_agent(
                    id="a-busy",
                    agent_type="claude",
                    state=AgentState.BUSY,
                    current_task_id="t-existing",
                ),
                make_agent(id="a-codex", agent_type="codex"),
            ],
            project_active_agent_counts={"p-1": 1},
            project_constraints={
                "p-1": ProjectConstraint(project_id="p-1", max_agents_by_type={"claude": 1})
            },
        )
        actions = Scheduler.schedule(state)
        # codex is unrestricted → should be assigned
        assert len(actions) == 1
        assert actions[0].agent_id == "a-codex"

    def test_type_limit_allows_up_to_limit(self):
        """max_agents_by_type={"claude": 2} allows two claude agents."""
        state = make_state(
            projects=[make_project(max_agents=4)],
            tasks=[make_task(id="t-1"), make_task(id="t-2")],
            agents=[
                make_agent(id="a-1", agent_type="claude"),
                make_agent(id="a-2", agent_type="claude"),
            ],
            project_constraints={
                "p-1": ProjectConstraint(project_id="p-1", max_agents_by_type={"claude": 2})
            },
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 2


# ── (d) pause_scheduling=true stops all task assignment ──


class TestPauseScheduling:
    def test_pause_blocks_all_assignments(self):
        """pause_scheduling=true prevents any tasks from being assigned."""
        state = make_state(
            projects=[make_project()],
            tasks=[make_task()],
            agents=[make_agent()],
            project_constraints={"p-1": ProjectConstraint(project_id="p-1", pause_scheduling=True)},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0


# ── (e) constraint on project A does not affect project B ──


class TestConstraintIsolation:
    def test_constraint_does_not_affect_other_projects(self):
        """Pausing project A should not block scheduling for project B."""
        state = make_state(
            projects=[
                make_project(id="p-1", name="alpha"),
                make_project(id="p-2", name="beta"),
            ],
            tasks=[
                make_task(id="t-1", project_id="p-1"),
                make_task(id="t-2", project_id="p-2"),
            ],
            agents=[make_agent(id="a-1"), make_agent(id="a-2")],
            project_constraints={"p-1": ProjectConstraint(project_id="p-1", pause_scheduling=True)},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].project_id == "p-2"

    def test_exclusive_on_one_project_does_not_affect_other(self):
        """Exclusive on project A does not limit project B's concurrency."""
        state = make_state(
            projects=[
                make_project(id="p-1", name="alpha", max_agents=4),
                make_project(id="p-2", name="beta", max_agents=4),
            ],
            tasks=[
                make_task(id="t-1", project_id="p-1"),
                make_task(id="t-2a", project_id="p-2"),
                make_task(id="t-2b", project_id="p-2"),
            ],
            agents=[
                make_agent(id="a-1"),
                make_agent(id="a-2"),
                make_agent(id="a-3"),
            ],
            project_constraints={"p-1": ProjectConstraint(project_id="p-1", exclusive=True)},
        )
        actions = Scheduler.schedule(state)
        # p-1: exclusive → 1 agent; p-2: unconstrained → 2 agents
        assert len(actions) == 3
        p1_actions = [a for a in actions if a.project_id == "p-1"]
        p2_actions = [a for a in actions if a.project_id == "p-2"]
        assert len(p1_actions) == 1
        assert len(p2_actions) == 2


# ── (g) multiple constraints stack correctly ──


class TestConstraintStacking:
    def test_exclusive_plus_max_agents_by_type(self):
        """exclusive + max_agents_by_type both apply simultaneously."""
        state = make_state(
            projects=[make_project(max_agents=4)],
            tasks=[make_task(id="t-1"), make_task(id="t-2")],
            agents=[
                make_agent(id="a-1", agent_type="claude"),
                make_agent(id="a-2", agent_type="codex"),
            ],
            project_constraints={
                "p-1": ProjectConstraint(
                    project_id="p-1",
                    exclusive=True,
                    max_agents_by_type={"claude": 2, "codex": 2},
                )
            },
        )
        actions = Scheduler.schedule(state)
        # exclusive caps at 1 total, regardless of per-type limits
        assert len(actions) == 1

    def test_pause_overrides_everything(self):
        """pause_scheduling=true blocks all assignments, even with exclusive=false."""
        state = make_state(
            projects=[make_project()],
            tasks=[make_task()],
            agents=[make_agent()],
            project_constraints={
                "p-1": ProjectConstraint(
                    project_id="p-1",
                    exclusive=False,
                    max_agents_by_type={"claude": 5},
                    pause_scheduling=True,
                )
            },
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0


# ── (h) constraint persists across scheduler tick cycles ──


class TestConstraintPersistence:
    def test_constraint_persists_across_ticks(self):
        """The same constraint dict produces consistent results across calls."""
        constraints = {"p-1": ProjectConstraint(project_id="p-1", exclusive=True)}

        for _ in range(5):
            state = make_state(
                projects=[make_project(max_agents=4)],
                tasks=[make_task(id="t-1"), make_task(id="t-2")],
                agents=[make_agent(id="a-1"), make_agent(id="a-2")],
                project_constraints=constraints,
            )
            actions = Scheduler.schedule(state)
            assert len(actions) == 1  # always capped at 1


# ── Command handler integration (requires async DB) ──


@pytest.fixture
async def db():
    """Create an in-memory database for command handler tests."""
    from src.database.adapters.sqlite import SQLiteDatabaseAdapter

    adapter = SQLiteDatabaseAdapter(":memory:")
    await adapter.initialize()
    yield adapter
    await adapter.close()


@pytest.fixture
async def handler(db):
    """Create a CommandHandler wired to the in-memory database."""
    from unittest.mock import MagicMock

    from src.command_handler import CommandHandler

    config = MagicMock()
    orchestrator = MagicMock()
    orchestrator.db = db
    ch = CommandHandler(orchestrator=orchestrator, config=config)
    return ch


class TestSetProjectConstraintCommand:
    """(f) and integration tests for set_project_constraint / release_project_constraint."""

    async def test_set_constraint_on_nonexistent_project(self, handler):
        """(f) Setting a constraint on a non-existent project returns a clear error."""
        result = await handler.execute(
            "set_project_constraint",
            {"project_id": "no-such-project", "exclusive": True},
        )
        assert "error" in result
        assert "not found" in result["error"]

    async def test_set_and_release_constraint(self, handler, db):
        """Round-trip: set a constraint, verify it exists, then release it."""
        from src.models import Project

        await db.create_project(Project(id="proj-1", name="Test Project"))

        # Set exclusive constraint
        result = await handler.execute(
            "set_project_constraint",
            {"project_id": "proj-1", "exclusive": True},
        )
        assert result.get("constraint_set") is True
        assert "exclusive" in result["active_fields"]

        # Verify it's in the DB
        c = await db.get_project_constraint("proj-1")
        assert c is not None
        assert c.exclusive is True

        # Release it
        result = await handler.execute(
            "release_project_constraint",
            {"project_id": "proj-1"},
        )
        assert result.get("constraint_released") is True

        # Verify it's gone
        c = await db.get_project_constraint("proj-1")
        assert c is None

    async def test_release_nonexistent_constraint(self, handler, db):
        """Releasing a constraint that doesn't exist returns an error."""
        from src.models import Project

        await db.create_project(Project(id="proj-2", name="Test Project 2"))
        result = await handler.execute(
            "release_project_constraint",
            {"project_id": "proj-2"},
        )
        assert "error" in result
        assert "No active constraint" in result["error"]

    async def test_constraint_stacking_via_merge(self, handler, db):
        """Setting exclusive then adding pause_scheduling merges both."""
        from src.models import Project

        await db.create_project(Project(id="proj-3", name="Test Project 3"))

        # Set exclusive
        await handler.execute(
            "set_project_constraint",
            {"project_id": "proj-3", "exclusive": True},
        )

        # Add pause_scheduling (should merge with exclusive)
        result = await handler.execute(
            "set_project_constraint",
            {"project_id": "proj-3", "pause_scheduling": True},
        )
        assert result.get("constraint_set") is True
        assert "exclusive" in result["active_fields"]
        assert "pause_scheduling" in result["active_fields"]

        # Verify both are set in DB
        c = await db.get_project_constraint("proj-3")
        assert c.exclusive is True
        assert c.pause_scheduling is True

    async def test_partial_release(self, handler, db):
        """Releasing only 'exclusive' keeps pause_scheduling active."""
        from src.models import Project

        await db.create_project(Project(id="proj-4", name="Test Project 4"))

        # Set both exclusive + pause_scheduling
        await handler.execute(
            "set_project_constraint",
            {"project_id": "proj-4", "exclusive": True, "pause_scheduling": True},
        )

        # Release only exclusive
        result = await handler.execute(
            "release_project_constraint",
            {"project_id": "proj-4", "fields": ["exclusive"]},
        )
        assert result.get("constraint_released") is False  # partial release
        assert "exclusive" in result["fields_released"]
        assert "pause_scheduling" in result["remaining_fields"]

        # Verify in DB
        c = await db.get_project_constraint("proj-4")
        assert c.exclusive is False
        assert c.pause_scheduling is True

    async def test_set_constraint_requires_at_least_one_field(self, handler, db):
        """Setting a constraint with no fields returns an error."""
        from src.models import Project

        await db.create_project(Project(id="proj-5", name="Test Project 5"))
        result = await handler.execute(
            "set_project_constraint",
            {"project_id": "proj-5"},
        )
        assert "error" in result
        assert "At least one constraint" in result["error"]

    async def test_release_on_nonexistent_project(self, handler):
        """Releasing a constraint on a non-existent project returns error."""
        result = await handler.execute(
            "release_project_constraint",
            {"project_id": "no-such-project"},
        )
        assert "error" in result
        assert "not found" in result["error"]
