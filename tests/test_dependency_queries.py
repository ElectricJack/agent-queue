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


@pytest.mark.parametrize("caller_transaction", [False, True])
async def test_new_blocker_demotes_ready_parent_and_withholds_its_child(db, caller_transaction):
    await _mktask(db, "up")
    for tid in ("parent", "child"):
        await db.create_task(Task(
            id=tid, project_id=PROJECT, title=tid, description=tid, status=TaskStatus.READY,
        ))
    await db.add_dependency("child", "parent", DepType.PARENT_CHILD.value)
    assert (await db.get_task("child")).is_blocked is False
    db._sm_enforce = True

    if caller_transaction:
        async with db._engine.begin() as conn:
            flipped = await db.add_dependency("parent", "up", conn=conn)
        assert flipped == {"parent", "child"}
    else:
        await db.add_dependency("parent", "up")

    assert (await db.get_task("parent")).status == TaskStatus.DEFINED
    assert (await db.get_task("child")).is_blocked is True
    assert await db.get_ready_frontier(PROJECT) == []


@pytest.mark.parametrize("dep_type,dep_status", [
    (DepType.BLOCKS, TaskStatus.COMPLETED),
    (DepType.RELATED, TaskStatus.DEFINED),
    (DepType.DISCOVERED_FROM, TaskStatus.DEFINED),
    (DepType.WAITS_FOR, TaskStatus.DEFINED),  # no children: vacuously satisfied
    (DepType.CONDITIONAL_BLOCKS, TaskStatus.BLOCKED),
])
async def test_satisfied_and_provenance_edges_preserve_ready_status(db, dep_type, dep_status):
    await db.create_task(Task(
        id="up", project_id=PROJECT, title="up", description="", status=dep_status,
    ))
    await db.create_task(Task(
        id="ready", project_id=PROJECT, title="ready", description="", status=TaskStatus.READY,
    ))

    await db.add_dependency("ready", "up", dep_type.value)

    task = await db.get_task("ready")
    assert task.status == TaskStatus.READY
    assert task.is_blocked is False


@pytest.mark.parametrize("status", [TaskStatus.READY, TaskStatus.IN_PROGRESS, TaskStatus.ASSIGNED])
async def test_new_blocker_preserves_claim_holder_status(db, status):
    await _mktask(db, "up")
    await db.create_task(Task(
        id="held", project_id=PROJECT, title="held", description="", status=status,
    ))
    async with db._engine.begin() as conn:
        await db._upsert_meta("held", "claimed_by_session", {"session_id": "holder"}, conn=conn)

    await db.add_dependency("held", "up")

    task = await db.get_task("held")
    assert task.status == status
    assert task.is_blocked is True


async def test_demoting_edge_and_status_roll_back_together(db):
    await _mktask(db, "up")
    await db.create_task(Task(
        id="ready", project_id=PROJECT, title="ready", description="", status=TaskStatus.READY,
    ))

    with pytest.raises(RuntimeError, match="abort"):
        async with db._engine.begin() as conn:
            await db.add_dependency("ready", "up", conn=conn)
            raise RuntimeError("abort")

    task = await db.get_task("ready")
    assert task.status == TaskStatus.READY
    assert task.is_blocked is False
    assert await db.get_typed_dependencies("ready") == []


async def test_has_waiting_dependents_counts_unfinished_blocking_edges(db):
    for tid in ("fix", "waiter", "done-waiter", "child", "note", "free"):
        await _mktask(db, tid)
    await db.add_dependency("done-waiter", "free", DepType.BLOCKS.value)
    await db.add_dependency("child", "free", DepType.PARENT_CHILD.value)
    await db.add_dependency("note", "free", DepType.DISCOVERED_FROM.value)
    await db.update_task("done-waiter", status=TaskStatus.COMPLETED)
    assert await db.has_waiting_dependents("free") is False

    await db.add_dependency("waiter", "fix", DepType.WAITS_FOR.value)
    assert await db.has_waiting_dependents("fix") is True
    await db.update_task("waiter", status=TaskStatus.FAILED)
    assert await db.has_waiting_dependents("fix") is False


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

    ``tasks.route`` was a PostgreSQL ``json`` column, which has no equality
    operator, so a ``SELECT DISTINCT`` over whole task rows could not be
    planned (fair-grove-86). The semi-join also avoids duplicate task rows.
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


async def test_get_stuck_defined_tasks_reads_tasks_that_carry_a_route(db):
    """Outage 2026-09-28: with ``tasks.route`` typed ``json``, this query's
    ``SELECT DISTINCT tasks.*`` failed every scheduler cycle (``could not
    identify an equality operator for type json``).  The route here is not
    null, and ``stuck`` has two failed blockers, which the old DISTINCT
    collapsed into one row."""
    route = {"reason": "router", "candidates": [{"profile_id": "p", "score": 1}]}
    statuses = {
        "failed": TaskStatus.FAILED,
        "blocked": TaskStatus.BLOCKED,
        "done": TaskStatus.COMPLETED,
    }
    for tid, status in statuses.items():
        await db.create_task(
            Task(id=tid, project_id=PROJECT, title=tid, description="", status=status)
        )
    for tid in ("stuck", "fine", "discovered", "second"):
        await db.create_task(
            Task(id=tid, project_id=PROJECT, title=tid, description="", route=route)
        )
    await db.create_task(
        Task(
            id="ready",
            project_id=PROJECT,
            title="ready",
            description="",
            status=TaskStatus.READY,
            route=route,
        )
    )
    await db.add_dependency("stuck", "failed")
    await db.add_dependency("stuck", "blocked")
    await db.add_dependency("second", "blocked")
    await db.add_dependency("fine", "done")
    await db.add_dependency("discovered", "failed", DepType.DISCOVERED_FROM.value)
    await db.add_dependency("ready", "failed")

    stuck = await db.get_stuck_defined_tasks(0)

    # Adding the undelivered blocker demotes "ready" to DEFINED too.
    assert sorted(task.id for task in stuck) == ["ready", "second", "stuck"]  # one row per task
    assert all(task.route == route for task in stuck)
