"""Caller-transaction-owned persistence helpers for integration schedules."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from src.database.tables import project_integration_schedules


class IntegrationScheduleQueriesMixin:
    """Lock and mutate one schedule without opening an independent connection."""

    async def lock_integration_schedule_on(
        self,
        conn: AsyncConnection,
        *,
        project_id: str,
        now: float,
        default_interval_seconds: int,
    ) -> dict[str, Any]:
        insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
        await conn.execute(
            insert_fn(project_integration_schedules)
            .values(
                project_id=project_id,
                enabled=False,
                interval_seconds=default_interval_seconds,
                next_due_at=now + default_interval_seconds,
                request_sequence=0,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["project_id"])
        )
        row = (
            (
                await conn.execute(
                    select(project_integration_schedules)
                    .where(project_integration_schedules.c.project_id == project_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        return dict(row)

    async def update_integration_schedule_on(
        self,
        conn: AsyncConnection,
        *,
        project_id: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        await conn.execute(
            update(project_integration_schedules)
            .where(project_integration_schedules.c.project_id == project_id)
            .values(**values)
        )
        row = (
            (
                await conn.execute(
                    select(project_integration_schedules).where(
                        project_integration_schedules.c.project_id == project_id
                    )
                )
            )
            .mappings()
            .one()
        )
        return dict(row)
