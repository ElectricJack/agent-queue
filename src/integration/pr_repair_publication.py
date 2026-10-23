"""Adopt a separately filed repair's published passing head into its root PR branch."""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from src.database.tables import projects, repos, tasks
from src.git.identity import task_publish_policy
from src.git.manager import RemoteRefState, _validate_ref, is_valid_git_oid
from src.integration.delivery_branches import repository_protected_branches
from src.integration.lock import CRITICAL_SECTION_SECONDS, BranchLock
from src.integration.models import BranchKey
from src.integration.ownership import BranchOwnershipError
from src.integration.pr_delivery import _OBSERVATION_FAILURES, DeliveredPullRequestClosure

PR_REPAIR_PUBLISHED_EVENT = "integration.pr_repair_published"


class _Refused(Exception):
    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(reason)
        self.outcome = outcome


class PRRepairPublication:
    """Fast-forward one open root PR to a repair's exact head under a server-acquired lease."""

    def __init__(self, db, repository, config, *, event_bus=None):
        self.db, self.repository, self.config = db, repository, config
        self.git, self.event_bus, self.locks = repository.git, event_bus, BranchLock(db)

    async def run(self, task_id, pr_number, *, dry_run=True, expected_head_sha=None,
                  reason=None, operator_id=None) -> dict:
        result = {"task_id": task_id, "pr_number": pr_number}
        try:
            await self._run(result, dry_run, expected_head_sha, reason, operator_id)
        except _Refused as exc:
            return {**result, "outcome": exc.outcome, "reason": str(exc)}
        except (*_OBSERVATION_FAILURES, BranchOwnershipError, TimeoutError) as exc:
            return {**result, "outcome": "blocked", "reason": f"{type(exc).__name__}: {exc}"}
        return result

    async def _run(self, result, dry_run, expected_head_sha, reason, operator_id):
        task = await self.db.get_task(result["task_id"])
        if task is None:
            raise _Refused("not_found", "repair task not found")
        project = await self.db.get_project(task.project_id)
        repo = await self.db.get_repo(project.integration_repository_id or "")
        result["project_id"] = project.id
        if (repo is None or repo.project_id != project.id or not repo.url
                or task.repo_id not in (None, repo.id)
                or project.hierarchical_integration_mode not in {"train", "hierarchy"}):
            raise _Refused("not_eligible", "the project has no designated integration repository")
        source = (task.branch_name or "").removeprefix("refs/heads/")
        resolved = await self.repository._resolve_repository(repo.id)
        await self.repository._ensure_retained_repository(resolved)
        binding = await self.git.bind_github_repository(repo.url)
        client = self.git._github_client(binding)
        url = result["pr_url"] = f"https://github.com/{binding.full_name}/pull/{result['pr_number']}"
        pr_identity = self._pr(await client.pull_request(url), binding)
        branch, old, base_branch, base_oid = pr_identity
        result.update(target_branch=branch, previous_oid=old)
        result.update(base_branch=base_branch, base_oid=base_oid)
        if not source or branch == source:
            raise _Refused("not_eligible", "the repair's own branch publishes through its worker")
        if not dry_run and old != expected_head_sha:
            raise _Refused("changed", "the PR head is not the one the dry run reported")
        store = str(resolved.retained_git_dir)
        tracker = DeliveredPullRequestClosure(self.db, self.repository)
        async with self.git.arepository_transaction(store):
            await self.repository._fetch_all_heads(resolved.retained_git_dir, resolved.origin_url)
            policy = await task_publish_policy(self.config, project, self.db, self.git, task.id,
                                               checkout_path=store, repository_url=repo.url)
            async with asyncio.timeout(CRITICAL_SECTION_SECONDS), self.db.immediate() as conn:
                # Repository reconfiguration and reopen/claim transitions wait for the
                # push and its audit.
                await self.db.lock_hierarchy_project(conn, project.id)
                for table, row_id in ((projects, project.id), (repos, repo.id), (tasks, task.id)):
                    await conn.execute(
                        select(table.c.id).where(table.c.id == row_id).with_for_update())
                if (await self.db.get_task(task.id) != task
                        or await self.db.get_project(project.id) != project
                        or await self.db.get_repo(repo.id) != repo):
                    raise _Refused("changed", "the repair or repository identity changed")
                if branch in {"main", "dev", "staging"} | await repository_protected_branches(
                        conn, repo.id, default_branch=project.repo_default_branch or "main"):
                    raise _Refused("not_eligible",
                                   "default and promotion branches cannot receive repairs")
                completion = await self.db.get_task_completion(task.id)
                if (task.status.value != "COMPLETED" or completion is None
                        or completion.outcome != "pass" or not completion.commits
                        or (completion.branch or "").removeprefix("refs/heads/") != source
                        or not is_valid_git_oid(completion.commits[-1])):
                    raise _Refused("not_eligible",
                                   "the repair has no passing completion with an exact head")
                tip = result["published_oid"] = completion.commits[-1]
                root_ids, live = await tracker._tracking_tasks(
                    project.id, url, branch, repo.id, conn=conn)
                _, repair_live = await tracker._tracking_tasks(
                    project.id, url, source, repo.id, conn=conn)
                root = await conn.scalar(select(tasks.c.id).where(
                    tasks.c.id.in_(root_ids), tasks.c.parent_task_id.is_(None),
                    tasks.c.pr_url == url, tasks.c.status == "COMPLETED",
                    tasks.c.branch_name.in_((branch, "refs/heads/" + branch)),
                ).limit(1))
                if not root:
                    raise _Refused("not_eligible", "no completed root task tracks the PR")
                if live or repair_live:
                    raise _Refused("blocked", "unfinished tasks or attached writers hold "
                                   f"the PR or repair: {', '.join(sorted({*live, *repair_live}))}")
                result["root_task_id"] = root
                if dry_run:
                    await self._heads(store, repo.url, source, branch, old, tip, base_oid)
                    result["outcome"] = "would_publish" if old != tip else "already_published"
                    return
                fence = await self.locks.acquire_on(
                    conn, BranchKey(repository_id=repo.id, branch=branch),
                    f"pr-repair:{task.id}:{completion.id}", role="integration",
                    ttl_seconds=CRITICAL_SECTION_SECONDS)
                async with self.locks.exclusion(fence, conn=conn):
                    if self._pr(await client.pull_request(url), binding) != pr_identity:
                        raise _Refused("changed", "the PR changed before publication")
                    await self._heads(store, repo.url, source, branch, old, tip, base_oid)
                    result["outcome"] = "already_published"
                    if old != tip:
                        if await self.git.apush_validated_delivery(
                            store, old, tip, branch, expected_remote_oid=old,
                            repository_url=repo.url, identity_policy=policy,
                            inherited_oids=(old, base_oid), event_bus=self.event_bus,
                            project_id=project.id,
                        ) != tip:
                            raise _Refused("blocked", "the push did not confirm the repair head")
                        result["outcome"] = "published"
                    await self.db.log_event(
                        PR_REPAIR_PUBLISHED_EVENT, project_id=project.id, task_id=task.id,
                        payload=json.dumps({**result, "completion_id": completion.id,
                                            "operator_id": operator_id, "reason": reason}),
                        conn=conn)
                await self.locks.release(fence, conn=conn)

    @staticmethod
    def _pr(pull, binding):
        head, base = pull.get("head") or {}, pull.get("base") or {}
        branch, oid = head.get("ref"), head.get("sha")
        base_branch, base_oid = base.get("ref"), base.get("sha")
        if (pull.get("state") != "open" or pull.get("merged_at")
                or (head.get("repo") or {}).get("id") != binding.repository_id
                or (base.get("repo") or {}).get("id") != binding.repository_id
                or not isinstance(branch, str) or branch.startswith("refs/")
                or not is_valid_git_oid(oid)
                or not isinstance(base_branch, str) or base_branch.startswith("refs/")
                or not is_valid_git_oid(base_oid)):
            raise _Refused("not_eligible", "the PR must be open with a head in the repository")
        _validate_ref(branch)
        _validate_ref(base_branch)
        return branch, oid, base_branch, base_oid

    async def _heads(self, store, url, source, target, old, tip, base_oid):
        for ref, expected in ((source, tip), (target, old)):
            remote = await self.git.als_remote_ref(store, ref, repository_url=url)
            if remote.state is not RemoteRefState.PRESENT:
                raise _Refused("blocked", f"remote branch {ref} is unavailable")
            if remote.oid != expected:
                raise _Refused("changed", f"remote branch {ref} moved to {remote.oid}")
        # Only authenticated, published base history is inherited, never arbitrary
        # local refs or the repair's own already-published commits.
        await self.git._arun(["cat-file", "-e", f"{base_oid}^{{commit}}"], cwd=store)
        if await self.git.ais_ancestor(store, old, tip, strict=True) is not True:
            raise _Refused("not_eligible", "the repair head does not descend from the PR head")
