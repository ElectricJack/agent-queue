"""Source outbox atomicity, commit ordering, watermark and safe retention."""

import asyncio

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from src.database.queries.wait_source_queries import WaitSourceQueriesMixin

from src.database.tables import wait_deadlines, wait_listener_cursors, wait_records, wait_source_events
from src.models import Project
from src.waits.sources import payload_hash, record_source_event


@pytest.fixture
async def db(reuse_database):
    db = await reuse_database("wait-sources")
    await db.create_project(Project(id="p", name="P"))
    return db


def source(revision="1", **overrides):
    return {"source_key": "task:t", "generation": 0, "revision": revision,
            "outcome_ref": "task:t", "payload": {"status": "COMPLETED"}, **overrides}


async def test_revision_replay_and_rollback_are_atomic(db):
    first = await db.wait_source_commit(**source())
    assert first > 0
    assert await db.wait_source_commit(**source()) is None
    assert await db.wait_source_commit(**source(generation=1)) > first
    with pytest.raises(RuntimeError, match="abort"):
        async with db._engine.begin() as conn:
            await record_source_event(conn, **source("rollback"))
            raise RuntimeError("abort")
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(wait_source_events))).mappings().all()
    assert len(rows) == 2
    assert all(row["payload_hash"] == payload_hash(row["payload"]) for row in rows)
    assert {row["source_revision"] for row in rows} == {"1"}


async def test_watermark_cannot_pass_an_earlier_inflight_writer(db):
    # A transaction opened under the engine's stricter default must not keep
    # the failed try-lock query's old snapshot after the writer commits.
    db._engine = db._engine.execution_options(isolation_level="REPEATABLE READ")
    async with db._engine.connect() as first_conn:
        tx = await first_conn.begin()
        first = await record_source_event(first_conn, **source("first"))
        second = await db.wait_source_commit(**source("second"))
        assert second > first  # Independent source writers can commit concurrently.
        watermark = asyncio.create_task(db.wait_source_watermark())
        # Give the watermark reader an opportunity to reach its fence.
        await asyncio.sleep(0.05)
        assert not watermark.done()
        # The waiting reader must not queue a lock that blocks later writers.
        third = await asyncio.wait_for(db.wait_source_commit(**source("third")), 2)
        await tx.commit()
        assert await watermark == third


async def test_notifications_are_commit_only_and_replay_is_silent(db):
    received = []
    notified = asyncio.Event()

    def receive(_conn, _pid, _channel, payload):
        received.append(int(payload))
        notified.set()

    async with db._engine.connect() as listener:
        raw = await listener.get_raw_connection()
        driver = raw.driver_connection
        await driver.add_listener("aq_wait_source", receive)
        try:
            with pytest.raises(RuntimeError):
                async with db._engine.begin() as conn:
                    await record_source_event(conn, **source("rollback"))
                    assert not received
                    raise RuntimeError("abort")
            seq = await db.wait_source_commit(**source("committed"))
            await asyncio.wait_for(notified.wait(), 2)
            assert received == [seq]
            notified.clear()
            assert await db.wait_source_commit(**source("committed")) is None
            # A second committed event is a delivery barrier for the silent replay.
            second = await db.wait_source_commit(**source("barrier"))
            await asyncio.wait_for(notified.wait(), 2)
            assert received == [seq, second]
        finally:
            await driver.remove_listener("aq_wait_source", receive)


async def test_watermark_retention_keeps_unread_events_and_cursor_floor(db):
    old = 100.0
    first = await db.wait_source_commit(**source("old"))
    pending = await db.wait_source_commit(**source("pending"))
    unread = await db.wait_source_commit(**source("unread"))
    async with db._engine.begin() as conn:
        await conn.execute(update(wait_source_events).values(committed_at=old))
        await conn.execute(update(wait_source_events).where(
            wait_source_events.c.seq.in_([first, unread]),
        ).values(dispatched_at=old))
        await conn.execute(insert(wait_listener_cursors).values(
            listener_key="source", last_seq=pending, updated_at=old,
        ))
    assert await db.purge_wait_source_events(now=old + 7 * 86400) == 0
    assert await db.purge_wait_source_events(now=old + 7 * 86400 + 1) == 1
    async with db._engine.connect() as conn:
        assert list((await conn.execute(select(wait_source_events.c.seq)
                                      .order_by(wait_source_events.c.seq))).scalars()) == [pending, unread]
    assert await db.wait_source_watermark(source_key="absent") == pending
    assert await db.wait_source_watermark() == unread
    async with db._engine.begin() as conn:
        await conn.execute(update(wait_source_events).values(dispatched_at=old))
        await conn.execute(update(wait_listener_cursors).values(last_seq=unread))
    assert await db.purge_wait_source_events(now=old + 7 * 86400 + 1) == 2
    # Reconstruct the reader with a fresh pool: retention must not erase the
    # durable source floor or allow a restarted listener to rewind its cursor.
    restarted = WaitSourceQueriesMixin()
    restarted._engine = create_async_engine(db._engine.url)
    try:
        assert await restarted.wait_source_watermark() == unread
        assert await restarted.wait_source_commit(**source("after-restart")) > unread
    finally:
        await restarted._engine.dispose()


async def timer(db, *, generation=1, deadline_type="due", cancelled_at=None):
    async with db._engine.begin() as conn:
        if generation == 1:
            await conn.execute(insert(wait_records).values(
                id="w", project_id="p", owner_kind="task", owner_id="owner", origin="agent",
                condition_kind="timer", condition={"due_at": 10}, source_key="timer:w",
                source_generation=0, source_watermark_seq=0, verification_state="verified",
                evidence_ref="timer:w", verified_at=1, created_at=1, request_hash="hash",
            ))
        await conn.execute(insert(wait_deadlines).values(
            id=f"d-{generation}", wait_id="w", deadline_type=deadline_type, generation=generation,
            due_at=10, cancelled_at=cancelled_at,
        ))


async def test_timer_firing_is_cas_fenced_and_rolls_back_with_its_event(db):
    await timer(db)
    assert await db.fire_wait_due_deadline("w", 1, now=9) is None
    with pytest.raises(RuntimeError):
        async with db._engine.begin() as conn:
            assert await db.fire_wait_due_deadline("w", 1, now=11, conn=conn)
            raise RuntimeError("abort")
    assert await db.fire_wait_due_deadline("w", 1, now=11)
    assert await db.fire_wait_due_deadline("w", 1, now=12) is None
    async with db._engine.connect() as conn:
        event = (await conn.execute(select(wait_source_events))).mappings().one()
    assert event["source_key"] == "timer:w" and event["occurred_at"] == 10


@pytest.mark.parametrize("case", ["cancelled", "superseded", "escalation"])
async def test_cancelled_stale_and_escalation_deadlines_emit_no_timer_event(db, case):
    await timer(db, cancelled_at=5 if case == "cancelled" else None,
                deadline_type="escalation" if case == "escalation" else "due")
    if case == "superseded":
        await timer(db, generation=2)
    assert await db.fire_wait_due_deadline("w", 1, now=11) is None
    assert await db.wait_source_watermark() == 0
