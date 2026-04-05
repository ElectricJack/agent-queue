"""Archived task operations."""

from __future__ import annotations

import json
import time

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import (
    agents,
    archived_tasks,
    task_context,
    task_criteria,
    task_dependencies,
    task_results,
    task_tools,
    tasks,
    token_ledger,
    workspaces,
)
from src.models import Task, TaskStatus, TaskType, VerificationType


class ArchiveQueryMixin:
    """Query mixin for archived task operations.  Expects ``self._engine``."""

    async def archive_task(self, task_id: str) -> bool:
        """Move a task from ``tasks`` into ``archived_tasks``.

        Returns *True* if the task was archived, *False* if not found.
        """
        task = await self.get_task(task_id)
        if task is None:
            return False

        now = time.time()
        async with self._engine.begin() as conn:
            # Insert into archive (skip if already archived).
            # on_conflict_do_nothing requires dialect-specific insert.
            _insert = pg_insert if self._engine.dialect.name == "postgresql" else sqlite_insert
            await conn.execute(
                _insert(archived_tasks)
                .on_conflict_do_nothing()
                .values(
                    id=task.id,
                    project_id=task.project_id,
                    parent_task_id=task.parent_task_id,
                    repo_id=task.repo_id,
                    title=task.title,
                    description=task.description,
                    priority=task.priority,
                    status=task.status.value,
                    verification_type=task.verification_type.value,
                    retry_count=task.retry_count,
                    max_retries=task.max_retries,
                    assigned_agent_id=task.assigned_agent_id,
                    branch_name=task.branch_name,
                    resume_after=task.resume_after,
                    requires_approval=int(task.requires_approval),
                    pr_url=task.pr_url,
                    plan_source=task.plan_source,
                    is_plan_subtask=int(task.is_plan_subtask),
                    task_type=task.task_type.value if task.task_type else None,
                    profile_id=task.profile_id,
                    preferred_workspace_id=task.preferred_workspace_id,
                    attachments=json.dumps(task.attachments) if task.attachments else "[]",
                    auto_approve_plan=int(task.auto_approve_plan),
                    created_at=0.0,
                    updated_at=0.0,
                    archived_at=now,
                )
            )

            # Copy original timestamps
            result = await conn.execute(
                select(tasks.c.created_at, tasks.c.updated_at).where(tasks.c.id == task_id)
            )
            row = result.fetchone()
            if row:
                await conn.execute(
                    update(archived_tasks)
                    .where(archived_tasks.c.id == task_id)
                    .values(created_at=row[0], updated_at=row[1])
                )

            # Clean up child rows, then remove from active table
            await conn.execute(delete(task_results).where(task_results.c.task_id == task_id))
            await conn.execute(delete(token_ledger).where(token_ledger.c.task_id == task_id))
            await conn.execute(
                delete(task_dependencies).where(
                    (task_dependencies.c.task_id == task_id)
                    | (task_dependencies.c.depends_on_task_id == task_id)
                )
            )
            await conn.execute(delete(task_criteria).where(task_criteria.c.task_id == task_id))
            await conn.execute(delete(task_context).where(task_context.c.task_id == task_id))
            await conn.execute(delete(task_tools).where(task_tools.c.task_id == task_id))
            await conn.execute(
                update(tasks).where(tasks.c.parent_task_id == task_id).values(parent_task_id=None)
            )
            await conn.execute(
                update(agents)
                .where(agents.c.current_task_id == task_id)
                .values(current_task_id=None)
            )
            await conn.execute(
                update(workspaces)
                .where(workspaces.c.locked_by_task_id == task_id)
                .values(locked_by_task_id=None, locked_at=None)
            )
            await conn.execute(delete(tasks).where(tasks.c.id == task_id))

        return True

    async def archive_completed_tasks(
        self,
        project_id: str | None = None,
    ) -> list[str]:
        """Archive all COMPLETED tasks. Returns list of archived task IDs."""
        stmt = select(tasks.c.id).where(tasks.c.status == TaskStatus.COMPLETED.value)
        if project_id:
            stmt = stmt.where(tasks.c.project_id == project_id)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            task_ids = [r[0] for r in result.fetchall()]

        for tid in task_ids:
            await self.archive_task(tid)

        return task_ids

    async def archive_old_terminal_tasks(
        self,
        statuses: list[str],
        older_than_seconds: float,
    ) -> list[str]:
        """Archive terminal tasks older than the threshold. Returns archived IDs."""
        if not statuses:
            return []

        cutoff = time.time() - older_than_seconds
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(tasks.c.id).where(
                    and_(
                        tasks.c.status.in_(statuses),
                        tasks.c.updated_at <= cutoff,
                    )
                )
            )
            task_ids = [r[0] for r in result.fetchall()]

        for tid in task_ids:
            await self.archive_task(tid)

        return task_ids

    async def list_archived_tasks(
        self,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return archived tasks as dicts, newest archived first."""
        stmt = select(archived_tasks)
        if project_id:
            stmt = stmt.where(archived_tasks.c.project_id == project_id)
        stmt = stmt.order_by(archived_tasks.c.archived_at.desc()).limit(limit)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_archived_task(r) for r in result.mappings().fetchall()]

    async def get_archived_task(self, task_id: str) -> dict | None:
        """Return a single archived task as a dict, or *None* if not found."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(archived_tasks).where(archived_tasks.c.id == task_id)
            )
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_archived_task(row)

    async def restore_archived_task(self, task_id: str) -> bool:
        """Move an archived task back into active ``tasks``. Returns *True* if restored."""
        archived = await self.get_archived_task(task_id)
        if archived is None:
            return False

        task = Task(
            id=archived["id"],
            project_id=archived["project_id"],
            parent_task_id=archived["parent_task_id"],
            repo_id=archived["repo_id"],
            title=archived["title"],
            description=archived["description"],
            priority=archived["priority"],
            status=TaskStatus.DEFINED,
            verification_type=VerificationType(archived["verification_type"]),
            retry_count=0,
            max_retries=archived["max_retries"],
            assigned_agent_id=None,
            branch_name=archived["branch_name"],
            resume_after=None,
            requires_approval=archived["requires_approval"],
            pr_url=archived["pr_url"],
            plan_source=archived["plan_source"],
            is_plan_subtask=archived["is_plan_subtask"],
            task_type=TaskType(archived["task_type"]) if archived["task_type"] else None,
        )
        await self.create_task(task)
        async with self._engine.begin() as conn:
            await conn.execute(delete(archived_tasks).where(archived_tasks.c.id == task_id))
        return True

    async def delete_archived_task(self, task_id: str) -> bool:
        """Permanently delete an archived task. Returns *True* if deleted."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(archived_tasks.c.id).where(archived_tasks.c.id == task_id)
            )
            if not result.fetchone():
                return False
            await conn.execute(delete(archived_tasks).where(archived_tasks.c.id == task_id))
        return True

    async def count_archived_tasks(
        self,
        project_id: str | None = None,
    ) -> int:
        """Return the total count of archived tasks."""
        stmt = select(func.count()).select_from(archived_tasks)
        if project_id:
            stmt = stmt.where(archived_tasks.c.project_id == project_id)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            row = result.fetchone()
            return row[0] if row else 0

    @staticmethod
    def _row_to_archived_task(row) -> dict:
        """Convert a database row from ``archived_tasks`` to a plain dict."""
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "parent_task_id": row["parent_task_id"],
            "repo_id": row["repo_id"],
            "title": row["title"],
            "description": row["description"],
            "priority": row["priority"],
            "status": row["status"],
            "verification_type": row["verification_type"],
            "retry_count": row["retry_count"],
            "max_retries": row["max_retries"],
            "assigned_agent_id": row["assigned_agent_id"],
            "branch_name": row["branch_name"],
            "resume_after": row["resume_after"],
            "requires_approval": bool(row["requires_approval"]),
            "pr_url": row["pr_url"],
            "plan_source": row.get("plan_source"),
            "is_plan_subtask": bool(row.get("is_plan_subtask", 0)),
            "task_type": row.get("task_type"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
        }
