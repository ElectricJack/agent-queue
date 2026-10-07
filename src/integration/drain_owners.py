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
    integration_candidate_ref_mutations,
    integration_promotion_intents,
    integration_parent_operation_completions,
    integration_release_results,
    integration_repair_operations,
    task_delivery_receipts,
    task_integration_checkpoints,
    tasks,
)


def terminal_reservation_clause(owner=integration_branch_owners, *, allow_cleanup_history=False):
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
            retained_cleanup_reservation_clause(owner) if allow_cleanup_history else False,
            and_(owner.c.owner_role == "verifier", completed_parent),
            and_(owner.c.owner_role.in_(("worker", "repair")), finished_task),
        ),
    )


def retained_cleanup_reservation_clause(owner=integration_branch_owners):
    """Legacy ephemeral cleanup authority retained across a new-train cutover.

    This proves an ended writer, not delivery or completed cleanup. The old
    fence remains intact for cleanup replay. Only rollout checks may omit it;
    owner release must continue to respect the batch's unfinished cleanup.
    """
    batch = integration_batches.alias("retained_cleanup_batch")
    operation = integration_repair_operations.alias("retained_cleanup_operation")
    return and_(
        owner.c.handoff_state == "reserved",
        owner.c.owner_role == "collector",
        owner.c.holder.is_(None),
        owner.c.session_id.is_(None),
        owner.c.workspace_id.is_(None),
        owner.c.ref.startswith("refs/heads/aq/integration/"),
        select(batch.c.id)
        .join(operation, operation.c.batch_id == batch.c.id)
        .where(
            batch.c.repository_id == owner.c.repository_id,
            batch.c.integration_branch == owner.c.ref,
            batch.c.target_ref.is_(None),
            batch.c.lifecycle == "promoted",
            batch.c.final_main_sha.is_not(None),
            batch.c.cleanup_state.in_(("pending", "conflict")),
            operation.c.id == owner.c.owner_id,
            operation.c.target_kind == "batch",
            operation.c.state.in_(("completed", "cancelled")),
            ~select(integration_candidate_ref_mutations.c.id)
            .where(
                integration_candidate_ref_mutations.c.batch_id == batch.c.id,
                integration_candidate_ref_mutations.c.state == "reserved",
            )
            .correlate(batch)
            .exists(),
            ~select(integration_promotion_intents.c.id)
            .where(
                integration_promotion_intents.c.root_batch_id == batch.c.id,
                integration_promotion_intents.c.state.not_in(
                    ("committed", "conflict", "superseded")
                ),
            )
            .correlate(batch)
            .exists(),
        )
        .correlate(owner)
        .exists(),
    )
