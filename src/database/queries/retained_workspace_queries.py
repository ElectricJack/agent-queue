"""Exact identity and atomic release for an explicit stop after a status change."""
from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import insert, or_, select, update

from src.database.tables import (
    agents, integration_branch_owners, job_workspace_pins, repos, sessions,
    task_context, task_integration_checkpoints, task_metadata, tasks, workspaces,
)
from src.git.process import PREPARE_CLEANUP_HOLD

STOP_META = "retained_workspace_stop"


class RetainedWorkspaceError(ValueError):
    """A missing proof retains the source, claim and owner for supported recovery."""


async def _meta(conn, task_id, key):
    value = await conn.scalar(select(task_metadata.c.value).where(
        task_metadata.c.task_id == task_id, task_metadata.c.key == key,
    ))
    return json.loads(value) if value is not None else None


async def _one(conn, table, condition):
    row = (await conn.execute(select(table).where(condition).with_for_update())).mappings().one_or_none()
    return dict(row) if row is not None else None


async def snapshot(db, conn, task_id):
    """Lock all release identities; a task status alone never establishes ownership."""
    task = await _one(conn, tasks, tasks.c.id == task_id)
    if task is None or task["status"] not in {"BLOCKED", "FAILED"}:
        raise RetainedWorkspaceError("Retained stop requires the unchanged BLOCKED or FAILED task")
    if await _meta(conn, task_id, PREPARE_CLEANUP_HOLD) is not None:
        raise RetainedWorkspaceError("Preparation child termination is unproved; retain its recovery hold")
    if await _meta(conn, task_id, "manual_pause") is not None:
        raise RetainedWorkspaceError("Manual pause owns cleanup; repeat the supported task pause")
    recovery_owner = await db._recovery_owner(conn, task)
    if recovery_owner["kind"] != "supervisor":
        raise RetainedWorkspaceError(
            f"Integration operation {recovery_owner.get('operation_id')} owns this cleanup"
        )
    if await conn.scalar(select(task_integration_checkpoints.c.task_id).where(
        task_integration_checkpoints.c.task_id == task_id,
        task_integration_checkpoints.c.episode_id.is_not(None),
    )):
        raise RetainedWorkspaceError("Integration episode owns this cleanup")
    rows = (await conn.execute(select(workspaces).where(
        workspaces.c.locked_by_task_id == task_id,
    ).order_by(workspaces.c.id).with_for_update())).mappings().all()
    if len(rows) != 1:
        raise RetainedWorkspaceError("Stop needs exactly one retained registered workspace")
    ws = dict(rows[0])
    if ws["job_pin_count"] or await conn.scalar(select(job_workspace_pins.c.job_id).where(
        job_workspace_pins.c.workspace_id == ws["id"],
    ).limit(1)):
        raise RetainedWorkspaceError("A job pins this workspace; let its existing owner settle it")
    receipt = await _meta(conn, task_id, "claim_workspace")
    session = await _one(conn, sessions, sessions.c.id == (receipt or {}).get("session_id"))
    if session is None or not session["instance_token"] or receipt != {
        "workspace_id": ws["id"], "generation": ws["generation"],
        "session_id": session["id"], "instance_token": session["instance_token"],
        "claim_epoch": task["claim_epoch"],
    }:
        raise RetainedWorkspaceError("Exact claim acquisition receipt is missing or changed")
    agent = await _one(conn, agents, agents.c.id == session["agent_id"])
    if (
        session["task_id"] != task_id or session["last_claim_epoch"] != task["claim_epoch"]
        or session["claim_phase"] != "active" or session["work_dir"] != ws["workspace_path"]
        or ws["project_id"] != task["project_id"] or session["project_id"] != task["project_id"]
        or ws["locked_by_agent_id"] != session["agent_id"]
        or task["assigned_agent_id"] not in (None, session["agent_id"])
        or agent is None or agent["current_task_id"] != task_id
    ):
        raise RetainedWorkspaceError("Task, session or workspace binding changed or is still preparing")
    competing = await conn.scalar(select(sessions.c.id).where(
        sessions.c.id != session["id"],
        or_(sessions.c.task_id == task_id,
            sessions.c.agent_id == session["agent_id"],
            sessions.c.work_dir == ws["workspace_path"]),
        or_(sessions.c.started_at >= session["started_at"],
            sessions.c.state.not_in(("stopped", "quarantined"))),
    ).limit(1))
    if competing:
        raise RetainedWorkspaceError("A successor or another session may own the retained source")
    base = await _one(conn, workspaces, workspaces.c.id == ws["base_workspace_id"])
    repository = await _one(conn, repos, repos.c.id == task["repo_id"])
    if (not ws["enabled"] or ws["slot_index"] is None or base is None
            or base["project_id"] != task["project_id"] or repository is None
            or repository["project_id"] != task["project_id"] or not task["branch_name"]):
        raise RetainedWorkspaceError("Managed slot, base and source repository identity are required")
    owners = [dict(row) for row in (await conn.execute(select(integration_branch_owners).where(
        or_(integration_branch_owners.c.owner_id == task_id,
            integration_branch_owners.c.workspace_id == ws["id"]),
    ).order_by(integration_branch_owners.c.id).with_for_update())).mappings().all()]
    for owner in owners:
        if (
            owner["fence"] is not None or owner["owner_id"] != task_id
            or owner["owner_role"] not in {"worker", "repair"}
            or owner["repository_id"] != repository["id"]
            or owner["ref"].removeprefix("refs/heads/")
            != task["branch_name"].removeprefix("refs/heads/")
            or owner["session_id"] != session["id"] or owner["workspace_id"] != ws["id"]
            or owner["handoff_state"] not in {"attached", "handoff_pending"}
        ):
            raise RetainedWorkspaceError("Integration branch owner needs its own fenced handoff")
    # Exclude mutable telemetry; keep every ownership and acquisition field.
    task_keys = ("id", "status", "project_id", "repo_id", "branch_name", "assigned_agent_id",
                 "claim_epoch", "retry_count", "resume_after")
    session_keys = ("id", "instance_token", "agent_id", "task_id", "project_id", "provider",
                    "name", "work_dir", "started_at", "last_claim_epoch", "claim_phase", "lifecycle")
    base_keys = ("id", "project_id", "workspace_path", "source_type", "kind_id", "generation")
    return {
        "task": {k: task[k] for k in task_keys}, "workspace": ws,
        "session": {k: session[k] for k in session_keys},
        "agent": {k: agent[k] for k in ("id", "current_task_id", "state")},
        "base": {k: base[k] for k in base_keys},
        "repository": {k: repository[k] for k in ("id", "project_id", "url", "default_branch")},
        "owners": owners, "claim": receipt,
    }


async def begin(db, task_id):
    async with db.immediate() as conn:
        task = await _one(conn, tasks, tasks.c.id == task_id)
        saved = await _meta(conn, task_id, STOP_META)
        if saved is not None and saved.get("cleanup_pending") is False:
            previous = saved["identity"]["task"]
            if task is None or any(task[key] != previous[key] for key in (
                "claim_epoch", "status", "project_id", "repo_id", "branch_name",
            )) or await conn.scalar(select(workspaces.c.id).where(
                workspaces.c.locked_by_task_id == task_id,
            ).limit(1)):
                raise RetainedWorkspaceError("Completed stop cannot adopt a successor")
            return saved
        current = await snapshot(db, conn, task_id)
        if saved is not None and saved.get("identity") != current:
            raise RetainedWorkspaceError("Retained stop identity changed; do not adopt a successor")
        if saved is None:
            saved = {"identity": current, "cleanup_pending": True}
            await db._upsert_meta(task_id, STOP_META, saved, conn=conn)
        return saved


async def validate(db, conn, task_id, saved):
    if (await _meta(conn, task_id, STOP_META) != saved
            or await snapshot(db, conn, task_id) != saved["identity"]):
        raise RetainedWorkspaceError("Retained stop identity changed; resources remain held")
    if saved.get("claim_retirement_pending"):
        await validate_checkpoint(conn, task_id, saved)


async def validate_checkpoint(conn, task_id, saved):
    checkpoint = await _meta(conn, task_id, "manual_pause_checkpoint")
    if not checkpoint or checkpoint.get("retained_stop_identity") != saved["identity"]:
        raise RetainedWorkspaceError("A full, identity-bound checkpoint is required for release")


async def prepare_claim_retirement(db, conn, task_id, saved):
    """Commit preservation and exact unlink intent while all reservations remain held."""
    await validate(db, conn, task_id, saved)
    await validate_checkpoint(conn, task_id, saved)
    saved = saved | {"claim_retirement_pending": True}
    await db._upsert_meta(task_id, STOP_META, saved, conn=conn)
    return saved


async def save_checkpoint(db, conn, task_id, saved, checkpoint):
    await validate(db, conn, task_id, saved)
    identity = saved["identity"]
    checkpoint = checkpoint | {
        "pause_project_id": identity["task"]["project_id"],
        "retained_stop_identity": identity,
    }
    await db._upsert_meta(task_id, "manual_pause_checkpoint", checkpoint, conn=conn)
    await conn.execute(insert(task_context).values(
        id=str(uuid.uuid4())[:12], task_id=task_id, type="retained_workspace_checkpoint",
        label="Full Git checkpoint before retained workspace stop", content=json.dumps(checkpoint),
        created_at=time.time(),
    ))


async def retain_cleanup_uncertainty(db, task_id, saved):
    """A cancelled Git child never clears a hold or writes into a successor epoch."""
    async with db.immediate() as conn:
        row = await _one(conn, tasks, tasks.c.id == task_id)
        if row is None or row["claim_epoch"] != saved["identity"]["task"]["claim_epoch"]:
            return
        if await _meta(conn, task_id, PREPARE_CLEANUP_HOLD) is not None:
            return
        await db._upsert_meta(task_id, PREPARE_CLEANUP_HOLD, {
            "task_id": task_id, "source": "retained_workspace_stop",
            "identity": saved["identity"],
            "reason": "Checkpoint or detach child termination is unproved",
        }, conn=conn)


async def finish(db, conn, task_id, saved):
    """Native stopped-holder release without replaying the task state transition."""
    await validate(db, conn, task_id, saved)
    identity = saved["identity"]
    await validate_checkpoint(conn, task_id, saved)
    ws, session = identity["workspace"], identity["session"]
    for owner in identity["owners"]:
        await conn.execute(update(integration_branch_owners).where(
            integration_branch_owners.c.id == owner["id"],
            integration_branch_owners.c.fence_token == owner["fence_token"],
        ).values(handoff_state="released", session_id=None, workspace_id=None,
                 confirmed_workspace_id=ws["id"], updated_at=time.time()))
    await conn.execute(update(workspaces).where(workspaces.c.id == ws["id"]).values(
        locked_by_task_id=None, locked_by_agent_id=None, locked_at=None, lock_mode=None,
    ))
    await conn.execute(update(sessions).where(sessions.c.id == session["id"]).values(
        state="stopped", desired_state="stopped", claim_phase=None, claim_phase_at=None,
        task_id=None if session["lifecycle"] == "pool" else task_id,
        last_claim_result="retained_workspace_stopped", end_reason="manually_stopped",
    ))
    await conn.execute(update(agents).where(agents.c.id == session["agent_id"]).values(
        state="IDLE", current_task_id=None,
    ))
    await conn.execute(update(tasks).where(tasks.c.id == task_id).values(assigned_agent_id=None))
    await db._upsert_meta(task_id, STOP_META, saved | {"cleanup_pending": False}, conn=conn)
