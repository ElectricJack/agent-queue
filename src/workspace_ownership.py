"""Ownership fences shared by workspace controls, placement and recovery."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from sqlalchemy import and_, exists, func, or_, select, update

from src.claim_file import CLAIM_FILE, read_claim_file
from src.database.tables import (
    agents,
    integration_branch_owners as owners,
    sessions,
    task_metadata,
    tasks,
    workspaces,
)
from src.integration.row_query import select_row
from src.waits.sources import execute_workspace_release

LIVE_STATES = ("starting", "running", "draining", "sleeping")
ATTACHED_STATES = ("attached", "handoff_pending")


def session_has_retained_task():
    """A stopped claim is recovery evidence only for its current unfinished task."""
    return exists(select(tasks.c.id).where(
        tasks.c.id == sessions.c.task_id,
        tasks.c.status.not_in(("COMPLETED", "FAILED")),
        or_(tasks.c.assigned_agent_id.is_(None),
            tasks.c.assigned_agent_id == sessions.c.agent_id),
        or_(sessions.c.last_claim_epoch.is_(None),
            tasks.c.claim_epoch == sessions.c.last_claim_epoch),
    )).correlate(sessions)


async def clear_stale_stopped_claim_phases(conn, *, session_id=None):
    """Repair historical phases without releasing any independent ownership fence."""
    stmt = update(sessions).where(
        sessions.c.state == "stopped",
        # A taskless phase can be an incomplete preparation. Without a task
        # identity, this repair cannot prove displacement or settlement.
        sessions.c.task_id.is_not(None),
        sessions.c.claim_phase.is_not(None),
        ~session_has_retained_task(),
    )
    if session_id is not None:
        stmt = stmt.where(sessions.c.id == session_id)
    result = await conn.execute(stmt.values(claim_phase=None, claim_phase_at=None))
    return result.rowcount


def session_retains_workspace():
    """Live writers and current recovery claims retain their checkout."""
    released_cooldown = and_(
        sessions.c.lifecycle == "task",
        sessions.c.state == "sleeping",
        sessions.c.sleep_reason == "rate_limit",
        ~exists(select(tasks.c.id).where(
            tasks.c.id == sessions.c.task_id,
            tasks.c.assigned_agent_id == sessions.c.agent_id,
        )).correlate(sessions),
    )
    return or_(
        and_(sessions.c.state.in_(LIVE_STATES), ~func.coalesce(released_cooldown, False)),
        and_(
            sessions.c.task_id.is_not(None), sessions.c.claim_phase.is_not(None),
            or_(sessions.c.state != "stopped", session_has_retained_task()),
        ),
    )


def unattached_workspace():
    """A cleared lock row does not revoke a session or integration attachment."""
    return and_(
        ~exists(
            select(sessions.c.id).where(
                sessions.c.work_dir == workspaces.c.workspace_path,
                session_retains_workspace(),
            )
        ).correlate(workspaces),
        ~exists(
            select(owners.c.id).where(
                or_(
                    owners.c.workspace_id == workspaces.c.id,
                    owners.c.confirmed_workspace_id == workspaces.c.id,
                ),
                owners.c.handoff_state.in_(ATTACHED_STATES),
            )
        ).correlate(workspaces),
    )


async def claim_blocks_acquisition(conn, path, *, requesting_task_id=None):
    """Permit known released markers to reach the existing stale-marker recovery.

    Unknown/unreadable markers fail closed. No marker is removed by placement.
    """
    if not os.path.lexists(os.path.join(path, CLAIM_FILE)):
        return False
    claim = read_claim_file(path)
    if not claim or not isinstance(claim.get("task_id"), str):
        return True
    row = await select_row(
        conn, tasks, tasks.c.id == claim["task_id"],
    )
    session = await select_row(
        conn, sessions, sessions.c.id == claim.get("session_id"), session_retains_workspace(),
    )
    if session:
        return True
    if requesting_task_id == claim["task_id"]:
        return False
    if row is None:
        return False
    epoch = claim.get("claim_epoch")
    if isinstance(epoch, int) and (row["claim_epoch"] or 0) > epoch:
        return False
    return row["status"] not in {"COMPLETED", "FAILED", "PAUSED", "BLOCKED", "READY", "DEFINED"}


async def ownership_blockers(conn, ws):
    """Fresh protection evidence, including holders absent from the lock columns."""
    path = ws["workspace_path"]
    blockers = []
    if os.path.lexists(os.path.join(path, CLAIM_FILE)):
        claim = read_claim_file(path) or {}
        blockers.append(
            {
                "kind": "claim_file",
                "task_id": claim.get("task_id"),
                "session_id": claim.get("session_id"),
                "claim_epoch": claim.get("claim_epoch"),
            }
        )
    if ws["locked_by_agent_id"]:
        agent = await select_row(
            conn, agents, agents.c.id == ws["locked_by_agent_id"],
        )
        if agent and (agent["state"] == "BUSY" or agent["current_task_id"]):
            blockers.append(
                {
                    "kind": "agent",
                    "task_id": agent["current_task_id"],
                    "agent_id": agent["id"],
                    "state": agent["state"],
                }
            )
    rows = (
        (
            await conn.execute(
                select(sessions).where(
                    or_(
                        sessions.c.work_dir == path,
                        sessions.c.agent_id == ws["locked_by_agent_id"]
                        if ws["locked_by_agent_id"]
                        else False,
                    ),
                    session_retains_workspace(),
                )
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        blockers.append(
            {
                "kind": "session",
                "task_id": row["task_id"],
                "session_id": row["id"],
                "state": row["state"],
                "claim_phase": row["claim_phase"],
            }
        )
    for row in (
        await conn.execute(
            select(owners).where(
                or_(owners.c.workspace_id == ws["id"], owners.c.confirmed_workspace_id == ws["id"]),
                owners.c.handoff_state.in_(ATTACHED_STATES),
            )
        )
    ).mappings():
        blockers.append(
            {
                "kind": "integration_owner",
                "task_id": row["owner_id"],
                "owner_id": row["id"],
                "session_id": row["session_id"],
                "handoff_state": row["handoff_state"],
            }
        )
    if ws["job_pin_count"]:
        blockers.append({"kind": "job_pin", "task_id": None})
    return blockers


async def release_workspace(db, workspace_id, *, force=False, task_id=None):
    """Guard the operator release on current evidence rather than a listing."""
    async with db.immediate() as conn:
        ws = (
            (
                await conn.execute(
                    select(workspaces)
                    .where(
                        workspaces.c.id == workspace_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if ws is None:
            return {"success": False, "error": f"Workspace '{workspace_id}' not found"}
        blockers = await ownership_blockers(conn, ws)
        if ws["locked_by_task_id"]:
            blockers.append({"kind": "task_lock", "task_id": ws["locked_by_task_id"]})
        protected = {b["task_id"] for b in blockers}
        if force and (not task_id or protected != {task_id}):
            return {
                "success": False,
                "error": "Force requires the exact protected task_id",
                "blockers": blockers,
            }
        if blockers and not force:
            return {
                "success": False,
                "error": "Workspace ownership is retained",
                "blockers": blockers,
            }
        if ws["job_pin_count"]:
            return {
                "success": False,
                "error": "Job pins must be released by their owner",
                "blockers": blockers,
            }
        await execute_workspace_release(
            conn,
            update(workspaces)
            .where(
                workspaces.c.id == workspace_id,
            )
            .values(
                locked_by_agent_id=None,
                locked_by_task_id=None,
                locked_at=None,
                lock_mode=None,
                generation=workspaces.c.generation + 1,
            ),
        )
        result = {
            "success": True,
            "workspace_id": workspace_id,
            "forced": force,
            "released_from_agent": ws["locked_by_agent_id"],
            "released_from_task": ws["locked_by_task_id"],
        }
        await db.log_event(
            "workspace.released",
            project_id=ws["project_id"],
            payload=json.dumps(result | {"task_id": task_id}),
            conn=conn,
        )
        return result


async def rebind_workspace(db, workspace_id, task_id, session_id):
    """Restore a lost lock only from a coherent, existing live claim."""
    async with db.immediate() as conn:
        # Match the claim writer's session-before-workspace lock order.
        session = (
            (
                await conn.execute(
                    select(sessions)
                    .where(
                        sessions.c.id == session_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        agent = (
            (
                await conn.execute(
                    select(agents)
                    .where(
                        agents.c.id == session["agent_id"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
            if session
            else None
        )
        task = (
            (
                await conn.execute(
                    select(tasks)
                    .where(
                        tasks.c.id == task_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        ws = (
            (
                await conn.execute(
                    select(workspaces)
                    .where(
                        workspaces.c.id == workspace_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        invalid = {
            "success": False,
            "error": "Workspace re-bind requires one coherent active claim",
        }
        if session is None or task is None or ws is None or agent is None:
            return invalid
        claim = read_claim_file(ws["workspace_path"])
        claimed_by = await conn.scalar(
            select(task_metadata.c.value).where(
                task_metadata.c.task_id == task_id,
                task_metadata.c.key == "claimed_by_session",
            )
        )
        if (
            session["lifecycle"] != "pool"
            or session["state"] not in ("starting", "running", "draining")
            or session["task_id"] != task_id
            or session["claim_phase"] != "active"
            or session["project_id"] != ws["project_id"]
            or task["project_id"] != ws["project_id"]
            or agent["current_task_id"] != task_id
            or claimed_by != json.dumps(session_id)
            or not session["work_dir"]
            or task["status"] != "IN_PROGRESS"
            or not session["agent_id"]
            or task["assigned_agent_id"] != session["agent_id"]
            or Path(session["work_dir"]).resolve() != Path(ws["workspace_path"]).resolve()
            or session["last_claim_epoch"] != task["claim_epoch"]
            or not claim
            or claim.get("task_id") != task_id
            or claim.get("session_id") != session_id
            or claim.get("claim_epoch") != task["claim_epoch"]
            or ws["locked_by_agent_id"] not in (None, session["agent_id"])
            or ws["locked_by_task_id"] not in (None, task_id)
            or ws["job_pin_count"]
        ):
            return invalid
        blockers = await ownership_blockers(conn, ws)
        if any(
            b.get("task_id") != task_id or b.get("session_id") not in (None, session_id)
            for b in blockers
        ):
            return invalid | {"blockers": blockers}
        await conn.execute(
            update(workspaces)
            .where(workspaces.c.id == workspace_id)
            .values(
                locked_by_task_id=task_id,
                locked_by_agent_id=session["agent_id"],
                locked_at=ws["locked_at"] or time.time(),
                lock_mode="exclusive",
                generation=workspaces.c.generation + 1,
            )
        )
        await db.log_event(
            "workspace.rebound",
            project_id=ws["project_id"],
            payload=json.dumps(
                {"workspace_id": workspace_id, "task_id": task_id, "session_id": session_id}
            ),
            conn=conn,
        )
        return {
            "success": True,
            "workspace_id": workspace_id,
            "task_id": task_id,
            "session_id": session_id,
        }


async def release_stopped_idle_workspace(orch, observed):
    """Repair agent-only locks after an exact stopped instance is proven quiescent."""
    from src.sessions.proctable import workspace_quiescent
    from src.sessions.provider import SessionHandle

    if (
        observed.lifecycle != "pool"
        or observed.state != "stopped"
        or observed.desired_state != "stopped"
        or observed.task_id
        or observed.claim_phase
        or not observed.agent_id
    ):
        return False
    ws = await orch.db.get_workspace_for_agent(observed.agent_id)
    if ws is None or ws.locked_by_task_id:
        return False
    provider = orch.session_providers.create(observed.provider, orch.config)
    handle = SessionHandle(observed.name, observed.provider, observed.instance_token)
    if await provider.confirm_stopped(handle) is not True:
        return False
    if await workspace_quiescent((ws.workspace_path,), observed.instance_token) is not True:
        return False
    async with orch.db.immediate() as conn:
        session = (
            (
                await conn.execute(
                    select(sessions)
                    .where(
                        sessions.c.id == observed.id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        agent = (
            (
                await conn.execute(
                    select(agents)
                    .where(
                        agents.c.id == observed.agent_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        slot = (
            (
                await conn.execute(
                    select(workspaces)
                    .where(
                        workspaces.c.id == ws.id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            session is None
            or agent is None
            or slot is None
            or session["instance_token"] != observed.instance_token
            or session["state"] != "stopped"
            or session["desired_state"] != "stopped"
            or session["task_id"]
            or session["claim_phase"]
            or session["last_claim_epoch"] != observed.last_claim_epoch
            or agent["current_task_id"]
            or agent["state"] == "BUSY"
            or slot["generation"] != ws.generation
            or slot["locked_by_task_id"]
            or slot["locked_by_agent_id"] != observed.agent_id
            or slot["workspace_path"] != observed.work_dir
            or await ownership_blockers(conn, slot)
        ):
            return False
        await execute_workspace_release(
            conn,
            update(workspaces)
            .where(
                workspaces.c.id == ws.id,
            )
            .values(
                locked_by_agent_id=None,
                locked_at=None,
                lock_mode=None,
                generation=workspaces.c.generation + 1,
            ),
        )
        return True


async def stopped_idle_workspace_sessions(db):
    """Bound recovery by held checkout inventory rather than all session history."""
    from src.database.queries.session_queries import _row_to_session

    async with db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(sessions)
                    .join(
                        workspaces,
                        and_(
                            workspaces.c.locked_by_agent_id == sessions.c.agent_id,
                            workspaces.c.workspace_path == sessions.c.work_dir,
                        ),
                    )
                    .where(
                        sessions.c.lifecycle == "pool",
                        sessions.c.state == "stopped",
                        sessions.c.desired_state == "stopped",
                        sessions.c.task_id.is_(None),
                        sessions.c.claim_phase.is_(None),
                        workspaces.c.locked_by_task_id.is_(None),
                    )
                    .distinct(workspaces.c.id)
                    .order_by(workspaces.c.id, sessions.c.started_at.desc())
                )
            )
            .mappings()
            .all()
        )
    return [_row_to_session(row) for row in rows]


async def workspace_capacity_holds(db, project_id):
    """Describe physical holds, including hibernation and stopped agent-only locks."""
    inventory = await db.list_workspaces(project_id)
    protected = await db.protected_workspace_ids(inventory)
    findings = []
    async with db._engine.connect() as conn:
        for ws in inventory:
            if not ws.enabled or not (
                ws.locked_by_task_id or ws.locked_by_agent_id or ws.id in protected
            ):
                continue
            row = (
                (
                    await conn.execute(
                        select(workspaces).where(
                            workspaces.c.id == ws.id,
                        )
                    )
                )
                .mappings()
                .one()
            )
            blockers = await ownership_blockers(conn, row)
            stopped_holder = next((
                blocker for blocker in blockers
                if blocker["kind"] == "session" and blocker["state"] == "stopped"
            ), None)
            session = (
                (
                    await conn.execute(
                        select(sessions.c.id, sessions.c.state)
                        .where(
                            sessions.c.id == stopped_holder["session_id"]
                            if stopped_holder else sessions.c.agent_id == ws.locked_by_agent_id,
                            sessions.c.work_dir == ws.workspace_path,
                        )
                        .order_by(sessions.c.started_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            status = (
                await conn.scalar(
                    select(tasks.c.status).where(
                        tasks.c.id == ws.locked_by_task_id,
                    )
                )
                if ws.locked_by_task_id
                else None
            )
            findings.append(
                {
                    "workspace_id": ws.id,
                    "task_id": ws.locked_by_task_id,
                    "task_status": status,
                    "agent_id": ws.locked_by_agent_id,
                    "session": dict(session) if session else None,
                    "reason": "hibernated_checkout"
                    if status in {"HIBERNATED", "RESUME_PENDING"}
                    else "stopped_session_retains_slot"
                    if stopped_holder
                    else "stopped_agent_lock"
                    if session and session["state"] == "stopped" and not ws.locked_by_task_id
                    else "retained_ownership",
                    "blockers": blockers,
                    **({"control": {
                        "command": "stop_task",
                        "params": {"task_id": stopped_holder["task_id"]},
                    }} if stopped_holder else {}),
                }
            )
    return findings
