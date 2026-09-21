"""Release branch-owner rows still held for a task that finished or is gone.

``integration_branch_owners`` fences writers of a branch in the hierarchy
(``hierarchy``/``train``) integration modes.  A project switched to
``development`` keeps whatever rows it had: development claims create none,
and ``guard_integration_mutation`` -- the only path that releases a worker's
row when its task is deleted or archived -- returns early outside the
hierarchy modes.  So every task that finished, was archived or was deleted
after the switch left its row ``reserved`` (or ``attached``, when a stopped
writer's slot was reused) forever.  Branch cleanup must treat any row that is
not ``released`` as live, so those rows pin their delivered branches on the
remote indefinitely.

:func:`finished_branch_owners` lists such rows with a verdict each, and
:func:`release_finished_branch_owners` releases the ones with no blocker.  It
is narrow on purpose:

* only ``worker``/``repair`` rows, whose owner is a task -- a ``collector``
  row's owner is an operation, and none of the questions below are askable
  of it;
* only when the owning task is ``COMPLETED``/``FAILED``, archived, or missing
  from both task tables;
* only in a repository no hierarchy/train project integrates, now or as its
  desired mode -- there, a finished child's row is still the parent's to
  transfer, and the hierarchy recovery path owns it;
* never while a live session still names the task, a workspace is still
  locked by it, a candidate ref mutation is in flight on the branch, or a
  running integration operation owns the task;
* an ``attached``/``handoff_pending`` row additionally needs the stop proof
  ``DevelopmentIntegration.preserve_stopped_owners`` uses: its session row is
  stopped in both ``state`` and ``desired_state``, and the session provider
  confirms, by a fresh probe, that the process is gone.

Releasing touches the ownership row and nothing else: no checkout, workspace
lock, session, agent or task changes.  A ``reserved`` row is detached by
construction ("a detached reservation cannot issue Git writes", as
``cancel_preserving`` puts it), so it is released as is; an attached row also
gets a fresh fence so any holder of the old one is refused, and remembers its
last checkout in ``confirmed_workspace_id``.  Each release writes an
``integration.branch_owner_released`` event.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select, update

from src.database.queries.hierarchy_queries import HIERARCHY_MODES
from src.database.queries.integration_state_queries import session_attached_clause
from src.database.tables import (
    archived_tasks,
    integration_branch_owners,
    integration_candidate_ref_mutations,
    projects,
    sessions,
    tasks,
    workspaces,
)
from src.integration.delegate_release import live_integration_owner

#: Doctor check that reports and (with ``--fix``) releases these rows.
CHECK_ID = "integration.finished_branch_owners"

#: Roles whose ``owner_id`` is a task id.
TASK_OWNER_ROLES = ("worker", "repair")

#: Live task statuses that end the task's claim on its branch.
FINISHED_TASK_STATUSES = ("COMPLETED", "FAILED")

#: Handoff states that name a writer (session + workspace).
HELD_STATES = ("attached", "handoff_pending")

#: Event written once per released row.
RELEASE_EVENT = "integration.branch_owner_released"

#: ``confirm_stopped(session_row) -> bool`` -- a fresh provider probe.
StopConfirmer = Callable[[dict[str, Any]], Awaitable[bool]]

#: Row fields a release re-reads under lock and requires unchanged.
_IDENTITY_FIELDS = (
    "id",
    "repository_id",
    "ref",
    "owner_id",
    "owner_role",
    "fence_token",
    "handoff_state",
    "session_id",
    "workspace_id",
)

#: Session fields the stop proof was taken against.
_PROOF_FIELDS = ("id", "name", "provider", "instance_token", "state", "desired_state")


def stop_confirmer_for(orchestrator: Any) -> StopConfirmer | None:
    """The daemon's provider-backed stop probe, or ``None`` outside the daemon.

    It is the same probe ``preserve_stopped_owners`` runs, so a row this
    module releases passed exactly the proof that sweep would take.
    """
    development = getattr(orchestrator, "development_integration", None)
    return getattr(development, "confirm_stopped", None)


async def finished_branch_owners(
    db, *, confirm_stopped: StopConfirmer | None = None
) -> list[dict[str, Any]]:
    """Every non-released task-owned row whose owner finished or is gone.

    Each finding carries ``blocker``: ``None`` when the row can be released
    now, otherwise why it is kept.  Without *confirm_stopped* an attached row
    is kept, because only the provider can prove its writer stopped.
    """
    return [_public(finding) for finding in await _scan(db, confirm_stopped)]


async def release_finished_branch_owners(
    db,
    *,
    confirm_stopped: StopConfirmer | None = None,
    released_by: str,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Release every row :func:`finished_branch_owners` clears, and return them.

    Each row is re-proved in its own transaction under the project's hierarchy
    lock and the row's lock: a row whose identity, owner, blockers or proven
    session changed since the scan is skipped, never released on the snapshot.
    Safe to repeat -- a released row is not selected again.
    """
    candidates = [
        finding for finding in await _scan(db, confirm_stopped) if finding["blocker"] is None
    ]
    released = []
    for finding in candidates:
        stamp = time.time() if now is None else now
        async with db.immediate() as conn:
            project_ids = await _project_ids(conn, finding["row"]["repository_id"])
            for project_id in project_ids:
                await db.lock_hierarchy_project(conn, project_id)
            current = (
                (
                    await conn.execute(
                        select(integration_branch_owners)
                        .where(integration_branch_owners.c.id == finding["row"]["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is None or any(
                current[field] != finding["row"][field] for field in _IDENTITY_FIELDS
            ):
                continue
            evaluated = await _evaluate(conn, dict(current), lock=True)
            if evaluated is None or evaluated["blocker"] is not None:
                continue
            if evaluated["owner_status"] != finding["owner_status"]:
                continue
            if evaluated["session"] is not None and any(
                evaluated["session"][field] != finding["session"][field] for field in _PROOF_FIELDS
            ):
                continue
            values: dict[str, Any] = {"handoff_state": "released", "updated_at": stamp}
            if current["handoff_state"] in HELD_STATES:
                values.update(
                    fence_token=current["fence_token"] + 1,
                    session_id=None,
                    workspace_id=None,
                    confirmed_workspace_id=current["workspace_id"],
                )
            result = await conn.execute(
                update(integration_branch_owners)
                .where(
                    integration_branch_owners.c.id == current["id"],
                    integration_branch_owners.c.fence_token == current["fence_token"],
                    integration_branch_owners.c.handoff_state == current["handoff_state"],
                )
                .values(**values)
            )
            if result.rowcount != 1:
                continue
            release = {
                **_public(evaluated),
                "released_by": released_by,
                "released_at": stamp,
            }
            await db.log_event(
                RELEASE_EVENT,
                project_id=release["project_id"],
                task_id=release["owner_id"],
                payload=json.dumps(release, sort_keys=True),
                conn=conn,
            )
            released.append(release)
    return released


async def _scan(db, confirm_stopped: StopConfirmer | None) -> list[dict[str, Any]]:
    async with db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(integration_branch_owners)
                    .where(
                        integration_branch_owners.c.handoff_state != "released",
                        integration_branch_owners.c.owner_role.in_(TASK_OWNER_ROLES),
                    )
                    .order_by(
                        integration_branch_owners.c.repository_id,
                        integration_branch_owners.c.ref,
                    )
                )
            )
            .mappings()
            .all()
        )
        findings = []
        for row in rows:
            evaluated = await _evaluate(conn, dict(row), lock=False)
            if evaluated is not None:
                findings.append(evaluated)
    # The provider probe runs after the read connection is returned: no
    # process I/O while a database connection is held.
    for finding in findings:
        if finding["blocker"] is None and finding["session"] is not None:
            finding["blocker"] = await _stop_proof_blocker(finding["session"], confirm_stopped)
    return findings


async def _evaluate(conn, row: dict[str, Any], *, lock: bool) -> dict[str, Any] | None:
    """Classify one row; ``None`` when its owner has not finished.

    With *lock*, the owning task and the writer session are read
    ``FOR UPDATE`` so the verdict holds until the caller's transaction ends.
    """
    owner_id = row["owner_id"]
    task_query = select(tasks.c.status, tasks.c.project_id).where(tasks.c.id == owner_id)
    task = (await conn.execute(task_query.with_for_update() if lock else task_query)).one_or_none()
    if task is not None:
        if task.status not in FINISHED_TASK_STATUSES:
            return None
        owner_status, project_id = task.status, task.project_id
    else:
        archived = (
            await conn.execute(
                select(archived_tasks.c.status, archived_tasks.c.project_id).where(
                    archived_tasks.c.id == owner_id
                )
            )
        ).one_or_none()
        owner_status = "archived" if archived is not None else "missing"
        project_id = archived.project_id if archived is not None else None

    integrating = (
        await conn.execute(
            select(
                projects.c.id,
                projects.c.hierarchical_integration_mode,
                projects.c.hierarchical_integration_desired_mode,
            ).where(projects.c.integration_repository_id == row["repository_id"])
        )
    ).all()
    if project_id is None and len(integrating) == 1:
        project_id = integrating[0].id
    finding = {
        "row": row,
        "owner_status": owner_status,
        "project_id": project_id,
        "session": None,
        "blocker": await _blocker(conn, row, integrating),
    }
    if finding["blocker"] is None and row["handoff_state"] in HELD_STATES:
        session, blocker = await _stopped_writer(conn, row, lock=lock)
        finding["session"], finding["blocker"] = session, blocker
    return finding


async def _blocker(conn, row: dict[str, Any], integrating) -> str | None:
    owner_id = row["owner_id"]
    if not integrating:
        return (
            f"no project integrates repository {row['repository_id']}, so nothing says "
            "which integration mode owns this row"
        )
    for project in integrating:
        for mode in (
            project.hierarchical_integration_mode,
            project.hierarchical_integration_desired_mode,
        ):
            if mode in HIERARCHY_MODES:
                return (
                    f"project {project.id} integrates in {mode} mode, where a finished "
                    "task's branch is still its parent's to transfer; the hierarchy "
                    "recovery path owns this row"
                )
    live_session = (
        await conn.execute(
            select(sessions.c.id)
            .where(sessions.c.task_id == owner_id, session_attached_clause())
            .limit(1)
        )
    ).scalar_one_or_none()
    if live_session is not None:
        return f"session {live_session} is still live for {owner_id}"
    locked = (
        await conn.execute(
            select(workspaces.c.id).where(workspaces.c.locked_by_task_id == owner_id).limit(1)
        )
    ).scalar_one_or_none()
    if locked is not None:
        return f"workspace {locked} is still locked by {owner_id}"
    mutation = (
        await conn.execute(
            select(integration_candidate_ref_mutations.c.id)
            .where(
                integration_candidate_ref_mutations.c.repository_id == row["repository_id"],
                integration_candidate_ref_mutations.c.branch == row["ref"],
                integration_candidate_ref_mutations.c.state == "reserved",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if mutation is not None:
        return f"candidate ref mutation {mutation} is still in flight on {row['ref']}"
    operation = await live_integration_owner(conn, [owner_id])
    if operation is not None:
        return (
            f"integration operation {operation['operation_id']} is {operation['state']} "
            f"and owns {owner_id} as its {operation['role']}"
        )
    if row["handoff_state"] == "reserved" and (row["session_id"] or row["workspace_id"]):
        return "a reserved row that still names a session or workspace is not detached"
    return None


async def _stopped_writer(
    conn, row: dict[str, Any], *, lock: bool
) -> tuple[dict[str, Any] | None, str | None]:
    """The attached row's writer session, or why its stop cannot be proven."""
    if not row["session_id"] or not row["workspace_id"]:
        return None, (
            f"{row['handoff_state']} row names no session and workspace, so its writer's "
            "stop cannot be proven"
        )
    query = select(sessions).where(sessions.c.id == row["session_id"])
    session = (
        (await conn.execute(query.with_for_update() if lock else query)).mappings().one_or_none()
    )
    if session is None:
        return None, (
            f"writer session {row['session_id']} has no record, so its stop cannot be proven"
        )
    if session["state"] != "stopped" or session["desired_state"] != "stopped":
        return None, (
            f"writer session {session['id']} is {session['state']} "
            f"(desired {session['desired_state']})"
        )
    return dict(session), None


async def _stop_proof_blocker(
    session: dict[str, Any], confirm_stopped: StopConfirmer | None
) -> str | None:
    if confirm_stopped is None:
        return (
            f"proving writer session {session['id']} stopped needs the daemon's session "
            "provider; run this check through the daemon"
        )
    try:
        confirmed = await confirm_stopped(dict(session))
    except Exception as exc:  # noqa: BLE001 - an unprovable stop is a kept row
        return f"the session provider could not probe {session['name']}: {exc}"
    if not confirmed:
        return f"the session provider still reports {session['name']}"
    return None


async def _project_ids(conn, repository_id: str) -> list[str]:
    return sorted(
        (
            await conn.execute(
                select(projects.c.id).where(projects.c.integration_repository_id == repository_id)
            )
        ).scalars()
    )


def _public(finding: dict[str, Any]) -> dict[str, Any]:
    row = finding["row"]
    return {
        "owner_row_id": row["id"],
        "repository_id": row["repository_id"],
        "project_id": finding["project_id"],
        "ref": row["ref"],
        "owner_id": row["owner_id"],
        "owner_role": row["owner_role"],
        "owner_status": finding["owner_status"],
        "handoff_state": row["handoff_state"],
        "fence_token": int(row["fence_token"]),
        "session_id": row["session_id"],
        "workspace_id": row["workspace_id"],
        "blocker": finding["blocker"],
    }


__all__ = [
    "CHECK_ID",
    "RELEASE_EVENT",
    "finished_branch_owners",
    "release_finished_branch_owners",
    "stop_confirmer_for",
]
