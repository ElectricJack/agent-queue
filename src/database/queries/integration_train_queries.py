"""Caller-transaction-owned projections for atomic integration-train sealing."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, case, exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection

from src.database.tables import (
    gates,
    integration_batch_members,
    integration_batches,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    integration_review_evidence,
    projects,
    repos,
    task_branch_origins,
    task_delivery_receipts,
    task_gates,
    task_integration_checkpoints,
    task_labels,
    tasks,
)
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

        has_children = exists(select(child.c.id).where(child.c.parent_task_id == tasks.c.id))
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
            )
        )
        delivered_to_root = exists(
            select(task_delivery_receipts.c.id).where(
                task_delivery_receipts.c.source_task_id == tasks.c.id,
                task_delivery_receipts.c.target_task_id.is_(None),
                task_delivery_receipts.c.repository_id == repository_id,
                task_delivery_receipts.c.target_branch == repository.c.default_branch,
                task_delivery_receipts.c.disposition == "code",
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
                tasks.c.repo_id == repository_id,
                tasks.c.pr_url.is_not(None),
                func.trim(tasks.c.pr_url) != "",
                ~held,
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

    async def latest_exact_reviews_on(
        self,
        conn: AsyncConnection,
        candidates: list[dict[str, Any]],
    ) -> dict[tuple[str, str, str, str, int], dict[str, Any]]:
        keys = {
            (
                row["task_id"],
                row["repository_id"],
                row["source_base"],
                row["source_head"],
                int(row["generation"]),
            )
            for row in candidates
        }
        if not keys:
            return {}
        key_columns = (
            integration_review_evidence.c.source_task_id,
            integration_review_evidence.c.repository_id,
            integration_review_evidence.c.source_base,
            integration_review_evidence.c.reviewed_head_sha,
            integration_review_evidence.c.generation,
        )
        rows = (
            await conn.execute(
                select(integration_review_evidence)
                .where(tuple_(*key_columns).in_(sorted(keys)))
                .order_by(
                    integration_review_evidence.c.created_at.desc(),
                    integration_review_evidence.c.id.desc(),
                )
            )
        ).mappings()
        latest: dict[tuple[str, str, str, str, int], dict[str, Any]] = {}
        for row in rows:
            value = dict(row)
            key = (
                value["source_task_id"],
                value["repository_id"],
                value["source_base"],
                value["reviewed_head_sha"],
                int(value["generation"]),
            )
            latest.setdefault(key, value)
        return latest
