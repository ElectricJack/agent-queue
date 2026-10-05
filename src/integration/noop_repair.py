"""Exact audited authoring proof for a completed, unchanged repair delegate."""

import json

from sqlalchemy import select

from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
from src.database.tables import (
    agents,
    archived_tasks,
    gates,
    integration_branch_owners,
    integration_outbox,
    integration_repair_stages,
    projects,
    sessions,
    task_completion_records,
    task_gates,
    task_metadata,
    tasks,
    workspaces,
)
from src.git.manager import RemoteRefState
from src.integration.finished_owners import _stop_proof_blocker
from src.integration.repair_lineage import introduced_repair_commits_on


async def stale_parent_claim_on(conn, operation, owner, *, confirm_stopped):
    parent_id = operation["parent_task_id"]
    if not parent_id:
        return None
    parent = (
        (await conn.execute(select(tasks).where(tasks.c.id == parent_id).with_for_update()))
        .mappings()
        .one()
    )
    if parent["status"] == "PAUSED" and parent["assigned_agent_id"] is None:
        return None
    if (
        parent["status"] != "IN_PROGRESS"
        or parent["assigned_agent_id"] is not None
        or owner["owner_id"] not in {operation["id"], parent_id}
        or owner["owner_role"] != "collector"
    ):
        raise ValueError(
            "parent is not an unassigned aggregate under this operation's collector fence"
        )
    holders = (
        (
            await conn.execute(
                select(sessions).where(sessions.c.task_id == parent_id).with_for_update()
            )
        )
        .mappings()
        .all()
    )
    if len(holders) != 1:
        raise ValueError("stale parent leaf claim has no unique holder")
    holder = holders[0]
    if (
        holder["state"] != "stopped"
        or holder["desired_state"] != "stopped"
        or holder["lifecycle"] != "pool"
        or holder["claim_phase"] != "active"
        or not holder["instance_token"]
        or not holder["last_claim_epoch"]
        or holder["last_claim_epoch"] != parent["claim_epoch"]
    ):
        raise ValueError("stale parent holder is live or its exact claim epoch is unavailable")
    if await conn.scalar(
        select(workspaces.c.id)
        .where(
            (workspaces.c.locked_by_task_id == parent_id)
            | (workspaces.c.workspace_path == holder["work_dir"])
            | (
                (workspaces.c.locked_by_agent_id == holder["agent_id"])
                if holder["agent_id"]
                else False
            ),
        )
        .limit(1)
    ):
        raise ValueError("stale parent holder still has a workspace or lock")
    if holder["agent_id"]:
        agent = (
            (
                await conn.execute(
                    select(agents).where(agents.c.id == holder["agent_id"]).with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if agent and agent["current_task_id"] not in {None, parent_id}:
            raise ValueError("stale parent holder's agent has different work")
    children = (
        (
            await conn.execute(
                select(tasks.c.id, tasks.c.status).where(
                    tasks.c.parent_task_id == parent_id,
                )
            )
        )
        .mappings()
        .all()
    )
    if not children or any(
        row["status"] not in {"COMPLETED", "FAILED", "ABANDONED"} for row in children
    ):
        raise ValueError("stale parent claim cannot release before all children are terminal")
    blocker = await _stop_proof_blocker(dict(holder), confirm_stopped)
    if blocker:
        raise ValueError(
            "stale parent holder's exact session instance is not confirmed stopped: " + blocker
        )
    return {
        "task_id": parent_id,
        "claim_epoch": parent["claim_epoch"],
        "session": dict(holder),
        "children": [dict(row) for row in children],
        "step": "release_stale_parent_claim_to_paused_collection",
    }


async def delegate_proof_on(
    conn, db, operation, stage, *, project_id, head_sha, confirm_stopped=None
):
    """Refuse partial audits, stale fences and holds on any delegate in the ladder."""
    if await introduced_repair_commits_on(conn, stage):
        raise ValueError("current repair stage introduced commits")
    task = None
    for table in (tasks, archived_tasks):
        task = (
            (
                await conn.execute(
                    select(table).where(table.c.id == stage["repair_task_id"]).with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if task is not None:
            break
    if (
        task is None
        or task["status"] != "COMPLETED"
        or task["assigned_agent_id"] is not None
        or task["created_by_kind"] != "integration_repair"
        or task["created_by_id"] != operation["id"]
        or task["project_id"] != project_id
        or not isinstance(task["branch_name"], str)
        or not task["branch_name"]
        or not task["repo_id"]
        or (stage["dossier"] or {}).get("branch_sha") != head_sha
    ):
        raise ValueError("current stage lacks its completed exact-head delegate")
    completion = (
        (
            await conn.execute(
                select(task_completion_records)
                .where(task_completion_records.c.task_id == task["id"])
                .order_by(
                    task_completion_records.c.completed_at.desc(),
                    task_completion_records.c.id.desc(),
                )
                .limit(1)
                .with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        completion is None
        or completion["outcome"] != "pass"
        or completion["branch"] != task["branch_name"]
        or json.loads(completion["commits"]) != []
    ):
        raise ValueError("latest repair completion is not an unchanged PASS")
    audits = (
        (
            await conn.execute(
                select(integration_outbox)
                .where(
                    integration_outbox.c.project_id == project_id,
                    integration_outbox.c.event_type == "integration.repair_delegate_closed",
                    integration_outbox.c.payload["operation_id"].as_string() == operation["id"],
                    integration_outbox.c.payload["stage"].as_integer() == stage["ordinal"],
                )
                .with_for_update()
            )
        )
        .mappings()
        .all()
    )
    keys = ("task_id", "session_id", "instance_token", "workspace_id", "fence_token")
    if (
        len(audits) != 1
        or any(
            not isinstance(audits[0]["payload"].get(key), str) or not audits[0]["payload"][key]
            for key in keys[:-1]
        )
        or audits[0]["payload"]["task_id"] != task["id"]
        or type(audits[0]["payload"].get("fence_token")) is not int
        or audits[0]["payload"]["fence_token"] < 1
        or completion["completed_at"] < audits[0]["created_at"]
    ):
        raise ValueError("repair delegate has an incomplete close audit")
    author = {key: audits[0]["payload"][key] for key in keys}
    accepted = await conn.scalar(
        select(task_metadata.c.value)
        .where(
            task_metadata.c.task_id == task["id"],
            task_metadata.c.key == ACCEPTED_CLOSE_KEY,
        )
        .with_for_update()
    )
    if table is tasks:
        accepted = json.loads(accepted) if accepted else {}
        if (
            not isinstance(accepted, dict)
            or accepted.get("completion_id") != completion["id"]
            or accepted.get("session_id") != author["session_id"]
            or type(accepted.get("claim_epoch")) is not int
            or accepted["claim_epoch"] != task["claim_epoch"]
        ):
            raise ValueError("repair lacks its exact accepted-close identity")
    else:
        count = len(
            (
                await conn.execute(
                    select(task_completion_records.c.id).where(
                        task_completion_records.c.task_id == task["id"]
                    )
                )
            ).all()
        )
        if count != 1:
            raise ValueError("archived repair has ambiguous completion history")
    owner = (
        (
            await conn.execute(
                select(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == task["repo_id"],
                    integration_branch_owners.c.ref.in_(
                        (
                            task["branch_name"].removeprefix("refs/heads/"),
                            "refs/heads/" + task["branch_name"].removeprefix("refs/heads/"),
                        )
                    ),
                )
                .with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        owner is None
        or owner["handoff_state"] != "reserved"
        or owner["session_id"]
        or owner["workspace_id"]
        or owner["fence_token"] <= author["fence_token"]
        or owner["owner_role"] not in {"repair", "collector", "verifier"}
        or owner["owner_id"]
        not in {
            task["id"],
            operation["id"],
            operation["parent_task_id"],
            operation["verifier_task_id"],
            operation["batch_id"],
        }
    ):
        raise ValueError("branch lacks a detached successor fence")
    stale_parent = await stale_parent_claim_on(
        conn, operation, owner, confirm_stopped=confirm_stopped
    )
    delegates = (
        (
            await conn.execute(
                select(integration_repair_stages.c.repair_task_id).where(
                    integration_repair_stages.c.operation_id == operation["id"],
                    integration_repair_stages.c.repair_task_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    protected = [
        value
        for value in [*delegates, operation["parent_task_id"], operation["verifier_task_id"]]
        if value
    ]
    project = (
        (await conn.execute(select(projects).where(projects.c.id == project_id).with_for_update()))
        .mappings()
        .one()
    )
    if project["status"] != "ACTIVE" or project["hierarchical_integration_draining"]:
        raise ValueError("project is paused or draining")
    for task_id in protected:
        if await db._read_manual_pause(conn, task_id) is not None:
            raise ValueError("manual pause on " + task_id)
    if await conn.scalar(
        select(gates.c.id)
        .join(task_gates, task_gates.c.gate_id == gates.c.id)
        .where(
            task_gates.c.task_id.in_(protected),
            gates.c.status == "open",
        )
        .limit(1)
    ):
        raise ValueError("open gate on parent, verifier or repair delegate")
    if await conn.scalar(
        select(sessions.c.id)
        .where(
            sessions.c.task_id.in_(protected),
            (sessions.c.state != "stopped") | sessions.c.claim_phase.is_not(None),
            sessions.c.id != (stale_parent["session"]["id"] if stale_parent else ""),
        )
        .limit(1)
    ) or await conn.scalar(
        select(workspaces.c.id)
        .where(
            workspaces.c.locked_by_task_id.in_(protected),
        )
        .limit(1)
    ):
        raise ValueError("a session or workspace still holds a delegate or aggregate")
    return {
        "completion_id": completion["id"],
        "authoring": author,
        "accepted_close": accepted,
        "completion": dict(completion),
        "close_audit": {
            key: audits[0][key]
            for key in ("id", "project_id", "event_type", "payload", "created_at")
        },
        "claim_epoch": task["claim_epoch"],
        "stale_parent_claim": stale_parent,
        "protected_tasks": sorted(set(protected)),
        "ownership": dict(owner),
        "fence_token": owner["fence_token"],
        "repository_id": owner["repository_id"],
        "branch": owner["ref"],
        "owner_id": owner["id"],
    }


async def remote_matches(promotion, proof, head_sha):
    if promotion is None:
        return False
    repository = await promotion._resolve_repository(proof["repository_id"])
    await promotion._ensure_retained_repository(repository)
    if proof["branch"].removeprefix("refs/heads/") == repository.repo.default_branch:
        return False
    remote = await promotion.git.als_remote_ref(
        str(repository.retained_git_dir),
        proof["branch"],
        repository_url=repository.origin_url,
    )
    return remote.state is RemoteRefState.PRESENT and remote.oid == head_sha
