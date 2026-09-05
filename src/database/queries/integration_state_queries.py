"""Read projections over durable hierarchical-integration state."""

from __future__ import annotations

from sqlalchemy import select

from src.database.tables import (
    integration_batches,
    integration_repair_operations,
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
