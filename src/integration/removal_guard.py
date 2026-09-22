"""Read-only safety checks for removing a task with integration history.

Archive preserves an identity in ``archived_tasks``; hard delete does not.
The checks in this module deliberately run before the hierarchy-mode gate so
an old integration record cannot become unsafe merely because a project was
switched to development or disabled.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import exists, func, literal, or_, select

from src.database.queries.blocked_state import _development_delivery_pending
from src.database.queries.task_references import (
    describe_integration_references,
    find_integration_task_references,
)
from src.database.tables import (
    gates,
    integration_batch_members,
    integration_batches,
    integration_candidate_resolutions,
    integration_repair_operations,
    integration_repair_stages,
    projects,
    repos,
    task_branch_origins,
    task_delivery_receipts,
    task_gates,
    task_integration_checkpoints,
    task_labels,
    tasks,
)
from src.models import TaskStatus

HIERARCHY_MODES = frozenset({"hierarchy", "train"})
_ACTIVE_BATCH_STATES = (
    "sealing",
    "sealed",
    "building",
    "testing",
    "repairing",
    "human_blocked",
    "promoting",
    "cleanup_pending",
)


def _error(code: str, detail: str, context: dict | None = None):
    # Import here: hierarchy owns the public error type and itself calls us.
    from src.database.queries.hierarchy_queries import HierarchyError

    return HierarchyError(code, detail, context)


async def _sealed_member_on(conn, root_id: str, ids: Sequence[str]) -> dict | None:
    """Return the first active batch member in the subtree or its ancestors."""
    scope = set(ids)
    current = root_id
    # Structural depth is capped at three; the generous bound protects legacy
    # malformed data without turning a removal check into an unbounded walk.
    for _ in range(16):
        row = (
            await conn.execute(select(tasks.c.parent_task_id).where(tasks.c.id == current))
        ).scalar_one_or_none()
        if not row:
            break
        scope.add(str(row))
        current = str(row)
    row = (
        await conn.execute(
            select(
                integration_batch_members.c.task_id,
                integration_batches.c.id.label("batch_id"),
                integration_batches.c.lifecycle,
            )
            .select_from(
                integration_batch_members.join(
                    integration_batches,
                    integration_batches.c.id == integration_batch_members.c.batch_id,
                )
            )
            .where(integration_batch_members.c.task_id.in_(sorted(scope)))
            .where(integration_batches.c.lifecycle.in_(_ACTIVE_BATCH_STATES))
            .order_by(integration_batch_members.c.task_id, integration_batches.c.id)
            .limit(1)
        )
    ).mappings().one_or_none()
    return dict(row) if row else None


async def _is_delegate_on(conn, task_id: str) -> bool:
    operation = integration_repair_operations
    stage = integration_repair_stages
    resolution = integration_candidate_resolutions
    row = await conn.execute(
        select(operation.c.id)
        .where(
            or_(
                operation.c.verifier_task_id == task_id,
                exists(
                    select(stage.c.operation_id).where(
                        stage.c.operation_id == operation.c.id,
                        stage.c.repair_task_id == task_id,
                    )
                ),
                exists(
                    select(resolution.c.id).where(
                        resolution.c.operation_id == operation.c.id,
                        resolution.c.repair_task_id == task_id,
                    )
                ),
            )
        )
        .limit(1)
    )
    return row.first() is not None


async def _cleanup_blockers_on(db, conn, ids: Sequence[str]) -> list[dict]:
    """Return only preserved delegate resources, not harmless stale locks."""
    blockers: list[dict] = []
    for task_id in sorted(ids):
        delegate = await _is_delegate_on(conn, task_id)
        for blocker in await db.get_integration_delegate_cleanup(task_id, conn=conn):
            # A detached reservation cannot write. It is released as part of
            # the removal; every other retained owner is intentionally a fence.
            if blocker["code"] == "branch_owner_retained" and (
                blocker.get("handoff_state") == "reserved"
                and not blocker.get("session_id")
                and not blocker.get("workspace_id")
            ):
                continue
            # A branch-owner row is a fence regardless of the task's role;
            # workspace/session preservation is meaningful only for a repair
            # delegate. A stale non-delegate lock remains ordinary cleanup.
            if blocker["code"] != "branch_owner_retained" and not delegate:
                continue
            blockers.append({"task_id": task_id, **blocker})
    return blockers


async def _development_undelivered_on(conn, ids: Sequence[str]) -> list[dict]:
    rows = (
        await conn.execute(
            select(tasks.c.id)
            .where(
                tasks.c.id.in_(list(ids)),
                tasks.c.status == TaskStatus.COMPLETED.value,
                _development_delivery_pending(tasks, include_foreign_repos=True),
            )
            .order_by(tasks.c.id)
        )
    ).scalars().all()
    if not rows:
        return []
    result: list[dict] = []
    for task_id in rows:
        holder = (
            await conn.execute(
                select(task_labels.c.label)
                .where(task_labels.c.task_id == task_id, task_labels.c.label.like("hold:%"))
                .order_by(task_labels.c.label)
                .limit(1)
            )
        ).scalar_one_or_none()
        if holder:
            result.append({"task_id": task_id, "holder": "hold_label", "detail": holder})
            continue
        open_gate = (
            await conn.execute(
                select(gates.c.id)
                .select_from(task_gates.join(gates, gates.c.id == task_gates.c.gate_id))
                .where(task_gates.c.task_id == task_id, gates.c.status == "open")
                .limit(1)
            )
        ).scalar_one_or_none()
        result.append(
            {
                "task_id": task_id,
                "holder": "open_gate" if open_gate else "awaiting_publication",
                "detail": str(open_gate) if open_gate else "not yet published",
            }
        )
    return result


async def _hierarchy_undelivered_on(
    conn, root_id: str, ids: Sequence[str], project_id: str
) -> tuple[str, list[dict]]:
    """Return default branch and the root's undelivered holder(s), if any."""
    repository = (
        await conn.execute(
            select(repos.c.id, repos.c.default_branch)
            .select_from(projects.join(repos, repos.c.id == projects.c.integration_repository_id))
            .where(projects.c.id == project_id)
        )
    ).mappings().one_or_none()
    if repository is None:
        return "default branch", []
    root = (
        await conn.execute(
            select(tasks.c.status, tasks.c.parent_task_id, tasks.c.repo_id, tasks.c.pr_url)
            .where(tasks.c.id == root_id)
        )
    ).mappings().one_or_none()
    if root is None or root["parent_task_id"] is not None:
        return str(repository["default_branch"]), []
    delivered = (
        await conn.execute(
            select(task_delivery_receipts.c.id)
            .where(
                task_delivery_receipts.c.source_task_id == root_id,
                task_delivery_receipts.c.target_task_id.is_(None),
                task_delivery_receipts.c.repository_id == repository["id"],
                func.regexp_replace(task_delivery_receipts.c.target_branch, "^refs/heads/", "")
                == func.regexp_replace(literal(repository["default_branch"]), "^refs/heads/", ""),
                task_delivery_receipts.c.disposition == "code",
            )
            .limit(1)
        )
    ).first()
    if delivered:
        return str(repository["default_branch"]), []
    collected = (
        await conn.execute(
            select(task_delivery_receipts.c.id)
            .where(
                task_delivery_receipts.c.source_task_id.in_(list(ids)),
                task_delivery_receipts.c.target_task_id.in_(list(ids)),
                task_delivery_receipts.c.disposition == "code",
            )
            .limit(1)
        )
    ).first()
    candidate = bool(
        root["status"] == TaskStatus.COMPLETED.value
        and root["repo_id"] == repository["id"]
        and str(root["pr_url"] or "").strip()
        and (
            await conn.execute(
                select(task_integration_checkpoints.c.task_id).where(
                    task_integration_checkpoints.c.task_id == root_id,
                    task_integration_checkpoints.c.repository_id == repository["id"],
                )
            )
        ).first()
        and (
            await conn.execute(
                select(task_branch_origins.c.id).where(
                    task_branch_origins.c.task_id == root_id,
                    task_branch_origins.c.repository_id == repository["id"],
                    task_branch_origins.c.retired_at.is_(None),
                )
            )
        ).first()
    )
    if not collected and not candidate:
        return str(repository["default_branch"]), []
    label = (
        await conn.execute(
            select(task_labels.c.label)
            .where(task_labels.c.task_id == root_id, task_labels.c.label.like("hold:%"))
            .order_by(task_labels.c.label)
            .limit(1)
        )
    ).scalar_one_or_none()
    holder = "hold_label" if label else ("collected_not_promoted" if collected else "awaiting_train")
    return str(repository["default_branch"]), [
        {"task_id": root_id, "holder": holder, "detail": label or holder}
    ]


async def undelivered_removal_holders(
    conn, *, root_id: str, ids: Sequence[str], project_id: str, mode: str | None
) -> tuple[str, list[dict]]:
    """Return the delivery holders an archive would otherwise refuse.

    The explicit abandon path records these same holders in its durable
    comment, so it cannot silently bypass a different delivery predicate.
    """
    if mode == "development":
        branch = (
            await conn.execute(
                select(repos.c.default_branch)
                .select_from(projects.join(repos, repos.c.id == projects.c.integration_repository_id))
                .where(projects.c.id == project_id)
            )
        ).scalar_one_or_none() or "default branch"
        return str(branch), await _development_undelivered_on(conn, ids)
    return await _hierarchy_undelivered_on(conn, root_id, ids, project_id)


async def assert_integration_permits_removal(
    db,
    conn,
    *,
    root_id: str,
    ids: Sequence[str],
    mutation: str,
    project_id: str,
    mode: str | None,
    abandon_undelivered: bool = False,
) -> None:
    """Raise the first ordered integration refusal for a removal.

    The caller owns the project's hierarchy advisory lock. This module makes
    no writes so a refusal leaves the archive/delete transaction untouched.
    """
    sealed = await _sealed_member_on(conn, root_id, ids)
    if sealed:
        raise _error(
            "sealed",
            f"{mutation} would change a sealed subtree: {sealed['task_id']} is a member of "
            f"integration batch {sealed['batch_id']} ({sealed['lifecycle']}). Wait for the "
            f"batch to finish; see `aq integration status {project_id}`.",
            {"task_id": sealed["task_id"], "batch_id": sealed["batch_id"], "lifecycle": sealed["lifecycle"]},
        )

    from src.integration.delegate_release import RELEASE_COMMAND, live_integration_owner

    owner = await live_integration_owner(conn, list(ids))
    if owner is not None:
        state = owner["state"]
        if state == "human_required":
            control = (
                f"Run `aq integration resume {owner['operation_id']}` to let it finish, or "
                f"`aq integration abort {owner['operation_id']} --reason \"...\"`"
            )
        else:
            control = (
                f"Wait for it, or cancel obsolete work with `aq integration cancel-preserving "
                f"{owner['operation_id']} --reason \"...\"`"
            )
        task_id = owner.get("task_id") or root_id
        raise _error(
            "integration_owned",
            f"integration operation {owner['operation_id']} is {state} and owns {task_id} as its "
            f"{owner['role']}; {mutation} is refused while it runs. {control}, then "
            f"`{RELEASE_COMMAND}`.",
            {"integration_operation": owner},
        )

    blockers = await _cleanup_blockers_on(db, conn, ids)
    if blockers:
        first = blockers[0]
        raise _error(
            "integration_cleanup_blocked",
            f"{first['task_id']} still holds {len(blockers)} preserved resource(s): "
            f"{first['code']}. {mutation} would release it silently; clear it first — "
            f"`aq task explain {first['task_id']}` lists each one.",
            {"blockers": blockers},
        )

    # Development publishers and their open repair manifests are another
    # form of live authority.  Check them before the delivery rule: an open
    # manifest needs to be settled, not merely abandoned, and this preserves
    # the long-standing ``integration_owned`` remedy for those callers.
    development_hold = await db._development_integration_hold(ids, project_id, conn=conn)
    if development_hold is not None:
        raise _error("integration_owned", development_hold)

    if mutation == "delete":
        found = await find_integration_task_references(conn, ids)
        if found:
            first = found[0]
            raise _error(
                "integration_history_retained",
                f"{mutation} is refused: {len(found)} integration audit record(s) name "
                f"{first['task_id']} ({describe_integration_references(found)}) and audit "
                "history is append-only. Archive the task instead; its history stays readable by id.",
                {"references": found},
            )
        return

    if abandon_undelivered:
        return
    branch, undelivered = await undelivered_removal_holders(
        conn, root_id=root_id, ids=ids, project_id=project_id, mode=mode
    )
    if undelivered:
        raise _error(
            "integration_undelivered",
            f"{len(undelivered)} task(s) under {root_id} have work that has not reached "
            f"{branch}: {undelivered[0]['task_id']} ({undelivered[0]['holder']}). Deliver it, "
            f"or abandon it with `aq task archive --task-id {root_id} "
            "--abandon-undelivered --reason \"...\"`.",
            {"mode": mode or "disabled", "default_branch": branch, "undelivered": undelivered},
        )
