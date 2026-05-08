"""task_workspace_requirements CRUD. See spec §3.3."""

from __future__ import annotations

import time

import pytest

from src.database import Database
from src.models import Project, Task


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    # Tasks need a project; the FK on task_workspace_requirements depends on
    # tasks existing.  Create a minimal project and task for the test scope.
    await database.create_project(Project(id="p1", name="test"))
    await database.create_task(Task(
        id="t1", project_id="p1", title="t1",
        description="", created_at=time.time(), updated_at=time.time(),
    ))
    await database.create_task(Task(
        id="t2", project_id="p1", title="t2",
        description="", created_at=time.time(), updated_at=time.time(),
    ))
    yield database
    await database.close()


class TestAddTaskRequirements:
    async def test_assigns_position_zero_for_first_of_kind(self, db):
        await db.add_task_workspace_requirements("t1", [("game-repo", None)])
        rows = await db.fetch_task_workspace_requirements("t1")
        assert len(rows) == 1
        assert rows[0].kind_id == "game-repo"
        assert rows[0].position == 0
        assert rows[0].alias is None

    async def test_assigns_distinct_positions_per_kind(self, db):
        await db.add_task_workspace_requirements(
            "t1",
            [
                ("game-repo", None),
                ("package-foo", "primary"),
                ("package-foo", "mirror"),
            ],
        )
        rows = await db.fetch_task_workspace_requirements("t1")
        by_kind: dict[str, list] = {}
        for r in rows:
            by_kind.setdefault(r.kind_id, []).append(r)
        assert {row.position for row in by_kind["game-repo"]} == {0}
        assert {row.position for row in by_kind["package-foo"]} == {0, 1}
        assert {row.alias for row in by_kind["package-foo"]} == {"primary", "mirror"}

    async def test_subsequent_add_continues_position_per_kind(self, db):
        """A second add() for the same task continues numbering, doesn't reset."""
        await db.add_task_workspace_requirements("t1", [("game-repo", None)])
        await db.add_task_workspace_requirements("t1", [("game-repo", "second")])
        rows = await db.fetch_task_workspace_requirements("t1")
        positions = {r.position for r in rows if r.kind_id == "game-repo"}
        assert positions == {0, 1}

    async def test_empty_requirements_is_noop(self, db):
        await db.add_task_workspace_requirements("t1", [])
        assert await db.fetch_task_workspace_requirements("t1") == []


class TestFetchTaskRequirements:
    async def test_orders_by_kind_then_position(self, db):
        await db.add_task_workspace_requirements(
            "t1",
            [
                ("zeta", None),
                ("alpha", None),
                ("alpha", "second"),
            ],
        )
        rows = await db.fetch_task_workspace_requirements("t1")
        keys = [(r.kind_id, r.position) for r in rows]
        assert keys == sorted(keys)
        assert keys == [("alpha", 0), ("alpha", 1), ("zeta", 0)]

    async def test_returns_empty_for_unknown_task(self, db):
        # No rows added; t1 has none.
        assert await db.fetch_task_workspace_requirements("t1") == []
        assert await db.fetch_task_workspace_requirements("nonexistent") == []

    async def test_isolation_between_tasks(self, db):
        await db.add_task_workspace_requirements("t1", [("a", None)])
        await db.add_task_workspace_requirements("t2", [("b", None)])
        t1_rows = await db.fetch_task_workspace_requirements("t1")
        t2_rows = await db.fetch_task_workspace_requirements("t2")
        assert [r.kind_id for r in t1_rows] == ["a"]
        assert [r.kind_id for r in t2_rows] == ["b"]


class TestDeleteTaskRequirements:
    async def test_clears_all_rows_for_task(self, db):
        await db.add_task_workspace_requirements("t1", [("x", None), ("y", None)])
        assert len(await db.fetch_task_workspace_requirements("t1")) == 2
        await db.delete_task_workspace_requirements("t1")
        assert await db.fetch_task_workspace_requirements("t1") == []

    async def test_does_not_affect_other_tasks(self, db):
        await db.add_task_workspace_requirements("t1", [("a", None)])
        await db.add_task_workspace_requirements("t2", [("b", None)])
        await db.delete_task_workspace_requirements("t1")
        assert await db.fetch_task_workspace_requirements("t1") == []
        assert len(await db.fetch_task_workspace_requirements("t2")) == 1
