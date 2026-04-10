"""Tests for workflow.stage.completed event emission (Roadmap 7.1.4).

Verifies that when all tasks in a running workflow reach COMPLETED status,
the orchestrator emits a ``workflow.stage.completed`` event on the event bus
with the correct payload (workflow_id, stage, task_ids).
"""

import time
from unittest.mock import AsyncMock

import pytest

from src.config import AppConfig
from src.event_bus import EventBus
from src.models import (
    Project,
    Task,
    TaskStatus,
    Workflow,
)
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str,
    project_id: str = "p-1",
    status: TaskStatus = TaskStatus.COMPLETED,
    workflow_id: str | None = None,
) -> Task:
    return Task(
        id=task_id,
        project_id=project_id,
        title=f"Task {task_id}",
        description="test",
        status=status,
        workflow_id=workflow_id,
    )


def _make_workflow(
    workflow_id: str = "wf-1",
    playbook_id: str = "pb-coord",
    playbook_run_id: str = "pbr-1",
    project_id: str = "p-1",
    status: str = "running",
    current_stage: str | None = "build",
    task_ids: list[str] | None = None,
) -> Workflow:
    return Workflow(
        workflow_id=workflow_id,
        playbook_id=playbook_id,
        playbook_run_id=playbook_run_id,
        project_id=project_id,
        status=status,
        current_stage=current_stage,
        task_ids=task_ids or [],
        created_at=time.time(),
    )


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


async def _setup_project(db, project_id: str = "p-1"):
    """Create a project for FK constraints."""
    await db.create_project(Project(id=project_id, name="test-project"))


async def _setup_workflow_prereqs(db, project_id="p-1", run_id="pbr-1"):
    """Create the project and playbook_run that workflows FK-reference."""
    from src.models import PlaybookRun

    await _setup_project(db, project_id)
    await db.create_playbook_run(
        PlaybookRun(
            run_id=run_id,
            playbook_id="pb-coord",
            playbook_version=1,
            trigger_event='{"type": "test"}',
            status="running",
            started_at=time.time(),
        )
    )


# ---------------------------------------------------------------------------
# Tests: _check_workflow_stage_completion
# ---------------------------------------------------------------------------


class TestWorkflowStageCompletion:
    """Verify workflow.stage.completed event emission logic."""

    async def test_no_workflow_id_noop(self, orch):
        """Tasks without workflow_id should not trigger any check."""
        task = _make_task("t-1", workflow_id=None)
        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(task)
        assert emitted == []

    async def test_nonexistent_workflow_noop(self, orch):
        """If the workflow doesn't exist in DB, no event is emitted."""
        task = _make_task("t-1", workflow_id="wf-missing")
        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(task)
        assert emitted == []

    async def test_workflow_not_running_noop(self, orch):
        """A paused or completed workflow should not emit stage completion."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(status="paused", task_ids=["t-1"])
        await orch.db.create_workflow(wf)

        task = _make_task("t-1", workflow_id="wf-1")
        await orch.db.create_task(task)

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(task)
        assert emitted == []

    async def test_all_tasks_completed_emits_event(self, orch):
        """When all workflow tasks are COMPLETED, the event is emitted."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            current_stage="build",
            task_ids=["t-1", "t-2"],
        )
        await orch.db.create_workflow(wf)

        # Create both tasks as COMPLETED
        t1 = _make_task("t-1", workflow_id="wf-1", status=TaskStatus.COMPLETED)
        t2 = _make_task("t-2", workflow_id="wf-1", status=TaskStatus.COMPLETED)
        await orch.db.create_task(t1)
        await orch.db.create_task(t2)

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(t2)

        assert len(emitted) == 1
        event = emitted[0]
        assert event["workflow_id"] == "wf-1"
        assert event["stage"] == "build"
        assert set(event["task_ids"]) == {"t-1", "t-2"}

    async def test_partial_completion_no_event(self, orch):
        """If some tasks are still in-progress, no event is emitted."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            current_stage="build",
            task_ids=["t-1", "t-2"],
        )
        await orch.db.create_workflow(wf)

        t1 = _make_task("t-1", workflow_id="wf-1", status=TaskStatus.COMPLETED)
        t2 = _make_task("t-2", workflow_id="wf-1", status=TaskStatus.IN_PROGRESS)
        await orch.db.create_task(t1)
        await orch.db.create_task(t2)

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(t1)
        assert emitted == []

    async def test_failed_task_blocks_stage_completion(self, orch):
        """A FAILED task should prevent stage completion."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            current_stage="test",
            task_ids=["t-1", "t-2"],
        )
        await orch.db.create_workflow(wf)

        t1 = _make_task("t-1", workflow_id="wf-1", status=TaskStatus.COMPLETED)
        t2 = _make_task("t-2", workflow_id="wf-1", status=TaskStatus.FAILED)
        await orch.db.create_task(t1)
        await orch.db.create_task(t2)

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(t1)
        assert emitted == []

    async def test_blocked_task_blocks_stage_completion(self, orch):
        """A BLOCKED task should prevent stage completion."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            current_stage="deploy",
            task_ids=["t-1", "t-2"],
        )
        await orch.db.create_workflow(wf)

        t1 = _make_task("t-1", workflow_id="wf-1", status=TaskStatus.COMPLETED)
        t2 = _make_task("t-2", workflow_id="wf-1", status=TaskStatus.BLOCKED)
        await orch.db.create_task(t1)
        await orch.db.create_task(t2)

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(t1)
        assert emitted == []

    async def test_single_task_workflow(self, orch):
        """A workflow with a single task emits event when that task completes."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            current_stage="solo",
            task_ids=["t-1"],
        )
        await orch.db.create_workflow(wf)

        t1 = _make_task("t-1", workflow_id="wf-1", status=TaskStatus.COMPLETED)
        await orch.db.create_task(t1)

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(t1)

        assert len(emitted) == 1
        assert emitted[0]["workflow_id"] == "wf-1"
        assert emitted[0]["stage"] == "solo"
        assert emitted[0]["task_ids"] == ["t-1"]

    async def test_null_current_stage_uses_empty_string(self, orch):
        """When workflow.current_stage is None, event uses empty string."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            current_stage=None,
            task_ids=["t-1"],
        )
        await orch.db.create_workflow(wf)

        t1 = _make_task("t-1", workflow_id="wf-1", status=TaskStatus.COMPLETED)
        await orch.db.create_task(t1)

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(t1)

        assert len(emitted) == 1
        assert emitted[0]["stage"] == ""

    async def test_empty_task_ids_noop(self, orch):
        """A workflow with no tasks should not emit."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            current_stage="build",
            task_ids=[],
        )
        await orch.db.create_workflow(wf)

        task = _make_task("t-1", workflow_id="wf-1")
        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(task)
        assert emitted == []

    async def test_event_schema_valid(self, orch):
        """The emitted event should pass schema validation."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            current_stage="build",
            task_ids=["t-1"],
        )
        await orch.db.create_workflow(wf)

        t1 = _make_task("t-1", workflow_id="wf-1", status=TaskStatus.COMPLETED)
        await orch.db.create_task(t1)

        # Use a dev-mode bus to enforce strict schema validation
        original_bus = orch.bus
        orch.bus = EventBus(env="dev", validate_events=True)
        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        # Should NOT raise EventValidationError
        await orch._check_workflow_stage_completion(t1)
        assert len(emitted) == 1

        # Restore
        orch.bus = original_bus

    async def test_workflow_completed_status_noop(self, orch):
        """An already-completed workflow should not emit stage events."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            status="completed",
            current_stage="done",
            task_ids=["t-1"],
        )
        await orch.db.create_workflow(wf)

        t1 = _make_task("t-1", workflow_id="wf-1", status=TaskStatus.COMPLETED)
        await orch.db.create_task(t1)

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(t1)
        assert emitted == []

    async def test_workflow_failed_status_noop(self, orch):
        """A failed workflow should not emit stage events."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            status="failed",
            current_stage="build",
            task_ids=["t-1"],
        )
        await orch.db.create_workflow(wf)

        t1 = _make_task("t-1", workflow_id="wf-1", status=TaskStatus.COMPLETED)
        await orch.db.create_task(t1)

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(t1)
        assert emitted == []

    async def test_db_error_during_workflow_fetch_graceful(self, orch):
        """Database errors when fetching workflow should be handled gracefully."""
        task = _make_task("t-1", workflow_id="wf-error")
        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        # Mock db.get_workflow to raise
        original_get = orch.db.get_workflow
        orch.db.get_workflow = AsyncMock(side_effect=RuntimeError("db broke"))

        await orch._check_workflow_stage_completion(task)
        assert emitted == []

        orch.db.get_workflow = original_get

    async def test_db_error_during_task_fetch_graceful(self, orch):
        """Database errors when fetching a workflow's task should be handled."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            current_stage="build",
            task_ids=["t-1", "t-bad"],
        )
        await orch.db.create_workflow(wf)

        t1 = _make_task("t-1", workflow_id="wf-1", status=TaskStatus.COMPLETED)
        await orch.db.create_task(t1)

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        # Mock get_task to fail on the second task
        original_get = orch.db.get_task

        async def flaky_get(tid):
            if tid == "t-bad":
                raise RuntimeError("db error")
            return await original_get(tid)

        orch.db.get_task = flaky_get

        await orch._check_workflow_stage_completion(t1)
        assert emitted == []  # Should not emit on error

        orch.db.get_task = original_get

    async def test_three_task_workflow_all_complete(self, orch):
        """A workflow with three tasks all completed emits correctly."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            current_stage="integration",
            task_ids=["t-1", "t-2", "t-3"],
        )
        await orch.db.create_workflow(wf)

        for tid in ["t-1", "t-2", "t-3"]:
            t = _make_task(tid, workflow_id="wf-1", status=TaskStatus.COMPLETED)
            await orch.db.create_task(t)

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        # The last task completing triggers the check
        last_task = await orch.db.get_task("t-3")
        await orch._check_workflow_stage_completion(last_task)

        assert len(emitted) == 1
        assert emitted[0]["stage"] == "integration"
        assert set(emitted[0]["task_ids"]) == {"t-1", "t-2", "t-3"}

    async def test_idempotent_when_called_multiple_times(self, orch):
        """Calling the check twice emits the event twice (no caching)."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            current_stage="build",
            task_ids=["t-1"],
        )
        await orch.db.create_workflow(wf)

        t1 = _make_task("t-1", workflow_id="wf-1", status=TaskStatus.COMPLETED)
        await orch.db.create_task(t1)

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(t1)
        await orch._check_workflow_stage_completion(t1)
        assert len(emitted) == 2  # no dedup at this layer

    async def test_task_not_in_db_blocks_completion(self, orch):
        """If a task_id in the workflow can't be found, stage is not complete."""
        await _setup_workflow_prereqs(orch.db)
        wf = _make_workflow(
            current_stage="build",
            task_ids=["t-1", "t-ghost"],  # t-ghost doesn't exist
        )
        await orch.db.create_workflow(wf)

        t1 = _make_task("t-1", workflow_id="wf-1", status=TaskStatus.COMPLETED)
        await orch.db.create_task(t1)

        emitted = []
        orch.bus.subscribe("workflow.stage.completed", lambda data: emitted.append(data))

        await orch._check_workflow_stage_completion(t1)
        assert emitted == []
