"""Source-CI repair lineage: which delegates a completion still authorizes.

The train outlives the source-delivery engine, so the lineage queries it needs
live here rather than in ``source_delivery``; that module re-exports them.
"""

from __future__ import annotations

import json

from sqlalchemy import or_, select

from src.database.tables import integration_source_ci

RETIREMENT_KEY = "source_ci_retirement"
SUPERSEDED = "source_ci_repair_superseded"
REOPEN_DISPOSITION = "superseded_by_reopen"


def repair_ids(record) -> set[str]:
    """Current and historical delegates retain the same exact source binding."""
    return {task_id for task_id in (
        record["repair_task_id"],
        *(attempt["task_id"] for attempt in record["repair_history"] or []),
    ) if task_id}


async def repair_bindings_on(conn, ids, *, repository_id):
    if not ids:
        return []
    return (await conn.execute(select(integration_source_ci).where(
        integration_source_ci.c.repository_id == repository_id,
        or_(integration_source_ci.c.repair_task_id.in_(ids), *(
            integration_source_ci.c.repair_history.contains([{"task_id": task_id}])
            for task_id in sorted(ids)
        )),
    ))).mappings().all()


async def superseded_source_repairs_on(db, conn, ids, *, repository_id) -> dict[str, dict]:
    """Fresh completion-head and retirement checks for the entire repair chain.

    Unlike delivery, supersession revokes a repair's purpose. Already delivered
    source work cannot authorize a delegate after that source was reopened.
    """
    from src.database.tables import task_integration_checkpoints, task_metadata
    from src.integration.delivery_truth import load_delivery_requests

    candidates = set(ids)
    ancestors, records, seen = set(ids), [], set()
    while ancestors - seen:
        frontier = ancestors - seen
        seen |= frontier
        found = await repair_bindings_on(conn, frontier, repository_id=repository_id)
        records.extend(found)
        ancestors.update(row["task_id"] for row in found)
    if not records:
        return {}
    sources = {row["task_id"] for row in records}
    requests = await load_delivery_requests(
        db, sources, repository_id=repository_id, target_ref="", conn=conn,
    )
    checkpoints = dict((await conn.execute(select(
        task_integration_checkpoints.c.task_id, task_integration_checkpoints.c.checkpoint_sha,
    ).where(task_integration_checkpoints.c.task_id.in_(sources),
            task_integration_checkpoints.c.repository_id == repository_id))).all())
    retired = dict((await conn.execute(select(
        task_metadata.c.task_id, task_metadata.c.value,
    ).where(task_metadata.c.task_id.in_(ancestors),
            task_metadata.c.key == RETIREMENT_KEY))).all())
    blocked = {
        task_id: {"code": SUPERSEDED, "ref": task_id, "task_id": task_id,
                  "detail": json.loads(value)["reason"]}
        for task_id, value in retired.items()
        if json.loads(value).get("disposition") == REOPEN_DISPOSITION
    }
    while True:
        before = set(blocked)
        for row in records:
            request = requests.get(row["task_id"])
            head = request.reported_source if request else None
            if (request is not None and not head and not request.requires_parent_completion
                    and request.completion_id == request.legacy_generation):
                # Legacy leaf observations bind the finished checkpoint. A
                # missing or invalid verified parent must never borrow it.
                head = checkpoints.get(row["task_id"])
            if (request is not None and not request.requires_parent_completion
                    and checkpoints.get(row["task_id"]) not in {None, head}):
                head = None  # A moved checkpoint cannot borrow an older close row.
            if (row["task_id"] not in blocked and request is not None
                    and request.task_status == "COMPLETED"
                    and request.repository_id == repository_id and head == row["source_head"]):
                continue
            for repair_id in repair_ids(row) & ancestors:
                blocked.setdefault(repair_id, {
                    "code": SUPERSEDED, "ref": repair_id, "task_id": repair_id,
                    "source_task_id": row["task_id"], "source_head": row["source_head"],
                    "detail": f"Source CI repair {repair_id} is superseded: "
                              f"{row['task_id']} no longer has completion head {row['source_head']}",
                })
        if set(blocked) == before:
            return {task_id: blocked[task_id] for task_id in sorted(candidates & blocked.keys())}
