"""Archived task operations."""

from __future__ import annotations

import json
import time

from src.models import Task, TaskStatus, TaskType, VerificationType


class ArchiveQueryMixin:
    """Query mixin for archived task operations.  Expects ``self._db``."""

    async def archive_task(self, task_id: str) -> bool:
        """Move a task from ``tasks`` into ``archived_tasks``.

        Returns *True* if the task was archived, *False* if not found.
        """
        task = await self.get_task(task_id)
        if task is None:
            return False

        now = time.time()
        await self._db.execute(
            "INSERT OR IGNORE INTO archived_tasks "
            "(id, project_id, parent_task_id, repo_id, title, description, "
            "priority, status, verification_type, retry_count, max_retries, "
            "assigned_agent_id, branch_name, resume_after, requires_approval, "
            "pr_url, plan_source, is_plan_subtask, task_type, profile_id, "
            "preferred_workspace_id, attachments, auto_approve_plan, "
            "created_at, updated_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.id,
                task.project_id,
                task.parent_task_id,
                task.repo_id,
                task.title,
                task.description,
                task.priority,
                task.status.value,
                task.verification_type.value,
                task.retry_count,
                task.max_retries,
                task.assigned_agent_id,
                task.branch_name,
                task.resume_after,
                int(task.requires_approval),
                task.pr_url,
                task.plan_source,
                int(task.is_plan_subtask),
                task.task_type.value if task.task_type else None,
                task.profile_id,
                task.preferred_workspace_id,
                json.dumps(task.attachments) if task.attachments else "[]",
                int(task.auto_approve_plan),
                0.0,
                0.0,
                now,
            ),
        )
        # Read original timestamps from the tasks row directly.
        cursor = await self._db.execute(
            "SELECT created_at, updated_at FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        if row:
            await self._db.execute(
                "UPDATE archived_tasks SET created_at = ?, updated_at = ? WHERE id = ?",
                (row["created_at"], row["updated_at"], task_id),
            )

        # Clean up child rows, then remove from active table.
        await self._db.execute("DELETE FROM task_results WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM token_ledger WHERE task_id = ?", (task_id,))
        await self._db.execute(
            "DELETE FROM task_dependencies WHERE task_id = ? OR depends_on_task_id = ?",
            (task_id, task_id),
        )
        await self._db.execute("DELETE FROM task_criteria WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM task_context WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM task_tools WHERE task_id = ?", (task_id,))
        await self._db.execute(
            "UPDATE tasks SET parent_task_id = NULL WHERE parent_task_id = ?",
            (task_id,),
        )
        await self._db.execute(
            "UPDATE agents SET current_task_id = NULL WHERE current_task_id = ?",
            (task_id,),
        )
        await self._db.execute(
            "UPDATE workspaces SET locked_by_task_id = NULL, locked_at = NULL "
            "WHERE locked_by_task_id = ?",
            (task_id,),
        )
        await self._db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await self._db.commit()
        return True

    async def archive_completed_tasks(
        self,
        project_id: str | None = None,
    ) -> list[str]:
        """Archive all COMPLETED tasks. Returns list of archived task IDs."""
        conditions = ["status = ?"]
        vals: list = [TaskStatus.COMPLETED.value]
        if project_id:
            conditions.append("project_id = ?")
            vals.append(project_id)
        where = f"WHERE {' AND '.join(conditions)}"
        cursor = await self._db.execute(
            f"SELECT id FROM tasks {where}",
            vals,
        )
        rows = await cursor.fetchall()
        task_ids = [r["id"] for r in rows]

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
        placeholders = ", ".join("?" for _ in statuses)
        cursor = await self._db.execute(
            f"SELECT id FROM tasks WHERE status IN ({placeholders}) AND updated_at <= ?",
            [*statuses, cutoff],
        )
        rows = await cursor.fetchall()
        task_ids = [r["id"] for r in rows]

        for tid in task_ids:
            await self.archive_task(tid)

        return task_ids

    async def list_archived_tasks(
        self,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return archived tasks as dicts, newest archived first."""
        conditions: list[str] = []
        vals: list = []
        if project_id:
            conditions.append("project_id = ?")
            vals.append(project_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT * FROM archived_tasks {where} ORDER BY archived_at DESC LIMIT ?",
            vals + [limit],
        )
        rows = await cursor.fetchall()
        return [self._row_to_archived_task(r) for r in rows]

    async def get_archived_task(self, task_id: str) -> dict | None:
        """Return a single archived task as a dict, or *None* if not found."""
        cursor = await self._db.execute("SELECT * FROM archived_tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
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
        await self._db.execute("DELETE FROM archived_tasks WHERE id = ?", (task_id,))
        await self._db.commit()
        return True

    async def delete_archived_task(self, task_id: str) -> bool:
        """Permanently delete an archived task. Returns *True* if deleted."""
        cursor = await self._db.execute("SELECT id FROM archived_tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        if not row:
            return False
        await self._db.execute("DELETE FROM archived_tasks WHERE id = ?", (task_id,))
        await self._db.commit()
        return True

    async def count_archived_tasks(
        self,
        project_id: str | None = None,
    ) -> int:
        """Return the total count of archived tasks."""
        conditions: list[str] = []
        vals: list = []
        if project_id:
            conditions.append("project_id = ?")
            vals.append(project_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT COUNT(*) as cnt FROM archived_tasks {where}",
            vals,
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    @staticmethod
    def _row_to_archived_task(row) -> dict:
        """Convert a database row from ``archived_tasks`` to a plain dict."""
        keys = row.keys()
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
            "plan_source": row["plan_source"] if "plan_source" in keys else None,
            "is_plan_subtask": bool(row["is_plan_subtask"]) if "is_plan_subtask" in keys else False,
            "task_type": row["task_type"] if "task_type" in keys else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
        }
