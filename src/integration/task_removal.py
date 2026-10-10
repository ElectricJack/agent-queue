"""Operator removal and retirement of operations with no surviving engine.

Stopping processes is the orchestrator's job. These short transactions hold the
same engine/batch exclusions as publishers and never delete Git refs or audit rows.
"""

from __future__ import annotations

import json
import time

from sqlalchemy import or_, select, text, update

from src.database import tables as t
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.queries.integration_state_queries import session_attached_clause
from src.database.queries.task_references import find_integration_task_references
from src.integration.delegate_release import LIVE_OPERATION_STATES, _owned_by, release_delegates_on
from src.integration.engine import lock_key
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
    """Maintenance backstop, also run by Remove under its transaction locks."""
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


async def prepare_removal(db, task_id):
    """Hold the entire subtree atomically before any process cleanup can yield."""
    async with db.immediate() as conn:
        project_id = await conn.scalar(select(t.tasks.c.project_id).where(t.tasks.c.id == task_id))
        if project_id is None:
            raise HierarchyError("not_found", task_id)
        await db.lock_hierarchy_project(conn, project_id)
        ids = await db.subtree_ids(task_id, conn=conn)
        # Claims take sessions before tasks. Pause shares that order.
        await conn.execute(select(t.sessions.c.id).where(
            t.sessions.c.task_id.in_(ids),
        ).order_by(t.sessions.c.id).with_for_update())
        snapshots, flipped = {}, set()
        for tid in sorted(ids):
            snapshot, result = await db._pause_task_on(tid, conn=conn, for_removal=True)
            snapshots[tid] = snapshot
            if result:
                flipped |= result.flipped
    await db.log_blocked_flips(flipped)
    return snapshots


async def remove_subtree(db, task_id, *, expected_ids, reason, actor, legacy_engine_gone=False):
    """Abort abandoned batches and remove identities in one rollback-safe commit."""
    now = time.time()
    async with db.immediate() as conn:
        root = (await conn.execute(select(t.tasks).where(t.tasks.c.id == task_id))).mappings().first()
        if root is None:
            raise HierarchyError("not_found", task_id)
        # Root and parent runtimes keep their remote actions under these locks.
        repository_ids = list((await conn.execute(select(t.repos.c.id).where(
            t.repos.c.project_id == root["project_id"],
        ).order_by(t.repos.c.id))).scalars())
        for repository_id in repository_ids:
            await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                               {"key": lock_key(repository_id)})
        operation = t.integration_repair_operations
        delegated = select(t.tasks.c.id).where(
            t.tasks.c.id.in_(expected_ids), _owned_by(operation, t.tasks.c.id),
        ).correlate(operation).exists()
        related_parents = list((await conn.execute(select(operation.c.parent_task_id).where(
            operation.c.state.in_(LIVE_OPERATION_STATES),
            or_(operation.c.parent_task_id.in_(expected_ids), delegated),
            operation.c.parent_task_id.is_not(None),
        ))).scalars())
        for tid in sorted(set(expected_ids) | set(related_parents)):
            await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                               {"key": parent_lock_key(tid)})
        await db.lock_hierarchy_project(conn, root["project_id"])
        ids = await db.subtree_ids(task_id, conn=conn)
        if set(ids) != set(expected_ids):
            raise HierarchyError("removal_changed", "The subtree changed; retry Remove to stop its new tasks.")
        await conn.execute(select(t.sessions.c.id).where(
            t.sessions.c.task_id.in_(ids),
        ).order_by(t.sessions.c.id).with_for_update())
        rows = (await conn.execute(select(t.tasks).where(
            t.tasks.c.id.in_(ids),
        ).order_by(t.tasks.c.id).with_for_update())).mappings().all()
        if any(row["assigned_agent_id"] or row["status"] not in (
            "PAUSED", *db._TERMINAL_TASK_STATUSES,
        ) for row in rows):
            raise HierarchyError("removal_changed", "A task changed during cleanup; retry Remove.")
        if await conn.scalar(select(t.sessions.c.id).where(
            t.sessions.c.task_id.in_(ids), session_attached_clause(),
        ).limit(1)):
            raise HierarchyError("live_descendants", "Session cleanup is pending; retry Remove.")
        scope = set(ids)
        ancestor = root["parent_task_id"]
        while ancestor and ancestor not in scope:
            scope.add(ancestor)
            ancestor = await conn.scalar(select(t.tasks.c.parent_task_id).where(t.tasks.c.id == ancestor))
        # publication() holds this row across managed Git transport; an abort waits
        # for that bounded transport and prevents another one from starting.
        batches = (await conn.execute(select(t.integration_batches).where(
            t.integration_batches.c.id.in_(select(t.integration_batch_members.c.batch_id).where(
                t.integration_batch_members.c.task_id.in_(scope),
            )), t.integration_batches.c.lifecycle != "promoted",
        ).order_by(t.integration_batches.c.id).with_for_update())).mappings().all()
        aborted = []
        for batch in batches:
            if batch["lifecycle"] != "aborted":
                await conn.execute(update(t.integration_batches).where(
                    t.integration_batches.c.id == batch["id"],
                ).values(intent="aborted", lifecycle="aborted", cleanup_state="pending",
                         human_abort_reason=reason, updated_at=now))
                aborted.append(batch["id"])
        operations = (await conn.execute(select(operation).where(
            operation.c.state.in_(LIVE_OPERATION_STATES),
            or_(operation.c.parent_task_id.in_(ids), delegated,
                operation.c.batch_id.in_([b["id"] for b in batches])),
        ).order_by(operation.c.id).with_for_update())).mappings().all()
        cancelled = []
        for row in operations:
            if await _cancel_orphan_on(db, conn, row, now=now, reason=reason,
                                       legacy_engine_gone=legacy_engine_gone):
                cancelled.append(row["id"])
        _, transitions = await release_delegates_on(
            db, conn, now=now, released_by=actor, operation_ids=cancelled,
        ) if cancelled else ([], [])
        # Any durable integration identity chooses archive, including non-FK history.
        history = bool(await find_integration_task_references(conn, ids))
        history_columns = [
            column for table in t.metadata.tables.values()
            if table.name.startswith("integration_") or table.name in (
                "task_delivery_receipts", "task_branch_origins", "task_integration_checkpoints",
            )
            for column in table.c if column.name.endswith("task_id")
        ]
        for column in history_columns:
            history |= bool(await conn.scalar(select(column).where(column.in_(ids)).limit(1)))
        if history:
            # Remove is the operator's decision that the work is not wanted.
            outcome = await db._archive_task_on(
                task_id, conn=conn, abandon_undelivered=True, abandon_reason=reason,
                abandoned_by=actor, archive_reason=reason, disposition="obsolete",
            )
            flipped, ready, settled = outcome
            flipped |= settled.flipped
            ready += list(settled.ready)
        else:
            # Archive's session/resource checks are also owed by a hard removal.
            live = await db.live_descendant_sessions(task_id, conn=conn)
            attached = await conn.scalar(select(t.sessions.c.id).where(
                t.sessions.c.task_id.in_(ids), session_attached_clause(),
            ).limit(1))
            if live or attached:
                raise HierarchyError("live_descendants", "Session cleanup is pending; retry Remove.")
            settled = await db._delete_task_body(task_id, cascade=True, conn=conn, branch_policy="keep")
            flipped, ready = settled.flipped, list(settled.ready)
        result = {"removed": task_id, "task_ids": ids,
                  "disposition": "archived" if history else "deleted", "branches": "keep",
                  "aborted_batches": aborted, "cancelled_operations": cancelled}
        await db.log_event("task.removed", project_id=root["project_id"], task_id=task_id,
                           payload=json.dumps({**result, "reason": reason, "actor": actor}), conn=conn)
    await db.log_blocked_flips(flipped)
    await db._notify_settled(settled.settled)
    await db._notify_ready(ready)
    for transition in transitions:
        await db.log_blocked_flips(transition.flipped)
        await db._notify_settled(transition.settled)
        await db._notify_ready(transition.ready)
    return result
