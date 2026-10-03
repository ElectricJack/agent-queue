"""Local Git checkpoints for explicit task pause, without changing the live index."""
from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path

from src.git.manager import GitError, GitManager, is_valid_git_oid

CHECKPOINT_META = "manual_pause_checkpoint"


async def _lock_sentinel_ignored(git, workspace: str) -> bool:
    """Is the workspace's ``.agent-queue-lock`` sentinel ignored by Git?"""
    try:
        await git._arun(["check-ignore", "-q", "--", ".agent-queue-lock"], cwd=workspace)
    except GitError:
        return False  # exit 1: not ignored (or not answerable -- exclude it explicitly)
    return True


async def capture_checkpoint(db, git, task_id: str, workspace: str) -> None:
    """Keep HEAD, staged and unstaged/untracked work reachable before slot reuse."""
    if not Path(workspace).is_dir():
        raise GitError("Paused workspace is missing; cannot preserve its work")
    if not await git.avalidate_checkout(workspace):
        return  # Non-Git workspaces have no destructive Git preparation.
    head = await git._arun(["rev-parse", "--verify", "HEAD"], cwd=workspace)
    branch = await git._arun(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=workspace)
    # The caller scopes the task's project identity (``commit_identity``).
    identity = git.resolve_commit_identity().config_args()
    with tempfile.TemporaryDirectory(prefix="aq-pause-index-") as temp:
        isolated = GitManager()
        index = Path(temp) / "index"
        live_index = await git._arun(["rev-parse", "--git-path", "index"], cwd=workspace)
        source_index = Path(live_index)
        if not source_index.is_absolute():
            source_index = Path(workspace) / source_index
        shutil.copyfile(source_index, index)
        isolated._SUBPROCESS_ENV = {**git._SUBPROCESS_ENV, "GIT_INDEX_FILE": str(index)}
        staged = await isolated._arun(["write-tree"], cwd=workspace)
        staged_commit = await git._arun(
            [*identity, "commit-tree", staged, "-p", head, "-m", "Paused task index"], cwd=workspace
        )
        # Naming an *ignored* file in a pathspec -- even an exclude one -- makes
        # ``git add`` exit 1 ("paths are ignored"), and the managed exclude
        # ignores the lock sentinel; ``add --all`` already skips it then.
        pathspec = ["--", "."]
        if not await _lock_sentinel_ignored(git, workspace):
            pathspec.append(":(exclude).agent-queue-lock")
        await isolated._arun(["add", "--all", *pathspec], cwd=workspace)
        tree = await isolated._arun(["write-tree"], cwd=workspace)
    checkpoint = await git._arun(
        [*identity, "commit-tree", tree, "-p", staged_commit, "-m", "Paused task worktree"], cwd=workspace
    )
    ref = f"refs/aq/task-pauses/{uuid.uuid4().hex}"
    await git._arun(["update-ref", ref, checkpoint], cwd=workspace)
    source = await git._arun(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=workspace)
    saved = {"head": head, "branch": branch, "index_tree": staged, "ref": ref,
             "commit": checkpoint, "source": source}
    await db.set_task_meta(task_id, CHECKPOINT_META, saved)
    await db.add_task_context(task_id, type="manual_pause_checkpoint",
                              label="Git checkpoint before manual pause", content=json.dumps(saved))


async def prepare_checkpoint(
    db, git, task_id: str, workspace: str, *, handoff_sha: str | None = None
) -> dict | None:
    """Validate source and destination before any reset or clean can run."""
    saved = await db.get_task_meta(task_id, CHECKPOINT_META)
    if not saved:
        return None
    if not Path(saved["source"]).is_dir():
        raise GitError("Paused task checkpoint repository is missing; workspace was left unchanged")
    target_roots = set((await git._arun(
        ["rev-list", "--max-parents=0", "HEAD"], cwd=workspace
    )).splitlines())
    if handoff_sha is None:
        await git._arun(["fetch", "--no-tags", "--", saved["source"], saved["ref"]], cwd=workspace)
    else:
        # FETCH_HEAD is shared by sibling slots. Bind the fetched snapshot to
        # our own temporary ref so another slot's fetch cannot change the proof.
        fetched_ref = f"refs/aq/task-restores/{uuid.uuid4().hex}"
        try:
            await git._arun(
                ["fetch", "--no-tags", "--no-write-fetch-head", "--", saved["source"],
                 f"{saved['ref']}:{fetched_ref}"], cwd=workspace,
            )
            fetched = await git._arun(["rev-parse", "--verify", fetched_ref], cwd=workspace)
            if fetched != saved["commit"]:
                raise GitError("Paused checkpoint ref changed; workspace was left unchanged")
        finally:
            await git._arun(["update-ref", "-d", fetched_ref], cwd=workspace)
    source_roots = set((await git._arun(
        ["rev-list", "--max-parents=0", saved["head"]], cwd=workspace
    )).splitlines())
    if not target_roots.intersection(source_roots):
        raise GitError("Paused checkpoint belongs to a different repository; workspace was left unchanged")
    if handoff_sha is not None:
        # An absent branch is restorable; a failed read is not proof of absence.
        ref = f"refs/heads/{saved['branch']}"
        refs = await git._arun(
            ["for-each-ref", "--format=%(refname) %(objectname)", "--", ref], cwd=workspace
        )
        tips = [line.partition(" ")[2] for line in refs.splitlines()
                if line.partition(" ")[0] == ref]
        if len(tips) > 1 or tips and not is_valid_git_oid(tips[0]):
            raise GitError("Paused task branch tip is ambiguous; workspace was left unchanged")
        return await _reconcile_handoff_checkpoint(
            git, workspace, saved, handoff_sha, tips[0] if tips else None
        )
    try:
        tip = await git._arun(["rev-parse", "--verify", f"refs/heads/{saved['branch']}"], cwd=workspace)
    except GitError:
        tip = None
    if tip is not None and tip != saved["head"]:
        raise GitError("Paused task branch changed since its checkpoint; workspace was left unchanged")
    return saved


async def _reconcile_handoff_checkpoint(git, workspace, saved, handoff_sha, branch_tip):
    """Plan restoration against audited progress without mutating a checkout or index."""
    original_head = saved["head"]
    checkpoint = saved["commit"]
    if not all(is_valid_git_oid(value) for value in (
        handoff_sha, original_head, checkpoint, saved["index_tree"]
    )):
        raise GitError("Paused checkpoint identity is ambiguous; workspace was left unchanged")
    index_commit = await git._arun(["rev-parse", f"{checkpoint}^"], cwd=workspace)
    if (
        await git._arun(["rev-parse", f"{index_commit}^"], cwd=workspace) != original_head
        or await git._arun(["rev-parse", f"{index_commit}^{{tree}}"], cwd=workspace)
        != saved["index_tree"]
    ):
        raise GitError("Paused checkpoint identity is ambiguous; workspace was left unchanged")

    head = original_head
    for candidate in (handoff_sha, branch_tip):
        if candidate is None or candidate == head:
            continue
        if await git.ais_ancestor(workspace, head, candidate, strict=True) is True:
            head = candidate
        elif await git.ais_ancestor(workspace, candidate, head, strict=True) is not True:
            raise GitError("Paused checkpoint and operator handoff diverged or are unproved; "
                           "workspace was left unchanged")

    index_tree, worktree_tree = saved["index_tree"], checkpoint
    if head != original_head:
        trees = []
        for snapshot in (index_commit, checkpoint):
            try:
                tree = await git._arun(
                    ["merge-tree", "--write-tree", f"--merge-base={original_head}", head, snapshot],
                    cwd=workspace,
                )
            except GitError as exc:
                raise GitError("Paused checkpoint content conflicts with operator handoff; "
                               "workspace was left unchanged") from exc
            if not is_valid_git_oid(tree):
                raise GitError("Paused checkpoint merge is ambiguous; workspace was left unchanged")
            trees.append(tree)
        index_tree, worktree_tree = trees
    # A restoration plan only: keep the durable snapshots reachable and unchanged
    # until execution starts, including after a failed/retried preparation.
    return saved | {"handoff_restore": {
        "head": head, "index_tree": index_tree, "worktree_tree": worktree_tree,
    }}


async def restore_checkpoint(db, git, task_id: str, workspace: str, *, saved=None) -> str | None:
    """Restore explicit continuation; consume only after execution starts."""
    saved = saved or await prepare_checkpoint(db, git, task_id, workspace)
    if not saved:
        return None
    branch = saved["branch"]
    plan = saved.get("handoff_restore")
    if plan:
        await git._arun(["switch", "-C", branch, plan["head"]], cwd=workspace)
    else:
        try:
            await git._arun(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=workspace)
        except GitError:
            await git._arun(["branch", branch, saved["head"]], cwd=workspace)
        await git._arun(["switch", branch], cwd=workspace)
    worktree_tree = plan["worktree_tree"] if plan else saved["commit"]
    index_tree = plan["index_tree"] if plan else saved["index_tree"]
    await git._arun(["read-tree", "--reset", "-u", worktree_tree], cwd=workspace)
    await git._arun(["read-tree", index_tree], cwd=workspace)
    return branch
