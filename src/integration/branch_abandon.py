"""Delete the branches of abandoned work, local and remote, after an audit row.

No-dangling-branches item 3.  An abandon decision is final: a task closed
obsolete (superseded, duplicate, plan changed), a subtree archived with
``--abandon-undelivered``, a failed task that was then closed, a superseded or
aborted batch.  The operator's words are "if we are abandoning a branch it
should get completely deleted", and the invariant the epic states is that
every branch on origin and in AQ's local checkouts is either live work or a
protected flow branch.  A branch whose work was deliberately thrown away is
neither, so it has to go.

Two rules make this safe to run from an abandon decision:

* **The audit row comes first.**  :class:`~src.database.tables.branch_deletion_audit`
  records the branch name and its exact head sha *before* anything is pushed.
  An abandoned branch is often unmerged — that is the point — so the sha is
  the only handle on that work afterwards.  A tip the default branch cannot
  reach is also written to a verified bundle in the same step
  (:func:`~src.integration.delivery_branches.preserve_branch_tips`), and the
  bundle path is recorded on the row.  Restore one with ``git bundle
  unbundle <bundle>`` and ``git push origin <sha>:refs/heads/<branch>``.
* **A live owner holds the branch.**  Nothing is deleted while
  :func:`~src.integration.delivery_branches.train_cleanup_hold` finds a live
  task, a live session, an active batch that still reads or writes the ref, an
  open integration subject, or a pending discard on it.  Over-holding costs a
  branch that stays until the next pass; under-holding costs work, so the
  check runs again on every attempt and never against a stale answer.

Deleting the work somebody deliberately abandoned is irreversible on origin,
so every delete is a lease on the head that was observed: a branch pushed to
in the meantime is recorded ``moved`` and kept.  The audit row's ``pending``
outcome is the durable intent — a daemon that dies between recording and
confirming leaves the row behind, and :meth:`BranchAbandonService.drain_due`
finishes it.

This module owns task branches and a batch's private candidate refs.  It is
deliberately *not* the backstop for anything merely old (that is
:func:`~src.integration.delivery_branches.expired_task_branches` and the daily
sweep), and it never touches a branch whose recovery is supposed to resume on
it: a failed close keeps its branch so ``aq task recover`` can reuse it.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from src.database.tables import (
    archived_tasks,
    branch_deletion_audit,
    integration_batches,
    integration_batch_members,
    task_branch_origins,
    tasks,
)
from src.git.manager import GitError, RemoteRefState
from src.integration.delivery_branches import (
    branch_of,
    deletable,
    preserve_branch_tips,
    repository_protected_branches,
    train_cleanup_hold,
)

logger = logging.getLogger(__name__)

#: Why a branch was abandoned.  Recorded on the audit row and asserted by its
#: check constraint, so a new caller cannot invent a fourth kind of ending.
OBSOLETE_CLOSE = "obsolete_close"
CANCEL = "cancel"
FAIL_CLOSE = "fail_close"
SUPERSEDE = "supersede"
ABORT = "abort"
DELETE = "delete"
ABANDON_REASONS = (
    OBSOLETE_CLOSE,
    CANCEL,
    FAIL_CLOSE,
    SUPERSEDE,
    ABORT,
    DELETE,
)
#: What became of a recorded ref.  Mirrors the table's check constraint.
AUDIT_OUTCOMES = ("pending", "deleted", "held", "failed", "moved")

#: A pending audit row that keeps failing backs off from ~30s toward this.
RETRY_BASE_SECONDS = 30.0
RETRY_MAX_SECONDS = 1800.0
#: Transport trouble is retried this many times before the row is parked as
#: ``failed`` for an operator to look at.
MAX_ATTEMPTS = 6
#: Where the bundle of an unmerged, abandoned tip is written.
BACKUP_DIRNAME = "branch-abandons"


async def record_branch_deletion(
    conn,
    *,
    project_id: str,
    repository_id: str | None,
    task_id: str | None,
    branch: str,
    head_sha: str,
    reason: str,
    detail: str | None = None,
    outcome: str = "pending",
    detail_reason: str | None = None,
    now: float | None = None,
) -> str:
    """Write the audit row for one abandoned branch, inside *conn*.

    The row is the durable record that this branch existed and what it pointed
    at, and it must commit before any ref is deleted — otherwise a daemon that
    dies mid-push leaves an unmerged tip with no handle on it at all.  A
    repeated record for the same branch and reason updates the pending row
    rather than piling up, so a retried abandon decision stays one fact; a row
    that already settled (``deleted``, ``held``) is left alone, because its
    outcome is the answer to that decision.
    """
    if reason not in ABANDON_REASONS:
        raise ValueError(f"unknown abandon reason: {reason!r}")
    if outcome not in AUDIT_OUTCOMES:
        raise ValueError(f"unknown abandon outcome: {outcome!r}")
    sha = (head_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("an audit row needs the branch's exact 40-character head sha")
    recorded_at = time.time() if now is None else now
    row_id = "branch-abandon-" + uuid.uuid4().hex
    existing = (
        (
            await conn.execute(
                select(branch_deletion_audit.c.id).where(
                    branch_deletion_audit.c.project_id == project_id,
                    branch_deletion_audit.c.branch == branch,
                    branch_deletion_audit.c.reason == reason,
                    branch_deletion_audit.c.outcome == "pending",
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        await conn.execute(
            update(branch_deletion_audit)
            .where(branch_deletion_audit.c.id == existing)
            .values(
                repository_id=repository_id,
                task_id=task_id,
                head_sha=sha,
                detail=detail,
                detail_reason=detail_reason,
                next_attempt_at=recorded_at,
                recorded_at=recorded_at,
            )
        )
        return str(existing)
    await conn.execute(
        branch_deletion_audit.insert().values(
            id=row_id,
            project_id=project_id,
            repository_id=repository_id,
            task_id=task_id,
            branch=branch,
            head_sha=sha,
            reason=reason,
            detail=detail,
            outcome=outcome,
            detail_reason=detail_reason,
            attempts=0,
            next_attempt_at=recorded_at if outcome == "pending" else None,
            recorded_at=recorded_at,
        )
    )
    return row_id


class BranchAbandonService:
    """Delete the branches an abandon decision abandoned, once, with an audit row.

    :meth:`abandon_task` is the entry point every task-level decision calls
    (obsolete close, abandon-and-archive, a failed task that was then closed).
    :meth:`drain_due` retries whatever it recorded but could not confirm, so a
    transient transport failure or a branch that was held at the time resolves
    without an operator.
    """

    def __init__(
        self,
        db: Any,
        *,
        data_dir: str | Path,
        git_manager: Any | None = None,
        clock=time.time,
    ) -> None:
        self.db = db
        self.data_dir = Path(data_dir)
        self.git = git_manager
        self.clock = clock

    # -- the decision ----------------------------------------------------

    async def abandon_task(
        self,
        task_id: str,
        *,
        reason: str,
        detail: str | None = None,
        principal: str = "",
    ) -> dict[str, Any]:
        """Delete every branch *task_id* put on the remote and in local checkouts.

        The branches considered are the task's own, its ``-wip`` sibling, its
        branch origins, and ``aq/<task-id>`` — the names a task's work can
        appear under.  Each is guarded, recorded and deleted in turn; the
        return value names what happened to every one of them, so a caller can
        report a refusal (``held``) instead of silently leaving a branch.
        """
        row = await self._task_row(task_id)
        if row is None:
            return {"task_id": task_id, "state": "gone", "branches": []}
        if self.git is None:
            return {
                "task_id": task_id,
                "state": "pending",
                "reason": "git_transport_unavailable",
                "branches": [],
            }
        project_id = row["project_id"]
        repository = await self.db.get_repo(row["repo_id"]) if row["repo_id"] else None
        if row["repo_id"] and repository is None:
            return {
                "task_id": task_id,
                "state": "pending",
                "reason": "repository_unavailable",
                "branches": [],
            }
        candidates = await self._candidate_branches(task_id, row)
        if not candidates:
            return {"task_id": task_id, "state": "done", "branches": []}

        outcomes: list[dict[str, Any]] = []
        pending = 0
        why = detail or (f"{reason} by {principal}" if principal else None)
        for branch in candidates:
            result = await self._abandon_branch(
                branch,
                project_id=project_id,
                repository_id=row["repo_id"],
                repository=repository,
                task_id=task_id,
                reason=reason,
                detail=why,
            )
            outcomes.append(result)
            if result["outcome"] in {"pending", "failed", "held"}:
                pending += 1
        state = "done" if not pending else "pending"
        summary = {"task_id": task_id, "state": state, "branches": outcomes}
        if pending:
            summary["reason"] = f"{pending} branch(es) still held or unconfirmed"
        return summary

    # -- one branch ------------------------------------------------------

    async def _abandon_branch(
        self,
        branch: str,
        *,
        project_id: str,
        repository_id: str | None,
        repository: Any,
        task_id: str | None,
        reason: str,
        detail: str | None,
    ) -> dict[str, Any]:
        """Guard, record and delete one branch; never raises for a refusal."""
        name = branch_of(branch) or branch
        if repository is None:
            return {
                "branch": name,
                "outcome": "pending",
                "detail": "the task names no repository, so no ref can be proven",
            }
        async with self.db._engine.connect() as conn:
            protected = await repository_protected_branches(
                conn, repository.id, default_branch=repository.default_branch
            )
        if not deletable(name, repository.default_branch, protected=protected):
            return {
                "branch": name,
                "outcome": "held",
                "detail": f"{name} is not an aq/ branch or is a protected flow target",
            }

        checkouts = await self._checkouts(project_id, repository)
        observed, local_tips, checkout = await self._observe(name, checkouts, repository)
        if observed is None and not local_tips:
            # Nothing named this branch anywhere. There is no sha to record and
            # no ref to delete; a row would claim a deletion that never was.
            return {"branch": name, "outcome": "absent", "detail": "no such branch"}
        head = observed or next(iter(local_tips.values()))

        async with self.db._engine.connect() as conn:
            held_by = await train_cleanup_hold(conn, repository_id=repository.id, ref=name)
        if held_by is not None:
            await self._record(
                project_id=project_id,
                repository_id=repository.id,
                task_id=task_id,
                branch=name,
                head=head,
                reason=reason,
                detail=detail,
                outcome="held",
                detail_reason=held_by,
            )
            return {"branch": name, "outcome": "held", "detail": held_by}

        backup = await self._back_up(name, head, checkout, repository)

        # The audit row commits before anything is pushed: it is the only handle
        # on an unmerged tip once the ref is gone.
        await self._record(
            project_id=project_id,
            repository_id=repository.id,
            task_id=task_id,
            branch=name,
            head=head,
            reason=reason,
            detail=detail,
            outcome="pending",
        )
        if backup:
            await self._note_backup(project_id, name, reason, backup)

        remote = "skipped"
        if observed is not None:
            remote = await self._delete_remote(name, head, checkout, repository)
        local = await self._delete_local(name, local_tips, checkouts)
        if remote == "failed":
            outcome, why = "failed", "the remote ref could not be confirmed deleted"
        elif remote == "moved":
            outcome, why = "moved", "the branch moved after it was recorded"
        elif remote == "held" or local == "held":
            outcome, why = "held", "a worktree still has the branch checked out"
        elif local == "failed":
            outcome, why = "failed", "a local ref could not be confirmed deleted"
        else:
            outcome, why = "deleted", None
        await self._settle(
            project_id=project_id,
            branch=name,
            reason=reason,
            outcome=outcome,
            detail_reason=why,
            backup_path=str(backup) if backup else None,
        )
        result = {"branch": name, "outcome": outcome, "remote": remote, "local": local}
        if why:
            result["detail"] = why
        if backup:
            result["backup"] = str(backup)
        if outcome == "deleted":
            await self._log(project_id, task_id, name, head, reason, outcome, why)
        return result

    async def _delete_remote(
        self, name: str, head: str, checkout: str | None, repository: Any
    ) -> str:
        """Delete the remote head under the observed exact lease."""
        if checkout is None:
            return "failed"
        try:
            await self.git.adelete_remote_ref_exact(checkout, name, head)
        except GitError as exc:
            observed = await self.git.als_remote_ref(checkout, name)
            if observed.state is RemoteRefState.ERROR:
                return "failed"
            if observed.state is RemoteRefState.ABSENT or observed.oid is None:
                return "deleted"
            if observed.oid != head:
                return "moved"
            logger.warning("Abandon delete of %s did not confirm: %s", name, exc)
            return "failed"
        after = await self.git.als_remote_ref(checkout, name)
        if after.state is RemoteRefState.ERROR:
            return "failed"
        if after.state is RemoteRefState.ABSENT or after.oid is None:
            return "deleted"
        return "moved" if after.oid != head else "failed"

    async def _delete_local(
        self, name: str, local_tips: dict[str, str], checkouts: Sequence[str]
    ) -> str:
        """Delete the local head in every checkout that has it.

        A branch checked out in a worktree is held: git will not delete it, and
        detaching somebody's worktree is not this service's decision.
        """
        if not local_tips:
            return "absent"
        for checkout in checkouts:
            try:
                attached = {
                    entry.get("branch") for entry in await self.git.aworktree_list(checkout)
                }
            except GitError:
                return "failed"
            if name in attached:
                return "held"
            tip = local_tips.get(checkout) or await self.git.arev_parse(
                checkout, f"refs/heads/{name}"
            )
            if tip is None:
                continue
            try:
                await self.git.adelete_local_ref_exact(
                    checkout, ref=f"refs/heads/{name}", expected_old_oid=tip
                )
            except GitError:
                if await self.git.aref_exists(checkout, f"refs/heads/{name}") is not False:
                    return "failed"
        return "deleted"

    async def _back_up(
        self, name: str, head: str, checkout: str | None, repository: Any
    ) -> Path | None:
        """Bundle the tip when the default branch cannot reach it.

        Unknown reachability is bundled too: the bundle is what makes an
        unmerged abandoned branch restorable, so the unproved case is the one
        that must not be skipped.  A failure here is logged and the delete
        still proceeds — the audit row already carries the sha.
        """
        if checkout is None:
            return None
        default_head = await self.git.als_remote_sha(checkout, repository.default_branch)
        if not default_head:
            return None
        try:
            reachable = await self.git.ais_ancestor(
                checkout, head, default_head, strict=True
            )
        except GitError:
            reachable = None
        if reachable is True:
            return None
        try:
            return await preserve_branch_tips(
                self._run_git,
                checkout,
                {name: head},
                target_sha=default_head,
                backup_dir=self.backup_dir,
                repository_id=str(repository.id),
                now=self.clock(),
            )
        except (GitError, OSError):
            logger.warning("Could not bundle the abandoned tip of %s", name, exc_info=True)
            return None

    # -- retries ---------------------------------------------------------

    async def drain_due(self, *, now: float | None = None, limit: int = 20) -> list[dict]:
        """Retry every recorded abandon whose backoff has elapsed."""
        observed_at = self.clock() if now is None else now
        async with self.db._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(branch_deletion_audit)
                        .where(
                            branch_deletion_audit.c.outcome == "pending",
                            (branch_deletion_audit.c.next_attempt_at.is_(None))
                            | (branch_deletion_audit.c.next_attempt_at <= observed_at),
                        )
                        .order_by(branch_deletion_audit.c.recorded_at, branch_deletion_audit.c.id)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        results = []
        for row in rows:
            results.append(await self._retry(dict(row), observed_at))
        return results

    async def _retry(self, row: dict[str, Any], observed_at: float) -> dict[str, Any]:
        attempts = int(row["attempts"]) + 1
        async with self.db._engine.begin() as conn:
            await conn.execute(
                update(branch_deletion_audit)
                .where(branch_deletion_audit.c.id == row["id"])
                .values(attempts=attempts, next_attempt_at=observed_at + self._backoff(attempts))
            )
        if self.git is None or not row["repository_id"]:
            return {"branch": row["branch"], "outcome": "failed", "detail": "no git transport"}
        repository = await self.db.get_repo(row["repository_id"])
        if repository is None:
            return {"branch": row["branch"], "outcome": "failed", "detail": "repository is gone"}
        result = await self._abandon_branch(
            row["branch"],
            project_id=row["project_id"],
            repository_id=row["repository_id"],
            repository=repository,
            task_id=row["task_id"],
            reason=row["reason"],
            detail=row["detail"],
        )
        if result["outcome"] == "pending" or (
            result["outcome"] == "failed" and attempts >= MAX_ATTEMPTS
        ):
            await self._settle(
                project_id=row["project_id"],
                branch=row["branch"],
                reason=row["reason"],
                outcome="failed",
                detail_reason=f"abandon delete gave up after {attempts} attempt(s)",
                backup_path=row["backup_path"],
            )
        return result

    # -- bookkeeping -----------------------------------------------------

    async def _record(
        self,
        *,
        project_id: str,
        repository_id: str | None,
        task_id: str | None,
        branch: str,
        head: str,
        reason: str,
        detail: str | None,
        outcome: str,
        detail_reason: str | None = None,
    ) -> str:
        async with self.db._engine.begin() as conn:
            return await record_branch_deletion(
                conn,
                project_id=project_id,
                repository_id=repository_id,
                task_id=task_id,
                branch=branch,
                head_sha=head,
                reason=reason,
                detail=detail,
                outcome=outcome,
                detail_reason=detail_reason,
                now=self.clock(),
            )

    async def _note_backup(self, project_id: str, branch: str, reason: str, backup: Path) -> None:
        """Record the bundle path on the audit row that is already committed."""
        async with self.db._engine.begin() as conn:
            await conn.execute(
                update(branch_deletion_audit)
                .where(
                    branch_deletion_audit.c.project_id == project_id,
                    branch_deletion_audit.c.branch == branch,
                    branch_deletion_audit.c.reason == reason,
                )
                .values(backup_path=str(backup))
            )

    async def _settle(
        self,
        *,
        project_id: str,
        branch: str,
        reason: str,
        outcome: str,
        detail_reason: str | None,
        backup_path: str | None,
    ) -> None:
        now = self.clock()
        async with self.db._engine.begin() as conn:
            await conn.execute(
                update(branch_deletion_audit)
                .where(
                    branch_deletion_audit.c.project_id == project_id,
                    branch_deletion_audit.c.branch == branch,
                    branch_deletion_audit.c.reason == reason,
                    branch_deletion_audit.c.outcome == "pending",
                )
                .values(
                    outcome=outcome,
                    detail_reason=detail_reason,
                    backup_path=backup_path,
                    next_attempt_at=None,
                    deleted_at=now if outcome == "deleted" else None,
                )
            )

    async def _log(
        self,
        project_id: str,
        task_id: str | None,
        branch: str,
        head: str,
        reason: str,
        outcome: str,
        detail: str | None,
    ) -> None:
        try:
            await self.db.log_event(
                "git.abandoned_branch_deleted",
                project_id=project_id,
                task_id=task_id,
                payload=json.dumps(
                    {"branch": branch, "head_sha": head, "reason": reason,
                     "outcome": outcome, "detail": detail},
                    sort_keys=True,
                ),
            )
        except Exception:
            logger.warning("Could not log the abandoned-branch deletion of %s", branch,
                           exc_info=True)

    # -- resolution helpers ----------------------------------------------

    async def _task_row(self, task_id: str) -> dict[str, Any] | None:
        for table in (tasks, archived_tasks):
            async with self.db._engine.connect() as conn:
                row = (
                    (
                        await conn.execute(
                            select(
                                table.c.id,
                                table.c.project_id,
                                table.c.repo_id,
                                table.c.branch_name,
                                table.c.status,
                            ).where(table.c.id == task_id)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            if row is not None:
                return dict(row)
        return None

    async def _candidate_branches(self, task_id: str, row: dict[str, Any]) -> list[str]:
        """Every name this task's work can appear under, most specific first."""
        names: list[str] = []
        for value in (row.get("branch_name"), f"aq/{task_id}"):
            name = branch_of(value)
            if name:
                names.append(name)
                names.append(f"{name}-wip")
        async with self.db._engine.connect() as conn:
            origins = (
                await conn.execute(
                    select(task_branch_origins.c.branch_name).where(
                        task_branch_origins.c.task_id == task_id,
                        task_branch_origins.c.branch_name.is_not(None),
                    )
                )
            ).scalars()
        for value in origins:
            name = branch_of(value)
            if name:
                names.append(name)
                names.append(f"{name}-wip")
        seen: set[str] = set()
        return [name for name in names if not (name in seen or seen.add(name))]

    async def _checkouts(self, project_id: str, repository: Any) -> list[str]:
        """AQ's local checkouts for a project, the repository's own paths last."""
        paths: list[str] = []
        workspaces = await self.db.list_workspaces(project_id=project_id)
        for workspace in workspaces:
            path = workspace.workspace_path
            if path and path not in paths:
                paths.append(path)
        for path in (repository.checkout_base_path, repository.source_path):
            if path and path not in paths:
                paths.append(path)
        return [path for path in paths if Path(path).is_dir()]

    async def _observe(
        self, name: str, checkouts: Sequence[str], repository: Any
    ) -> tuple[str | None, dict[str, str], str | None]:
        """The exact remote head, the local tips, and the checkout to act from.

        The remote is read with ``ls-remote`` (never a remote-tracking ref) and
        the local tips from each checkout, so a stale fetch cannot decide
        anything.  ``ERROR`` is reported as no observation at all: absence and
        "could not ask" are not the same answer.
        """
        local_tips: dict[str, str] = {}
        remote_head: str | None = None
        for checkout in checkouts:
            tip = await self.git.arev_parse(checkout, f"refs/heads/{name}")
            if tip:
                local_tips[checkout] = tip
        for checkout in checkouts:
            observed = await self.git.als_remote_ref(
                checkout, name, repository_url=repository.url or None
            )
            if observed.state is RemoteRefState.PRESENT and observed.oid:
                remote_head = observed.oid
                break
        act_from = next(iter(local_tips), None) or (checkouts[0] if checkouts else None)
        return remote_head, local_tips, act_from

    async def _run_git(self, checkout: str, *args: str) -> str:
        result = await self.git.arun_git_result(list(args), cwd=checkout)
        if result.returncode:
            raise GitError(result.stderr or result.stdout or "Git command failed")
        return result.stdout.strip()

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups" / BACKUP_DIRNAME

    @staticmethod
    def _backoff(attempts: int) -> float:
        return min(RETRY_BASE_SECONDS * (2 ** max(attempts - 1, 0)), RETRY_MAX_SECONDS)

    # -- batch candidate refs --------------------------------------------

    async def abandon_batch_candidates(
        self,
        batch_id: str,
        *,
        reason: str = ABORT,
        detail: str | None = None,
        principal: str = "",
    ) -> dict[str, Any]:
        """Record an aborted or superseded batch's private candidate refs.

        A batch's *members* keep their branches: an unchanged member returns to
        pending and is collected again, so deleting one would throw away live
        work.  The refs a batch owns outright are its candidate head and the
        retained candidate it was built from, and those are what this records.
        """
        if reason not in {ABORT, SUPERSEDE}:
            raise ValueError("a batch's own refs are abandoned by abort or supersede only")
        async with self.db._engine.connect() as conn:
            batch = (
                (
                    await conn.execute(
                        select(integration_batches).where(integration_batches.c.id == batch_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if batch is None:
            return {"batch_id": batch_id, "state": "gone", "branches": []}
        from src.integration.batches import candidate_ref
        from src.integration.train_sources import RETAINED_CANDIDATE_PREFIX

        repository = await self.db.get_repo(batch["repository_id"])
        refs = [candidate_ref(batch_id), RETAINED_CANDIDATE_PREFIX + batch_id]
        why = detail or (f"{reason} by {principal}" if principal else None)
        outcomes = []
        for ref in refs:
            outcomes.append(
                await self._abandon_branch(
                    ref,
                    project_id=batch["project_id"],
                    repository_id=batch["repository_id"],
                    repository=repository,
                    task_id=None,
                    reason=reason,
                    detail=why,
                )
            )
        pending = sum(1 for row in outcomes if row["outcome"] in {"pending", "failed", "held"})
        return {
            "batch_id": batch_id,
            "state": "done" if not pending else "pending",
            "branches": outcomes,
        }

    async def member_source_refs(self, batch_id: str) -> dict[str, str]:
        """``source_ref -> task_id`` for a batch, for callers that must report holds."""
        async with self.db._engine.connect() as conn:
            return {
                str(source_ref): str(task_id)
                for task_id, source_ref in (
                    await conn.execute(
                        select(
                            integration_batch_members.c.task_id,
                            integration_batch_members.c.source_ref,
                        ).where(integration_batch_members.c.batch_id == batch_id)
                    )
                ).all()
                if source_ref
            }


def branch_abandon_for(orchestrator: Any) -> BranchAbandonService | None:
    """The daemon's abandon service, or ``None`` when it cannot be built."""
    db = getattr(orchestrator, "db", None)
    git = getattr(orchestrator, "git", None)
    config = getattr(orchestrator, "config", None)
    if db is None or git is None or config is None:
        return None
    return BranchAbandonService(db, data_dir=config.data_dir, git_manager=git)


__all__ = [
    "ABANDON_REASONS",
    "ABORT",
    "CANCEL",
    "DELETE",
    "FAIL_CLOSE",
    "OBSOLETE_CLOSE",
    "SUPERSEDE",
    "BranchAbandonService",
    "branch_abandon_for",
    "record_branch_deletion",
]
