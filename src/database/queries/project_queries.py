"""Project CRUD operations."""

from __future__ import annotations

import json
import time

from sqlalchemy import delete, insert, select, update

from src.database.tables import (
    chat_analyzer_suggestions,
    events,
    project_constraints,
    projects,
    repos,
    task_context,
    task_criteria,
    task_dependencies,
    task_metadata,
    task_results,
    task_tools,
    tasks,
    token_ledger,
    workspaces,
)
from src.models import Project, ProjectConstraint, ProjectStatus


class ProjectQueryMixin:
    """Query mixin for project operations.  Expects ``self._engine``."""

    async def create_project(self, project: Project) -> None:
        """Insert a new project row."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(projects).values(
                    id=project.id,
                    name=project.name,
                    credit_weight=project.credit_weight,
                    max_concurrent_agents=project.max_concurrent_agents,
                    status=project.status.value,
                    total_tokens_used=project.total_tokens_used,
                    budget_limit=project.budget_limit,
                    discord_channel_id=project.discord_channel_id,
                    repo_url=project.repo_url,
                    repo_default_branch=project.repo_default_branch,
                    default_profile_id=project.default_profile_id,
                    created_at=time.time(),
                )
            )

    async def get_project(self, project_id: str) -> Project | None:
        """Fetch a single project by ID."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(projects).where(projects.c.id == project_id))
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_project(row)

    async def list_projects(
        self,
        status: ProjectStatus | None = None,
    ) -> list[Project]:
        """List all projects, optionally filtered by status."""
        stmt = select(projects)
        if status:
            stmt = stmt.where(projects.c.status == status.value)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_project(r) for r in result.mappings().fetchall()]

    async def update_project(self, project_id: str, **kwargs) -> None:
        """Update arbitrary project fields."""
        values = {}
        for key, value in kwargs.items():
            if isinstance(value, ProjectStatus):
                value = value.value
            values[key] = value
        async with self._engine.begin() as conn:
            await conn.execute(update(projects).where(projects.c.id == project_id).values(**values))

    async def delete_project(self, project_id: str) -> None:
        """Delete a project and all associated data (cascading)."""
        async with self._engine.begin() as conn:
            # Get all task IDs for this project
            result = await conn.execute(select(tasks.c.id).where(tasks.c.project_id == project_id))
            task_ids = [r[0] for r in result.fetchall()]

            for tid in task_ids:
                await conn.execute(delete(task_results).where(task_results.c.task_id == tid))
                await conn.execute(
                    delete(task_dependencies).where(
                        (task_dependencies.c.task_id == tid)
                        | (task_dependencies.c.depends_on_task_id == tid)
                    )
                )
                await conn.execute(delete(task_criteria).where(task_criteria.c.task_id == tid))
                await conn.execute(delete(task_context).where(task_context.c.task_id == tid))
                await conn.execute(delete(task_metadata).where(task_metadata.c.task_id == tid))
                await conn.execute(delete(task_tools).where(task_tools.c.task_id == tid))

            await conn.execute(
                delete(chat_analyzer_suggestions).where(
                    chat_analyzer_suggestions.c.project_id == project_id
                )
            )
            # hooks and hook_runs tables removed (playbooks spec §13 Phase 3)
            await conn.execute(delete(token_ledger).where(token_ledger.c.project_id == project_id))
            await conn.execute(delete(tasks).where(tasks.c.project_id == project_id))
            await conn.execute(delete(workspaces).where(workspaces.c.project_id == project_id))
            await conn.execute(delete(repos).where(repos.c.project_id == project_id))
            await conn.execute(delete(events).where(events.c.project_id == project_id))
            await conn.execute(
                delete(project_constraints).where(project_constraints.c.project_id == project_id)
            )
            await conn.execute(delete(projects).where(projects.c.id == project_id))

    # ── Project constraint operations ────────────────────────────────

    async def set_project_constraint(self, constraint: ProjectConstraint) -> None:
        """Insert or update the constraint record for a project.

        Uses INSERT-or-REPLACE semantics (SQLite: ON CONFLICT REPLACE,
        PostgreSQL: ON CONFLICT DO UPDATE).  Any fields not provided on
        the new constraint object overwrite the old record — callers must
        merge fields before calling this method if they want additive
        "stacking" behavior.
        """
        async with self._engine.begin() as conn:
            # Delete-then-insert is simpler and works on both SQLite and PG.
            await conn.execute(
                delete(project_constraints).where(
                    project_constraints.c.project_id == constraint.project_id
                )
            )
            await conn.execute(
                insert(project_constraints).values(
                    project_id=constraint.project_id,
                    exclusive=int(constraint.exclusive),
                    max_agents_by_type=json.dumps(constraint.max_agents_by_type),
                    pause_scheduling=int(constraint.pause_scheduling),
                    created_by=constraint.created_by,
                    created_at=constraint.created_at or time.time(),
                )
            )

    async def get_project_constraint(self, project_id: str) -> ProjectConstraint | None:
        """Fetch the active constraint for a project, or None."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(project_constraints).where(project_constraints.c.project_id == project_id)
            )
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_project_constraint(row)

    async def list_project_constraints(self) -> list[ProjectConstraint]:
        """Fetch all active project constraints."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(project_constraints))
            return [self._row_to_project_constraint(r) for r in result.mappings().fetchall()]

    async def delete_project_constraint(self, project_id: str) -> bool:
        """Remove the constraint for a project.  Returns True if a row was deleted."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(project_constraints).where(project_constraints.c.project_id == project_id)
            )
            return result.rowcount > 0

    @staticmethod
    def _row_to_project_constraint(row) -> ProjectConstraint:
        """Convert a database row to a ProjectConstraint model."""
        mat = row.get("max_agents_by_type", "{}")
        if isinstance(mat, str):
            mat = json.loads(mat)
        return ProjectConstraint(
            project_id=row["project_id"],
            exclusive=bool(row["exclusive"]),
            max_agents_by_type=mat,
            pause_scheduling=bool(row["pause_scheduling"]),
            created_by=row.get("created_by"),
            created_at=row.get("created_at", 0.0),
        )

    @staticmethod
    def _row_to_project(row) -> Project:
        """Convert a database row to a Project model."""
        channel_id = row.get("discord_channel_id")
        if not channel_id:
            channel_id = row.get("discord_control_channel_id")
        return Project(
            id=row["id"],
            name=row["name"],
            credit_weight=row["credit_weight"],
            max_concurrent_agents=row["max_concurrent_agents"],
            status=ProjectStatus(row["status"]),
            total_tokens_used=row["total_tokens_used"],
            budget_limit=row["budget_limit"],
            discord_channel_id=channel_id,
            repo_url=row["repo_url"] if row.get("repo_url") else "",
            repo_default_branch=row["repo_default_branch"]
            if row.get("repo_default_branch")
            else "main",
            default_profile_id=row.get("default_profile_id"),
        )
