"""Current daemon ports for author-assisted completion, scoped to one proposal."""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from src.git.manager import GitError
from src.integration.close_merge import CloseMerge, ClosePolicy, CloseStore, policy_digest
from src.integration.gitops import GitOperations, RetainedRepository
from src.integration.models import CloseMergePolicy
from src.integration.regeneration import regeneration_wait
from src.integration.train_sources import TRAIN_MODES, LeasedPublish

#: How long one close request waits for its shared regeneration. Past it the
#: run continues detached and the close returns its durable operation id; this
#: stays well inside the CLI's close read timeout.
CLOSE_REGENERATION_WAIT_SECONDS = 300.0


async def current_policy(db, project):
    """Read the same live project projection as the git-first train.

    A disabled train leaves ordinary source publication and completion available.
    """
    from src.integration.train_policy import load_train_policy

    view = await load_train_policy(db, project.id)
    raw = project.hierarchical_integration_policy or {}
    close = CloseMergePolicy.model_validate(raw.get("close_merge") or {})
    enabled = (view.enabled and project.status.value == "ACTIVE"
               and project.hierarchical_integration_mode in TRAIN_MODES
               and not project.hierarchical_integration_draining)
    return ClosePolicy(enabled, close.author_assist, close.author_budget_seconds,
                       policy_digest(raw), (raw.get("parent") or {}).get("required_checks"))


async def proposal(handler, task, session, args):
    """Called only after ordinary close's claim and deliverable guards."""
    if args.get("outcome") != "pass" or session is None or not task.parent_task_id:
        return None
    project = await handler.db.get_project(task.project_id)
    if not project or not (await current_policy(handler.db, project)).enabled:
        await CloseStore(handler.db).retire_previous(task.id, session.id, task.claim_epoch)
        return None
    parent = await handler.db.get_task(task.parent_task_id)
    repo = await handler.db.get_repo(task.repo_id) if task.repo_id else None
    if (not parent or not repo or parent.repo_id != task.repo_id or not parent.branch_name
            or parent.branch_name.removeprefix("refs/heads/") == repo.default_branch
            or task.repo_id != project.integration_repository_id):
        return None  # Root/default publication remains the existing train's job.
    git = handler.orchestrator.git
    path = session.work_dir
    if await git.ahas_uncommitted_changes(path, strict=True):
        raise GitError("commit the clean final source before author-assisted close")
    source = await git.arev_parse(path, "HEAD")
    if args.get("commit") and args["commit"] != source:
        raise GitError("close commit differs from final source")
    if await git.aget_current_branch(path, strict=True) != task.branch_name.removeprefix(
            "refs/heads/"):
        raise GitError("source checkout is not on its task branch")
    remote = await git.als_remote_ref(path, task.branch_name)
    if remote.oid != source:
        raise GitError("publish the exact final source with aq git push before close")
    await CloseStore(handler.db).retire_previous(task.id, session.id, task.claim_epoch, source)
    return await CloseStore(handler.db).proposal(task=task, session=session, source=source,
                                                parent=parent, args=args)


class DaemonClose:
    def __init__(self, orch):
        self.orch, self.db, self.git = orch, orch.db, orch.git
        self.store = CloseStore(self.db)
        self.publish = LeasedPublish(self.db, self.git, holder="author-close")
        self.gitops = GitOperations(self.db, git=self.git, repository=None, authority=None)

    async def policy(self, row):
        return await current_policy(self.db, await self.db.get_project(row["project_id"]))

    async def authorize(self, row):
        from src.operator_decisions import OperatorDecisions

        task = await self.db.get_task(row["task_id"])
        session = await self.db.get_session(row["session_id"])
        parent = await self.db.get_task(row["target_task_id"])
        repo = await self.db.get_repo(row["repository_id"])
        if (not task or not session or not parent or not repo
                or task.claim_epoch != row["claim_epoch"] or session.task_id != task.id
                or task.assigned_agent_id != session.agent_id or session.desired_state != "running"
                or task.status.value != "IN_PROGRESS" or task.parent_task_id != parent.id
                or parent.repo_id != repo.id or task.repo_id != repo.id
                or parent.project_id != row["project_id"] or repo.project_id != row["project_id"]
                or "refs/heads/" + parent.branch_name.removeprefix("refs/heads/") != row["target_ref"]
                or row["target_ref"] == "refs/heads/" + repo.default_branch):
            raise GitError("close delegation no longer matches the live claim and parent")
        if parent.status.value in {"PAUSED", "FAILED", "CANCELLED"}:
            raise GitError("parent has an explicit stop")
        decisions = OperatorDecisions(self.db)
        for kind, identity in (("task", task.id), ("task", parent.id)):
            if await decisions.holds(kind, identity):
                raise GitError("explicit human hold prevents parent publication")
        from src.integration.batches import withheld_sources_query

        async with self.db._engine.connect() as conn:
            withheld = (await conn.execute(withheld_sources_query(SimpleNamespace(
                project_id=row["project_id"], repository_id=row["repository_id"],
                target_ref=row["target_ref"],
            )))).mappings().all()
        if any(item["task_id"] == task.id and item["source_sha"] == row["source_oid"]
               for item in withheld):
            raise GitError("explicit batch abort withholds this exact source")
        if (await self.git.ahas_uncommitted_changes(session.work_dir, strict=True)
                or await self.git.arev_parse(session.work_dir, "HEAD") != row["source_oid"]
                or (await self.git.als_remote_ref(session.work_dir, task.branch_name)).oid
                != row["source_oid"]):
            raise GitError("frozen source changed; close the new source generation")

    async def observe(self, row):
        from src.integration.repository_git import RepositoryGit

        reader = RepositoryGit(self.db, data_dir=self.orch.config.data_dir, git_manager=self.git)
        resolved = await reader._resolve_repository(row["repository_id"])
        await reader._ensure_retained_repository(resolved)
        async with self.git.arepository_transaction(str(resolved.retained_git_dir)):
            await reader._fetch_all_heads(resolved.retained_git_dir, resolved.origin_url)
        binding = await self.orch.github_repository_binding_resolver(resolved.repo)
        repo = RetainedRepository(row["repository_id"], resolved.retained_git_dir, binding,
                                  resolved.repo.default_branch)
        await self.gitops.validate_repository(repo)
        parent = await self.gitops.remote(repo, row["target_ref"])
        if not parent:
            raise GitError("parent remote is unavailable")
        await self.gitops.exact(repo, parent)
        await self.gitops.exact(repo, row["source_oid"])
        from src.warm_workspaces import primary_store

        store = await primary_store(self.db, self.git, row["project_id"], row["repository_id"],
                                    repo.store, parent)
        if store != repo.store:
            await self.git._arun(["fetch", "--no-tags", "--no-write-fetch-head", str(repo.store),
                                 row["source_oid"]], cwd=str(store))
            repo = RetainedRepository(repo.repository_id, store, repo.binding, repo.default_branch)
            await self.gitops.validate_repository(repo)
        return repo, parent

    async def contains(self, repo, source, target):
        await self.gitops.exact(repo, source)
        await self.gitops.exact(repo, target)
        return await self.gitops.is_ancestor(repo, source, target)

    async def prepare(self, row, repo, store):
        from src.warm_workspaces import WarmWorkspacePool

        source = row["candidate_oid"] or row["source_oid"]
        base = await self.gitops.run(repo, "merge-base", row["base_oid"], source)
        with regeneration_wait(CLOSE_REGENERATION_WAIT_SECONDS):
            merged = await self.gitops.merge_sources(repo, row["base_oid"], [SimpleNamespace(
                task_id=row["task_id"], head_sha=source, base_sha=base)], created_at=time.time())
        pool = WarmWorkspacePool(self.git, repo.store,
                                 Path(self.orch.config.data_dir) / "close-candidates")

        async def released(manifest, path, head):
            old = await store.get(manifest["owner"].get("operation_id"))
            task = await self.db.get_task(old["task_id"]) if old else None
            # Reuse only a settled, exactly preserved candidate with no author.
            return bool(old and old["state"] == "finished" and old["candidate_oid"] == head
                        and task and task.status.value == "COMPLETED")

        head = merged["head"] if merged["outcome"] == "merged" else row["base_oid"]
        async with self.git.arepository_transaction(str(repo.store)):
            path = await pool.acquire({"operation_id": row["id"], "attempt": row["attempts"]},
                                      head, released=released)
        row = await store.save(row, candidate_path=str(path), candidate_oid=head)
        if merged["outcome"] != "merged":
            # Leave the real conflicted index and MERGE_HEAD with the author.
            await self.git.arun_git_result(
                [*self.git.resolve_commit_identity().config_args(),
                 "merge", "--no-ff", "--no-commit", source], cwd=str(path))
        else:
            row = await store.save(row, evidence={"regenerated_oid": head})
        return row

    async def resolved(self, row, repo):
        path = row["candidate_path"]
        if await self.git.ahas_uncommitted_changes(path, strict=True):
            return None
        merge = await self.git.arun_git_result(["rev-parse", "--verify", "MERGE_HEAD"], cwd=path)
        if merge.returncode == 0:
            return None
        head = await self.git.arev_parse(path, "HEAD")
        if not await self.contains(repo, row["source_oid"], head):
            return None
        return head

    async def regenerate(self, row, repo, head):
        from src.integration.regeneration import generated_paths, regenerated_tree

        if not await generated_paths(self.git, repo.store, head):
            return head
        with regeneration_wait(CLOSE_REGENERATION_WAIT_SECONDS):
            tree = await regenerated_tree(self.git, repo.store,
                                          await self.git.atree_sha(str(repo.store), head),
                                          command=repo.regenerate,
                                          timeout_seconds=repo.regenerate_timeout_seconds)
        if tree != await self.git.atree_sha(str(repo.store), head):
            head = await self.gitops.run(repo, *self.git.resolve_commit_identity().config_args(),
                                        "commit-tree", tree, "-p", head, "-m",
                                        "Regenerate author close candidate")
            await self.git._arun(["reset", "--hard", head], cwd=row["candidate_path"])
        await self.gitops.run(repo, "update-ref", row["candidate_ref"], head)
        return head

    async def check(self, row, repo, policy):
        from src.integration.close_checks import check_candidate

        return await check_candidate(self, row, repo, policy)

    async def budget(self, row, policy):
        from src.database.queries.wait_queries import register_wait
        from src.waits.conditions import Scope, SleepDecision
        from src.waits.policy import effective_policy
        from src.waits.registry import build_registry

        async with self.db.immediate() as conn:
            receipt = await register_wait(
                conn, scope=Scope(row["project_id"], "integration", row["id"], principal="service"),
                condition={"kind": "close_merge", "operation_id": row["id"],
                           "budget_seconds": policy.author_budget_seconds},
                idempotency_key="author-budget", registry=build_registry(),
                policy=effective_policy(self.orch.config, row["project_id"]),
                decide_sleep=lambda *_: SleepDecision("in_place", "author owns candidate"),
                read_source_watermark=self.db.wait_source_watermark, now=time.time(),
            )
        return receipt["wait_id"]

    async def handoff(self, row, *, enabled):
        # Existing train/in-place repair owns delivery. The retained row/ref is
        # exact input evidence; source completion makes it eligible normally.
        if enabled:
            await self.orch.bus.emit("integration.readiness_changed", {
                "project_id": row["project_id"], "repository_id": row["repository_id"],
                "operation_id": row["id"], "reason": "author_close_handoff",
            })

    async def run(self, operation_id):
        return await CloseMerge(self.store, self).run(operation_id)
