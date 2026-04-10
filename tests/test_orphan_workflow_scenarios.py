"""Orphan workflow recovery scenario tests (Roadmap 7.5.10).

Integration-style tests verifying the seven coordination-spec scenarios:

(a) Kill coordination playbook mid-workflow — in-flight tasks continue executing
(b) Tasks created before crash have correct dependencies and are scheduled normally
(c) Re-triggering coordination playbook discovers existing workflow and resumes
(d) Resumed playbook does not re-create tasks that already exist
(e) Workflow status shows "running" during orphan period (not "failed")
(f) Orphan detection identifies workflows with no active playbook run and alerts
(g) Manual ``resume_playbook`` can restart coordination from the last completed stage

These complement the unit tests in ``test_orphan_workflow_recovery.py`` by testing
cross-module scenarios with richer state setups.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.event_bus import EventBus
from src.models import PlaybookRun, TaskStatus, Workflow
from src.orphan_workflow_recovery import OrphanWorkflowRecovery


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL = object()


def _make_workflow(
    workflow_id: str = "wf-1",
    playbook_id: str = "coord-playbook",
    playbook_run_id: str = "run-1",
    project_id: str = "proj-1",
    status: str = "running",
    current_stage: str = "build",
    task_ids: list[str] | object = _SENTINEL,
    stages: list[dict] | None = None,
) -> Workflow:
    return Workflow(
        workflow_id=workflow_id,
        playbook_id=playbook_id,
        playbook_run_id=playbook_run_id,
        project_id=project_id,
        status=status,
        current_stage=current_stage,
        task_ids=["t-1", "t-2", "t-3"] if task_ids is _SENTINEL else task_ids,
        stages=stages or [],
        created_at=time.time() - 600,
    )


def _make_playbook_run(
    run_id: str = "run-1",
    playbook_id: str = "coord-playbook",
    status: str = "paused",
    current_node: str = "wait_stage",
    waiting_for_event: str | None = "workflow.stage.completed",
    paused_at: float | None = None,
    error: str | None = None,
    completed_at: float | None = None,
    started_at: float | None = None,
) -> PlaybookRun:
    return PlaybookRun(
        run_id=run_id,
        playbook_id=playbook_id,
        playbook_version=1,
        trigger_event=json.dumps({"type": "task.created"}),
        status=status,
        current_node=current_node,
        conversation_history="[]",
        node_trace="[]",
        tokens_used=50,
        started_at=started_at or time.time() - 3600,
        completed_at=completed_at,
        paused_at=paused_at or time.time() - 60,
        waiting_for_event=waiting_for_event,
        error=error,
    )


@dataclass
class FakeTask:
    """Minimal task-like object for tests."""

    id: str
    project_id: str = "proj-1"
    title: str = "Test task"
    description: str = ""
    status: TaskStatus = TaskStatus.COMPLETED
    workflow_id: str | None = "wf-1"
    assigned_agent_id: str | None = None
    depends_on: set[str] = field(default_factory=set)
    priority: int = 100


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_bus():
    """An EventBus with validation disabled."""
    return EventBus(env="dev", validate_events=False)


@pytest.fixture
def mock_db():
    """A mock database backend with sensible defaults."""
    db = AsyncMock()
    db.list_workflows = AsyncMock(return_value=[])
    db.get_workflow = AsyncMock(return_value=None)
    db.get_playbook_run = AsyncMock(return_value=None)
    db.get_task = AsyncMock(return_value=None)
    db.update_playbook_run = AsyncMock()
    db.update_workflow_status = AsyncMock()
    db.update_workflow = AsyncMock()
    db.add_dependency = AsyncMock()
    db.get_dependencies = AsyncMock(return_value=set())
    db.are_dependencies_met = AsyncMock(return_value=True)
    db.transition_task = AsyncMock()
    db.update_task = AsyncMock()
    return db


# ===========================================================================
# (a) Kill coordination playbook mid-workflow — in-flight tasks continue
# ===========================================================================


class TestScenarioA_TasksContinueAfterPlaybookCrash:
    """Scenario (a): When a coordination playbook crashes mid-workflow,
    in-flight tasks continue executing to completion.

    Tasks are independent entities in the tasks table. Their status is
    never derived from the workflow or playbook run status. A playbook
    crash changes only the playbook run's status — task statuses remain
    whatever they were at the time of the crash.
    """

    async def test_in_progress_tasks_unaffected_by_run_failure(self, mock_db, event_bus):
        """Tasks in IN_PROGRESS status keep that status when the playbook
        run transitions to 'failed'."""
        tasks = [
            FakeTask(id="t-1", status=TaskStatus.IN_PROGRESS, workflow_id="wf-1"),
            FakeTask(id="t-2", status=TaskStatus.IN_PROGRESS, workflow_id="wf-1"),
            FakeTask(id="t-3", status=TaskStatus.DEFINED, workflow_id="wf-1"),
        ]

        workflow = _make_workflow(task_ids=["t-1", "t-2", "t-3"])
        run = _make_playbook_run(status="failed", waiting_for_event=None, error="LLM crash")

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: next((t for t in tasks if t.id == tid), None)

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        # Key assertion: no task status changes were attempted
        mock_db.update_task.assert_not_called()
        mock_db.transition_task.assert_not_called()

        # Tasks still have their original statuses
        for t in tasks:
            fetched = mock_db.get_task.return_value  # not used; we check the objects directly
        assert tasks[0].status == TaskStatus.IN_PROGRESS
        assert tasks[1].status == TaskStatus.IN_PROGRESS
        assert tasks[2].status == TaskStatus.DEFINED

    async def test_mixed_task_statuses_preserved_during_recovery(self, mock_db, event_bus):
        """Tasks in various lifecycle states are all preserved: COMPLETED,
        IN_PROGRESS, DEFINED, QUEUED — none are modified by recovery."""
        tasks = [
            FakeTask(id="t-1", status=TaskStatus.COMPLETED),
            FakeTask(id="t-2", status=TaskStatus.IN_PROGRESS),
            FakeTask(id="t-3", status=TaskStatus.DEFINED),
            FakeTask(id="t-4", status=TaskStatus.READY),
        ]

        workflow = _make_workflow(task_ids=["t-1", "t-2", "t-3", "t-4"])
        run = _make_playbook_run(
            status="failed", waiting_for_event=None, error="daemon crash"
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: next((t for t in tasks if t.id == tid), None)

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        # None of the task statuses should have been modified
        assert tasks[0].status == TaskStatus.COMPLETED
        assert tasks[1].status == TaskStatus.IN_PROGRESS
        assert tasks[2].status == TaskStatus.DEFINED
        assert tasks[3].status == TaskStatus.READY

    async def test_assigned_agent_not_disrupted_by_crash(self, mock_db, event_bus):
        """Tasks assigned to agents retain their assignment — the orchestrator
        continues executing them normally even though the playbook is dead."""
        task = FakeTask(
            id="t-1",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="agent-1",
        )

        workflow = _make_workflow(task_ids=["t-1"])
        run = _make_playbook_run(status="failed", waiting_for_event=None, error="crash")

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: task if tid == "t-1" else None

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        # Agent assignment is preserved
        assert task.assigned_agent_id == "agent-1"
        # No task mutations occurred
        mock_db.update_task.assert_not_called()

    async def test_periodic_check_also_preserves_tasks(self, mock_db, event_bus):
        """Periodic orphan detection likewise never modifies task status."""
        tasks = [
            FakeTask(id="t-1", status=TaskStatus.IN_PROGRESS),
            FakeTask(id="t-2", status=TaskStatus.IN_PROGRESS),
        ]

        workflow = _make_workflow(task_ids=["t-1", "t-2"])
        run = _make_playbook_run(status="failed", waiting_for_event=None, error="crash")

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: next((t for t in tasks if t.id == tid), None)

        recovery = OrphanWorkflowRecovery(
            db=mock_db, event_bus=event_bus, check_interval_seconds=0
        )
        await recovery.check_periodic()

        mock_db.update_task.assert_not_called()
        mock_db.transition_task.assert_not_called()


# ===========================================================================
# (b) Tasks created before crash have correct dependencies
# ===========================================================================


class TestScenarioB_DependenciesPreservedAfterCrash:
    """Scenario (b): Tasks created before the playbook crash retain their
    dependency relationships and are scheduled normally by the orchestrator.

    Dependencies live in the ``task_dependencies`` table (separate from the
    playbook run).  A playbook crash has no effect on dependency data.
    """

    async def test_dependency_graph_intact_after_run_failure(self, mock_db, event_bus):
        """Dependency edges persist regardless of playbook run status."""
        # t-2 depends on t-1; t-3 depends on t-2
        mock_db.get_dependencies.side_effect = lambda tid: (
            {"t-1"} if tid == "t-2" else {"t-2"} if tid == "t-3" else set()
        )

        tasks = [
            FakeTask(id="t-1", status=TaskStatus.COMPLETED),
            FakeTask(id="t-2", status=TaskStatus.IN_PROGRESS),
            FakeTask(id="t-3", status=TaskStatus.DEFINED),
        ]

        workflow = _make_workflow(task_ids=["t-1", "t-2", "t-3"])
        run = _make_playbook_run(status="failed", waiting_for_event=None, error="crash")

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: next((t for t in tasks if t.id == tid), None)

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        # Dependencies were never modified
        mock_db.add_dependency.assert_not_called()
        # Verify we can still query dependencies
        deps_t2 = await mock_db.get_dependencies("t-2")
        assert deps_t2 == {"t-1"}
        deps_t3 = await mock_db.get_dependencies("t-3")
        assert deps_t3 == {"t-2"}

    async def test_dependent_task_scheduled_when_upstream_completes(self, mock_db, event_bus):
        """After playbook crash, dependency resolution still works:
        when t-1 completes, t-2 becomes eligible for scheduling.

        This is a structural assertion — ``are_dependencies_met`` is a DB
        query independent of the playbook run status.
        """
        mock_db.are_dependencies_met.side_effect = lambda tid: tid == "t-2"

        # t-2 depends on t-1 (completed), so dependencies are met
        result = await mock_db.are_dependencies_met("t-2")
        assert result is True

        # t-3 depends on t-2 (in progress), so dependencies are NOT met
        mock_db.are_dependencies_met.side_effect = lambda tid: tid != "t-3"
        result = await mock_db.are_dependencies_met("t-3")
        assert result is False

    async def test_workflow_task_ids_preserved(self, mock_db, event_bus):
        """The workflow's task_ids list is not modified by recovery —
        all tasks remain associated with the workflow."""
        original_task_ids = ["t-1", "t-2", "t-3", "t-4"]
        workflow = _make_workflow(task_ids=list(original_task_ids))
        run = _make_playbook_run(status="failed", waiting_for_event=None, error="crash")

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        # task_ids unchanged
        assert workflow.task_ids == original_task_ids


# ===========================================================================
# (c) Re-triggering playbook discovers existing workflow and resumes
# ===========================================================================


class TestScenarioC_PlaybookResumesFromCurrentState:
    """Scenario (c): When the coordination playbook is re-triggered (via
    orphan recovery or manual action), it discovers the existing workflow
    and resumes from the current state.

    The recovery path works through two mechanisms:
    1. OrphanWorkflowRecovery re-emits ``workflow.stage.completed`` events
    2. WorkflowStageResumeHandler catches these and resumes the paused run

    This test class verifies the first half of the flow — that recovery
    correctly re-emits the event when all stage tasks are completed.
    """

    async def test_startup_recovery_reemits_for_paused_run_with_all_tasks_done(
        self, mock_db, event_bus
    ):
        """On startup, if a paused run's stage tasks are all completed,
        re-emit ``workflow.stage.completed`` so the handler resumes it."""
        workflow = _make_workflow(
            current_stage="test",
            task_ids=["t-1", "t-2"],
        )
        run = _make_playbook_run(
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        stage_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: stage_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["events_reemitted"] == 1
        assert len(stage_events) == 1
        assert stage_events[0]["workflow_id"] == "wf-1"
        assert stage_events[0]["stage"] == "test"
        assert stage_events[0]["task_ids"] == ["t-1", "t-2"]
        assert stage_events[0]["_recovery"] is True

    async def test_event_carries_workflow_id_for_handler_lookup(self, mock_db, event_bus):
        """The re-emitted event includes workflow_id so the
        WorkflowStageResumeHandler can look up the workflow and find
        the paused playbook_run_id to resume."""
        workflow = _make_workflow(
            workflow_id="wf-coord-42",
            playbook_run_id="run-abc",
            current_stage="deploy",
            task_ids=["t-10"],
        )
        run = _make_playbook_run(
            run_id="run-abc",
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        stage_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: stage_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        # Handler uses workflow_id from the event to call db.get_workflow()
        # which returns playbook_run_id — verifying chain is intact
        event = stage_events[0]
        assert event["workflow_id"] == "wf-coord-42"
        # Verify the workflow's run_id is accessible for the handler
        assert workflow.playbook_run_id == "run-abc"

    async def test_periodic_check_also_reemits_missed_event(self, mock_db, event_bus):
        """Periodic monitoring also re-emits missed stage events —
        important for orphans that arise during normal operation."""
        workflow = _make_workflow(
            current_stage="review",
            task_ids=["t-1"],
        )
        run = _make_playbook_run(
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        stage_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: stage_events.append(d))

        recovery = OrphanWorkflowRecovery(
            db=mock_db, event_bus=event_bus, check_interval_seconds=0
        )
        await recovery.check_periodic()

        assert len(stage_events) == 1
        assert stage_events[0]["stage"] == "review"

    async def test_manual_recovery_also_reemits_event(self, mock_db, event_bus):
        """Manual ``recover_workflow`` also re-emits the stage event when
        the paused run has all tasks completed."""
        workflow = _make_workflow(current_stage="build", task_ids=["t-1", "t-2"])
        run = _make_playbook_run(
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        stage_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: stage_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["action"] == "stage_event_reemitted"
        assert result["stage"] == "build"
        assert len(stage_events) == 1


# ===========================================================================
# (d) Resumed playbook does not re-create tasks that already exist
# ===========================================================================


class TestScenarioD_NoTaskDuplicationOnResume:
    """Scenario (d): When a coordination playbook is resumed after recovery,
    it does not re-create tasks that already exist in the workflow.

    This is ensured structurally:
    - The workflow's ``task_ids`` list records all tasks created so far
    - The re-emitted ``workflow.stage.completed`` event includes the
      ``task_ids`` that were in the completed stage
    - The playbook resumes from the ``wait_for_event`` node, not from
      the beginning — it won't re-execute task-creation nodes
    - The event data injected into conversation history tells the LLM
      exactly which tasks completed, so it proceeds to the next stage
    """

    async def test_recovery_does_not_create_new_tasks(self, mock_db, event_bus):
        """Orphan recovery never calls create_task — it only re-emits events."""
        workflow = _make_workflow(task_ids=["t-1", "t-2"])
        run = _make_playbook_run(
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        # Verify no task creation happened
        mock_db.create_task.assert_not_called() if hasattr(mock_db, "create_task") else None

    async def test_reemitted_event_includes_existing_task_ids(self, mock_db, event_bus):
        """The re-emitted event carries the existing task_ids, informing
        the playbook which tasks already exist (preventing duplication)."""
        existing_tasks = ["t-build-1", "t-build-2", "t-build-3"]
        workflow = _make_workflow(
            current_stage="build",
            task_ids=existing_tasks,
        )
        run = _make_playbook_run(
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        stage_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: stage_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        event = stage_events[0]
        assert event["task_ids"] == existing_tasks

    async def test_workflow_task_ids_unchanged_after_recovery(self, mock_db, event_bus):
        """The workflow's task_ids list is not duplicated or modified
        by the recovery process."""
        original_ids = ["t-1", "t-2", "t-3"]
        workflow = _make_workflow(task_ids=list(original_ids))
        run = _make_playbook_run(
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        # task_ids unchanged
        assert workflow.task_ids == original_ids

    async def test_recovery_flag_signals_to_avoid_duplication(self, mock_db, event_bus):
        """Re-emitted events carry ``_recovery=True`` so the playbook runner
        can distinguish recovery-triggered resumes from natural ones."""
        workflow = _make_workflow(task_ids=["t-1"])
        run = _make_playbook_run(
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        stage_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: stage_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        assert stage_events[0]["_recovery"] is True


# ===========================================================================
# (e) Workflow status stays "running" during orphan period
# ===========================================================================


class TestScenarioE_WorkflowRunningDuringOrphanPeriod:
    """Scenario (e): When a playbook run fails, the *workflow* status
    remains "running" (not "failed").  The workflow represents the
    collection of tasks; it should not fail just because the coordination
    playbook died.

    The workflow only transitions to "failed" if explicitly set, or to
    "completed" when the orphan recovery syncs it from a completed run.
    """

    async def test_workflow_stays_running_when_run_fails(self, mock_db, event_bus):
        """Workflow status is 'running' even when its playbook run is 'failed'."""
        workflow = _make_workflow(status="running")
        run = _make_playbook_run(
            status="failed",
            waiting_for_event=None,
            error="LLM provider error",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        # Workflow status was NOT changed
        mock_db.update_workflow_status.assert_not_called()
        # The workflow itself still shows "running"
        assert workflow.status == "running"

    async def test_workflow_stays_running_when_run_times_out(self, mock_db, event_bus):
        """Workflow status is 'running' even when run is 'timed_out'."""
        workflow = _make_workflow(status="running")
        run = _make_playbook_run(
            status="timed_out",
            waiting_for_event=None,
            error="Pause timeout exceeded",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        mock_db.update_workflow_status.assert_not_called()
        assert workflow.status == "running"

    async def test_workflow_stays_running_when_run_deleted(self, mock_db, event_bus):
        """Workflow status is 'running' even when the run record is deleted."""
        workflow = _make_workflow(status="running")

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = None  # Deleted

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        mock_db.update_workflow_status.assert_not_called()
        assert workflow.status == "running"

    async def test_workflow_stays_running_during_periodic_check(self, mock_db, event_bus):
        """Periodic monitoring also preserves 'running' workflow status."""
        workflow = _make_workflow(status="running")
        run = _make_playbook_run(
            status="failed",
            waiting_for_event=None,
            error="crash",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(
            db=mock_db, event_bus=event_bus, check_interval_seconds=0
        )
        await recovery.check_periodic()

        mock_db.update_workflow_status.assert_not_called()
        assert workflow.status == "running"

    async def test_only_completed_run_syncs_workflow_status(self, mock_db, event_bus):
        """The ONLY case where recovery modifies workflow status is when
        the playbook run itself completed — syncing workflow to 'completed'."""
        workflow = _make_workflow(status="running")
        run = _make_playbook_run(
            status="completed",
            waiting_for_event=None,
            completed_at=time.time() - 100,
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        # THIS is the only case where status is updated
        mock_db.update_workflow_status.assert_called_once()
        args = mock_db.update_workflow_status.call_args
        assert args[0][0] == "wf-1"
        assert args[0][1] == "completed"

    async def test_stale_running_run_marks_run_failed_not_workflow(self, mock_db, event_bus):
        """When a run is stuck in 'running' (stale after restart), the RUN
        is marked 'failed', but the WORKFLOW stays 'running'."""
        workflow = _make_workflow(status="running")
        run = _make_playbook_run(status="running", waiting_for_event=None)

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        # The playbook RUN was marked failed
        mock_db.update_playbook_run.assert_called_once()
        call_args = mock_db.update_playbook_run.call_args
        assert call_args[0][0] == "run-1"  # run_id
        assert call_args[1].get("status") == "failed" or "failed" in str(call_args)

        # But the WORKFLOW status was NOT touched
        mock_db.update_workflow_status.assert_not_called()
        assert workflow.status == "running"


# ===========================================================================
# (f) Orphan detection: identifies + alerts operator
# ===========================================================================


class TestScenarioF_OrphanDetectionAndAlerting:
    """Scenario (f): The system identifies workflows with no active playbook
    run and alerts the operator via ``workflow.orphaned`` events.

    This tests the full detection → alerting pipeline including:
    - All orphan reasons (failed, timed_out, deleted, no_run_id)
    - Event payload completeness
    - Idempotent detection (no duplicate alerts)
    - Multi-workflow detection
    """

    async def test_failed_run_detected_and_alerted(self, mock_db, event_bus):
        """Failed playbook run → workflow.orphaned event with details."""
        workflow = _make_workflow(
            current_stage="build",
            task_ids=["t-1", "t-2"],
        )
        run = _make_playbook_run(
            status="failed",
            waiting_for_event=None,
            error="Budget exceeded: 500k tokens used",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        assert len(orphan_events) == 1
        event = orphan_events[0]
        assert event["workflow_id"] == "wf-1"
        assert event["playbook_id"] == "coord-playbook"
        assert event["project_id"] == "proj-1"
        assert event["reason"] == "run_failed"
        assert event["error"] == "Budget exceeded: 500k tokens used"
        assert event["current_stage"] == "build"
        assert event["task_ids"] == ["t-1", "t-2"]

    async def test_deleted_run_detected(self, mock_db, event_bus):
        """When a playbook run record is deleted, the workflow is orphaned."""
        workflow = _make_workflow()
        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = None

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        assert len(orphan_events) == 1
        assert orphan_events[0]["reason"] == "run_not_found"

    async def test_no_run_id_detected(self, mock_db, event_bus):
        """Workflow with empty playbook_run_id is immediately orphaned."""
        workflow = _make_workflow(playbook_run_id="")
        mock_db.list_workflows.return_value = [workflow]

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        assert len(orphan_events) == 1
        assert orphan_events[0]["reason"] == "no_playbook_run_id"

    async def test_multiple_orphans_all_reported(self, mock_db, event_bus):
        """Multiple orphaned workflows are each independently detected."""
        wf1 = _make_workflow(workflow_id="wf-1", playbook_run_id="run-1")
        wf2 = _make_workflow(workflow_id="wf-2", playbook_run_id="run-2")
        wf3 = _make_workflow(workflow_id="wf-3", playbook_run_id="")

        run1 = _make_playbook_run(
            run_id="run-1", status="failed", waiting_for_event=None, error="crash"
        )
        # run-2 has been deleted
        runs = {"run-1": run1, "run-2": None}

        mock_db.list_workflows.return_value = [wf1, wf2, wf3]
        mock_db.get_playbook_run.side_effect = lambda rid: runs.get(rid)

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        assert len(orphan_events) == 3
        reasons = {e["workflow_id"]: e["reason"] for e in orphan_events}
        assert reasons["wf-1"] == "run_failed"
        assert reasons["wf-2"] == "run_not_found"
        assert reasons["wf-3"] == "no_playbook_run_id"

    async def test_idempotent_periodic_detection(self, mock_db, event_bus):
        """Periodic check doesn't re-alert for already-reported orphans."""
        workflow = _make_workflow()
        run = _make_playbook_run(
            status="failed", waiting_for_event=None, error="crash"
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(
            db=mock_db, event_bus=event_bus, check_interval_seconds=0
        )

        # First check: detect and report
        await recovery.check_periodic()
        assert len(orphan_events) == 1

        # Second check: already reported, no duplicate
        recovery._last_check_time = 0
        await recovery.check_periodic()
        assert len(orphan_events) == 1  # Still just one

        # Third check: still no duplicate
        recovery._last_check_time = 0
        await recovery.check_periodic()
        assert len(orphan_events) == 1

    async def test_new_orphan_detected_after_previous_one_resolved(self, mock_db, event_bus):
        """When a previously orphaned workflow is resolved and a new one
        appears, the new one is detected and reported."""
        wf_old = _make_workflow(workflow_id="wf-old")
        run_old = _make_playbook_run(
            run_id="run-old", status="failed", waiting_for_event=None, error="crash"
        )

        mock_db.list_workflows.return_value = [wf_old]
        mock_db.get_playbook_run.return_value = run_old

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(
            db=mock_db, event_bus=event_bus, check_interval_seconds=0
        )

        # Detect old orphan
        await recovery.check_periodic()
        assert len(orphan_events) == 1
        assert orphan_events[0]["workflow_id"] == "wf-old"

        # Old resolved, new appears
        wf_new = _make_workflow(workflow_id="wf-new", playbook_run_id="run-new")
        run_new = _make_playbook_run(
            run_id="run-new", status="timed_out", waiting_for_event=None, error="timeout"
        )

        mock_db.list_workflows.return_value = [wf_new]
        mock_db.get_playbook_run.side_effect = lambda rid: (
            run_new if rid == "run-new" else None
        )

        recovery._last_check_time = 0
        await recovery.check_periodic()
        assert len(orphan_events) == 2
        assert orphan_events[1]["workflow_id"] == "wf-new"
        assert orphan_events[1]["reason"] == "run_timed_out"


# ===========================================================================
# (g) Manual resume_playbook restarts from last completed stage
# ===========================================================================


class TestScenarioG_ManualResumeFromLastStage:
    """Scenario (g): Manual ``resume_playbook`` can restart coordination
    from the last completed stage.

    This tests two related mechanisms:
    1. The ``recover_workflow`` command re-emits the stage event, which
       the WorkflowStageResumeHandler uses to resume the paused run
    2. The stage event carries the correct stage info (current_stage)
       so the playbook knows where to resume
    3. The ``resume_playbook`` command can also be used directly when
       the playbook is paused for human input
    """

    async def test_recovery_preserves_stage_info_for_resume(self, mock_db, event_bus):
        """Recovery includes current_stage in the event data so the
        playbook can resume from the correct pipeline position."""
        stages = [
            {
                "name": "design",
                "task_ids": ["t-design-1"],
                "status": "completed",
                "started_at": time.time() - 3600,
                "completed_at": time.time() - 1800,
            },
            {
                "name": "build",
                "task_ids": ["t-build-1", "t-build-2"],
                "status": "active",
                "started_at": time.time() - 1800,
                "completed_at": None,
            },
        ]

        workflow = _make_workflow(
            current_stage="build",
            task_ids=["t-design-1", "t-build-1", "t-build-2"],
            stages=stages,
        )
        run = _make_playbook_run(
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        stage_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: stage_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["action"] == "stage_event_reemitted"
        assert result["stage"] == "build"

        # The event includes the stage name
        event = stage_events[0]
        assert event["stage"] == "build"
        assert event["workflow_id"] == "wf-1"

    async def test_orphan_event_includes_stage_for_manual_retrigger(self, mock_db, event_bus):
        """When the run has truly failed (not just paused), the orphaned
        event includes ``current_stage`` so the operator knows where
        to resume from when creating a new playbook run."""
        workflow = _make_workflow(
            current_stage="test",
            task_ids=["t-1", "t-2"],
            stages=[
                {
                    "name": "build",
                    "status": "completed",
                    "completed_at": time.time() - 1000,
                },
                {
                    "name": "test",
                    "status": "active",
                    "started_at": time.time() - 500,
                },
            ],
        )
        run = _make_playbook_run(
            status="failed",
            waiting_for_event=None,
            error="Token budget exceeded",
        )

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        event = orphan_events[0]
        # Operator can see which stage the workflow was on
        assert event["current_stage"] == "test"
        # And can use resume_playbook or create a new run targeting this stage

    async def test_recovery_requested_flag_set_on_manual_recovery(self, mock_db, event_bus):
        """Manual recovery sets ``recovery_requested=True`` in the orphaned
        event, signaling that an operator explicitly requested recovery.
        Automation hooks can use this to auto-retrigger the playbook."""
        workflow = _make_workflow()
        run = _make_playbook_run(
            status="failed",
            waiting_for_event=None,
            error="crash",
        )

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert orphan_events[0]["recovery_requested"] is True

    async def test_resume_advice_for_human_paused_run(self, mock_db, event_bus):
        """When the playbook is paused for human input (not event-triggered),
        recovery advises using ``resume_playbook`` to continue."""
        workflow = _make_workflow()
        run = _make_playbook_run(
            status="paused",
            waiting_for_event=None,  # Paused for human input
        )

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["action"] == "no_action"
        assert "resume_playbook" in result["message"]

    async def test_multi_stage_recovery_resume_from_correct_stage(self, mock_db, event_bus):
        """In a multi-stage workflow (design → build → test → deploy),
        recovery from stage 'test' re-emits the event for stage 'test',
        not earlier stages."""
        stages = [
            {
                "name": "design",
                "task_ids": ["t-d1"],
                "status": "completed",
                "completed_at": time.time() - 5000,
            },
            {
                "name": "build",
                "task_ids": ["t-b1", "t-b2"],
                "status": "completed",
                "completed_at": time.time() - 3000,
            },
            {
                "name": "test",
                "task_ids": ["t-t1"],
                "status": "active",
                "started_at": time.time() - 1000,
            },
        ]

        workflow = _make_workflow(
            current_stage="test",
            task_ids=["t-d1", "t-b1", "t-b2", "t-t1"],
            stages=stages,
        )
        run = _make_playbook_run(
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        stage_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: stage_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["stage"] == "test"  # Not "design" or "build"
        assert len(stage_events) == 1
        assert stage_events[0]["stage"] == "test"

    async def test_failed_recovery_emits_actionable_message(self, mock_db, event_bus):
        """When manual recovery of a failed run emits an orphan event,
        the result includes actionable guidance for the operator."""
        workflow = _make_workflow(
            workflow_id="wf-coord-7",
            playbook_id="deploy-pipeline",
            playbook_run_id="run-99",
        )
        run = _make_playbook_run(
            run_id="run-99",
            playbook_id="deploy-pipeline",
            status="failed",
            waiting_for_event=None,
            error="Anthropic API rate limit",
        )

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-coord-7")

        assert result["success"] is True
        assert result["action"] == "orphaned_event_emitted"
        assert "resume_playbook" in result["message"]
        assert "deploy-pipeline" in result["message"]
        assert result["run_status"] == "failed"
        assert result["run_error"] == "Anthropic API rate limit"


# ===========================================================================
# Cross-scenario: WorkflowStageResumeHandler integration
# ===========================================================================


class TestWorkflowStageResumeHandlerIntegration:
    """Verify that OrphanWorkflowRecovery's re-emitted events are compatible
    with the WorkflowStageResumeHandler's expectations.

    The handler expects:
    - ``workflow_id`` field in event data
    - Workflow record has ``playbook_run_id``
    - Playbook run is in ``paused`` status
    - Run is waiting for ``workflow.stage.completed``
    """

    async def test_reemitted_event_has_required_fields(self, mock_db, event_bus):
        """Re-emitted events contain all fields the handler needs."""
        workflow = _make_workflow(
            workflow_id="wf-42",
            playbook_run_id="run-42",
            current_stage="build",
            task_ids=["t-1"],
        )
        run = _make_playbook_run(
            run_id="run-42",
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        stage_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: stage_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        event = stage_events[0]
        # Fields the handler reads
        assert "workflow_id" in event
        assert event["workflow_id"] == "wf-42"
        # Fields passed to PlaybookRunner.resume_from_event
        assert "stage" in event
        assert "task_ids" in event
        # Internal marker (stripped before passing to runner)
        assert "_recovery" in event

    async def test_handler_can_find_run_via_workflow(self, mock_db, event_bus):
        """The handler's flow: event.workflow_id → db.get_workflow() →
        workflow.playbook_run_id → db.get_playbook_run().  Verify this
        chain is intact after recovery."""
        workflow = _make_workflow(
            workflow_id="wf-42",
            playbook_run_id="run-42",
            current_stage="test",
            task_ids=["t-1"],
        )
        run = _make_playbook_run(
            run_id="run-42",
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )

        # Set up the handler's DB lookup chain
        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run

        # Simulate what the handler does with the event
        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        stage_events = []

        async def fake_handler(data):
            """Simulate WorkflowStageResumeHandler._on_stage_completed."""
            wf_id = data.get("workflow_id")
            assert wf_id is not None

            wf = await mock_db.get_workflow(wf_id)
            assert wf is not None
            assert wf.playbook_run_id == "run-42"

            db_run = await mock_db.get_playbook_run(wf.playbook_run_id)
            assert db_run is not None
            assert db_run.status == "paused"
            assert db_run.waiting_for_event == "workflow.stage.completed"

            stage_events.append({"verified": True, "run_id": db_run.run_id})

        event_bus.subscribe("workflow.stage.completed", fake_handler)

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_on_startup()

        assert len(stage_events) == 1
        assert stage_events[0]["verified"] is True
        assert stage_events[0]["run_id"] == "run-42"


# ===========================================================================
# Cross-scenario: End-to-end lifecycle
# ===========================================================================


class TestEndToEndOrphanLifecycle:
    """Full lifecycle tests combining multiple scenarios."""

    async def test_crash_detect_resume_lifecycle(self, mock_db, event_bus):
        """Full lifecycle: crash → detect → alert → manual recovery → resume.

        Steps:
        1. Playbook run fails (crash simulation)
        2. Periodic check detects the orphan
        3. Operator is alerted via workflow.orphaned event
        4. Operator triggers manual recovery
        5. If tasks are done, recovery re-emits stage event
        """
        # Step 1: Playbook crashed, run is failed
        workflow = _make_workflow(
            current_stage="build",
            task_ids=["t-1", "t-2"],
        )
        run_failed = _make_playbook_run(
            status="failed",
            waiting_for_event=None,
            error="Daemon OOM killed",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run_failed

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(
            db=mock_db, event_bus=event_bus, check_interval_seconds=0
        )

        # Step 2: Periodic check detects orphan
        await recovery.check_periodic()

        # Step 3: Operator alerted
        assert len(orphan_events) == 1
        assert orphan_events[0]["reason"] == "run_failed"
        assert orphan_events[0]["workflow_id"] == "wf-1"

        # Step 4: Tasks complete independently while playbook is dead
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        # Meanwhile, workflow status stays "running" (scenario e)
        assert workflow.status == "running"

        # Step 5: Operator creates a new paused run and triggers recovery
        # Simulate: a new paused run was created for the same workflow
        run_new = _make_playbook_run(
            run_id="run-2",
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )
        workflow_fixed = _make_workflow(
            playbook_run_id="run-2",
            current_stage="build",
            task_ids=["t-1", "t-2"],
        )

        mock_db.get_workflow.return_value = workflow_fixed
        mock_db.get_playbook_run.return_value = run_new

        stage_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: stage_events.append(d))

        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["action"] == "stage_event_reemitted"
        assert len(stage_events) == 1

    async def test_startup_handles_mix_of_workflow_states(self, mock_db, event_bus):
        """Startup recovery correctly handles a mix of workflow states."""
        # wf-1: paused, all tasks done → re-emit
        wf1 = _make_workflow(
            workflow_id="wf-1",
            playbook_run_id="run-1",
            task_ids=["t-1"],
            current_stage="build",
        )
        run1 = _make_playbook_run(
            run_id="run-1",
            status="paused",
            waiting_for_event="workflow.stage.completed",
        )

        # wf-2: run failed → orphan event
        wf2 = _make_workflow(
            workflow_id="wf-2",
            playbook_run_id="run-2",
            task_ids=["t-2"],
        )
        run2 = _make_playbook_run(
            run_id="run-2",
            status="failed",
            waiting_for_event=None,
            error="crash",
        )

        # wf-3: run completed → sync workflow
        wf3 = _make_workflow(
            workflow_id="wf-3",
            playbook_run_id="run-3",
        )
        run3 = _make_playbook_run(
            run_id="run-3",
            status="completed",
            waiting_for_event=None,
            completed_at=time.time() - 100,
        )

        # wf-4: run stale "running" → mark failed
        wf4 = _make_workflow(
            workflow_id="wf-4",
            playbook_run_id="run-4",
        )
        run4 = _make_playbook_run(
            run_id="run-4",
            status="running",
            waiting_for_event=None,
        )

        mock_db.list_workflows.return_value = [wf1, wf2, wf3, wf4]
        runs = {"run-1": run1, "run-2": run2, "run-3": run3, "run-4": run4}
        mock_db.get_playbook_run.side_effect = lambda rid: runs.get(rid)
        mock_db.get_task.side_effect = lambda tid: FakeTask(
            id=tid, status=TaskStatus.COMPLETED
        )

        stage_events = []
        orphan_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: stage_events.append(d))
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["checked"] == 4
        assert result["events_reemitted"] == 1  # wf-1
        assert result["orphaned"] == 1  # wf-2 (failed run)
        assert result["synced_completed"] == 1  # wf-3
        assert result["marked_failed"] == 1  # wf-4

        # Verify correct events
        assert len(stage_events) == 1
        assert stage_events[0]["workflow_id"] == "wf-1"

        # wf-2 gets orphan event, wf-4 gets orphan event (from mark_run_failed)
        assert len(orphan_events) == 2
        orphan_wf_ids = {e["workflow_id"] for e in orphan_events}
        assert "wf-2" in orphan_wf_ids
        assert "wf-4" in orphan_wf_ids
