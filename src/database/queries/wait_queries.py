"""Verified v2 registration on a caller-owned transaction; no runtime activation.

Lock order: tasks -> wait_records -> continuations -> admission_reservations ->
wait_deadlines -> wait_edges. The graph advisory lock precedes these locks,
serialising opposite registrations. No adapter opens a transaction, emits an
event, sleeps a session or dispatches a wake here.

Registration locks only its owner before subscribe/re-observe; locking producer
tasks would prevent source commits in that window. Priority projection writes
follow the observation under the graph fence, in sorted task order. Source
writers must not acquire that fence after locking a source task.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import replace

from sqlalchemy import and_, func, insert, or_, select, update

from src.database.tables import (
    agents,
    admission_reservations,
    continuations,
    events,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_repair_stages,
    messages,
    projects,
    sessions,
    tasks,
    wait_deadlines,
    wait_edges,
    wait_escalations,
    wait_priority_projection,
    wait_records,
)
from src.waits.adapters.base import foreign, invalid, row
from src.waits.conditions import NodeKey, Scope, SleepDecision, WaitError, canonical
from src.waits.explain import build_wait_explanation, graph_paths, owner_key
from src.waits.graph import (
    GRAPH_LOCK,
    add_edges,
    clear_edges,
    lock_component_tasks,
    lock_graph,
    recompute_from,
    would_create_cycle,
)

# Separate from source allocation's shared/exclusive 0x415157414954 fence.
# Source writers must remain able to commit during subscribe/re-observe.
REGISTRATION_LOCK = GRAPH_LOCK


async def verify_owner(conn, scope: Scope, *, lock_owner=True) -> Scope:
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
    statement = select(tasks).where(tasks.c.id == scope.owner_id)
    if lock_owner:
        statement = statement.with_for_update()
    task = (await conn.execute(statement)).mappings().first()
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
    on_loop_detected=None,
    ceiling_anchor_at=None,
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
    scope = await verify_owner(conn, scope, lock_owner=False)
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
    changed_keys = {f"{node_kind}:{scope.owner_id}"} | {
        key for edge in edges for key in (edge.waiter.key, edge.blocker.key)
    }
    await lock_component_tasks(conn, changed_keys, lock_rows=False)
    scope = await verify_owner(conn, scope)
    if await would_create_cycle(conn, edges):
        raise WaitError("wait.cycle", "condition would create a semantic dependency cycle")
    if loop_check:
        decision = await loop_check(conn, scope, evidence, now)
    else:
        from src.waits.loops import detect
        decision = await detect(conn, scope, evidence, now, policy=policy)
    if decision is None:
        decision = decide_sleep(cond, evidence)
    else:
        measured = decide_sleep(cond, evidence)
        decision = SleepDecision(decision.mode, decision.reason,
                                 {**measured.inputs, **decision.inputs, 'loop_detected': True})
        if getattr(policy, 'hibernation_enabled', True) is False:
            decision = SleepDecision('in_place', 'flag', decision.inputs)
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
    if decision.reason == "wait.loop_detected":
        escalate_at = now
    if ceiling_anchor_at is None:
        from src.waits.decision import ceiling_anchor

        ceiling_anchor_at = await ceiling_anchor(conn, scope, now=now, policy=policy)
    ceiling_at = (
        ceiling_anchor_at + policy.in_place_ceiling_seconds
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
            ceiling_anchor_at=ceiling_anchor_at if ceiling_at is not None else None,
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
    await add_edges(conn, wait_id, edges, source_revision=evidence.observation.revision, now=now)
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
        await clear_edges(conn, wait_id, now=now)
    await recompute_from(conn, changed_keys, now=now)
    await conn.execute(insert(events).values(
        event_type="wait.registered", project_id=scope.project_id,
        task_id=scope.owner_id if scope.owner_kind == "task" else None,
        timestamp=now, payload=json.dumps({"wait_id": wait_id, "project_id": scope.project_id,
                                         "condition": cond.as_dict(),
                                         "decision": receipt["decision"]}),
    ))
    if receipt["success"] and decision.reason == "wait.loop_detected":
        payload = {"wait_id": wait_id, "project_id": scope.project_id,
                   "task_id": scope.owner_id, "registrations": [
            *decision.inputs.get("loop_registrations", []),
            {"wait_id": wait_id, "registered_at": now},
        ]}
        for kind in ("wait.loop_detected", "continuation.loop_detected"):
            await conn.execute(insert(events).values(
                event_type=kind, project_id=scope.project_id, task_id=scope.owner_id,
                timestamp=now, payload=json.dumps(payload),
            ))
        callback = on_loop_detected or getattr(registry, "loop_handler", None)
        if callback:
            wait = await get_wait(conn, wait_id, project_id=scope.project_id)
            await callback(conn, wait, now=now)
    return receipt


async def resolve_wait(
    conn,
    wait_id,
    *,
    project_id,
    outcome,
    now,
    result_ref=None,
    expected_version=None,
):
    """Immutable outcome CAS and graph retraction, on the caller's transaction.

    Source observation, command authorisation and wake/continuation handling
    belong to the calling service. A replay never overwrites an outcome.
    Take the graph fence BEFORE any task/record locks, as registration does.
    """
    if outcome not in {"satisfied", "cancelled", "lifted_by_supervisor", "source_unavailable"}:
        invalid("a terminal wait outcome is required")
    await lock_graph(conn)
    saved = await get_wait(conn, wait_id, project_id=project_id)
    if saved is None:
        raise WaitError("wait.not_found", "wait is outside the query scope or missing")
    roots = set(
        (
            await conn.execute(
                select(wait_edges.c.waiter_key).where(
                    wait_edges.c.wait_id == wait_id,
                    wait_edges.c.active.is_(True),
                )
            )
        ).scalars()
    )
    roots.add(owner_key(saved))
    await lock_component_tasks(conn, roots)
    saved = (
        (
            await conn.execute(
                select(wait_records)
                .where(
                    wait_records.c.id == wait_id,
                    wait_records.c.project_id == project_id,
                )
                .with_for_update()
            )
        )
        .mappings()
        .one()
    )
    if saved["outcome"] != "pending":
        return {
            "success": True,
            "changed": False,
            "wait_id": wait_id,
            "outcome": saved["outcome"],
            "result_ref": saved["result_ref"],
        }
    if expected_version is not None and expected_version != saved["version"]:
        raise WaitError("wait.version_conflict", "wait version changed")
    await conn.execute(
        update(wait_records)
        .where(
            wait_records.c.id == wait_id,
            wait_records.c.version == saved["version"],
            wait_records.c.outcome == "pending",
        )
        .values(
            outcome=outcome,
            resolved_at=now,
            cleared_at=now,
            blocking=False,
            result_ref=result_ref,
            version=saved["version"] + 1,
            verification_state=(
                "unavailable" if outcome == "source_unavailable" else saved["verification_state"]
            ),
        )
    )
    await conn.execute(
        update(wait_deadlines)
        .where(
            wait_deadlines.c.wait_id == wait_id,
            wait_deadlines.c.cancelled_at.is_(None),
            wait_deadlines.c.fired_at.is_(None),
        )
        .values(cancelled_at=now)
    )
    changed_keys = roots | await clear_edges(conn, wait_id, now=now)
    await recompute_from(conn, changed_keys, now=now)
    return {
        "success": True,
        "changed": True,
        "wait_id": wait_id,
        "outcome": outcome,
        "result_ref": result_ref,
    }


async def cancel_wait(conn, wait_id, *, project_id, now, expected_version=None):
    return await resolve_wait(
        conn,
        wait_id,
        project_id=project_id,
        outcome="cancelled",
        now=now,
        expected_version=expected_version,
    )


async def replace_wait_edges(conn, wait_id, edges, *, project_id, source_revision, now):
    """Refresh adapter-verified batch/branch ownership without deleting history.

    The source service supplies freshly verified edges and acquires this fence
    before its own row locks. A terminal wait can never regain active edges.
    """
    await lock_graph(conn)
    saved = await get_wait(conn, wait_id, project_id=project_id)
    if saved is None:
        raise WaitError("wait.not_found", "wait is outside the query scope or missing")
    if saved["outcome"] != "pending":
        return False
    if any(e.waiter.project_id != project_id or e.blocker.project_id != project_id for e in edges):
        foreign()
    if any(e.waiter.key != owner_key(saved) for e in edges):
        invalid("replacement edges must belong to the wait owner")
    if await would_create_cycle(conn, edges, excluding_wait_id=wait_id):
        raise WaitError("wait.cycle", "condition would create a semantic dependency cycle")
    roots = {owner_key(saved)} | {key for e in edges for key in (e.waiter.key, e.blocker.key)}
    await lock_component_tasks(conn, roots)
    await conn.execute(
        select(wait_records.c.id).where(wait_records.c.id == wait_id).with_for_update()
    )
    roots |= await clear_edges(conn, wait_id, now=now)
    await add_edges(conn, wait_id, edges, source_revision=source_revision, now=now)
    await recompute_from(conn, roots, now=now)
    return True


def _page(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
        invalid("limit must be 1..1000 and offset must be nonnegative")


async def _rows(conn, statement):
    return [dict(r) for r in (await conn.execute(statement)).mappings()]


async def _explain_rows(conn, records, *, project_id, history, now):
    """Bounded related reads; callers can use REPEATABLE READ for a snapshot."""
    ids = [r["id"] for r in records]
    task_ids = [r["owner_id"] for r in records if r["owner_kind"] in {"task", "scheduler"}]
    task_rows = {
        r["id"]: r
        for r in await _rows(
            conn,
            select(tasks).where(
                tasks.c.id.in_(task_ids),
                tasks.c.project_id == project_id,
            ),
        )
    }
    projections = {
        r["task_id"]: r
        for r in await _rows(
            conn,
            select(wait_priority_projection).where(
                wait_priority_projection.c.task_id.in_(task_rows)
            ),
        )
    }
    edge_query = select(wait_edges).where(
        wait_edges.c.waiter_project_id == project_id,
        wait_edges.c.blocker_project_id == project_id,
    )
    if not history:
        edge_query = edge_query.where(wait_edges.c.active.is_(True))
    edges = await _rows(
        conn, edge_query.order_by(wait_edges.c.created_at, wait_edges.c.id).limit(10_001)
    )
    edges_truncated = len(edges) > 10_000
    edges = edges[:10_000]
    deadlines = await _rows(
        conn,
        select(wait_deadlines)
        .where(wait_deadlines.c.wait_id.in_(ids))
        .order_by(wait_deadlines.c.due_at, wait_deadlines.c.id),
    )
    escalations = await _rows(
        conn,
        select(wait_escalations)
        .where(wait_escalations.c.wait_id.in_(ids))
        .order_by(wait_escalations.c.raised_at, wait_escalations.c.id),
    )
    continuation_rows = await _rows(
        conn,
        select(continuations)
        .where(
            continuations.c.wait_id.in_(ids),
        )
        .order_by(continuations.c.suspension_generation),
    )
    reservation_rows = await _rows(
        conn,
        select(admission_reservations)
        .where(
            admission_reservations.c.project_id == project_id,
            or_(
                admission_reservations.c.task_id.in_(task_ids),
                admission_reservations.c.continuation_id.in_([r["id"] for r in continuation_rows]),
            ),
        )
        .order_by(admission_reservations.c.effective_priority, admission_reservations.c.fifo_seq),
    )
    result = []
    for record in records:
        continuation = next(
            (c for c in reversed(continuation_rows) if c["wait_id"] == record["id"]), None
        )
        item = build_wait_explanation(
            record,
            task=task_rows.get(record["owner_id"])
            if record["owner_kind"] in {"task", "scheduler"}
            else None,
            projection=projections.get(record["owner_id"]),
            edges=edges,
            deadlines=[d for d in deadlines if d["wait_id"] == record["id"]],
            escalations=[e for e in escalations if e["wait_id"] == record["id"]],
            reservations=[
                r
                for r in reservation_rows
                if (r["task_id"] in task_rows and r["task_id"] == record["owner_id"])
                or (continuation and r["continuation_id"] == continuation["id"])
            ],
            continuation=continuation,
            now=now,
            history=history,
        )
        item["chain_truncated"] |= edges_truncated
        result.append(item)
    return result


async def wait_explain(
    conn, *, project_id, wait_id=None, task_id=None, history=False, now, limit=100, offset=0
):
    """Read persisted verified facts separately from untrusted claimed prose.

    Exactly one selector is required. Foreign/missing ids are indistinguishable.
    No observation, mutation, LLM call, wake or policy evaluation occurs here.
    """
    _page(limit, offset)
    if bool(wait_id) == bool(task_id):
        invalid("supply one wait_id or task_id")
    query = select(wait_records).where(wait_records.c.project_id == project_id)
    if wait_id:
        query = query.where(wait_records.c.id == wait_id)
    else:
        if not await conn.scalar(
            select(tasks.c.id).where(
                tasks.c.id == task_id,
                tasks.c.project_id == project_id,
            )
        ):
            raise WaitError("wait.not_found", "wait owner is outside the query scope or missing")
        query = query.where(
            wait_records.c.owner_id == task_id, wait_records.c.owner_kind.in_(("task", "scheduler"))
        )
        if not history:
            query = query.where(wait_records.c.outcome == "pending")
    total = await conn.scalar(select(func.count()).select_from(query.subquery()))
    if wait_id and not total:
        raise WaitError("wait.not_found", "wait is outside the query scope or missing")
    records = await _rows(
        conn,
        query.order_by(wait_records.c.created_at, wait_records.c.id).limit(limit).offset(offset),
    )
    explained = await _explain_rows(
        conn,
        records,
        project_id=project_id,
        history=history,
        now=now,
    )
    owner_task_id = task_id or (
        records[0]["owner_id"]
        if records and records[0]["owner_kind"] in {"task", "scheduler"}
        else None
    )
    task = await row(conn, tasks, tasks.c.id == owner_task_id, tasks.c.project_id == project_id)
    projection = (
        await row(
            conn, wait_priority_projection, wait_priority_projection.c.task_id == owner_task_id
        )
        if task
        else None
    )
    return {
        "success": True,
        "waits": explained,
        "task": {
            "id": task["id"],
            "lifecycle": task["status"],
            "base_priority": task["priority"],
            "effective_priority": task["effective_priority"],
            "donor_task_id": projection["donor_task_id"] if projection else None,
            "donor_path": projection["donor_path"] if projection else [],
            "graph_version": projection["graph_version"] if projection else None,
        }
        if task
        else None,
        "total": total,
        "offset": offset,
        "truncated": offset + len(records) < total,
    }


async def wait_graph(
    conn, *, project_id=None, fleet=False, fleet_authorized=False, now, limit=100, offset=0,
    costs=False,
):
    """Project/fleet summaries share the explain projection and explicit bounds.

    CommandHandler must derive fleet_authorized from waits.fleet_summary, never
    the request. Schema/read-only queries remain inert before the v2 cutover.
    """
    _page(limit, offset)
    if fleet and not fleet_authorized:
        foreign()
    if not fleet and not project_id:
        invalid("project_id is required")
    project_query = select(projects.c.id).order_by(projects.c.id)
    if not fleet:
        project_query = project_query.where(projects.c.id == project_id)
    project_total = await conn.scalar(select(func.count()).select_from(project_query.subquery()))
    project_ids = (await conn.execute(project_query.limit(limit).offset(offset))).scalars().all()
    summaries = []
    for pid in project_ids:
        pending = select(wait_records).where(
            wait_records.c.project_id == pid, wait_records.c.outcome == "pending"
        )
        counts = await _rows(
            conn,
            select(
                wait_records.c.condition_kind.label("kind"),
                wait_records.c.sleep_decision.label("decision"),
                func.count().label("count"),
            )
            .where(wait_records.c.project_id == pid, wait_records.c.outcome == "pending")
            .group_by(wait_records.c.condition_kind, wait_records.c.sleep_decision)
            .order_by(wait_records.c.condition_kind, wait_records.c.sleep_decision),
        )
        records = await _rows(
            conn, pending.order_by(wait_records.c.created_at, wait_records.c.id).limit(limit)
        )
        edges = await _rows(
            conn,
            select(wait_edges)
            .where(
                wait_edges.c.waiter_project_id == pid,
                wait_edges.c.blocker_project_id == pid,
                wait_edges.c.active.is_(True),
            )
            .order_by(wait_edges.c.created_at, wait_edges.c.id)
            .limit(10_001),
        )
        escalated = await _rows(
            conn,
            select(wait_escalations)
            .join(
                wait_records,
                wait_records.c.id == wait_escalations.c.wait_id,
            )
            .where(wait_records.c.project_id == pid)
            .order_by(
                wait_escalations.c.raised_at,
                wait_escalations.c.id,
            )
            .limit(limit + 1),
        )
        branch_tasks = (
            select(integration_branch_owners.c.owner_id.label("task_id"))
            .where(
                integration_branch_owners.c.handoff_state != "released",
            )
            .union(
                select(integration_repair_stages.c.repair_task_id.label("task_id")).where(
                    integration_repair_stages.c.state.in_(("active", "awaiting_completion")),
                ),
                select(integration_batch_members.c.task_id)
                .join(
                    integration_batches,
                    integration_batches.c.id == integration_batch_members.c.batch_id,
                )
                .where(integration_batches.c.lifecycle.not_in(("delivered", "cancelled"))),
            )
        )
        blockers = await _rows(
            conn,
            select(tasks.c.id, tasks.c.status, tasks.c.priority, tasks.c.effective_priority)
            .where(
                tasks.c.project_id == pid,
                tasks.c.id.in_(select(branch_tasks.subquery().c.task_id)),
            )
            .order_by(tasks.c.effective_priority, tasks.c.created_at, tasks.c.id)
            .limit(limit + 1),
        )
        reservations = await _rows(
            conn,
            select(admission_reservations)
            .where(
                admission_reservations.c.project_id == pid,
                admission_reservations.c.task_id.in_([b["id"] for b in blockers[:limit]]),
            )
            .order_by(
                admission_reservations.c.effective_priority, admission_reservations.c.fifo_seq
            ),
        )
        for blocker in blockers[:limit]:
            blocker["reservations"] = [r for r in reservations if r["task_id"] == blocker["id"]]
        # Held mail is counted without exposing its body or evaluating commentary.
        mail_join = and_(
            messages.c.project_id == pid,
            messages.c.archived_at.is_(None),
            messages.c.delivered_at.is_(None),
            messages.c.created_seq > continuations.c.held_cursor_seq,
            or_(
                and_(messages.c.to_kind == "task", messages.c.to_id == continuations.c.task_id),
                and_(
                    messages.c.to_kind == "session", messages.c.to_id == wait_records.c.session_id
                ),
            ),
        )
        backlog = await _rows(
            conn,
            select(
                continuations.c.id,
                continuations.c.task_id,
                continuations.c.resume_state,
                func.count(messages.c.id).label("held_messages"),
            )
            .join(wait_records, wait_records.c.id == continuations.c.wait_id)
            .outerjoin(messages, mail_join)
            .where(
                wait_records.c.project_id == pid,
                continuations.c.resume_state.not_in(("acknowledged", "failed", "cancelled")),
            )
            .group_by(continuations.c.id)
            .order_by(continuations.c.id)
            .limit(limit + 1),
        )
        cost_projection = {}
        if costs:
            from src.database.queries.continuation_queries import measurement_medians

            medians = await measurement_medians(conn, pid, now=now)
            cost_projection = {'costs': {'durations': list(medians['durations'].values()),
                                         'resume': medians['cost']}}
        summaries.append(
            {
                "project_id": pid,
                **cost_projection,
                "counts": counts,
                "waits": await _explain_rows(conn, records, project_id=pid, history=False, now=now),
                "total_waits": sum(c["count"] for c in counts),
                "escalated_waits": escalated[:limit],
                "branch_blockers": blockers[:limit],
                "held_message_backlog": backlog[:limit],
                **graph_paths(edges[:10_000]),
                "truncated": {
                    "waits": sum(c["count"] for c in counts) > len(records),
                    "edges": len(edges) > 10_000,
                    "escalations": len(escalated) > limit,
                    "branch_blockers": len(blockers) > limit,
                    "backlog": len(backlog) > limit,
                },
            }
        )
    return {
        "success": True,
        "projects": summaries,
        "total_projects": project_total,
        "offset": offset,
        "truncated": offset + len(project_ids) < project_total,
    }
