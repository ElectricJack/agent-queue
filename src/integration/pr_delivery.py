"""Prove an open pull request's work is on the default branch, then close it.

GitHub marks a PR merged by itself when its exact head becomes reachable from
the base branch, which is how train-delivered PRs and the legacy PRs a carrier
root merges close.  A change delivered under other commits (cherry-picked,
rebased, re-delivered) leaves its PR open, and an untracked operator branch
has no task for any train control to act on.  This control closes such a PR
only on Git proof, tried in this order:

* ``ancestor`` -- the PR head is reachable from the default branch;
* ``patch_equivalent`` -- the PR is a linear series and every commit has a
  patch-identical commit on the default branch (``rev-list --cherry-mark``);
* ``content_equivalent`` -- merging the PR head into the default branch
  changes nothing.

Anything else is ``undelivered`` and stays open.  The dry run only reads;
applying needs the head it reported and a reason, proves again under the
retained repository's lock, excludes unfinished tasks and attached writers,
posts one marked proof comment, re-reads the head,
closes the PR and records ``integration.pr_closed_delivered``.  Tasks,
completions, receipts and branches are never changed.
"""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import and_, or_, select

from src.database.queries.integration_state_queries import session_attached_clause
from src.database.tables import sessions, tasks, workspaces
from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitError, RemoteRefState, is_valid_git_oid
from src.integration.promotion_contracts import PromotionError

PR_CLOSED_DELIVERED_EVENT = "integration.pr_closed_delivered"
UNDELIVERED_FILE_LIMIT = 20

ANCESTOR = "ancestor"
PATCH_EQUIVALENT = "patch_equivalent"
CONTENT_EQUIVALENT = "content_equivalent"

#: What reading the repository, GitHub or Git can raise; each one is ``blocked``.
_OBSERVATION_FAILURES = (GitError, GitHubAccessError, PromotionError, OSError, ValueError)


class DeliveredPullRequestClosure:
    """Close one open PR of the designated repository once Git proves its work landed."""

    def __init__(self, db, promotion, *, clock=time.time) -> None:
        self.db = db
        self.promotion = promotion
        self.git = promotion.git
        self.clock = clock

    async def run(
        self,
        project_id: str,
        pr_number: int,
        *,
        dry_run: bool = True,
        expected_head_sha: str | None = None,
        reason: str | None = None,
        operator_id: str | None = None,
        aq_only: bool = False,
    ) -> dict[str, Any]:
        observed, client = await self._observe(project_id, pr_number)
        if aq_only and observed.get("branch") and not observed["branch"].startswith("aq/"):
            return {**observed, "outcome": "not_eligible", "reason": "PR is outside aq/"}
        if observed["outcome"] != "would_close" or dry_run:
            return observed
        if observed["head_sha"] != expected_head_sha:
            return {
                **observed,
                "outcome": "changed",
                "reason": "the PR head is not the one the dry run reported; run the dry run again",
            }
        marker = _marker(pr_number, observed["head_sha"])
        try:
            async with self.db.immediate() as conn:
                await self.db.lock_hierarchy_project(conn, project_id)
                task_ids, live_task_ids = await self._tracking_tasks(
                    project_id, observed["pr_url"], observed["branch"],
                    observed["repository_id"], conn=conn,
                )
                observed["task_ids"] = task_ids
                if live_task_ids:
                    return self._held_by_tasks(observed, live_task_ids)
                if not await client.has_comment_marker(number=pr_number, marker=marker):
                    await client.comment_pull_request(
                        number=pr_number, marker=marker, body=_comment(marker, observed)
                    )
                current = await client.exact_pull_request(number=pr_number)
                if current is None or current["head_sha"] != observed["head_sha"]:
                    return {
                        **observed,
                        "outcome": "changed",
                        "reason": "the PR head moved before it was closed; run the dry run again",
                    }
                if current["state"] != "closed":
                    await client.close_pull_request(number=pr_number)
                await self.db.log_event(
                    PR_CLOSED_DELIVERED_EVENT,
                    project_id=project_id,
                    task_id=task_ids[0] if len(task_ids) == 1 else None,
                    payload=json.dumps(
                        {
                            "pr_number": pr_number,
                            "pr_url": observed["pr_url"],
                            "branch": observed["branch"],
                            "head_sha": observed["head_sha"],
                            "target_sha": observed["target_sha"],
                            "proof": observed["proof"],
                            "task_ids": task_ids,
                            "operator_id": operator_id,
                            "reason": reason,
                            "at": self.clock(),
                        }
                    ),
                    conn=conn,
                )
        except _OBSERVATION_FAILURES as exc:
            return {**observed, "outcome": "blocked", "reason": f"GitHub write failed: {exc}"}
        return {**observed, "outcome": "closed"}

    async def _observe(self, project_id: str, pr_number: int) -> tuple[dict[str, Any], Any]:
        """Read the PR and prove its delivery; the GitHub client serves the apply path."""
        base: dict[str, Any] = {"project_id": project_id, "pr_number": pr_number}
        project = await self.db.get_project(project_id)
        if project is None:
            return {**base, "outcome": "not_found", "reason": "project not found"}, None
        repository_id = getattr(project, "integration_repository_id", None)
        repo = await self.db.get_repo(repository_id) if repository_id else None
        if repo is None or repo.project_id != project_id or not repo.url:
            return {
                **base,
                "outcome": "not_eligible",
                "reason": "the project has no designated integration repository",
            }, None
        default_branch = repo.default_branch.removeprefix("refs/heads/")
        try:
            resolved = await self.promotion._resolve_repository(repo.id)
            await self.promotion._ensure_retained_repository(resolved)
            binding = await self.git.bind_github_repository(repo.url)
            client = self.git._github_client(binding)
            pr_url = f"https://github.com/{binding.full_name}/pull/{pr_number}"
            base["pr_url"] = pr_url
            pull = await client.pull_request(pr_url)
        except _OBSERVATION_FAILURES as exc:
            return {**base, "outcome": "blocked", "reason": f"the PR could not be read: {exc}"}, None
        head, target = pull.get("head"), pull.get("base")
        head_repo = head.get("repo") if isinstance(head, dict) else None
        if (
            not isinstance(head, dict)
            or not isinstance(target, dict)
            or not is_valid_git_oid(head.get("sha"))
            or not isinstance(head.get("ref"), str)
            or not head["ref"]
        ):
            return {**base, "outcome": "blocked", "reason": "the PR identity was malformed"}, None
        base.update(
            branch=head["ref"],
            head_sha=head["sha"],
            state="merged" if pull.get("merged_at") else pull.get("state"),
            repository_id=repo.id,
        )
        task_ids, live_task_ids = await self._tracking_tasks(
            project_id, pr_url, head["ref"], repo.id
        )
        base["task_ids"] = task_ids
        if pull.get("state") != "open":
            return {
                **base,
                "outcome": "nothing_to_close",
                "reason": f"the PR is already {base['state']}",
            }, None
        if target.get("ref") != default_branch:
            return {
                **base,
                "outcome": "not_eligible",
                "reason": f"the PR does not target the default branch {default_branch}",
            }, None
        if not isinstance(head_repo, dict) or head_repo.get("id") != binding.repository_id:
            return {
                **base,
                "outcome": "not_eligible",
                "reason": "the PR head is not a branch of the designated repository",
            }, None
        if live_task_ids:
            return self._held_by_tasks(base, live_task_ids), None
        store = str(resolved.retained_git_dir)
        try:
            async with self.git.arepository_transaction(store):
                await self.promotion._fetch_all_heads(
                    resolved.retained_git_dir, resolved.origin_url
                )
                remote = await self.git.als_remote_ref(store, head["ref"])
                main = await self.git.als_remote_ref(store, default_branch)
                if remote.state is not RemoteRefState.PRESENT or remote.oid != head["sha"]:
                    return {
                        **base,
                        "outcome": "changed",
                        "reason": "the remote head branch is not the PR head",
                    }, None
                if main.state is not RemoteRefState.PRESENT:
                    return {
                        **base, "outcome": "blocked", "reason": "default branch is unavailable",
                    }, None
                base["target_sha"] = main.oid
                proof, undelivered = await self._prove(store, main.oid, head["sha"])
        except _OBSERVATION_FAILURES as exc:
            return {**base, "outcome": "blocked", "reason": f"Git proof failed: {exc}"}, None
        if proof is None:
            return {
                **base,
                "outcome": "undelivered",
                "undelivered": undelivered,
                "reason": "no commit or content of the PR is proven on the default branch",
            }, None
        return {**base, "outcome": "would_close", "proof": proof}, client

    async def _prove(
        self, store: str, target: str, head: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """The first proof that reaches *target*, else what merging *head* would change."""
        if (await self._git(store, "merge-base", "--is-ancestor", head, target)).returncode == 0:
            return {"kind": ANCESTOR}, None
        merge_base = (await self._git(store, "merge-base", target, head)).stdout.strip()
        if not is_valid_git_oid(merge_base):
            raise ValueError("the PR head has no merge-base with the default branch")
        commits = await self._git(store, "rev-list", "--reverse", f"{merge_base}..{head}")
        merges = await self._git(store, "rev-list", "--min-parents=2", f"{merge_base}..{head}")
        series = commits.stdout.split()
        if commits.returncode or merges.returncode or not series:
            raise ValueError("the PR commit series could not be read")
        # Patch identity proves a linear series only: a merge commit's
        # resolution is not a patch either side carries.
        if not merges.stdout.strip():
            marks = await self._git(
                store, "rev-list", "--cherry-mark", "--right-only", "--no-merges",
                f"{target}...{head}",
            )
            if marks.returncode == 0 and _all_marked(marks.stdout, len(series)):
                upstream = await self._git(
                    store, "rev-list", "--cherry-mark", "--left-only", "--no-merges",
                    f"{target}...{head}",
                )
                return {
                    "kind": PATCH_EQUIVALENT,
                    "merge_base": merge_base,
                    "commits": series,
                    "equivalent_commits": [
                        line[1:] for line in upstream.stdout.splitlines() if line.startswith("=")
                    ],
                }, None
        target_tree = (await self._git(store, "rev-parse", f"{target}^{{tree}}")).stdout.strip()
        merged = await self._git(store, "merge-tree", "--write-tree", target, head)
        tree = (merged.stdout.splitlines() or [""])[0].strip()
        if merged.returncode == 0 and tree == target_tree:
            return {"kind": CONTENT_EQUIVALENT, "merge_base": merge_base, "commits": series}, None
        undelivered: dict[str, Any] = {"merge_base": merge_base, "commits": series}
        if merged.returncode == 1:
            return None, {
                **undelivered,
                "conflict": True,
                "summary": "merging the PR into the default branch conflicts",
            }
        if merged.returncode or not is_valid_git_oid(tree):
            raise ValueError((merged.stderr or "merge-tree failed").strip())
        stat = await self._git(store, "diff", "--shortstat", target_tree, tree)
        names = await self._git(store, "diff", "--name-only", target_tree, tree)
        files = [line for line in names.stdout.splitlines() if line]
        return None, {
            **undelivered,
            "conflict": False,
            "summary": stat.stdout.strip(),
            "file_count": len(files),
            "files": files[:UNDELIVERED_FILE_LIMIT],
        }

    async def _git(self, store: str, *args: str):
        return await self.git.arun_git_result(
            list(args), cwd=store, env={"LC_ALL": "C"}, lock_held=True
        )

    async def _tracking_tasks(
        self, project_id: str, pr_url: str, branch: str, repository_id: str, *, conn=None
    ) -> tuple[list[str], list[str]]:
        if conn is None:
            async with self.db._engine.connect() as reader:
                return await self._tracking_tasks_on(
                    reader, project_id, pr_url, branch, repository_id
                )
        return await self._tracking_tasks_on(
            conn, project_id, pr_url, branch, repository_id, lock=True
        )

    async def _tracking_tasks_on(
        self, conn, project_id, pr_url, branch, repository_id, *, lock=False
    ) -> tuple[list[str], list[str]]:
        live_writer = select(sessions.c.id).where(
            sessions.c.task_id == tasks.c.id, session_attached_clause()
        ).exists()
        locked_workspace = select(workspaces.c.id).where(
            workspaces.c.locked_by_task_id == tasks.c.id
        ).exists()
        live = or_(
            tasks.c.status.not_in(("COMPLETED", "FAILED")),
            tasks.c.assigned_agent_id.is_not(None), live_writer, locked_workspace,
        )
        statement = select(tasks.c.id, live.label("live")).where(
            tasks.c.project_id == project_id,
            or_(
                tasks.c.pr_url == pr_url,
                and_(
                    or_(tasks.c.repo_id == repository_id, tasks.c.repo_id.is_(None)),
                    tasks.c.branch_name.in_((branch, "refs/heads/" + branch)),
                ),
            ),
        ).order_by(tasks.c.id)
        if lock:
            statement = statement.with_for_update(of=tasks)
        rows = (await conn.execute(statement)).all()
        return [row.id for row in rows], [row.id for row in rows if row.live]

    @staticmethod
    def _held_by_tasks(observed: dict[str, Any], task_ids: list[str]) -> dict[str, Any]:
        return {
            **observed, "outcome": "not_eligible", "live_task_ids": task_ids,
            "reason": "unfinished tasks or attached writers still hold the PR",
        }


def _all_marked(output: str, expected: int) -> bool:
    """Every PR commit is listed, and every one has a patch-identical twin."""
    lines = [line for line in output.splitlines() if line]
    return len(lines) == expected and all(line.startswith("=") for line in lines)


def _marker(pr_number: int, head_sha: str) -> str:
    return f"<!-- aq-delivered-pr:{pr_number}:{head_sha} -->"


def train_delivery_comment(
    batch_id: str, source_sha: str, promoted_sha: str, target_ref: str,
    repair_commits: list[str],
) -> tuple[str, str]:
    """One replayable promotion comment, including fixes beyond the PR head.

    Cleanup posts this even if GitHub already marked the PR merged. GitHub
    owns that state; posting the audit comment never closes or merges a PR.
    """
    marker = f"<!-- aq-promoted-batch:{batch_id}:{source_sha}:{promoted_sha} -->"
    body = (
        f"{marker}\nDelivered by integration batch `{batch_id}`: source `{source_sha}` "
        f"promoted at `{promoted_sha}` to `{target_ref}`."
    )
    body += "\n\nIntegration repair commits included in the promoted batch:\n" + (
        "\n".join(f"- `{sha}`" for sha in repair_commits)
        if repair_commits else "No integration repair commits."
    )
    return marker, body


def _comment(marker: str, observed: dict[str, Any]) -> str:
    """The public proof; the operator's audit reason stays in the event."""
    proof = observed["proof"]
    lines = [
        marker,
        (
            f"Closing as delivered: head `{observed['head_sha']}` is proven on the default "
            f"branch at `{observed['target_sha']}` ({proof['kind']})."
        ),
    ]
    if proof["kind"] == PATCH_EQUIVALENT:
        lines.append(
            "Patch-identical default-branch commits: "
            + ", ".join(f"`{sha}`" for sha in proof["equivalent_commits"])
            + "."
        )
    elif proof["kind"] == CONTENT_EQUIVALENT:
        lines.append("Merging this head into the default branch changes nothing.")
    return "\n".join(lines)
