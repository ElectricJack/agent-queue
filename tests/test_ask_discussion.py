"""Discussion persists before acknowledgement and never exercises decision authority."""

import json
import time

import pytest
from sqlalchemy import func, select, update

from src.api.models.ask import ASK_ARGS
from src.asks.discussion import discussion_status
from src.database.tables import conversation_inputs, escalation_messages, messages
from src.sessions.spec import named_session_name
from tests.test_review_commands import _scoped
from tests.test_review_commands import env as review_env  # noqa: F401
from tests.test_review_feedback import submitted


@pytest.fixture
async def env(review_env):  # noqa: F811
    handler, db = review_env
    await db.update_session("supervisor", name=named_session_name("supervisor", "p"))
    profile = await db.get_profile("supervisor")
    await db.update_profile(
        "supervisor",
        aq_commands=list(set(profile.aq_commands) | set(ASK_ARGS) | {"supervisor_inbox_reply"}),
    )
    return handler, db


async def ask_for(handler):
    rid = await submitted(handler)
    shown = await handler.execute("review_show", {"review_id": rid})
    return rid, shown["ask"]


async def discuss(handler, ask, text="Explain the proposed design", key="question"):
    return await handler.execute(
        "ask_reply",
        {
            "ask_id": ask["id"],
            "expected": ask["expected"],
            "text": text,
            "idempotency_key": key,
        },
    )


async def test_discussion_is_atomic_replayable_and_revision_bound(env):
    handler, db = env
    rid, ask = await ask_for(handler)
    first = await discuss(handler, ask)
    assert first["success"], first
    assert (await discuss(handler, ask))["receipt"] == first["receipt"]
    conflict = await discuss(handler, ask, text="Changed retry body")
    assert conflict["error_code"] == "ask.input_conflict"
    shown = await _scoped(
        handler,
        "ask_show",
        {"ask_id": ask["id"], "input_id": first["input_id"]},
        session_id="supervisor",
    )
    assert shown["success"], shown
    assert shown["document"]["content"].startswith("# Design")
    async with db.immediate() as conn:
        item = await db.get_ask_input(first["input_id"], conn=conn)
        message = (
            (
                await conn.execute(
                    select(messages).where(messages.c.id == item["supervisor_message_id"])
                )
            )
            .mappings()
            .one()
        )
        assert message["thread_id"] == f"ask:{ask['id']}:revision:1"
        packet = json.loads(message["body"].split("\n", 1)[1])
        assert "UNTRUSTED" in packet["label"]
        assert packet["version"]["content_sha256"] == shown["document"]["content_sha256"]
        assert await conn.scalar(select(func.count()).select_from(conversation_inputs)) == 1
    assert (await db.get_review(rid))["state"] == "in_review"


async def test_answer_scope_promotion_and_no_decision(env):
    handler, db = env
    rid, ask = await ask_for(handler)
    first = await discuss(handler, ask)
    args = {
        "ask_id": ask["id"],
        "input_id": first["input_id"],
        "text": "Use a durable receipt.",
        "idempotency_key": "answer",
    }
    denied = await handler.execute("ask_discussion_reply", args)
    assert denied["error_code"] == "ask.not_supervisor"
    reply = await _scoped(handler, "ask_discussion_reply", args, session_id="supervisor")
    assert reply["success"], reply
    replay = await _scoped(handler, "ask_discussion_reply", args, session_id="supervisor")
    assert replay["success"] and not replay["created"]
    changed = await _scoped(
        handler, "ask_discussion_reply", {**args, "text": "Different"}, session_id="supervisor"
    )
    assert changed["error_code"] == "ask.idempotency_conflict"
    apply = await _scoped(
        handler,
        "ask_apply_input",
        {
            "ask_id": ask["id"],
            "input_id": first["input_id"],
            "expected": ask["expected"],
            "option_key": "approve",
            "interpretation": "Approve",
            "idempotency_key": "apply",
        },
        session_id="supervisor",
    )
    assert apply["error_code"] == "ask.discussion_only"
    promoted = await handler.execute(
        "review_comment",
        {
            "review_id": rid,
            "revision": 1,
            "body": "Add a durable receipt.",
            "source_discussion_id": reply["entry_id"],
        },
    )
    assert promoted["success"], promoted
    assert (await db.get_review(rid))["feedback_version"] == 1
    assert (await db.get_review(rid))["state"] == "in_review"


async def test_offline_delay_uses_delivery_facts_and_elapsed_time(env):
    handler, db = env
    _, ask = await ask_for(handler)
    first = await discuss(handler, ask)
    async with db.immediate() as conn:
        item = await db.get_ask_input(first["input_id"], conn=conn)
        queued = await discussion_status(db, item, conn=conn, now=item["created_at"] + 1)
        assert queued["state"] == "queued"
        delayed = await discussion_status(db, item, conn=conn, now=item["created_at"] + 16)
        assert delayed["state"] == "delayed" and delayed["elapsed_seconds"] == 16
        await conn.execute(
            update(messages)
            .where(messages.c.id == item["supervisor_message_id"])
            .values(delivered_at=time.time())
        )
        assert (await discussion_status(db, item, conn=conn))["state"] == "answering"


async def test_global_supervisor_fallback_reports_actual_answering_session(env):
    handler, db = env
    await db.update_session(
        "supervisor", project_id=None, name=named_session_name("supervisor", "global")
    )
    _, ask = await ask_for(handler)
    first = await discuss(handler, ask)
    assert first["success"], first
    async with db.immediate() as conn:
        item = await db.get_ask_input(first["input_id"], conn=conn)
        await conn.execute(
            update(messages)
            .where(messages.c.id == item["supervisor_message_id"])
            .values(delivered_at=time.time())
        )
        status = await discussion_status(db, item, conn=conn, now=item["created_at"] + 16)
        assert status["state"] == "answering" and status["supervisor_live"]


async def test_retention_keeps_promoted_words_but_expires_ordinary_chat(env):
    handler, db = env
    rid, ask = await ask_for(handler)
    keep = await discuss(handler, ask, key="keep")
    discard = await discuss(handler, ask, key="discard")
    promoted = await handler.execute(
        "review_comment",
        {
            "review_id": rid,
            "revision": 1,
            "body": "Formal work",
            "source_discussion_id": keep["receipt"]["entry_id"],
        },
    )
    assert promoted["success"], promoted
    expired_ids = await db.list_expired_conversation_message_ids(older_than=time.time() + 1)
    async with db.immediate() as conn:
        item = await db.get_ask_input(discard["input_id"], conn=conn)
        assert item["supervisor_message_id"] in expired_ids
    await db.expire_conversation_text(older_than=time.time() + 1, now=time.time() + 2)
    async with db.immediate() as conn:
        rows = {r["id"]: r for r in (await conn.execute(select(escalation_messages))).mappings()}
        assert rows[keep["receipt"]["entry_id"]]["text"] == "Explain the proposed design"
        assert rows[discard["receipt"]["entry_id"]]["text"] == "[Ordinary discussion expired]"


async def test_discussion_message_failure_rolls_back_acknowledgeable_input(env, monkeypatch):
    from unittest.mock import AsyncMock

    handler, db = env
    _, ask = await ask_for(handler)
    monkeypatch.setattr(
        db, "insert_ask_message", AsyncMock(side_effect=RuntimeError("write failed"))
    )
    failed = await discuss(handler, ask)
    assert failed.get("success") is not True
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(conversation_inputs)) == 0
        assert await conn.scalar(select(func.count()).select_from(escalation_messages)) == 0


async def test_post_decision_discussion_is_explicit_and_labelled(env):
    from tests.test_review_feedback import accept, prepared

    handler, db = env
    rid, _ = await ask_for(handler)
    args, preview = await prepared(handler, rid)
    accepted = await accept(handler, args, preview)
    assert accepted["success"], accepted
    shown = await handler.execute("ask_show", {"ask_id": args["ask_id"]})
    reply_args = {
        "ask_id": args["ask_id"],
        "expected": shown["expected"],
        "text": "Explain the approved version",
        "idempotency_key": "after",
    }
    refused = await handler.execute("ask_reply", reply_args)
    assert refused["error_code"] == "ask.post_decision", refused
    discussion = await handler.execute("ask_reply", {**reply_args, "post_decision": True})
    assert discussion["success"], discussion
    answer = await _scoped(
        handler,
        "ask_discussion_reply",
        {
            "ask_id": args["ask_id"],
            "input_id": discussion["input_id"],
            "text": "Explanation",
            "idempotency_key": "answer-after",
        },
        session_id="supervisor",
    )
    assert answer["success"], answer
    history = await handler.execute("ask_history", {"ask_id": args["ask_id"]})
    assert "post-decision discussion" in history["items"][-1]["text"]
    assert (await db.get_review(rid))["state"] == "approved"
