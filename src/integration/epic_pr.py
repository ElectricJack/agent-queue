"""Open the pull request for a completed train epic."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select, update

from src.database.tables import archived_tasks, projects, repos, tasks
from src.integration.epic_dependencies import dependencies_for


def render_body(
    *,
    epic_id: str,
    epic_title: str,
    children: list[dict],
    dependencies: list[dict],
) -> str:
    """Describe the epic and carry its stable identity in a GitHub trailer."""
    lines = [epic_title, "", "## Children", ""]
    lines.extend(f"- `{child['id']}` {child['title']}" for child in children)
    if dependencies:
        lines.extend(("", "## Depends on", ""))
        lines.extend(f"- `{dependency['id']}` {dependency['title']}" for dependency in dependencies)
    lines.extend(("", f"AQ-Epic: {epic_id}"))
    return "\n".join(lines)


class EpicPullRequestService:
    """Create one GitHub pull request and retain its URL on the root task."""

    def __init__(self, db, *, git_manager, clock=time.time) -> None:
        self._db = db
        self._git = git_manager
        self._clock = clock

    async def open_for_epic(self, epic_id: str) -> dict[str, Any]:
        async with self._db.immediate() as conn:
            epic = (
                await conn.execute(select(tasks).where(tasks.c.id == epic_id))
            ).mappings().one_or_none()
            if epic is None:
                return {"outcome": "unknown_epic", "epic_id": epic_id}
            if epic["pr_url"]:
                return {"outcome": "already_open", "epic_id": epic_id, "pr_url": epic["pr_url"]}
            project = (
                await conn.execute(
                    select(projects.c.hierarchical_integration_mode, projects.c.integration_repository_id)
                    .where(projects.c.id == epic["project_id"])
                )
            ).one()
            if project.hierarchical_integration_mode != "train":
                return {"outcome": "not_train", "epic_id": epic_id}
            if epic["parent_task_id"] is not None:
                return {"outcome": "not_root_epic", "epic_id": epic_id}
            if epic["status"] != "COMPLETED":
                return {"outcome": "epic_incomplete", "epic_id": epic_id}

            active_children = (
                await conn.execute(
                    select(tasks.c.id, tasks.c.title, tasks.c.status)
                    .where(tasks.c.parent_task_id == epic_id)
                    .order_by(tasks.c.id)
                )
            ).mappings().all()
            archived_children = (
                await conn.execute(
                    select(archived_tasks.c.id, archived_tasks.c.title, archived_tasks.c.status)
                    .where(archived_tasks.c.parent_task_id == epic_id)
                    .order_by(archived_tasks.c.id)
                )
            ).mappings().all()
            children = sorted(
                [*active_children, *archived_children], key=lambda child: child["id"]
            )
            if not children:
                return {"outcome": "not_epic", "epic_id": epic_id}
            pending = [child["id"] for child in children if child["status"] != "COMPLETED"]
            if pending:
                return {"outcome": "children_incomplete", "epic_id": epic_id, "pending": pending}
            if not epic["branch_name"]:
                return {"outcome": "branch_missing", "epic_id": epic_id}

            repository = (
                await conn.execute(
                    select(repos).where(
                        repos.c.id == project.integration_repository_id,
                        repos.c.project_id == epic["project_id"],
                        repos.c.id == epic["repo_id"],
                    )
                )
            ).mappings().one_or_none()
            if repository is None or not repository["url"]:
                return {"outcome": "repository_missing", "epic_id": epic_id}

            edges = await dependencies_for(conn, [epic_id])
            dependency_ids = sorted(edges.get(epic_id, set()))
            dependencies = []
            if dependency_ids:
                active_dependencies = (
                    await conn.execute(
                        select(tasks.c.id, tasks.c.title).where(tasks.c.id.in_(dependency_ids))
                    )
                ).mappings().all()
                archived_dependencies = (
                    await conn.execute(
                        select(archived_tasks.c.id, archived_tasks.c.title).where(
                            archived_tasks.c.id.in_(dependency_ids)
                        )
                    )
                ).mappings().all()
                dependencies = sorted(
                    [*active_dependencies, *archived_dependencies], key=lambda dep: dep["id"]
                )
            body = render_body(
                epic_id=epic_id,
                epic_title=epic["title"],
                children=[{"id": child["id"], "title": child["title"]} for child in children],
                dependencies=[{"id": dep["id"], "title": dep["title"]} for dep in dependencies],
            )

        binding = await self._git.bind_github_repository(repository["url"])
        pr_url = await self._git.acreate_pr(
            repository["source_path"] or repository["checkout_base_path"],
            branch=epic["branch_name"],
            title=epic["title"],
            body=body,
            base=repository["default_branch"],
            project_id=epic["project_id"],
            repository=binding,
        )

        async with self._db.immediate() as conn:
            await conn.execute(
                update(tasks)
                .where(tasks.c.id == epic_id, tasks.c.pr_url.is_(None))
                .values(pr_url=pr_url)
            )
            stored = (
                await conn.execute(select(tasks.c.pr_url).where(tasks.c.id == epic_id))
            ).scalar_one()
        if stored != pr_url:
            return {"outcome": "already_open", "epic_id": epic_id, "pr_url": stored}
        return {"outcome": "opened", "epic_id": epic_id, "pr_url": pr_url}
