"""Dependency query mixin: idempotent ``add_dependency`` (follow-up to
Phase 2 review — the raw INSERT used to raise on duplicate edges, forcing
callers like the pipeline compiler's per-task-review handler to wrap the
call in ``on_failure`` forwarding).
"""

from __future__ import annotations

import pytest

from src.database import Database
from src.models import DepType, Project, Task, TaskStatus
from tests.db_fixtures import lease_dsn


PROJECT = "p-dep"


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("dep.db"))
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
    async def test_description_is_persisted_on_the_edge(self, db):
        await _mktask(db, "spawned")
        await _mktask(db, "origin")

        await db.add_dependency(
            "spawned",
            "origin",
            DepType.DISCOVERED_FROM.value,
            description="The parser exposed a separate compatibility fix",
        )

        assert await db.get_typed_dependencies_detailed("spawned") == [
            {
                "depends_on_task_id": "origin",
                "dep_type": DepType.DISCOVERED_FROM.value,
                "description": "The parser exposed a separate compatibility fix",
            }
        ]

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


async def test_list_project_edges_returns_typed_rows_for_one_project(db):
    await db.create_project(Project(id="p2", name="P2"))
    for tid, pid in (("a", PROJECT), ("b", PROJECT), ("c", "p2")):
        await db.create_task(Task(id=tid, project_id=pid, title=tid, description=""))
    await db.add_dependency("b", "a", description="needs a")
    await db.add_dependency("c", "a")  # cross-project edge, from p2

    rows = await db.list_project_edges(PROJECT)

    assert rows == [
        {"task_id": "b", "depends_on_task_id": "a", "dep_type": "blocks", "description": "needs a"},
    ]
    assert await db.list_project_edges("p2") == [
        {"task_id": "c", "depends_on_task_id": "a", "dep_type": "blocks", "description": None},
    ]


async def test_get_typed_dependencies_for_tasks_matches_the_per_task_reads(db):
    for tid in ("a", "b", "c", "lonely"):
        await db.create_task(Task(id=tid, project_id=PROJECT, title=tid, description=""))
    await db.add_dependency("c", "b")
    await db.add_dependency("c", "a", "waits-for")
    await db.add_dependency("b", "a")

    batched = await db.get_typed_dependencies_for_tasks(["a", "b", "c", "lonely"])

    for tid in ("a", "b", "c", "lonely"):
        assert batched[tid] == await db.get_typed_dependencies(tid), tid
    assert batched["lonely"] == []


async def test_get_task_statuses_returns_only_existing_ids(db):
    await db.create_task(Task(id="a", project_id=PROJECT, title="a", description=""))
    await db.create_task(
        Task(id="b", project_id=PROJECT, title="b", description="", status=TaskStatus.COMPLETED)
    )

    assert await db.get_task_statuses(["a", "b", "ghost"]) == {"a": "DEFINED", "b": "COMPLETED"}
    assert await db.get_task_statuses([]) == {}


async def test_stuck_defined_tasks_are_listed_once_with_their_route(db):
    """A task blocked by two failed upstreams is one row, whatever its route holds.

    ``tasks.route`` is a PostgreSQL ``json`` column, which has no equality
    operator, so a ``SELECT DISTINCT`` over whole task rows cannot even be
    planned.  On 2026-09-28 that made this query raise on every scheduler
    cycle, and the cycle never reached pool or session reconciliation
    (fair-grove-86).
    """
    for tid, status in (
        ("failed-up", TaskStatus.FAILED),
        ("blocked-up", TaskStatus.BLOCKED),
        ("done-up", TaskStatus.COMPLETED),
    ):
        await db.create_task(
            Task(id=tid, project_id=PROJECT, title=tid, description="", status=status)
        )
    route = {"profile_id": "standard-high-claude", "reasons": ["class standard-high"]}
    for tid in ("stuck", "waiting", "later"):
        await db.create_task(
            Task(
                id=tid,
                project_id=PROJECT,
                title=tid,
                description="",
                status=TaskStatus.DEFINED,
                route=route,
            )
        )
    await db.add_dependency("stuck", "failed-up")
    await db.add_dependency("stuck", "blocked-up", DepType.WAITS_FOR.value)
    await db.add_dependency("stuck", "done-up")
    await db.add_dependency("waiting", "done-up")
    await db.add_dependency("later", "blocked-up")

    rows = await db.get_stuck_defined_tasks(threshold_seconds=3600)

    assert sorted(t.id for t in rows) == ["later", "stuck"]
    assert all(t.route == route for t in rows)
