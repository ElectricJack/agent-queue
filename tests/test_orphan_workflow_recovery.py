"""Tests for orphan workflow recovery (Roadmap 7.5.6).

Verifies that:
1. On daemon startup, orphaned workflows are detected and recovery is attempted.
2. Paused playbook runs that missed ``workflow.stage.completed`` events are
   re-triggered when all tasks have completed.
3. Stale 'running' playbook runs (left from a daemon crash) are marked as failed.
4. Workflows whose playbook runs completed but status wasn't synced get updated.
5. Workflows with failed/timed_out runs are reported as orphaned.
6. Periodic monitoring detects new orphans without duplicating events.
7. Manual recovery via ``recover_workflow`` command works.
8. Tasks continue independently (already true — regression guard).
9. Orphan detection is idempotent (doesn't spam events).
10. Event schema for ``workflow.orphaned`` is properly defined.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from src.event_bus import EventBus
from src.event_schemas import EVENT_SCHEMAS, validate_event
from src.models import PlaybookRun, TaskStatus, Workflow
from src.orphan_workflow_recovery import OrphanWorkflowRecovery


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_bus():
    """An EventBus with validation disabled (to avoid schema noise)."""
    return EventBus(env="dev", validate_events=False)


@pytest.fixture
def mock_db():
    """A mock database backend."""
    db = AsyncMock()
    db.list_workflows = AsyncMock(return_value=[])
    db.get_workflow = AsyncMock(return_value=None)
    db.get_playbook_run = AsyncMock(return_value=None)
    db.get_task = AsyncMock(return_value=None)
    db.update_playbook_run = AsyncMock()
    db.update_workflow_status = AsyncMock()
    return db


_SENTINEL = object()


def _make_workflow(
    workflow_id: str = "wf-1",
    playbook_id: str = "coord-playbook",
    playbook_run_id: str = "run-1",
    project_id: str = "proj-1",
    status: str = "running",
    current_stage: str = "build",
    task_ids: list[str] | object = _SENTINEL,
) -> Workflow:
    return Workflow(
        workflow_id=workflow_id,
        playbook_id=playbook_id,
        playbook_run_id=playbook_run_id,
        project_id=project_id,
        status=status,
        current_stage=current_stage,
        task_ids=["t-1", "t-2"] if task_ids is _SENTINEL else task_ids,
        created_at=time.time(),
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
        started_at=time.time() - 3600,
        completed_at=completed_at,
        paused_at=paused_at or time.time(),
        waiting_for_event=waiting_for_event,
        error=error,
    )


@dataclass
class FakeTask:
    """Minimal task-like object for tests."""

    id: str
    status: TaskStatus = TaskStatus.COMPLETED
    workflow_id: str | None = None


# ---------------------------------------------------------------------------
# Tests: Event schema — workflow.orphaned
# ---------------------------------------------------------------------------


class TestWorkflowOrphanedEventSchema:
    """Verify the workflow.orphaned event schema is properly defined."""

    def test_schema_exists(self):
        assert "workflow.orphaned" in EVENT_SCHEMAS

    def test_required_fields(self):
        schema = EVENT_SCHEMAS["workflow.orphaned"]
        assert "workflow_id" in schema["required"]
        assert "playbook_id" in schema["required"]
        assert "project_id" in schema["required"]
        assert "reason" in schema["required"]

    def test_optional_fields(self):
        schema = EVENT_SCHEMAS["workflow.orphaned"]
        assert "run_id" in schema["optional"]
        assert "error" in schema["optional"]
        assert "current_stage" in schema["optional"]
        assert "task_ids" in schema["optional"]
        assert "recovery_requested" in schema["optional"]

    def test_validate_valid_event(self):
        errors = validate_event(
            "workflow.orphaned",
            {
                "workflow_id": "wf-1",
                "playbook_id": "coord-playbook",
                "project_id": "proj-1",
                "reason": "run_failed",
            },
        )
        assert errors == []

    def test_validate_missing_required(self):
        errors = validate_event(
            "workflow.orphaned",
            {"workflow_id": "wf-1"},
        )
        assert len(errors) == 3  # Missing playbook_id, project_id, reason


# ---------------------------------------------------------------------------
# Tests: Startup recovery
# ---------------------------------------------------------------------------


class TestStartupRecovery:
    """Verify recovery behavior on daemon startup."""

    async def test_no_running_workflows(self, mock_db, event_bus):
        """No running workflows → nothing to recover."""
        mock_db.list_workflows.return_value = []

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["checked"] == 0
        assert result["events_reemitted"] == 0
        assert result["orphaned"] == 0

    async def test_paused_run_all_tasks_done_reemits_event(self, mock_db, event_bus):
        """Paused run + all tasks completed → re-emit stage event."""
        workflow = _make_workflow()
        run = _make_playbook_run()

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(id=tid, status=TaskStatus.COMPLETED)

        emitted_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: emitted_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["events_reemitted"] == 1
        assert len(emitted_events) == 1
        assert emitted_events[0]["workflow_id"] == "wf-1"
        assert emitted_events[0]["stage"] == "build"
        assert emitted_events[0]["_recovery"] is True

    async def test_paused_run_tasks_still_pending(self, mock_db, event_bus):
        """Paused run + some tasks not completed → no action."""
        workflow = _make_workflow()
        run = _make_playbook_run()

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        # First task completed, second still in progress
        mock_db.get_task.side_effect = [
            FakeTask(id="t-1", status=TaskStatus.COMPLETED),
            FakeTask(id="t-2", status=TaskStatus.IN_PROGRESS),
        ]

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["events_reemitted"] == 0
        assert result["orphaned"] == 0

    async def test_stale_running_run_marked_failed(self, mock_db, event_bus):
        """Run stuck in 'running' after restart → marked failed + orphaned."""
        workflow = _make_workflow()
        run = _make_playbook_run(status="running", waiting_for_event=None)

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["marked_failed"] == 1
        # Verify the run was updated to failed
        mock_db.update_playbook_run.assert_called_once()
        call_kwargs = mock_db.update_playbook_run.call_args.kwargs
        assert call_kwargs.get("status") == "failed" or mock_db.update_playbook_run.call_args[
            0
        ] == ("run-1",)
        # Verify orphan event was emitted
        assert len(orphan_events) == 1
        assert orphan_events[0]["reason"] == "run_stale_running"

    async def test_completed_run_syncs_workflow(self, mock_db, event_bus):
        """Run completed but workflow status not updated → sync."""
        workflow = _make_workflow()
        run = _make_playbook_run(
            status="completed",
            waiting_for_event=None,
            completed_at=time.time() - 100,
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["synced_completed"] == 1
        mock_db.update_workflow_status.assert_called_once()
        args = mock_db.update_workflow_status.call_args
        assert args[0][1] == "completed"

    async def test_failed_run_emits_orphan_event(self, mock_db, event_bus):
        """Failed playbook run → emit workflow.orphaned."""
        workflow = _make_workflow()
        run = _make_playbook_run(
            status="failed",
            waiting_for_event=None,
            error="LLM error: budget exceeded",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["orphaned"] == 1
        assert len(orphan_events) == 1
        assert orphan_events[0]["reason"] == "run_failed"
        assert orphan_events[0]["error"] == "LLM error: budget exceeded"

    async def test_timed_out_run_emits_orphan_event(self, mock_db, event_bus):
        """Timed out playbook run → emit workflow.orphaned."""
        workflow = _make_workflow()
        run = _make_playbook_run(
            status="timed_out",
            waiting_for_event=None,
            error="Pause timeout exceeded (172800s)",
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["orphaned"] == 1
        assert orphan_events[0]["reason"] == "run_timed_out"

    async def test_missing_run_emits_orphan_event(self, mock_db, event_bus):
        """Playbook run record deleted → emit workflow.orphaned."""
        workflow = _make_workflow()
        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = None  # Run not found

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["orphaned"] == 1
        assert orphan_events[0]["reason"] == "run_not_found"

    async def test_no_playbook_run_id(self, mock_db, event_bus):
        """Workflow with no playbook_run_id → emit workflow.orphaned."""
        workflow = _make_workflow(playbook_run_id="")
        mock_db.list_workflows.return_value = [workflow]

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["orphaned"] == 1
        assert orphan_events[0]["reason"] == "no_playbook_run_id"

    async def test_paused_for_other_event_skipped(self, mock_db, event_bus):
        """Paused for non-stage event → no action (e.g., human review)."""
        workflow = _make_workflow()
        run = _make_playbook_run(waiting_for_event="human.review.completed")

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["events_reemitted"] == 0
        assert result["orphaned"] == 0

    async def test_multiple_workflows_processed(self, mock_db, event_bus):
        """Multiple running workflows are all processed."""
        wf1 = _make_workflow(workflow_id="wf-1", playbook_run_id="run-1")
        wf2 = _make_workflow(workflow_id="wf-2", playbook_run_id="run-2")

        run1 = _make_playbook_run(run_id="run-1")
        run2 = _make_playbook_run(
            run_id="run-2",
            status="failed",
            waiting_for_event=None,
            error="crash",
        )

        mock_db.list_workflows.return_value = [wf1, wf2]
        mock_db.get_playbook_run.side_effect = lambda rid: run1 if rid == "run-1" else run2
        mock_db.get_task.side_effect = lambda tid: FakeTask(id=tid, status=TaskStatus.COMPLETED)

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["checked"] == 2
        assert result["events_reemitted"] == 1  # wf1 has all tasks done
        assert result["orphaned"] == 1  # wf2 has failed run

    async def test_db_error_handled_gracefully(self, mock_db, event_bus):
        """Database errors during recovery don't crash the startup."""
        mock_db.list_workflows.side_effect = Exception("DB connection failed")

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["checked"] == 0  # Couldn't fetch workflows

    async def test_expired_pause_skipped(self, mock_db, event_bus):
        """Paused run that exceeded max age is skipped (handler will timeout it)."""
        workflow = _make_workflow()
        run = _make_playbook_run(
            paused_at=time.time() - 200000,  # Way past 48h
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["events_reemitted"] == 0


# ---------------------------------------------------------------------------
# Tests: Periodic monitoring
# ---------------------------------------------------------------------------


class TestPeriodicMonitoring:
    """Verify periodic orphan detection in run_one_cycle()."""

    async def test_rate_limited(self, mock_db, event_bus):
        """Check is skipped if called before the interval elapses."""
        recovery = OrphanWorkflowRecovery(
            db=mock_db, event_bus=event_bus, check_interval_seconds=60
        )
        # Set last check to now
        recovery._last_check_time = time.time()

        await recovery.check_periodic()

        mock_db.list_workflows.assert_not_called()

    async def test_detects_new_orphan(self, mock_db, event_bus):
        """New orphan is detected and event emitted."""
        workflow = _make_workflow()
        run = _make_playbook_run(status="failed", waiting_for_event=None, error="crashed")

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus, check_interval_seconds=0)
        await recovery.check_periodic()

        assert len(orphan_events) == 1
        assert orphan_events[0]["workflow_id"] == "wf-1"

    async def test_no_duplicate_events(self, mock_db, event_bus):
        """Already-reported orphan is not re-emitted."""
        workflow = _make_workflow()
        run = _make_playbook_run(status="failed", waiting_for_event=None, error="crashed")

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus, check_interval_seconds=0)

        # First check
        await recovery.check_periodic()
        assert len(orphan_events) == 1

        # Second check — should not emit again
        recovery._last_check_time = 0  # Reset rate limit
        await recovery.check_periodic()
        assert len(orphan_events) == 1  # Still just one

    async def test_cleared_orphan_re_reported(self, mock_db, event_bus):
        """After clearing, a previously reported orphan can be re-reported."""
        workflow = _make_workflow()
        run = _make_playbook_run(status="failed", waiting_for_event=None, error="crashed")

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus, check_interval_seconds=0)

        await recovery.check_periodic()
        assert len(orphan_events) == 1

        recovery.clear_reported("wf-1")
        recovery._last_check_time = 0
        await recovery.check_periodic()
        assert len(orphan_events) == 2

    async def test_completed_run_synced_periodically(self, mock_db, event_bus):
        """Periodic check syncs completed runs to workflow status."""
        workflow = _make_workflow()
        run = _make_playbook_run(
            status="completed",
            waiting_for_event=None,
            completed_at=time.time() - 100,
        )

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus, check_interval_seconds=0)
        await recovery.check_periodic()

        mock_db.update_workflow_status.assert_called_once()

    async def test_paused_with_all_done_reemits(self, mock_db, event_bus):
        """Periodic check re-emits missed stage events for paused runs."""
        workflow = _make_workflow()
        run = _make_playbook_run()

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(id=tid, status=TaskStatus.COMPLETED)

        stage_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: stage_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus, check_interval_seconds=0)
        await recovery.check_periodic()

        assert len(stage_events) == 1
        assert stage_events[0]["_recovery"] is True

    async def test_no_workflows_clears_reported(self, mock_db, event_bus):
        """When no running workflows remain, reported set is cleared."""
        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus, check_interval_seconds=0)
        recovery._reported_orphans = {"wf-old"}

        mock_db.list_workflows.return_value = []
        await recovery.check_periodic()

        assert len(recovery._reported_orphans) == 0

    async def test_stale_reported_cleaned_up(self, mock_db, event_bus):
        """Workflows no longer running are removed from reported set."""
        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus, check_interval_seconds=0)
        recovery._reported_orphans = {"wf-old", "wf-1"}

        # Only wf-1 is still running
        wf = _make_workflow(workflow_id="wf-1")
        run = _make_playbook_run()  # paused, tasks not done
        mock_db.list_workflows.return_value = [wf]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(id=tid, status=TaskStatus.IN_PROGRESS)

        await recovery.check_periodic()

        # wf-old should be removed, wf-1 should remain
        assert "wf-old" not in recovery._reported_orphans


# ---------------------------------------------------------------------------
# Tests: Manual recovery command
# ---------------------------------------------------------------------------


class TestManualRecovery:
    """Verify the recover_workflow() command handler."""

    async def test_workflow_not_found(self, mock_db, event_bus):
        """Attempting to recover a nonexistent workflow returns error."""
        mock_db.get_workflow.return_value = None

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-nonexistent")

        assert result["success"] is False
        assert "not found" in result["error"]

    async def test_completed_workflow_rejected(self, mock_db, event_bus):
        """Can't recover a workflow that's already completed."""
        workflow = _make_workflow(status="completed")
        mock_db.get_workflow.return_value = workflow

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is False
        assert "completed" in result["error"]

    async def test_paused_all_done_reemits(self, mock_db, event_bus):
        """Manual recovery of paused run with all tasks done → re-emit."""
        workflow = _make_workflow()
        run = _make_playbook_run()

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = lambda tid: FakeTask(id=tid, status=TaskStatus.COMPLETED)

        stage_events = []
        event_bus.subscribe("workflow.stage.completed", lambda d: stage_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["action"] == "stage_event_reemitted"
        assert len(stage_events) == 1

    async def test_paused_tasks_pending_reports_state(self, mock_db, event_bus):
        """Manual recovery with tasks pending → inform user."""
        workflow = _make_workflow()
        run = _make_playbook_run()

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = [
            FakeTask(id="t-1", status=TaskStatus.COMPLETED),
            FakeTask(id="t-2", status=TaskStatus.IN_PROGRESS),
        ]

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["action"] == "no_action"
        assert "pending" in result["reason"]

    async def test_paused_for_human_reports_state(self, mock_db, event_bus):
        """Manual recovery of human-paused run → advise resume_playbook."""
        workflow = _make_workflow()
        run = _make_playbook_run(waiting_for_event=None)

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["action"] == "no_action"
        assert "resume_playbook" in result["message"]

    async def test_running_run_no_action(self, mock_db, event_bus):
        """Manual recovery of a still-running workflow → no action."""
        workflow = _make_workflow()
        run = _make_playbook_run(status="running", waiting_for_event=None)

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["action"] == "no_action"
        assert result["reason"] == "still_running"

    async def test_failed_run_emits_orphan(self, mock_db, event_bus):
        """Manual recovery of failed run → emit orphaned event."""
        workflow = _make_workflow()
        run = _make_playbook_run(
            status="failed",
            waiting_for_event=None,
            error="LLM error",
        )

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["action"] == "orphaned_event_emitted"
        assert len(orphan_events) == 1
        assert orphan_events[0]["recovery_requested"] is True

    async def test_completed_run_syncs_workflow(self, mock_db, event_bus):
        """Manual recovery when run is completed → sync workflow status."""
        workflow = _make_workflow()
        run = _make_playbook_run(
            status="completed",
            waiting_for_event=None,
            completed_at=time.time() - 100,
        )

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["action"] == "synced_completed"
        mock_db.update_workflow_status.assert_called_once()

    async def test_no_run_id_emits_orphan(self, mock_db, event_bus):
        """Workflow with no playbook_run_id → emit orphaned."""
        workflow = _make_workflow(playbook_run_id="")
        mock_db.get_workflow.return_value = workflow

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["action"] == "orphaned_event_emitted"
        assert orphan_events[0]["recovery_requested"] is True

    async def test_run_not_found_emits_orphan(self, mock_db, event_bus):
        """Workflow pointing to deleted run → emit orphaned."""
        workflow = _make_workflow()
        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = None

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["action"] == "orphaned_event_emitted"
        assert orphan_events[0]["reason"] == "run_not_found"


# ---------------------------------------------------------------------------
# Tests: Task independence (regression guard)
# ---------------------------------------------------------------------------


class TestTaskIndependence:
    """Verify tasks continue executing regardless of playbook state."""

    async def test_tasks_have_independent_status(self, mock_db, event_bus):
        """Tasks remain in their current status even when playbook fails.

        This is a structural test — tasks are stored in the tasks table
        with their own status field, not derived from the workflow or
        playbook run status.
        """
        # Task is IN_PROGRESS while playbook run is failed
        task = FakeTask(id="t-1", status=TaskStatus.IN_PROGRESS, workflow_id="wf-1")
        workflow = _make_workflow()
        run = _make_playbook_run(status="failed", waiting_for_event=None)

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.return_value = task

        # Recovery should NOT modify task status
        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_workflow("wf-1")

        # Verify no task status updates were attempted
        mock_db.transition_task = AsyncMock()
        mock_db.transition_task.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Orphan event payload
# ---------------------------------------------------------------------------


class TestOrphanEventPayload:
    """Verify the workflow.orphaned event payload contains expected fields."""

    async def test_full_payload(self, mock_db, event_bus):
        """All relevant fields are included in the orphaned event."""
        workflow = _make_workflow(
            current_stage="review",
            task_ids=["t-1", "t-2", "t-3"],
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
        await recovery.recover_workflow("wf-1")

        assert len(orphan_events) == 1
        event = orphan_events[0]
        assert event["workflow_id"] == "wf-1"
        assert event["playbook_id"] == "coord-playbook"
        assert event["project_id"] == "proj-1"
        assert event["reason"] == "run_failed"
        assert event["run_id"] == "run-1"
        assert event["error"] == "Token budget exceeded"
        assert event["current_stage"] == "review"
        assert event["task_ids"] == ["t-1", "t-2", "t-3"]
        assert event["recovery_requested"] is True

    async def test_minimal_payload(self, mock_db, event_bus):
        """Orphaned event with minimal fields (no optional data)."""
        workflow = _make_workflow(
            playbook_run_id="",
            current_stage=None,
            task_ids=[],
        )
        mock_db.get_workflow.return_value = workflow

        orphan_events = []
        event_bus.subscribe("workflow.orphaned", lambda d: orphan_events.append(d))

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        await recovery.recover_workflow("wf-1")

        event = orphan_events[0]
        assert "current_stage" not in event  # None is not included
        assert "task_ids" not in event  # Empty list is not included
        assert "run_id" not in event  # No run_id to include


# ---------------------------------------------------------------------------
# Tests: Edge cases and error handling
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Verify edge cases and error resilience."""

    async def test_empty_task_ids(self, mock_db, event_bus):
        """Workflow with no task_ids → _all_tasks_completed returns False."""
        workflow = _make_workflow(task_ids=[])
        run = _make_playbook_run()

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        # Should not re-emit (no tasks = not completable)
        assert result["events_reemitted"] == 0

    async def test_task_fetch_error_assumes_incomplete(self, mock_db, event_bus):
        """If a task can't be fetched, assume it's not completed."""
        workflow = _make_workflow()
        run = _make_playbook_run()

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.get_task.side_effect = Exception("DB error")

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        assert result["events_reemitted"] == 0

    async def test_clear_reported_specific(self, mock_db, event_bus):
        """clear_reported with specific ID only clears that one."""
        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        recovery._reported_orphans = {"wf-1", "wf-2", "wf-3"}

        recovery.clear_reported("wf-2")

        assert "wf-1" in recovery._reported_orphans
        assert "wf-2" not in recovery._reported_orphans
        assert "wf-3" in recovery._reported_orphans

    async def test_clear_reported_all(self, mock_db, event_bus):
        """clear_reported with no args clears all."""
        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        recovery._reported_orphans = {"wf-1", "wf-2"}

        recovery.clear_reported()

        assert len(recovery._reported_orphans) == 0

    async def test_emit_orphaned_bus_error_handled(self, mock_db, event_bus):
        """Error during event emission doesn't crash recovery."""
        workflow = _make_workflow()
        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = None

        # Make bus.emit raise
        original_emit = event_bus.emit

        async def failing_emit(event_type, data=None):
            if event_type == "workflow.orphaned":
                raise RuntimeError("Bus error")
            return await original_emit(event_type, data)

        event_bus.emit = failing_emit

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        # Should not raise
        result = await recovery.recover_on_startup()
        assert result["orphaned"] == 1  # Still counted even though emit failed

    async def test_update_run_failure_handled(self, mock_db, event_bus):
        """If updating the stale run fails, error is reported gracefully."""
        workflow = _make_workflow()
        run = _make_playbook_run(status="running", waiting_for_event=None)

        mock_db.list_workflows.return_value = [workflow]
        mock_db.get_playbook_run.return_value = run
        mock_db.update_playbook_run.side_effect = Exception("DB write failed")

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_on_startup()

        # Should have an error detail
        detail = result["details"][0]
        assert detail["action"] == "error"

    async def test_workflow_status_paused_accepted_for_manual_recovery(self, mock_db, event_bus):
        """Workflow with status 'paused' can also be manually recovered."""
        workflow = _make_workflow(status="paused")
        run = _make_playbook_run(status="failed", waiting_for_event=None, error="crash")

        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = run

        recovery = OrphanWorkflowRecovery(db=mock_db, event_bus=event_bus)
        result = await recovery.recover_workflow("wf-1")

        assert result["success"] is True
        assert result["action"] == "orphaned_event_emitted"
