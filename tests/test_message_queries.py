"""Tests for ``MessageQueriesMixin`` — supervisor-agent §4 / §12 (Queries row).

Covers create/pending ordering, the ``mark_delivered`` compare-and-set,
archive semantics, and ``since`` filtering.
"""

from __future__ import annotations

import time

import pytest

from src.database import SQLiteDatabaseAdapter
from src.models import Message, Project


@pytest.fixture
async def db(tmp_path):
    database = SQLiteDatabaseAdapter(str(tmp_path / "messages.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="p1"))
    await database.create_project(Project(id="p2", name="p2"))
    yield database
    await database.close()


async def _send(db, **overrides):
    params = {
        "project_id": "p1",
        "from_kind": "user",
        "from_id": "discord:1",
        "to_kind": "session",
        "to_id": "supervisor-p1",
        "body": "hello",
    }
    params.update(overrides)
    return await db.create_message(**params)


class TestCreateAndGet:
    async def test_create_returns_populated_message(self, db):
        msg = await _send(db, subject="hi", thread_id="t1")
        assert isinstance(msg, Message)
        assert msg.id.startswith("msg-")
        assert msg.subject == "hi"
        assert msg.thread_id == "t1"
        assert msg.created_at > 0
        assert msg.delivered_at is None
        assert msg.read_at is None
        assert msg.archive_after_inject is False

    async def test_round_trip(self, db):
        msg = await _send(db, archive_after_inject=True, priority=5)
        fetched = await db.get_message(msg.id)
        assert fetched == msg
        assert fetched.archive_after_inject is True
        assert fetched.priority == 5

    async def test_get_missing_returns_none(self, db):
        assert await db.get_message("msg-nope") is None

    async def test_reply_to_id_links(self, db):
        first = await _send(db)
        second = await _send(db, reply_to_id=first.id)
        assert (await db.get_message(second.id)).reply_to_id == first.id


class TestPendingOrdering:
    async def test_priority_then_created_at(self, db):
        low = await _send(db, body="low", priority=200)
        high = await _send(db, body="high", priority=10)
        mid_a = await _send(db, body="a", priority=100)
        mid_b = await _send(db, body="b", priority=100)

        pending = await db.get_pending_messages("session", "supervisor-p1")
        assert [m.id for m in pending] == [high.id, mid_a.id, mid_b.id, low.id]

    async def test_delivered_rows_excluded(self, db):
        first = await _send(db)
        second = await _send(db)
        await db.mark_delivered(first.id)
        pending = await db.get_pending_messages("session", "supervisor-p1")
        assert [m.id for m in pending] == [second.id]

    async def test_archived_rows_excluded(self, db):
        msg = await _send(db)
        await db.archive_messages([msg.id])
        assert await db.get_pending_messages("session", "supervisor-p1") == []

    async def test_scoped_to_recipient(self, db):
        await _send(db, to_id="supervisor-p1")
        await _send(db, to_kind="task", to_id="some-task")
        pending = await db.get_pending_messages("session", "supervisor-p1")
        assert len(pending) == 1

    async def test_limit_is_applied(self, db):
        for _ in range(5):
            await _send(db)
        assert len(await db.get_pending_messages("session", "supervisor-p1", limit=2)) == 2


class TestMarkDelivered:
    async def test_first_call_claims_second_returns_false(self, db):
        """The compare-and-set idempotency guard (spec §4, §13)."""
        msg = await _send(db)
        assert await db.mark_delivered(msg.id) is True
        assert await db.mark_delivered(msg.id) is False

    async def test_second_call_does_not_overwrite_stamp_or_via(self, db):
        msg = await _send(db)
        await db.mark_delivered(msg.id, via="nudge")
        first = await db.get_message(msg.id)
        await db.mark_delivered(msg.id, via="transcript_tail")
        second = await db.get_message(msg.id)
        assert second.delivered_at == first.delivered_at
        assert second.via == "nudge"

    async def test_unknown_id_returns_false(self, db):
        assert await db.mark_delivered("msg-nope") is False

    async def test_via_is_persisted(self, db):
        msg = await _send(db)
        await db.mark_delivered(msg.id, via="transcript_tail")
        assert (await db.get_message(msg.id)).via == "transcript_tail"


class TestMarkRead:
    async def test_compare_and_set(self, db):
        msg = await _send(db)
        assert await db.mark_read(msg.id) is True
        assert await db.mark_read(msg.id) is False
        assert (await db.get_message(msg.id)).read_at is not None


class TestArchive:
    async def test_archives_once(self, db):
        a = await _send(db)
        b = await _send(db)
        assert await db.archive_messages([a.id, b.id]) == 2
        assert await db.archive_messages([a.id, b.id]) == 0

    async def test_empty_list_is_a_noop(self, db):
        assert await db.archive_messages([]) == 0

    async def test_archived_at_is_set(self, db):
        msg = await _send(db)
        await db.archive_messages([msg.id])
        assert (await db.get_message(msg.id)).archived_at is not None


class TestListMessages:
    async def test_newest_first(self, db):
        first = await _send(db, body="one")
        second = await _send(db, body="two")
        rows = await db.list_messages(project_id="p1")
        assert [m.id for m in rows] == [second.id, first.id]

    async def test_since_is_exclusive_lower_bound(self, db):
        """``since`` excludes rows at exactly that timestamp.

        Deliberately anchored on a row's own ``created_at`` rather than a
        second ``time.time()`` call: the Windows clock ticks about every
        15 ms, so two consecutive inserts routinely share a timestamp and a
        "created before/after now" assertion would be a coin flip.
        """
        old = await _send(db, body="old")
        assert [m.id for m in await db.list_messages(project_id="p1", since=0.0)] == [old.id]
        assert await db.list_messages(project_id="p1", since=old.created_at) == []
        assert await db.list_messages(project_id="p1", since=time.time() + 3600) == []

    async def test_project_filter(self, db):
        await _send(db, project_id="p1")
        other = await _send(db, project_id="p2")
        rows = await db.list_messages(project_id="p2")
        assert [m.id for m in rows] == [other.id]

    async def test_thread_filter(self, db):
        await _send(db, thread_id="t1")
        await _send(db, thread_id="t2")
        rows = await db.list_messages(thread_id="t1")
        assert len(rows) == 1

    async def test_archived_hidden_by_default(self, db):
        msg = await _send(db)
        await db.archive_messages([msg.id])
        assert await db.list_messages(project_id="p1") == []
        assert len(await db.list_messages(project_id="p1", include_archived=True)) == 1

    async def test_recipient_filter(self, db):
        await _send(db, to_kind="task", to_id="task-1")
        await _send(db, to_kind="session", to_id="supervisor-p1")
        rows = await db.list_messages(to_kind="task", to_id="task-1")
        assert len(rows) == 1

    async def test_limit(self, db):
        for _ in range(4):
            await _send(db)
        assert len(await db.list_messages(project_id="p1", limit=2)) == 2
