"""Read projections over durable hierarchical-integration state."""

from __future__ import annotations

from sqlalchemy import and_, select

from src.database.tables import (
    integration_batches,
    integration_repair_operations,
    integration_repair_stages,
    task_integration_checkpoints,
)


class IntegrationStateQueriesMixin:
    """Integration-state reads; state mutations stay caller-transaction owned."""

    async def get_integration_checkpoint(self, task_id: str) -> dict | None:
        statement = select(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == task_id
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_integration_batch(self, batch_id: str) -> dict | None:
        statement = select(integration_batches).where(integration_batches.c.id == batch_id)
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_integration_operation(self, operation_id: str) -> dict | None:
        statement = select(integration_repair_operations).where(
            integration_repair_operations.c.id == operation_id
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def get_active_integration_repair_for_task(
        self, repair_task_id: str
    ) -> dict | None:
        """Resolve one repair task's current active, nonterminal operation."""
        statement = (
            select(integration_repair_operations)
            .select_from(
                integration_repair_operations.join(
                    integration_repair_stages,
                    and_(
                        integration_repair_stages.c.operation_id
                        == integration_repair_operations.c.id,
                        integration_repair_stages.c.ordinal
                        == integration_repair_operations.c.active_stage,
                    ),
                )
            )
            .where(integration_repair_stages.c.repair_task_id == repair_task_id)
            .where(integration_repair_stages.c.state == "active")
            .where(
                integration_repair_operations.c.state.in_(
                    ("active", "escalated", "human_required")
                )
            )
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return dict(rows[0]) if len(rows) == 1 else None
