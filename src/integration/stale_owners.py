"""Release the stale integration owners a project's drain is waiting on.

``aq integration release-stale-owners`` (``integration_release_stale_owners``)
is the bulk, project-scoped form of ``release-owner`` for the rows that
command cannot take: ``reserved`` branch-owner rows, which name no writer,
left behind when a train run's tasks finished, were archived or were deleted.
Every ``integration_branch_owners`` row that is not ``released`` counts as
active integration work, so a requested drain (``aq integration enable
--mode disabled``) never completes while one remains.

A row is released only when it is provably safe:

* it is ``reserved`` and names no session or workspace.  An ``attached`` or
  ``handoff_pending`` row is reported: it names a writer, and
  ``release-owner`` owns it, with its stop proof and preservation;
* its owner is finished -- a task that is COMPLETED or FAILED, archived or
  deleted, or an integration operation that ended -- and nothing still acts
  for it: no live session, workspace lock, in-flight candidate ref mutation
  or running operation, and no hierarchy/train project owns the row
  (:func:`src.integration.finished_owners._blocker`, the questions the
  ``integration.finished_branch_owners`` doctor check asks);
* nothing still relies on its fence: no active integration batch lists the
  branch and no unsettled promotion intent names it.  A promoted batch whose
  cleanup is pending keeps its integration branch's owner, because cleanup of
  that local ref checks the owner's fence -- run ``retry-cleanup`` first.  Its
  members' rows are not kept: their work is on the default branch, and
  cleanup refuses to delete a source ref whose owner is not released;
* its work is on origin, proved after one ``git fetch --prune`` of the
  repository: the branch tip is an ancestor of the default branch, or the
  branch is gone and its owner is terminal (COMPLETED, archived, deleted, or
  an ended operation -- a FAILED task may be retried, so a gone branch proves
  nothing for it); and no worktree or local branch holds work origin lacks.

Everything else is reported with its reason and left alone.  A release is the
compare-and-swap ``release-owner`` makes: ``released`` with a fresh fence, one
``integration_owner_recoveries`` audit row naming the principal, an
``integration.owner_recovered`` event and a note on the owning task.  A dry
run fetches but writes nothing.  A released row is never selected again, so
the command is idempotent.

It also drops the project's integration lease when the lease has expired and
its batch is finished (``integration.stale_lease_released``): the drain counts
a lease row however long ago it expired.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import and_, delete, or_, select, update

from src.database.tables import (
    archived_tasks,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_promotion_intents,
    integration_repair_operations,
    project_integration_leases,
    projects,
    repos,
    tasks,
)
from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitError
from src.integration.delegate_release import ENDED_OPERATION_STATES
from src.integration.delivery_branches import (
    ACTIVE_BATCH_LIFECYCLES,
    SETTLED_INTENT_STATES,
    branch_of,
)
from src.integration.finished_owners import _blocker as _finished_owner_blocker
from src.integration.owner_recovery import (
    COLLECTOR_ROLE,
    NOT_ELIGIBLE,
    ORIGIN_UNREACHABLE,
    RECOVERED_EVENT,
    RELEASED,
    STALE_FENCE,
    OwnerRecovery,
    RecoveryOutcome,
    _project_for,
    _project_ids,
    _Refusal,
)

#: The ``principal``-bearing event written for each released lease.
LEASE_RELEASED_EVENT = "integration.stale_lease_released"

#: Why a row was released.
TIP_ON_DEFAULT_BRANCH = "tip_on_default_branch"
REF_GONE = "ref_gone"

#: Why a row or lease was kept.
NOT_RESERVED = "not_reserved"
TOO_RECENT = "too_recent"
OWNER_ACTIVE = "owner_active"
OWNER_UNKNOWN = "owner_unknown"
OWNER_BLOCKED = "owner_blocked"
BATCH_ACTIVE = "batch_active"
BATCH_CLEANUP_PENDING = "batch_cleanup_pending"
PROMOTION_UNSETTLED = "promotion_unsettled"
CHECKOUT_IN_USE = "checkout_in_use"
LOCAL_WORK_UNPUBLISHED = "local_work_unpublished"
INSPECTION_FAILED = "inspection_failed"
BRANCH_NOT_ON_DEFAULT = "branch_not_on_default"
FAILED_OWNER_REF_GONE = "failed_owner_ref_gone"
LEASE_LIVE = "lease_live"

#: Owner states that end the owner's claim on its branch.
_FINISHED = frozenset(
    {"COMPLETED", "FAILED", "archived", "deleted"}
    | {f"operation:{state}" for state in ENDED_OPERATION_STATES}
)
#: Finished owners whose gone branch proves the work is not wanted back.
_TERMINAL = _FINISHED - {"FAILED"}


class StaleOwnerRelease(OwnerRecovery):
    """Release a project's provably safe ``reserved`` owners and its expired lease."""

    def __init__(self, db, git, git_mutex, *, clock: Callable[[], float] = time.time) -> None:
        # No stop probe: a reserved row names no writer to prove gone.
        super().__init__(db, git, git_mutex, confirm_stopped=None, clock=clock)

    async def run(
        self,
        project_id: str,
        *,
        principal: str,
        dry_run: bool = False,
        older_than_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Report every unreleased owner of *project_id*; release the safe ones."""
        now = self.clock()
        async with self.db._engine.connect() as conn:
            exists = (
                await conn.execute(select(projects.c.id).where(projects.c.id == project_id))
            ).scalar_one_or_none()
            if exists is None:
                return {"outcome": "not_found", "project_id": project_id}
            rows = [
                dict(row)
                for row in (
                    await conn.execute(
                        select(integration_branch_owners)
                        .select_from(
                            integration_branch_owners.join(
                                repos, repos.c.id == integration_branch_owners.c.repository_id
                            )
                        )
                        .where(
                            repos.c.project_id == project_id,
                            integration_branch_owners.c.handoff_state != "released",
                        )
                        .order_by(
                            integration_branch_owners.c.repository_id,
                            integration_branch_owners.c.ref,
                        )
                    )
                ).mappings()
            ]
            verdicts: dict[str, dict[str, Any]] = {}
            eligible: list[dict[str, Any]] = []
            cutoff = None if older_than_seconds is None else now - older_than_seconds
            for row in rows:
                if row["handoff_state"] != "reserved":
                    verdicts[row["id"]] = _kept(
                        NOT_RESERVED,
                        f"a {row['handoff_state']} row names a writer; use "
                        f"`aq integration release-owner --owner-row-id {row['id']}`",
                    )
                elif cutoff is not None and row["updated_at"] > cutoff:
                    verdicts[row["id"]] = _kept(
                        TOO_RECENT, "changed more recently than --older-than"
                    )
                else:
                    try:
                        row["owner_status"] = await _row_blocker(conn, row, lock=False)
                    except _Refusal as refusal:
                        verdicts[row["id"]] = _kept(refusal.reason, refusal.detail)
                    else:
                        eligible.append(row)

        # Git runs with no database connection held.
        by_repository: dict[str, list[dict[str, Any]]] = {}
        for row in eligible:
            by_repository.setdefault(row["repository_id"], []).append(row)
        proofs: dict[str, tuple[str, str]] = {}
        for repository_id, repository_rows in by_repository.items():
            try:
                proofs.update(await self._branch_proofs(repository_id, repository_rows))
            except _Refusal as refusal:
                for row in repository_rows:
                    verdicts[row["id"]] = _kept(refusal.reason, refusal.detail)

        for row in eligible:
            if row["id"] in verdicts:
                continue
            proof, detail = proofs[row["id"]]
            if proof == REF_GONE and row["owner_status"] not in _TERMINAL:
                verdicts[row["id"]] = _kept(
                    FAILED_OWNER_REF_GONE,
                    f"{row['ref']} is gone from origin, but its owner {row['owner_id']} is "
                    f"{row['owner_status']} and may be retried",
                )
            elif proof not in (TIP_ON_DEFAULT_BRANCH, REF_GONE):
                verdicts[row["id"]] = _kept(proof, detail)
            elif dry_run:
                verdicts[row["id"]] = {"outcome": RELEASED, "reason": proof, "detail": detail}
            else:
                verdicts[row["id"]] = await self._release_stale(
                    row, proof, detail, principal=principal
                )

        outcomes = [
            {
                "owner_row_id": row["id"],
                "repository_id": row["repository_id"],
                "ref": row["ref"],
                "owner_id": row["owner_id"],
                "owner_role": row["owner_role"],
                "handoff_state": row["handoff_state"],
                **verdicts[row["id"]],
                "dry_run": dry_run,
            }
            for row in rows
        ]
        leases = await self._stale_leases(project_id, principal=principal, dry_run=dry_run)
        released = sum(item["outcome"] == RELEASED for item in outcomes) + sum(
            item["outcome"] == RELEASED for item in leases
        )
        return {
            "outcome": "released" if released else "nothing_to_release",
            "project_id": project_id,
            "dry_run": dry_run,
            "count": released,
            "outcomes": outcomes,
            "leases": leases,
        }

    # -- git proof --------------------------------------------------------------

    async def _branch_proofs(
        self, repository_id: str, rows: list[dict[str, Any]]
    ) -> dict[str, tuple[str, str]]:
        """``{row id: (proof or refusal reason, detail)}`` from one pruned fetch."""
        checkout, default_branch, _row_path, repository_url = await self._checkout_for(
            {"repository_id": repository_id, "workspace_id": None}
        )
        if checkout is None:
            raise _Refusal(
                ORIGIN_UNREACHABLE,
                f"no checkout of repository {repository_id} exists on this host, so its "
                "branches cannot be compared with origin",
            )
        live_dirs = await self._live_work_dirs()
        proofs: dict[str, tuple[str, str]] = {}
        async with self._mutex(checkout):
            try:
                await self.git.afetch_origin(
                    checkout, repository_url=repository_url, lock_held=True, all_heads=True
                )
                refs = await self._refs(checkout)
                worktrees = await self.git.aworktree_list(checkout)
            except (GitError, GitHubAccessError) as exc:
                raise _Refusal(ORIGIN_UNREACHABLE, f"git fetch origin failed: {exc}") from exc
            default_ref = f"refs/remotes/origin/{default_branch}"
            if default_ref not in refs:
                raise _Refusal(ORIGIN_UNREACHABLE, f"origin has no default branch {default_branch}")
            for row in rows:
                proofs[row["id"]] = await self._prove(
                    checkout, row, refs, default_branch, worktrees, live_dirs
                )
        return proofs

    async def _refs(self, checkout: str) -> dict[str, str]:
        listing = await self.git._arun(
            [
                "for-each-ref",
                "--format=%(objectname) %(refname)",
                "refs/remotes/origin/",
                "refs/heads/",
            ],
            cwd=checkout,
        )
        refs = {}
        for line in listing.splitlines():
            sha, _, name = line.strip().partition(" ")
            if sha and name:
                refs[name] = sha
        return refs

    async def _prove(
        self,
        checkout: str,
        row: dict[str, Any],
        refs: dict[str, str],
        default_branch: str,
        worktrees: list[dict[str, Any]],
        live_dirs: set[str],
    ) -> tuple[str, str]:
        branch = branch_of(row["ref"]) or ""
        origin_sha = refs.get(f"refs/remotes/origin/{branch}")
        try:
            for entry in worktrees:
                path = entry["path"]
                if entry.get("branch") != branch or not os.path.isdir(path):
                    continue
                if os.path.realpath(path) in live_dirs:
                    return CHECKOUT_IN_USE, (
                        f"{branch} is checked out at {path}, where a live session works"
                    )
                head = entry.get("head")
                if await self._dirty(path) or (
                    head and not await self._on_origin(checkout, head, origin_sha, default_branch)
                ):
                    return LOCAL_WORK_UNPUBLISHED, (
                        f"{branch} is checked out at {path} with work origin does not carry"
                    )
            local_sha = refs.get(f"refs/heads/{branch}")
            if local_sha and not await self._on_origin(
                checkout, local_sha, origin_sha, default_branch
            ):
                return LOCAL_WORK_UNPUBLISHED, (
                    f"local {branch} at {local_sha} is on neither origin/{branch} nor "
                    f"origin/{default_branch}"
                )
        except GitError as exc:
            return INSPECTION_FAILED, f"inspecting {branch}: {exc}"
        if origin_sha is None:
            return REF_GONE, f"{branch} no longer exists on origin"
        on_default = await self.git.ais_ancestor(
            checkout, origin_sha, f"refs/remotes/origin/{default_branch}", strict=True
        )
        if on_default is None:
            return ORIGIN_UNREACHABLE, f"could not compare {branch} with {default_branch}"
        if not on_default:
            return BRANCH_NOT_ON_DEFAULT, (
                f"origin/{branch} at {origin_sha} is not on origin/{default_branch}"
            )
        return TIP_ON_DEFAULT_BRANCH, f"origin/{branch} at {origin_sha} is on {default_branch}"

    # -- release ----------------------------------------------------------------

    async def _release_stale(
        self, row: dict[str, Any], proof: str, detail: str, *, principal: str
    ) -> dict[str, Any]:
        """The ``release-owner`` compare-and-swap, re-proved under the project lock."""
        owners = integration_branch_owners
        now = self.clock()
        async with self.db.immediate() as conn:
            for project_id in await _project_ids(conn, row["repository_id"]):
                await self.db.lock_hierarchy_project(conn, project_id)
            current = (
                (
                    await conn.execute(
                        select(owners).where(owners.c.id == row["id"]).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is None or any(
                current[field] != row[field]
                for field in ("fence_token", "handoff_state", "owner_id", "owner_role", "ref")
            ):
                return _kept(STALE_FENCE, f"owner row {row['id']} changed after the check")
            try:
                owner_status = await _row_blocker(conn, dict(current), lock=True)
            except _Refusal as refusal:
                return _kept(refusal.reason, refusal.detail)
            if proof == REF_GONE and owner_status not in _TERMINAL:
                return _kept(FAILED_OWNER_REF_GONE, f"owner {row['owner_id']} is {owner_status}")
            swapped = await conn.execute(
                update(owners)
                .where(
                    owners.c.id == row["id"],
                    owners.c.fence_token == row["fence_token"],
                    owners.c.handoff_state == "reserved",
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
            if swapped.rowcount != 1:
                return _kept(STALE_FENCE, f"owner row {row['id']} changed after the check")
            evidence = {
                "control": "release_stale_owners",
                "handoff_state": "reserved",
                "fence_token": int(row["fence_token"]),
                "released_fence_token": int(row["fence_token"]) + 1,
                "owner_status": owner_status,
                "proof": proof,
                "detail": detail,
            }
            result = RecoveryOutcome(row["id"], RELEASED, None, evidence)
            await self._audit(conn, dict(current), result, principal, now)
            await self.db.log_event(
                RECOVERED_EVENT,
                project_id=await _project_for(conn, dict(current)),
                task_id=None if row["owner_role"] == COLLECTOR_ROLE else row["owner_id"],
                payload=json.dumps(
                    {**result.to_dict(), "principal": principal}, sort_keys=True, default=str
                ),
                conn=conn,
            )
        await self._comment(
            row,
            f"Integration owner `{row['id']}` of `{row['ref']}` was released by "
            f"`aq integration release-stale-owners` ({principal}): {detail}.",
        )
        return {
            "outcome": RELEASED,
            "reason": proof,
            "detail": detail,
            "released_fence_token": int(row["fence_token"]) + 1,
        }

    # -- the project lease --------------------------------------------------------

    async def _stale_leases(
        self, project_id: str, *, principal: str, dry_run: bool
    ) -> list[dict[str, Any]]:
        """Release the project's lease when it expired and its batch is finished."""
        leases = project_integration_leases
        now = self.clock()
        async with self.db.immediate() as conn:
            if not dry_run:
                await self.db.lock_hierarchy_project(conn, project_id)
            lease = (
                (
                    await conn.execute(
                        select(leases).where(leases.c.project_id == project_id).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if lease is None:
                return []
            batch = (
                await conn.execute(
                    select(
                        integration_batches.c.lifecycle, integration_batches.c.cleanup_state
                    ).where(integration_batches.c.id == lease["batch_id"])
                )
            ).one_or_none()
            item = {
                "project_id": project_id,
                "batch_id": lease["batch_id"],
                "owner_id": lease["owner_id"],
                "fence_token": int(lease["fence_token"]),
                "expires_at": lease["expires_at"],
                "batch_lifecycle": batch.lifecycle if batch is not None else None,
                "dry_run": dry_run,
            }
            if lease["expires_at"] > now:
                return [{**item, **_kept(LEASE_LIVE, "the lease has not expired")}]
            if batch is not None and (
                batch.lifecycle in ACTIVE_BATCH_LIFECYCLES
                or (batch.lifecycle == "promoted" and batch.cleanup_state != "complete")
            ):
                return [
                    {
                        **item,
                        **_kept(
                            BATCH_ACTIVE,
                            f"batch {lease['batch_id']} is {batch.lifecycle} "
                            f"(cleanup {batch.cleanup_state})",
                        ),
                    }
                ]
            finished = (
                f"batch {lease['batch_id']} is "
                f"{batch.lifecycle if batch is not None else 'gone'} and the lease expired"
            )
            if dry_run:
                return [
                    {**item, "outcome": RELEASED, "reason": "lease_expired", "detail": finished}
                ]
            deleted = await conn.execute(
                delete(leases).where(
                    leases.c.project_id == project_id,
                    leases.c.batch_id == lease["batch_id"],
                    leases.c.owner_id == lease["owner_id"],
                    leases.c.fence_token == lease["fence_token"],
                )
            )
            if deleted.rowcount != 1:
                return [{**item, **_kept(STALE_FENCE, "the lease changed after the check")}]
            released = {**item, "outcome": RELEASED, "reason": "lease_expired", "detail": finished}
            await self.db.log_event(
                LEASE_RELEASED_EVENT,
                project_id=project_id,
                payload=json.dumps(
                    {**released, "principal": principal}, sort_keys=True, default=str
                ),
                conn=conn,
            )
        return [released]


def stale_owner_release_for(orchestrator: Any) -> StaleOwnerRelease | None:
    """The daemon's :class:`StaleOwnerRelease`, or ``None`` without a DB and Git."""
    db = getattr(orchestrator, "db", None)
    git = getattr(orchestrator, "git", None)
    if db is None or git is None:
        return None
    return StaleOwnerRelease(db, git, getattr(orchestrator, "_git_mutex", None))


def _kept(reason: str, detail: str) -> dict[str, Any]:
    return {"outcome": NOT_ELIGIBLE, "reason": reason, "detail": detail}


async def _row_blocker(conn, row: dict[str, Any], *, lock: bool) -> str:
    """The owner's state when nothing in the database keeps *row*; else refuse."""
    if row["session_id"] or row["workspace_id"]:
        raise _Refusal(
            NOT_RESERVED,
            "a reserved row that still names a session or workspace is not detached; use "
            f"`aq integration release-owner --owner-row-id {row['id']}`",
        )
    owner_status = await _owner_status(conn, row, lock=lock)
    if owner_status is None:
        raise _Refusal(
            OWNER_UNKNOWN,
            f"{row['owner_role']} owner {row['owner_id']} is neither a task nor an operation",
        )
    if owner_status not in _FINISHED:
        raise _Refusal(OWNER_ACTIVE, f"owner {row['owner_id']} is {owner_status}")
    integrating = (
        await conn.execute(
            select(
                projects.c.id,
                projects.c.hierarchical_integration_mode,
                projects.c.hierarchical_integration_desired_mode,
            ).where(projects.c.integration_repository_id == row["repository_id"])
        )
    ).all()
    blocker = await _finished_owner_blocker(conn, row, integrating)
    if blocker is not None:
        raise _Refusal(OWNER_BLOCKED, blocker)
    await _fence_blocker(conn, row)
    return owner_status


async def _owner_status(conn, row: dict[str, Any], *, lock: bool) -> str | None:
    """``COMPLETED``… for a task, ``archived``, ``operation:<state>``, ``deleted``."""
    owner_id = row["owner_id"]
    query = select(tasks.c.status).where(tasks.c.id == owner_id)
    status = (await conn.execute(query.with_for_update() if lock else query)).scalar_one_or_none()
    if status is not None:
        return str(status)
    if (
        await conn.execute(select(archived_tasks.c.id).where(archived_tasks.c.id == owner_id))
    ).scalar_one_or_none() is not None:
        return "archived"
    state = (
        await conn.execute(
            select(integration_repair_operations.c.state).where(
                integration_repair_operations.c.id == owner_id
            )
        )
    ).scalar_one_or_none()
    if state is not None:
        return f"operation:{state}"
    # A worker or repair row's owner is a task: one in neither task table was
    # deleted.  A collector's owner is an operation, which is never deleted,
    # so an unknown one is not evidence of anything.
    return None if row["owner_role"] == COLLECTOR_ROLE else "deleted"


async def _fence_blocker(conn, row: dict[str, Any]) -> None:
    """Refuse while a batch or promotion intent still relies on the row's fence."""
    branch = branch_of(row["ref"])
    batches = (
        await conn.execute(
            select(
                integration_batches.c.id,
                integration_batches.c.lifecycle,
                integration_batches.c.cleanup_state,
                integration_batches.c.integration_branch,
            ).where(
                integration_batches.c.repository_id == row["repository_id"],
                or_(
                    integration_batches.c.lifecycle.in_(ACTIVE_BATCH_LIFECYCLES),
                    and_(
                        integration_batches.c.lifecycle == "promoted",
                        integration_batches.c.cleanup_state != "complete",
                    ),
                ),
            )
        )
    ).all()
    for batch in batches:
        active = batch.lifecycle in ACTIVE_BATCH_LIFECYCLES
        if branch_of(batch.integration_branch) == branch:
            if active:
                raise _Refusal(BATCH_ACTIVE, f"batch {batch.id} is {batch.lifecycle}")
            raise _Refusal(
                BATCH_CLEANUP_PENDING,
                f"promoted batch {batch.id} has cleanup {batch.cleanup_state}, and cleanup of "
                f"its integration branch checks this owner's fence; run `aq integration "
                f"retry-cleanup {batch.id}` first",
            )
        if not active:
            continue
        members = (
            await conn.execute(
                select(
                    integration_batch_members.c.task_id, integration_batch_members.c.source_ref
                ).where(integration_batch_members.c.batch_id == batch.id)
            )
        ).all()
        if any(
            branch in (branch_of(member.source_ref), f"aq/{member.task_id}") for member in members
        ):
            raise _Refusal(BATCH_ACTIVE, f"batch {batch.id} is {batch.lifecycle}")
    intents = (
        await conn.execute(
            select(
                integration_promotion_intents.c.id,
                integration_promotion_intents.c.state,
                integration_promotion_intents.c.target_branch,
                integration_promotion_intents.c.recovery_ref,
            ).where(
                integration_promotion_intents.c.repository_id == row["repository_id"],
                integration_promotion_intents.c.state.not_in(SETTLED_INTENT_STATES),
            )
        )
    ).all()
    for intent in intents:
        if branch in (branch_of(intent.target_branch), branch_of(intent.recovery_ref)):
            raise _Refusal(PROMOTION_UNSETTLED, f"promotion intent {intent.id} is {intent.state}")


__all__ = [
    "LEASE_RELEASED_EVENT",
    "REF_GONE",
    "TIP_ON_DEFAULT_BRANCH",
    "StaleOwnerRelease",
    "stale_owner_release_for",
]
