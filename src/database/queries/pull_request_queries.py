"""PR links recorded on tasks in active projects, including archived tasks."""

from __future__ import annotations

from sqlalchemy import and_, func, select, union_all

from src.database.tables import archived_tasks, projects, repos, tasks


async def list_known_pull_requests(db) -> list[dict]:
    def candidates(task_table):
        return (
            select(
                task_table.c.id.label("task_id"),
                task_table.c.title.label("task_title"),
                task_table.c.pr_url,
                task_table.c.created_at.label("task_created_at"),
                projects.c.id.label("project_id"),
                projects.c.name.label("project_name"),
                func.coalesce(repos.c.url, projects.c.repo_url).label("repository_url"),
            )
            .select_from(
                task_table.join(projects, task_table.c.project_id == projects.c.id)
                .outerjoin(
                    repos,
                    and_(
                        task_table.c.repo_id == repos.c.id,
                        repos.c.project_id == projects.c.id,
                    ),
                )
            )
            .where(
                projects.c.status == "ACTIVE",
                task_table.c.pr_url.is_not(None),
                func.trim(task_table.c.pr_url) != "",
            )
        )

    query = union_all(candidates(tasks), candidates(archived_tasks))
    async with db._engine.begin() as conn:
        result = await conn.execute(query)
        return [dict(row) for row in result.mappings()]
