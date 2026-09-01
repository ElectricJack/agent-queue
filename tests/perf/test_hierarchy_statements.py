"""Statement budgets for hierarchy reads — spec §15.2 (size-independent)."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event, insert

from src.database import Database
from src.database.tables import task_dependencies as task_dependencies_t, tasks as tasks_t
from src.models import Project, Task, TaskStatus
from src.task_graph import parse_graph
from src.task_graph.creator import build_plan, write_plan

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


# --- write side (spec §15.2 scale) ----------------------------------------

#: §15.2 reference scale: 5,000 live tasks and ~2,500 blocking edges.
SEED_TASKS = 5000
SEED_EDGES = 2500

#: Graph size the write-side budget is stated for.
PLAN_NODES = 200


async def seed_scale(
    db,
    n_tasks: int = SEED_TASKS,
    n_edges: int = SEED_EDGES,
    profile_id: str | None = None,
    intelligence_class: str | None = None,
) -> None:
    """Bulk-insert a §15.2-scale queue (raw inserts — this is fixture cost).

    ``profile_id``, when given, is stamped on every READY task so claim-path
    perf tests can exercise ``select_ready_for_profile`` at scale (the caller
    is responsible for the referenced ``agent_profiles`` row existing — this
    is a raw insert, so the FK is enforced but not satisfied for you).
    """
    now = time.time()
    n_paired = min(n_edges, n_tasks // 2)
    # Pair ``seed-{2i+1}`` (the dependent) with ``seed-{2i}`` (its
    # predecessor).  Dependents are always READY -- a dependent is only
    # interesting to the frontier query if it is otherwise schedulable.
    # Predecessors alternate: half COMPLETED (so their dependent is
    # unblocked) and half READY (non-terminal, so their dependent stays
    # blocked).  That leaves half the dependents on the frontier and half
    # held off it.
    #
    # ``is_blocked`` must agree with the edges, not just default to 0 from
    # the raw insert, or the seeded queue doesn't reflect what
    # ``add_dependency`` would actually have projected and any perf number
    # that depends on the frontier query's ``is_blocked`` filter is
    # measuring an unrealistically-clean queue.
    def _is_dependent(i: int) -> bool:
        return i % 2 == 1 and (i - 1) // 2 < n_paired

    def _predecessor_completed(i: int) -> bool:
        """Predecessors ``seed-{2j}`` with even *j* are COMPLETED."""
        return (i // 2) % 2 == 0

    def _status(i: int) -> str:
        if _is_dependent(i):
            return TaskStatus.READY.value
        if i % 2 == 0 and (i // 2) < n_paired and _predecessor_completed(i):
            return TaskStatus.COMPLETED.value
        return TaskStatus.READY.value

    blocked_dependents = {
        2 * i + 1 for i in range(n_paired) if not _predecessor_completed(2 * i)
    }
    rows = [
        {
            "id": f"seed-{i}",
            "project_id": PROJECT_ID,
            "title": f"t{i}",
            "description": "d",
            "status": _status(i),
            # Stamped on every READY task, as the docstring says.
            "profile_id": profile_id if _status(i) == TaskStatus.READY.value else None,
            "intelligence_class": (
                intelligence_class if _status(i) == TaskStatus.READY.value else None
            ),
            "is_blocked": 1 if i in blocked_dependents else 0,
            "created_at": now,
            "updated_at": now,
        }
        for i in range(n_tasks)
    ]
    edges = [
        {
            "task_id": f"seed-{2 * i + 1}",
            "depends_on_task_id": f"seed-{2 * i}",
            "dep_type": "blocks",
        }
        for i in range(n_paired)
    ]
    async with db._engine.begin() as conn:
        await conn.execute(insert(tasks_t), rows)
        await conn.execute(insert(task_dependencies_t), edges)


def _graph(n: int) -> dict:
    """A graph of *n* nodes in a chain of pairs (~n/2 blocking edges)."""
    nodes = []
    for i in range(n):
        node = {"key": f"n{i}", "title": f"N{i}"}
        if i % 2:
            node["needs"] = [{"on": f"n{i - 1}"}]
        nodes.append(node)
    return {"version": 1, "parent": {"title": "Epic"}, "nodes": nodes}


async def test_write_plan_is_bulk_at_scale(db):
    """A 200-node ``write_plan`` must not re-validate the parent per node.

    Before ``set_parent_bulk`` this issued ~23 statements per node (each
    ``set_parent`` re-read *every* blocking edge in the database for its DAG
    check) — 4,609 statements and 5.4 s at this scale.
    """
    await seed_scale(db)
    plan = await build_plan(db, parse_graph(_graph(PLAN_NODES)), project_id=PROJECT_ID)
    async with count_statements(db) as c:
        started = time.perf_counter()
        await write_plan(db, plan)
        elapsed = time.perf_counter() - started
    print(
        f"\nwrite_plan({PLAN_NODES} nodes) at {SEED_TASKS} tasks / "
        f"{SEED_EDGES} edges: {c['n']} statements, {elapsed:.2f}s"
    )
    budget = 3 * PLAN_NODES + 20
    assert c["n"] <= budget, f"{c['n']} statements > budget {budget}"
    assert elapsed <= 4.0, f"{elapsed:.2f}s > 4s"
    # ... and it actually linked them.
    assert (await db.get_task(plan.task_ids[0])).parent_task_id == plan.parent_id
