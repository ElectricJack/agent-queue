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

#: The regeneration command a repository is assumed to ship, matching the
#: legacy candidate, promotion and train defaults. A repository may configure
#: another one; a merge never runs an empty command.
DEFAULT_REGENERATE_COMMAND = "scripts/regenerate-generated.sh"

#: Default timeout (seconds) applied to the regenerator subprocess.
REGENERATION_TIMEOUT_SECONDS = 600

#: Maximum tail (chars) of the regenerator output retained on failure.
REGENERATION_OUTPUT_TAIL_CHARS = 4000


class RegenerationFailure(RuntimeError):
    """The scratch-worktree regeneration could not produce a safe tree.

    ``retryable`` marks infrastructure failures (the regenerator could not
    start, timed out or exited non-zero, or a git step failed) as opposed to a
    content-level refusal such as a changed non-generated path.
    """

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class GeneratedMergeConflict(RuntimeError):
    """A source conflict or regeneration failure, with the original Git evidence."""

    def __init__(self, result, reason: str | None = None, *, files=()):
        self.stdout, self.stderr = result.stdout, result.stderr
        self.reason = reason
        self.files = tuple(files)
        super().__init__(reason or result.stdout or result.stderr or "merge conflict")


class GeneratedRegenerationFailure(GitError):
    """Retryable clean-merge failure carrying the artifact paths for repair."""

    def __init__(self, reason: str, files):
        self.files = tuple(files)
        super().__init__(reason)


class MissingRegenerator(RegenerationFailure):
    """No regeneration command is configured, so an artifact cannot be rebuilt.

    A configuration gap, never a member's content: the caller reports it as its
    own blocker instead of parking a member as a conflict.
    """

    def __init__(self, reason: str | None = None) -> None:
        super().__init__(
            reason or "no regenerate command is configured for this repository", retryable=False
        )


async def merge_generated_tree(
    git: GitManager,
    store: Path,
    merge_args: list[str],
    *,
    command: str | None = DEFAULT_REGENERATE_COMMAND,
    timeout_seconds: int = REGENERATION_TIMEOUT_SECONDS,
    regenerate: bool = True,
    regenerations: list[dict] | None = None,
) -> str:
    """Merge exact inputs and rebuild overlapping generated artifacts.

    ``merge_args`` names a ``merge-tree --write-tree`` operation, optionally
    with an explicit base, ending with the two commits. Never infer a different
    base by redoing the merge in a checkout. Restore generated conflicts from
    the current side, then regenerate against all merged sources. Clean text
    merges also need rebuilding when both sides changed a generated artifact.

    ``command`` must name a command line: an empty one is a configuration gap
    (:class:`MissingRegenerator`), never a silent regeneration. ``regenerate``
    is the caller's policy; with it off, an overlapping generated artifact is
    an ordinary conflict.
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
    if not regenerate:
        # The caller declined regeneration, so an overlapping generated
        # artifact is an ordinary conflict; no member is blamed for content.
        raise GeneratedMergeConflict(
            merged, "generated artifacts overlap and regeneration is disabled"
        )
    if not shlex.split(command or ""):
        raise MissingRegenerator(f"no regenerate command is configured ({command!r})")
    try:
        rebuilt = await regenerated_tree(
            git, store, tree, command=command, timeout_seconds=timeout_seconds,
            restore_from=current, restore_paths=tuple(sorted(conflicts)),
        )
    except MissingRegenerator:
        # A configuration gap is never a member's conflict, however the merge
        # resolved; the caller reports it as its own blocker.
        raise
    except RegenerationFailure as exc:
        if exc.retryable and not merged.returncode:
            # A clean merge has no conflict to record; an infrastructure
            # failure must stay retryable instead of parking the member.
            raise GeneratedRegenerationFailure(
                f"generated regeneration failed: {exc.reason}", sorted(generated),
            ) from exc
        raise GeneratedMergeConflict(
            merged, f"generated regeneration failed: {exc.reason}", files=sorted(generated),
        ) from exc
    if regenerations is not None:
        regenerations.append({
            "files": sorted(generated), "merged_tree": tree, "regenerated_tree": rebuilt,
            "parents": [current, other],
        })
    return rebuilt


def _subprocess_env() -> dict[str, str]:
    """The minimal, worker-scoped environment the regenerator sees.

    Resolve installed CLI tools from the daemon's interpreter ``bin``, then
    the standard user installation directory (also when the daemon uses
    system Python), then fixed system directories. Never inherit ambient
    ``PATH`` or credentials. The canonical scripts still enforce generator
    versions; discovering a tool does not authorize a different version.
    Database refusal sentinels keep generators off the operator's database.
    """
    home = Path.home()
    tool_dirs = dict.fromkeys((
        str(Path(sys.executable).parent), str(home / ".local" / "bin"),
        "/usr/local/bin", "/usr/bin", "/bin",
    ))
    return {
        "PATH": os.pathsep.join(tool_dirs),
        "HOME": str(home),
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
            raise RegenerationFailure(
                restored.stderr or "could not restore generated conflicts", retryable=True
            )

    argv = shlex.split(command or "")
    if not argv:
        raise MissingRegenerator(f"no regenerate command is configured ({command!r})")
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
        raise RegenerationFailure(
            f"could not start `{command}`: {exc}", retryable=True
        ) from exc
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError as exc:
        _kill_process_group(process)
        raise RegenerationFailure(
            f"`{command}` timed out after {timeout_seconds}s", retryable=True
        ) from exc
    except asyncio.CancelledError:
        _kill_process_group(process)
        raise
    if process.returncode != 0:
        tail = output.decode("utf-8", "replace")[-REGENERATION_OUTPUT_TAIL_CHARS:]
        raise RegenerationFailure(
            f"`{command}` exited {process.returncode}: {tail}", retryable=True
        )

    await _checked(git, ["add", "-A"], worktree)
    new_tree = (await _checked(git, ["write-tree"], worktree)).stdout.strip()
    # ``merge_tree_sha`` is already a tree SHA (``merge-tree --write-tree`` or
    # ``rev-parse HEAD^{tree}``), so it is the diff baseline directly.
    if new_tree == merge_tree_sha:
        return new_tree
    old_tree = merge_tree_sha

    changed = [
        line
        for line in (
            await _checked(git, ["diff-tree", "--name-only", "-r", old_tree, new_tree], worktree)
        ).stdout.splitlines()
        if line
    ]
    # ``check-attr -z --stdin`` emits ``<path>\0<attribute>\0<value>\0`` per path.
    attr_output = (
        await _checked(
            git, ["check-attr", "-z", "--stdin", "merge"], worktree,
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


async def _checked(git: GitManager, args: list[str], worktree: Path, **kwargs):
    result = await git.arun_git_result(args, cwd=str(worktree), **kwargs)
    if result.returncode:
        raise RegenerationFailure(
            f"git {args[0]} failed during regeneration: {result.stderr.strip()}",
            retryable=True,
        )
    return result


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
