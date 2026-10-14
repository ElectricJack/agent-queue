"""Local Git checkpoints for explicit task pause, without changing the live index."""
from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from src.git.manager import GitError, GitManager, is_valid_git_oid

CHECKPOINT_META = "manual_pause_checkpoint"


class CheckpointIdentityError(GitError):
    """The source changed; bounded salvage must not read an unrelated checkout."""


async def validate_checkpoint_branch(git, workspace: str, expected_branch: str | None) -> None:
    """Reject an attached foreign branch; never infer a detached branch identity."""
    if expected_branch is None or not Path(workspace).is_dir():
        return
    if not (Path(workspace) / ".git").exists() and not await git.avalidate_checkout(workspace):
        return  # A non-Git workspace has no branch to prove.
    try:
        ref = await git._arun(["symbolic-ref", "--quiet", "HEAD"], cwd=workspace)
    except GitError:
        # Detached HEAD is explicit; a failed read alone proves no identity.
        ref = await git._arun(["rev-parse", "--symbolic-full-name", "HEAD"], cwd=workspace)
    expected = "refs/heads/" + expected_branch.removeprefix("refs/heads/")
    if ref not in {"HEAD", expected}:
        raise CheckpointIdentityError(
            "Paused workspace branch does not match its task; retain source for guarded recovery"
        )


async def _lock_sentinel_ignored(git, workspace: str) -> bool:
    """Is the workspace's ``.agent-queue-lock`` sentinel ignored by Git?"""
    try:
        await git._arun(["check-ignore", "-q", "--", ".agent-queue-lock"], cwd=workspace)
    except GitError:
        return False  # exit 1: not ignored (or not answerable -- exclude it explicitly)
    return True


@asynccontextmanager
async def isolated_index(git, workspace: str):
    """Copy the live index (if any); every write stays in the temporary copy."""
    with tempfile.TemporaryDirectory(prefix="aq-pause-index-") as temp:
        isolated = GitManager()
        index = Path(temp) / "index"
        live_index = await git._arun(["rev-parse", "--git-path", "index"], cwd=workspace)
        source_index = Path(live_index)
        if not source_index.is_absolute():
            source_index = Path(workspace) / source_index
        if source_index.exists():
            shutil.copyfile(source_index, index)
        isolated._SUBPROCESS_ENV = {**git._SUBPROCESS_ENV, "GIT_INDEX_FILE": str(index)}
        yield isolated


async def _add_worktree(git, workspace: str) -> None:
    pathspec = ["--", "."]
    if not await _lock_sentinel_ignored(git, workspace):
        pathspec.append(":(exclude).agent-queue-lock")
    await git._arun(["add", "--all", *pathspec], cwd=workspace)


async def capture_checkpoint(
    db, git, task_id: str, workspace: str, *, validate=None, save=None, expected_branch=None
) -> None:
    """Keep HEAD, staged and unstaged/untracked work reachable before slot reuse."""
    if not Path(workspace).is_dir():
        raise GitError("Paused workspace is missing; cannot preserve its work")
    if not await git.avalidate_checkout(workspace):
        if (Path(workspace) / ".git").exists():
            raise GitError("Paused Git repository is unavailable; cannot prove preservation")
        return  # Non-Git workspaces have no destructive Git preparation.
    source = await git._arun(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=workspace
    )
    head = await git._arun(["rev-parse", "--verify", "HEAD"], cwd=workspace)
    head_ref = await git._arun(["rev-parse", "--symbolic-full-name", "HEAD"], cwd=workspace)
    if head_ref != "HEAD" and not head_ref.startswith("refs/heads/"):
        raise CheckpointIdentityError("Paused workspace HEAD identity is ambiguous")
    branch = head_ref.removeprefix("refs/heads/") if head_ref != "HEAD" else None
    if branch is not None and expected_branch is not None and (
        branch != expected_branch.removeprefix("refs/heads/")
    ):
        raise CheckpointIdentityError("Paused workspace branch does not match its task")
    # The caller scopes the task's project identity (``commit_identity``).
    identity = git.resolve_commit_identity().config_args()
    async with isolated_index(git, workspace) as isolated:
        staged = await isolated._arun(["write-tree"], cwd=workspace)
        staged_commit = await git._arun(
            [*identity, "commit-tree", staged, "-p", head, "-m", "Paused task index"], cwd=workspace
        )
        # Naming an *ignored* file in a pathspec -- even an exclude one -- makes
        # ``git add`` exit 1 ("paths are ignored"), and the managed exclude
        # ignores the lock sentinel; ``add --all`` already skips it then.
        await _add_worktree(isolated, workspace)
        tree = await isolated._arun(["write-tree"], cwd=workspace)
    checkpoint = await git._arun(
        [*identity, "commit-tree", tree, "-p", staged_commit, "-m", "Paused task worktree"], cwd=workspace
    )
    ref = f"refs/aq/task-pauses/{uuid.uuid4().hex}"
    if (await git._arun(["rev-parse", "--verify", "HEAD"], cwd=workspace) != head
            or await git._arun(["rev-parse", "--symbolic-full-name", "HEAD"], cwd=workspace)
            != head_ref
            or await git._arun(
                ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=workspace
            ) != source):
        raise CheckpointIdentityError("Paused workspace Git identity changed during capture")
    if validate is not None:
        await validate()
    await git._arun(["update-ref", ref, checkpoint], cwd=workspace)
    saved = {"task_id": task_id, "head": head, "branch": branch, "index_tree": staged, "ref": ref,
             "commit": checkpoint, "source": source}
    if save is not None:
        await save(saved)
    else:
        await db.set_task_meta(task_id, CHECKPOINT_META, saved)
        await db.add_task_context(task_id, type="manual_pause_checkpoint",
                                  label="Git checkpoint before manual pause", content=json.dumps(saved))


async def salvage_checkpoint(
    db, git, task_id: str, workspace: str, *, max_bytes=0, save=None, expected_branch=None
) -> bool:
    """Best-effort binary patch, retaining the live staging boundary even on failure."""
    await validate_checkpoint_branch(git, workspace, expected_branch)
    source = await git._arun(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=workspace
    )
    async def validate_source():
        if await git._arun(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=workspace
        ) != source:
            raise CheckpointIdentityError("Paused workspace Git identity changed during salvage")
        await validate_checkpoint_branch(git, workspace, expected_branch)

    async with isolated_index(git, workspace) as isolated:
        await _add_worktree(isolated, workspace)
        patch = await isolated._arun(["diff", "--cached", "--binary"], cwd=workspace)
        await validate_source()
        if not patch:
            return False
        if max_bytes and len(patch.encode("utf-8")) > max_bytes:
            stat = await isolated._arun(["diff", "--cached", "--stat"], cwd=workspace)
            patch = f"# Salvage exceeds {max_bytes} bytes; only diffstat preserved.\n{stat}"
        record = save if save is not None else db.add_task_context
        await validate_source()
        await record(
            task_id, type="worktree_salvage", label=f"Pause salvage from {workspace}",
            content="# Partial salvage; HEAD and separate staging state are not archived.\n" + patch,
        )
        return True


async def _validate_checkpoint_task(db, task_id, saved, expected_branch=None):
    task = await db.get_task(task_id)
    if (task is None or saved.get("task_id", task_id) != task_id
            or saved.get("pause_project_id", task.project_id) != task.project_id):
        raise GitError("Paused checkpoint task identity differs; workspace was left unchanged")
    expected = expected_branch or getattr(task, "branch_name", None)
    current = getattr(task, "branch_name", None)
    if (expected is None or saved["branch"] != expected.removeprefix("refs/heads/")
            or current is not None and current.removeprefix("refs/heads/")
            != expected.removeprefix("refs/heads/")):
        raise GitError("Paused checkpoint branch lacks task ownership proof; "
                       "workspace was left unchanged")


async def prepare_checkpoint(
    db, git, task_id: str, workspace: str, *, handoff_sha: str | None = None,
    expected_branch: str | None = None,
) -> dict | None:
    """Validate source and destination before any reset or clean can run."""
    saved = await db.get_task_meta(task_id, CHECKPOINT_META)
    if not saved:
        return None
    saved = _retained_stop_restoration(saved, task_id)
    if saved.get("branch") is None:
        raise GitError("Paused checkpoint has detached HEAD and no proved branch; "
                       "retain the checkpoint for guarded recovery; workspace was left unchanged")
    await _validate_checkpoint_task(db, task_id, saved, expected_branch)
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


def _retained_stop_restoration(saved, task_id):
    """Use separate exact branch proof; preserve the durable detached capture."""
    if saved.get("branch") is not None or "retained_stop_branch" not in saved:
        return saved
    proof = saved["retained_stop_branch"]
    identity = saved.get("retained_stop_identity") or {}
    task = identity.get("task") or {}
    if (proof.get("task_id") != task_id or task.get("id") != task_id
            or proof.get("source") != saved.get("source")
            or proof.get("head") != saved.get("head")
            or proof.get("claim") != identity.get("claim")
            or not proof.get("branch")
            or proof["branch"] != str(task.get("branch_name") or "").removeprefix("refs/heads/")):
        raise GitError("Retained checkpoint branch proof changed; workspace was left unchanged")
    return saved | {"branch": proof["branch"]}


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


async def restore_checkpoint(
    db, git, task_id: str, workspace: str, *, saved=None, expected_branch=None
) -> str | None:
    """Restore explicit continuation; consume only after execution starts."""
    saved = saved or await prepare_checkpoint(
        db, git, task_id, workspace, expected_branch=expected_branch
    )
    if not saved:
        return None
    saved = _retained_stop_restoration(saved, task_id)
    if saved.get("branch") is None:
        raise GitError("Paused checkpoint has no proved branch; workspace was left unchanged")
    await _validate_checkpoint_task(db, task_id, saved, expected_branch)
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
