"""The conversation outbox port and its unbound and recording bindings."""

from __future__ import annotations

import pytest

from src.conversations.outbox import ConversationOutbox, RecordingOutbox, UnboundOutbox


async def test_recording_outbox_records_rows_and_dedups_by_key():
    outbox = RecordingOutbox()

    first = await outbox.enqueue(
        owner_id="conv-1",
        kind="thread_open",
        dedup_key="conv-open:discord:900",
        payload={"text": "On it."},
    )
    replay = await outbox.enqueue(
        owner_id="conv-1",
        kind="thread_open",
        dedup_key="conv-open:discord:900",
        payload={"text": "different"},
    )
    reply = await outbox.enqueue(
        owner_id="conv-1",
        kind="reply",
        dedup_key="conv-reply:msg-conv-reply-abc",
        payload={"text": "Answer"},
        priority=10,
        due_at=5.0,
    )

    assert (first, replay, reply) == ("out-1", "out-1", "out-2")
    assert outbox.bound is True
    assert outbox.rows == [
        {
            "id": "out-1",
            "owner_id": "conv-1",
            "kind": "thread_open",
            "dedup_key": "conv-open:discord:900",
            "payload": {"text": "On it."},
            "priority": 20,
            "due_at": None,
        },
        {
            "id": "out-2",
            "owner_id": "conv-1",
            "kind": "reply",
            "dedup_key": "conv-reply:msg-conv-reply-abc",
            "payload": {"text": "Answer"},
            "priority": 10,
            "due_at": 5.0,
        },
    ]
    assert outbox.by_key("conv-reply:msg-conv-reply-abc") == outbox.rows[1]
    assert outbox.by_key("missing") is None


async def test_unbound_outbox_is_unbound_and_refuses_every_enqueue():
    outbox = UnboundOutbox()

    assert outbox.bound is False
    with pytest.raises(RuntimeError, match="outbox unbound"):
        await outbox.enqueue(owner_id="conv-1", kind="thread_open", dedup_key="k", payload={})


def test_both_bindings_satisfy_the_port():
    assert isinstance(RecordingOutbox(), ConversationOutbox)
    assert isinstance(UnboundOutbox(), ConversationOutbox)
