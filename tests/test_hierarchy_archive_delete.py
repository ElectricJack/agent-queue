"""Delete refuse/cascade and subtree-atomic archive — spec §7."""

from __future__ import annotations

import time

import pytest

from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.models import Project, Task, TaskStatus

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )


async def tree(db, statuses=("IN_PROGRESS", "READY", "READY")):
    """Build parent+2 children, all IN_PROGRESS/READY so ``add_dependency``
    (parent-child) never trips ``container_closed``, then stamp the intended
    terminal statuses in place with a raw UPDATE (controller ruling #1)."""
    await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
    await mktask(db, "c1", status=TaskStatus.READY)
    await mktask(db, "c2", status=TaskStatus.READY)
    await db.add_dependency("c1", "p", "parent-child")
    await db.add_dependency("c2", "p", "parent-child")
    await _set_statuses(db, {"p": statuses[0], "c1": statuses[1], "c2": statuses[2]})


async def _set_statuses(db, id_to_status: dict) -> None:
    from sqlalchemy import update

    from src.database.tables import tasks

    async with db._engine.begin() as conn:
        for tid, status in id_to_status.items():
            await conn.execute(update(tasks).where(tasks.c.id == tid).values(status=status))


class TestDelete:
    async def test_refuses_container_with_children(self, db):
        await tree(db)
        with pytest.raises(HierarchyError) as exc:
            await db.delete_task("p")
        assert exc.value.code == "has_children"
        assert await db.get_task("p") is not None

    async def test_cascade_deletes_subtree(self, db):
        await tree(db)
        await db.delete_task("p", cascade=True)
        assert await db.get_task("p") is None
        assert await db.get_task("c1") is None
        assert await db.get_task("c2") is None

    async def test_deleting_last_child_settles_container(self, db):
        await tree(db, statuses=("IN_PROGRESS", "COMPLETED", "READY"))
        await db.delete_task("c2")
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED


class TestArchive:
    async def test_refuses_open_descendants(self, db):
        await tree(db, statuses=("COMPLETED", "COMPLETED", "READY"))
        with pytest.raises(HierarchyError) as exc:
            await db.archive_task("p")
        assert exc.value.code == "open_descendants"

    async def test_archives_subtree_together(self, db):
        await tree(db, statuses=("COMPLETED", "COMPLETED", "FAILED"))
        assert await db.archive_task("p") is True
        for tid in ("p", "c1", "c2"):
            assert await db.get_task(tid) is None
            assert (await db.get_archived_task(tid)) is not None
        assert (await db.get_archived_task("c1"))["parent_task_id"] == "p"

    async def test_sweep_selects_only_terminal_subtree_roots(self, db):
        await tree(db, statuses=("COMPLETED", "COMPLETED", "READY"))
        await mktask(db, "lone", status=TaskStatus.COMPLETED)
        # Make every row old enough.
        async with db._engine.begin() as conn:
            from sqlalchemy import update

            from src.database.tables import tasks

            await conn.execute(update(tasks).values(updated_at=time.time() - 10_000))
        archived = await db.archive_old_terminal_tasks(["COMPLETED"], older_than_seconds=1)
        assert archived == ["lone"]
        assert await db.get_task("p") is not None
        assert await db.get_task("c1") is not None
