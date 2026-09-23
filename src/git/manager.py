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
import logging
import math
import os
import re
import signal
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from src.git.askpass_broker import (
    GitCredentialTopology,
    make_request_channel,
    pin_git_credential_topology,
    serve_one_credential,
    zeroize,
)
from src.git.askpass_fd import answer_prompt
from src.git.github import GitHubAccess, GitHubClient
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialMode,
    GitHubRepositoryBinding,
)

if TYPE_CHECKING:
    from src.event_bus import EventBus

logger = logging.getLogger(__name__)


# Root-promotion authority must cover this aggregate operation plus cleanup.
# Keep these exported so the authority holder derives its horizon from the
# transport contract instead of maintaining an independent timeout literal.
APP_AUTH_PUSH_TIMEOUT_SECONDS = 120.0
APP_AUTH_PUSH_CLEANUP_MARGIN_SECONDS = 5.0


@contextmanager
def _zeroized_credential(buffer: bytearray):
    try:
        yield buffer
    finally:
        zeroize(buffer)


def _safe_authenticated_git_detail(value: str, token: str | None, *, limit: int = 512) -> str:
    """Keep useful failure text without returning a credential to callers or logs."""
    from src.projects.github import scrub_secrets

    if token:
        value = value.replace(token, "***")
    value = re.sub(r"(?im)(authorization\s*:\s*)[^\r\n]*", r"\1***", value)
    safe = scrub_secrets(value)
    return safe[-limit:].replace("\r", "\\r").replace("\n", "\\n")


class GitError(Exception):
    pass


class RemoteRefState(StrEnum):
    """Fail-closed result states for an exact remote reference read."""

    PRESENT = "present"
    ABSENT = "absent"
    ERROR = "error"


@dataclass(frozen=True)
class RemoteRefResult:
    state: RemoteRefState
    oid: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PullRequestIdentity:
    """Immutable PR facts that must agree from review through merge.

    ``base_oid`` is recorded but is *not* part of :attr:`pin`.  It is the tip
    of the base branch at the moment of the read, so it moves on every push
    to the default branch — with several agents delivering concurrently it
    differs between two reads seconds apart for reasons that have nothing to
    do with this PR.  Nothing the merge relies on depends on it: the delivery
    diff inspected before merging runs from the merge-base of that tip and
    the head, which base movement does not change, and ``gh pr merge``
    merges into whatever the base tip is at that moment.  The base *branch
    name* is pinned, because a PR retargeted onto another branch lands its
    commits somewhere the review never looked at.
    """

    repository: str
    number: int
    base_ref: str
    base_oid: str
    head_ref: str
    head_oid: str
    #: GitHub's own count of files the PR changes, read in the same snapshot
    #: as the OIDs.  It is pinned as one more fact that must agree between
    #: the two identity reads; the delivery diff itself is derived from the
    #: OIDs and does not depend on it.
    changed_files: int
    #: The GitHub host serving this PR, preserved for the pinned-OID fetch.
    host: str = "github.com"

    @property
    def clone_url(self) -> str:
        """The HTTPS URL ``git fetch`` reads the pinned commits from."""
        return f"https://{self.host}/{self.repository}.git"

    @property
    def pin(self) -> tuple[str, int, str, str, str, int]:
        """The facts that fix *what* a review of this PR reviewed.

        The changed-file count belongs here: it is a property of the head
        being merged, and a count that differs between the two identity
        snapshots means the listing inspected in between may not be the
        PR's diff any more.
        """
        return (
            self.repository,
            self.number,
            self.base_ref,
            self.head_ref,
            self.head_oid,
            self.changed_files,
        )


# ---------------------------------------------------------------------------
# Refname validation — trust rule R4 (docs/specs/design/trust-and-ops.md §2.4)
# ---------------------------------------------------------------------------

#: Conservative subset of ``git check-ref-format``: must start with an
#: alphanumeric (so a name can never be read as an option), and may then
#: contain letters, digits, ``.``, ``_``, ``/`` and ``-``.  Whitespace, ``..``,
#: shell metacharacters and a leading ``-`` are all rejected.
_REFNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_ZERO_OID = "0" * 40


def is_valid_git_oid(value: object) -> bool:
    """Return whether *value* is one exact lowercase SHA-1 Git object ID."""
    return isinstance(value, str) and _OID_RE.fullmatch(value) is not None


_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
#: ``https://<host>/<owner>/<repo>/pull/<n>`` with an optional trailing slash,
#: query or fragment — the shape ``gh pr create`` prints and ``gh pr merge``
#: accepts.  Owner and repo use the same alphabet as :data:`_REPOSITORY_RE`.
_PR_URL_RE = re.compile(
    r"^https://(?P<host>[A-Za-z0-9.-]+)/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"/pull/(?P<number>[1-9][0-9]*)/?(?:[?#].*)?$"
)


def _pr_changed_file_count(data: dict) -> int:
    """GitHub's changed-file count from the REST ``pulls/{n}`` snapshot, or raise.

    The count comes from the same response as the OIDs, so it is one more
    fact of that snapshot the second identity read must agree with.  A head
    that moved and came back with a different number of changed files is
    caught here even before the OIDs are compared.  Only the REST spelling
    ``changed_files`` is read —
    the identity never comes from ``gh pr view --json`` (whose field is
    ``changedFiles``), so accepting it would only widen the guard.  Anything
    but a non-negative integer (``bool`` is an ``int`` subclass and is not a
    count) is an incomplete identity and fails closed.
    """
    count = data.get("changed_files")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise GitError("could not resolve complete PR identity")
    return count


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


#: Characters that only appear in a git *revision expression* (``HEAD~1``,
#: ``main@{yesterday}``, ``v1^{commit}``), never in a plain branch name.
_REVISION_SYNTAX_RE = re.compile(r"[~^@{}]")


def _delivery_source_rev(source: str) -> str:
    """The revision to ``rev-parse`` for a delivery *source*.

    Git's ref lookup order tries ``refs/<name>`` and ``refs/tags/<name>``
    before ``refs/heads/<name>``, so a bare ``rev-parse main`` answers with a
    tag called ``main`` when one exists — and a tag planted in a checkout
    would then be what gets validated and pushed to ``refs/heads/main``.
    A plain branch name therefore resolves ``refs/heads/<name>`` explicitly
    (peeled to a commit); only ``HEAD``, object ids, fully qualified refs and
    revision expressions keep bare resolution, since those name the object
    the caller means without any lookup-order ambiguity.
    """
    if (
        source == "HEAD"
        or source.startswith("refs/")
        or _OID_RE.fullmatch(source.lower())
        or _REVISION_SYNTAX_RE.search(source)
    ):
        return source
    return f"refs/heads/{source}^{{commit}}"


def _expected_remote_oid(value: str | None, force_with_lease: bool) -> str | None:
    """Normalize a caller's explicit remote lease before any network access.

    The all-zero OID means "create only if absent". It is exclusive with a
    tracking-ref ``force_with_lease``: a caller names the tip it expects, or
    asks for the remote-tracking one, never both.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not _OID_RE.fullmatch(value.lower()):
        raise GitError("invalid expected remote OID")
    if force_with_lease:
        raise GitError("choose an explicit expected remote OID or force_with_lease")
    return value.lower()


def _is_local_repository(reference: str) -> bool:
    """The same local-path test :meth:`GitManager._abind_git_repository` uses."""
    return reference.startswith(("/", "./", "../", "file://")) or Path(reference).exists()


def _github_full_name(reference: str) -> str | None:
    """Case-folded ``owner/name`` for a GitHub reference, else ``None``."""
    from src.projects.github import GitHubError, parse_github_repository

    if _is_local_repository(reference):
        return None
    try:
        return parse_github_repository(reference).full_name.lower()
    except GitHubError:
        return None


def _repository_location(reference: str, base: str) -> str:
    """Comparable location of a local or non-GitHub repository reference."""
    reference = reference.strip()
    if _is_local_repository(reference):
        path = reference.removeprefix("file://")
        return os.path.realpath(os.path.join(base, path))
    reference = reference.rstrip("/")
    return reference.removesuffix(".git")


def _require_authorized_repository(configured_url: str, repository_url: str, *, base: str) -> None:
    """Refuse a checkout remote naming anything but the authorized repository.

    A worker controls its checkout's Git configuration, so the remote URL it
    holds is a claim to check against the task/project record, never the
    authority itself. This runs before any repository binding or credential
    selection. GitHub names compare case-insensitively, as GitHub does; SSH
    and HTTPS spellings of one repository are the same repository.
    """
    authorized = _github_full_name(repository_url)
    configured = _github_full_name(configured_url)
    if authorized is not None or configured is not None:
        matches = authorized == configured
    else:
        matches = _repository_location(configured_url, base) == _repository_location(
            repository_url, base
        )
    if not matches:
        raise GitError("push destination is not the authorized repository")


class GitManager:
    _APP_GIT_EXECUTABLE = "/usr/bin/git"
    _APP_CREDENTIAL_BROKER_TIMEOUT = 30.0
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

    # The first fetch into a PR delivery-diff cache pulls a repository's
    # whole commit and tree history (no blobs); every later one is
    # incremental.  Ten minutes covers a large repository once.
    _PR_DIFF_FETCH_TIMEOUT = 600

    #: Directory, under the directory ``pr_merge`` runs in, holding one
    #: blob-less bare repository per ``<host>/<owner>/<repo>`` from which PR
    #: delivery diffs are derived.
    PR_DIFF_CACHE_DIRNAME = "pr-diff-cache"

    # Git subcommands that modify shared repository state (pack files,
    # object store) and must be serialized when multiple worktrees share
    # the same underlying repository.  ``pull`` includes an implicit
    # ``fetch`` and therefore also needs serialization.
    _SERIALIZED_SUBCOMMANDS: frozenset[str] = frozenset({"fetch", "gc", "pull"})

    _SCOPED_ENV_KEYS: frozenset[str] = frozenset(
        {
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_AUTHOR_DATE",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
            "GIT_COMMITTER_DATE",
            "LC_ALL",
        }
    )
    _MAX_STDIN_BYTES = 1024 * 1024

    def __init__(self, github_access: GitHubAccess | None = None) -> None:
        # The daemon injects its startup-composed credential authority here.
        # Standalone local-only uses may omit it, but daemon services must
        # share this exact object so App configuration cannot be bypassed by
        # constructing an ambient-login GitHub transport.
        # Optional lock provider for serializing shared git operations
        # across branch-isolated worktrees.  When set, ``_arun`` acquires
        # the returned lock before executing serialized subcommands.
        self._lock_provider: Callable[[str], asyncio.Lock | None] | None = None
        # One lock per PR delivery-diff cache (see ``_apr_delivery_diff``),
        # so two merges of PRs in the same repository never fetch into the
        # same bare cache at once.
        self._pr_diff_cache_locks: dict[str, asyncio.Lock] = {}
        self._repository_operation_locks: dict[str, asyncio.Lock] = {}
        self.github_access = github_access

    async def bind_github_repository(self, repository_url: str) -> GitHubRepositoryBinding:
        if self.github_access is None:
            raise GitError("GitHub access service is not configured")
        try:
            return await self.github_access.bind_repository(repository_url)
        except GitHubAccessError as exc:
            raise GitError(str(exc)) from exc

    def _github_client(self, repository: GitHubRepositoryBinding) -> GitHubClient:
        if self.github_access is None:
            raise GitError("GitHub access service is not configured")
        if not isinstance(repository, GitHubRepositoryBinding):
            raise GitError("an authorized GitHub repository binding is required")
        return GitHubClient(
            repository, access=self.github_access,
            max_response_bytes=16 * 1024 * 1024,
            max_pagination_bytes=32 * 1024 * 1024,
        )

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
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                # Cancellation (including daemon shutdown) must not leave a
                # reset writing after its claim request has gone away.
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass  # Process already exited before we could kill it
                await proc.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
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

    async def arun_git_result(
        self,
        args: list[str],
        *,
        cwd: str,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        lock_held: bool = False,
    ) -> subprocess.CompletedProcess:
        """Run Git while preserving expected nonzero status and diagnostics.

        Promotion construction needs status 1 from ``merge-tree`` to remain a
        domain conflict, and ``commit-tree`` needs deterministic identity and
        message input.  Per-call environment additions are deliberately
        limited to Git identity/date variables and ``LC_ALL``.
        """
        if not args:
            raise GitError("git command is empty")
        if stdin is not None and len(stdin.encode("utf-8")) > self._MAX_STDIN_BYTES:
            raise GitError("git command stdin exceeds the bounded input limit")
        overrides = dict(env or {})
        unexpected = set(overrides) - self._SCOPED_ENV_KEYS
        if unexpected:
            raise GitError("unsupported per-call git environment: " + ", ".join(sorted(unexpected)))

        lock: asyncio.Lock | None = None
        if not lock_held and self._lock_provider and args[0] in self._SERIALIZED_SUBCOMMANDS:
            lock = self._lock_provider(cwd)
        if lock is not None:
            async with lock:
                return await self._arun_git_result_unlocked(
                    args, cwd=cwd, stdin=stdin, env=overrides, timeout=timeout
                )
        return await self._arun_git_result_unlocked(
            args, cwd=cwd, stdin=stdin, env=overrides, timeout=timeout
        )

    async def _arun_git_result_unlocked(
        self,
        args: list[str],
        *,
        cwd: str,
        stdin: str | None,
        env: dict[str, str],
        timeout: int | None,
    ) -> subprocess.CompletedProcess:
        effective_timeout = timeout or self._GIT_TIMEOUT
        command = ["git", *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**self._SUBPROCESS_ENV, **env},
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(None if stdin is None else stdin.encode("utf-8")),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                raise GitError(
                    f"git {' '.join(args)} timed out after {effective_timeout}s "
                    "(possible credential prompt)"
                )
        except FileNotFoundError as exc:
            if not Path(cwd).is_dir():
                raise GitError(f"git working directory does not exist: {cwd}") from exc
            raise GitError("git executable not found") from exc
        return subprocess.CompletedProcess(
            args=command,
            returncode=proc.returncode,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    @asynccontextmanager
    async def arepository_transaction(self, cwd: str) -> AsyncIterator[None]:
        """Serialize a compound object/ref operation for one Git store."""
        lock = self._lock_provider(cwd) if self._lock_provider else None
        if lock is None:
            key = str(Path(cwd).resolve())
            lock = self._repository_operation_locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

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
            self.push_validated_delivery(
                checkout_path,
                f"origin/{default_branch}",
                branch_name,
                branch_name,
            )
        except GitError:
            try:
                self.push_validated_delivery(
                    checkout_path,
                    f"origin/{default_branch}",
                    branch_name,
                    branch_name,
                    force_with_lease=True,
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
            self.push_validated_delivery(
                checkout_path,
                f"origin/{default_branch}",
                "HEAD",
                branch_name,
                force_with_lease=True,
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
        """Safely publish a local branch relative to the repository default.

        When *force_with_lease* is ``True``, uses ``--force-with-lease`` so the
        push is safe for retries: if the branch was already pushed in a
        previous attempt, a second push with amended/additional commits will
        succeed as long as no *other* user pushed to the same branch in the
        meantime.  This resolves **Gap G5** for PR branch pushes.

        The source is resolved once (a plain branch name as
        ``refs/heads/<name>``, so a same-named tag can neither shadow nor
        block it) and its merge-base diff is checked for daemon-owned paths
        before the exact object ID is pushed. This keeps the retained
        synchronous API safe for task-delivery use.
        """
        default_branch = self.get_default_branch(checkout_path)
        self.push_validated_delivery(
            checkout_path,
            f"origin/{default_branch}",
            branch_name,
            branch_name,
            force_with_lease=force_with_lease,
        )

    def _resolve_delivery_tip(self, checkout_path: str, source: str) -> str:
        """Synchronous twin of :meth:`_aresolve_delivery_tip`.

        A plain branch name is resolved as ``refs/heads/<name>`` so a
        same-named tag cannot shadow it (see :func:`_delivery_source_rev`);
        a missing branch fails closed rather than falling back to whatever
        else carries the name.
        """
        rev = _delivery_source_rev(source)
        try:
            tip = self._run(["rev-parse", "--verify", rev], cwd=checkout_path).strip()
        except GitError as e:
            raise GitError(f"could not resolve delivery source {rev}: {e}") from e
        if not _OID_RE.fullmatch(tip.lower()):
            raise GitError(f"could not resolve immutable delivery tip for {source}")
        return tip

    def push_validated_delivery(
        self,
        checkout_path: str,
        base_ref: str,
        source_ref: str,
        branch: str,
        *,
        force_with_lease: bool = False,
    ) -> str:
        """Synchronously resolve once, validate, and push an exact delivery OID.

        The synchronous twin of :meth:`apush_validated_delivery` for the
        retained sync delivery paths (:meth:`push_branch`,
        :meth:`mid_chain_sync`, :meth:`sync_and_merge`).  ``force_with_lease``
        only changes how the remote ref may move; it cannot bypass the
        reserved-path check.
        """
        source_ref = _validate_rev(source_ref, field="delivery source")
        base_ref = _validate_rev(base_ref, field="delivery base")
        branch = _validate_ref(branch)
        tip = self._resolve_delivery_tip(checkout_path, source_ref)
        paths = self.reserved_paths_in_diff(checkout_path, base_ref, tip)
        if paths:
            raise GitError("reserved delivery paths: " + ", ".join(paths))
        self._push_oid(
            checkout_path,
            tip,
            branch,
            force_with_lease=force_with_lease,
        )
        return tip

    def _push_oid(
        self,
        checkout_path: str,
        tip: str,
        branch: str,
        *,
        force_with_lease: bool = False,
    ) -> None:
        """Synchronously push an OID without consulting a mutable ref."""
        if not _OID_RE.fullmatch(tip.lower()):
            raise GitError("invalid immutable push tip")
        branch = _validate_ref(branch)
        args = ["push", "origin", f"{tip}:refs/heads/{branch}"]
        if force_with_lease:
            args.insert(2, "--force-with-lease")
        self._run(args, cwd=checkout_path)

    def reserved_paths_in_diff(
        self,
        checkout_path: str,
        base_ref: str,
        tip_ref: str,
    ) -> list[str]:
        """Synchronously return daemon-owned paths changed by a delivery tip.

        The synchronous twin of :meth:`areserved_paths_in_diff`: the
        comparison starts at the merge-base and runs with ``--no-renames`` so
        a rename lists both its source and its destination. Git failures
        propagate — this is a fail-closed delivery gate.
        """
        base_ref = _validate_rev(base_ref, field="delivery base")
        tip_ref = _validate_rev(tip_ref, field="delivery tip")
        merge_base = self._run(["merge-base", base_ref, tip_ref], cwd=checkout_path)
        changed = self._run(
            ["diff", "--no-renames", "--name-only", "-z", merge_base, tip_ref, "--"],
            cwd=checkout_path,
        )
        return sorted(self._daemon_bookkeeping_paths(changed))

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
                self.push_validated_delivery(
                    checkout_path,
                    f"origin/{default_branch}",
                    default_branch,
                    default_branch,
                )
                return (True, "")
            except GitError as e:
                if str(e).startswith("reserved delivery paths:"):
                    return (False, f"delivery_guard_failed: {e}")
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
                or path == ".github/agent-queue-integration.json"
            )
        ]

    def commit_all(
        self,
        checkout_path: str,
        message: str,
        *,
        exclude_plans: bool = True,
        no_verify: bool = False,
    ) -> bool:
        """Stage all changes and commit. Returns True if a commit was made, False if nothing to commit.

        Uses add-all-then-check-staged pattern: ``git add -A`` stages
        everything (including untracked files the agent created), then
        ``git diff --cached --quiet`` checks whether anything is actually
        staged.  This avoids the race condition of checking status before
        staging.

        Plan files (``.claude/plan.md``, ``plan.md``, ``.claude/plans/``)
        are automatically unstaged to prevent them from being committed to
        target repos unless *exclude_plans* is ``False``.  System-level
        operations (auto-remediation, plan archival, workspace cleanup)
        should pass ``exclude_plans=False`` to ensure all changes are
        committed.

        Pass ``no_verify=True`` to skip pre-commit hooks (``--no-verify``).
        This is intended for system-level auto-remediation commits where
        hook failures would prevent workspace cleanup.
        """
        self._run(["add", "-A"], cwd=checkout_path)
        # Unstage plan files so they never reach target repo history.
        if exclude_plans:
            for pattern in self._PLAN_FILE_EXCLUDES:
                try:
                    self._run(["reset", "HEAD", "--", pattern], cwd=checkout_path)
                except GitError:
                    pass  # Not staged or doesn't exist — fine
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
        commit_args = ["commit", "-m", message]
        if no_verify:
            commit_args.append("--no-verify")
        self._run(commit_args, cwd=checkout_path)
        return True

    def create_pr(
        self, checkout_path: str, branch: str, title: str, body: str,
        base: str = "main", *, repository: GitHubRepositoryBinding | None = None,
    ) -> str:
        """Compatibility entry point; the async shared client owns execution."""
        return asyncio.run(self.acreate_pr(
            checkout_path, branch, title, body, base, repository=repository
        ))

    def check_pr_merged(
        self, checkout_path: str, pr_url: str, *,
        repository: GitHubRepositoryBinding | None = None,
    ) -> bool | None:
        """Compatibility entry point; the async shared client owns execution."""
        return asyncio.run(self.acheck_pr_merged(
            checkout_path, pr_url, repository=repository
        ))

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

    # ------------------------------------------------------------------
    # Async public API
    #
    # Each method below is the async counterpart of its synchronous twin,
    # using ``_arun`` / ``_arun_subprocess`` instead of ``_run`` /
    # ``subprocess.run``.  All production callers (orchestrator, command
    # handler, Discord bot) use these exclusively.  The synchronous API
    # is retained only for backward compatibility and tests.
    # ------------------------------------------------------------------

    async def acreate_checkout(
        self, repo_url: str, checkout_path: str, *, no_checkout: bool = False
    ) -> None:
        if self._uses_existing_ssh(repo_url):
            os.makedirs(os.path.dirname(checkout_path), exist_ok=True)
            args = ["clone", "--no-checkout"] if no_checkout else ["clone"]
            await self._arun([*args, repo_url, checkout_path])
            return
        binding = await self._abind_git_repository(repo_url)
        if binding is not None and self.github_access is not None:
            token = await self._atoken_for_repository(binding)
            await self._aclone_with_auth_to_url(
                repo_url,
                checkout_path,
                source_url=f"https://github.com/{binding.full_name}.git",
                token=token,
                no_checkout=no_checkout,
            )
            return
        os.makedirs(os.path.dirname(checkout_path), exist_ok=True)
        args = ["clone", "--no-checkout"] if no_checkout else ["clone"]
        await self._arun([*args, repo_url, checkout_path])

    async def acreate_bare_checkout(self, repo_url: str, checkout_path: str) -> None:
        """Create a bare retained store through the selected repository authority."""
        if self._uses_existing_ssh(repo_url):
            os.makedirs(os.path.dirname(checkout_path), exist_ok=True)
            await self._arun(["clone", "--bare", "--", repo_url, checkout_path])
            return
        binding = await self._abind_git_repository(repo_url)
        if binding is not None:
            token = await self._atoken_for_repository(binding)
            await self._aclone_with_auth_to_url(
                repo_url, checkout_path,
                source_url=f"https://github.com/{binding.full_name}.git",
                token=token, bare=True,
            )
            return
        os.makedirs(os.path.dirname(checkout_path), exist_ok=True)
        await self._arun(["clone", "--bare", "--", repo_url, checkout_path])

    @staticmethod
    def _is_github_ssh_url(repo_url: str) -> bool:
        return repo_url.lower().startswith(("git@github.com:", "ssh://git@github.com"))

    def _uses_existing_ssh(self, repo_url: str) -> bool:
        selected = (
            self.github_access is not None
            and self.github_access.auth.mode is GitHubCredentialMode.EXISTING_LOGIN
            and self._is_github_ssh_url(repo_url)
        )
        if selected:
            from src.projects.github import GitHubError, parse_github_repository

            try:
                parse_github_repository(repo_url)
            except GitHubError as exc:
                raise GitError("invalid GitHub repository source") from exc
        return selected

    async def _abind_git_repository(self, repo_url: str) -> GitHubRepositoryBinding | None:
        """Resolve an explicit GitHub source, never a repository guessed from cwd."""
        if (
            self.github_access is None
            or repo_url.startswith(("/", "./", "../", "file://"))
            or Path(repo_url).exists()
        ):
            return None
        from src.projects.github import GitHubError, parse_github_repository

        try:
            parse_github_repository(repo_url)
        except GitHubError as exc:
            if "github.com" in repo_url.lower():
                raise GitError("invalid GitHub repository source") from exc
            return None
        return await self.github_access.bind_repository(repo_url)

    async def _atoken_for_repository(self, binding: GitHubRepositoryBinding) -> str | None:
        if self.github_access is None:
            raise GitError("GitHub access service is not configured")
        if not isinstance(binding, GitHubRepositoryBinding):
            raise GitError("an authorized GitHub repository binding is required")
        token = await self.github_access.installation_token(binding)
        if self.github_access.auth.mode is GitHubCredentialMode.APP and not token:
            raise GitError("GitHub App credential is unavailable")
        return token

    async def _aclone_with_auth_to_url(
        self,
        configured_url: str,
        checkout_path: str,
        *,
        source_url: str,
        token: str | None,
        bare: bool = False,
        no_checkout: bool = False,
    ) -> None:
        """Clone in isolation and move a credential-free local clone into place."""
        destination = Path(checkout_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and any(destination.iterdir()):
            raise GitError("checkout destination is not empty")
        deadline = asyncio.get_running_loop().time() + self._GIT_TIMEOUT
        with tempfile.TemporaryDirectory(prefix="aq-app-clone-", dir=destination.parent) as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            home = root / "home"
            home.mkdir(mode=0o700)
            imported = root / "source.git"
            staged = root / "checkout"
            await self._arun_authenticated_git(
                ["clone", "--bare", "--no-local", "--template=", source_url, str(imported)],
                home=home,
                repository_url=source_url,
                token=token,
                deadline=deadline,
                budget_seconds=self._GIT_TIMEOUT,
            )
            clone_args = ["-c", "core.hooksPath=/dev/null", "clone"]
            if bare:
                clone_args.append("--bare")
            elif no_checkout:
                clone_args.append("--no-checkout")
            clone_args.extend(["--no-local", "--template=", str(imported), str(staged)])
            await self._run_isolated_import_git(
                clone_args,
                home=home,
                deadline=deadline,
            )
            await self._run_isolated_import_git(
                ["-C", str(staged), "config", "remote.origin.url", configured_url],
                home=home,
                deadline=deadline,
            )
            if bare:
                await self._run_isolated_import_git(
                    ["-C", str(staged), "config", "remote.origin.fetch",
                     "+refs/heads/*:refs/remotes/origin/*"],
                    home=home, deadline=deadline,
                )
            os.replace(staged, destination)

    async def afetch_origin(
        self,
        checkout_path: str,
        *,
        repository_url: str,
        lock_held: bool = False,
        all_heads: bool = False,
    ) -> None:
        """Fetch an explicitly authorized origin without credentials in the checkout."""
        fetch_args = (
            ["fetch", "--no-tags", "--prune", "origin",
             "+refs/heads/*:refs/remotes/origin/*"]
            if all_heads else ["fetch", "origin"]
        )
        if self._uses_existing_ssh(repository_url):
            from src.projects.github import GitHubError, parse_github_repository

            configured_origin = await self._arun(
                ["config", "--get", "remote.origin.url"], cwd=checkout_path
            )
            try:
                matches = (
                    parse_github_repository(configured_origin).full_name
                    == parse_github_repository(repository_url).full_name
                )
            except GitHubError:
                matches = False
            if not matches:
                raise GitError("GitHub repository reference did not match the authorized repository")
            run = self._arun_unlocked if lock_held else self._arun
            await run(fetch_args, cwd=checkout_path)
            return
        binding = await self._abind_git_repository(repository_url)
        if binding is None:
            if (
                self.github_access is not None
                and self.github_access.auth.mode is GitHubCredentialMode.APP
            ):
                configured_origin = await self._arun(
                    ["config", "--get", "remote.origin.url"], cwd=checkout_path
                )
                if "github.com" in configured_origin.lower():
                    raise GitError("authorized GitHub repository is required for App fetch")
            run = self._arun_unlocked if lock_held else self._arun
            await run(fetch_args, cwd=checkout_path)
            return
        assert self.github_access is not None
        configured_origin = await self._arun(
            ["config", "--get", "remote.origin.url"], cwd=checkout_path
        )
        self.github_access.validate_repository_reference(binding, configured_origin)
        source_url = f"https://github.com/{binding.full_name}.git"

        async def acquire() -> None:
            token = await self._atoken_for_repository(binding)
            await self._afetch_origin_with_auth_to_url(
                checkout_path, source_url=source_url, token=token
            )

        if lock_held:
            await acquire()
        else:
            async with self.arepository_transaction(checkout_path):
                await acquire()

    async def _afetch_origin_with_auth_to_url(
        self,
        checkout_path: str,
        *,
        source_url: str,
        token: str | None,
    ) -> None:
        """Private file-URL seam for a staged branch/tag fetch and local import."""
        deadline = asyncio.get_running_loop().time() + self._GIT_TIMEOUT
        with tempfile.TemporaryDirectory(prefix="aq-app-origin-fetch-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            home = root / "home"
            home.mkdir(mode=0o700)
            imported = root / "source.git"
            await self._run_isolated_import_git(
                ["init", "--bare", "--template=", str(imported)],
                home=home,
                deadline=deadline,
            )
            await self._arun_authenticated_git(
                [f"--git-dir={imported}", "fetch", "--no-tags", "--force", source_url,
                 "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"],
                home=home,
                repository_url=source_url,
                token=token,
                deadline=deadline,
                budget_seconds=self._GIT_TIMEOUT,
            )
            destination_git_dir = await self._arun(
                ["rev-parse", "--absolute-git-dir"], cwd=checkout_path
            )
            await self._run_isolated_import_git(
                ["-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
                 f"--git-dir={destination_git_dir}", "fetch", "--no-tags", "--force", "--prune",
                 str(imported), "+refs/heads/*:refs/remotes/origin/*"],
                home=home,
                deadline=deadline,
            )
            await self._run_isolated_import_git(
                ["-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
                 f"--git-dir={destination_git_dir}", "fetch", "--no-tags", "--force",
                 str(imported), "+refs/tags/*:refs/tags/*"],
                home=home,
                deadline=deadline,
            )
            imported_refs = await self._run_isolated_import_git(
                [f"--git-dir={imported}", "for-each-ref", "--format=%(objectname) %(refname)",
                 "refs/heads", "refs/tags"],
                home=home, deadline=deadline,
            )
            destination_refs = await self._run_isolated_import_git(
                [f"--git-dir={destination_git_dir}", "for-each-ref",
                 "--format=%(objectname) %(refname)", "refs/remotes/origin", "refs/tags"],
                home=home, deadline=deadline,
            )
            imported_map = dict(
                (ref, oid) for oid, ref in
                (line.split(" ", 1) for line in imported_refs.decode("ascii").splitlines())
            )
            destination_map = dict(
                (ref, oid) for oid, ref in
                (line.split(" ", 1) for line in destination_refs.decode("ascii").splitlines())
            )
            for ref, oid in imported_map.items():
                destination_ref = (
                    ref.replace("refs/heads/", "refs/remotes/origin/", 1)
                    if ref.startswith("refs/heads/") else ref
                )
                if destination_map.get(destination_ref) != oid:
                    raise GitError("authenticated Git fetch imported a different ref")

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

    async def ahas_remote(
        self, checkout_path: str, remote: str = "origin", *, strict: bool = False
    ) -> bool | None:
        """Return whether *remote* exists, or ``None`` on probe failure in strict mode."""
        try:
            result = await self._arun_subprocess(
                ["git", "remote"], cwd=checkout_path, timeout=self._GIT_TIMEOUT
            )
        except Exception:
            return None if strict else False
        if result.returncode != 0:
            return None if strict else False
        return remote in (result.stdout or "").splitlines()

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
            await self.apush_validated_delivery(
                checkout_path,
                f"origin/{default_branch}",
                branch_name,
                branch_name,
            )
        except GitError:
            try:
                await self.apush_validated_delivery(
                    checkout_path,
                    f"origin/{default_branch}",
                    branch_name,
                    branch_name,
                    force_with_lease=True,
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
        expected_remote_oid: str | None = None,
        event_bus: EventBus | None = None,
        project_id: str | None = None,
    ) -> str:
        """Push a named branch for an explicit, non-delivery Git command.

        This primitive pins the branch to an object ID but intentionally does
        not apply the daemon delivery-path policy. Automatic task delivery
        must use :meth:`apush_validated_delivery` with its target base.
        Returns the exact commit ID that was published.
        """
        _validate_ref(branch_name)
        expected_remote_oid = _expected_remote_oid(expected_remote_oid, force_with_lease)
        tip = await self._aresolve_delivery_tip(checkout_path, branch_name)
        remote_ref_before = await self._apush_oid(
            checkout_path,
            tip,
            branch_name,
            force_with_lease=force_with_lease or expected_remote_oid is not None,
            expected_old_oid=expected_remote_oid,
        )

        await self._aemit_push_event(
            checkout_path,
            branch_name,
            remote_ref_before,
            tip,
            event_bus=event_bus,
            project_id=project_id,
        )
        return tip

    async def _aremote_ref_before_push(
        self,
        checkout_path: str,
        branch: str,
        *,
        event_bus: EventBus | None,
    ) -> str | None:
        """``origin/<branch>`` as the remote-tracking ref has it, or ``None``.

        Read *before* a push so the ``git.push`` event can carry the
        ``<before>..<tip>`` range the push actually moved the branch over.
        ``None`` means the remote branch does not exist yet (a first push) --
        or that no event bus was given, in which case there is no event to
        compute the range for and the extra ``rev-parse`` is skipped.
        """
        if event_bus is None:
            return None
        try:
            out = await self._arun(
                ["rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
                cwd=checkout_path,
            )
        except GitError:
            return None
        return out.strip() or None

    @staticmethod
    def _push_commit_range(remote_ref_before: str | None, tip: str) -> str:
        """The ``commit_range`` a ``git.push`` event carries.

        One shape for every push path: ``<remote-before>..<tip>`` when the
        remote branch already existed, the bare ``<tip>`` on a first push.
        """
        if remote_ref_before:
            return f"{remote_ref_before}..{tip}"
        return tip

    async def _aemit_push_event(
        self,
        checkout_path: str,
        branch: str,
        remote_ref_before: str | None,
        tip: str,
        *,
        event_bus: EventBus | None,
        project_id: str | None,
    ) -> None:
        """Emit ``git.push`` after a successful push, best-effort.

        Never fails the push because the event could not be emitted.
        """
        if event_bus is None:
            return
        try:
            await event_bus.emit(
                "git.push",
                {
                    "branch": branch,
                    "remote": "origin",
                    "commit_range": self._push_commit_range(remote_ref_before, tip),
                    "project_id": project_id,
                },
            )
        except Exception:
            logger.debug("Failed to emit git.push event for %s", checkout_path, exc_info=True)

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
        if delete_remote:
            deadline = asyncio.get_running_loop().time() + APP_AUTH_PUSH_TIMEOUT_SECONDS
            destination_url, token = await self._apush_destination(checkout_path, "origin")
            observed = await self._aobserved_remote_head(
                checkout_path, branch_name, remote="origin", destination_url=destination_url,
                token=token, deadline=deadline,
            )
            if observed is not None:
                local_tip = await self.arev_parse(
                    checkout_path, f"refs/heads/{branch_name}"
                )
                if local_tip is None or local_tip != observed:
                    raise GitError("remote branch moved beyond its local cleanup tip")
                await self._atransfer_exact_ref(
                    checkout_path, None, branch_name, observed,
                    remote="origin", destination_url=destination_url, token=token,
                    deadline=deadline, lock_held=False,
                )
        try:
            await self._arun(["branch", "-d", branch_name], cwd=checkout_path)
        except GitError:
            try:
                await self._arun(["branch", "-D", branch_name], cwd=checkout_path)
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

    async def aremove_worktree_exact(self, source_path: str, worktree_path: str) -> None:
        """Remove a verified retained worktree without discarding dirty files."""
        await self._arun(["worktree", "remove", worktree_path], cwd=source_path)

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
                    value[len("refs/heads/") :] if value.startswith("refs/heads/") else value
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
            return (
                git_path
                if os.path.isabs(git_path)
                else os.path.abspath(os.path.join(checkout_path, git_path))
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
            output = await self._arun(["diff", "--name-only", base_branch, "--"], cwd=checkout_path)
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

        Pass ``no_verify=True`` to skip pre-commit hooks (``--no-verify``).
        This is intended for system-level auto-remediation commits where
        hook failures would prevent workspace cleanup.

        When *event_bus* is provided, a ``git.commit`` event is emitted
        after a successful commit with the commit hash, branch, changed
        files, message, and optional *project_id* / *agent_id*.

        **Trust boundary (R4, flag-value case).**  *message* is agent-authored
        and therefore untrusted, but it only ever reaches git as the value of
        the ``-m`` flag in an argv list — never interpolated into a shell
        string and never in a position git could read as an option.  The
        ``["reset", "HEAD", "--", pattern]`` call below is the template for
        pathspec arguments.  See ``docs/specs/design/trust-and-ops.md`` §2.4.
        """
        await self._arun(["add", "-A"], cwd=checkout_path)
        if exclude_plans:
            for pattern in self._PLAN_FILE_EXCLUDES:
                try:
                    await self._arun(["reset", "HEAD", "--", pattern], cwd=checkout_path)
                except GitError:
                    pass
        result = await self._arun_subprocess(
            ["git", "diff", "--cached", "--quiet"],
            cwd=checkout_path,
            timeout=self._GIT_TIMEOUT,
        )
        if result.returncode == 0:
            return False
        commit_args = ["commit", "-m", message]
        if no_verify:
            commit_args.append("--no-verify")
        await self._arun(commit_args, cwd=checkout_path)

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
        *,
        repository: GitHubRepositoryBinding | None = None,
    ) -> str:
        _validate_ref(branch)
        _validate_ref(base, field="base branch")
        try:
            creation = await self._github_client(repository).create_pull_request_result(
                title=title, body=body, base=base, head=branch
            )
        except (GitHubAccessError, ValueError) as exc:
            raise GitError(f"could not create PR: {exc}") from exc

        pr_url = creation.url
        if event_bus is not None and creation.created:
            try:
                await event_bus.emit(
                    "git.pr.created",
                    {"pr_url": pr_url, "branch": branch, "title": title,
                     "project_id": project_id},
                )
            except Exception:
                logger.debug("Failed to emit git.pr.created event for %s", checkout_path,
                             exc_info=True)
        return pr_url

    async def acheck_pr_merged(
        self, checkout_path: str, pr_url: str, *,
        repository: GitHubRepositoryBinding | None = None,
    ) -> bool | None:
        try:
            data = await self._github_client(repository).pull_request(pr_url)
        except GitHubAccessError as exc:
            raise GitError(f"could not view PR: {exc}") from exc
        if data.get("merged_at"):
            sha = data.get("merge_commit_sha")
            if data.get("state") != "closed" or not isinstance(sha, str) or not _OID_RE.fullmatch(sha):
                raise GitError("merged PR state was incomplete")
            return True
        if data.get("state") == "open":
            return False
        return None

    async def amerge_pr(
        self,
        checkout_path: str,
        pr_url: str,
        method: str = "squash",
        *,
        expected_head_oid: str | None = None,
        expected_base_ref: str | None = None,
        repository: GitHubRepositoryBinding | None = None,
    ) -> dict:
        """Merge only the validated head and base through the shared client."""
        if method not in ("squash", "merge", "rebase"):
            return {"success": False, "sha": None, "error": f"invalid method: {method}"}
        if expected_head_oid is not None:
            expected_head_oid = expected_head_oid.lower()
            if not _OID_RE.fullmatch(expected_head_oid):
                return {"success": False, "sha": None, "error": "invalid expected PR head OID"}
        try:
            current = await self.avalidate_pr_for_merge(
                checkout_path, pr_url, repository=repository
            )
        except GitError as exc:
            return {"success": False, "sha": None, "error": str(exc)}
        if expected_head_oid is not None and current.head_oid != expected_head_oid:
            return {
                "success": False, "sha": None,
                "error": ("PR identity changed after validation (head moved from "
                          f"{expected_head_oid} to {current.head_oid}); refusing merge"),
            }
        if expected_base_ref is not None and current.base_ref != expected_base_ref:
            return {
                "success": False, "sha": None,
                "error": ("PR identity changed after validation (retargeted from "
                          f"{expected_base_ref} to {current.base_ref}); refusing merge"),
            }
        from src.git.github import GitHubMergeReconciled

        try:
            sha = await self._github_client(repository).merge_pull_request(
                pr_url, method=method, expected_head_oid=current.head_oid,
                expected_base_ref=current.base_ref,
            )
        except GitHubMergeReconciled as exc:
            return {
                "success": True, "sha": exc.sha, "error": None,
                "outcome": exc.outcome,
            }
        except (GitError, GitHubAccessError, ValueError) as exc:
            return {"success": False, "sha": None, "error": str(exc)}
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
        result = await self.als_remote_ref(checkout_path, branch)
        return result.oid if result.state is RemoteRefState.PRESENT else None

    async def als_remote_ref(
        self, checkout_path: str, branch: str, *, remote: str = "origin",
        repository_url: str | None = None,
    ) -> RemoteRefResult:
        """Read one exact remote head without conflating absence and I/O failure.

        ``repository_url`` confines the read to that authorized repository;
        a checkout remote naming another one is an error, not an observation.
        """
        branch = _validate_ref(branch)
        try:
            destination_url, token = await self._apush_destination(
                checkout_path, remote, repository_url=repository_url
            )
            if destination_url is not None:
                oid = await self._aobserved_remote_head(
                    checkout_path, branch, remote=remote,
                    destination_url=destination_url, token=token,
                    deadline=asyncio.get_running_loop().time() + self._GIT_TIMEOUT,
                )
                return RemoteRefResult(
                    RemoteRefState.PRESENT if oid is not None else RemoteRefState.ABSENT,
                    oid=oid,
                )
        except (GitError, GitHubAccessError, asyncio.TimeoutError) as exc:
            return RemoteRefResult(RemoteRefState.ERROR, error=str(exc))
        result = await self.arun_git_result(
            ["ls-remote", "--heads", remote, f"refs/heads/{branch}"],
            cwd=checkout_path,
            env={"LC_ALL": "C"},
        )
        if result.returncode != 0:
            return RemoteRefResult(
                RemoteRefState.ERROR,
                error=(result.stderr or result.stdout or "git ls-remote failed").strip(),
            )
        expected_ref = f"refs/heads/{branch}"
        matches: list[str] = []
        for line in result.stdout.splitlines():
            sha, separator, ref = line.partition("\t")
            candidate = sha.strip().lower()
            if separator and ref.strip() == expected_ref and _OID_RE.fullmatch(candidate):
                matches.append(candidate)
        if not matches:
            return RemoteRefResult(RemoteRefState.ABSENT)
        if len(matches) != 1:
            return RemoteRefResult(RemoteRefState.ERROR, error="remote returned duplicate refs")
        return RemoteRefResult(RemoteRefState.PRESENT, oid=matches[0])

    async def afetch_repository_oid(
        self, destination_git_dir: str, *, repository: GitHubRepositoryBinding,
        oid: str, destination_ref: str,
    ) -> str:
        """Import an exact repository object with a credential selected for this fetch."""
        token = await self._atoken_for_repository(repository)
        return await self.afetch_exact_oid_with_app_auth(
            destination_git_dir, repository=repository, token=token,
            oid=oid, destination_ref=destination_ref,
        )

    async def apush_repository_oid(
        self, checkout_path: str, *, repository: GitHubRepositoryBinding,
        tip_oid: str, branch: str, expected_old_oid: str,
        authority_deadline: float | None = None,
    ) -> str:
        """Transfer an immutable OID under an exact lease with fresh credentials."""
        token = await self._atoken_for_repository(repository)
        return await self.apush_oid_with_app_auth(
            checkout_path, repository=repository, token=token,
            tip_oid=tip_oid, branch=branch, expected_old_oid=expected_old_oid,
            authority_deadline=authority_deadline,
        )

    async def adelete_repository_ref(
        self, checkout_path: str, *, repository: GitHubRepositoryBinding,
        branch: str, expected_old_oid: str,
        authority_deadline: float | None = None,
    ) -> str:
        """Lease-delete one repository head with a credential selected now."""
        token = await self._atoken_for_repository(repository)
        return await self.adelete_ref_with_app_auth(
            checkout_path, repository=repository, token=token,
            branch=branch, expected_old_oid=expected_old_oid,
            authority_deadline=authority_deadline,
        )

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

        This is a non-delivery recovery primitive: stranded-work preservation
        must save the complete commit even when it contains daemon-owned
        paths. Automatic task delivery must use
        :meth:`apush_validated_delivery` instead.
        """
        tip = await self._aresolve_delivery_tip(checkout_path, "HEAD")
        remote_ref_before = await self._apush_oid(checkout_path, tip, branch)
        await self._aemit_push_event(
            checkout_path,
            branch,
            remote_ref_before,
            tip,
            event_bus=event_bus,
            project_id=project_id,
        )

    async def _aresolve_delivery_tip(self, checkout_path: str, source: str) -> str:
        """Resolve a delivery *source* to the one commit id that will be pushed.

        A plain branch name is resolved as ``refs/heads/<name>`` so a
        same-named tag cannot shadow it (see :func:`_delivery_source_rev`);
        a missing branch fails closed rather than falling back to whatever
        else carries the name.
        """
        rev = _delivery_source_rev(source)
        try:
            tip = (await self._arun(["rev-parse", "--verify", rev], cwd=checkout_path)).strip()
        except GitError as e:
            raise GitError(f"could not resolve delivery source {rev}: {e}") from e
        if not _OID_RE.fullmatch(tip.lower()):
            raise GitError(f"could not resolve immutable delivery tip for {source}")
        return tip

    async def apush_validated_ref(
        self,
        checkout_path: str,
        source_ref: str,
        branch: str,
        *,
        force_with_lease: bool = False,
        expected_old_oid: str | None = None,
    ) -> str:
        """Resolve *source_ref* once and push that exact commit to *branch*.

        A merge/rebase hook or another local process may move a named ref after
        its content has been guarded. The object-ID refspec makes Git deliver
        precisely the validated object rather than resolving the name again.

        This low-level helper does not enforce reserved-path policy. Automatic
        task delivery must use :meth:`apush_validated_delivery`.
        """
        source_ref = _validate_rev(source_ref, field="push source")
        branch = _validate_ref(branch)
        tip = await self._aresolve_delivery_tip(checkout_path, source_ref)
        await self._apush_oid(
            checkout_path,
            tip,
            branch,
            force_with_lease=force_with_lease,
            expected_old_oid=expected_old_oid,
        )
        return tip

    async def adelete_remote_ref_exact(
        self, checkout_path: str, branch: str, expected_old_oid: str,
        *, remote: str = "origin",
    ) -> None:
        """Delete a remote branch only under its observed exact old-tip lease."""
        branch = _validate_ref(branch)
        if not isinstance(expected_old_oid, str) or _OID_RE.fullmatch(expected_old_oid) is None:
            raise GitError("invalid expected target OID")
        deadline = asyncio.get_running_loop().time() + APP_AUTH_PUSH_TIMEOUT_SECONDS
        destination_url, token = await self._apush_destination(checkout_path, remote)
        observed = await self._aobserved_remote_head(
            checkout_path, branch, remote=remote,
            destination_url=destination_url, token=token, deadline=deadline,
        )
        if observed is None:
            return
        if observed != expected_old_oid:
            raise GitError("remote head differs from expected target")
        await self._atransfer_exact_ref(
            checkout_path, None, branch, expected_old_oid,
            remote=remote, destination_url=destination_url, token=token,
            deadline=deadline, lock_held=False,
        )

    async def _apush_oid(
        self,
        checkout_path: str,
        tip: str,
        branch: str,
        *,
        force_with_lease: bool = False,
        expected_old_oid: str | None = None,
        remote: str = "origin",
        lock_held: bool = False,
        repository_url: str | None = None,
    ) -> str | None:
        """Publish an immutable commit under an observed, exact remote lease.

        Ordinary pushes also prove ancestry before using the lease. A lease
        alone permits non-fast-forward replacement, so it is never a substitute
        for that proof. The post-transfer read settles an uncertain response
        before callers emit a push event. ``repository_url`` names the
        authorized repository; every destination resolution for this push is
        checked against it before a credential is selected.
        """
        if not _OID_RE.fullmatch(tip.lower()):
            raise GitError("invalid immutable push tip")
        branch = _validate_ref(branch)
        if force_with_lease and expected_old_oid is None:
            expected_old_oid = await self._alocal_tracking_oid(checkout_path, remote, branch)
        deadline = asyncio.get_running_loop().time() + APP_AUTH_PUSH_TIMEOUT_SECONDS
        destination_url, token = await self._apush_destination(
            checkout_path, remote, repository_url=repository_url
        )
        observed = await self._aobserved_remote_head(
            checkout_path, branch, remote=remote, destination_url=destination_url,
            token=token, deadline=deadline, repository_url=repository_url,
        )
        explicit_expected = expected_old_oid is not None
        if explicit_expected:
            if not isinstance(expected_old_oid, str) or _OID_RE.fullmatch(expected_old_oid) is None:
                raise GitError("invalid expected target OID")
            if observed != (None if expected_old_oid == _ZERO_OID else expected_old_oid):
                raise GitError("remote head differs from expected target")
        else:
            expected_old_oid = observed or _ZERO_OID
        if observed is not None and not force_with_lease:
            try:
                await self._aensure_remote_ancestor(
                    checkout_path, observed, tip, remote=remote,
                    destination_url=destination_url, token=token, deadline=deadline,
                    lock_held=lock_held,
                )
            except GitError as exc:
                if not explicit_expected:
                    raise
                raise GitError("delivery tip is not a descendant of its expected target") from exc
        await self._atransfer_exact_ref(
            checkout_path, tip, branch, expected_old_oid,
            remote=remote, destination_url=destination_url, token=token,
            deadline=deadline, lock_held=lock_held, repository_url=repository_url,
        )
        return observed

    async def _alocal_tracking_oid(self, checkout_path: str, remote: str, branch: str) -> str:
        """Preserve the caller's known tip for a requested branch rewrite."""
        remote = _validate_ref(remote, field="remote")
        try:
            oid = await self._arun(
                ["rev-parse", "--verify", f"refs/remotes/{remote}/{branch}"],
                cwd=checkout_path,
            )
        except GitError:
            return _ZERO_OID
        if _OID_RE.fullmatch(oid) is None:
            raise GitError("invalid remote-tracking tip for force-with-lease")
        return oid

    async def _apush_destination(
        self, checkout_path: str, remote: str, *, repository_url: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Bind a named remote or frozen origin URL to its credential source.

        With ``repository_url`` the checkout's remote must name that
        authorized repository, and the credential is selected for the
        authorized record rather than for whatever the checkout configures.
        The comparison runs before binding, so a remote retargeted at another
        repository never reaches credential selection.
        """
        if isinstance(remote, str) and remote.startswith(
            ("/", "./", "../", "file://", "https://", "http://", "ssh://", "git@")
        ):
            if any(ord(char) < 32 for char in remote):
                raise GitError("invalid remote URL")
            configured_url = remote
        else:
            remote = _validate_ref(remote, field="remote")
            configured_url = await self._arun(
                ["config", "--get", f"remote.{remote}.url"], cwd=checkout_path
            )
        if repository_url is not None:
            if not isinstance(repository_url, str) or not repository_url or any(
                ord(char) < 32 for char in repository_url
            ):
                raise GitError("invalid authorized repository URL")
            _require_authorized_repository(configured_url, repository_url, base=checkout_path)
            if self._uses_existing_ssh(configured_url):
                return None, None
            binding = await self._abind_git_repository(repository_url)
            if binding is None:
                return None, None
            return (
                f"https://github.com/{binding.full_name}.git",
                await self._atoken_for_repository(binding),
            )
        if self._uses_existing_ssh(configured_url):
            return None, None
        binding = await self._abind_git_repository(configured_url)
        if binding is None:
            if (
                self.github_access is not None
                and self.github_access.auth.mode is GitHubCredentialMode.APP
                and "github.com" in configured_url.lower()
            ):
                raise GitError("authorized GitHub repository is required for App push")
            return None, None
        assert self.github_access is not None
        self.github_access.validate_repository_reference(binding, configured_url)
        return f"https://github.com/{binding.full_name}.git", await self._atoken_for_repository(binding)

    async def _aobserved_remote_head(
        self,
        checkout_path: str,
        branch: str,
        *,
        remote: str,
        destination_url: str | None,
        token: str | None,
        deadline: float,
        repository_url: str | None = None,
    ) -> str | None:
        branch = _validate_ref(branch)
        if destination_url is None:
            # The re-read of the checkout remote is held to the same authority.
            confinement = {} if repository_url is None else {"repository_url": repository_url}
            result = await self.als_remote_ref(
                checkout_path, branch, remote=remote, **confinement
            )
            if result.state is RemoteRefState.ERROR:
                raise GitError(result.error or "remote head observation failed")
            return result.oid
        with tempfile.TemporaryDirectory(prefix="aq-app-ref-") as temporary:
            home = Path(temporary)
            home.chmod(0o700)
            output = await self._arun_authenticated_git(
                ["ls-remote", "--heads", destination_url, f"refs/heads/{branch}"],
                home=home, repository_url=destination_url, token=token, deadline=deadline,
                budget_seconds=self._GIT_TIMEOUT,
            )
        lines = output.decode("ascii", errors="replace").splitlines()
        if not lines:
            return None
        if len(lines) != 1:
            raise GitError("remote returned duplicate refs")
        oid, separator, ref = lines[0].partition("\t")
        if separator != "\t" or ref != f"refs/heads/{branch}" or _OID_RE.fullmatch(oid) is None:
            raise GitError("remote returned an invalid head")
        return oid

    async def _aensure_remote_ancestor(
        self,
        checkout_path: str,
        old_oid: str,
        tip: str,
        *,
        remote: str,
        destination_url: str | None,
        token: str | None,
        deadline: float,
        lock_held: bool,
    ) -> None:
        if await self.arev_parse(checkout_path, f"{old_oid}^{{commit}}") != old_oid:
            if destination_url is None:
                runner = self._arun_unlocked if lock_held else self._arun
                await runner(["fetch", "--no-tags", remote, old_oid], cwd=checkout_path)
            else:
                git_dir = await self._arun(
                    ["rev-parse", "--absolute-git-dir"], cwd=checkout_path
                )
                temporary_ref = f"refs/aq/push-observed/{uuid.uuid4().hex}"
                try:
                    await self._afetch_exact_oid_with_app_auth_to_url(
                        git_dir, destination_url=destination_url, token=token,
                        oid=old_oid, destination_ref=temporary_ref,
                        timeout_seconds=self._remaining_app_push_budget(deadline),
                    )
                finally:
                    if await self.arev_parse(checkout_path, temporary_ref) == old_oid:
                        await self._arun(
                            ["update-ref", "-d", temporary_ref, old_oid], cwd=checkout_path
                        )
        if not await self._apush_is_ancestor(checkout_path, old_oid, tip):
            raise GitError("normal push would not fast-forward the remote head")

    async def _apush_is_ancestor(self, checkout_path: str, old_oid: str, tip: str) -> bool:
        """Check the real commit graph, ignoring worker-controlled replace refs."""
        result = await self._arun_git_result_unlocked(
            ["merge-base", "--is-ancestor", old_oid, tip], cwd=checkout_path,
            stdin=None,
            env={"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": "/dev/null"},
            timeout=self._GIT_TIMEOUT,
        )
        if result.returncode not in (0, 1):
            raise GitError("could not prove push ancestry")
        return result.returncode == 0

    async def _atransfer_exact_ref(
        self,
        checkout_path: str,
        tip: str | None,
        branch: str,
        expected_old_oid: str,
        *,
        remote: str,
        destination_url: str | None,
        token: str | None,
        deadline: float,
        lock_held: bool,
        repository_url: str | None = None,
    ) -> None:
        async def observe() -> str | None:
            return await self._aobserved_remote_head(
                checkout_path, branch, remote=remote, destination_url=destination_url,
                token=token, deadline=deadline, repository_url=repository_url,
            )

        try:
            if destination_url is None:
                runner = self._arun_unlocked if lock_held else self._arun
                refspec = f"{tip}:refs/heads/{branch}" if tip is not None else f":refs/heads/{branch}"
                await runner(
                    ["push", remote,
                     f"--force-with-lease=refs/heads/{branch}:{expected_old_oid}", refspec],
                    cwd=checkout_path,
                )
            else:
                await self._apush_oid_with_app_auth_to_url(
                    checkout_path, destination_url=destination_url, token=token,
                    tip_oid=tip, branch=branch, expected_old_oid=expected_old_oid,
                    _deadline=deadline,
                )
        except (GitError, asyncio.TimeoutError):
            if await observe() != tip:
                raise
        if await observe() != tip:
            raise GitError("remote head did not confirm exact transfer outcome")

    async def apush_validated_delivery(
        self,
        checkout_path: str,
        base_ref: str | None,
        source_ref: str,
        branch: str,
        *,
        force_with_lease: bool = False,
        expected_remote_oid: str | None = None,
        repository_url: str | None = None,
        event_bus: EventBus | None = None,
        project_id: str | None = None,
    ) -> str:
        """Inspect and push one immutable delivery tip without a ref-name race.

        Resolve the source exactly once, diff that content-addressed OID from
        its target base, then use the same OID in the remote refspec. A later
        mutation of ``HEAD`` or a branch name is therefore irrelevant.

        ``base_ref=None`` is a *root delivery*: the target branch has no base
        on origin (a new default branch cut from a workspace whose recorded
        default was never pushed), so there is no merge-base to diff from and
        nothing on origin has vetted the tree. The reserved gate then covers
        every tracked path in the tip (:meth:`areserved_paths_in_tree`).

        ``expected_remote_oid`` is the caller's explicit lease for a rewrite
        of its own branch (the squash workflow): the push lands only while
        the remote still holds exactly that OID, and all zeros means the
        branch must still be absent. Without it an existing remote head must
        be an ancestor of the tip. ``repository_url`` confines the push to
        that authorized repository (see :meth:`_apush_destination`).
        """
        source_ref = _validate_rev(source_ref, field="delivery source")
        if base_ref is not None:
            base_ref = _validate_rev(base_ref, field="delivery base")
        branch = _validate_ref(branch)
        expected_remote_oid = _expected_remote_oid(expected_remote_oid, force_with_lease)
        tip = await self._aresolve_delivery_tip(checkout_path, source_ref)
        if base_ref is None:
            paths = await self.areserved_paths_in_tree(checkout_path, tip)
        else:
            paths = await self.areserved_paths_in_diff(checkout_path, base_ref, tip)
        if paths:
            raise GitError("reserved delivery paths: " + ", ".join(paths))
        remote_ref_before = await self._apush_oid(
            checkout_path,
            tip,
            branch,
            force_with_lease=force_with_lease or expected_remote_oid is not None,
            expected_old_oid=expected_remote_oid,
            repository_url=repository_url,
        )
        await self._aemit_push_event(
            checkout_path,
            branch,
            remote_ref_before,
            tip,
            event_bus=event_bus,
            project_id=project_id,
        )
        return tip

    async def apush_expected_delivery(
        self,
        checkout_path: str,
        base_oid: str,
        tip_oid: str,
        branch: str,
        expected_old_oid: str,
        *,
        lock_held: bool = False,
        remote: str = "origin",
    ) -> str:
        """Push one validated candidate with an exact remote old-tip lease.

        Callers have already selected the target transition.  This helper
        checks the candidate's local ancestry and daemon-owned-path policy,
        then uses only immutable object IDs in its refspec.  The explicit
        lease is deliberately independent of ``origin/<branch>``: a stale
        tracking ref is not authority to overwrite a concurrent remote move.
        """
        branch = _validate_ref(branch)
        for name, oid in (
            ("delivery base", base_oid),
            ("delivery tip", tip_oid),
            ("expected target", expected_old_oid),
        ):
            if not isinstance(oid, str) or not _OID_RE.fullmatch(oid.lower()):
                raise GitError(f"invalid {name} OID")

        for oid in (base_oid, tip_oid):
            await self._arun(["cat-file", "-e", f"{oid}^{{commit}}"], cwd=checkout_path)
        if not await self._apush_is_ancestor(checkout_path, base_oid, tip_oid):
            raise GitError("delivery tip is not a descendant of its expected base")
        paths = await self.areserved_paths_in_diff(checkout_path, base_oid, tip_oid)
        if paths:
            raise GitError("reserved delivery paths: " + ", ".join(paths))
        await self._apush_oid(
            checkout_path, tip_oid, branch, expected_old_oid=expected_old_oid,
            remote=remote, lock_held=lock_held,
        )
        return tip_oid

    async def apush_oid_with_app_auth(
        self,
        checkout_path: str,
        *,
        repository: GitHubRepositoryBinding,
        token: str | None,
        tip_oid: str,
        branch: str,
        expected_old_oid: str,
        authority_deadline: float | None = None,
    ) -> str:
        """Push one immutable OID through an isolated daemon-owned Git context."""
        loop = asyncio.get_running_loop()
        maximum_deadline = loop.time() + APP_AUTH_PUSH_TIMEOUT_SECONDS
        if authority_deadline is None:
            deadline = maximum_deadline
        else:
            if (
                isinstance(authority_deadline, bool)
                or not isinstance(authority_deadline, (int, float))
                or not math.isfinite(authority_deadline)
            ):
                raise GitError("invalid authenticated Git authority deadline")
            deadline = min(float(authority_deadline), maximum_deadline)
        try:
            self._remaining_app_push_budget(deadline)
        except asyncio.TimeoutError as exc:
            raise GitError("authenticated Git push authority deadline expired") from exc
        if token is not None and (not isinstance(token, str) or not token):
            raise GitError("invalid GitHub App credential")
        branch = _validate_ref(branch)
        for label, oid in (("tip", tip_oid), ("expected target", expected_old_oid)):
            if not isinstance(oid, str) or _OID_RE.fullmatch(oid) is None:
                raise GitError(f"invalid {label} OID")
        remote_url = f"https://github.com/{repository.full_name}.git"
        return await self._apush_oid_with_app_auth_to_url(
            checkout_path,
            destination_url=remote_url,
            token=token,
            tip_oid=tip_oid,
            branch=branch,
            expected_old_oid=expected_old_oid,
            _deadline=deadline,
        )

    async def adelete_ref_with_app_auth(
        self,
        checkout_path: str,
        *,
        repository: GitHubRepositoryBinding,
        token: str | None,
        branch: str,
        expected_old_oid: str,
        authority_deadline: float | None = None,
    ) -> str:
        """Delete one App-bound remote head under an exact expected-old lease."""
        loop = asyncio.get_running_loop()
        maximum_deadline = loop.time() + APP_AUTH_PUSH_TIMEOUT_SECONDS
        deadline = maximum_deadline if authority_deadline is None else min(
            float(authority_deadline), maximum_deadline
        )
        try:
            self._remaining_app_push_budget(deadline)
        except (asyncio.TimeoutError, TypeError, ValueError, OverflowError) as exc:
            raise GitError("authenticated Git delete authority deadline expired") from exc
        if token is not None and (not isinstance(token, str) or not token):
            raise GitError("invalid GitHub App credential")
        branch = _validate_ref(branch)
        if not isinstance(expected_old_oid, str) or _OID_RE.fullmatch(expected_old_oid) is None:
            raise GitError("invalid expected target OID")
        await self._apush_oid_with_app_auth_to_url(
            checkout_path,
            destination_url=f"https://github.com/{repository.full_name}.git",
            token=token,
            tip_oid=None,
            branch=branch,
            expected_old_oid=expected_old_oid,
            _deadline=deadline,
        )
        return expected_old_oid

    async def adelete_local_ref_exact(
        self, checkout_path: str, *, ref: str, expected_old_oid: str
    ) -> None:
        """Delete a daemon-owned local head only when its tip is unchanged."""
        ref = _validate_ref(ref, field="ref")
        if not ref.startswith("refs/heads/"):
            raise GitError("cleanup ref must be a complete head ref")
        if _OID_RE.fullmatch(expected_old_oid) is None:
            raise GitError("invalid expected target OID")
        await self._arun(["update-ref", "-d", ref, expected_old_oid], cwd=checkout_path)

    async def afetch_exact_oid_with_app_auth(
        self,
        destination_git_dir: str,
        *,
        repository: GitHubRepositoryBinding,
        token: str | None,
        oid: str,
        destination_ref: str,
    ) -> str:
        """Fetch one exact App-bound OID, then import it credential-free."""
        return await self._afetch_exact_oid_with_app_auth_to_url(
            destination_git_dir,
            destination_url=f"https://github.com/{repository.full_name}.git",
            token=token,
            oid=oid,
            destination_ref=destination_ref,
        )

    async def _afetch_exact_oid_with_app_auth_to_url(
        self,
        destination_git_dir: str,
        *,
        destination_url: str,
        token: str | None,
        oid: str,
        destination_ref: str,
        timeout_seconds: float | None = None,
    ) -> str:
        """Fetch an exact commit in isolation, then import only that OID locally."""
        if _OID_RE.fullmatch(oid) is None:
            raise GitError("invalid exact fetch OID")
        if not destination_ref.startswith("refs/aq/"):
            raise GitError("exact fetch destination must be daemon recovery namespace")
        destination_ref = "refs/" + _validate_ref(destination_ref.removeprefix("refs/"))
        destination = Path(destination_git_dir).resolve(strict=True)
        if not (
            destination_url.startswith("https://github.com/")
            or destination_url.startswith("file://")
        ):
            raise GitError("invalid authenticated Git source")
        fetch_timeout = timeout_seconds if timeout_seconds is not None else self._GIT_TIMEOUT
        if not math.isfinite(fetch_timeout) or fetch_timeout <= 0:
            raise GitError("invalid authenticated Git fetch deadline")
        deadline = asyncio.get_running_loop().time() + fetch_timeout
        with tempfile.TemporaryDirectory(prefix="aq-app-fetch-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            home = root / "home"
            home.mkdir(mode=0o700)
            imported = root / "repository.git"
            await self._run_isolated_import_git(
                ["init", "--bare", "--template=", str(imported)],
                home=home, deadline=deadline,
            )
            await self._arun_authenticated_git(
                [f"--git-dir={imported}", "fetch", "--no-tags", "--force",
                 destination_url, f"{oid}:refs/aq/exact"],
                home=home,
                repository_url=destination_url,
                token=token,
                deadline=deadline,
                budget_seconds=fetch_timeout,
            )
            verified = await self._run_isolated_import_git(
                [f"--git-dir={imported}", "rev-parse", "refs/aq/exact^{commit}"],
                home=home, deadline=deadline,
            )
            if verified.decode("ascii", errors="replace") != oid:
                raise GitError("authenticated exact Git fetch returned another object")
            await self._run_isolated_import_git(
                ["-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
                 f"--git-dir={destination}", "fetch", "--no-tags", "--force",
                 str(imported), f"{oid}:{destination_ref}"],
                home=home, deadline=deadline,
            )
            local = await self._run_isolated_import_git(
                [f"--git-dir={destination}", "rev-parse", f"{destination_ref}^{{commit}}"],
                home=home, deadline=deadline,
            )
            if local.decode("ascii", errors="replace") != oid:
                raise GitError("authenticated exact Git import returned another object")
        return oid

    @staticmethod
    def _app_git_environment(home: Path) -> dict[str, str]:
        """Return the complete allowlist for the privileged Git process."""
        return {
            "HOME": str(home),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }

    def _existing_git_auth_environment(self) -> dict[str, str]:
        """Isolate Git configuration while retaining the operator's ``gh`` login."""
        return self._SUBPROCESS_ENV | {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }

    async def _arun_authenticated_git(
        self,
        args: list[str],
        *,
        home: Path,
        repository_url: str,
        token: str | None,
        deadline: float,
        budget_seconds: float | None = None,
    ) -> bytes:
        """Run one contained network Git command against a pinned repository URL."""
        if not (
            repository_url.startswith("https://github.com/")
            or repository_url.startswith("file://")  # private local test seam
        ):
            raise GitError("invalid authenticated Git source")
        if token is not None and (not isinstance(token, str) or not token):
            raise GitError("invalid GitHub App credential")
        uses_existing_auth = token is None
        loop = asyncio.get_running_loop()
        run_started = loop.time()
        initial_remaining = max(0.0, deadline - run_started)
        configured_budget = budget_seconds if budget_seconds is not None else initial_remaining
        token_buffer = bytearray(token.encode("utf-8")) if token is not None else bytearray()
        with _zeroized_credential(token_buffer):
            topology = (
                None
                if uses_existing_auth
                else await self._app_git_credential_topology(home=home, deadline=deadline)
            )
            authority = "https://x-access-token@github.com"
            broker_channel = request_channel = None
            broker_task: asyncio.Task[bool] | None = None
            process: asyncio.subprocess.Process | None = None
            stderr = b""
            broker_timeout: float | None = None
            broker_budget: float | None = None
            phase = "budget_preflight"
            operation_timeout = asyncio.timeout_at(deadline)
            try:
                request_fd: int | None = None
                if not uses_existing_auth:
                    self._remaining_app_push_budget(deadline)
                    broker_channel, request_channel = make_request_channel()
                    request_fd = request_channel.fileno()
                environment = (
                    self._existing_git_auth_environment()
                    if uses_existing_auth
                    else self._app_git_environment(home)
                )
                if request_fd is not None:
                    environment.update(
                        {
                            "GIT_ASKPASS": str(Path(answer_prompt.__code__.co_filename)),
                            "GIT_ASKPASS_REQUIRE": "force",
                            "AQ_GIT_APP_REQUEST_FD": str(request_fd),
                            "AQ_GIT_APP_USERNAME": "x-access-token",
                            "AQ_GIT_APP_AUTHORITY": authority,
                            "AQ_GIT_APP_REPOSITORY": repository_url,
                        }
                    )
                command = [
                    "-c", "core.hooksPath=/dev/null",
                    "-c", "credential.helper=",
                    "-c", "http.proxy=",
                    "-c", "https.proxy=",
                    "-c", "http.followRedirects=false",
                    "-c", "protocol.allow=never",
                    "-c", "protocol.https.allow=always",
                    "-c", "protocol.ext.allow=never",
                ]
                if uses_existing_auth:
                    command.extend(["-c", "credential.helper=!gh auth git-credential"])
                if repository_url.startswith("file://"):
                    command.extend(["-c", "protocol.file.allow=always"])
                # A username without a password makes Git request the broker
                # credential before a public repository can answer anonymously.
                # The token itself is never placed in a URL or argument.
                git_url = (
                    repository_url.replace("https://", "https://x-access-token@", 1)
                    if not uses_existing_auth and repository_url.startswith("https://")
                    else repository_url
                )
                command.extend(git_url if arg == repository_url else arg for arg in args)
                async with operation_timeout:
                    phase = "subprocess_spawn"
                    process = await asyncio.create_subprocess_exec(
                        self._APP_GIT_EXECUTABLE,
                        *command,
                        cwd=str(home),
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=environment,
                        pass_fds=(request_fd,) if request_fd is not None else (),
                        start_new_session=True,
                    )
                    if request_channel is not None:
                        request_channel.close()
                        request_channel = None
                    if broker_channel is not None and topology is not None:
                        phase = "credential_broker_start"
                        broker_budget = self._remaining_app_push_budget(deadline)
                        broker_timeout = min(
                            broker_budget, self._APP_CREDENTIAL_BROKER_TIMEOUT
                        )
                        broker_task = asyncio.create_task(
                            serve_one_credential(
                                broker_channel,
                                token_buffer,
                                git_pid=process.pid,
                                topology=topology,
                                authority=authority,
                                repository=repository_url,
                                prompt=f"Password for '{authority}': ",
                                timeout=broker_timeout,
                            )
                        )
                        broker_channel = None
                    phase = "git_communicate"
                    output, stderr = await process.communicate()
                    phase = "process_cleanup"
                    await self._kill_app_git_group(process)
                    phase = "credential_broker_settle"
                    served = (
                        await self._settle_app_credential_broker(broker_task)
                        if broker_task is not None
                        else True
                    )
                    broker_task = None
            except BaseException as exc:
                failure_time = loop.time()
                broker_state = "not_started"
                if broker_task is not None:
                    if broker_task.done():
                        try:
                            broker_state = "served" if broker_task.result() else "not_served"
                        except (asyncio.CancelledError, Exception):
                            broker_state = "failed"
                    else:
                        broker_state = "pending"
                if process is not None:
                    await self._kill_app_git_group(process)
                if broker_task is not None:
                    await self._settle_app_credential_broker(broker_task)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                detail = _safe_authenticated_git_detail(str(exc), token)
                if isinstance(exc, asyncio.TimeoutError):
                    timeout_detail = (
                        f"phase={phase}, configured_budget={configured_budget:.1f}s, "
                        f"budget_at_run_start={initial_remaining:.1f}s, "
                        f"elapsed_in_run={failure_time - run_started:.1f}s, "
                        f"remaining_budget={max(0.0, deadline - failure_time):.1f}s, "
                        f"outer_deadline_expired={operation_timeout.expired()}, "
                        f"broker_state={broker_state}"
                    )
                    if broker_timeout is not None:
                        timeout_detail += f", broker_timeout={broker_timeout:.1f}s"
                    detail = f"{timeout_detail}; {detail}" if detail else timeout_detail
                suffix = f": {detail}" if detail else ""
                if stderr:
                    tail = _safe_authenticated_git_detail(
                        stderr.decode("utf-8", errors="replace"), token
                    )
                    if tail:
                        suffix += f"; stderr tail: {tail}"
                raise GitError(
                    f"authenticated Git acquisition failed: exception "
                    f"{type(exc).__name__}{suffix}"
                ) from None
            finally:
                if request_channel is not None:
                    request_channel.close()
                if broker_channel is not None:
                    broker_channel.close()
            if process.returncode != 0 or (repository_url.startswith("https://") and not served):
                reasons = []
                if process.returncode != 0:
                    reasons.append(f"git exited with returncode {process.returncode}")
                if repository_url.startswith("https://") and not served:
                    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                    reasons.append(
                        "credential broker did not serve token "
                        f"(timeout={broker_timeout:.1f}s, "
                        f"budget_at_start={broker_budget:.1f}s, "
                        f"remaining_push_budget={remaining:.1f}s)"
                    )
                tail = _safe_authenticated_git_detail(
                    stderr.decode("utf-8", errors="replace"), token
                )
                if tail:
                    reasons.append(f"stderr tail: {tail}")
                raise GitError("authenticated Git acquisition failed: " + "; ".join(reasons))
            return output

    @staticmethod
    async def _kill_app_git_group(process: asyncio.subprocess.Process) -> None:
        """Terminate and reap the isolated privileged process group."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + APP_AUTH_PUSH_CLEANUP_MARGIN_SECONDS
        try:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(
                        asyncio.shield(process.wait()),
                        timeout=min(0.25, max(0.0, deadline - loop.time())),
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            # The leader can exit before a transport descendant.  Address the
            # original process-group id once more even after the leader is reaped.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise GitError("authenticated Git cleanup exceeded its safety margin")
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise GitError("authenticated Git cleanup exceeded its safety margin") from exc

        # ``process.wait()`` only reaps the group leader. A descendant can
        # remain briefly visible (usually as an orphaned zombie) after the
        # group-wide SIGKILL, so do not return privileged-operation success
        # until the kernel reports that the whole isolated group is gone.
        while True:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise GitError("authenticated Git cleanup exceeded its safety margin")
            await asyncio.sleep(min(0.01, remaining))

    @staticmethod
    async def _settle_app_credential_broker(task: asyncio.Task[bool]) -> bool:
        """Collect a completed broker or cancel it without waiting for channel EOF."""
        try:
            await asyncio.sleep(0)
            if not task.done():
                task.cancel()
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                if not task.done():
                    task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                raise
            return False
        except Exception:
            return False

    async def _run_isolated_import_git(
        self,
        args: list[str],
        *,
        home: Path,
        deadline: float | None = None,
    ) -> bytes:
        """Run a credential-free Git import/verification command safely."""
        process: asyncio.subprocess.Process | None = None
        try:
            timeout = (
                asyncio.timeout(self._GIT_TIMEOUT)
                if deadline is None
                else asyncio.timeout_at(deadline)
            )
            async with timeout:
                if deadline is not None:
                    self._remaining_app_push_budget(deadline)
                process = await asyncio.create_subprocess_exec(
                    "/usr/bin/git",
                    *args,
                    cwd=str(home),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=self._app_git_environment(home),
                    start_new_session=True,
                )
                stdout, _ = await process.communicate()
        except BaseException as exc:
            if process is not None:
                await self._kill_app_git_group(process)
            if isinstance(exc, (FileNotFoundError, asyncio.TimeoutError)):
                raise GitError("authenticated Git push preparation failed") from exc
            raise
        if process.returncode != 0:
            raise GitError("authenticated Git push preparation failed")
        return stdout.strip()

    @staticmethod
    def _remaining_app_push_budget(deadline: float) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return remaining

    async def _app_git_credential_topology(
        self, *, home: Path, deadline: float | None = None
    ) -> GitCredentialTopology:
        """Pin the supported root-owned Git transport and packaged askpass."""
        try:
            raw_exec_path = await self._run_isolated_import_git(
                ["--exec-path"], home=home, deadline=deadline
            )
            exec_path = Path(raw_exec_path.decode("ascii"))
            topology = pin_git_credential_topology(
                exec_path=exec_path,
                askpass_path=Path(answer_prompt.__code__.co_filename),
            )
            if deadline is not None:
                self._remaining_app_push_budget(deadline)
            return topology
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise GitError("authenticated Git credential topology is unavailable") from exc

    async def _apush_oid_with_app_auth_to_url(
        self,
        checkout_path: str,
        *,
        destination_url: str,
        token: str | None,
        tip_oid: str | None,
        branch: str,
        expected_old_oid: str,
        _deadline: float | None = None,
    ) -> str:
        """Stage an exact graph, then push it without consulting worker Git state.

        ``destination_url`` is private so production callers cannot choose a
        transport.  Its file-URL support exists only for credential-free local
        containment tests; :meth:`apush_oid_with_app_auth` always constructs a
        literal validated GitHub.com URL from the frozen repository binding.
        """
        if _deadline is None:
            _deadline = asyncio.get_running_loop().time() + APP_AUTH_PUSH_TIMEOUT_SECONDS
        if token is not None and (not isinstance(token, str) or not token):
            raise GitError("invalid GitHub App credential")
        branch = _validate_ref(branch)
        for label, oid in (("expected target", expected_old_oid),):
            if not isinstance(oid, str) or _OID_RE.fullmatch(oid) is None:
                raise GitError(f"invalid {label} OID")
        if tip_oid is not None and (
            not isinstance(tip_oid, str) or _OID_RE.fullmatch(tip_oid) is None
        ):
            raise GitError("invalid tip OID")
        if (
            not (
                destination_url.startswith("https://github.com/")
                or destination_url.startswith("file://")
            )
            or "\n" in destination_url
            or "\x00" in destination_url
        ):
            raise GitError("invalid authenticated Git destination")

        checkout = Path(checkout_path).resolve(strict=True)
        uses_existing_auth = token is None
        token_buffer = bytearray(token.encode("utf-8")) if token is not None else bytearray()
        with (
            _zeroized_credential(token_buffer),
            tempfile.TemporaryDirectory(prefix="aq-app-push-") as temporary,
        ):
            token = ""
            root = Path(temporary)
            root.chmod(0o700)
            home = root / "home"
            home.mkdir(mode=0o700)
            repository = root / "repository.git"
            await self._run_isolated_import_git(
                ["init", "--bare", "--template=", str(repository)],
                home=home,
                deadline=_deadline,
            )
            if tip_oid is not None:
                await self._run_isolated_import_git(
                    [
                        "-c",
                        "protocol.allow=never",
                        "-c",
                        "protocol.file.allow=always",
                        f"--git-dir={repository}",
                        "fetch",
                        "--no-tags",
                        "--force",
                        str(checkout),
                        f"{tip_oid}:refs/aq/imported",
                    ],
                    home=home,
                    deadline=_deadline,
                )
                imported = await self._run_isolated_import_git(
                    [f"--git-dir={repository}", "rev-parse", "refs/aq/imported^{commit}"],
                    home=home,
                    deadline=_deadline,
                )
                if imported.decode("ascii", errors="replace") != tip_oid:
                    raise GitError("authenticated Git push preparation failed")

            topology = (
                None
                if uses_existing_auth
                else await self._app_git_credential_topology(home=home, deadline=_deadline)
            )

            authority = "https://x-access-token@github.com"
            prompt = f"Password for '{authority}': "
            broker_channel = None
            request_channel = None
            broker_task: asyncio.Task[bool] | None = None
            process: asyncio.subprocess.Process | None = None
            request_fd: int | None = None
            if not uses_existing_auth:
                try:
                    self._remaining_app_push_budget(_deadline)
                    broker_channel, request_channel = make_request_channel()
                    self._remaining_app_push_budget(_deadline)
                except OSError as exc:
                    zeroize(token_buffer)
                    raise GitError("authenticated Git credential broker is unavailable") from exc
                request_fd = request_channel.fileno()
            environment = (
                self._existing_git_auth_environment()
                if uses_existing_auth
                else self._app_git_environment(home)
            )
            if request_fd is not None:
                environment.update(
                    {
                        "GIT_ASKPASS": str(Path(answer_prompt.__code__.co_filename)),
                        "GIT_ASKPASS_REQUIRE": "force",
                        "AQ_GIT_APP_REQUEST_FD": str(request_fd),
                        "AQ_GIT_APP_USERNAME": "x-access-token",
                        "AQ_GIT_APP_AUTHORITY": authority,
                        "AQ_GIT_APP_REPOSITORY": destination_url,
                    }
                )
            arguments = [
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "credential.helper=",
                "-c",
                "http.proxy=",
                "-c",
                "https.proxy=",
                "-c",
                "http.followRedirects=false",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                "-c",
                "protocol.ext.allow=never",
            ]
            if uses_existing_auth:
                arguments.extend(["-c", "credential.helper=!gh auth git-credential"])
            if destination_url.startswith("file://"):
                arguments.extend(["-c", "protocol.file.allow=always"])
            arguments.extend(
                [
                    f"--git-dir={repository}",
                    "push",
                    "--no-verify",
                    destination_url,
                    f"--force-with-lease=refs/heads/{branch}:{expected_old_oid}",
                    (
                        f"{tip_oid}:refs/heads/{branch}"
                        if tip_oid is not None
                        else f":refs/heads/{branch}"
                    ),
                ]
            )
            try:
                self._remaining_app_push_budget(_deadline)
                async with asyncio.timeout_at(_deadline):
                    process = await asyncio.create_subprocess_exec(
                        self._APP_GIT_EXECUTABLE,
                        *arguments,
                        cwd=str(home),
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        env=environment,
                        pass_fds=(request_fd,) if request_fd is not None else (),
                        start_new_session=True,
                    )
                    if request_channel is not None:
                        request_channel.close()
                        request_channel = None
                    if broker_channel is not None and topology is not None:
                        broker_task = asyncio.create_task(
                            serve_one_credential(
                                broker_channel,
                                token_buffer,
                                git_pid=process.pid,
                                topology=topology,
                                authority=authority,
                                repository=destination_url,
                                prompt=prompt,
                                timeout=min(
                                    self._remaining_app_push_budget(_deadline),
                                    self._APP_CREDENTIAL_BROKER_TIMEOUT,
                                ),
                            )
                        )
                        broker_channel = None
                    await process.wait()
                    await self._kill_app_git_group(process)
                    if broker_task is not None:
                        broker_served = await self._settle_app_credential_broker(broker_task)
                        broker_task = None
                    else:
                        broker_served = True
            except BaseException as exc:
                if process is not None:
                    await self._kill_app_git_group(process)
                if broker_task is not None:
                    await self._settle_app_credential_broker(broker_task)
                    broker_task = None
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise GitError("authenticated Git push failed") from exc
            finally:
                if request_channel is not None:
                    request_channel.close()
                if broker_channel is not None:
                    broker_channel.close()
            if process.returncode != 0:
                raise GitError("authenticated Git push failed")
            if destination_url.startswith("https://") and not broker_served:
                raise GitError("authenticated Git push failed")
        return tip_oid or expected_old_oid

    async def alist_prs(
        self,
        checkout_path: str,
        *,
        state: str = "open",
        base: str | None = None,
        head: str | None = None,
        limit: int = 30,
        repository: GitHubRepositoryBinding | None = None,
    ) -> list[dict] | None:
        """Repository-bound PR list, or None when GitHub could not answer."""
        try:
            return await self._github_client(repository).list_pull_requests(
                state=state, base=base, head=head, limit=limit
            )
        except (GitError, GitHubAccessError, ValueError):
            return None

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
            short = name[len(prefix) :]
            if short and short != "HEAD":
                names.append(short)
        return names

    async def aget_pr_identity(
        self, checkout_path: str, pr_url: str, *,
        repository: GitHubRepositoryBinding | None = None,
    ) -> PullRequestIdentity:
        """Resolve one immutable PR snapshot within an authorized repository."""
        try:
            number = GitHubAccess.validate_pr_url(repository, pr_url)
            data = await self._github_client(repository).pull_request(pr_url)
            resource_number = data["number"]
            base = data["base"]
            head = data["head"]
            repo_name = base["repo"]["full_name"]
            base_ref = base["ref"]
            head_ref = head["ref"]
            base_oid = base["sha"].lower()
            head_oid = head["sha"].lower()
            changed_files = _pr_changed_file_count(data)
        except (GitHubAccessError, KeyError, TypeError, ValueError, AttributeError) as exc:
            raise GitError(f"could not resolve complete PR identity: {exc}") from exc
        if (
            resource_number != number
            or repo_name != repository.full_name
            or not isinstance(base_ref, str)
            or not isinstance(head_ref, str)
            or not _OID_RE.fullmatch(base_oid)
            or not _OID_RE.fullmatch(head_oid)
        ):
            raise GitError("could not resolve complete PR identity")
        return PullRequestIdentity(
            repository=repo_name, number=number, base_ref=base_ref, base_oid=base_oid,
            head_ref=head_ref, head_oid=head_oid, changed_files=changed_files,
        )

    def _pr_diff_cache_lock(self, cache: str) -> asyncio.Lock:
        """The lock serializing fetches into the delivery-diff cache at ``cache``."""
        lock = self._pr_diff_cache_locks.get(cache)
        if lock is None:
            lock = self._pr_diff_cache_locks[cache] = asyncio.Lock()
        return lock

    async def _apr_delivery_diff(
        self, cache_root: str, identity: PullRequestIdentity, *,
        repository: GitHubRepositoryBinding,
    ) -> str:
        """NUL-delimited paths PR ``identity`` changes, derived from its pinned OIDs.

        GitHub's PR-files listing is addressed by PR *number*, so a head
        force-pushed A -> B -> A while a paginated listing runs is inspected
        as B's diff yet merged as A: both identity reads see A, and when B
        changes as many files as A the pinned count agrees too.  This
        derives the diff from the OIDs themselves instead.  ``head_oid`` and
        ``base_oid`` are fetched *by OID* — content-addressed, so the fetch
        either yields exactly those commits or fails — into a daemon-owned
        bare repository under ``cache_root`` (one per host and repository,
        created on first use), and the diff is
        ``git diff-tree --no-renames --name-only`` from
        ``merge-base(base_oid, head_oid)`` to ``head_oid`` — the same
        merge-base diff GitHub lists, with no 3000-entry cap and no reliance
        on the changed-file count.  ``--no-renames`` reports a rename as a
        deletion plus an addition, so a reserved path is listed under its
        reserved name whichever way it moved.

        ``cache_root`` is the daemon data directory, not a checkout, which
        is why the PR cannot simply be fetched into ``origin``. The fetch
        uses the authorized repository and selected credential mode. A fetch
        error is an unknown diff, never a clean one.
        """
        owner, repo = identity.repository.split("/")
        cache = str(
            Path(cache_root) / self.PR_DIFF_CACHE_DIRNAME / identity.host / owner / f"{repo}.git"
        )
        try:
            async with self._pr_diff_cache_lock(cache):
                await self._arun(["init", "--bare", "--quiet", cache])
                await self._afetch_pr_diff_commits(cache, identity, repository=repository)
                # A fetch that returned without delivering the exact commits
                # (not our ref, a stale cache, an interrupted pack) cannot be
                # diffed; prove both objects are present before trusting it.
                for oid in (identity.head_oid, identity.base_oid):
                    await self._arun(
                        ["rev-parse", "--verify", "--quiet", f"{oid}^{{commit}}"], cwd=cache
                    )
                merge_base = await self._arun(
                    ["merge-base", identity.base_oid, identity.head_oid], cwd=cache
                )
                if not _OID_RE.fullmatch(merge_base):
                    raise GitError(f"git merge-base returned no single commit: {merge_base!r}")
                return await self._arun(
                    [
                        "diff-tree",
                        "-r",
                        "--no-renames",
                        "--name-only",
                        "-z",
                        merge_base,
                        identity.head_oid,
                    ],
                    cwd=cache,
                )
        except GitError as exc:
            raise GitError(f"could not inspect PR delivery diff: {exc}") from exc

    async def _afetch_pr_diff_commits(
        self, cache: str, identity: PullRequestIdentity, *,
        repository: GitHubRepositoryBinding,
    ) -> None:
        """Fetch pinned PR commits with the authorized repository credentials."""
        if repository.full_name != identity.repository:
            raise GitError("PR diff repository did not match authorized repository")
        if self.github_access is None:
            raise GitError("GitHub access service is not configured")
        if self.github_access.auth.mode is GitHubCredentialMode.APP:
            binding = await self.github_access.bind_repository(identity.clone_url)
            if binding != repository:
                raise GitError("PR diff repository did not match authorized repository")
            fetch_deadline = asyncio.get_running_loop().time() + self._PR_DIFF_FETCH_TIMEOUT
            for label, oid in (("head", identity.head_oid), ("base", identity.base_oid)):
                token = await self._atoken_for_repository(binding)
                remaining = fetch_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise GitError("PR delivery diff fetch deadline expired")
                await self._afetch_exact_oid_with_app_auth_to_url(
                    cache,
                    destination_url=f"https://github.com/{binding.full_name}.git",
                    token=token,
                    oid=oid,
                    destination_ref=f"refs/aq/pr/{label}",
                    timeout_seconds=remaining,
                )
            return
        await self._arun(
            ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
             "fetch", "--quiet", "--no-tags", "--filter=blob:none", identity.clone_url,
             identity.head_oid, identity.base_oid],
            cwd=cache,
            timeout=self._PR_DIFF_FETCH_TIMEOUT,
        )

    async def avalidate_pr_for_merge(
        self, checkout_path: str, pr_url: str, *,
        repository: GitHubRepositoryBinding | None = None,
    ) -> PullRequestIdentity:
        """Fail closed unless a PR identity and its reserved-path diff are stable.

        The identity — repository, number, branch names, OIDs and GitHub's
        changed-file count — is read in one snapshot; the delivery diff is
        then derived from the pinned OIDs (:meth:`_apr_delivery_diff`), so
        the diff inspected is by construction the diff of the head that will
        be merged, whatever the PR's mutable head does meanwhile.  Re-reading
        the identity afterwards additionally proves the pinned head, target
        branch and count are still the PR's, so the merge that follows (with
        ``--match-head-commit``) is a merge of what was inspected.  The base
        branch's tip is not compared: it advances with every concurrent
        delivery and does not change a merge-base diff (see
        :attr:`PullRequestIdentity.pin`).

        ``checkout_path`` is where ``gh`` runs and where the delivery-diff
        cache lives (``pr_merge`` passes the daemon data dir); it need not be
        a checkout.
        """
        identity = await self.aget_pr_identity(checkout_path, pr_url, repository=repository)
        changed = await self._apr_delivery_diff(checkout_path, identity, repository=repository)
        reserved = self._daemon_bookkeeping_paths(changed)
        if reserved:
            raise GitError(
                "PR changes reserved daemon bookkeeping paths: " + ", ".join(sorted(reserved))
            )
        if (await self.aget_pr_identity(checkout_path, pr_url, repository=repository)).pin != identity.pin:
            raise GitError("PR identity changed while its delivery diff was inspected")
        return identity

    async def apr_base_ref(
        self, checkout_path: str, pr_url: str, *,
        repository: GitHubRepositoryBinding | None = None,
    ) -> str | None:
        """Return a PR's validated base branch, or None when unreadable."""
        try:
            data = await self._github_client(repository).pull_request(pr_url)
            base = _validate_ref(data["base"]["ref"], field="base branch")
        except (GitError, GitHubAccessError, KeyError, TypeError, ValueError):
            return None
        return base

    async def apr_check_rollup(
        self, checkout_path: str, pr_url: str, *,
        repository: GitHubRepositoryBinding | None = None,
    ) -> list[dict] | None:
        """Return the PR head check rollup; None remains an unknown verdict."""
        try:
            return await self._github_client(repository).check_rollup(pr_url)
        except (GitError, GitHubAccessError, ValueError):
            return None

    # -- branch CI reads (ci-main-sentinel) ---------------------------------

    _FAILED_TEST_LINE = re.compile(r"(?:^|\s)(?:FAILED|ERROR) (tests/[^\s]+)")

    @staticmethod
    def github_repo_slug(repo_url: str) -> str | None:
        """``owner/repo`` from a GitHub clone URL, or ``None`` if it is not one."""
        text = (repo_url or "").strip()
        match = re.match(
            r"^(?:https?://github\.com/|git@github\.com:|ssh://git@github\.com/)"
            r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
            text,
        )
        if match is None:
            return None
        return f"{match.group(1)}/{match.group(2)}"

    async def acommit_head_sha(
        self, slug: str, ref: str, *, cwd: str | None = None,
        repository: GitHubRepositoryBinding | None = None,
    ) -> str | None:
        """The commit sha a branch resolves to on the authorized repository."""
        try:
            client = self._github_client(repository)
            if slug != repository.full_name:
                raise GitError("CI repository did not match authorized repository")
            return await client.commit_head(ref)
        except (GitError, GitHubAccessError, ValueError):
            return None

    async def acommit_check_runs(
        self, slug: str, sha: str, *, cwd: str | None = None,
        repository: GitHubRepositoryBinding | None = None,
    ) -> list[dict] | None:
        try:
            client = self._github_client(repository)
            if slug != repository.full_name:
                raise GitError("CI repository did not match authorized repository")
            return await client.commit_check_runs(sha)
        except (GitError, GitHubAccessError, ValueError):
            return None

    async def ajob_failed_tests(
        self, slug: str, job_id: int | str, *, cwd: str | None = None,
        repository: GitHubRepositoryBinding | None = None,
    ) -> list[str] | None:
        try:
            client = self._github_client(repository)
            if slug != repository.full_name:
                raise GitError("CI repository did not match authorized repository")
            if isinstance(job_id, bool) or not str(job_id).isdigit():
                return None
            log = await client.job_log(int(job_id))
        except (GitError, GitHubAccessError, ValueError):
            return None
        found: set[str] = set()
        for line in log.splitlines():
            match = self._FAILED_TEST_LINE.search(line)
            if match:
                found.add(match.group(1).rstrip(","))
        return sorted(found)

    async def apr_behind_base(
        self, checkout_path: str, pr_url: str, *,
        repository: GitHubRepositoryBinding | None = None,
    ) -> tuple[str, int] | None:
        """Return (base branch, behind count) for the authorized PR head."""
        try:
            client = self._github_client(repository)
            data = await client.pull_request(pr_url)
            base = _validate_ref(data["base"]["ref"], field="base branch")
            head = data["head"]["sha"]
            comparison = await client.compare(base, head)
            behind = comparison.get("behind_by")
            if isinstance(behind, bool) or not isinstance(behind, int) or behind < 0:
                return None
            return base, behind
        except (GitError, GitHubAccessError, KeyError, TypeError, ValueError):
            return None

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
        addition, deletion, modification, or rename made by task commits is
        caught. Rename detection is disabled (``--no-renames``, overriding
        any ``diff.renames`` setting) because git would otherwise report
        ``git mv .aq/claim.json moved.json`` as the single path
        ``moved.json`` and hide that a daemon-owned file was deleted; with
        detection off both the source and the destination of a rename are
        listed. Unlike preview helpers, Git failures propagate: callers use
        this as a fail-closed delivery gate before merge, push, or PR
        acceptance.
        """
        base_ref = _validate_rev(base_ref, field="delivery base")
        tip_ref = _validate_rev(tip_ref, field="delivery tip")
        merge_base = await self._arun(["merge-base", base_ref, tip_ref], cwd=checkout_path)
        changed = await self._arun(
            ["diff", "--no-renames", "--name-only", "-z", merge_base, tip_ref, "--"],
            cwd=checkout_path,
        )
        return sorted(self._daemon_bookkeeping_paths(changed))

    async def areserved_paths_in_index(self, checkout_path: str) -> list[str]:
        """Return daemon-owned paths currently staged in *checkout_path*.

        Git errors propagate because task-close auto-remediation uses this as
        a fail-closed guard immediately after staging.
        """
        cached = await self._arun(
            ["diff", "--cached", "--name-only", "-z", "--"], cwd=checkout_path
        )
        return sorted(self._daemon_bookkeeping_paths(cached))

    async def areserved_paths_in_tree(self, checkout_path: str, rev: str) -> list[str]:
        """Return daemon-owned paths tracked anywhere in *rev*'s tree.

        The root-delivery counterpart of :meth:`areserved_paths_in_diff`:
        when a branch is published to a remote that holds no base for it,
        every tracked path is new content from origin's point of view, so a
        reserved path that a normal delivery would excuse as "unchanged on
        the base" is here being published for the first time. Git failures
        propagate, as for the diff gate.
        """
        rev = _validate_rev(rev, field="delivery tip")
        listed = await self._arun(
            ["ls-tree", "-r", "--name-only", "-z", rev, "--"], cwd=checkout_path
        )
        return sorted(self._daemon_bookkeeping_paths(listed))

    async def aget_current_branch(self, checkout_path: str, *, strict: bool = False) -> str | None:
        """Return the checked-out branch, or ``None`` on failure in strict mode."""
        try:
            return await self._arun(["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout_path)
        except GitError:
            return None if strict else ""

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
        head_ref: str | None = None,
        include_workspace_head: bool = True,
        repository: GitHubRepositoryBinding | None = None,
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
        ``head_ref`` supplies the exact local or remote-tracking ref whose tip
        must match, while ``branch_name`` remains the GitHub branch identity.
        Best-effort throughout: any gh/git failure returns ``None``.
        """
        if include_workspace_head:
            url = await self._open_pr_url_by_head_name(
                checkout_path, branch_name, repository=repository
            )
            if url:
                return url

        refs = [_validate_rev(head_ref, field="PR head") if head_ref else branch_name]
        if include_workspace_head:
            refs.append("HEAD")
        tips = {sha for ref in refs if (sha := await self.arev_parse(checkout_path, ref))}
        if not tips:
            return None
        return await self._open_pr_url_by_head_commit(
            checkout_path, tips, branch_name, repository=repository
        )

    async def _open_pr_url_by_head_name(
        self,
        checkout_path: str,
        branch_name: str,
        *, repository: GitHubRepositoryBinding | None = None,
    ) -> str | None:
        try:
            prs = await self._github_client(repository).list_pull_requests(
                state="all", head=branch_name, limit=100
            )
        except (GitError, GitHubAccessError, ValueError):
            return None
        return next(
            (pr["url"] for pr in prs if pr.get("state") in {"OPEN", "MERGED"}), None
        )

    async def _open_pr_url_by_head_commit(
        self,
        checkout_path: str,
        tips: set[str],
        branch_name: str,
        *, repository: GitHubRepositoryBinding | None = None,
    ) -> str | None:
        """URL of an open or merged PR whose head commit is one of *tips*."""
        try:
            prs = await self._github_client(repository).list_pull_requests(
                state="all", limit=100, include_head_oid=True
            )
        except (GitError, GitHubAccessError, ValueError):
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
                    "Accepting open PR %s from branch '%s' for '%s' (same head commit %s)",
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
        *,
        strict: bool = False,
    ) -> bool | None:
        """Return reachability, or ``None`` on a probe error in strict mode."""
        try:
            result = await self._arun_subprocess(
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    _validate_ref(ancestor),
                    _validate_rev(descendant),
                ],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except Exception:
            return None if strict else False
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        return None if strict else False

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
            exists = await self.aref_exists(checkout_path, ref)
            if exists is True:
                return True
            if exists is None:
                return None
        return False

    async def aref_exists(self, checkout_path: str, ref: str) -> bool | None:
        """Return whether an exact Git ref exists, or ``None`` on probe failure."""
        try:
            result = await self._arun_subprocess(
                ["git", "show-ref", "--verify", "--quiet", _validate_rev(ref)],
                cwd=checkout_path,
                timeout=self._GIT_TIMEOUT,
            )
        except Exception:
            return None
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        return None

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

    async def aget_default_branch(
        self, checkout_path: str, *, repository_url: str | None = None
    ) -> str:
        if repository_url is not None and not self._uses_existing_ssh(repository_url):
            binding = await self._abind_git_repository(repository_url)
            if binding is not None and self.github_access is not None:
                configured_origin = await self._arun(
                    ["config", "--get", "remote.origin.url"], cwd=checkout_path
                )
                self.github_access.validate_repository_reference(binding, configured_origin)
                token = await self._atoken_for_repository(binding)
                source_url = f"https://github.com/{binding.full_name}.git"
                deadline = asyncio.get_running_loop().time() + self._GIT_TIMEOUT
                with tempfile.TemporaryDirectory(prefix="aq-app-head-") as temporary:
                    home = Path(temporary)
                    home.chmod(0o700)
                    output = await self._arun_authenticated_git(
                        ["ls-remote", "--symref", source_url, "HEAD"],
                        home=home, repository_url=source_url, token=token, deadline=deadline,
                        budget_seconds=self._GIT_TIMEOUT,
                    )
                for line in output.decode("ascii", errors="replace").splitlines():
                    if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                        return _validate_ref(line.removeprefix("ref: refs/heads/").split("\t", 1)[0])
                raise GitError("authenticated default branch discovery failed")
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

    async def acreate_github_repo(
        self,
        name: str,
        *,
        private: bool = True,
        org: str | None = None,
        description: str = "",
    ) -> str:
        if self.github_access is None:
            raise GitError("GitHub access service is not configured")
        return await self.github_access.create_repository(
            name, private=private, org=org, description=description
        )

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
