"""Open the pull request for a completed train root.

A train root is either an epic whose children were collected into its branch
or a childless root filed straight onto its own ``aq/epic/...`` branch.  The
second shape closes through the leaf checkpoint (``checkpoint_leaf_completion``)
and has no parent completion to open its PR, so this service accepts it once
its checkpoint names the finished source head.
"""

from __future__ import annotations

import re
import time
from typing import Any

from sqlalchemy import func, or_, select, update

from src.database.tables import (
    archived_tasks,
    projects,
    repos,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
)
from src.integration.epic_dependencies import dependencies_for

_OID = re.compile(r"[0-9a-f]{40}")


def leaf_root_source(checkpoint, origin, *, branch: str | None, repo_id: str | None) -> str:
    """Classify a childless root's checkpoint as a PR source.

    Returns ``ready`` when the checkpoint names a finished leaf head on the
    root's own branch (the shape ``eligible_root_page_on`` seats as a leaf),
    ``no_checkpoint`` when the root is outside hierarchical delivery (the legacy
    completion pipeline owns its PR), ``no_changes`` when the head is still
    the origin base, and ``checkpoint_not_ready`` otherwise.
    """
    if checkpoint is None:
        return "no_checkpoint"
    if (
        origin is None
        or checkpoint["branch"] != branch
        or checkpoint["repository_id"] != repo_id
        or not checkpoint["checkpoint_sha"]
        or not _OID.fullmatch(checkpoint["checkpoint_sha"])
        or checkpoint["episode_id"] is not None
        or checkpoint["current_verification_id"] is not None
    ):
        return "checkpoint_not_ready"
    if checkpoint["checkpoint_sha"] == origin["base_sha"]:
        return "no_changes"
    return "ready"


def render_body(
    *,
    epic_id: str,
    epic_title: str,
    children: list[dict],
    dependencies: list[dict],
) -> str:
    """Describe the epic and carry its stable identity in a GitHub trailer.

    A childless root has no children section.
    """
    lines = [epic_title]
    if children:
        lines.extend(("", "## Children", ""))
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
            checkpoint, origin = await self._source_rows_on(conn, epic)
            if not children:
                source = leaf_root_source(
                    checkpoint, origin, branch=epic["branch_name"], repo_id=epic["repo_id"]
                )
                if source == "no_checkpoint":
                    return {"outcome": "not_epic", "epic_id": epic_id}
                if source != "ready":
                    return {"outcome": source, "epic_id": epic_id}
                head = checkpoint["checkpoint_sha"]
            else:
                head = checkpoint["verified_sha"] if checkpoint is not None else None
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
        if head is not None:
            # A head already on the default branch (a fix-forward merged by
            # hand) has nothing to propose; GitHub would refuse the PR anyway.
            ahead = await self._git.acommits_ahead_of_base(
                repository=binding, base=repository["default_branch"], head_sha=head
            )
            if ahead == 0:
                return {"outcome": "already_on_default", "epic_id": epic_id, "head_sha": head}
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
                .where(
                    tasks.c.id == epic_id,
                    or_(tasks.c.pr_url.is_(None), func.trim(tasks.c.pr_url) == ""),
                )
                .values(pr_url=pr_url)
            )
            stored = (
                await conn.execute(select(tasks.c.pr_url).where(tasks.c.id == epic_id))
            ).scalar_one()
        if stored != pr_url:
            return {"outcome": "already_open", "epic_id": epic_id, "pr_url": stored}
        return {"outcome": "opened", "epic_id": epic_id, "pr_url": pr_url}

    @staticmethod
    async def _source_rows_on(conn, epic):
        """The root's live checkpoint and origin, either of which may be absent."""
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == epic["id"]
                )
            )
        ).mappings().one_or_none()
        origin = (
            await conn.execute(
                select(task_branch_origins.c.base_sha).where(
                    task_branch_origins.c.task_id == epic["id"],
                    task_branch_origins.c.repository_id == epic["repo_id"],
                    task_branch_origins.c.retired_at.is_(None),
                )
            )
        ).mappings().one_or_none()
        return checkpoint, origin
