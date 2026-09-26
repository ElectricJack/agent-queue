"""Durable supervisor conversations, inputs, backfill cursors and gaps on PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from sqlalchemy import func, select

from src.database import Database
from src.database.queries.conversation_queries import (
    ConversationClosed,
    ConversationConflict,
    ConversationNotFound,
    ConversationStateError,
)
from src.database.tables import (
    conversation_inputs,
    messages,
    supervisor_conversations,
)
from tests.db_fixtures import lease_dsn

AUTHOR = "111111111111111111"
CHANNEL = "222222222222222222"


@pytest.fixture
async def db():
    database = Database(lease_dsn("conversation-queries"))
    await database.initialize()
    yield database
    await database.close()


def accept_args(**overrides) -> dict:
    values = {
        "transport": "discord",
        "guild_id": "1",
        "channel_id": CHANNEL,
        "external_message_id": "900",
        "external_root_message_id": "900",
        "external_thread_id": None,
        "author_id": AUTHOR,
        "verified_actor": f"human:discord:{AUTHOR}",
        "text": "hello supervisor",
        "audience": [AUTHOR],
        "source": "gateway",
        "received_at": 1000.0,
        "conversation_id": None,
        "brief": "Operator asks: hello supervisor",
        "now": 1000.0,
    }
    values.update(overrides)
    return values


async def row_counts(db: Database) -> tuple[int, int, int]:
    async with db._engine.connect() as conn:
        return (
            await conn.scalar(select(func.count()).select_from(supervisor_conversations)),
            await conn.scalar(select(func.count()).select_from(conversation_inputs)),
            await conn.scalar(select(func.count()).select_from(messages)),
        )


async def open_conversation(db: Database, **overrides) -> dict:
    accepted = await db.accept_conversation_input(**accept_args(**overrides))
    conversation = accepted["conversation"]
    assert await db.bind_conversation_thread(
        conversation["id"], external_thread_id="thread-1", now=1001.0
    )
    return accepted


async def test_accept_writes_conversation_input_and_supervisor_message(db):
    accepted = await db.accept_conversation_input(**accept_args())

    conversation = accepted["conversation"]
    item = accepted["input"]
    assert accepted["created"] is True
    assert conversation["id"].startswith("conv-")
    assert item["id"].startswith("cinput-")
    assert accepted["supervisor_message_id"] == f"msg-{item['id']}"
    assert conversation == {
        "id": conversation["id"],
        "transport": "discord",
        "guild_id": "1",
        "channel_id": CHANNEL,
        "external_root_message_id": "900",
        "external_thread_id": None,
        "thread_id": f"conversation:{conversation['id']}",
        "created_by": f"human:discord:{AUTHOR}",
        "audience": [AUTHOR],
        "state": "opening",
        "created_at": 1000.0,
        "updated_at": 1000.0,
        "closed_at": None,
    }
    assert item == {
        "id": item["id"],
        "conversation_id": conversation["id"],
        "transport": "discord",
        "external_message_id": "900",
        "verified_actor": f"human:discord:{AUTHOR}",
        "author_id": AUTHOR,
        "channel_id": CHANNEL,
        "text": "hello supervisor",
        "text_sha256": hashlib.sha256(b"hello supervisor").hexdigest(),
        "char_count": 16,
        "received_at": 1000.0,
        "source": "gateway",
        "state": "accepted",
        "supervisor_message_id": f"msg-{item['id']}",
        "reply_message_id": None,
        "delay_notified_at": None,
        "text_expired_at": None,
        "created_at": 1000.0,
    }

    notice = await db.get_message(accepted["supervisor_message_id"])
    assert notice is not None
    assert (
        notice.project_id,
        notice.from_kind,
        notice.from_id,
        notice.to_kind,
        notice.to_id,
        notice.thread_id,
        notice.subject,
        notice.body,
        notice.body_kind,
        notice.priority,
        notice.delivered_at,
    ) == (
        None,
        "user",
        f"discord:{AUTHOR}",
        "session",
        "supervisor-global",
        conversation["thread_id"],
        f"Discord conversation {conversation['id']}",
        "Operator asks: hello supervisor",
        "conversation_input",
        50,
        None,
    )
    assert await db.get_conversation(conversation["id"]) == conversation
    assert await db.get_conversation_input(item["id"]) == item
    assert (
        await db.find_conversation_input_by_external(transport="discord", external_message_id="900")
        == item
    )


async def test_replay_returns_the_original_rows_and_writes_nothing(db):
    first = await db.accept_conversation_input(**accept_args())
    before = await row_counts(db)

    replay = await db.accept_conversation_input(
        **accept_args(text="edited later", brief="another brief", source="backfill", now=2000.0)
    )

    assert replay == {
        "created": False,
        "conversation": first["conversation"],
        "input": first["input"],
        "supervisor_message_id": first["supervisor_message_id"],
    }
    assert await row_counts(db) == before == (1, 1, 1)


async def test_concurrent_replays_create_exactly_one_input(db):
    results = await asyncio.gather(
        *(db.accept_conversation_input(**accept_args()) for _ in range(4))
    )

    assert sorted(result["created"] for result in results) == [False, False, False, True]
    assert len({result["input"]["id"] for result in results}) == 1
    assert await row_counts(db) == (1, 1, 1)


async def test_follow_up_on_an_open_conversation_appends_an_input(db):
    first = await open_conversation(db)
    conversation_id = first["conversation"]["id"]

    follow = await db.accept_conversation_input(
        **accept_args(
            external_message_id="901",
            external_thread_id="thread-1",
            text="and another thing",
            received_at=1100.0,
            now=1100.0,
            conversation_id=conversation_id,
        )
    )

    assert follow["created"] is True
    assert follow["conversation"]["id"] == conversation_id
    assert follow["conversation"]["state"] == "open"
    assert follow["conversation"]["updated_at"] == 1100.0
    assert follow["input"]["conversation_id"] == conversation_id
    assert await row_counts(db) == (1, 2, 2)
    notice = await db.get_message(follow["supervisor_message_id"])
    assert notice.thread_id == first["conversation"]["thread_id"]


async def test_follow_up_on_a_closed_conversation_is_refused(db):
    first = await open_conversation(db)
    conversation_id = first["conversation"]["id"]
    assert await db.set_conversation_state(conversation_id, state="closed", now=1200.0)
    before = await row_counts(db)

    with pytest.raises(ConversationClosed):
        await db.accept_conversation_input(
            **accept_args(
                external_message_id="902",
                received_at=1300.0,
                now=1300.0,
                conversation_id=conversation_id,
            )
        )
    assert await row_counts(db) == before


async def test_follow_up_on_an_unknown_conversation_is_refused(db):
    with pytest.raises(ConversationNotFound):
        await db.accept_conversation_input(**accept_args(conversation_id="conv-missing"))
    assert await row_counts(db) == (0, 0, 0)


async def test_invalid_input_writes_nothing(db):
    with pytest.raises(ValueError, match="4000"):
        await db.accept_conversation_input(**accept_args(text="x" * 4001))
    with pytest.raises(ValueError, match="source"):
        await db.accept_conversation_input(**accept_args(source="http"))
    with pytest.raises(ValueError, match="text"):
        await db.accept_conversation_input(**accept_args(text=""))
    assert await row_counts(db) == (0, 0, 0)


async def test_count_inputs_counts_the_window_per_author_and_channel(db):
    other = "333333333333333333"
    first = await open_conversation(db)
    conversation_id = first["conversation"]["id"]
    for message_id, author, received in (("901", AUTHOR, 1100.0), ("902", other, 1200.0)):
        await db.accept_conversation_input(
            **accept_args(
                external_message_id=message_id,
                author_id=author,
                verified_actor=f"human:discord:{author}",
                received_at=received,
                now=received,
                conversation_id=conversation_id,
            )
        )
    await db.accept_conversation_input(
        **accept_args(
            external_message_id="950",
            external_root_message_id="950",
            channel_id="elsewhere",
            received_at=1150.0,
            now=1150.0,
        )
    )

    assert await db.count_conversation_inputs(since=1000.0, author_id=AUTHOR) == 3
    assert await db.count_conversation_inputs(since=1100.0, author_id=AUTHOR) == 2
    assert await db.count_conversation_inputs(since=1100.0, author_id=other) == 1
    assert await db.count_conversation_inputs(since=1000.0, channel_id=CHANNEL) == 3
    assert await db.count_conversation_inputs(since=1150.0, channel_id=CHANNEL) == 1
    assert (
        await db.count_conversation_inputs(since=1100.0, author_id=AUTHOR, channel_id=CHANNEL) == 1
    )


async def test_bind_moves_opening_to_open_once(db):
    accepted = await db.accept_conversation_input(**accept_args())
    conversation_id = accepted["conversation"]["id"]

    assert await db.bind_conversation_thread(
        conversation_id, external_thread_id="thread-1", now=1001.0
    )
    assert not await db.bind_conversation_thread(
        conversation_id, external_thread_id="thread-2", now=1002.0
    )

    bound = await db.get_conversation(conversation_id)
    assert (bound["state"], bound["external_thread_id"], bound["updated_at"]) == (
        "open",
        "thread-1",
        1001.0,
    )
    assert (
        await db.find_conversation_by_thread(
            transport="discord", channel_id=CHANNEL, external_thread_id="thread-1"
        )
        == bound
    )
    assert (
        await db.find_conversation_by_thread(
            transport="discord", channel_id="reconfigured", external_thread_id="thread-1"
        )
        is None
    )


async def test_set_state_honours_expected_states_and_stamps_closing(db):
    accepted = await db.accept_conversation_input(**accept_args())
    conversation_id = accepted["conversation"]["id"]

    assert not await db.set_conversation_state(
        conversation_id, state="closed", now=1100.0, expected=("open",)
    )
    assert await db.set_conversation_state(
        conversation_id, state="delivery_blocked", now=1100.0, expected=("opening",)
    )
    assert await db.set_conversation_state(conversation_id, state="closed", now=1200.0)

    closed = await db.get_conversation(conversation_id)
    assert (closed["state"], closed["updated_at"], closed["closed_at"]) == (
        "closed",
        1200.0,
        1200.0,
    )
    with pytest.raises(ValueError, match="state"):
        await db.set_conversation_state(conversation_id, state="gone", now=1300.0)


async def test_reply_is_addressed_to_the_author_in_the_thread_and_answers_the_input(db):
    first = await open_conversation(db)
    conversation = first["conversation"]
    item = first["input"]

    reply = await db.record_conversation_reply(
        conversation_id=conversation["id"],
        input_id=item["id"],
        reply_message_id="msg-conv-reply-abc",
        body="Here is the answer.",
        now=1500.0,
    )

    assert reply["created"] is True
    assert reply["input"]["state"] == "answered"
    assert reply["input"]["reply_message_id"] == "msg-conv-reply-abc"
    assert reply["conversation"]["updated_at"] == 1500.0
    message = await db.get_message("msg-conv-reply-abc")
    assert (
        message.project_id,
        message.from_kind,
        message.from_id,
        message.to_kind,
        message.to_id,
        message.thread_id,
        message.reply_to_id,
        message.body,
        message.body_kind,
        message.created_at,
    ) == (
        None,
        "session",
        "supervisor-global",
        "user",
        f"discord:{AUTHOR}",
        conversation["thread_id"],
        item["supervisor_message_id"],
        "Here is the answer.",
        "conversation_reply",
        1500.0,
    )
    assert reply["message"]["id"] == "msg-conv-reply-abc"
    notice = await db.get_message(item["supervisor_message_id"])
    assert notice.read_at == 1500.0

    before = await row_counts(db)
    replay = await db.record_conversation_reply(
        conversation_id=conversation["id"],
        input_id=item["id"],
        reply_message_id="msg-conv-reply-abc",
        body="Here is the answer.",
        now=1600.0,
    )
    assert replay["created"] is False
    assert replay["message"] == reply["message"]
    assert replay["input"] == reply["input"]
    assert await row_counts(db) == before


async def test_reply_refuses_a_foreign_input_a_closed_conversation_and_a_revoked_input(db):
    first = await open_conversation(db)
    other = await db.accept_conversation_input(
        **accept_args(external_message_id="950", external_root_message_id="950")
    )
    conversation_id = first["conversation"]["id"]

    with pytest.raises(ConversationConflict, match="input"):
        await db.record_conversation_reply(
            conversation_id=conversation_id,
            input_id=other["input"]["id"],
            reply_message_id="msg-conv-reply-x",
            body="wrong thread",
            now=1500.0,
        )
    with pytest.raises(ConversationNotFound):
        await db.record_conversation_reply(
            conversation_id=conversation_id,
            input_id="cinput-missing",
            reply_message_id="msg-conv-reply-y",
            body="nothing",
            now=1500.0,
        )

    async with db.immediate() as conn:
        await conn.execute(
            conversation_inputs.update()
            .where(conversation_inputs.c.id == other["input"]["id"])
            .values(state="revoked")
        )
    with pytest.raises(ConversationStateError, match="revoked"):
        await db.record_conversation_reply(
            conversation_id=other["conversation"]["id"],
            input_id=other["input"]["id"],
            reply_message_id="msg-conv-reply-z",
            body="too late",
            now=1500.0,
        )

    assert await db.set_conversation_state(conversation_id, state="closed", now=1600.0)
    with pytest.raises(ConversationClosed):
        await db.record_conversation_reply(
            conversation_id=conversation_id,
            input_id=first["input"]["id"],
            reply_message_id="msg-conv-reply-w",
            body="after close",
            now=1700.0,
        )
    assert await row_counts(db) == (2, 2, 2)


async def test_history_is_newest_first_with_reply_pointer_and_pages_by_received_at(db):
    first = await open_conversation(db)
    conversation_id = first["conversation"]["id"]
    for message_id, received in (("901", 1100.0), ("902", 1200.0)):
        await db.accept_conversation_input(
            **accept_args(
                external_message_id=message_id,
                received_at=received,
                now=received,
                conversation_id=conversation_id,
            )
        )
    await db.record_conversation_reply(
        conversation_id=conversation_id,
        input_id=first["input"]["id"],
        reply_message_id="msg-conv-reply-1",
        body="answer one",
        now=1300.0,
    )

    page = await db.list_conversation_inputs(conversation_id, limit=2)
    assert [row["external_message_id"] for row in page] == ["902", "901"]
    older = await db.list_conversation_inputs(conversation_id, before=page[-1]["received_at"])
    assert [row["external_message_id"] for row in older] == ["900"]
    assert (older[0]["reply_body"], older[0]["reply_created_at"]) == ("answer one", 1300.0)
    assert (page[0]["reply_body"], page[0]["reply_created_at"]) == (None, None)

    other = await db.accept_conversation_input(
        **accept_args(
            external_message_id="950",
            external_root_message_id="950",
            received_at=1400.0,
            now=1400.0,
        )
    )
    listed = await db.list_conversations()
    assert [row["id"] for row in listed] == [other["conversation"]["id"], conversation_id]
    assert [row["id"] for row in await db.list_conversations(states=["open"])] == [conversation_id]
    assert [row["id"] for row in await db.list_conversations(before=1400.0)] == [conversation_id]


async def test_awaiting_supervisor_lists_undelivered_unnotified_accepted_inputs(db):
    first = await open_conversation(db)
    conversation_id = first["conversation"]["id"]
    late = await db.accept_conversation_input(
        **accept_args(
            external_message_id="901",
            received_at=1500.0,
            now=1500.0,
            conversation_id=conversation_id,
        )
    )
    delivered = await db.accept_conversation_input(
        **accept_args(
            external_message_id="902",
            received_at=1000.0,
            now=1000.0,
            conversation_id=conversation_id,
        )
    )
    await db.mark_delivered(delivered["supervisor_message_id"])

    waiting = await db.list_inputs_awaiting_supervisor(older_than=1100.0)
    assert [row["id"] for row in waiting] == [first["input"]["id"]]
    assert waiting[0]["conversation_state"] == "open"

    assert await db.mark_delay_notified(first["input"]["id"], now=2000.0)
    assert not await db.mark_delay_notified(first["input"]["id"], now=2001.0)
    assert await db.list_inputs_awaiting_supervisor(older_than=1100.0) == []
    assert [row["id"] for row in await db.list_inputs_awaiting_supervisor(older_than=1600.0)] == [
        late["input"]["id"]
    ]
    assert await db.conversation_counts() == {
        "by_state": {"opening": 0, "open": 1, "closed": 0, "delivery_blocked": 0},
        "inputs_pending_supervisor": 3,
    }


async def test_expire_keeps_the_dedup_row_and_tombstones_are_deleted_later(db):
    first = await open_conversation(db)
    conversation_id = first["conversation"]["id"]
    await db.record_conversation_reply(
        conversation_id=conversation_id,
        input_id=first["input"]["id"],
        reply_message_id="msg-conv-reply-1",
        body="answer",
        now=1050.0,
    )
    recent = await db.accept_conversation_input(
        **accept_args(
            external_message_id="901",
            received_at=5000.0,
            now=5000.0,
            conversation_id=conversation_id,
        )
    )
    old_pending = await db.accept_conversation_input(
        **accept_args(
            external_message_id="902",
            received_at=1500.0,
            now=1500.0,
            conversation_id=conversation_id,
        )
    )

    assert await db.expire_conversation_text(older_than=2000.0, now=6000.0) == 2
    assert await db.expire_conversation_text(older_than=2000.0, now=6001.0) == 0

    answered = await db.get_conversation_input(first["input"]["id"])
    assert (answered["text"], answered["text_expired_at"], answered["state"]) == (
        None,
        6000.0,
        "answered",
    )
    expired = await db.get_conversation_input(old_pending["input"]["id"])
    assert (expired["text"], expired["state"]) == (None, "expired")
    assert (await db.get_conversation_input(recent["input"]["id"]))["text"] == "hello supervisor"

    replay = await db.accept_conversation_input(**accept_args(now=7000.0))
    assert replay["created"] is False and replay["input"]["id"] == first["input"]["id"]

    assert await db.delete_conversation_tombstones(older_than=2000.0) == 2
    assert await db.get_conversation_input(first["input"]["id"]) is None
    assert await db.get_conversation_input(recent["input"]["id"]) is not None
    assert await db.get_conversation(conversation_id) is not None


async def test_close_idle_conversations_closes_open_and_blocked_ones(db):
    idle = await open_conversation(db)
    blocked = await db.accept_conversation_input(
        **accept_args(external_message_id="950", external_root_message_id="950")
    )
    await db.set_conversation_state(
        blocked["conversation"]["id"], state="delivery_blocked", now=1000.0
    )
    busy = await db.accept_conversation_input(
        **accept_args(
            external_message_id="960",
            external_root_message_id="960",
            received_at=5000.0,
            now=5000.0,
        )
    )
    await db.bind_conversation_thread(
        busy["conversation"]["id"], external_thread_id="thread-busy", now=5000.0
    )

    assert await db.close_idle_conversations(idle_since=2000.0, now=6000.0) == 2
    assert (await db.get_conversation(idle["conversation"]["id"]))["closed_at"] == 6000.0
    assert (await db.get_conversation(blocked["conversation"]["id"]))["state"] == "closed"
    assert (await db.get_conversation(busy["conversation"]["id"]))["state"] == "open"
    assert await db.close_idle_conversations(idle_since=2000.0, now=6001.0) == 0


async def test_backfill_cursor_and_gaps_round_trip(db):
    assert await db.get_backfill_cursor(transport="discord", channel_id=CHANNEL) is None

    await db.advance_backfill_cursor(
        transport="discord", channel_id=CHANNEL, last_external_message_id="1000", now=10.0
    )
    await db.advance_backfill_cursor(
        transport="discord", channel_id=CHANNEL, last_external_message_id="1005", now=20.0
    )
    assert await db.get_backfill_cursor(transport="discord", channel_id=CHANNEL) == {
        "transport": "discord",
        "channel_id": CHANNEL,
        "last_external_message_id": "1005",
        "advanced_at": 20.0,
    }

    gap = await db.record_intake_gap(
        transport="discord",
        channel_id=CHANNEL,
        gap_from=100.0,
        gap_to=200.0,
        reason="cursor_expired",
        now=300.0,
    )
    later = await db.record_intake_gap(
        transport="discord",
        channel_id="thread-1",
        gap_from=200.0,
        gap_to=250.0,
        reason="pass_cap",
        now=400.0,
    )
    assert gap == {
        "id": gap["id"],
        "transport": "discord",
        "channel_id": CHANNEL,
        "gap_from": 100.0,
        "gap_to": 200.0,
        "reason": "cursor_expired",
        "recorded_at": 300.0,
    }
    assert gap["id"].startswith("gap-")
    assert await db.list_intake_gaps() == [later, gap]
    assert await db.list_intake_gaps(limit=1) == [later]
    with pytest.raises(ValueError, match="reason"):
        await db.record_intake_gap(
            transport="discord",
            channel_id=CHANNEL,
            gap_from=1.0,
            gap_to=2.0,
            reason="unknown",
            now=3.0,
        )
