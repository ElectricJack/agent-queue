"""Bounded read-only selectors for one integration reconciliation tick."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_, select

from src.database.tables import (
    integration_repair_operations,
    integration_repair_stages,
)


def _require_limit(limit: int) -> None:
    if limit <= 0:
        raise ValueError("integration reconciliation page limit must be positive")


class IntegrationReconciliationQueriesMixin:
    """Stable keyset pages; callers own transitions and cursor advancement."""

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
