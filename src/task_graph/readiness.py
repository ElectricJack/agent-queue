"""Read-only claim-frontier labels, shared by task, graph and digest readers.

READY is a lifecycle status, not proof that a pool can claim a task. These
labels use the claim query itself and cached delivery evidence; reads never
fetch remote refs or change admission.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select

from src.database.tables import task_dependencies, tasks

DELIVERY_CODES = frozenset(
    {
        "frontier_sibling_prerequisite_not_delivered",
        "frontier_prerequisite_not_on_default_branch",
        "prerequisite_not_on_default_branch",
        "frontier_epic_refresh_pending",
    }
)


def readiness_from_reasons(reasons: list[dict]) -> dict:
    """Keep delivery waiting distinct from routing failures and unknown evidence."""
    delivery = [r for r in reasons if r["code"] in DELIVERY_CODES]
    unknown = any(r["code"] == "delivery_evidence_unavailable" for r in reasons)
    state = (
        "waiting_on_delivery"
        if delivery
        else "unknown"
        if unknown
        else "withheld"
        if reasons
        else "claimable"
    )
    label = {
        "waiting_on_delivery": "Waiting on delivery",
        "unknown": "Checking delivery evidence",
        "withheld": "Withheld",
        "claimable": "Ready",
    }[state]
    chosen = delivery or reasons
    concrete = [r for r in chosen if r.get("action")]
    details = list(dict.fromkeys(r.get("action") or r["detail"] for r in concrete or chosen))
    batches = sorted({batch for r in chosen for batch in r.get("batch_ids", [])})
    if batches:
        details.append("Finish epic-refresh batches: " + ", ".join(batches))
    return {
        "state": state,
        "claimable": not reasons,
        "label": label,
        "reason": "; ".join(details),
        "reasons": reasons,
    }


async def load_readiness(db, rows: Iterable[dict]) -> dict[str, dict]:
    """Evaluate READY rows together, sharing one delivery snapshot per project."""
    rows = [row for row in rows if row.get("status") == "READY"]
    if not rows:
        return {}
    reasons = await db.claim_frontier_exclusions_many(
        [row["id"] for row in rows],
        cached_only=True,
        router_ready_projects=getattr(db, "_ready_router_projects", frozenset()),
        project_ids={row["project_id"] for row in rows if row.get("project_id")},
    )
    return {tid: {"claim_readiness": readiness_from_reasons(why)} for tid, why in reasons.items()}


async def attach_readiness(db, rows: list[dict]) -> None:
    readiness = await load_readiness(db, rows)
    for row in rows:
        row.update(readiness.get(row["id"], {}))


async def readiness_for_ids(db, ids) -> dict[str, dict]:
    if not ids:
        return {}
    async with db._engine.connect() as conn:
        rows = await conn.execute(
            select(tasks.c.id, tasks.c.status, tasks.c.project_id).where(
                tasks.c.id.in_(ids), tasks.c.status == "READY"
            )
        )
        rows = rows.mappings().all()
    return await load_readiness(db, rows)


async def delivery_waits(db, project_ids=None) -> list[dict]:
    """Current delivery holds in the destination's scope, never activity facts."""
    query = (
        select(tasks.c.id, tasks.c.title, tasks.c.project_id, tasks.c.status)
        .where(
            tasks.c.status == "READY",
        )
        .order_by(tasks.c.id)
    )
    if project_ids is not None:
        query = query.where(tasks.c.project_id.in_(project_ids))
    async with db._engine.connect() as conn:
        rows = (await conn.execute(query)).mappings().all()
    readiness = await load_readiness(db, rows)
    return [
        {
            "task_id": row["id"],
            "title": row["title"],
            "project_id": row["project_id"],
            **readiness[row["id"]]["claim_readiness"],
        }
        for row in rows
        if readiness.get(row["id"], {}).get("claim_readiness", {}).get("state")
        == "waiting_on_delivery"
    ]


async def delivery_details(db, rows, modes) -> dict[str, list[dict]]:
    """Name the exact sources and targets of failed delivery predicates.

    Predicate failure remains authoritative. Source selection only explains
    that failure; missing preserved origins name the epic rather than inventing
    an undelivered sibling.
    """
    from src.database.queries.hierarchy_queries import (
        integration_rework_cutoff,
    )
    from src.database.tables import (
        task_delivery_receipts,
        task_integration_checkpoints,
    )
    from src.integration.delivery_observer import delivery_targets
    from sqlalchemy import exists, func, literal

    if not rows:
        return {}
    ids = [row["id"] for row in rows]
    source = tasks.alias("readiness_source")
    parent = tasks.alias("readiness_parent")
    checkpoint = task_integration_checkpoints
    receipt = task_delivery_receipts
    delivered = exists(
        select(literal(1))
        .where(
            receipt.c.source_task_id == source.c.id,
            receipt.c.target_task_id == tasks.c.parent_task_id,
            receipt.c.repository_id == checkpoint.c.repository_id,
            receipt.c.target_branch == parent.c.branch_name,
            receipt.c.disposition == "code",
            receipt.c.reviewed_head_sha == checkpoint.c.checkpoint_sha,
            receipt.c.created_at
            >= func.coalesce(integration_rework_cutoff(source.c.id), source.c.created_at),
        )
        .correlate(tasks, source, parent, checkpoint)
    )
    async with db._engine.connect() as conn:
        targets = (
            (
                await conn.execute(
                    select(
                        tasks.c.id,
                        tasks.c.parent_task_id,
                        parent.c.branch_name,
                    )
                    .select_from(
                        tasks.outerjoin(parent, parent.c.id == tasks.c.parent_task_id)
                    )
                    .where(tasks.c.id.in_(ids))
                )
            )
            .mappings()
            .all()
        )
        sources = (
            (
                await conn.execute(
                    select(
                        tasks.c.id.label("dependent_id"),
                        tasks.c.project_id,
                        tasks.c.parent_task_id,
                        source.c.id,
                        source.c.parent_task_id.label("source_parent"),
                        delivered.label("delivered"),
                    )
                    .select_from(
                        tasks.join(task_dependencies, task_dependencies.c.task_id == tasks.c.id)
                        .join(source, source.c.id == task_dependencies.c.depends_on_task_id)
                        .outerjoin(parent, parent.c.id == tasks.c.parent_task_id)
                        .outerjoin(checkpoint, checkpoint.c.task_id == source.c.id)
                    )
                    .where(
                        tasks.c.id.in_(ids),
                        source.c.status == "COMPLETED",
                        task_dependencies.c.dep_type == "blocks",
                    )
                    .order_by(tasks.c.id, source.c.id)
                )
            )
            .mappings()
            .all()
        )
        default_targets = await delivery_targets(
            conn,
            {
                row["id"]
                for row in sources
                if row["source_parent"] is None or row["source_parent"] != row["parent_task_id"]
            },
        )
    result = {row["id"]: [] for row in rows}
    target_by_id = {row["id"]: row for row in targets}
    flags = {row["id"]: row for row in rows}
    for source in sources:
        tid = source["dependent_id"]
        target = target_by_id[tid]
        mode = modes.get(source["project_id"])
        sibling = source["source_parent"] == source["parent_task_id"] and source["parent_task_id"]
        if sibling:
            delivered = (
                source["id"] in mode.delivered_prerequisite_ids
                if mode and mode.delivered_prerequisite_ids is not None
                else source["delivered"]
            )
            if mode and mode.stacked and source["id"] in mode.stackable_prerequisite_ids:
                delivered = True
            if delivered or flags[tid]["sibling_prerequisite_not_delivered"]:
                continue
            code = "frontier_sibling_prerequisite_not_delivered"
            detail = f"Waiting to land: {source['id']} on {target['branch_name']} (epic {target['parent_task_id']})"
        else:
            if flags[tid]["prerequisite_not_on_default_branch"]:
                continue
            if mode and source["id"] in mode.default_prerequisite_ids:
                continue
            code = "prerequisite_not_on_default_branch"
            default_target = default_targets.get(source["id"])
            branch = (
                default_target.target_ref.removeprefix("refs/heads/")
                if default_target
                else "the default branch"
            )
            detail = f"Waiting to land: {source['id']} on {branch}"
        result[tid].append({"code": code, "ref": source["id"], "detail": detail})
    for tid, row in flags.items():
        target = target_by_id[tid]
        if not row["epic_refresh_pending"]:
            cross_sources = sorted(
                {
                    s["id"]
                    for s in sources
                    if s["dependent_id"] == tid
                    and (s["source_parent"] != s["parent_task_id"] or s["source_parent"] is None)
                }
            )
            result[tid].append(
                {
                    "code": "frontier_epic_refresh_pending",
                    "ref": target["parent_task_id"],
                    "detail": f"Epic refresh required: {target['parent_task_id']} "
                    f"({target['branch_name']}) must contain "
                    + ", ".join(cross_sources)
                    + " and finish open epic-refresh batches",
                }
            )
        if not row["sibling_prerequisite_not_delivered"] and not any(
            r["code"] == "frontier_sibling_prerequisite_not_delivered" for r in result[tid]
        ):
            result[tid].append(
                {
                    "code": "frontier_sibling_prerequisite_not_delivered",
                    "ref": target["parent_task_id"],
                    "detail": f"Epic origin needs refresh: preserve {target['parent_task_id']} ({target['branch_name']}) before claiming",
                }
            )
    return result
