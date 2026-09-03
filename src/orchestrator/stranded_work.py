"""Never lose a worker's commits — the push-before-you-close rule.

The outage this exists to prevent (task ``solid-harbor.43``, 2026-09-03): a
worker committed five commits — about 5,800 lines — in its slot worktree,
never pushed them, and closed the task ``blocked``.  The close was accepted,
the slot was reset for the next task, and every re-run started from ``main``,
could not find the work or its prerequisites, and closed ``blocked`` again.
Nothing anywhere reported that the commits existed.

The rule this module implements is deliberately narrow:

    A close that is not a pass, and a slot reset, must first put every commit
    that no remote branch carries onto ``origin``.  Work is never discarded,
    and a close is never silently accepted while unpushed commits exist.

Three details matter and each is a bug that was easy to write instead:

* **"Unpushed" is asked as "no remote branch has it"**
  (``rev-list HEAD --not --remotes``), not as ``@{u}``.  A slot worktree is
  routinely on a detached HEAD, or on a branch with no upstream, and both
  make the upstream question return "nothing to push" about real work.
* **The push is never forced and never reuses a diverged name.**  A remote
  ``aq/<task-id>`` that this HEAD does not descend from belongs to someone
  else's run; the work goes to ``aq/<task-id>-wip`` (then
  ``-wip-<sha7>``) instead of overwriting it.
* **A failure to push is loud.**  :func:`preserve_unpushed_work` reports
  ``push_failed`` with the git error, and the close path turns that into a
  refused close that keeps the task claimed — the agent still has the
  workspace and can push by hand.  Reporting success here would recreate the
  exact silence the module exists to remove.

The one case that is *not* an error is a repository with no remote at all:
there is nowhere to push, the local branch is all there is, and the caller
records the branch and SHA so a human can find it.  Callers must not treat
``no_remote`` as "nothing was at risk".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.git.manager import GitError

logger = logging.getLogger(__name__)

#: Branch namespace every rescued branch lands in — the same one
#: ``WorktreeSlotManager`` gives a task branch, so the salvage branch sorts
#: next to the run it came from.  Duplicated rather than imported from
#: ``worktree_manager`` because that module imports *this* one.
BRANCH_PREFIX = "aq/"

#: Task metadata keys.  ``unmerged_branch`` is the contract the close
#: protocol, the dashboard and the next agent read: "your predecessor's
#: commits are on this branch".
UNMERGED_BRANCH_META = "unmerged_branch"
UNMERGED_COMMIT_META = "unmerged_commit"


@dataclass
class StrandedWork:
    """What :func:`preserve_unpushed_work` found and what it did about it.

    ``status`` is the whole result; the other fields describe it.

    * ``clean`` — every commit on HEAD is on some remote branch.
    * ``pushed`` — commits were found and are now on ``origin/<branch>``.
    * ``push_failed`` — commits were found and could not be pushed.  The
      close path refuses the close on this.
    * ``no_remote`` — commits were found and the repository has no remote.
      Reported, not an error; ``branch`` is the local branch (or ``None``
      on a detached HEAD).
    * ``unknown`` — the question could not be asked (no checkout, git
      failure).  Treated as "nothing to do" by callers, never as proof of
      safety.
    """

    status: str
    branch: str | None = None
    commit: str | None = None
    count: int = 0
    error: str | None = None

    @property
    def at_risk(self) -> bool:
        """True when commits exist that no remote branch carries."""
        return self.status in ("push_failed", "no_remote")

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "branch": self.branch,
            "commit": self.commit,
            "count": self.count,
            "error": self.error,
        }


def _candidate_branches(task_id: str, head: str | None = None) -> list[str]:
    """Branch names to try, in order, for *task_id*'s rescued commits.

    The task's own branch first — that is where a reviewer, a retry and the
    dashboard already look.  ``-wip`` when that name is occupied by commits
    this HEAD does not descend from, and a sha-suffixed name after that so
    two different rescued HEADs cannot collide on ``-wip`` either.
    """
    stem = f"{BRANCH_PREFIX}{task_id}"
    names = [stem, f"{stem}-wip"]
    if head:
        names.append(f"{stem}-wip-{head[:7]}")
    return names


async def preserve_unpushed_work(
    git,
    workspace: str | None,
    task_id: str,
    *,
    event_bus=None,
    project_id: str | None = None,
) -> StrandedWork:
    """Push every commit in *workspace* that no remote branch carries.

    Safe to call on a clean workspace, on a workspace that is not a git
    checkout, and on a repository with no remote — all three return without
    touching anything.  Never forces a push and never deletes anything.
    """
    if not workspace or not task_id:
        return StrandedWork(status="unknown")
    try:
        if not await git.avalidate_checkout(workspace):
            return StrandedWork(status="unknown")
    except Exception:  # pragma: no cover - defensive
        logger.debug("preserve_unpushed_work: checkout probe failed", exc_info=True)
        return StrandedWork(status="unknown")

    count = await git.acount_commits_not_on_any_remote(workspace)
    if count is None:
        return StrandedWork(status="unknown")
    if count == 0:
        return StrandedWork(status="clean")

    head = await git.arev_parse(workspace, "HEAD")
    local_branch: str | None = None
    try:
        local_branch = (await git.aget_current_branch(workspace)) or None
    except Exception:  # pragma: no cover - defensive
        local_branch = None

    if not await git.ahas_remote(workspace):
        return StrandedWork(
            status="no_remote",
            branch=local_branch,
            commit=head,
            count=count,
        )

    last_error: str | None = None
    for branch in _candidate_branches(task_id, head):
        remote_sha = await git.als_remote_sha(workspace, branch)
        if remote_sha and not await git.ais_ancestor(workspace, remote_sha, "HEAD"):
            # Somebody else's commits live there.  Pushing would need
            # ``--force``, which is exactly the discard this module forbids.
            last_error = f"origin/{branch} has commits this HEAD does not descend from"
            continue
        try:
            await git.apush_head_to(
                workspace, branch, event_bus=event_bus, project_id=project_id
            )
        except GitError as exc:
            last_error = str(exc)
            continue
        logger.info(
            "Preserved %d unpushed commit(s) for %s on origin/%s (%s)",
            count,
            task_id,
            branch,
            (head or "")[:12],
        )
        return StrandedWork(
            status="pushed", branch=branch, commit=head, count=count
        )

    logger.warning(
        "Could not preserve %d unpushed commit(s) for %s: %s", count, task_id, last_error
    )
    return StrandedWork(
        status="push_failed",
        branch=local_branch,
        commit=head,
        count=count,
        error=last_error,
    )


def unpushed_close_issues(work: StrandedWork) -> list[str]:
    """Agent-facing feedback for a close refused over unpushed commits."""
    where = f" (branch `{work.branch}`)" if work.branch else ""
    return [
        f"{work.count} commit(s) in this workspace{where} are on no remote branch, "
        f"and the daemon could not push them for you: {work.error or 'push failed'}. "
        "Push them yourself — `git push -u origin HEAD:refs/heads/aq/<task-id>-wip` "
        "— then close again. Closing now would strand the work: the slot is reset "
        "for the next task and nothing else knows these commits exist."
    ]
