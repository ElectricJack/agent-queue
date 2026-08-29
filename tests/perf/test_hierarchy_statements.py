"""Statement budgets for hierarchy reads — spec §15.2 (size-independent)."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event

from src.database import Database
from src.models import Project, Task, TaskStatus

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "perf.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


@asynccontextmanager
async def count_statements(db):
    counter = {"n": 0}

    def _hook(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        yield counter
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)


async def build_wide_tree(db, width: int):
    await db.create_task(
        Task(
            id="root",
            project_id=PROJECT_ID,
            title="r",
            description="r",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    for i in range(width):
        cid = f"root.{i + 1}"
        await db.create_task(
            Task(
                id=cid,
                project_id=PROJECT_ID,
                title=cid,
                description=cid,
                status=TaskStatus.READY,
                parent_task_id=None,
            )
        )
        await db.add_dependency(cid, "root", "parent-child")
        gid = f"{cid}.1"
        await db.create_task(
            Task(
                id=gid,
                project_id=PROJECT_ID,
                title=gid,
                description=gid,
                status=TaskStatus.COMPLETED,
            )
        )
        await db.add_dependency(gid, cid, "parent-child")


@pytest.mark.parametrize("width", [3, 60])
async def test_tree_children_progress_are_size_independent(db, width):
    await build_wide_tree(db, width)
    async with count_statements(db) as c:
        await db.get_task_tree("root")
    assert c["n"] <= 3
    async with count_statements(db) as c:
        await db.get_children("root", recursive=True)
    assert c["n"] <= 3
    async with count_statements(db) as c:
        await db.get_group_progress("root")
    assert c["n"] <= 3
    async with count_statements(db) as c:
        await db.get_children_summary("root")
    assert c["n"] <= 1
