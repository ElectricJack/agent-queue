"""Diagnose completed child delivery and support the shadow collector.

Active Git-first redrive observes current parent containment and leaves delivery
with the train. Shadow collection proves the child's published checkpoint head
and origin ancestry, then records approved leaf evidence. Rejected heads and
open reviewers retain authority; failed proof backs off without overriding them.
``ensure_evidence`` serves the collector and ``run`` serves the supervisor's
``redrive-child``.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import exists, insert, select

from src.database.queries.hierarchy_queries import HierarchyError
from src.database.queries.task_queries import INTEGRATION_REWORK_AT_KEY
from src.database.tables import (
    integration_branch_owners,
    integration_promotion_intents,
    integration_repair_operations,
    integration_review_evidence,
    projects,
    repos,
    sessions,
    task_branch_origins,
    task_completion_records,
    task_delivery_receipts,
    task_dependencies,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)
from src.git.manager import GitError, RemoteRefState, bounded_git_reads, safe_git_detail
from src.integration.promotion_contracts import PromotionError
from src.models import TaskStatus

logger = logging.getLogger(__name__)

_EVIDENCE_NAMESPACE = uuid.UUID("5d7c3f0e-9b1a-4c55-8d2e-2f6a8b7e4c19")

#: ``reviewer_identity`` of evidence the collector records on its own.
COMPLETION_IDENTITY_PREFIX = "completion:"

#: Event written once per applied redrive.
REDRIVE_EVENT = "integration.child_redriven"

MANAGED_MODES = ("hierarchy", "train")
_COLLECTING_OPERATION_STATES = ("active", "escalated")
_REVIEW_PROFILES = ("reviewer", "final-reviewer")
_FINISHED_STATUSES = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
#: A promotion intent in any other state is still being written or repaired.
_SETTLED_INTENT_STATES = ("committed", "superseded")

Collect = Callable[[str], Awaitable[str | None]]


class _ProofFailed(Exception):
    """Git does not show the recorded head as the child's published tip."""

    def __init__(self, reason: str, remote_head: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.remote_head = remote_head


def _snapshot_identity(snapshot: dict[str, Any]) -> tuple:
    return tuple(
        snapshot.get(key)
        for key in (
            "status", "parent_task_id", "repository_id", "branch", "base_sha", "head_sha",
            "generation", "episode_id", "has_children", "origin_parent_task_id",
            "origin_materialized", "origin_reserved", "completion_id", "rework_at",
        )
    )


async def _snapshot_on(conn, task_id: str) -> dict[str, Any] | None:
    """Everything that decides whether *task_id* can be assembled; reads only."""
    task = (
        await conn.execute(select(tasks).where(tasks.c.id == task_id))
    ).mappings().one_or_none()
    if task is None:
        return None
    _delivery_query = select(
        projects.c.hierarchical_integration_mode, projects.c.integration_repository_id
    ).where(projects.c.id == task["project_id"])
    project = (await conn.execute(_delivery_query)).mappings().one_or_none()
    _delivery_query = select(task_integration_checkpoints).where(
        task_integration_checkpoints.c.task_id == task_id
    )
    checkpoint = (await conn.execute(_delivery_query)).mappings().one_or_none()
    _delivery_query = select(task_branch_origins).where(
        task_branch_origins.c.task_id == task_id,
        task_branch_origins.c.repository_id == task["repo_id"],
        task_branch_origins.c.retired_at.is_(None),
    )
    origin = (await conn.execute(_delivery_query)).mappings().one_or_none()
    has_children = await conn.scalar(select(exists().where(tasks.c.parent_task_id == task_id)))
    _delivery_query = (
        select(task_completion_records.c.id, task_completion_records.c.completed_at)
        .where(task_completion_records.c.task_id == task_id)
        .order_by(
            task_completion_records.c.completed_at.desc(), task_completion_records.c.id.desc()
        )
        .limit(1)
    )
    completion = (await conn.execute(_delivery_query)).mappings().one_or_none()
    _delivery_query = select(task_metadata.c.value).where(
        task_metadata.c.task_id == task_id, task_metadata.c.key == INTEGRATION_REWORK_AT_KEY
    )
    rework_value = (await conn.execute(_delivery_query)).scalar_one_or_none()
    _delivery_query = select(task_metadata.c.task_id).where(
        task_metadata.c.task_id == task_id, task_metadata.c.key == "object_experiment"
    )
    object_experiment = (await conn.execute(_delivery_query)).scalar_one_or_none()
    parent = parent_checkpoint = operation = None
    if task["parent_task_id"] is not None:
        _delivery_query = select(
            tasks.c.id, tasks.c.status, tasks.c.branch_name, tasks.c.repo_id
        ).where(tasks.c.id == task["parent_task_id"])
        parent = (await conn.execute(_delivery_query)).mappings().one_or_none()
        _delivery_query = select(
            task_integration_checkpoints.c.state, task_integration_checkpoints.c.episode_id
        ).where(task_integration_checkpoints.c.task_id == task["parent_task_id"])
        parent_checkpoint = (await conn.execute(_delivery_query)).mappings().one_or_none()
        if parent_checkpoint is not None and parent_checkpoint["episode_id"] is not None:
            _delivery_query = select(
                integration_repair_operations.c.id, integration_repair_operations.c.state
            ).where(
                integration_repair_operations.c.parent_task_id == task["parent_task_id"],
                integration_repair_operations.c.episode_id == parent_checkpoint["episode_id"],
                integration_repair_operations.c.state.in_(_COLLECTING_OPERATION_STATES),
            )
            operation = (await conn.execute(_delivery_query)).mappings().first()
    return {
        "task_id": task_id, "project_id": task["project_id"],
        "status": task["status"], "parent_task_id": task["parent_task_id"],
        "pr_url": task["pr_url"], "repository_id": task["repo_id"],
        "branch": task["branch_name"],
        "mode": project["hierarchical_integration_mode"] if project else None,
        "integration_repository_id": project["integration_repository_id"] if project else None,
        "has_children": bool(has_children),
        "checkpoint": dict(checkpoint) if checkpoint is not None else None,
        "head_sha": checkpoint["checkpoint_sha"] if checkpoint is not None else None,
        "generation": int(checkpoint["generation"]) if checkpoint is not None else None,
        "episode_id": checkpoint["episode_id"] if checkpoint is not None else None,
        "base_sha": origin["base_sha"] if origin is not None else None,
        "origin_parent_task_id": origin["parent_task_id"] if origin is not None else None,
        "origin_materialized": bool(origin["materialized"]) if origin is not None else None,
        "origin_reserved": bool(origin["reserved"]) if origin is not None else None,
        "completion_id": completion["id"] if completion is not None else None,
        "completed_at": completion["completed_at"] if completion is not None else None,
        "rework_at": float(json.loads(rework_value)) if rework_value is not None else None,
        "object_experiment": object_experiment is not None,
        "parent": dict(parent) if parent is not None else None,
        "parent_checkpoint": dict(parent_checkpoint) if parent_checkpoint is not None else None,
        "operation": dict(operation) if operation is not None else None,
    }


def _child_refusal(snapshot: dict[str, Any]) -> tuple[str, str] | None:
    """Why this task is not a completed code child that could be assembled."""
    if snapshot.get("object_experiment"):
        return "not_eligible", "object evaluation is an artifact-only experiment"
    if snapshot["mode"] not in MANAGED_MODES:
        return "not_eligible", "the project is not in hierarchy or train mode"
    if snapshot["parent_task_id"] is None:
        return "not_eligible", (
            "a root: the train delivers it through its pull request "
            "(`aq integration redrive-root`)"
        )
    if snapshot["status"] != TaskStatus.COMPLETED.value:
        return "not_eligible", f"the child is {snapshot['status']}, not COMPLETED"
    if snapshot["has_children"] or snapshot["episode_id"] is not None:
        return "not_eligible", "a nested parent: its own parent integration completes it"
    if snapshot["checkpoint"] is None:
        return "not_eligible", (
            "no integration checkpoint: the child predates its parent's collection"
        )
    if (
        snapshot["repository_id"] is None
        or snapshot["repository_id"] != snapshot["integration_repository_id"]
        or snapshot["checkpoint"]["repository_id"] != snapshot["repository_id"]
        or snapshot["checkpoint"]["branch"] != snapshot["branch"]
    ):
        return "blocked", "the child's checkpoint is not on its branch in the integration repository"
    if (
        snapshot["base_sha"] is None
        or not snapshot["origin_materialized"]
        or not snapshot["origin_reserved"]
        or snapshot["origin_parent_task_id"] != snapshot["parent_task_id"]
    ):
        return "blocked", (
            "the child's branch origin is missing, unmaterialized, or was not cut from its parent"
        )
    if snapshot["head_sha"] == snapshot["base_sha"]:
        return "blocked", (
            "the checkpoint is still the origin base: the close recorded no code; "
            "a no-code child needs `aq integration record-noop`"
        )
    return None


def _parent_refusal(snapshot: dict[str, Any]) -> str | None:
    parent = snapshot["parent"]
    parent_checkpoint = snapshot["parent_checkpoint"]
    if parent is None:
        return "the parent task is missing"
    if parent["status"] != TaskStatus.PAUSED.value:
        return f"the parent is {parent['status']}, not PAUSED collecting its children"
    if parent_checkpoint is None or parent_checkpoint["state"] != "awaiting_children":
        return "the parent's checkpoint is not awaiting children"
    if snapshot["operation"] is None:
        return (
            "the parent has no live collection operation for its current episode; "
            "a cancelled one is reopened with `aq integration reopen-collection "
            f"{parent['id']}`"
        )
    return None


async def latest_evidence_on(
    conn,
    *,
    task_id: str,
    repository_id: str,
    base_sha: str,
    head_sha: str,
    generation: int,
) -> dict[str, Any] | None:
    """The newest review verdict on exactly this child snapshot, if any."""
    evidence = integration_review_evidence
    _delivery_query = (
        select(evidence)
        .where(
            evidence.c.source_task_id == task_id,
            evidence.c.repository_id == repository_id,
            evidence.c.source_base == base_sha,
            evidence.c.reviewed_head_sha == head_sha,
            evidence.c.generation == generation,
        )
        .order_by(evidence.c.created_at.desc(), evidence.c.id.desc())
        .limit(1)
    )
    row = (await conn.execute(_delivery_query)).mappings().one_or_none()
    return dict(row) if row is not None else None


async def _open_reviewer_on(conn, task_id: str) -> str | None:
    """An unfinished reviewer task filed about *task_id* (``discovered-from``)."""
    return await conn.scalar(
        select(tasks.c.id)
        .join(task_dependencies, task_dependencies.c.task_id == tasks.c.id)
        .where(
            task_dependencies.c.depends_on_task_id == task_id,
            task_dependencies.c.dep_type == "discovered-from",
            tasks.c.profile_id.in_(_REVIEW_PROFILES),
            tasks.c.status.not_in(_FINISHED_STATUSES),
        )
        .order_by(tasks.c.id)
        .limit(1)
    )


async def _matching_receipt_on(conn, snapshot: dict[str, Any]):
    """Newest code receipt for the child's exact head and current parent branch."""
    query = select(task_delivery_receipts).where(
        task_delivery_receipts.c.source_task_id == snapshot["task_id"],
        task_delivery_receipts.c.target_task_id == snapshot["parent_task_id"],
        task_delivery_receipts.c.repository_id == snapshot["repository_id"],
        task_delivery_receipts.c.target_branch == snapshot["parent"]["branch_name"],
        task_delivery_receipts.c.reviewed_head_sha == snapshot["head_sha"],
        task_delivery_receipts.c.disposition == "code",
    ).order_by(task_delivery_receipts.c.created_at.desc()).limit(1)
    return (await conn.execute(query)).mappings().one_or_none()


class ChildDelivery:
    """Prove a completed child from Git and hand it to its parent's collector."""

    def __init__(
        self,
        db: Any,
        promotion_service: Any,
        *,
        collect: Collect | None = None,
        train_blockers: Callable[[str], Awaitable[dict | None]] | None = None,
        request_visit: Callable[[str, str, str], object] | None = None,
        clock: Callable[[], float] = time.time,
        retry_seconds: float = 60.0,
        max_backoff_seconds: float = 3600.0,
    ) -> None:
        if retry_seconds <= 0 or max_backoff_seconds <= 0:
            raise ValueError("child delivery retry and backoff must be positive")
        self.db = db
        self.promotion = promotion_service
        self.collect = collect
        self.train_blockers = train_blockers
        self.request_visit = request_visit
        self.clock = clock
        self.retry_seconds = retry_seconds
        self.max_backoff_seconds = max_backoff_seconds
        #: (task id, head) -> (consecutive misses, retry not before)
        self._deferred: dict[tuple[str, str], tuple[int, float]] = {}

    # ------------------------------------------------------------------
    # Collector path
    # ------------------------------------------------------------------

    async def ensure_evidence(self, task_id: str, now: float) -> dict[str, Any] | None:
        """Record completion evidence for a completed child lacking a verdict.

        Returns the approved evidence row now pinning the child's head, or
        ``None`` when the child cannot (yet) be proven; a child that fails its
        proof is retried with exponential backoff.
        """
        async with self.db._engine.connect() as conn:
            _delivery_query = select(task_integration_checkpoints.c.checkpoint_sha).where(
                task_integration_checkpoints.c.task_id == task_id
            )
            head = (await conn.execute(_delivery_query)).scalar_one_or_none()
        key = (task_id, head or "")
        deferred = self._deferred.get(key)
        if deferred is not None and deferred[1] > now:
            return None
        diagnosis, snapshot, tree = await self._diagnose(task_id)
        if diagnosis["outcome"] != "would_advance" or diagnosis.get("evidence_id") is not None:
            if diagnosis["outcome"] == "blocked":
                logger.info(
                    "Child %s cannot be assembled yet: %s", task_id, diagnosis["reason"]
                )
            self._defer(task_id, head or "", now)
            return None
        try:
            evidence, created = await self._record(
                snapshot,
                tree,
                reviewer_identity=f"{COMPLETION_IDENTITY_PREFIX}{task_id}",
                decision_path="completion_proof",
                reason=None,
            )
        except HierarchyError as exc:
            logger.info("Child %s completion evidence refused: %s", task_id, exc)
            self._defer(task_id, snapshot["head_sha"], now)
            return None
        self._deferred.pop(key, None)
        if created:
            logger.info(
                "Child %s: recorded completion evidence %s for head %s",
                task_id, evidence["id"], snapshot["head_sha"],
            )
        return evidence

    def _defer(self, task_id: str, head: str, now: float) -> None:
        key = (task_id, head)
        misses = self._deferred.get(key, (0, 0.0))[0] + 1
        delay = min(self.retry_seconds * 2 ** (misses - 1), self.max_backoff_seconds)
        self._deferred[key] = (misses, now + delay)

    # ------------------------------------------------------------------
    # Supervisor control
    # ------------------------------------------------------------------

    async def run(
        self,
        task_id: str,
        *,
        dry_run: bool = True,
        expected_head_sha: str | None = None,
        reason: str | None = None,
        operator_id: str | None = None,
        adopt_source: bool = False,
    ) -> dict[str, Any]:
        if adopt_source:
            return await self._adopt_source(
                task_id, dry_run=dry_run, expected_head_sha=expected_head_sha,
                reason=reason, operator_id=operator_id,
            )
        diagnosis, snapshot, tree = await self._diagnose(task_id)
        visit = isinstance(tree, tuple)  # Git-first: (repository id, target ref) to revisit
        if dry_run or not visit and diagnosis["outcome"] != "would_advance":
            return diagnosis
        if diagnosis.get("head_sha") != expected_head_sha:
            return {
                **diagnosis,
                "outcome": "changed",
                "reason": "the child's head is not the one the dry run reported; "
                "run the dry run again",
            }
        if visit:
            return await self._request_train_visit(diagnosis, snapshot, *tree, reason, operator_id)
        try:
            evidence, created = await self._record(
                snapshot,
                tree,
                reviewer_identity=operator_id or "human:local-operator",
                decision_path="operator_redrive",
                reason=reason,
            )
        except HierarchyError as exc:
            return {
                **diagnosis,
                "outcome": "changed" if exc.code == "stale_head" else "blocked",
                "reason": exc.detail or exc.code,
            }
        collection = None
        if self.collect is not None:
            try:
                collection = await self.collect(snapshot["parent_task_id"])
            except Exception:
                logger.warning(
                    "Redrive of %s could not queue its parent's collection now; "
                    "the collector's next pass will",
                    task_id,
                    exc_info=True,
                )
        result = {**diagnosis, "outcome": "advanced", "evidence_id": evidence["id"],
                  "collection": collection, "reason": (
                      "recorded approved completion evidence for the head" if created
                      else "approved evidence already pinned the head")}
        await self.db.log_event(REDRIVE_EVENT, project_id=snapshot["project_id"], task_id=task_id,
                                payload=json.dumps({
            "operator_id": operator_id, "reason": reason, "at": self.clock(),
            "parent_task_id": snapshot["parent_task_id"], "head_sha": snapshot["head_sha"],
            "tree_sha": tree, "evidence_id": evidence["id"], "evidence_created": created,
            "collection": collection,
        }))
        return result

    async def _request_train_visit(self, diagnosis, snapshot, repository_id, target_ref, reason,
                                   operator_id) -> dict[str, Any]:
        """Wake the train lane of a pending child's parent target; never awaits the visit."""
        if self.request_visit is None:
            return {**diagnosis, "outcome": "blocked", "reason": (
                f"{diagnosis['reason']}; no integration train is running to visit {target_ref}")}
        self.request_visit(snapshot["project_id"], repository_id, target_ref)
        await self.db.log_event(REDRIVE_EVENT, project_id=snapshot["project_id"],
                                task_id=snapshot["task_id"], payload=json.dumps({
            "operator_id": operator_id, "reason": reason, "at": self.clock(),
            "parent_task_id": snapshot["parent_task_id"], "head_sha": snapshot["head_sha"],
            "repository_id": repository_id, "target_ref": target_ref,
            "diagnosis": diagnosis["reason"], "collection": "train_visit_requested",
        }))
        return {**diagnosis, "outcome": "advanced", "collection": "train_visit_requested",
                "reason": f"requested a train visit of {target_ref}; the train collects the "
                f"child once it observes the target (was: {diagnosis['reason']})"}

    async def diagnose(self, task_id: str) -> dict[str, Any]:
        """Report what the child's assembly waits on; writes nothing."""
        diagnosis, _snapshot, _tree = await self._diagnose(task_id)
        return diagnosis

    async def _adoption_snapshot(self, conn, task_id: str) -> tuple[dict, dict, int]:
        """Read the complete source binding and exclude attached or live writers."""
        snapshot = await _snapshot_on(conn, task_id)
        if snapshot is None:
            raise _ProofFailed("the child does not exist")
        refusal = _child_refusal(snapshot)
        if refusal is not None:
            raise _ProofFailed(refusal[1])
        parent = snapshot["parent"]
        if (parent is None or parent["repo_id"] != snapshot["repository_id"]
                or not parent["branch_name"]):
            raise _ProofFailed("the current parent has no matching repository and target branch")
        origin = (await conn.execute(select(task_branch_origins).where(
            task_branch_origins.c.task_id == task_id,
            task_branch_origins.c.repository_id == snapshot["repository_id"],
            task_branch_origins.c.retired_at.is_(None),
        ))).mappings().one_or_none()
        if (origin is None or origin["branch_name"] != snapshot["branch"]
                or origin["parent_repository_id"] != snapshot["repository_id"]
                or not origin["parent_ref"]
                or origin["parent_ref"].removeprefix("refs/heads/") !=
                parent["branch_name"].removeprefix("refs/heads/")):
            raise _ProofFailed("the recorded origin does not match the child and parent branches")
        repository = (await conn.execute(select(repos).where(
            repos.c.id == snapshot["repository_id"],
        ))).mappings().one_or_none()
        if repository is None or repository["project_id"] != snapshot["project_id"]:
            raise _ProofFailed("the repository no longer belongs to the child's project")
        completion = (await conn.execute(select(task_completion_records).where(
            task_completion_records.c.id == snapshot["completion_id"],
        ))).mappings().one_or_none()
        if completion is None or completion["outcome"] != "pass":
            raise _ProofFailed("source adoption requires an existing passing completion generation")
        if completion["branch"] not in (None, snapshot["branch"]):
            raise _ProofFailed("the completion names a different branch")
        try:
            commits = json.loads(completion["commits"])
        except (TypeError, ValueError) as exc:
            raise _ProofFailed("the completion has invalid recorded commits") from exc
        if not isinstance(commits, list) or commits and commits[-1] != snapshot["head_sha"]:
            raise _ProofFailed("the recorded final completion commit differs from the checkpoint")
        task = (await conn.execute(select(tasks.c.claim_epoch, tasks.c.assigned_agent_id).where(
            tasks.c.id == task_id,
        ))).one_or_none()
        if task is None:
            raise _ProofFailed("the child disappeared during source observation")
        writer_queries = (
            select(sessions.c.id).where(sessions.c.task_id == task_id,
                                      sessions.c.state != "stopped"),
            select(workspaces.c.id).where(workspaces.c.locked_by_task_id == task_id),
            select(integration_branch_owners.c.owner_id).where(
                integration_branch_owners.c.repository_id == snapshot["repository_id"],
                integration_branch_owners.c.ref.in_((snapshot["branch"],
                                                   "refs/heads/" + snapshot["branch"])),
                integration_branch_owners.c.handoff_state.in_(("attached", "handoff_pending")),
            ),
        )
        writer_present = bool(task.assigned_agent_id)
        for query in writer_queries:
            writer_present |= bool(await conn.scalar(query.exists().select()))
        if writer_present:
            raise _ProofFailed("source adoption requires the child session and workspace released")
        identity = {
            "task": _snapshot_identity(snapshot), "parent": dict(parent),
            "completion": dict(completion), "origin": dict(origin), "repository": dict(repository),
            "claim_epoch": task.claim_epoch, "mode": snapshot["mode"],
            "integration_repository_id": snapshot["integration_repository_id"],
        }
        return snapshot, identity, task.claim_epoch

    async def _adopt_source(self, task_id, *, dry_run, expected_head_sha, reason, operator_id):
        from src.integration.delivery_observer import prerequisite_observer
        from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance

        result = {"task_id": task_id, "outcome": "blocked"}
        if prerequisite_observer(self.db) is None:
            return {**result, "reason": "source adoption requires active Git-first delivery"}
        if not dry_run and (not operator_id or not reason or not reason.strip()):
            return {**result, "reason": "adoption requires an operator and a nonblank reason"}
        try:
            async with self.db._engine.connect() as conn:
                snapshot, identity, claim_epoch = await self._adoption_snapshot(conn, task_id)
            result.update(
                project_id=snapshot["project_id"], parent_task_id=snapshot["parent_task_id"],
                branch=snapshot["branch"], head_sha=snapshot["head_sha"],
                base_sha=snapshot["base_sha"], completion_id=snapshot["completion_id"],
            )
            await self._prove(snapshot)
            if expected_head_sha is not None and expected_head_sha != snapshot["head_sha"]:
                return {**result, "outcome": "changed", "reason": "the previewed head changed"}
            if not dry_run and expected_head_sha is None:
                raise _ProofFailed("applying source adoption requires the previewed exact head")
            resolved = await self.promotion._resolve_repository(snapshot["repository_id"])
            store = GitProvenance(self.promotion.git, str(resolved.retained_git_dir),
                                  repository_url=resolved.origin_url)
            completed = CompletedSource(CompletionIdentity(
                snapshot["project_id"], snapshot["repository_id"], task_id,
                snapshot["completion_id"],
            ), snapshot["head_sha"])
            # The hierarchy lock fences reopen/claim, then the repository lock
            # fences publication. Recheck after the network proof, before any write.
            async with self.db.immediate() as conn:
                await self.db.lock_hierarchy_project(conn, snapshot["project_id"])
                _current, current_identity, _epoch = await self._adoption_snapshot(conn, task_id)
                if current_identity != identity:
                    return {**result, "outcome": "changed", "reason": "completion binding changed"}
                async with self.promotion.git.arepository_transaction(store.checkout):
                    retained = await store.read_completion(completed.identity)
                    if retained is not None:
                        if retained["source_oid"] != completed.source_oid:
                            raise _ProofFailed("retained generation already binds another source")
                        return {**result, "outcome": "nothing_to_redrive",
                                "reason": "the exact completion source is already retained"}
                    if dry_run:
                        return {**result, "outcome": "would_advance",
                                "reason": "would retain the verified existing completion source"}
                    remote = await self.promotion.git.als_remote_ref(
                        str(resolved.retained_git_dir), snapshot["branch"],
                    )
                    if (remote.state is not RemoteRefState.PRESENT
                            or remote.oid != completed.source_oid):
                        raise _ProofFailed("the published source changed after preview")
                    await store.write_completion(completed, claim_epoch=claim_epoch)
            await self.db.log_event(
                REDRIVE_EVENT, project_id=snapshot["project_id"], task_id=task_id,
                payload=json.dumps({
                    "operator_id": operator_id, "reason": reason, "action": "adopt_source",
                    "completion_id": snapshot["completion_id"], "head_sha": completed.source_oid,
                    "parent_task_id": snapshot["parent_task_id"],
                }),
            )
            return {**result, "outcome": "advanced",
                    "reason": "retained the completion source; the train owns delivery"}
        except (_ProofFailed, GitError, ValueError, PromotionError) as exc:
            return {**result, "reason": safe_git_detail(str(exc))}

    # ------------------------------------------------------------------
    # Shared
    # ------------------------------------------------------------------

    async def _diagnose(
        self, task_id: str
    ) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        from src.integration.delivery_observer import prerequisite_observer

        observer = prerequisite_observer(self.db)
        if observer is not None:
            return await self._diagnose_git(task_id, observer)
        async with self.db._engine.connect() as conn:
            snapshot = await _snapshot_on(conn, task_id)
            if snapshot is None:
                return {"outcome": "not_found", "task_id": task_id}, None, None
            base: dict[str, Any] = {
                "task_id": task_id, "project_id": snapshot["project_id"],
                "parent_task_id": snapshot["parent_task_id"], "branch": snapshot["branch"],
                "parent_branch": (snapshot["parent"] or {}).get("branch_name"),
                "head_sha": snapshot["head_sha"], "base_sha": snapshot["base_sha"],
                "checkpoint": {
                    key: snapshot["checkpoint"][key]
                    for key in ("state", "generation", "checkpoint_sha", "episode_id")
                } if snapshot["checkpoint"] is not None else None,
            }
            refusal = _child_refusal(snapshot)
            if refusal is not None:
                outcome, reason = refusal
                return {**base, "outcome": outcome, "reason": reason}, snapshot, None
            parent_refusal = _parent_refusal(snapshot)
            if parent_refusal is not None:
                return {**base, "outcome": "blocked", "reason": parent_refusal}, snapshot, None
            head = snapshot["head_sha"]
            receipt = await _matching_receipt_on(conn, snapshot)
            if receipt is not None:
                if (
                    snapshot["rework_at"] is None
                    or receipt["created_at"] >= snapshot["rework_at"]
                ):
                    return {
                        **base,
                        "outcome": "nothing_to_redrive",
                        "reason": f"already delivered into the parent (receipt {receipt['id']})",
                    }, snapshot, None
                # Receipts are historical audit; nothing reissues one for a newer
                # completion. Git-first delivery proves containment instead.
                return {**base, "outcome": "blocked", "reason": (
                    "the matching receipt predates the child's current completion; "
                    "only Git-first delivery can prove it again")}, snapshot, None
            _delivery_query = (
                select(integration_promotion_intents.c.id, integration_promotion_intents.c.state)
                .where(
                    integration_promotion_intents.c.source_task_id == task_id,
                    integration_promotion_intents.c.source_head == head,
                    integration_promotion_intents.c.state.not_in(_SETTLED_INTENT_STATES),
                )
                .limit(1)
            )
            intent = (await conn.execute(_delivery_query)).mappings().one_or_none()
            if intent is not None:
                return {
                    **base,
                    "outcome": "nothing_to_redrive",
                    "reason": (
                        f"promotion {intent['id']} of this head is already {intent['state']}; "
                        "the collection operation owns it"
                    ),
                }, snapshot, None
            latest = await latest_evidence_on(
                conn,
                task_id=task_id,
                repository_id=snapshot["repository_id"],
                base_sha=snapshot["base_sha"],
                head_sha=head,
                generation=snapshot["generation"],
            )
            if latest is not None and latest["verdict"] == "rejected":
                return {
                    **base,
                    "outcome": "blocked",
                    "evidence_id": latest["id"],
                    "reason": "a reviewer rejected this head; the child must be reworked",
                }, snapshot, None
            reviewer = await _open_reviewer_on(conn, task_id)
            if reviewer is not None and latest is None:
                return {
                    **base,
                    "outcome": "blocked",
                    "reason": f"reviewer task {reviewer} is still open for this child",
                }, snapshot, None
        try:
            remote_head, tree = await self._prove(snapshot)
        except _ProofFailed as exc:
            return {
                **base,
                "outcome": "blocked",
                "remote_head_sha": exc.remote_head,
                "reason": exc.reason,
            }, snapshot, None
        diagnosis = {
            **base,
            "outcome": "would_advance",
            "remote_head_sha": remote_head,
            "tree_sha": tree,
        }
        if latest is not None:
            diagnosis.update(
                evidence_id=latest["id"],
                reason="approved evidence already pins this head; the collector queues it",
            )
        else:
            diagnosis["reason"] = (
                "the child is complete with its published head and no approved evidence"
            )
        return diagnosis, snapshot, tree

    @bounded_git_reads
    async def _diagnose_git(self, task_id, observer):
        """The Git-first train owns collection; receipts and parent episodes do not."""
        async with self.db._engine.connect() as conn:
            snapshot = await _snapshot_on(conn, task_id)
        if snapshot is None:
            return {"outcome": "not_found", "task_id": task_id}, None, None
        parent = snapshot["parent"] or {}
        base = {
            "task_id": task_id, "project_id": snapshot["project_id"],
            "parent_task_id": snapshot["parent_task_id"], "branch": snapshot["branch"],
            "parent_branch": parent.get("branch_name"), "head_sha": snapshot["head_sha"],
            "base_sha": snapshot["base_sha"],
        }
        if (snapshot["mode"] not in MANAGED_MODES or snapshot["parent_task_id"] is None
                or snapshot["status"] != TaskStatus.COMPLETED.value):
            return {**base, "outcome": "not_eligible",
                    "reason": "not a completed child in hierarchy or train mode"}, snapshot, None
        if not parent.get("branch_name"):
            return {**base, "outcome": "blocked",
                    "reason": "the parent's target branch is missing"}, snapshot, None
        target_ref = "refs/heads/" + parent["branch_name"].removeprefix("refs/heads/")
        pending = snapshot["repository_id"], target_ref  # what a train visit collects into
        view = await observer.observe([task_id])
        fresh = await view.fresh()
        async with self.db._engine.connect() as conn:
            current = await _snapshot_on(conn, task_id)
            verified = await view.verified_on(conn, [task_id]) if fresh else {}
        proof = verified.get(task_id)
        if (current is None or _snapshot_identity(current) != _snapshot_identity(snapshot)
                or proof is None or proof.request.target_ref != target_ref):
            return {**base, "outcome": "blocked",
                    "reason": "current parent containment could not be verified in Git"}, snapshot, pending
        from src.integration.delivery_truth import DeliveryState

        if proof.state is DeliveryState.UNKNOWN:
            reason = proof.reason
            if reason == "missing_git_provenance":
                reason += (f"; preview source recovery with `aq integration redrive-child "
                           f"{task_id} --adopt-source`")
                pending = None
            elif reason == "snapshot_unavailable":
                reason += f": {target_ref} is not observable now (see `aq integration status`)"
            return {**base, "outcome": "blocked", "reason": reason}, snapshot, pending
        if not proof.satisfied:
            from src.integration.obsolete_children import obsolete_child_blocker
            from src.integration.status import IntegrationStatusService

            for observed in view.snapshots:
                if (observed.project_id, observed.repository_id, observed.target_ref) != (
                    proof.request.project_id, proof.request.repository_id, proof.request.target_ref,
                ):
                    continue
                obsolete = await obsolete_child_blocker(
                    observed, task_id=task_id, source_base=snapshot["base_sha"],
                    source_oid=proof.source_oid,
                )
                if obsolete:
                    return {**base, "outcome": "blocked",
                            "reason": "obsolete_child: " + obsolete["detail"]}, snapshot, None
            read_blockers = self.train_blockers or IntegrationStatusService(
                self.db, git_first="active", delivery=observer,
            ).train_task_blockers
            status = await read_blockers(task_id) or {}
            blockers = status.get("blockers", [])
            if blockers:
                return {**base, "outcome": "blocked", "reason": "; ".join(
                    f"{item['code']}: {item['detail']}" for item in blockers
                )}, snapshot, pending
            if not status.get("batches"):
                return {**base, "outcome": "blocked", "reason": (
                    f"no batch contains this child; {proof.reason}; "
                    "the train has not reported a source admission decision"
                )}, snapshot, pending
        return {
            **base, "outcome": "nothing_to_redrive",
            "reason": ("already delivered into the parent (Git containment)" if proof.satisfied
                       else "the Git-first train owns delivery of this pending child"),
        }, snapshot, None if proof.satisfied else pending

    async def _prove(self, snapshot: dict[str, Any]) -> tuple[str, str]:
        """Return the remote tip and tree of the child's head, or say why not."""
        head = snapshot["head_sha"]
        promotion = self.promotion
        try:
            resolved = await promotion._resolve_repository(snapshot["repository_id"])
            if resolved.repo.project_id != snapshot["project_id"]:
                raise _ProofFailed("the child's repository belongs to another project")
            await promotion._ensure_retained_repository(resolved)
            store = resolved.retained_git_dir
            async with promotion.git.arepository_transaction(str(store)):
                await promotion._fetch_all_heads(store, resolved.origin_url)
                remote = await promotion.git.als_remote_ref(str(store), snapshot["branch"])
                if remote.state is RemoteRefState.ERROR:
                    raise _ProofFailed(
                        f"could not read the child's remote branch: {remote.error}"
                    )
                if remote.state is not RemoteRefState.PRESENT:
                    raise _ProofFailed("the child's branch is not published on the repository")
                if remote.oid != head:
                    raise _ProofFailed(
                        "the remote branch moved from the recorded head; "
                        "the child must close again at its new head",
                        remote_head=remote.oid,
                    )
                if not await promotion._is_ancestor(store, snapshot["base_sha"], head):
                    raise _ProofFailed(
                        "the head does not descend from the child's origin base",
                        remote_head=remote.oid,
                    )
                tree = await promotion._tree_oid(store, head)
        except PromotionError as exc:
            raise _ProofFailed(f"could not prove the head from the repository: {exc}") from exc
        return remote.oid, tree

    async def _record(
        self,
        snapshot: dict[str, Any],
        tree: str,
        *,
        reviewer_identity: str,
        decision_path: str,
        reason: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """Append approved leaf evidence after re-reading the child under lock."""
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, snapshot["project_id"])
            current = await _snapshot_on(conn, snapshot["task_id"])
            if (
                current is None
                or _snapshot_identity(current) != _snapshot_identity(snapshot)
                or _child_refusal(current) is not None
            ):
                raise HierarchyError("stale_head", "the child changed before its evidence commit")
            latest = await latest_evidence_on(
                conn,
                task_id=snapshot["task_id"],
                repository_id=snapshot["repository_id"],
                base_sha=snapshot["base_sha"],
                head_sha=snapshot["head_sha"],
                generation=snapshot["generation"],
            )
            if latest is not None:
                if latest["verdict"] == "approved":
                    return latest, False
                raise HierarchyError("rejected", "a reviewer rejected this head")
            reviewer = await _open_reviewer_on(conn, snapshot["task_id"])
            if reviewer is not None:
                raise HierarchyError("reviewer_open", f"reviewer task {reviewer} is still open")
            identity = ":".join(
                (
                    "completion",
                    snapshot["task_id"],
                    snapshot["repository_id"],
                    snapshot["base_sha"],
                    snapshot["head_sha"],
                    str(snapshot["generation"]),
                    tree,
                    reviewer_identity,
                )
            )
            evidence = {
                "id": f"review-{uuid.uuid5(_EVIDENCE_NAMESPACE, identity)}",
                "source_task_id": snapshot["task_id"],
                "repository_id": snapshot["repository_id"],
                "source_base": snapshot["base_sha"],
                "reviewed_head_sha": snapshot["head_sha"],
                "reviewed_tree_sha": tree,
                "reviewer_task_id": None,
                "reviewer_session_attempt_id": None,
                "reviewer_identity": reviewer_identity,
                "review_kind": "leaf",
                "generation": snapshot["generation"],
                "verdict": "approved",
                "evidence": {
                    "decision_path": decision_path,
                    "parent_task_id": snapshot["parent_task_id"],
                    "remote_head_sha": snapshot["head_sha"],
                    **({"reason": reason} if reason else {}),
                },
                "created_at": self.clock(),
            }
            await conn.execute(insert(integration_review_evidence).values(**evidence))
        return evidence, True
