"""Bounded read-only selectors for one integration reconciliation tick."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_, select

from src.database.tables import (
    integration_batches,
    integration_candidate_revisions,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    project_integration_schedules,
    projects,
)


def _require_limit(limit: int) -> None:
    if limit <= 0:
        raise ValueError("integration reconciliation page limit must be positive")


class IntegrationReconciliationQueriesMixin:
    """Stable keyset pages; callers own transitions and cursor advancement."""

    async def due_integration_schedule_page(
        self, *, now: float, after: tuple[float, str] | None, limit: int
    ) -> list[dict[str, Any]]:
        _require_limit(limit)
        due_at = project_integration_schedules.c.next_due_at
        project_id = project_integration_schedules.c.project_id
        statement = (
            select(project_integration_schedules)
            .join(projects, projects.c.id == project_id)
            .where(
                project_integration_schedules.c.enabled.is_(True),
                due_at <= now,
                projects.c.status == "ACTIVE",
                projects.c.hierarchical_integration_mode == "train",
            )
            .order_by(due_at, project_id)
            .limit(limit)
        )
        if after is not None:
            statement = statement.where(
                or_(due_at > after[0], and_(due_at == after[0], project_id > after[1]))
            )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def due_integration_repair_stage_page(
        self,
        *,
        now: float,
        after: tuple[float, str, int] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        _require_limit(limit)
        deadline = integration_repair_stages.c.deadline_at
        operation_id = integration_repair_stages.c.operation_id
        ordinal = integration_repair_stages.c.ordinal
        statement = (
            select(
                operation_id,
                ordinal.label("stage"),
                deadline,
                integration_repair_stages.c.deadline_event_id,
            )
            .join(
                integration_repair_operations,
                integration_repair_operations.c.id == operation_id,
            )
            .where(
                integration_repair_operations.c.active_stage == ordinal,
                integration_repair_operations.c.state.in_(("active", "escalated")),
                integration_repair_stages.c.state.in_(("active", "awaiting_completion")),
                deadline.is_not(None),
                deadline <= now,
            )
            .order_by(deadline, operation_id, ordinal)
            .limit(limit)
        )
        if after is not None:
            statement = statement.where(
                or_(
                    deadline > after[0],
                    and_(deadline == after[0], operation_id > after[1]),
                    and_(
                        deadline == after[0],
                        operation_id == after[1],
                        ordinal > after[2],
                    ),
                )
            )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def pending_candidate_ci_page(
        self, *, after: tuple[float, str, int] | None, limit: int
    ) -> list[dict[str, Any]]:
        _require_limit(limit)
        updated_at = integration_candidate_revisions.c.updated_at
        batch_id = integration_candidate_revisions.c.batch_id
        revision = integration_candidate_revisions.c.revision
        statement = (
            select(
                integration_batches.c.project_id,
                integration_batches.c.repository_id,
                batch_id,
                revision,
                integration_candidate_revisions.c.head_sha.label("candidate_sha"),
                integration_repair_operations.c.id.label("operation_id"),
                updated_at,
            )
            .join(integration_batches, integration_batches.c.id == batch_id)
            .join(
                integration_repair_operations,
                integration_repair_operations.c.batch_id == batch_id,
            )
            .where(
                integration_batches.c.current_revision == revision,
                integration_batches.c.lifecycle.in_(("testing", "repairing")),
                integration_candidate_revisions.c.state.in_(("built", "testing")),
                integration_candidate_revisions.c.head_sha.is_not(None),
                integration_repair_operations.c.target_kind == "batch",
                integration_repair_operations.c.state.in_(("active", "escalated")),
            )
            .order_by(updated_at, batch_id, revision)
            .limit(limit)
        )
        if after is not None:
            statement = statement.where(
                or_(
                    updated_at > after[0],
                    and_(updated_at == after[0], batch_id > after[1]),
                    and_(
                        updated_at == after[0],
                        batch_id == after[1],
                        revision > after[2],
                    ),
                )
            )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]

    async def unresolved_integration_intent_page(
        self, *, after: tuple[float, str] | None, limit: int
    ) -> list[dict[str, Any]]:
        _require_limit(limit)
        updated_at = integration_promotion_intents.c.updated_at
        intent_id = integration_promotion_intents.c.id
        statement = (
            select(integration_promotion_intents)
            .where(
                integration_promotion_intents.c.state.not_in(
                    ("committed", "conflict", "superseded")
                )
            )
            .order_by(updated_at, intent_id)
            .limit(limit)
        )
        if after is not None:
            statement = statement.where(
                or_(
                    updated_at > after[0],
                    and_(updated_at == after[0], intent_id > after[1]),
                )
            )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [dict(row) for row in rows]
