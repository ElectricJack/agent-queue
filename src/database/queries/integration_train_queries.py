"""Caller-transaction-owned projections for atomic integration-train sealing."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from src.database.tables import (
    archived_tasks,
    gates,
    integration_batch_members,
    integration_batches,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    projects,
    repos,
    task_branch_origins,
    task_delivery_receipts,
    task_gates,
    task_integration_checkpoints,
    task_labels,
    tasks,
)
from src.database.queries.blocked_state import obsolete_marker
from src.database.queries.ingest_hold import not_ingest_held
from src.models import TaskStatus


_ACTIVE_BATCH_LIFECYCLES = (
    "sealing",
    "sealed",
    "building",
    "testing",
    "repairing",
    "human_blocked",
    "promoting",
    "cleanup_pending",
)


def legacy_batch_row():
    """Predicate naming the ``integration_batches`` rows the old engine owns.

    A non-null ``target_ref`` is the Git-first train's additive input/intent
    shape (see ``tables.py``); the legacy rows are the ones
    ``uq_integration_batches_active_project`` makes mutually exclusive per
    project. The train alone advances its own rows; historical maintenance
    must not treat them as legacy subjects or hold their members out of the
    current frontier.
    """
    return integration_batches.c.target_ref.is_(None)


def open_target_batches(project_id, repository_id, target_ref):
    """Git-first batches still owning a target, including paused/failed visits.

    Match the train's open-batch definition: only promotion or abort releases
    the target. A member need not be the task whose branch is being written.
    SQL expressions are accepted for correlated hierarchy reads.
    """
    return select(integration_batches).where(
        integration_batches.c.project_id == project_id,
        integration_batches.c.repository_id == repository_id,
        integration_batches.c.target_ref == target_ref,
        integration_batches.c.intent != "aborted",
        integration_batches.c.lifecycle != "promoted",
    ).correlate_except(integration_batches)


def _root_delivery_receipt_conditions(repository_id: str, default_branch):
    """The receipt rules shared by eligibility and dependency satisfaction."""
    return (
        task_delivery_receipts.c.target_task_id.is_(None),
        task_delivery_receipts.c.repository_id == repository_id,
        func.regexp_replace(task_delivery_receipts.c.target_branch, "^refs/heads/", "")
        == func.regexp_replace(default_branch, "^refs/heads/", ""),
        task_delivery_receipts.c.disposition == "code",
    )


class IntegrationTrainQueriesMixin:
    """Exact frontier reads that retain the caller's lock and transaction."""

    async def eligible_root_page_on(
        self,
        conn: AsyncConnection,
        *,
        project_id: str,
        repository_id: str,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("integration train page limit must be positive")

        child = tasks.alias("integration_train_child")
        checkpoint = task_integration_checkpoints.alias("integration_train_checkpoint")
        origin = task_branch_origins.alias("integration_train_origin")
        repository = repos.alias("integration_train_repository")
        completion = integration_parent_operation_completions.alias(
            "integration_train_completion"
        )
        verification = integration_parent_verifications.alias(
            "integration_train_verification"
        )
        operation = integration_repair_operations.alias("integration_train_operation")

        archived_child = archived_tasks.alias("integration_train_archived_child")
        has_children = or_(
            exists(select(child.c.id).where(child.c.parent_task_id == tasks.c.id)),
            exists(select(archived_child.c.id).where(
                archived_child.c.parent_task_id == tasks.c.id,
            )),
        )
        leaf_identity = and_(
            ~has_children,
            checkpoint.c.checkpoint_sha.is_not(None),
            checkpoint.c.episode_id.is_(None),
            checkpoint.c.current_verification_id.is_(None),
        )
        parent_identity = and_(
            has_children,
            checkpoint.c.verified_generation == checkpoint.c.generation,
            checkpoint.c.verified_sha == checkpoint.c.checkpoint_sha,
            checkpoint.c.current_verification_id
            == checkpoint.c.last_completed_verification_id,
            completion.c.operation_id == checkpoint.c.last_completed_operation_id,
            completion.c.verification_id
            == checkpoint.c.last_completed_verification_id,
            completion.c.parent_task_id == tasks.c.id,
            completion.c.episode_id == checkpoint.c.episode_id,
            verification.c.id == completion.c.verification_id,
            verification.c.operation_id == completion.c.operation_id,
            verification.c.parent_task_id == completion.c.parent_task_id,
            verification.c.episode_id == completion.c.episode_id,
            verification.c.generation == checkpoint.c.generation,
            verification.c.head_sha == checkpoint.c.verified_sha,
            operation.c.id == completion.c.operation_id,
            operation.c.target_kind == "parent",
            operation.c.parent_task_id == tasks.c.id,
            operation.c.episode_id == completion.c.episode_id,
            operation.c.state == "completed",
        )
        source_head = case(
            (has_children, checkpoint.c.verified_sha),
            else_=checkpoint.c.checkpoint_sha,
        ).label("source_head")
        source_kind = case((has_children, "parent"), else_="leaf").label(
            "source_kind"
        )

        held = exists(
            select(task_labels.c.task_id).where(
                task_labels.c.task_id == tasks.c.id,
                task_labels.c.label.like("hold:%"),
            )
        )
        unresolved_gate = exists(
            select(task_gates.c.task_id)
            .select_from(task_gates.join(gates, gates.c.id == task_gates.c.gate_id))
            .where(task_gates.c.task_id == tasks.c.id, gates.c.status == "open")
        )
        active_membership = exists(
            select(integration_batch_members.c.task_id)
            .select_from(
                integration_batch_members.join(
                    integration_batches,
                    integration_batches.c.id == integration_batch_members.c.batch_id,
                )
            )
            .where(
                integration_batch_members.c.task_id == tasks.c.id,
                integration_batches.c.lifecycle.in_(_ACTIVE_BATCH_LIFECYCLES),
                legacy_batch_row(),
            )
        )
        delivered_to_root = exists(
            select(task_delivery_receipts.c.id).where(
                task_delivery_receipts.c.source_task_id == tasks.c.id,
                *_root_delivery_receipt_conditions(
                    repository_id, repository.c.default_branch
                ),
            )
        )

        statement = (
            select(
                tasks.c.id.label("task_id"),
                tasks.c.pr_url,
                tasks.c.integration_mode.label("task_integration_mode"),
                checkpoint.c.branch.label("source_branch"),
                projects.c.integration_mode.label("project_integration_mode"),
                repository.c.id.label("repository_id"),
                repository.c.default_branch,
                origin.c.base_sha.label("source_base"),
                source_head,
                source_kind,
                checkpoint.c.generation,
                checkpoint.c.current_verification_id,
            )
            .select_from(
                tasks.join(projects, projects.c.id == tasks.c.project_id)
                .join(
                    repository,
                    and_(
                        repository.c.id == repository_id,
                        repository.c.project_id == tasks.c.project_id,
                    ),
                )
                .join(
                    checkpoint,
                    and_(
                        checkpoint.c.task_id == tasks.c.id,
                        checkpoint.c.repository_id == repository_id,
                    ),
                )
                .join(
                    origin,
                    and_(
                        origin.c.task_id == tasks.c.id,
                        origin.c.repository_id == repository_id,
                        origin.c.retired_at.is_(None),
                    ),
                )
                .outerjoin(
                    completion,
                    completion.c.operation_id == checkpoint.c.last_completed_operation_id,
                )
                .outerjoin(
                    verification,
                    verification.c.id == checkpoint.c.current_verification_id,
                )
                .outerjoin(operation, operation.c.id == completion.c.operation_id)
            )
            .where(
                tasks.c.project_id == project_id,
                tasks.c.parent_task_id.is_(None),
                tasks.c.status == TaskStatus.COMPLETED.value,
                ~obsolete_marker(tasks),
                tasks.c.repo_id == repository_id,
                tasks.c.pr_url.is_not(None),
                func.trim(tasks.c.pr_url) != "",
                ~held,
                # A held ingestion container is never a train root.
                not_ingest_held(),
                ~unresolved_gate,
                ~active_membership,
                ~delivered_to_root,
                or_(leaf_identity, parent_identity),
            )
        )
        if after is not None:
            statement = statement.where(
                or_(
                    tasks.c.id > after[0],
                    and_(tasks.c.id == after[0], source_head > after[1]),
                )
            )
        rows = (
            await conn.execute(statement.order_by(tasks.c.id, source_head).limit(limit))
        ).mappings()
        return [dict(row) for row in rows]
