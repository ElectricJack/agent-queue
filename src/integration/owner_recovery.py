"""Release an integration branch owner whose writer is gone, preserving its work first.

An ``integration_branch_owners`` row in ``attached`` or ``handoff_pending``
fences its branch for one writer.  Several paths end a writer without
releasing that row (spec §1, G1–G7): delegate retirement and
``cancel_preserving`` keep it by design, a failed transfer leaves it
``handoff_pending``, archive/delete forget it, and slot reuse destroys the
proof every exit-time confirmer needs.  The row then pins the branch, the
writer's claim and its workspace lock forever.

:class:`OwnerRecovery` decides from facts that stay checkable after the fact,
never from the task row or its status:

1. **The writer is gone.**  The row's session is stopped in ``state`` and
   ``desired_state`` and the session provider confirms it by a fresh probe
   (a row naming no session has no writer), and no live session names the
   owning task.  Otherwise ``writer_live``.
2. **The branch is safe.**  After ``git fetch origin`` in the repository's
   base checkout, every worktree holding the branch (or the row's own
   checkout, detached) is inspected.  One a live session works in refuses
   with ``checkout_in_use``.  Uncommitted work is snapshotted through a
   temporary index -- the working tree and the live index are never touched
   -- and anything origin does not already carry (a dirty tree, a HEAD or a
   local branch ahead of or diverged from origin and not on the default
   branch) is pushed, never forced, to ``aq/preserved/<owner-row-id>``.  The
   branch is then detached in place, without reset or clean.  A fetch or
   push failure refuses with ``origin_unreachable``.
3. **Release in one transaction.**  A compare-and-swap on
   ``(id, fence_token, handoff_state)`` (``stale_fence`` when it misses)
   releases the row with a fresh fence, unlocks the row's workspace
   (disabling it when dirty, so allocation cannot recycle it) and releases
   the gone writer's claim, then writes the audit row and
   ``integration.owner_recovered``.

Every non-dry run writes one ``integration_owner_recoveries`` row.  Refusals
emit ``integration.owner_recovery_refused``; a refusal repeating the latest
recorded reason within :data:`REFUSAL_THROTTLE_SECONDS` is not recorded again,
and the task hears about each refusal reason once.  A dry run pushes,
detaches and records nothing and lists what it would do in
``evidence["planned"]``.

There is no force: a row the check refuses stays refused, with its reason.

See docs/superpowers/specs/2026-09-21-integration-owner-recovery-design.md §3.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import and_, case, insert, not_, or_, select, update

from src.database.queries.integration_state_queries import session_attached_clause
from src.database.tables import (
    archived_tasks,
    integration_branch_owners,
    integration_owner_recoveries,
    projects,
    repos,
    sessions,
    tasks,
    workspaces,
)
from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitError, GitManager, RemoteRefState
from src.integration.finished_owners import (
    StopConfirmer,
    _live_task_session,
    _project_ids,
    _stop_proof_blocker,
    _stopped_writer,
    stop_confirmer_for,
)
from src.models import TaskStatus

logger = logging.getLogger(__name__)

#: Handoff states that name a writer, and so can strand a branch.
RECOVERABLE_STATES: tuple[str, ...] = ("attached", "handoff_pending")

#: A ``collector`` row's owner is an integration operation, not a writer.
COLLECTOR_ROLE = "collector"

RELEASED = "released"
PRESERVED_AND_RELEASED = "preserved_and_released"
NOT_ELIGIBLE = "not_eligible"

WRITER_LIVE = "writer_live"
CHECKOUT_IN_USE = "checkout_in_use"
ORIGIN_UNREACHABLE = "origin_unreachable"
STALE_FENCE = "stale_fence"
NOT_FOUND = "not_found"
NOT_RECOVERABLE_STATE = "not_recoverable_state"

RECOVERED_EVENT = "integration.owner_recovered"
REFUSED_EVENT = "integration.owner_recovery_refused"

#: Where preserved work lands on origin: ``aq/preserved/<owner-row-id>``.
PRESERVED_PREFIX = "aq/preserved/"

#: A refusal repeating the latest recorded reason within this window is not
#: recorded again (the sweep retries every 300 s).
REFUSAL_THROTTLE_SECONDS = 300.0

#: The sweep leaves a row alone until it has been unchanged this long, so the
#: normal exit-time handoff always gets the first try.
DEFAULT_QUIET_SECONDS = 600.0

#: The daemon's lock-sentinel file, never part of a snapshot or a dirty verdict.
_LOCK_SENTINEL = ".agent-queue-lock"

_IDENTITY = ("Agent Queue", "agent-queue@localhost")

#: Statuses of a task whose writer was still working when it stopped.
_IN_FLIGHT_STATUSES = (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS)

#: ``git_mutex(base_checkout_path)`` -- the orchestrator's per-repository lock.
GitMutex = Callable[[str], Any]


@dataclass(frozen=True)
class RecoveryOutcome:
    """One row's verdict and the evidence it was reached on."""

    owner_row_id: str
    outcome: str
    reason: str | None
    evidence: dict
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _Refusal(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class _Plan:
    """What the branch check found and did (or, in a dry run, would do)."""

    preserved: bool
    workspace_clean: bool


class OwnerRecovery:
    """Prove a branch owner's writer gone and its branch safe, then release it."""

    def __init__(
        self,
        db,
        git,
        git_mutex: GitMutex | None,
        *,
        confirm_stopped: StopConfirmer | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.git = git
        self.git_mutex = git_mutex
        self.confirm_stopped = confirm_stopped
        self.clock = clock

    # -- public ---------------------------------------------------------------

    async def recover(
        self, owner_row_id: str, *, principal: str, dry_run: bool = False
    ) -> RecoveryOutcome:
        """Run the check on one owner row and release it when it passes."""
        row = await self._load(owner_row_id)
        if row is None:
            return RecoveryOutcome(
                owner_row_id,
                NOT_ELIGIBLE,
                NOT_FOUND,
                {"detail": f"no integration branch owner row {owner_row_id}"},
                dry_run,
            )
        evidence: dict[str, Any] = {
            "handoff_state": row["handoff_state"],
            "fence_token": int(row["fence_token"]),
            "session_id": row["session_id"],
            "workspace_id": row["workspace_id"],
        }
        try:
            if row["handoff_state"] not in RECOVERABLE_STATES or (
                row["owner_role"] == COLLECTOR_ROLE
            ):
                raise _Refusal(
                    NOT_RECOVERABLE_STATE,
                    f"a {row['owner_role']} row in {row['handoff_state']} names no writer to "
                    "recover; only worker and repair rows in "
                    f"{' or '.join(RECOVERABLE_STATES)} are recoverable",
                )
            session = await self._prove_writer_gone(row, evidence)
            plan = await self._secure_branch(row, evidence, dry_run=dry_run)
            outcome = PRESERVED_AND_RELEASED if plan.preserved else RELEASED
            if dry_run:
                return RecoveryOutcome(row["id"], outcome, None, evidence, True)
            return await self._release(row, session, plan, outcome, evidence, principal)
        except _Refusal as refusal:
            evidence["detail"] = refusal.detail
            result = RecoveryOutcome(row["id"], NOT_ELIGIBLE, refusal.reason, evidence, dry_run)
            if not dry_run:
                await self._record_refusal(row, result, principal)
            return result

    async def recover_many(
        self, owner_row_ids: list[str], *, principal: str, dry_run: bool = False
    ) -> list[RecoveryOutcome]:
        """:meth:`recover` each row in turn.

        A row whose check raises (a local Git failure outside the named
        refusals) is logged and left out of the result; the others still run.
        """
        results = []
        for owner_row_id in owner_row_ids:
            try:
                results.append(
                    await self.recover(owner_row_id, principal=principal, dry_run=dry_run)
                )
            except Exception:
                logger.warning("Owner recovery of %s failed", owner_row_id, exc_info=True)
        return results

    async def candidates(
        self, *, quiet_seconds: float = DEFAULT_QUIET_SECONDS, limit: int = 50
    ) -> list[dict]:
        """Recoverable rows whose writer is not live and that have been quiet.

        A row qualifies when it names no session or its session is stopped in
        both ``state`` and ``desired_state``, and it has been unchanged for
        *quiet_seconds*.  Oldest first, at most *limit*.
        """
        owners = integration_branch_owners
        cutoff = self.clock() - quiet_seconds
        async with self.db._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(owners)
                    .select_from(owners.outerjoin(sessions, sessions.c.id == owners.c.session_id))
                    .where(
                        owners.c.handoff_state.in_(RECOVERABLE_STATES),
                        owners.c.owner_role != COLLECTOR_ROLE,
                        owners.c.updated_at <= cutoff,
                        or_(
                            owners.c.session_id.is_(None),
                            and_(sessions.c.id.is_not(None), not_(session_attached_clause())),
                        ),
                    )
                    .order_by(owners.c.updated_at, owners.c.id)
                    .limit(limit)
                )
            ).mappings()
            return [dict(row) for row in rows]

    # -- step 1: the row ------------------------------------------------------

    async def _load(self, owner_row_id: str) -> dict[str, Any] | None:
        async with self.db.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(integration_branch_owners)
                        .where(integration_branch_owners.c.id == owner_row_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row is not None else None

    # -- step 2: the writer is gone -------------------------------------------

    async def _prove_writer_gone(
        self, row: dict[str, Any], evidence: dict[str, Any]
    ) -> dict[str, Any] | None:
        session = None
        async with self.db._engine.connect() as conn:
            if row["session_id"]:
                session, blocker = await _stopped_writer(
                    conn, row, lock=False, require_workspace=False
                )
                if blocker is not None:
                    raise _Refusal(WRITER_LIVE, blocker)
            live = await _live_task_session(conn, row["owner_id"])
        if live is not None:
            evidence["session_id"] = live
            raise _Refusal(WRITER_LIVE, f"session {live} is still live for {row['owner_id']}")
        if session is None:
            evidence["stop_proof"] = {"session_id": None, "detail": "the row names no writer"}
            return None
        # The provider probe runs with no database connection held.
        blocker = await _stop_proof_blocker(session, self.confirm_stopped)
        if blocker is not None:
            raise _Refusal(WRITER_LIVE, blocker)
        evidence["stop_proof"] = {
            "session_id": session["id"],
            "name": session["name"],
            "provider": session["provider"],
            "instance_token": session["instance_token"],
            "state": session["state"],
            "desired_state": session["desired_state"],
            "confirmed_at": self.clock(),
        }
        return session

    # -- step 3: the branch is safe -------------------------------------------

    async def _secure_branch(
        self, row: dict[str, Any], evidence: dict[str, Any], *, dry_run: bool
    ) -> _Plan:
        branch = row["ref"].removeprefix("refs/heads/")
        checkout, default_branch, row_path, repository_url = await self._checkout_for(row)
        evidence.update(
            checkout=checkout,
            origin_sha=None,
            local_sha=None,
            worktree=None,
            preserved_ref=None,
            preserved_sha=None,
        )
        if checkout is None:
            raise _Refusal(
                ORIGIN_UNREACHABLE,
                f"no checkout of repository {row['repository_id']} exists on this host, so "
                "the branch cannot be compared with origin",
            )
        live_dirs = await self._live_work_dirs()
        planned: list[dict[str, Any]] = []
        preserve: list[tuple[str, str | None, str]] = []  # (kind, worktree, sha)
        detach: list[str] = []
        workspace_clean = True
        row_inspected = False
        async with self._mutex(checkout):
            try:
                await self.git.afetch_origin(
                    checkout, repository_url=repository_url, lock_held=True
                )
            except (GitError, GitHubAccessError) as exc:
                raise _Refusal(
                    ORIGIN_UNREACHABLE,
                    f"git fetch origin failed: {exc}",
                ) from exc
            remote = await self.git.als_remote_ref(checkout, branch)
            if remote.state is RemoteRefState.ERROR:
                raise _Refusal(ORIGIN_UNREACHABLE, f"reading origin/{branch}: {remote.error}")
            origin_sha = remote.oid if remote.state is RemoteRefState.PRESENT else None
            local_sha = await self.git.arev_parse(checkout, f"refs/heads/{branch}")
            evidence.update(origin_sha=origin_sha, local_sha=local_sha)

            held_by_worktree = False
            for entry in await self.git.aworktree_list(checkout):
                path = entry["path"]
                holds = entry.get("branch") == branch
                own_detached = (
                    row_path is not None and _same_path(path, row_path) and "branch" not in entry
                )
                if not (holds or own_detached):
                    continue
                if not os.path.isdir(path):
                    evidence.setdefault("missing_worktrees", []).append(path)
                    continue
                if os.path.realpath(path) in live_dirs:
                    if holds:
                        evidence["worktree"] = path
                        raise _Refusal(
                            CHECKOUT_IN_USE,
                            f"{branch} is checked out at {path}, where a live session works",
                        )
                    # The row's old slot, detached and reused by a successor:
                    # the branch is not there, and the work is not the writer's.
                    evidence.setdefault("skipped_worktrees", []).append(path)
                    continue
                held_by_worktree = held_by_worktree or holds
                if evidence["worktree"] is None or holds:
                    evidence["worktree"] = path
                head = entry.get("head") or await self.git.arev_parse(path, "HEAD")
                dirty = await self._dirty(path)
                if row_path is not None and _same_path(path, row_path):
                    workspace_clean, row_inspected = not dirty, True
                if dirty:
                    preserve.append(("snapshot", path, head))
                elif head and not await self._on_origin(checkout, head, origin_sha, default_branch):
                    preserve.append(("commit", path, head))
                if holds:
                    detach.append(path)
            if (
                row_path is not None
                and not row_inspected
                and os.path.isdir(row_path)
                and os.path.realpath(row_path) not in live_dirs
            ):
                # The row's checkout was not inspected above (reused, or on
                # another branch): its cleanliness still decides whether it
                # may be recycled once unlocked.
                with contextlib.suppress(GitError):
                    workspace_clean = not await self._dirty(row_path)

            if (
                local_sha
                and not held_by_worktree
                and not await self._on_origin(checkout, local_sha, origin_sha, default_branch)
            ):
                preserve.append(("commit", None, local_sha))

            preserved_ref = PRESERVED_PREFIX + row["id"]
            if preserve and dry_run:
                for kind, path, sha in preserve:
                    planned.append(
                        {
                            "action": "push",
                            "ref": preserved_ref,
                            "source": (
                                f"snapshot of {path}"
                                if kind == "snapshot"
                                else f"HEAD of {path}"
                                if path
                                else f"local branch {branch}"
                            ),
                            "sha": None if kind == "snapshot" else sha,
                        }
                    )
            elif preserve:
                shas = []
                for kind, path, sha in preserve:
                    if kind == "snapshot":
                        sha = await self._snapshot(path, sha, row["id"])
                    shas.append(sha)
                evidence["preserved_sha"] = await self._push_preserved(
                    checkout, preserved_ref, shas, row["id"], repository_url
                )
                evidence["preserved_ref"] = preserved_ref
            for path in detach:
                if dry_run:
                    planned.append({"action": "detach", "worktree": path})
                else:
                    # Never reset or clean: the checkout keeps the writer's work.
                    await self.git._arun(["switch", "--detach"], cwd=path)
                    evidence.setdefault("detached", []).append(path)
        if dry_run:
            evidence["planned"] = planned
        return _Plan(preserved=bool(preserve), workspace_clean=workspace_clean)

    async def _checkout_for(self, row: dict[str, Any]) -> tuple[str | None, str, str | None, str]:
        """Base checkout, default branch, row checkout, authorized repository URL.

        The base is the row's workspace's base clone when it has one, else a
        project-repo base workspace of a project integrating the repository.
        """
        async with self.db._engine.connect() as conn:
            repository = (
                await conn.execute(
                    select(repos.c.default_branch, repos.c.url).where(
                        repos.c.id == row["repository_id"]
                    )
                )
            ).one_or_none()
            if repository is None:
                raise _Refusal(ORIGIN_UNREACHABLE, "repository is unavailable")
            default_branch = repository.default_branch or "main"
            candidates: list[str] = []
            row_path = None
            if row["workspace_id"]:
                own = (
                    await conn.execute(
                        select(workspaces.c.workspace_path, workspaces.c.base_workspace_id).where(
                            workspaces.c.id == row["workspace_id"]
                        )
                    )
                ).one_or_none()
                if own is not None:
                    row_path = own.workspace_path
                    if own.base_workspace_id:
                        base = (
                            await conn.execute(
                                select(workspaces.c.workspace_path).where(
                                    workspaces.c.id == own.base_workspace_id
                                )
                            )
                        ).scalar_one_or_none()
                        if base:
                            candidates.append(base)
                    candidates.append(own.workspace_path)
            project_ids = await _project_ids(conn, row["repository_id"])
            if project_ids:
                candidates.extend(
                    (
                        await conn.execute(
                            select(workspaces.c.workspace_path)
                            .where(
                                workspaces.c.project_id.in_(project_ids),
                                or_(
                                    workspaces.c.kind_id.is_(None),
                                    workspaces.c.kind_id == "project-repo",
                                ),
                            )
                            .order_by(
                                workspaces.c.base_workspace_id.is_not(None),
                                case((workspaces.c.source_type == "clone", 0), else_=1),
                                workspaces.c.id,
                            )
                        )
                    ).scalars()
                )
        checkout = next((path for path in candidates if os.path.isdir(path)), None)
        return checkout, default_branch, row_path, repository.url

    async def _live_work_dirs(self) -> set[str]:
        async with self.db._engine.connect() as conn:
            dirs = (
                await conn.execute(select(sessions.c.work_dir).where(session_attached_clause()))
            ).scalars()
            return {os.path.realpath(d) for d in dirs if d}

    def _mutex(self, checkout: str):
        if self.git_mutex is None:
            return contextlib.nullcontext()
        return self.git_mutex(checkout)

    async def _dirty(self, path: str) -> bool:
        status = await self.git._arun(
            ["status", "--porcelain", "--", ".", f":(exclude){_LOCK_SENTINEL}"], cwd=path
        )
        return bool(status.strip())

    async def _on_origin(
        self, checkout: str, sha: str, origin_sha: str | None, default_branch: str
    ) -> bool:
        """Does origin already carry *sha*: on the branch, or on the default branch?"""
        if origin_sha and (
            sha == origin_sha or await self.git.ais_ancestor(checkout, sha, origin_sha)
        ):
            return True
        return bool(
            await self.git.ais_ancestor(checkout, sha, f"refs/remotes/origin/{default_branch}")
        )

    async def _snapshot(self, path: str, head: str, owner_row_id: str) -> str:
        """Commit HEAD plus every change and untracked file, leaving the checkout as is.

        A copy of the live index is extended through ``GIT_INDEX_FILE``, the
        pattern ``task_checkpoint.capture_checkpoint`` uses, so neither the
        working tree nor the writer's staged state changes.
        """
        from src.orchestrator.task_checkpoint import _lock_sentinel_ignored

        with tempfile.TemporaryDirectory(prefix="aq-owner-recovery-") as temp:
            index = Path(temp) / "index"
            live_index = Path(await self.git._arun(["rev-parse", "--git-path", "index"], cwd=path))
            if not live_index.is_absolute():
                live_index = Path(path) / live_index
            if live_index.exists():
                shutil.copyfile(live_index, index)
            isolated = GitManager()
            isolated._SUBPROCESS_ENV = {**self.git._SUBPROCESS_ENV, "GIT_INDEX_FILE": str(index)}
            pathspec = ["--", "."]
            if not await _lock_sentinel_ignored(self.git, path):
                pathspec.append(f":(exclude){_LOCK_SENTINEL}")
            await isolated._arun(["add", "--all", *pathspec], cwd=path)
            tree = await isolated._arun(["write-tree"], cwd=path)
        return await self._commit(path, tree, [head], owner_row_id)

    async def _commit(self, cwd: str, tree: str, parents: list[str], owner_row_id: str) -> str:
        """A preservation commit, dated from its first parent so a rerun is identical."""
        stamp = (await self.git._arun(["show", "-s", "--format=%ct", parents[0]], cwd=cwd)).strip()
        date = f"{stamp} +0000"
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        args += ["-m", f"aq: preserved by owner recovery {owner_row_id}"]
        name, email = _IDENTITY
        result = await self.git.arun_git_result(
            args,
            cwd=cwd,
            env={
                "GIT_AUTHOR_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_NAME": name,
                "GIT_COMMITTER_EMAIL": email,
                "GIT_COMMITTER_DATE": date,
            },
        )
        if result.returncode != 0:
            raise GitError(f"git commit-tree failed: {(result.stderr or '').strip()}")
        return result.stdout.strip()

    async def _push_preserved(
        self, checkout: str, ref: str, shas: list[str], owner_row_id: str,
        repository_url: str,
    ) -> str:
        """Put every sha in *shas* on origin's *ref*, never forcing; return its tip.

        One source is pushed as is.  Several -- or a ref that already holds
        something else -- are joined under one commit that has each as a
        parent, so the push is a fast-forward and nothing already preserved
        is dropped.  A ref already holding everything is left alone.
        """
        shas = list(dict.fromkeys(shas))
        existing = await self.git.als_remote_ref(checkout, ref)
        if existing.state is RemoteRefState.ERROR:
            raise _Refusal(ORIGIN_UNREACHABLE, f"reading origin/{ref}: {existing.error}")
        held = existing.oid if existing.state is RemoteRefState.PRESENT else None
        if held is not None:
            if await self.git.arev_parse(checkout, f"{held}^{{commit}}") is None:
                try:
                    await self.git.afetch_origin(
                        checkout, repository_url=repository_url, lock_held=True
                    )
                except (GitError, GitHubAccessError) as exc:
                    raise _Refusal(
                        ORIGIN_UNREACHABLE,
                        f"fetching origin/{ref}: {exc}",
                    ) from exc
            missing = [
                sha
                for sha in shas
                if sha != held and not await self.git.ais_ancestor(checkout, sha, held)
            ]
            if not missing:
                return held
            shas = missing
        if len(shas) == 1 and (
            held is None or await self.git.ais_ancestor(checkout, held, shas[0])
        ):
            tip = shas[0]
        else:
            parents = shas + ([held] if held is not None else [])
            tip = await self._commit(checkout, f"{shas[0]}^{{tree}}", parents, owner_row_id)
        try:
            await self.git.apush_validated_ref(checkout, tip, ref)
        except GitError as exc:
            raise _Refusal(ORIGIN_UNREACHABLE, f"pushing {ref}: {exc}") from exc
        return tip

    # -- step 5: release ------------------------------------------------------

    async def _release(
        self,
        row: dict[str, Any],
        session: dict[str, Any] | None,
        plan: _Plan,
        outcome: str,
        evidence: dict[str, Any],
        principal: str,
    ) -> RecoveryOutcome:
        owners = integration_branch_owners
        now = self.clock()
        claim_result = None
        async with self.db.immediate() as conn:
            for project_id in await _project_ids(conn, row["repository_id"]):
                await self.db.lock_hierarchy_project(conn, project_id)
            swapped = await conn.execute(
                update(owners)
                .where(
                    owners.c.id == row["id"],
                    owners.c.fence_token == row["fence_token"],
                    owners.c.handoff_state == row["handoff_state"],
                )
                .values(
                    handoff_state="released",
                    fence_token=row["fence_token"] + 1,
                    session_id=None,
                    workspace_id=None,
                    confirmed_workspace_id=row["workspace_id"],
                    updated_at=now,
                )
            )
            if swapped.rowcount == 1:
                evidence["released_fence_token"] = int(row["fence_token"]) + 1
                await self._unlock_workspace(conn, row, session, plan, evidence)
                if session is not None:
                    claim_result = await self._release_claim(conn, row, session, now, evidence)
                result = RecoveryOutcome(row["id"], outcome, None, evidence)
                project_id = await _project_for(conn, row)
                await self._audit(conn, row, result, principal, now)
                await self.db.log_event(
                    RECOVERED_EVENT,
                    project_id=project_id,
                    task_id=row["owner_id"],
                    payload=json.dumps(
                        {**result.to_dict(), "principal": principal}, sort_keys=True, default=str
                    ),
                    conn=conn,
                )
        if swapped.rowcount != 1:
            detail = (
                f"owner row {row['id']} changed after the check (fence "
                f"{row['fence_token']}, {row['handoff_state']}); it was not released"
            )
            if evidence.get("preserved_ref"):
                detail += f"; {evidence['preserved_ref']} keeps what was preserved"
            raise _Refusal(STALE_FENCE, detail)
        if claim_result is not None:
            await self.db._after_release(claim_result)
        await self._comment(row, _release_comment(row, result, principal))
        return result

    async def _unlock_workspace(
        self,
        conn,
        row: dict[str, Any],
        session: dict[str, Any] | None,
        plan: _Plan,
        evidence: dict[str, Any],
    ) -> None:
        if not row["workspace_id"]:
            return
        holders = [workspaces.c.locked_by_task_id == row["owner_id"]]
        agent_id = session["agent_id"] if session is not None else None
        if agent_id and not await _agent_live_elsewhere(conn, agent_id, session["id"]):
            holders.append(workspaces.c.locked_by_agent_id == agent_id)
        values: dict[str, Any] = {
            "locked_by_task_id": None,
            "locked_by_agent_id": None,
            "locked_at": None,
            "lock_mode": None,
        }
        if not plan.workspace_clean:
            # A dirty checkout must not be recycled by allocation.
            values["enabled"] = False
        unlocked = await conn.execute(
            update(workspaces)
            .where(workspaces.c.id == row["workspace_id"], or_(*holders))
            .values(**values)
        )
        evidence["workspace_unlocked"] = unlocked.rowcount == 1
        if unlocked.rowcount == 1 and not plan.workspace_clean:
            evidence["workspace_disabled"] = True

    async def _release_claim(
        self, conn, row: dict[str, Any], session: dict[str, Any], now: float, evidence
    ):
        """Release the gone writer's claim, after the owner row stopped protecting it."""
        current = (
            (await conn.execute(select(sessions).where(sessions.c.id == session["id"])))
            .mappings()
            .one_or_none()
        )
        if current is None or current["claim_phase"] is None:
            return None
        if current["task_id"] not in (None, row["owner_id"]):
            evidence["claim_released"] = False
            evidence["claim_left"] = (
                f"session {current['id']} holds a claim on {current['task_id']}, not "
                f"{row['owner_id']}"
            )
            return None
        if current["agent_id"] and await _agent_live_elsewhere(
            conn, current["agent_id"], current["id"]
        ):
            # release_claim unwinds the agent's locks and current task, which
            # would corrupt the live session that reuses the agent.
            evidence["claim_released"] = False
            evidence["claim_left"] = (
                f"agent {current['agent_id']} already runs another live session"
            )
            return None
        status = TaskStatus.READY
        if current["task_id"]:
            status = TaskStatus(
                (
                    await conn.execute(
                        select(tasks.c.status).where(tasks.c.id == current["task_id"])
                    )
                ).scalar_one()
            )
        # Every settled status is kept exactly as it is.  A task still in
        # flight goes back to READY: that is what pool teardown does with a
        # stopped writer's claim (``terminate_pool_session``), and it only
        # skipped it because this owner row protected the claim -- teardown
        # of a stopped session is never retried, so nothing else would.
        in_flight = status in _IN_FLIGHT_STATUSES
        result = await self.db.release_claim(
            current["id"],
            task_status=TaskStatus.READY if in_flight else status,
            context="integration_owner_recovery",
            now=now,
            conn=conn,
            expected_task_id=current["task_id"],
            expected_claim_epoch=current["last_claim_epoch"],
            preserve_terminal_task=not in_flight,
            release_workspace_lock=False,
        )
        evidence["claim_released"] = bool(result.released)
        evidence["claim_task_status"] = status.value
        return result

    # -- audit ----------------------------------------------------------------

    async def _audit(
        self, conn, row: dict[str, Any], result: RecoveryOutcome, principal: str, now: float
    ) -> None:
        await conn.execute(
            insert(integration_owner_recoveries).values(
                id=uuid.uuid4().hex,
                owner_row_id=row["id"],
                repository_id=row["repository_id"],
                ref=row["ref"],
                task_id=None if row["owner_role"] == COLLECTOR_ROLE else row["owner_id"],
                outcome=result.outcome,
                reason=result.reason,
                evidence=json.loads(json.dumps(result.evidence, default=str)),
                principal=principal,
                created_at=now,
            )
        )

    async def _record_refusal(
        self, row: dict[str, Any], result: RecoveryOutcome, principal: str
    ) -> None:
        audit = integration_owner_recoveries
        now = self.clock()
        async with self.db.immediate() as conn:
            latest = (
                await conn.execute(
                    select(audit.c.reason, audit.c.created_at)
                    .where(audit.c.owner_row_id == row["id"])
                    .order_by(audit.c.created_at.desc())
                    .limit(1)
                )
            ).one_or_none()
            if (
                latest is not None
                and latest.reason == result.reason
                and now - latest.created_at < REFUSAL_THROTTLE_SECONDS
            ):
                return
            first_of_reason = (
                await conn.execute(
                    select(audit.c.id)
                    .where(audit.c.owner_row_id == row["id"], audit.c.reason == result.reason)
                    .limit(1)
                )
            ).scalar_one_or_none() is None
            await self._audit(conn, row, result, principal, now)
            await self.db.log_event(
                REFUSED_EVENT,
                project_id=await _project_for(conn, row),
                task_id=None if row["owner_role"] == COLLECTOR_ROLE else row["owner_id"],
                payload=json.dumps(
                    {**result.to_dict(), "principal": principal}, sort_keys=True, default=str
                ),
                conn=conn,
            )
        if first_of_reason and row["owner_role"] != COLLECTOR_ROLE:
            await self._comment(row, _refusal_comment(row, result))

    async def _comment(self, row: dict[str, Any], body: str) -> None:
        async with self.db._engine.connect() as conn:
            exists = (
                await conn.execute(select(tasks.c.id).where(tasks.c.id == row["owner_id"]))
            ).scalar_one_or_none()
        if exists is None:
            return
        try:
            await self.db.add_task_comment(
                row["owner_id"], body, author_kind="supervisor", author_id="owner-recovery"
            )
        except Exception:  # the release (or refusal) is the record; the note is a courtesy
            logger.debug("owner recovery: task comment failed", exc_info=True)


def owner_recovery_for(orchestrator: Any) -> OwnerRecovery | None:
    """The daemon's :class:`OwnerRecovery`, or ``None`` when it cannot be built.

    Without the provider-backed stop probe no writer can be proven gone, so a
    service built without it could only ever refuse.
    """
    db = getattr(orchestrator, "db", None)
    git = getattr(orchestrator, "git", None)
    confirm_stopped = stop_confirmer_for(orchestrator)
    if db is None or git is None or confirm_stopped is None:
        return None
    return OwnerRecovery(
        db,
        git,
        getattr(orchestrator, "_git_mutex", None),
        confirm_stopped=confirm_stopped,
    )


async def _agent_live_elsewhere(conn, agent_id: str, session_id: str) -> bool:
    """Does *agent_id* run a live session other than *session_id*?"""
    return (
        await conn.execute(
            select(sessions.c.id)
            .where(
                sessions.c.agent_id == agent_id,
                sessions.c.id != session_id,
                session_attached_clause(),
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def _project_for(conn, row: dict[str, Any]) -> str | None:
    """The owning task's project, else the one project integrating the repository."""
    if row["owner_role"] != COLLECTOR_ROLE:
        for table in (tasks, archived_tasks):
            project_id = (
                await conn.execute(select(table.c.project_id).where(table.c.id == row["owner_id"]))
            ).scalar_one_or_none()
            if project_id is not None:
                return project_id
    integrating = (
        (
            await conn.execute(
                select(projects.c.id).where(
                    projects.c.integration_repository_id == row["repository_id"]
                )
            )
        )
        .scalars()
        .all()
    )
    return integrating[0] if len(integrating) == 1 else None


def _same_path(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _release_comment(row: dict[str, Any], result: RecoveryOutcome, principal: str) -> str:
    body = (
        f"Integration owner `{row['id']}` of `{row['ref']}` was released by owner recovery "
        f"({principal}): {result.outcome}. Its writer is gone and the branch is safe on origin"
    )
    if result.evidence.get("preserved_ref"):
        body += (
            f"; unsaved work was preserved at `{result.evidence['preserved_ref']}` "
            f"(`{result.evidence['preserved_sha']}`)"
        )
    return body + "."


def _refusal_comment(row: dict[str, Any], result: RecoveryOutcome) -> str:
    return (
        f"Owner recovery could not release integration owner `{row['id']}` of "
        f"`{row['ref']}`: `{result.reason}` -- {result.evidence.get('detail', '')}. "
        "The owner-recovery sweep retries it."
    )


__all__ = [
    "CHECKOUT_IN_USE",
    "DEFAULT_QUIET_SECONDS",
    "NOT_ELIGIBLE",
    "NOT_FOUND",
    "NOT_RECOVERABLE_STATE",
    "ORIGIN_UNREACHABLE",
    "PRESERVED_AND_RELEASED",
    "PRESERVED_PREFIX",
    "RECOVERABLE_STATES",
    "RECOVERED_EVENT",
    "REFUSAL_THROTTLE_SECONDS",
    "REFUSED_EVENT",
    "RELEASED",
    "STALE_FENCE",
    "WRITER_LIVE",
    "OwnerRecovery",
    "RecoveryOutcome",
    "owner_recovery_for",
]
