"""Classify detached, terminal train owners for rollout drain checks.

An ownership row can outlive the work it fenced.  Keeping that historical
reservation is useful for replay and recovery, but it cannot keep a project in
train mode after its batch or parent has finished.  Only detached reservations
qualify; attached and pending handoffs still require writer stop proof.
"""

from sqlalchemy import and_, or_, select

from src.database.tables import (
    archived_tasks,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_parent_operation_completions,
    integration_release_results,
    integration_repair_operations,
    task_delivery_receipts,
    task_integration_checkpoints,
    tasks,
)


def terminal_reservation_clause(owner=integration_branch_owners):
    """SQL predicate for an owner whose fenced work has durably finished."""
    detached = and_(
        owner.c.handoff_state == "reserved",
        owner.c.session_id.is_(None),
        owner.c.workspace_id.is_(None),
    )
    released_batch = (
        select(integration_batches.c.id)
        .join(
            integration_release_results,
            integration_release_results.c.batch_id == integration_batches.c.id,
        )
        .where(
            integration_batches.c.repository_id == owner.c.repository_id,
            integration_batches.c.integration_branch == owner.c.ref,
            integration_batches.c.lifecycle == "promoted",
            integration_batches.c.cleanup_state == "complete",
            owner.c.owner_id == integration_release_results.c.operation_id,
        )
        .exists()
    )
    completed_parent = (
        select(integration_parent_operation_completions.c.operation_id)
        .join(
            integration_repair_operations,
            integration_repair_operations.c.id
            == integration_parent_operation_completions.c.operation_id,
        )
        .join(
            task_integration_checkpoints,
            task_integration_checkpoints.c.task_id
            == integration_parent_operation_completions.c.parent_task_id,
        )
        .where(
            integration_repair_operations.c.state == "completed",
            task_integration_checkpoints.c.repository_id == owner.c.repository_id,
            task_integration_checkpoints.c.branch == owner.c.ref,
            or_(
                integration_repair_operations.c.verifier_task_id == owner.c.owner_id,
                integration_repair_operations.c.parent_task_id == owner.c.owner_id,
            ),
        )
        .exists()
    )
    parent = tasks.alias("drain_owner_parent")
    archived_parent = archived_tasks.alias("drain_owner_archived_parent")
    delivered_child = (
        select(task_delivery_receipts.c.id)
        .where(
            task_delivery_receipts.c.source_task_id == owner.c.owner_id,
            task_delivery_receipts.c.repository_id == owner.c.repository_id,
            task_delivery_receipts.c.disposition.in_(("code", "noop")),
        )
        .exists()
    )
    released_member = (
        select(integration_batch_members.c.task_id)
        .join(integration_batches, integration_batches.c.id == integration_batch_members.c.batch_id)
        .join(
            integration_release_results,
            integration_release_results.c.batch_id == integration_batches.c.id,
        )
        .where(
            integration_batch_members.c.task_id == owner.c.owner_id,
            integration_batches.c.repository_id == owner.c.repository_id,
            integration_batches.c.lifecycle == "promoted",
            integration_batches.c.cleanup_state == "complete",
        )
        .exists()
    )
    finished_task = or_(
        select(tasks.c.id)
        .where(
            tasks.c.id == owner.c.owner_id,
            tasks.c.status == "COMPLETED",
            or_(
                delivered_child,
                released_member,
                select(parent.c.id)
                .where(
                    parent.c.id == tasks.c.parent_task_id,
                    parent.c.status == "COMPLETED",
                )
                .exists(),
                select(archived_parent.c.id)
                .where(
                    archived_parent.c.id == tasks.c.parent_task_id,
                    archived_parent.c.status == "COMPLETED",
                )
                .exists(),
            ),
        )
        .exists(),
        select(archived_tasks.c.id)
        .where(
            archived_tasks.c.id == owner.c.owner_id,
            archived_tasks.c.status == "COMPLETED",
            or_(
                delivered_child,
                released_member,
                select(parent.c.id)
                .where(
                    parent.c.id == archived_tasks.c.parent_task_id,
                    parent.c.status == "COMPLETED",
                )
                .exists(),
                select(archived_parent.c.id)
                .where(
                    archived_parent.c.id == archived_tasks.c.parent_task_id,
                    archived_parent.c.status == "COMPLETED",
                )
                .exists(),
            ),
        )
        .exists(),
    )
    return and_(
        detached,
        or_(
            and_(owner.c.owner_role == "collector", released_batch),
            and_(owner.c.owner_role == "verifier", completed_parent),
            and_(owner.c.owner_role.in_(("worker", "repair")), finished_task),
        ),
    )
