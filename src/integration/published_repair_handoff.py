"""Reservation-scoped detach proof for a published pool candidate repair.

Installed only on candidate acceptance, after its exact Git lineage check.
Ordinary owner handoffs never consult this publication exception.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from src.database.tables import (
    agents,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_ref_mutations,
    integration_candidate_resolutions,
    integration_candidate_revisions,
    integration_repair_operations,
    integration_repair_stages,
    project_integration_leases,
    projects,
    sessions,
    tasks,
    workspaces,
)


async def _snapshot_on(db, conn, owner: dict, reservation_id: str, *, now: float) -> dict | None:
    """Read exact authority under hierarchy/row locks; no caller evidence is trusted."""

    async def row(table, *conditions):
        value = (
            (await conn.execute(select(table).where(*conditions).with_for_update()))
            .mappings()
            .one_or_none()
        )
        return dict(value) if value is not None else None

    project_id = await conn.scalar(
        select(integration_candidate_resolutions.c.project_id).where(
            integration_candidate_resolutions.c.id == reservation_id
        )
    )
    if project_id is None:
        return None
    await db.lock_hierarchy_project(conn, project_id)
    reservation = await row(
        integration_candidate_resolutions,
        integration_candidate_resolutions.c.id == reservation_id,
    )
    if (
        reservation is None
        or reservation["project_id"] != project_id
        or reservation["state"] != "pushed"
        or reservation["target_kind"] != "qualified"
        or reservation["target_branch"] != f"refs/heads/aq/integration-repairs/{reservation_id}"
        or reservation["push_evidence"]
        != {
            "remote_sha": reservation["resolved_head_sha"],
            "target_branch": reservation["target_branch"],
        }
        or reservation["handoff_owner_id"] is not None
    ):
        return None
    project = await row(projects, projects.c.id == project_id)
    batch = await row(integration_batches, integration_batches.c.id == reservation["batch_id"])
    lease = await row(
        project_integration_leases, project_integration_leases.c.project_id == project_id
    )
    operation = await row(
        integration_repair_operations,
        integration_repair_operations.c.id == reservation["operation_id"],
    )
    stage = await row(
        integration_repair_stages,
        integration_repair_stages.c.operation_id == reservation["operation_id"],
        integration_repair_stages.c.ordinal == reservation["stage_ordinal"],
    )
    revision = await row(
        integration_candidate_revisions,
        integration_candidate_revisions.c.batch_id == reservation["batch_id"],
        integration_candidate_revisions.c.revision == reservation["revision"],
    )
    member = await row(
        integration_candidate_member_results,
        integration_candidate_member_results.c.batch_id == reservation["batch_id"],
        integration_candidate_member_results.c.revision == reservation["revision"],
        integration_candidate_member_results.c.member_ordinal == reservation["member_ordinal"],
    )
    manifest = [
        dict(value)
        for value in (
            await conn.execute(
                select(integration_batch_members)
                .where(integration_batch_members.c.batch_id == reservation["batch_id"])
                .order_by(integration_batch_members.c.ordinal)
                .with_for_update()
            )
        ).mappings()
    ]
    # Match the pool claim protocol: session before owner/workspace.
    session = await row(sessions, sessions.c.id == reservation["repair_session_id"])
    task = await row(tasks, tasks.c.id == reservation["repair_task_id"])
    current_owner = await row(
        integration_branch_owners, integration_branch_owners.c.id == owner["id"]
    )
    workspace = await row(workspaces, workspaces.c.id == reservation["repair_workspace_id"])
    agent = await row(agents, agents.c.id == session["agent_id"]) if session else None
    mutations = [
        dict(value)
        for value in (
            await conn.execute(
                select(integration_candidate_ref_mutations)
                .where(
                    integration_candidate_ref_mutations.c.resolution_id == reservation_id,
                    integration_candidate_ref_mutations.c.purpose == "repair_resolution",
                )
                .with_for_update()
            )
        ).mappings()
    ]
    if (
        any(
            value is None
            for value in (
                project,
                batch,
                lease,
                operation,
                stage,
                revision,
                member,
                session,
                task,
                current_owner,
                workspace,
                agent,
            )
        )
        or len(mutations) != 1
    ):
        return None
    mutation = mutations[0]
    detail = member["conflict_evidence"] or {}
    subject = stage["current_subject"] or {}
    frozen_member = next(
        (m for m in manifest if m["ordinal"] == reservation["member_ordinal"]), None
    )
    branch = reservation["branch"]
    task_id = reservation["repair_task_id"]
    instance = reservation["repair_session_instance_token"]
    if (
        project["hierarchical_integration_mode"] != "train"
        or project["integration_repository_id"] != reservation["repository_id"]
        or batch["project_id"] != project_id
        or batch["repository_id"] != reservation["repository_id"]
        or batch["integration_branch"] != branch
        or batch["current_revision"] != reservation["revision"]
        or batch["lifecycle"] != "repairing"
        or lease["batch_id"] != batch["id"]
        or lease["repository_id"] != reservation["repository_id"]
        or float(lease["expires_at"]) <= now
        or operation["target_kind"] != "batch"
        or operation["batch_id"] != batch["id"]
        or operation["episode_id"] != reservation["operation_episode_id"]
        or operation["active_stage"] != reservation["stage_ordinal"]
        or operation["state"] not in {"active", "escalated"}
        or stage["repair_task_id"] != task_id
        or stage["writer_kind"] != "repair_delegate"
        or stage["state"] not in {"active", "awaiting_completion"}
        or stage["deadline_at"] != reservation["stage_deadline_at"]
        or float(stage["deadline_at"]) <= now
        or subject.get("kind") != "batch"
        or subject.get("revision") != reservation["revision"]
        or subject.get("candidate_sha")
        not in {
            revision["construction_base_sha"],
            revision["head_sha"],
            reservation["partial_head_sha"],
        }
        or (stage["dossier"] or {}).get("manifest")
        != {
            "kind": "batch",
            "batch_id": batch["id"],
            "source_manifest_digest": batch["source_manifest_digest"],
            "revision": reservation["revision"],
        }
        or revision["next_member_ordinal"] != reservation["member_ordinal"]
        or member["result"] != "conflict"
        or detail.get("batch_id") != batch["id"]
        or detail.get("revision") != reservation["revision"]
        or detail.get("ordinal") != reservation["member_ordinal"]
        or detail.get("operation_id") != reservation["operation_id"]
        or any(
            detail.get(key) != reservation[key]
            for key in (
                "partial_head_sha",
                "source_base_sha",
                "source_head_sha",
            )
        )
        or frozen_member is None
        or frozen_member["repository_id"] != reservation["repository_id"]
        or frozen_member["source_base_sha"] != reservation["source_base_sha"]
        or frozen_member["reviewed_head_sha"] != reservation["source_head_sha"]
        or mutation["state"] != "applied"
        or any(
            mutation[key] != value
            for key, value in {
                "batch_id": batch["id"],
                "revision": reservation["revision"],
                "member_ordinal": reservation["member_ordinal"],
                "repository_id": reservation["repository_id"],
                "branch": branch,
                "target_branch": reservation["target_branch"],
                "expected_old_sha": "0" * 40,
                "desired_sha": reservation["resolved_head_sha"],
                "remote_sha": reservation["resolved_head_sha"],
                "operation_id": reservation["operation_id"],
                "operation_episode_id": reservation["operation_episode_id"],
                "operation_stage": reservation["stage_ordinal"],
                "branch_owner_id": task_id,
                "branch_owner_role": "repair",
                "branch_fence_token": reservation["fence_token"],
            }.items()
        )
        or any(
            current_owner.get(key) != owner.get(key)
            for key in (
                "id",
                "repository_id",
                "ref",
                "owner_id",
                "owner_role",
                "fence_token",
                "session_id",
                "workspace_id",
            )
        )
        or current_owner["repository_id"] != reservation["repository_id"]
        or current_owner["ref"] != branch
        or current_owner["owner_role"] != "repair"
        or current_owner["owner_id"] != task_id
        or reservation["fence_owner_id"] != task_id
        or current_owner["fence_token"] != reservation["fence_token"]
        or current_owner["handoff_state"] != "handoff_pending"
        or current_owner["session_id"] != session["id"]
        or current_owner["workspace_id"] != workspace["id"]
        or session["lifecycle"] != "pool"
        or session["state"] not in {"running", "draining"}
        or session["project_id"] != project_id
        or session["task_id"] != task_id
        or not instance
        or session["instance_token"] != instance
        or session["last_claim_epoch"] != task["claim_epoch"]
        or session["claim_phase"] != "active"
        or session["claim_phase_at"] is None
        or float(reservation["created_at"]) < float(session["claim_phase_at"])
        or task["project_id"] != project_id
        or task["repo_id"] != reservation["repository_id"]
        or str(task["branch_name"] or "").removeprefix("refs/heads/")
        != branch.removeprefix("refs/heads/")
        or task["status"] != "IN_PROGRESS"
        or task["assigned_agent_id"] != agent["id"]
        or agent["state"] != "BUSY"
        or agent["current_task_id"] != task_id
        or workspace["project_id"] != project_id
        or not workspace["enabled"]
        or workspace["locked_by_task_id"] != task_id
        or workspace["locked_by_agent_id"] != agent["id"]
        or session["work_dir"] != workspace["workspace_path"]
        or str(Path(workspace["workspace_path"]).resolve()) != reservation["repair_workspace_path"]
    ):
        return None
    base = None
    if workspace["slot_index"] is not None:
        base = await row(workspaces, workspaces.c.id == workspace["base_workspace_id"])
        if base is None or base["project_id"] != project_id:
            return None
    # Activity timestamps and lease renewal do not change authority.
    return {
        "reservation": reservation,
        "manifest": manifest,
        "revision": revision,
        "member": member,
        "batch": {
            key: batch[key]
            for key in (
                "id",
                "repository_id",
                "integration_branch",
                "current_revision",
                "lifecycle",
                "source_manifest_digest",
                "policy_snapshot",
                "artifact_snapshot",
            )
        },
        "operation": {key: operation[key] for key in ("id", "episode_id", "active_stage", "state")},
        "stage": stage,
        "lease": {key: lease[key] for key in ("owner_id", "fence_token")},
        "session": {
            key: session[key]
            for key in (
                "id",
                "task_id",
                "agent_id",
                "instance_token",
                "work_dir",
                "state",
                "lifecycle",
                "last_claim_epoch",
                "claim_phase",
                "claim_phase_at",
            )
        },
        "workspace": {
            key: workspace[key]
            for key in (
                "id",
                "workspace_path",
                "project_id",
                "locked_by_task_id",
                "locked_by_agent_id",
                "base_workspace_id",
                "slot_index",
                "enabled",
            )
        },
        "mutex_path": base["workspace_path"] if base else workspace["workspace_path"],
    }


async def confirm_published_pool_repair_handoff(
    db,
    git,
    git_mutex,
    owner: dict,
    reservation_id: str,
    *,
    remote_head_reader,
) -> bool:
    """Detach only the exact published reservation and CAS its claim-bound owner."""
    from src.orchestrator.workspace_attachments import mark_integration_pool_handoff_released

    async with db.immediate() as conn:
        snapshot = await _snapshot_on(db, conn, owner, reservation_id, now=time.time())
    if snapshot is None:
        return False
    reservation = snapshot["reservation"]
    workspace = snapshot["workspace"]
    checkout = workspace["workspace_path"]
    branch = reservation["branch"].removeprefix("refs/heads/")
    head = reservation["resolved_head_sha"]
    async with git_mutex(snapshot["mutex_path"]):

        async def exact_checkout():
            current = await git._arun_unlocked(["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout)
            return (
                current in {branch, "HEAD"}
                and not await git._arun_unlocked(["status", "--porcelain"], cwd=checkout)
                and await git._arun_unlocked(["rev-parse", "HEAD"], cwd=checkout) == head
                and await git._arun_unlocked(["rev-parse", "HEAD^{tree}"], cwd=checkout)
                == reservation["resolved_tree_sha"]
            )

        if not await exact_checkout():
            return False
        if (
            await remote_head_reader(reservation["target_branch"].removeprefix("refs/heads/"))
            != head
        ):
            return False
        # Recheck after the remote read: the live worker can edit its checkout.
        if not await exact_checkout():
            return False
        await git._arun_unlocked(["switch", "--detach", head], cwd=checkout)
        if (
            await git._arun_unlocked(["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout) != "HEAD"
            or not await exact_checkout()
        ):
            return False
        async with db.immediate() as conn:
            current = await _snapshot_on(db, conn, owner, reservation_id, now=time.time())
            if current != snapshot or not await exact_checkout():
                return False
            return await mark_integration_pool_handoff_released(
                db,
                owner,
                workspace=SimpleNamespace(id=workspace["id"]),
                task_id=reservation["repair_task_id"],
                session_instance_token=reservation["repair_session_instance_token"],
                conn=conn,
            )
