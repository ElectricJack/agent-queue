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
        sessions = FakeSessionManager(activity_map={("session", "supervisor-p1", "p1"): "idle"})
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
        sessions = FakeSessionManager(activity_map={("session", "supervisor-p1", "p1"): "busy"})
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
        sessions = FakeSessionManager(activity_map={("session", "supervisor-p1", "p1"): "sleeping"})
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
            db,
            from_kind="session",
            from_id="supervisor-p1",
            to_kind="user",
            to_id="discord:1",
            body="a reply",
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
        sessions = FakeSessionManager(activity_map={("session", "supervisor-p1", "p1"): "idle"})
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
        sessions = FakeSessionManager(activity_map={("session", "supervisor-p1", "p1"): "idle"})
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

    async def test_unknown_recipient_kind_is_left_pending_without_session_calls(self, db):
        """A defensive to_kind the engine doesn't route (not in today's
        MESSAGE_TO_KINDS routing) must be left pending: no activity probe,
        no start, no nudge, no mark_delivered — and no exception.

        The real table CHECK-constrains to_kind, so the unknown kind is
        injected through a DB double rather than a stored row.
        """
        from src.models import Message

        pending_row = Message(
            id="msg-unknown",
            project_id="p1",
            from_kind="user",
            from_id="discord:1",
            to_kind="webhook",
            to_id="w1",
            body="who routes this?",
            created_at=time.time(),
        )

        class _UnknownRecipientDB:
            def __init__(self):
                self.marked: list = []
                self.archived: list = []

            async def get_pending_recipients(self):
                return [("webhook", "w1", "p1")]

            async def get_pending_messages(self, to_kind, to_id, limit):
                assert (to_kind, to_id) == ("webhook", "w1")
                return [pending_row]

            async def mark_delivered(self, message_id, via=None):
                self.marked.append((message_id, via))
                return True

            async def archive_messages(self, ids):
                self.archived.append(list(ids))

        class _NeverCalledSessions:
            async def activity(self, **kwargs):
                raise AssertionError(f"activity() called for unknown kind: {kwargs}")

            async def ensure_started(self, **kwargs):
                raise AssertionError(f"ensure_started() called for unknown kind: {kwargs}")

            async def nudge(self, **kwargs):
                raise AssertionError(f"nudge() called for unknown kind: {kwargs}")

            async def tail_assistant_turn(self, **kwargs):
                raise AssertionError(f"tail_assistant_turn() called: {kwargs}")

        double = _UnknownRecipientDB()
        bus = RecordingBus()
        engine = make_engine(double, _NeverCalledSessions(), bus=bus)

        result = await engine.run_delivery_pass()

        assert result == {"success": True, "delivered": 0, "skipped_busy": 0, "parked": 0}
        assert double.marked == []
        assert double.archived == []
        assert bus.events == []

    async def test_priority_ordering_preserved_in_nudge(self, db):
        sessions = FakeSessionManager(activity_map={("session", "supervisor-p1", "p1"): "idle"})
        engine = make_engine(db, sessions)
        low = await _send(db, body="low", priority=200)
        high = await _send(db, body="high", priority=10)

        await engine.run_delivery_pass()

        text = sessions.nudges[0][3]
        assert text.index(high.id) < text.index(low.id)


class TestParking:
    async def test_stale_system_sender_is_archived_without_creating_user_notice(self, db):
        sessions = FakeSessionManager()
        bus = RecordingBus()
        engine = make_engine(db, sessions, bus=bus)
        msg = await _send(
            db, from_kind="system", from_id="delivery-engine", to_id="retired-session"
        )
        async with db._engine.begin() as conn:
            await conn.execute(
                sa_update(messages)
                .where(messages.c.id == msg.id)
                .values(created_at=time.time() - PARK_AFTER_SECONDS - 1)
            )

        assert (await engine.run_delivery_pass())["parked"] == 1
        assert (await db.get_message(msg.id)).archived_at is not None
        assert await db.list_messages(project_id="p1", to_kind="user") == []
        assert bus.events == []

    async def test_fresh_absent_session_is_not_parked_and_remains_pending(self, db):
        engine = make_engine(db, FakeSessionManager())
        msg = await _send(db, to_id="retired-session")

        assert (await engine.run_delivery_pass())["parked"] == 0
        stored = await db.get_message(msg.id)
        assert stored.archived_at is None
        assert stored.delivered_at is None

    async def test_profile_and_task_recipients_are_never_parked_in_mixed_stale_pass(self, db):
        engine = make_engine(db, FakeSessionManager())
        profile = await _send(db, to_kind="profile", to_id="supervisor")
        task = await _send(db, to_kind="task", to_id="t1")
        session = await _send(db, to_kind="session", to_id="retired-session")
        async with db._engine.begin() as conn:
            await conn.execute(
                sa_update(messages)
                .where(messages.c.id.in_([profile.id, task.id, session.id]))
                .values(created_at=time.time() - PARK_AFTER_SECONDS - 1)
            )

        assert (await engine.run_delivery_pass())["parked"] == 1
        assert (await db.get_message(profile.id)).archived_at is None
        assert (await db.get_message(task.id)).archived_at is None
        assert (await db.get_message(session.id)).archived_at is not None

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
    async def test_existing_reply_prevents_tail_query_and_duplicate_reply(self, db):
        sessions = FakeSessionManager()
        engine = make_engine(db, sessions)
        original = await _send(db)
        await db.mark_delivered(original.id, via="nudge")
        await db.create_message(
            project_id="p1",
            from_kind="session",
            from_id="supervisor-p1",
            to_kind="user",
            to_id="discord:1",
            body="already replied",
            reply_to_id=original.id,
        )
        sessions.tail_assistant_turn = None  # type: ignore[assignment]

        assert await engine.check_reply_timeouts() == 0
        assert len(await db.list_messages(project_id="p1", to_kind="user")) == 1

    async def test_tail_reply_to_session_sender_is_not_fanned_out_as_platform_message(self, db):
        """Redesign of the approved plan's item 7: the requested
        "profile-originated timed-out message" cannot exist — ``messages.
        from_kind`` is CHECK-constrained (``ck_messages_from_kind``) to
        ``{session, user, system}``, so ``from_kind="profile"`` is
        unrepresentable.  The contract the item was after is the general
        one: a transcript-tail reply addressed back to a *non-user*
        sender must emit ``message.replied`` only — ``message.sent`` is
        the platform fan-out envelope and firing it here would make
        Discord render an agent-to-agent reply as if it were addressed
        to the user.
        """
        sessions = FakeSessionManager(tail_map={("task", "review", "p1"): "worker tail reply"})
        bus = RecordingBus()
        engine = make_engine(db, sessions, bus=bus)
        original = await _send(
            db,
            from_kind="session",
            from_id="s-owner",
            to_kind="task",
            to_id="review",
            thread_id="t-a2a",
        )
        await db.mark_delivered(original.id, via="prime")
        async with db._engine.begin() as conn:
            await conn.execute(
                sa_update(messages)
                .where(messages.c.id == original.id)
                .values(delivered_at=time.time() - 999)
            )

        assert await engine.check_reply_timeouts() == 1

        replies = await db.list_messages(project_id="p1", to_kind="session")
        assert len(replies) == 1
        reply = replies[0]
        assert reply.reply_to_id == original.id
        assert reply.to_kind == "session"
        assert reply.to_id == "s-owner"
        assert reply.via == "transcript_tail"
        # message.replied only — never the platform message.sent envelope.
        assert [e.event for e in bus.events] == ["message.replied"]
        assert bus.events[0].payload["reply_id"] == reply.id
        assert await db.list_messages(project_id="p1", to_kind="user") == []

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


class TestTranscriptTailDeduplication:
    @pytest.mark.parametrize("project_id", ["p1", None])
    @pytest.mark.parametrize("delivery_times", [(100, 101, 102), (102, 100, 101)])
    async def test_backlog_reuses_one_reply_across_sweeps_and_restart(
        self, db, project_id, delivery_times,
    ):
        sessions = FakeSessionManager(tail_map={("task", "review", project_id): "Review updated."})
        bus = RecordingBus()
        backlog = [await _send(db, project_id=project_id, to_kind="task", to_id="review")
                   for _ in range(3)]
        newest = backlog[delivery_times.index(102)]
        oldest = backlog[delivery_times.index(100)]
        async with db._engine.begin() as conn:
            for i, msg in enumerate(backlog):
                await conn.execute(sa_update(messages).where(messages.c.id == msg.id)
                                   .values(delivered_at=delivery_times[i]))

        assert await make_engine(db, sessions, bus=bus).check_reply_timeouts() == 1
        first = (await db.list_messages(to_kind="user"))[0]
        assert first.reply_to_id == newest.id
        # Forget process state, archive the original reply and its request,
        # and push them beyond the old 200-row history scan.
        await db.archive_messages([first.id, newest.id])
        for _ in range(201):
            await _send(db, to_kind="profile", to_id="unrelated")
        restarted = make_engine(db, sessions, bus=bus)
        for _ in range(3):
            assert await restarted.check_reply_timeouts() == 0
        replies = await db.list_messages(to_kind="user", include_archived=True)
        assert [r.id for r in replies] == [first.id]
        assert len([e for e in bus.events if e.event == "message.sent"]) == 1
        # No incoming feedback is discarded or marked read by suppression.
        assert (await db.get_message(oldest.id)).archived_at is None
        assert (await db.get_message(oldest.id)).read_at is None

    async def test_new_request_can_receive_the_same_text(self, db):
        sessions = FakeSessionManager(tail_map={("task", "review", "p1"): "Still waiting."})
        first = await _send(db, to_kind="task", to_id="review")
        async with db._engine.begin() as conn:
            await conn.execute(sa_update(messages).where(messages.c.id == first.id)
                               .values(delivered_at=100))
        assert await make_engine(db, sessions).check_reply_timeouts() == 1
        later = await _send(db, to_kind="task", to_id="review")
        async with db._engine.begin() as conn:
            await conn.execute(sa_update(messages).where(messages.c.id == later.id)
                               .values(delivered_at=200))
        assert await make_engine(db, sessions).check_reply_timeouts() == 1
        replies = await db.list_messages(to_kind="user")
        assert {r.reply_to_id for r in replies} == {first.id, later.id}

    @pytest.mark.parametrize("different", [
        {"from_id": "discord:2"}, {"thread_id": "discord:other"},
        {"to_id": "other-worker"}, {"project_id": "p2"},
    ])
    async def test_fallback_does_not_suppress_other_conversations(self, db, different):
        await db.create_project(Project(id="p2", name="p2"))
        sessions = FakeSessionManager()
        first = await _send(db, to_kind="task", to_id="review", thread_id="discord:channel")
        other = await _send(db, **({"to_kind": "task", "to_id": "review",
                                   "thread_id": "discord:channel"} | different))
        async with db._engine.begin() as conn:
            await conn.execute(sa_update(messages).where(messages.c.id.in_([first.id, other.id]))
                               .values(delivered_at=100))
        for msg in [first, other]:
            sessions.tail_map[(msg.to_kind, msg.to_id, msg.project_id)] = "Same reply text."
        assert await make_engine(db, sessions).check_reply_timeouts() == 2
        replies = await db.list_messages(to_kind="user")
        assert {r.reply_to_id for r in replies} == {first.id, other.id}


class TestTranscriptTailFanout:
    async def test_tail_reply_emits_message_sent_with_full_payload(self, db):
        sessions = FakeSessionManager()
        bus = RecordingBus()
        engine = make_engine(db, sessions, bus=bus)
        msg = await _send(
            db,
            from_kind="user",
            from_id="discord:1",
            to_kind="session",
            to_id="supervisor-p1",
            thread_id="discord:chan-1:9",
            subject="hi",
        )
        sessions.activity_map[("session", "supervisor-p1", "p1")] = "idle"
        await engine.run_delivery_pass()
        async with db._engine.begin() as conn:
            await conn.execute(
                sa_update(messages)
                .where(messages.c.id == msg.id)
                .values(delivered_at=time.time() - 999)
            )
        sessions.tail_map[("session", "supervisor-p1", "p1")] = "tail body"
        bus.events.clear()

        resolved = await engine.check_reply_timeouts()

        assert resolved == 1
        sent = [e for e in bus.events if e.event == "message.sent"]
        replied = [e for e in bus.events if e.event == "message.replied"]
        assert len(sent) == 1
        assert len(replied) == 1
        p = sent[0].payload
        # Payload must carry the envelope Discord's _on_message_sent needs
        # to route the message to the originating thread.
        assert p["project_id"] == "p1"
        assert p["from_kind"] == "session"
        assert p["from_id"] == "supervisor-p1"
        assert p["to_kind"] == "user"
        assert p["to_id"] == "discord:1"
        assert p["thread_id"] == "discord:chan-1:9"
        # The reply row (not the original) is what got sent.
        replies = await db.list_messages(project_id="p1", to_kind="user")
        assert p["message_id"] == replies[0].id

    async def test_tail_reply_no_message_sent_when_cas_loses(self, db, monkeypatch):
        sessions = FakeSessionManager()
        bus = RecordingBus()
        engine = make_engine(db, sessions, bus=bus)
        msg = await _send(
            db,
            from_kind="user",
            from_id="discord:1",
            to_kind="session",
            to_id="supervisor-p1",
            thread_id="t-x",
        )
        sessions.activity_map[("session", "supervisor-p1", "p1")] = "idle"
        await engine.run_delivery_pass()
        async with db._engine.begin() as conn:
            await conn.execute(
                sa_update(messages)
                .where(messages.c.id == msg.id)
                .values(delivered_at=time.time() - 999)
            )
        sessions.tail_map[("session", "supervisor-p1", "p1")] = "tail body"
        bus.events.clear()

        # Force the CAS on the newly-created reply to lose.
        original_mark = db.mark_delivered

        async def losing_mark(message_id, via):
            if via == "transcript_tail":
                return False
            return await original_mark(message_id, via=via)

        monkeypatch.setattr(db, "mark_delivered", losing_mark)

        resolved = await engine.check_reply_timeouts()

        assert resolved == 0
        assert [e.event for e in bus.events] == []  # no replied, no sent

    async def test_tail_dedupe_key_distinguishes_projects(self, db):
        # Two projects, same thread_id — both tails should be recovered.
        await db.create_project(Project(id="p2", name="p2"))
        sessions = FakeSessionManager()
        bus = RecordingBus()
        engine = make_engine(db, sessions, bus=bus)

        m1 = await _send(
            db,
            project_id="p1",
            to_kind="session",
            to_id="supervisor-p1",
            thread_id="shared-thread",
        )
        m2 = await _send(
            db,
            project_id="p2",
            to_kind="session",
            to_id="supervisor-p2",
            thread_id="shared-thread",
        )
        sessions.activity_map[("session", "supervisor-p1", "p1")] = "idle"
        sessions.activity_map[("session", "supervisor-p2", "p2")] = "idle"
        await engine.run_delivery_pass()
        async with db._engine.begin() as conn:
            await conn.execute(
                sa_update(messages)
                .where(messages.c.id.in_([m1.id, m2.id]))
                .values(delivered_at=time.time() - 999)
            )
        sessions.tail_map[("session", "supervisor-p1", "p1")] = "p1 tail"
        sessions.tail_map[("session", "supervisor-p2", "p2")] = "p2 tail"

        resolved = await engine.check_reply_timeouts()

        assert resolved == 2
        p1_replies = await db.list_messages(project_id="p1", to_kind="user")
        p2_replies = await db.list_messages(project_id="p2", to_kind="user")
        assert len(p1_replies) == 1 and p1_replies[0].body == "p1 tail"
        assert len(p2_replies) == 1 and p2_replies[0].body == "p2 tail"


class TestBusOptional:
    async def test_no_bus_no_error(self, db):
        sessions = FakeSessionManager(activity_map={("session", "supervisor-p1", "p1"): "idle"})
        engine = make_engine(db, sessions, bus=None)
        await _send(db)
        result = await engine.run_delivery_pass()
        assert result["delivered"] == 1


# --------------------------------------------------------------------------
# Task 3: cascade wiring — Orchestrator._deliver_messages()
# --------------------------------------------------------------------------


class _FakeEngine:
    """Records calls to ``run_delivery_pass`` / ``check_reply_timeouts``.

    Optional ``raise_on_pass`` makes ``run_delivery_pass`` raise so the
    cascade's try/except is exercised.
    """

    def __init__(self, raise_on_pass: bool = False):
        self.pass_calls: int = 0
        self.timeout_calls: int = 0
        self.raise_on_pass = raise_on_pass

    async def run_delivery_pass(self):
        self.pass_calls += 1
        if self.raise_on_pass:
            raise RuntimeError("boom")
        return {"success": True, "delivered": 0, "skipped_busy": 0, "parked": 0}

    async def check_reply_timeouts(self):
        self.timeout_calls += 1
        return 0


class _FakeMessagesConfig:
    def __init__(self, enabled: bool, delivery_interval: float = 5.0):
        self.enabled = enabled
        self.delivery_interval = delivery_interval


class _FakeOrchConfig:
    def __init__(self, enabled: bool, delivery_interval: float = 5.0):
        self.messages = _FakeMessagesConfig(enabled, delivery_interval)


class _StubOrch:
    """Minimum surface :meth:`Orchestrator._deliver_messages` reads.

    We import the method off the real class and bind it to this stub so we
    can exercise the throttle + gate + try/except contract without paying
    the full ``Orchestrator.__init__`` cost.
    """

    def __init__(self, *, enabled: bool, engine: _FakeEngine, interval: float = 5.0):
        self.config = _FakeOrchConfig(enabled, interval)
        self.message_delivery = engine
        self._last_delivery_pass: float = 0.0


class TestCascadeWiring:
    async def test_disabled_flag_never_calls_engine(self):
        from src.orchestrator.core import Orchestrator

        engine = _FakeEngine()
        stub = _StubOrch(enabled=False, engine=engine)
        await Orchestrator._deliver_messages(stub)
        assert engine.pass_calls == 0
        assert engine.timeout_calls == 0

    async def test_enabled_first_call_runs_pass_and_timeouts(self):
        from src.orchestrator.core import Orchestrator

        engine = _FakeEngine()
        stub = _StubOrch(enabled=True, engine=engine, interval=5.0)
        await Orchestrator._deliver_messages(stub)
        assert engine.pass_calls == 1
        assert engine.timeout_calls == 1
        assert stub._last_delivery_pass > 0

    async def test_throttle_skips_within_interval(self):
        from src.orchestrator.core import Orchestrator

        engine = _FakeEngine()
        stub = _StubOrch(enabled=True, engine=engine, interval=60.0)
        await Orchestrator._deliver_messages(stub)
        await Orchestrator._deliver_messages(stub)
        await Orchestrator._deliver_messages(stub)
        assert engine.pass_calls == 1  # subsequent calls throttled out

    async def test_throttle_fires_again_after_interval(self):
        from src.orchestrator.core import Orchestrator

        engine = _FakeEngine()
        stub = _StubOrch(enabled=True, engine=engine, interval=5.0)
        await Orchestrator._deliver_messages(stub)
        # Pretend enough wall-clock elapsed.
        stub._last_delivery_pass = time.time() - 10.0
        await Orchestrator._deliver_messages(stub)
        assert engine.pass_calls == 2
        assert engine.timeout_calls == 2

    async def test_engine_exception_does_not_propagate(self):
        from src.orchestrator.core import Orchestrator

        engine = _FakeEngine(raise_on_pass=True)
        stub = _StubOrch(enabled=True, engine=engine, interval=5.0)
        # Must not raise: the cascade's try/except swallows delivery
        # failures so one bad pass cannot break the 5s cycle.
        await Orchestrator._deliver_messages(stub)
        assert engine.pass_calls == 1
        # ``check_reply_timeouts`` is skipped when the pass raises — the
        # single try/except wraps both, matching the spec §5 policy that a
        # delivery failure is a single logical unit.
        assert engine.timeout_calls == 0


async def test_internal_question_handoff_does_not_generate_user_transcript_reply(db):
    msg = await db.create_message(project_id=None, from_kind="system", from_id="agent-questions",
        to_kind="session", to_id="n-supervisor--global", body="Untrusted worker question",
        body_kind="agent_question")
    await db.mark_delivered(msg.id)
    async with db._engine.begin() as conn:
        await conn.execute(sa_update(messages).where(messages.c.id == msg.id).values(delivered_at=1))
    sessions = FakeSessionManager(tail_map={("session", "n-supervisor--global", None): "A supervisor answer"})
    engine = make_engine(db, sessions, config=MessagesConfig(transcript_tail_fallback=True, reply_timeout=1))
    assert await engine.check_reply_timeouts() == 0
    assert await db.list_messages(to_kind="user") == []
