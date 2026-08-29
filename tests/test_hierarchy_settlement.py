"""Container settlement — spec §7."""

from __future__ import annotations

import time

import pytest

from src.database import Database
from src.models import Project, SessionRecord, Task, TaskStatus

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


async def family(db, n=2):
    await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
    kids = []
    for i in range(n):
        await mktask(db, f"c{i}", status=TaskStatus.READY)
        await db.add_dependency(f"c{i}", "p", "parent-child")
        kids.append(f"c{i}")
    return kids


class TestSettlement:
    async def test_last_child_completion_completes_container_in_same_call(self, db):
        kids = await family(db)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        assert (await db.get_task("p")).status == TaskStatus.IN_PROGRESS
        await db.transition_task(kids[1], TaskStatus.COMPLETED)
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED

    async def test_listener_receives_settled_ids(self, db):
        seen = []

        async def cb(ids):
            seen.append(list(ids))

        db.set_settlement_listener(cb)
        kids = await family(db, n=1)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        assert seen == [["p"]]

    async def test_live_session_guard(self, db):
        kids = await family(db, n=1)
        now = time.time()
        await db.create_session(
            SessionRecord(
                id="s1",
                task_id="p",
                project_id=PROJECT_ID,
                profile_id="worker",
                harness="claude",
                provider="fake",
                name="s-p",
                lifecycle="task",
                state="running",
                work_dir="/tmp",
                epoch="e",
                instance_token="t",
                started_at=now,
                last_activity=now,
            )
        )
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        assert (await db.get_task("p")).status == TaskStatus.IN_PROGRESS

    async def test_failed_child_does_not_settle(self, db):
        kids = await family(db)
        await db.transition_task(kids[0], TaskStatus.COMPLETED)
        await db.transition_task(kids[1], TaskStatus.FAILED)
        assert (await db.get_task("p")).status == TaskStatus.IN_PROGRESS

    async def test_settles_up_to_three_levels(self, db):
        await mktask(db, "g", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c", status=TaskStatus.READY)
        await db.add_dependency("p", "g", "parent-child")
        await db.add_dependency("c", "p", "parent-child")
        await db.transition_task("c", TaskStatus.COMPLETED)
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED
        assert (await db.get_task("g")).status == TaskStatus.COMPLETED

    async def test_emptied_container_settles_on_reparent(self, db):
        kids = await family(db, n=1)
        await mktask(db, "p2", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            await db.set_parent(kids[0], "p2", conn=conn)
        assert (await db.get_task("p")).status == TaskStatus.COMPLETED
        assert (await db.get_task("p2")).status == TaskStatus.IN_PROGRESS

    async def test_non_container_in_progress_leaf_is_untouched(self, db):
        await mktask(db, "leaf", status=TaskStatus.IN_PROGRESS)
        async with db._engine.begin() as conn:
            settled = await db.settle_containers({"leaf"}, conn=conn)
        assert settled == []
        assert (await db.get_task("leaf")).status == TaskStatus.IN_PROGRESS
