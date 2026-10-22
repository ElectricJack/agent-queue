"""Durable review evidence, promotion intents, and delivery receipts."""

from __future__ import annotations

from typing import Any

from sqlalchemy import insert, select

from src.database.tables import (
    integration_promotion_intents,
    integration_repair_operations,
    integration_review_evidence,
    projects,
    task_branch_origins,
    task_delivery_receipts,
    tasks,
)


class IntegrationDeliveryQueriesMixin:
    """Backend-neutral promotion operations with transactional idempotency."""

    async def append_integration_review_evidence(self, evidence: dict[str, Any]) -> dict:
        required = {
            "id",
            "source_task_id",
            "repository_id",
            "source_base",
            "reviewed_head_sha",
            "reviewed_tree_sha",
            "reviewer_task_id",
            "review_kind",
            "generation",
            "verdict",
            "evidence",
            "created_at",
        }
        missing = required - evidence.keys()
        if missing:
            raise ValueError("review evidence missing: " + ", ".join(sorted(missing)))
        async with self.immediate() as conn:
            source_task_id = evidence["source_task_id"]
            project_id = (
                await conn.execute(
                    select(tasks.c.project_id)
                    .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
                    .where(tasks.c.id == source_task_id)
                )
            ).scalar_one_or_none()
            if project_id is None:
                raise ValueError("review evidence source task project does not exist")
            await self.lock_hierarchy_project(conn, project_id)
            current_project_id = (
                await conn.execute(
                    select(tasks.c.project_id)
                    .select_from(tasks.join(projects, projects.c.id == tasks.c.project_id))
                    .where(tasks.c.id == source_task_id)
                )
            ).scalar_one_or_none()
            if current_project_id != project_id:
                raise ValueError("review evidence source task project does not exist")
            await conn.execute(insert(integration_review_evidence).values(**evidence))
        return dict(evidence)

    async def get_active_integration_verifier_for_task(
        self, verifier_task_id: str
    ) -> dict | None:
        statement = (
            select(integration_repair_operations)
            .where(integration_repair_operations.c.verifier_task_id == verifier_task_id)
            .where(
                integration_repair_operations.c.state.in_(
                    ("active", "escalated", "human_required")
                )
            )
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_integration_verifier_operation(
        self, verifier_task_id: str
    ) -> dict | None:
        """Return the verifier binding even after its parent operation completes.

        A branchless verifier closes its own task only after completing the
        parent operation.  Keeping this lookup independent of operation state
        prevents that final close from falling through to the legacy Git
        integration pipeline.
        """
        statement = (
            select(integration_repair_operations)
            .where(integration_repair_operations.c.verifier_task_id == verifier_task_id)
            .order_by(integration_repair_operations.c.created_at.desc())
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_active_parent_integration_operation(self, parent_task_id: str) -> dict | None:
        statement = (
            select(integration_repair_operations)
            .where(integration_repair_operations.c.parent_task_id == parent_task_id)
            .where(
                integration_repair_operations.c.state.in_(
                    ("active", "escalated", "human_required")
                )
            )
            .order_by(integration_repair_operations.c.created_at.desc())
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_applicable_integration_review_evidence(
        self,
        *,
        source_task_id: str,
        repository_id: str,
        source_base: str,
        reviewed_head_sha: str,
        current_generation: int,
    ) -> dict | None:
        """Return the one current approved exact tuple, never an older approval."""
        statement = (
            select(integration_review_evidence)
            .where(integration_review_evidence.c.source_task_id == source_task_id)
            .where(integration_review_evidence.c.repository_id == repository_id)
            .where(integration_review_evidence.c.source_base == source_base)
            .where(integration_review_evidence.c.reviewed_head_sha == reviewed_head_sha)
            .where(integration_review_evidence.c.generation == current_generation)
            .order_by(
                integration_review_evidence.c.generation.desc(),
                integration_review_evidence.c.created_at.desc(),
                integration_review_evidence.c.id.desc(),
            )
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        if row is None:
            return None
        result = dict(row)
        if result["verdict"] != "approved":
            return None
        return result

    async def get_integration_review_evidence(self, evidence_id: str) -> dict | None:
        statement = select(integration_review_evidence).where(
            integration_review_evidence.c.id == evidence_id
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_task_branch_origin_for_promotion(
        self, task_id: str, repository_id: str
    ) -> dict | None:
        statement = (
            select(task_branch_origins)
            .where(task_branch_origins.c.task_id == task_id)
            .where(task_branch_origins.c.repository_id == repository_id)
            .where(task_branch_origins.c.retired_at.is_(None))
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_integration_promotion_intent(self, intent_id: str) -> dict | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(integration_promotion_intents).where(
                            integration_promotion_intents.c.id == intent_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row is not None else None

    async def list_integration_delivery_receipts(
        self,
        *,
        source_task_id: str,
        repository_id: str,
        target_branch: str,
    ) -> list[dict]:
        statement = (
            select(task_delivery_receipts)
            .where(task_delivery_receipts.c.source_task_id == source_task_id)
            .where(task_delivery_receipts.c.repository_id == repository_id)
            .where(task_delivery_receipts.c.target_branch == target_branch)
            .order_by(task_delivery_receipts.c.created_at, task_delivery_receipts.c.id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]
