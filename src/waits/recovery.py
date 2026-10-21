"""Repair derived wait ownership while retaining every continuation and outcome."""

from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import and_, exists, insert, or_, select, update

from src.database.queries.continuation_queries import retire_task_waits
from src.database.tables import (
    continuations, sessions, task_context, tasks, wait_deadlines, wait_records, workspaces,
)
from src.waits.graph import clear_edges, lock_graph, recompute_from
from src.waits.sources import execute_task_status_mutation

FINAL = ("acknowledged", "failed", "cancelled")
LIVE = ("starting", "running", "draining", "hibernating", "stopping")


async def orphan_report(conn, *, project_id=None):
    active = select(continuations.c.id).where(
        continuations.c.wait_id == wait_records.c.id,
        continuations.c.resume_state.not_in(FINAL),
    ).exists()
    alive = select(sessions.c.id).where(
        sessions.c.id == wait_records.c.session_id, sessions.c.state.in_(LIVE),
        sessions.c.desired_state == "running",
    ).exists()
    missing = select(wait_records.c.id.label("wait_id"), wait_records.c.owner_id.label("task_id"),
                     wait_records.c.project_id).where(
        wait_records.c.origin.in_(("agent", "human")), wait_records.c.cleared_at.is_(None),
        ~active, ~alive,
    )
    broken = select(continuations.c.id.label("continuation_id"), continuations.c.wait_id,
                    continuations.c.task_id, wait_records.c.project_id).select_from(
        continuations.join(wait_records, wait_records.c.id == continuations.c.wait_id)
        .outerjoin(tasks, tasks.c.id == continuations.c.task_id),
    ).where(continuations.c.resume_state.not_in(FINAL), or_(
        tasks.c.id.is_(None),
        # WAITING/PAUSED and an outcome delivered to a fresh IN_PROGRESS session
        # are legal intermediate states, not stranded continuations.
        tasks.c.status.not_in(("WAITING", "HIBERNATED", "RESUME_PENDING", "PAUSED", "IN_PROGRESS")),
        and_(tasks.c.status == "IN_PROGRESS", continuations.c.stopped_at.is_not(None),
             continuations.c.resume_state.not_in(("launching", "delivered"))),
        and_(tasks.c.status.in_(("WAITING", "IN_PROGRESS")), ~alive,
             continuations.c.resume_state.not_in(("launching", "delivered"))),
    ))
    if project_id:
        missing = missing.where(wait_records.c.project_id == project_id)
        broken = broken.where(wait_records.c.project_id == project_id)
    rows = [{**dict(row), "reason": "session_gone"}
            for row in (await conn.execute(missing)).mappings()]
    rows += [{**dict(row), "reason": "continuation_owner_state"}
             for row in (await conn.execute(broken)).mappings()]
    return rows


async def repair_orphans(db, *, project_id=None, now=None):
    now = time.time() if now is None else now
    repaired, held = [], []
    async with db._engine.begin() as conn:
        await lock_graph(conn)
        for candidate in await orphan_report(conn, project_id=project_id):
            task = (await conn.execute(select(tasks).where(
                tasks.c.id == candidate["task_id"],
            ).with_for_update())).mappings().first()
            wait = (await conn.execute(select(wait_records).where(
                wait_records.c.id == candidate["wait_id"],
            ).with_for_update())).mappings().one()
            intent = (await conn.execute(select(continuations).where(
                continuations.c.wait_id == wait["id"], continuations.c.resume_state.not_in(FINAL),
            ).with_for_update())).mappings().first()
            # Never detach a checkout, release a live claim or undo a manual pause.
            live = await conn.scalar(select(exists(select(sessions.c.id).where(
                sessions.c.state.in_(LIVE), or_(sessions.c.id == wait["session_id"],
                    sessions.c.task_id == candidate["task_id"]),
            ))))
            locked_workspace = await conn.scalar(select(exists(select(workspaces.c.id).where(
                workspaces.c.locked_by_task_id == candidate["task_id"],
            ))))
            if live or locked_workspace or (task and task["assigned_agent_id"]):
                held.append({**candidate, "hold": "ownership_not_released"})
                continue
            if task and task["status"] not in ("COMPLETED", "FAILED", "PAUSED"):
                if not intent or not intent["summary"] or not intent["saved_head"]:
                    held.append({**candidate, "hold": "summary_or_saved_head_missing"})
                    continue
                await conn.execute(insert(task_context).values(
                    id=str(uuid.uuid4()), task_id=task["id"], type="handoff",
                    label="wait orphan recovery", created_at=now, content=json.dumps({
                        "version": 1,
                        "agent": {"detail": intent["summary"], "next_step":
                            "Inspect the preserved continuation and saved HEAD before resuming."},
                        "facts": {**intent["facts"], "head": intent["saved_head"],
                                  "branch": intent["branch"], "claim_epoch": task["claim_epoch"],
                                  "continuation_id": intent["id"]},
                        "reason": "source_unavailable",
                    }),
                ))
                await execute_task_status_mutation(conn, update(tasks).where(
                    tasks.c.id == task["id"],
                ).values(status="READY", claim_epoch=tasks.c.claim_epoch + 1, updated_at=now))
            if wait["outcome"] == "pending":
                await conn.execute(update(wait_records).where(wait_records.c.id == wait["id"]).values(
                    outcome="source_unavailable", resolved_at=now,
                    result_ref=f"doctor:waits.orphans:{wait['id']}",
                ))
            if task:
                await retire_task_waits(conn, task["id"], reason="wait_orphan_recovery", now=now)
            else:
                await conn.execute(update(wait_records).where(wait_records.c.id == wait["id"]).values(
                    blocking=False, cleared_at=now, version=wait_records.c.version + 1,
                ))
                await conn.execute(update(wait_deadlines).where(
                    wait_deadlines.c.wait_id == wait["id"], wait_deadlines.c.fired_at.is_(None),
                ).values(cancelled_at=now))
                changed = await clear_edges(conn, wait["id"], now=now)
                await recompute_from(conn, changed, now=now)
                if intent:
                    await conn.execute(update(continuations).where(
                        continuations.c.id == intent["id"],
                    ).values(resume_state="cancelled", hold_reason="wait_orphan_recovery",
                             version=continuations.c.version + 1))
            repaired.append(candidate)
    return {"repaired": repaired, "held": held}
