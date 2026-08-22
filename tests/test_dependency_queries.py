"""Dependency query mixin: idempotent ``add_dependency`` (follow-up to
Phase 2 review — the raw INSERT used to raise on duplicate edges, forcing
callers like the pipeline compiler's per-task-review handler to wrap the
call in ``on_failure`` forwarding).
"""

from __future__ import annotations

import pytest

from src.database import SQLiteDatabaseAdapter
from src.models import DepType, Project, Task, TaskStatus


PROJECT = "p-dep"


@pytest.fixture
async def db(tmp_path):
    database = SQLiteDatabaseAdapter(str(tmp_path / "dep.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT, name="dep"))
    yield database
    await database.close()


async def _mktask(db, tid):
    await db.create_task(
        Task(id=tid, project_id=PROJECT, title=tid, description=tid, status=TaskStatus.DEFINED)
    )
    return tid


class TestAddDependencyIdempotent:
    async def test_second_add_is_noop_and_does_not_raise(self, db):
        await _mktask(db, "t1")
        await _mktask(db, "t2")

        await db.add_dependency("t1", "t2", DepType.BLOCKS.value)
        # Second call with identical (task, depends_on, dep_type) must not raise.
        await db.add_dependency("t1", "t2", DepType.BLOCKS.value)

        deps = await db.get_typed_dependencies("t1")
        assert deps == [("t2", DepType.BLOCKS.value)]

    async def test_different_dep_types_coexist(self, db):
        await _mktask(db, "t1")
        await _mktask(db, "t2")

        await db.add_dependency("t1", "t2", DepType.BLOCKS.value)
        await db.add_dependency("t1", "t2", DepType.DISCOVERED_FROM.value)
        # Re-adding either should still be a no-op.
        await db.add_dependency("t1", "t2", DepType.BLOCKS.value)
        await db.add_dependency("t1", "t2", DepType.DISCOVERED_FROM.value)

        deps = await db.get_typed_dependencies("t1")
        assert sorted(deps) == sorted(
            [
                ("t2", DepType.BLOCKS.value),
                ("t2", DepType.DISCOVERED_FROM.value),
            ]
        )
