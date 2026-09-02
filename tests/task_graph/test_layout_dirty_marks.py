"""Dirty marks and layout-row cleanup on every graph write path (design §4.10)."""

import pytest
from sqlalchemy import func, select

from src.database import Database
from src.database.tables import task_layout_cells, task_layouts
from src.models import Project, Task, TaskStatus
from src.task_graph.layout.driver import LayoutDriver


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "dm.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1"))
    yield d
    await d.close()


async def marks(db):
    _, rows = await db.pop_layout_dirty("p1", min_age_seconds=0)
    return rows


async def drain(db):
    seq, rows = await db.pop_layout_dirty("p1", min_age_seconds=0)
    if seq:
        async with db._engine.begin() as conn:
            await db.clear_layout_dirty("p1", seq, conn=conn)
    return rows


async def layout_row_count(db, task_ids=None, project_id=None):
    async with db._engine.begin() as conn:
        q1 = select(func.count()).select_from(task_layouts)
        q2 = select(func.count()).select_from(task_layout_cells)
        if task_ids is not None:
            q1 = q1.where(task_layouts.c.task_id.in_(list(task_ids)))
            q2 = q2.where(task_layout_cells.c.task_id.in_(list(task_ids)))
        if project_id is not None:
            q1 = q1.where(task_layouts.c.project_id == project_id)
            q2 = q2.where(task_layout_cells.c.project_id == project_id)
        return (await conn.execute(q1)).scalar_one(), (await conn.execute(q2)).scalar_one()


# ── dirty marks ─────────────────────────────────────────────────────────


async def test_create_marks(db):
    await db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    assert ("a", "task.created") in await marks(db)


async def test_set_parent_marks_with_old_parent(db):
    for t in ("a", "b", "c"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("c", "a", conn=conn)
        await db.set_parent("c", "b", conn=conn)
    m = await marks(db)
    assert ("c", "parent.changed:-") in m and ("c", "parent.changed:a") in m


async def test_set_parent_bulk_marks_each_child(db):
    for t in ("p", "a", "b"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    await drain(db)
    async with db._engine.begin() as conn:
        await db.set_parent_bulk(["a", "b"], "p", conn=conn)
    m = await marks(db)
    assert ("a", "parent.changed:-") in m and ("b", "parent.changed:-") in m


async def test_dependency_marks_both_endpoints(db):
    for t in ("a", "b"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    await db.add_dependency("b", "a")
    m = await marks(db)
    assert ("a", "dependency.changed") in m and ("b", "dependency.changed") in m
    await drain(db)
    await db.remove_dependency("b", "a")
    assert ("b", "dependency.changed") in await marks(db)


async def test_remove_all_dependencies_on_marks(db):
    for t in ("a", "b", "c"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    await db.add_dependency("b", "a")
    await db.add_dependency("c", "a")
    await drain(db)
    await db.remove_all_dependencies_on("a")
    m = await marks(db)
    assert ("a", "dependency.changed") in m
    assert ("b", "dependency.changed") in m and ("c", "dependency.changed") in m


async def test_status_marks_only_on_finished_boundary(db):
    await db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    await drain(db)  # drain create mark
    await db.transition_task("a", TaskStatus.READY, force=True)
    assert await marks(db) == []
    await db.transition_task("a", TaskStatus.COMPLETED, force=True)
    assert ("a", "status.finished") in await marks(db)
    await drain(db)
    await db.transition_task("a", TaskStatus.READY, force=True)
    assert ("a", "status.reopened") in await marks(db)


async def test_delete_and_archive_mark(db):
    await db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    await db.create_task(Task(id="b", project_id="p1", title="b", description=""))
    await drain(db)
    await db.delete_task("a")
    assert ("a", "task.deleted") in await marks(db)
    await db.transition_task("b", TaskStatus.COMPLETED, force=True)
    await drain(db)
    assert await db.archive_task("b")
    assert ("b", "task.archived") in await marks(db)


async def test_delete_cascade_marks_every_descendant(db):
    for t in ("p", "c1", "c2"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("c1", "p", conn=conn)
        await db.set_parent("c2", "p", conn=conn)
    await drain(db)
    await db.delete_task("p", cascade=True)
    m = await marks(db)
    for t in ("p", "c1", "c2"):
        assert (t, "task.deleted") in m


# ── layout-row cleanup (FK holders) ─────────────────────────────────────


async def test_delete_task_clears_layout_rows(db):
    for t in ("a", "b"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    await LayoutDriver(db).full_layout("p1", "all")
    assert (await layout_row_count(db, ["a"]))[0] == 1
    await db.delete_task("a")
    assert await layout_row_count(db, ["a"]) == (0, 0)


async def test_delete_task_cascade_clears_subtree_layout_rows(db):
    for t in ("p", "c1"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    async with db._engine.begin() as conn:
        await db.set_parent("c1", "p", conn=conn)
    await LayoutDriver(db).full_layout("p1", "all")
    await db.delete_task("p", cascade=True)
    assert await layout_row_count(db, ["p", "c1"]) == (0, 0)


async def test_archive_task_clears_layout_rows(db):
    await db.create_task(Task(id="a", project_id="p1", title="a", description=""))
    await db.create_task(Task(id="b", project_id="p1", title="b", description=""))
    await LayoutDriver(db).full_layout("p1", "all")
    await db.transition_task("b", TaskStatus.COMPLETED, force=True)
    assert await db.archive_task("b")
    assert await layout_row_count(db, ["b"]) == (0, 0)
    assert (await layout_row_count(db, ["a"]))[0] == 1


async def test_delete_project_clears_all_layout_state(db):
    for t in ("a", "b"):
        await db.create_task(Task(id=t, project_id="p1", title=t, description=""))
    await LayoutDriver(db).full_layout("p1", "all")
    await db.enqueue_layout_job("p1", "all", "tidy")
    async with db._engine.begin() as conn:
        await db.mark_layout_dirty("p1", ["a"], "task.created", conn=conn)
    await db.delete_project("p1")
    assert await layout_row_count(db, project_id="p1") == (0, 0)
    assert await db.get_layout_meta("p1", "all") is None
    assert await db.dirty_layout_projects() == []
    assert await db.next_layout_job() is None
