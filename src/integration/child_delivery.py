"""Carry a completed child of a managed parent into the parent's assembly.

A managed parent (``PAUSED``, checkpoint ``awaiting_children``) assembles a
COMPLETED child only once an *approved* ``integration_review_evidence`` row
pins the child's exact checkpoint head (``CollectionService.queue_next``);
``delivery_promote`` re-checks that row before it pushes.  Until 2026-09-09 the
default pipeline filed a reviewer for every completed task, and the reviewer's
close wrote that row.  Commit cf0002b9c retired automatic reviews, and nothing
took their place for children: GitHub review ingestion covers roots only.  A
child's close still advanced its checkpoint (its ``working`` state is the
normal finished-leaf shape), but no collector ever assembled it, no receipt
reached the parent, and every sibling whose ``needs`` named it stayed out of
the claim frontier (vivid-ridge: sharp-impact stalled on .1 and .3).

:class:`ChildDelivery` proves a completed child from Git -- the remote branch
tip is the recorded checkpoint head, and it descends from the child's origin
base -- and records approved ``leaf`` evidence of exactly that head and tree:

* ``ensure_evidence`` is the collector's durable path.  It runs for every
  completed child still lacking a verdict, backs off a child whose proof fails,
  and never overrides a reviewer: a head a reviewer rejected, or a child with an
  open reviewer task, is left alone.  Human review stays on the root's pull
  request, and the parent's aggregate verification still runs its checks.
* ``run`` is the dry-run-first supervisor control for one stuck child
  (``aq integration redrive-child``).

:func:`stuck_children_statement` backs ``aq doctor --check
integration.stuck_children``.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import and_, exists, insert, select

from src.database.queries.hierarchy_queries import HierarchyError
from src.database.queries.task_queries import INTEGRATION_REWORK_AT_KEY
from src.database.tables import (
    integration_promotion_intents,
    integration_repair_operations,
    integration_review_evidence,
    projects,
    task_branch_origins,
    task_completion_records,
    task_delivery_receipts,
    task_dependencies,
    task_integration_checkpoints,
    task_metadata,
    tasks,
)
from src.git.manager import RemoteRefState
from src.integration.promotion import PromotionError
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
    project = (
        await conn.execute(
            select(
                projects.c.hierarchical_integration_mode,
                projects.c.integration_repository_id,
            ).where(projects.c.id == task["project_id"])
        )
    ).mappings().one_or_none()
    checkpoint = (
        await conn.execute(
            select(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == task_id
            )
        )
    ).mappings().one_or_none()
    origin = (
        await conn.execute(
            select(task_branch_origins).where(
                task_branch_origins.c.task_id == task_id,
                task_branch_origins.c.repository_id == task["repo_id"],
                task_branch_origins.c.retired_at.is_(None),
            )
        )
    ).mappings().one_or_none()
    has_children = (
        await conn.execute(select(exists().where(tasks.c.parent_task_id == task_id)))
    ).scalar_one()
    completion = (
        await conn.execute(
            select(task_completion_records.c.id, task_completion_records.c.completed_at)
            .where(task_completion_records.c.task_id == task_id)
            .order_by(
                task_completion_records.c.completed_at.desc(),
                task_completion_records.c.id.desc(),
            )
            .limit(1)
        )
    ).mappings().one_or_none()
    rework_value = (
        await conn.execute(
            select(task_metadata.c.value).where(
                task_metadata.c.task_id == task_id,
                task_metadata.c.key == INTEGRATION_REWORK_AT_KEY,
            )
        )
    ).scalar_one_or_none()
    parent = parent_checkpoint = operation = None
    if task["parent_task_id"] is not None:
        parent = (
            await conn.execute(
                select(tasks.c.id, tasks.c.status, tasks.c.branch_name, tasks.c.repo_id).where(
                    tasks.c.id == task["parent_task_id"]
                )
            )
        ).mappings().one_or_none()
        parent_checkpoint = (
            await conn.execute(
                select(
                    task_integration_checkpoints.c.state,
                    task_integration_checkpoints.c.episode_id,
                ).where(task_integration_checkpoints.c.task_id == task["parent_task_id"])
            )
        ).mappings().one_or_none()
        if parent_checkpoint is not None and parent_checkpoint["episode_id"] is not None:
            operation = (
                await conn.execute(
                    select(
                        integration_repair_operations.c.id,
                        integration_repair_operations.c.state,
                    ).where(
                        integration_repair_operations.c.parent_task_id == task["parent_task_id"],
                        integration_repair_operations.c.episode_id
                        == parent_checkpoint["episode_id"],
                        integration_repair_operations.c.state.in_(_COLLECTING_OPERATION_STATES),
                    )
                )
            ).mappings().first()
    return {
        "task_id": task_id,
        "project_id": task["project_id"],
        "status": task["status"],
        "parent_task_id": task["parent_task_id"],
        "repository_id": task["repo_id"],
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
        "parent": dict(parent) if parent is not None else None,
        "parent_checkpoint": dict(parent_checkpoint) if parent_checkpoint is not None else None,
        "operation": dict(operation) if operation is not None else None,
    }


def _child_refusal(snapshot: dict[str, Any]) -> tuple[str, str] | None:
    """Why this task is not a completed code child that could be assembled."""
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
        return "the parent has no live collection operation for its current episode"
    return None


def stuck_children_statement(*, updated_before: float, limit: int = 200):
    """Completed code children a collecting parent has not assembled.

    The child is COMPLETED with its leaf checkpoint still ``working``, its
    parent is ``PAUSED`` with an ``awaiting_children`` checkpoint and a live
    collection operation, no receipt carries its checkpoint head into the
    parent's branch, no promotion of that head is still being written or
    repaired, and neither the task nor its checkpoint changed since
    *updated_before*.
    """
    child = tasks
    parent = tasks.alias("stuck_child_parent")
    checkpoint = task_integration_checkpoints.alias("stuck_child_checkpoint")
    parent_checkpoint = task_integration_checkpoints.alias("stuck_child_parent_checkpoint")
    operation = integration_repair_operations
    origin = task_branch_origins
    delivered = exists(
        select(task_delivery_receipts.c.id).where(
            task_delivery_receipts.c.source_task_id == child.c.id,
            task_delivery_receipts.c.repository_id == checkpoint.c.repository_id,
            task_delivery_receipts.c.target_branch == parent.c.branch_name,
            task_delivery_receipts.c.reviewed_head_sha == checkpoint.c.checkpoint_sha,
        )
    )
    promoting = exists(
        select(integration_promotion_intents.c.id).where(
            integration_promotion_intents.c.source_task_id == child.c.id,
            integration_promotion_intents.c.source_head == checkpoint.c.checkpoint_sha,
            integration_promotion_intents.c.state.not_in(_SETTLED_INTENT_STATES),
        )
    )
    grandchild = tasks.alias("stuck_child_grandchild")
    nested = exists(select(grandchild.c.id).where(grandchild.c.parent_task_id == child.c.id))
    return (
        select(
            child.c.id.label("task_id"),
            child.c.project_id,
            child.c.parent_task_id,
            child.c.branch_name.label("branch"),
            child.c.updated_at,
            checkpoint.c.checkpoint_sha.label("head_sha"),
            checkpoint.c.generation,
            checkpoint.c.repository_id,
            checkpoint.c.updated_at.label("checkpoint_updated_at"),
            origin.c.base_sha,
        )
        .select_from(
            child.join(parent, parent.c.id == child.c.parent_task_id)
            .join(projects, projects.c.id == child.c.project_id)
            .join(checkpoint, checkpoint.c.task_id == child.c.id)
            .join(parent_checkpoint, parent_checkpoint.c.task_id == parent.c.id)
            .join(
                operation,
                and_(
                    operation.c.parent_task_id == parent.c.id,
                    operation.c.episode_id == parent_checkpoint.c.episode_id,
                ),
            )
            .join(
                origin,
                and_(
                    origin.c.task_id == child.c.id,
                    origin.c.repository_id == checkpoint.c.repository_id,
                    origin.c.retired_at.is_(None),
                ),
            )
        )
        .where(
            projects.c.hierarchical_integration_mode.in_(MANAGED_MODES),
            child.c.status == TaskStatus.COMPLETED.value,
            checkpoint.c.state == "working",
            checkpoint.c.episode_id.is_(None),
            checkpoint.c.checkpoint_sha != origin.c.base_sha,
            parent.c.status == TaskStatus.PAUSED.value,
            parent_checkpoint.c.state == "awaiting_children",
            operation.c.state.in_(_COLLECTING_OPERATION_STATES),
            child.c.updated_at < updated_before,
            checkpoint.c.updated_at < updated_before,
            ~nested,
            ~delivered,
            ~promoting,
        )
        .order_by(child.c.id)
        .limit(limit)
    )


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
    row = (
        await conn.execute(
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
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


async def _open_reviewer_on(conn, task_id: str) -> str | None:
    """An unfinished reviewer task filed about *task_id* (``discovered-from``)."""
    return (
        await conn.execute(
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
    ).scalar_one_or_none()


class ChildDelivery:
    """Prove a completed child from Git and hand it to its parent's collector."""

    def __init__(
        self,
        db: Any,
        promotion_service: Any,
        *,
        collect: Collect | None = None,
        clock: Callable[[], float] = time.time,
        retry_seconds: float = 60.0,
        max_backoff_seconds: float = 3600.0,
    ) -> None:
        if retry_seconds <= 0 or max_backoff_seconds <= 0:
            raise ValueError("child delivery retry and backoff must be positive")
        self.db = db
        self.promotion = promotion_service
        self.collect = collect
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
            head = (
                await conn.execute(
                    select(task_integration_checkpoints.c.checkpoint_sha).where(
                        task_integration_checkpoints.c.task_id == task_id
                    )
                )
            ).scalar_one_or_none()
        key = (task_id, head or "")
        deferred = self._deferred.get(key)
        if deferred is not None and deferred[1] > now:
            return None
        diagnosis, snapshot, tree = await self._diagnose(task_id)
        if (
            diagnosis["outcome"] != "would_advance"
            or diagnosis.get("evidence_id") is not None
            or diagnosis.get("redrive_kind") == "receipt_reissue"
        ):
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
    ) -> dict[str, Any]:
        diagnosis, snapshot, tree = await self._diagnose(task_id)
        if dry_run or diagnosis["outcome"] != "would_advance":
            return diagnosis
        if diagnosis.get("head_sha") != expected_head_sha:
            return {
                **diagnosis,
                "outcome": "changed",
                "reason": "the child's head is not the one the dry run reported; "
                "run the dry run again",
            }
        try:
            if diagnosis.get("redrive_kind") == "receipt_reissue":
                receipt, created = await self._reissue_receipt(
                    snapshot, diagnosis["stale_receipt_id"],
                )
                evidence = None
            else:
                evidence, created = await self._record(
                    snapshot,
                    tree,
                    reviewer_identity=operator_id or "human:local-operator",
                    decision_path="operator_redrive",
                    reason=reason,
                )
                receipt = None
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
        result = {
            **diagnosis,
            "outcome": "advanced",
            "evidence_id": evidence["id"] if evidence is not None else None,
            "receipt_id": receipt["id"] if receipt is not None else None,
            "collection": collection,
            "reason": (
                "reissued a receipt for the current completion"
                if receipt is not None and created
                else "current completion already has a receipt"
                if receipt is not None
                else "recorded approved completion evidence for the head"
                if created
                else "approved evidence already pinned the head"
            ),
        }
        await self.db.log_event(
            REDRIVE_EVENT,
            project_id=snapshot["project_id"],
            task_id=task_id,
            payload=json.dumps(
                {
                    "operator_id": operator_id,
                    "reason": reason,
                    "parent_task_id": snapshot["parent_task_id"],
                    "head_sha": snapshot["head_sha"],
                    "tree_sha": tree,
                    "evidence_id": evidence["id"] if evidence is not None else None,
                    "receipt_id": receipt["id"] if receipt is not None else None,
                    "evidence_created": created if evidence is not None else False,
                    "receipt_created": created if receipt is not None else False,
                    "collection": collection,
                    "at": self.clock(),
                }
            ),
        )
        return result

    async def diagnose(self, task_id: str) -> dict[str, Any]:
        """Report what the child's assembly waits on; writes nothing."""
        diagnosis, _snapshot, _tree = await self._diagnose(task_id)
        return diagnosis

    # ------------------------------------------------------------------
    # Shared
    # ------------------------------------------------------------------

    async def _diagnose(
        self, task_id: str
    ) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        async with self.db._engine.connect() as conn:
            snapshot = await _snapshot_on(conn, task_id)
            if snapshot is None:
                return {"outcome": "not_found", "task_id": task_id}, None, None
            base: dict[str, Any] = {
                "task_id": task_id,
                "project_id": snapshot["project_id"],
                "parent_task_id": snapshot["parent_task_id"],
                "branch": snapshot["branch"],
                "parent_branch": (snapshot["parent"] or {}).get("branch_name"),
                "head_sha": snapshot["head_sha"],
                "base_sha": snapshot["base_sha"],
                "checkpoint": (
                    {
                        key: snapshot["checkpoint"][key]
                        for key in ("state", "generation", "checkpoint_sha", "episode_id")
                    }
                    if snapshot["checkpoint"] is not None
                    else None
                ),
            }
            refusal = _child_refusal(snapshot)
            if refusal is not None:
                outcome, reason = refusal
                return {**base, "outcome": outcome, "reason": reason}, snapshot, None
            parent_refusal = _parent_refusal(snapshot)
            if parent_refusal is not None:
                return {**base, "outcome": "blocked", "reason": parent_refusal}, snapshot, None
            head = snapshot["head_sha"]
            receipt = (
                await conn.execute(
                    select(task_delivery_receipts).where(
                        task_delivery_receipts.c.source_task_id == task_id,
                        task_delivery_receipts.c.target_task_id == snapshot["parent_task_id"],
                        task_delivery_receipts.c.repository_id == snapshot["repository_id"],
                        task_delivery_receipts.c.target_branch == base["parent_branch"],
                        task_delivery_receipts.c.reviewed_head_sha == head,
                        task_delivery_receipts.c.disposition == "code",
                    ).order_by(task_delivery_receipts.c.created_at.desc()).limit(1)
                )
            ).mappings().one_or_none()
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
                stale_receipt = dict(receipt)
            else:
                stale_receipt = None
            if stale_receipt is not None and (
                stale_receipt["parent_operation_id"] != snapshot["operation"]["id"]
                or stale_receipt["parent_episode_id"]
                != snapshot["parent_checkpoint"]["episode_id"]
            ):
                return {
                    **base,
                    "outcome": "blocked",
                    "reason": "the old receipt belongs to a previous parent collection episode",
                }, snapshot, None
            if stale_receipt is None:
                intent = (
                    await conn.execute(
                        select(
                            integration_promotion_intents.c.id,
                            integration_promotion_intents.c.state,
                        ).where(
                            integration_promotion_intents.c.source_task_id == task_id,
                            integration_promotion_intents.c.source_head == head,
                            integration_promotion_intents.c.state.not_in(_SETTLED_INTENT_STATES),
                        ).limit(1)
                    )
                ).mappings().one_or_none()
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
            if stale_receipt is not None:
                await self._prove_receipt_ancestry(snapshot, stale_receipt)
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
        if stale_receipt is not None:
            diagnosis.update(
                redrive_kind="receipt_reissue",
                stale_receipt_id=stale_receipt["id"],
                reason="the matching receipt predates the child's current completion",
            )
            return diagnosis, snapshot, tree
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

    async def _prove_receipt_ancestry(
        self, snapshot: dict[str, Any], receipt: dict[str, Any]
    ) -> None:
        """A reissued receipt must still name code on the live parent branch."""
        after_sha = receipt["after_sha"]
        if not after_sha:
            raise _ProofFailed("the old receipt has no incorporated parent head to prove")
        try:
            resolved = await self.promotion._resolve_repository(snapshot["repository_id"])
            await self.promotion._ensure_retained_repository(resolved)
            store = resolved.retained_git_dir
            async with self.promotion.git.arepository_transaction(str(store)):
                await self.promotion._fetch_all_heads(store, resolved.origin_url)
                remote = await self.promotion.git.als_remote_ref(
                    str(store), snapshot["parent"]["branch_name"]
                )
                if remote.state is not RemoteRefState.PRESENT:
                    raise _ProofFailed("the parent's published branch cannot be proven")
                if not await self.promotion._is_ancestor(store, after_sha, remote.oid):
                    raise _ProofFailed(
                        "the old receipt's incorporated head is no longer on the parent branch"
                    )
        except PromotionError as exc:
            raise _ProofFailed(f"could not prove the parent branch: {exc}") from exc

    async def _reissue_receipt(
        self,
        snapshot: dict[str, Any],
        stale_receipt_id: str,
    ) -> tuple[dict[str, Any], bool]:
        """Acknowledge the same incorporated head for a newer task completion."""
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, snapshot["project_id"])
            current = await _snapshot_on(conn, snapshot["task_id"])
            if (
                current is None
                or _snapshot_identity(current) != _snapshot_identity(snapshot)
                or _child_refusal(current) is not None
                or _parent_refusal(current) is not None
            ):
                raise HierarchyError("stale_head", "the child or parent changed before redrive")
            matching = (
                await conn.execute(
                    select(task_delivery_receipts).where(
                        task_delivery_receipts.c.source_task_id == snapshot["task_id"],
                        task_delivery_receipts.c.target_task_id == snapshot["parent_task_id"],
                        task_delivery_receipts.c.repository_id == snapshot["repository_id"],
                        task_delivery_receipts.c.target_branch == current["parent"]["branch_name"],
                        task_delivery_receipts.c.reviewed_head_sha == snapshot["head_sha"],
                        task_delivery_receipts.c.disposition == "code",
                    ).order_by(task_delivery_receipts.c.created_at.desc()).limit(1)
                )
            ).mappings().one_or_none()
            if matching is None or current["rework_at"] is None:
                raise HierarchyError("stale_head", "the receipt or rework marker changed before redrive")
            if matching["created_at"] >= current["rework_at"]:
                return dict(matching), False
            if matching["id"] != stale_receipt_id:
                raise HierarchyError("stale_head", "the receipt changed before redrive")
            if (
                matching["parent_operation_id"] != current["operation"]["id"]
                or matching["parent_episode_id"] != current["parent_checkpoint"]["episode_id"]
            ):
                raise HierarchyError("stale_head", "the parent collection episode changed")
            domain_key = f"redrive:{current['completion_id']}:{matching['id']}"
            receipt = dict(matching)
            receipt.update(
                id=f"receipt-{uuid.uuid5(_EVIDENCE_NAMESPACE, domain_key)}",
                domain_key=domain_key,
                created_at=max(self.clock(), current["rework_at"]),
            )
            await conn.execute(insert(task_delivery_receipts).values(**receipt))
        return receipt, True

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
