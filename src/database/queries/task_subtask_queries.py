"""Durable, non-schedulable checklist rows a single agent ticks off inside one task.

A subtask is never scheduled, claimed or assigned on its own — unlike a
child task in the hierarchy, it is pure bookkeeping local to whichever agent
owns the parent task. See ``task_subtasks`` in ``src/database/tables.py``.
"""

from __future__ import annotations

import time

from sqlalchemy import case, func, select, update

from src.database.tables import task_subtasks

#: Row states a subtask can be in. ``skip_open_task_subtasks`` and the
#: "settled" half of ``count_task_subtasks`` both key off this split.
SUBTASK_STATUSES = ("pending", "in_progress", "done", "skipped")
OPEN_SUBTASK_STATUSES = ("pending", "in_progress")
SETTLED_SUBTASK_STATUSES = ("done", "skipped")

MAX_SUBTASKS_PER_TASK = 200

_LIST_COLUMNS = (
    task_subtasks.c.id,
    task_subtasks.c.task_id,
    task_subtasks.c.project_id,
    task_subtasks.c.ordinal,
    task_subtasks.c.title,
    task_subtasks.c.status,
    task_subtasks.c.note,
    task_subtasks.c.created_at,
    task_subtasks.c.updated_at,
)


def _subtask_id(task_id: str, ordinal: int) -> str:
    return f"{task_id}#s{ordinal}"


class TaskSubtaskQueriesMixin:
    """Query mixin for ``task_subtasks``. Expects ``self._engine``."""

    async def add_task_subtasks(
        self, task_id: str, project_id: str, items: list[dict]
    ) -> list[dict]:
        """Append ``items`` after the current max ordinal, in one transaction.

        ``items`` is ``[{"title": str, "context": str = ""}, ...]``. Raises
        ``ValueError("subtask_limit")`` if the append would exceed
        ``MAX_SUBTASKS_PER_TASK``. Returns the inserted rows, ordinal-ordered.
        """
        if not items:
            return []
        now = time.time()
        async with self._engine.begin() as conn:
            current_max = (
                await conn.execute(
                    select(func.max(task_subtasks.c.ordinal)).where(
                        task_subtasks.c.task_id == task_id
                    )
                )
            ).scalar()
            base = current_max or 0
            if base + len(items) > MAX_SUBTASKS_PER_TASK:
                raise ValueError("subtask_limit")
            rows = []
            for offset, item in enumerate(items, start=1):
                ordinal = base + offset
                rows.append(
                    {
                        "id": _subtask_id(task_id, ordinal),
                        "task_id": task_id,
                        "project_id": project_id,
                        "ordinal": ordinal,
                        "title": item["title"],
                        "context": item.get("context", ""),
                        "status": "pending",
                        "note": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            await conn.execute(task_subtasks.insert(), rows)
            return rows

    async def list_task_subtasks(self, task_id: str) -> list[dict]:
        """Ordinal-ordered subtasks for ``task_id``, without ``context``."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(*_LIST_COLUMNS)
                .where(task_subtasks.c.task_id == task_id)
                .order_by(task_subtasks.c.ordinal)
            )
            return [dict(row) for row in result.mappings()]

    async def get_task_subtask(self, task_id: str, ordinal: int) -> dict | None:
        """One subtask, including ``context``, or ``None`` if absent."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(*_LIST_COLUMNS, task_subtasks.c.context).where(
                    task_subtasks.c.task_id == task_id,
                    task_subtasks.c.ordinal == ordinal,
                )
            )
            row = result.mappings().first()
            return dict(row) if row else None

    async def update_task_subtask(
        self,
        task_id: str,
        ordinal: int,
        *,
        status: str | None = None,
        note: str | None = None,
    ) -> dict | None:
        """Set ``status``/``note`` on one subtask and bump ``updated_at``.

        Returns the updated row (including ``context``), or ``None`` if the
        subtask does not exist. Raises ``ValueError`` for an invalid status.
        """
        if status is not None and status not in SUBTASK_STATUSES:
            raise ValueError(f"status must be one of {', '.join(SUBTASK_STATUSES)}")
        values = {"updated_at": time.time()}
        if status is not None:
            values["status"] = status
        if note is not None:
            values["note"] = note
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(task_subtasks)
                .where(task_subtasks.c.task_id == task_id, task_subtasks.c.ordinal == ordinal)
                .values(**values)
            )
            if result.rowcount != 1:
                return None
            row = (
                await conn.execute(
                    select(*_LIST_COLUMNS, task_subtasks.c.context).where(
                        task_subtasks.c.task_id == task_id,
                        task_subtasks.c.ordinal == ordinal,
                    )
                )
            ).mappings().first()
            return dict(row) if row else None

    async def skip_open_task_subtasks(self, task_id: str, note: str) -> int:
        """Flip every open (``pending``/``in_progress``) subtask to ``skipped``.

        *note* is a default, not an overwrite: a row that already carries a
        note keeps it. The worker's own note is the evidence of why the item
        was left; the blanket close note says nothing that the ``skipped``
        status does not already say.

        Returns the number of rows flipped.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(task_subtasks)
                .where(
                    task_subtasks.c.task_id == task_id,
                    task_subtasks.c.status.in_(OPEN_SUBTASK_STATUSES),
                )
                .values(
                    status="skipped",
                    note=case(
                        (func.coalesce(task_subtasks.c.note, "") == "", note),
                        else_=task_subtasks.c.note,
                    ),
                    updated_at=time.time(),
                )
            )
            return result.rowcount

    async def count_task_subtasks(self, task_ids: list[str]) -> dict[str, tuple[int, int]]:
        """``{task_id: (total, settled)}`` via one ``GROUP BY``.

        ``settled`` is ``done`` + ``skipped``. Task ids with no subtasks are
        absent from the result.
        """
        if not task_ids:
            return {}
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(
                    task_subtasks.c.task_id,
                    func.count().label("total"),
                    func.sum(
                        case((task_subtasks.c.status.in_(SETTLED_SUBTASK_STATUSES), 1), else_=0)
                    ).label("settled"),
                )
                .where(task_subtasks.c.task_id.in_(task_ids))
                .group_by(task_subtasks.c.task_id)
            )
            return {row.task_id: (row.total, int(row.settled)) for row in result}
