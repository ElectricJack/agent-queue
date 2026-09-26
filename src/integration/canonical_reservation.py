"""Restore a stopped train producer's canonical branch reservation."""

from __future__ import annotations

from sqlalchemy import select

from src.database.queries.integration_state_queries import session_attached_clause
from src.database.tables import (
    integration_branch_owners,
    projects,
    sessions,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
    workspaces,
)
from src.integration.models import BranchKey
from src.integration.ownership import BranchBusy, BranchOwnership


async def reserve_canonical_task_branch(db, task_id: str) -> dict:
    """Reserve only a detached READY/BLOCKED producer's persisted branch.

    The task, origin and checkpoint must still agree. A project hierarchy lock
    excludes a concurrent claim while the owner row is checked and acquired;
    the ownership table's unique key excludes a competing repository writer.
    Attached rows are left for the provider-backed owner recovery path.
    """
    async with db.immediate() as conn:
        identity = (
            await conn.execute(select(tasks.c.project_id).where(tasks.c.id == task_id))
        ).scalar_one_or_none()
        if identity is None:
            return {"outcome": "not_found", "reason": "task does not exist"}
        await db.lock_hierarchy_project(conn, identity)
        task = (
            (await conn.execute(select(tasks).where(tasks.c.id == task_id).with_for_update()))
            .mappings()
            .one_or_none()
        )
        if task is None or task["project_id"] != identity:
            return {"outcome": "not_found", "reason": "task changed while reserving"}
        project = (
            (await conn.execute(select(projects).where(projects.c.id == identity)))
            .mappings()
            .one_or_none()
        )
        repository_id, branch = task["repo_id"], task["branch_name"]
        if (
            project is None
            or project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
            or project["integration_repository_id"] != repository_id
            or not branch
            or task["status"] not in {"READY", "BLOCKED"}
            or task["created_by_kind"] == "integration_repair"
        ):
            return {"outcome": "not_eligible", "reason": "task is not a detached train producer"}
        checkpoint = (
            (
                await conn.execute(
                    select(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id == task_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        origin = (
            (
                await conn.execute(
                    select(task_branch_origins).where(
                        task_branch_origins.c.task_id == task_id,
                        task_branch_origins.c.repository_id == repository_id,
                        task_branch_origins.c.retired_at.is_(None),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            checkpoint is None
            or checkpoint["repository_id"] != repository_id
            or checkpoint["branch"] != branch
            or checkpoint["state"] == "verifying"
            or origin is None
            or origin["branch_name"] != branch
            or not origin["reserved"]
            or not origin["materialized"]
        ):
            return {"outcome": "not_eligible", "reason": "canonical branch evidence disagrees"}
        live_session = (
            await conn.execute(
                select(sessions.c.id)
                .where(sessions.c.task_id == task_id, session_attached_clause())
                .limit(1)
            )
        ).scalar_one_or_none()
        held_workspace = (
            await conn.execute(
                select(workspaces.c.id).where(workspaces.c.locked_by_task_id == task_id).limit(1)
            )
        ).scalar_one_or_none()
        if task["assigned_agent_id"] or live_session or held_workspace:
            return {"outcome": "not_eligible", "reason": "task still has a writer or workspace"}
        # Verifiers and repair delegates can share their parent's branch. A
        # missing owner row must not let this producer override their turn.
        other_task = (
            await conn.execute(
                select(tasks.c.id)
                .where(
                    tasks.c.id != task_id,
                    tasks.c.repo_id == repository_id,
                    tasks.c.branch_name == branch,
                    tasks.c.status.in_(("READY", "ASSIGNED", "IN_PROGRESS", "BLOCKED")),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if other_task is not None:
            return {"outcome": "not_eligible", "reason": "another task uses the canonical branch"}
        refs = {
            branch,
            f"refs/heads/{branch.removeprefix('refs/heads/')}",
            branch.removeprefix("refs/heads/"),
        }
        rows = (
            (
                await conn.execute(
                    select(integration_branch_owners)
                    .where(
                        integration_branch_owners.c.repository_id == repository_id,
                        integration_branch_owners.c.ref.in_(refs),
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if any(row["ref"] != branch and row["handoff_state"] != "released" for row in rows):
            return {"outcome": "not_eligible", "reason": "another spelling of branch has an owner"}
        owner = next((row for row in rows if row["ref"] == branch), None)
        if owner is not None and owner["handoff_state"] != "released":
            if (
                owner["owner_id"] == task_id
                and owner["owner_role"] == "worker"
                and owner["handoff_state"] == "reserved"
                and owner["session_id"] is None
                and owner["workspace_id"] is None
            ):
                return {"outcome": "already_reserved", "task_id": task_id, "branch": branch}
            return {"outcome": "not_eligible", "reason": "branch has another or unresolved owner"}
        try:
            fence = await BranchOwnership(db).acquire(
                BranchKey(repository_id=repository_id, branch=branch), task_id, "worker", conn=conn
            )
        except BranchBusy:
            return {"outcome": "not_eligible", "reason": "branch acquisition raced another owner"}
        return {
            "outcome": "acquired",
            "task_id": task_id,
            "branch": branch,
            "fence_token": fence.token,
        }
