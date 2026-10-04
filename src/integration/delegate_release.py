"""Release the delegate tasks of an integration operation that has ended.

A verifier, a repair-stage writer and a candidate-member resolver are all
*delegates*: tickets an integration operation files so someone does a piece of
its work.  When the operation ends -- cancelled by an operator, superseded by a
newer generation, or completed without ever needing that delegate -- the ticket
is obsolete.  Nothing schedules it, nothing waits on it, and nobody will ever
close it.

Until this module existed the obsolete ticket was also permanently in the way.
The ticket settled (:func:`release_delegates` is the same transition
``RepairService.retire_terminal_delegates`` always made), but four integration
history tables referenced ``tasks.id`` with ``RESTRICT``/``NO ACTION``, and both
``archive_task`` and ``delete_task`` remove the ``tasks`` row -- so the operator
got ``ForeignKeyViolationError`` with no subject, no cause and no remedy.

The archive-history revision drops those four foreign keys: archive preserves
the id in ``archived_tasks`` while hard delete remains refused by
``src.integration.removal_guard``. What this module adds is the *live* half:
:meth:`~src.database.queries.hierarchy_queries.HierarchyQueryMixin.guard_integration_mutation`
refuses a removal while a running operation owns the task, naming the
operation, its state and the command that releases it, and the release itself
settles the ticket and writes an ``integration_delegate_releases`` audit row.

See ``docs/superpowers/specs/2026-09-20-integration-delegate-release-design.md``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import and_, delete, insert, or_, select

from src.database.queries.integration_state_queries import session_attached_clause
from src.database.tables import (
    integration_candidate_resolutions,
    integration_delegate_releases,
    integration_repair_operations,
    integration_repair_stages,
    agents,
    gates,
    sessions,
    task_dependencies,
    task_gates,
    task_metadata,
    task_results,
    tasks,
)
from src.models import TaskStatus

#: The daemon maintenance source that settles stranded terminal delegates.
RELEASE_COMMAND = "automatic delegate cleanup"

#: Operation states that mean the operation is over.  ``cancelled`` disposes of
#: its delegates as ``cancelled``; ``completed`` means the operation finished
#: without this delegate, which is ``superseded``.
ENDED_OPERATION_STATES = ("completed", "cancelled")

#: Operation states in which the operation is still running, so its delegates
#: are its own business and no removal may take them.
LIVE_OPERATION_STATES = ("active", "escalated", "human_required")

#: Candidate-member reservation states that still hold a real writer.
LIVE_RESOLUTION_STATES = ("reserved", "pushed")

#: A delegate in one of these has not settled yet.  ``ASSIGNED``/``IN_PROGRESS``
#: are absent on purpose: those carry a live writer, whose authority is never
#: taken here.
UNSETTLED_DELEGATE_STATUSES = ("DEFINED", "READY", "BLOCKED", "PAUSED")

#: Task metadata a release clears.  The hold a cancellation placed is
#: superseded by the terminal disposition; its snapshot is kept in
#: ``integration_retirement`` as evidence.
_CLEARED_HOLDS = (
    "needs_attention",
    "claim_prepare_backoff_until",
    "blocked_terminal",
    "manual_pause",
)


def retired_delegate_message(operation_id: str, state: str, *, action: str) -> str:
    """Why a delegate cannot be *action*-ed, and what to run instead.

    ``BLOCKED`` with no explanation is what this replaces: the operator could
    see the task was stuck but not which operation owned it, whether that
    operation was still running, or what would let go.
    """
    return (
        f"Integration operation {operation_id} is {state}; its delegate is no longer "
        f"required and cannot be {action}. The daemon's {RELEASE_COMMAND} "
        "settles as FAILED. It can be deleted or archived only once no integration "
        "history names it."
    )


def retired_writer_close_feedback(retirement: dict[str, Any]) -> str:
    """What a retired delegate's writer is told when its close is refused.

    *retirement* is ``get_retired_integration_writer``'s proof.  The close can
    never be accepted, so the worker is told to stop and acknowledge its
    drain; the session reconciler stops a drain-acknowledged worker holding a
    proven-retired delegate, and owner recovery preserves its checkout before
    anything is released (bold-impact-53).
    """
    reason = str(retirement["reason"])
    return (
        f"{reason[:1].upper()}{reason[1:]}: this delegate is retired "
        f"({retirement['disposition']}), so there is nothing left to close. "
        "Do not retry the close and do not close --outcome fail. Its branch and "
        "workspace stay preserved for cleanup; stop working and run "
        "`aq session drain-ack`. The session reconciler then stops this session, "
        "owner recovery preserves its checkout, and the integration reconciler "
        "records the retirement."
    )


def _decoded(raw: Any) -> Any:
    """A ``task_metadata`` value; older rows may hold an unencoded string."""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _owned_by(operation, task_id_column):
    """The OR of every way *operation* can own the task in ``task_id_column``."""
    stage = integration_repair_stages
    resolution = integration_candidate_resolutions
    return or_(
        operation.c.verifier_task_id == task_id_column,
        select(stage.c.operation_id)
        .where(stage.c.operation_id == operation.c.id, stage.c.repair_task_id == task_id_column)
        .correlate(operation, tasks)
        .exists(),
        select(resolution.c.id)
        .where(
            resolution.c.operation_id == operation.c.id,
            resolution.c.repair_task_id == task_id_column,
        )
        .correlate(operation, tasks)
        .exists(),
    )


async def _role_of(conn, operation_id: str, task_id: str) -> str:
    """Which delegate seat *task_id* occupies in *operation_id*.

    A task can sit in more than one (a repair-stage writer whose candidate
    reservation names it too); the most specific seat that explains why it
    exists wins, and the audit row records that one.
    """
    verifier = (
        await conn.execute(
            select(integration_repair_operations.c.id).where(
                integration_repair_operations.c.id == operation_id,
                integration_repair_operations.c.verifier_task_id == task_id,
            )
        )
    ).first()
    if verifier is not None:
        return "verifier"
    stage = (
        await conn.execute(
            select(integration_repair_stages.c.ordinal).where(
                integration_repair_stages.c.operation_id == operation_id,
                integration_repair_stages.c.repair_task_id == task_id,
            )
        )
    ).first()
    if stage is not None:
        return "repair_stage"
    return "candidate_member"


def _stranded_statement(*, operation_ids: list[str] | None, limit: int):
    """Delegates of an ended operation that have not settled and have no writer."""
    operation = integration_repair_operations
    attached_session = (
        select(sessions.c.id)
        .where(sessions.c.task_id == tasks.c.id, session_attached_clause())
        .exists()
    )
    statement = (
        select(
            tasks,
            operation.c.id.label("operation_id"),
            operation.c.state.label("operation_state"),
        )
        .select_from(tasks.join(operation, _owned_by(operation, tasks.c.id)))
        .where(
            operation.c.state.in_(ENDED_OPERATION_STATES),
            tasks.c.status.in_(UNSETTLED_DELEGATE_STATUSES),
            tasks.c.assigned_agent_id.is_(None),
            ~attached_session,
        )
        .order_by(operation.c.updated_at, tasks.c.id)
        .limit(limit)
    )
    if operation_ids is not None:
        statement = statement.where(operation.c.id.in_(operation_ids))
    return statement


async def stranded_delegates(
    db, *, conn=None, operation_ids: list[str] | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Report the delegates :func:`release_delegates` would settle.

    Read-only, and deliberately narrow: a task is *not* listed merely because
    integration history still names it.  That history is a separate question
    (the integration-history removal guard), and listing every historical
    verifier forever would bury the ones that are actually stuck.
    """
    if conn is None:
        async with db._engine.connect() as owned:
            return await stranded_delegates(
                db, conn=owned, operation_ids=operation_ids, limit=limit
            )
    statement = _stranded_statement(operation_ids=operation_ids, limit=limit)
    rows = (await conn.execute(statement)).mappings().all()
    seen: set[str] = set()
    reported: list[dict[str, Any]] = []
    for row in rows:
        if row["id"] in seen:
            continue  # owned by more than one ended operation
        seen.add(row["id"])
        reported.append(
            {
                "task_id": row["id"],
                "project_id": row["project_id"],
                "title": row["title"],
                "task_status": row["status"],
                "operation_id": row["operation_id"],
                "operation_state": row["operation_state"],
                "role": await _role_of(conn, row["operation_id"], row["id"]),
                "disposition": _disposition(row["operation_state"]),
                "cleanup": await db.get_integration_delegate_cleanup(row["id"], conn=conn),
            }
        )
    return reported


def _disposition(operation_state: str) -> str:
    return "cancelled" if operation_state == "cancelled" else "superseded"


async def release_delegates_on(
    db,
    conn,
    *,
    now: float,
    released_by: str,
    operation_ids: list[str] | None = None,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Settle stranded delegates on a caller-owned transaction.

    Returns ``(releases, transitions)``; the caller fires the transitions'
    post-commit notifications once its own transaction has committed.

    The ticket's execution disposition and the cleanup of what it still holds
    are separate facts.  A delegate with no session that is anything but fully
    stopped becomes terminal ``FAILED`` -- non-success and never runnable
    again -- so nothing schedules it, reminds about it or waits on it as paused
    work.  A stopped writer's retained claim is history, not a writer
    (:func:`session_attached_clause`).  A retained
    branch owner or workspace lock does not keep the ticket open: it is
    preserved exactly as found and recorded as a named cleanup blocker, which
    ``aq task explain`` re-reads live.  A live writer still defers the release;
    its authority is never taken here.  Branches, stage evidence and retry
    counters are untouched, and no successful completion is manufactured.  A
    delegate an earlier version of this pass left ``PAUSED`` rolls forward on
    the next call.
    """
    rows = (
        await conn.execute(
            _stranded_statement(operation_ids=operation_ids, limit=limit).with_for_update(
                of=(tasks, integration_repair_operations), skip_locked=True
            )
        )
    ).mappings().all()
    releases: list[dict[str, Any]] = []
    transitions: list[Any] = []
    settled: set[str] = set()
    for task in rows:
        if task["id"] in settled:
            continue  # owned by more than one ended operation
        settled.add(task["id"])
        state = task["operation_state"]
        disposition = _disposition(state)
        reason = f"integration operation {task['operation_id']} is {state}"
        meta = {
            key: _decoded(value)
            for key, value in (
                await conn.execute(
                    select(task_metadata.c.key, task_metadata.c.value).where(
                        task_metadata.c.task_id == task["id"],
                        task_metadata.c.key.in_(
                            ("integration_retirement", "needs_attention", "manual_pause")
                        ),
                    )
                )
            ).all()
        }
        earlier = meta.get("integration_retirement")
        earlier = earlier if isinstance(earlier, dict) else {}
        cleanup = await db.get_integration_delegate_cleanup(task["id"], conn=conn)
        role = await _role_of(conn, task["operation_id"], task["id"])
        previous_status = earlier.get("previous_status", task["status"])
        transition = await db._apply_transition(
            conn,
            task["id"],
            TaskStatus.FAILED,
            context="integration_delegate_retired",
            force=True,
            _manual_pause_control=True,
            resume_after=None,
        )
        transitions.append(transition)
        await db._upsert_meta(
            task["id"],
            "integration_retirement",
            {
                "operation_id": task["operation_id"],
                "state": state,
                "disposition": disposition,
                "previous_status": previous_status,
                "retired_at": now,
                "reason": reason,
                "previous_attention": meta.get(
                    "needs_attention", earlier.get("previous_attention")
                ),
                "previous_hold": meta.get("manual_pause"),
                **({"paused_at": earlier["retired_at"]} if "retired_at" in earlier else {}),
                "cleanup": {"state": "blocked" if cleanup else "clear", "blockers": cleanup},
            },
            conn=conn,
        )
        await conn.execute(
            delete(task_metadata).where(
                task_metadata.c.task_id == task["id"],
                task_metadata.c.key.in_(_CLEARED_HOLDS),
            )
        )
        release = {
            "id": f"rel-{uuid.uuid4().hex[:12]}",
            "operation_id": task["operation_id"],
            "operation_state": state,
            "task_id": task["id"],
            "project_id": task["project_id"],
            "role": role,
            "disposition": disposition,
            "previous_status": previous_status,
            "reason": reason,
            "released_by": released_by,
            "released_at": now,
            "cleanup": {"state": "blocked" if cleanup else "clear", "blockers": cleanup},
        }
        # The audit row outlives the task: it is what answers "why did this end,
        # and who ended it" after a later delete or archive of the task.
        await conn.execute(insert(integration_delegate_releases).values(**release))
        await db.log_event(
            "task.updated",
            project_id=task["project_id"],
            task_id=task["id"],
            payload=f"{reason}; delegate retired as {disposition}",
            conn=conn,
        )
        releases.append(release)
    return releases, transitions


async def release_delegates(
    db,
    *,
    now: float,
    released_by: str,
    operation_ids: list[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Open a transaction, run :func:`release_delegates_on`, then notify."""
    async with db.immediate() as conn:
        releases, transitions = await release_delegates_on(
            db,
            conn,
            now=now,
            released_by=released_by,
            operation_ids=operation_ids,
            limit=limit,
        )
    for transition in transitions:
        await db.log_blocked_flips(transition.flipped)
        await db._notify_settled(transition.settled)
        await db._notify_ready(transition.ready)
    return releases


async def live_integration_owner(
    conn, task_ids: list[str], *, archive_obsolete: bool = False
) -> dict[str, Any] | None:
    """The first still-running operation that owns any task in *task_ids*.

    It covers a reference the foreign keys never did:
    ``integration_repair_stages.repair_task_id`` has no constraint at all, so
    the repair delegate of an *active* operation could be deleted out from
    under its running writer.
    """
    if not task_ids:
        return None
    operation = integration_repair_operations
    stage = integration_repair_stages
    resolution = integration_candidate_resolutions

    for role, task_column, condition in (
        ("verifier", operation.c.verifier_task_id, operation.c.verifier_task_id.in_(task_ids)),
        ("parent", operation.c.parent_task_id, operation.c.parent_task_id.in_(task_ids)),
    ):
        row = (
            await conn.execute(
                select(operation.c.id, operation.c.state, task_column.label("task_id"))
                .where(operation.c.state.in_(LIVE_OPERATION_STATES), condition)
                .order_by(operation.c.updated_at.desc(), operation.c.id)
                .limit(1)
            )
        ).mappings().first()
        if row is not None:
            return {
                "operation_id": row["id"],
                "state": row["state"],
                "role": role,
                "task_id": row["task_id"],
            }

    row = (
        await conn.execute(
            select(operation.c.id, operation.c.state, stage.c.repair_task_id)
            .select_from(operation.join(stage, stage.c.operation_id == operation.c.id))
            .where(
                operation.c.state.in_(LIVE_OPERATION_STATES),
                stage.c.repair_task_id.in_(task_ids),
                or_(
                    stage.c.ordinal >= operation.c.active_stage,
                    stage.c.state.not_in(("passed", "failed", "expired", "cancelled")),
                ) if archive_obsolete else True,
            )
            .order_by(operation.c.updated_at.desc(), operation.c.id)
            .limit(1)
        )
    ).mappings().first()
    if row is not None:
        return {
            "operation_id": row["id"],
            "state": row["state"],
            "role": "repair_stage",
            "task_id": row["repair_task_id"],
        }

    row = (
        await conn.execute(
            select(operation.c.id, operation.c.state, resolution.c.repair_task_id)
            .select_from(
                resolution.join(operation, operation.c.id == resolution.c.operation_id)
            )
            .where(
                and_(
                    resolution.c.repair_task_id.in_(task_ids),
                    # The operation's state, not the resolution's local state,
                    # owns authority. A rejected reservation under a live
                    # operation still names the delegate it may inspect.
                    operation.c.state.in_(LIVE_OPERATION_STATES),
                )
            )
            .order_by(resolution.c.updated_at.desc(), resolution.c.id)
            .limit(1)
        )
    ).mappings().first()
    if row is not None:
        return {
            "operation_id": row["id"],
            "state": row["state"],
            "role": "candidate_member",
            "task_id": row["repair_task_id"],
        }
    return None


async def assert_obsolete_delegate_on(db, conn, task_id: str) -> dict[str, Any]:
    """Prove a generated delegate can leave the graph without releasing authority.

    Called inside the ordinary archive transaction under the project lock.
    It never releases an owner, claim or human gate to make the proof pass.
    """
    from src.database.queries.hierarchy_queries import HierarchyError

    refs = [dict(row) for row in (await conn.execute(
        select(integration_repair_stages, integration_repair_operations.c.state.label(
            "operation_state"), integration_repair_operations.c.active_stage)
        .join(integration_repair_operations,
              integration_repair_operations.c.id == integration_repair_stages.c.operation_id)
        .where(integration_repair_stages.c.repair_task_id == task_id)
        .with_for_update(of=(integration_repair_operations, integration_repair_stages))
    )).mappings()]
    # Repair dispatch holds operation/stage before its task. Follow the same
    # order so an archive cannot deadlock that authority check.
    task = (await conn.execute(select(tasks).where(
        tasks.c.id == task_id,
    ).with_for_update())).mappings().one_or_none()
    if task is None:
        raise HierarchyError("not_found", task_id)
    blockers = []
    if (
        task["created_by_kind"] != "integration_repair"
        or task["status"] not in {"COMPLETED", "FAILED", "BLOCKED"}
        or task["parent_task_id"] is not None
    ):
        blockers.append({"code": "not_terminal_generated_delegate"})
    if task["assigned_agent_id"] is not None:
        blockers.append({"code": "assigned_agent"})
    if not refs or any(
        row["operation_state"] not in ENDED_OPERATION_STATES and (
            row["ordinal"] >= row["active_stage"]
            or row["state"] not in {"passed", "failed", "expired", "cancelled"}
        ) for row in refs
    ):
        blockers.append({"code": "current_repair_delegate"})
    owner = await live_integration_owner(conn, [task_id], archive_obsolete=True)
    if owner is not None:
        blockers.append({"code": "integration_owned", **owner})
    blockers.extend(await db.get_integration_delegate_cleanup(task_id, conn=conn))
    claims = (await conn.execute(select(sessions.c.id).where(
        sessions.c.task_id == task_id, sessions.c.claim_phase.is_not(None),
    ).with_for_update())).scalars().all()
    blockers.extend({"code": "claim_retained", "session_id": sid} for sid in claims)
    if (await conn.execute(select(agents.c.id).where(
        agents.c.current_task_id == task_id,
    ))).first():
        blockers.append({"code": "agent_claim_retained"})
    if (await conn.execute(select(tasks.c.id).where(
        tasks.c.parent_task_id == task_id,
    ))).first():
        blockers.append({"code": "children_retained"})
    dependencies = [dict(row) for row in (await conn.execute(select(task_dependencies).where(
        or_(task_dependencies.c.task_id == task_id,
            task_dependencies.c.depends_on_task_id == task_id),
    ))).mappings()]
    if any(row["dep_type"] in {"blocks", "parent-child", "waits-for", "conditional-blocks"}
           for row in dependencies):
        blockers.append({"code": "dependency_retained"})
    open_gates = (await conn.execute(select(gates.c.id).join(
        task_gates, task_gates.c.gate_id == gates.c.id,
    ).where(task_gates.c.task_id == task_id, gates.c.status == "open")
        .with_for_update(of=gates))).scalars().all()
    blockers.extend({"code": "open_gate", "gate_id": gid} for gid in open_gates)
    if (await conn.execute(select(integration_candidate_resolutions.c.id).where(
        integration_candidate_resolutions.c.repair_task_id == task_id,
        integration_candidate_resolutions.c.state.in_(LIVE_RESOLUTION_STATES),
    ).with_for_update())).first():
        blockers.append({"code": "candidate_resolution_retained"})
    if blockers:
        raise HierarchyError("delegate_archive_blocked", task_id, {"blockers": blockers})
    metadata = dict((await conn.execute(select(task_metadata.c.key, task_metadata.c.value).where(
        task_metadata.c.task_id == task_id,
    ))).all())
    results = [dict(row) for row in (await conn.execute(select(task_results).where(
        task_results.c.task_id == task_id,
    ).order_by(task_results.c.created_at, task_results.c.id))).mappings()]
    session_history = (await conn.execute(select(sessions.c.id).where(
        sessions.c.task_id == task_id,
    ).order_by(sessions.c.id))).scalars().all()
    return {"stages": refs, "dependencies": dependencies, "metadata": metadata,
            "results": results, "session_ids": session_history}


async def archive_obsolete_delegates(
    db, *, operation_ids=None, limit: int = 100, after: str | None = None
) -> dict:
    """Reconcile a bounded page; all safety proofs are repeated by archive_task."""
    from src.database.queries.hierarchy_queries import HierarchyError

    stage, operation = integration_repair_stages, integration_repair_operations
    statement = select(tasks.c.id).join(stage, stage.c.repair_task_id == tasks.c.id).join(
        operation, operation.c.id == stage.c.operation_id,
    ).where(
        tasks.c.created_by_kind == "integration_repair",
        tasks.c.status.in_(("COMPLETED", "FAILED", "BLOCKED")),
        or_(operation.c.state.in_(ENDED_OPERATION_STATES), and_(
            stage.c.ordinal < operation.c.active_stage,
            stage.c.state.in_(("passed", "failed", "expired", "cancelled")),
        )),
    ).distinct().order_by(tasks.c.id).limit(limit)
    if operation_ids is not None:
        statement = statement.where(operation.c.id.in_(operation_ids))
    if after is not None:
        statement = statement.where(tasks.c.id > after)
    async with db._engine.connect() as conn:
        candidates = (await conn.execute(statement)).scalars().all()
    archived, blockers = [], []
    for task_id in candidates:
        try:
            if await db.archive_task(task_id, obsolete_integration_delegate=True):
                archived.append(task_id)
        except HierarchyError as exc:
            blockers.append({"task_id": task_id, "code": exc.code, "detail": exc.detail,
                             **exc.context})
    return {"archived_delegates": archived, "blockers": blockers,
            "next_after": candidates[-1] if len(candidates) == limit else None}
