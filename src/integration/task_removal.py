"""Retire orphan operations without changing Git refs or audit history.

These short maintenance transactions share the publishers' engine locks.
"""

from __future__ import annotations

import json

from sqlalchemy import or_, select, text, update

from src.database import tables as t
from src.database.queries.integration_state_queries import session_attached_clause
from src.integration.delegate_release import LIVE_OPERATION_STATES, _owned_by, release_delegates_on
from src.integration.owner_guards import parent_lock_key


async def _cancel_orphan_on(db, conn, operation, *, now, reason, legacy_engine_gone=False):
    """A durable live Subject or attached writer is ownership, never an orphan."""
    subject = t.integration_subjects
    identity = (
        (subject.c.task_id == operation["parent_task_id"])
        & (subject.c.kind == "parent_episode")
        if operation["target_kind"] == "parent"
        else subject.c.batch_id == operation["batch_id"]
    )
    subjects = (await conn.execute(select(subject).where(identity).with_for_update())).mappings().all()
    if not legacy_engine_gone and any(
        row["engine"] == "reconciler" and row["phase"] != "done" for row in subjects
    ):
        return False
    operation_ids = select(t.integration_repair_operations.c.id).where(
        t.integration_repair_operations.c.id == operation["id"],
        _owned_by(t.integration_repair_operations, t.tasks.c.id),
    ).exists()
    writer_ids = select(t.tasks.c.id).where(or_(
        operation_ids, t.tasks.c.id == operation["parent_task_id"],
        t.tasks.c.id.in_([row["writer_task_id"] for row in subjects if row["writer_task_id"]]),
    ))
    ids = list((await conn.execute(writer_ids)).scalars())
    # Freeze claim assignment while proving the operation has no writer.
    sessions = (await conn.execute(select(t.sessions).where(or_(
        t.sessions.c.task_id.in_(ids),
        t.sessions.c.id.in_([row["writer_session_id"] for row in subjects if row["writer_session_id"]]),
    )).order_by(t.sessions.c.id).with_for_update())).mappings().all()
    tasks = (await conn.execute(select(t.tasks).where(
        t.tasks.c.id.in_(ids),
    ).order_by(t.tasks.c.id).with_for_update())).mappings().all()
    attached = await conn.scalar(select(t.sessions.c.id).where(
        t.sessions.c.id.in_([row["id"] for row in sessions]), session_attached_clause(),
    ).limit(1))
    if any(row["assigned_agent_id"] for row in tasks) or attached:
        return False
    await conn.execute(update(t.integration_repair_operations).where(
        t.integration_repair_operations.c.id == operation["id"],
        t.integration_repair_operations.c.state.in_(LIVE_OPERATION_STATES),
    ).values(state="cancelled", updated_at=now))
    # A legacy Subject is only a projection; there is no engine to revisit it.
    for row in subjects:
        if row["phase"] != "done":
            await db.update_integration_subject_on(
                conn, subject_id=row["id"], expected_version=row["version"],
                values={"phase": "done", "next_due_at": None, "gate_id": None,
                        "closed_reason": reason, "wait_reason": None}, now=now,
            )
    await db.log_event(
        "integration.orphan_cancelled", task_id=operation["parent_task_id"],
        payload=json.dumps({"operation_id": operation["id"], "reason": reason}), conn=conn,
    )
    return True


async def settle_orphaned_parent_operations(db, now, *, legacy_engine_gone=False, limit=100):
    """Maintenance backstop for parent operations with no runtime or writer."""
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(t.integration_repair_operations).where(
            t.integration_repair_operations.c.target_kind == "parent",
            t.integration_repair_operations.c.state.in_(LIVE_OPERATION_STATES),
        ).order_by(t.integration_repair_operations.c.updated_at).limit(limit))).mappings().all()
    cancelled = []
    for row in rows:
        transitions = []
        async with db.immediate() as conn:
            # Exclusive with parent bootstrap and subject runtime mutations.
            available = await conn.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                          {"key": parent_lock_key(row["parent_task_id"])})
            if not available:
                continue
            current = (await conn.execute(select(t.integration_repair_operations).where(
                t.integration_repair_operations.c.id == row["id"],
                t.integration_repair_operations.c.state.in_(LIVE_OPERATION_STATES),
            ).with_for_update())).mappings().first()
            if current and await _cancel_orphan_on(
                db, conn, current, now=now, legacy_engine_gone=legacy_engine_gone,
                reason="Parent operation has no surviving runtime or writer",
            ):
                cancelled.append(row["id"])
                # Retire queued delegates in this transaction before another
                # claim can attach to the now-cancelled operation's work.
                _, transitions = await release_delegates_on(
                    db, conn, now=now, released_by="orphan reconciliation",
                    operation_ids=[row["id"]],
                )
        for transition in transitions:
            await db.log_blocked_flips(transition.flipped)
            await db._notify_settled(transition.settled)
            await db._notify_ready(transition.ready)
    return cancelled
