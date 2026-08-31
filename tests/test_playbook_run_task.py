from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models import Project, TaskStatus
from src.playbooks.run_task import playbook_status_to_task_status, sync_playbook_run_task


@pytest.mark.parametrize(
    ("run_status", "task_status"),
    [
        ("running", TaskStatus.IN_PROGRESS),
        ("paused", TaskStatus.PAUSED),
        ("completed", TaskStatus.COMPLETED),
        ("failed", TaskStatus.FAILED),
        ("timed_out", TaskStatus.FAILED),
        ("cancelled", TaskStatus.FAILED),
    ],
)
def test_playbook_status_mapping(run_status, task_status):
    assert playbook_status_to_task_status(run_status) == task_status


async def test_sync_creates_root_directly_in_run_status():
    created = SimpleNamespace(id="run-root", status=TaskStatus.IN_PROGRESS)
    handler = MagicMock()
    handler.db.list_tasks = AsyncMock(return_value=[])
    handler.db.get_task = AsyncMock(return_value=created)
    handler.execute = AsyncMock(return_value={"success": True, "task_id": "run-root", "created": True})

    task_id = await sync_playbook_run_task(
        handler,
        project_id="project-one",
        playbook_id="release",
        run_id="run-123",
        status="running",
    )

    assert task_id == "run-root"
    handler.execute.assert_awaited_once_with(
        "ensure_task",
        {
            "project_id": "project-one",
            "title": "Playbook run: release",
            "description": "Playbook release run run-123",
            "dedup_key": "playbook-run:run-123",
            "initial_status": "IN_PROGRESS",
        },
    )
    handler.db.transition_task.assert_not_called()


async def test_sync_reuses_terminal_root_and_projects_new_status():
    existing = SimpleNamespace(
        id="run-root",
        dedup_key="playbook-run:run-123",
        status=TaskStatus.IN_PROGRESS,
    )
    updated = SimpleNamespace(id="run-root", status=TaskStatus.COMPLETED)
    handler = MagicMock()
    handler.db.list_tasks = AsyncMock(return_value=[existing])
    handler.db.get_task = AsyncMock(return_value=updated)
    handler.db.transition_task = AsyncMock()
    handler._emit_task_graph_change = AsyncMock()

    task_id = await sync_playbook_run_task(
        handler,
        project_id="project-one",
        playbook_id="release",
        run_id="run-123",
        status="completed",
    )

    assert task_id == "run-root"
    handler.db.transition_task.assert_awaited_once_with(
        "run-root",
        TaskStatus.COMPLETED,
        context="playbook_run_projection",
        force=True,
        _manual_pause_control=True,
    )
    handler._emit_task_graph_change.assert_awaited_once_with("task.updated", updated)
    handler.execute.assert_not_called()


async def test_sync_without_project_is_a_noop():
    handler = MagicMock()
    handler.execute = AsyncMock()
    assert await sync_playbook_run_task(
        handler,
        project_id=None,
        playbook_id="system-maintenance",
        run_id="run-123",
        status="running",
    ) is None
    handler.execute.assert_not_awaited()


async def test_sync_failure_never_escapes_into_playbook_execution():
    handler = MagicMock()
    handler.db.list_tasks = AsyncMock(side_effect=RuntimeError("database unavailable"))
    assert await sync_playbook_run_task(
        handler,
        project_id="project-one",
        playbook_id="release",
        run_id="run-123",
        status="running",
    ) is None


async def test_sync_projects_status_into_real_task_database(command_handler_factory):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="project-one", name="Project One"))

    task_id = await sync_playbook_run_task(
        handler,
        project_id="project-one",
        playbook_id="release",
        run_id="run-real",
        status="running",
    )

    task = await handler.db.get_task(task_id)
    assert task is not None
    assert task.dedup_key == "playbook-run:run-real"
    assert task.status == TaskStatus.IN_PROGRESS

    for run_status, expected in [
        ("paused", TaskStatus.PAUSED),
        ("running", TaskStatus.IN_PROGRESS),
        ("completed", TaskStatus.COMPLETED),
    ]:
        assert await sync_playbook_run_task(
            handler,
            project_id="project-one",
            playbook_id="release",
            run_id="run-real",
            status=run_status,
        ) == task_id
        projected = await handler.db.get_task(task_id)
        assert projected is not None
        assert projected.status == expected
    await handler.db.close()
