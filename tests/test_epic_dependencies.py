"""Declared epic dependencies order batches and defer unsatisfied members."""

from __future__ import annotations

import pytest
from sqlalchemy import insert

from src.database import Database
from src.database.tables import epic_dependencies
from src.integration.epic_dependencies import declare, dependencies_for, order_members
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db():
    database = Database(lease_dsn("epic-dependencies.db"))
    await database.initialize()
    yield database
    await database.close()


def _member(task_id: str) -> dict:
    return {"task_id": task_id, "source_head": f"sha-{task_id}"}


async def test_declare_is_idempotent(db):
    async with db.immediate() as conn:
        await declare(conn, dependent_task_id="e2", dependency_task_id="e1", now=1.0)
        await declare(conn, dependent_task_id="e2", dependency_task_id="e1", now=2.0)
        edges = await dependencies_for(conn, ["e2"])
    assert edges == {"e2": {"e1"}}


async def test_dependencies_for_returns_only_requested_tasks(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(epic_dependencies),
            [
                {"dependent_task_id": "e2", "dependency_task_id": "e1", "declared_at": 1.0},
                {"dependent_task_id": "e9", "dependency_task_id": "e8", "declared_at": 1.0},
            ],
        )
        edges = await dependencies_for(conn, ["e2"])
        empty = await dependencies_for(conn, [])
    assert edges == {"e2": {"e1"}}
    assert empty == {}


def test_a_dependency_is_ordered_before_its_dependent():
    ordered, deferred = order_members(
        [_member("e2"), _member("e1")], {"e2": {"e1"}}, delivered=set()
    )
    assert [m["task_id"] for m in ordered] == ["e1", "e2"]
    assert deferred == []


def test_independent_epics_keep_lexicographic_order():
    ordered, deferred = order_members(
        [_member("b"), _member("a"), _member("c")], {}, delivered=set()
    )
    assert [m["task_id"] for m in ordered] == ["a", "b", "c"]
    assert deferred == []


def test_newly_ready_epic_precedes_a_later_independent_epic():
    ordered, deferred = order_members(
        [_member("c"), _member("b"), _member("a")],
        {"b": {"a"}},
        delivered=set(),
    )
    assert [m["task_id"] for m in ordered] == ["a", "b", "c"]
    assert deferred == []


def test_an_epic_whose_dependency_is_absent_is_deferred():
    ordered, deferred = order_members([_member("e2")], {"e2": {"e1"}}, delivered=set())
    assert ordered == []
    assert [m["task_id"] for m in deferred] == ["e2"]


def test_a_dependency_already_on_main_does_not_defer():
    ordered, deferred = order_members([_member("e2")], {"e2": {"e1"}}, delivered={"e1"})
    assert [m["task_id"] for m in ordered] == ["e2"]
    assert deferred == []


def test_a_dependent_of_a_deferred_epic_is_also_deferred():
    ordered, deferred = order_members(
        [_member("e2"), _member("e3")],
        {"e2": {"e1"}, "e3": {"e2"}},
        delivered=set(),
    )
    assert ordered == []
    assert [m["task_id"] for m in deferred] == ["e2", "e3"]


def test_a_dependency_cycle_defers_the_cycle_and_its_dependents():
    ordered, deferred = order_members(
        [_member("e1"), _member("e2"), _member("e3"), _member("a")],
        {"e1": {"e2"}, "e2": {"e1"}, "e3": {"e2"}},
        delivered=set(),
    )
    assert [m["task_id"] for m in ordered] == ["a"]
    assert [m["task_id"] for m in deferred] == ["e1", "e2", "e3"]
