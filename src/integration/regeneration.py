"""Regenerate generated artifacts from the exact merged sources in a scratch tree.

Candidates, child promotion and CI-repair rebuilds share the merge mechanism.
Only generated artifacts may change during regeneration; source conflicts keep
their original diagnostics and remain the caller's responsibility.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import shlex
import shutil
import signal
import sys
import tempfile
import weakref
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from src.git.manager import GitError, GitManager, is_valid_git_oid
from src.integration.bookkeeping_guard import LegacyBookkeepingConflict, check_merge
from src.integration.development import GENERATED_MERGE_DRIVER
from src.integration.regeneration_settings import (
    REGENERATION_TIMEOUT_SECONDS as REGENERATION_TIMEOUT_SECONDS,
    regeneration_timeout_seconds,
)
from src.sessions.env import SCRATCH_DB_SENTINEL

logger = logging.getLogger(__name__)

#: The regeneration command a repository is assumed to ship, matching the
#: legacy candidate, promotion and train defaults. A repository may configure
#: another one; a merge never runs an empty command.
DEFAULT_REGENERATE_COMMAND = "scripts/regenerate-generated.sh"

#: Regenerator commands one daemon runs at once. Concurrent closes and train
#: visits queue for a slot instead of starving each other's budgets, and the
#: time spent queued never counts against a budget.
REGENERATION_CONCURRENCY = 1

#: The budget scales with the one-minute load per CPU, at most by this factor.
REGENERATION_LOAD_SCALE_MAX = 4.0

#: Outcomes kept for runs whose caller stopped waiting, so the same call
#: rejoins the shared result instead of rebuilding it.
REGENERATION_RETAINED_OUTCOMES = 64

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


class RegenerationPending(RuntimeError):
    """The shared regeneration was still queued or running when the caller's
    :func:`regeneration_wait` bound expired.

    The run continues detached. Repeating the same call joins it, or takes its
    retained outcome once it settles. Never a failure and never a conflict.
    """

    def __init__(self, state: str, waited_seconds: float) -> None:
        self.state = state
        self.waited_seconds = waited_seconds
        self.reason = (
            f"generated regeneration still {state} after {waited_seconds:g}s; "
            "it continues detached and the same call rejoins it"
        )
        super().__init__(self.reason)


class _RegenerationTimeout(RegenerationFailure):
    """A killed subprocess whose detached scratch tree can be retried."""


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


async def generated_paths(git: GitManager, store: Path, tree: str, paths=None) -> set[str]:
    """Read generated-path policy from the candidate, including new attributes."""
    if paths is None:
        listed = await git.arun_git_result(
            ["ls-tree", "-r", "--name-only", "-z", tree], cwd=str(store),
        )
        if listed.returncode:
            raise GitError(listed.stderr or "could not list candidate artifacts")
        paths = set(listed.stdout.split("\0")) - {""}
    if not paths:
        return set()
    attributes = await git.arun_git_result(
        ["check-attr", f"--source={tree}", "-z", "--stdin", "merge"],
        cwd=str(store), stdin="\0".join(sorted(paths)) + "\0",
    )
    if attributes.returncode:
        raise GitError(attributes.stderr or "generated attribute probe failed")
    fields = attributes.stdout.split("\0")
    return {
        fields[i] for i in range(0, len(fields) - 2, 3)
        if fields[i + 2] == GENERATED_MERGE_DRIVER
    }


async def merge_generated_tree(
    git: GitManager,
    store: Path,
    merge_args: list[str],
    *,
    command: str | None = DEFAULT_REGENERATE_COMMAND,
    timeout_seconds: int = REGENERATION_TIMEOUT_SECONDS,
    regenerate: bool = True,
    regenerations: list[dict] | None = None,
    defer_regeneration: bool = False,
) -> str:
    """Merge exact sources and rebuild changed generated artifacts.

    ``merge_args`` names a ``merge-tree --write-tree`` operation, optionally
    with an explicit base, ending with the two commits. Never infer a different
    base by redoing the merge in a checkout. Discard generated changes from
    the merge, then regenerate against all merged sources. Even single-sided
    changes must be rebuilt: their outputs may depend on another member's input.

    ``command`` must name a command line: an empty one is a configuration gap
    (:class:`MissingRegenerator`), never a silent regeneration. ``regenerate``
    is the caller's policy; with it off, an overlapping generated artifact is
    an ordinary conflict.

    ``defer_regeneration`` restores generated paths without invoking the
    command and records them in ``regenerations``. Batch construction runs
    that command once on the final combined tree.
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
            ["diff", "--no-renames", "--name-only", "-z", base, side], cwd=str(store)
        )
        if result.returncode:
            raise GitError(result.stderr or "could not inspect merge inputs")
        changed.append(set(result.stdout.split("\0")) - {""})
    # Fence every source side before restoring or deferring any generated file.
    # This also catches clean, single-sided legacy edits that invoke no driver.
    try:
        await check_merge(git, store, base, current, other)
    except LegacyBookkeepingConflict as exc:
        raise GeneratedMergeConflict(merged, str(exc), files=exc.paths) from exc
    candidates = ((changed[0] | changed[1]) if regenerate else (changed[0] & changed[1]))
    candidates |= conflicts
    if not candidates:
        if merged.returncode:
            raise GeneratedMergeConflict(merged)
        return tree
    generated = await generated_paths(git, store, tree, candidates)
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
            restore_from=current, restore_paths=tuple(sorted(generated)),
            restore_only=defer_regeneration,
            verified_merges=((base, current, other),),
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
            "deferred": defer_regeneration,
        })
    return rebuilt


def _subprocess_env(verified_merges=()) -> dict[str, str]:
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
    env = {
        "PATH": os.pathsep.join(tool_dirs),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "AQ_DB_SCOPE": "worker",
        "AQ_DATABASE_URL": SCRATCH_DB_SENTINEL,
        "AGENT_QUEUE_DB": SCRATCH_DB_SENTINEL,
    }
    if verified_merges:
        env["AQ_GENERATED_MERGE_INPUTS"] = json.dumps(verified_merges)
    return env


def load_scaled_timeout(
    base_seconds: float, *, load: float | None = None, cpus: int | None = None,
) -> float:
    """Stretch *base_seconds* by the box's one-minute load per CPU.

    An idle box keeps the configured budget; a box at three times its CPU
    count gives the regenerator three times as long, capped at
    :data:`REGENERATION_LOAD_SCALE_MAX`.
    """
    if load is None:
        try:
            load = os.getloadavg()[0]
        except OSError:
            return float(base_seconds)
    per_cpu = load / max(1, cpus or os.cpu_count() or 1)
    return float(base_seconds) * min(REGENERATION_LOAD_SCALE_MAX, max(1.0, per_cpu))


_WAIT_SECONDS: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "aq_regeneration_wait_seconds", default=None,
)


@contextmanager
def regeneration_wait(seconds: float | None):
    """Bound how long regenerations in this context wait for the shared run.

    Past the bound :func:`regenerated_tree` raises :class:`RegenerationPending`
    while the run continues detached. ``None`` (the default) waits for it.
    """
    token = _WAIT_SECONDS.set(seconds)
    try:
        yield
    finally:
        _WAIT_SECONDS.reset(token)


@dataclass
class _Flight:
    task: asyncio.Task | None = None
    state: str = "queued"
    detached: bool = False


@dataclass
class _Scheduler:
    """One event loop's regeneration slots, in-flight runs and retained outcomes."""

    gate: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(REGENERATION_CONCURRENCY)
    )
    flights: dict[tuple, _Flight] = field(default_factory=dict)
    retained: OrderedDict = field(default_factory=OrderedDict)
    scratch: OrderedDict = field(default_factory=OrderedDict)

    def settle(self, key: tuple, flight: _Flight) -> None:
        self.flights.pop(key, None)
        if flight.task.cancelled():
            return
        error = flight.task.exception()  # Always retrieved: no unhandled warning.
        if not flight.detached:
            return
        self.retained[key] = (error, None if error else flight.task.result())
        self.retained.move_to_end(key)
        while len(self.retained) > REGENERATION_RETAINED_OUTCOMES:
            self.retained.popitem(last=False)


_SCHEDULERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _scheduler() -> _Scheduler:
    loop = asyncio.get_running_loop()
    scheduler = _SCHEDULERS.get(loop)
    if scheduler is None:
        scheduler = _SCHEDULERS[loop] = _Scheduler()
    return scheduler


async def regenerated_tree(
    git: GitManager,
    store: Path,
    merge_tree_sha: str,
    *,
    command: str,
    timeout_seconds: int = REGENERATION_TIMEOUT_SECONDS,
    restore_from: str | None = None,
    restore_paths: tuple[str, ...] = (),
    restore_only: bool = False,
    verified_merges: tuple[tuple[str, str, str], ...] = (),
) -> str:
    """Materialize *merge_tree_sha* in a scratch worktree, run *command* there,
    commit the sweep into a fresh tree, and return that tree SHA.

    Every path that changes relative to *merge_tree_sha* must carry the
    ``merge=aq-generated`` attribute in the checkout's ``.gitattributes``; a
    changed non-generated path, a non-zero regenerator exit, or any git
    failure raises :class:`RegenerationFailure` so the caller falls back to
    its existing conflict handling.

    Scratch worktrees are removed after success or terminal failure. A detached
    subprocess timeout retains its tree for an identical retry, including partial
    generated output and ignored caches. Retention is bounded per event loop by
    :data:`REGENERATION_RETAINED_OUTCOMES`; evicted worktrees are removed.

    ``restore_only`` returns the tree after restoring the specified generated
    conflicts. It leaves the command for the batch's final combined sweep.

    A command run is single-flight per event loop: identical inputs share one
    run, at most :data:`REGENERATION_CONCURRENCY` commands run at once, and
    *timeout_seconds* (scaled by :func:`load_scaled_timeout`) starts only when
    a run gets its slot. The run is detached from its callers: a caller whose
    :func:`regeneration_wait` bound expires gets :class:`RegenerationPending`,
    a cancelled caller leaves the run going, and the same call later rejoins
    it or takes its retained outcome (a retained failure is reported once).
    """
    args = (git, store, merge_tree_sha, command, timeout_seconds, restore_from,
            restore_paths, restore_only, verified_merges)
    if restore_only:
        return await _scratch_run(*args)
    scheduler = _scheduler()
    key = (str(Path(store).resolve()), merge_tree_sha, command, restore_from,
           tuple(restore_paths), tuple(tuple(merge) for merge in verified_merges))
    flight = scheduler.flights.get(key)
    if flight is None:
        retained = await _retained_outcome(git, store, scheduler, key)
        if retained is not None:
            return retained
        flight = scheduler.flights[key] = _Flight()
        flight.task = asyncio.create_task(_slotted_run(scheduler, key, flight, args))
        flight.task.add_done_callback(lambda _task, f=flight: scheduler.settle(key, f))
    wait_seconds = _WAIT_SECONDS.get()
    try:
        done, _ = await asyncio.wait({flight.task}, timeout=wait_seconds)
    except asyncio.CancelledError:
        flight.detached = True
        raise
    if not done:
        flight.detached = True
        raise RegenerationPending(flight.state, wait_seconds)
    return flight.task.result()


async def _retained_outcome(git, store, scheduler, key):
    """A settled detached run's tree, or raise its failure exactly once."""
    error, tree = scheduler.retained.get(key, (None, None))
    if error is not None:
        del scheduler.retained[key]
        raise error
    if tree is None:
        return None
    # Unreachable sweep objects can be pruned; a vanished tree runs again.
    probe = await git.arun_git_result(["cat-file", "-e", f"{tree}^{{tree}}"], cwd=str(store))
    if probe.returncode:
        del scheduler.retained[key]
        return None
    scheduler.retained.move_to_end(key)
    return tree


async def _slotted_run(scheduler: _Scheduler, key: tuple, flight: _Flight, args: tuple) -> str:
    async with scheduler.gate:
        flight.state = "running"
        return await _scratch_run(*args, scheduler=scheduler, key=key, flight=flight)


async def _scratch_run(
    git, store, merge_tree_sha, command, timeout_seconds, restore_from,
    restore_paths, restore_only, verified_merges,
    *, scheduler=None, key=None, flight=None,
) -> str:
    retained = scheduler.scratch.pop(key, None) if scheduler is not None else None
    worktree = retained[2] if retained else Path(
        tempfile.mkdtemp(prefix="aq-regen-", dir=str(store.parent))
    )
    keep = False
    try:
        return await _run(
            git, store, worktree, merge_tree_sha, command,
            timeout_seconds if restore_only else load_scaled_timeout(
                regeneration_timeout_seconds(timeout_seconds)
            ),
            restore_from, restore_paths,
            restore_only,
            verified_merges,
            initialized=retained is not None,
        )
    except _RegenerationTimeout:
        if flight is not None and flight.detached:
            scheduler.scratch[key] = (git, store, worktree)
            keep = True
            while len(scheduler.scratch) > REGENERATION_RETAINED_OUTCOMES:
                _, (old_git, old_store, old_worktree) = scheduler.scratch.popitem(last=False)
                await _remove_worktree(old_git, old_store, old_worktree)
        raise
    finally:
        if not keep:
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
    restore_only: bool,
    verified_merges: tuple[tuple[str, str, str], ...],
    *, initialized: bool = False,
) -> str:
    # ``git worktree add`` needs a commit, not a tree SHA; wrap the merge tree
    # in a placeholder commit first.
    if not initialized:
        placeholder = (
            await git.arun_git_result(
                ["commit-tree", merge_tree_sha, "-m", "regeneration placeholder"],
                cwd=str(store),
            )
        ).stdout.strip()
        await git.aworktree_add(str(store), str(worktree), ref=placeholder)

    if restore_paths and not initialized:
        # A generated file may be new on the other side or deleted on ours.
        # Remove merged versions first, then restore only paths our tree has.
        await _checked(
            git, ["rm", "-f", "--ignore-unmatch", "--",
                  *(f":(literal){path}" for path in restore_paths)], worktree,
        )
        listed = await _checked(
            git, ["ls-tree", "-r", "--name-only", "-z", restore_from, "--",
                  *(f":(literal){path}" for path in restore_paths)], worktree,
        )
        existing = sorted(set(listed.stdout.split("\0")) - {""})
        if existing:
            await _checked(
                git,
                ["restore", f"--source={restore_from}", "--staged", "--worktree", "--",
                 *(f":(literal){path}" for path in existing)], worktree,
            )

    if restore_only:
        # Intermediate batch merges retain generated artifacts from the current
        # side. The final combined tree receives the single regeneration sweep.
        return (await _checked(git, ["write-tree"], worktree)).stdout.strip()

    argv = shlex.split(command or "")
    if not argv:
        raise MissingRegenerator(f"no regenerate command is configured ({command!r})")
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(worktree),
            env=_subprocess_env(verified_merges),
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
        await process.communicate()
        raise _RegenerationTimeout(
            f"`{command}` timed out after {timeout_seconds:g}s", retryable=True
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
