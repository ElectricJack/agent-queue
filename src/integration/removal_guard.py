"""Read-only safety checks for removing a task with integration history.

Archive preserves an identity in ``archived_tasks``; hard delete does not.
The checks in this module deliberately run before the hierarchy-mode gate so
an old integration record cannot become unsafe merely because a project was
switched to development or disabled.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import exists, func, literal, or_, select

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
#: How an operator resolves completed work that has not reached the default
#: branch before it is abandoned or archived: keep it for the train to
#: deliver, or archive it as obsolete (with a note) or retired. An archive
#: always retires the subtree's branches through the audited path, which
#: bundles each unmerged tip before deleting it.
DELIVERY_DISPOSITIONS = ("deliver", "obsolete", "retire")
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


async def _development_undelivered_on(conn, ids: Sequence[str], delivery=None) -> list[dict]:
    """The COMPLETED tasks in *ids* whose work git has not proven on the target.

    *delivery* is the :class:`~src.integration.delivery_observer.DeliveryView`
    the caller took before this transaction; each identity is rechecked here.
    Evidence that is missing, stale or unknown holds the task exactly as
    pending work does, so an archive never trusts a row or a guess.
    """
    from src.integration.delivery_observer import delivery_sensitive_ids
    from src.integration.delivery_truth import DeliveryState

    sensitive = await delivery_sensitive_ids(conn, ids)
    verified = await delivery.verified_on(conn, sensitive) if delivery is not None else {}
    rows = sorted(
        task_id for task_id in sensitive
        if task_id not in verified or not verified[task_id].satisfied
    )
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
        if open_gate:
            result.append({"task_id": task_id, "holder": "open_gate", "detail": str(open_gate)})
            continue
        evidence = verified.get(task_id)
        if evidence is not None and evidence.state is DeliveryState.PENDING:
            result.append(
                {"task_id": task_id, "holder": "awaiting_publication",
                 "detail": "not yet published"}
            )
            continue
        reason = evidence.reason if evidence is not None else "delivery not verified in git"
        result.append({"task_id": task_id, "holder": "delivery_unknown", "detail": reason})
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
    conn, *, root_id: str, ids: Sequence[str], project_id: str, mode: str | None,
    delivery=None,
) -> tuple[str, list[dict]]:
    """Return the delivery holders an archive would otherwise refuse.

    The explicit abandon path records these same holders in its durable
    comment, so it cannot silently bypass a different delivery predicate.
    Development delivery is git's answer (*delivery*, see
    :func:`_development_undelivered_on`).
    """
    if mode == "development":
        branch = (
            await conn.execute(
                select(repos.c.default_branch)
                .select_from(projects.join(repos, repos.c.id == projects.c.integration_repository_id))
                .where(projects.c.id == project_id)
            )
        ).scalar_one_or_none() or "default branch"
        return str(branch), await _development_undelivered_on(conn, ids, delivery)
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
    delivery=None,
    obsolete_integration_delegate: bool = False,
) -> None:
    """Raise the first ordered integration refusal for a removal.

    The caller owns the project's hierarchy advisory lock. This module makes
    no writes so a refusal leaves the archive/delete transaction untouched.
    *delivery* is the git view taken before the transaction; without one,
    development work that needs proof is held as unknown.
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

    if obsolete_integration_delegate:
        from src.integration.delegate_release import assert_obsolete_delegate_on

        await assert_obsolete_delegate_on(db, conn, root_id)
    owner = await live_integration_owner(
        conn, list(ids), archive_obsolete=obsolete_integration_delegate
    )
    if owner is not None:
        state = owner["state"]
        control = "Inspect the owning Subject and its gate; wait for it to settle"
        task_id = owner.get("task_id") or root_id
        raise _error(
            "integration_owned",
            f"integration operation {owner['operation_id']} is {state} and owns {task_id} as its "
            f"{owner['role']}; {mutation} is refused while it runs. {control}, then "
            f"the daemon performs {RELEASE_COMMAND}.",
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
        conn, root_id=root_id, ids=ids, project_id=project_id, mode=mode, delivery=delivery
    )
    if undelivered:
        raise _error(
            "integration_undelivered",
            f"{len(undelivered)} task(s) under {root_id} have work that has not reached "
            f"{branch}: {undelivered[0]['task_id']} ({undelivered[0]['holder']}). Choose a "
            f"disposition with `aq task archive --task-id {root_id} --disposition ...`: "
            "`deliver` keeps it for the train, `obsolete --reason \"<note>\"` or "
            "`retire --reason \"...\"` archives it and retires its branches after a backup.",
            {"mode": mode or "disabled", "default_branch": branch, "undelivered": undelivered},
        )
