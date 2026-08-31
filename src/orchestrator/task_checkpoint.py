"""Local Git checkpoints for explicit task pause, without changing the live index."""
from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path

from src.git.manager import GitError, GitManager

CHECKPOINT_META = "manual_pause_checkpoint"


async def capture_checkpoint(db, git, task_id: str, workspace: str) -> None:
    """Keep HEAD, staged and unstaged/untracked work reachable before slot reuse."""
    if not Path(workspace).is_dir():
        raise GitError("Paused workspace is missing; cannot preserve its work")
    if not await git.avalidate_checkout(workspace):
        return  # Non-Git workspaces have no destructive Git preparation.
    head = await git._arun(["rev-parse", "--verify", "HEAD"], cwd=workspace)
    branch = await git._arun(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=workspace)
    identity = ["-c", "user.name=Agent Queue", "-c", "user.email=agent-queue@localhost"]
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
        await isolated._arun(
            ["add", "--all", "--", ".", ":(exclude).agent-queue-lock", ":(exclude).aq-worktree.json"],
            cwd=workspace,
        )
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


async def prepare_checkpoint(db, git, task_id: str, workspace: str) -> dict | None:
    """Validate source and destination before any reset or clean can run."""
    saved = await db.get_task_meta(task_id, CHECKPOINT_META)
    if not saved:
        return None
    if not Path(saved["source"]).is_dir():
        raise GitError("Paused task checkpoint repository is missing; workspace was left unchanged")
    target_roots = set((await git._arun(
        ["rev-list", "--max-parents=0", "HEAD"], cwd=workspace
    )).splitlines())
    await git._arun(["fetch", "--no-tags", "--", saved["source"], saved["ref"]], cwd=workspace)
    source_roots = set((await git._arun(
        ["rev-list", "--max-parents=0", saved["head"]], cwd=workspace
    )).splitlines())
    if not target_roots.intersection(source_roots):
        raise GitError("Paused checkpoint belongs to a different repository; workspace was left unchanged")
    try:
        tip = await git._arun(["rev-parse", "--verify", f"refs/heads/{saved['branch']}"], cwd=workspace)
    except GitError:
        pass
    else:
        if tip != saved["head"]:
            raise GitError("Paused task branch changed since its checkpoint; workspace was left unchanged")
    return saved


async def restore_checkpoint(db, git, task_id: str, workspace: str, *, saved=None) -> str | None:
    """Restore explicit continuation; consume only after execution starts."""
    saved = saved or await prepare_checkpoint(db, git, task_id, workspace)
    if not saved:
        return None
    branch = saved["branch"]
    try:
        await git._arun(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=workspace)
    except GitError:
        await git._arun(["branch", branch, saved["head"]], cwd=workspace)
    await git._arun(["switch", branch], cwd=workspace)
    await git._arun(["read-tree", "--reset", "-u", saved["commit"]], cwd=workspace)
    await git._arun(["read-tree", saved["index_tree"]], cwd=workspace)
    return branch
