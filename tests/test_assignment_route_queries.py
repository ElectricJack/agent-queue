from __future__ import annotations

import pytest

from src.assignment_routing import assignment_input_hash
from src.database import Database
from src.models import PlaybookRun, Project, Task, TaskAssignmentRoute


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "assignment-routes.db"))
    await database.initialize()
    yield database
    await database.close()


async def _seed(db):
    project = Project(id="project-1", name="Project")
    task = Task(
        id="task-1",
        project_id=project.id,
        title="Route me",
        description="Choose a class",
        updated_at=50.0,
    )
    await db.create_project(project)
    await db.create_task(task)
    task = await db.get_task(task.id)
    run = PlaybookRun(
        run_id="run-1",
        playbook_id="default-assignment-routing",
        playbook_version=1,
        started_at=60.0,
    )
    await db.create_playbook_run(run)
    return task


def _route(task, *, run_id="run-1", intelligence_class="fast-low"):
    return TaskAssignmentRoute(
        task_id=task.id,
        project_id=task.project_id,
        input_hash=assignment_input_hash(task),
        task_updated_at=task.updated_at,
        options_hash="options-1",
        intelligence_class=intelligence_class,
        provider=None,
        playbook_id="default-assignment-routing",
        playbook_version=1,
        playbook_run_id=run_id,
        reason="Small focused change.",
        decided_at=61.0,
    )


@pytest.mark.asyncio
async def test_upsert_replaces_current_route(db):
    task = await _seed(db)
    route = _route(task)

    async with db.immediate() as conn:
        await db.upsert_task_assignment_routes([route], conn=conn)
    replacement = _route(task, intelligence_class="standard-medium")
    async with db.immediate() as conn:
        await db.upsert_task_assignment_routes([replacement], conn=conn)

    saved = await db.get_task_assignment_route(task.id)
    assert saved is not None
    assert saved.intelligence_class == "standard-medium"
    assert await db.list_task_assignment_routes([task.id]) == [saved]


@pytest.mark.asyncio
async def test_task_delete_cascades_assignment_route(db):
    task = await _seed(db)
    async with db.immediate() as conn:
        await db.upsert_task_assignment_routes([_route(task)], conn=conn)

    await db.delete_task(task.id, cascade=True)

    assert await db.get_task_assignment_route(task.id) is None


@pytest.mark.asyncio
async def test_project_assignment_playbook_round_trips(db):
    project = Project(
        id="project-route",
        name="Project Route",
        assignment_playbook_id="custom-router",
    )
    await db.create_project(project)

    assert (await db.get_project(project.id)).assignment_playbook_id == "custom-router"
    await db.update_project(project.id, assignment_playbook_id=None)
    assert (await db.get_project(project.id)).assignment_playbook_id is None
