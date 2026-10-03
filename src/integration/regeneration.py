"""Regenerate generated artifacts from the exact merged sources in a scratch tree.

Candidates, child promotion and CI-repair rebuilds share the merge mechanism.
Only generated artifacts may change during regeneration; source conflicts keep
their original diagnostics and remain the caller's responsibility.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import signal
import sys
import tempfile
from pathlib import Path

from src.git.manager import GitError, GitManager, is_valid_git_oid
from src.integration.development import GENERATED_MERGE_DRIVER
from src.sessions.env import SCRATCH_DB_SENTINEL

logger = logging.getLogger(__name__)

#: Default timeout (seconds) applied to the regenerator subprocess.
REGENERATION_TIMEOUT_SECONDS = 600

#: Maximum tail (chars) of the regenerator output retained on failure.
REGENERATION_OUTPUT_TAIL_CHARS = 4000


class RegenerationFailure(RuntimeError):
    """The scratch-worktree regeneration could not produce a safe tree."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GeneratedMergeConflict(RuntimeError):
    """A source conflict or regeneration failure, with the original Git evidence."""

    def __init__(self, result, reason: str | None = None):
        self.stdout, self.stderr = result.stdout, result.stderr
        self.reason = reason
        super().__init__(reason or result.stdout or result.stderr or "merge conflict")


async def merge_generated_tree(
    git: GitManager,
    store: Path,
    merge_args: list[str],
    *,
    command: str = "scripts/regenerate-generated.sh",
    timeout_seconds: int = REGENERATION_TIMEOUT_SECONDS,
) -> str:
    """Merge exact inputs and rebuild overlapping generated artifacts.

    ``merge_args`` names a ``merge-tree --write-tree`` operation, optionally
    with an explicit base, ending with the two commits. Never infer a different
    base by redoing the merge in a checkout. Restore generated conflicts from
    the current side, then regenerate against all merged sources. Clean text
    merges also need rebuilding when both sides changed a generated artifact.
    """
    merged = await git.arun_git_result(merge_args, cwd=str(store))
    if merged.returncode not in {0, 1}:
        raise GitError(merged.stderr or "merge-tree failed")
    tree = (merged.stdout.splitlines() or [""])[0].strip()
    if not is_valid_git_oid(tree):
        raise GitError("merge-tree did not produce a tree OID")
    conflicts = {
        line.split("\t", 1)[1]
        for line in merged.stdout.splitlines()[1:]
        if "\t" in line and line.split("\t", 1)[0].endswith((" 1", " 2", " 3"))
    }
    current, other = merge_args[-2:]
    base = next(
        (arg.removeprefix("--merge-base=") for arg in merge_args if arg.startswith("--merge-base=")),
        None,
    )
    if base is None:
        result = await git.arun_git_result(["merge-base", current, other], cwd=str(store))
        if result.returncode or not is_valid_git_oid(result.stdout.strip()):
            raise GitError(result.stderr or "could not determine merge base")
        base = result.stdout.strip()
    changed = []
    for side in (current, other):
        result = await git.arun_git_result(
            ["diff", "--name-only", "-z", base, side], cwd=str(store)
        )
        if result.returncode:
            raise GitError(result.stderr or "could not inspect merge inputs")
        changed.append(set(result.stdout.split("\0")) - {""})
    overlaps = sorted((changed[0] & changed[1]) | conflicts)
    if not overlaps:
        if merged.returncode:
            raise GeneratedMergeConflict(merged)
        return tree
    attributes = await git.arun_git_result(
        ["check-attr", f"--source={tree}", "-z", "--stdin", "merge"],
        cwd=str(store), stdin="\0".join(overlaps) + "\0",
    )
    if attributes.returncode:
        raise GitError(attributes.stderr or "generated attribute probe failed")
    fields = attributes.stdout.split("\0")
    generated = {
        fields[i] for i in range(0, len(fields) - 2, 3)
        if fields[i + 2] == GENERATED_MERGE_DRIVER
    }
    if merged.returncode and (not conflicts or conflicts - generated):
        raise GeneratedMergeConflict(merged)
    if not generated:
        return tree
    try:
        return await regenerated_tree(
            git, store, tree, command=command, timeout_seconds=timeout_seconds,
            restore_from=current, restore_paths=tuple(sorted(conflicts)),
        )
    except RegenerationFailure as exc:
        raise GeneratedMergeConflict(merged, f"generated regeneration failed: {exc.reason}") from exc


def _subprocess_env() -> dict[str, str]:
    """The minimal, worker-scoped environment the regenerator sees.

    Mirrors the worker session isolation: the daemon's interpreter ``bin``
    first on ``PATH`` (the generators import installed packages), and the
    database refusal sentinels so no generator can open the operator's
    database.
    """
    return {
        "PATH": f"{Path(sys.executable).parent}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "AQ_DB_SCOPE": "worker",
        "AQ_DATABASE_URL": SCRATCH_DB_SENTINEL,
        "AGENT_QUEUE_DB": SCRATCH_DB_SENTINEL,
    }


async def regenerated_tree(
    git: GitManager,
    store: Path,
    merge_tree_sha: str,
    *,
    command: str,
    timeout_seconds: int = REGENERATION_TIMEOUT_SECONDS,
    restore_from: str | None = None,
    restore_paths: tuple[str, ...] = (),
) -> str:
    """Materialize *merge_tree_sha* in a scratch worktree, run *command* there,
    commit the sweep into a fresh tree, and return that tree SHA.

    Every path that changes relative to *merge_tree_sha* must carry the
    ``merge=aq-generated`` attribute in the checkout's ``.gitattributes``; a
    changed non-generated path, a non-zero regenerator exit, or any git
    failure raises :class:`RegenerationFailure` so the caller falls back to
    its existing conflict handling.

    The scratch worktree is always removed (and its registration pruned),
    including on failure.  Objects written during the sweep stay in the bare
    store's object database, which is harmless unreachable object space.
    """
    worktree = Path(tempfile.mkdtemp(prefix="aq-regen-", dir=str(store.parent)))
    try:
        return await _run(
            git, store, worktree, merge_tree_sha, command, timeout_seconds,
            restore_from, restore_paths,
        )
    finally:
        await _remove_worktree(git, store, worktree)


async def _run(
    git: GitManager,
    store: Path,
    worktree: Path,
    merge_tree_sha: str,
    command: str,
    timeout_seconds: int,
    restore_from: str | None,
    restore_paths: tuple[str, ...],
) -> str:
    # ``git worktree add`` needs a commit, not a tree SHA; wrap the merge tree
    # in a placeholder commit first.
    placeholder = (
        await git.arun_git_result(
            ["commit-tree", merge_tree_sha, "-m", "regeneration placeholder"],
            cwd=str(store),
        )
    ).stdout.strip()
    await git.aworktree_add(str(store), str(worktree), ref=placeholder)

    if restore_paths:
        restored = await git.arun_git_result(
            ["restore", f"--source={restore_from}", "--staged", "--worktree", "--",
             *(f":(literal){path}" for path in restore_paths)],
            cwd=str(worktree),
        )
        if restored.returncode:
            raise RegenerationFailure(restored.stderr or "could not restore generated conflicts")

    argv = shlex.split(command)
    if not argv:
        raise RegenerationFailure(f"empty regenerator command: {command!r}")
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(worktree),
            env=_subprocess_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise RegenerationFailure(f"could not start `{command}`: {exc}") from exc
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError as exc:
        _kill_process_group(process)
        raise RegenerationFailure(
            f"`{command}` timed out after {timeout_seconds}s"
        ) from exc
    except asyncio.CancelledError:
        _kill_process_group(process)
        raise
    if process.returncode != 0:
        tail = output.decode("utf-8", "replace")[-REGENERATION_OUTPUT_TAIL_CHARS:]
        raise RegenerationFailure(f"`{command}` exited {process.returncode}: {tail}")

    await git.arun_git_result(["add", "-A"], cwd=str(worktree))
    new_tree = (await git.arun_git_result(
        ["write-tree"], cwd=str(worktree)
    )).stdout.strip()
    # ``merge_tree_sha`` is already a tree SHA (``merge-tree --write-tree`` or
    # ``rev-parse HEAD^{tree}``), so it is the diff baseline directly.
    if new_tree == merge_tree_sha:
        return new_tree
    old_tree = merge_tree_sha

    changed = [
        line
        for line in (
            await git.arun_git_result(
                ["diff-tree", "--name-only", "-r", old_tree, new_tree],
                cwd=str(worktree),
            )
        ).stdout.splitlines()
        if line
    ]
    # ``check-attr -z --stdin`` emits ``<path>\0<attribute>\0<value>\0`` per path.
    attr_output = (
        await git.arun_git_result(
            ["check-attr", "-z", "--stdin", "merge"],
            cwd=str(worktree),
            stdin="\0".join(changed) + "\0",
        )
    ).stdout
    fields = attr_output.split("\0")
    for i in range(0, len(fields) - 2, 3):
        if fields[i + 2] != GENERATED_MERGE_DRIVER:
            raise RegenerationFailure(
                f"non-generated path changed during regeneration: {fields[i]}"
            )
    logger.info(
        "candidate regeneration: rebuilt %d generated file(s) on tree %s",
        len(changed), new_tree,
        extra={"merge_tree": merge_tree_sha, "regenerated": changed},
    )
    return new_tree


def _kill_process_group(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _remove_worktree(git: GitManager, store: Path, worktree: Path) -> None:
    for attempt in (
        lambda: git.aremove_worktree_exact(str(store), str(worktree)),
        lambda: git.arun_git_result(
            ["worktree", "remove", "--force", str(worktree)], cwd=str(store)
        ),
    ):
        try:
            await attempt()
            break
        except (GitError, RuntimeError, OSError):
            continue
    try:
        await git.aworktree_prune(str(store))
    except (GitError, RuntimeError):
        pass
    try:
        shutil.rmtree(worktree, ignore_errors=True)
    except OSError:
        pass
