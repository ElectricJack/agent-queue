"""Deliver a passed task's pushed branch by hand (``task_deliver``).

The supervisor's control for a task whose worker pushed its branch and closed
``pass``, but whose close stopped at git verification -- agile-flare and
stark-vault blocked on "Could not authorize PR repository" because their
projects push to bare repositories on disk.  It does what ``direct``
integration would have done: merge the task branch into the repository's
default branch and complete the task.

Every step happens in a private bare repository under the daemon's data
directory, created for the call and removed afterwards.  No worker slot and
no operator checkout is switched, reset or merged into.  The push is a plain
fast-forward of the default branch fetched moments earlier: if anyone moved
it since, the push is refused rather than overwritten, and the reserved-path
gate of :meth:`GitManager.apush_validated_delivery` applies as it does to
every other delivery.

Refused, with nothing written: a task that is not ``BLOCKED``, one with open
children, a project whose own publisher owns delivery (development,
hierarchy, train), a task in ``pull_request`` mode on a repository that can
host its pull request (merge the PR instead), a branch that was never pushed,
and a merge that conflicts.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from src.git.manager import GitError
from src.integration.delivery_path import (
    MANAGED_MODES,
    effective_integration_mode,
    lacks_pull_request_host,
    task_repository_url,
)
from src.models import INTEGRATION_MODE_PULL_REQUEST, TaskStatus

_IDENTITY = {
    "GIT_AUTHOR_NAME": "Agent Queue",
    "GIT_AUTHOR_EMAIL": "aq@localhost",
    "GIT_COMMITTER_NAME": "Agent Queue",
    "GIT_COMMITTER_EMAIL": "aq@localhost",
    "LC_ALL": "C",
}


def _refused(outcome: str, error: str, **extra: Any) -> dict:
    return {"success": False, "outcome": outcome, "error": error, **extra}


class ManualDelivery:
    """Merge one BLOCKED task's pushed branch into its default branch."""

    def __init__(self, db: Any, git: Any, *, data_dir: str | Path) -> None:
        self.db = db
        self.git = git
        self.root = Path(data_dir) / "manual-delivery"

    async def deliver(
        self,
        task_id: str,
        *,
        reason: str,
        operator_id: str,
        default_mode: str,
        dry_run: bool = False,
    ) -> dict:
        if not reason or not reason.strip():
            return _refused("invalid", "a delivery reason is required")
        task = await self.db.get_task(task_id)
        if task is None:
            return _refused("not_found", f"task '{task_id}' not found")
        if task.status != TaskStatus.BLOCKED:
            return _refused(
                "not_blocked",
                f"only a BLOCKED task can be delivered by hand; {task_id} is "
                f"{task.status.value}",
            )
        project = await self.db.get_project(task.project_id)
        if project is None:
            return _refused("not_found", f"project '{task.project_id}' not found")
        managed = getattr(project, "hierarchical_integration_mode", None)
        if managed in MANAGED_MODES:
            return _refused(
                "integration_managed",
                f"{task.project_id} delivers through {managed} integration; use its "
                "controls (`aq integration status`) instead",
            )
        open_children = await self.db.open_children(task_id)
        if open_children:
            return _refused(
                "open_children",
                f"task {task_id} has open children: {', '.join(open_children)}",
                open_children=open_children,
            )
        url = await task_repository_url(self.db, task, project)
        mode, source = await effective_integration_mode(
            self.db, task, default_mode=default_mode
        )
        if mode == INTEGRATION_MODE_PULL_REQUEST and not lacks_pull_request_host(url):
            return _refused(
                "pull_request_mode",
                f"{task_id} integrates by pull request (from {source}) on a repository "
                "that can host one; open or merge its PR (`aq pr merge`) instead",
            )
        branch = (task.branch_name or "").removeprefix("refs/heads/")
        if not branch:
            return _refused("no_branch", f"task {task_id} has no recorded branch")
        if not url:
            return _refused("no_repository", f"project {task.project_id} has no repository")
        repo = await self.db.get_repo(task.repo_id) if task.repo_id else None
        default_branch = (
            (repo.default_branch if repo else None) or project.repo_default_branch or "main"
        )

        self.root.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix=f"{task_id}-", dir=self.root))
        try:
            return await self._deliver_in(
                workdir, task, url, branch, default_branch,
                reason=reason.strip(), operator_id=operator_id, dry_run=dry_run,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, True)

    async def _git(self, cwd: Path, *args: str, stdin: str | None = None,
                   env: dict[str, str] | None = None):
        return await self.git.arun_git_result(
            list(args), cwd=str(cwd), stdin=stdin, env=env or {"LC_ALL": "C"}
        )

    async def _rev(self, cwd: Path, ref: str) -> str | None:
        result = await self._git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if result.returncode:
            return None
        return result.stdout.strip() or None

    async def _deliver_in(
        self,
        workdir: Path,
        task: Any,
        url: str,
        branch: str,
        default_branch: str,
        *,
        reason: str,
        operator_id: str,
        dry_run: bool,
    ) -> dict:
        repo_dir = workdir / "repository.git"
        for cwd, args in (
            (workdir, ("init", "--bare", "--quiet", str(repo_dir))),
            (repo_dir, ("remote", "add", "origin", url)),
        ):
            result = await self._git(cwd, *args)
            if result.returncode:
                return _refused("git_error", (result.stderr or f"git {args[0]} failed").strip())
        try:
            await self.git.afetch_origin(str(repo_dir), repository_url=url, all_heads=True)
        except GitError as exc:
            return _refused("fetch_failed", f"could not fetch {url}: {exc}")

        base_ref = f"refs/remotes/origin/{default_branch}"
        head_ref = f"refs/remotes/origin/{branch}"
        base = await self._rev(repo_dir, base_ref)
        if base is None:
            return _refused("no_default_branch", f"{url} has no branch {default_branch}")
        head = await self._rev(repo_dir, head_ref)
        if head is None:
            return _refused(
                "branch_not_pushed",
                f"{branch} is not on {url}; the worker's commits were never pushed",
            )
        plan = {
            "task_id": task.id,
            "repository_url": url,
            "branch": branch,
            "default_branch": default_branch,
            "base_sha": base,
            "head_sha": head,
        }

        if await self.git.ais_ancestor(str(repo_dir), head_ref, base_ref, strict=True):
            plan.update(method="already_delivered", delivered_sha=base)
        elif await self.git.ais_ancestor(str(repo_dir), base_ref, head_ref, strict=True):
            plan.update(method="fast_forward", delivered_sha=head)
        else:
            merged = await self._git(
                repo_dir, "merge-tree", "--write-tree", "--name-only", "--no-messages",
                base, head,
            )
            lines = [line for line in merged.stdout.splitlines() if line.strip()]
            if merged.returncode == 1:
                return _refused(
                    "conflict",
                    f"{branch} conflicts with {default_branch}; resolve it on the branch "
                    "and push, then deliver again",
                    conflict_files=lines[1:], **plan,
                )
            if merged.returncode or not lines:
                return _refused(
                    "git_error", (merged.stderr or "git merge-tree failed").strip(), **plan
                )
            message = (
                f"Merge branch '{branch}' into {default_branch}\n\n"
                f"Delivered by hand for task {task.id} ({operator_id}): {reason}\n"
            )
            commit = await self._git(
                repo_dir, "commit-tree", lines[0], "-p", base, "-p", head,
                stdin=message, env=_IDENTITY,
            )
            if commit.returncode or not commit.stdout.strip():
                return _refused(
                    "git_error", (commit.stderr or "git commit-tree failed").strip(), **plan
                )
            plan.update(method="merge", delivered_sha=commit.stdout.strip())

        if dry_run:
            return {"success": True, "outcome": "would_deliver", **plan}
        if plan["method"] != "already_delivered":
            try:
                # The lease is the default branch as fetched: a remote that
                # moved since is refused, never overwritten.
                await self.git.apush_validated_delivery(
                    str(repo_dir), base, plan["delivered_sha"], default_branch,
                    expected_remote_oid=base, repository_url=url, project_id=task.project_id,
                )
            except GitError as exc:
                return _refused("push_failed", f"push to {default_branch} refused: {exc}", **plan)

        delivered_at = time.time()
        await self.db.set_task_meta(task.id, "merged_at", delivered_at)
        await self.db.set_task_meta(
            task.id,
            "manual_delivery",
            {**plan, "operator": operator_id, "reason": reason, "delivered_at": delivered_at},
        )
        try:
            await self.db.transition_task(
                task.id, TaskStatus.COMPLETED, context="operator_delivery"
            )
        except Exception as exc:  # noqa: BLE001 - the push already happened
            return _refused(
                "delivered_not_completed",
                f"{plan['delivered_sha']} is on {default_branch}, but completing "
                f"{task.id} failed: {exc}",
                **plan,
            )
        await self.db.log_event(
            "task_delivered_by_hand",
            project_id=task.project_id,
            task_id=task.id,
            payload=f"{plan['method']} {plan['delivered_sha']} by {operator_id}: {reason}"[:500],
        )
        return {"success": True, "outcome": "delivered", **plan}
