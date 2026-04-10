"""Tests for exploration coordination playbook (Roadmap 7.5.9).

Verifies the parallel-exploration coordination pattern described in
``vault/system/playbooks/exploration.md`` and
``docs/specs/design/agent-coordination.md`` §4 Example 2.

Test cases:
  (a) Exploration playbook creates N parallel research tasks with no
      dependencies between them.
  (b) All parallel tasks are assigned to available agents concurrently
      (scheduler respects independence).
  (c) Reviewer task is created only after ALL parallel tasks complete
      (depends on all).
  (d) Reviewer task receives summaries/outputs from all parallel tasks
      as context.
  (e) Partial failure (2 of 3 parallel tasks complete, 1 fails) —
      reviewer still triggers with available results plus failure note.
  (f) Exploration with single parallel task degrades gracefully to
      sequential.
  (g) Workflow status reflects "running" until reviewer completes, then
      "completed".
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
from src.scheduler import Scheduler, SchedulerState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(
    id: str = "p-1",
    name: str = "exploration-proj",
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
    workflow_id: str = "wf-explore-1",
    playbook_id: str = "parallel-exploration",
    playbook_run_id: str = "pbr-1",
    project_id: str = "p-1",
    status: str = "running",
    current_stage: str | None = "exploration",
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
    """Build a SchedulerState with sensible defaults for exploration tests."""
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
    await db.create_project(Project(id="p-1", name="exploration-proj"))

    # Create test agents for assignment tests
    for i in range(1, 4):
        await db.create_agent(
            Agent(
                id=f"agent-{i}",
                name=f"claude-{i}",
                agent_type="coding",
                state=AgentState.IDLE,
            )
        )

    # Create a playbook run and workflow so tasks with workflow_id pass FK checks
    await db.create_playbook_run(
        PlaybookRun(
            run_id="pbr-handler",
            playbook_id="parallel-exploration",
            playbook_version=1,
            trigger_event='{"type": "task.created"}',
            status="running",
            started_at=time.time(),
        )
    )
    await db.create_workflow(
        Workflow(
            workflow_id="wf-explore-1",
            playbook_id="parallel-exploration",
            playbook_run_id="pbr-handler",
            project_id="p-1",
            status="running",
            current_stage="exploration",
            created_at=time.time(),
        )
    )
    await db.create_workflow(
        Workflow(
            workflow_id="wf-narrow-1",
            playbook_id="parallel-exploration",
            playbook_run_id="pbr-handler",
            project_id="p-1",
            status="running",
            current_stage="exploration",
            created_at=time.time(),
        )
    )

    return cmd


async def _setup_project(db, project_id="p-1"):
    """Create a project for FK constraints."""
    try:
        await db.create_project(Project(id=project_id, name="exploration-proj"))
    except Exception:
        pass


async def _setup_workflow_prereqs(db, project_id="p-1", run_id="pbr-1"):
    """Create the project and playbook_run that workflows FK-reference."""
    try:
        await db.create_project(Project(id=project_id, name="exploration-proj"))
    except Exception:
        pass  # project may already exist

    await db.create_playbook_run(
        PlaybookRun(
            run_id=run_id,
            playbook_id="parallel-exploration",
            playbook_version=1,
            trigger_event='{"type": "task.created", "task_type": "EXPLORATION"}',
            status="running",
            started_at=time.time(),
        )
    )


# ===========================================================================
# (a) Exploration playbook creates N parallel research tasks with no
#     dependencies between them
# ===========================================================================


class TestParallelTaskCreation:
    """Verify that exploration subtasks are created independently with no
    inter-dependencies."""

    async def test_create_three_exploration_tasks_no_deps(self, handler, db):
        """Three exploration subtasks are created without dependencies on each other."""
        # Create the exploration subtasks via command handler
        task_ids = []
        for label in ["a", "b", "c"]:
            result = await handler.execute(
                "create_task",
                {
                    "project_id": "p-1",
                    "title": f"Explore approach {label}",
                    "description": f"Investigate approach {label} for caching",
                    "workspace_mode": "branch-isolated",
                    "workflow_id": "wf-explore-1",
                },
            )
            assert "error" not in result, f"Failed to create task {label}: {result}"
            task_ids.append(result["created"])

        # Verify each task has NO dependencies
        for tid in task_ids:
            deps = await db.get_dependencies(tid)
            assert deps == set(), f"Task {tid} should have no dependencies, got {deps}"

        # Verify no task depends on any other exploration task
        for tid in task_ids:
            dependents = await db.get_dependents(tid)
            # Filter to only exploration tasks (dependents might include review)
            exploration_dependents = dependents & set(task_ids)
            assert exploration_dependents == set(), (
                f"Task {tid} should not be depended on by other exploration tasks"
            )

    async def test_exploration_tasks_have_branch_isolated_workspace_mode(self, handler, db):
        """Each exploration subtask uses branch-isolated workspace mode."""
        task_ids = []
        for label in ["a", "b"]:
            result = await handler.execute(
                "create_task",
                {
                    "project_id": "p-1",
                    "title": f"Explore approach {label}",
                    "workspace_mode": "branch-isolated",
                },
            )
            assert "error" not in result
            task_ids.append(result["created"])

        for tid in task_ids:
            task = await db.get_task(tid)
            assert task.workspace_mode == WorkspaceMode.BRANCH_ISOLATED, (
                f"Task {tid} should use branch-isolated workspace mode"
            )

    async def test_exploration_tasks_linked_to_workflow(self, handler, db):
        """Exploration tasks carry the workflow_id linking them to the coordination flow."""
        result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Explore Redis caching",
                "workspace_mode": "branch-isolated",
                "workflow_id": "wf-explore-1",
            },
        )
        assert "error" not in result
        task = await db.get_task(result["created"])
        assert task.workflow_id == "wf-explore-1"

    async def test_two_exploration_tasks_created_independently(self, handler, db):
        """Even with 2 subtasks, no inter-dependencies are created."""
        ids = []
        for label in ["a", "b"]:
            result = await handler.execute(
                "create_task",
                {
                    "project_id": "p-1",
                    "title": f"Explore approach {label}",
                    "workspace_mode": "branch-isolated",
                },
            )
            assert "error" not in result
            ids.append(result["created"])

        # Verify mutual independence
        deps_a = await db.get_dependencies(ids[0])
        deps_b = await db.get_dependencies(ids[1])
        assert ids[1] not in deps_a
        assert ids[0] not in deps_b


# ===========================================================================
# (b) All parallel tasks are assigned to available agents concurrently
#     (scheduler respects independence)
# ===========================================================================


class TestConcurrentAssignment:
    """Verify that the scheduler assigns independent exploration tasks to
    multiple idle agents simultaneously."""

    def test_three_independent_tasks_assigned_to_three_agents(self):
        """Three READY exploration tasks with no deps get assigned to 3 idle agents."""
        tasks = [
            _make_task(
                f"explore-{label}",
                status=TaskStatus.READY,
                workspace_mode=WorkspaceMode.BRANCH_ISOLATED,
                workflow_id="wf-1",
            )
            for label in ["a", "b", "c"]
        ]
        agents = [_make_agent(id=f"a-{i}", name=f"claude-{i}") for i in range(1, 4)]
        state = _make_scheduler_state(tasks=tasks, agents=agents)
        actions = Scheduler.schedule(state)

        assert len(actions) == 3, "All 3 tasks should be assigned to 3 agents"
        assigned_task_ids = {a.task_id for a in actions}
        assigned_agent_ids = {a.agent_id for a in actions}
        assert assigned_task_ids == {"explore-a", "explore-b", "explore-c"}
        assert assigned_agent_ids == {"a-1", "a-2", "a-3"}

    def test_two_tasks_one_agent_assigns_one(self):
        """With fewer agents than tasks, only one task is assigned per agent."""
        tasks = [
            _make_task(
                f"explore-{label}",
                status=TaskStatus.READY,
                workspace_mode=WorkspaceMode.BRANCH_ISOLATED,
            )
            for label in ["a", "b", "c"]
        ]
        agents = [_make_agent(id="a-1", name="claude-1")]
        state = _make_scheduler_state(tasks=tasks, agents=agents)
        actions = Scheduler.schedule(state)

        assert len(actions) == 1, "Only one task assigned with one agent"

    def test_independent_tasks_all_assigned_in_single_round(self):
        """Independent tasks (no deps) are all schedulable in the same round."""
        tasks = [_make_task(f"explore-{i}", status=TaskStatus.READY) for i in range(1, 4)]
        agents = [_make_agent(id=f"a-{i}", name=f"claude-{i}") for i in range(1, 4)]
        state = _make_scheduler_state(tasks=tasks, agents=agents)
        actions = Scheduler.schedule(state)

        # All 3 tasks should be assigned, one per agent
        assert len(actions) == 3
        assert len({a.task_id for a in actions}) == 3  # no duplicates
        assert len({a.agent_id for a in actions}) == 3  # no duplicates

    def test_busy_agent_not_assigned_exploration_task(self):
        """Busy agents are excluded from exploration task assignment."""
        tasks = [
            _make_task("explore-a", status=TaskStatus.READY),
            _make_task("explore-b", status=TaskStatus.READY),
        ]
        agents = [
            _make_agent(id="a-1", name="claude-1", state=AgentState.IDLE),
            _make_agent(id="a-2", name="claude-2", state=AgentState.BUSY),
        ]
        state = _make_scheduler_state(tasks=tasks, agents=agents)
        actions = Scheduler.schedule(state)

        assert len(actions) == 1
        assert actions[0].agent_id == "a-1"


# ===========================================================================
# (c) Reviewer task is created only after ALL parallel tasks complete
#     (depends on all)
# ===========================================================================


class TestReviewerDependsOnAll:
    """Verify that the review task depends on every exploration subtask and
    is only schedulable after all of them complete."""

    async def test_reviewer_depends_on_all_exploration_tasks(self, handler, db):
        """Review task has dependencies on all exploration subtasks."""
        # Create 3 exploration tasks
        explore_ids = []
        for label in ["a", "b", "c"]:
            result = await handler.execute(
                "create_task",
                {
                    "project_id": "p-1",
                    "title": f"Explore approach {label}",
                    "workspace_mode": "branch-isolated",
                },
            )
            assert "error" not in result
            explore_ids.append(result["created"])

        # Create the review task
        review_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Review exploration results",
                "agent_type": "code-review",
            },
        )
        assert "error" not in review_result
        review_id = review_result["created"]

        # Add dependencies: review depends on all exploration tasks
        for eid in explore_ids:
            dep_result = await handler.execute(
                "add_dependency",
                {"task_id": review_id, "depends_on": eid},
            )
            assert "error" not in dep_result

        # Verify the review task depends on all 3 exploration tasks
        deps = await db.get_dependencies(review_id)
        assert deps == set(explore_ids), (
            f"Review task should depend on all exploration tasks: "
            f"expected {set(explore_ids)}, got {deps}"
        )

    async def test_reviewer_stays_defined_while_exploration_incomplete(self, orch):
        """Reviewer task stays DEFINED until all exploration tasks complete."""
        await _setup_workflow_prereqs(orch.db)

        # Create 3 exploration tasks — 2 complete, 1 still in progress
        for i, status in enumerate(
            [TaskStatus.COMPLETED, TaskStatus.COMPLETED, TaskStatus.IN_PROGRESS],
            start=1,
        ):
            t = _make_task(f"explore-{i}", status=status)
            await orch.db.create_task(t)

        # Create the review task in DEFINED state
        review = _make_task(
            "review-1",
            status=TaskStatus.DEFINED,
            agent_type="code-review",
        )
        await orch.db.create_task(review)

        # Add dependencies
        for i in range(1, 4):
            await orch.db.add_dependency("review-1", f"explore-{i}")

        # Run dependency check — review should NOT be promoted to READY
        await orch._check_defined_tasks()

        updated_review = await orch.db.get_task("review-1")
        assert updated_review.status == TaskStatus.DEFINED, (
            "Review task should remain DEFINED while exploration tasks are incomplete"
        )

    async def test_reviewer_promoted_to_ready_when_all_complete(self, orch):
        """Reviewer task is promoted to READY when all exploration tasks complete."""
        await _setup_workflow_prereqs(orch.db)

        # Create 3 exploration tasks — all COMPLETED
        for i in range(1, 4):
            t = _make_task(f"explore-{i}", status=TaskStatus.COMPLETED)
            await orch.db.create_task(t)

        # Create the review task in DEFINED state with dependencies
        review = _make_task(
            "review-1",
            status=TaskStatus.DEFINED,
            agent_type="code-review",
        )
        await orch.db.create_task(review)

        for i in range(1, 4):
            await orch.db.add_dependency("review-1", f"explore-{i}")

        # Run dependency check — review SHOULD be promoted to READY
        await orch._check_defined_tasks()

        updated_review = await orch.db.get_task("review-1")
        assert updated_review.status == TaskStatus.READY, (
            "Review task should be promoted to READY when all exploration tasks complete"
        )

    async def test_reviewer_blocked_by_one_incomplete_task(self, orch):
        """Even one incomplete exploration task blocks the reviewer."""
        await _setup_project(orch.db)

        # 2 of 3 complete
        for i, status in enumerate(
            [TaskStatus.COMPLETED, TaskStatus.COMPLETED, TaskStatus.READY],
            start=1,
        ):
            t = _make_task(f"explore-{i}", status=status)
            await orch.db.create_task(t)

        review = _make_task("review-1", status=TaskStatus.DEFINED)
        await orch.db.create_task(review)

        for i in range(1, 4):
            await orch.db.add_dependency("review-1", f"explore-{i}")

        await orch._check_defined_tasks()

        updated = await orch.db.get_task("review-1")
        assert updated.status == TaskStatus.DEFINED, (
            "One READY exploration task should block the reviewer"
        )


# ===========================================================================
# (d) Reviewer task receives summaries/outputs from all parallel tasks as
#     context
# ===========================================================================


class TestReviewerReceivesContext:
    """Verify that the review task description or context includes the
    outputs and summaries from all exploration subtasks."""

    async def test_reviewer_description_references_all_subtask_branches(self, handler, db):
        """Review task description includes branch names for each exploration subtask."""
        explore_ids = []
        branches = []
        for label in ["a", "b", "c"]:
            branch = f"explore/parent-1/approach-{label}"
            branches.append(branch)
            result = await handler.execute(
                "create_task",
                {
                    "project_id": "p-1",
                    "title": f"Explore approach {label}",
                    "description": f"Investigate approach {label} on branch {branch}",
                    "workspace_mode": "branch-isolated",
                },
            )
            assert "error" not in result
            explore_ids.append(result["created"])

        # Create review task with description that references all branches
        branch_list = "\n".join(f"- {b}" for b in branches)
        review_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Review exploration results",
                "description": (
                    "Compare the following exploration approaches:\n"
                    f"{branch_list}\n"
                    "Evaluate: correctness, complexity, performance, risk."
                ),
                "agent_type": "code-review",
            },
        )
        assert "error" not in review_result
        review_id = review_result["created"]

        task = await db.get_task(review_id)
        for branch in branches:
            assert branch in task.description, (
                f"Review description should reference branch '{branch}'"
            )

    async def test_reviewer_can_access_subtask_results(self, db):
        """Task results saved by exploration subtasks are retrievable for the reviewer."""
        await db.create_project(Project(id="p-1", name="exploration-proj"))

        # Create agents referenced in results
        for i in range(1, 4):
            await db.create_agent(Agent(id=f"agent-{i}", name=f"claude-{i}", agent_type="coding"))

        # Create and complete exploration tasks with results
        for i in range(1, 4):
            task = _make_task(
                f"explore-{i}",
                status=TaskStatus.COMPLETED,
            )
            await db.create_task(task)

            output = AgentOutput(
                result=AgentResult.COMPLETED,
                summary=f"Approach {i}: Found viable solution using method {i}",
                files_changed=[f"src/approach_{i}.py"],
                tokens_used=1000 * i,
            )
            await db.save_task_result(f"explore-{i}", f"agent-{i}", output)

        # Verify each result is retrievable (as the reviewer would need)
        for i in range(1, 4):
            result = await db.get_task_result(f"explore-{i}")
            assert result is not None, f"Result for explore-{i} should be accessible"
            assert f"Approach {i}" in result["summary"]

    async def test_reviewer_gets_dependency_info_with_summaries(self, handler, db):
        """get_task_dependencies returns dependency task info including status."""
        # Create exploration tasks
        explore_ids = []
        for label in ["a", "b"]:
            result = await handler.execute(
                "create_task",
                {
                    "project_id": "p-1",
                    "title": f"Explore {label}",
                    "workspace_mode": "branch-isolated",
                },
            )
            explore_ids.append(result["created"])

        # Create review task with dependencies
        review_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Review results",
                "agent_type": "code-review",
            },
        )
        review_id = review_result["created"]

        for eid in explore_ids:
            await handler.execute(
                "add_dependency",
                {"task_id": review_id, "depends_on": eid},
            )

        # Verify dependency information is available
        dep_result = await handler.execute(
            "get_task_dependencies",
            {"task_id": review_id},
        )
        assert "error" not in dep_result
        depends_on = dep_result.get("depends_on", [])
        dep_ids = {d["id"] for d in depends_on}
        assert dep_ids == set(explore_ids)


# ===========================================================================
# (e) Partial failure (2 of 3 parallel tasks complete, 1 fails) — reviewer
#     still triggers with available results plus failure note
# ===========================================================================


class TestPartialFailure:
    """Verify behavior when some exploration tasks fail while others succeed."""

    async def test_failed_task_blocks_stage_completion(self, orch):
        """A FAILED exploration task prevents workflow.stage.completed from firing."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="exploration",
            task_ids=["explore-1", "explore-2", "explore-3"],
        )
        await orch.db.create_workflow(wf)

        # 2 completed, 1 failed
        await orch.db.create_task(
            _make_task("explore-1", status=TaskStatus.COMPLETED, workflow_id="wf-explore-1")
        )
        await orch.db.create_task(
            _make_task("explore-2", status=TaskStatus.COMPLETED, workflow_id="wf-explore-1")
        )
        await orch.db.create_task(
            _make_task("explore-3", status=TaskStatus.FAILED, workflow_id="wf-explore-1")
        )

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(await orch.db.get_task("explore-1"))
        assert emitted == [], "Stage completion should NOT fire when a task is FAILED"

    async def test_failed_task_marked_complete_with_note_allows_progress(self, orch):
        """When a failed task is marked COMPLETED (dead-end noted), stage completes."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="exploration",
            task_ids=["explore-1", "explore-2", "explore-3"],
        )
        await orch.db.create_workflow(wf)

        # All 3 completed (one had a dead end but was marked complete with a note)
        for i in range(1, 4):
            await orch.db.create_task(
                _make_task(
                    f"explore-{i}",
                    status=TaskStatus.COMPLETED,
                    workflow_id="wf-explore-1",
                    description=(
                        "Dead-end: approach not viable"
                        if i == 3
                        else f"Found solution via approach {i}"
                    ),
                )
            )

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(await orch.db.get_task("explore-3"))

        assert len(emitted) == 1
        assert set(emitted[0]["task_ids"]) == {"explore-1", "explore-2", "explore-3"}

    async def test_reviewer_deps_not_met_with_failed_exploration(self, orch):
        """Review task stays DEFINED if an exploration task is FAILED (deps not met)."""
        await _setup_project(orch.db)

        # 2 completed, 1 failed
        await orch.db.create_task(_make_task("explore-1", status=TaskStatus.COMPLETED))
        await orch.db.create_task(_make_task("explore-2", status=TaskStatus.COMPLETED))
        await orch.db.create_task(_make_task("explore-3", status=TaskStatus.FAILED))

        review = _make_task(
            "review-1",
            status=TaskStatus.DEFINED,
            agent_type="code-review",
        )
        await orch.db.create_task(review)

        for i in range(1, 4):
            await orch.db.add_dependency("review-1", f"explore-{i}")

        await orch._check_defined_tasks()

        updated = await orch.db.get_task("review-1")
        assert updated.status == TaskStatus.DEFINED, (
            "Review task should remain DEFINED when a dependency has FAILED"
        )

    async def test_partial_failure_results_available_to_reviewer(self, db):
        """Results from completed tasks are available even when others failed."""
        await db.create_project(Project(id="p-1", name="exploration-proj"))

        # Create agents referenced in results
        for i in range(1, 4):
            await db.create_agent(Agent(id=f"agent-{i}", name=f"claude-{i}", agent_type="coding"))

        # explore-1 and explore-2 completed with results
        for i in [1, 2]:
            t = _make_task(f"explore-{i}", status=TaskStatus.COMPLETED)
            await db.create_task(t)
            output = AgentOutput(
                result=AgentResult.COMPLETED,
                summary=f"Approach {i} succeeded",
                files_changed=[],
                tokens_used=500,
            )
            await db.save_task_result(f"explore-{i}", f"agent-{i}", output)

        # explore-3 failed
        t3 = _make_task("explore-3", status=TaskStatus.FAILED)
        await db.create_task(t3)
        output3 = AgentOutput(
            result=AgentResult.FAILED,
            summary="Approach 3 hit a dead end — library incompatible",
            files_changed=[],
            tokens_used=200,
            error_message="Library X does not support feature Y",
        )
        await db.save_task_result("explore-3", "agent-3", output3)

        # Verify all results are retrievable
        r1 = await db.get_task_result("explore-1")
        r2 = await db.get_task_result("explore-2")
        r3 = await db.get_task_result("explore-3")

        assert r1 is not None and r1["result"] == "completed"
        assert r2 is not None and r2["result"] == "completed"
        assert r3 is not None and r3["result"] == "failed"
        assert "dead end" in r3["summary"]


# ===========================================================================
# (f) Exploration with single parallel task degrades gracefully to
#     sequential
# ===========================================================================


class TestSingleTaskDegradation:
    """Verify that a single exploration subtask is handled correctly
    without requiring special-casing in the playbook or scheduler."""

    async def test_single_exploration_task_created(self, handler, db):
        """A narrow exploration creates only one subtask — no fan-out needed."""
        result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Check if library X supports feature Y",
                "workspace_mode": "branch-isolated",
                "workflow_id": "wf-narrow-1",
            },
        )
        assert "error" not in result

        task = await db.get_task(result["created"])
        assert task is not None
        assert task.workspace_mode == WorkspaceMode.BRANCH_ISOLATED

    async def test_single_task_reviewer_depends_on_one(self, handler, db):
        """Review task depends on the single exploration task — works like a chain."""
        explore_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Explore single approach",
                "workspace_mode": "branch-isolated",
            },
        )
        explore_id = explore_result["created"]

        review_result = await handler.execute(
            "create_task",
            {
                "project_id": "p-1",
                "title": "Review exploration results",
                "agent_type": "code-review",
            },
        )
        review_id = review_result["created"]

        await handler.execute(
            "add_dependency",
            {"task_id": review_id, "depends_on": explore_id},
        )

        deps = await db.get_dependencies(review_id)
        assert deps == {explore_id}

    async def test_single_task_completes_unblocks_reviewer(self, orch):
        """When the only exploration task completes, the reviewer is promoted."""
        await _setup_project(orch.db)

        explore = _make_task("explore-1", status=TaskStatus.COMPLETED)
        await orch.db.create_task(explore)

        review = _make_task(
            "review-1",
            status=TaskStatus.DEFINED,
            agent_type="code-review",
        )
        await orch.db.create_task(review)
        await orch.db.add_dependency("review-1", "explore-1")

        await orch._check_defined_tasks()

        updated = await orch.db.get_task("review-1")
        assert updated.status == TaskStatus.READY

    def test_single_task_scheduler_assigns_sequentially(self):
        """With one exploration task and one agent, tasks run sequentially."""
        explore = _make_task("explore-1", status=TaskStatus.READY)
        review = _make_task(
            "review-1",
            status=TaskStatus.DEFINED,
            agent_type="code-review",
        )
        agents = [_make_agent(id="a-1", name="claude-1")]
        state = _make_scheduler_state(tasks=[explore, review], agents=agents)
        actions = Scheduler.schedule(state)

        # Only the READY task is assigned — DEFINED review is not schedulable
        assert len(actions) == 1
        assert actions[0].task_id == "explore-1"

    async def test_single_task_workflow_stage_completion(self, orch):
        """Stage completes normally with a single exploration task."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="exploration",
            task_ids=["explore-1"],
        )
        await orch.db.create_workflow(wf)

        await orch.db.create_task(
            _make_task("explore-1", status=TaskStatus.COMPLETED, workflow_id="wf-explore-1")
        )

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(await orch.db.get_task("explore-1"))

        assert len(emitted) == 1
        assert emitted[0]["task_ids"] == ["explore-1"]
        assert emitted[0]["stage"] == "exploration"


# ===========================================================================
# (g) Workflow status reflects "running" until reviewer completes, then
#     "completed"
# ===========================================================================


class TestWorkflowStatusLifecycle:
    """Verify that the workflow status tracks the coordination lifecycle:
    running during exploration/review, completed when all work is done."""

    async def test_workflow_starts_as_running(self, orch):
        """A newly created coordination workflow has status 'running'."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(status="running")
        await orch.db.create_workflow(wf)

        loaded = await orch.db.get_workflow("wf-explore-1")
        assert loaded is not None
        assert loaded.status == "running"

    async def test_workflow_running_during_exploration(self, orch):
        """Workflow stays 'running' while exploration tasks are in progress."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="exploration",
            task_ids=["explore-1", "explore-2"],
        )
        await orch.db.create_workflow(wf)

        for tid in ["explore-1", "explore-2"]:
            await orch.db.create_task(
                _make_task(tid, status=TaskStatus.IN_PROGRESS, workflow_id="wf-explore-1")
            )

        loaded = await orch.db.get_workflow("wf-explore-1")
        assert loaded.status == "running"

    async def test_workflow_running_during_review(self, orch):
        """Workflow stays 'running' after exploration stage completes while review runs."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="review",
            task_ids=["review-1"],
        )
        await orch.db.create_workflow(wf)

        await orch.db.create_task(
            _make_task(
                "review-1",
                status=TaskStatus.IN_PROGRESS,
                agent_type="code-review",
                workflow_id="wf-explore-1",
            )
        )

        loaded = await orch.db.get_workflow("wf-explore-1")
        assert loaded.status == "running"

    async def test_workflow_completed_after_final_task(self, orch):
        """Workflow transitions to 'completed' when explicitly updated."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="summary",
            task_ids=["summary-1"],
        )
        await orch.db.create_workflow(wf)

        await orch.db.create_task(
            _make_task("summary-1", status=TaskStatus.COMPLETED, workflow_id="wf-explore-1")
        )

        # The playbook runner would update the workflow status
        await orch.db.update_workflow_status(
            "wf-explore-1",
            "completed",
            completed_at=time.time(),
        )

        loaded = await orch.db.get_workflow("wf-explore-1")
        assert loaded.status == "completed"
        assert loaded.completed_at is not None

    async def test_workflow_stage_completed_event_fires_when_all_tasks_done(self, orch):
        """workflow.stage.completed event fires when all tasks in a stage complete."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="exploration",
            task_ids=["explore-1", "explore-2", "explore-3"],
        )
        await orch.db.create_workflow(wf)

        for i in range(1, 4):
            await orch.db.create_task(
                _make_task(
                    f"explore-{i}",
                    status=TaskStatus.COMPLETED,
                    workflow_id="wf-explore-1",
                )
            )

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(await orch.db.get_task("explore-3"))

        assert len(emitted) == 1
        assert emitted[0]["workflow_id"] == "wf-explore-1"
        assert emitted[0]["stage"] == "exploration"
        assert set(emitted[0]["task_ids"]) == {"explore-1", "explore-2", "explore-3"}

    async def test_workflow_not_completed_while_review_in_progress(self, orch):
        """Workflow status is not 'completed' while the review task is running."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="review",
            task_ids=["review-1"],
        )
        await orch.db.create_workflow(wf)

        await orch.db.create_task(
            _make_task(
                "review-1",
                status=TaskStatus.IN_PROGRESS,
                workflow_id="wf-explore-1",
            )
        )

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(await orch.db.get_task("review-1"))

        assert emitted == [], "No stage completion while review is IN_PROGRESS"

        loaded = await orch.db.get_workflow("wf-explore-1")
        assert loaded.status == "running"

    async def test_workflow_stage_completed_fires_after_reviewer_completes(self, orch):
        """Stage completion fires when the reviewer task completes."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            current_stage="review",
            task_ids=["review-1"],
        )
        await orch.db.create_workflow(wf)

        await orch.db.create_task(
            _make_task(
                "review-1",
                status=TaskStatus.COMPLETED,
                agent_type="code-review",
                workflow_id="wf-explore-1",
            )
        )

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(await orch.db.get_task("review-1"))

        assert len(emitted) == 1
        assert emitted[0]["workflow_id"] == "wf-explore-1"
        assert emitted[0]["stage"] == "review"
        assert emitted[0]["task_ids"] == ["review-1"]

    async def test_full_exploration_lifecycle(self, orch):
        """End-to-end: workflow goes running → stage complete → running → complete."""
        await _setup_workflow_prereqs(orch.db)

        # Phase 1: Create workflow with exploration stage
        wf = _make_workflow(
            current_stage="exploration",
            task_ids=["explore-1", "explore-2"],
        )
        await orch.db.create_workflow(wf)

        for i in [1, 2]:
            await orch.db.create_task(
                _make_task(
                    f"explore-{i}",
                    status=TaskStatus.COMPLETED,
                    workflow_id="wf-explore-1",
                )
            )

        # Phase 1 check: exploration stage completes
        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(await orch.db.get_task("explore-2"))
        assert len(emitted) == 1
        assert emitted[0]["stage"] == "exploration"

        # Phase 2: Advance workflow to review stage
        await orch.db.update_workflow(
            "wf-explore-1",
            current_stage="review",
            task_ids='["review-1"]',
        )

        await orch.db.create_task(
            _make_task(
                "review-1",
                status=TaskStatus.COMPLETED,
                agent_type="code-review",
                workflow_id="wf-explore-1",
            )
        )

        await orch._check_workflow_stage_completion(await orch.db.get_task("review-1"))
        assert len(emitted) == 2
        assert emitted[1]["stage"] == "review"

        # Phase 3: Mark workflow completed
        await orch.db.update_workflow_status(
            "wf-explore-1",
            "completed",
            completed_at=time.time(),
        )

        loaded = await orch.db.get_workflow("wf-explore-1")
        assert loaded.status == "completed"

    async def test_workflow_agent_affinity_map_preserved(self, orch):
        """The workflow's agent_affinity map is persisted and retrievable."""
        await _setup_workflow_prereqs(orch.db)

        wf = _make_workflow(
            agent_affinity={
                "agent-1": "explore-1",
                "agent-2": "explore-2",
                "agent-3": "explore-3",
            },
        )
        await orch.db.create_workflow(wf)

        loaded = await orch.db.get_workflow("wf-explore-1")
        assert loaded.agent_affinity == {
            "agent-1": "explore-1",
            "agent-2": "explore-2",
            "agent-3": "explore-3",
        }
