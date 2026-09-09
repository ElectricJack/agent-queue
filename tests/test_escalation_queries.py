"""Durable escalation, reply, delivery, and digest persistence on PostgreSQL."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from src.database import Database
from src.database.queries.escalation_queries import EscalationConflict, EscalationStateError
from src.database.tables import escalation_messages, messages
from src.models import Project, Task, TaskStatus
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db():
    database = Database(lease_dsn("escalations"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Project"))
    yield database
    await database.close()


def incident(*, ident: str = "attempt-1", escalation_id: str = "esc-1") -> dict:
    return {
        "id": escalation_id,
        "project_id": "p",
        "task_id": "t",
        "source_kind": "task_attempt",
        "source_identity": ident,
        "incident_key": f"task:t:attempt:{ident}",
        "supervisor_owner": "supervisor-p",
        "task_title": "Deploy safely",
        "task_status": "BLOCKED",
        "summary": "Deployment is blocked",
        "investigation": "Validated staging and reproduced the failure",
        "decision_requested": "Approve the documented production change?",
        "choices": ["approve", "keep blocked"],
        "severity": "high",
        "now": 10.0,
    }


async def make_incident(db: Database, **overrides):
    ident = overrides.pop("ident", "attempt-1")
    escalation_id = overrides.pop("escalation_id", "esc-1")
    values = incident(ident=ident, escalation_id=escalation_id)
    values.update(overrides)
    return await db.create_escalation(**values)


async def test_source_incident_replay_is_idempotent_but_new_attempt_is_distinct(db):
    first, created = await make_incident(db)
    replay, replay_created = await make_incident(db, escalation_id="ignored-replay-id")
    second, second_created = await make_incident(
        db,
        ident="attempt-2",
        escalation_id="esc-2",
        incident_key="task:t:attempt:attempt-2",
    )

    assert created is True and replay_created is False and second_created is True
    assert replay["id"] == first["id"] == "esc-1"
    assert second["id"] == "esc-2"
    assert len(await db.list_escalations(project_id="p")) == 2

    with pytest.raises(EscalationConflict, match="source_identity"):
        await make_incident(
            db, ident="different-source", incident_key="task:t:attempt:attempt-1"
        )


async def test_soft_task_reference_survives_archive_and_terminal_reply_stays_closed(db):
    await db.create_task(
        Task(
            id="t",
            project_id="p",
            title="Deploy safely",
            description="Blocked deployment",
            status=TaskStatus.BLOCKED,
        )
    )
    row, _ = await make_incident(db)
    assert await db.archive_task("t") is True
    assert (await db.get_escalation(row["id"]))["task_id"] == "t"

    resolved = await db.transition_escalation(
        row["id"],
        expected_revision=0,
        new_state="cancelled",
        terminal_outcome="operator kept the work blocked",
        terminal_evidence={"decision": "keep blocked"},
        now=20.0,
    )
    late = await db.accept_escalation_reply(
        row["id"],
        transport="discord",
        external_message_id="late-1",
        verified_actor="discord:user-7",
        text="Actually, proceed",
        received_at=21.0,
    )

    assert resolved["state"] == "cancelled" and resolved["revision"] == 1
    assert late["terminal"] is True and late["supervisor_enqueued"] is False
    assert late["escalation"]["state"] == "cancelled"
    assert len(await db.list_escalation_messages(row["id"])) == 1
    assert await db.get_pending_messages("session", "supervisor-p") == []


async def test_reply_replay_is_idempotent_and_revision_fences_supervisor_transition(db):
    row, _ = await make_incident(db)
    first_reply = await db.accept_escalation_reply(
        row["id"], transport="dashboard", external_message_id="dashboard-message-1",
        verified_actor="user:operator", text="Please investigate the staged option",
        received_at=11.0,
    )
    resolving = await db.transition_escalation(
        row["id"], expected_revision=first_reply["escalation"]["revision"],
        new_state="resolving", now=12.0
    )

    reply = await db.accept_escalation_reply(
        row["id"],
        transport="discord",
        external_message_id="discord-message-42",
        verified_actor="discord:user-7",
        text="Use the staged option",
        received_sequence=42,
        received_at=13.0,
    )
    replay = await db.accept_escalation_reply(
        row["id"],
        transport="discord",
        external_message_id="discord-message-42",
        verified_actor="discord:user-7",
        text="Use the staged option",
        received_sequence=42,
        received_at=14.0,
    )

    assert reply["created"] is True and replay["created"] is False
    assert reply["escalation"]["state"] == "reply_received"
    assert reply["escalation"]["revision"] == resolving["revision"] + 1
    assert replay["reply"]["id"] == reply["reply"]["id"]
    pending = await db.get_pending_messages("session", "supervisor-p")
    assert len(pending) == 2 and pending[-1].body == "Use the staged option"
    assert len(await db.list_escalation_messages(row["id"])) == 2

    # The supervisor's pre-reply revision cannot resolve over unseen evidence.
    assert (
        await db.transition_escalation(
            row["id"],
            expected_revision=resolving["revision"],
            new_state="resolved",
            terminal_outcome="stale action",
            now=15.0,
        )
        is None
    )

    with pytest.raises(EscalationConflict, match="text"):
        await db.accept_escalation_reply(
            row["id"],
            transport="discord",
            external_message_id="discord-message-42",
            verified_actor="discord:user-7",
            text="different replay content",
        )


async def test_reply_and_supervisor_enqueue_roll_back_together(db):
    row, _ = await make_incident(db)
    # Force the middle statement in reply acceptance to fail. The surrounding
    # transaction must leave neither the immutable reply nor a revision bump.
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(messages).values(
                id="msg-fixed-reply",
                project_id="p",
                from_kind="system",
                from_id="test",
                to_kind="session",
                to_id="supervisor-p",
                body="collision",
                created_at=9.0,
            )
        )

    with pytest.raises(IntegrityError):
        await db.accept_escalation_reply(
            row["id"],
            transport="dashboard",
            external_message_id="dashboard-reply-1",
            verified_actor="user:operator",
            text="Proceed",
            reply_id="fixed-reply",
            received_at=11.0,
        )

    saved = await db.get_escalation(row["id"])
    assert saved["state"] == "needs_human" and saved["revision"] == 0
    assert await db.list_escalation_messages(row["id"]) == []
    async with db._engine.connect() as conn:
        count = await conn.scalar(select(func.count()).select_from(escalation_messages))
    assert count == 0


async def test_cas_allows_only_one_concurrent_supervisor_transition(db):
    row, _ = await make_incident(db)
    peer = Database(db._dsn)
    await peer.initialize()
    try:
        winners = await asyncio.gather(
            db.transition_escalation(
                row["id"], expected_revision=0, new_state="stale",
                terminal_outcome="source disappeared", now=11.0
            ),
            peer.transition_escalation(
                row["id"], expected_revision=0, new_state="cancelled",
                terminal_outcome="cancelled concurrently", now=11.0
            ),
        )
    finally:
        await peer.close()
    assert sum(result is not None for result in winners) == 1
    assert (await db.get_escalation(row["id"]))["revision"] == 1


async def test_delivery_dedup_lease_recovery_receipts_and_unknown_are_durable(db):
    row, _ = await make_incident(db)
    delivery, created = await db.enqueue_escalation_delivery(
        row["id"],
        dedup_key="escalation:esc-1:root:0",
        kind="root",
        payload={"summary": "Deployment is blocked"},
        available_at=10.0,
        delivery_id="delivery-1",
    )
    replay, replay_created = await db.enqueue_escalation_delivery(
        row["id"],
        dedup_key="escalation:esc-1:root:0",
        kind="root",
        payload={"summary": "Deployment is blocked"},
        available_at=10.0,
        delivery_id="ignored",
    )
    assert created is True and replay_created is False and replay["id"] == delivery["id"]

    first = await db.claim_escalation_deliveries(
        lease_owner="sender-1", now=10.0, lease_seconds=5.0
    )
    assert first[0]["attempt_count"] == 1
    assert await db.claim_escalation_deliveries(lease_owner="sender-2", now=14.0) == []
    recovered = await db.claim_escalation_deliveries(lease_owner="sender-2", now=16.0)
    assert recovered[0]["id"] == delivery["id"] and recovered[0]["attempt_count"] == 2
    assert (
        await db.finish_escalation_delivery(
            delivery["id"], lease_owner="sender-1", status="unknown", now=16.0
        )
        is None
    )
    retried = await db.finish_escalation_delivery(
        delivery["id"],
        lease_owner="sender-2",
        status="retry",
        next_attempt_at=30.0,
        last_error="rate limited",
        now=17.0,
    )
    assert retried["status"] == "retry" and retried["lease_owner"] is None
    assert await db.claim_escalation_deliveries(lease_owner="sender-3", now=29.0) == []
    final_claim = await db.claim_escalation_deliveries(lease_owner="sender-3", now=30.0)
    sent = await db.finish_escalation_delivery(
        final_claim[0]["id"],
        lease_owner="sender-3",
        status="sent",
        external_receipt_id="discord:root-7",
        channel_id="channel-1",
        root_message_id="root-7",
        thread_id="thread-7",
        now=31.0,
    )
    assert sent["status"] == "sent" and sent["receipt_confirmed_at"] == 31.0
    assert (await db.get_escalation_delivery(delivery["id"]))["thread_id"] == "thread-7"
    assert len(await db.list_escalation_deliveries(row["id"], statuses=["sent"])) == 1

    unknown, _ = await db.enqueue_escalation_delivery(
        row["id"],
        dedup_key="escalation:esc-1:followup:1",
        kind="followup",
        payload={"text": "Checking"},
        available_at=40.0,
    )
    await db.claim_escalation_deliveries(lease_owner="sender-4", now=40.0)
    await db.finish_escalation_delivery(
        unknown["id"], lease_owner="sender-4", status="unknown", now=41.0,
        last_error="send result ambiguous"
    )
    assert await db.claim_escalation_deliveries(lease_owner="sender-5", now=1000.0) == []


async def test_digest_windows_reserve_suppression_and_recover_expired_lease(db):
    window, created = await db.reserve_digest_window(
        destination="discord:channel-1",
        config_generation=3,
        window_start=0.0,
        window_end=3600.0,
        activity_cursor={"event_id": "evt-10"},
        due_at=3600.0,
        window_id="window-1",
    )
    replay, replay_created = await db.reserve_digest_window(
        destination="discord:channel-1",
        config_generation=3,
        window_start=0.0,
        window_end=3600.0,
        activity_cursor={"event_id": "evt-10"},
        due_at=3600.0,
    )
    assert created is True and replay_created is False and replay["id"] == window["id"]

    evaluated = await db.complete_digest_evaluation(
        window["id"],
        activity_cursor={"event_id": "evt-20"},
        output_hash="sha256:digest",
        payload={"text": "One task completed"},
        suppression_reason=None,
        now=3500.0,
    )
    assert evaluated["payload"]["text"] == "One task completed"
    first = await db.claim_digest_windows(
        lease_owner="digest-1", now=3600.0, lease_seconds=10.0
    )
    assert first[0]["attempt_count"] == 1
    assert await db.claim_digest_windows(lease_owner="digest-2", now=3609.0) == []
    recovered = await db.claim_digest_windows(lease_owner="digest-2", now=3611.0)
    assert recovered[0]["attempt_count"] == 2
    assert (
        await db.finish_digest_delivery(
            window["id"], lease_owner="digest-1", status="unknown", now=3612.0
        )
        is None
    )
    sent = await db.finish_digest_delivery(
        window["id"], lease_owner="digest-2", status="sent", now=3612.0,
        external_receipt_id="discord:digest-1"
    )
    assert sent["send_status"] == "sent"
    assert (await db.get_digest_window(window["id"]))["external_receipt_id"] == "discord:digest-1"
    assert len(await db.list_digest_windows(statuses=["sent"])) == 1

    silent, _ = await db.reserve_digest_window(
        destination="discord:channel-1",
        config_generation=3,
        window_start=3600.0,
        window_end=7200.0,
        activity_cursor={"event_id": "evt-20"},
        due_at=7200.0,
        window_id="window-2",
    )
    suppressed = await db.complete_digest_evaluation(
        silent["id"],
        activity_cursor={"event_id": "evt-20"},
        output_hash=None,
        payload=None,
        suppression_reason="no eligible activity",
        now=7100.0,
    )
    assert suppressed["send_status"] == "suppressed"
    assert await db.claim_digest_windows(lease_owner="digest-3", now=8000.0) == []


async def test_invalid_terminal_and_delivery_transitions_fail_before_mutation(db):
    row, _ = await make_incident(db)
    reply = await db.accept_escalation_reply(
        row["id"], transport="dashboard", external_message_id="r-invalid",
        verified_actor="user:operator", text="Proceed"
    )
    resolving = await db.transition_escalation(
        row["id"], expected_revision=reply["escalation"]["revision"], new_state="resolving"
    )
    with pytest.raises(EscalationStateError, match="requires an outcome"):
        await db.transition_escalation(
            row["id"], expected_revision=resolving["revision"], new_state="resolved"
        )
    delivery, _ = await db.enqueue_escalation_delivery(
        row["id"], dedup_key="d", kind="root", payload={}, available_at=0.0
    )
    claimed = await db.claim_escalation_deliveries(lease_owner="sender", now=1.0)
    assert claimed[0]["id"] == delivery["id"]
    with pytest.raises(EscalationStateError, match="confirmed external receipt"):
        await db.finish_escalation_delivery(
            delivery["id"], lease_owner="sender", status="sent", now=2.0
        )
