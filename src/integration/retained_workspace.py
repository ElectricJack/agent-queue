"""The exact repair successor may acquire its reserved, stopped checkout."""

from sqlalchemy import and_, exists, or_, select

from src.database.tables import (
    integration_branch_owners,
    integration_repair_operations,
    integration_repair_stages,
    sessions,
    tasks,
    workspaces,
)


def retained_repair_reservation(task_id: str, agent_id: str, workspace_id: str):
    """Admission predicate for a task lock installed by repair handoff.

    This grants only acquisition of the reservation by its assigned successor.
    Preparation still verifies the branch/head and attaches the current fence
    before launching a writer; ordinary task locks remain unavailable.
    """
    stage = integration_repair_stages.alias("retained_stage")
    operation = integration_repair_operations.alias("retained_operation")
    owner = integration_branch_owners.alias("retained_owner")
    predecessor = sessions.alias("retained_predecessor")
    previous_task = tasks.alias("retained_previous_task")
    successor = tasks.alias("retained_successor")
    proof = stage.c.retained_handoff
    return exists(
        select(stage.c.operation_id)
        .select_from(
            stage.join(
                operation,
                and_(
                    operation.c.id == stage.c.operation_id,
                    operation.c.active_stage == stage.c.ordinal,
                ),
            )
            .join(successor, successor.c.id == stage.c.repair_task_id)
            .join(
                owner,
                and_(
                    owner.c.owner_id == successor.c.id,
                    owner.c.repository_id == successor.c.repo_id,
                    owner.c.ref == successor.c.branch_name,
                ),
            )
            .join(predecessor, predecessor.c.id == proof["old_session_id"].as_string())
            .join(previous_task, previous_task.c.id == proof["old_task_id"].as_string())
        )
        .where(
            workspaces.c.id == workspace_id,
            workspaces.c.locked_by_task_id == task_id,
            stage.c.retained_workspace_id == workspaces.c.id,
            stage.c.writer_kind == "repair_delegate",
            stage.c.state.in_(("active", "awaiting_completion")),
            operation.c.state.in_(("active", "escalated")),
            successor.c.id == task_id,
            successor.c.assigned_agent_id == agent_id,
            successor.c.status == "IN_PROGRESS",
            successor.c.project_id == workspaces.c.project_id,
            successor.c.preferred_workspace_id == workspaces.c.id,
            owner.c.owner_role == "repair",
            owner.c.handoff_state == "reserved",
            owner.c.session_id.is_(None),
            owner.c.workspace_id.is_(None),
            owner.c.fence_token == proof["new_fence_token"].as_integer(),
            proof["new_task_id"].as_string() == task_id,
            proof["workspace_id"].as_string() == workspaces.c.id,
            predecessor.c.instance_token == proof["instance_token"].as_string(),
            or_(
                predecessor.c.task_id == proof["old_task_id"].as_string(),
                and_(
                    predecessor.c.lifecycle == "pool",
                    predecessor.c.task_id.is_(None),
                    predecessor.c.claim_phase.is_(None),
                    predecessor.c.last_claim_epoch > 0,
                    predecessor.c.last_claim_epoch == previous_task.c.claim_epoch,
                ),
            ),
            predecessor.c.work_dir == workspaces.c.workspace_path,
            predecessor.c.state == "stopped",
            predecessor.c.desired_state == "stopped",
        )
        .correlate(workspaces)
    )
