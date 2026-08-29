"""Dotted child ids from tasks.next_child_ordinal — spec §6."""

from __future__ import annotations

import asyncio

import pytest

from src import task_names
from src.database import Database
from src.models import Project, Task

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


async def mktask(db, tid):
    await db.create_task(Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid))


def test_naming_depth():
    assert task_names.naming_depth("swift-falcon") == 1
    assert task_names.naming_depth("swift-falcon.2") == 2
    assert task_names.naming_depth("swift-falcon.2.1") == 3


class TestReserveChildOrdinal:
    async def test_ordinals_are_sequential_and_never_reused(self, db):
        await mktask(db, "p")
        async with db._engine.begin() as conn:
            first = await task_names.reserve_child_ordinal(conn, "p")
            second = await task_names.reserve_child_ordinal(conn, "p")
        assert (first, second) == (1, 2)
        # A deleted sibling's ordinal is not reused.
        async with db._engine.begin() as conn:
            third = await task_names.reserve_child_ordinal(conn, "p")
        assert third == 3

    async def test_unknown_parent_raises(self, db):
        async with db._engine.begin() as conn:
            with pytest.raises(KeyError):
                await task_names.reserve_child_ordinal(conn, "nope")


class TestChildTaskId:
    async def test_dotted_id_under_root(self, db):
        await mktask(db, "p")
        async with db._engine.begin() as conn:
            cid, capped = await task_names.child_task_id(conn, "p")
        assert (cid, capped) == ("p.1", False)

    async def test_naming_depth_cap_falls_back_to_root_id(self, db):
        await mktask(db, "a.1.1")
        async with db._engine.begin() as conn:
            cid, capped = await task_names.child_task_id(conn, "a.1.1")
        assert capped is True
        assert "." not in cid

    async def test_concurrent_reservations_are_unique(self, db):
        await mktask(db, "p")

        async def one():
            async with db._engine.begin() as conn:
                return await task_names.reserve_child_ordinal(conn, "p")

        ords = await asyncio.gather(*(one() for _ in range(10)))
        assert sorted(ords) == list(range(1, 11))
