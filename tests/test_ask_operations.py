"""Application crash recovery: verify source receipts before releasing review gates."""

import asyncio

import pytest

from sqlalchemy import select, update

from src.api.models import get_all_response_models
from src.asks.operations import ReviewOperations, get_operation
from src.database.tables import review_feedback_obligations, task_criteria, task_gates, tasks
from tests.test_review_commands import _scoped
from tests.test_review_commands import env as env
from tests.test_review_feedback import accept, comment, prepared, reserve, submitted


async def decision_row(db, decision_id):
    async with db._engine.connect() as conn:
        return await db.get_ask_decision(decision_id, conn=conn)


async def test_filing_failure_retains_review_hold_and_retries_once(env, monkeypatch):
    handler, db = env
    rid = await submitted(handler)
    await comment(handler, rid, "Keep this human follow-up")
    args, _ = await prepared(handler, rid, note="Preserve my note too")
    prepared_response = await handler.execute("ask_prepare_decision", args)
    preview = prepared_response["preview"]
    models = get_all_response_models()
    serialized = models["ask_prepare_decision"].model_validate(prepared_response).model_dump()
    assert serialized["preview"]["packet"] == preview["packet"]
    assert serialized["expected"] == prepared_response["expected"]
    original = handler._file_review_feedback

    async def unavailable(*args):
        raise RuntimeError("Task service unavailable")

    monkeypatch.setattr(handler, "_file_review_feedback", unavailable)
    result = await accept(handler, args, preview)
    assert result["application_state"] == "failed", result
    serialized = models["ask_decide"].model_validate(result).model_dump()
    assert serialized["decision_id"] == result["decision_id"]
    assert serialized["application_state"] == "failed" and serialized["success"] is False
    review = await db.get_review(rid)
    assert review["pending_decision_id"] == result["decision_id"]
    assert review["state"] == "in_review"
    assert (await db.get_gate(review["gate_id"]))["status"] == "open"
    monkeypatch.setattr(handler, "_file_review_feedback", original)
    repaired = await handler.execute("ask_retry_operation", {"decision_id": result["decision_id"]})
    serialized = models["ask_retry_operation"].model_validate(repaired).model_dump()
    assert serialized["decision_id"] == result["decision_id"] and serialized["success"] is True
    retry = await accept(handler, args, preview)
    assert retry["success"], retry
    again = await accept(handler, args, preview)
    assert again["success"] and again["decision_id"] == retry["decision_id"]
    async with db._engine.connect() as conn:
        filing = await get_operation(conn, retry["decision_id"], "file_task")
        items = await db.list_review_feedback_obligations(retry["decision_id"], conn=conn)
        assert len(items) == 2
        assert all(i["task_id"] == filing["receipt"]["task_id"] for i in items)
        rows = (
            await conn.execute(
                select(tasks).where(tasks.c.dedup_key == filing["receipt"]["dedup_key"])
            )
        ).all()
        assert len(rows) == 1
        assert (
            len(
                (
                    await conn.execute(
                        select(task_criteria).where(
                            task_criteria.c.task_id == items[0]["task_id"],
                        )
                    )
                ).all()
            )
            == 2
        )
    assert await handler._review_feedback_close_error(items[0]["task_id"])
    for item in items:
        completed = await handler.execute(
            "review_feedback_complete",
            {
                "task_id": item["task_id"],
                "obligation_id": item["id"],
                "evidence": f"Addressed {item['feedback_key']} in change 123 with its regression check",
            },
        )
        assert completed["success"], completed
        assert models["review_feedback_complete"].model_validate(completed).model_dump() == completed
    assert await handler._review_feedback_close_error(items[0]["task_id"]) is None


async def test_crash_after_task_insert_keeps_application_gate_until_finalize(env, monkeypatch):
    handler, db = env
    rid = await submitted(handler)
    args, preview = await prepared(handler, rid, option="request_changes", note="Please revise")
    original = ReviewOperations.finalize

    async def crash(*args):
        raise RuntimeError("Crash after committed filing")

    monkeypatch.setattr(ReviewOperations, "finalize", crash)
    result = await accept(handler, args, preview)
    assert result["application_state"] == "failed", result
    async with db._engine.connect() as conn:
        filing = await get_operation(conn, result["decision_id"], "file_task")
    task_id = filing["receipt"]["task_id"]
    assert (await db.get_task(task_id)).is_blocked
    assert (await db.get_gate(filing["receipt"]["application_gate_id"]))["status"] == "open"
    bypass = await handler.execute(
        "gate_resolve",
        {
            "gate_id": filing["receipt"]["application_gate_id"],
            "resolved_by": "human:operator",
        },
    )
    assert bypass["error_code"] == "ask.application_pending"
    monkeypatch.setattr(ReviewOperations, "finalize", original)
    results = await asyncio.gather(accept(handler, args, preview), accept(handler, args, preview))
    assert any(r["success"] for r in results), results
    final = await accept(handler, args, preview)
    assert final["success"], final
    assert (await db.get_review(rid))["state"] == "changes_requested"
    assert (await db.get_gate(filing["receipt"]["application_gate_id"]))["status"] == "resolved"


@pytest.mark.parametrize(
    "damage",
    ["criterion", "obligation", "gate_link", "criterion_text", "packet_hash", "failed_task"],
)
async def test_mapping_loss_refuses_gate_release(env, monkeypatch, damage):
    handler, db = env
    rid = await submitted(handler)
    await comment(handler, rid, "Follow up on documentation")
    args, preview = await prepared(handler, rid)
    original = ReviewOperations.record_filing

    async def corrupt(self, decision, conn, tid, parent):
        await original(self, decision, conn, tid, parent)
        if damage == "criterion":
            await conn.execute(task_criteria.delete().where(task_criteria.c.task_id == tid))
        elif damage == "obligation":
            await conn.execute(
                review_feedback_obligations.delete().where(
                    review_feedback_obligations.c.decision_id == decision["id"],
                )
            )
        elif damage == "gate_link":
            await conn.execute(task_gates.delete().where(task_gates.c.task_id == tid))
        elif damage == "packet_hash":
            from src.database.tables import escalation_actions

            filing = await get_operation(conn, decision["id"], "file_task")
            await conn.execute(
                update(escalation_actions)
                .where(escalation_actions.c.id == filing["id"])
                .values(
                    receipt={**filing["receipt"], "packet_sha256": "0" * 64},
                )
            )
        elif damage == "failed_task":
            await conn.execute(update(tasks).where(tasks.c.id == tid).values(status="FAILED"))
        else:
            await conn.execute(
                update(task_criteria)
                .where(task_criteria.c.task_id == tid)
                .values(
                    content="Silently replaced the human instruction",
                )
            )

    monkeypatch.setattr(ReviewOperations, "record_filing", corrupt)
    result = await accept(handler, args, preview)
    assert result["application_state"] == "failed", result
    review = await db.get_review(rid)
    assert review["state"] == "in_review"
    assert (await db.get_gate(review["gate_id"]))["status"] == "open"


async def test_event_failure_preserves_seq_and_one_ingestion_intent(env, monkeypatch):
    handler, db = env
    rid = await submitted(handler)
    args, preview = await prepared(handler, rid)
    original = handler._emit_review_application_event
    emitted = []

    async def crash(name, payload):
        emitted.append((name, payload["seq"]))
        if name == "spec.approved":
            raise RuntimeError("bus delivery failed after durable event")
        await original(name, payload)

    monkeypatch.setattr(handler, "_emit_review_application_event", crash)
    first = await accept(handler, args, preview)
    assert first["application_state"] == "failed", first
    assert (await db.get_review(rid))["state"] == "approved"
    monkeypatch.setattr(handler, "_emit_review_application_event", original)
    retry = await accept(handler, args, preview)
    assert retry["success"], retry
    events = await db.get_recent_events(event_type="spec.approved")
    assert len(events) == 1
    async with db._engine.connect() as conn:
        operation = await get_operation(conn, first["decision_id"], "ingestion")
    assert (
        operation["receipt"]["seq"] == [seq for name, seq in emitted if name == "spec.approved"][0]
    )


async def test_post_acceptance_vault_edit_is_preserved_for_repair(env):
    handler, db = env
    rid = await submitted(handler)
    args, preview = await prepared(handler, rid)
    async with db.immediate() as conn:
        decision = await reserve(handler, args, preview, conn)
    service = handler._review_service()
    review = await db.get_review(rid)
    path = service._path(review)
    path.write_text("# External edit after acceptance\n")
    result = await ReviewOperations(service).apply(decision["id"])
    assert result["application_state"] == "failed", result
    assert path.read_text() == "# External edit after acceptance\n"
    assert not await db.get_recent_events(event_type="spec.approved")
    frozen = await decision_row(db, decision["id"])
    assert frozen["packet_sha256"] == preview["packet_sha256"]


async def test_decline_records_no_revision_work_or_ingestion(env):
    handler, db = env
    rid = await submitted(handler)
    args, preview = await prepared(handler, rid, option="decline", note="Do not proceed")
    result = await accept(handler, args, preview)
    assert result["success"], result
    review = await db.get_review(rid)
    assert review["state"] == "rejected"
    assert (await db.get_gate(review["gate_id"]))["status"] == "cancelled"
    async with db._engine.connect() as conn:
        assert await get_operation(conn, result["decision_id"], "file_task") is None
        assert await get_operation(conn, result["decision_id"], "ingestion") is None
        obligations = await db.list_review_feedback_obligations(result["decision_id"], conn=conn)
        assert obligations[0]["progress"] == "pending"
        assert obligations[0]["logical_owner"] == "supervisor:p"


async def test_legacy_approved_projection_is_not_a_repair_authority(env):
    handler, db = env
    rid = await submitted(handler)
    from src.database.tables import doc_reviews

    async with db.immediate() as conn:
        await conn.execute(
            update(doc_reviews).where(doc_reviews.c.id == rid).values(state="approved")
        )
    result = await ReviewOperations(handler._review_service()).reconcile_review(rid)
    assert result["application_state"] == "unknown"
    review = await db.get_review(rid)
    assert (await db.get_gate(review["gate_id"]))["status"] == "open"


async def test_mapping_transaction_rolls_back_task_and_retries(env, monkeypatch):
    handler, db = env
    rid = await submitted(handler)
    await comment(handler, rid, "Preserve this follow-up")
    args, preview = await prepared(handler, rid)
    original = ReviewOperations.record_filing

    async def crash(self, decision, conn, tid, parent):
        await original(self, decision, conn, tid, parent)
        raise RuntimeError("Crash before transaction commit")

    monkeypatch.setattr(ReviewOperations, "record_filing", crash)
    result = await accept(handler, args, preview)
    assert result["application_state"] == "failed", result
    async with db._engine.connect() as conn:
        filing = await get_operation(conn, result["decision_id"], "file_task")
        assert filing["status"] == "pending" and not filing["receipt"]
        assert not (
            await conn.execute(
                select(tasks).where(
                    tasks.c.dedup_key == f"review-followup:{result['decision_id']}",
                )
            )
        ).first()
    monkeypatch.setattr(ReviewOperations, "record_filing", original)
    assert (await accept(handler, args, preview))["success"]


async def test_old_decision_replay_does_not_settle_new_revision(env):
    handler, db = env
    rid = await submitted(handler)
    args, preview = await prepared(
        handler, rid, option="request_changes", note="Return another draft"
    )
    assert (await accept(handler, args, preview))["success"]
    revised = await handler.execute(
        "review_submit", {"review_id": rid, "content": "# Revised design\n"}
    )
    assert revised["success"], revised
    new_args, new_preview = await prepared(handler, rid)
    assert new_args["expected"]["decision_generation"] > args["expected"]["decision_generation"]
    assert (await accept(handler, args, preview))["success"]
    async with db._engine.connect() as conn:
        ask = await db.get_ask(args["ask_id"], conn=conn)
    assert ask["decision_state"] == "pending"
    assert ask["next_actor"] == "human" and ask["application_state"] == "not_started"
    assert (await db.get_gate((await db.get_review(rid))["gate_id"]))["status"] == "open"


async def test_revoked_delegation_after_acceptance_keeps_gate_held(env, monkeypatch):
    handler, db = env
    rid = await submitted(handler)
    await comment(handler, rid, "Keep this follow-up")
    await handler.execute("review_delegate", {"review_id": rid, "to": "supervisor"})
    shown = await _scoped(handler, "review_show", {"review_id": rid}, session_id="supervisor")
    args = {
        "ask_id": shown["ask"]["id"],
        "expected": shown["ask"]["expected"],
        "option_key": "approve",
    }
    preview = await _scoped(handler, "ask_prepare_decision", args, session_id="supervisor")
    assert preview["success"], preview
    original = ReviewOperations.finalize

    async def revoked(self, decision):
        await db.update_session("supervisor", instance_token="replacement-token")
        return await original(self, decision)

    monkeypatch.setattr(ReviewOperations, "finalize", revoked)
    result = await _scoped(
        handler,
        "ask_decide",
        {
            **args,
            "preview_id": preview["preview"]["id"],
            "idempotency_key": "revoked-accept",
        },
        session_id="supervisor",
    )
    assert result["application_state"] == "failed", result
    assert result["error_code"] == "ask.not_decider"
    review = await db.get_review(rid)
    assert review["pending_decision_id"] == result["decision_id"]
    assert review["state"] == "in_review"
    assert (await db.get_gate(review["gate_id"]))["status"] == "open"


async def test_legacy_preview_retry_reconciles_same_accepted_decision(env, monkeypatch):
    handler, db = env
    rid = await submitted(handler)
    args = {"review_id": rid, "revision": 1, "decision": "approve", "note": "Keep this follow-up"}
    preview = await handler.execute("review_decide", args)
    assert preview["error_code"] == "ask.preview_required", preview
    accepted = {**args, "preview_id": preview["preview"]["id"], "expected": preview["expected"]}
    original = handler._file_review_feedback

    async def crash(*args):
        raise RuntimeError("Temporary filing outage")

    monkeypatch.setattr(handler, "_file_review_feedback", crash)
    first = await handler.execute("review_decide", accepted)
    assert first["application_state"] == "failed", first
    monkeypatch.setattr(handler, "_file_review_feedback", original)
    retry = await handler.execute("review_decide", accepted)
    assert retry.get("success") and retry["decision_id"] == first["decision_id"], retry
    again = await handler.execute("review_decide", accepted)
    assert again.get("success") and again["decision_id"] == first["decision_id"], again
