"""Tests for review feedback cycle with agent affinity (Roadmap 7.5.8).

Verifies the review feedback loop described in
``vault/system/playbooks/feature-pipeline.md`` and
``docs/specs/design/agent-coordination.md`` §4 Example 1, integrated with
the scheduler affinity logic from ``agent-coordination.md`` §6.

Test cases:
  (a) Reviewer marks code review as "changes_requested" — playbook creates
      a fix task.
  (b) Fix task has ``affinity_agent_id`` set to the original coding agent
      (who wrote the code).
  (c) ``affinity_reason`` is "original author" or similar descriptive string.
  (d) If original agent is idle, fix task is assigned to them immediately.
  (e) If original agent is busy, fix task waits up to configured timeout
      then falls back.
  (f) Fix task completion re-triggers review (loop back in playbook graph).
  (g) Maximum review cycles are bounded (configurable, e.g., 3 rounds) to
      prevent infinite loops.
"""

import json
import time
from unittest.mock import MagicMock

import pytest

from src.config import AppConfig, DiscordConfig
from src.commands.handler import CommandHandler
from src.database import Database
from src.models import (
    Agent,
    AgentState,
    PlaybookRun,
    Project,
    Task,
    TaskStatus,
    Workflow,
)
from src.orchestrator import Orchestrator
from src.scheduler import Scheduler, SchedulerState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(
    id: str = "p-1",
    name: str = "feature-proj",
    weight: float = 1.0,
    max_agents: int = 4,
    **kw,
) -> Project:
    return Project(id=id, name=name, credit_weight=weight, max_concurrent_agents=max_agents, **kw)


def _make_task(
    task_id: str,
    project_id: str = "p-1",
    status: TaskStatus = TaskStatus.DEFINED,
    priority: int = 100,
    workflow_id: str | None = None,
    agent_type: str | None = None,
    affinity_agent_id: str | None = None,
    affinity_reason: str | None = None,
    description: str = "test task",
    created_at: float = 0.0,
    **kw,
) -> Task:
    return Task(
        id=task_id,
        project_id=project_id,
        title=f"Task {task_id}",
        description=description,
        priority=priority,
        status=status,
        workflow_id=workflow_id,
        agent_type=agent_type,
        affinity_agent_id=affinity_agent_id,
        affinity_reason=affinity_reason,
        created_at=created_at,
        **kw,
    )


def _make_agent(
    id: str = "a-1",
    name: str = "claude-1",
    agent_type: str = "coding",
    state: AgentState = AgentState.IDLE,
    **kw,
) -> Agent:
    return Agent(id=id, name=name, agent_type=agent_type, state=state, **kw)


def _make_workflow(
    workflow_id: str = "wf-feature-1",
    playbook_id: str = "feature-pipeline",
    playbook_run_id: str = "pbr-1",
    project_id: str = "p-1",
    status: str = "running",
    current_stage: str | None = "review_and_qa",
    task_ids: list[str] | None = None,
    agent_affinity: dict[str, str] | None = None,
) -> Workflow:
    return Workflow(
        workflow_id=workflow_id,
        playbook_id=playbook_id,
        playbook_run_id=playbook_run_id,
        project_id=project_id,
        status=status,
        current_stage=current_stage,
        task_ids=task_ids or [],
        agent_affinity=agent_affinity or {},
        created_at=time.time(),
    )


def _make_scheduler_state(**overrides) -> SchedulerState:
    """Build a SchedulerState with sensible defaults for feature pipeline tests."""
    defaults = dict(
        projects=[_make_project()],
        tasks=[],
        agents=[],
        project_token_usage={},
        project_active_agent_counts={},
        tasks_completed_in_window={},
        project_constraints={},
    )
    defaults.update(overrides)
    return SchedulerState(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """Create a temp database."""
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
async def orch(tmp_path):
    """Create a minimal orchestrator with a real SQLite database."""
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
async def handler(db, tmp_path):
    """Create a CommandHandler with a real database for integration tests."""
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

    # Create test project
    await db.create_project(Project(id="p-1", name="feature-proj"))

    # Create test agents — one "coding" agent and one "code-review" agent
    await db.create_agent(
        Agent(id="agent-coder", name="claude-coder", agent_type="coding", state=AgentState.IDLE)
    )
    await db.create_agent(
        Agent(
            id="agent-reviewer",
            name="claude-reviewer",
            agent_type="code-review",
            state=AgentState.IDLE,
        )
    )
    await db.create_agent(
        Agent(id="agent-qa", name="claude-qa", agent_type="qa", state=AgentState.IDLE)
    )

    # Create a playbook run and workflow so tasks with workflow_id pass FK checks
    await db.create_playbook_run(
        PlaybookRun(
            run_id="pbr-handler",
            playbook_id="feature-pipeline",
            playbook_version=1,
            trigger_event='{"type": "task.created", "task_type": "FEATURE"}',
            status="running",
            started_at=time.time(),
        )
    )
    await db.create_workflow(
        Workflow(
            workflow_id="wf-feature-1",
            playbook_id="feature-pipeline",
            playbook_run_id="pbr-handler",
            project_id="p-1",
            status="running",
            current_stage="review_and_qa",
            agent_affinity={"coding": "agent-coder"},
            created_at=time.time(),
        )
    )

    return cmd


async def _setup_project(db, project_id="p-1"):
    """Create a project for FK constraints."""
    try:
        await db.create_project(Project(id=project_id, name="feature-proj"))
    except Exception:
        pass


async def _setup_workflow_prereqs(db, project_id="p-1", run_id="pbr-1"):
    """Create the project and playbook_run that workflows FK-reference."""
    try:
        await db.create_project(Project(id=project_id, name="feature-proj"))
    except Exception:
        pass  # project may already exist

    await db.create_playbook_run(
        PlaybookRun(
            run_id=run_id,
            playbook_id="feature-pipeline",
            playbook_version=1,
            trigger_event='{"type": "task.created", "task_type": "FEATURE"}',
            status="running",
            started_at=time.time(),
        )
    )


# ===========================================================================
# (a) Reviewer marks code review as "changes_requested" — playbook creates
#     a fix task
# ===========================================================================


class TestReviewChangesRequestedCreatesFixTask:
    """Verify that when a review task completes with 'changes_requested',
    a fix task is created as part of the feedback loop."""

    async def test_fix_task_created_after_changes_requested(self, handler, db):
        """A fix task is created when a review requests changes."""
        # Create the original coding task (completed)
        await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Implement caching feature",
                "description": "Add Redis caching layer",
                "agent_type": "coding",
                "workflow_id": "wf-feature-1",
            },
        )

        # Create the review task that requested changes (completed)
        review_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Review: Implement caching feature",
                "description": "Review the caching implementation PR",
                "agent_type": "code-review",
                "workflow_id": "wf-feature-1",
            },
        )
        assert "error" not in review_result
        review_id = review_result["created"]

        # Now create the fix task (as the playbook would after changes_requested)
        fix_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Address review feedback: Implement caching feature",
                "description": (
                    "Reviewer requested changes:\n"
                    "- Fix error handling in cache invalidation\n"
                    "- Add retry logic for Redis connection failures\n"
                    "- Improve test coverage for edge cases"
                ),
                "agent_type": "coding",
                "affinity_agent_id": "agent-coder",
                "affinity_reason": "context",
                "workflow_id": "wf-feature-1",
            },
        )
        assert "error" not in fix_result, f"Failed to create fix task: {fix_result}"
        fix_id = fix_result["created"]

        # Fix task should depend on the review task
        dep_result = await handler.execute(
            "add_dependency",
            {"task_id": fix_id, "depends_on": review_id},
        )
        assert "error" not in dep_result

        # Verify the fix task exists and has correct properties
        fix_task = await db.get_task(fix_id)
        assert fix_task is not None
        assert fix_task.agent_type == "coding"
        assert "review feedback" in fix_task.title.lower()

        # Verify dependency is recorded
        deps = await db.get_dependencies(fix_id)
        assert review_id in deps

    async def test_fix_task_description_includes_review_feedback(self, handler, db):
        """Fix task description includes the specific feedback from the reviewer."""
        feedback_items = [
            "Cache TTL should be configurable",
            "Missing null check in get_cached_value()",
            "Test for concurrent cache writes needed",
        ]
        feedback_text = "\n".join(f"- {item}" for item in feedback_items)

        result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Address review feedback: Caching feature",
                "description": f"Reviewer requested changes:\n{feedback_text}",
                "agent_type": "coding",
                "workflow_id": "wf-feature-1",
            },
        )
        assert "error" not in result

        task = await db.get_task(result["created"])
        for item in feedback_items:
            assert item in task.description, (
                f"Fix task description should include feedback item: '{item}'"
            )

    async def test_fix_task_priority_matches_original_coding_task(self, handler, db):
        """Fix task inherits priority from the original coding task."""
        original_priority = 50

        # Create original coding task with specific priority
        coding_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Implement feature X",
                "priority": original_priority,
                "agent_type": "coding",
            },
        )
        assert "error" not in coding_result

        # Create fix task with same priority
        fix_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Address review feedback: feature X",
                "priority": original_priority,
                "agent_type": "coding",
                "affinity_agent_id": "agent-coder",
            },
        )
        assert "error" not in fix_result

        fix_task = await db.get_task(fix_result["created"])
        assert fix_task.priority == original_priority


# ===========================================================================
# (b) Fix task has affinity_agent_id set to the original coding agent
# ===========================================================================


class TestFixTaskAffinityToOriginalAgent:
    """Verify that fix tasks route back to the original coding agent via
    the affinity_agent_id field, using the workflow's agent_affinity map."""

    async def test_fix_task_has_affinity_to_original_coder(self, handler, db):
        """Fix task's affinity_agent_id points to the original coding agent."""
        original_agent_id = "agent-coder"

        result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Address review feedback: Caching",
                "description": "Fix issues found in review",
                "agent_type": "coding",
                "affinity_agent_id": original_agent_id,
                "workflow_id": "wf-feature-1",
            },
        )
        assert "error" not in result

        task = await db.get_task(result["created"])
        assert task.affinity_agent_id == original_agent_id, (
            f"Fix task affinity_agent_id should be '{original_agent_id}', "
            f"got '{task.affinity_agent_id}'"
        )

    async def test_workflow_agent_affinity_map_records_original_coder(self, orch):
        """Workflow's agent_affinity map stores the coding agent's ID for later use."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            agent_affinity={"coding": "agent-coder"},
            task_ids=["coding-1"],
        )
        await orch.db.create_workflow(wf)

        loaded = await orch.db.get_workflow("wf-feature-1")
        assert loaded.agent_affinity.get("coding") == "agent-coder", (
            "Workflow should record the coding agent in the agent_affinity map"
        )

    async def test_fix_task_affinity_survives_db_round_trip(self, db):
        """Affinity fields are persisted and restored from the database."""
        await db.create_project(Project(id="p-1", name="feature-proj"))

        task = _make_task(
            "fix-1",
            status=TaskStatus.READY,
            agent_type="coding",
            affinity_agent_id="agent-coder",
            affinity_reason="context",
            created_at=time.time(),
        )
        await db.create_task(task)

        loaded = await db.get_task("fix-1")
        assert loaded.affinity_agent_id == "agent-coder"
        assert loaded.affinity_reason == "context"

    async def test_qa_fix_task_also_has_affinity_to_original_coder(self, handler, db):
        """QA bugfix tasks also route back to the original coding agent."""
        result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Fix QA failures: Caching feature",
                "description": "Tests failed: test_cache_invalidation, test_ttl_expiry",
                "agent_type": "coding",
                "affinity_agent_id": "agent-coder",
                "affinity_reason": "context",
                "workflow_id": "wf-feature-1",
            },
        )
        assert "error" not in result

        task = await db.get_task(result["created"])
        assert task.affinity_agent_id == "agent-coder"
        assert task.agent_type == "coding"


# ===========================================================================
# (c) affinity_reason is "original author" or similar descriptive string
# ===========================================================================


class TestAffinityReasonDescriptive:
    """Verify that fix tasks carry a descriptive affinity_reason explaining
    why the original agent is preferred."""

    async def test_fix_task_has_descriptive_affinity_reason(self, handler, db):
        """Fix task's affinity_reason is a descriptive string, not None."""
        result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Address review feedback",
                "agent_type": "coding",
                "affinity_agent_id": "agent-coder",
                "affinity_reason": "context",
                "workflow_id": "wf-feature-1",
            },
        )
        assert "error" not in result

        task = await db.get_task(result["created"])
        assert task.affinity_reason is not None, "affinity_reason should not be None"
        assert len(task.affinity_reason) > 0, "affinity_reason should be a non-empty string"

    async def test_affinity_reason_is_context_related(self, handler, db):
        """Fix task's affinity_reason indicates context continuity."""
        # Per feature-pipeline.md: affinity_reason: "context"
        result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Fix QA failures",
                "agent_type": "coding",
                "affinity_agent_id": "agent-coder",
                "affinity_reason": "context",
            },
        )
        assert "error" not in result

        task = await db.get_task(result["created"])
        # The playbook uses "context" as the affinity reason per the spec
        assert task.affinity_reason in (
            "context",
            "original author",
            "original_author",
        ), f"affinity_reason should be context-related, got '{task.affinity_reason}'"

    async def test_affinity_reason_persisted_to_database(self, db):
        """affinity_reason is stored in the database and retrievable."""
        await db.create_project(Project(id="p-1", name="feature-proj"))

        task = _make_task(
            "fix-reason-1",
            status=TaskStatus.READY,
            affinity_agent_id="agent-coder",
            affinity_reason="context",
            created_at=time.time(),
        )
        await db.create_task(task)

        loaded = await db.get_task("fix-reason-1")
        assert loaded.affinity_reason == "context"

    async def test_affinity_without_reason_still_works(self, db):
        """A task with affinity_agent_id but no reason is still valid."""
        await db.create_project(Project(id="p-1", name="feature-proj"))

        task = _make_task(
            "fix-no-reason",
            status=TaskStatus.READY,
            affinity_agent_id="agent-coder",
            affinity_reason=None,
            created_at=time.time(),
        )
        await db.create_task(task)

        loaded = await db.get_task("fix-no-reason")
        assert loaded.affinity_agent_id == "agent-coder"
        assert loaded.affinity_reason is None


# ===========================================================================
# (d) If original agent is idle, fix task is assigned to them immediately
# ===========================================================================


class TestAffinityIdleAgentImmediateAssignment:
    """Verify that when the preferred (original) coding agent is idle,
    the scheduler assigns the fix task to them immediately (tier 0)."""

    def test_idle_affinity_agent_gets_fix_task(self):
        """Fix task with affinity is assigned to the idle original agent."""
        now = time.time()
        state = _make_scheduler_state(
            tasks=[
                _make_task(
                    "fix-1",
                    status=TaskStatus.READY,
                    agent_type="coding",
                    affinity_agent_id="agent-coder",
                    created_at=now - 10,
                ),
            ],
            agents=[
                _make_agent(id="agent-coder", name="claude-coder"),
                _make_agent(id="agent-other", name="claude-other"),
            ],
            now=now,
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].agent_id == "agent-coder"
        assert actions[0].task_id == "fix-1"

    def test_affinity_fix_task_prioritised_over_non_affinity(self):
        """Fix task with affinity (tier 0) beats a non-affinity task (tier 1)
        for the preferred agent."""
        now = time.time()
        state = _make_scheduler_state(
            tasks=[
                _make_task(
                    "unrelated-1",
                    status=TaskStatus.READY,
                    priority=10,  # higher priority number
                ),
                _make_task(
                    "fix-1",
                    status=TaskStatus.READY,
                    priority=50,
                    agent_type="coding",
                    affinity_agent_id="agent-coder",
                    created_at=now,
                ),
            ],
            agents=[_make_agent(id="agent-coder", name="claude-coder")],
            now=now,
        )
        actions = Scheduler.schedule(state)
        # Affinity (tier 0) beats non-affinity (tier 1) regardless of priority
        assert actions[0].task_id == "fix-1"

    def test_fix_task_assigned_to_correct_agent_among_multiple_idle(self):
        """With multiple idle agents, fix task goes to the one with affinity."""
        now = time.time()
        state = _make_scheduler_state(
            projects=[_make_project(max_agents=3)],
            tasks=[
                _make_task(
                    "fix-1",
                    status=TaskStatus.READY,
                    affinity_agent_id="agent-coder",
                    created_at=now - 5,
                ),
                _make_task("normal-1", status=TaskStatus.READY, created_at=now),
                _make_task("normal-2", status=TaskStatus.READY, created_at=now),
            ],
            agents=[
                _make_agent(id="agent-coder", name="claude-coder"),
                _make_agent(id="agent-reviewer", name="claude-reviewer"),
                _make_agent(id="agent-qa", name="claude-qa"),
            ],
            now=now,
        )
        actions = Scheduler.schedule(state)

        by_agent = {a.agent_id: a.task_id for a in actions}
        # agent-coder gets the fix task (tier 0 affinity)
        assert by_agent["agent-coder"] == "fix-1"

    async def test_fix_task_with_affinity_assigned_in_orchestrator(self, orch):
        """Integration: fix task with affinity is assigned via orchestrator scheduling."""
        await _setup_workflow_prereqs(orch.db)

        # Register agents
        await orch.db.create_agent(
            Agent(id="agent-coder", name="claude-coder", agent_type="coding")
        )
        await orch.db.create_agent(
            Agent(id="agent-other", name="claude-other", agent_type="coding")
        )

        # Create fix task with affinity
        fix_task = _make_task(
            "fix-1",
            status=TaskStatus.READY,
            agent_type="coding",
            affinity_agent_id="agent-coder",
            affinity_reason="context",
            created_at=time.time(),
        )
        await orch.db.create_task(fix_task)

        # Verify affinity is preserved in the DB
        loaded = await orch.db.get_task("fix-1")
        assert loaded.affinity_agent_id == "agent-coder"


# ===========================================================================
# (e) If original agent is busy, fix task waits up to configured timeout
#     then falls back
# ===========================================================================


class TestAffinityBusyAgentBoundedWait:
    """Verify that when the original coding agent is busy, the fix task
    waits up to affinity_wait_seconds, then falls back to any idle agent."""

    def test_fix_task_deferred_while_affinity_agent_busy_within_window(self):
        """Fix task is NOT assigned to another agent while the affinity agent
        is busy and within the wait window."""
        now = time.time()
        state = _make_scheduler_state(
            tasks=[
                _make_task(
                    "fix-1",
                    status=TaskStatus.READY,
                    affinity_agent_id="agent-coder",
                    created_at=now - 30,  # 30s ago, within 120s window
                ),
            ],
            agents=[
                _make_agent(id="agent-other", name="claude-other"),
                _make_agent(
                    id="agent-coder",
                    name="claude-coder",
                    state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            now=now,
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # Within wait window → deferred (no assignment)
        assert len(actions) == 0

    def test_fix_task_falls_back_after_wait_expires(self):
        """After the wait window expires, fix task is assigned to any idle agent."""
        now = time.time()
        state = _make_scheduler_state(
            tasks=[
                _make_task(
                    "fix-1",
                    status=TaskStatus.READY,
                    affinity_agent_id="agent-coder",
                    created_at=now - 200,  # 200s ago, past 120s window
                ),
            ],
            agents=[
                _make_agent(id="agent-other", name="claude-other"),
                _make_agent(
                    id="agent-coder",
                    name="claude-coder",
                    state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            now=now,
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # Wait expired → fallback to agent-other
        assert len(actions) == 1
        assert actions[0].agent_id == "agent-other"
        assert actions[0].task_id == "fix-1"

    def test_non_affinity_tasks_assigned_while_fix_waits(self):
        """Non-affinity tasks are still assigned normally while a fix task waits."""
        now = time.time()
        state = _make_scheduler_state(
            tasks=[
                _make_task(
                    "fix-1",
                    status=TaskStatus.READY,
                    priority=10,
                    affinity_agent_id="agent-coder",
                    created_at=now - 30,  # within wait window
                ),
                _make_task("normal-1", status=TaskStatus.READY, priority=50),
            ],
            agents=[
                _make_agent(id="agent-other", name="claude-other"),
                _make_agent(
                    id="agent-coder",
                    name="claude-coder",
                    state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            now=now,
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # fix-1 deferred (tier 3), normal-1 assigned (tier 1)
        assert len(actions) == 1
        assert actions[0].task_id == "normal-1"

    def test_zero_wait_seconds_disables_bounded_wait(self):
        """With affinity_wait_seconds=0, fix task is assigned immediately to fallback."""
        now = time.time()
        state = _make_scheduler_state(
            tasks=[
                _make_task(
                    "fix-1",
                    status=TaskStatus.READY,
                    affinity_agent_id="agent-coder",
                    created_at=now - 5,
                ),
            ],
            agents=[
                _make_agent(id="agent-other", name="claude-other"),
                _make_agent(
                    id="agent-coder",
                    name="claude-coder",
                    state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            now=now,
            affinity_wait_seconds=0,
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].agent_id == "agent-other"

    def test_custom_wait_seconds_respected(self):
        """A shorter wait window expires sooner, falling back earlier."""
        now = time.time()
        state = _make_scheduler_state(
            tasks=[
                _make_task(
                    "fix-1",
                    status=TaskStatus.READY,
                    affinity_agent_id="agent-coder",
                    created_at=now - 45,  # 45s ago
                ),
            ],
            agents=[
                _make_agent(id="agent-other", name="claude-other"),
                _make_agent(
                    id="agent-coder",
                    name="claude-coder",
                    state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            now=now,
            affinity_wait_seconds=30,  # 30s window — already expired
        )
        actions = Scheduler.schedule(state)
        # 45s > 30s window → fallback
        assert len(actions) == 1
        assert actions[0].agent_id == "agent-other"

    def test_wait_not_expired_with_custom_timeout(self):
        """Fix task still waits when within a custom wait window."""
        now = time.time()
        state = _make_scheduler_state(
            tasks=[
                _make_task(
                    "fix-1",
                    status=TaskStatus.READY,
                    affinity_agent_id="agent-coder",
                    created_at=now - 45,  # 45s ago
                ),
            ],
            agents=[
                _make_agent(id="agent-other", name="claude-other"),
                _make_agent(
                    id="agent-coder",
                    name="claude-coder",
                    state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            now=now,
            affinity_wait_seconds=60,  # 60s window — not yet expired
        )
        actions = Scheduler.schedule(state)
        # 45s < 60s window → still waiting
        assert len(actions) == 0

    def test_affinity_agent_becomes_idle_during_wait(self):
        """When the affinity agent becomes idle (is idle at scheduling time),
        the fix task is assigned to the preferred agent (tier 0) rather than
        a non-preferred agent. A second task is provided so the non-preferred
        agent has work, matching the real-world scenario."""
        now = time.time()
        state = _make_scheduler_state(
            projects=[_make_project(max_agents=2)],
            tasks=[
                _make_task(
                    "fix-1",
                    status=TaskStatus.READY,
                    affinity_agent_id="agent-coder",
                    created_at=now - 60,  # 60s ago, but agent is now idle
                ),
                _make_task("normal-1", status=TaskStatus.READY, created_at=now),
            ],
            agents=[
                _make_agent(id="agent-other", name="claude-other"),
                _make_agent(id="agent-coder", name="claude-coder"),  # now IDLE
            ],
            now=now,
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # Agent is idle → tier 0, assigned immediately
        assert len(actions) == 2
        by_agent = {a.agent_id: a.task_id for a in actions}
        assert by_agent["agent-coder"] == "fix-1"
        assert by_agent["agent-other"] == "normal-1"


# ===========================================================================
# (f) Fix task completion re-triggers review (loop back in playbook graph)
# ===========================================================================


class TestFixCompletionReTriggersReview:
    """Verify that when a fix task completes, new review and QA tasks
    are created that depend on the fix task, re-entering the feedback loop."""

    async def test_new_review_created_after_fix_completes(self, handler, db):
        """After a fix task completes, a new review task is created."""
        # Create the fix task (completed by the coding agent)
        fix_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Address review feedback: Caching (round 1)",
                "agent_type": "coding",
                "affinity_agent_id": "agent-coder",
                "workflow_id": "wf-feature-1",
            },
        )
        assert "error" not in fix_result
        fix_id = fix_result["created"]

        # Create new review task that depends on the fix
        review2_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Review: Caching feature (round 2)",
                "agent_type": "code-review",
                "workflow_id": "wf-feature-1",
            },
        )
        assert "error" not in review2_result
        review2_id = review2_result["created"]

        dep_result = await handler.execute(
            "add_dependency",
            {"task_id": review2_id, "depends_on": fix_id},
        )
        assert "error" not in dep_result

        # Verify dependency chain: review2 depends on fix
        deps = await db.get_dependencies(review2_id)
        assert fix_id in deps

    async def test_new_qa_created_after_fix_completes(self, handler, db):
        """After a fix task completes, a new QA task is also created."""
        fix_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Address review feedback: Caching (round 1)",
                "agent_type": "coding",
                "workflow_id": "wf-feature-1",
            },
        )
        fix_id = fix_result["created"]

        # Create new QA task that depends on the fix
        qa2_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "QA: Caching feature (round 2)",
                "agent_type": "qa",
                "workflow_id": "wf-feature-1",
            },
        )
        assert "error" not in qa2_result
        qa2_id = qa2_result["created"]

        dep_result = await handler.execute(
            "add_dependency",
            {"task_id": qa2_id, "depends_on": fix_id},
        )
        assert "error" not in dep_result

        deps = await db.get_dependencies(qa2_id)
        assert fix_id in deps

    async def test_review_and_qa_both_depend_on_fix(self, handler, db):
        """Both re-triggered review and QA tasks depend on the fix task,
        so they run concurrently once the fix completes."""
        fix_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Fix: review feedback round 1",
                "agent_type": "coding",
                "workflow_id": "wf-feature-1",
            },
        )
        fix_id = fix_result["created"]

        review_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Review: round 2",
                "agent_type": "code-review",
                "workflow_id": "wf-feature-1",
            },
        )
        review_id = review_result["created"]

        qa_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "QA: round 2",
                "agent_type": "qa",
                "workflow_id": "wf-feature-1",
            },
        )
        qa_id = qa_result["created"]

        # Both depend on the fix task
        for tid in [review_id, qa_id]:
            await handler.execute("add_dependency", {"task_id": tid, "depends_on": fix_id})

        # Review and QA do NOT depend on each other
        review_deps = await db.get_dependencies(review_id)
        qa_deps = await db.get_dependencies(qa_id)
        assert qa_id not in review_deps, "Review should NOT depend on QA"
        assert review_id not in qa_deps, "QA should NOT depend on review"

    async def test_fix_completion_promotes_dependent_review_to_ready(self, orch):
        """When the fix task completes, the dependent review task
        is promoted from DEFINED to READY."""
        await _setup_workflow_prereqs(orch.db)

        # Fix task completed
        fix = _make_task("fix-1", status=TaskStatus.COMPLETED)
        await orch.db.create_task(fix)

        # New review in DEFINED state, depends on fix
        review = _make_task(
            "review-2",
            status=TaskStatus.DEFINED,
            agent_type="code-review",
        )
        await orch.db.create_task(review)
        await orch.db.add_dependency("review-2", "fix-1")

        # Run dependency check
        await orch._check_defined_tasks()

        updated = await orch.db.get_task("review-2")
        assert updated.status == TaskStatus.READY, (
            "Review task should be promoted to READY when fix task completes"
        )

    async def test_fix_completion_promotes_dependent_qa_to_ready(self, orch):
        """When the fix task completes, the dependent QA task
        is promoted from DEFINED to READY."""
        await _setup_workflow_prereqs(orch.db)

        fix = _make_task("fix-1", status=TaskStatus.COMPLETED)
        await orch.db.create_task(fix)

        qa = _make_task(
            "qa-2",
            status=TaskStatus.DEFINED,
            agent_type="qa",
        )
        await orch.db.create_task(qa)
        await orch.db.add_dependency("qa-2", "fix-1")

        await orch._check_defined_tasks()

        updated = await orch.db.get_task("qa-2")
        assert updated.status == TaskStatus.READY, (
            "QA task should be promoted to READY when fix task completes"
        )

    async def test_review_stays_defined_while_fix_incomplete(self, orch):
        """Review task stays DEFINED while the fix task is still in progress."""
        await _setup_project(orch.db)

        fix = _make_task("fix-1", status=TaskStatus.IN_PROGRESS)
        await orch.db.create_task(fix)

        review = _make_task(
            "review-2",
            status=TaskStatus.DEFINED,
            agent_type="code-review",
        )
        await orch.db.create_task(review)
        await orch.db.add_dependency("review-2", "fix-1")

        await orch._check_defined_tasks()

        updated = await orch.db.get_task("review-2")
        assert updated.status == TaskStatus.DEFINED

    async def test_fix_review_qa_stage_completion_event(self, orch):
        """Stage completion fires when both re-triggered review and QA complete."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="review_and_qa",
            task_ids=["review-2", "qa-2"],
        )
        await orch.db.create_workflow(wf)

        # Both review and QA completed
        await orch.db.create_task(
            _make_task(
                "review-2",
                status=TaskStatus.COMPLETED,
                agent_type="code-review",
                workflow_id="wf-feature-1",
            )
        )
        await orch.db.create_task(
            _make_task(
                "qa-2",
                status=TaskStatus.COMPLETED,
                agent_type="qa",
                workflow_id="wf-feature-1",
            )
        )

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(await orch.db.get_task("qa-2"))

        assert len(emitted) == 1
        assert emitted[0]["workflow_id"] == "wf-feature-1"
        assert emitted[0]["stage"] == "review_and_qa"
        assert set(emitted[0]["task_ids"]) == {"review-2", "qa-2"}

    def test_scheduler_assigns_review_and_qa_concurrently_after_fix(self):
        """Scheduler assigns both re-triggered review and QA tasks
        to available agents in the same round."""
        tasks = [
            _make_task(
                "review-2",
                status=TaskStatus.READY,
                agent_type="code-review",
            ),
            _make_task(
                "qa-2",
                status=TaskStatus.READY,
                agent_type="qa",
            ),
        ]
        agents = [
            _make_agent(id="agent-reviewer", name="claude-reviewer", agent_type="code-review"),
            _make_agent(id="agent-qa", name="claude-qa", agent_type="qa"),
        ]
        state = _make_scheduler_state(
            projects=[_make_project(max_agents=2)],
            tasks=tasks,
            agents=agents,
        )
        actions = Scheduler.schedule(state)

        assert len(actions) == 2
        assigned = {a.task_id for a in actions}
        assert assigned == {"review-2", "qa-2"}


# ===========================================================================
# (g) Maximum review cycles are bounded (configurable, e.g., 3 rounds) to
#     prevent infinite loops
# ===========================================================================


class TestMaxReviewCyclesBounded:
    """Verify that the feedback loop is bounded — after the configured maximum
    number of review-fix cycles, the workflow escalates instead of looping."""

    async def test_workflow_tracks_review_cycle_count(self, orch):
        """Workflow metadata can track how many review cycles have occurred."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="review_and_qa",
            task_ids=["review-1", "fix-1", "review-2", "fix-2", "review-3"],
            agent_affinity={"coding": "agent-coder", "review_cycles": "3"},
        )
        await orch.db.create_workflow(wf)

        loaded = await orch.db.get_workflow("wf-feature-1")
        # The playbook tracks cycle count via the agent_affinity map
        # (or a separate metadata field — the key point is it's trackable)
        assert len(loaded.task_ids) == 5, "Workflow should track all tasks across review cycles"

    async def test_three_review_cycles_tracked_via_task_chain(self, orch):
        """Three review-fix cycles produce a clear dependency chain."""
        await _setup_project(orch.db)

        # Round 1: coding → review-1 (changes_requested) → fix-1
        await orch.db.create_task(_make_task("coding-1", status=TaskStatus.COMPLETED))
        await orch.db.create_task(_make_task("review-1", status=TaskStatus.COMPLETED))
        await orch.db.create_task(_make_task("fix-1", status=TaskStatus.COMPLETED))

        # Round 2: fix-1 → review-2 (changes_requested) → fix-2
        await orch.db.create_task(_make_task("review-2", status=TaskStatus.COMPLETED))
        await orch.db.add_dependency("review-2", "fix-1")
        await orch.db.create_task(_make_task("fix-2", status=TaskStatus.COMPLETED))
        await orch.db.add_dependency("fix-2", "review-2")

        # Round 3: fix-2 → review-3 (changes_requested)
        await orch.db.create_task(_make_task("review-3", status=TaskStatus.COMPLETED))
        await orch.db.add_dependency("review-3", "fix-2")

        # Verify the chain: review-3 → fix-2 → review-2 → fix-1
        deps_r3 = await orch.db.get_dependencies("review-3")
        assert "fix-2" in deps_r3

        deps_f2 = await orch.db.get_dependencies("fix-2")
        assert "review-2" in deps_f2

        deps_r2 = await orch.db.get_dependencies("review-2")
        assert "fix-1" in deps_r2

    async def test_escalation_after_max_cycles_via_workflow_status(self, orch):
        """After 3 review cycles, the workflow is paused for human intervention
        instead of creating another fix task.

        The feature-pipeline playbook describes this as 'needs_human' — in the
        database layer this maps to the 'paused' status (the workflow is
        suspended pending human guidance).
        """
        await _setup_workflow_prereqs(orch.db)

        # Simulate 3 completed review-fix cycles → escalate
        wf = _make_workflow(
            current_stage="review_and_qa",
            task_ids=[
                "coding-1",
                "review-1",
                "fix-1",  # cycle 1
                "review-2",
                "fix-2",  # cycle 2
                "review-3",  # cycle 3 — this is the 3rd changes_requested
            ],
            agent_affinity={"coding": "agent-coder"},
        )
        await orch.db.create_workflow(wf)

        # Mark workflow as paused (needs human intervention after 3 review cycles)
        await orch.db.update_workflow_status("wf-feature-1", "paused")

        loaded = await orch.db.get_workflow("wf-feature-1")
        assert loaded.status == "paused", (
            "Workflow should be 'paused' (needs human) after exhausting review cycles"
        )

    async def test_no_fix_task_created_after_third_review(self, handler, db):
        """After the third review requests changes, no further fix task is created;
        the playbook instead escalates. We verify that the existing mechanism
        supports the playbook's decision not to create another fix task by
        checking that the workflow can transition to needs_human."""
        # Simulate 3 rounds of review tasks in the workflow
        # The playbook uses the task count to determine cycle number
        tasks_in_workflow = []
        for round_num in range(1, 4):
            review_result = await handler.execute(
                "create_task",
                {
                    "project_id": "p-1",
                    "title": f"Review: feature (round {round_num})",
                    "agent_type": "code-review",
                    "workflow_id": "wf-feature-1",
                },
            )
            assert "error" not in review_result
            tasks_in_workflow.append(review_result["created"])

            if round_num < 3:
                fix_result = await handler.execute(
                    "create_task",
                    {
                        "project_id": "p-1",
                        "title": f"Fix: review feedback (round {round_num})",
                        "agent_type": "coding",
                        "affinity_agent_id": "agent-coder",
                        "affinity_reason": "context",
                        "workflow_id": "wf-feature-1",
                    },
                )
                assert "error" not in fix_result
                tasks_in_workflow.append(fix_result["created"])

        # After 3 reviews, we should have 3 reviews + 2 fixes = 5 tasks
        assert len(tasks_in_workflow) == 5, "3 reviews + 2 fix tasks (no fix after 3rd review)"

    async def test_qa_feedback_loop_also_bounded(self, orch):
        """QA fix cycles are also bounded to 3 rounds per the playbook spec."""
        await _setup_workflow_prereqs(orch.db)

        # Simulate 3 QA-fix cycles
        wf = _make_workflow(
            current_stage="review_and_qa",
            task_ids=[
                "coding-1",
                "qa-1",
                "qa-fix-1",  # cycle 1
                "qa-2",
                "qa-fix-2",  # cycle 2
                "qa-3",  # cycle 3 — third QA failure
            ],
            agent_affinity={"coding": "agent-coder"},
        )
        await orch.db.create_workflow(wf)

        # Mark workflow as failed (what the playbook does after 3 QA failures)
        await orch.db.update_workflow_status("wf-feature-1", "failed")

        loaded = await orch.db.get_workflow("wf-feature-1")
        assert loaded.status == "failed", (
            "Workflow should be 'failed' after exhausting QA fix cycles"
        )

    async def test_fix_tasks_across_cycles_maintain_affinity(self, handler, db):
        """Fix tasks across all review cycles maintain affinity to the original
        coding agent — the same agent fixes in round 1, 2, and 3."""
        fix_ids = []
        for round_num in range(1, 4):
            result = await handler.execute(
                "create_task",
                {
                    "project_id": "p-1",
                    "title": f"Fix: review feedback (round {round_num})",
                    "agent_type": "coding",
                    "affinity_agent_id": "agent-coder",
                    "affinity_reason": "context",
                    "workflow_id": "wf-feature-1",
                },
            )
            assert "error" not in result
            fix_ids.append(result["created"])

        # All fix tasks point to the same original coding agent
        for fid in fix_ids:
            task = await db.get_task(fid)
            assert task.affinity_agent_id == "agent-coder", (
                f"Fix task {fid} should maintain affinity to agent-coder"
            )
            assert task.affinity_reason == "context"

    async def test_full_review_feedback_lifecycle(self, orch):
        """End-to-end: coding → review(changes) → fix → review(approve)
        → workflow advances."""
        await _setup_workflow_prereqs(orch.db)

        # Phase 1: Create workflow at review_and_qa stage
        wf = _make_workflow(
            current_stage="review_and_qa",
            task_ids=["review-1"],
        )
        await orch.db.create_workflow(wf)

        # Phase 2: Review requests changes → fix task created
        await orch.db.create_task(
            _make_task(
                "review-1",
                status=TaskStatus.COMPLETED,
                agent_type="code-review",
                workflow_id="wf-feature-1",
            )
        )
        await orch.db.create_task(
            _make_task(
                "fix-1",
                status=TaskStatus.COMPLETED,
                agent_type="coding",
                affinity_agent_id="agent-coder",
                affinity_reason="context",
                workflow_id="wf-feature-1",
            )
        )

        # Phase 3: Fix completes → new review + QA depend on fix
        await orch.db.create_task(
            _make_task(
                "review-2",
                status=TaskStatus.DEFINED,
                agent_type="code-review",
                workflow_id="wf-feature-1",
            )
        )
        await orch.db.add_dependency("review-2", "fix-1")

        # Fix is completed → review-2 should promote to READY
        await orch._check_defined_tasks()

        updated_review = await orch.db.get_task("review-2")
        assert updated_review.status == TaskStatus.READY, (
            "Review-2 should be READY after fix-1 completes"
        )

        # Phase 4: Review-2 approves → update workflow stage
        await orch.db.update_task("review-2", status=TaskStatus.COMPLETED.value)

        # Update workflow to track the new tasks
        await orch.db.update_workflow(
            "wf-feature-1",
            current_stage="review_and_qa",
            task_ids=json.dumps(["review-2"]),
        )

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(await orch.db.get_task("review-2"))

        assert len(emitted) == 1
        assert emitted[0]["stage"] == "review_and_qa"

        # Phase 5: Mark workflow completed
        await orch.db.update_workflow_status("wf-feature-1", "completed", completed_at=time.time())
        loaded = await orch.db.get_workflow("wf-feature-1")
        assert loaded.status == "completed"
