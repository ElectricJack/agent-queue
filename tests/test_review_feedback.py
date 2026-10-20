"""Frozen review packets and the common PostgreSQL mutation boundary."""

import asyncio

import pytest
from sqlalchemy import select

from src.asks.models import DecisionIntent, SourceVersion
from src.asks.review import authenticated_actor
from src.database.tables import ask_decisions, escalation_actions
from src.reviews.service import ReviewError
from tests.test_review_commands import _scoped
from tests.test_review_commands import env as env


async def submitted(handler):
    result = await handler.execute(
        "review_submit",
        {
            "project_id": "p",
            "task_id": "author",
            "kind": "spec",
            "title": "Frozen feedback",
            "content": "# Design\n\nThe initial design.\n",
        },
    )
    assert result["success"], result
    return result["review_id"]


async def comment(handler, rid, body, revision=1):
    result = await handler.execute(
        "review_comment",
        {
            "review_id": rid,
            "revision": revision,
            "body": body,
            "heading_path": ["Design"],
        },
    )
    assert result["success"], result
    return result["comment_id"]


async def prepared(handler, rid, *, option="approve", note="", dispositions=()):
    shown = await handler.execute("review_show", {"review_id": rid})
    assert shown["success"], shown
    ask = shown["ask"]
    args = {
        "ask_id": ask["id"],
        "expected": ask["expected"],
        "option_key": option,
        "overall_note": note,
        "dispositions": list(dispositions),
    }
    result = await handler.execute("ask_prepare_decision", args)
    assert result["success"], result
    return args, result["preview"]


async def accept(handler, args, preview, key="accept-1"):
    return await handler.execute(
        "ask_decide",
        {
            **args,
            "preview_id": preview["id"],
            "idempotency_key": key,
        },
    )


async def reserve(handler, args, preview, conn):
    service = handler._ask_service()
    ask, source = await handler._ask_source_for_caller(args["ask_id"], conn=conn)
    actor = authenticated_actor()
    intent = DecisionIntent(
        args["option_key"],
        SourceVersion(**args["expected"]),
        args["overall_note"],
        tuple(args["dispositions"]),
    )
    await handler.db.insert_ask_correspondence(
        {
            "id": "reserved-input",
            "ask_id": ask["id"],
            "direction": "inbound",
            "transport": "local",
            "verified_actor": actor.principal_id,
            "text": args["overall_note"] or args["option_key"],
            "entry_kind": "decision_evidence",
            "received_at": service.clock(),
            "created_at": service.clock(),
        },
        conn=conn,
    )
    return (
        await service.decide(
            source,
            actor,
            intent,
            conn=conn,
            preview_id=preview["id"],
            idempotency_key="reserved",
            evidence_entry_id="reserved-input",
        )
    )[0]


async def test_packet_keeps_every_revision_response_and_verbatim_note(env):
    handler, db = env
    rid = await submitted(handler)
    cid = await comment(handler, rid, "Original human instruction")
    revised = await handler.execute(
        "review_submit",
        {
            "review_id": rid,
            "content": "# Design\n\nThe corrected design.\n",
            "changes_note": "Responded to the instruction",
            "resolves": [cid],
        },
    )
    assert revised["success"], revised
    await comment(handler, rid, "Older-revision annotation", revision=1)
    args, preview = await prepared(handler, rid, note="  Keep this note verbatim.\n")
    domain = preview["packet"]["domain"]
    assert len(domain["comments"]) == 2
    assert domain["comments"][0]["responder_disposition"]["accepted"] is False
    assert domain["feedback"][0]["disposition"] == "follow_up"
    assert domain["feedback"][0]["original_revision"] == 1
    assert domain["feedback"][0]["original_anchor"]["heading_path"] == ["Design"]
    assert domain["overall_note"] == "  Keep this note verbatim.\n"
    assert domain["feedback"][-1]["acceptance_text"] == domain["overall_note"]
    assert (await db.get_review(rid))["pending_decision_id"] is None
    async with db._engine.connect() as conn:
        assert not (await conn.execute(select(ask_decisions))).first()
        assert not (await conn.execute(select(escalation_actions))).first()


@pytest.mark.parametrize(
    "mutation",
    ["comment", "attachment", "disposition", "revise", "delegate", "withdraw", "import", "reopen"],
)
async def test_mutations_wait_for_source_then_refuse_reserved_packet(env, mutation):
    handler, db = env
    rid = await submitted(handler)
    cid = await comment(handler, rid, "Keep it durable")
    args, preview = await prepared(handler, rid)
    service = handler._review_service()
    entered = asyncio.Event()

    async def mutate():
        entered.set()
        if mutation == "comment":
            return await service.comment(
                review_id=rid,
                revision=1,
                quote=None,
                heading_path=[],
                body="Too late",
                author="human:local-operator",
            )
        if mutation == "attachment":
            async with db.immediate() as write_conn:
                return await db.insert_review_attachment(
                    {"review_id": rid, "revision": 1}, conn=write_conn
                )
        if mutation == "disposition":
            async with db.immediate() as write_conn:
                return await db.set_review_comment_disposition(
                    rid, cid, {"proposed": "applied"}, conn=write_conn
                )
        if mutation == "revise":
            return await service.revise(
                review_id=rid,
                content="# New\n",
                changes_note="Change design",
                resolves=[],
                submitted_by="human:local-operator",
                submitted_task_id="author",
            )
        if mutation == "delegate":
            return await service.delegate(review_id=rid, to="supervisor")
        if mutation == "import":
            return await service.import_edits(review_id=rid, by="human:local-operator")
        if mutation == "reopen":
            return await service.reopen(review_id=rid, revision=1, by="human:local-operator")
        return await service.withdraw(review_id=rid, reason="Withdraw", by="human:local-operator")

    async with db.immediate() as conn:
        await reserve(handler, args, preview, conn)
        task = asyncio.create_task(mutate())
        await entered.wait()
        # A real second connection must block on the row, not check unlocked state.
        await asyncio.sleep(0.05)
        assert not task.done()
    with pytest.raises(ReviewError) as exc:
        await asyncio.wait_for(task, timeout=5)
    assert exc.value.code in {"decision_pending", "review_closed"}
    assert len(await db.list_review_comments(rid)) == 1
    assert (await db.get_review(rid))["current_revision"] == 1


async def test_stale_feedback_preview_and_caller_authority_are_refused(env):
    handler, db = env
    rid = await submitted(handler)
    args, preview = await prepared(handler, rid)
    await comment(handler, rid, "Saved in another tab")
    result = await accept(handler, args, preview)
    assert result["error_code"] == "ask.stale_feedback", result
    result = await handler.execute("ask_decide", {**args, "resolved_by": "human:jack"})
    assert result["error_code"] == "ask.not_decider", result
    assert (await db.get_review(rid))["state"] == "in_review"


@pytest.mark.parametrize(
    "text",
    [
        "[blocking] Correct the invariant",
        "Approve only if retries are bounded",
        "Change the spec to require receipts",
    ],
)
async def test_conditional_approval_returns_revision_and_keeps_gate(env, text):
    handler, db = env
    rid = await submitted(handler)
    await comment(handler, rid, text)
    args, preview = await prepared(handler, rid)
    assert preview["packet"]["domain"]["effective_option"] == "fold_in"
    result = await accept(handler, args, preview)
    assert result["success"], result
    assert result["state"] == "changes_requested"
    review = await db.get_review(rid)
    assert (await db.get_gate(review["gate_id"]))["status"] == "open"
    assert not await db.get_recent_events(event_type="spec.approved")


async def test_legacy_feedback_decision_requires_explicit_packet_acceptance(env):
    handler, db = env
    rid = await submitted(handler)
    await comment(handler, rid, "Please record this")
    result = await handler.execute(
        "review_decide",
        {
            "review_id": rid,
            "revision": 1,
            "decision": "approve",
            "note": "Approved with follow-up",
        },
    )
    assert result["error_code"] == "ask.preview_required", result
    assert (await db.get_review(rid))["state"] == "in_review"
    assert result["preview"]["packet"]["domain"]["feedback"]


async def test_core_decision_preserves_authenticated_supervisor_scope(env):
    handler, db = env
    rid = await submitted(handler)
    assert (await handler.execute("review_delegate", {"review_id": rid, "to": "supervisor"}))[
        "success"
    ]
    shown = await _scoped(handler, "review_show", {"review_id": rid}, session_id="supervisor")
    args = {
        "ask_id": shown["ask"]["id"],
        "expected": shown["ask"]["expected"],
        "option_key": "approve",
    }
    preview = await _scoped(handler, "ask_prepare_decision", args, session_id="supervisor")
    assert preview["success"], preview
    result = await _scoped(
        handler,
        "ask_decide",
        {
            **args,
            "preview_id": preview["preview"]["id"],
            "idempotency_key": "supervisor-accept",
        },
        session_id="supervisor",
    )
    assert result["success"], result
    async with db._engine.connect() as conn:
        decision = await db.get_ask_decision(result["decision_id"], conn=conn)
    assert decision["actor_id"] == "session:supervisor"
    assert decision["actor"]["provenance"]["instance_token"] == "supervisor-token"


async def test_historic_rejection_is_not_decline(env):
    handler, db = env
    rid = await submitted(handler)
    from sqlalchemy import update
    from src.database.tables import doc_reviews

    async with db.immediate() as conn:
        await conn.execute(
            update(doc_reviews).where(doc_reviews.c.id == rid).values(state="rejected")
        )
    shown = await handler.execute("review_show", {"review_id": rid})
    async with db._engine.connect() as conn:
        ask = await db.get_ask(shown["ask"]["id"], conn=conn)
    assert ask["decision_state"] == "changes_requested"
