"""task_workspace_requirements CRUD. See spec §3.3."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, insert, select

from src.database.tables import task_workspace_requirements


@dataclass
class TaskRequirementRow:
    task_id: str
    kind_id: str
    position: int
    alias: str | None


class TaskRequirementsQueryMixin:
    """Query mixin for ``task_workspace_requirements``. Expects ``self._engine``."""

    async def add_task_workspace_requirements(
        self,
        task_id: str,
        requirements: list[tuple[str, str | None]],
    ) -> None:
        """Insert rows; assigns ``position = MAX(position) + 1`` per
        ``(task_id, kind_id)``.

        ``requirements`` is a list of ``(kind_id, alias)`` tuples; pass
        ``alias=None`` for unaliased requirements.  Existing rows are not
        touched — call :meth:`delete_task_workspace_requirements` first to
        replace.
        """
        if not requirements:
            return
        async with self._engine.begin() as conn:
            existing = (
                await conn.execute(
                    select(
                        task_workspace_requirements.c.kind_id,
                        func.max(task_workspace_requirements.c.position),
                    )
                    .where(task_workspace_requirements.c.task_id == task_id)
                    .group_by(task_workspace_requirements.c.kind_id)
                )
            ).fetchall()
            counts: dict[str, int] = {kind: (max_pos or 0) + 1 for kind, max_pos in existing}

            rows = []
            for kind_id, alias in requirements:
                pos = counts.get(kind_id, 0)
                counts[kind_id] = pos + 1
                rows.append(
                    {
                        "task_id": task_id,
                        "kind_id": kind_id,
                        "position": pos,
                        "alias": alias,
                    }
                )
            await conn.execute(insert(task_workspace_requirements), rows)

    async def fetch_task_workspace_requirements(
        self, task_id: str
    ) -> list[TaskRequirementRow]:
        """Return all rows for a task, ordered by ``(kind_id, position)``."""
        async with self._engine.begin() as conn:
            result = (
                await conn.execute(
                    select(task_workspace_requirements)
                    .where(task_workspace_requirements.c.task_id == task_id)
                    .order_by(
                        task_workspace_requirements.c.kind_id,
                        task_workspace_requirements.c.position,
                    )
                )
            ).mappings().fetchall()
        return [
            TaskRequirementRow(
                task_id=r["task_id"],
                kind_id=r["kind_id"],
                position=r["position"],
                alias=r["alias"],
            )
            for r in result
        ]

    async def delete_task_workspace_requirements(self, task_id: str) -> None:
        """Remove every row for a task. Used on task delete or replace."""
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(task_workspace_requirements).where(
                    task_workspace_requirements.c.task_id == task_id
                )
            )
