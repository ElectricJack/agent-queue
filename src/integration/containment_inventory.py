"""Read-only, repository-scoped configuration and writer fences for cutover."""

from __future__ import annotations

from sqlalchemy import and_, or_, select, union

from src.database import tables as t
from src.database.queries.integration_state_queries import (
    session_attached_clause,
    unresolved_claim_clause,
)
from src.integration.cutover import inventory_on
from src.integration.quiesce import _ended_published_resolution_clause

# Apply locks these against insert/update/delete as well as existing-row changes.
# This short final transaction contains no remote I/O. Timeout is a refusal.
FENCED_TABLES = (
    "projects", "repos", "tasks", "archived_tasks", "task_metadata",
    "task_completion_records", "task_branch_origins", "sessions", "workspaces",
    "integration_subjects", "integration_branch_owners", "integration_batches",
    "integration_batch_members", "integration_repair_operations",
    "integration_promotion_intents", "integration_candidate_ref_mutations",
    "integration_candidate_resolutions", "integration_attestation_publications",
    "system_config", "task_integration_checkpoints",
    "integration_parent_episodes", "integration_parent_verifications",
    "integration_parent_operation_completions", "events",
)


async def inventory(conn, project_id, *, now):
    project = (await conn.execute(select(t.projects).where(
        t.projects.c.id == project_id,
    ))).mappings().one_or_none()
    if project is None or not project["integration_repository_id"]:
        raise ValueError("repository_unbound: project needs a designated integration repository")
    repository_id = project["integration_repository_id"]
    repository = (await conn.execute(select(t.repos).where(
        t.repos.c.id == repository_id, t.repos.c.project_id == project_id,
    ))).mappings().one_or_none()
    if repository is None:
        raise ValueError("repository_mismatch: designated repository belongs to another project")

    def scoped(table):
        return and_(table.c.project_id == project_id,
                    or_(table.c.repo_id == repository_id, table.c.repo_id.is_(None)))

    task_ids = select(t.tasks.c.id).where(scoped(t.tasks))
    all_ids = union(task_ids, select(t.archived_tasks.c.id).where(scoped(t.archived_tasks)))
    batch_ids = select(t.integration_batches.c.id).where(
        t.integration_batches.c.project_id == project_id,
        t.integration_batches.c.repository_id == repository_id,
    )
    base_ids = select(t.workspaces.c.id).where(
        t.workspaces.c.project_id == project_id,
        t.workspaces.c.workspace_path == repository["checkout_base_path"],
    )
    names = ("id", "project_id", "repo_id", "parent_task_id", "status", "branch_name",
             "updated_at", "claim_epoch", "legacy_completion_id")
    statements = {
        "tasks": select(*(t.tasks.c[n] for n in names)).where(scoped(t.tasks)),
        "archived": select(*(t.archived_tasks.c[n] for n in names
                             if n in t.archived_tasks.c)).where(scoped(t.archived_tasks)),
        "completions": select(t.task_completion_records).where(
            t.task_completion_records.c.task_id.in_(all_ids)),
        "metadata": select(t.task_metadata).where(t.task_metadata.c.task_id.in_(task_ids)),
        "origins": select(t.task_branch_origins).where(
            t.task_branch_origins.c.repository_id == repository_id,
            t.task_branch_origins.c.task_id.in_(all_ids)),
        "sessions": select(*(t.sessions.c[n] for n in (
            "id", "task_id", "state", "desired_state", "claim_phase", "last_claim_epoch",
            "work_dir", "ended_at",
        ))).where(t.sessions.c.project_id == project_id, or_(
            t.sessions.c.task_id.in_(task_ids), and_(
                t.sessions.c.task_id.is_(None), t.sessions.c.claim_phase.is_not(None),
            )), session_attached_clause()),
        "claims": select(t.task_metadata).where(
            t.task_metadata.c.task_id.in_(task_ids),
            t.task_metadata.c.key == "claimed_by_session", unresolved_claim_clause()),
        "workspaces": select(t.workspaces).where(
            t.workspaces.c.project_id == project_id, or_(
                t.workspaces.c.id.in_(base_ids), t.workspaces.c.base_workspace_id.in_(base_ids),
                t.workspaces.c.locked_by_task_id.in_(task_ids),
            )),
        "repairs": select(t.integration_repair_operations).where(or_(
            t.integration_repair_operations.c.parent_task_id.in_(all_ids),
            t.integration_repair_operations.c.batch_id.in_(batch_ids),
        ), t.integration_repair_operations.c.state.not_in(("completed", "cancelled"))),
        "mutations": select(t.integration_candidate_ref_mutations).where(
            t.integration_candidate_ref_mutations.c.repository_id == repository_id,
            t.integration_candidate_ref_mutations.c.state == "reserved"),
        "resolutions": select(t.integration_candidate_resolutions).where(
            t.integration_candidate_resolutions.c.batch_id.in_(batch_ids),
            t.integration_candidate_resolutions.c.state.in_(("reserved", "pushed")),
            ~_ended_published_resolution_clause()),
        "attestations": select(t.integration_attestation_publications).where(
            t.integration_attestation_publications.c.batch_id.in_(batch_ids),
            t.integration_attestation_publications.c.state == "reserved"),
        "members": select(t.integration_batch_members).where(
            t.integration_batch_members.c.batch_id.in_(batch_ids)),
    }
    rows = await inventory_on(conn, project_id, repository_id, now=now)
    for name, query in statements.items():
        rows[name] = [dict(row) for row in (await conn.execute(query)).mappings()]
    # Configuration only: usage counters change independently of the cutover.
    fields = ("id", "status", "repo_url", "repo_default_branch", "promotion_flow",
              "default_branch_cutover", "integration_mode", "integration_repository_id",
              "hierarchical_integration_mode", "hierarchical_integration_desired_mode",
              "hierarchical_integration_draining", "hierarchical_integration_generation",
              "hierarchical_integration_policy")
    return {"project": {n: project[n] for n in fields}, "repository": dict(repository),
            "inventory": rows}


def writer_blockers(facts):
    project, rows = facts["project"], facts["inventory"]
    blockers = []
    if project["status"] != "PAUSED":
        blockers.append({"code": "claims_not_paused"})
    if (project["hierarchical_integration_mode"] != "disabled"
        or project["hierarchical_integration_desired_mode"] != "disabled"
        or project["hierarchical_integration_draining"]):
        blockers.append({"code": "integration_not_stopped"})
    for family in ("subjects", "owners", "intents", "batches", "sessions", "claims",
                   "repairs", "mutations", "resolutions", "attestations"):
        for row in rows[family]:
            blockers.append({"code": "live_" + family, "id": row.get("id", row.get("task_id"))})
    for row in rows["workspaces"]:
        if row["locked_by_task_id"] or row["locked_by_agent_id"] or row["job_pin_count"]:
            blockers.append({"code": "live_workspace", "id": row["id"]})
    for row in rows["tasks"]:
        if row["status"] in {"ASSIGNED", "IN_PROGRESS"}:
            blockers.append({"code": "active_task", "id": row["id"]})
    return blockers
