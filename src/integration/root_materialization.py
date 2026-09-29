"""Recover a completed legacy train root's missing delivery identity."""

from __future__ import annotations

import json
import time
from uuid import uuid4

from sqlalchemy import insert, select

from src.database.tables import (
    archived_tasks,
    projects,
    repos,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
)
from src.git.github import GitHubAccess
from src.git.manager import RemoteRefState, is_valid_git_oid


class RootMaterialization:
    """Prove a PR and its Git lineage before recording a legacy leaf root."""

    def __init__(self, db, promotion, *, clock=time.time) -> None:
        self.db = db
        self.promotion = promotion
        self.git = promotion.git
        self.clock = clock

    async def run(
        self,
        task_id: str,
        *,
        dry_run: bool = True,
        expected_head_sha: str | None = None,
        reason: str | None = None,
        operator_id: str | None = None,
    ) -> dict:
        async with self.db._engine.connect() as conn:
            state = await self._state(conn, task_id)
        if state["outcome"] != "candidate":
            return state
        task, repo = state["task"], state["repo"]
        result = {
            "task_id": task_id,
            "project_id": task["project_id"],
            "branch": task["branch_name"],
            "pr_url": task["pr_url"],
        }
        try:
            resolved = await self.promotion._resolve_repository(repo["id"])
            if resolved.repo.project_id != task["project_id"]:
                return {**result, "outcome": "blocked", "reason": "repository project changed"}
            await self.promotion._ensure_retained_repository(resolved)
            binding = await self.git.bind_github_repository(repo["url"])
            GitHubAccess.validate_pr_url(binding, task["pr_url"])
            pull = await self.git._github_client(binding).pull_request(task["pr_url"])
            head, base = pull.get("head"), pull.get("base")
            if (
                pull.get("state") != "open"
                or not isinstance(head, dict)
                or not isinstance(base, dict)
                or not isinstance(head.get("repo"), dict)
                or not isinstance(base.get("repo"), dict)
                or head["repo"].get("id") != binding.repository_id
                or base["repo"].get("id") != binding.repository_id
                or head.get("ref") != task["branch_name"]
                or base.get("ref") != repo["default_branch"]
                or not is_valid_git_oid(head.get("sha"))
            ):
                return {
                    **result,
                    "outcome": "blocked",
                    "reason": "PR route is not the exact open root branch to the default branch",
                }
            head_sha = head["sha"]
            store = str(resolved.retained_git_dir)
            async with self.git.arepository_transaction(store):
                await self.promotion._fetch_all_heads(
                    resolved.retained_git_dir, resolved.origin_url
                )
                remote = await self.git.als_remote_ref(store, task["branch_name"])
                main = await self.git.als_remote_ref(store, repo["default_branch"])
                if remote.state is not RemoteRefState.PRESENT or remote.oid != head_sha:
                    return {
                        **result,
                        "outcome": "changed",
                        "reason": "remote root branch differs from the PR head",
                    }
                if main.state is not RemoteRefState.PRESENT:
                    return {
                        **result,
                        "outcome": "blocked",
                        "reason": "default branch is unavailable",
                    }
                merge_base = await self.git.arun_git_result(
                    ["merge-base", main.oid, head_sha],
                    cwd=store,
                    lock_held=True,
                )
                base_sha = merge_base.stdout.strip()
                if merge_base.returncode != 0 or not is_valid_git_oid(base_sha):
                    return {
                        **result,
                        "outcome": "blocked",
                        "reason": "PR head has no provable merge-base with the default branch",
                    }
                if base_sha == head_sha:
                    return {
                        **result,
                        "outcome": "blocked",
                        "reason": "PR head has no changes beyond the default branch",
                    }
                ancestry = await self.git.arun_git_result(
                    ["merge-base", "--is-ancestor", base_sha, head_sha],
                    cwd=store,
                    lock_held=True,
                )
                if ancestry.returncode != 0:
                    return {
                        **result,
                        "outcome": "blocked",
                        "reason": "PR head does not descend from its default-branch merge-base",
                    }
        except Exception as exc:
            return {**result, "outcome": "blocked", "reason": f"Git or GitHub proof failed: {exc}"}
        result.update(head_sha=head_sha, base_sha=base_sha)
        if dry_run:
            return {**result, "outcome": "would_materialize"}
        if expected_head_sha != head_sha:
            return {**result, "outcome": "changed", "reason": "head differs from the dry run"}
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, task["project_id"])
            current = await self._state(conn, task_id)
            if (
                current["outcome"] != "candidate"
                or current["task"] != task
                or current["repo"] != repo
            ):
                return {
                    **result,
                    "outcome": "changed",
                    "reason": "root identity changed during proof",
                }
            now = self.clock()
            await conn.execute(
                insert(task_branch_origins).values(
                    id=str(uuid4()),
                    task_id=task_id,
                    repository_id=repo["id"],
                    branch_name=task["branch_name"],
                    parent_task_id=None,
                    parent_repository_id=None,
                    parent_ref=repo["default_branch"],
                    base_sha=base_sha,
                    creation_generation=0,
                    reserved=True,
                    materialized=True,
                    created_at=now,
                    materialized_at=now,
                )
            )
            await conn.execute(
                insert(task_integration_checkpoints).values(
                    task_id=task_id,
                    repository_id=repo["id"],
                    branch=task["branch_name"],
                    generation=0,
                    checkpoint_sha=head_sha,
                    state="working",
                    version=0,
                    updated_at=now,
                )
            )
            await self.db.log_event(
                "integration.root_materialized",
                project_id=task["project_id"],
                task_id=task_id,
                payload=json.dumps(
                    {
                        "head_sha": head_sha,
                        "base_sha": base_sha,
                        "pr_url": task["pr_url"],
                        "operator_id": operator_id,
                        "reason": reason,
                        "at": now,
                    }
                ),
                conn=conn,
            )
        return {**result, "outcome": "materialized"}

    async def _state(self, conn, task_id: str) -> dict:
        task = (
            (await conn.execute(select(tasks).where(tasks.c.id == task_id)))
            .mappings()
            .one_or_none()
        )
        if task is None:
            return {"outcome": "not_found", "task_id": task_id}
        project = (
            (await conn.execute(select(projects).where(projects.c.id == task["project_id"])))
            .mappings()
            .one()
        )
        base = {"task_id": task_id, "project_id": task["project_id"]}
        if (
            project["hierarchical_integration_mode"] != "train"
            or project["integration_repository_id"] != task["repo_id"]
            or task["parent_task_id"] is not None
            or task["status"] != "COMPLETED"
            or not task["branch_name"]
            or not task["pr_url"]
            or not task["pr_url"].strip()
        ):
            return {
                **base,
                "outcome": "not_eligible",
                "reason": "requires a completed train root with a PR and canonical branch",
            }
        repo = (
            (
                await conn.execute(
                    select(repos).where(
                        repos.c.id == task["repo_id"], repos.c.project_id == task["project_id"]
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if repo is None:
            return {**base, "outcome": "not_eligible", "reason": "designated repository is missing"}
        children = (
            await conn.execute(select(tasks.c.id).where(tasks.c.parent_task_id == task_id).limit(1))
        ).first()
        archived = (
            await conn.execute(
                select(archived_tasks.c.id)
                .where(archived_tasks.c.parent_task_id == task_id)
                .limit(1)
            )
        ).first()
        if children or archived:
            return {
                **base,
                "outcome": "not_eligible",
                "reason": "parent roots require original verification evidence",
            }
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints.c.task_id).where(
                    task_integration_checkpoints.c.task_id == task_id
                )
            )
        ).first()
        origins = (
            await conn.execute(
                select(task_branch_origins.c.id)
                .where(task_branch_origins.c.task_id == task_id)
                .limit(1)
            )
        ).first()
        if checkpoint or origins:
            return {
                **base,
                "outcome": "not_eligible",
                "reason": "integration identity already exists",
            }
        return {**base, "outcome": "candidate", "task": dict(task), "repo": dict(repo)}
