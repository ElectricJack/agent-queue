"""GitManager -- wraps git CLI commands for the orchestrator's workspace management.

All operations have both synchronous and async variants.  The async methods
(prefixed with ``a``) use ``asyncio.create_subprocess_exec()`` so they do not
block the event loop — critical for the orchestrator and Discord bot which
share a single-threaded asyncio event loop.  Synchronous methods are preserved
for backward compatibility and non-async callers.

Key workflows:
  - **Clone repos:** ``create_checkout`` clones a project's repository.
  - **Prepare task branches:** ``prepare_for_task`` fetches latest, creates a
    fresh branch off the default branch (handling both normal repos and
    worktrees).
  - **Commit agent work:** ``commit_all`` stages everything and commits if
    there are changes.
  - **Push and PR:** ``push_branch`` pushes to origin; ``create_pr`` and
    ``check_pr_merged`` delegate to the ``gh`` CLI for GitHub PR operations.

Design strengths (see specs/git/git.md §10 for the full list):
  - **Fresh starting point:** ``prepare_for_task`` always fetches remote state
    before creating a task branch, so agents start from recent code.
  - **Worktree-aware:** Detects worktrees and avoids default-branch checkout
    conflicts automatically.
  - **Retry-resilient:** Existing branches are reused on task retry, never
    fail with "branch already exists".
  - **Graceful degradation:** Operations that may legitimately fail (no remote,
    no upstream) are caught and suppressed rather than propagated.
  - **Atomic commits:** ``commit_all`` uses add-then-check-staged to avoid
    race conditions between status checks and staging.

Resolved gaps:
  - **G1 (resolved):** ``merge_branch`` now fetches and hard-resets
    ``origin/<default_branch>`` before merging, and ``_merge_and_push``
    resets local main on push failure to avoid diverged state.
  - **G2 (resolved):** ``recover_workspace`` resets the local default branch
    to ``origin/<default_branch>`` after any failed merge-and-push, ensuring
    the workspace is clean for the next task.
  - **G4 (resolved):** ``prepare_for_task`` now uses hard-reset on the normal
    path and rebases existing branches on retry. ``switch_to_branch`` also
    rebases onto ``origin/<default_branch>`` after switching.

Resolved gaps (continued):
  - **G3 (resolved):** ``sync_and_merge`` now attempts rebase-before-merge
    when a direct merge fails with conflicts.  The task branch is rebased
    onto ``origin/<default_branch>`` and the merge retried.  If the rebase
    itself conflicts, the original ``merge_conflict`` error is returned.

Resolved gaps (continued):
  - **G5 (resolved):** ``push_branch`` now accepts a ``force_with_lease``
    keyword argument.  When ``True``, uses ``--force-with-lease`` for
    idempotent retries of PR branches.  The orchestrator passes this flag
    when pushing task branches for PR creation.

Resolved gaps (continued):
  - **G6 (resolved):** ``mid_chain_sync`` pushes intermediate subtask work
    to the remote and rebases the chain branch onto ``origin/<default_branch>``
    between subtask completions.  The orchestrator calls this after each
    non-final subtask when mid-chain rebasing was enabled (the config knob
    was retired with the plan-discovery flow), reducing drift for long chains.

See specs/git/git.md for the full behavioral specification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.event_bus import EventBus

logger = logging.getLogger(__name__)


class GitError(Exception):
    pass


@dataclass(frozen=True)
class PullRequestIdentity:
    """Immutable PR facts that must agree from review through merge."""

    repository: str
    number: int
    base_ref: str
    base_oid: str
    head_ref: str
    head_oid: str


# ---------------------------------------------------------------------------
# Refname validation — trust rule R4 (docs/specs/design/trust-and-ops.md §2.4)
# ---------------------------------------------------------------------------

#: Conservative subset of ``git check-ref-format``: must start with an
#: alphanumeric (so a name can never be read as an option), and may then
#: contain letters, digits, ``.``, ``_``, ``/`` and ``-``.  Whitespace, ``..``,
#: shell metacharacters and a leading ``-`` are all rejected.
_REFNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
#: ``https://<host>/<owner>/<repo>/pull/<n>`` with an optional trailing slash,
#: query or fragment — the shape ``gh pr create`` prints and ``gh pr merge``
#: accepts.  Owner and repo use the same alphabet as :data:`_REPOSITORY_RE`.
_PR_URL_RE = re.compile(
    r"^https://(?P<host>[A-Za-z0-9.-]+)/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"/pull/(?P<number>[1-9][0-9]*)/?(?:[?#].*)?$"
)


def _validate_ref(name: str, *, field: str = "branch") -> str:
    """Return *name* unchanged, or raise :class:`GitError`.

    Branch/ref names reach git as **positional** arguments.  System-generated
    names (``aq/<task-id>``) are safe, but ``base_branch`` and friends can
    arrive from task metadata — untrusted text by §2.2 — and a value beginning
    with ``-`` would be parsed as an option rather than a ref.  Every
    ref-accepting API validates before spawning git.

    The regex is deliberately narrower than ``git check-ref-format``; a
    legitimate but exotic branch name is fixed by renaming the branch, not by
    loosening the guard.
    """
    if not isinstance(name, str) or not name:
        raise GitError(f"invalid {field} name: empty value")
    if not _REFNAME_RE.match(name):
        raise GitError(
            f"invalid {field} name {name!r}: must start with a letter or digit and "
            "contain only letters, digits, '.', '_', '/' and '-' "
            "(git check-ref-format subset; blocks argument injection)"
        )
    if ".." in name or name.endswith(".lock") or name.endswith("/"):
        raise GitError(f"invalid {field} name {name!r}: rejected by git check-ref-format rules")
    return name


#: A *revision expression* — a refname plus git's navigation suffixes
#: (``HEAD~1``, ``HEAD^``, ``main@{yesterday}``).  Same anchor as
#: :data:`_REFNAME_RE`: the first character must be a letter or digit, so a
#: value beginning with ``-`` still cannot be parsed as an option.  Shell
#: metacharacters, whitespace and quotes remain excluded.
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}~^-]*$")

#: A branch-name *prefix* (``aq/``).  Same anchor as :data:`_REFNAME_RE`
#: but a trailing ``/`` is legal — a prefix is not itself a refname.
_BRANCH_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _validate_rev(name: str, *, field: str = "revision") -> str:
    """Like :func:`_validate_ref`, but for **read-only** revision arguments.

    ``git diff`` legitimately takes revision expressions, and the tool schemas
    the LLM reads advertise them (``vibecop``'s ``diff_ref`` names ``HEAD~3``).
    Validating those with :func:`_validate_ref` turns an advertised, harmless
    call into an error dict — which teaches the agent the tool is broken
    rather than that the input was wrong.

    The argument-injection property is unchanged: the first character must
    still be alphanumeric, so a leading ``-`` remains impossible, and the
    character class adds only git's own revision syntax.  Write paths
    (checkout, push, delete, merge) keep the stricter :func:`_validate_ref` —
    there is no reason to move a branch to ``HEAD@{2}`` through this API.
    """
    if not isinstance(name, str) or not name:
        raise GitError(f"invalid {field}: empty value")
    if not _REVISION_RE.match(name):
        raise GitError(
            f"invalid {field} {name!r}: must start with a letter or digit and contain "
            "only refname characters plus git's revision suffixes "
            "('~', '^', '@', '{', '}') — blocks argument injection"
        )
    if name.endswith(".lock"):
        raise GitError(f"invalid {field} {name!r}: rejected by git check-ref-format rules")
    return name


class GitManager:
    # Environment overrides for all git/gh subprocess calls.  Prevents
    # interactive credential prompts that would otherwise write directly to
    # /dev/tty, bypassing capture_output and flooding the terminal (or
    # freezing WSL entirely when the daemon runs headless).
    # NOTE (trust-and-ops §2.5): this inherits the full daemon environment.
    # Acceptable because git/gh here are daemon-side tools, not agent
    # sessions — R6 scrubbing applies to agent subprocesses
    # (``src.env_scrub.scrub_env``).  Revisit when worktree-execution
    # centralizes git invocation.
    _SUBPROCESS_ENV: dict[str, str] = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",  # git: never prompt for credentials
        "GIT_ASKPASS": "/bin/false",  # git: reject askpass-based prompts
        "GH_PROMPT_DISABLED": "1",  # gh CLI: never prompt interactively
    }

    # Default timeout (seconds) for git operations.  Clone/fetch can be slow
    # on large repos so we allow a generous window, but never infinite.
    _GIT_TIMEOUT = 120

    # Git subcommands that modify shared repository state (pack files,
    # object store) and must be serialized when multiple worktrees share
    # the same underlying repository.  ``pull`` includes an implicit
    # ``fetch`` and therefore also needs serialization.
    _SERIALIZED_SUBCOMMANDS: frozenset[str] = frozenset({"fetch", "gc", "pull"})

    def __init__(self) -> None:
        # Optional lock provider for serializing shared git operations
        # across branch-isolated worktrees.  When set, ``_arun`` acquires
        # the returned lock before executing serialized subcommands.
        self._lock_provider: Callable[[str], asyncio.Lock | None] | None = None

    def set_lock_provider(
        self,
        provider: Callable[[str], asyncio.Lock | None] | None,
    ) -> None:
        """Register a callback that resolves a workspace path to a shared lock.

        The provider receives the ``cwd`` argument from ``_arun`` and should
        return an :class:`asyncio.Lock` if the path belongs to a shared
        repository (e.g. a branch-isolated workspace or one of its
        worktrees), or ``None`` if no serialization is needed.
        """
        self._lock_provider = provider

    def _run(self, args: list[str], cwd: str | None = None, timeout: int | None = None) -> str:
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=cwd,
                capture_output=True,
                text=True,
                env=self._SUBPROCESS_ENV,
                timeout=timeout or self._GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise GitError(
                f"git {' '.join(args)} timed out after "
                f"{timeout or self._GIT_TIMEOUT}s (possible credential prompt)"
            )
        if result.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    async def _arun(
        self, args: list[str], cwd: str | None = None, timeout: int | None = None
    ) -> str:
        """Async version of :meth:`_run` using ``asyncio.create_subprocess_exec``.

        Does not block the event loop — suitable for use from the orchestrator
        and Discord bot coroutines.

        When a :meth:`set_lock_provider` callback is registered and the git
        subcommand is in :attr:`_SERIALIZED_SUBCOMMANDS`, the returned lock
        is acquired before the subprocess executes.  This serializes shared
        operations (fetch, gc, pull) across branch-isolated worktrees that
        share the same underlying git object store.
        """
        lock: asyncio.Lock | None = None
        if self._lock_provider and cwd and args and args[0] in self._SERIALIZED_SUBCOMMANDS:
            lock = self._lock_provider(cwd)

        if lock is not None:
            async with lock:
                return await self._arun_unlocked(args, cwd, timeout)
        return await self._arun_unlocked(args, cwd, timeout)

    async def _arun_unlocked(
        self, args: list[str], cwd: str | None = None, timeout: int | None = None
    ) -> str:
        """Execute a git command without lock acquisition.

        This is the raw subprocess implementation.  Most callers should use
        :meth:`_arun` which adds automatic serialization for shared git
        operations.  Use ``_arun_unlocked`` only when the caller has already
        acquired the appropriate lock (e.g. for compound operations that need
        a single lock scope spanning multiple git commands).
        """
        effective_timeout = timeout or self._GIT_TIMEOUT
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._SUBPROCESS_ENV,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass  # Process already exited before we could kill it
                await proc.wait()
                raise GitError(
                    f"git {' '.join(args)} timed out after "
                    f"{effective_timeout}s (possible credential prompt)"
                )
        except FileNotFoundError as exc:
            if cwd is not None and not Path(cwd).is_dir():
                raise GitError(f"git working directory does not exist: {cwd}") from exc
            raise GitError("git executable not found") from exc
        stdout_str = stdout.decode(errors="replace").strip()
        stderr_str = stderr.decode(errors="replace").strip()
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {stderr_str}")
        return stdout_str

    async def _arun_subprocess(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Async helper for non-git commands (e.g. ``gh`` CLI).

        Returns a :class:`subprocess.CompletedProcess`-compatible object so
        callers can inspect ``returncode``, ``stdout``, and ``stderr``.
        """
        effective_timeout = timeout or self._GIT_TIMEOUT
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._SUBPROCESS_ENV,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass  # Process already exited before we could kill it
                await proc.wait()
                raise subprocess.TimeoutExpired(cmd, effective_timeout)
        except FileNotFoundError:
            raise FileNotFoundError(f"{cmd[0]} executable not found")
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    def create_checkout(self, repo_url: str, checkout_path: str) -> None:
        os.makedirs(os.path.dirname(checkout_path), exist_ok=True)
        self._run(["clone", repo_url, checkout_path])

    def validate_checkout(self, checkout_path: str) -> bool:
        if not os.path.isdir(checkout_path):
            return False
        try:
            self._run(["rev-parse", "--git-dir"], cwd=checkout_path)
            return True
        except GitError:
            return False

    def _is_worktree(self, checkout_path: str) -> bool:
        """Check if the given path is a git worktree (not the main working tree)."""
        try:
            # In a worktree, git-dir points to .git/worktrees/<name>
            # In a normal repo, git-dir is just .git
            git_dir = self._run(["rev-parse", "--git-dir"], cwd=checkout_path)
            return "worktrees" in git_dir
        except GitError:
            return False

    def has_remote(self, checkout_path: str, remote: str = "origin") -> bool:
        """Check if the given remote exists in the repository."""
        try:
            self._run(["remote", "get-url", remote], cwd=checkout_path)
            return True
        except GitError:
            return False

    def create_branch(self, checkout_path: str, branch_name: str) -> None:
        try:
            self._run(["checkout", "-b", branch_name], cwd=checkout_path)
        except GitError:
            # Branch already exists — switch to it
            self._run(["checkout", branch_name], cwd=checkout_path)

    def checkout_branch(self, checkout_path: str, branch_name: str) -> None:
        """Switch to an existing branch."""
        self._run(["checkout", branch_name], cwd=checkout_path)

    def list_branches(self, checkout_path: str) -> list[str]:
        """Return a list of local branch names. Current branch is prefixed with '*'."""
        try:
            output = self._run(["branch", "--list"], cwd=checkout_path)
            return [line.strip() for line in output.split("\n") if line.strip()]
        except GitError:
            return []

    def pull_latest_main(
        self,
        checkout_path: str,
        default_branch: str = "main",
    ) -> None:
        """Fetch from origin and hard-reset the default branch to match remote.

        Encapsulates the fetch + hard-reset pattern so callers can ensure their
        local default branch exactly matches ``origin/<default_branch>``, even
        if previous merge commits or failed operations left it diverged.

        This is safer than ``git pull`` because pull can fail when the local
        branch has diverged (e.g. from un-pushed merge commits left by
        ``_merge_and_push``). A hard reset unconditionally moves the branch
        pointer to match the remote.

        Must be called while the default branch is checked out (for normal
        repos) or used in worktree-aware callers that skip checkout.
        """
        self._run(["fetch", "origin"], cwd=checkout_path)
        self._run(["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path)

    def _rebase_onto_default(
        self,
        checkout_path: str,
        default_branch: str = "main",
    ) -> None:
        """Attempt to rebase the current branch onto ``origin/<default_branch>``.

        If the rebase encounters conflicts, it is aborted and the branch is
        left as-is. The agent can still work with the branch in its current
        state — it just won't have the latest main changes incorporated.
        """
        try:
            self._run(["rebase", f"origin/{default_branch}"], cwd=checkout_path)
        except GitError:
            # Conflicts during rebase — abort and leave branch as-is.
            # The agent can still work with the branch; it just won't
            # have the latest main changes incorporated.
            try:
                self._run(["rebase", "--abort"], cwd=checkout_path)
            except GitError:
                pass  # rebase may not be in progress if it failed early

    def prepare_for_task(
        self,
        checkout_path: str,
        branch_name: str,
        default_branch: str = "main",
    ) -> None:
        """Fetch latest and create a task branch off the default branch.

        Two code paths depending on whether the checkout is a worktree:
        - **Normal repo:** checkout default branch, hard-reset to
          ``origin/<default_branch>``, then create the task branch. The hard
          reset ensures we always match remote even if a previous
          ``_merge_and_push`` left local main diverged.
        - **Worktree:** Can't checkout the default branch (it's already checked
          out in the main working tree), so we create the task branch directly
          from ``origin/<default_branch>`` in a single step.

        In both cases, if the branch already exists (e.g. task retried after a
        restart), we switch to it and rebase onto ``origin/<default_branch>``
        so the agent starts with the latest upstream changes.
        """
        # Check if this is a worktree
        is_worktree = self._is_worktree(checkout_path)

        self._run(["fetch", "origin"], cwd=checkout_path)

        if is_worktree:
            # In a worktree, we can't checkout the default branch if it's already
            # checked out in the source repo. Instead, fetch updates and create
            # the new branch directly from the remote default branch.
            try:
                self._run(
                    ["checkout", "-b", branch_name, f"origin/{default_branch}"], cwd=checkout_path
                )
            except GitError:
                # Branch already exists (retry) — switch to it and rebase
                # onto latest origin/<default_branch> so agent has fresh code.
                self._run(["checkout", branch_name], cwd=checkout_path)
                self._rebase_onto_default(checkout_path, default_branch)
        else:
            # Normal checkout flow: hard-reset default branch to match remote,
            # then create task branch. Hard reset is used instead of pull
            # because pull can fail when local main has diverged (e.g. from
            # un-pushed merge commits left by _merge_and_push).
            try:
                self._run(["checkout", default_branch], cwd=checkout_path)
            except GitError:
                # The specified default branch doesn't exist locally.
                # This can happen when the caller passed a stale/wrong
                # default_branch value (e.g. "main" when the repo uses
                # "master").  Re-detect and retry once.
                detected = self.get_default_branch(checkout_path)
                if detected != default_branch:
                    default_branch = detected
                    self._run(["checkout", default_branch], cwd=checkout_path)
                else:
                    raise
            self._run(
                ["reset", "--hard", f"origin/{default_branch}"],
                cwd=checkout_path,
            )
            try:
                self._run(["checkout", "-b", branch_name], cwd=checkout_path)
            except GitError:
                # Branch already exists (e.g. task retried after restart) —
                # switch to it and rebase onto latest main so the agent
                # doesn't work on stale code from the previous attempt.
                self._run(["checkout", branch_name], cwd=checkout_path)
                self._rebase_onto_default(checkout_path, default_branch)

    def switch_to_branch(
        self,
        checkout_path: str,
        branch_name: str,
        default_branch: str = "main",
        rebase: bool = False,
    ) -> None:
        """Switch to an existing branch, pulling latest and optionally rebasing.

        Used for subtask branch reuse: when a plan generates multiple subtasks
        that should share a branch, this lets the second task pick up where the
        first left off rather than creating a new branch.

        When *rebase* is ``True``, the branch is rebased onto
        ``origin/<default_branch>`` after switching so subtask chains stay
        closer to main and reduce the chance of merge conflicts when the work
        is eventually merged back.

        If the branch doesn't exist locally or on the remote (e.g. LINK repos
        with no remote), creates it as a new local branch.
        """
        try:
            self._run(["fetch", "origin"], cwd=checkout_path)
        except GitError:
            pass  # may fail if no remote configured
        try:
            self._run(["checkout", branch_name], cwd=checkout_path)
        except GitError:
            # Branch doesn't exist locally — try tracking remote
            try:
                self._run(
                    ["checkout", "-b", branch_name, f"origin/{branch_name}"], cwd=checkout_path
                )
            except GitError:
                # No remote branch either (e.g. LINK repo) — create fresh
                self._run(["checkout", "-b", branch_name], cwd=checkout_path)
        try:
            self._run(["pull", "origin", branch_name], cwd=checkout_path)
        except GitError:
            pass  # may fail if no upstream tracking

        if rebase:
            # Rebase onto origin/<default_branch> so subtask chains stay close
            # to main and reduce merge conflicts later.
            self._rebase_onto_default(checkout_path, default_branch)

    def mid_chain_sync(
        self,
        checkout_path: str,
        branch_name: str,
        default_branch: str = "main",
    ) -> bool:
        """Push intermediate subtask work and rebase onto latest main.

        Called between subtask completions in a chained plan to:

        1. **Push** current commits to remote — saves intermediate work so
           it survives agent crashes and is visible to other clones.
        2. **Rebase** the branch onto ``origin/<default_branch>`` — keeps
           the subtask chain close to main and reduces the chance of large
           merge conflicts when the final subtask merges the accumulated
           work.
        3. **Force-push** the rebased branch — updates the remote ref to
           match the rewritten (rebased) history.

        This resolves **Gap G6** for long subtask chains where drift from
        ``main`` would otherwise accumulate across multiple sequential
        subtask executions.

        Returns ``True`` if the full sync (push + rebase + force-push)
        succeeded.  Returns ``False`` if the rebase conflicted — the branch
        is left in its original pre-rebase state and the initial push may
        still have saved the intermediate work to the remote.

        All failures are non-fatal: callers should catch exceptions and
        continue — the next subtask can still work on the branch as-is.
        """
        # 1. Push current branch commits to remote (saves intermediate work).
        #    First push may fail if the branch hasn't been pushed before or
        #    if a previous mid-chain sync already pushed + rebased, so fall
        #    back to --force-with-lease which is safe for agent-owned branches.
        try:
            self._run(["push", "origin", branch_name], cwd=checkout_path)
        except GitError:
            try:
                self._run(
                    ["push", "--force-with-lease", "origin", branch_name],
                    cwd=checkout_path,
                )
            except GitError:
                pass  # Push failed — continue with rebase anyway

        # 2. Fetch latest remote state so rebase target is up to date.
        self._run(["fetch", "origin"], cwd=checkout_path)

        # 3. Rebase onto origin/<default_branch>.
        try:
            self._run(
                ["rebase", f"origin/{default_branch}"],
                cwd=checkout_path,
            )
        except GitError:
            # Rebase conflicts — abort and leave branch as-is.
            try:
                self._run(["rebase", "--abort"], cwd=checkout_path)
            except GitError:
                pass
            return False

        # 4. Force-push the rebased branch so remote matches local.
        try:
            self._run(
                ["push", "--force-with-lease", "origin", branch_name],
                cwd=checkout_path,
            )
        except GitError:
            pass  # Rebased locally but push failed — next subtask will try

        return True

    def pull_branch(
        self,
        checkout_path: str,
        branch_name: str | None = None,
    ) -> str:
        """Pull (fetch + merge) a branch from the ``origin`` remote.

        If *branch_name* is ``None``, the current branch is used.  Returns the
        name of the branch that was pulled.
        """
        if not branch_name:
            branch_name = self.get_current_branch(checkout_path)
            if not branch_name:
                raise GitError("Could not determine current branch")
        self._run(["pull", "origin", branch_name], cwd=checkout_path)
        return branch_name

    def push_branch(
        self,
        checkout_path: str,
        branch_name: str,
        *,
        force_with_lease: bool = False,
    ) -> None:
        """Push a local branch to the ``origin`` remote.

        When *force_with_lease* is ``True``, uses ``--force-with-lease`` so the
        push is safe for retries: if the branch was already pushed in a
        previous attempt, a second push with amended/additional commits will
        succeed as long as no *other* user pushed to the same branch in the
        meantime.  This resolves **Gap G5** for PR branch pushes.

        Plain push (default) is used for the ``sync_and_merge`` flow where
        only the default branch is pushed and force-push is never appropriate.
        """
        args = ["push", "origin", branch_name]
        if force_with_lease:
            args.insert(2, "--force-with-lease")
        self._run(args, cwd=checkout_path)

    def rebase_onto(
        self,
        checkout_path: str,
        branch_name: str,
        target_branch: str = "main",
    ) -> bool:
        """Rebase branch onto target. Returns True on success, False on conflict.

        Switches to *branch_name*, then rebases it onto
        ``origin/<target_branch>``.  If the rebase encounters conflicts it is
        aborted and the method returns ``False`` — the branch is left in its
        original pre-rebase state.

        Used by :meth:`sync_and_merge` for its rebase-before-merge conflict
        resolution (Gap G3), and available as a public API for callers that
        need to rebase an arbitrary branch onto any target.
        """
        original = self._run(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=checkout_path,
        )
        self._run(["checkout", branch_name], cwd=checkout_path)
        # Use local target branch — callers that need remote state
        # (like sync_and_merge) fetch beforehand.
        rebase_target = target_branch
        try:
            self._run(["rebase", rebase_target], cwd=checkout_path)
            # Return to the original branch so callers find the repo
            # in the same state as before the call.
            self._run(["checkout", original], cwd=checkout_path)
            return True
        except GitError:
            try:
                self._run(["rebase", "--abort"], cwd=checkout_path)
            except GitError:
                pass  # rebase may not be in progress if it failed early
            try:
                self._run(["checkout", original], cwd=checkout_path)
            except GitError:
                pass
            return False

    def merge_branch(
        self,
        checkout_path: str,
        branch_name: str,
        default_branch: str = "main",
    ) -> bool:
        """Merge branch into default. Returns True if successful, False if conflict.

        Checks out the default branch, fetches from origin, and hard-resets
        to ``origin/<default_branch>`` before merging.  This ensures the
        local default branch matches the remote even when other agents have
        pushed since the last fetch (resolves **Gap G1**).

        .. note:: For rebase-before-merge conflict resolution, use
           :meth:`sync_and_merge` which attempts a rebase of the task branch
           onto ``origin/<default_branch>`` when the direct merge fails.
        """
        self._run(["checkout", default_branch], cwd=checkout_path)
        # Pull latest remote state before merging so we don't merge into
        # a stale local copy of the default branch (fixes G1).
        try:
            self._run(["fetch", "origin"], cwd=checkout_path)
            self._run(["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path)
        except GitError:
            pass  # no remote or no tracking branch — use local state as-is
        try:
            self._run(["merge", branch_name], cwd=checkout_path)
            return True
        except GitError:
            self._run(["merge", "--abort"], cwd=checkout_path)
            return False

    def sync_and_merge(
        self,
        checkout_path: str,
        branch_name: str,
        default_branch: str = "main",
        max_retries: int = 1,
    ) -> tuple[bool, str]:
        """Pull latest main, merge branch, push. Returns (success, error_msg).

        Encapsulates the full sync-merge-push flow as a single higher-level
        operation.  Callers (e.g. the orchestrator) no longer need to
        coordinate fetch / checkout / reset / merge / push individually.

        Steps:
          1. Fetch latest remote state.
          2. Checkout the default branch and hard-reset to ``origin/<default_branch>``.
          3. Attempt the merge; on conflict, try rebasing the task branch
             onto ``origin/<default_branch>`` and retry the merge once.
             If the rebase itself conflicts or the retry merge still fails,
             return ``merge_conflict``.
          4. Push with up to *max_retries* retries.  On push failure (e.g.
             another agent pushed in the meantime), pull --rebase and retry.
             If all retries are exhausted, return a push failure message.
        """
        # 1. Fetch latest
        self._run(["fetch", "origin"], cwd=checkout_path)

        # 2. Checkout and hard-reset main to origin
        self._run(["checkout", default_branch], cwd=checkout_path)
        self._run(["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path)

        # 3. Attempt merge
        try:
            self._run(["merge", branch_name], cwd=checkout_path)
        except GitError:
            self._run(["merge", "--abort"], cwd=checkout_path)

            # 3a. Direct merge failed — attempt rebase-before-merge.
            # Rebase the task branch onto origin/<default_branch> so it
            # incorporates upstream changes, then retry the merge.
            rebased = self.rebase_onto(
                checkout_path,
                branch_name,
                default_branch,
            )
            if not rebased:
                # Rebase itself conflicted — give up
                # Switch back to default branch for a clean state
                self._run(["checkout", default_branch], cwd=checkout_path)
                return (False, "merge_conflict")

            # 3b. Rebase succeeded — retry merge on a fresh default branch
            self._run(["checkout", default_branch], cwd=checkout_path)
            self._run(
                ["reset", "--hard", f"origin/{default_branch}"],
                cwd=checkout_path,
            )
            try:
                self._run(["merge", branch_name], cwd=checkout_path)
            except GitError:
                self._run(["merge", "--abort"], cwd=checkout_path)
                return (False, "merge_conflict")

        # 4. Push with retry
        for attempt in range(max_retries + 1):
            try:
                self._run(["push", "origin", default_branch], cwd=checkout_path)
                return (True, "")
            except GitError as e:
                if attempt < max_retries:
                    # Re-pull (rebase) to incorporate whatever was pushed
                    # in the meantime, then retry the push.
                    self._run(
                        ["pull", "--rebase", "origin", default_branch],
                        cwd=checkout_path,
                    )
                else:
                    return (False, f"push_failed: {e}")

        return (False, "push_failed_exhausted")  # pragma: no cover

    def recover_workspace(
        self,
        checkout_path: str,
        default_branch: str = "main",
    ) -> None:
        """Reset workspace to a clean state after a failed merge-and-push.

        Checks out the default branch and hard-resets it to
        ``origin/<default_branch>`` so the workspace is ready for the
        next task.  This undoes any local merge commit left behind by a
        failed push.

        Best-effort: callers should wrap in try/except if they cannot
        tolerate failures here (e.g. the workspace is in a broken git
        state that even checkout cannot recover from).
        """
        self._run(["checkout", default_branch], cwd=checkout_path)
        self._run(
            ["reset", "--hard", f"origin/{default_branch}"],
            cwd=checkout_path,
        )

    def delete_branch(
        self,
        checkout_path: str,
        branch_name: str,
        *,
        delete_remote: bool = True,
    ) -> None:
        """Delete a branch locally and optionally on the remote."""
        try:
            self._run(["branch", "-d", branch_name], cwd=checkout_path)
        except GitError:
            # Force-delete if not fully merged (e.g. squash-merged PR)
            try:
                self._run(["branch", "-D", branch_name], cwd=checkout_path)
            except GitError:
                pass  # branch may not exist locally
        if delete_remote:
            try:
                self._run(["push", "origin", "--delete", branch_name], cwd=checkout_path)
            except GitError:
                pass  # branch may not exist on remote (already deleted)

    def create_worktree(self, source_path: str, worktree_path: str, branch: str) -> None:
        """Create a git worktree for agent isolation on linked repos."""
        os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
        self._run(["worktree", "add", "-b", branch, worktree_path], cwd=source_path)

    def remove_worktree(self, source_path: str, worktree_path: str) -> None:
        """Remove a git worktree."""
        try:
            self._run(["worktree", "remove", worktree_path], cwd=source_path)
        except GitError:
            # Force remove if normal remove fails
            self._run(["worktree", "remove", "--force", worktree_path], cwd=source_path)

    def init_repo(self, path: str) -> None:
        """Initialize a new git repo with an empty initial commit."""
        os.makedirs(path, exist_ok=True)
        self._run(["init"], cwd=path)
        self._run(["commit", "--allow-empty", "-m", "Initial commit"], cwd=path)

    def get_diff(self, checkout_path: str, base_branch: str = "main") -> str:
        """Return the full diff against base branch."""
        try:
            return self._run(["diff", base_branch], cwd=checkout_path)
        except GitError:
            return ""

    def get_git_path(self, checkout_path: str, path: str) -> str:
        """Resolve a Git-internal *path* to an absolute filesystem path."""
        try:
            return self._run(
                ["rev-parse", "--path-format=absolute", "--git-path", path],
                cwd=checkout_path,
            )
        except GitError:
            git_path = self._run(["rev-parse", "--git-path", path], cwd=checkout_path)
            return git_path if os.path.isabs(git_path) else os.path.abspath(
                os.path.join(checkout_path, git_path)
            )

    def get_changed_files(self, checkout_path: str, base_branch: str = "main") -> list[str]:
        try:
            output = self._run(["diff", "--name-only", base_branch], cwd=checkout_path)
            return output.split("\n") if output else []
        except GitError:
            return []

    # Plan file paths that should never be committed to target repos.
    # These are working files used by the orchestrator's auto-task system;
    # committing them causes duplicate subtask generation when they
    # persist on the default branch.
    _PLAN_FILE_EXCLUDES = [
        ".claude/plan.md",
        ".claude/plans/",
        "plan.md",
    ]
    # Daemon-owned runtime state must never become task work, even when a
    # repository already tracks one of these paths and ignore rules cannot
    # suppress its staged change.
    _DAEMON_BOOKKEEPING_EXCLUDES = [".aq/", ".aq-worktree.json", ".codex/"]
    _DAEMON_BOOKKEEPING_ADD_EXCLUDES = [
        ":(exclude).aq/**",
        ":(exclude).aq-worktree.json",
        ":(exclude).codex/**",
    ]
    _COMMIT_HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit")

    @classmethod
    def _daemon_bookkeeping_paths(cls, cached_output: str) -> list[str]:
        """Return daemon-owned paths from NUL-delimited cached file output."""
        return [
            path
            for path in cached_output.split("\0")
            if path
            and (
                path == ".aq-worktree.json"
                or path.startswith(".aq/")
                or path.startswith(".codex/")
            )
        ]

    def _unstage_daemon_bookkeeping(self, checkout_path: str) -> None:
        """Clear any daemon bookkeeping a caller staged before commit_all."""
        self._run(
            ["reset", "HEAD", "--", *self._DAEMON_BOOKKEEPING_EXCLUDES],
            cwd=checkout_path,
        )

    def _refuse_cached_daemon_bookkeeping(self, checkout_path: str) -> None:
        """Abort before commit if daemon-owned state remains in the index."""
        cached = self._run(
            ["diff", "--cached", "--name-only", "-z", "--"], cwd=checkout_path
        )
        paths = self._daemon_bookkeeping_paths(cached)
        if paths:
            raise GitError(
                "refusing to commit reserved daemon bookkeeping paths: " + ", ".join(paths)
            )

    async def _aunstage_daemon_bookkeeping(self, checkout_path: str) -> None:
        """Async counterpart to :meth:`_unstage_daemon_bookkeeping`."""
        await self._arun(
            ["reset", "HEAD", "--", *self._DAEMON_BOOKKEEPING_EXCLUDES],
            cwd=checkout_path,
        )

    async def _arefuse_cached_daemon_bookkeeping(self, checkout_path: str) -> None:
        """Async counterpart to :meth:`_refuse_cached_daemon_bookkeeping`."""
        cached = await self._arun(
            ["diff", "--cached", "--name-only", "-z", "--"], cwd=checkout_path
        )
        paths = self._daemon_bookkeeping_paths(cached)
        if paths:
            raise GitError(
                "refusing to commit reserved daemon bookkeeping paths: " + ", ".join(paths)
            )

    @classmethod
    @contextmanager
    def _commit_hooks_overlay(cls, hooks_path: str, *, no_verify: bool):
        """Yield a temporary hooks path that seals reserved index entries.

        ``git commit`` still drives its normal hook lifecycle.  Each installed
        user hook is delegated exactly once, and the wrappers for hooks that
        run before the commit is finalized restore daemon-owned paths in the
        index to ``HEAD`` before returning to Git.  An empty overlay makes
        ``no_verify=True`` genuinely hook-free, including hook types that
        Git's own ``--no-verify`` flag does not suppress.
        """
        original_dir = Path(hooks_path)
        with tempfile.TemporaryDirectory(prefix="aq-commit-hooks-") as temp_dir:
            overlay = Path(temp_dir)
            if not no_verify:
                for hook_name in cls._COMMIT_HOOKS:
                    original = original_dir / hook_name
                    if hook_name != "pre-commit" and not os.access(original, os.X_OK):
                        continue
                    delegate = ""
                    if os.access(original, os.X_OK):
                        delegate = f"{shlex.quote(str(original))} \"$@\" || status=$?\n"
                    wrapper = (
                        "#!/bin/sh\n"
                        "status=0\n"
                        f"{delegate}"
                        "git reset -q HEAD -- .aq/ .aq-worktree.json .codex/\n"
                        "cleanup_status=$?\n"
                        'if test "$status" -ne 0; then exit "$status"; fi\n'
                        'exit "$cleanup_status"\n'
                    )
                    target = overlay / hook_name
                    target.write_text(wrapper, encoding="utf-8")
                    target.chmod(0o700)
            yield str(overlay)

    def commit_all(
        self,
        checkout_path: str,
        message: str,
        *,
        exclude_plans: bool = True,
        no_verify: bool = False,
    ) -> bool:
        """Stage task changes and commit, returning whether a commit was made.

        Uses add-all-then-check-staged pattern, while excluding daemon-owned
        bookkeeping from the initial add and clearing any such paths that
        were already staged.  ``git diff --cached --quiet`` then checks
        whether anything is actually staged.  This avoids the race condition
        of checking status before staging.  ``False`` means no legitimate
        staged task change remained after sanitization; excluded daemon or
        plan paths may still be modified in the working tree.

        Plan files (``.claude/plan.md``, ``plan.md``, ``.claude/plans/``)
        are automatically unstaged to prevent them from being committed to
        target repos unless *exclude_plans* is ``False``.  System-level
        operations (auto-remediation, plan archival, workspace cleanup)
        should pass ``exclude_plans=False`` to ensure all changes are
        committed.

        Pass ``no_verify=True`` to skip all commit hooks.
        This is intended for system-level auto-remediation commits where hook
        failures would prevent workspace cleanup. Otherwise ``git commit``
        runs the repository's native commit hook lifecycle exactly once; a
        temporary hooks overlay removes daemon-owned paths after each hook
        before Git can finalize the commit.
        """
        self._unstage_daemon_bookkeeping(checkout_path)
        self._run(
            ["add", "-A", "--", ".", *self._DAEMON_BOOKKEEPING_ADD_EXCLUDES],
            cwd=checkout_path,
        )
        # Unstage plan files so they never reach target repo history.
        if exclude_plans:
            for pattern in self._PLAN_FILE_EXCLUDES:
                try:
                    self._run(["reset", "HEAD", "--", pattern], cwd=checkout_path)
                except GitError:
                    pass  # Not staged or doesn't exist — fine
        self._refuse_cached_daemon_bookkeeping(checkout_path)
        # git diff --cached --quiet exits 1 if there are staged changes
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=checkout_path,
            capture_output=True,
            env=self._SUBPROCESS_ENV,
            timeout=self._GIT_TIMEOUT,
        )
        if result.returncode == 0:
            return False  # Nothing to commit
        if result.returncode != 1:
            raise GitError(f"git diff --cached --quiet failed: {result.stderr.strip()}")
        hooks_path = self.get_git_path(checkout_path, "hooks")
        with self._commit_hooks_overlay(hooks_path, no_verify=no_verify) as overlay:
            commit_args = ["-c", f"core.hooksPath={overlay}", "commit", "-m", message]
            if no_verify:
                commit_args.insert(-2, "--no-verify")
            try:
                self._run(commit_args, cwd=checkout_path)
            finally:
                self._unstage_daemon_bookkeeping(checkout_path)
                self._refuse_cached_daemon_bookkeeping(checkout_path)
        return True

    def create_pr(
        self,
        checkout_path: str,
        branch: str,
        title: str,
        body: str,
        base: str = "main",
    ) -> str:
        """Create a GitHub PR using the ``gh`` CLI. Returns the PR URL.

        Delegates to ``gh pr create`` rather than the GitHub API directly,
        so the user's existing gh authentication is reused.
        """
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--title",
                    title,
                    "--body",
                    body,
                    "--base",
                    base,
                    "--head",
                    branch,
                ],
                cwd=checkout_path,
                capture_output=True,
                text=True,
                env=self._SUBPROCESS_ENV,
                timeout=self._GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise GitError("gh pr create timed out (possible auth prompt)")
        if result.returncode != 0:
            raise GitError(f"gh pr create failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def check_pr_merged(self, checkout_path: str, pr_url: str) -> bool | None:
        """Check if a PR has been merged via the ``gh`` CLI.

        Returns True (merged), False (still open), None (closed without merge).
        The orchestrator polls this for ``pr-merged`` gates to detect when
        a human merges the PR and the task can be marked COMPLETED.
        """
        try:
            result = subprocess.run(
                ["gh", "pr", "view", pr_url, "--json", "state,mergedAt"],
                cwd=checkout_path,
                capture_output=True,
                text=True,
                env=self._SUBPROCESS_ENV,
                timeout=self._GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise GitError("gh pr view timed out (possible auth prompt)")
        if result.returncode != 0:
            raise GitError(f"gh pr view failed: {result.stderr.strip()}")
        data = json.loads(result.stdout)
        state = data.get("state", "").upper()
        if state == "MERGED" or data.get("mergedAt"):
            return True
        if state == "OPEN":
            return False
        # CLOSED without merge
        return None

    def get_status(self, checkout_path: str) -> str:
        """Return the output of `git status` for the given repository path."""
        try:
            return self._run(["status"], cwd=checkout_path)
        except GitError:
            return ""

    def get_current_branch(self, checkout_path: str) -> str:
        """Return the current branch name."""
        try:
            return self._run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout_path)
        except GitError:
            return ""

    def has_non_plan_changes(
        self,
        checkout_path: str,
        default_branch: str = "main",
        min_files: int = 3,
        min_lines: int = 50,
    ) -> bool:
        """Check if the branch has substantial code changes beyond plan files.

        Compares the current HEAD against the merge-base with the default
        branch, excluding plan file paths from the diff.  Returns True if
        the diff exceeds the given thresholds (files changed or lines
        changed), indicating the plan was likely already implemented.

        Returns False (conservative) on any git error so callers fall
        through to normal task-generation behaviour.
        """
        try:
            # Find merge-base between current HEAD and default branch
            merge_base = self._run(
                ["merge-base", f"origin/{default_branch}", "HEAD"],
                cwd=checkout_path,
            )
        except GitError:
            # No merge-base available (e.g. shallow clone, no remote) — be
            # conservative and allow task generation.
            return False

        try:
            # Get diff stat excluding plan files
            stat_output = self._run(
                [
                    "diff",
                    "--stat",
                    f"{merge_base}..HEAD",
                    "--",
                    ".",
                    ":!.claude/plan.md",
                    ":!plan.md",
                    ":!.claude/plans/",
                ],
                cwd=checkout_path,
            )
        except GitError:
            return False

        if not stat_output:
            return False

        # Parse the summary line, e.g. "5 files changed, 120 insertions(+), 30 deletions(-)"
        # It's always the last line of git diff --stat output.
        lines = stat_output.strip().split("\n")
        summary = lines[-1] if lines else ""

        files_match = re.search(r"(\d+)\s+files?\s+changed", summary)
        insertions_match = re.search(r"(\d+)\s+insertions?", summary)
        deletions_match = re.search(r"(\d+)\s+deletions?", summary)

        files_changed = int(files_match.group(1)) if files_match else 0
        insertions = int(insertions_match.group(1)) if insertions_match else 0
        deletions = int(deletions_match.group(1)) if deletions_match else 0
        total_lines = insertions + deletions

        return files_changed >= min_files or total_lines >= min_lines

    def get_default_branch(self, checkout_path: str) -> str:
        """Detect the default branch for the repository.

        Tries multiple strategies to determine the default branch:
        1. Query the remote HEAD symbolic ref (most reliable)
        2. Check for common default branch names (main, master, develop)
        3. Fall back to the current branch

        Returns the detected default branch name, or "main" as a last resort.
        """
        # Strategy 1: Try to get the default branch from remote HEAD
        try:
            # This works if the remote has a HEAD symbolic ref set
            remote_head = self._run(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=checkout_path)
            # Output format: "refs/remotes/origin/main"
            # Extract just the branch name
            if remote_head.startswith("refs/remotes/origin/"):
                return remote_head.replace("refs/remotes/origin/", "")
        except GitError:
            pass

        # Strategy 2: Check which common default branches exist locally
        for branch in ["main", "master", "develop", "trunk"]:
            try:
                self._run(["rev-parse", "--verify", branch], cwd=checkout_path)
                return branch
            except GitError:
                continue

        # Strategy 3: Check which common default branches exist on remote
        try:
            remote_branches = self._run(["ls-remote", "--heads", "origin"], cwd=checkout_path)
            for branch in ["main", "master", "develop", "trunk"]:
                if f"refs/heads/{branch}" in remote_branches:
                    return branch
        except GitError:
            pass

        # Last resort: use current branch or default to "main"
        current = self.get_current_branch(checkout_path)
        return current if current else "main"

    def get_recent_commits(self, checkout_path: str, count: int = 5) -> str:
        """Return recent commit log (one-line format)."""
        try:
            return self._run(["log", "--oneline", f"-{count}"], cwd=checkout_path)
        except GitError:
            return ""

    def check_gh_auth(self) -> bool:
        """Check if the ``gh`` CLI is authenticated.

        Returns ``True`` if ``gh auth status`` exits successfully, ``False``
        otherwise.  Used to pre-validate before attempting repo creation so
        callers can surface a helpful error message.
        """
        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                env=self._SUBPROCESS_ENV,
                timeout=30,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def create_github_repo(
        self,
        name: str,
        *,
        private: bool = True,
        org: str | None = None,
        description: str = "",
    ) -> str:
        """Create a GitHub repository via the ``gh`` CLI.

        Returns the HTTPS URL of the newly created repository.

        Parameters:
            name:        Repository name (e.g. ``"my-app"``).
            private:     Create a private repo (default ``True``).
            org:         GitHub organization.  ``None`` for a personal repo.
            description: Optional repo description.

        Raises:
            GitError: If ``gh repo create`` fails (auth issues, name conflict,
                      network errors, etc.).
        """
        full_name = f"{org}/{name}" if org else name
        cmd = ["gh", "repo", "create", full_name]
        cmd.append("--private" if private else "--public")
        if description:
            cmd.extend(["--description", description])
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self._SUBPROCESS_ENV,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            raise GitError("gh repo create timed out after 60s (possible auth prompt)")
        if result.returncode != 0:
            raise GitError(f"gh repo create failed: {result.stderr.strip()}")
        # gh repo create prints the repo URL to stdout, but may also include
        # deprecation warnings or other messages.  Extract the URL robustly.
        url = ""
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("https://") or line.startswith("http://"):
                url = line
                break
        if not url:
            # Some gh versions print URL to stderr instead
            for line in reversed(result.stderr.strip().splitlines()):
                line = line.strip()
                if line.startswith("https://") or line.startswith("http://"):
                    url = line
                    break
        if not url:
            raise GitError(
                "gh repo create succeeded but no repository URL was found "
                f"in output: {result.stdout.strip()}"
            )
        return url

    # ------------------------------------------------------------------
    # Async public API
    #
    # Each method below is the async counterpart of its synchronous twin,
    # using ``_arun`` / ``_arun_subprocess`` instead of ``_run`` /
    # ``subprocess.run``.  All production callers (orchestrator, command
    # handler, Discord bot) use these exclusively.  The synchronous API
    # is retained only for backward compatibility and tests.
    # ------------------------------------------------------------------

    async def acreate_checkout(self, repo_url: str, checkout_path: str) -> None:
        os.makedirs(os.path.dirname(checkout_path), exist_ok=True)
        await self._arun(["clone", repo_url, checkout_path])

    async def avalidate_checkout(self, checkout_path: str) -> bool:
        if not os.path.isdir(checkout_path):
            return False
        try:
            await self._arun(["rev-parse", "--git-dir"], cwd=checkout_path)
            return True
        except GitError:
            return False

    async def _ais_worktree(self, checkout_path: str) -> bool:
        try:
            git_dir = await self._arun(["rev-parse", "--git-dir"], cwd=checkout_path)
            return "worktrees" in git_dir
        except GitError:
            return False

    async def ahas_remote(self, checkout_path: str, remote: str = "origin") -> bool:
        try:
            await self._arun(["remote", "get-url", remote], cwd=checkout_path)
            return True
        except GitError:
            return False

    async def aget_remote_url(self, checkout_path: str, remote: str = "origin") -> str | None:
        """Return the URL for *remote*, or ``None`` if no remote is configured."""
        try:
            url = await self._arun(["remote", "get-url", remote], cwd=checkout_path)
            return url.strip() if url and url.strip() else None
        except GitError:
            return None

    async def amerge_base(self, cwd: str, ref_a: str, ref_b: str) -> str:
        """Return the merge-base SHA of two refs (empty string on failure)."""
        _validate_rev(ref_a)
        _validate_rev(ref_b)
        try:
            out = await self._arun(["merge-base", ref_a, ref_b], cwd=cwd)
            return out.strip()
        except GitError:
            return ""

    async def acreate_branch(self, checkout_path: str, branch_name: str) -> None:
        _validate_ref(branch_name)
        try:
            await self._arun(["checkout", "-b", branch_name], cwd=checkout_path)
        except GitError:
            await self._arun(["checkout", branch_name], cwd=checkout_path)

    async def acheckout_branch(self, checkout_path: str, branch_name: str) -> None:
        _validate_ref(branch_name)
        await self._arun(["checkout", branch_name], cwd=checkout_path)

    async def alist_branches(self, checkout_path: str) -> list[str]:
        try:
            output = await self._arun(["branch", "--list"], cwd=checkout_path)
            return [line.strip() for line in output.split("\n") if line.strip()]
        except GitError:
            return []

    async def apull_latest_main(
        self,
        checkout_path: str,
        default_branch: str = "main",
    ) -> None:
        _validate_ref(default_branch, field="default branch")
        await self._arun(["fetch", "origin"], cwd=checkout_path)
        await self._arun(["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path)

    async def _arebase_onto_default(
        self,
        checkout_path: str,
        default_branch: str = "main",
    ) -> None:
        _validate_ref(default_branch, field="default branch")
        try:
            await self._arun(["rebase", f"origin/{default_branch}"], cwd=checkout_path)
        except GitError:
            try:
                await self._arun(["rebase", "--abort"], cwd=checkout_path)
            except GitError:
                pass

    async def aprepare_for_task(
        self,
        checkout_path: str,
        branch_name: str,
        default_branch: str = "main",
    ) -> None:
        _validate_ref(branch_name)
        _validate_ref(default_branch, field="default branch")
        is_worktree = await self._ais_worktree(checkout_path)
        await self._arun(["fetch", "origin"], cwd=checkout_path)

        if is_worktree:
            try:
                await self._arun(
                    ["checkout", "-b", branch_name, f"origin/{default_branch}"],
                    cwd=checkout_path,
                )
            except GitError:
                await self._arun(["checkout", branch_name], cwd=checkout_path)
                await self._arebase_onto_default(checkout_path, default_branch)
        else:
            try:
                await self._arun(["checkout", default_branch], cwd=checkout_path)
            except GitError:
                detected = await self.aget_default_branch(checkout_path)
                if detected != default_branch:
                    default_branch = detected
                    await self._arun(["checkout", default_branch], cwd=checkout_path)
                else:
                    raise
            await self._arun(
                ["reset", "--hard", f"origin/{default_branch}"],
                cwd=checkout_path,
            )
            try:
                await self._arun(["checkout", "-b", branch_name], cwd=checkout_path)
            except GitError:
                await self._arun(["checkout", branch_name], cwd=checkout_path)
                await self._arebase_onto_default(checkout_path, default_branch)

    async def aswitch_to_branch(
        self,
        checkout_path: str,
        branch_name: str,
        default_branch: str = "main",
        rebase: bool = False,
    ) -> None:
        _validate_ref(branch_name)
        _validate_ref(default_branch, field="default branch")
        try:
            await self._arun(["fetch", "origin"], cwd=checkout_path)
        except GitError:
            pass
        try:
            await self._arun(["checkout", branch_name], cwd=checkout_path)
        except GitError:
            try:
                await self._arun(
                    ["checkout", "-b", branch_name, f"origin/{branch_name}"],
                    cwd=checkout_path,
                )
            except GitError:
                await self._arun(["checkout", "-b", branch_name], cwd=checkout_path)
        try:
            await self._arun(["pull", "origin", branch_name], cwd=checkout_path)
        except GitError:
            pass
        if rebase:
            await self._arebase_onto_default(checkout_path, default_branch)

    async def amid_chain_sync(
        self,
        checkout_path: str,
        branch_name: str,
        default_branch: str = "main",
    ) -> bool:
        _validate_ref(branch_name)
        _validate_ref(default_branch, field="default branch")
        try:
            await self.apush_validated_ref(checkout_path, branch_name, branch_name)
        except GitError:
            try:
                await self.apush_validated_ref(
                    checkout_path, branch_name, branch_name, force_with_lease=True
                )
            except GitError:
                pass
        await self._arun(["fetch", "origin"], cwd=checkout_path)
        try:
            await self._arun(
                ["rebase", f"origin/{default_branch}"],
                cwd=checkout_path,
            )
        except GitError:
            try:
                await self._arun(["rebase", "--abort"], cwd=checkout_path)
            except GitError:
                pass
            return False
        try:
            await self.apush_validated_delivery(
                checkout_path,
                f"origin/{default_branch}",
                "HEAD",
                branch_name,
                force_with_lease=True,
            )
        except GitError:
            pass
        return True

    async def apull_branch(
        self,
        checkout_path: str,
        branch_name: str | None = None,
    ) -> str:
        if not branch_name:
            branch_name = await self.aget_current_branch(checkout_path)
            if not branch_name:
                raise GitError("Could not determine current branch")
        _validate_ref(branch_name)
        await self._arun(["pull", "origin", branch_name], cwd=checkout_path)
        return branch_name

    async def apush_branch(
        self,
        checkout_path: str,
        branch_name: str,
        *,
        force_with_lease: bool = False,
        event_bus: EventBus | None = None,
        project_id: str | None = None,
    ) -> None:
        _validate_ref(branch_name)
        # Capture the remote ref before pushing so we can compute commit_range.
        remote_ref_before: str | None = None
        if event_bus is not None:
            try:
                remote_ref_before = await self._arun(
                    ["rev-parse", f"origin/{branch_name}"],
                    cwd=checkout_path,
                )
            except GitError:
                # Remote branch doesn't exist yet (first push).
                remote_ref_before = None

        tip = (
            await self._arun(["rev-parse", "--verify", branch_name], cwd=checkout_path)
        ).strip()
        if not _OID_RE.fullmatch(tip.lower()):
            raise GitError(f"could not resolve immutable delivery tip for {branch_name}")
        args = ["push", "origin", f"{tip}:refs/heads/{branch_name}"]
        if force_with_lease:
            args.insert(2, "--force-with-lease")
        await self._arun(args, cwd=checkout_path)

        # Emit git.push event on success
        if event_bus is not None:
            try:
                if remote_ref_before:
                    commit_range = f"{remote_ref_before}..{tip}"
                else:
                    commit_range = tip
                await event_bus.emit(
                    "git.push",
                    {
                        "branch": branch_name,
                        "remote": "origin",
                        "commit_range": commit_range,
                        "project_id": project_id,
                    },
                )
            except Exception:
                # Event emission is best-effort; never fail the push
                # because we couldn't emit the event.
                logger.debug(
                    "Failed to emit git.push event for %s",
                    checkout_path,
                    exc_info=True,
                )

    async def arebase_onto(
        self,
        checkout_path: str,
        branch_name: str,
        target_branch: str = "main",
    ) -> bool:
        _validate_ref(branch_name)
        _validate_ref(target_branch, field="target branch")
        original = await self._arun(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=checkout_path,
        )
        await self._arun(["checkout", branch_name], cwd=checkout_path)
        rebase_target = target_branch
        try:
            await self._arun(["rebase", rebase_target], cwd=checkout_path)
            await self._arun(["checkout", original], cwd=checkout_path)
            return True
        except GitError:
            try:
                await self._arun(["rebase", "--abort"], cwd=checkout_path)
            except GitError:
                pass
            try:
                await self._arun(["checkout", original], cwd=checkout_path)
            except GitError:
                pass
            return False

    async def amerge_branch(
        self,
        checkout_path: str,
        branch_name: str,
        default_branch: str = "main",
    ) -> bool:
        _validate_ref(branch_name)
        _validate_ref(default_branch, field="default branch")
        await self._arun(["checkout", default_branch], cwd=checkout_path)
        try:
            await self._arun(["fetch", "origin"], cwd=checkout_path)
            await self._arun(["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path)
        except GitError:
            pass
        try:
            await self._arun(["merge", branch_name], cwd=checkout_path)
            return True
        except GitError:
            await self._arun(["merge", "--abort"], cwd=checkout_path)
            return False

    async def async_and_merge(
        self,
        checkout_path: str,
        branch_name: str,
        default_branch: str = "main",
        max_retries: int = 1,
    ) -> tuple[bool, str]:
        _validate_ref(branch_name)
        _validate_ref(default_branch, field="default branch")
        await self._arun(["fetch", "origin"], cwd=checkout_path)
        await self._arun(["checkout", default_branch], cwd=checkout_path)
        await self._arun(["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path)
        try:
            await self._arun(["merge", branch_name], cwd=checkout_path)
        except GitError:
            await self._arun(["merge", "--abort"], cwd=checkout_path)
            rebased = await self.arebase_onto(
                checkout_path,
                branch_name,
                default_branch,
            )
            if not rebased:
                await self._arun(["checkout", default_branch], cwd=checkout_path)
                return (False, "merge_conflict")
            await self._arun(["checkout", default_branch], cwd=checkout_path)
            await self._arun(
                ["reset", "--hard", f"origin/{default_branch}"],
                cwd=checkout_path,
            )
            try:
                await self._arun(["merge", branch_name], cwd=checkout_path)
            except GitError:
                await self._arun(["merge", "--abort"], cwd=checkout_path)
                return (False, "merge_conflict")
        for attempt in range(max_retries + 1):
            try:
                await self.apush_validated_delivery(
                    checkout_path, f"origin/{default_branch}", "HEAD", default_branch
                )
                return (True, "")
            except GitError as e:
                if str(e).startswith("reserved delivery paths:"):
                    return (False, f"delivery_guard_failed: {e}")
                if attempt < max_retries:
                    await self._arun(
                        ["pull", "--rebase", "origin", default_branch],
                        cwd=checkout_path,
                    )
                else:
                    return (False, f"push_failed: {e}")
        return (False, "push_failed_exhausted")  # pragma: no cover

    async def arecover_workspace(
        self,
        checkout_path: str,
        default_branch: str = "main",
    ) -> None:
        _validate_ref(default_branch, field="default branch")
        await self._arun(["checkout", default_branch], cwd=checkout_path)
        await self._arun(
            ["reset", "--hard", f"origin/{default_branch}"],
            cwd=checkout_path,
        )

    async def adelete_branch(
        self,
        checkout_path: str,
        branch_name: str,
        *,
        delete_remote: bool = True,
    ) -> None:
        _validate_ref(branch_name)
        try:
            await self._arun(["branch", "-d", branch_name], cwd=checkout_path)
        except GitError:
            try:
                await self._arun(["branch", "-D", branch_name], cwd=checkout_path)
            except GitError:
                pass
        if delete_remote:
            try:
                await self._arun(["push", "origin", "--delete", branch_name], cwd=checkout_path)
            except GitError:
                pass

    async def acreate_worktree(
        self,
        source_path: str,
        worktree_path: str,
        branch: str,
    ) -> None:
        _validate_ref(branch)
        os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
        await self._arun(["worktree", "add", "-b", branch, worktree_path], cwd=source_path)

    async def aremove_worktree(self, source_path: str, worktree_path: str) -> None:
        try:
            await self._arun(["worktree", "remove", worktree_path], cwd=source_path)
        except GitError:
            await self._arun(["worktree", "remove", "--force", worktree_path], cwd=source_path)

    # ── Worktree slots (worktree-execution spec §4) ───────────────────────
    #
    # The primitives WorktreeSlotManager composes.  Every ref-accepting
    # argument is guarded (trust-and-ops §2.2): slot refs are system-generated
    # today, but ``base_branch`` reaches them from task metadata, which is
    # untrusted text.

    async def aworktree_add(
        self,
        base_path: str,
        worktree_path: str,
        *,
        ref: str,
        detach: bool = True,
    ) -> None:
        """Add a worktree at *worktree_path* checked out at *ref*.

        ``detach=True`` (the default, and what slot creation uses) claims no
        branch — branches are per task, created later by
        ``reset_slot_for_task``.  ``detach=False`` checks *ref* out as a
        branch, which git refuses if it is already checked out elsewhere.
        """
        _validate_ref(ref, field="worktree ref")
        Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
        args = ["worktree", "add"]
        if detach:
            args.append("--detach")
        args += [worktree_path, ref]
        await self._arun(args, cwd=base_path)

    async def aworktree_prune(self, base_path: str) -> None:
        """Drop ``.git/worktrees`` registrations whose directory is gone."""
        await self._arun(["worktree", "prune"], cwd=base_path)

    async def aworktree_list(self, base_path: str) -> list[dict]:
        """Parsed ``git worktree list --porcelain``.

        Each entry has ``path`` plus whichever of ``head`` / ``branch`` /
        ``detached`` / ``bare`` / ``locked`` / ``prunable`` git reported.
        ``branch`` is shortened from ``refs/heads/x`` to ``x``.
        """
        out = await self._arun(["worktree", "list", "--porcelain"], cwd=base_path)
        entries: list[dict] = []
        current: dict | None = None
        for raw in out.splitlines():
            line = raw.rstrip()
            if not line:
                if current:
                    entries.append(current)
                    current = None
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                if current:
                    entries.append(current)
                current = {"path": value}
            elif current is None:
                continue
            elif key == "HEAD":
                current["head"] = value
            elif key == "branch":
                current["branch"] = (
                    value[len("refs/heads/") :]
                    if value.startswith("refs/heads/")
                    else value
                )
            else:
                # Valueless flags (detached, bare) and valued ones
                # (locked <reason>, prunable <reason>).
                current[key] = value or True
        if current:
            entries.append(current)
        return entries

    async def alist_merged_branches(
        self,
        base_path: str,
        *,
        into: str,
        prefix: str = "aq/",
    ) -> list[str]:
        """Local branches already merged into *into*, filtered by *prefix*.

        The target itself is excluded — deleting it is never what the caller
        meant.
        """
        _validate_ref(into, field="merge target")
        # An empty prefix means "no filter" — every local branch merged into
        # *into* is a candidate.  Anything else must look like a refname stem.
        if prefix and not _BRANCH_PREFIX_RE.match(prefix):
            raise GitError(
                f"invalid branch prefix {prefix!r}: must start with a letter or "
                "digit and contain only letters, digits, '.', '_', '/' and '-'"
            )
        out = await self._arun(
            ["branch", "--merged", into, "--format=%(refname:short)"],
            cwd=base_path,
        )
        merged = []
        for line in out.splitlines():
            name = line.strip()
            if not name or name == into or not name.startswith(prefix):
                continue
            merged.append(name)
        return merged

    async def adelete_local_branch(
        self,
        base_path: str,
        branch: str,
        *,
        force: bool = False,
    ) -> None:
        """Delete a *local* branch.  Never touches the remote.

        Distinct from :meth:`adelete_branch`, which also runs
        ``push origin --delete`` by default — remote pruning is a separate,
        policy-gated concern (``worktrees.prune_remote_branches``).
        """
        _validate_ref(branch)
        await self._arun(["branch", "-D" if force else "-d", branch], cwd=base_path)

    async def aworktree_base_path(self, path: str) -> str | None:
        """Resolve the base repository directory for a worktree *path*.

        Uses ``git rev-parse --git-common-dir`` rather than a directory naming
        convention, so it works for any layout — worktree-execution §7.4
        retires the ``.worktrees-<base>/`` path parsing.  Returns ``None``
        when *path* is not inside a git repository.
        """
        try:
            out = await self._arun(
                ["rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=path,
            )
        except GitError:
            return None
        common = Path(out.strip())
        if common.name == ".git":
            return str(common.parent)
        # Bare repo, or a layout where the common dir *is* the repo.
        return str(common)

    async def aget_git_path(self, checkout_path: str, path: str) -> str:
        """Resolve a Git-internal *path* to an absolute filesystem path."""
        try:
            return await self._arun(
                ["rev-parse", "--path-format=absolute", "--git-path", path],
                cwd=checkout_path,
            )
        except GitError:
            # ``--path-format=absolute`` is unavailable on older Git. The
            # older ``--git-path`` still locates separate-git-dir layouts;
            # make its relative result absolute against the checkout.
            git_path = await self._arun(["rev-parse", "--git-path", path], cwd=checkout_path)
            return git_path if os.path.isabs(git_path) else os.path.abspath(
                os.path.join(checkout_path, git_path)
            )

    async def ainit_repo(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)
        await self._arun(["init"], cwd=path)
        await self._arun(["commit", "--allow-empty", "-m", "Initial commit"], cwd=path)

    async def aget_diff(
        self,
        checkout_path: str,
        base_branch: str = "main",
        to_ref: str | None = None,
        *,
        name_status: bool = False,
        numstat: bool = False,
    ) -> str:
        """Return diff output.

        Two-arg form (legacy) ``aget_diff(cwd, base)`` diffs the
        working-tree against ``base_branch`` — used by plugins to
        preview local edits.

        Three-arg form ``aget_diff(cwd, from_ref, to_ref)`` diffs
        ``from_ref..to_ref`` — used by task-files sidebar. ``name_status``
        toggles ``--name-status``; ``numstat`` toggles ``--numstat``.
        Both can be set but the two formats interleave awkwardly;
        callers pick one.

        Refs are validated with :func:`_validate_rev` so revision
        expressions (``HEAD~1``, ``origin/main``) pass while shell
        injection shapes are rejected.
        """
        _validate_rev(base_branch, field="base branch")
        if to_ref is not None:
            _validate_rev(to_ref, field="to ref")
        args = ["diff"]
        if name_status:
            args.append("--name-status")
        if numstat:
            args.append("--numstat")
        if to_ref is not None:
            args.append(f"{base_branch}..{to_ref}")
        else:
            args.extend([base_branch, "--"])
        try:
            return await self._arun(args, cwd=checkout_path)
        except GitError:
            # Legacy two-arg form swallowed errors as empty string;
            # preserve that. The three-arg form re-raises because
            # callers (task_files) need to distinguish diff failure.
            if to_ref is None:
                return ""
            raise

    async def aget_changed_files(
        self,
        checkout_path: str,
        base_branch: str = "main",
    ) -> list[str]:
        # Read-only: revision expressions (HEAD~1, HEAD^, main@{1}) allowed.
        _validate_rev(base_branch, field="base branch")
        try:
            output = await self._arun(
                ["diff", "--name-only", base_branch, "--"], cwd=checkout_path
            )
            return output.split("\n") if output else []
        except GitError:
            return []

    async def acommit_all(
        self,
        checkout_path: str,
        message: str,
        *,
        exclude_plans: bool = True,
        no_verify: bool = False,
        event_bus: EventBus | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> bool:
        """Async version of :meth:`commit_all`.

        See :meth:`commit_all` for parameter docs.  Pass
        ``exclude_plans=False`` for system-level operations that need
        to commit all changes including plan files.

        Pass ``no_verify=True`` to skip all commit hooks.
        This is intended for system-level auto-remediation commits where hook
        failures would prevent workspace cleanup. Otherwise ``git commit``
        runs the repository's native commit hook lifecycle exactly once while
        daemon-owned paths are removed after every hook boundary.

        When *event_bus* is provided, a ``git.commit`` event is emitted
        after a successful commit with the commit hash, branch, changed
        files, message, and optional *project_id* / *agent_id*.

        **Trust boundary (R4, flag-value case).**  *message* is agent-authored
        and therefore untrusted, but it only ever reaches git as the value of
        the ``-m`` flag in an argv list — never interpolated into a shell
        string and never in a position git could read as an option.  The
        ``["reset", "HEAD", "--", path]`` call below is the template for
        pathspec arguments.  See ``docs/specs/design/trust-and-ops.md`` §2.4.
        """
        await self._aunstage_daemon_bookkeeping(checkout_path)
        await self._arun(
            ["add", "-A", "--", ".", *self._DAEMON_BOOKKEEPING_ADD_EXCLUDES],
            cwd=checkout_path,
        )
        if exclude_plans:
            for pattern in self._PLAN_FILE_EXCLUDES:
                try:
                    await self._arun(["reset", "HEAD", "--", pattern], cwd=checkout_path)
                except GitError:
                    pass
        await self._arefuse_cached_daemon_bookkeeping(checkout_path)
        result = await self._arun_subprocess(
            ["git", "diff", "--cached", "--quiet"],
            cwd=checkout_path,
            timeout=self._GIT_TIMEOUT,
        )
        if result.returncode == 0:
            return False
        if result.returncode != 1:
            raise GitError(f"git diff --cached --quiet failed: {result.stderr.strip()}")
        hooks_path = await self.aget_git_path(checkout_path, "hooks")
        with self._commit_hooks_overlay(hooks_path, no_verify=no_verify) as overlay:
            commit_args = ["-c", f"core.hooksPath={overlay}", "commit", "-m", message]
            if no_verify:
                commit_args.insert(-2, "--no-verify")
            try:
                await self._arun(commit_args, cwd=checkout_path)
            finally:
                await self._aunstage_daemon_bookkeeping(checkout_path)
                await self._arefuse_cached_daemon_bookkeeping(checkout_path)

        # Emit git.commit event on success
        if event_bus is not None:
            try:
                commit_hash = await self._arun(["rev-parse", "HEAD"], cwd=checkout_path)
                branch = await self._arun(["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout_path)
                # Get the list of files changed in the commit we just made
                changed_output = await self._arun(
                    ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
                    cwd=checkout_path,
                )
                changed_files = [f for f in changed_output.splitlines() if f]
                await event_bus.emit(
                    "git.commit",
                    {
                        "commit_hash": commit_hash,
                        "branch": branch,
                        "changed_files": changed_files,
                        "message": message,
                        "project_id": project_id,
                        "agent_id": agent_id,
                    },
                )
            except Exception:
                # Event emission is best-effort; never fail the commit
                # because we couldn't emit the event.
                logger.debug(
                    "Failed to emit git.commit event for %s",
                    checkout_path,
                    exc_info=True,
                )

        return True

    async def acreate_pr(
        self,
        checkout_path: str,
        branch: str,
        title: str,
        body: str,
        base: str = "main",
        event_bus: EventBus | None = None,
        project_id: str | None = None,
    ) -> str:
        _validate_ref(branch)
        _validate_ref(base, field="base branch")
        try:
            result = await self._arun_subprocess(
                [
                    "gh",
                    "pr",
                    "create",
                    "--title",
                    title,
                    "--body",
                    body,
                    "--base",
                    base,
                    "--head",
                    branch,
                ],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise GitError("gh pr create timed out (possible auth prompt)")
        if result.returncode != 0:
            raise GitError(f"gh pr create failed: {result.stderr.strip()}")
        pr_url = result.stdout.strip()

        # Emit git.pr.created event on success
        if event_bus is not None:
            try:
                await event_bus.emit(
                    "git.pr.created",
                    {
                        "pr_url": pr_url,
                        "branch": branch,
                        "title": title,
                        "project_id": project_id,
                    },
                )
            except Exception:
                logger.debug(
                    "Failed to emit git.pr.created event for %s",
                    checkout_path,
                    exc_info=True,
                )

        return pr_url

    async def acheck_pr_merged(self, checkout_path: str, pr_url: str) -> bool | None:
        try:
            result = await self._arun_subprocess(
                ["gh", "pr", "view", pr_url, "--json", "state,mergedAt"],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise GitError("gh pr view timed out (possible auth prompt)")
        if result.returncode != 0:
            raise GitError(f"gh pr view failed: {result.stderr.strip()}")
        data = json.loads(result.stdout)
        state = data.get("state", "").upper()
        if state == "MERGED" or data.get("mergedAt"):
            return True
        if state == "OPEN":
            return False
        return None

    async def amerge_pr(
        self,
        checkout_path: str,
        pr_url: str,
        method: str = "squash",
        *,
        expected_head_oid: str | None = None,
        expected_base_oid: str | None = None,
    ) -> dict:
        """Merge a PR via ``gh pr merge``.

        Parameters
        ----------
        checkout_path:
            Any valid checkout of the repo (gh reads the remote from here).
        pr_url:
            Full PR URL, e.g. ``https://github.com/org/repo/pull/42``.
        method:
            One of ``"squash"``, ``"merge"``, ``"rebase"``.  Defaults to
            ``"squash"`` — matches the project convention documented in
            the shipped final-reviewer profile.

        Returns
        -------
        dict
            ``{"success": bool, "sha": str | None, "error": str | None}``.
            ``sha`` is best-effort — gh only prints it in some flows;
            callers who need the merged sha should query the branch head
            after this returns.
        """
        if method not in ("squash", "merge", "rebase"):
            return {"success": False, "sha": None, "error": f"invalid method: {method}"}
        if expected_head_oid is not None:
            expected_head_oid = expected_head_oid.lower()
            if not _OID_RE.fullmatch(expected_head_oid):
                return {"success": False, "sha": None, "error": "invalid expected PR head OID"}
        if expected_base_oid is not None:
            expected_base_oid = expected_base_oid.lower()
            if not _OID_RE.fullmatch(expected_base_oid):
                return {"success": False, "sha": None, "error": "invalid expected PR base OID"}
        try:
            current = await self.avalidate_pr_for_merge(checkout_path, pr_url)
        except GitError as exc:
            return {"success": False, "sha": None, "error": str(exc)}
        if (
            (expected_head_oid is not None and current.head_oid != expected_head_oid)
            or (expected_base_oid is not None and current.base_oid != expected_base_oid)
        ):
            return {
                "success": False,
                "sha": None,
                "error": "PR identity changed after validation; refusing merge",
            }
        expected_head_oid = current.head_oid
        flag = f"--{method}"
        command = ["gh", "pr", "merge", pr_url, flag]
        if expected_head_oid is not None:
            command.extend(["--match-head-commit", expected_head_oid])
        command.append("--delete-branch")
        try:
            result = await self._arun_subprocess(
                command,
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "sha": None, "error": "gh pr merge timed out"}
        if result.returncode != 0:
            return {
                "success": False,
                "sha": None,
                "error": (result.stderr or result.stdout or "gh pr merge failed").strip(),
            }
        # gh prints "Merged pull request #N (<sha>)" in some flows; best-effort parse.
        # Strip surrounding punctuation before checking for a 40-char hex token.
        sha: str | None = None
        for tok in (result.stdout or "").split():
            tok = tok.strip("().,;:")
            if len(tok) == 40 and all(c in "0123456789abcdef" for c in tok):
                sha = tok
                break
        return {"success": True, "sha": sha, "error": None}

    async def acount_commits_not_on_any_remote(self, checkout_path: str) -> int | None:
        """How many commits reachable from ``HEAD`` no remote branch carries.

        ``git rev-list --count HEAD --not --remotes`` is the question "would
        deleting this worktree lose work?" asked exactly: it walks HEAD and
        subtracts every remote-tracking ref, so it is true for a detached
        HEAD, for a branch whose upstream was never set, and for a branch
        pushed under a different name.  ``@{u}`` answers none of those.

        ``None`` means the question could not be answered (no checkout, git
        error) — callers must treat that as "unknown", never as zero.
        """
        try:
            out = await self._arun(
                ["rev-list", "--count", "HEAD", "--not", "--remotes"],
                cwd=checkout_path,
            )
        except GitError:
            return None
        try:
            return int(out.strip())
        except ValueError:
            return None

    async def als_remote_sha(self, checkout_path: str, branch: str) -> str | None:
        """SHA of ``origin/<branch>`` **as the remote has it right now**.

        Asks the remote (``git ls-remote``) rather than a remote-tracking
        ref: the caller is about to decide whether a push would clobber
        somebody else's work, and a stale ``refs/remotes/origin/*`` is
        exactly the wrong evidence for that.  ``None`` means "no such
        branch on the remote", or the remote could not be reached.
        """
        _validate_ref(branch)
        try:
            out = await self._arun(
                ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
                cwd=checkout_path,
            )
        except GitError:
            return None
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.strip() == f"refs/heads/{branch}" and len(sha.strip()) == 40:
                return sha.strip()
        return None

    async def apush_head_to(
        self,
        checkout_path: str,
        branch: str,
        *,
        event_bus: EventBus | None = None,
        project_id: str | None = None,
    ) -> None:
        """Push ``HEAD`` to ``origin/<branch>``, creating the branch if needed.

        Distinct from :meth:`apush_branch`, which pushes a *named local*
        branch and therefore cannot save a detached HEAD — the state a slot
        worktree is routinely left in.  Never forced: a rejected push means
        the remote branch has commits this HEAD does not, and the caller
        picks a different name rather than overwriting them.
        """
        tip = await self.apush_validated_ref(checkout_path, "HEAD", branch)
        if event_bus is not None:
            try:
                await event_bus.emit(
                    "git.push",
                    {
                        "branch": branch,
                        "remote": "origin",
                        "commit_range": tip,
                        "project_id": project_id,
                    },
                )
            except Exception:
                logger.debug(
                    "Failed to emit git.push event for %s", checkout_path, exc_info=True
                )

    async def apush_validated_ref(
        self,
        checkout_path: str,
        source_ref: str,
        branch: str,
        *,
        force_with_lease: bool = False,
    ) -> str:
        """Resolve *source_ref* once and push that exact commit to *branch*.

        A merge/rebase hook or another local process may move a named ref after
        its content has been guarded. The object-ID refspec makes Git deliver
        precisely the validated object rather than resolving the name again.
        """
        source_ref = _validate_rev(source_ref, field="push source")
        branch = _validate_ref(branch)
        tip = (await self._arun(["rev-parse", "--verify", source_ref], cwd=checkout_path)).strip()
        if not _OID_RE.fullmatch(tip.lower()):
            raise GitError(f"could not resolve immutable delivery tip for {source_ref}")
        args = ["push", "origin", f"{tip}:refs/heads/{branch}"]
        if force_with_lease:
            args.insert(2, "--force-with-lease")
        await self._arun(args, cwd=checkout_path)
        return tip

    async def apush_validated_delivery(
        self,
        checkout_path: str,
        base_ref: str,
        source_ref: str,
        branch: str,
        *,
        force_with_lease: bool = False,
        event_bus: EventBus | None = None,
        project_id: str | None = None,
    ) -> str:
        """Inspect and push one immutable delivery tip without a ref-name race.

        Resolve the source exactly once, diff that content-addressed OID from
        its target base, then use the same OID in the remote refspec. A later
        mutation of ``HEAD`` or a branch name is therefore irrelevant.
        """
        source_ref = _validate_rev(source_ref, field="delivery source")
        base_ref = _validate_rev(base_ref, field="delivery base")
        branch = _validate_ref(branch)
        tip = (
            await self._arun(["rev-parse", "--verify", source_ref], cwd=checkout_path)
        ).strip()
        if not _OID_RE.fullmatch(tip.lower()):
            raise GitError(f"could not resolve immutable delivery tip for {source_ref}")
        paths = await self.areserved_paths_in_diff(checkout_path, base_ref, tip)
        if paths:
            raise GitError("reserved delivery paths: " + ", ".join(paths))
        pushed = await self.apush_validated_ref(
            checkout_path, tip, branch, force_with_lease=force_with_lease
        )
        if event_bus is not None:
            try:
                await event_bus.emit(
                    "git.push",
                    {
                        "branch": branch,
                        "remote": "origin",
                        "commit_range": pushed,
                        "project_id": project_id,
                    },
                )
            except Exception:
                logger.debug("Failed to emit git.push event for %s", checkout_path, exc_info=True)
        return pushed

    async def alist_prs(
        self,
        checkout_path: str,
        *,
        state: str = "open",
        base: str | None = None,
        head: str | None = None,
        limit: int = 30,
    ) -> list[dict] | None:
        """``gh pr list`` as data, or ``None`` when gh could not answer.

        ``None`` and ``[]`` mean different things and callers must not
        conflate them: no ``gh``, no auth and no network all give ``None``
        ("unknown"), while ``[]`` is gh saying there are genuinely no such
        pull requests.  A doctor check that read ``None`` as ``[]`` would
        report every branch on an offline machine as stranded.
        """
        if state not in ("open", "closed", "merged", "all"):
            return None
        args = ["gh", "pr", "list", "--state", state, "--limit", str(max(1, int(limit)))]
        if base is not None:
            args += ["--base", _validate_ref(base, field="base branch")]
        if head is not None:
            args += ["--head", _validate_ref(head, field="head branch")]
        args += ["--json", "number,url,title,baseRefName,headRefName,state"]
        try:
            result = await self._arun_subprocess(
                args, cwd=checkout_path, timeout=self._GIT_TIMEOUT
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout or "[]")
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, list) else None

    async def alist_remote_branches(
        self, checkout_path: str, *, remote: str = "origin"
    ) -> list[str] | None:
        """Branch names on *remote*, from its remote-tracking refs.

        Reads ``refs/remotes/<remote>`` rather than asking the network, so
        the caller decides whether a ``fetch`` happened first.  ``<remote>/HEAD``
        is skipped — it is a symbolic ref, not a branch.
        """
        try:
            out = await self._arun(
                [
                    "for-each-ref",
                    "--format=%(refname:short)",
                    f"refs/remotes/{_validate_ref(remote, field='remote')}",
                ],
                cwd=checkout_path,
            )
        except GitError:
            return None
        prefix = f"{remote}/"
        names = []
        for line in out.splitlines():
            name = line.strip()
            if not name.startswith(prefix):
                continue
            short = name[len(prefix):]
            if short and short != "HEAD":
                names.append(short)
        return names

    async def aget_pr_identity(self, checkout_path: str, pr_url: str) -> PullRequestIdentity:
        """Resolve the PR identity GitHub will merge, or fail closed.

        Reads the REST pull-request resource (``gh api
        repos/{owner}/{repo}/pulls/{n}``) rather than ``gh pr view --json``.
        The GraphQL field list gh exposes depends on the gh version —
        ``baseRefOid`` only exists from gh 2.46 and there is no
        ``baseRepository`` field on any version, so asking for them made
        every merge fail closed on the gh 2.45 this project supports — while
        the REST resource has carried ``base.sha``, ``head.sha`` and
        ``base.repo.full_name`` for years.  Host, owner, repo and number come
        from the URL, so no checkout is needed to resolve them, and the
        repository and OIDs come from one response so the subsequent
        PR-files query can be tied to an immutable snapshot.
        """
        url = _PR_URL_RE.fullmatch(pr_url.strip())
        if url is None:
            raise GitError("could not resolve PR identity: not a GitHub pull request URL")
        host, owner, repo, number = url.group("host", "owner", "repo", "number")
        try:
            result = await self._arun_subprocess(
                ["gh", "api", "--hostname", host, f"repos/{owner}/{repo}/pulls/{number}"],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except Exception as exc:
            raise GitError(f"could not resolve PR identity: {exc}") from exc
        if result.returncode != 0:
            raise GitError(f"could not resolve PR identity: {result.stderr.strip()}")
        try:
            data = json.loads(result.stdout)
            resource_number = data["number"]
            repository = data["base"]["repo"]["full_name"]
            base_ref = data["base"]["ref"]
            head_ref = data["head"]["ref"]
            base_oid = data["base"]["sha"].lower()
            head_oid = data["head"]["sha"].lower()
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise GitError("could not resolve complete PR identity") from exc
        if (
            resource_number != int(number)
            or not isinstance(repository, str)
            or not _REPOSITORY_RE.fullmatch(repository)
            or not isinstance(base_ref, str)
            or not isinstance(head_ref, str)
            or not _OID_RE.fullmatch(base_oid)
            or not _OID_RE.fullmatch(head_oid)
        ):
            raise GitError("could not resolve complete PR identity")
        return PullRequestIdentity(
            repository=repository,
            number=int(number),
            base_ref=base_ref,
            base_oid=base_oid,
            head_ref=head_ref,
            head_oid=head_oid,
        )

    async def _apr_changed_paths(
        self, checkout_path: str, identity: PullRequestIdentity
    ) -> list[str]:
        """Return every PR-file path from GitHub's paginated merge-base diff."""
        endpoint = f"repos/{identity.repository}/pulls/{identity.number}/files"
        try:
            result = await self._arun_subprocess(
                ["gh", "api", "--paginate", endpoint, "--jq", ".[].filename"],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except Exception as exc:
            raise GitError(f"could not inspect PR delivery diff: {exc}") from exc
        if result.returncode != 0:
            raise GitError(f"could not inspect PR delivery diff: {result.stderr.strip()}")
        return [path for path in (result.stdout or "").splitlines() if path]

    async def avalidate_pr_for_merge(
        self, checkout_path: str, pr_url: str
    ) -> PullRequestIdentity:
        """Fail closed unless a PR identity and its reserved-path diff are stable.

        The REST PR-files endpoint is GitHub's merge-base PR diff and supports
        pagination. Re-reading the identity after that potentially long query
        proves the inspected diff still belongs to the precise base/head pair.
        """
        identity = await self.aget_pr_identity(checkout_path, pr_url)
        paths = await self._apr_changed_paths(checkout_path, identity)
        reserved = self._daemon_bookkeeping_paths("\0".join(paths))
        if reserved:
            raise GitError(
                "PR changes reserved daemon bookkeeping paths: " + ", ".join(sorted(reserved))
            )
        if await self.aget_pr_identity(checkout_path, pr_url) != identity:
            raise GitError("PR identity changed while its delivery diff was inspected")
        return identity

    async def apr_base_ref(self, checkout_path: str, pr_url: str) -> str | None:
        """The branch a PR targets (``baseRefName``), or ``None`` if unknown.

        A PR whose base is not the project default branch does not put its
        commits on the default branch when it merges — that is the whole
        stacked-PR failure this exists to detect.  ``None`` is a first-class
        answer (no ``gh``, no auth, no network) and callers must not read it
        as "targets the default branch".
        """
        try:
            result = await self._arun_subprocess(
                ["gh", "pr", "view", pr_url, "--json", "baseRefName"],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError):
            return None
        base = data.get("baseRefName")
        if not base:
            return None
        try:
            # gh's answer becomes a git ref argument downstream.
            return _validate_ref(str(base), field="base branch")
        except GitError:
            return None

    async def apr_check_rollup(self, checkout_path: str, pr_url: str) -> list[dict] | None:
        """A PR head's status-check rollup, or ``None`` if it can't be read.

        The raw ``statusCheckRollup`` entries, straight from
        ``gh pr view --json`` — one per check run or commit status on the
        PR's head commit.  Judging them is
        :func:`src.git.ci_gate.classify_rollup`'s job; this method only
        fetches, so the interesting decisions stay testable without gh.

        ``None`` is a first-class answer (no ``gh``, no auth, no network,
        malformed JSON) and means "CI status unknown".  Callers must not
        read it as green: ``_cmd_pr_merge`` under
        ``integration.merge_ci_policy: required`` refuses on ``None``,
        because "could not check" is exactly the state in which merging
        blind lands regressions.  An *empty list* is different — it means
        the rollup was read and nothing has reported yet.
        """
        try:
            result = await self._arun_subprocess(
                ["gh", "pr", "view", pr_url, "--json", "statusCheckRollup"],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError):
            return None
        rollup = data.get("statusCheckRollup")
        if rollup is None:
            # gh reports ``null`` for a PR whose head has no checks at all.
            return []
        if not isinstance(rollup, list):
            return None
        return rollup

    async def arev_parse(self, checkout_path: str, ref: str) -> str | None:
        """Return the SHA for ``ref`` in ``checkout_path``, or None.

        Best-effort: returns None on any failure (missing checkout,
        unknown ref, gh/git error).  Callers must not raise on None.
        """
        try:
            result = await self._arun_subprocess(
                ["git", "rev-parse", "--verify", ref],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        sha = (result.stdout or "").strip()
        return sha if len(sha) == 40 else None

    async def aget_status(self, checkout_path: str) -> str:
        try:
            return await self._arun(["status"], cwd=checkout_path)
        except GitError:
            return ""

    async def areserved_paths_in_diff(
        self,
        checkout_path: str,
        base_ref: str,
        tip_ref: str,
    ) -> list[str]:
        """Return daemon-owned paths changed by a delivery tip.

        The comparison starts at the merge-base so an unchanged reserved
        path already tracked by the target branch is harmless, while an
        addition, deletion, or modification made by task commits is caught.
        Unlike preview helpers, Git failures propagate: callers use this as
        a fail-closed delivery gate before merge, push, or PR acceptance.
        """
        base_ref = _validate_rev(base_ref, field="delivery base")
        tip_ref = _validate_rev(tip_ref, field="delivery tip")
        merge_base = await self._arun(
            ["merge-base", base_ref, tip_ref], cwd=checkout_path
        )
        changed = await self._arun(
            ["diff", "--name-only", "-z", merge_base, tip_ref, "--"],
            cwd=checkout_path,
        )
        return sorted(self._daemon_bookkeeping_paths(changed))

    async def aget_current_branch(self, checkout_path: str) -> str:
        try:
            return await self._arun(["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout_path)
        except GitError:
            return ""

    async def ahas_uncommitted_changes(
        self, checkout_path: str, *, strict: bool = False
    ) -> bool | None:
        """Return whether the workspace has staged or unstaged changes.

        With ``strict=True``, return ``None`` when Git cannot determine the
        status. Callers that use cleanliness as proof that work does not exist
        must keep that state distinct from a clean checkout.
        """
        try:
            output = await self._arun_subprocess(
                ["git", "status", "--porcelain"],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
            if output.returncode != 0:
                return None if strict else False
            return bool(output.stdout and output.stdout.strip())
        except Exception:
            return None if strict else False

    async def astaged_patch(self, checkout_path: str) -> str:
        """The staged diff as an **appliable** patch.  Output is not stripped.

        ``_arun`` / ``_arun_unlocked`` strip their output, which corrupts a
        patch: ``git apply`` rejects one with no trailing newline, and a
        binary hunk needs the blank line that terminates its base85 block —
        so a stripped patch fails with "corrupt binary patch at line N".
        ``--binary`` is likewise mandatory; without it git emits only
        ``Binary files a/x and b/x differ`` and the bytes are unrecoverable.

        Used by worktree salvage, whose entire purpose is that the archived
        text can be applied later.
        """
        result = await self._arun_subprocess(
            ["git", "diff", "--cached", "--binary", "HEAD"], cwd=checkout_path
        )
        if result.returncode != 0:
            raise GitError(f"git diff --cached failed: {result.stderr.strip()}")
        return result.stdout

    @staticmethod
    def _resolve_git_dir(checkout_path: str) -> str:
        """The real git directory for a checkout — clone or worktree.

        For a clone this is ``<path>/.git``.  For a linked worktree ``.git``
        is a file containing ``gitdir: <absolute path>``; returning that path
        is what lets lock-file cleanup find ``index.lock`` at all.  Falls back
        to ``<path>/.git`` when the pointer cannot be read.
        """
        dot_git = os.path.join(checkout_path, ".git")
        try:
            if os.path.isfile(dot_git):
                with open(dot_git, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if line.startswith("gitdir:"):
                            target = line.split(":", 1)[1].strip()
                            if not os.path.isabs(target):
                                target = os.path.join(checkout_path, target)
                            return os.path.normpath(target)
        except OSError:
            pass
        return dot_git

    async def aabort_in_progress_operations(self, checkout_path: str) -> None:
        """Abort any in-progress merge, rebase, or cherry-pick.

        Also removes stale ``.git/index.lock`` files left by crashed
        processes (e.g. a killed agent) that would block all subsequent
        git operations.

        This is a best-effort method — individual failures are silently
        ignored because not all operations may be in progress.
        """
        # Remove stale git lock files that block all operations.  In a
        # worktree ``.git`` is a *file* holding ``gitdir: <path>``, and the
        # index lives under ``<base>/.git/worktrees/<name>/`` — resolving it
        # is what makes this work for slot worktrees and not only clones.
        git_dir = self._resolve_git_dir(checkout_path)
        for lock_name in ("index.lock", "shallow.lock", "refs/heads.lock"):
            lock_path = os.path.join(git_dir, lock_name)
            try:
                if os.path.exists(lock_path):
                    os.remove(lock_path)
            except OSError:
                pass

        # Abort in-progress merge
        try:
            await self._arun(["merge", "--abort"], cwd=checkout_path)
        except GitError:
            pass

        # Abort in-progress rebase
        try:
            await self._arun(["rebase", "--abort"], cwd=checkout_path)
        except GitError:
            pass

        # Abort in-progress cherry-pick
        try:
            await self._arun(["cherry-pick", "--abort"], cwd=checkout_path)
        except GitError:
            pass

    async def aforce_clean_workspace(self, checkout_path: str) -> bool:
        """Force the workspace into a clean state using the most aggressive
        git operations available.

        This is the nuclear option — it resets the index and working tree
        to HEAD, removes all untracked files (including those in
        ``.gitignore``), and aborts any in-progress operations first.

        Returns True if the workspace is clean after all attempts,
        False if cleanup failed (extremely unlikely).
        """
        # Step 1: Abort any in-progress operations
        await self.aabort_in_progress_operations(checkout_path)

        # Step 2: Hard-reset index and working tree to HEAD
        try:
            await self._arun(["reset", "--hard", "HEAD"], cwd=checkout_path)
        except GitError:
            pass

        # Step 3: Remove all untracked files including ignored ones
        try:
            await self._arun(["clean", "-fdx"], cwd=checkout_path)
        except GitError:
            pass

        return not await self.ahas_uncommitted_changes(checkout_path)

    async def afind_open_pr(
        self,
        checkout_path: str,
        branch_name: str,
        *,
        include_workspace_head: bool = True,
    ) -> str | None:
        """Return an open or merged PR URL delivering *branch_name*, or ``None``.

        Matches the head branch **name** first, then falls back to matching
        the head **commit**.  A PR delivers commits, not a name: a task
        description that names a different delivery branch, or an agent that
        opened the PR from a second ref pointed at the same tip, publishes
        exactly these commits under another head name.  Treating that as "no
        PR" sends a correct, pushed task into a pointless retry, so any open
        PR whose head commit is this branch's tip (or, when
        *include_workspace_head* is true, the workspace's ``HEAD`` for when
        the agent never moved the task branch) counts.

        A merged pull request is evidence that the branch's work has already
        shipped. Closed-but-unmerged PRs deliberately remain a failure.
        Best-effort throughout: any gh/git failure returns ``None``.
        """
        if include_workspace_head:
            url = await self._open_pr_url_by_head_name(checkout_path, branch_name)
            if url:
                return url

        refs = [branch_name]
        if include_workspace_head:
            refs.append("HEAD")
        tips = {sha for ref in refs if (sha := await self.arev_parse(checkout_path, ref))}
        if not tips:
            return None
        return await self._open_pr_url_by_head_commit(checkout_path, tips, branch_name)

    async def _open_pr_url_by_head_name(
        self,
        checkout_path: str,
        branch_name: str,
    ) -> str | None:
        try:
            result = await self._arun_subprocess(
                [
                    "gh",
                    "pr",
                    "list",
                    "--head",
                    branch_name,
                    "--state",
                    "all",
                    "--json",
                    "url,state",
                    "--jq",
                    'first(.[] | select(.state == "OPEN" or .state == "MERGED") | .url) // empty',
                ],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        # ``--jq`` is deliberately single-valued, but retain this boundary
        # guard so an older gh implementation or a mocked command cannot
        # store a newline-delimited list in a task's ``pr_url``.
        return next(
            (line.strip() for line in (result.stdout or "").splitlines() if line.strip()), None
        )

    async def _open_pr_url_by_head_commit(
        self,
        checkout_path: str,
        tips: set[str],
        branch_name: str,
    ) -> str | None:
        """URL of an open or merged PR whose head commit is one of *tips*."""
        try:
            result = await self._arun_subprocess(
                [
                    "gh",
                    "pr",
                    "list",
                    "--state",
                    "all",
                    "--json",
                    "url,state,headRefName,headRefOid",
                    "--limit",
                    "100",
                ],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        try:
            prs = json.loads(result.stdout or "[]")
        except (ValueError, TypeError):
            return None
        if not isinstance(prs, list):
            return None
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            if (
                pr.get("state") in {"OPEN", "MERGED"}
                and pr.get("headRefOid") in tips
                and pr.get("url")
            ):
                logger.info(
                    "Accepting open PR %s from branch '%s' for '%s' "
                    "(same head commit %s)",
                    pr.get("url"),
                    pr.get("headRefName"),
                    branch_name,
                    str(pr.get("headRefOid"))[:8],
                )
                return pr["url"]
        return None

    async def ais_ancestor(
        self,
        checkout_path: str,
        ancestor: str,
        descendant: str,
    ) -> bool:
        """Return whether *ancestor* is reachable from *descendant*."""
        try:
            await self._arun(
                ["merge-base", "--is-ancestor", _validate_ref(ancestor), _validate_rev(descendant)],
                cwd=checkout_path,
            )
            return True
        except GitError:
            return False

    async def acount_commits_ahead(
        self,
        checkout_path: str,
        branch: str,
        base: str,
    ) -> int | None:
        """Return how many commits *branch* carries that *base* does not.

        ``None`` means the question could not be answered (a missing ref, a
        detached worktree, any git failure) — callers must treat that as
        "unknown" rather than as zero.
        """
        try:
            output = await self._arun(
                ["rev-list", f"{_validate_rev(base)}..{_validate_rev(branch)}", "--count"],
                cwd=checkout_path,
            )
        except GitError:
            return None
        try:
            return int(output.strip())
        except ValueError:
            return None

    async def abranch_exists(self, checkout_path: str, branch: str) -> bool | None:
        """Return whether *branch* exists locally or in ``origin``, else ``None`` on error."""
        branch = _validate_ref(branch, field="branch")
        for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
            try:
                result = await self._arun_subprocess(
                    ["git", "show-ref", "--verify", "--quiet", ref],
                    cwd=checkout_path,
                    timeout=self._GIT_TIMEOUT,
                )
            except Exception:
                return None
            if result.returncode == 0:
                return True
            if result.returncode != 1:
                return None
        return False

    async def ahas_non_plan_changes(
        self,
        checkout_path: str,
        default_branch: str = "main",
        min_files: int = 3,
        min_lines: int = 50,
    ) -> bool:
        try:
            merge_base = await self._arun(
                ["merge-base", f"origin/{default_branch}", "HEAD"],
                cwd=checkout_path,
            )
        except GitError:
            return False
        try:
            stat_output = await self._arun(
                [
                    "diff",
                    "--stat",
                    f"{merge_base}..HEAD",
                    "--",
                    ".",
                    # Exclude plan files
                    ":!.claude/plan.md",
                    ":!plan.md",
                    ":!.claude/plans/",
                    ":!docs/plans/",
                    ":!docs/plan.md",
                    ":!plans/",
                    # Exclude non-code artifacts (notes, logs, test results)
                    ":!notes/",
                    ":!*.log",
                    ":!test-results*",
                ],
                cwd=checkout_path,
            )
        except GitError:
            return False
        if not stat_output:
            return False
        lines = stat_output.strip().split("\n")
        summary = lines[-1] if lines else ""
        files_match = re.search(r"(\d+)\s+files?\s+changed", summary)
        insertions_match = re.search(r"(\d+)\s+insertions?", summary)
        deletions_match = re.search(r"(\d+)\s+deletions?", summary)
        files_changed = int(files_match.group(1)) if files_match else 0
        insertions = int(insertions_match.group(1)) if insertions_match else 0
        deletions = int(deletions_match.group(1)) if deletions_match else 0
        total_lines = insertions + deletions
        return files_changed >= min_files or total_lines >= min_lines

    async def aget_default_branch(self, checkout_path: str) -> str:
        try:
            remote_head = await self._arun(
                ["symbolic-ref", "refs/remotes/origin/HEAD"],
                cwd=checkout_path,
            )
            if remote_head.startswith("refs/remotes/origin/"):
                return remote_head.replace("refs/remotes/origin/", "")
        except GitError:
            pass
        for branch in ["main", "master", "develop", "trunk"]:
            try:
                await self._arun(["rev-parse", "--verify", branch], cwd=checkout_path)
                return branch
            except GitError:
                continue
        try:
            remote_branches = await self._arun(
                ["ls-remote", "--heads", "origin"], cwd=checkout_path
            )
            for branch in ["main", "master", "develop", "trunk"]:
                if f"refs/heads/{branch}" in remote_branches:
                    return branch
        except GitError:
            pass
        current = await self.aget_current_branch(checkout_path)
        return current if current else "main"

    async def aget_recent_commits(
        self,
        checkout_path: str,
        count: int = 5,
    ) -> str:
        try:
            return await self._arun(["log", "--oneline", f"-{count}"], cwd=checkout_path)
        except GitError:
            return ""

    async def acheck_gh_auth(self) -> bool:
        try:
            result = await self._arun_subprocess(
                ["gh", "auth", "status"],
                timeout=30,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    async def acreate_github_repo(
        self,
        name: str,
        *,
        private: bool = True,
        org: str | None = None,
        description: str = "",
    ) -> str:
        full_name = f"{org}/{name}" if org else name
        cmd = ["gh", "repo", "create", full_name]
        cmd.append("--private" if private else "--public")
        if description:
            cmd.extend(["--description", description])
        try:
            result = await self._arun_subprocess(cmd, timeout=60)
        except subprocess.TimeoutExpired:
            raise GitError("gh repo create timed out after 60s (possible auth prompt)")
        if result.returncode != 0:
            raise GitError(f"gh repo create failed: {result.stderr.strip()}")
        url = ""
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("https://") or line.startswith("http://"):
                url = line
                break
        if not url:
            for line in reversed(result.stderr.strip().splitlines()):
                line = line.strip()
                if line.startswith("https://") or line.startswith("http://"):
                    url = line
                    break
        if not url:
            raise GitError(
                "gh repo create succeeded but no repository URL was found "
                f"in output: {result.stdout.strip()}"
            )
        return url

    @staticmethod
    def slugify(text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r"[^\w\s-]", "", text)
        text = re.sub(r"[\s_]+", "-", text)
        text = re.sub(r"-+", "-", text)
        return text.strip("-")

    @staticmethod
    def make_branch_name(task_id: str, title: str) -> str:
        """Build a branch name in ``<task-id>/<slug>`` format.

        Examples: ``brave-fox/add-retry-logic``, ``calm-river/fix-auth-bug``.
        The task ID prefix makes branches easy to trace back to their task,
        and the slug suffix provides human-readable context.
        """
        return f"{task_id}/{GitManager.slugify(title)}"

    # NOTE: Duplicate async block removed — the canonical async public API
    # is defined above (after the synchronous methods), starting at the
    # "Async public API" comment block around line 1018.
    # The removed block was an older copy that lacked plan-file exclusion
    # in acommit_all and TimeoutExpired handling in acreate_pr/acheck_pr_merged.
    #
    # If you need to add a new async method, add it in the block above,
    # alongside the existing async methods.
