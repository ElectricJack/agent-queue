"""Foundational packet/idempotency, durable audit and operation lease invariants."""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from src.asks.models import AskError, DecisionActor, DecisionIntent
from src.asks.service import version_of
from src.database.tables import (
    ask_decisions,
    doc_review_revisions,
    doc_reviews,
    escalation_actions,
    escalation_messages,
    human_asks,
)
from tests.ask_helpers import db as db_fixture, service as service_fixture  # noqa: F401
from tests.ask_helpers import (
    actor,
    evidence,
    snapshot,
)


async def prepared(db, service, *, conn):
    source = snapshot().identity
    publication = await service.publish(source, conn=conn)
    intent = DecisionIntent("approve", version_of(publication.ask))
    preview = await service.prepare_decision(source, actor(), intent, conn=conn)
    eid = await evidence(db, publication.ask, conn=conn)
    return source, intent, preview, eid


async def reserve(db, service, *, conn, key="client-1"):
    source, intent, preview, eid = await prepared(db, service, conn=conn)
    return await service.reserve_decision(
        source,
        actor(),
        intent,
        preview_id=preview["id"],
        idempotency_key=key,
        evidence_entry_id=eid,
        conn=conn,
    )


async def test_decision_is_reserved_once_and_remains_unapplied(db, service):
    async with db._engine.begin() as conn:
        source, intent, preview, eid = await prepared(db, service, conn=conn)
        kwargs = dict(
            preview_id=preview["id"], idempotency_key="click", evidence_entry_id=eid, conn=conn
        )
        first, created = await service.reserve_decision(source, actor(), intent, **kwargs)
        again, replay_created = await service.reserve_decision(source, actor(), intent, **kwargs)
        assert created and not replay_created and first == again
        assert first["packet"]["domain"]["review_revision"] == 1
        operation = (await conn.execute(select(escalation_actions))).mappings().one()
        assert operation["status"] == "pending" and operation["decision_id"] == first["id"]
        assert (
            await conn.execute(select(func.count()).select_from(ask_decisions))
        ).scalar_one() == 1
    assert (await db.get_review("rev-ask"))["state"] == "in_review"


async def test_concurrent_decisions_reserve_one_generation(db, service):
    async with db._engine.begin() as conn:
        source, intent, preview, eid = await prepared(db, service, conn=conn)

    async def decide(key):
        async with db._engine.begin() as conn:
            return await service.reserve_decision(
                source,
                actor(),
                intent,
                preview_id=preview["id"],
                idempotency_key=key,
                evidence_entry_id=eid,
                conn=conn,
            )

    results = await asyncio.gather(decide("same"), decide("same"))
    assert sorted(created for _, created in results) == [False, True]
    assert results[0][0]["id"] == results[1][0]["id"]


async def test_idempotency_key_rejects_changed_payload(db, service):
    async with db._engine.begin() as conn:
        source, intent, preview, eid = await prepared(db, service, conn=conn)
        kwargs = dict(
            preview_id=preview["id"], idempotency_key="key", evidence_entry_id=eid, conn=conn
        )
        await service.reserve_decision(source, actor(), intent, **kwargs)
        with pytest.raises(AskError) as err:
            await service.reserve_decision(
                source, actor(), replace(intent, overall_note="New condition"), **kwargs
            )
        assert err.value.code == "ask.idempotency_conflict"


@pytest.mark.parametrize(
    "change,code",
    [
        ("feedback", "ask.stale_feedback"),
        ("revision", "ask.stale_source"),
        ("note", "ask.stale_preview"),
        ("expiry", "ask.stale_preview"),
        ("actor", "ask.not_decider"),
        ("input", "ask.stale_preview"),
    ],
)
async def test_preview_is_bound_and_revalidated(db, service, change, code):
    async with db._engine.begin() as conn:
        source, intent, preview, eid = await prepared(db, service, conn=conn)
        principal = actor()
        if change == "feedback":
            await conn.execute(update(doc_reviews).values(feedback_version=1))
        if change == "revision":
            await conn.execute(update(doc_reviews).values(current_revision=2))
        if change == "note":
            intent = replace(intent, overall_note="An added condition")
        if change == "expiry":
            service.clock = lambda: 1600
        if change == "actor":
            principal = DecisionActor("user:other", {"authenticated": "operator"})
        if change == "input":
            principal = replace(principal, input_id="another-input")
        with pytest.raises(AskError) as err:
            await service.reserve_decision(
                source,
                principal,
                intent,
                preview_id=preview["id"],
                idempotency_key="click",
                evidence_entry_id=eid,
                conn=conn,
            )
        assert err.value.code == code
        assert (await conn.execute(select(func.count()).select_from(ask_decisions))).scalar() == 0


async def test_decision_evidence_and_revision_are_pinned(db, service):
    async with db._engine.begin() as conn:
        decision, _ = await reserve(db, service, conn=conn)
        for table in (doc_review_revisions, doc_reviews, escalation_messages, human_asks):
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(delete(table))
        assert (await db.get_ask_decision(decision["id"], conn=conn))["packet"] == decision[
            "packet"
        ]


async def test_operations_dependency_lease_owner_generation_and_unknown(db, service):
    async with db._engine.begin() as conn:
        decision, _ = await reserve(db, service, conn=conn)
        op = dict((await conn.execute(select(escalation_actions))).mappings().one())
        child = {k: v for k, v in op.items() if k not in {"lease_owner", "lease_expires_at"}}
        child.update(
            id="child-op",
            operation_key="notify",
            operation_kind="notify",
            dependency_keys=[op["operation_key"]],
            idempotency_key="notify",
        )
        await db.insert_ask_operation(child, conn=conn)
        assert (
            await db.claim_ask_operation(
                "child-op", "worker", now=1000, lease_seconds=10, conn=conn
            )
            is None
        )
        claimed = await db.claim_ask_operation(
            op["id"], "owner", now=1000, lease_seconds=10, conn=conn
        )
        generation = claimed["lease_generation"]
        for owner, gen in (("other", generation), ("owner", generation - 1)):
            assert not await db.complete_ask_operation(
                op["id"],
                owner,
                gen,
                state="succeeded",
                receipt={"applied": True},
                now=1001,
                conn=conn,
            )
        assert await db.expire_ask_operation(op["id"], now=1010, conn=conn)
        assert not await db.complete_ask_operation(
            op["id"],
            "owner",
            generation,
            state="succeeded",
            receipt={"late": True},
            now=1011,
            conn=conn,
        )
        assert (
            await db.claim_ask_operation(op["id"], "retry", now=1011, lease_seconds=10, conn=conn)
            is None
        )
        with pytest.raises(AskError, match="retry safe"):
            await db.reconcile_ask_operation(
                op["id"], generation, state="pending", receipt={"observed": "unknown"}, conn=conn
            )
        assert await db.reconcile_ask_operation(
            op["id"],
            generation,
            state="pending",
            retry_safe=True,
            receipt={"source_receipt": "not_applied"},
            conn=conn,
        )
        retried = await db.claim_ask_operation(
            op["id"], "owner-2", now=1020, lease_seconds=10, conn=conn
        )
        assert retried["lease_generation"] == generation + 1
        assert await db.complete_ask_operation(
            op["id"],
            "owner-2",
            generation + 1,
            state="succeeded",
            receipt={"decision_id": decision["id"]},
            now=1021,
            conn=conn,
        )
        assert await db.claim_ask_operation(
            "child-op", "worker", now=1022, lease_seconds=10, conn=conn
        )


async def test_feedback_has_per_item_evidence_and_soft_task_reference(db, service):
    async with db._engine.begin() as conn:
        decision, _ = await reserve(db, service, conn=conn)
        obligation, created = await db.insert_review_feedback_obligation(
            {
                "id": "obligation-1",
                "decision_id": decision["id"],
                "feedback_key": "overall_note",
                "original_text": "Add retry evidence",
                "original_anchor": {},
                "original_author": "user:jack",
                "disposition": "follow_up",
                "accepted_by": {"evidence": "evidence-1"},
                "logical_owner": "supervisor:p-ask",
                "acceptance_text": "Verify retry result",
                "created_at": 1000,
                "updated_at": 1000,
            },
            conn=conn,
        )
        assert created
        assert await db.update_review_feedback_progress(
            obligation["id"],
            progress="filed",
            evidence=None,
            task_id="archived-task",
            now=1001,
            conn=conn,
        )
        with pytest.raises(ValueError, match="own completion evidence"):
            await db.update_review_feedback_progress(
                obligation["id"], progress="completed", evidence={}, now=1002, conn=conn
            )
        assert await db.update_review_feedback_progress(
            obligation["id"],
            progress="completed",
            evidence={"test": "passed item 1"},
            now=1003,
            conn=conn,
        )
        assert (await db.list_review_feedback_obligations(decision["id"], conn=conn))[0][
            "task_id"
        ] == "archived-task"


async def test_database_generation_and_actor_request_constraints(db, service):
    async with db._engine.begin() as conn:
        decision, _ = await reserve(db, service, conn=conn)
        with pytest.raises(IntegrityError, match="uq_ask_decisions_generation"):
            async with conn.begin_nested():
                await conn.execute(
                    insert(ask_decisions).values(
                        **{**decision, "id": "second", "idempotency_key": "another"}
                    )
                )
        with pytest.raises(IntegrityError, match="uq_ask_decisions_request"):
            async with conn.begin_nested():
                await conn.execute(
                    insert(ask_decisions).values(
                        **{**decision, "id": "second", "decision_generation": 2}
                    )
                )


async def test_replay_after_source_changes_returns_same_receipt_with_current_authority(db, service):
    async with db._engine.begin() as conn:
        source, intent, preview, eid = await prepared(db, service, conn=conn)
        kwargs = dict(
            preview_id=preview["id"], idempotency_key="replay", evidence_entry_id=eid, conn=conn
        )
        first, _ = await service.reserve_decision(source, actor(), intent, **kwargs)
        await conn.execute(update(doc_reviews).values(current_revision=2, feedback_version=5))
        service.clock = lambda: (
            2000
        )  # accepting an expired preview again is forbidden; receipt reads are safe
        replay, created = await service.reserve_decision(source, actor(), intent, **kwargs)
        assert replay == first and not created
        assert (await db.get_ask(first["ask_id"], conn=conn))["application_state"] == "applying"
        adapter = service.adapters.get("review")
        adapter.current = replace(adapter.current, decider={"principal": "user:other"})
        with pytest.raises(AskError) as err:
            await service.reserve_decision(source, actor(), intent, **kwargs)
        assert err.value.code == "ask.not_decider"


async def test_packet_and_projection_lock_source_before_ask(db, service):
    from sqlalchemy import event

    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        normalized = " ".join(statement.lower().split())
        if "doc_reviews" in normalized and "for update" in normalized:
            statements.append("source")
        if "insert into human_asks" in normalized or (
            "from human_asks" in normalized and "for update" in normalized
        ):
            statements.append("ask")

    event.listen(db._engine.sync_engine, "before_cursor_execute", record)
    try:
        async with db._engine.begin() as conn:
            source, intent, preview, eid = await prepared(db, service, conn=conn)
            statements.clear()
            await service.reserve_decision(
                source,
                actor(),
                intent,
                preview_id=preview["id"],
                idempotency_key="order",
                evidence_entry_id=eid,
                conn=conn,
            )
            assert statements[0] == "source"
            assert statements[1:] and set(statements[1:]) == {"ask"}
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", record)


async def test_decision_rejects_cross_project_revision_and_operation(db, service):
    from src.models import Project

    await db.create_project(Project(id="p-other", name="Other project"))
    async with db._engine.begin() as conn:
        first, _ = await reserve(db, service, conn=conn)
        # Even internal query callers cannot attach a reviewed decision to a
        # document from a different project by reusing an opaque revision id.
        await db.insert_review(
            review={
                "id": "rev-other",
                "project_id": "p-other",
                "kind": "spec",
                "title": "Other",
                "vault_path": "projects/p-other/specs/other.md",
                "state": "in_review",
                "created_at": 1,
                "updated_at": 1,
            },
            revision={
                "review_id": "rev-other",
                "revision": 1,
                "content": "Other",
                "content_sha256": "other-hash",
                "submitted_by": "user:jack",
                "submitted_at": 1,
            },
            conn=conn,
        )
        with pytest.raises(AskError, match="Review project/revision/hash"):
            await db.insert_ask_decision(
                {
                    **first,
                    "id": "different",
                    "decision_generation": 2,
                    "idempotency_key": "different",
                    "review_id": "rev-other",
                    "content_sha256": "other-hash",
                },
                conn=conn,
            )
        operation = dict((await conn.execute(select(escalation_actions))).mappings().one())
        other = (await service.publish_snapshot(snapshot("different-source"), conn=conn)).ask
        with pytest.raises(AskError, match="another ask"):
            await db.insert_ask_operation(
                {**operation, "id": "cross-operation", "ask_id": other["id"]}, conn=conn
            )
