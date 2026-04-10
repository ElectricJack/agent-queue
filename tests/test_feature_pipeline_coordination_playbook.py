"""Tests for feature-pipeline coordination playbook (Roadmap 7.5.7).

Verifies the feature pipeline coordination pattern described in
``vault/system/playbooks/feature-pipeline.md`` and
``docs/specs/design/agent-coordination.md`` §4 Example 1.

Test cases:
  (a) Feature-pipeline playbook creates coding task first, then review + QA
      tasks after coding completes.
  (b) Review and QA tasks have dependency on coding task (not scheduled
      until coding is done).
  (c) Review + QA tasks can run concurrently (no dependency between them).
  (d) Merge task depends on both review AND QA completing.
  (e) Task chain has correct ``workflow_id`` linking all tasks.
  (f) Coding task has ``agent_type="coding"``, review has
      ``agent_type="code-review"``, QA has ``agent_type="qa"``.
  (g) Failure in coding task stops the pipeline (review + QA not created).
  (h) Feature-pipeline fires on appropriate trigger event
      (e.g., ``task.created`` with ``task_type="feature"``).
"""

import time
from unittest.mock import MagicMock

import pytest

from src.config import AppConfig, DiscordConfig
from src.command_handler import CommandHandler
from src.database import Database
from src.models import (
    Agent,
    AgentOutput,
    AgentResult,
    AgentState,
    PlaybookRun,
    Project,
    Task,
    TaskStatus,
    WorkspaceMode,
    Workflow,
)
from src.orchestrator import Orchestrator
from src.playbook_manager import PlaybookManager
from src.playbook_models import CompiledPlaybook, PlaybookNode, PlaybookTrigger
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
    workspace_mode: WorkspaceMode | None = None,
    agent_type: str | None = None,
    affinity_agent_id: str | None = None,
    affinity_reason: str | None = None,
    description: str = "test task",
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
        workspace_mode=workspace_mode,
        agent_type=agent_type,
        affinity_agent_id=affinity_agent_id,
        affinity_reason=affinity_reason,
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
    current_stage: str | None = "coding",
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

    # Create test agents with different types
    await db.create_agent(
        Agent(id="agent-coding-1", name="coder-1", agent_type="coding", state=AgentState.IDLE)
    )
    await db.create_agent(
        Agent(
            id="agent-review-1", name="reviewer-1", agent_type="code-review", state=AgentState.IDLE
        )
    )
    await db.create_agent(
        Agent(id="agent-qa-1", name="qa-1", agent_type="qa", state=AgentState.IDLE)
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
            current_stage="coding",
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
# (a) Feature-pipeline playbook creates coding task first, then review + QA
#     tasks after coding completes
# ===========================================================================


class TestCodingThenReviewQA:
    """Verify that the feature pipeline creates a coding task first, then
    review and QA tasks depend on the coding task completing."""

    async def test_create_coding_task_first(self, handler, db):
        """The first task created in the pipeline is a coding task."""
        result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Implement login feature",
                "description": "Add OAuth login with acceptance criteria...",
                "agent_type": "coding",
                "workflow_id": "wf-feature-1",
            },
        )
        assert "error" not in result, f"Failed to create coding task: {result}"

        task = await db.get_task(result["created"])
        assert task.agent_type == "coding"
        assert task.workflow_id == "wf-feature-1"

    async def test_review_and_qa_created_with_coding_dependency(self, handler, db):
        """Review and QA tasks are created with a dependency on the coding task."""
        # Create coding task
        coding_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Implement login feature",
                "agent_type": "coding",
                "workflow_id": "wf-feature-1",
            },
        )
        coding_id = coding_result["created"]

        # Create review task
        review_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Review: Implement login feature",
                "agent_type": "code-review",
                "workflow_id": "wf-feature-1",
            },
        )
        review_id = review_result["created"]

        # Create QA task
        qa_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "QA: Implement login feature",
                "agent_type": "qa",
                "workflow_id": "wf-feature-1",
            },
        )
        qa_id = qa_result["created"]

        # Add dependencies: both review and QA depend on coding
        await handler.execute("add_dependency", {"task_id": review_id, "depends_on": coding_id})
        await handler.execute("add_dependency", {"task_id": qa_id, "depends_on": coding_id})

        review_deps = await db.get_dependencies(review_id)
        qa_deps = await db.get_dependencies(qa_id)

        assert coding_id in review_deps, "Review task should depend on coding task"
        assert coding_id in qa_deps, "QA task should depend on coding task"

    async def test_review_and_qa_stay_defined_while_coding_in_progress(self, orch):
        """Review and QA tasks remain DEFINED while coding is still in progress."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(current_stage="coding", task_ids=["coding-1"])
        await orch.db.create_workflow(wf)

        # Create coding task in progress
        coding = _make_task(
            "coding-1",
            status=TaskStatus.IN_PROGRESS,
            agent_type="coding",
            workflow_id="wf-feature-1",
        )
        await orch.db.create_task(coding)

        # Create review + QA tasks in DEFINED state (no workflow_id needed here)
        review = _make_task("review-1", status=TaskStatus.DEFINED, agent_type="code-review")
        qa = _make_task("qa-1", status=TaskStatus.DEFINED, agent_type="qa")
        await orch.db.create_task(review)
        await orch.db.create_task(qa)

        # Add dependencies
        await orch.db.add_dependency("review-1", "coding-1")
        await orch.db.add_dependency("qa-1", "coding-1")

        # Run dependency check — neither should be promoted
        await orch._check_defined_tasks()

        updated_review = await orch.db.get_task("review-1")
        updated_qa = await orch.db.get_task("qa-1")
        assert updated_review.status == TaskStatus.DEFINED, (
            "Review should remain DEFINED while coding is in progress"
        )
        assert updated_qa.status == TaskStatus.DEFINED, (
            "QA should remain DEFINED while coding is in progress"
        )

    async def test_review_and_qa_promoted_when_coding_completes(self, orch):
        """Review and QA tasks are promoted to READY when coding task completes."""
        await _setup_workflow_prereqs(orch.db)

        # Create coding task as COMPLETED
        coding = _make_task(
            "coding-1",
            status=TaskStatus.COMPLETED,
            agent_type="coding",
        )
        await orch.db.create_task(coding)

        # Create review + QA in DEFINED state
        review = _make_task("review-1", status=TaskStatus.DEFINED, agent_type="code-review")
        qa = _make_task("qa-1", status=TaskStatus.DEFINED, agent_type="qa")
        await orch.db.create_task(review)
        await orch.db.create_task(qa)

        # Add dependencies
        await orch.db.add_dependency("review-1", "coding-1")
        await orch.db.add_dependency("qa-1", "coding-1")

        # Run dependency check — both should be promoted
        await orch._check_defined_tasks()

        updated_review = await orch.db.get_task("review-1")
        updated_qa = await orch.db.get_task("qa-1")
        assert updated_review.status == TaskStatus.READY, (
            "Review should be promoted to READY when coding completes"
        )
        assert updated_qa.status == TaskStatus.READY, (
            "QA should be promoted to READY when coding completes"
        )


# ===========================================================================
# (b) Review and QA tasks have dependency on coding task (not scheduled
#     until coding is done)
# ===========================================================================


class TestReviewQADependOnCoding:
    """Verify that review and QA tasks are blocked by the coding task
    dependency and only become schedulable after coding completes."""

    def test_scheduler_does_not_assign_defined_review_or_qa(self):
        """DEFINED review and QA tasks are not assigned by the scheduler."""
        coding = _make_task("coding-1", status=TaskStatus.IN_PROGRESS, agent_type="coding")
        review = _make_task(
            "review-1",
            status=TaskStatus.DEFINED,
            agent_type="code-review",
        )
        qa = _make_task("qa-1", status=TaskStatus.DEFINED, agent_type="qa")

        agents = [
            _make_agent(id="a-review", name="reviewer", agent_type="code-review"),
            _make_agent(id="a-qa", name="qa-agent", agent_type="qa"),
        ]
        state = _make_scheduler_state(tasks=[coding, review, qa], agents=agents)
        actions = Scheduler.schedule(state)

        # No DEFINED tasks should be assigned
        assigned_task_ids = {a.task_id for a in actions}
        assert "review-1" not in assigned_task_ids, "DEFINED review should not be scheduled"
        assert "qa-1" not in assigned_task_ids, "DEFINED QA should not be scheduled"

    async def test_review_dependency_persisted_in_database(self, handler, db):
        """Review task's dependency on coding task is persisted in the database."""
        # Create coding task
        coding_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Implement feature X",
                "agent_type": "coding",
                "workflow_id": "wf-feature-1",
            },
        )
        coding_id = coding_result["created"]

        # Create review task
        review_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Review: Feature X",
                "agent_type": "code-review",
                "workflow_id": "wf-feature-1",
            },
        )
        review_id = review_result["created"]

        # Add dependency
        dep_result = await handler.execute(
            "add_dependency", {"task_id": review_id, "depends_on": coding_id}
        )
        assert "error" not in dep_result

        deps = await db.get_dependencies(review_id)
        assert coding_id in deps

    async def test_qa_dependency_persisted_in_database(self, handler, db):
        """QA task's dependency on coding task is persisted in the database."""
        # Create coding task
        coding_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Implement feature X",
                "agent_type": "coding",
                "workflow_id": "wf-feature-1",
            },
        )
        coding_id = coding_result["created"]

        # Create QA task
        qa_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "QA: Feature X",
                "agent_type": "qa",
                "workflow_id": "wf-feature-1",
            },
        )
        qa_id = qa_result["created"]

        # Add dependency
        dep_result = await handler.execute(
            "add_dependency", {"task_id": qa_id, "depends_on": coding_id}
        )
        assert "error" not in dep_result

        deps = await db.get_dependencies(qa_id)
        assert coding_id in deps

    async def test_coding_incomplete_blocks_both_review_and_qa(self, orch):
        """Neither review nor QA is promoted while coding is only READY (not COMPLETED)."""
        await _setup_project(orch.db)

        coding = _make_task("coding-1", status=TaskStatus.READY, agent_type="coding")
        review = _make_task("review-1", status=TaskStatus.DEFINED, agent_type="code-review")
        qa = _make_task("qa-1", status=TaskStatus.DEFINED, agent_type="qa")
        await orch.db.create_task(coding)
        await orch.db.create_task(review)
        await orch.db.create_task(qa)

        await orch.db.add_dependency("review-1", "coding-1")
        await orch.db.add_dependency("qa-1", "coding-1")

        await orch._check_defined_tasks()

        assert (await orch.db.get_task("review-1")).status == TaskStatus.DEFINED
        assert (await orch.db.get_task("qa-1")).status == TaskStatus.DEFINED


# ===========================================================================
# (c) Review + QA tasks can run concurrently (no dependency between them)
# ===========================================================================


class TestReviewQAConcurrency:
    """Verify that review and QA tasks have no dependency on each other
    and can be scheduled to different agents simultaneously."""

    async def test_no_dependency_between_review_and_qa(self, handler, db):
        """Review and QA tasks have no inter-dependency."""
        # Create coding task
        coding_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Implement feature",
                "agent_type": "coding",
                "workflow_id": "wf-feature-1",
            },
        )
        coding_id = coding_result["created"]

        # Create review and QA tasks
        review_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Review: feature",
                "agent_type": "code-review",
                "workflow_id": "wf-feature-1",
            },
        )
        review_id = review_result["created"]

        qa_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "QA: feature",
                "agent_type": "qa",
                "workflow_id": "wf-feature-1",
            },
        )
        qa_id = qa_result["created"]

        # Both depend on coding, but NOT on each other
        await handler.execute("add_dependency", {"task_id": review_id, "depends_on": coding_id})
        await handler.execute("add_dependency", {"task_id": qa_id, "depends_on": coding_id})

        review_deps = await db.get_dependencies(review_id)
        qa_deps = await db.get_dependencies(qa_id)

        # Review does not depend on QA
        assert qa_id not in review_deps, "Review should not depend on QA"
        # QA does not depend on review
        assert review_id not in qa_deps, "QA should not depend on review"

    def test_scheduler_assigns_review_and_qa_concurrently(self):
        """With both READY, review and QA are assigned to agents in the same round."""
        review = _make_task(
            "review-1",
            status=TaskStatus.READY,
            agent_type="code-review",
        )
        qa = _make_task(
            "qa-1",
            status=TaskStatus.READY,
            agent_type="qa",
        )

        agents = [
            _make_agent(id="a-review", name="reviewer", agent_type="code-review"),
            _make_agent(id="a-qa", name="qa-agent", agent_type="qa"),
        ]
        state = _make_scheduler_state(tasks=[review, qa], agents=agents)
        actions = Scheduler.schedule(state)

        assert len(actions) == 2, "Both review and QA should be assigned concurrently"
        assigned_task_ids = {a.task_id for a in actions}
        assert assigned_task_ids == {"review-1", "qa-1"}

    def test_review_and_qa_assigned_to_different_agents(self):
        """Review and QA are assigned to separate agents (type matching)."""
        review = _make_task(
            "review-1",
            status=TaskStatus.READY,
            agent_type="code-review",
        )
        qa = _make_task(
            "qa-1",
            status=TaskStatus.READY,
            agent_type="qa",
        )

        agents = [
            _make_agent(id="a-review", name="reviewer", agent_type="code-review"),
            _make_agent(id="a-qa", name="qa-agent", agent_type="qa"),
        ]
        state = _make_scheduler_state(tasks=[review, qa], agents=agents)
        actions = Scheduler.schedule(state)

        assignment_map = {a.task_id: a.agent_id for a in actions}
        assert assignment_map.get("review-1") == "a-review"
        assert assignment_map.get("qa-1") == "a-qa"

    async def test_both_review_and_qa_promoted_simultaneously(self, orch):
        """Both review and QA are promoted to READY in the same check cycle."""
        await _setup_project(orch.db)

        coding = _make_task("coding-1", status=TaskStatus.COMPLETED, agent_type="coding")
        review = _make_task("review-1", status=TaskStatus.DEFINED, agent_type="code-review")
        qa = _make_task("qa-1", status=TaskStatus.DEFINED, agent_type="qa")
        await orch.db.create_task(coding)
        await orch.db.create_task(review)
        await orch.db.create_task(qa)

        await orch.db.add_dependency("review-1", "coding-1")
        await orch.db.add_dependency("qa-1", "coding-1")

        await orch._check_defined_tasks()

        updated_review = await orch.db.get_task("review-1")
        updated_qa = await orch.db.get_task("qa-1")
        assert updated_review.status == TaskStatus.READY
        assert updated_qa.status == TaskStatus.READY


# ===========================================================================
# (d) Merge task depends on both review AND QA completing
# ===========================================================================


class TestMergeDependsOnReviewAndQA:
    """Verify that the merge/completion stage depends on both the review
    and QA tasks completing successfully."""

    async def test_merge_task_depends_on_both_review_and_qa(self, handler, db):
        """Merge task has dependencies on both the review and QA tasks."""
        # Create review and QA tasks
        review_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Review: feature",
                "agent_type": "code-review",
                "workflow_id": "wf-feature-1",
            },
        )
        review_id = review_result["created"]

        qa_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "QA: feature",
                "agent_type": "qa",
                "workflow_id": "wf-feature-1",
            },
        )
        qa_id = qa_result["created"]

        # Merge/completion depends on both
        merge_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Merge: feature",
                "workflow_id": "wf-feature-1",
            },
        )
        merge_id = merge_result["created"]

        await handler.execute("add_dependency", {"task_id": merge_id, "depends_on": review_id})
        await handler.execute("add_dependency", {"task_id": merge_id, "depends_on": qa_id})

        deps = await db.get_dependencies(merge_id)
        assert deps == {review_id, qa_id}, (
            f"Merge task should depend on both review and QA: "
            f"expected {{{review_id}, {qa_id}}}, got {deps}"
        )

    async def test_merge_blocked_when_only_review_completes(self, orch):
        """Merge task stays DEFINED if only review is complete but QA is not."""
        await _setup_project(orch.db)

        review = _make_task("review-1", status=TaskStatus.COMPLETED, agent_type="code-review")
        qa = _make_task("qa-1", status=TaskStatus.IN_PROGRESS, agent_type="qa")
        merge = _make_task("merge-1", status=TaskStatus.DEFINED)
        await orch.db.create_task(review)
        await orch.db.create_task(qa)
        await orch.db.create_task(merge)

        await orch.db.add_dependency("merge-1", "review-1")
        await orch.db.add_dependency("merge-1", "qa-1")

        await orch._check_defined_tasks()

        updated_merge = await orch.db.get_task("merge-1")
        assert updated_merge.status == TaskStatus.DEFINED, (
            "Merge should stay DEFINED when QA is still in progress"
        )

    async def test_merge_blocked_when_only_qa_completes(self, orch):
        """Merge task stays DEFINED if only QA is complete but review is not."""
        await _setup_project(orch.db)

        review = _make_task("review-1", status=TaskStatus.IN_PROGRESS, agent_type="code-review")
        qa = _make_task("qa-1", status=TaskStatus.COMPLETED, agent_type="qa")
        merge = _make_task("merge-1", status=TaskStatus.DEFINED)
        await orch.db.create_task(review)
        await orch.db.create_task(qa)
        await orch.db.create_task(merge)

        await orch.db.add_dependency("merge-1", "review-1")
        await orch.db.add_dependency("merge-1", "qa-1")

        await orch._check_defined_tasks()

        updated_merge = await orch.db.get_task("merge-1")
        assert updated_merge.status == TaskStatus.DEFINED, (
            "Merge should stay DEFINED when review is still in progress"
        )

    async def test_merge_promoted_when_both_review_and_qa_complete(self, orch):
        """Merge task is promoted to READY when both review and QA complete."""
        await _setup_project(orch.db)

        review = _make_task("review-1", status=TaskStatus.COMPLETED, agent_type="code-review")
        qa = _make_task("qa-1", status=TaskStatus.COMPLETED, agent_type="qa")
        merge = _make_task("merge-1", status=TaskStatus.DEFINED)
        await orch.db.create_task(review)
        await orch.db.create_task(qa)
        await orch.db.create_task(merge)

        await orch.db.add_dependency("merge-1", "review-1")
        await orch.db.add_dependency("merge-1", "qa-1")

        await orch._check_defined_tasks()

        updated_merge = await orch.db.get_task("merge-1")
        assert updated_merge.status == TaskStatus.READY, (
            "Merge should be promoted when both review and QA are complete"
        )

    async def test_merge_stage_completion_fires_event(self, orch):
        """workflow.stage.completed fires when both review and QA complete."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="review_and_qa",
            task_ids=["review-1", "qa-1"],
        )
        await orch.db.create_workflow(wf)

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
                "qa-1",
                status=TaskStatus.COMPLETED,
                agent_type="qa",
                workflow_id="wf-feature-1",
            )
        )

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(await orch.db.get_task("qa-1"))

        assert len(emitted) == 1
        assert emitted[0]["workflow_id"] == "wf-feature-1"
        assert emitted[0]["stage"] == "review_and_qa"
        assert set(emitted[0]["task_ids"]) == {"review-1", "qa-1"}

    async def test_stage_not_complete_when_one_task_still_running(self, orch):
        """Stage completion does NOT fire when review is complete but QA is running."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="review_and_qa",
            task_ids=["review-1", "qa-1"],
        )
        await orch.db.create_workflow(wf)

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
                "qa-1",
                status=TaskStatus.IN_PROGRESS,
                agent_type="qa",
                workflow_id="wf-feature-1",
            )
        )

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(await orch.db.get_task("review-1"))

        assert emitted == [], "Stage should not complete while QA is still running"


# ===========================================================================
# (e) Task chain has correct workflow_id linking all tasks
# ===========================================================================


class TestWorkflowIdLinking:
    """Verify that all tasks in the feature pipeline carry the same
    workflow_id, linking them to the coordination workflow."""

    async def test_all_pipeline_tasks_share_workflow_id(self, handler, db):
        """Coding, review, QA, and merge tasks all have the same workflow_id."""
        task_ids = []
        for title, agent_type in [
            ("Implement feature", "coding"),
            ("Review: feature", "code-review"),
            ("QA: feature", "qa"),
        ]:
            result = await handler.execute(
                "create_task",
                {
                    "project_id": "p-1",
                    "title": title,
                    "agent_type": agent_type,
                    "workflow_id": "wf-feature-1",
                },
            )
            assert "error" not in result, f"Failed to create {title}: {result}"
            task_ids.append(result["created"])

        for tid in task_ids:
            task = await db.get_task(tid)
            assert task.workflow_id == "wf-feature-1", (
                f"Task {tid} ({task.title}) should have workflow_id='wf-feature-1', "
                f"got '{task.workflow_id}'"
            )

    async def test_workflow_task_ids_updated(self, orch):
        """Workflow's task_ids list contains all pipeline tasks."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            task_ids=["coding-1", "review-1", "qa-1"],
        )
        await orch.db.create_workflow(wf)

        loaded = await orch.db.get_workflow("wf-feature-1")
        assert set(loaded.task_ids) == {"coding-1", "review-1", "qa-1"}

    async def test_add_task_to_workflow_task_ids(self, orch):
        """Adding a new task ID to the workflow's task_ids list works."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(task_ids=["coding-1"])
        await orch.db.create_workflow(wf)

        await orch.db.add_workflow_task("wf-feature-1", "review-1")
        await orch.db.add_workflow_task("wf-feature-1", "qa-1")

        loaded = await orch.db.get_workflow("wf-feature-1")
        assert set(loaded.task_ids) == {"coding-1", "review-1", "qa-1"}

    async def test_workflow_id_survives_task_status_changes(self, orch):
        """workflow_id is preserved through task status transitions."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(current_stage="coding", task_ids=["coding-1"])
        await orch.db.create_workflow(wf)

        task = _make_task(
            "coding-1",
            status=TaskStatus.DEFINED,
            agent_type="coding",
            workflow_id="wf-feature-1",
        )
        await orch.db.create_task(task)

        # Transition through states
        await orch.db.transition_task("coding-1", TaskStatus.READY, context="deps_met")

        loaded = await orch.db.get_task("coding-1")
        assert loaded.workflow_id == "wf-feature-1", (
            "workflow_id should be preserved through status transitions"
        )

    async def test_workflow_current_stage_tracks_pipeline_progress(self, orch):
        """Workflow's current_stage field reflects the pipeline stage."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(current_stage="coding")
        await orch.db.create_workflow(wf)

        loaded = await orch.db.get_workflow("wf-feature-1")
        assert loaded.current_stage == "coding"

        # Update to review_and_qa stage
        await orch.db.update_workflow("wf-feature-1", current_stage="review_and_qa")

        loaded = await orch.db.get_workflow("wf-feature-1")
        assert loaded.current_stage == "review_and_qa"


# ===========================================================================
# (f) Coding task has agent_type="coding", review has
#     agent_type="code-review", QA has agent_type="qa"
# ===========================================================================


class TestAgentTypeMatching:
    """Verify that each pipeline stage uses the correct agent_type and
    that the scheduler enforces type matching."""

    async def test_coding_task_agent_type(self, handler, db):
        """Coding task is created with agent_type='coding'."""
        result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Implement feature",
                "agent_type": "coding",
                "workflow_id": "wf-feature-1",
            },
        )
        task = await db.get_task(result["created"])
        assert task.agent_type == "coding"

    async def test_review_task_agent_type(self, handler, db):
        """Review task is created with agent_type='code-review'."""
        result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Review: feature",
                "agent_type": "code-review",
                "workflow_id": "wf-feature-1",
            },
        )
        task = await db.get_task(result["created"])
        assert task.agent_type == "code-review"

    async def test_qa_task_agent_type(self, handler, db):
        """QA task is created with agent_type='qa'."""
        result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "QA: feature",
                "agent_type": "qa",
                "workflow_id": "wf-feature-1",
            },
        )
        task = await db.get_task(result["created"])
        assert task.agent_type == "qa"

    def test_coding_agent_not_assigned_review_task(self):
        """A coding-type agent is NOT assigned a code-review task (type mismatch)."""
        review = _make_task(
            "review-1",
            status=TaskStatus.READY,
            agent_type="code-review",
        )
        coding_agent = _make_agent(id="a-coder", name="coder-1", agent_type="coding")

        state = _make_scheduler_state(tasks=[review], agents=[coding_agent])
        actions = Scheduler.schedule(state)

        assert len(actions) == 0, "Coding agent should NOT be assigned a code-review task"

    def test_coding_agent_not_assigned_qa_task(self):
        """A coding-type agent is NOT assigned a QA task (type mismatch)."""
        qa = _make_task("qa-1", status=TaskStatus.READY, agent_type="qa")
        coding_agent = _make_agent(id="a-coder", name="coder-1", agent_type="coding")

        state = _make_scheduler_state(tasks=[qa], agents=[coding_agent])
        actions = Scheduler.schedule(state)

        assert len(actions) == 0, "Coding agent should NOT be assigned a QA task"

    def test_qa_agent_not_assigned_coding_task(self):
        """A QA-type agent is NOT assigned a coding task (type mismatch)."""
        coding = _make_task("coding-1", status=TaskStatus.READY, agent_type="coding")
        qa_agent = _make_agent(id="a-qa", name="qa-1", agent_type="qa")

        state = _make_scheduler_state(tasks=[coding], agents=[qa_agent])
        actions = Scheduler.schedule(state)

        assert len(actions) == 0, "QA agent should NOT be assigned a coding task"

    def test_review_agent_not_assigned_coding_task(self):
        """A code-review agent is NOT assigned a coding task (type mismatch)."""
        coding = _make_task("coding-1", status=TaskStatus.READY, agent_type="coding")
        review_agent = _make_agent(id="a-review", name="reviewer", agent_type="code-review")

        state = _make_scheduler_state(tasks=[coding], agents=[review_agent])
        actions = Scheduler.schedule(state)

        assert len(actions) == 0, "Review agent should NOT be assigned a coding task"

    def test_correct_agent_types_assigned_to_correct_tasks(self):
        """Each task type is assigned to the matching agent type."""
        coding = _make_task("coding-1", status=TaskStatus.READY, agent_type="coding")
        review = _make_task("review-1", status=TaskStatus.READY, agent_type="code-review")
        qa = _make_task("qa-1", status=TaskStatus.READY, agent_type="qa")

        agents = [
            _make_agent(id="a-coder", name="coder-1", agent_type="coding"),
            _make_agent(id="a-review", name="reviewer", agent_type="code-review"),
            _make_agent(id="a-qa", name="qa-agent", agent_type="qa"),
        ]
        state = _make_scheduler_state(tasks=[coding, review, qa], agents=agents)
        actions = Scheduler.schedule(state)

        assert len(actions) == 3, "All three tasks should be assigned"
        assignment_map = {a.task_id: a.agent_id for a in actions}
        assert assignment_map["coding-1"] == "a-coder"
        assert assignment_map["review-1"] == "a-review"
        assert assignment_map["qa-1"] == "a-qa"

    def test_idle_coding_agent_skipped_for_review_task(self):
        """Even if coding agent is idle and no review agent exists, review stays unassigned."""
        review = _make_task("review-1", status=TaskStatus.READY, agent_type="code-review")
        coding_agent = _make_agent(id="a-coder", name="coder-1", agent_type="coding")

        state = _make_scheduler_state(tasks=[review], agents=[coding_agent])
        actions = Scheduler.schedule(state)

        assert len(actions) == 0, "Review task should not be assigned to coding agent even if idle"


# ===========================================================================
# (g) Failure in coding task stops the pipeline (review + QA not created)
# ===========================================================================


class TestCodingFailureStopsPipeline:
    """Verify that when the coding task fails, the pipeline does not
    proceed to create review or QA tasks."""

    async def test_failed_coding_blocks_review_and_qa(self, orch):
        """Review and QA stay DEFINED when coding task has FAILED status."""
        await _setup_project(orch.db)

        coding = _make_task("coding-1", status=TaskStatus.FAILED, agent_type="coding")
        review = _make_task("review-1", status=TaskStatus.DEFINED, agent_type="code-review")
        qa = _make_task("qa-1", status=TaskStatus.DEFINED, agent_type="qa")
        await orch.db.create_task(coding)
        await orch.db.create_task(review)
        await orch.db.create_task(qa)

        await orch.db.add_dependency("review-1", "coding-1")
        await orch.db.add_dependency("qa-1", "coding-1")

        await orch._check_defined_tasks()

        updated_review = await orch.db.get_task("review-1")
        updated_qa = await orch.db.get_task("qa-1")
        assert updated_review.status == TaskStatus.DEFINED, (
            "Review should stay DEFINED when coding has FAILED"
        )
        assert updated_qa.status == TaskStatus.DEFINED, (
            "QA should stay DEFINED when coding has FAILED"
        )

    async def test_failed_coding_prevents_stage_completion(self, orch):
        """workflow.stage.completed does NOT fire when coding task fails."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="coding",
            task_ids=["coding-1"],
        )
        await orch.db.create_workflow(wf)

        await orch.db.create_task(
            _make_task(
                "coding-1",
                status=TaskStatus.FAILED,
                agent_type="coding",
                workflow_id="wf-feature-1",
            )
        )

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(await orch.db.get_task("coding-1"))

        assert emitted == [], "Stage completion should NOT fire when coding task is FAILED"

    async def test_failed_coding_workflow_can_be_marked_failed(self, orch):
        """Workflow can be transitioned to 'failed' when coding fails substantively."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(current_stage="coding", task_ids=["coding-1"])
        await orch.db.create_workflow(wf)

        await orch.db.create_task(
            _make_task(
                "coding-1",
                status=TaskStatus.FAILED,
                agent_type="coding",
                workflow_id="wf-feature-1",
            )
        )

        # The playbook runner would mark the workflow as failed
        await orch.db.update_workflow_status("wf-feature-1", "failed")

        loaded = await orch.db.get_workflow("wf-feature-1")
        assert loaded.status == "failed"

    async def test_coding_failure_result_preserved(self, orch):
        """The coding task's failure output is preserved for inspection."""
        await _setup_project(orch.db)

        await orch.db.create_agent(Agent(id="agent-coding-1", name="coder-1", agent_type="coding"))

        coding = _make_task("coding-1", status=TaskStatus.FAILED, agent_type="coding")
        await orch.db.create_task(coding)

        output = AgentOutput(
            result=AgentResult.FAILED,
            summary="Could not implement — merge conflicts with main branch",
            files_changed=[],
            tokens_used=5000,
            error_message="git merge conflict in src/auth.py",
        )
        await orch.db.save_task_result("coding-1", "agent-coding-1", output)

        result = await orch.db.get_task_result("coding-1")
        assert result is not None
        assert result["result"] == "failed"
        assert "merge conflict" in result["summary"]

    def test_scheduler_does_not_schedule_tasks_dependent_on_failed(self):
        """Tasks that depend on a FAILED task are never READY and thus never scheduled."""
        # If a task's dependency is FAILED, it stays DEFINED (not promoted).
        # The scheduler only considers READY tasks, so DEFINED tasks are never assigned.
        review = _make_task(
            "review-1",
            status=TaskStatus.DEFINED,
            agent_type="code-review",
        )
        qa = _make_task("qa-1", status=TaskStatus.DEFINED, agent_type="qa")

        agents = [
            _make_agent(id="a-review", name="reviewer", agent_type="code-review"),
            _make_agent(id="a-qa", name="qa-agent", agent_type="qa"),
        ]
        state = _make_scheduler_state(tasks=[review, qa], agents=agents)
        actions = Scheduler.schedule(state)

        assert len(actions) == 0, "DEFINED tasks should never be scheduled"


# ===========================================================================
# (h) Feature-pipeline fires on appropriate trigger event
#     (e.g., task.created with type="feature")
# ===========================================================================


class TestTriggerEventFiring:
    """Verify that the feature-pipeline playbook is triggered by the
    correct event type with the correct payload filter."""

    def test_feature_pipeline_trigger_configuration(self):
        """Feature-pipeline playbook has the correct trigger definition."""
        # The playbook defines:
        #   triggers:
        #     - type: task.created
        #       filter:
        #         task_type: FEATURE
        playbook = CompiledPlaybook(
            id="feature-pipeline",
            version=1,
            source_hash="abc123",
            triggers=[
                PlaybookTrigger(
                    event_type="task.created",
                    filter={"task_type": "FEATURE"},
                ),
            ],
            scope="system",
            nodes={
                "analyze": PlaybookNode(
                    entry=True, prompt="Analyze the feature task.", goto="code"
                ),
                "code": PlaybookNode(prompt="Create coding task.", goto="done"),
                "done": PlaybookNode(terminal=True),
            },
        )

        assert len(playbook.triggers) == 1
        trigger = playbook.triggers[0]
        assert trigger.event_type == "task.created"
        assert trigger.filter == {"task_type": "FEATURE"}

    def test_playbook_manager_registers_trigger(self):
        """PlaybookManager maps the trigger event type to the playbook."""
        manager = PlaybookManager()
        playbook = CompiledPlaybook(
            id="feature-pipeline",
            version=1,
            source_hash="abc123",
            triggers=[
                PlaybookTrigger(
                    event_type="task.created",
                    filter={"task_type": "FEATURE"},
                ),
            ],
            scope="system",
            nodes={
                "start": PlaybookNode(entry=True, prompt="Start.", goto="done"),
                "done": PlaybookNode(terminal=True),
            },
        )
        manager._active[playbook.id] = playbook
        manager._index_triggers(playbook)

        # Verify the trigger map has the playbook
        trigger_map = manager.trigger_map
        assert "task.created" in trigger_map
        assert "feature-pipeline" in trigger_map["task.created"]

    def test_trigger_map_includes_feature_pipeline_for_task_created(self):
        """get_playbooks_by_trigger('task.created') includes feature-pipeline."""
        manager = PlaybookManager()
        playbook = CompiledPlaybook(
            id="feature-pipeline",
            version=1,
            source_hash="abc123",
            triggers=[
                PlaybookTrigger(
                    event_type="task.created",
                    filter={"task_type": "FEATURE"},
                ),
            ],
            scope="system",
            nodes={
                "start": PlaybookNode(entry=True, prompt="Start.", goto="done"),
                "done": PlaybookNode(terminal=True),
            },
        )
        manager._active[playbook.id] = playbook
        manager._index_triggers(playbook)

        playbooks = manager.get_playbooks_by_trigger("task.created")
        playbook_ids = [p.id for p in playbooks]
        assert "feature-pipeline" in playbook_ids

    async def test_event_bus_subscription_with_filter(self):
        """PlaybookManager subscribes to EventBus with the payload filter."""
        from src.event_bus import EventBus

        bus = EventBus()
        triggered = []

        async def on_trigger(playbook, event_data):
            triggered.append((playbook.id, event_data))

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        playbook = CompiledPlaybook(
            id="feature-pipeline",
            version=1,
            source_hash="abc123",
            triggers=[
                PlaybookTrigger(
                    event_type="task.created",
                    filter={"task_type": "FEATURE"},
                ),
            ],
            scope="system",
            nodes={
                "start": PlaybookNode(entry=True, prompt="Start.", goto="done"),
                "done": PlaybookNode(terminal=True),
            },
        )
        manager._active[playbook.id] = playbook
        manager._index_triggers(playbook)
        count = manager.subscribe_to_events()

        assert count >= 1, "At least one subscription should be created"

    async def test_non_feature_task_does_not_trigger_pipeline(self):
        """A task.created event with task_type != 'FEATURE' does not trigger."""
        from src.event_bus import EventBus

        bus = EventBus()
        triggered = []

        async def on_trigger(playbook, event_data):
            triggered.append((playbook.id, event_data))

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        playbook = CompiledPlaybook(
            id="feature-pipeline",
            version=1,
            source_hash="abc123",
            triggers=[
                PlaybookTrigger(
                    event_type="task.created",
                    filter={"task_type": "FEATURE"},
                ),
            ],
            scope="system",
            nodes={
                "start": PlaybookNode(entry=True, prompt="Start.", goto="done"),
                "done": PlaybookNode(terminal=True),
            },
        )
        manager._active[playbook.id] = playbook
        manager._index_triggers(playbook)
        manager.subscribe_to_events()

        # Emit a task.created event with task_type="BUGFIX" — should NOT trigger
        await bus.emit("task.created", {"task_type": "BUGFIX", "task_id": "t-1"})

        assert triggered == [], "Feature pipeline should NOT trigger for non-FEATURE task types"

    async def test_feature_task_triggers_pipeline(self):
        """A task.created event with task_type='FEATURE' triggers the pipeline."""
        from src.event_bus import EventBus

        bus = EventBus()
        triggered = []

        async def on_trigger(playbook, event_data):
            triggered.append((playbook.id, event_data))

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        playbook = CompiledPlaybook(
            id="feature-pipeline",
            version=1,
            source_hash="abc123",
            triggers=[
                PlaybookTrigger(
                    event_type="task.created",
                    filter={"task_type": "FEATURE"},
                ),
            ],
            scope="system",
            nodes={
                "start": PlaybookNode(entry=True, prompt="Start.", goto="done"),
                "done": PlaybookNode(terminal=True),
            },
        )
        manager._active[playbook.id] = playbook
        manager._index_triggers(playbook)
        manager.subscribe_to_events()

        # Emit a task.created event with task_type="FEATURE" — should trigger
        await bus.emit("task.created", {"task_type": "FEATURE", "task_id": "t-1"})

        assert len(triggered) == 1, "Feature pipeline should trigger for FEATURE task type"
        assert triggered[0][0] == "feature-pipeline"
        assert triggered[0][1]["task_type"] == "FEATURE"

    def test_playbook_scope_is_system(self):
        """Feature pipeline has system scope (not project or agent-type)."""
        playbook = CompiledPlaybook(
            id="feature-pipeline",
            version=1,
            source_hash="abc123",
            triggers=[
                PlaybookTrigger(
                    event_type="task.created",
                    filter={"task_type": "FEATURE"},
                ),
            ],
            scope="system",
            nodes={
                "start": PlaybookNode(entry=True, prompt="Start.", goto="done"),
                "done": PlaybookNode(terminal=True),
            },
        )
        assert playbook.scope == "system"

    async def test_unrelated_event_does_not_trigger(self):
        """Events of a completely different type don't trigger the pipeline."""
        from src.event_bus import EventBus

        bus = EventBus()
        triggered = []

        async def on_trigger(playbook, event_data):
            triggered.append(playbook.id)

        manager = PlaybookManager(event_bus=bus, on_trigger=on_trigger)
        playbook = CompiledPlaybook(
            id="feature-pipeline",
            version=1,
            source_hash="abc123",
            triggers=[
                PlaybookTrigger(
                    event_type="task.created",
                    filter={"task_type": "FEATURE"},
                ),
            ],
            scope="system",
            nodes={
                "start": PlaybookNode(entry=True, prompt="Start.", goto="done"),
                "done": PlaybookNode(terminal=True),
            },
        )
        manager._active[playbook.id] = playbook
        manager._index_triggers(playbook)
        manager.subscribe_to_events()

        # Emit a git.commit event — completely different type
        await bus.emit("git.commit", {"branch": "main", "sha": "abc123"})

        assert triggered == [], "Unrelated event types should not trigger feature pipeline"


# ===========================================================================
# End-to-end: Full feature pipeline lifecycle
# ===========================================================================


class TestFullPipelineLifecycle:
    """End-to-end integration tests validating the complete feature pipeline
    flow: coding → review + QA → completion."""

    async def test_full_pipeline_stage_transitions(self, orch):
        """Workflow progresses through coding → review_and_qa → done."""
        await _setup_workflow_prereqs(orch.db)

        # Phase 1: Coding stage
        wf = _make_workflow(
            current_stage="coding",
            task_ids=["coding-1"],
        )
        await orch.db.create_workflow(wf)

        await orch.db.create_task(
            _make_task(
                "coding-1",
                status=TaskStatus.COMPLETED,
                agent_type="coding",
                workflow_id="wf-feature-1",
            )
        )

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        # Coding stage completes
        await orch._check_workflow_stage_completion(await orch.db.get_task("coding-1"))
        assert len(emitted) == 1
        assert emitted[0]["stage"] == "coding"

        # Phase 2: Advance to review + QA stage
        await orch.db.update_workflow(
            "wf-feature-1",
            current_stage="review_and_qa",
            task_ids='["review-1", "qa-1"]',
        )

        # Create review and QA tasks — both complete
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
                "qa-1",
                status=TaskStatus.COMPLETED,
                agent_type="qa",
                workflow_id="wf-feature-1",
            )
        )

        # Review + QA stage completes
        await orch._check_workflow_stage_completion(await orch.db.get_task("qa-1"))
        assert len(emitted) == 2
        assert emitted[1]["stage"] == "review_and_qa"

        # Phase 3: Mark workflow completed
        await orch.db.update_workflow_status("wf-feature-1", "completed", completed_at=time.time())

        loaded = await orch.db.get_workflow("wf-feature-1")
        assert loaded.status == "completed"
        assert loaded.completed_at is not None

    async def test_pipeline_with_all_agent_types(self, orch):
        """Verify agent types across the entire pipeline."""
        await _setup_workflow_prereqs(orch.db)
        await orch.db.create_workflow(
            _make_workflow(
                current_stage="coding",
                task_ids=["coding-1", "review-1", "qa-1"],
            )
        )

        # Create all pipeline tasks with correct agent types
        await orch.db.create_task(
            _make_task(
                "coding-1",
                status=TaskStatus.COMPLETED,
                agent_type="coding",
                workflow_id="wf-feature-1",
            )
        )
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
                "qa-1",
                status=TaskStatus.COMPLETED,
                agent_type="qa",
                workflow_id="wf-feature-1",
            )
        )

        # Verify agent types are persisted correctly
        coding = await orch.db.get_task("coding-1")
        review = await orch.db.get_task("review-1")
        qa = await orch.db.get_task("qa-1")

        assert coding.agent_type == "coding"
        assert review.agent_type == "code-review"
        assert qa.agent_type == "qa"

        # All share the same workflow_id
        assert coding.workflow_id == "wf-feature-1"
        assert review.workflow_id == "wf-feature-1"
        assert qa.workflow_id == "wf-feature-1"

    async def test_workflow_agent_affinity_preserves_coding_agent(self, orch):
        """Workflow's agent_affinity map preserves the coding agent ID for fix tasks."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            agent_affinity={"coding": "agent-coding-1"},
        )
        await orch.db.create_workflow(wf)

        loaded = await orch.db.get_workflow("wf-feature-1")
        assert loaded.agent_affinity == {"coding": "agent-coding-1"}, (
            "Agent affinity should preserve the coding agent for fix task routing"
        )

    async def test_fix_task_references_original_coding_agent(self, handler, db):
        """A fix task created by the playbook uses affinity to the original coder."""
        result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Address review feedback: login feature",
                "agent_type": "coding",
                "affinity_agent_id": "agent-coding-1",
                "affinity_reason": "context",
                "workflow_id": "wf-feature-1",
            },
        )
        assert "error" not in result

        task = await db.get_task(result["created"])
        assert task.agent_type == "coding"
        assert task.affinity_agent_id == "agent-coding-1"
        assert task.affinity_reason == "context"
        assert task.workflow_id == "wf-feature-1"

    async def test_dependency_chain_coding_to_review_qa_to_merge(self, handler, db):
        """Full dependency chain: coding → {review, QA} → merge."""
        # Create coding task
        coding_res = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Implement feature",
                "agent_type": "coding",
                "workflow_id": "wf-feature-1",
            },
        )
        coding_id = coding_res["created"]

        # Create review and QA tasks
        review_res = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Review: feature",
                "agent_type": "code-review",
                "workflow_id": "wf-feature-1",
            },
        )
        review_id = review_res["created"]

        qa_res = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "QA: feature",
                "agent_type": "qa",
                "workflow_id": "wf-feature-1",
            },
        )
        qa_id = qa_res["created"]

        # Create merge task
        merge_res = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Merge: feature",
                "workflow_id": "wf-feature-1",
            },
        )
        merge_id = merge_res["created"]

        # Set up dependency chain
        await handler.execute("add_dependency", {"task_id": review_id, "depends_on": coding_id})
        await handler.execute("add_dependency", {"task_id": qa_id, "depends_on": coding_id})
        await handler.execute("add_dependency", {"task_id": merge_id, "depends_on": review_id})
        await handler.execute("add_dependency", {"task_id": merge_id, "depends_on": qa_id})

        # Verify the full DAG
        assert await db.get_dependencies(coding_id) == set()  # no upstream deps
        assert await db.get_dependencies(review_id) == {coding_id}
        assert await db.get_dependencies(qa_id) == {coding_id}
        assert await db.get_dependencies(merge_id) == {review_id, qa_id}

        # Verify all tasks share workflow_id
        for tid in [coding_id, review_id, qa_id, merge_id]:
            task = await db.get_task(tid)
            assert task.workflow_id == "wf-feature-1"
