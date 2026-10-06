"""Refresh an epic through the attested train before starting its dependents."""

from __future__ import annotations

import hashlib
import json
import time

from sqlalchemy import select, update

from src.database.tables import projects, repos, task_branch_origins, task_dependencies, tasks
from src.integration.batches import Batch, BatchMember, BatchStore
from src.integration.delivery_observer import prerequisite_observer
from src.integration.train import TrainTarget


class EpicRefreshPending(ValueError):
    """Normal admission wait while the train checks or repairs an epic."""


class EpicRefresh:
    def __init__(self, db, train=None, *, clock=time.time):
        self.db, self.train, self.clock = db, train, clock

    async def identity(self, task_id, *, conn=None):
        child = tasks.alias("refresh_epic_child")
        query = select(tasks.c.id, tasks.c.project_id, tasks.c.branch_name,
                       tasks.c.repo_id, repos.c.default_branch).select_from(
            tasks.join(projects, projects.c.id == tasks.c.project_id)
            .join(repos, repos.c.id == projects.c.integration_repository_id)
        ).where(tasks.c.id == task_id, tasks.c.repo_id == repos.c.id,
                projects.c.hierarchical_integration_mode.in_(("hierarchy", "train")),
                projects.c.status == "ACTIVE",
                tasks.c.branch_name.is_not(None),
                select(child.c.id).where(child.c.parent_task_id == task_id).exists())
        if conn is None:
            async with self.db._engine.connect() as owned:
                return (await owned.execute(query)).mappings().one_or_none()
        return (await conn.execute(query)).mappings().one_or_none()

    async def inspect(self, task_id, *, snapshot=None):
        from src.integration.train_sources import project_snapshot

        row = await self.identity(task_id)
        if row is None:
            raise ValueError("task is not an epic in its project's integration repository")
        ref = "refs/heads/" + row["branch_name"].removeprefix("refs/heads/")
        default = "refs/heads/" + row["default_branch"].removeprefix("refs/heads/")
        if ref == default:
            raise ValueError("an epic cannot own the default branch")
        target = TrainTarget(row["project_id"], row["repo_id"], ref, "epic")
        observed = snapshot.for_target(ref) if snapshot else await project_snapshot(self.db, target)
        if observed is None or observed.error or not observed.target_oid:
            raise ValueError("epic branch cannot be observed")
        main = observed.for_target(default)
        if main.error or not main.target_oid:
            raise ValueError("default branch cannot be observed")
        git, store = observed.observation.git, observed.observation.store
        counts = await git.arun_git_result(
            ["rev-list", "--left-right", "--count", f"{observed.target_oid}...{main.target_oid}"],
            cwd=store,
        )
        if counts.returncode:
            raise ValueError("epic distance cannot be observed")
        ahead, behind = map(int, counts.stdout.split())
        result = {"task_id": task_id, "project_id": row["project_id"], "target_ref": ref,
                  "target_sha": observed.target_oid, "default_ref": default,
                  "default_sha": main.target_oid, "ahead": ahead, "behind": behind}
        return row, target, observed, result

    async def refresh(self, task_id, *, dry_run=True, operator_id=None):
        from src.integration.train_sources import DatabaseBatches

        row, target, observed, result = await self.inspect(task_id)
        if dry_run:
            return {**result, "outcome": "preview", "dry_run": True}
        batches = DatabaseBatches(self.db, clock=self.clock)
        current = await batches.current(target)
        if current is not None and not current.epic_refresh:
            return {**result, "outcome": "pending", "dry_run": False, "batch_id": current.id}
        if current is None and not result["behind"]:
            return {**result, "outcome": "current", "dry_run": False}
        if self.train is None:
            raise ValueError("the Git-first integration train is unavailable")
        if current is None:
            git, store = observed.observation.git, observed.observation.store
            base = await git.arun_git_result(
                ["merge-base", result["target_sha"], result["default_sha"]], cwd=store,
            )
            if base.returncode:
                raise ValueError("epic and default branch have no common base")
            member = BatchMember(task_id, result["default_sha"], base.stdout.strip())
            key = hashlib.sha256(repr((target.key, member, result["target_sha"])).encode()).hexdigest()
            batch = Batch("train-epic-refresh-" + key, target.project_id, target.repository_id,
                          target.target_ref, created_at=self.clock())
            tree = await git.atree_sha(store, member.source_sha)
            if await self.identity(task_id) != row or not await observed.is_fresh():
                raise ValueError("epic identity changed during refresh")
            current = await BatchStore(self.db, clock=self.clock).freeze(
                batch, (member,), trees={task_id: tree})
        visit = await self.train.visit(target)
        if operator_id:
            await self.db.log_event("integration.epic_refresh", project_id=row["project_id"],
                task_id=task_id, payload=json.dumps({**result, "batch_id": current.id,
                    "operator_id": operator_id, "state": visit.state}))
        return {**result, "target_sha": visit.target_sha or result["target_sha"],
                "outcome": "refreshed" if visit.state == "delivered" else "pending",
                "dry_run": False, "batch_id": current.id, "state": visit.state,
                "candidate_sha": visit.candidate_sha, "detail": visit.detail}

    async def child_base(self, task, origin):
        if not task.parent_task_id:
            return None
        project = await self.db.get_project(task.project_id)
        policy = (project.hierarchical_integration_policy or {})
        if policy.get("cross_epic_prerequisites") == "completed":
            return None
        source = tasks.alias("refresh_child_source")
        async with self.db._engine.connect() as conn:
            cross = await conn.scalar(select(source.c.id).select_from(task_dependencies.join(
                source, source.c.id == task_dependencies.c.depends_on_task_id,
            )).where(task_dependencies.c.task_id == task.id, task_dependencies.c.dep_type == "blocks",
                     source.c.parent_task_id.is_distinct_from(task.parent_task_id)
                     | source.c.parent_task_id.is_(None)).limit(1))
        if cross is None:
            return None
        observer = prerequisite_observer(self.db)
        if observer is None:
            raise ValueError("cross-epic prerequisite Git truth is unavailable")
        view = await observer.prerequisite_view(task.project_id, task_id=task.id)
        if not await view.fresh():
            raise ValueError("cross-epic prerequisite target changed")
        refreshed = await self.refresh(task.parent_task_id, dry_run=False)
        if refreshed["outcome"] == "pending":
            raise EpicRefreshPending(f"epic refresh pending: {refreshed['batch_id']}")
        row, target, observed, result = await self.inspect(task.parent_task_id)
        if result["behind"] or not await observed.is_fresh() or not await view.fresh():
            raise ValueError("epic refresh or default branch changed during child preparation")
        if (origin["parent_task_id"] != task.parent_task_id
                or origin["parent_repository_id"] != target.repository_id
                or origin["parent_ref"].removeprefix("refs/heads/") != target.target_ref.removeprefix(
                    "refs/heads/")):
            raise ValueError("child origin no longer names the refreshed epic")
        annotation = {"parent_ref": target.target_ref, "head_sha": result["target_sha"],
                      "default_ref": result["default_ref"], "default_sha": result["default_sha"],
                      "batch_id": refreshed.get("batch_id")}
        async with self.db.immediate() as conn:
            await conn.execute(select(tasks.c.id).where(
                tasks.c.id.in_(sorted({task.id, task.parent_task_id, *view.all_ids})),
            ).order_by(tasks.c.id).with_for_update())
            live_parent = await conn.scalar(select(tasks.c.parent_task_id).where(tasks.c.id == task.id))
            required = set((await conn.execute(select(source.c.id).select_from(
                task_dependencies.join(source, source.c.id == task_dependencies.c.depends_on_task_id),
            ).where(task_dependencies.c.task_id == task.id, task_dependencies.c.dep_type == "blocks",
                    source.c.parent_task_id.is_distinct_from(live_parent)
                    | source.c.parent_task_id.is_(None)))).scalars())
            current = await view.default.verified_on(conn, view.default.evidence)
            if (live_parent != task.parent_task_id or not required or current.keys() != required
                    or any(not proof.satisfied for proof in current.values())):
                raise ValueError("cross-epic prerequisite identity changed during preparation")
            saved = await conn.execute(update(task_branch_origins).where(
                task_branch_origins.c.id == origin["id"],
                task_branch_origins.c.retired_at.is_(None),
                task_branch_origins.c.base_sha == origin["base_sha"],
                task_branch_origins.c.parent_task_id == task.parent_task_id,
                task_branch_origins.c.parent_repository_id == target.repository_id,
                task_branch_origins.c.parent_ref == origin["parent_ref"],
            ).values(base_refresh=annotation))
            if saved.rowcount != 1:
                raise ValueError("child origin changed during refresh")
        return result["target_sha"]
