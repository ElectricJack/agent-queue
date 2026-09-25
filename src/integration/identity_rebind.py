"""Rebind a live task that inherited a deleted predecessor's integration identity.

Before task naming reserved every identity integration history still keys
(``src/task_names.py`` ``_task_identity_exists``), a deleted task's name could
be minted again.  The new task then inherited the predecessor's
``task_branch_origins`` row and ``task_integration_checkpoints`` row: a
hierarchy close takes the checkpoint as proof the task produces the
predecessor's branch, and the ``integration.reused_task_identity`` doctor check
reports it.  ``aq integration rebind-reused-identity`` is the guarded repair
the design note ``docs/specs/design/integration-identity-diagnostics.md``
specifies:

* the *inherited origins* are the ones the doctor check reports -- unretired
  and created before the task -- and the *predecessor checkpoint* is the
  task's checkpoint only when it was last written before the task existed and
  binds the same repository and branch;
* live writers, an unreleased owner of the origin's ref, dependent
  integration history and hierarchy/train projects (where the origin is the
  task's live delivery identity) refuse;
* after one fetch every predecessor commit -- the origin's base, the
  checkpoint and verified SHAs, and the ref's current tip -- must be an
  ancestor of the default-branch tip, the ref may be gone, and anything else
  must be named exactly with ``--discard-tip``;
* ``--apply`` retires the origins (the rows stay), deletes the checkpoint
  after copying it verbatim into an ``integration.task_identity_rebound``
  event, and re-checks everything under the project hierarchy lock first.

No Git ref, owner row, ``task.deleted`` event or ``integration_owner_recoveries``
row is touched: that deletion evidence stays.  A dry run fetches but writes
nothing.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy import and_, delete, literal, or_, select, update

from src.database.queries.hierarchy_queries import HIERARCHY_MODES
from src.database.tables import (
    integration_batch_members,
    integration_branch_owners,
    integration_child_dispositions,
    integration_parent_episodes,
    integration_promotion_intents,
    integration_repair_operations,
    integration_review_evidence,
    integration_root_intent_members,
    projects,
    repos,
    sessions,
    task_branch_origins,
    task_delivery_receipts,
    task_integration_checkpoints,
    tasks,
    workspaces,
)
from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitError

logger = logging.getLogger(__name__)

EVENT = "integration.task_identity_rebound"

OUTCOMES = (
    "rebound",
    "would_rebind",
    "nothing_to_rebind",
    "unproven",
    "blocked",
    "changed",
    "invalid",
    "not_found",
)

#: Why an identity cannot be rebound.
LIVE_WRITER = "live_writer"
OWNER_HELD = "owner_held"
DEPENDENT_HISTORY = "dependent_history"
CHECKPOINT_REWRITTEN = "checkpoint_rewritten"
CHECKPOINT_MISMATCH = "checkpoint_mismatch"
HIERARCHICAL_PROJECT = "hierarchical_project"
BRANCH_UNRECORDED = "branch_unrecorded"

#: What one predecessor commit proved against the default-branch tip.
ON_DEFAULT_BRANCH = "on_default_branch"
NOT_ON_DEFAULT_BRANCH = "not_on_default_branch"
UNAVAILABLE = "unavailable"
ABSENT = "absent"
DISCARDED = "discarded"
_UNPROVEN_STATES = frozenset({NOT_ON_DEFAULT_BRANCH, UNAVAILABLE})

PROOF_LIMIT = (
    "A commit the predecessor pushed beyond its recorded checkpoint that later left "
    "the ref (branch deleted or force-pushed) is not observable on origin; this "
    "control neither proves nor discards it."
)

_LIVE_TASK_STATES = frozenset({"ASSIGNED", "IN_PROGRESS"})
_LIVE_SESSION_STATES = ("starting", "running", "draining")
_CHECKPOINT_HISTORY = (
    "episode_id",
    "current_verification_id",
    "last_completed_operation_id",
    "last_completed_verification_id",
)


class _Changed(RuntimeError):
    """The identity moved between its proof and the apply."""


class ReusedIdentityRebind:
    """Prove and rebind one task's inherited integration identity."""

    def __init__(self, db, *, development, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.development = development
        self.git = development.git
        self.clock = clock

    async def run(
        self,
        task_id: str,
        *,
        principal: str,
        dry_run: bool = True,
        expected_origin_ids: Iterable[str] = (),
        discard_tips: Iterable[str] = (),
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Report what the identity proves; rebind it when everything is settled."""
        expected = set(expected_origin_ids)
        discards = set(discard_tips)
        if not dry_run and not (expected and (reason or "").strip()):
            return _result(
                "invalid", task_id, dry_run,
                error="applying requires every inherited origin id and a reason",
            )
        async with self.db._engine.connect() as conn:
            identity = await _read(conn, task_id, lock=False)
            if identity is None:
                return _result("not_found", task_id, dry_run, error="task not found")
            if not identity["origins"]:
                return _result(
                    "nothing_to_rebind", task_id, dry_run,
                    project_id=identity["task"]["project_id"],
                    evidence=_evidence(identity),
                )
            blockers = await _blockers(conn, identity)

        # Git runs with no database connection held.
        try:
            proofs = await self._prove(identity)
        except _Blocked as exc:
            return _result(
                "blocked", task_id, dry_run,
                project_id=identity["task"]["project_id"],
                error=str(exc),
                evidence=_evidence(identity),
                unproven=tuple(_blocker_line(item) for item in blockers),
            )

        unproven_shas = {
            proof["sha"]
            for origin in proofs
            for proof in origin["proofs"]
            if proof["state"] in _UNPROVEN_STATES
        }
        stray = sorted(discards - unproven_shas)
        outcomes = _settle(identity, proofs, blockers, discards)
        report = {
            "project_id": identity["task"]["project_id"],
            "head_sha": proofs[0]["default_sha"],
            "outcomes": tuple(outcomes),
            "evidence": _evidence(identity),
        }
        if stray:
            return _result(
                "invalid", task_id, dry_run, **report,
                error=(
                    "--discard-tip names a commit this identity does not leave unproven: "
                    + ", ".join(stray)
                ),
            )
        unproven = tuple(_blocker_line(item) for item in blockers) + tuple(
            line for origin in outcomes for line in origin["unproven"]
        )
        if unproven:
            return _result(
                "unproven", task_id, dry_run, **report, unproven=unproven,
                error=f"{len(unproven)} item(s) cannot be proven; nothing was rebound",
            )
        origin_ids = {origin["id"] for origin in identity["origins"]}
        if dry_run:
            return _result("would_rebind", task_id, dry_run, **report, count=len(origin_ids))
        if expected != origin_ids:
            return _result(
                "changed", task_id, dry_run, **report,
                error=(
                    f"the inherited origins are {', '.join(sorted(origin_ids))}; "
                    f"the apply named {', '.join(sorted(expected))}"
                ),
            )
        try:
            event_id = await self._apply(
                identity, outcomes, principal=principal, reason=(reason or "").strip()
            )
        except _Changed as exc:
            return _result("changed", task_id, dry_run, **report, error=str(exc))
        await self._comment(
            task_id,
            "Integration identity rebound by `aq integration rebind-reused-identity` "
            f"({principal}): retired inherited origin(s) "
            + ", ".join(f"`{origin_id}`" for origin_id in sorted(origin_ids))
            + (
                " and removed the predecessor checkpoint"
                if identity["checkpoint"] is not None
                else ""
            )
            + f". Reason: {(reason or '').strip()}",
        )
        logger.info(
            "integration.task_identity_rebound task=%s origins=%s principal=%s",
            task_id,
            ",".join(sorted(origin_ids)),
            principal,
        )
        return _result(
            "rebound", task_id, dry_run, **report,
            count=len(origin_ids), id=str(event_id), reason=(reason or "").strip(),
        )

    # -- git proof --------------------------------------------------------------

    async def _prove(self, identity: dict[str, Any]) -> list[dict[str, Any]]:
        """Per inherited origin: every predecessor commit against the default tip."""
        from src.integration.development import DevelopmentBusy

        results: list[dict[str, Any]] = []
        for origin in identity["origins"]:
            repo = await self.db.get_repo(origin["repository_id"])
            if repo is None:
                raise _Blocked(f"repository {origin['repository_id']} is not configured")
            default_ref = "refs/heads/" + repo.default_branch.removeprefix("refs/heads/")
            candidates = _recorded_commits(origin, identity["checkpoint"])
            try:
                async with self.development.exclusion(repo.id):
                    store = await self.development.store(repo)
                    default_sha = await self.development.remote(store, default_ref)
                    if not default_sha:
                        raise _Blocked(f"{default_ref} is absent on origin")
                    tip = None
                    if origin["branch_name"]:
                        tip = await self.development.remote(
                            store, "refs/heads/" + origin["branch_name"]
                        )
                        if tip:
                            candidates.setdefault(tip, []).append("branch_tip")
                    proofs = []
                    for sha, sources in candidates.items():
                        on_default = await self.git.ais_ancestor(
                            str(store), sha, default_sha, strict=True
                        )
                        state = (
                            UNAVAILABLE
                            if on_default is None
                            else ON_DEFAULT_BRANCH
                            if on_default
                            else NOT_ON_DEFAULT_BRANCH
                        )
                        proofs.append({"sha": sha, "sources": sources, "state": state})
                    if origin["branch_name"] and not tip:
                        proofs.append({"sha": None, "sources": ["branch_tip"], "state": ABSENT})
            except DevelopmentBusy as exc:
                raise _Blocked(str(exc)) from exc
            except (GitError, GitHubAccessError, ValueError) as exc:
                raise _Blocked(f"origin could not be inspected: {exc}") from exc
            results.append(
                {
                    "origin_id": origin["id"],
                    "default_ref": default_ref,
                    "default_sha": default_sha,
                    "proofs": proofs,
                }
            )
        return results

    # -- apply ------------------------------------------------------------------

    async def _apply(
        self,
        identity: dict[str, Any],
        outcomes: list[dict[str, Any]],
        *,
        principal: str,
        reason: str,
    ) -> int:
        """Retire the origins and drop the checkpoint, re-proved under the lock."""
        task = identity["task"]
        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, task["project_id"])
            current = await _read(conn, task["id"], lock=True)
            if current is None or _snapshot(current) != _snapshot(identity):
                raise _Changed("the task's integration identity changed after the proof")
            blockers = await _blockers(conn, current)
            if blockers:
                raise _Changed(
                    "the identity can no longer be rebound: "
                    + "; ".join(_blocker_line(item) for item in blockers)
                )
            for origin in current["origins"]:
                retired = await conn.execute(
                    update(task_branch_origins)
                    .where(
                        task_branch_origins.c.id == origin["id"],
                        task_branch_origins.c.retired_at.is_(None),
                    )
                    .values(retired_at=now)
                )
                if retired.rowcount != 1:
                    raise _Changed(f"origin {origin['id']} changed after the proof")
            checkpoint = current["checkpoint"]
            if checkpoint is not None:
                removed = await conn.execute(
                    delete(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id == task["id"],
                        task_integration_checkpoints.c.version == checkpoint["version"],
                    )
                )
                if removed.rowcount != 1:
                    raise _Changed("the checkpoint changed after the proof")
            return await self.db.log_event(
                EVENT,
                project_id=task["project_id"],
                task_id=task["id"],
                payload=json.dumps(
                    {
                        **_evidence(current),
                        "origins": current["origins"],
                        "proofs": outcomes,
                        "discarded_tips": sorted(
                            proof["sha"]
                            for origin in outcomes
                            for proof in origin["proofs"]
                            if proof["state"] == DISCARDED
                        ),
                        "reason": reason,
                        "principal": principal,
                        "at": now,
                    },
                    sort_keys=True,
                    default=str,
                ),
                conn=conn,
            )

    async def _comment(self, task_id: str, body: str) -> None:
        try:
            await self.db.add_task_comment(
                task_id, body, author_kind="supervisor", author_id="identity-rebind"
            )
        except Exception:  # the event is the record; the note is a courtesy
            logger.debug("identity rebind: task comment failed", exc_info=True)


class _Blocked(RuntimeError):
    """Origin could not be inspected."""


async def _read(conn, task_id: str, *, lock: bool) -> dict[str, Any] | None:
    """The task, its inherited origins, checkpoint and the origins' owners."""

    def locked(statement):
        return statement.with_for_update() if lock else statement

    task = (
        await conn.execute(locked(select(tasks).where(tasks.c.id == task_id)))
    ).mappings().one_or_none()
    if task is None:
        return None
    task = dict(task)
    origins = [
        dict(row)
        for row in (
            await conn.execute(
                locked(
                    select(task_branch_origins)
                    .where(
                        task_branch_origins.c.task_id == task_id,
                        task_branch_origins.c.retired_at.is_(None),
                        task_branch_origins.c.created_at < task["created_at"],
                    )
                    .order_by(task_branch_origins.c.id)
                )
            )
        ).mappings()
    ]
    checkpoint = (
        await conn.execute(
            locked(
                select(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == task_id
                )
            )
        )
    ).mappings().one_or_none()
    owners = []
    for origin in origins:
        if not origin["branch_name"]:
            continue
        row = (
            await conn.execute(
                locked(
                    select(integration_branch_owners).where(
                        integration_branch_owners.c.repository_id == origin["repository_id"],
                        integration_branch_owners.c.ref == origin["branch_name"],
                    )
                )
            )
        ).mappings().one_or_none()
        if row is not None:
            owners.append(dict(row))
    # The task's project and every project owning an inherited origin's
    # repository: a deleted task of another project can leave the origin.
    modes = dict(
        (
            await conn.execute(
                select(projects.c.id, projects.c.hierarchical_integration_mode)
                .where(
                    or_(
                        projects.c.id == task["project_id"],
                        projects.c.id.in_(
                            select(repos.c.project_id).where(
                                repos.c.id.in_(
                                    sorted({origin["repository_id"] for origin in origins})
                                )
                            )
                        ),
                    )
                )
                .order_by(projects.c.id)
            )
        ).all()
    )
    return {
        "task": task,
        "modes": modes,
        "origins": origins,
        "checkpoint": dict(checkpoint) if checkpoint is not None else None,
        "owners": owners,
    }


async def _blockers(conn, identity: dict[str, Any]) -> list[dict[str, str]]:
    """Every database fact that forbids rebinding *identity*, with its cause."""
    task = identity["task"]
    task_id = task["id"]
    found: list[dict[str, str]] = []

    def refuse(cause: str, detail: str) -> None:
        found.append({"cause": cause, "detail": detail})

    for project_id, mode in identity["modes"].items():
        if mode in HIERARCHY_MODES:
            refuse(
                HIERARCHICAL_PROJECT,
                f"project {project_id} is in {mode} mode, where a branch origin is live "
                "delivery identity",
            )
    if task["status"] in _LIVE_TASK_STATES:
        refuse(LIVE_WRITER, f"task is {task['status']}")
    live_session = (
        await conn.execute(
            select(sessions.c.id)
            .where(sessions.c.task_id == task_id, sessions.c.state.in_(_LIVE_SESSION_STATES))
            .limit(1)
        )
    ).scalar_one_or_none()
    if live_session is not None:
        refuse(LIVE_WRITER, f"session {live_session} is live for the task")
    workspace = (
        await conn.execute(
            select(workspaces.c.id).where(workspaces.c.locked_by_task_id == task_id).limit(1)
        )
    ).scalar_one_or_none()
    if workspace is not None:
        refuse(LIVE_WRITER, f"workspace {workspace} is locked by the task")
    for owner in identity["owners"]:
        if owner["handoff_state"] != "released":
            refuse(
                OWNER_HELD,
                f"owner row {owner['id']} holds {owner['ref']} for {owner['owner_id']} "
                f"({owner['owner_role']}, {owner['handoff_state']})",
            )
    for origin in identity["origins"]:
        if not origin["branch_name"]:
            refuse(
                BRANCH_UNRECORDED,
                f"origin {origin['id']} records no branch, so its exact ref cannot be recovered",
            )
    checkpoint = identity["checkpoint"]
    if checkpoint is not None:
        if checkpoint["updated_at"] >= task["created_at"]:
            refuse(
                CHECKPOINT_REWRITTEN,
                "the checkpoint was written after the task was created, so it cannot be "
                "attributed to the predecessor",
            )
        if not any(
            checkpoint["repository_id"] == origin["repository_id"]
            and checkpoint["branch"] == origin["branch_name"]
            for origin in identity["origins"]
        ):
            refuse(
                CHECKPOINT_MISMATCH,
                f"the checkpoint binds {checkpoint['repository_id']}:{checkpoint['branch']}, "
                "not an inherited origin",
            )
        pointers = [column for column in _CHECKPOINT_HISTORY if checkpoint[column]]
        if pointers:
            refuse(DEPENDENT_HISTORY, f"the checkpoint names {', '.join(pointers)}")
    for label, table, condition in _history(task_id):
        present = (
            await conn.execute(select(literal(1)).select_from(table).where(condition).limit(1))
        ).scalar_one_or_none()
        if present is not None:
            refuse(DEPENDENT_HISTORY, f"{label} reference the task")
    return found


def _history(task_id: str):
    """Integration history that depends on the task's identity."""
    return (
        (
            "live child origins",
            task_branch_origins,
            and_(
                task_branch_origins.c.parent_task_id == task_id,
                task_branch_origins.c.retired_at.is_(None),
            ),
        ),
        (
            "parent episodes",
            integration_parent_episodes,
            integration_parent_episodes.c.parent_task_id == task_id,
        ),
        (
            "delivery receipts",
            task_delivery_receipts,
            or_(
                task_delivery_receipts.c.source_task_id == task_id,
                task_delivery_receipts.c.target_task_id == task_id,
            ),
        ),
        (
            "integration batch members",
            integration_batch_members,
            integration_batch_members.c.task_id == task_id,
        ),
        (
            "promotion intents",
            integration_promotion_intents,
            or_(
                integration_promotion_intents.c.source_task_id == task_id,
                integration_promotion_intents.c.target_task_id == task_id,
            ),
        ),
        (
            "root intent members",
            integration_root_intent_members,
            integration_root_intent_members.c.source_task_id == task_id,
        ),
        (
            "repair operations",
            integration_repair_operations,
            or_(
                integration_repair_operations.c.parent_task_id == task_id,
                integration_repair_operations.c.verifier_task_id == task_id,
            ),
        ),
        (
            "child dispositions",
            integration_child_dispositions,
            or_(
                integration_child_dispositions.c.parent_task_id == task_id,
                integration_child_dispositions.c.child_task_id == task_id,
            ),
        ),
        (
            "review evidence rows",
            integration_review_evidence,
            integration_review_evidence.c.source_task_id == task_id,
        ),
    )


def _recorded_commits(
    origin: dict[str, Any], checkpoint: dict[str, Any] | None
) -> dict[str, list[str]]:
    """The predecessor commits the database recorded for *origin*, sha -> sources."""
    commits: dict[str, list[str]] = {origin["base_sha"]: ["origin_base"]}
    if (
        checkpoint is not None
        and checkpoint["repository_id"] == origin["repository_id"]
        and checkpoint["branch"] == origin["branch_name"]
    ):
        for column, source in (("checkpoint_sha", "checkpoint"), ("verified_sha", "verified")):
            if checkpoint[column]:
                commits.setdefault(checkpoint[column], []).append(source)
    return commits


def _settle(
    identity: dict[str, Any],
    proofs: list[dict[str, Any]],
    blockers: list[dict[str, str]],
    discards: set[str],
) -> list[dict[str, Any]]:
    """Apply the named discards and describe each origin for the report."""
    by_id = {origin["id"]: origin for origin in identity["origins"]}
    settled = []
    for proof in proofs:
        origin = by_id[proof["origin_id"]]
        items = []
        unproven = []
        for item in proof["proofs"]:
            item = dict(item)
            if item["state"] in _UNPROVEN_STATES and item["sha"] in discards:
                item["observed"] = item["state"]
                item["state"] = DISCARDED
            elif item["state"] in _UNPROVEN_STATES:
                where = (
                    f"is not on {proof['default_ref']} at {proof['default_sha']}"
                    if item["state"] == NOT_ON_DEFAULT_BRANCH
                    else "is not in the fetched repository"
                )
                unproven.append(
                    f"{item['state']}: {origin['branch_name']} "
                    f"({'/'.join(item['sources'])}) {item['sha']} {where}; "
                    f"abandon it explicitly with --discard-tip {item['sha']}"
                )
            items.append(item)
        settled.append(
            {
                "id": origin["id"],
                "repository_id": origin["repository_id"],
                "branch": origin["branch_name"],
                "base_sha": origin["base_sha"],
                "created_at": origin["created_at"],
                "default_ref": proof["default_ref"],
                "default_sha": proof["default_sha"],
                "proofs": items,
                "unproven": unproven,
            }
        )
    return settled


def _evidence(identity: dict[str, Any]) -> dict[str, Any]:
    task = identity["task"]
    return {
        "task": {
            key: task[key]
            for key in ("id", "project_id", "status", "created_at", "repo_id", "branch_name")
        },
        "modes": identity["modes"],
        "inherited_origin_ids": [origin["id"] for origin in identity["origins"]],
        "checkpoint": identity["checkpoint"],
        "owners": identity["owners"],
        "proof_limit": PROOF_LIMIT,
    }


def _snapshot(identity: dict[str, Any]) -> str:
    """What the proof relied on, compared again under the lock."""
    task = identity["task"]
    return json.dumps(
        {
            "task": {key: task[key] for key in ("created_at", "status", "project_id")},
            "modes": identity["modes"],
            "origins": identity["origins"],
            "checkpoint": identity["checkpoint"],
            "owners": identity["owners"],
        },
        sort_keys=True,
        default=str,
    )


def _blocker_line(item: dict[str, str]) -> str:
    return f"{item['cause']}: {item['detail']}"


def _result(outcome: str, task_id: str, dry_run: bool, **values: Any) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "task_id": task_id,
        "dry_run": dry_run,
        **({"count": 0, "outcomes": (), "unproven": ()} | values),
    }


def reused_identity_rebind_for(handler: Any) -> ReusedIdentityRebind | None:
    """The command handler's rebind service, or ``None`` without a DB and Git."""
    db = getattr(handler, "db", None)
    if db is None or getattr(getattr(handler, "orchestrator", None), "git", None) is None:
        return None
    return ReusedIdentityRebind(db, development=handler._development_integration())
