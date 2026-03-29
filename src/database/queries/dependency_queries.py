"""Task dependency operations."""

from __future__ import annotations

from src.models import Task, TaskStatus


class DependencyQueryMixin:
    """Query mixin for task dependency operations.  Expects ``self._db``."""

    async def add_dependency(self, task_id: str, depends_on: str) -> None:
        """Add a dependency edge between two tasks."""
        await self._db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
            (task_id, depends_on),
        )
        await self._db.commit()

    async def get_dependencies(self, task_id: str) -> set[str]:
        """Return IDs of tasks that *task_id* depends on."""
        cursor = await self._db.execute(
            "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return {r["depends_on_task_id"] for r in rows}

    async def get_all_dependencies(self) -> dict[str, set[str]]:
        """Return the full dependency graph as {task_id: {dep_ids}}."""
        cursor = await self._db.execute("SELECT * FROM task_dependencies")
        rows = await cursor.fetchall()
        deps: dict[str, set[str]] = {}
        for r in rows:
            deps.setdefault(r["task_id"], set()).add(r["depends_on_task_id"])
        return deps

    async def are_dependencies_met(self, task_id: str) -> bool:
        """Check whether all upstream dependencies are COMPLETED."""
        cursor = await self._db.execute(
            "SELECT d.depends_on_task_id, t.status "
            "FROM task_dependencies d "
            "JOIN tasks t ON t.id = d.depends_on_task_id "
            "WHERE d.task_id = ?",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return all(r["status"] == TaskStatus.COMPLETED.value for r in rows)

    async def get_stuck_defined_tasks(self, threshold_seconds: int) -> list[Task]:
        """Return DEFINED tasks blocked by a BLOCKED or FAILED dependency."""
        cursor = await self._db.execute(
            "SELECT DISTINCT t.* FROM tasks t "
            "JOIN task_dependencies d ON d.task_id = t.id "
            "JOIN tasks dep ON dep.id = d.depends_on_task_id "
            "WHERE t.status = ? AND dep.status IN (?, ?) "
            "ORDER BY t.created_at ASC",
            (
                TaskStatus.DEFINED.value,
                TaskStatus.BLOCKED.value,
                TaskStatus.FAILED.value,
            ),
        )
        rows = await cursor.fetchall()
        # Use the task mixin's _row_to_task (available via multiple inheritance)
        return [self._row_to_task(r) for r in rows]

    async def get_blocking_dependencies(
        self, task_id: str,
    ) -> list[tuple[str, str, str]]:
        """Return (dep_task_id, dep_title, dep_status) for unmet dependencies."""
        cursor = await self._db.execute(
            "SELECT t.id, t.title, t.status "
            "FROM task_dependencies d "
            "JOIN tasks t ON t.id = d.depends_on_task_id "
            "WHERE d.task_id = ? AND t.status != ?",
            (task_id, TaskStatus.COMPLETED.value),
        )
        rows = await cursor.fetchall()
        return [(r["id"], r["title"], r["status"]) for r in rows]

    async def get_dependents(self, task_id: str) -> set[str]:
        """Return task IDs that directly depend on *task_id* (reverse lookup)."""
        cursor = await self._db.execute(
            "SELECT task_id FROM task_dependencies WHERE depends_on_task_id = ?",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return {r["task_id"] for r in rows}

    async def get_dependency_map_for_tasks(
        self, task_ids: list[str],
    ) -> dict[str, dict]:
        """Batch-fetch dependency data for multiple tasks in two queries.

        Returns a mapping of task_id → {"depends_on": [...], "blocks": [...]}.
        """
        if not task_ids:
            return {}

        result: dict[str, dict] = {
            tid: {"depends_on": [], "blocks": []} for tid in task_ids
        }

        placeholders = ",".join("?" for _ in task_ids)
        cursor = await self._db.execute(
            "SELECT d.task_id, d.depends_on_task_id, t.status "
            "FROM task_dependencies d "
            "JOIN tasks t ON t.id = d.depends_on_task_id "
            f"WHERE d.task_id IN ({placeholders})",
            task_ids,
        )
        for row in await cursor.fetchall():
            tid = row["task_id"]
            if tid in result:
                result[tid]["depends_on"].append({
                    "id": row["depends_on_task_id"],
                    "status": row["status"],
                })

        cursor = await self._db.execute(
            "SELECT d.depends_on_task_id, d.task_id "
            "FROM task_dependencies d "
            f"WHERE d.depends_on_task_id IN ({placeholders})",
            task_ids,
        )
        for row in await cursor.fetchall():
            blocked_by = row["depends_on_task_id"]
            if blocked_by in result:
                result[blocked_by]["blocks"].append(row["task_id"])

        for entry in result.values():
            entry["blocks"] = sorted(entry["blocks"])

        return result

    async def remove_dependency(self, task_id: str, depends_on: str) -> None:
        """Remove a single dependency edge."""
        await self._db.execute(
            "DELETE FROM task_dependencies "
            "WHERE task_id = ? AND depends_on_task_id = ?",
            (task_id, depends_on),
        )
        await self._db.commit()

    async def remove_all_dependencies_on(self, depends_on_task_id: str) -> None:
        """Remove all dependency edges pointing to a given task."""
        await self._db.execute(
            "DELETE FROM task_dependencies WHERE depends_on_task_id = ?",
            (depends_on_task_id,),
        )
        await self._db.commit()
