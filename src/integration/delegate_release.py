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

The rule now is that **integration history names a task by id, never by foreign
key** (migration ``a00000000011``).  Liveness is enforced instead by
:meth:`~src.database.queries.hierarchy_queries.HierarchyQueryMixin.guard_integration_mutation`,
which can name the operation, its state and the command that releases it.

See ``docs/superpowers/specs/2026-09-20-integration-delegate-release-design.md``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import and_, delete, insert, or_, select

from src.database.tables import (
    integration_candidate_resolutions,
    integration_delegate_releases,
    integration_repair_operations,
    integration_repair_stages,
    sessions,
    task_metadata,
    tasks,
)
from src.models import TaskStatus

#: What an operator runs to settle every stranded delegate on the install.
RELEASE_COMMAND = "aq doctor --check integration.stranded_delegates --fix"

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
        f"required and cannot be {action}. Release it with `{RELEASE_COMMAND}`, then "
        "delete or archive the task."
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
        .where(
            sessions.c.task_id == tasks.c.id,
            or_(
                sessions.c.state != "stopped",
                sessions.c.desired_state != "stopped",
                sessions.c.claim_phase.is_not(None),
            ),
        )
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
    integration history still names it.  Since ``a00000000011`` that no longer
    blocks anything, and listing every historical verifier forever would bury
    the ones that are actually stuck.
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
    are separate facts.  A delegate with no live session or claim becomes
    terminal ``FAILED`` -- non-success and never runnable again -- so nothing
    schedules it, reminds about it or waits on it as paused work.  A retained
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
        # and who ended it" after the delete or archive the release unblocks.
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


async def live_integration_owner(conn, task_ids: list[str]) -> dict[str, Any] | None:
    """The first still-running operation that owns any task in *task_ids*.

    This is what replaced the four foreign keys.  It covers strictly more than
    they did: ``integration_repair_stages.repair_task_id`` never had a
    constraint at all, so the repair delegate of an *active* operation could be
    deleted out from under its running writer.
    """
    if not task_ids:
        return None
    operation = integration_repair_operations
    stage = integration_repair_stages
    resolution = integration_candidate_resolutions

    for role, condition in (
        ("verifier", operation.c.verifier_task_id.in_(task_ids)),
        ("parent", operation.c.parent_task_id.in_(task_ids)),
    ):
        row = (
            await conn.execute(
                select(operation.c.id, operation.c.state)
                .where(operation.c.state.in_(LIVE_OPERATION_STATES), condition)
                .order_by(operation.c.updated_at.desc(), operation.c.id)
                .limit(1)
            )
        ).mappings().first()
        if row is not None:
            return {"operation_id": row["id"], "state": row["state"], "role": role}

    row = (
        await conn.execute(
            select(operation.c.id, operation.c.state, stage.c.repair_task_id)
            .select_from(operation.join(stage, stage.c.operation_id == operation.c.id))
            .where(
                operation.c.state.in_(LIVE_OPERATION_STATES),
                stage.c.repair_task_id.in_(task_ids),
            )
            .order_by(operation.c.updated_at.desc(), operation.c.id)
            .limit(1)
        )
    ).mappings().first()
    if row is not None:
        return {"operation_id": row["id"], "state": row["state"], "role": "repair_stage"}

    row = (
        await conn.execute(
            select(operation.c.id, operation.c.state)
            .select_from(
                resolution.join(operation, operation.c.id == resolution.c.operation_id)
            )
            .where(
                and_(
                    resolution.c.repair_task_id.in_(task_ids),
                    resolution.c.state.in_(LIVE_RESOLUTION_STATES),
                    # An unfinished reservation whose operation already ended is
                    # moot, not a live claim -- requiring both is what keeps the
                    # release from re-wedging the very rows it exists to free.
                    operation.c.state.in_(LIVE_OPERATION_STATES),
                )
            )
            .order_by(resolution.c.updated_at.desc(), resolution.c.id)
            .limit(1)
        )
    ).mappings().first()
    if row is not None:
        return {"operation_id": row["id"], "state": row["state"], "role": "candidate_member"}
    return None
