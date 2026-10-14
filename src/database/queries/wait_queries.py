"""Verified v2 registration on a caller-owned transaction; no runtime activation.

Lock order: tasks -> wait_records -> continuations -> admission_reservations ->
wait_deadlines -> wait_edges. The graph advisory lock precedes these locks,
serialising opposite registrations. No adapter opens a transaction, emits an
event, sleeps a session or dispatches a wake here.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import replace

from sqlalchemy import func, insert, select, update

from src.database.tables import (
    agents,
    messages,
    projects,
    sessions,
    tasks,
    wait_deadlines,
    wait_edges,
    wait_records,
)
from src.waits.adapters.base import foreign, invalid, row
from src.waits.conditions import NodeKey, Scope, SleepDecision, WaitError, canonical
from src.waits.graph import would_create_cycle

# Separate from source allocation's shared/exclusive 0x415157414954 fence.
# Source writers must remain able to commit during subscribe/re-observe.
REGISTRATION_LOCK = 0x41515747524150


async def verify_owner(conn, scope: Scope) -> Scope:
    """Transaction-local counterpart of the claim command's ownership guard.

    Command callers must construct Scope from their authenticated principal,
    never from the request body. No command/session modules are imported here.
    The holder task is locked before wait rows; session reads don't take locks.
    """
    if scope.principal == "service":
        if scope.owner_kind not in {"scheduler", "playbook", "integration"}:
            foreign()
        if not await conn.scalar(select(projects.c.id).where(projects.c.id == scope.project_id)):
            foreign()
        return replace(scope, claim_epoch=scope.claim_epoch or 0)
    session = await row(conn, sessions, sessions.c.id == scope.session_id)
    if (
        not session
        or not scope.instance_token
        or session["instance_token"] != scope.instance_token
        or session["state"] not in {"starting", "running"}
        or session["desired_state"] != "running"
    ):
        foreign()
    if scope.owner_kind == "session":
        if (
            not scope.elevated
            or session["lifecycle"] != "named"
            or session["profile_id"] != "supervisor"
            or session["id"] != scope.owner_id
            or session["project_id"] not in {scope.project_id, None}
        ):
            foreign()
        return replace(scope, claim_epoch=0)
    if (
        scope.owner_kind != "task"
        or session["task_id"] != scope.owner_id
        or session["project_id"] != scope.project_id
    ):
        foreign()
    task = (
        (
            await conn.execute(
                select(tasks)
                .where(
                    tasks.c.id == scope.owner_id,
                )
                .with_for_update()
            )
        )
        .mappings()
        .first()
    )
    if not task or task["project_id"] != scope.project_id:
        foreign()
    agent = await row(conn, agents, agents.c.id == session["agent_id"])
    if (
        # The command transitions to WAITING after this query commits. A lost
        # receipt must remain replayable by that same still-live claim.
        task["status"] not in {"IN_PROGRESS", "WAITING"}
        or task["assigned_agent_id"] != session["agent_id"]
        or (scope.claim_epoch is not None and scope.claim_epoch != task["claim_epoch"])
        or (
            session["last_claim_epoch"] is not None
            and session["last_claim_epoch"] != task["claim_epoch"]
        )
        or (
            session["lifecycle"] == "pool"
            and (scope.claim_epoch is None or session["claim_phase"] != "active")
        )
        or session["lifecycle"] not in {"pool", "task"}
        or not agent
        or agent["current_task_id"] != scope.owner_id
        or agent["state"] != "BUSY"
    ):
        raise WaitError("stale_claim", "wait registration requires the current live claim")
    return replace(scope, claim_epoch=task["claim_epoch"])


async def get_wait(conn, wait_id: str, *, project_id: str):
    return await row(
        conn, wait_records, wait_records.c.id == wait_id, wait_records.c.project_id == project_id
    )


async def register_wait(
    conn,
    *,
    scope: Scope,
    condition,
    idempotency_key: str,
    registry,
    policy,
    decide_sleep,
    now: float,
    read_source_watermark=None,
    source_watermark: int | None = None,
    claimed: str | None = None,
    escalate_after_seconds: float | None = None,
    loop_check=None,
) -> dict:
    """Register and return a stable receipt, or a structured refusal.

    ``policy``, ``decide_sleep`` and ``loop_check`` are server-injected seams
    for their owning leaves. decide_sleep(cond, evidence) is synchronous;
    loop_check(conn, scope, evidence, now) returns an overriding SleepDecision
    or None. Runtime flag checks belong to the caller; v1 never calls here.

    Supply exactly one of read_source_watermark or source_watermark. The async
    callback calls db.wait_source_watermark() on a separate short READ COMMITTED
    transaction and releases its fence before returning. It runs before any
    registration lock. If the caller already holds graph/owner/source locks or
    has emitted source events, pass a global bound prefetched BEFORE those locks
    instead, and acquire REGISTRATION_LOCK before the owner/source locks in that
    transaction. Never acquire the exclusive source fence while holding them.
    This connection must also use READ COMMITTED so post-fence source reads see
    committed changes even when the caller's transaction already started.

    A pre-existing satisfied source raises WaitError before any insert. The
    subscribe/observe race instead RETURNS an already_satisfied receipt so the
    caller commits its audit row; raising there would roll back that evidence.
    """
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        invalid("idempotency_key is required")
    if claimed is not None and (not isinstance(claimed, str) or len(claimed) > 1024):
        invalid("claimed must be at most 1024 characters")
    if not math.isfinite(now):
        invalid("registration time must be finite")
    if escalate_after_seconds is not None and (
        type(escalate_after_seconds) not in (int, float)
        or not math.isfinite(escalate_after_seconds)
        or escalate_after_seconds <= 0
    ):
        invalid("escalate_after_seconds must be positive")
    if (read_source_watermark is None) == (source_watermark is None):
        raise ValueError("supply one fenced watermark reader or prefetched global bound")
    if await conn.get_isolation_level() != "READ COMMITTED":
        raise ValueError("wait registration requires READ COMMITTED source observations")
    if read_source_watermark is not None:
        source_watermark = await read_source_watermark()
    if type(source_watermark) is not int or source_watermark < 0:
        raise ValueError("source watermark must be a nonnegative committed sequence bound")
    await conn.execute(select(func.pg_advisory_xact_lock(REGISTRATION_LOCK)))
    scope = await verify_owner(conn, scope)
    cond = registry.normalise(condition)
    request_hash = hashlib.sha256(
        canonical(
            {
                "condition": cond.as_dict(),
                "claimed": claimed,
                "escalate_after_seconds": escalate_after_seconds,
            }
        ).encode()
    ).hexdigest()
    old = await row(
        conn,
        wait_records,
        wait_records.c.project_id == scope.project_id,
        wait_records.c.owner_kind == scope.owner_kind,
        wait_records.c.owner_id == scope.owner_id,
        wait_records.c.claim_epoch == scope.claim_epoch,
        wait_records.c.idempotency_key == idempotency_key,
    )
    if old:
        if old["request_hash"] != request_hash:
            raise WaitError("wait.idempotency_conflict", "key names a different request")
        # Replay precedes source verification: the producer may have finished or
        # disappeared since the receipt was committed, and timers may be past due.
        return old["decision_inputs"]["registration_receipt"]
    evidence = await registry.verify(conn, cond, scope)
    cond = evidence.condition
    adapter = registry.adapter(cond.kind)
    if evidence.observation.state == "satisfied":
        raise WaitError(
            "wait.already_satisfied",
            "condition is already satisfied",
            result_ref=evidence.observation.result_ref,
        )
    origin = (
        "scheduler"
        if cond.kind == "resource"
        else "human"
        if cond.kind == "human"
        else scope.owner_kind
        if scope.owner_kind in {"integration", "playbook"}
        else "agent"
    )
    if cond.kind != "resource":
        active = await conn.scalar(
            select(wait_records.c.id)
            .where(
                wait_records.c.owner_kind == scope.owner_kind,
                wait_records.c.owner_id == scope.owner_id,
                wait_records.c.cleared_at.is_(None),
                wait_records.c.origin != "scheduler",
            )
            .limit(1)
        )
        if active:
            raise WaitError(
                "wait.active_exists", "owner already has an execution wait", wait_id=active
            )
    else:
        active = await conn.scalar(
            select(wait_records.c.id)
            .where(
                wait_records.c.owner_kind == scope.owner_kind,
                wait_records.c.owner_id == scope.owner_id,
                wait_records.c.source_key == evidence.source.identity,
                wait_records.c.source_generation == evidence.source.generation,
                wait_records.c.origin == "scheduler",
            )
            .limit(1)
        )
        if active:
            raise WaitError(
                "wait.active_exists", "resource identity is already recorded", wait_id=active
            )
    node_kind = {"scheduler": "task", "integration": "batch"}.get(
        scope.owner_kind, scope.owner_kind
    )
    edges = adapter.edges(cond, NodeKey(f"{node_kind}:{scope.owner_id}", scope.project_id))
    if await would_create_cycle(conn, edges):
        raise WaitError("wait.cycle", "condition would create a semantic dependency cycle")
    decision = await loop_check(conn, scope, evidence, now) if loop_check else None
    if decision is None:
        decision = decide_sleep(cond, evidence)
    if not isinstance(decision, SleepDecision) or decision.mode not in {"in_place", "hibernate"}:
        raise ValueError("sleep decision must be supplied by the server")
    if cond.kind == "human":
        decision = SleepDecision("hibernate", "human", decision.inputs)
    deadlines = adapter.deadline_policy(cond, policy)
    seconds = deadlines.escalate_after_seconds
    if escalate_after_seconds is not None:
        if seconds is None or escalate_after_seconds > seconds:
            invalid("an agent may only shorten a non-human escalation deadline")
        seconds = escalate_after_seconds
    escalate_at = (deadlines.due_at or now) + seconds if seconds is not None else None
    ceiling_at = (
        now + policy.in_place_ceiling_seconds
        if decision.mode == "in_place" and cond.kind not in {"human", "resource"}
        else None
    )
    wait_id = str(uuid.uuid4())
    delivery = {
        "instance_token": scope.instance_token,
        "held_cursor_seq": int(await conn.scalar(select(func.max(messages.c.created_seq))) or 0),
    }
    receipt = {
        "success": True,
        "wait_id": wait_id,
        "condition": cond.as_dict(),
        "evidence": {
            "ref": evidence.evidence_ref,
            "verified_at": now,
            "revision": evidence.observation.revision,
            "facts": dict(evidence.observation.facts),
        },
        "decision": {
            "mode": decision.mode,
            "reason": decision.reason,
            "inputs": dict(decision.inputs),
        },
        "escalate_at": escalate_at,
        "ceiling_at": ceiling_at,
        "next_step": "end_turn" if decision.mode == "in_place" else "checkpoint_then_end_turn",
    }
    await conn.execute(
        insert(wait_records).values(
            id=wait_id,
            project_id=scope.project_id,
            owner_kind=scope.owner_kind,
            owner_id=scope.owner_id,
            origin=origin,
            session_id=scope.session_id,
            claim_epoch=scope.claim_epoch,
            condition_kind=cond.kind,
            condition=cond.as_record(),
            source_key=evidence.source.identity,
            source_generation=evidence.source.generation,
            source_watermark_seq=source_watermark,
            verification_state="verified",
            evidence_ref=evidence.evidence_ref,
            verified_at=now,
            created_at=now,
            escalate_at=escalate_at,
            due_at=deadlines.due_at,
            ceiling_anchor_at=now if ceiling_at is not None else None,
            sleep_decision=decision.mode,
            decision_reason=decision.reason,
            decision_inputs={**decision.inputs, "delivery": delivery,
                             "registration_receipt": receipt},
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            claimed=claimed,
            blocking=cond.kind != "resource",
            version=1,
        )
    )
    for deadline_type, at in (
        ("due", deadlines.due_at),
        ("escalation", escalate_at),
        ("ceiling", ceiling_at),
    ):
        if at is not None:
            deadline_id = str(uuid.uuid4())
            await conn.execute(
                insert(wait_deadlines).values(
                    id=deadline_id,
                    wait_id=wait_id,
                    deadline_type=deadline_type,
                    due_at=at,
                    generation=1,
                )
            )
            await conn.execute(select(func.pg_notify("aq_wait_deadline", deadline_id)))
    for edge in edges:
        await conn.execute(
            insert(wait_edges).values(
                id=str(uuid.uuid4()),
                wait_id=wait_id,
                waiter_key=edge.waiter.key,
                waiter_project_id=edge.waiter.project_id,
                blocker_key=edge.blocker.key,
                blocker_project_id=edge.blocker.project_id,
                relation=edge.relation,
                source_revision=evidence.observation.revision,
                created_at=now,
            )
        )
    observed = await registry.observe(conn, cond)
    if observed.state != "unsatisfied":
        outcome = "satisfied" if observed.state == "satisfied" else "source_unavailable"
        receipt.update(
            success=False,
            code=("wait.already_satisfied" if outcome == "satisfied" else "wait.source_unresolved"),
            result_ref=observed.result_ref,
            next_step="continue",
        )
        # Take locks in the declared order even for this immediate audit transition.
        await conn.execute(
            update(wait_records)
            .where(
                wait_records.c.id == wait_id,
                wait_records.c.version == 1,
            )
            .values(
                outcome=outcome,
                resolved_at=now,
                result_ref=observed.result_ref,
                blocking=False,
                cleared_at=now,
                version=2,
                verification_state="unavailable" if outcome == "source_unavailable" else "verified",
                decision_inputs={**decision.inputs, "delivery": delivery,
                                 "registration_receipt": receipt},
            )
        )
        await conn.execute(
            update(wait_deadlines)
            .where(wait_deadlines.c.wait_id == wait_id)
            .values(cancelled_at=now)
        )
        await conn.execute(
            update(wait_edges)
            .where(wait_edges.c.wait_id == wait_id)
            .values(active=False, cleared_at=now)
        )
    return receipt
