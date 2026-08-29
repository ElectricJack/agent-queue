"""task.ready — every entry into the frontier is recorded and emitted (spec §9)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from src.database import Database
from src.database.tables import events
from src.event_bus import EventBus
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
    await db.create_task(Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid,
                              status=status, **kw))


async def ready_rows(db, task_id):
    async with db._engine.begin() as conn:
        rows = (await conn.execute(select(events.c.payload).where(
            (events.c.event_type == "task.ready") & (events.c.task_id == task_id)))).fetchall()
    return [r[0] for r in rows]


class TestFrontierEntry:
    async def test_promotion_defined_to_ready_records_audit_row_in_transaction(self, db):
        await mktask(db, "a")
        seen = []
        async def listener(entries):  # list[tuple[task_id, reason]]
            seen.append(list(entries))

        db.set_ready_listener(listener)
        await db.transition_task("a", TaskStatus.READY, context="promotion")
        assert await ready_rows(db, "a") == ["promoted"]
        assert seen == [[("a", "promoted")]]

    async def test_ready_but_blocked_is_not_a_frontier_entry(self, db):
        await mktask(db, "dep")
        await mktask(db, "a")
        await db.add_dependency("a", "dep", "blocks")
        await db.transition_task("a", TaskStatus.READY)
        assert await ready_rows(db, "a") == []

    async def test_unblocking_a_ready_task_records_entry(self, db):
        await mktask(db, "dep", status=TaskStatus.READY)
        await mktask(db, "a", status=TaskStatus.READY)
        await db.add_dependency("a", "dep", "blocks")
        assert (await db.get_task("a")).is_blocked is True
        await db.transition_task("dep", TaskStatus.COMPLETED)
        assert await ready_rows(db, "a") == ["unblocked"]

    async def test_hold_removal_records_entry(self, db):
        await mktask(db, "a", status=TaskStatus.READY)
        await db.add_task_label("a", "hold:triage")
        entered = await db.remove_task_label("a", "hold:triage")
        assert entered == ["a"]
        assert await ready_rows(db, "a") == ["hold_removed"]

    async def test_same_status_write_does_not_double_record(self, db):
        await mktask(db, "a", status=TaskStatus.READY)
        await db.transition_task("a", TaskStatus.READY, priority=5)
        assert await ready_rows(db, "a") == []


class TestSettlementFrontierEntry:
    """Frontier entries produced by container settlement and gate/dep bypass
    callers must be recorded exactly once — see R1-R4 review fixes."""

    async def test_settle_via_add_dependency_unblocks_dependent_records_one_entry(self, db):
        await mktask(db, "container", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "child", status=TaskStatus.COMPLETED)
        await mktask(db, "d", status=TaskStatus.READY)
        await db.add_dependency("d", "container", "blocks")
        assert (await db.get_task("d")).is_blocked is True

        seen = []
        async def listener(entries):
            seen.append(list(entries))
        db.set_ready_listener(listener)

        await db.add_dependency("child", "container", "parent-child")

        assert (await db.get_task("container")).status == TaskStatus.COMPLETED
        assert await ready_rows(db, "d") == ["unblocked"]
        assert seen == [[("d", "unblocked")]]

    async def test_child_completion_settles_parent_and_unblocks_dependent(self, db):
        await mktask(db, "p", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "c0", status=TaskStatus.READY)
        await db.add_dependency("c0", "p", "parent-child")
        await mktask(db, "d", status=TaskStatus.READY)
        await db.add_dependency("d", "p", "blocks")
        assert (await db.get_task("d")).is_blocked is True

        seen = []
        async def listener(entries):
            seen.append(list(entries))
        db.set_ready_listener(listener)

        await db.transition_task("c0", TaskStatus.COMPLETED)

        assert (await db.get_task("p")).status == TaskStatus.COMPLETED
        assert await ready_rows(db, "d") == ["unblocked"]
        assert seen == [[("d", "unblocked")]]

    async def test_resolve_gate_unblocks_waiter_records_one_entry(self, db):
        await mktask(db, "d", status=TaskStatus.READY)
        gate_id, _created = await db.create_gate(
            PROJECT_ID, "human", "t", waiter_task_ids=["d"]
        )
        assert (await db.get_task("d")).is_blocked is True

        seen = []
        async def listener(entries):
            seen.append(list(entries))
        db.set_ready_listener(listener)

        await db.resolve_gate(gate_id, resolved_by="test")

        assert await ready_rows(db, "d") == ["unblocked"]
        assert seen == [[("d", "unblocked")]]

    async def test_same_status_projection_write_unblocks_conditional_dependent(self, db):
        await mktask(db, "a", status=TaskStatus.FAILED, retry_count=2, max_retries=3)
        await mktask(db, "d", status=TaskStatus.READY)
        await db.add_dependency("d", "a", "conditional-blocks")
        assert (await db.get_task("d")).is_blocked is True

        seen = []
        async def listener(entries):
            seen.append(list(entries))
        db.set_ready_listener(listener)

        await db.transition_task("a", TaskStatus.FAILED, retry_count=3)

        assert (await db.get_task("d")).is_blocked is False
        assert await ready_rows(db, "d") == ["unblocked"]
        assert seen == [[("d", "unblocked")]]


class TestWaitFor:
    async def test_wait_for_returns_matching_event(self):
        bus = EventBus()

        async def fire():
            await asyncio.sleep(0.01)
            await bus.emit("task.ready", {"task_id": "t", "project_id": "p", "title": "t"})

        asyncio.create_task(fire())
        got = await bus.wait_for(["task.ready"], filter={"project_id": "p"}, timeout=1.0)
        assert got["task_id"] == "t"
        assert bus.subscriber_count("task.ready") == 0

    async def test_wait_for_times_out(self):
        bus = EventBus()
        assert await bus.wait_for(["task.ready"], timeout=0.05) is None
