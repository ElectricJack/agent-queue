"""Tests for :class:`MessageDeliveryEngine` — supervisor-agent §5, §7, §11.1.

Uses the real SQLite adapter fixture (as :mod:`tests.test_message_queries`
does) plus an in-process :class:`FakeSessionManager` that implements
:class:`SessionManagerProto`. Covers each policy branch listed in
`.superpowers/sdd/task-2-brief.md` Step 1.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest
from sqlalchemy import update as sa_update

from src.config import MessagesConfig
from src.database import SQLiteDatabaseAdapter
from src.database.tables import messages
from src.messages.delivery import PARK_AFTER_SECONDS, MessageDeliveryEngine
from src.models import Project


# --------------------------------------------------------------------------
# Fixtures / fakes
# --------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    database = SQLiteDatabaseAdapter(str(tmp_path / "delivery.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="p1"))
    yield database
    await database.close()


@dataclass
class RecordedEvent:
    event: str
    payload: dict


class RecordingBus:
    """Minimal event bus stub — records ``emit`` calls."""

    def __init__(self):
        self.events: list[RecordedEvent] = []

    async def emit(self, event: str, payload: dict) -> None:
        self.events.append(RecordedEvent(event, dict(payload)))


@dataclass
class FakeSessionManager:
    """Dict-driven :class:`SessionManagerProto` for the delivery engine.

    - ``activity_map`` keyed by ``(kind, target_id, project_id)`` → Activity.
      Missing entries default to ``"absent"``.
    - ``nudges`` records each ``nudge()`` call as
      ``(kind, target_id, project_id, text)``.
    - ``nudge_returns`` optionally overrides the default ``True`` return.
    - ``ensure_started_calls`` records ``ensure_started`` invocations; when
      called, the manager flips the activity for that target to ``"idle"``
      (unless overridden by ``ensure_started_returns``).
    - ``tail_map`` keyed the same way as ``activity_map`` returns the tail.
    """

    activity_map: dict[tuple, str] = field(default_factory=dict)
    nudge_returns: bool = True
    ensure_started_returns: bool = True
    nudges: list[tuple] = field(default_factory=list)
    ensure_started_calls: list[tuple] = field(default_factory=list)
    tail_map: dict[tuple, str | None] = field(default_factory=dict)

    async def activity(self, *, kind, target_id, project_id):
        return self.activity_map.get((kind, target_id, project_id), "absent")

    async def ensure_started(self, *, kind, target_id, project_id):
        self.ensure_started_calls.append((kind, target_id, project_id))
        if self.ensure_started_returns:
            self.activity_map[(kind, target_id, project_id)] = "idle"
        return self.ensure_started_returns

    async def nudge(self, *, kind, target_id, project_id, text):
        self.nudges.append((kind, target_id, project_id, text))
        return self.nudge_returns

    async def tail_assistant_turn(self, *, kind, target_id, project_id, since):
        return self.tail_map.get((kind, target_id, project_id))


def make_engine(db, sessions, *, config=None, bus=None):
    return MessageDeliveryEngine(
        db=db,
        sessions=sessions,
        config=config or MessagesConfig(enabled=True),
        bus=bus,
    )


async def _send(db, **overrides):
    params = dict(
        project_id="p1",
        from_kind="user",
        from_id="discord:1",
        to_kind="session",
        to_id="supervisor-p1",
        body="hello",
    )
    params.update(overrides)
    return await db.create_message(**params)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestDeliveryPolicy:
    async def test_idle_supervisor_nudged_and_marked_delivered(self, db):
        sessions = FakeSessionManager(
            activity_map={("session", "supervisor-p1", "p1"): "idle"}
        )
        bus = RecordingBus()
        engine = make_engine(db, sessions, bus=bus)
        msg = await _send(db, subject="hi", body="world")

        result = await engine.run_delivery_pass()

        assert result == {"success": True, "delivered": 1, "skipped_busy": 0, "parked": 0}
        assert len(sessions.nudges) == 1
        text = sessions.nudges[0][3]
        assert "msg-" in text and msg.id in text
        assert "world" in text
        assert "aq reply" in text
        stored = await db.get_message(msg.id)
        assert stored.delivered_at is not None
        assert stored.via == "nudge"
        assert [e.event for e in bus.events] == ["message.delivered"]
        assert bus.events[0].payload["message_id"] == msg.id
        assert bus.events[0].payload["method"] == "nudge"
        assert bus.events[0].payload["project_id"] == "p1"

    async def test_busy_recipient_skipped(self, db):
        sessions = FakeSessionManager(
            activity_map={("session", "supervisor-p1", "p1"): "busy"}
        )
        bus = RecordingBus()
        engine = make_engine(db, sessions, bus=bus)
        msg = await _send(db)

        result = await engine.run_delivery_pass()

        assert result["skipped_busy"] == 1
        assert result["delivered"] == 0
        assert sessions.nudges == []
        assert (await db.get_message(msg.id)).delivered_at is None
        assert bus.events == []

    async def test_sleeping_supervisor_started_then_nudged(self, db):
        sessions = FakeSessionManager(
            activity_map={("session", "supervisor-p1", "p1"): "sleeping"}
        )
        engine = make_engine(db, sessions)
        msg = await _send(db)

        await engine.run_delivery_pass()

        assert sessions.ensure_started_calls == [("session", "supervisor-p1", "p1")]
        assert len(sessions.nudges) == 1
        assert (await db.get_message(msg.id)).delivered_at is not None

    async def test_sleeping_start_failure_leaves_pending(self, db):
        sessions = FakeSessionManager(
            activity_map={("session", "supervisor-p1", "p1"): "sleeping"},
            ensure_started_returns=False,
        )
        engine = make_engine(db, sessions)
        msg = await _send(db)

        await engine.run_delivery_pass()

        assert sessions.nudges == []
        assert (await db.get_message(msg.id)).delivered_at is None

    async def test_absent_task_recipient_pending_untouched(self, db):
        sessions = FakeSessionManager()  # empty map => absent
        engine = make_engine(db, sessions)
        msg = await _send(db, to_kind="task", to_id="task-1")

        result = await engine.run_delivery_pass()

        assert result == {"success": True, "delivered": 0, "skipped_busy": 0, "parked": 0}
        assert sessions.nudges == []
        assert (await db.get_message(msg.id)).delivered_at is None

    async def test_user_recipient_platform_delivered_and_message_sent(self, db):
        sessions = FakeSessionManager()
        bus = RecordingBus()
        engine = make_engine(db, sessions, bus=bus)
        msg = await _send(
            db, from_kind="session", from_id="supervisor-p1",
            to_kind="user", to_id="discord:1", body="a reply",
        )

        result = await engine.run_delivery_pass()

        assert result["delivered"] == 1
        stored = await db.get_message(msg.id)
        assert stored.via == "platform"
        assert stored.delivered_at is not None
        assert sessions.nudges == []
        assert [e.event for e in bus.events] == ["message.sent"]
        p = bus.events[0].payload
        assert p["message_id"] == msg.id
        assert p["to_kind"] == "user" and p["to_id"] == "discord:1"

    async def test_nudge_returns_false_leaves_pending(self, db):
        sessions = FakeSessionManager(
            activity_map={("session", "supervisor-p1", "p1"): "idle"},
            nudge_returns=False,
        )
        bus = RecordingBus()
        engine = make_engine(db, sessions, bus=bus)
        msg = await _send(db)

        await engine.run_delivery_pass()

        assert (await db.get_message(msg.id)).delivered_at is None
        assert bus.events == []

    async def test_cas_race_no_double_event(self, db):
        """Pre-marking one message means the engine's mark_delivered returns
        False and no ``message.delivered`` event is emitted for that row."""
        sessions = FakeSessionManager(
            activity_map={("session", "supervisor-p1", "p1"): "idle"}
        )
        bus = RecordingBus()
        engine = make_engine(db, sessions, bus=bus)
        first = await _send(db, body="one")
        second = await _send(db, body="two")

        # Simulate a racing claimant
        assert await db.mark_delivered(first.id, via="nudge") is True

        await engine.run_delivery_pass()

        # Both are still nudged (batch renders all pending), but only the
        # unclaimed one produces a message.delivered event.
        assert len(sessions.nudges) == 1  # both rendered in a single batch
        delivered_events = [e for e in bus.events if e.event == "message.delivered"]
        assert len(delivered_events) == 1
        assert delivered_events[0].payload["message_id"] == second.id

    async def test_batch_respects_max_inject(self, db):
        sessions = FakeSessionManager(
            activity_map={("session", "supervisor-p1", "p1"): "idle"}
        )
        cfg = MessagesConfig(enabled=True, max_inject_per_prompt=2)
        engine = make_engine(db, sessions, config=cfg)
        for i in range(4):
            await _send(db, body=f"m{i}")

        await engine.run_delivery_pass()

        # First pass takes 2. Second pass takes 2 more.
        assert len(sessions.nudges) == 1
        # Count msg- markers in the nudge text.
        text = sessions.nudges[0][3]
        assert text.count("[msg-") == 2

    async def test_priority_ordering_preserved_in_nudge(self, db):
        sessions = FakeSessionManager(
            activity_map={("session", "supervisor-p1", "p1"): "idle"}
        )
        engine = make_engine(db, sessions)
        low = await _send(db, body="low", priority=200)
        high = await _send(db, body="high", priority=10)

        await engine.run_delivery_pass()

        text = sessions.nudges[0][3]
        assert text.index(high.id) < text.index(low.id)


class TestParking:
    async def test_stale_session_message_parked_to_user(self, db):
        # Spec §7 line 463: the 24h park sweep applies ONLY to
        # ``to_kind="session"`` rows.
        sessions = FakeSessionManager()  # session absent
        bus = RecordingBus()
        engine = make_engine(db, sessions, bus=bus)
        msg = await _send(
            db,
            from_kind="user",
            from_id="discord:1",
            to_kind="session",
            to_id="s-task-1",  # plain (non-supervisor) name → absent
        )
        # Backdate the row past the park horizon.
        async with db._engine.begin() as conn:
            await conn.execute(
                sa_update(messages)
                .where(messages.c.id == msg.id)
                .values(created_at=time.time() - PARK_AFTER_SECONDS - 10)
            )

        result = await engine.run_delivery_pass()

        assert result["parked"] == 1
        original = await db.get_message(msg.id)
        assert original.archived_at is not None
        # A user-addressed copy referencing the original exists.
        copies = await db.list_messages(project_id="p1", to_kind="user")
        assert len(copies) == 1
        assert copies[0].reply_to_id == msg.id
        assert copies[0].to_id == "discord:1"

    async def test_task_recipient_never_parked_even_when_stale(self, db):
        # Task recipients ride into ``aq prime``; the delivery engine
        # must not park them, no matter how long they've sat.
        sessions = FakeSessionManager()  # task absent
        engine = make_engine(db, sessions)
        msg = await _send(
            db,
            from_kind="user",
            from_id="discord:1",
            to_kind="task",
            to_id="task-1",
        )
        async with db._engine.begin() as conn:
            await conn.execute(
                sa_update(messages)
                .where(messages.c.id == msg.id)
                .values(created_at=time.time() - PARK_AFTER_SECONDS - 10)
            )

        result = await engine.run_delivery_pass()

        assert result["parked"] == 0
        original = await db.get_message(msg.id)
        assert original.archived_at is None
        assert original.delivered_at is None


class TestProfileRecipients:
    async def test_profile_messages_untouched_by_pass(self, db):
        # ``to_kind="profile"`` is project-agnostic and consumed via
        # ``aq inbox``/prime — the delivery engine leaves it pending
        # regardless of age and never touches a session for it.
        sessions = FakeSessionManager()
        bus = RecordingBus()
        engine = make_engine(db, sessions, bus=bus)
        msg = await _send(
            db,
            from_kind="user",
            from_id="discord:1",
            to_kind="profile",
            to_id="supervisor",
        )
        # Age past the park horizon to prove it still isn't parked.
        async with db._engine.begin() as conn:
            await conn.execute(
                sa_update(messages)
                .where(messages.c.id == msg.id)
                .values(created_at=time.time() - PARK_AFTER_SECONDS - 10)
            )

        result = await engine.run_delivery_pass()

        assert result == {"success": True, "delivered": 0, "skipped_busy": 0, "parked": 0}
        assert sessions.nudges == []
        assert sessions.ensure_started_calls == []
        stored = await db.get_message(msg.id)
        assert stored.delivered_at is None
        assert stored.archived_at is None
        assert bus.events == []


class TestReplyTimeouts:
    async def test_transcript_tail_creates_reply(self, db):
        sessions = FakeSessionManager()
        bus = RecordingBus()
        engine = make_engine(db, sessions, bus=bus)
        msg = await _send(
            db,
            from_kind="user",
            from_id="discord:1",
            to_kind="session",
            to_id="supervisor-p1",
            thread_id="t-1",
        )
        # Deliver it via nudge path.
        sessions.activity_map[("session", "supervisor-p1", "p1")] = "idle"
        await engine.run_delivery_pass()
        # Now age its delivered_at past reply_timeout.
        async with db._engine.begin() as conn:
            await conn.execute(
                sa_update(messages)
                .where(messages.c.id == msg.id)
                .values(delivered_at=time.time() - 999)
            )
        sessions.tail_map[("session", "supervisor-p1", "p1")] = "an assistant reply"

        resolved = await engine.check_reply_timeouts()

        assert resolved == 1
        replies = await db.list_messages(project_id="p1", thread_id="t-1", to_kind="user")
        assert len(replies) == 1
        reply = replies[0]
        assert reply.reply_to_id == msg.id
        assert reply.via == "transcript_tail"
        assert reply.body == "an assistant reply"

    async def test_no_tail_no_reply(self, db):
        sessions = FakeSessionManager()
        engine = make_engine(db, sessions)
        msg = await _send(db, to_kind="session", to_id="supervisor-p1")
        sessions.activity_map[("session", "supervisor-p1", "p1")] = "idle"
        await engine.run_delivery_pass()
        async with db._engine.begin() as conn:
            await conn.execute(
                sa_update(messages)
                .where(messages.c.id == msg.id)
                .values(delivered_at=time.time() - 999)
            )
        # tail_map returns None → no reply created.
        assert await engine.check_reply_timeouts() == 0

    async def test_disabled_by_config(self, db):
        sessions = FakeSessionManager()
        cfg = MessagesConfig(enabled=True, transcript_tail_fallback=False)
        engine = make_engine(db, sessions, config=cfg)
        msg = await _send(db, to_kind="session", to_id="supervisor-p1")
        sessions.activity_map[("session", "supervisor-p1", "p1")] = "idle"
        await engine.run_delivery_pass()
        sessions.tail_map[("session", "supervisor-p1", "p1")] = "some text"
        async with db._engine.begin() as conn:
            await conn.execute(
                sa_update(messages)
                .where(messages.c.id == msg.id)
                .values(delivered_at=time.time() - 999)
            )
        assert await engine.check_reply_timeouts() == 0


class TestBusOptional:
    async def test_no_bus_no_error(self, db):
        sessions = FakeSessionManager(
            activity_map={("session", "supervisor-p1", "p1"): "idle"}
        )
        engine = make_engine(db, sessions, bus=None)
        await _send(db)
        result = await engine.run_delivery_pass()
        assert result["delivered"] == 1
