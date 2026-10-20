"""Held-message cursor and in-place acknowledgement on a caller-owned transaction."""

import json

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from src.database.queries.wait_queries import REGISTRATION_LOCK, verify_owner
from src.database.tables import agents, messages, sessions, task_metadata, tasks, wait_records
from src.waits.conditions import WaitError
from src.waits.holding import WAKE_BODY_KINDS, addressed_to_wait
from src.waits.sources import execute_task_status_mutation


async def held_messages(conn, wait):
    delivery = (wait["decision_inputs"] or {}).get("delivery", {})
    rows = await conn.execute(select(messages).select_from(
        messages.join(wait_records, wait_records.c.id == wait["id"])
        .join(sessions, sessions.c.id == wait_records.c.session_id),
    ).where(
        addressed_to_wait(),
        messages.c.created_seq > delivery.get("held_cursor_seq", 0),
        messages.c.delivered_at.is_(None), messages.c.archived_at.is_(None),
        or_(messages.c.body_kind.is_(None), messages.c.body_kind.not_in(WAKE_BODY_KINDS)),
    ).order_by(messages.c.created_seq))
    return [dict(row) for row in rows.mappings()]


async def acknowledge_in_place(conn, *, wait_id, scope, now):
    """For the wait_ack command owner; continuation acknowledgements are separate.

    Replays return the same held bodies. Advancing the cursor, marking exactly
    those messages delivered, releasing protection and rebasing liveness commit
    together. Mail arriving after this snapshot remains in the ordinary inbox.
    """
    await conn.execute(select(func.pg_advisory_xact_lock(REGISTRATION_LOCK)))
    scope = await verify_owner(conn, scope)
    row = (await conn.execute(select(wait_records).where(
        wait_records.c.id == wait_id,
    ).with_for_update())).mappings().first()
    if row is None or any(row[key] != getattr(scope, key) for key in (
        "project_id", "owner_kind", "owner_id", "session_id", "claim_epoch",
    )):
        raise WaitError("continuation.stale_generation", "wait belongs to another holder")
    delivery = (row["decision_inputs"] or {}).get("delivery", {})
    if delivery.get("instance_token") != scope.instance_token:
        raise WaitError("continuation.stale_generation", "wait process changed")
    if row["sleep_decision"] != "in_place":
        raise WaitError("continuation.stale_generation", "continuation acknowledgement required")
    if row["outcome"] == "pending":
        raise WaitError("wait.pending", "wait has no committed outcome")
    if "acknowledgement" in delivery:
        return delivery["acknowledgement"]
    held = await held_messages(conn, row)
    result = {"success": True, "wait_id": wait_id, "outcome": row["outcome"],
              "held_messages": held, "held_messages_heading": "Held while waiting"}
    ids = [message["id"] for message in held]
    await conn.execute(update(messages).where(
        messages.c.id.in_([*ids, f"wait:{wait_id}:outcome"]),
        messages.c.delivered_at.is_(None),
    ).values(delivered_at=now, read_at=now, via="wait_ack"))
    delivery = {**delivery, "acknowledged_at": now, "acknowledgement": result,
                "held_cursor_seq": max([delivery.get("held_cursor_seq", 0),
                                        *[message["created_seq"] for message in held]])}
    await conn.execute(update(wait_records).where(
        wait_records.c.id == wait_id, wait_records.c.version == row["version"],
    ).values(cleared_at=now, blocking=False, version=row["version"] + 1,
             decision_inputs={**row["decision_inputs"], "delivery": delivery}))
    await conn.execute(update(sessions).where(sessions.c.id == scope.session_id).values(
        last_activity=now,
    ))
    if scope.owner_kind == "task":
        await execute_task_status_mutation(conn, update(tasks).where(
            tasks.c.id == scope.owner_id, tasks.c.status == "WAITING",
        ).values(status="IN_PROGRESS", updated_at=now))
        await conn.execute(update(agents).where(agents.c.current_task_id == scope.owner_id).values(
            last_heartbeat=now,
        ))
        for key, value in (("stall_nudges", "0"), ("stall_last_action", str(now))):
            await conn.execute(insert(task_metadata).values(
                task_id=scope.owner_id, key=key, value=json.dumps(value),
            ).on_conflict_do_update(index_elements=[task_metadata.c.task_id, task_metadata.c.key],
                                    set_={"value": json.dumps(value)}))
    return result
