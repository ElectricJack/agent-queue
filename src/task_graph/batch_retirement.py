"""Prove prior obsolete retirement without retiring or releasing any task."""

import json

from sqlalchemy import or_, select

from src.database.queries.claim_queries import CLAIMED_BY_SESSION_KEY
from src.database.queries.hierarchy_queries import LIVE_SESSION_STATES
from src.database.tables import (
    agents, archived_tasks, ingest_batch_receipts, integration_batch_members,
    integration_batches, integration_branch_owners, integration_subjects,
    sessions, task_comments, task_dependencies, task_metadata, tasks, workspaces,
)
from src.models import DepType
from src.review_keys import fidelity_review_dedup_key
from src.sessions.obsolete import ObsoleteClose, ObsoleteCloseRefused


async def prove_retired_on(db, conn, batch, members):
    """Lock and prove every historical member and reviewer on the caller's boundary.

    Archive retirement preserves its server-authored marker in comments. Live
    obsolete closes retain metadata and must have completed owner cleanup. A
    terminal status alone proves neither. No task/hold/evidence is changed here.
    """
    member_ids = {member["task_id"] for member in members}
    ids = set(member_ids)
    if not ids:
        raise ObsoleteCloseRefused("membership", "Batch has no retained membership")
    if batch["reviewer_task_id"]:
        ids.add(batch["reviewer_task_id"])
    ids.update((await conn.execute(select(ingest_batch_receipts.c.reviewer_task_id).where(
        ingest_batch_receipts.c.batch_id == batch["batch_id"],
    ))).scalars())
    # Include earlier assignments even when a reviewer was archived before it
    # attested. The stable dispatch key survives in the archive.
    for table in (tasks, archived_tasks):
        ids.update((await conn.execute(select(table.c.id).where(
            table.c.project_id == batch["project_id"],
            table.c.dedup_key == fidelity_review_dedup_key(batch["batch_id"]),
        ))).scalars())
    rows = {row["id"]: dict(row) for row in (await conn.execute(select(tasks).where(
        tasks.c.id.in_(sorted(ids)),
    ).order_by(tasks.c.id).with_for_update())).mappings()}
    archived = {row["id"]: dict(row) for row in (await conn.execute(select(archived_tasks).where(
        archived_tasks.c.id.in_(sorted(ids - rows.keys())),
    ).order_by(archived_tasks.c.id).with_for_update())).mappings()}
    held = set((await conn.execute(select(tasks.c.id).where(
        tasks.c.held_batch_id == batch["batch_id"],
    ).with_for_update())).scalars())
    if held != member_ids & rows.keys():
        raise ObsoleteCloseRefused("membership", "Live membership and held tasks differ")
    lifecycle = ObsoleteClose(db, release_owner=None)
    evidence = {}
    for task_id in sorted(ids):
        row = rows.get(task_id) or archived.get(task_id)
        if row is None or row["project_id"] != batch["project_id"]:
            raise ObsoleteCloseRefused("retirement_required", f"Missing retained task {task_id}")
        if task_id in rows:
            marker = await lifecycle._marker(conn, task_id)
            if (row["status"] != "COMPLETED" or not marker
                    or (marker.get("cleanup") or {}).get("state") != "done"):
                raise ObsoleteCloseRefused(
                    "retirement_required", f"{task_id} needs completed obsolete retirement/cleanup",
                )
            await lifecycle._refuse_unsafe(conn, row, retirement=marker)
        else:
            marker = None
            bodies = (await conn.execute(select(task_comments.c.body).where(
                task_comments.c.task_id == task_id,
                task_comments.c.project_id == batch["project_id"],
                task_comments.c.author_kind == "supervisor",
                task_comments.c.body.startswith("Retirement evidence: "),
            ).order_by(task_comments.c.created_at.desc()))).scalars()
            for body in bodies:
                try:
                    candidate = json.loads(body.removeprefix("Retirement evidence: "))
                except (TypeError, ValueError):
                    continue
                if isinstance(candidate, dict):
                    marker = candidate
                    break
        if (not marker or marker.get("disposition") != "obsolete"
                or not marker.get("reason") or not marker.get("closed_by")
                or marker.get("closed_at") is None):
            raise ObsoleteCloseRefused(
                "retirement_required", f"{task_id} lacks supported obsolete retirement evidence",
            )
        evidence[task_id] = marker

    # Recheck actual bindings even when stale terminal rows/cleanup claim all
    # work is finished. Never release owners or remove stale claims here.
    probes = (
        select(sessions.c.id).where(sessions.c.task_id.in_(ids),
                                   sessions.c.state.in_(LIVE_SESSION_STATES)),
        select(task_metadata.c.task_id).where(task_metadata.c.task_id.in_(ids),
                                             task_metadata.c.key == CLAIMED_BY_SESSION_KEY),
        select(agents.c.id).where(agents.c.current_task_id.in_(ids)),
        select(workspaces.c.id).where(workspaces.c.locked_by_task_id.in_(ids)),
        select(integration_subjects.c.id).where(
            integration_subjects.c.task_id.in_(ids), integration_subjects.c.phase != "done",
        ),
        select(integration_batches.c.id).where(
            integration_batches.c.id.in_(select(integration_batch_members.c.batch_id).where(
                integration_batch_members.c.task_id.in_(ids),
            )),
            integration_batches.c.intent != "aborted", integration_batches.c.lifecycle != "promoted",
        ),
        select(integration_branch_owners.c.id).where(
            integration_branch_owners.c.owner_id.in_(ids),
            or_(integration_branch_owners.c.handoff_state != "released",
                integration_branch_owners.c.holder.is_not(None)),
        ),
    )
    for probe in probes:
        if await conn.scalar(probe.limit(1).with_for_update()):
            raise ObsoleteCloseRefused("needs_quiescence", "A retired task still has a claim/owner")
    pending = await conn.scalar(select(task_dependencies.c.task_id).where(
        task_dependencies.c.depends_on_task_id.in_(ids),
        task_dependencies.c.task_id.not_in(ids),
        task_dependencies.c.dep_type != DepType.PARENT_CHILD.value,
    ).limit(1))
    if pending:
        raise ObsoleteCloseRefused(
            "pending_requirements", f"{pending} still depends on retired batch work; rewire first",
        )
    return evidence
