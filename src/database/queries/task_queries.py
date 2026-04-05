"""Task CRUD and filtering operations."""

from __future__ import annotations

import json
import logging
import time
import uuid

from sqlalchemy import delete, insert, select, update, func, and_

from src.database.tables import (
    task_context,
    task_criteria,
    task_dependencies,
    task_metadata,
    task_results,
    task_tools,
    tasks,
    token_ledger,
)
from src.models import Task, TaskStatus, TaskType, VerificationType
from src.state_machine import is_valid_status_transition

logger = logging.getLogger(__name__)


class TaskQueryMixin:
    """Query mixin for task operations.  Expects ``self._engine``."""

    async def create_task(self, task: Task) -> None:
        """Insert a new task row."""
        now = time.time()
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(tasks).values(
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
                    created_at=now,
                    updated_at=now,
                )
            )

    async def get_task(self, task_id: str) -> Task | None:
        """Fetch a single task by ID."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(tasks).where(tasks.c.id == task_id))
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_task(row)

    async def list_tasks(
        self,
        project_id: str | None = None,
        status: TaskStatus | None = None,
    ) -> list[Task]:
        """List tasks with optional project/status filters."""
        stmt = select(tasks)
        conditions = []
        if project_id:
            conditions.append(tasks.c.project_id == project_id)
        if status:
            conditions.append(tasks.c.status == status.value)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(tasks.c.priority.asc(), tasks.c.created_at.asc())
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    async def list_active_tasks(
        self,
        project_id: str | None = None,
        exclude_statuses: set[TaskStatus] | None = None,
    ) -> list[Task]:
        """List non-terminal tasks, optionally filtered by project."""
        if exclude_statuses is None:
            exclude_statuses = {TaskStatus.COMPLETED}

        conditions = []
        if exclude_statuses:
            conditions.append(tasks.c.status.notin_([s.value for s in exclude_statuses]))
        if project_id:
            conditions.append(tasks.c.project_id == project_id)

        stmt = select(tasks)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(tasks.c.priority.asc(), tasks.c.created_at.asc())
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    async def list_active_tasks_all_projects(self) -> list[Task]:
        """Return all non-completed tasks across every project."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(tasks)
                .where(tasks.c.status != TaskStatus.COMPLETED.value)
                .order_by(
                    tasks.c.project_id.asc(),
                    tasks.c.priority.asc(),
                    tasks.c.created_at.asc(),
                )
            )
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    async def count_tasks_by_status(
        self,
        project_id: str | None = None,
    ) -> dict[str, int]:
        """Return a {status_value: count} mapping for quick summary stats."""
        stmt = select(tasks.c.status, func.count().label("cnt"))
        if project_id:
            stmt = stmt.where(tasks.c.project_id == project_id)
        stmt = stmt.group_by(tasks.c.status)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return {r["status"]: r["cnt"] for r in result.mappings().fetchall()}

    async def update_task(self, task_id: str, **kwargs) -> None:
        """Update arbitrary task fields."""
        values = {}
        for key, value in kwargs.items():
            if isinstance(value, (TaskStatus, VerificationType, TaskType)):
                value = value.value
            values[key] = value
        values["updated_at"] = time.time()
        async with self._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values))

    async def transition_task(
        self,
        task_id: str,
        new_status: TaskStatus,
        *,
        context: str = "",
        **kwargs,
    ) -> None:
        """Update task status with state-machine validation.

        Fetches the current status, checks it against the formal state
        machine, and logs a warning if the transition is not valid.  The
        update is **always applied** regardless of validation outcome
        (logging-only enforcement).
        """
        task = await self.get_task(task_id)
        if task is None:
            logger.warning("transition_task: task '%s' not found, cannot validate", task_id)
            await self.update_task(task_id, status=new_status, **kwargs)
            return

        current_status = task.status

        if current_status == new_status:
            if kwargs:
                await self.update_task(task_id, **kwargs)
            return

        if not is_valid_status_transition(current_status, new_status):
            ctx = f" ({context})" if context else ""
            logger.warning(
                "Invalid task status transition: %s -> %s for task '%s'%s",
                current_status.value,
                new_status.value,
                task_id,
                ctx,
            )

        await self.update_task(task_id, status=new_status, **kwargs)

    async def delete_task(self, task_id: str) -> None:
        """Delete a task and all related child rows."""
        async with self._engine.begin() as conn:
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
            await conn.execute(delete(tasks).where(tasks.c.id == task_id))

    async def get_task_updated_at(self, task_id: str) -> float | None:
        """Return the ``updated_at`` timestamp for a task, or *None*."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(tasks.c.updated_at).where(tasks.c.id == task_id))
            row = result.fetchone()
            return row[0] if row else None

    async def get_task_created_at(self, task_id: str) -> float | None:
        """Return the ``created_at`` timestamp for a task, or *None*."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(tasks.c.created_at).where(tasks.c.id == task_id))
            row = result.fetchone()
            return row[0] if row else None

    async def add_task_context(
        self,
        task_id: str,
        *,
        type: str,
        label: str,
        content: str,
    ) -> str:
        """Insert a task_context row and return its generated ID."""
        ctx_id = str(uuid.uuid4())[:12]
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(task_context).values(
                    id=ctx_id,
                    task_id=task_id,
                    type=type,
                    label=label,
                    content=content,
                )
            )
        return ctx_id

    async def get_task_contexts(self, task_id: str) -> list[dict]:
        """Return all task_context rows for *task_id* as dicts."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(
                    task_context.c.id,
                    task_context.c.task_id,
                    task_context.c.type,
                    task_context.c.label,
                    task_context.c.content,
                ).where(task_context.c.task_id == task_id)
            )
            return [dict(r) for r in result.mappings().fetchall()]

    # ---- task_metadata (key-value store) ----

    async def set_task_meta(self, task_id: str, key: str, value) -> None:
        """Upsert a single metadata key for a task. *value* is JSON-serialised."""
        encoded = json.dumps(value)
        async with self._engine.begin() as conn:
            # Try update first; if no row matched, insert.
            result = await conn.execute(
                update(task_metadata)
                .where(
                    and_(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == key,
                    )
                )
                .values(value=encoded)
            )
            if result.rowcount == 0:
                await conn.execute(
                    insert(task_metadata).values(task_id=task_id, key=key, value=encoded)
                )

    async def get_task_meta(self, task_id: str, key: str):
        """Return a single metadata value (JSON-decoded), or ``None``."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_metadata.c.value).where(
                    and_(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == key,
                    )
                )
            )
            row = result.fetchone()
            return json.loads(row[0]) if row else None

    async def get_all_task_meta(self, task_id: str) -> dict:
        """Return all metadata for a task as ``{key: decoded_value}``."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_metadata.c.key, task_metadata.c.value).where(
                    task_metadata.c.task_id == task_id
                )
            )
            return {r.key: json.loads(r.value) for r in result.fetchall()}

    async def delete_task_meta(self, task_id: str, key: str) -> None:
        """Remove a single metadata key for a task."""
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(task_metadata).where(
                    and_(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == key,
                    )
                )
            )

    async def get_subtasks(self, parent_task_id: str) -> list[Task]:
        """Return all direct children of a task."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(tasks).where(tasks.c.parent_task_id == parent_task_id)
            )
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    async def get_task_tree(self, root_task_id: str) -> dict | None:
        """Return a nested dict representing the full task hierarchy."""
        root = await self.get_task(root_task_id)
        if root is None:
            return None

        async def _build_subtree(task: Task) -> dict:
            children = await self.get_subtasks(task.id)
            child_nodes = []
            for child in children:
                child_nodes.append(await _build_subtree(child))
            return {"task": task, "children": child_nodes}

        return await _build_subtree(root)

    async def get_parent_tasks(self, project_id: str) -> list[Task]:
        """Return top-level tasks for a project (those with no parent)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(tasks)
                .where((tasks.c.project_id == project_id) & (tasks.c.parent_task_id.is_(None)))
                .order_by(tasks.c.priority.asc(), tasks.c.created_at.asc())
            )
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    @staticmethod
    def _row_to_task(row) -> Task:
        """Convert a database row to a Task model."""
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            parent_task_id=row["parent_task_id"],
            repo_id=row["repo_id"],
            title=row["title"],
            description=row["description"],
            priority=row["priority"],
            status=TaskStatus(row["status"]),
            verification_type=VerificationType(row["verification_type"]),
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            assigned_agent_id=row["assigned_agent_id"],
            branch_name=row["branch_name"],
            resume_after=row["resume_after"],
            requires_approval=bool(row["requires_approval"]),
            pr_url=row["pr_url"],
            plan_source=row["plan_source"],
            is_plan_subtask=bool(row["is_plan_subtask"]),
            task_type=TaskType(row["task_type"]) if row["task_type"] else None,
            profile_id=row["profile_id"],
            preferred_workspace_id=row["preferred_workspace_id"],
            attachments=json.loads(row["attachments"]) if row["attachments"] else [],
            auto_approve_plan=bool(row["auto_approve_plan"]),
        )
