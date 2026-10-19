"""Exact fresh-session delivery and acknowledgement, never an inferred resume."""

import json

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from src.database.tables import (
    agents, continuations, messages, sessions, task_metadata, tasks, wait_records,
)
from src.waits.conditions import WaitError
from src.waits.graph import lock_graph
from src.waits.sources import execute_task_status_mutation


async def locked_resume(conn, continuation_id, *, session_id, instance_token, claim_epoch):
    await lock_graph(conn)
    initial = (await conn.execute(select(continuations).where(
        continuations.c.id == continuation_id,
    ))).mappings().first()
    if not initial:
        raise WaitError("continuation.stale_generation", "continuation disappeared")
    task = (await conn.execute(select(tasks).where(
        tasks.c.id == initial["task_id"],
    ).with_for_update())).mappings().one()
    wait = (await conn.execute(select(wait_records).where(
        wait_records.c.id == initial["wait_id"],
    ).with_for_update())).mappings().one()
    intent = (await conn.execute(select(continuations).where(
        continuations.c.id == continuation_id,
    ).with_for_update())).mappings().one()
    session = (await conn.execute(select(sessions).where(
        sessions.c.id == session_id,
    ).with_for_update())).mappings().first()
    binding = intent["facts"].get("resume_delivery", {})
    if (not session or intent["new_session_id"] != session_id
            or binding.get("instance_token") != instance_token
            or session["instance_token"] != instance_token
            or session["state"] not in {"starting", "running"}
            or session["desired_state"] != "running"
            or session["task_id"] != task["id"]
            or session["project_id"] != wait["project_id"]
            or task["assigned_agent_id"] != session["agent_id"]
            or task["status"] not in {"RESUME_PENDING", "IN_PROGRESS"}
            or task["claim_epoch"] != claim_epoch or binding.get("claim_epoch") != claim_epoch
            or session["last_claim_epoch"] != claim_epoch or wait["outcome"] == "pending"):
        raise WaitError("continuation.stale_generation", "fresh holder binding changed")
    return dict(task), dict(wait), dict(intent), dict(session)


async def mark_delivered(conn, *, continuation_id, session_id, instance_token, claim_epoch, now):
    _, wait, intent, _ = await locked_resume(
        conn, continuation_id, session_id=session_id,
        instance_token=instance_token, claim_epoch=claim_epoch,
    )
    if intent["resume_state"] in {"delivered", "acknowledged"}:
        return
    if intent["resume_state"] != "launching" or not intent["envelope_id"]:
        raise WaitError("continuation.stale_generation", "outcome has no launch binding")
    await conn.execute(update(messages).where(
        messages.c.id == intent["envelope_id"], messages.c.to_id == session_id,
    ).values(delivered_at=now, via="prime"))
    await conn.execute(update(continuations).where(continuations.c.id == intent["id"]).values(
        resume_state="delivered", resumed_at=now, version=intent["version"] + 1,
    ))
    await conn.execute(update(wait_records).where(wait_records.c.id == wait["id"]).values(
        wake_delivered_at=now,
    ))


async def acknowledge(conn, args, *, scope, now):
    if not args.get("continuation_id") or not args.get("generation"):
        raise WaitError("continuation.stale_generation", "continuation and generation are required")
    task, wait, intent, session = await locked_resume(
        conn, args["continuation_id"], session_id=scope.session_id,
        instance_token=scope.instance_token, claim_epoch=scope.claim_epoch,
    )
    if (intent["wait_id"] != args["wait_id"] or intent["task_id"] != scope.owner_id
            or intent["suspension_generation"] != args["generation"]):
        raise WaitError("continuation.stale_generation", "acknowledgement targets another generation")
    delivery = intent["facts"]["resume_delivery"]
    if intent["resume_state"] == "acknowledged":
        return delivery["acknowledgement"]
    envelope = (await conn.execute(select(messages).where(
        messages.c.id == intent["envelope_id"], messages.c.to_kind == "session",
        messages.c.to_id == session["id"], messages.c.delivered_at.is_not(None),
    ))).mappings().first()
    if intent["resume_state"] != "delivered" or envelope is None:
        raise WaitError("wait.ack_required", "outcome must be delivered to this process first")
    body = json.loads(envelope["body"])
    if (body.get("summary_ref") != f"continuation:{intent['id']}"
            or body.get("continuation_generation") != intent["suspension_generation"]):
        raise WaitError("continuation.stale_generation", "delivered envelope binding changed")
    ids = delivery["held_message_ids"]
    held = [dict(row) for row in (await conn.execute(select(messages).where(
        messages.c.id.in_(ids),
    ).order_by(messages.c.created_seq))).mappings()]
    result = {"success": True, "acknowledged": True, "wait_id": wait["id"],
              "continuation_id": intent["id"], "generation": intent["suspension_generation"],
              "held_messages": held, "next_step": "continue"}
    await conn.execute(update(messages).where(messages.c.id.in_([*ids, intent["envelope_id"]])).values(
        delivered_at=now, read_at=now, via="wait_ack",
    ))
    cursor = max([intent["held_cursor_seq"], *[row["created_seq"] for row in held]])
    await conn.execute(update(continuations).where(continuations.c.id == intent["id"]).values(
        resume_state="acknowledged", acknowledged_at=now, held_cursor_seq=cursor,
        facts={**intent["facts"], "resume_delivery": {**delivery, "acknowledgement": result}},
        version=intent["version"] + 1,
    ))
    await conn.execute(update(wait_records).where(wait_records.c.id == wait["id"]).values(
        cleared_at=now, blocking=False, version=wait["version"] + 1,
        decision_inputs={**wait["decision_inputs"], "delivery": {
            **wait["decision_inputs"].get("delivery", {}), "acknowledged_at": now,
            "held_cursor_seq": cursor}},
    ))
    await execute_task_status_mutation(conn, update(tasks).where(tasks.c.id == task["id"]).values(
        status="IN_PROGRESS", updated_at=now,
    ))
    await conn.execute(update(sessions).where(sessions.c.id == session["id"]).values(last_activity=now))
    await conn.execute(update(agents).where(agents.c.id == session["agent_id"]).values(last_heartbeat=now))
    for key, value in (("stall_nudges", "0"), ("stall_last_action", str(now))):
        await conn.execute(insert(task_metadata).values(
            task_id=task["id"], key=key, value=json.dumps(value),
        ).on_conflict_do_update(index_elements=[task_metadata.c.task_id, task_metadata.c.key],
                                set_={"value": json.dumps(value)}))
    return result
