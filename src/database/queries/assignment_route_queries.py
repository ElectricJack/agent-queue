"""Persistence for the current successful assignment-playbook decision."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import task_assignment_routes
from src.models import TaskAssignmentRoute


_FIELDS = (
    "project_id",
    "input_hash",
    "task_updated_at",
    "options_hash",
    "intelligence_class",
    "provider",
    "playbook_id",
    "playbook_version",
    "playbook_run_id",
    "reason",
    "decided_at",
)


class AssignmentRouteQueryMixin:
    async def get_task_assignment_route(
        self, task_id: str, *, conn=None
    ) -> TaskAssignmentRoute | None:
        async def _read(connection):
            result = await connection.execute(
                select(task_assignment_routes).where(task_assignment_routes.c.task_id == task_id)
            )
            row = result.mappings().fetchone()
            return self._row_to_assignment_route(row) if row else None

        if conn is not None:
            return await _read(conn)
        async with self._engine.begin() as connection:
            return await _read(connection)

    async def list_task_assignment_routes(
        self, task_ids: Sequence[str], *, conn=None
    ) -> list[TaskAssignmentRoute]:
        if not task_ids:
            return []

        async def _read(connection):
            result = await connection.execute(
                select(task_assignment_routes)
                .where(task_assignment_routes.c.task_id.in_(list(task_ids)))
                .order_by(task_assignment_routes.c.task_id)
            )
            return [self._row_to_assignment_route(row) for row in result.mappings().fetchall()]

        if conn is not None:
            return await _read(conn)
        async with self._engine.begin() as connection:
            return await _read(connection)

    async def upsert_task_assignment_routes(
        self, routes: Sequence[TaskAssignmentRoute], *, conn
    ) -> None:
        if not routes:
            return
        values = [route.__dict__ for route in routes]
        insert_fn = postgresql_insert if conn.dialect.name == "postgresql" else sqlite_insert
        statement = insert_fn(task_assignment_routes).values(values)
        excluded = statement.excluded
        statement = statement.on_conflict_do_update(
            index_elements=[task_assignment_routes.c.task_id],
            set_={field: getattr(excluded, field) for field in _FIELDS},
        )
        await conn.execute(statement)

    @staticmethod
    def _row_to_assignment_route(row) -> TaskAssignmentRoute:
        return TaskAssignmentRoute(**{column.name: row[column.name] for column in task_assignment_routes.c})
