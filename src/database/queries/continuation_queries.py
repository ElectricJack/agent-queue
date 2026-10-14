"""Checkpoint and read projections on caller-owned transactions.

The hibernation owner creates the intent and its held-mail cursor before asking
for a checkpoint. These helpers never stop, park, launch or acknowledge a session.
Mutation lock order is tasks -> wait_records -> continuations.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import and_, or_, select, update

from src.database.queries.wait_queries import verify_owner
from src.database.tables import continuations, messages, sessions, tasks, wait_records
from src.handoffs import HANDOFF_BYTES
from src.waits.conditions import Condition, WaitError


def validate_summary(summary: str) -> str:
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary.encode("utf-8")) > HANDOFF_BYTES
    ):
        raise WaitError("wait.checkpoint_invalid", "summary must contain 1–8192 UTF-8 bytes")
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


async def _locked_intent(conn, *, wait_id, continuation_id=None, generation=None):
    predicates = [continuations.c.wait_id == wait_id]
    if continuation_id is not None:
        predicates.append(continuations.c.id == continuation_id)
    else:
        predicates.append(
            continuations.c.resume_state.not_in(("acknowledged", "failed", "cancelled"))
        )
    intent = await conn.execute(select(continuations).where(*predicates).with_for_update())
    intent = intent.mappings().first()
    if (
        intent is None
        or (generation is not None and intent["suspension_generation"] != generation)
        or intent["resume_state"] not in {"sleeping", "queued"}
        or intent["stopped_at"] is not None
    ):
        raise WaitError("continuation.stale_generation", "no current checkpoint intent")
    return intent


async def checkpoint_continuation(
    conn,
    *,
    scope,
    wait_id,
    summary,
    facts,
    continuation_id=None,
    generation=None,
):
    """Repeat the authenticated holder fence, then persist exact prose and its hash."""
    digest = validate_summary(summary)
    if scope is None or scope.owner_kind != "task" or scope.principal != "agent":
        raise WaitError("wait.out_of_scope", "checkpoint requires a task holder")
    scope = await verify_owner(conn, scope)
    wait = (
        (
            await conn.execute(
                select(wait_records).where(wait_records.c.id == wait_id).with_for_update()
            )
        )
        .mappings()
        .first()
    )
    if not wait or (
        wait["project_id"],
        wait["owner_kind"],
        wait["owner_id"],
        wait["session_id"],
        wait["claim_epoch"],
    ) != (scope.project_id, "task", scope.owner_id, scope.session_id, scope.claim_epoch):
        raise WaitError("continuation.stale_generation", "wait holder changed")
    if wait["sleep_decision"] != "hibernate" or wait["outcome"] == "cancelled":
        raise WaitError("wait.checkpoint_invalid", "wait is not awaiting a hibernation checkpoint")
    intent = await _locked_intent(
        conn,
        wait_id=wait_id,
        continuation_id=continuation_id,
        generation=generation,
    )
    if intent["task_id"] != scope.owner_id:
        raise WaitError("continuation.stale_generation", "continuation owner changed")
    if intent["summary_degraded"]:
        raise WaitError("wait.checkpoint_invalid", "summary deadline already committed")
    if intent["summary"] is not None:
        if intent["summary_sha256"] != digest:
            raise WaitError("wait.checkpoint_invalid", "checkpoint already captured")
    else:
        await conn.execute(
            update(continuations)
            .where(continuations.c.id == intent["id"])
            .values(
                summary=summary,
                summary_sha256=digest,
                summary_degraded=False,
                facts=facts,
                version=continuations.c.version + 1,
            )
        )
    return {
        "success": True,
        "continuation_id": intent["id"],
        "generation": intent["suspension_generation"],
        "next_step": "end_turn",
    }


async def degrade_checkpoint(
    conn,
    *,
    task_id,
    wait_id,
    continuation_id,
    generation,
    now,
    transition_budget_seconds,
    facts,
):
    """Service-side deadline step; missing prose becomes labelled server facts only.

    The deadline owner calls this inside its fenced CommandHandler transaction.
    Budget/clock/facts are server inputs. Replays preserve the first snapshot.
    """
    task = (
        (await conn.execute(select(tasks).where(tasks.c.id == task_id).with_for_update()))
        .mappings()
        .first()
    )
    wait = (
        (
            await conn.execute(
                select(wait_records).where(wait_records.c.id == wait_id).with_for_update()
            )
        )
        .mappings()
        .first()
    )
    if (
        not task
        or not wait
        or wait["owner_kind"] != "task"
        or wait["owner_id"] != task_id
        or wait["project_id"] != task["project_id"]
        or wait["claim_epoch"] != task["claim_epoch"]
        or task["status"] not in {"IN_PROGRESS", "WAITING"}
        or wait["sleep_decision"] != "hibernate"
        or wait["outcome"] == "cancelled"
    ):
        raise WaitError("continuation.stale_generation", "hibernation owner changed")
    intent = await _locked_intent(
        conn,
        wait_id=wait_id,
        continuation_id=continuation_id,
        generation=generation,
    )
    if intent["task_id"] != task_id:
        raise WaitError("continuation.stale_generation", "continuation owner changed")
    # Intent creation also anchors an eviction's transition, after registration.
    anchor = max(wait["created_at"], intent["created_at"] or wait["created_at"])
    if intent["summary"] is None and not intent["summary_degraded"]:
        if now < anchor + transition_budget_seconds:
            raise WaitError("wait.checkpoint_invalid", "summary transition budget has not expired")
        await conn.execute(
            update(continuations)
            .where(continuations.c.id == intent["id"])
            .values(
                summary_degraded=True,
                facts=facts,
                version=continuations.c.version + 1,
            )
        )
    return {
        "success": True,
        "continuation_id": intent["id"],
        "generation": intent["suspension_generation"],
        "next_step": "end_turn",
    }


def wait_outcome_envelope(wait, continuation=None, *, held_messages=(), decision=None):
    """The same outcome envelope for in-place delivery and a fresh continuation."""
    if wait["outcome"] == "pending":
        raise WaitError("wait.checkpoint_invalid", "pending wait has no outcome envelope")
    return {
        "kind": "wait_outcome",
        "wait_id": wait["id"],
        "continuation_generation": continuation["suspension_generation"] if continuation else None,
        "outcome": wait["outcome"],
        "condition": Condition.from_record(wait["condition"]).as_dict(),
        "result_ref": wait["result_ref"],
        "decision": decision,
        "summary_ref": f"continuation:{continuation['id']}" if continuation else None,
        "held_messages": list(held_messages),
        "next_action": "ack_then_continue",
    }


async def continuation_prime_context(conn, *, task_id, project_id, session_id):
    """Scope the read to the newly launched session, never an old task attempt.

    Held mail can address the old session, the task or its old profile. Prime is
    read-only here; wait_ack owns delivery stamps and advancing the cursor.
    """
    intent = (
        (
            await conn.execute(
                select(continuations).where(
                    continuations.c.task_id == task_id,
                    continuations.c.new_session_id == session_id,
                    continuations.c.resume_state.in_(("launching", "delivered", "acknowledged")),
                    select(sessions.c.id)
                    .where(
                        sessions.c.id == session_id,
                        sessions.c.task_id == task_id,
                        sessions.c.project_id == project_id,
                    )
                    .exists(),
                )
            )
        )
        .mappings()
        .first()
    )
    if intent is None:
        return None
    wait = (
        (
            await conn.execute(
                select(wait_records).where(
                    wait_records.c.id == intent["wait_id"],
                    wait_records.c.project_id == project_id,
                    wait_records.c.owner_kind == "task",
                    wait_records.c.owner_id == task_id,
                )
            )
        )
        .mappings()
        .first()
    )
    if wait is None or wait["outcome"] == "pending":
        return None
    old = (
        (
            await conn.execute(
                select(sessions).where(
                    sessions.c.id == wait["session_id"],
                    sessions.c.project_id == project_id,
                )
            )
        )
        .mappings()
        .first()
    )
    recipients = [and_(messages.c.to_kind == "task", messages.c.to_id == task_id)]
    if old:
        recipients.extend(
            [
                and_(
                    messages.c.to_kind == "session", messages.c.to_id.in_((old["id"], old["name"]))
                ),
                and_(messages.c.to_kind == "profile", messages.c.to_id == old["profile_id"]),
            ]
        )
    held = (
        (
            await conn.execute(
                select(messages)
                .where(
                    messages.c.project_id == project_id,
                    messages.c.created_seq > intent["held_cursor_seq"],
                    messages.c.delivered_at.is_(None),
                    messages.c.archived_at.is_(None),
                    or_(
                        messages.c.body_kind.is_(None),
                        messages.c.body_kind.not_in(
                            ("wait_outcome", "wait_suspend", "wait_decision")
                        ),
                    ),
                    or_(*recipients),
                )
                .order_by(messages.c.created_seq)
            )
        )
        .mappings()
        .all()
    )
    decision = (wait["decision_inputs"] or {}).get("decision")
    return {
        "continuation": dict(intent),
        "held_messages": [dict(m) for m in held],
        "envelope": wait_outcome_envelope(
            wait, intent, held_messages=[m["id"] for m in held], decision=decision
        ),
    }
