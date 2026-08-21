"""Merge slot lease — atomic acquire/renew/release/break.

Worktree-execution implementation spec §5 / design §4.1: one integration
lease per project, seeded lazily, atomic conditional UPDATE, TTL-fenced
so a crashed holder cannot starve integration forever.

These tests hit real SQLite (no mocking) — the atomicity guarantees only
hold at the SQL level, and mocking would not observe them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.database import Database
from src.models import Project
from src.orchestrator.merge_slot import (
    acquire_merge_slot,
    break_expired_merge_slots,
    release_merge_slot,
    renew_merge_slot,
)


class RecordingBus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


@pytest.fixture
async def db(tmp_path: Path):
    d = Database(str(tmp_path / "aq.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="alpha"))
    await d.create_project(Project(id="p2", name="beta"))
    try:
        yield d
    finally:
        await d.close()


# ── acquire / release ───────────────────────────────────────────────────


async def test_concurrent_acquire_exactly_one_winner(db):
    """Two concurrent acquires on one project: only one wins."""
    results = await asyncio.gather(
        acquire_merge_slot(db, "p1", "task-A", ttl=60),
        acquire_merge_slot(db, "p1", "task-B", ttl=60),
    )
    assert sorted(results) == [False, True]


async def test_holder_reacquire_is_idempotent_and_renews(db):
    """The current holder can re-acquire — it extends the lease."""
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    # Read the lease
    from src.database.tables import merge_slots
    from sqlalchemy import select

    async with db._engine.begin() as conn:
        row1 = (
            await conn.execute(select(merge_slots).where(merge_slots.c.project_id == "p1"))
        ).mappings().fetchone()
    exp1 = row1["expires_at"]

    await asyncio.sleep(0.05)
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=120) is True

    async with db._engine.begin() as conn:
        row2 = (
            await conn.execute(select(merge_slots).where(merge_slots.c.project_id == "p1"))
        ).mappings().fetchone()
    assert row2["expires_at"] > exp1
    assert row2["holder_task_id"] == "task-A"


async def test_non_holder_acquire_against_live_lease_fails(db):
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    assert await acquire_merge_slot(db, "p1", "task-B", ttl=60) is False


async def test_expired_lease_is_stealable(db):
    # 0-ttl lease expires immediately.
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=0) is True
    await asyncio.sleep(0.01)
    assert await acquire_merge_slot(db, "p1", "task-B", ttl=60) is True

    from src.database.tables import merge_slots
    from sqlalchemy import select

    async with db._engine.begin() as conn:
        row = (
            await conn.execute(select(merge_slots).where(merge_slots.c.project_id == "p1"))
        ).mappings().fetchone()
    assert row["holder_task_id"] == "task-B"


async def test_release_by_holder_frees_slot(db):
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    await release_merge_slot(db, "p1", "task-A")
    assert await acquire_merge_slot(db, "p1", "task-B", ttl=60) is True


async def test_release_is_idempotent_and_non_holder_release_is_noop(db):
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    # double release
    await release_merge_slot(db, "p1", "task-A")
    await release_merge_slot(db, "p1", "task-A")
    # non-holder release does not free A's original hold — but slot is already free.
    # Re-acquire, then let B try to release.  A stays holder.
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    await release_merge_slot(db, "p1", "task-B")
    # A still holds; B cannot acquire.
    assert await acquire_merge_slot(db, "p1", "task-B", ttl=60) is False


async def test_renew_only_by_holder(db):
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    assert await renew_merge_slot(db, "p1", "task-A", ttl=120) is True
    assert await renew_merge_slot(db, "p1", "task-B", ttl=120) is False


# ── break_expired ───────────────────────────────────────────────────────


async def test_break_expired_clears_only_expired(db):
    bus = RecordingBus()
    # p1 has an expired lease, p2 a live one.
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=0) is True
    assert await acquire_merge_slot(db, "p2", "task-B", ttl=60) is True
    await asyncio.sleep(0.02)

    count = await break_expired_merge_slots(db, bus)
    assert count == 1

    # p1's slot is now free; p2's still held.
    assert await acquire_merge_slot(db, "p1", "task-C", ttl=60) is True
    assert await acquire_merge_slot(db, "p2", "task-C", ttl=60) is False


async def test_break_expired_returns_zero_when_none_expired(db):
    bus = RecordingBus()
    assert await acquire_merge_slot(db, "p1", "task-A", ttl=60) is True
    assert await break_expired_merge_slots(db, bus) == 0


# ── persistence ──────────────────────────────────────────────────────────


async def test_slot_state_survives_database_reopen(tmp_path):
    d = Database(str(tmp_path / "aq.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="alpha"))
    assert await acquire_merge_slot(d, "p1", "task-A", ttl=60) is True
    await d.close()

    d2 = Database(str(tmp_path / "aq.db"))
    await d2.initialize()
    try:
        # A different task cannot acquire — original row persisted.
        assert await acquire_merge_slot(d2, "p1", "task-B", ttl=60) is False
        # Original holder can renew.
        assert await renew_merge_slot(d2, "p1", "task-A", ttl=120) is True
    finally:
        await d2.close()
