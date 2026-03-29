"""Task CRUD and filtering operations."""

from __future__ import annotations

import json
import logging
import time
import uuid

from src.models import Task, TaskStatus, TaskType, VerificationType
from src.state_machine import is_valid_status_transition

logger = logging.getLogger(__name__)


class TaskQueryMixin:
    """Query mixin for task operations.  Expects ``self._db``."""

    async def create_task(self, task: Task) -> None:
        """Insert a new task row."""
        now = time.time()
        await self._db.execute(
            "INSERT INTO tasks (id, project_id, parent_task_id, repo_id, title, "
            "description, priority, status, verification_type, retry_count, "
            "max_retries, assigned_agent_id, branch_name, resume_after, "
            "requires_approval, pr_url, plan_source, is_plan_subtask, "
            "task_type, profile_id, preferred_workspace_id, attachments, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task.id, task.project_id, task.parent_task_id, task.repo_id,
             task.title, task.description, task.priority, task.status.value,
             task.verification_type.value, task.retry_count, task.max_retries,
             task.assigned_agent_id, task.branch_name, task.resume_after,
             int(task.requires_approval), task.pr_url, task.plan_source,
             int(task.is_plan_subtask),
             task.task_type.value if task.task_type else None,
             task.profile_id,
             task.preferred_workspace_id,
             json.dumps(task.attachments) if task.attachments else "[]",
             now, now),
        )
        await self._db.commit()

    async def get_task(self, task_id: str) -> Task | None:
        """Fetch a single task by ID."""
        cursor = await self._db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    async def list_tasks(
        self,
        project_id: str | None = None,
        status: TaskStatus | None = None,
    ) -> list[Task]:
        """List tasks with optional project/status filters."""
        conditions = []
        vals = []
        if project_id:
            conditions.append("project_id = ?")
            vals.append(project_id)
        if status:
            conditions.append("status = ?")
            vals.append(status.value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT * FROM tasks {where} ORDER BY priority ASC, created_at ASC",
            vals,
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def list_active_tasks(
        self,
        project_id: str | None = None,
        exclude_statuses: set[TaskStatus] | None = None,
    ) -> list[Task]:
        """List non-terminal tasks, optionally filtered by project.

        Unlike :meth:`list_tasks`, this method performs status filtering at the
        SQL level so the database only returns actionable rows.

        Parameters
        ----------
        project_id:
            Optional project filter.
        exclude_statuses:
            Set of :class:`TaskStatus` values to exclude.  Defaults to
            COMPLETED only.
        """
        if exclude_statuses is None:
            exclude_statuses = {TaskStatus.COMPLETED}

        conditions: list[str] = []
        vals: list = []

        if exclude_statuses:
            placeholders = ", ".join("?" for _ in exclude_statuses)
            conditions.append(f"status NOT IN ({placeholders})")
            vals.extend(s.value for s in exclude_statuses)

        if project_id:
            conditions.append("project_id = ?")
            vals.append(project_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT * FROM tasks {where} ORDER BY priority ASC, created_at ASC",
            vals,
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def list_active_tasks_all_projects(self) -> list[Task]:
        """Return all non-completed tasks across every project."""
        terminal = (TaskStatus.COMPLETED.value,)
        placeholders = ", ".join("?" for _ in terminal)
        cursor = await self._db.execute(
            f"SELECT * FROM tasks WHERE status NOT IN ({placeholders}) "
            "ORDER BY project_id ASC, priority ASC, created_at ASC",
            list(terminal),
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def count_tasks_by_status(
        self, project_id: str | None = None,
    ) -> dict[str, int]:
        """Return a {status_value: count} mapping for quick summary stats."""
        conditions: list[str] = []
        vals: list = []
        if project_id:
            conditions.append("project_id = ?")
            vals.append(project_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT status, COUNT(*) as cnt FROM tasks {where} GROUP BY status",
            vals,
        )
        rows = await cursor.fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    async def update_task(self, task_id: str, **kwargs) -> None:
        """Update arbitrary task fields."""
        sets = []
        vals = []
        for key, value in kwargs.items():
            if isinstance(value, (TaskStatus, VerificationType, TaskType)):
                value = value.value
            sets.append(f"{key} = ?")
            vals.append(value)
        sets.append("updated_at = ?")
        vals.append(time.time())
        vals.append(task_id)
        await self._db.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self._db.commit()

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
            logger.warning(
                "transition_task: task '%s' not found, cannot validate", task_id
            )
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
        await self._db.execute("DELETE FROM task_results WHERE task_id = ?", [task_id])
        await self._db.execute("DELETE FROM token_ledger WHERE task_id = ?", [task_id])
        await self._db.execute(
            "DELETE FROM task_dependencies WHERE task_id = ? OR depends_on_task_id = ?",
            [task_id, task_id],
        )
        await self._db.execute("DELETE FROM task_criteria WHERE task_id = ?", [task_id])
        await self._db.execute("DELETE FROM task_context WHERE task_id = ?", [task_id])
        await self._db.execute("DELETE FROM task_tools WHERE task_id = ?", [task_id])
        await self._db.execute("DELETE FROM tasks WHERE id = ?", [task_id])
        await self._db.commit()

    async def get_task_updated_at(self, task_id: str) -> float | None:
        """Return the ``updated_at`` timestamp for a task, or *None*."""
        cursor = await self._db.execute(
            "SELECT updated_at FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return row["updated_at"] if row else None

    async def get_task_created_at(self, task_id: str) -> float | None:
        """Return the ``created_at`` timestamp for a task, or *None*."""
        cursor = await self._db.execute(
            "SELECT created_at FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return row["created_at"] if row else None

    async def add_task_context(
        self, task_id: str, *, type: str, label: str, content: str,
    ) -> str:
        """Insert a task_context row and return its generated ID."""
        ctx_id = str(uuid.uuid4())[:12]
        await self._db.execute(
            "INSERT INTO task_context (id, task_id, type, label, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (ctx_id, task_id, type, label, content),
        )
        await self._db.commit()
        return ctx_id

    async def get_task_contexts(self, task_id: str) -> list[dict]:
        """Return all task_context rows for *task_id* as dicts."""
        cursor = await self._db.execute(
            "SELECT id, task_id, type, label, content FROM task_context WHERE task_id = ?",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_subtasks(self, parent_task_id: str) -> list[Task]:
        """Return all direct children of a task."""
        cursor = await self._db.execute(
            "SELECT * FROM tasks WHERE parent_task_id = ?", (parent_task_id,)
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

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
        cursor = await self._db.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND parent_task_id IS NULL "
            "ORDER BY priority ASC, created_at ASC",
            (project_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

    @staticmethod
    def _row_to_task(row) -> Task:
        """Convert a database row to a Task model."""
        keys = row.keys()
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
            requires_approval=bool(row["requires_approval"]) if "requires_approval" in keys else False,
            pr_url=row["pr_url"] if "pr_url" in keys else None,
            plan_source=row["plan_source"] if "plan_source" in keys else None,
            is_plan_subtask=bool(row["is_plan_subtask"]) if "is_plan_subtask" in keys else False,
            task_type=TaskType(row["task_type"]) if "task_type" in keys and row["task_type"] else None,
            profile_id=row["profile_id"] if "profile_id" in keys else None,
            preferred_workspace_id=row["preferred_workspace_id"] if "preferred_workspace_id" in keys else None,
            attachments=json.loads(row["attachments"]) if "attachments" in keys and row["attachments"] else [],
        )
