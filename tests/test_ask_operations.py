"""Application crash recovery: verify source receipts before releasing review gates."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from sqlalchemy import select, update

from src.api.models import get_all_response_models
from src.asks.operations import (
    FIDELITY_REVIEW_DISPATCH, ReviewOperations, get_operation, reserve_fidelity_review_dispatch,
)
from src.commands import spec_ingest_commands
from src.commands.principal import ExecutionPrincipal, current_principal, principal_context
from src.database.queries import ingest_batch_queries as batches
from src.database.queries.proposal_queries import insert_proposal
from src.database.tables import (
    escalation_actions, ingest_batches, integration_outbox,
    review_feedback_obligations, task_criteria, task_gates, tasks,
)
from src.integration.outbox import IntegrationOutbox, RETRY_EXHAUSTED_PREFIX
from src.models import Task, TaskStatus
from src.orchestrator import Orchestrator
from tests.test_review_commands import _scoped
from tests.test_review_commands import env as env
from tests.test_review_feedback import accept, comment, prepared, reserve, submitted


async def decision_row(db, decision_id):
    async with db._engine.connect() as conn:
        return await db.get_ask_decision(decision_id, conn=conn)


async def held_batch(env, monkeypatch):
    handler, db = env
    rid = await submitted(handler)
    args, preview = await prepared(handler, rid)
    accepted = await accept(handler, args, preview)
    assert accepted["success"], accepted
    decision = await decision_row(db, accepted["decision_id"])
    proposal = await insert_proposal(db, project_id="p", source="spec", payload={}, status="held")
    clock = [time.time() + 120]
    monkeypatch.setattr(spec_ingest_commands, "time", SimpleNamespace(time=lambda: clock[0]))
    async with db.immediate() as conn:
        result = await batches.create_batch(
            conn, batch_id=proposal, project_id="p", source_review_id=rid, source_revision=1,
            source_content_sha256=decision["content_sha256"], decision_id=decision["id"],
            lineage=batches._no_lineage("test"), idempotency_key=proposal, now=clock[0] - 61,
        )
        await reserve_fidelity_review_dispatch(db, result["batch"], conn=conn)
    await db.create_task(Task(id="held-leaf", project_id="p", title="Held leaf", description="",
                              status=TaskStatus.READY))
    async with db.immediate() as conn:
        await batches.add_member(conn, proposal, task_id="held-leaf", member_key="leaf",
                                 role="leaf", section_anchor="Scope", primary_outcome="Leaf",
                                 created_revision=1)
    return handler, db, proposal, decision, clock


async def held_id(db):
    async with db._engine.connect() as conn:
        return (await conn.execute(select(tasks.c.held_batch_id).where(
            tasks.c.id == "held-leaf",
        ))).scalar_one()


async def batch_events(db):
    async with db._engine.connect() as conn:
        return (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type.startswith("batch."),
        ))).mappings().all()


async def accept_batch_events(db, now):
    async def accepted(*args):
        return True

    return await IntegrationOutbox(db, accepted, clock=lambda: now).dispatch_due(now)


async def test_batch_reconcile_missing_reviewer_retry_and_restart(env, monkeypatch):
    handler, db, bid, decision, clock = await held_batch(env, monkeypatch)
    async with db.immediate() as conn:
        batch = await batches.get_batch(conn, bid)
        one = await reserve_fidelity_review_dispatch(db, batch, conn=conn)
        two = await reserve_fidelity_review_dispatch(db, batch, conn=conn)
        assert one["id"] == two["id"]
    with principal_context(ExecutionPrincipal.service("recovery-test")):
        result = await handler.execute("batch_reconcile", {})
    assert result["success"] and len(result["recovered"]) == 1, result
    first = (await batch_events(db))[0]
    async with db._engine.connect() as conn:
        op = await get_operation(conn, decision["id"], FIDELITY_REVIEW_DISPATCH)
    assert op["parameters"]["batch_id"] == bid
    assert op["retry_at"] == clock[0] + 60 and op["status"] == "pending"
    assert not (await handler.execute("batch_reconcile", {}))["recovered"]
    # Concurrent/restarted passes retain a single pending outbox identity.
    clock[0] += 60
    await asyncio.gather(*(handler.execute("batch_reconcile", {}) for _ in range(2)))
    assert len(await batch_events(db)) == 1
    assert await accept_batch_events(db, clock[0]) == 1
    clock[0] += 60
    result = await handler.execute("batch_reconcile", {})
    assert len(result["recovered"]) == 1 and len(await batch_events(db)) == 2
    assert result["recovered"][0]["event_id"] != first["id"]
    # Bus acceptance without a reviewer receipt must not admit a held task.
    assert (await held_id(db)) == bid


@pytest.mark.parametrize("state", ["held", "reviewing", "needs_scope", "retired"])
async def test_batch_reconcile_preserves_live_reviewer_and_scope_state(env, monkeypatch, state):
    handler, db, bid, _, clock = await held_batch(env, monkeypatch)
    await db.create_task(Task(id="reviewer", project_id="p", title="Review", description="",
                              status=TaskStatus.READY))
    async with db.immediate() as conn:
        await batches.assign_reviewer(conn, bid, reviewer_task_id="reviewer")
        await conn.execute(update(ingest_batches).where(ingest_batches.c.batch_id == bid)
                           .values(state=state, updated_at=clock[0] - 61))
    assert not (await handler.execute("batch_reconcile", {}))["recovered"]
    assert not await batch_events(db)
    await db.update_task("reviewer", status=TaskStatus.FAILED)
    result = await handler.execute("batch_reconcile", {})
    assert bool(result["recovered"]) == (state in {"held", "reviewing"})
    assert (await held_id(db)) == bid


@pytest.mark.parametrize("damage", [None, "revision", "digest", "reviewer", "nonpassing"])
async def test_batch_reconcile_requires_current_assigned_passing_receipt(env, monkeypatch, damage):
    handler, db, bid, _, clock = await held_batch(env, monkeypatch)
    await db.create_task(Task(id="reviewer", project_id="p", title="Review", description="",
                              status=TaskStatus.READY))
    async with db.immediate() as conn:
        await batches.assign_reviewer(conn, bid, reviewer_task_id="reviewer")
        batch = await batches.get_batch(conn, bid)
        recorded = await batches.record_receipt(
            conn, bid, structure_revision=1, structure_digest=batch["structure_digest"],
            reviewer_task_id="other" if damage == "reviewer" else "reviewer",
            reviewer_session_id="review-session", passing=damage != "nonpassing",
            section_verdicts={"Scope": "pass"}, reverse_coverage_verdict="pass",
            walkthrough_verdict="pass", structural_findings=[],
        )
        assert recorded["success"], recorded
        await conn.execute(update(ingest_batches).where(ingest_batches.c.batch_id == bid).values(
            updated_at=clock[0] - 61,
            structure_revision=2 if damage == "revision" else 1,
            structure_digest="changed" if damage == "digest" else batch["structure_digest"],
        ))
    result = await handler.execute("batch_reconcile", {})
    if damage:
        assert not result["recovered"]
        assert not await batch_events(db)
    else:
        assert result["recovered"][0]["event_type"] == "batch.receipt_recorded"
        await accept_batch_events(db, clock[0])
        clock[0] += 60
        replay = await handler.execute("batch_reconcile", {})
        assert replay["recovered"][0]["event_type"] == "batch.receipt_recorded"
        assert len(await batch_events(db)) == 2
    assert (await held_id(db)) == bid


async def test_batch_reconcile_repairs_lost_release_event_and_stops_after_acceptance(env, monkeypatch):
    handler, db, bid, _, clock = await held_batch(env, monkeypatch)
    async with db.immediate() as conn:
        await batches.release(conn, bid, expected_revision=1,
                              expected_digest=await batches.live_digest(conn, bid),
                              actor="service:release", release_receipt={"released": True},
                              now=clock[0] - 61)
        # Later live edits must not change the release notification identity.
        await conn.execute(update(ingest_batches).where(ingest_batches.c.batch_id == bid)
                           .values(structure_revision=2, structure_digest="later edit"))
    result = await handler.execute("batch_reconcile", {})
    assert result["recovered"][0]["event_type"] == "batch.released", result
    event = (await batch_events(db))[0]
    assert event["payload"]["structure_revision"] == 1
    assert event["payload"]["structure_digest"] != "later edit"
    assert await accept_batch_events(db, clock[0]) == 1
    clock[0] += 60
    assert not (await handler.execute("batch_reconcile", {}))["recovered"]
    assert (await held_id(db)) is None


async def test_batch_reconcile_isolates_bad_source_and_rearms_exhausted_delivery(env, monkeypatch):
    handler, db, bid, _, clock = await held_batch(env, monkeypatch)
    async with db.immediate() as conn:
        await conn.execute(update(ingest_batches).where(ingest_batches.c.batch_id == bid)
                           .values(decision_id="missing-decision"))
    failed = await handler.execute("batch_reconcile", {})
    assert failed["errors"][0]["batch_id"] == bid
    assert not (await handler.execute("batch_reconcile", {}))["errors"]
    # Restore the source, then simulate a delivery which exhausted its age budget.
    async with db.immediate() as conn:
        op = (await conn.execute(select(escalation_actions).where(
            escalation_actions.c.operation_kind == FIDELITY_REVIEW_DISPATCH,
        ))).mappings().one()
        await conn.execute(update(ingest_batches).where(ingest_batches.c.batch_id == bid)
                           .values(decision_id=op["decision_id"]))
    clock[0] += 60
    await handler.execute("batch_reconcile", {})
    event = (await batch_events(db))[0]
    async with db.immediate() as conn:
        await conn.execute(update(integration_outbox).where(integration_outbox.c.id == event["id"])
                           .values(last_error=RETRY_EXHAUSTED_PREFIX + "deadline", attempts=5))
    clock[0] += 60
    await handler.execute("batch_reconcile", {})
    events = await batch_events(db)
    assert len(events) == 1 and events[0]["last_error"] is None and events[0]["attempts"] == 5
    assert events[0]["available_at"] == clock[0]


async def test_batch_reconcile_bounded_bad_page_does_not_starve_later_batch(env, monkeypatch):
    handler, db, bid, _, clock = await held_batch(env, monkeypatch)
    async with db.immediate() as conn:
        for index in range(21):
            proposal = await insert_proposal(db, project_id="p", source="spec", payload={},
                                             conn=conn, status="held")
            await batches.create_batch(
                conn, batch_id=proposal, project_id="p", source_review_id=f"missing-{index}",
                source_revision=1, source_content_sha256="missing", decision_id="missing",
                lineage=batches._no_lineage("missing"), idempotency_key=proposal,
                now=clock[0] - 120,
            )
    first = await handler.execute("batch_reconcile", {})
    assert len(first["errors"]) == 20 and not first["recovered"], first
    second = await handler.execute("batch_reconcile", {})
    assert len(second["errors"]) == 1 and second["recovered"][0]["batch_id"] == bid, second
    assert not (await handler.execute("batch_reconcile", {}))["errors"]


async def test_batch_reconcile_preserves_exhausted_dispatch_and_refuses_session_authority(env, monkeypatch):
    handler, db, bid, decision, _ = await held_batch(env, monkeypatch)
    async with db.immediate() as conn:
        await conn.execute(update(escalation_actions).where(
            escalation_actions.c.decision_id == decision["id"],
            escalation_actions.c.operation_kind == FIDELITY_REVIEW_DISPATCH,
        ).values(status="failed", outcome="failed", completed_at=time.time()))
    assert not (await handler.execute("batch_reconcile", {}))["recovered"]
    assert not await batch_events(db)
    assert not (await handler.execute("batch_reconcile", {"batch_id": bid}))["success"]
    refusal = await _scoped(handler, "batch_reconcile", {}, session_id="supervisor")
    assert not refusal.get("success"), refusal


async def test_ask_maintenance_invokes_batch_reconcile_after_intake_failure():
    from unittest.mock import AsyncMock

    seen = []

    async def execute(name, args):
        seen.append((name, args))
        if name == "ask_reconcile":
            raise RuntimeError("intake failed")
        assert current_principal().service_name == "batch-reconcile"

    harness = SimpleNamespace(_command_handler=SimpleNamespace(execute=execute),
                              _last_conversation_maintenance=time.time(),
                              db=SimpleNamespace(list_conversations=AsyncMock(return_value=[])))
    await Orchestrator._maintain_conversations(harness, now=time.time())
    assert seen == [("ask_reconcile", {"apply": True, "limit": 100}), ("batch_reconcile", {})]


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


@pytest.mark.parametrize("damage", [None, "cycle", "missing", "cross-project", "container"])
async def test_external_coverage_precedes_review_gate_release(env, monkeypatch, damage):
    from src.models import Task, TaskStatus
    from src.database.queries.proposal_queries import existing_graph_edges

    handler, db = env
    result = await handler.execute("review_submit", {
        "project_id": "p", "kind": "spec", "title": "Implementation prerequisites",
        "content": "---\nspec_kind: implementation\n---\n# Implementation\nPinned requirements.",
    })
    assert result["success"], result
    rid = result["review_id"]
    review = await db.get_review(rid)
    for tid in ("waiting-a", "waiting-b"):
        await db.create_task(Task(id=tid, project_id="p", title=tid, description="", status=TaskStatus.DEFINED))
    async with db.immediate() as conn:
        await db.attach_gate_waiters(review["gate_id"], ["waiting-a", "waiting-b"], conn=conn)
    cid = await comment(handler, rid, "Required external implementation prerequisite")
    args, preview = await prepared(handler, rid, dispositions=[{
        "feedback_key": cid, "disposition": "follow_up", "required_before_release": True,
        "external_prerequisite": True, "reason": "Separate implementation; no document edits",
    }])
    original = ReviewOperations.finalize

    async def inspect_before_release(self, decision):
        async with db._engine.connect() as conn:
            filing = await get_operation(conn, decision["id"], "file_task")
        target = filing["receipt"]["task_id"]
        assert (await db.get_gate(review["gate_id"]))["status"] == "open"
        assert all([(await db.get_task(tid)).is_blocked for tid in ("waiting-a", "waiting-b")])
        if damage == "cycle":
            await db.add_dependency(target, "waiting-a")
        elif damage == "missing":
            await db.update_task(target, status=TaskStatus.COMPLETED)
            assert await db.archive_task(target)
        elif damage == "cross-project":
            await db.update_task(target, project_id="other")
        elif damage == "container":
            await db.create_task(Task(id="waiter-child", project_id="p", title="Child", description="",
                                      parent_task_id="waiting-a"))
            async with db.immediate() as conn:
                await db.mark_container("waiting-a", conn=conn)
        return await original(self, decision)

    monkeypatch.setattr(ReviewOperations, "finalize", inspect_before_release)
    applied = await accept(handler, args, preview)
    if damage:
        assert not applied["success"] and applied["application_state"] == "failed", applied
        assert (await db.get_gate(review["gate_id"]))["status"] == "open"
        assert all([(await db.get_task(tid)).is_blocked for tid in ("waiting-a", "waiting-b")])
        assert (await db.get_review(rid))["state"] == "in_review"
    else:
        assert applied["success"], applied
        assert (await db.get_gate(review["gate_id"]))["status"] == "resolved"
        async with db._engine.connect() as conn:
            [obligation] = await db.list_review_feedback_obligations(applied["decision_id"], conn=conn)
        edges = await existing_graph_edges(db, "p")
        assert {(f, t) for f, t, k in edges if k == "blocks"} == {
            ("waiting-a", obligation["task_id"]), ("waiting-b", obligation["task_id"]),
        }
        assert all([(await db.get_task(tid)).is_blocked for tid in ("waiting-a", "waiting-b")])
