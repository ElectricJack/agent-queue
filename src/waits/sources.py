"""Transactional source outbox, independent of wait activation flags.

Source keys are ``kind:identity`` (messages use their thread identity). A generation
identifies an incarnation; a revision identifies one mutation in that incarnation.
Consumers must observe the authoritative source again, rather than treating an event
payload as a verdict. No agent/session/command imports belong in this module.

Keys: task:<id> uses claim_epoch; dependency:<dependent-id>, job:<id>,
message:<thread-id>, playbook_condition:<run-id>, ci_check:<repo>:<sha>:<name>,
and resource:<workspace|provider|pool>:<id> use generation 0. Review:<id> uses
the document revision; batch:<id> uses baseline_generation; timers reuse the
registered source identity. Resource cycle identities are revisions. Config uses
resource:config. wait_source_watermark supplies the safe bound for cursor drains.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

# Writers share this lock until commit. A short exclusive watermark read waits
# for their commits before exposing MAX(seq), without serializing source writers.
_SOURCE_SEQUENCE_LOCK = 0x415157414954


def payload_hash(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest()


async def record_source_event(
    conn: AsyncConnection,
    *,
    source_key: str,
    generation: int,
    revision: str,
    outcome_ref: str,
    payload: dict,
    occurred_at: float | None = None,
) -> int | None:
    """Write and notify on the caller's transaction; a replay returns ``None``.

    Call only after the source's guarded write succeeds, before committing. The
    shared transaction lock fences safe watermark reads from in-flight events.
    PostgreSQL discards both the row and NOTIFY on rollback. Payloads contain
    evidence identities/state, never message bodies, configuration or credentials.
    """
    from src.database.tables import wait_source_events

    if not source_key or not revision or not outcome_ref or generation < 0:
        raise ValueError("source identity, nonnegative generation and revision are required")
    digest = payload_hash(payload)
    await conn.execute(
        text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": _SOURCE_SEQUENCE_LOCK}
    )
    now = time.time()
    seq = (await conn.execute(
        insert(wait_source_events).values(
            id="wse-" + uuid.uuid4().hex,
            source_key=source_key, source_generation=generation, source_revision=revision,
            occurred_at=now if occurred_at is None else occurred_at, committed_at=now,
            outcome_ref=outcome_ref, payload_hash=digest, payload=payload,
        ).on_conflict_do_nothing(constraint="uq_wait_source_events_revision")
        .returning(wait_source_events.c.seq)
    )).scalar_one_or_none()
    if seq is not None:
        await conn.execute(
            text("SELECT pg_notify('aq_wait_source', :seq)"), {"seq": str(seq)}
        )
    return seq


async def record_task_transition(conn, task_id: str, old_status: str, new_status: str) -> None:
    """Called once after a successful status CAS, on its connection."""
    from src.database.tables import tasks

    if old_status == new_status:
        return
    row = (await conn.execute(select(
        tasks.c.project_id, tasks.c.claim_epoch, tasks.c.updated_at,
    ).where(tasks.c.id == task_id))).mappings().one()
    await record_source_event(
        conn, source_key=f"task:{task_id}", generation=row["claim_epoch"] or 0,
        revision=uuid.uuid4().hex, outcome_ref=f"task:{task_id}",
        occurred_at=row["updated_at"], payload={
            "project_id": row["project_id"], "task_id": task_id,
            "previous_status": old_status, "status": new_status,
        },
    )
    await record_dependency_source_change(conn, task_id, "status_changed")


async def execute_task_status_mutation(conn, statement):
    """Cover guarded legacy bulk status writers outside the task query mixin."""
    from src.database.tables import tasks

    previous = (await conn.execute(select(tasks.c.id, tasks.c.status).where(
        statement.whereclause,
    ).with_for_update())).all()
    result = await conn.execute(statement)
    if result.rowcount:
        for task_id, old_status in previous:
            new_status = await conn.scalar(select(tasks.c.status).where(tasks.c.id == task_id))
            await record_task_transition(conn, task_id, old_status, new_status)
    return result


async def insert_message_with_source_event(conn, statement):
    """Execute message INSERTs, preserving single-row RETURNING and bulk rowcount.

    All message writers use this seam, including internal durable notifications and
    collaboration replies. Inspect the persisted metadata only after an actual insert;
    ON CONFLICT no-ops never manufacture a second event. The bound id is required
    because the original RETURNING result belongs to the caller, not this helper.
    Bulk notification writers have no RETURNING consumers; add one there to identify
    precisely the rows inserted, rather than replaying existing conflict rows.
    """
    from src.database.tables import messages

    message_id = statement.compile().params.get("id")
    if message_id is None:
        if statement._returning:
            raise ValueError("bulk message source writes cannot have RETURNING consumers")
        statement = statement.returning(messages.c.id)
    result = await conn.execute(statement)
    ids = [message_id] if message_id is not None and result.rowcount else []
    if message_id is None:
        ids = list(result.scalars())
    if ids:
        rows = (await conn.execute(select(
            messages.c.id,
            messages.c.thread_id, messages.c.created_seq, messages.c.created_at,
            messages.c.project_id, messages.c.to_kind, messages.c.to_id,
            messages.c.from_kind, messages.c.from_id, messages.c.reply_to_id,
            messages.c.body_kind,
        ).where(messages.c.id.in_(ids)).order_by(messages.c.created_seq))).mappings().all()
        for row in rows:
            await record_source_event(
                conn, source_key=f"message:{row['thread_id'] or row['id']}", generation=0,
                revision=str(row["created_seq"]), outcome_ref=f"message:{row['id']}",
                occurred_at=row["created_at"], payload={"message_id": row["id"], **dict(row)},
            )
    return result


async def record_dependency_change(conn, task_id, depends_on, dep_type, action):
    await record_source_event(
        conn, source_key=f"dependency:{task_id}", generation=0, revision=uuid.uuid4().hex,
        outcome_ref=f"task:{task_id}", payload={
            "task_id": task_id, "depends_on_task_id": depends_on,
            "dep_type": dep_type, "action": action,
        },
    )


async def record_dependency_source_change(conn, source_task_id, action):
    """An existing edge can become satisfied without the edge itself changing."""
    from src.database.tables import task_dependencies

    rows = (await conn.execute(select(task_dependencies).where(
        task_dependencies.c.depends_on_task_id == source_task_id,
    ))).mappings().all()
    for row in rows:
        await record_dependency_change(conn, row["task_id"], source_task_id,
                                       row["dep_type"], action)


async def record_playbook_snapshot(conn, snapshot, receipt=None):
    await record_source_event(
        conn, source_key=f"playbook_condition:{snapshot.run_id}", generation=0,
        revision=str(snapshot.version), outcome_ref=f"playbook:{snapshot.run_id}",
        occurred_at=snapshot.updated_at,
        payload={"run_id": snapshot.run_id, "lifecycle": snapshot.lifecycle.value,
                 "step_id": snapshot.current_step_id,
                 "wait_id": receipt.wait_id if receipt else None,
                 "outcome": receipt.outcome if receipt else None},
    )


async def record_batch_event(conn, batch_id):
    from src.database.tables import integration_batch_members, integration_batches, wait_source_events

    row = (await conn.execute(select(integration_batches).where(
        integration_batches.c.id == batch_id,
    ).with_for_update())).mappings().first()
    if row is None:
        return
    payload = {"batch_id": batch_id, "project_id": row["project_id"],
               "lifecycle": row["lifecycle"], "revision": row["current_revision"]}
    previous = (await conn.execute(select(wait_source_events).where(
        wait_source_events.c.source_key == f"batch:{batch_id}",
    ).order_by(wait_source_events.c.seq.desc()).limit(1))).mappings().first()
    if (previous is not None and previous["source_generation"] == row["baseline_generation"]
            and previous["payload"] == payload):
        return
    await record_source_event(
        conn, source_key=f"batch:{batch_id}", generation=row["baseline_generation"],
        revision=uuid.uuid4().hex,
        outcome_ref=f"batch:{batch_id}", occurred_at=row["updated_at"],
        payload=payload,
    )
    members = (await conn.execute(select(integration_batch_members.c.task_id).where(
        integration_batch_members.c.batch_id == batch_id,
    ))).scalars().all()
    for task_id in members:
        await record_dependency_source_change(conn, task_id, "delivery_changed")


async def execute_batch_mutation(conn, statement):
    """Retain SQL results while recording every successful batch lifecycle write.

    Several batch writers don't enqueue integration events. Lock their selected rows
    before the mutation so a guarded update and its source record describe the same
    rows; the result remains available to callers using RETURNING or rowcount.
    """
    from sqlalchemy.sql.dml import Insert

    from src.database.tables import integration_batches

    params = statement.compile().params
    if isinstance(statement, Insert):
        ids = [params["id"]]
    elif "lifecycle" in params:
        ids = list((await conn.execute(select(integration_batches.c.id).where(
            statement.whereclause,
        ).with_for_update())).scalars())
    else:
        return await conn.execute(statement)
    result = await conn.execute(statement)
    if result.rowcount:
        for batch_id in ids:
            await record_batch_event(conn, batch_id)
    return result


async def execute_workspace_release(conn, statement):
    """Record releases for the actual workspace identities on the owning transaction."""
    from src.database.tables import workspaces

    rows = (await conn.execute(select(
        workspaces.c.id, workspaces.c.project_id,
    ).where(statement.whereclause, or_(workspaces.c.locked_by_task_id.is_not(None),
                                    workspaces.c.locked_by_agent_id.is_not(None)))
        .with_for_update())).mappings().all()
    result = await conn.execute(statement)
    for row in rows:
        await record_source_event(
            conn, source_key=f"resource:workspace:{row['id']}", generation=0,
            revision=uuid.uuid4().hex, outcome_ref=f"workspace:{row['id']}",
            payload={"workspace_id": row["id"], "project_id": row["project_id"],
                     "state": "released"},
        )
    return result


async def record_pool_observations(db, measurement, now):
    """Persist sizing inputs at the cycle boundary; size_pools remains pure policy."""
    from dataclasses import asdict

    for key, supply in measurement.supply.items():
        await db.wait_source_commit(
            source_key=f"resource:pool:{key.profile_id}", generation=0, revision=str(now),
            outcome_ref=f"pool:{key.profile_id}", occurred_at=now,
            payload={"profile_id": key.profile_id, "supply": asdict(supply),
                     "demand": measurement.demand.get(key, 0),
                     "bounds": list(measurement.bounds[key])},
        )
