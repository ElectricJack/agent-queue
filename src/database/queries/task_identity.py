"""Identity-only task lookup across active and archived task storage."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from src.database.tables import archived_tasks, tasks


@dataclass(frozen=True)
class TaskIdentity:
    task_id: str
    project_id: str
    repo_id: str | None
    branch_name: str | None
    parent_task_id: str | None
    status: str
    archived: bool


async def resolve_task_identity_on(conn, task_id: str) -> TaskIdentity | None:
    """Resolve a live task first, then its archived identity, without locking."""
    for table, archived in ((tasks, False), (archived_tasks, True)):
        row = (
            await conn.execute(
                select(
                    table.c.id,
                    table.c.project_id,
                    table.c.repo_id,
                    table.c.branch_name,
                    table.c.parent_task_id,
                    table.c.status,
                ).where(table.c.id == task_id)
            )
        ).mappings().one_or_none()
        if row is not None:
            return TaskIdentity(
                task_id=row["id"],
                project_id=row["project_id"],
                repo_id=row["repo_id"],
                branch_name=row["branch_name"],
                parent_task_id=row["parent_task_id"],
                status=row["status"],
                archived=archived,
            )
    return None
