"""Project CRUD operations."""

from __future__ import annotations

import time

from src.models import Project, ProjectStatus


class ProjectQueryMixin:
    """Query mixin for project operations.  Expects ``self._db``."""

    async def create_project(self, project: Project) -> None:
        """Insert a new project row."""
        await self._db.execute(
            "INSERT INTO projects (id, name, credit_weight, max_concurrent_agents, "
            "status, total_tokens_used, budget_limit, "
            "discord_channel_id, repo_url, repo_default_branch, "
            "default_profile_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project.id,
                project.name,
                project.credit_weight,
                project.max_concurrent_agents,
                project.status.value,
                project.total_tokens_used,
                project.budget_limit,
                project.discord_channel_id,
                project.repo_url,
                project.repo_default_branch,
                project.default_profile_id,
                time.time(),
            ),
        )
        await self._db.commit()

    async def get_project(self, project_id: str) -> Project | None:
        """Fetch a single project by ID."""
        cursor = await self._db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_project(row)

    async def list_projects(
        self,
        status: ProjectStatus | None = None,
    ) -> list[Project]:
        """List all projects, optionally filtered by status."""
        if status:
            cursor = await self._db.execute(
                "SELECT * FROM projects WHERE status = ?", (status.value,)
            )
        else:
            cursor = await self._db.execute("SELECT * FROM projects")
        rows = await cursor.fetchall()
        return [self._row_to_project(r) for r in rows]

    async def update_project(self, project_id: str, **kwargs) -> None:
        """Update arbitrary project fields."""
        sets = []
        vals = []
        for key, value in kwargs.items():
            if isinstance(value, ProjectStatus):
                value = value.value
            sets.append(f"{key} = ?")
            vals.append(value)
        vals.append(project_id)
        await self._db.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", vals)
        await self._db.commit()

    async def delete_project(self, project_id: str) -> None:
        """Delete a project and all associated data (cascading)."""
        # Get all task IDs for this project
        cursor = await self._db.execute("SELECT id FROM tasks WHERE project_id = ?", (project_id,))
        task_rows = await cursor.fetchall()
        task_ids = [r["id"] for r in task_rows]

        for tid in task_ids:
            await self._db.execute("DELETE FROM task_results WHERE task_id = ?", (tid,))
            await self._db.execute(
                "DELETE FROM task_dependencies WHERE task_id = ? OR depends_on_task_id = ?",
                (tid, tid),
            )
            await self._db.execute("DELETE FROM task_criteria WHERE task_id = ?", (tid,))
            await self._db.execute("DELETE FROM task_context WHERE task_id = ?", (tid,))
            await self._db.execute("DELETE FROM task_tools WHERE task_id = ?", (tid,))

        await self._db.execute(
            "DELETE FROM chat_analyzer_suggestions WHERE project_id = ?", (project_id,)
        )
        await self._db.execute("DELETE FROM hook_runs WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM hooks WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM token_ledger WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM workspaces WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM repos WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM events WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await self._db.commit()

    @staticmethod
    def _row_to_project(row) -> Project:
        """Convert a database row to a Project model."""
        keys = row.keys()
        channel_id = row["discord_channel_id"] if "discord_channel_id" in keys else None
        if not channel_id and "discord_control_channel_id" in keys:
            channel_id = row["discord_control_channel_id"]
        return Project(
            id=row["id"],
            name=row["name"],
            credit_weight=row["credit_weight"],
            max_concurrent_agents=row["max_concurrent_agents"],
            status=ProjectStatus(row["status"]),
            total_tokens_used=row["total_tokens_used"],
            budget_limit=row["budget_limit"],
            discord_channel_id=channel_id,
            repo_url=row["repo_url"] if "repo_url" in keys and row["repo_url"] else "",
            repo_default_branch=(
                row["repo_default_branch"]
                if "repo_default_branch" in keys and row["repo_default_branch"]
                else "main"
            ),
            default_profile_id=(
                row["default_profile_id"] if "default_profile_id" in keys else None
            ),
        )
