"""Durable bounded repair stages for parent and root integration operations."""

from __future__ import annotations

import hashlib
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import and_, delete, func, insert, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    agents,
    archived_tasks,
    integration_attestation_publications,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_operation_artifact_pins,
    integration_outbox,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stage_evidence,
    integration_repair_stages,
    messages,
    playbook_artifacts,
    project_integration_leases,
    projects,
    sessions,
    task_delivery_receipts,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)
from src.git.manager import GitError, is_valid_git_oid
from src.integration.engine import root_engine_guard
from src.integration.parent_engine import parent_engine_guard, parent_stage_at_entry

from src.integration.green_continuation import (
    enqueue_green_continuation_on,
    promotion_fingerprint,
)
from src.integration.models import BranchKey, Fence, HierarchicalIntegrationPolicy, RepairPolicy
from src.integration.outbox import enqueue_integration_event
from src.integration.ownership import BranchBusy, BranchOwnership, StaleFence
from src.models import Task, TaskStatus
from src.playbooks.artifact_ref import ArtifactRef

logger = logging.getLogger(__name__)


def repair_subject_sha(subject: dict[str, Any] | None) -> str:
    """The exact commit a repair stage's current subject is anchored on.

    The two target kinds carry different canonical subjects: a parent
    episode's is ``{"kind": "parent", "generation", "head_sha"}`` while a
    root batch's is ``{"kind": "batch", "revision", "candidate_sha"}``.
    Every consumer wants the OID, not the shape, so resolve it in one place
    -- reaching for ``subject["head_sha"]`` raises ``KeyError`` on every
    root repair and turns a passing close into a blocked task.
    """
    raw = subject or {}
    return str(raw.get("head_sha") or raw.get("candidate_sha") or "")


# Detached predecessors transfer through the existing fence; pending
# handoffs still require stopped-writer proof. Attached predecessors preserve
# their checkout when moving to any bounded successor stage.
_ATTACHED_PRIMARY_STATES = frozenset({"attached"})
_TRANSFERABLE_PRIMARY_STATES = frozenset({"reserved", "released", "handoff_pending"})
STUCK_BATCH_ATTEMPTS = 3
#: How long a delivered construction re-drive may run before a stage-0 deadline
#: that still finds no candidate escalates (``_redrive_unfinished_construction_on``).
CONSTRUCTION_REDRIVE_GRACE_SECONDS = 600.0
#: A due stage whose writer is live, held, or not yet provably stopped is
#: revisited on this bounded clock instead of being superseded.
WRITER_RECHECK_SECONDS = 300.0
#: Writer allocations that may start on one unchanged subject head: the first
#: writer plus at most two more (same-ordinal refiles and successor stages).
MAX_WRITER_ALLOCATIONS_PER_SUBJECT = 3
#: Same-ordinal refiles of a stopped writer that never published, per stage.
MAX_REFILES_PER_STAGE = 1
#: Deadline deferrals each stage dossier keeps verbatim; older ones are counted.
_DEFERRAL_HISTORY = 10
#: Deferral reasons the supervisor hears about once per stage.  A live writer,
#: an operator's own hold, or a row a concurrent expiry already released is
#: not news.
_NOTICE_DEFERRALS = frozenset({
    "writer_unclaimed", "stale_fence", "stop_proof_unavailable", "stale_claim",
    "checkout_in_use", "origin_unreachable",
})
#: Delegate statuses with no writer: the pool has not (re)claimed it yet.
_UNCLAIMED_STATUSES = frozenset({
    TaskStatus.DEFINED.value, TaskStatus.PAUSED.value, TaskStatus.READY.value,
})
#: Delegate statuses a stopped writer can leave behind without closing.
_STOPPED_STATUSES = frozenset({
    TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value, TaskStatus.BLOCKED.value,
    TaskStatus.PAUSED.value, TaskStatus.READY.value,
})
#: Exit-time holds a refile clears; an operator's ``manual_pause`` stays binding.
_REFILE_CLEARED_HOLDS = ("needs_attention", "claim_prepare_backoff_until")


class _StoppedWriter(Exception):
    """An expiring stage's writer stopped without publishing; prove it outside the lock."""

    def __init__(self, facts: dict[str, Any]) -> None:
        super().__init__(facts["task_id"])
        self.facts = facts


def _unfinished_candidate_publication(batch_id):
    """Built candidates retain collector authority through audit publication."""
    revision = integration_candidate_revisions
    publication = integration_candidate_publications
    batch = integration_batches
    return select(revision.c.batch_id).select_from(
        revision.join(batch, batch.c.id == revision.c.batch_id).outerjoin(
            publication,
            (publication.c.batch_id == revision.c.batch_id)
            & (publication.c.revision == revision.c.revision),
        )
    ).where(
        batch.c.id == batch_id,
        batch.c.current_revision == revision.c.revision,
        revision.c.state.in_(("built", "green")),
        or_(publication.c.state.is_(None), publication.c.state != "pr_published"),
    ).exists()


def _operator_held(task_id):
    """``aq task pause`` wrote a hold on *task_id*; only ``aq task resume`` lifts it.

    A held delegate and a never-launched one are both ``PAUSED`` with no
    timer, so the status alone cannot tell them apart.
    """
    return select(task_metadata.c.task_id).where(
        task_metadata.c.task_id == task_id, task_metadata.c.key == "manual_pause"
    ).exists()


class _RepairInvariant(ValueError):
    """Persisted repair identity is internally inconsistent."""


class RepairService:
    """Own atomic stage clocks and evidence-accounting transitions."""

    def __init__(
        self,
        db,
        *,
        clock: Callable[[], float] = time.time,
        confirm_handoff=None,
        confirm_stopped=None,
        owner_recovery=None,
    ) -> None:
        self.db = db
        self.clock = clock
        self._ownership = BranchOwnership(db, confirm_handoff=confirm_handoff, clock=clock)
        self._confirm_stopped = confirm_stopped
        self._owner_recovery = owner_recovery
        self._reservation_cursor: str | None = None
        self._archive_cursor: str | None = None

    async def pending_dispatches(self, *, after=None, limit=20):
        """Replay incomplete continuous-policy handoffs through the command path.

        A delegate an operator paused is not replayed: the hold is the
        operator's decision, and ``aq task resume`` is what releases it.
        """
        stage = integration_repair_stages
        operation = integration_repair_operations
        statement = select(stage.c.operation_id, stage.c.ordinal).select_from(
            stage.join(operation, operation.c.id == stage.c.operation_id)
            .outerjoin(tasks, tasks.c.id == stage.c.repair_task_id)
        ).where(
            operation.c.active_stage == stage.c.ordinal,
            operation.c.state.in_(("active", "escalated")),
            stage.c.state == "active",
            ~((stage.c.ordinal == 0) & _unfinished_candidate_publication(operation.c.batch_id)),
            or_(
                and_(
                    stage.c.policy["on_exhausted"].as_string() == "continue",
                    or_(stage.c.repair_task_id.is_(None),
                        (tasks.c.status == "PAUSED") & ~_operator_held(stage.c.repair_task_id),
                        (tasks.c.status == "COMPLETED") & (stage.c.attempts > 0)),
                ),
                # ``aq integration reopen-collection`` dispatches its stage
                # itself; whatever the policy, retry a dispatch that failed.
                and_(
                    stage.c.repair_task_id.is_(None),
                    stage.c.dossier["reopened"].as_string().is_not(None),
                ),
            ),
        ).order_by(stage.c.operation_id, stage.c.ordinal).limit(limit)
        if after is not None:
            statement = statement.where(tuple_(stage.c.operation_id, stage.c.ordinal) > after)
        async with self.db._engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(statement)).mappings()]

    async def retire_terminal_delegates(self, now: float, *, limit: int = 100) -> list[str]:
        """Settle unfinished delegates whose owning operation has already ended.

        One line of glue over :mod:`src.integration.delegate_release`, which
        the orchestrator tick, ``integration_abort`` and
        ``aq doctor --check integration.stranded_delegates --fix`` all share.
        Keeping one implementation is the point: three callers settling a
        delegate three slightly different ways is how the earlier version
        left tickets ``PAUSED`` that a later one had to roll forward.
        """
        from src.integration.delegate_release import release_delegates

        released = await release_delegates(
            self.db, now=now, released_by="integration_service", limit=limit
        )
        owner_row_ids = list(
            dict.fromkeys(
                blocker["owner_row_id"]
                for release in released
                for blocker in release.get("cleanup", {}).get("blockers", [])
                if blocker.get("code") == "branch_owner_retained"
                and blocker.get("owner_row_id")
            )
        )
        if self._owner_recovery is not None and owner_row_ids:
            await self._owner_recovery.recover_many(
                owner_row_ids, principal="delegate_retirement"
            )
        from src.integration.delegate_release import archive_obsolete_delegates

        archived = await archive_obsolete_delegates(
            self.db, limit=limit, after=self._archive_cursor
        )
        self._archive_cursor = archived["next_after"]
        return [row["task_id"] for row in released]

    async def reconcile_accepted_delegates(self, now: float, *, limit: int = 100) -> list[str]:
        from src.integration.accepted_repair import reconcile_stopped_accepted_delegates

        return await reconcile_stopped_accepted_delegates(self.db, now, limit=limit)

    async def reserve_batch_operation_on(
        self, conn, batch_id: str, *, now: float | None = None
    ) -> dict[str, Any]:
        """Reserve Task8's one pinned, no-stage operation in its transaction."""
        reserved_at = self.clock() if now is None else now
        batch = (
            await conn.execute(
                select(integration_batches)
                .where(integration_batches.c.id == batch_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        if batch is None:
            raise ValueError("integration batch does not exist")
        existing = (
            await conn.execute(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.batch_id == batch_id
                )
            )
        ).mappings().one_or_none()
        if existing is not None:
            if (
                existing["target_kind"] != "batch"
                or existing["episode_id"] != batch_id
                or existing["policy_snapshot"] != batch["policy_snapshot"]
                or existing["artifact_snapshot"] != batch["artifact_snapshot"]
            ):
                raise ValueError("batch repair operation identity conflicts")
            return dict(existing)

        project = (
            await conn.execute(
                select(projects).where(projects.c.id == batch["project_id"])
            )
        ).mappings().one_or_none()
        if (
            project is None
            or project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
            or project["integration_repository_id"] != batch["repository_id"]
        ):
            raise ValueError("batch is outside enabled hierarchical integration scope")
        policy = HierarchicalIntegrationPolicy.model_validate(batch["policy_snapshot"])
        boundary = policy.root
        artifact = (
            await conn.execute(
                select(playbook_artifacts).where(
                    playbook_artifacts.c.artifact_sha256
                    == boundary.route.artifact.artifact_sha256
                )
            )
        ).mappings().one_or_none()
        if (
            artifact is None
            or ArtifactRef.from_row(artifact).as_dict()
            != boundary.route.artifact.model_dump(mode="json")
            or batch["artifact_snapshot"]
            != boundary.route.artifact.model_dump(mode="json")
        ):
            raise ValueError("batch route artifact identity is not stored and frozen")
        operation = {
            "id": f"repair-batch-{batch_id}",
            "target_kind": "batch",
            "batch_id": batch_id,
            "parent_task_id": None,
            "episode_id": batch_id,
            "active_stage": 0,
            "state": "active",
            "policy_snapshot": policy.model_dump(mode="json"),
            "artifact_snapshot": boundary.route.artifact.model_dump(mode="json"),
            "required_check_version": boundary.required_checks.version,
            "verifier_task_id": None,
            "route_playbook_id": boundary.route.playbook_id,
            "route_scope": boundary.route.scope,
            "route_scope_identifier": boundary.route.scope_identifier,
            "route_activation_id": boundary.route.activation_id,
            "created_at": reserved_at,
            "updated_at": reserved_at,
        }
        await conn.execute(insert(integration_repair_operations).values(**operation))
        await conn.execute(
            insert(integration_operation_artifact_pins).values(
                operation_id=operation["id"],
                artifact_sha256=boundary.route.artifact.artifact_sha256,
            )
        )
        return operation

    @parent_engine_guard("operation", outcome="stale")
    @root_engine_guard("operation", outcome="stale")
    async def start(
        self,
        operation_id: str,
        starting_sha: str,
        trigger_id: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Activate or durably continue an operation's bounded repair stage."""
        if not is_valid_git_oid(starting_sha) or not str(trigger_id).strip():
            return {"outcome": "stale", "operation_id": operation_id}
        activated_at = self.clock() if now is None else now
        continuation_transition = None
        continuation_result = None
        async with self.db.immediate() as conn:
            operation_snapshot = (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(integration_repair_operations.c.id == operation_id)
                )
            ).mappings().one_or_none()
            if operation_snapshot is None:
                return {"outcome": "stale", "operation_id": operation_id}
            try:
                project_id = await self._operation_project_id_on(
                    conn, dict(operation_snapshot)
                )
            except ValueError:
                return {"outcome": "invariant_error", "operation_id": operation_id}
            await self.db.lock_hierarchy_project(conn, project_id)
            operation = (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(integration_repair_operations.c.id == operation_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if operation is None:
                return {"outcome": "stale", "operation_id": operation_id}

            existing = (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.ordinal == 0,
                    )
                )
            ).mappings().one_or_none()
            active_stage = None
            if existing is not None:
                if int(operation["active_stage"]) == 0:
                    active_stage = existing
                else:
                    active_stage = (
                        await conn.execute(
                            select(integration_repair_stages).where(
                                integration_repair_stages.c.operation_id == operation_id,
                                integration_repair_stages.c.ordinal == operation["active_stage"],
                            )
                        )
                    ).mappings().one_or_none()
            # ``aq integration reopen-collection`` can return a cancelled
            # parent collection to life with its last stage still cancelled.
            # That stage has no writer to continue, so the next conflict gets
            # a fresh stage of its own instead of a stale refusal.
            reopened = bool(
                active_stage is not None
                and active_stage["state"] == "cancelled"
                and operation["target_kind"] == "parent"
                and operation["state"] in {"active", "escalated"}
            )
            # The shipped parent policy passes its operation key on a merge
            # conflict. Resolve that alias to the exact durable intent, never
            # treat the operation ID itself as failure evidence. Older pinned
            # policy artifacts must keep working for their entire episode.
            if trigger_id == operation_id and operation["target_kind"] == "parent":
                trigger_id = await self._resolve_parent_conflict_trigger_on(
                    conn,
                    dict(operation),
                    dict(active_stage) if active_stage is not None and not reopened else None,
                    starting_sha=starting_sha,
                )
                if trigger_id is None:
                    return {"outcome": "stale", "operation_id": operation_id}
            if reopened:
                return await self._start_fresh_parent_stage_on(
                    conn,
                    dict(operation),
                    dict(active_stage),
                    starting_sha=starting_sha,
                    trigger_id=trigger_id,
                    project_id=project_id,
                    now=activated_at,
                )
            if existing is not None:
                if active_stage is None:
                    return {"outcome": "invariant_error", "operation_id": operation_id}
                expected_deadline = (
                    f"repair-deadline-{operation_id}-{int(active_stage['ordinal'])}"
                )
                if (
                    active_stage["starting_sha"] == starting_sha
                    and active_stage["trigger_id"] == trigger_id
                    and active_stage["deadline_event_id"] == expected_deadline
                ):
                    return self._start_value(active_stage, outcome="already_started")
                if operation["target_kind"] != "parent":
                    return {"outcome": "invariant_error", "operation_id": operation_id}
                continuation_result = await self._continue_parent_stage_on(
                    conn,
                    operation=dict(operation),
                    stage=dict(active_stage),
                    starting_sha=starting_sha,
                    trigger_id=trigger_id,
                    project_id=project_id,
                    now=activated_at,
                )
                if continuation_result is None:
                    return {"outcome": "stale", "operation_id": operation_id}
                continuation_result, continuation_transition = continuation_result

            elif operation["state"] != "active" or int(operation["active_stage"]) != 0:
                return {"outcome": "stale", "operation_id": operation_id}

            if existing is None:
                try:
                    context = await self._start_context_on(
                        conn,
                        dict(operation),
                        starting_sha=starting_sha,
                        trigger_id=trigger_id,
                    )
                except _RepairInvariant:
                    return {"outcome": "invariant_error", "operation_id": operation_id}
                if context is None:
                    return {"outcome": "stale", "operation_id": operation_id}
                policy, boundary, subject = context
                deadline_at = activated_at + boundary.repair.primary_seconds
                row = {
                    "operation_id": operation_id,
                    "ordinal": 0,
                    "policy": boundary.repair.model_dump(mode="json"),
                    "intelligence_class": boundary.primary_intelligence_class,
                    # Deprecated column: routes come from the router, never
                    # from the policy, so new stages record no profile.
                    "profile_id": None,
                    "repair_task_id": None,
                    "writer_kind": None,
                    "starting_sha": starting_sha,
                    "trigger_id": trigger_id,
                    "current_subject": subject,
                    "deadline_event_id": f"repair-deadline-{operation_id}-0",
                    "started_at": activated_at,
                    "deadline_at": deadline_at,
                    "attempts": 0,
                    "dossier": await self._initial_dossier_on(
                        conn,
                        operation=dict(operation),
                        subject=subject,
                        starting_sha=starting_sha,
                        trigger_id=trigger_id,
                        boundary=boundary,
                        started_at=activated_at,
                        deadline_at=deadline_at,
                    ),
                    "state": "active",
                }
                await conn.execute(insert(integration_repair_stages).values(**row))
                return self._start_value(row, outcome="started")

        if continuation_transition is not None:
            await self.db.log_blocked_flips(continuation_transition.flipped)
            await self.db._notify_settled(continuation_transition.settled)
            await self.db._notify_ready(continuation_transition.ready)
        return continuation_result

    async def _start_fresh_parent_stage_on(
        self,
        conn,
        operation: dict[str, Any],
        cancelled: dict[str, Any],
        *,
        starting_sha: str,
        trigger_id: str,
        project_id: str,
        now: float,
    ) -> dict[str, Any]:
        """Open a writerless stage for a reopened collection's next conflict."""
        stale = {"outcome": "stale", "operation_id": operation["id"]}
        conflict = (
            await conn.execute(
                select(integration_promotion_intents)
                .where(integration_promotion_intents.c.id == trigger_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        parent = (
            await conn.execute(
                select(tasks).where(tasks.c.id == operation["parent_task_id"]).with_for_update()
            )
        ).mappings().one_or_none()
        if (
            conflict is None
            or parent is None
            or conflict["state"] != "conflict"
            or conflict["expected_target"] != starting_sha
            or conflict["operation_key"] != operation["id"]
            or conflict["fence_owner_id"] != operation["id"]
            or conflict["target_task_id"] != parent["id"]
            or conflict["project_id"] != project_id
            or parent["status"] != TaskStatus.PAUSED.value
        ):
            return stale
        ordinal = int(
            (
                await conn.execute(
                    select(func.max(integration_repair_stages.c.ordinal)).where(
                        integration_repair_stages.c.operation_id == operation["id"]
                    )
                )
            ).scalar_one()
        ) + 1
        plan = await self.fresh_parent_stage_plan_on(conn, operation, dict(conflict), ordinal)
        if isinstance(plan, str):
            return stale
        row = await self.fresh_parent_stage_row_on(
            conn,
            operation,
            dict(conflict),
            plan,
            now=now,
            extra={
                "previous_stage": {
                    "ordinal": int(cancelled["ordinal"]),
                    "state": cancelled["state"],
                    "trigger_id": cancelled["trigger_id"],
                    "starting_sha": cancelled["starting_sha"],
                },
            },
        )
        await conn.execute(insert(integration_repair_stages).values(**row))
        advanced = await conn.execute(
            update(integration_repair_operations)
            .where(
                integration_repair_operations.c.id == operation["id"],
                integration_repair_operations.c.state == operation["state"],
                integration_repair_operations.c.active_stage == cancelled["ordinal"],
            )
            .values(
                active_stage=ordinal,
                state="active" if ordinal == 0 else "escalated",
                updated_at=now,
            )
        )
        if advanced.rowcount != 1:
            raise _RepairInvariant("repair operation changed while opening a fresh stage")
        return self._start_value(row, outcome="started")

    async def fresh_parent_stage_plan_on(
        self,
        conn,
        operation: dict[str, Any],
        conflict: dict[str, Any],
        ordinal: int,
    ) -> dict[str, Any] | str:
        """The frozen budget of a writerless parent stage bound to *conflict*.

        Used where a parent collection has no live stage to continue: the
        stage ``aq integration reopen-collection`` opens, and the next conflict
        of a reopened collection.  Returns why when the operation's frozen
        route cannot repair *conflict*.
        """
        try:
            context = await self._start_context_on(
                conn,
                operation,
                starting_sha=conflict["expected_target"],
                trigger_id=conflict["id"],
            )
        except _RepairInvariant as exc:
            return f"the operation's frozen repair route is unusable: {exc}"
        if context is None:
            return f"conflict {conflict['id']} does not match the operation's frozen route"
        _policy, boundary, subject = context
        repair = boundary.repair
        if ordinal == 0:
            intelligence_class = (
                boundary.primary_intelligence_class or repair.debug_intelligence_class
            )
            timeout, limit = repair.primary_seconds, repair.primary_attempts
        else:
            intelligence_class = repair.debug_intelligence_class
            timeout, limit = repair.debug_seconds, repair.debug_attempts
        return {
            "ordinal": ordinal,
            "intelligence_class": intelligence_class,
            "timeout_seconds": timeout,
            "attempt_limit": limit,
            "boundary": boundary,
            "subject": subject,
        }

    async def fresh_parent_stage_row_on(
        self,
        conn,
        operation: dict[str, Any],
        conflict: dict[str, Any],
        plan: dict[str, Any],
        *,
        now: float,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A writerless stage row bound to *conflict*, shaped like a debug escalation's.

        Its dossier carries the continuation marker, so a parent policy's
        literal stage-zero dispatch selects it (:meth:`_effective_dispatch_stage_on`).
        """
        ordinal = int(plan["ordinal"])
        starting_sha = conflict["expected_target"]
        deadline_at = now + plan["timeout_seconds"]
        dossier = await self._initial_dossier_on(
            conn,
            operation=operation,
            subject=plan["subject"],
            starting_sha=starting_sha,
            trigger_id=conflict["id"],
            boundary=plan["boundary"],
            started_at=now,
            deadline_at=deadline_at,
        )
        dossier["budget"] = {
            "ordinal": ordinal,
            "started_at": now,
            "deadline_at": deadline_at,
            "attempt_limit": plan["attempt_limit"],
            "attempts": 0,
        }
        dossier["current_conflict"] = {
            "intent_id": conflict["id"],
            "source_task_id": conflict["source_task_id"],
            "source_head": conflict["source_head"],
            "source_base": conflict["source_base"],
            "expected_target": starting_sha,
            "diagnostics": conflict["conflict_diagnostics"] or {},
        }
        dossier["continuations"] = [
            {"intent_id": conflict["id"], "starting_sha": starting_sha, "recorded_at": now}
        ]
        dossier.update(extra or {})
        return {
            "operation_id": operation["id"],
            "ordinal": ordinal,
            "policy": plan["boundary"].repair.model_dump(mode="json"),
            "intelligence_class": plan["intelligence_class"],
            # Deprecated column: the router chooses the route from the class hint.
            "profile_id": None,
            "repair_task_id": None,
            "writer_kind": None,
            "starting_sha": starting_sha,
            "trigger_id": conflict["id"],
            "current_subject": plan["subject"],
            "deadline_event_id": f"repair-deadline-{operation['id']}-{ordinal}",
            "success_subject": None,
            "success_evidence_id": None,
            "started_at": now,
            "deadline_at": deadline_at,
            "attempts": 0,
            "dossier": dossier,
            "state": "active",
        }

    async def _resolve_parent_conflict_trigger_on(
        self,
        conn,
        operation: dict[str, Any],
        active_stage: dict[str, Any] | None,
        *,
        starting_sha: str,
    ) -> str | None:
        """Resolve a frozen operation-key alias to one exact conflict intent.

        A retry of the intent already bound to the active stage remains valid
        after that intent advances past ``conflict``. A different intent must
        still be the sole persisted conflict for this exact parent subject.
        """
        scope = (
            integration_promotion_intents.c.operation_key == operation["id"],
            integration_promotion_intents.c.target_task_id == operation["parent_task_id"],
            integration_promotion_intents.c.expected_target == starting_sha,
        )
        if (
            active_stage is not None
            and active_stage.get("trigger_id")
            and active_stage.get("starting_sha") == starting_sha
        ):
            current = (
                await conn.execute(
                    select(integration_promotion_intents.c.id).where(
                        *scope,
                        integration_promotion_intents.c.id == active_stage["trigger_id"],
                    )
                )
            ).scalar_one_or_none()
            if current is not None:
                return str(current)
        intent_ids = (
            await conn.execute(
                select(integration_promotion_intents.c.id)
                .where(
                    *scope,
                    integration_promotion_intents.c.state == "conflict",
                )
                .order_by(integration_promotion_intents.c.id)
                .limit(2)
            )
        ).scalars().all()
        return str(intent_ids[0]) if len(intent_ids) == 1 else None

    async def continue_current_parent_conflict_on(
        self,
        conn,
        operation: dict[str, Any],
        stage: dict[str, Any],
        *,
        project_id: str,
        now: float,
        validate_only: bool = False,
    ) -> dict[str, Any]:
        """Continue a resumed parent repair from its one current conflict.

        Recovery owns the parent-state transition and already holds the
        operation, parent, owner, and project locks. It must not reconstruct
        a conflict from an event that may name a completed prior delegate.
        Instead, resolve exactly one durable conflict while those locks are
        held, then reuse the ordinary continuation checks below.
        """
        if operation["target_kind"] != "parent":
            return {"outcome": "none"}
        parent = (
            await conn.execute(
                select(tasks)
                .where(tasks.c.id == operation["parent_task_id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        if parent is None:
            return {"outcome": "stale"}
        conflicts = (
            await conn.execute(
                select(integration_promotion_intents)
                .where(
                    integration_promotion_intents.c.operation_key == operation["id"],
                    integration_promotion_intents.c.target_task_id == parent["id"],
                    integration_promotion_intents.c.repository_id == parent["repo_id"],
                    integration_promotion_intents.c.target_branch == parent["branch_name"],
                    integration_promotion_intents.c.state.in_(("conflict", "resolution_reserved")),
                )
                .order_by(integration_promotion_intents.c.id)
                .limit(2)
                .with_for_update()
            )
        ).mappings().all()
        if not conflicts:
            return {"outcome": "none"}
        if len(conflicts) != 1 or conflicts[0]["state"] != "conflict":
            return {"outcome": "stale"}
        conflict = dict(conflicts[0])
        continuation = (stage["dossier"] or {}).get("continuations", [])
        if (
            continuation
            and continuation[-1].get("intent_id") == conflict["id"]
            and continuation[-1].get("starting_sha") == conflict["expected_target"]
            and stage["trigger_id"] == conflict["id"]
            and stage["starting_sha"] == conflict["expected_target"]
        ):
            return {"outcome": "already_continued"}
        result = await self._continue_parent_stage_on(
            conn,
            operation=operation,
            stage=stage,
            starting_sha=conflict["expected_target"],
            trigger_id=conflict["id"],
            project_id=project_id,
            now=now,
            allow_blocked_parent=validate_only,
            validate_only=validate_only,
        )
        if result is None:
            return {"outcome": "stale"}
        if validate_only:
            return {"outcome": "ready"}
        value, transition = result
        return {"outcome": "continued", "value": value, "transition": transition}

    async def _continue_parent_stage_on(
        self,
        conn,
        *,
        operation: dict[str, Any],
        stage: dict[str, Any],
        starting_sha: str,
        trigger_id: str,
        project_id: str,
        now: float,
        allow_blocked_parent: bool = False,
        validate_only: bool = False,
    ):
        """Rebind one detached delegate to a later conflict without new budget."""
        if (
            operation["state"] not in {"active", "escalated"}
            or int(operation["active_stage"]) != int(stage["ordinal"])
            or stage["state"] not in {"active", "awaiting_completion"}
            or stage["writer_kind"] != "repair_delegate"
            or not stage["repair_task_id"]
            or stage["deadline_at"] is None
            or now >= float(stage["deadline_at"])
        ):
            return None
        try:
            context = await self._start_context_on(
                conn,
                operation,
                starting_sha=starting_sha,
                trigger_id=trigger_id,
            )
        except _RepairInvariant:
            return None
        if context is None:
            return None
        _policy, _boundary, subject = context
        parent = (
            await conn.execute(
                select(tasks).where(tasks.c.id == operation["parent_task_id"])
            )
        ).mappings().one_or_none()
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == operation["parent_task_id"]
                )
            )
        ).mappings().one_or_none()
        conflict_rows = (
            await conn.execute(
                select(integration_promotion_intents)
                .where(
                    integration_promotion_intents.c.operation_key == operation["id"],
                    integration_promotion_intents.c.target_task_id
                    == operation["parent_task_id"],
                    integration_promotion_intents.c.repository_id == parent["repo_id"]
                    if parent is not None
                    else integration_promotion_intents.c.repository_id.is_(None),
                    integration_promotion_intents.c.target_branch == parent["branch_name"]
                    if parent is not None
                    else integration_promotion_intents.c.target_branch.is_(None),
                    integration_promotion_intents.c.state.in_(
                        ("conflict", "resolution_reserved")
                    ),
                )
                .order_by(integration_promotion_intents.c.id)
                .limit(2)
                .with_for_update()
            )
        ).mappings().all()
        if len(conflict_rows) != 1:
            return None
        conflict = conflict_rows[0]
        source = (
            await conn.execute(select(tasks).where(tasks.c.id == conflict["source_task_id"]))
        ).mappings().one_or_none()
        owner = (
            await conn.execute(
                select(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == conflict["repository_id"],
                    integration_branch_owners.c.ref == conflict["target_branch"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        delegate = (
            await conn.execute(
                select(tasks)
                .where(tasks.c.id == stage["repair_task_id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        sessions_for_delegate = (
            await conn.execute(select(sessions).where(sessions.c.task_id == stage["repair_task_id"]))
        ).mappings().all()
        locked_workspace = (
            await conn.execute(
                select(workspaces.c.id)
                .where(workspaces.c.locked_by_task_id == stage["repair_task_id"])
                .limit(1)
            )
        ).first()
        live_mutation = (
            await conn.execute(
                select(integration_candidate_ref_mutations.c.id)
                .where(
                    integration_candidate_ref_mutations.c.repository_id
                    == conflict["repository_id"],
                    integration_candidate_ref_mutations.c.branch == conflict["target_branch"],
                    integration_candidate_ref_mutations.c.state == "reserved",
                )
                .limit(1)
            )
        ).first()
        if (
            parent is None
            or checkpoint is None
            or parent["project_id"] != project_id
            or parent["status"]
            not in (
                {TaskStatus.PAUSED.value, TaskStatus.BLOCKED.value}
                if allow_blocked_parent
                else {TaskStatus.PAUSED.value}
            )
            or checkpoint["episode_id"] != operation["episode_id"]
            or checkpoint["state"] != "awaiting_children"
            or conflict["id"] != trigger_id
            or conflict["state"] != "conflict"
            or conflict["project_id"] != project_id
            or conflict["expected_target"] != starting_sha
            or conflict["fence_owner_id"] != operation["id"]
            or source is None
            or source["parent_task_id"] != parent["id"]
            or source["project_id"] != project_id
            or source["repo_id"] != parent["repo_id"]
            or source["status"] != TaskStatus.COMPLETED.value
            or owner is None
            or owner["owner_id"] != operation["id"]
            or owner["owner_role"] != "collector"
            or owner["handoff_state"] != "reserved"
            or owner["session_id"] is not None
            or owner["workspace_id"] is not None
            or int(owner["fence_token"]) != int(conflict["fence_token"])
            or delegate is None
            or delegate["project_id"] != project_id
            or delegate["parent_task_id"] is not None
            or delegate["repo_id"] != parent["repo_id"]
            or delegate["branch_name"] != parent["branch_name"]
            or delegate["created_by_kind"] != "integration_repair"
            or delegate["created_by_id"] != operation["id"]
            or delegate["status"] != TaskStatus.COMPLETED.value
            or delegate["assigned_agent_id"] is not None
            or any(
                row["state"] != "stopped" or row["claim_phase"] is not None
                for row in sessions_for_delegate
            )
            or locked_workspace is not None
            or live_mutation is not None
        ):
            return None

        if validate_only:
            return {"outcome": "ready"}

        dossier = dict(stage["dossier"] or {})
        continuations = list(dossier.get("continuations", []))
        continuations.append(
            {
                "intent_id": trigger_id,
                "starting_sha": starting_sha,
                "recorded_at": now,
            }
        )
        dossier.update(
            {
                "starting_sha": starting_sha,
                "trigger_id": trigger_id,
                "branch_sha": starting_sha,
                "receipts": await self._current_receipts_on(conn, operation),
                "continuations": continuations,
                "current_conflict": {
                    "intent_id": trigger_id,
                    "source_task_id": conflict["source_task_id"],
                    "source_head": conflict["source_head"],
                    "source_base": conflict["source_base"],
                    "expected_target": starting_sha,
                    "diagnostics": conflict["conflict_diagnostics"] or {},
                },
            }
        )
        updated_stage = stage | {
            "starting_sha": starting_sha,
            "trigger_id": trigger_id,
            "current_subject": subject,
            "success_subject": None,
            "success_evidence_id": None,
            "state": "active",
            "dossier": dossier,
        }
        changed = await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == operation["id"],
                integration_repair_stages.c.ordinal == stage["ordinal"],
                integration_repair_stages.c.trigger_id == stage["trigger_id"],
                integration_repair_stages.c.starting_sha == stage["starting_sha"],
                integration_repair_stages.c.repair_task_id == stage["repair_task_id"],
                integration_repair_stages.c.state.in_(("active", "awaiting_completion")),
            )
            .values(
                starting_sha=starting_sha,
                trigger_id=trigger_id,
                current_subject=subject,
                success_subject=None,
                success_evidence_id=None,
                state="active",
                dossier=dossier,
            )
        )
        if changed.rowcount != 1:
            raise RuntimeError("repair continuation lost its stage compare-and-swap")
        transition = await self.db._apply_transition(
            conn,
            stage["repair_task_id"],
            TaskStatus.PAUSED,
            context="integration_repair_continuation",
            force=True,
            _manual_pause_control=True,
            assigned_agent_id=None,
            description=self._delegate_description(operation, updated_stage),
        )
        return self._start_value(updated_stage, outcome="already_started") | {
            "continued": True
        }, transition

    @parent_engine_guard("operation", outcome="continue", refusal={"action": "stale", "attempts": 0})
    @root_engine_guard("operation", outcome="continue", refusal={"action": "stale", "attempts": 0})
    async def record_result(
        self, operation_id: str, evidence_id: str, *, now: float | None = None
    ) -> dict[str, Any]:
        """Record one exact check attempt under the current stage budget."""
        recorded_at = self.clock() if now is None else now
        entry_stage = parent_stage_at_entry(self.db, operation_id)
        post_transition = None
        async with self.db.immediate() as conn:
            operation = (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(integration_repair_operations.c.id == operation_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if operation is None:
                return self._result_value("continue", "stale", 0)
            if entry_stage is not None and operation["active_stage"] != entry_stage:
                # Deadline processing won after the guarded call observed its
                # stage. Evidence for that visit cannot count in its successor.
                return self._result_value("continue", "stale", 0)
            previous = (
                await conn.execute(
                    select(integration_repair_stage_evidence).where(
                        integration_repair_stage_evidence.c.evidence_id == evidence_id
                    )
                )
            ).mappings().one_or_none()
            if previous is not None:
                if previous["operation_id"] != operation_id:
                    return self._result_value("continue", "stale", 0)
                linked_stage = (
                    await conn.execute(
                        select(integration_repair_stages).where(
                            integration_repair_stages.c.operation_id == operation_id,
                            integration_repair_stages.c.ordinal == previous["ordinal"],
                        )
                    )
                ).mappings().one()
                value = self._result_value(
                    previous["result_outcome"],
                    "duplicate",
                    int(linked_stage["attempts"]),
                )
                if previous["result_outcome"] == "escalate":
                    value["stage"] = int(previous["ordinal"]) + 1
                return value
            stage = (
                await conn.execute(
                    select(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.ordinal == operation["active_stage"],
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if (
                operation["state"] == "human_required"
                and stage is not None
                and stage["state"] in {"failed", "expired"}
            ):
                return self._result_value(
                    "budget_exhausted", "block_for_human", int(stage["attempts"])
                )
            if stage is not None and (stage["dossier"] or {}).get("supervisor_recovery"):
                return self._result_value(
                    "budget_exhausted", "supervisor_recovery", int(stage["attempts"])
                )
            if stage is None or stage["state"] not in {"active", "awaiting_completion"}:
                return self._result_value(
                    "continue", "stale", int(stage["attempts"]) if stage else 0
                )
            evidence = (
                await conn.execute(
                    select(integration_check_evidence).where(
                        integration_check_evidence.c.id == evidence_id
                    )
                )
            ).mappings().one_or_none()
            if not self._evidence_matches(operation, stage, evidence):
                return self._result_value(
                    "continue", "stale", int(stage["attempts"])
                )
            counted = bool(
                evidence["classification"] != "infrastructure"
                and evidence["conclusion"] in {"success", "failure"}
            )
            action = "repair"
            if not counted:
                action = (
                    "infrastructure_retry"
                    if evidence["classification"] == "infrastructure"
                    else "inconclusive"
                )
            attempts = int(stage["attempts"]) + int(counted)
            dossier = self._dossier_with_evidence(
                stage["dossier"], evidence, attempts=attempts
            )
            if operation["target_kind"] == "batch" and counted:
                dossier["batch_failure_streak"] = (
                    int((stage["dossier"] or {}).get("batch_failure_streak", 0)) + 1
                    if evidence["conclusion"] == "failure"
                    else 0
                )
            outcome = "continue"
            result_extra: dict[str, Any] = {}
            limit = (
                RepairPolicy.model_validate(stage["policy"]).primary_attempts
                if int(stage["ordinal"]) == 0
                else RepairPolicy.model_validate(stage["policy"]).debug_attempts
            )
            if counted and evidence["conclusion"] == "success":
                action = "completion_ready"
                await conn.execute(
                    update(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.ordinal == stage["ordinal"],
                    )
                    .values(
                        attempts=attempts,
                        state="awaiting_completion",
                        success_subject=stage["current_subject"],
                        success_evidence_id=evidence_id,
                        dossier=dossier,
                    )
                )
            elif counted and evidence["conclusion"] == "failure" and attempts >= limit:
                if int(stage["ordinal"]) == 0 or RepairPolicy.model_validate(stage["policy"]).on_exhausted == "continue":
                    outcome = "escalate"
                    action = "dispatch_debug"
                    continued = await self._activate_debug_on(
                        conn,
                        operation=dict(operation),
                        primary=dict(stage) | {"dossier": dossier},
                        attempts=attempts,
                        now=recorded_at,
                    )
                    if continued:
                        result_extra["stage"] = int(stage["ordinal"]) + 1
                    else:
                        outcome = "budget_exhausted"
                        action = "supervisor_recovery"
                        result_extra["stage"] = int(stage["ordinal"])
                else:
                    outcome = "human_required"
                    action = "block_for_human"
                    post_transition = await self._human_block_on(
                        conn,
                        operation=dict(operation),
                        stage=dict(stage) | {"dossier": dossier},
                        attempts=attempts,
                        now=recorded_at,
                        terminal_state="failed",
                    )
            elif counted:
                await conn.execute(
                    update(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.ordinal == stage["ordinal"],
                    )
                    .values(attempts=attempts, dossier=dossier)
                )
            else:
                await conn.execute(
                    update(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.ordinal == stage["ordinal"],
                    )
                    .values(dossier=dossier)
                )
            await conn.execute(
                insert(integration_repair_stage_evidence).values(
                    operation_id=operation_id,
                    ordinal=stage["ordinal"],
                    evidence_id=evidence_id,
                    counted_attempt=counted,
                    result_outcome=outcome,
                    result_action=action,
                    recorded_at=recorded_at,
                )
            )
            if (
                operation["target_kind"] == "batch"
                and counted
                and evidence["conclusion"] == "failure"
                and dossier["batch_failure_streak"] >= STUCK_BATCH_ATTEMPTS
            ):
                await self._escalate_stuck_batch_on(
                    conn, operation, failures=dossier["batch_failure_streak"], now=recorded_at
                )
            result = self._result_value(outcome, action, attempts) | result_extra
        if post_transition is not None:
            await self.db.log_blocked_flips(post_transition.flipped)
            await self.db._notify_settled(post_transition.settled)
            await self.db._notify_ready(post_transition.ready)
        return result

    @staticmethod
    async def _escalate_stuck_batch_on(
        conn, operation, *, failures: int, now: float
    ) -> None:
        """Notify the supervisor once after three consecutive counted failures.

        The stage dossier carries the durable streak into a debug stage. The
        message's stable primary key prevents repeat notifications after more
        failures or process restarts.
        """
        batch = (
            await conn.execute(
                select(integration_batches).where(
                    integration_batches.c.id == operation["batch_id"]
                )
            )
        ).mappings().one()
        members = (
            await conn.execute(
                select(integration_batch_members.c.task_id)
                .where(integration_batch_members.c.batch_id == operation["batch_id"])
                .order_by(integration_batch_members.c.ordinal)
            )
        ).scalars().all()
        age_seconds = max(0, int(now - batch["created_at"]))
        await conn.execute(
            pg_insert(messages)
            .values(
                id=f"msg-stuck-batch-{operation['batch_id']}",
                project_id=batch["project_id"],
                from_kind="system",
                from_id="integration-repair",
                to_kind="session",
                to_id=f"supervisor-{batch['project_id']}",
                subject=f"Integration batch {operation['batch_id']} needs attention",
                body=(
                    f"Integration batch {operation['batch_id']} has {failures} consecutive "
                    f"failed repair checks after {age_seconds} seconds. "
                    f"Members: {', '.join(members) or '(none)'}"
                ),
                created_at=now,
                priority=50,
                archive_after_inject=1,
                body_kind="integration_stuck_batch",
            )
            .on_conflict_do_nothing(index_elements=[messages.c.id])
        )

    async def _successor_for_closed_writer(self, operation_id, stage):
        async with self.db.immediate() as conn:
            stage = await self._effective_dispatch_stage_on(conn, operation_id, stage)
            context = await self._dispatch_context_on(conn, operation_id, stage)
            if context is None:
                return stage
            operation, repair_stage, _target, _project = context
            if (RepairPolicy.model_validate(repair_stage["policy"]).on_exhausted != "continue"
                    or not repair_stage["repair_task_id"] or repair_stage["state"] != "active"):
                return stage
            status = (await conn.execute(select(tasks.c.status).where(
                tasks.c.id == repair_stage["repair_task_id"]))).scalar_one_or_none()
            if status != "COMPLETED":
                return stage
            continued = await self._activate_debug_on(conn, operation=dict(operation), primary=dict(repair_stage),
                attempts=repair_stage["attempts"], now=self.clock())
            return stage + 1 if continued else stage

    async def missing_delegate_reservations(
        self, *, limit: int = 100, after: str | None = None
    ) -> list[dict]:
        """Read detached current delegates that cannot pass owner admission."""
        stage, operation, owner = (
            integration_repair_stages, integration_repair_operations, integration_branch_owners
        )
        valid_owner = select(owner.c.id).where(
            owner.c.repository_id == tasks.c.repo_id,
            or_(
                owner.c.ref == tasks.c.branch_name,
                owner.c.ref == "refs/heads/" + tasks.c.branch_name,
                "refs/heads/" + owner.c.ref == tasks.c.branch_name,
            ),
            owner.c.owner_id == tasks.c.id,
            owner.c.owner_role == "repair",
            owner.c.handoff_state == "reserved",
        ).exists()
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(
                select(stage.c.operation_id, stage.c.ordinal, stage.c.repair_task_id)
                .select_from(stage.join(operation, operation.c.id == stage.c.operation_id)
                             .join(tasks, tasks.c.id == stage.c.repair_task_id))
                .where(
                    operation.c.active_stage == stage.c.ordinal,
                    operation.c.state.in_(("active", "escalated")),
                    stage.c.state.in_(("active", "awaiting_completion")),
                    stage.c.writer_kind == "repair_delegate",
                    tasks.c.status.in_(("READY", "PAUSED", "BLOCKED")),
                    operation.c.id > (after or ""),
                    ~valid_owner,
                )
                .order_by(stage.c.operation_id, stage.c.ordinal).limit(limit)
            )).mappings().all()
        return [dict(row) for row in rows]

    async def reconcile_delegate_reservations(self, now: float) -> None:
        """Retry interrupted handoffs without restarting the repair clock."""
        rows = await self.missing_delegate_reservations(after=self._reservation_cursor)
        if not rows and self._reservation_cursor is not None:
            self._reservation_cursor = None
            rows = await self.missing_delegate_reservations()
        for row in rows:
            # A persistently busy or broken handoff must not starve later pages.
            self._reservation_cursor = row["operation_id"]
            await self.dispatch(row["operation_id"], int(row["ordinal"]))

    async def reserve_delegate(self, task_id: str) -> dict:
        """Recover only the delegate already bound to the current stage."""
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(
                select(integration_repair_stages.c.operation_id,
                       integration_repair_stages.c.ordinal)
                .join(integration_repair_operations,
                      integration_repair_operations.c.id
                      == integration_repair_stages.c.operation_id)
                .where(
                    integration_repair_stages.c.repair_task_id == task_id,
                    integration_repair_stages.c.writer_kind == "repair_delegate",
                    integration_repair_operations.c.active_stage
                    == integration_repair_stages.c.ordinal,
                    integration_repair_operations.c.state.in_(("active", "escalated")),
                )
            )).mappings().all()
        if len(rows) != 1:
            return {"outcome": "not_eligible", "reason": "task is not the active repair delegate"}
        result = await self.dispatch(rows[0]["operation_id"], int(rows[0]["ordinal"]))
        outcome = {"dispatched": "acquired", "already_dispatched": "already_reserved"}.get(
            result["outcome"], "not_eligible"
        )
        return {"outcome": outcome, "dispatch": result}

    @parent_engine_guard("operation", outcome="stale")
    @root_engine_guard("operation", outcome="busy")
    async def dispatch(self, operation_id: str, stage: int) -> dict[str, Any]:
        """Create and safely hand off to the exact current repair writer."""
        if stage < 0:
            return self._dispatch_value("stale", operation_id, stage)
        async with self.db.immediate() as conn:
            project_id = (await conn.execute(
                select(integration_batches.c.project_id)
                .join(integration_repair_operations,
                      integration_repair_operations.c.batch_id == integration_batches.c.id)
                .where(integration_repair_operations.c.id == operation_id)
            )).scalar_one_or_none()
            if project_id is not None:
                from src.integration.accepted_repair import accepted_candidate_on

                await self.db.lock_hierarchy_project(conn, project_id)
                operation = (await conn.execute(select(integration_repair_operations).where(
                    integration_repair_operations.c.id == operation_id,
                ).with_for_update())).mappings().one()
                current = (await conn.execute(select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == operation["active_stage"],
                ).with_for_update())).mappings().one_or_none()
                if current is not None and await accepted_candidate_on(conn, operation, current):
                    # Continuous repair policies must not interpret the original
                    # delegate's terminal bookkeeping as demand for another writer.
                    return self._dispatch_value(
                        "already_dispatched", operation_id, int(current["ordinal"]),
                        repair_task_id=current["repair_task_id"], writer_kind="repair_delegate",
                    )
        stage = await self._successor_for_closed_writer(operation_id, stage)

        # The durable relationship and paused task are committed before the
        # ownership callback is allowed to stop/detach the predecessor.
        async with self.db.immediate() as conn:
            stage = await self._effective_dispatch_stage_on(conn, operation_id, stage)
            context = await self._dispatch_context_on(conn, operation_id, stage)
            if context is None:
                return self._dispatch_value("stale", operation_id, stage)
            operation, repair_stage, target, project_id = context
            repair_task_id = repair_stage["repair_task_id"]
            reused = await self._reuse_verifier_on(
                conn, operation, repair_stage, target, project_id
            )
            if reused is not None:
                return reused
            if repair_task_id is not None:
                task = (
                    await conn.execute(
                        select(tasks).where(tasks.c.id == repair_task_id).with_for_update()
                    )
                ).mappings().one_or_none()
                if task is None and repair_stage["writer_kind"] == "repair_delegate":
                    task = await self._restore_archived_delegate_on(
                        conn, repair_task_id, operation, repair_stage, target, project_id
                    )
                if repair_stage["writer_kind"] != "repair_delegate":
                    return await self._dispatch_unknown(
                        operation_id, stage, "writer_kind",
                        f"stage writer {repair_task_id} is a {repair_stage['writer_kind']}, "
                        "not a repair delegate this command can hand off",
                        conn=conn, repair_task_id=repair_task_id,
                        writer_kind=repair_stage["writer_kind"],
                    )
                if task is None:
                    return await self._dispatch_unknown(
                        operation_id, stage, "delegate_missing",
                        f"repair delegate {repair_task_id} has no task row and no "
                        "restorable archive",
                        conn=conn, repair_task_id=repair_task_id,
                        writer_kind=repair_stage["writer_kind"],
                    )
                if await self.db._read_manual_pause(conn, repair_task_id) is not None:
                    # An operator's hold on the delegate is a human decision.
                    return self._dispatch_value(
                        "human_required",
                        operation_id,
                        stage,
                        repair_task_id=repair_task_id,
                        writer_kind="repair_delegate",
                    ) | {"reason": f"repair delegate {repair_task_id} is held by an operator"}
                if not self._delegate_task_matches(task, operation, target, project_id):
                    return await self._dispatch_unknown(
                        operation_id, stage, "delegate_mismatch",
                        f"repair delegate {repair_task_id} ({task['status']}) does not match "
                        "the operation's project, branch, provenance or a dispatchable status",
                        conn=conn, repair_task_id=repair_task_id, writer_kind="repair_delegate",
                    )
            else:
                # The delegate is filed with the stage's class as a hint; the
                # router writes its route.  A stage's ``profile_id`` column is
                # deprecated and never chooses one.
                intelligence_class = repair_stage["intelligence_class"]
                if not intelligence_class:
                    return self._dispatch_value(
                        "configuration_blocked", operation_id, stage
                    )
                repair_task_id = f"repair-{operation_id}-{stage}"
                if (repair_stage["dossier"] or {}).get("membership_ejections"):
                    repair_task_id += f"-r{repair_stage['current_subject']['revision']}"
                collision = (
                    await conn.execute(
                        select(tasks).where(tasks.c.id == repair_task_id).with_for_update()
                    )
                ).mappings().one_or_none()
                if collision is not None:
                    return await self._dispatch_unknown(
                        operation_id, stage, "delegate_id_collision",
                        f"task {repair_task_id} already exists ({collision['status']}, created "
                        f"by {collision['created_by_kind']} {collision['created_by_id']}) but "
                        "is not linked to this stage",
                        conn=conn,
                    )
                await self.db.create_task(
                    Task(
                        id=repair_task_id,
                        project_id=project_id,
                        title=f"Repair integration stage {stage}",
                        description=await self._delegate_description_on(
                            conn, operation, repair_stage
                        ),
                        status=TaskStatus.PAUSED,
                        parent_task_id=None,
                        repo_id=target.repository_id,
                        branch_name=target.branch,
                        class_hint=intelligence_class,
                        created_by_kind="integration_repair",
                        created_by_id=operation_id,
                    ),
                    conn=conn,
                )
                linked = await conn.execute(
                    update(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.ordinal == stage,
                        integration_repair_stages.c.repair_task_id.is_(None),
                        integration_repair_stages.c.writer_kind.is_(None),
                        integration_repair_stages.c.state.in_(
                            ("active", "awaiting_completion")
                        ),
                    )
                    .values(
                        repair_task_id=repair_task_id,
                        writer_kind="repair_delegate",
                    )
                )
                if linked.rowcount != 1:
                    raise RuntimeError("repair stage writer changed while linking delegate")

        owner = await self._ownership.get_owner(target)
        if owner is None:
            return await self._dispatch_unknown(
                operation_id, stage, "owner_missing",
                f"integration branch {target.branch} has no owner row to hand to "
                f"{repair_task_id}",
                repair_task_id=repair_task_id, writer_kind="repair_delegate",
            )
        # The deadline retires authority, not the writer's unpublished work.
        # Use the existing provider-backed recovery after drain has stopped
        # this exact predecessor. A live/unknown writer remains busy. This
        # targeted handoff is independent of the optional general quiet sweep.
        if (
            operation["target_kind"] == "batch"
            and stage > 0
            and await self._is_primary_writer(
                owner, operation_id, ordinal=stage - 1,
                states=frozenset({"attached", "handoff_pending"}),
            )
        ):
            if self._owner_recovery is None:
                return self._dispatch_value(
                    "busy", operation_id, stage, repair_task_id=repair_task_id,
                    writer_kind="repair_delegate",
                )
            recovered = await self._owner_recovery.recover(
                owner["id"], principal="repair_stage_rollover"
            )
            if recovered.outcome not in {"released", "preserved_and_released"}:
                return self._dispatch_value(
                    "busy", operation_id, stage, repair_task_id=repair_task_id,
                    writer_kind="repair_delegate",
                )
            owner = await self._ownership.get_owner(target)
            if owner is None:
                return self._dispatch_value("stale", operation_id, stage)

        from src.integration.repair_progress import batch_recovery_progress

        progress = None
        if owner["handoff_state"] != "attached" or owner["owner_id"] != repair_task_id:
            try:
                progress = await batch_recovery_progress(
                    self.db, self._owner_recovery, operation, repair_stage, target
                )
            except GitError as exc:
                async with self.db.immediate() as conn:
                    context = await self._dispatch_context_on(conn, operation_id, stage)
                    if context is not None and context[1]["repair_task_id"] == repair_task_id:
                        dossier = dict(context[1]["dossier"] or {})
                        dossier["preserved_progress_blocker"] = str(exc)
                        await conn.execute(update(integration_repair_stages).where(
                            integration_repair_stages.c.operation_id == operation_id,
                            integration_repair_stages.c.ordinal == stage,
                        ).values(dossier=dossier))
                # Preserved history that no longer proves its lineage is an
                # unexplained remote move: a human decision, not a retry.
                return self._dispatch_value(
                    "human_required", operation_id, stage, repair_task_id=repair_task_id,
                    writer_kind="repair_delegate",
                ) | {"reason": f"preserved repair progress is unusable: {exc}"}
        if (
            owner["owner_id"] == repair_task_id
            and owner["owner_role"] == "repair"
            and owner["handoff_state"] in {"reserved", "attached"}
        ):
            fence = Fence(
                target=target,
                owner_id=repair_task_id,
                token=int(owner["fence_token"]),
            )
            replay = True
        elif owner["handoff_state"] == "released":
            # ``_archive_one`` releases detached reservations along with the
            # task. A legacy archive can still be restored for an active
            # repair stage, but its old fence must be explicitly reclaimed
            # before the restored task becomes dispatchable again.
            try:
                fence = await self._ownership.acquire(target, repair_task_id, "repair")
            except (BranchBusy, StaleFence):
                return self._dispatch_value(
                    "busy",
                    operation_id,
                    stage,
                    repair_task_id=repair_task_id,
                    writer_kind="repair_delegate",
                )
            replay = False
        else:
            retained_primary = stage > 0 and await self._is_primary_writer(
                owner, operation_id, ordinal=stage - 1
            )
            if retained_primary:
                fence = await self._retained_debug_handoff(
                    operation,
                    repair_stage,
                    target,
                    owner,
                    repair_task_id,
                )
                if fence is None:
                    return self._dispatch_value(
                        "busy",
                        operation_id,
                        stage,
                        repair_task_id=repair_task_id,
                        writer_kind="repair_delegate",
                    )
            else:
                released_primary = stage > 0 and await self._is_primary_writer(
                    owner, operation_id, ordinal=stage - 1, states=_TRANSFERABLE_PRIMARY_STATES
                )
                if not released_primary and not self._predecessor_matches(
                    owner, operation
                ):
                    return await self._dispatch_unknown(
                        operation_id, stage, "owner_not_predecessor",
                        f"the fence belongs to {owner['owner_id']} ({owner['owner_role']}, "
                        f"{owner['handoff_state']}), which is not a predecessor this stage "
                        f"may take over for {repair_task_id}",
                        repair_task_id=repair_task_id, writer_kind="repair_delegate",
                    )
                old_fence = Fence(
                    target=target,
                    owner_id=owner["owner_id"],
                    token=int(owner["fence_token"]),
                )
                try:
                    fence = await self._ownership.transfer(
                        old_fence, repair_task_id, "repair"
                    )
                except (BranchBusy, StaleFence):
                    return self._dispatch_value(
                        "busy",
                        operation_id,
                        stage,
                        repair_task_id=repair_task_id,
                        writer_kind="repair_delegate",
                    )
            replay = False

        async with self.db.immediate() as conn:
            context = await self._dispatch_context_on(conn, operation_id, stage)
            owner = (
                await conn.execute(
                    select(integration_branch_owners)
                    .where(
                        integration_branch_owners.c.repository_id
                        == target.repository_id,
                        integration_branch_owners.c.ref == target.branch,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            task = (
                await conn.execute(
                    select(tasks).where(tasks.c.id == repair_task_id).with_for_update()
                )
            ).mappings().one_or_none()
            attached_session = None
            attached_workspace = None
            if owner is not None and owner["handoff_state"] == "attached":
                attached_session = (
                    await conn.execute(
                        select(sessions)
                        .where(sessions.c.id == owner["session_id"])
                        .with_for_update()
                    )
                ).mappings().one_or_none()
                attached_workspace = (
                    await conn.execute(
                        select(workspaces)
                        .where(workspaces.c.id == owner["workspace_id"])
                        .with_for_update()
                    )
                ).mappings().one_or_none()
            reserved = bool(
                owner is not None
                and owner["handoff_state"] == "reserved"
                and task is not None
                and task["status"]
                in {TaskStatus.PAUSED.value, TaskStatus.READY.value}
            )
            attached = bool(
                owner is not None
                and owner["handoff_state"] == "attached"
                and task is not None
                and task["status"]
                in {TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value}
                and attached_session is not None
                and attached_session["task_id"] == repair_task_id
                and attached_session["project_id"] == project_id
                and attached_session["state"] in {"starting", "running", "draining"}
                and attached_workspace is not None
                and attached_workspace["locked_by_task_id"] == repair_task_id
                and attached_workspace["project_id"] == project_id
                and attached_workspace["enabled"]
                and attached_session["work_dir"]
                == attached_workspace["workspace_path"]
            )
            if (
                context is None
                or context[1]["repair_task_id"] != repair_task_id
                or context[1]["writer_kind"] != "repair_delegate"
                or owner is None
                or owner["owner_id"] != repair_task_id
                or owner["owner_role"] != "repair"
                or int(owner["fence_token"]) != fence.token
                or not (reserved or attached)
            ):
                return await self._dispatch_unknown(
                    operation_id, stage, "handoff_unconfirmed",
                    f"after the handoff to {repair_task_id} (fence {fence.token}) the stage, "
                    "owner row, delegate status or attached session no longer form a "
                    "coherent reserved or attached writer",
                    conn=conn, repair_task_id=repair_task_id, writer_kind="repair_delegate",
                )
            ready = []
            if progress is not None and reserved:
                current_stage = context[1]
                if (
                    current_stage["current_subject"] != progress["subject"]
                    or (current_stage["dossier"] or {}).get("manifest") != progress["manifest"]
                ):
                    return await self._dispatch_unknown(
                        operation_id, stage, "progress_subject_moved",
                        "the stage subject or manifest changed after preserved progress "
                        f"{progress['ref']} was proven; it is re-proven on the next dispatch",
                        conn=conn,
                    )
                dossier = dict(current_stage["dossier"] or {})
                dossier.pop("preserved_progress_blocker", None)
                dossier.update(
                    preserved_progress=progress,
                    branch_sha=progress["sha"],
                    starting_sha=progress["sha"],
                    repair_commits=progress["repair_commits"],
                )
                await conn.execute(update(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == stage,
                ).values(starting_sha=progress["sha"], dossier=dossier))
                await conn.execute(update(tasks).where(tasks.c.id == repair_task_id).values(
                    description=await self._delegate_description_on(
                        conn, operation, dict(current_stage)
                        | {"starting_sha": progress["sha"], "dossier": dossier},
                    ),
                ))
            # The fence is handed over either way; an operator hold keeps the
            # delegate PAUSED until ``aq task resume`` restores it to READY.
            if (
                reserved
                and task["status"] == TaskStatus.PAUSED.value
                and await self.db._read_manual_pause(conn, repair_task_id) is None
            ):
                transition = await self.db._apply_transition(
                    conn,
                    repair_task_id,
                    TaskStatus.READY,
                    context="integration_repair_dispatch",
                    _manual_pause_control=True,
                )
                ready = transition.ready
        await self.db._notify_ready(ready)
        return self._dispatch_value(
            "already_dispatched" if replay else "dispatched",
            operation_id,
            stage,
            repair_task_id=repair_task_id,
            writer_kind="repair_delegate",
            fence=fence,
        )

    async def complete_delegate(
        self,
        repair_task_id: str,
        *,
        operation_id: str,
        stage: int,
        session_id: str,
        instance_token: str,
        workspace_id: str,
        fence_token: int,
        head_sha: str,
        commit_proof: dict[str, Any] | None = None,
        now: float | None = None,
        accepted_close: dict | None = None,
    ) -> dict[str, Any]:
        """Atomically close one exact attached repair writer and enqueue its fact.

        ``accepted_close`` is the delegate's ``task close`` identity, recorded
        with the COMPLETED transition (``_apply_transition``).
        """
        completed_at = self.clock() if now is None else now
        transition = None
        async with self.db.immediate() as conn:
            project_id = (await conn.execute(
                select(tasks.c.project_id).where(tasks.c.id == repair_task_id)
            )).scalar_one_or_none()
            if project_id is None:
                return {"outcome": "stale"}
            # CI takes project -> batch/revision -> operation. Serialize here
            # before get_repair_filing_scope locks operation/stage and owner.
            await self.db.lock_hierarchy_project(conn, project_id)
            scope = await self.db.get_repair_filing_scope(
                repair_task_id, session_id=session_id, conn=conn
            )
            expected = {
                "operation_id": operation_id,
                "stage": stage,
                "writer_kind": "repair_delegate",
                "session_id": session_id,
                "instance_token": instance_token,
                "workspace_id": workspace_id,
                "fence_token": fence_token,
            }
            actual = (
                {
                    key: scope[key]
                    for key in (
                        "operation_id",
                        "stage",
                        "writer_kind",
                        "session_id",
                        "instance_token",
                        "workspace_id",
                        "fence_token",
                    )
                }
                if scope is not None and scope["active"]
                else None
            )
            if actual != expected:
                return {"outcome": "stale"}
            if scope["target_kind"] == "parent":
                # A pushed Git commit alone does not complete the delivery
                # protocol. Keep the live writer attached until its exact
                # resolution has a durable, fenced push observation.
                pending = (await conn.execute(
                    select(integration_promotion_intents).where(
                        integration_promotion_intents.c.operation_key == operation_id,
                        integration_promotion_intents.c.state.in_(
                            ["conflict", "resolution_reserved"]
                        ),
                    ).with_for_update()
                )).mappings().all()
                for intent in pending:
                    evidence = intent["resolution_push_evidence"] or {}
                    if (
                        intent["state"] != "resolution_reserved"
                        or intent["resolution_head_sha"] != head_sha
                        or evidence.get("kind") != "exact_resolution_push_observed"
                        or evidence.get("remote_sha") != head_sha
                    ):
                        return {
                            "outcome": "resolution_required",
                            "intent_id": intent["id"],
                            "feedback": (
                                f"Conflict intent {intent['id']} has no recorded push "
                                "of this exact resolution. While retaining this claim, "
                                "run aq system integration-resolve-conflict and then "
                                "aq system integration-push-conflict-resolution with "
                                "the current repair fence (see --help), then close again. "
                                "A direct Git push alone does not record delivery."
                            ),
                        }
                await self.bind_current_parent_subject_on(
                    conn,
                    operation_id,
                    head_sha=head_sha,
                    commit_proof=commit_proof,
                    now=completed_at,
                )
            else:
                await self.adopt_batch_repair_on(
                    conn, operation_id, head_sha=head_sha,
                    commit_proof=commit_proof, now=completed_at,
                )
            transition = await self.db._apply_transition(
                conn,
                repair_task_id,
                TaskStatus.COMPLETED,
                context="integration_repair_delegate_closed",
                assigned_agent_id=None,
                accepted_close=accepted_close,
            )
            project_id = str(scope["project_id"])
            event_id = (f"repair-delegate-closed-{operation_id}-{stage}-{repair_task_id}"
                        f"-{fence_token}-{session_id}")
            event_payload = {
                "operation_id": operation_id,
                "stage": stage,
                "task_id": repair_task_id,
                "session_id": session_id,
                "instance_token": instance_token,
                "workspace_id": workspace_id,
                "fence_token": fence_token,
            }
            if scope["target_kind"] == "batch":
                current = (await conn.execute(
                    select(
                        integration_repair_operations.c.batch_id,
                        integration_repair_stages.c.current_subject,
                    ).select_from(
                        integration_repair_operations.join(
                            integration_repair_stages,
                            integration_repair_stages.c.operation_id
                            == integration_repair_operations.c.id,
                        )
                    ).where(
                        integration_repair_operations.c.id == operation_id,
                        integration_repair_stages.c.ordinal == stage,
                    )
                )).one()
                subject = current.current_subject
                event_payload.update(
                    batch_id=current.batch_id,
                    revision=subject["revision"],
                    head_sha=subject["candidate_sha"],
                )
            await enqueue_integration_event(
                conn,
                event_id=event_id,
                dedup_key=(f"repair-delegate-closed:{operation_id}:{stage}:{repair_task_id}"
                           f":{fence_token}:{session_id}"),
                project_id=project_id,
                event_type="integration.repair_delegate_closed",
                payload=event_payload,
                available_at=completed_at,
            )
        await self.db.log_blocked_flips(transition.flipped)
        await self.db._notify_settled(transition.settled)
        await self.db._notify_ready(transition.ready)
        return {"outcome": "completed", "event_id": event_id}

    @parent_engine_guard("operation", outcome="stale")
    @root_engine_guard("operation", outcome="stale")
    async def expire(
        self, operation_id: str, stage: int, *, now: float | None = None
    ) -> dict[str, Any]:
        """Conditionally expire the exact current stage at its absolute deadline.

        Under a continuing ladder the clock alone never allocates a writer that
        nobody needed: a delegate the pool never claimed extends the stage, a
        live writer is revisited, and a stopped writer that published nothing
        is proven gone and refiled at the same ordinal.  Conclusive attempts or
        a moved subject head escalate as before.
        """
        observed_at = self.clock() if now is None else now
        try:
            return await self._expire(operation_id, stage, observed_at, classify=True)
        except _StoppedWriter as stopped:
            return await self._expire_stopped_writer(
                operation_id, stage, stopped.facts, observed_at
            )

    async def _expire(
        self, operation_id: str, stage: int, observed_at: float, *, classify: bool
    ) -> dict[str, Any]:
        if stage < 0:
            return self._timeout_value("stale", "ignore", operation_id, stage)
        async with self.db.immediate() as conn:
            # Candidate/CI writers lock hierarchy before operation and stage.
            # The acceptance proof below reads those same candidate rows.
            project_id = (await conn.execute(
                select(integration_batches.c.project_id)
                .join(integration_repair_operations,
                      integration_repair_operations.c.batch_id == integration_batches.c.id)
                .where(integration_repair_operations.c.id == operation_id)
            )).scalar_one_or_none()
            if project_id is not None:
                await self.db.lock_hierarchy_project(conn, project_id)
            operation = (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(integration_repair_operations.c.id == operation_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            row = (
                await conn.execute(
                    select(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.ordinal == stage,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if operation is None or row is None:
                return self._timeout_value("stale", "ignore", operation_id, stage)
            if row["state"] in {"failed", "expired"}:
                if (row["dossier"] or {}).get("supervisor_recovery"):
                    return self._timeout_value(
                        "already_terminal", "supervisor_recovery", operation_id, stage
                    )
                continues = stage == 0 or RepairPolicy.model_validate(row["policy"]).on_exhausted == "continue"
                return self._timeout_value(
                    "already_terminal",
                    "dispatch_debug" if continues else "block_for_human",
                    operation_id,
                    stage + 1 if continues else stage,
                )
            if row["state"] in {"passed", "cancelled"}:
                return self._timeout_value(
                    "already_terminal", "none", operation_id, stage
                )
            if (
                int(operation["active_stage"]) != stage
                or operation["state"] not in {"active", "escalated"}
                or row["state"] not in {"active", "awaiting_completion"}
            ):
                return self._timeout_value("stale", "ignore", operation_id, stage)
            if row["deadline_at"] is None or observed_at < float(row["deadline_at"]):
                return self._timeout_value("not_due", "wait", operation_id, stage)
            live_mutation = (
                await conn.execute(
                    select(integration_candidate_ref_mutations.c.id).where(
                        integration_candidate_ref_mutations.c.operation_id == operation_id,
                        integration_candidate_ref_mutations.c.operation_stage == stage,
                        integration_candidate_ref_mutations.c.state == "reserved",
                    )
                )
            ).scalar_one_or_none()
            if live_mutation is not None:
                return self._timeout_value("not_due", "wait", operation_id, stage)
            live_attestation = (
                await conn.execute(
                    select(integration_attestation_publications.c.id).where(
                        integration_attestation_publications.c.operation_id == operation_id,
                        integration_attestation_publications.c.state == "reserved",
                        (
                            integration_attestation_publications.c.prewrite_at.is_not(None)
                            | (integration_attestation_publications.c.expires_at > observed_at)
                        ),
                    )
                )
            ).scalar_one_or_none()
            if live_attestation is not None:
                return self._timeout_value("not_due", "wait", operation_id, stage)
            if (
                operation["target_kind"] == "batch"
                and row["state"] == "awaiting_completion"
                and await self._root_success_is_current_on(conn, operation, row)
            ):
                return self._timeout_value(
                    "not_due", "awaiting_promotion", operation_id, stage
                )
            if operation["target_kind"] == "batch":
                from src.integration.accepted_repair import accepted_candidate_on

                if await accepted_candidate_on(conn, operation, row) is not None:
                    return self._timeout_value("not_due", "wait", operation_id, stage)
                if await self._redrive_unfinished_construction_on(
                    conn, dict(operation), dict(row), now=observed_at
                ):
                    return self._timeout_value("not_due", "wait", operation_id, stage)
            continues = (
                stage == 0
                or RepairPolicy.model_validate(row["policy"]).on_exhausted == "continue"
            )
            if continues and classify:
                writer = await self._expiring_writer_on(conn, dict(operation), dict(row))
                disposition = writer["disposition"] if writer is not None else "ladder"
                if disposition == "stopped":
                    # Nothing was written in this transaction; the stop proof
                    # runs Git and provider probes without the row locks.
                    raise _StoppedWriter(writer)
                if disposition != "ladder":
                    return await self._defer_expiry_on(
                        conn, dict(operation), dict(row), writer,
                        reason=writer["reason"], now=observed_at,
                    )
            if continues:
                continued = await self._activate_debug_on(
                    conn,
                    operation=dict(operation),
                    primary=dict(row),
                    attempts=int(row["attempts"]),
                    now=observed_at,
                    terminal_state="expired",
                )
                return self._timeout_value(
                    "expired", "dispatch_debug" if continued else "supervisor_recovery",
                    operation_id, stage + 1 if continued else stage
                )
            transition = await self._human_block_on(
                conn,
                operation=dict(operation),
                stage=dict(row),
                attempts=int(row["attempts"]),
                now=observed_at,
                terminal_state="expired",
            )
            result = self._timeout_value(
                "expired", "block_for_human", operation_id, 1
            )
        if transition is not None:
            await self.db.log_blocked_flips(transition.flipped)
            await self.db._notify_settled(transition.settled)
            await self.db._notify_ready(transition.ready)
        return result

    async def _redrive_unfinished_construction_on(
        self, conn, operation: dict[str, Any], stage: dict[str, Any], *, now: float
    ) -> bool:
        """Re-enter construction once before escalating a stage with nothing to repair.

        Stage 0 starts before the candidate is built.  If its deadline finds the
        batch still ``building`` with a ``constructing`` revision and no writer ever
        assigned, construction stopped without a continuation (a failed
        construct-and-test run).  A debug writer cannot change the frozen
        manifest, so the operation's route first rebuilds; the ladder escalates
        as before only once that re-drive was delivered and the grace passed.
        Attempts, deadlines and budgets are untouched.
        """
        if (
            int(stage["ordinal"]) != 0
            or stage["repair_task_id"] is not None
            or stage["writer_kind"] is not None
            or operation["batch_id"] is None
        ):
            return False
        batch = (await conn.execute(
            select(integration_batches)
            .where(integration_batches.c.id == operation["batch_id"])
            .with_for_update()
        )).mappings().one_or_none()
        if batch is None or batch["lifecycle"] != "building":
            return False
        revision = (await conn.execute(
            select(integration_candidate_revisions.c.state).where(
                integration_candidate_revisions.c.batch_id == batch["id"],
                integration_candidate_revisions.c.revision == batch["current_revision"],
            )
        )).scalar_one_or_none()
        if revision != "constructing":
            return False
        event_id = (
            f"integration-construction-redrive:{operation['id']}:0:"
            f"{int(batch['current_revision'])}"
        )
        redrive = (await conn.execute(
            select(integration_outbox.c.available_at, integration_outbox.c.delivered_at)
            .where(integration_outbox.c.id == event_id)
        )).mappings().one_or_none()
        if redrive is None:
            await enqueue_integration_event(
                conn, event_id=event_id, dedup_key=event_id,
                project_id=batch["project_id"], event_type="integration.sealed",
                payload={"batch_id": batch["id"], "operation_id": operation["id"]},
                available_at=now,
            )
            return True
        # An undelivered re-drive (no route accepts it) must not hold the ladder
        # forever either: the grace runs from delivery, or else from enqueue.
        started = redrive["delivered_at"]
        if started is None:
            started = redrive["available_at"]
        return now - float(started) < CONSTRUCTION_REDRIVE_GRACE_SECONDS

    async def _expiring_writer_on(
        self, conn, operation: dict[str, Any], stage: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Classify a due stage's delegate from durable facts, never from the clock.

        ``ladder`` keeps the existing escalation: a conclusive attempt, a subject
        head that moved since this writer was allocated, a writer that closed,
        or a spent refile budget.  ``unclaimed`` is a capacity wait, ``live``
        and ``held`` are waits, and ``stopped`` needs a stop proof before its
        ordinal is refiled.  ``None`` leaves a stage without a delegate (or
        one this cannot read) to the ladder.
        """
        from src.database.queries.task_queries import TERMINAL_BLOCKED_META_KEY
        from src.integration.finished_owners import _live_task_session
        from src.integration.owner_recovery import RECOVERABLE_STATES

        task_id = stage["repair_task_id"]
        if (
            stage["state"] != "active"
            or stage["writer_kind"] != "repair_delegate"
            or not task_id
            or stage["deadline_at"] is None
        ):
            return None
        task = (await conn.execute(
            select(tasks).where(tasks.c.id == task_id).with_for_update()
        )).mappings().one_or_none()
        context = await self._dispatch_context_on(conn, operation["id"], int(stage["ordinal"]))
        if task is None or context is None:
            return None
        target = context[2]
        owner = (await conn.execute(
            select(integration_branch_owners).where(
                integration_branch_owners.c.repository_id == target.repository_id,
                integration_branch_owners.c.ref == target.branch,
            ).with_for_update()
        )).mappings().one_or_none()
        rows = await self._stage_rows_on(conn, operation["id"])
        subject_sha = self._subject_sha(stage["current_subject"])
        facts = {
            "operation_id": operation["id"],
            "batch_id": operation.get("batch_id"),
            "stage": int(stage["ordinal"]),
            "task_id": task_id,
            "task_status": task["status"],
            "claim_epoch": int(task["claim_epoch"] or 0),
            "attempts": int(stage["attempts"]),
            "deadline_at": float(stage["deadline_at"]),
            "subject_sha": subject_sha,
            "allocation_sha": self._writer_allocation_sha(stage, rows),
            "target": target,
            "owner": dict(owner) if owner is not None else None,
        }

        def verdict(disposition: str, reason: str) -> dict[str, Any]:
            return facts | {"disposition": disposition, "reason": reason}

        if (
            facts["attempts"] > 0
            or not is_valid_git_oid(subject_sha)
            or subject_sha != facts["allocation_sha"]
        ):
            return verdict("ladder", "progress")
        if await self.db._read_manual_pause(conn, task_id) is not None:
            return verdict("held", "operator_hold")
        if await _live_task_session(conn, task_id) is not None:
            return verdict("live", "writer_live")
        status = task["status"]
        attached = bool(
            owner is not None
            and owner["owner_id"] == task_id
            and owner["handoff_state"] in RECOVERABLE_STATES
        )
        if status in _UNCLAIMED_STATUSES and not attached:
            return verdict("unclaimed", "writer_unclaimed")
        terminal = await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == task_id,
            task_metadata.c.key == TERMINAL_BLOCKED_META_KEY,
        ))
        if status not in _STOPPED_STATUSES or (
            status == TaskStatus.BLOCKED.value and terminal is not None
        ):
            return verdict("ladder", "writer_closed")
        if (
            len((stage["dossier"] or {}).get("writer_refiles") or []) >= MAX_REFILES_PER_STAGE
            or self._allocations_on_head(rows, subject_sha) >= MAX_WRITER_ALLOCATIONS_PER_SUBJECT
        ):
            return verdict("ladder", "refile_budget_exhausted")
        return verdict("stopped", "writer_stopped")

    @staticmethod
    async def _stage_rows_on(conn, operation_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in (await conn.execute(
            select(integration_repair_stages)
            .where(integration_repair_stages.c.operation_id == operation_id)
            .order_by(integration_repair_stages.c.ordinal)
        )).mappings()]

    @classmethod
    def _stage_allocation_sha(cls, stage: dict[str, Any], rows: list[dict[str, Any]]) -> str:
        """The subject head this stage's first writer was allocated on."""
        dossier = stage["dossier"] or {}
        allocated = (dossier.get("allocation") or {}).get("subject_sha")
        if allocated:
            return str(allocated)
        ordinal = int(stage["ordinal"])
        predecessor = next((row for row in rows if int(row["ordinal"]) == ordinal - 1), None)
        if ordinal == 0 or predecessor is None or dossier.get("continuations"):
            # Stage zero and a continued conflict move ``starting_sha`` together
            # with the subject; a successor inherits its predecessor's subject.
            return str(stage["starting_sha"] or "")
        return cls._subject_sha(predecessor["current_subject"])

    @classmethod
    def _writer_allocation_sha(cls, stage: dict[str, Any], rows: list[dict[str, Any]]) -> str:
        """The subject head the stage's current writer was allocated (or refiled) on."""
        refiles = (stage["dossier"] or {}).get("writer_refiles") or []
        if refiles and refiles[-1].get("subject_sha"):
            return str(refiles[-1]["subject_sha"])
        return cls._stage_allocation_sha(stage, rows)

    @classmethod
    def _allocations_on_head(cls, rows: list[dict[str, Any]], head: str) -> int:
        """Stages and same-ordinal refiles whose writer started on *head*."""
        return sum(
            int(cls._stage_allocation_sha(row, rows) == head)
            + sum(
                1
                for refile in (row["dossier"] or {}).get("writer_refiles") or []
                if refile.get("subject_sha") == head
            )
            for row in rows
        )

    async def _locked_due_stage_on(
        self, conn, operation_id: str, stage: int, facts: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Re-lock the exact stage *facts* describe, or ``None`` if it moved on."""
        project_id = (await conn.execute(
            select(integration_batches.c.project_id)
            .join(integration_repair_operations,
                  integration_repair_operations.c.batch_id == integration_batches.c.id)
            .where(integration_repair_operations.c.id == operation_id)
        )).scalar_one_or_none()
        if project_id is not None:
            await self.db.lock_hierarchy_project(conn, project_id)
        operation = (await conn.execute(
            select(integration_repair_operations)
            .where(integration_repair_operations.c.id == operation_id)
            .with_for_update()
        )).mappings().one_or_none()
        row = (await conn.execute(
            select(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == operation_id,
                integration_repair_stages.c.ordinal == stage,
            )
            .with_for_update()
        )).mappings().one_or_none()
        if (
            operation is None
            or row is None
            or operation["state"] not in {"active", "escalated"}
            or int(operation["active_stage"]) != stage
            or row["state"] != "active"
            or row["repair_task_id"] != facts["task_id"]
            or row["writer_kind"] != "repair_delegate"
            or int(row["attempts"]) != facts["attempts"]
            or row["deadline_at"] is None
            or float(row["deadline_at"]) != facts["deadline_at"]
            or self._subject_sha(row["current_subject"]) != facts["subject_sha"]
        ):
            return None
        return dict(operation), dict(row)

    async def _defer_expiry_on(
        self,
        conn,
        operation: dict[str, Any],
        stage: dict[str, Any],
        facts: dict[str, Any],
        *,
        reason: str,
        now: float,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Move a due stage's deadline forward without consuming its ordinal.

        An unclaimed delegate gets one primary budget (capacity is not a failed
        repair); anything else is revisited after :data:`WRITER_RECHECK_SECONDS`.
        Attempts, ordinal, writer and fence are untouched.
        """
        ordinal = int(stage["ordinal"])
        policy = RepairPolicy.model_validate(stage["policy"])
        seconds = (
            policy.primary_seconds if reason == "writer_unclaimed" else WRITER_RECHECK_SECONDS
        )
        previous = float(stage["deadline_at"])
        deadline = max(previous, now) + seconds
        entry = {
            "reason": reason,
            "at": now,
            "previous_deadline_at": previous,
            "deadline_at": deadline,
            "task_id": facts["task_id"],
            "task_status": facts["task_status"],
            "claim_epoch": facts["claim_epoch"],
        }
        if detail:
            entry["detail"] = detail
        dossier = dict(stage["dossier"] or {})
        history = list(dossier.get("deadline_deferrals") or []) + [entry]
        dossier["deadline_deferrals"] = history[-_DEFERRAL_HISTORY:]
        dossier["deadline_deferral_count"] = int(dossier.get("deadline_deferral_count") or 0) + 1
        dossier["budget"] = dict(dossier.get("budget") or {}) | {"deadline_at": deadline}
        changed = await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == operation["id"],
                integration_repair_stages.c.ordinal == ordinal,
                integration_repair_stages.c.state == "active",
                integration_repair_stages.c.deadline_at == previous,
            )
            .values(deadline_at=deadline, dossier=dossier)
        )
        waiting = self._timeout_value("not_due", "wait", operation["id"], ordinal)
        if changed.rowcount != 1:
            return waiting
        if reason in _NOTICE_DEFERRALS:
            if reason == "writer_unclaimed":
                subject = f"Repair stage {ordinal} of {operation['id']} is waiting for a writer"
                body = (
                    f"Repair stage {ordinal} of operation {operation['id']} reached its "
                    f"deadline before any writer claimed delegate {facts['task_id']} "
                    f"(status {facts['task_status']}, claim epoch {facts['claim_epoch']}). "
                    "This is a capacity wait, not a failed repair: the deadline moved by "
                    f"one primary budget to {deadline} and no successor stage was "
                    "allocated. If it persists, check pool capacity for intelligence class "
                    f"{stage['intelligence_class']}."
                )
            else:
                subject = f"Repair stage {ordinal} of {operation['id']} cannot refile its writer"
                body = (
                    f"Repair stage {ordinal} of operation {operation['id']} is due and its "
                    f"delegate {facts['task_id']} (status {facts['task_status']}) has "
                    f"stopped without publishing, but it was not refiled: {reason}"
                    + (f": {detail}" if detail else "")
                    + f". The stage is revisited every {WRITER_RECHECK_SECONDS:.0f}s; no "
                    "successor stage was allocated and the fence was not moved."
                )
            await self._stage_notice_on(
                conn, operation,
                key=f"repair-deferred:{operation['id']}:{ordinal}:{reason}",
                subject=subject, body=body, body_kind="integration_repair_deferred", now=now,
            )
        return waiting | {"reason": reason, "deadline_at": deadline}

    async def _defer_expiry(
        self,
        operation_id: str,
        stage: int,
        facts: dict[str, Any],
        *,
        reason: str,
        now: float,
        detail: str | None = None,
    ) -> dict[str, Any]:
        async with self.db.immediate() as conn:
            current = await self._locked_due_stage_on(conn, operation_id, stage, facts)
            if current is None:
                return self._timeout_value("not_due", "wait", operation_id, stage) | {
                    "reason": "stage_changed"
                }
            operation, row = current
            return await self._defer_expiry_on(
                conn, operation, row, facts, reason=reason, now=now, detail=detail
            )

    async def _expire_stopped_writer(
        self, operation_id: str, stage: int, facts: dict[str, Any], now: float
    ) -> dict[str, Any]:
        """Prove a stopped, unpublished writer gone, then refile its ordinal.

        The stop proof is the existing provider-backed owner recovery.  A dry
        run first: a checkout with unpublished commits keeps the rollover path,
        whose successor resumes the preserved tip.  A writer with nothing to
        preserve is released and its own ordinal is re-armed for a new claim.
        """
        from src.integration.owner_recovery import (
            NOT_ELIGIBLE,
            PRESERVED_AND_RELEASED,
            RECOVERABLE_STATES,
        )

        owner = facts["owner"]
        if (
            owner is not None
            and owner["owner_id"] == facts["task_id"]
            and owner["handoff_state"] in RECOVERABLE_STATES
        ):
            if self._owner_recovery is None:
                return await self._defer_expiry(
                    operation_id, stage, facts, reason="stop_proof_unavailable", now=now,
                    detail="no provider-backed owner recovery is configured",
                )
            proofs = []
            for dry_run in (True, False):
                recovered = await self._owner_recovery.recover(
                    owner["id"], principal="repair_stage_expiry", dry_run=dry_run
                )
                if recovered.outcome == NOT_ELIGIBLE:
                    return await self._defer_expiry(
                        operation_id, stage, facts, reason=recovered.reason or "stop_unproven",
                        now=now, detail=(recovered.evidence or {}).get("detail"),
                    )
                if recovered.outcome == PRESERVED_AND_RELEASED:
                    return await self._expire(operation_id, stage, now, classify=False)
                proofs.append(recovered)
            evidence = proofs[-1].evidence or {}
            proof = {
                "owner_row_id": owner["id"],
                "outcome": proofs[-1].outcome,
                "released_fence_token": evidence.get("released_fence_token"),
                "stop_proof": evidence.get("stop_proof"),
                "claim_released": evidence.get("claim_released"),
            }
        else:
            refusal = self._refile_owner_refusal(owner, facts)
            if refusal is not None:
                return await self._defer_expiry(
                    operation_id, stage, facts, reason="stale_fence", now=now, detail=refusal
                )
            proof = {
                "owner_row_id": owner["id"],
                "outcome": "detached",
                "owner_id": owner["owner_id"],
                "handoff_state": owner["handoff_state"],
                "fence_token": int(owner["fence_token"]),
            }
        return await self._refile_stage(operation_id, stage, facts, proof, now)

    @staticmethod
    def _refile_owner_refusal(owner: dict[str, Any] | None, facts: dict[str, Any]) -> str | None:
        """Why the branch fence does not belong to this stage's stopped writer."""
        if owner is None:
            return "the integration branch has no owner row"
        if owner["handoff_state"] == "released":
            return None
        if (
            owner["handoff_state"] == "reserved"
            and owner["session_id"] is None
            and owner["workspace_id"] is None
            and (
                (owner["owner_id"] == facts["task_id"] and owner["owner_role"] == "repair")
                or (
                    owner["owner_role"] == "collector"
                    and owner["owner_id"] in {facts["operation_id"], facts["batch_id"]}
                )
            )
        ):
            return None
        return (
            f"the fence belongs to {owner['owner_id']} ({owner['owner_role']}, "
            f"{owner['handoff_state']}, token {owner['fence_token']}), not to stopped "
            f"writer {facts['task_id']}"
        )

    async def _refile_stage(
        self,
        operation_id: str,
        stage: int,
        facts: dict[str, Any],
        proof: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        """Re-arm the same ordinal and delegate for a new claim, with a fresh clock.

        The delegate keeps its identity (and so every stage, archive and
        release relation); only its claim, hold metadata and deadline renew.
        Attempts are durable, as with a human resume.
        """
        from src.integration.finished_owners import _live_task_session

        task_id = facts["task_id"]
        transition = None
        async with self.db.immediate() as conn:
            current = await self._locked_due_stage_on(conn, operation_id, stage, facts)
            if current is None:
                return self._timeout_value("not_due", "wait", operation_id, stage) | {
                    "reason": "stage_changed"
                }
            operation, row = current
            task = (await conn.execute(
                select(tasks).where(tasks.c.id == task_id).with_for_update()
            )).mappings().one_or_none()
            if task is None:
                return self._timeout_value("not_due", "wait", operation_id, stage) | {
                    "reason": "stage_changed"
                }
            if await self.db._read_manual_pause(conn, task_id) is not None:
                return await self._defer_expiry_on(
                    conn, operation, row, facts, reason="operator_hold", now=now
                )
            if await _live_task_session(conn, task_id) is not None:
                return await self._defer_expiry_on(
                    conn, operation, row, facts, reason="writer_live", now=now
                )
            claimed = (await conn.execute(
                select(sessions.c.id).where(
                    sessions.c.task_id == task_id, sessions.c.claim_phase.is_not(None)
                ).limit(1)
            )).scalar_one_or_none()
            if claimed is not None:
                return await self._defer_expiry_on(
                    conn, operation, row, facts, reason="stale_claim", now=now,
                    detail=f"stopped session {claimed} still holds a claim on {task_id}",
                )
            policy = RepairPolicy.model_validate(row["policy"])
            deadline = now + (policy.primary_seconds if stage == 0 else policy.debug_seconds)
            dossier = dict(row["dossier"] or {})
            dossier["writer_refiles"] = list(dossier.get("writer_refiles") or []) + [{
                "task_id": task_id,
                "claim_epoch": int(task["claim_epoch"] or 0),
                "task_status": task["status"],
                "subject_sha": facts["subject_sha"],
                "previous_deadline_at": float(row["deadline_at"]),
                "deadline_at": deadline,
                "refiled_at": now,
                "stop_proof": proof,
            }]
            dossier["budget"] = dict(dossier.get("budget") or {}) | {"deadline_at": deadline}
            changed = await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == stage,
                    integration_repair_stages.c.state == "active",
                    integration_repair_stages.c.repair_task_id == task_id,
                )
                .values(deadline_at=deadline, dossier=dossier)
            )
            if changed.rowcount != 1:
                raise _RepairInvariant("repair stage changed while refiling its writer")
            await conn.execute(delete(task_metadata).where(
                task_metadata.c.task_id == task_id,
                task_metadata.c.key.in_(_REFILE_CLEARED_HOLDS),
            ))
            if task["status"] != TaskStatus.PAUSED.value:
                transition = await self.db._apply_transition(
                    conn,
                    task_id,
                    TaskStatus.PAUSED,
                    context="integration_repair_refile",
                    force=True,
                    _manual_pause_control=True,
                    assigned_agent_id=None,
                )
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(
                resume_after=None,
                description=await self._delegate_description_on(
                    conn, operation, row | {"dossier": dossier}
                ),
            ))
        if transition is not None:
            await self.db.log_blocked_flips(transition.flipped)
            await self.db._notify_settled(transition.settled)
            await self.db._notify_ready(transition.ready)
        try:
            dispatched = (await self.dispatch(operation_id, stage))["outcome"]
        except Exception:
            # The stage is durable; the continuation passes retry its dispatch.
            logger.warning(
                "Refiled repair stage %s/%s could not dispatch now; the continuation "
                "passes retry it", operation_id, stage, exc_info=True,
            )
            dispatched = "runtime_error"
        return self._timeout_value("not_due", "wait", operation_id, stage) | {
            "reason": "writer_refiled", "deadline_at": deadline, "dispatch": dispatched,
        }

    async def _stage_notice_on(
        self, conn, operation: dict[str, Any], *, key: str, subject: str, body: str,
        body_kind: str, now: float,
    ) -> None:
        """One supervisor message per *key*, however often the condition recurs."""
        project_id = await self._operation_project_id_on(conn, operation)
        await conn.execute(pg_insert(messages).values(
            id=f"msg-{key}", project_id=project_id,
            from_kind="system", from_id="integration-repair", to_kind="session",
            to_id=f"supervisor-{project_id}", subject=subject, body=body,
            created_at=now, priority=50, archive_after_inject=1, body_kind=body_kind,
        ).on_conflict_do_nothing(index_elements=[messages.c.id]))

    async def due_stages(
        self,
        *,
        now: float | None = None,
        after: tuple[float, str, int] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one bounded page of current stages whose deadline is due."""
        observed_at = self.clock() if now is None else now
        return await self.db.due_integration_repair_stage_page(
            now=observed_at, after=after, limit=limit
        )

    async def resume_root_collection(
        self, operation_id: str, batch_id: str, revision: int, candidate_sha: str
    ) -> bool:
        """Return an unclaimed debug reservation to its root collector.

        This is intentionally only a continuation after exact green CI has
        been persisted.  A stage-one delegate that is merely reserved has no
        writer to hand off from, but leaving it as branch owner prevents the
        collector from publishing/promoting that proved candidate.  Attached
        (or even assigned) delegates are left alone: their writer authority is
        never taken as a side effect of CI observation.
        """
        transition = None
        async with self.db.immediate() as conn:
            # All competing CI/promotion paths acquire the project hierarchy
            # lock before their batch row.  Discover the project without
            # locking, then re-read the batch under that canonical order.
            project_id = (
                await conn.execute(
                    select(integration_batches.c.project_id).where(
                        integration_batches.c.id == batch_id
                    )
                )
            ).scalar_one_or_none()
            if project_id is None:
                return False
            await self.db.lock_hierarchy_project(conn, project_id)
            batch = (
                await conn.execute(
                    select(integration_batches)
                    .where(integration_batches.c.id == batch_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if batch is None or batch["project_id"] != project_id:
                return False
            operation = (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(
                        integration_repair_operations.c.id == operation_id,
                        integration_repair_operations.c.batch_id == batch_id,
                        integration_repair_operations.c.episode_id == batch_id,
                        integration_repair_operations.c.target_kind == "batch",
                        integration_repair_operations.c.state == "escalated",
                        integration_repair_operations.c.active_stage >= 1,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            candidate = (
                await conn.execute(
                    select(integration_candidate_revisions)
                    .where(
                        integration_candidate_revisions.c.batch_id == batch_id,
                        integration_candidate_revisions.c.revision == revision,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            stage = None
            if operation is not None:
                stage = (
                    await conn.execute(
                        select(integration_repair_stages)
                        .where(
                            integration_repair_stages.c.operation_id == operation_id,
                            integration_repair_stages.c.ordinal == operation["active_stage"],
                        )
                        .with_for_update()
                    )
                ).mappings().one_or_none()
            if (
                operation is None
                or candidate is None
                or stage is None
                or int(batch["current_revision"]) != revision
                or batch["lifecycle"] != "testing"
                or candidate["head_sha"] != candidate_sha
                or candidate["state"] != "green"
                or candidate["ci_evidence_id"] is None
                or batch["tested_candidate_sha"] != candidate_sha
                or batch["ci_evidence_id"] != candidate["ci_evidence_id"]
                or stage["state"] != "awaiting_completion"
                or stage["current_subject"] != self._batch_subject(candidate)
                or stage["success_subject"] != stage["current_subject"]
                or stage["success_evidence_id"] != candidate["ci_evidence_id"]
                or not stage["repair_task_id"]
                or stage["writer_kind"] != "repair_delegate"
            ):
                return False
            target = BranchKey(
                repository_id=batch["repository_id"], branch=batch["integration_branch"]
            )
            owner = await self._ownership._locked_row(conn, target)
            live_mutation = (
                await conn.execute(
                    select(integration_candidate_ref_mutations.c.id).where(
                        integration_candidate_ref_mutations.c.repository_id == target.repository_id,
                        integration_candidate_ref_mutations.c.branch == target.branch,
                        integration_candidate_ref_mutations.c.state == "reserved",
                    )
                )
            ).scalar_one_or_none()
            task = (
                await conn.execute(
                    select(tasks)
                    .where(tasks.c.id == stage["repair_task_id"])
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if (
                owner is None
                or live_mutation is not None
                or owner["owner_id"] != stage["repair_task_id"]
                or owner["owner_role"] != "repair"
                or owner["handoff_state"] != "reserved"
                or owner["session_id"] is not None
                or owner["workspace_id"] is not None
                or task is None
                or task["project_id"] != batch["project_id"]
                or task["status"] not in {TaskStatus.PAUSED.value, TaskStatus.READY.value}
                or task["assigned_agent_id"] is not None
            ):
                return False
            if task["status"] == TaskStatus.READY.value:
                transition = await self.db._apply_transition(
                    conn,
                    task["id"],
                    TaskStatus.PAUSED,
                    context="integration_candidate_ci_collector_resume",
                    _manual_pause_control=True,
                    assigned_agent_id=None,
                )
            await self._ownership._claim_released(
                conn,
                owner,
                target,
                operation_id,
                "collector",
            )
        if transition is not None:
            await self.db.log_blocked_flips(transition.flipped)
            await self.db._notify_settled(transition.settled)
            await self.db._notify_ready(transition.ready)
        return True

    async def return_green_delegate_branch(
        self, batch_id: str, *, emit_continuation: bool = True, now: float | None = None
    ) -> Fence | None:
        """Return a closed writer's branch to the collector of its exact green candidate.

        CI can turn the candidate green while its repair delegate is still
        attached; promotion then waits for the collector fence.  The guarded
        delegate close finishes that writer on the unchanged candidate and
        leaves its own detached reservation.  This is the only continuation:
        it moves that reservation to the batch collector, records the handoff
        in the stage dossier and, when asked, enqueues a fresh green
        continuation bound to the new fence.  It never touches an attached,
        assigned or unfinished writer, a changed head, or other evidence.
        """
        observed_at = self.clock() if now is None else now
        async with self.db.immediate() as conn:
            # Same canonical order as CI and promotion: project lock first.
            project_id = (
                await conn.execute(
                    select(integration_batches.c.project_id).where(
                        integration_batches.c.id == batch_id
                    )
                )
            ).scalar_one_or_none()
            if project_id is None:
                return None
            await self.db.lock_hierarchy_project(conn, project_id)
            batch = (
                await conn.execute(
                    select(integration_batches)
                    .where(integration_batches.c.id == batch_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            operation = (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(
                        integration_repair_operations.c.batch_id == batch_id,
                        integration_repair_operations.c.episode_id == batch_id,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if batch is None or operation is None or batch["project_id"] != project_id:
                return None
            return await self.return_green_delegate_branch_on(
                conn,
                batch=dict(batch),
                operation=dict(operation),
                emit_continuation=emit_continuation,
                now=observed_at,
            )

    async def return_green_delegate_branch_on(
        self,
        conn,
        *,
        batch: dict[str, Any],
        operation: dict[str, Any],
        emit_continuation: bool,
        now: float,
    ) -> Fence | None:
        """Hand off under the caller's project lock; ``None`` when not exactly eligible."""
        if (
            operation["target_kind"] != "batch"
            or operation["batch_id"] != batch["id"]
            or operation["episode_id"] != batch["id"]
            or operation["state"] not in {"active", "escalated"}
            or batch["lifecycle"] != "testing"
            or not batch["integration_branch"]
        ):
            return None
        revision = (
            await conn.execute(
                select(integration_candidate_revisions)
                .where(
                    integration_candidate_revisions.c.batch_id == batch["id"],
                    integration_candidate_revisions.c.revision == batch["current_revision"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        stage = (
            await conn.execute(
                select(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation["id"],
                    integration_repair_stages.c.ordinal == operation["active_stage"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if revision is None or stage is None:
            return None
        subject = self._batch_subject(revision)
        if (
            revision["state"] != "green"
            or not revision["ci_evidence_id"]
            or revision["ci_evidence_id"] != batch["ci_evidence_id"]
            or revision["head_sha"] != batch["tested_candidate_sha"]
            or stage["state"] != "awaiting_completion"
            or stage["writer_kind"] != "repair_delegate"
            or not stage["repair_task_id"]
            or stage["current_subject"] != subject
            or stage["success_subject"] != subject
            or stage["success_evidence_id"] != revision["ci_evidence_id"]
        ):
            return None
        evidence = (
            await conn.execute(
                select(integration_check_evidence).where(
                    integration_check_evidence.c.id == stage["success_evidence_id"]
                )
            )
        ).mappings().one_or_none()
        if (
            not self._evidence_matches(operation, stage, evidence)
            or evidence["conclusion"] != "success"
            or evidence["classification"] != "conclusive"
        ):
            return None
        task = (
            await conn.execute(
                select(tasks).where(tasks.c.id == stage["repair_task_id"]).with_for_update()
            )
        ).mappings().one_or_none()
        if (
            task is None
            or task["status"] != TaskStatus.COMPLETED.value
            or task["assigned_agent_id"] is not None
            or task["project_id"] != batch["project_id"]
            or task["repo_id"] != batch["repository_id"]
            or str(task["branch_name"] or "").removeprefix("refs/heads/")
            != batch["integration_branch"].removeprefix("refs/heads/")
            or task["created_by_kind"] != "integration_repair"
            or task["created_by_id"] != operation["id"]
        ):
            return None
        target = BranchKey(
            repository_id=batch["repository_id"], branch=batch["integration_branch"]
        )
        owner = await self._ownership._locked_row(conn, target)
        # Only the close pipeline's finished self-transfer: ``released`` is the
        # instant inside that transfer, and taking it would fail the close's
        # own handoff proof.  A crash-orphaned ``released`` row is claimed by
        # the collector's ordinary acquire instead.
        if (
            owner is None
            or owner["owner_id"] != task["id"]
            or owner["owner_role"] != "repair"
            or owner["handoff_state"] != "reserved"
            or owner["session_id"] is not None
            or owner["workspace_id"] is not None
        ):
            return None
        try:
            fence = await self._ownership.transfer_detached_on(
                conn,
                Fence(target=target, owner_id=owner["owner_id"], token=int(owner["fence_token"])),
                operation["id"],
                "collector",
            )
        except (BranchBusy, StaleFence):
            return None
        dossier = dict(stage["dossier"] or {})
        handoffs = list(dossier.get("green_handoffs", []))
        handoffs.append(
            {
                "task_id": task["id"],
                "from_fence": int(owner["fence_token"]),
                "to_owner_id": fence.owner_id,
                "to_fence": fence.token,
                "candidate_sha": revision["head_sha"],
                "revision": int(revision["revision"]),
                "evidence_id": revision["ci_evidence_id"],
                "at": now,
            }
        )
        dossier["green_handoffs"] = handoffs
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == operation["id"],
                integration_repair_stages.c.ordinal == stage["ordinal"],
            )
            .values(dossier=dossier)
        )
        if emit_continuation:
            lease = (
                await conn.execute(
                    select(project_integration_leases).where(
                        project_integration_leases.c.project_id == batch["project_id"]
                    )
                )
            ).mappings().one_or_none()
            collector = await self._ownership._locked_row(conn, target)
            await enqueue_green_continuation_on(
                conn,
                project_id=batch["project_id"],
                operation_id=operation["id"],
                batch_id=batch["id"],
                revision=int(revision["revision"]),
                head_sha=revision["head_sha"],
                fingerprint=promotion_fingerprint(
                    operation_id=operation["id"],
                    batch_id=batch["id"],
                    revision=int(revision["revision"]),
                    head_sha=revision["head_sha"],
                    evidence_id=revision["ci_evidence_id"],
                    owner=collector,
                    lease=dict(lease) if lease is not None else None,
                ),
                generation=0,
                now=now,
            )
        return fence

    async def adopt_batch_repair_on(
        self,
        conn,
        operation_id: str,
        *,
        head_sha: str,
        commit_proof: dict[str, Any] | None,
        now: float,
    ) -> None:
        """Bind a proved CI repair while the caller holds the exact writer fence.

        The old candidate and its CI/publication identity stay immutable. A new
        revision copies reviewed member results and requires its own evidence.
        This is internal to the guarded delegate close, not an operator override.
        """
        if not is_valid_git_oid(head_sha) or commit_proof is None:
            raise ValueError("batch repair requires exact verified commit lineage")
        operation = (
            (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(integration_repair_operations.c.id == operation_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if operation["target_kind"] != "batch" or operation["state"] not in {"active", "escalated"}:
            raise ValueError("batch repair operation is not active")
        batch, revision = await self._current_batch_subject_rows_on(conn, operation)
        stage = (
            (
                await conn.execute(
                    select(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.ordinal == operation["active_stage"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if stage["state"] not in {"active", "awaiting_completion"}:
            raise ValueError("batch repair stage is no longer active")
        if stage["state"] == "active" and now >= float(stage["deadline_at"]):
            raise ValueError("batch repair stage is no longer active")
        if stage["current_subject"] != self._batch_subject(revision):
            raise ValueError("batch repair subject changed during close")
        if stage["state"] == "awaiting_completion":
            # CI can finish while its exact repair writer is still attached.
            # Like expire's awaiting_promotion path, this unchanged green
            # handoff may outlive the deadline; it grants no further repair.
            evidence = (
                await conn.execute(
                    select(integration_check_evidence).where(
                        integration_check_evidence.c.id == stage["success_evidence_id"]
                    )
                )
            ).mappings().one_or_none()
            if (
                head_sha != revision["head_sha"]
                or revision["state"] != "green"
                or batch["lifecycle"] != "testing"
                or batch["tested_candidate_sha"] != head_sha
                or stage["success_subject"] != stage["current_subject"]
                or stage["success_evidence_id"] != revision["ci_evidence_id"]
                or stage["success_evidence_id"] != batch["ci_evidence_id"]
                or not self._evidence_matches(operation, stage, evidence)
                or evidence["conclusion"] != "success"
                or evidence["classification"] != "conclusive"
            ):
                raise ValueError("batch repair completion requires the exact green candidate")
        stage_dossier = dict(stage["dossier"] or {})
        rebuild_conflict = stage_dossier.get("candidate_rebuild_conflict")
        construction_base_sha = revision["construction_base_sha"]
        if rebuild_conflict is not None:
            expected_parents = {
                rebuild_conflict.get("candidate_sha"),
                rebuild_conflict.get("new_base_sha"),
            }
            actual_parents = list(commit_proof.get("head_parents") or [])
            if (
                rebuild_conflict.get("kind") != "candidate_rebuild"
                or rebuild_conflict.get("operation_id") != operation_id
                or int(rebuild_conflict.get("operation_stage", -1))
                != int(stage["ordinal"])
                or rebuild_conflict.get("batch_id") != batch["id"]
                or int(rebuild_conflict.get("revision", -1))
                != int(revision["revision"])
                or rebuild_conflict.get("candidate_sha") != revision["head_sha"]
                or not is_valid_git_oid(rebuild_conflict.get("new_base_sha", ""))
                or head_sha == revision["head_sha"]
                or len(actual_parents) != 2
                or set(actual_parents) != expected_parents
            ):
                raise ValueError(
                    "batch rebuild repair must be the exact ancestry-preserving merge"
                )
            construction_base_sha = rebuild_conflict["new_base_sha"]
            history = list(stage_dossier.get("candidate_rebuild_conflicts", []))
            history.append(
                {
                    **rebuild_conflict,
                    "resolved_head_sha": head_sha,
                    "resolved_at": now,
                }
            )
            stage_dossier["candidate_rebuild_conflicts"] = history
            stage_dossier.pop("candidate_rebuild_conflict", None)
        dossier = self._dossier_with_repair_commits(
            stage_dossier, revision["head_sha"], head_sha, commit_proof
        )
        if head_sha == revision["head_sha"]:
            return
        # Main can advance after candidate CI is already green. Its frozen
        # rebuild conflict still requires a new revision and fresh CI below.
        constructed_states = {"built", "testing", "red"}
        if rebuild_conflict is not None:
            constructed_states.add("green")
        if revision["state"] not in constructed_states:
            raise ValueError("batch CI repair requires a fully constructed candidate")
        members = (
            (
                await conn.execute(
                    select(integration_candidate_member_results).where(
                        integration_candidate_member_results.c.batch_id == batch["id"],
                        integration_candidate_member_results.c.revision == revision["revision"],
                    )
                )
            )
            .mappings()
            .all()
        )
        if any(member["result"] not in {"applied", "skipped"} for member in members):
            raise ValueError("batch CI repair cannot replace an unresolved member")
        next_revision = int(revision["revision"]) + 1
        await conn.execute(
            update(integration_candidate_revisions)
            .where(
                integration_candidate_revisions.c.batch_id == batch["id"],
                integration_candidate_revisions.c.revision == revision["revision"],
            )
            .values(state="superseded", updated_at=now)
        )
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id=batch["id"],
                revision=next_revision,
                construction_base_sha=construction_base_sha,
                next_member_ordinal=revision["next_member_ordinal"],
                repair_parent_revision=revision["revision"],
                head_sha=head_sha,
                state="built",
                created_at=now,
                updated_at=now,
            )
        )
        for member in members:
            await conn.execute(
                insert(integration_candidate_member_results).values(
                    **{
                        **dict(member),
                        "revision": next_revision,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            )
        await conn.execute(
            update(integration_batches)
            .where(
                integration_batches.c.id == batch["id"],
            )
            .values(
                current_revision=next_revision,
                tested_candidate_sha=None,
                ci_evidence_id=None,
                lifecycle="testing",
                updated_at=now,
            )
        )
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == operation_id,
                integration_repair_stages.c.ordinal == stage["ordinal"],
            )
            .values(dossier=dossier)
        )
        await self.bind_current_batch_subject_on(conn, operation_id, now=now)

    async def bind_current_batch_subject_on(
        self, conn, operation_id: str, *, now: float | None = None
    ) -> dict[str, Any]:
        """Bind Task9's authoritative current revision without resetting its budget."""
        observed_at = self.clock() if now is None else now
        operation = (
            await conn.execute(
                select(integration_repair_operations)
                .where(integration_repair_operations.c.id == operation_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            operation is None
            or operation["target_kind"] != "batch"
            or operation["state"] not in {"active", "escalated"}
        ):
            raise ValueError("batch repair operation is not active")
        batch, revision = await self._current_batch_subject_rows_on(conn, operation)
        stage = (
            await conn.execute(
                select(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == operation["active_stage"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if stage is None or stage["state"] not in {"active", "awaiting_completion"}:
            raise ValueError("batch repair stage is not current")
        subject = self._batch_subject(revision)
        if stage["current_subject"] != subject:
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == stage["ordinal"],
                )
                .values(
                    current_subject=subject,
                    state="active",
                    success_subject=None,
                    success_evidence_id=None,
                )
            )
        return {
            "operation_id": operation_id,
            "stage": int(stage["ordinal"]),
            "subject": subject,
            "deadline_due": observed_at >= float(stage["deadline_at"]),
        }

    async def record_batch_rebuild_conflict_on(
        self,
        conn,
        operation_id: str,
        *,
        revision_number: int,
        candidate_sha: str,
        new_base_sha: str,
        diagnostics: str,
        fence: Fence,
        now: float,
    ) -> dict[str, Any]:
        """Freeze a conflicting main advance under the current root repair budget.

        The candidate remains the stage subject and the integration branch remains
        byte-for-byte where reviewed CI left it.  A delegate resolves the exact
        ``candidate_sha``/``new_base_sha`` pair with a two-parent merge; its close
        then adopts a new revision whose construction base is that frozen main.
        """
        if not is_valid_git_oid(candidate_sha) or not is_valid_git_oid(new_base_sha):
            return {"outcome": "stale"}
        operation = (
            await conn.execute(
                select(integration_repair_operations)
                .where(integration_repair_operations.c.id == operation_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            operation is None
            or operation["target_kind"] != "batch"
            or operation["state"] not in {"active", "escalated"}
        ):
            return {"outcome": "stale"}
        batch, revision = await self._current_batch_subject_rows_on(conn, operation)
        stage = (
            await conn.execute(
                select(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == operation["active_stage"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        owner = (
            await conn.execute(
                select(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == batch["repository_id"],
                    integration_branch_owners.c.ref == batch["integration_branch"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        live_mutation = (
            await conn.execute(
                select(integration_candidate_ref_mutations.c.id)
                .where(
                    integration_candidate_ref_mutations.c.repository_id
                    == batch["repository_id"],
                    integration_candidate_ref_mutations.c.branch
                    == batch["integration_branch"],
                    integration_candidate_ref_mutations.c.state == "reserved",
                )
                .limit(1)
            )
        ).first()
        subject = self._batch_subject(revision)
        if (
            int(batch["current_revision"]) != revision_number
            or int(revision["revision"]) != revision_number
            or revision["head_sha"] != candidate_sha
            or revision["state"] not in {"built", "testing", "green", "red"}
            or stage is None
            or stage["state"] not in {"active", "awaiting_completion"}
            or stage["current_subject"] != subject
            or fence.target.repository_id != batch["repository_id"]
            or fence.target.branch != batch["integration_branch"]
            or fence.owner_id != operation_id
            or owner is None
            or owner["owner_id"] != fence.owner_id
            or owner["owner_role"] != "collector"
            or int(owner["fence_token"]) != fence.token
            or owner["handoff_state"] != "reserved"
            or owner["session_id"] is not None
            or owner["workspace_id"] is not None
            or live_mutation is not None
        ):
            return {"outcome": "busy"}

        conflict_id = hashlib.sha256(
            f"{operation_id}:{revision_number}:{candidate_sha}:{new_base_sha}".encode()
        ).hexdigest()
        conflict = {
            "kind": "candidate_rebuild",
            "id": conflict_id,
            "operation_id": operation_id,
            "operation_stage": int(stage["ordinal"]),
            "batch_id": batch["id"],
            "revision": revision_number,
            "candidate_sha": candidate_sha,
            "new_base_sha": new_base_sha,
            "integration_branch": batch["integration_branch"],
            "diagnostics": diagnostics,
            "resolution": {
                "kind": "two_parent_merge",
                "parents": [candidate_sha, new_base_sha],
            },
        }
        dossier = dict(stage["dossier"] or {})
        existing = dossier.get("candidate_rebuild_conflict")
        if existing is not None and existing.get("id") != conflict_id:
            return {
                "outcome": "conflict_already_frozen",
                "stage": int(stage["ordinal"]),
                "deadline_due": now >= float(stage["deadline_at"]),
            }
        if existing is not None:
            conflict = existing

        dossier["candidate_rebuild_conflict"] = conflict
        repair_task_id = stage["repair_task_id"]
        transition = None
        if repair_task_id is not None:
            task = (
                await conn.execute(
                    select(tasks).where(tasks.c.id == repair_task_id).with_for_update()
                )
            ).mappings().one_or_none()
            if (
                task is None
                or task["project_id"] != batch["project_id"]
                or task["repo_id"] != batch["repository_id"]
                or task["branch_name"] != batch["integration_branch"]
                or task["parent_task_id"] is not None
                or task["created_by_kind"] != "integration_repair"
                or task["created_by_id"] != operation_id
                or task["assigned_agent_id"] is not None
                or stage["writer_kind"] != "repair_delegate"
            ):
                return {"outcome": "busy"}
            sessions_for_delegate = (
                await conn.execute(
                    select(sessions).where(sessions.c.task_id == repair_task_id)
                )
            ).mappings().all()
            locked_workspace = (
                await conn.execute(
                    select(workspaces.c.id)
                    .where(workspaces.c.locked_by_task_id == repair_task_id)
                    .limit(1)
                )
            ).first()
            if (
                any(
                    row["state"] != "stopped" or row["claim_phase"] is not None
                    for row in sessions_for_delegate
                )
                or locked_workspace is not None
            ):
                return {"outcome": "busy"}
            if task["status"] == TaskStatus.COMPLETED.value:
                # This is the root counterpart to parent-conflict continuation:
                # collector ownership plus stopped sessions and no task-locked
                # workspace prove the completed delegate is detached. Reuse its
                # identity, but never its checkout or commits.
                transition = await self.db._apply_transition(
                    conn,
                    repair_task_id,
                    TaskStatus.PAUSED,
                    context="integration_root_rebuild_continuation",
                    force=True,
                    _manual_pause_control=True,
                    assigned_agent_id=None,
                    description=self._delegate_description(
                        operation, dict(stage) | {"dossier": dossier}
                    ),
                )
            elif task["status"] not in {
                TaskStatus.PAUSED.value,
                TaskStatus.READY.value,
            }:
                return {"outcome": "busy"}

        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == operation_id,
                integration_repair_stages.c.ordinal == stage["ordinal"],
            )
            .values(
                state="active",
                success_subject=None,
                success_evidence_id=None,
                dossier=dossier,
            )
        )
        await conn.execute(
            update(integration_batches)
            .where(
                integration_batches.c.id == batch["id"],
                integration_batches.c.current_revision == revision_number,
            )
            .values(lifecycle="repairing", updated_at=now)
        )
        if repair_task_id is not None:
            await conn.execute(
                update(tasks)
                .where(tasks.c.id == repair_task_id)
                .values(
                    description=self._delegate_description(
                        operation, dict(stage) | {"dossier": dossier}
                    )
                )
            )
        return {
            "outcome": "replayed" if existing is not None else "recorded",
            "stage": int(stage["ordinal"]),
            "deadline_due": now >= float(stage["deadline_at"]),
            "conflict_id": conflict_id,
            "transition": transition,
        }

    async def bind_current_parent_subject_on(
        self,
        conn,
        operation_id: str,
        *,
        head_sha: str,
        commit_proof: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Bind a transaction-proved parent HEAD without resetting stage budget."""
        if not is_valid_git_oid(head_sha):
            raise ValueError("parent repair subject HEAD is not an exact Git OID")
        observed_at = self.clock() if now is None else now
        operation = (
            await conn.execute(
                select(integration_repair_operations)
                .where(integration_repair_operations.c.id == operation_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            operation is None
            or operation["target_kind"] != "parent"
            or operation["state"] not in {"active", "escalated"}
        ):
            raise ValueError("parent repair operation is not active")
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints)
                .where(
                    task_integration_checkpoints.c.task_id
                    == operation["parent_task_id"]
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if checkpoint is None or checkpoint["episode_id"] != operation["episode_id"]:
            raise ValueError("parent repair checkpoint identity conflicts")
        stage = (
            await conn.execute(
                select(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == operation["active_stage"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if stage is None or stage["state"] not in {"active", "awaiting_completion"}:
            raise ValueError("parent repair stage is not current")
        subject = {
            "kind": "parent",
            "generation": int(checkpoint["generation"]),
            "head_sha": head_sha,
        }
        changed = stage["current_subject"] != subject
        if changed:
            previous_sha = self._subject_sha(stage["current_subject"])
            dossier = self._dossier_with_repair_commits(
                stage["dossier"], previous_sha, head_sha, commit_proof
            )
            if commit_proof is not None and previous_sha != head_sha:
                from src.integration.parent_repair_heads import (
                    EXTENSIONS, advance_checkpoint_on, extension,
                )

                owner = (await conn.execute(select(integration_branch_owners).where(
                    integration_branch_owners.c.repository_id == checkpoint["repository_id"],
                    integration_branch_owners.c.ref == checkpoint["branch"],
                ).with_for_update())).mappings().one_or_none()
                scope = None
                if owner is not None and owner["session_id"] is not None:
                    scope = await self.db.get_repair_filing_scope(
                        owner["owner_id"], session_id=owner["session_id"], conn=conn,
                    )
                pending_receipt = await conn.scalar(select(integration_promotion_intents.c.id).where(
                    integration_promotion_intents.c.operation_key == operation_id,
                    integration_promotion_intents.c.state.in_(["conflict", "resolution_reserved"]),
                ).limit(1))
                if scope is not None and scope["active"] and pending_receipt is None:
                    if scope["operation_id"] != operation_id or scope["stage"] != stage["ordinal"]:
                        raise ValueError("repair head authoring fence belongs to another stage")
                    edge = extension(operation, checkpoint, stage, commit_proof, {
                        "task_id": owner["owner_id"], "session_id": scope["session_id"],
                        "instance_token": scope["instance_token"],
                        "workspace_id": scope["workspace_id"], "fence_token": scope["fence_token"],
                    })
                    dossier[EXTENSIONS] = [*(dossier.get(EXTENSIONS) or []), edge]
                    await advance_checkpoint_on(conn, checkpoint, head_sha, observed_at)
            dossier["receipts"] = await self._current_receipts_on(conn, operation)
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == stage["ordinal"],
                )
                .values(
                    current_subject=subject,
                    state="active",
                    success_subject=None,
                    success_evidence_id=None,
                    dossier=dossier,
                )
            )
        return {
            "operation_id": operation_id,
            "stage": int(stage["ordinal"]),
            "subject": subject,
            "deadline_due": observed_at >= float(stage["deadline_at"]),
            "changed": changed,
        }

    async def _root_success_is_current_on(self, conn, operation, stage) -> bool:
        try:
            _batch, revision = await self._current_batch_subject_rows_on(conn, operation)
        except ValueError:
            return False
        return bool(
            stage["success_evidence_id"]
            and stage["success_subject"] == self._batch_subject(revision)
            and stage["current_subject"] == stage["success_subject"]
        )

    async def _effective_dispatch_stage_on(
        self, conn, operation_id: str, requested_stage: int
    ) -> int:
        """Map the frozen parent artifact's stage zero to a continued stage.

        Old pinned hierarchical-delivery artifacts always pass literal zero
        after ``start``. Only the durable continuation marker written by
        :meth:`_continue_parent_stage_on` permits that alias to select a later
        active stage; ordinary debug dispatch remains explicitly stage one.
        """
        operation = (
            await conn.execute(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.id == operation_id
                )
            )
        ).mappings().one_or_none()
        if (
            operation is None
            or requested_stage != 0
            or operation["target_kind"] != "parent"
            or int(operation["active_stage"]) == 0
        ):
            return requested_stage
        active_stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == operation["active_stage"],
                )
            )
        ).mappings().one_or_none()
        continuation = (
            (active_stage["dossier"] or {}).get("continuations", [])[-1]
            if active_stage is not None
            and (active_stage["dossier"] or {}).get("continuations")
            else None
        )
        if (
            active_stage is not None
            and continuation is not None
            and continuation.get("intent_id") == active_stage["trigger_id"]
            and continuation.get("starting_sha") == active_stage["starting_sha"]
        ):
            return int(active_stage["ordinal"])
        return requested_stage

    async def _dispatch_context_on(self, conn, operation_id: str, stage: int):
        operation = (
            await conn.execute(
                select(integration_repair_operations)
                .where(integration_repair_operations.c.id == operation_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        repair_stage = (
            await conn.execute(
                select(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation_id,
                    integration_repair_stages.c.ordinal == stage,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            operation is None
            or repair_stage is None
            or int(operation["active_stage"]) != stage
            or operation["state"] not in {"active", "escalated"}
            or repair_stage["state"] not in {"active", "awaiting_completion"}
        ):
            return None
        if operation["target_kind"] == "parent":
            parent = (
                await conn.execute(
                    select(tasks).where(tasks.c.id == operation["parent_task_id"])
                )
            ).mappings().one_or_none()
            checkpoint = (
                await conn.execute(
                    select(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id
                        == operation["parent_task_id"]
                    )
                )
            ).mappings().one_or_none()
            if (
                parent is None
                or checkpoint is None
                or checkpoint["episode_id"] != operation["episode_id"]
                or not parent["repo_id"]
                or not parent["branch_name"]
            ):
                return None
            project_id = parent["project_id"]
            target = BranchKey(
                repository_id=parent["repo_id"], branch=parent["branch_name"]
            )
        elif operation["target_kind"] == "batch":
            batch = (
                await conn.execute(
                    select(integration_batches).where(
                        integration_batches.c.id == operation["batch_id"]
                    )
                )
            ).mappings().one_or_none()
            if (
                batch is None
                or batch["id"] != operation["episode_id"]
                or not batch["integration_branch"]
                or batch["lifecycle"] not in {"testing", "repairing"}
            ):
                return None
            if stage == 0 and await conn.scalar(
                select(_unfinished_candidate_publication(batch["id"]))
            ):
                return None
            project_id = batch["project_id"]
            target = BranchKey(
                repository_id=batch["repository_id"],
                branch=batch["integration_branch"],
            )
        else:
            return None
        project = (
            await conn.execute(select(projects).where(projects.c.id == project_id))
        ).mappings().one_or_none()
        if (
            project is None
            or project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
            or project["integration_repository_id"] != target.repository_id
        ):
            return None
        return dict(operation), dict(repair_stage), target, str(project_id)

    async def _reuse_verifier_on(
        self, conn, operation, repair_stage, target, project_id: str
    ) -> dict[str, Any] | None:
        if operation["target_kind"] != "parent" or int(repair_stage["ordinal"]) != 0:
            return None
        expected_task_id = operation.get("verifier_task_id") or operation.get(
            "parent_task_id"
        )
        if repair_stage["repair_task_id"] is not None:
            if (
                repair_stage["repair_task_id"] != expected_task_id
                or repair_stage["writer_kind"] != "existing_verifier"
            ):
                return None
        owner = (
            await conn.execute(
                select(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == target.repository_id,
                    integration_branch_owners.c.ref == target.branch,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            owner is None
            or owner["owner_id"] != expected_task_id
            or owner["owner_role"] != "verifier"
            or owner["handoff_state"] != "attached"
            or not owner["session_id"]
            or not owner["workspace_id"]
        ):
            return None
        task = (
            await conn.execute(select(tasks).where(tasks.c.id == expected_task_id))
        ).mappings().one_or_none()
        session = (
            await conn.execute(
                select(sessions).where(sessions.c.id == owner["session_id"])
            )
        ).mappings().one_or_none()
        workspace = (
            await conn.execute(
                select(workspaces).where(workspaces.c.id == owner["workspace_id"])
            )
        ).mappings().one_or_none()
        if (
            task is None
            or task["project_id"] != project_id
            or task["repo_id"] != target.repository_id
            or task["branch_name"] != target.branch
            or task["status"]
            not in {TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value}
            or session is None
            or session["task_id"] != expected_task_id
            or session["project_id"] != project_id
            or session["state"] not in {"starting", "running", "draining"}
            or workspace is None
            or workspace["project_id"] != project_id
            or not workspace["enabled"]
        ):
            return None
        if repair_stage["repair_task_id"] is None:
            linked = await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation["id"],
                    integration_repair_stages.c.ordinal == 0,
                    integration_repair_stages.c.repair_task_id.is_(None),
                    integration_repair_stages.c.writer_kind.is_(None),
                )
                .values(
                    repair_task_id=expected_task_id,
                    writer_kind="existing_verifier",
                )
            )
            if linked.rowcount != 1:
                return None
        return self._dispatch_value(
            "writer_reused",
            operation["id"],
            0,
            repair_task_id=expected_task_id,
            writer_kind="existing_verifier",
            fence=Fence(
                target=target,
                owner_id=expected_task_id,
                token=int(owner["fence_token"]),
            ),
        )

    async def _is_primary_writer(
        self,
        owner,
        operation_id: str,
        *,
        states: frozenset[str] = _ATTACHED_PRIMARY_STATES,
        ordinal: int = 0,
    ) -> bool:
        """Whether *owner* is a predecessor writer in one of *states*.

        ``attached`` (the default) is the retained-handoff case of design spec
        §9.2: the primary is still live and its dirty checkout is rebound to
        the debugger.  ``_TRANSFERABLE_PRIMARY_STATES`` is the other half — a
        delegate that closed successfully self-transfers back to a ``reserved``
        fence in its own ``repair`` role
        (``arelease_integration_writer_for_retry``), which is already proven
        stopped and detached and is therefore a valid predecessor for the next
        stage.  Without it the escalation after a *successful* primary stage
        fell through to :meth:`_predecessor_matches`, which knows only
        ``collector`` and ``verifier``, and answered ``human_required``.
        A pending handoff is also recognized, but transfer still requires fresh
        server-side stop and detach confirmation before assigning its fence.
        """
        async with self.db._engine.connect() as conn:
            primary = (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.ordinal <= ordinal,
                        integration_repair_stages.c.repair_task_id == owner["owner_id"],
                    )
                )
            ).mappings().first()
        return bool(
            primary is not None
            and primary["repair_task_id"] == owner["owner_id"]
            and self._writer_role_matches(
                primary["writer_kind"], owner["owner_role"]
            )
            and owner["handoff_state"] in states
        )

    @staticmethod
    def _writer_role_matches(writer_kind: str | None, owner_role: str | None) -> bool:
        return (writer_kind, owner_role) in {
            ("repair_delegate", "repair"),
            ("existing_verifier", "verifier"),
        }

    async def _retained_debug_handoff(
        self, operation, debug_stage, target, owner, debug_task_id: str
    ) -> Fence | None:
        """Fence a stopped primary and atomically retain its dirty workspace."""
        if self._confirm_stopped is None:
            return None
        proof = self._confirm_stopped(dict(owner))
        if inspect.isawaitable(proof):
            proof = await proof
        if not isinstance(proof, dict):
            return None
        workspace_id = str(proof.get("workspace_id") or "")
        session_id = str(proof.get("session_id") or "")
        head_sha = str(proof.get("head_sha") or "")
        instance_token = str(proof.get("instance_token") or "")
        commit_proof = proof.get("commit_proof")
        if (
            workspace_id != owner.get("workspace_id")
            or session_id != owner.get("session_id")
            or not is_valid_git_oid(head_sha)
            or not instance_token
        ):
            return None
        old_task_id = owner["owner_id"]
        old_token = int(owner["fence_token"])
        released_claim = None
        released_epoch = None
        retained_path = None
        async with self.db.immediate() as conn:
            context = await self._dispatch_context_on(
                conn, operation["id"], int(debug_stage["ordinal"])
            )
            primary = (
                await conn.execute(
                    select(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation["id"],
                        integration_repair_stages.c.ordinal < int(debug_stage["ordinal"]),
                        integration_repair_stages.c.repair_task_id == old_task_id,
                    )
                    .order_by(integration_repair_stages.c.ordinal.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            current_owner = (
                await conn.execute(
                    select(integration_branch_owners)
                    .where(
                        integration_branch_owners.c.repository_id
                        == target.repository_id,
                        integration_branch_owners.c.ref == target.branch,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            workspace = (
                await conn.execute(
                    select(workspaces)
                    .where(workspaces.c.id == workspace_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            session = (
                await conn.execute(
                    select(sessions).where(sessions.c.id == session_id).with_for_update()
                )
            ).mappings().one_or_none()
            old_task = (
                await conn.execute(
                    select(tasks).where(tasks.c.id == old_task_id).with_for_update()
                )
            ).mappings().one_or_none()
            if (
                context is None
                or context[1]["repair_task_id"] != debug_task_id
                or primary is None
                or primary["repair_task_id"] != old_task_id
                or not self._writer_role_matches(
                    primary["writer_kind"], current_owner["owner_role"]
                    if current_owner is not None
                    else None,
                )
                or current_owner is None
                or current_owner["owner_id"] != old_task_id
                or current_owner["owner_role"]
                not in {"repair", "verifier"}
                or int(current_owner["fence_token"]) != old_token
                or current_owner["handoff_state"] != "attached"
                or current_owner["session_id"] != session_id
                or current_owner["workspace_id"] != workspace_id
                or session is None
                or session["task_id"] != old_task_id
                or session["state"] != "stopped"
                or session["desired_state"] != "stopped"
                or session["instance_token"] != instance_token
                or workspace is None
                or workspace["locked_by_task_id"] != old_task_id
                or workspace["project_id"] != context[3]
                or old_task is None
                or old_task["repo_id"] != target.repository_id
                or old_task["branch_name"] != target.branch
            ):
                return None
            new_token = old_token + 1
            changed = await conn.execute(
                update(integration_branch_owners)
                .where(
                    integration_branch_owners.c.id == current_owner["id"],
                    integration_branch_owners.c.fence_token == old_token,
                    integration_branch_owners.c.owner_id == old_task_id,
                    integration_branch_owners.c.handoff_state == "attached",
                )
                .values(
                    owner_id=debug_task_id,
                    owner_role="repair",
                    fence_token=new_token,
                    handoff_state="reserved",
                    session_id=None,
                    workspace_id=None,
                    updated_at=self.clock(),
                )
            )
            rebound = await conn.execute(
                update(workspaces)
                .where(
                    workspaces.c.id == workspace_id,
                    workspaces.c.locked_by_task_id == old_task_id,
                )
                .values(
                    locked_by_task_id=debug_task_id,
                    locked_by_agent_id=None,
                    locked_at=self.clock(),
                )
            )
            provenance = {
                "old_task_id": old_task_id,
                "new_task_id": debug_task_id,
                "old_session_id": session_id,
                "workspace_id": workspace_id,
                "old_fence_token": old_token,
                "new_fence_token": new_token,
                "head_sha": head_sha,
                "instance_token": instance_token,
            }
            current_debug = (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.operation_id == operation["id"],
                        integration_repair_stages.c.ordinal == debug_stage["ordinal"],
                    )
                )
            ).mappings().one()
            debug_dossier = dict(current_debug["dossier"] or {})
            starting_sha = head_sha
            if operation["target_kind"] == "parent":
                # A stopped checkout proves retained work, not publication.
                # Keep remote admission and later stages anchored on the
                # published subject; resume local work through its provenance.
                starting_sha = current_debug["starting_sha"]
                debug_dossier = self._dossier_with_repair_commits(
                    debug_dossier,
                    self._subject_sha(current_debug["current_subject"]),
                    head_sha,
                    commit_proof,
                )
            debug_dossier["receipts"] = await self._current_receipts_on(conn, operation)
            stage_changed = await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation["id"],
                    integration_repair_stages.c.ordinal == debug_stage["ordinal"],
                    integration_repair_stages.c.repair_task_id == debug_task_id,
                    integration_repair_stages.c.retained_workspace_id.is_(None),
                )
                .values(
                    starting_sha=starting_sha,
                    retained_workspace_id=workspace_id,
                    retained_handoff=provenance,
                    dossier=debug_dossier,
                )
            )
            await conn.execute(
                update(tasks)
                .where(tasks.c.id == debug_task_id)
                .values(
                    preferred_workspace_id=workspace_id,
                    description=await self._delegate_description_on(
                        conn,
                        operation,
                        dict(current_debug)
                        | {"starting_sha": starting_sha, "dossier": debug_dossier},
                    ),
                )
            )
            if old_task["assigned_agent_id"]:
                await conn.execute(
                    update(agents)
                    .where(
                        agents.c.id == old_task["assigned_agent_id"],
                        agents.c.current_task_id == old_task_id,
                    )
                    .values(state="IDLE", current_task_id=None)
                )
            old_status = (
                TaskStatus.PAUSED
                if primary["writer_kind"] == "existing_verifier"
                else TaskStatus.BLOCKED
            )
            await self.db._apply_transition(
                conn,
                old_task_id,
                old_status,
                context="integration_repair_retained_handoff",
                force=True,
                _manual_pause_control=True,
                assigned_agent_id=None,
            )
            if session["lifecycle"] == "pool" and session["last_claim_epoch"] is not None:
                released_epoch = session["last_claim_epoch"]
                retained_path = workspace["workspace_path"]
                released_claim = await self.db.release_claim(
                    session_id, conn=conn, task_status=old_status,
                    context="integration_repair_retained_handoff", now=self.clock(),
                    expected_task_id=old_task_id, expected_claim_epoch=released_epoch,
                    preserve_terminal_task=True, stop_after_release=True,
                )
                if not released_claim.released:
                    raise _RepairInvariant("retained writer claim could not be fenced and released")
            if (
                changed.rowcount != 1
                or rebound.rowcount != 1
                or stage_changed.rowcount != 1
            ):
                raise RuntimeError("retained repair handoff lost its compare-and-swap")
        if released_claim is not None:
            from src.claim_file import remove_claim_file_if_matches
            remove_claim_file_if_matches(retained_path, old_task_id, released_epoch)
            await self.db.log_blocked_flips(released_claim.flipped)
            await self.db._notify_settled(released_claim.settled)
            await self.db._notify_ready(released_claim.ready)
        return Fence(target=target, owner_id=debug_task_id, token=new_token)

    async def _restore_archived_delegate_on(
        self, conn, task_id, operation, stage, target, project_id
    ):
        """Recover a legacy archive of this still-active stage's exact delegate."""
        archived = (await conn.execute(
            select(archived_tasks).where(archived_tasks.c.id == task_id).with_for_update()
        )).mappings().one_or_none()
        if archived is None or archived["status"] not in {"COMPLETED", "FAILED", "BLOCKED"}:
            return None
        candidate = dict(archived) | {"status": TaskStatus.PAUSED.value}
        if not self._delegate_task_matches(candidate, operation, target, project_id):
            return None
        if not stage["intelligence_class"]:
            return None
        # The operation/stage locks precede this restore. The normal dispatch
        # handoff still fences the branch before this task can become READY.
        await self.db.create_task(Task(
            id=task_id, project_id=project_id, title=archived["title"],
            description=archived["description"] + "\n\n" + self._delegate_description(operation, stage),
            status=TaskStatus.PAUSED, priority=archived["priority"],
            repo_id=target.repository_id, branch_name=target.branch,
            retry_count=archived["retry_count"], max_retries=archived["max_retries"],
            # Filed unrouted with the stage's class hint; the router writes
            # the route, whatever profile the archived copy carried.
            class_hint=stage["intelligence_class"],
            created_by_kind="integration_repair", created_by_id=operation["id"],
            created_at=archived["created_at"],
        ), conn=conn)
        await conn.execute(delete(archived_tasks).where(archived_tasks.c.id == task_id))
        return (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().one()

    @staticmethod
    def _delegate_task_matches(task, operation, target, project_id: str) -> bool:
        return bool(
            task["project_id"] == project_id
            and task["parent_task_id"] is None
            and task["repo_id"] == target.repository_id
            and str(task["branch_name"] or "").removeprefix("refs/heads/")
            == target.branch.removeprefix("refs/heads/")
            and task["created_by_kind"] == "integration_repair"
            and task["created_by_id"] == operation["id"]
            and task["status"]
            in {
                TaskStatus.PAUSED.value,
                TaskStatus.READY.value,
                TaskStatus.ASSIGNED.value,
                TaskStatus.IN_PROGRESS.value,
            }
        )

    @staticmethod
    def _predecessor_matches(owner, operation) -> bool:
        if owner["owner_role"] == "collector":
            return owner["owner_id"] in {operation["id"], operation.get("batch_id")}
        return bool(
            owner["owner_role"] == "verifier"
            and owner["owner_id"]
            in {operation.get("verifier_task_id"), operation.get("parent_task_id")}
        )

    async def _delegate_description_on(self, conn, operation, repair_stage) -> str:
        conflict = None
        batch = None
        revision = None
        if operation["target_kind"] == "batch" and operation.get("batch_id"):
            batch = (
                await conn.execute(
                    select(integration_batches).where(
                        integration_batches.c.id == operation["batch_id"]
                    )
                )
            ).mappings().one_or_none()
            revision = None
            if batch is not None:
                revision = (
                    await conn.execute(
                        select(integration_candidate_revisions).where(
                            integration_candidate_revisions.c.batch_id == batch["id"],
                            integration_candidate_revisions.c.revision
                            == batch["current_revision"],
                        )
                    )
                ).mappings().one_or_none()
            member = None
            if revision is not None:
                member = (
                    await conn.execute(
                        select(integration_candidate_member_results).where(
                            integration_candidate_member_results.c.batch_id == batch["id"],
                            integration_candidate_member_results.c.revision
                            == revision["revision"],
                            integration_candidate_member_results.c.member_ordinal
                            == revision["next_member_ordinal"],
                            integration_candidate_member_results.c.result == "conflict",
                        )
                    )
                ).mappings().one_or_none()
            detail = member["conflict_evidence"] if member is not None else None
            if detail and detail.get("operation_id") == operation["id"]:
                conflict = dict(detail)
                if operation["policy_snapshot"]["root"]["repair"].get("conflict_scope") == "batch":
                    conflict["members"] = [dict(row) for row in (await conn.execute(
                        select(
                            integration_batch_members.c.ordinal,
                            integration_batch_members.c.task_id,
                            integration_batch_members.c.source_base_sha,
                            integration_batch_members.c.reviewed_head_sha,
                        ).where(integration_batch_members.c.batch_id == batch["id"])
                        .order_by(integration_batch_members.c.ordinal)
                    )).mappings()]
        candidate_ci = None
        if (
            conflict is None
            and batch is not None
            and revision is not None
            and revision["state"] == "testing"
        ):
            evidence = (
                await conn.execute(
                    select(integration_check_evidence)
                    .where(
                        integration_check_evidence.c.batch_id == batch["id"],
                        integration_check_evidence.c.candidate_revision
                        == revision["revision"],
                    )
                    .order_by(integration_check_evidence.c.observed_at.desc())
                    .limit(1)
                )
            ).mappings().first()
            candidate_ci = {
                "candidate_sha": revision["head_sha"],
                "construction_base_sha": revision["construction_base_sha"],
                "batch_id": batch["id"],
                "revision": revision["revision"],
                "run_id": evidence["run_id"] if evidence is not None else None,
                "conclusion": evidence["conclusion"] if evidence is not None else None,
            }
        return self._delegate_description(
            operation, repair_stage, conflict=conflict, candidate_ci=candidate_ci
        )

    @staticmethod
    def _delegate_description(operation, repair_stage, *, conflict=None, candidate_ci=None) -> str:
        description = (
            "Execute the frozen hierarchical-integration repair stage.\n\n"
            f"Operation: {operation['id']}\n"
            f"Stage: {repair_stage['ordinal']}\n"
            f"Starting SHA: {repair_stage['starting_sha']}\n"
            f"Dossier: {repair_stage['dossier']}"
        )
        refiles = (repair_stage["dossier"] or {}).get("writer_refiles")
        if refiles:
            description += (
                "\n\nThis stage was refiled at the same ordinal: its previous writer "
                f"(claim epoch {refiles[-1]['claim_epoch']}) stopped without publishing "
                "anything. Start from the published stage head."
            )
        progress = (repair_stage["dossier"] or {}).get("preserved_progress")
        if progress:
            description += (
                "\n\nResume the preserved unpublished progress at "
                f"{progress['ref']} ({progress['sha']}). The workspace starts at this exact tip. "
                f"Frozen members already present as ancestors: {progress['completed_member_ordinals']}. "
                "Keep those merges and finish only the remaining work. The original partial head "
                "still anchors the complete repair commit range and candidate submission. "
                "Preservation is not candidate acceptance: exact candidate CI and publication "
                "still use the frozen subject, manifest, and current delegate fence."
            )
        if candidate_ci is not None:
            run_line = (
                f"CI run: {candidate_ci['run_id']} ({candidate_ci['conclusion']})\n"
                if candidate_ci["run_id"]
                else "CI run: not yet visible"
            )
            description += (
                "\n\n## Awaiting candidate CI — no failure evidence exists yet\n\n"
                f"Batch: {candidate_ci['batch_id']}\n"
                f"Candidate revision: {candidate_ci['revision']}\n"
                f"Candidate SHA: {candidate_ci['candidate_sha']}\n"
                f"Construction base: {candidate_ci['construction_base_sha']}\n"
                f"{run_line}\n\n"
                "The frozen dossier's failed checks, logs and attempted commands are empty "
                "BECAUSE candidate CI has not reported yet. There is nothing on record to "
                "repair. Do not push, do not rebase, and do not fabricate a fix.\n\n"
                "Protocol:\n"
                "1. Check the candidate CI status for the revision above before acting "
                "(read the run above with your CI read; `aq git ci-baseline-status` covers "
                "branch baselines, not this candidate).\n"
                "2. While the run is pending or missing: close this stage pass-unchanged "
                "when CI turns green; the daemon publishes under its fence.\n"
                "3. If and only if a required check on the exact candidate SHA fails: "
                "then repair, record the failed checks, and submit through "
                "aq integration resolve-candidate-member as usual.\n"
                "A fix after a green result cannot be bound to this candidate; it would "
                "force a superseding revision instead."
            )
        if not conflict:
            return description
        if conflict.get("members"):
            merge_start = (
                "Continue at the preserved tip and merge every frozen source head still absent in "
                if progress
                else "Start at the partial head and merge EVERY remaining frozen source head in "
            )
            manifest = "\n".join(
                f"- {member['ordinal']}: {member['task_id']}, base {member['source_base_sha']}, "
                f"head {member['reviewed_head_sha']}"
                for member in conflict["members"]
            )
            return (
                f"{description}\n\n## Complete batch conflict repair\n\n"
                f"Batch: {conflict['batch_id']}\nRevision: {conflict['revision']}\n"
                f"Partial head: {conflict['partial_head_sha']}\n\n{manifest}\n\n{merge_start}"
                "manifest order in this workspace. Resolve all conflicts together, preserving "
                "the intended features. Retain every source head as an ancestor of the final "
                "head. You may edit any necessary repair file, including earlier features and "
                "migrations; re-chain colliding migration revisions and check alembic heads. "
                "Regenerate generated artifacts from resolved sources; never hand-merge them. "
                "Run focused checks, commit repairs, and record the complete first-parent "
                "range with git rev-list --first-parent --reverse PARTIAL_HEAD..HEAD.\n\n"
                "Submit through aq integration resolve-candidate-member with "
                "--resolved-head-sha HEAD --resolved-tree-sha TREE and one "
                "--repair-commit-sha per first-parent commit. The frozen policy makes this "
                "a complete batch resolution; the daemon derives all authority and publishes "
                "under its fence. Do not push the integration branch or main yourself. "
                "Candidate CI must pass on the exact resulting SHA before promotion."
            )
        member_start = (
            "Continue from the exact preserved tip above. "
            if progress else "Start from the exact partial head above. "
        )
        return (
            f"{description}\n\n"
            "## Candidate member conflict\n\n"
            f"Batch: {conflict['batch_id']}\n"
            f"Candidate revision: {conflict['revision']}\n"
            f"Member ordinal: {conflict['ordinal']}\n"
            f"Partial head: {conflict['partial_head_sha']}\n"
            f"Member source base: {conflict['source_base_sha']}\n"
            f"Member reviewed head: {conflict['source_head_sha']}\n\n"
            f"{member_start}Resolve only this member's conflict, "
            "commit the repair as a linear non-merge range, and do not push the integration "
            "branch yourself. Record `git rev-parse HEAD`, `git rev-parse HEAD^{tree}`, and "
            "each commit from `git rev-list --reverse PARTIAL_HEAD..HEAD`, then run:\n\n"
            "    aq integration resolve-candidate-member \\\n"
            "      --resolved-head-sha RESOLVED_HEAD_SHA \\\n"
            "      --resolved-tree-sha RESOLVED_TREE_SHA \\\n"
            "      --repair-commit-sha REPAIR_COMMIT_SHA\n\n"
            "Repeat `--repair-commit-sha` in oldest-to-newest order for every repair commit. "
            "The command derives the batch, member, operation, partial head, claim, workspace, "
            "and branch fence from this authenticated assignment; never supply or push a "
            "replacement lineage by hand."
        )

    async def _current_batch_subject_rows_on(self, conn, operation):
        batch = (
            await conn.execute(
                select(integration_batches).where(
                    integration_batches.c.id == operation["batch_id"]
                )
            )
        ).mappings().one_or_none()
        if batch is None or operation["episode_id"] != batch["id"]:
            raise ValueError("batch repair operation identity changed")
        revision = (
            await conn.execute(
                select(integration_candidate_revisions).where(
                    integration_candidate_revisions.c.batch_id == batch["id"],
                    integration_candidate_revisions.c.revision == batch["current_revision"],
                )
            )
        ).mappings().one_or_none()
        if revision is None:
            raise ValueError("batch current candidate revision is missing")
        return dict(batch), dict(revision)

    @staticmethod
    def _batch_subject(revision: dict[str, Any]) -> dict[str, Any]:
        candidate_sha = revision.get("head_sha") or revision["construction_base_sha"]
        return {
            "kind": "batch",
            "revision": int(revision["revision"]),
            "candidate_sha": candidate_sha,
        }

    async def _initial_dossier_on(
        self,
        conn,
        *,
        operation: dict[str, Any],
        subject: dict[str, Any],
        starting_sha: str,
        trigger_id: str,
        boundary,
        started_at: float,
        deadline_at: float,
    ) -> dict[str, Any]:
        if operation["target_kind"] == "parent":
            manifest = {
                "kind": "parent_episode",
                "parent_task_id": operation["parent_task_id"],
                "episode_id": operation["episode_id"],
                "generation": int(subject["generation"]),
            }
        else:
            batch = (
                await conn.execute(
                    select(integration_batches).where(
                        integration_batches.c.id == operation["batch_id"]
                    )
                )
            ).mappings().one()
            manifest = {
                "kind": "batch",
                "batch_id": operation["batch_id"],
                "source_manifest_digest": batch["source_manifest_digest"],
                "revision": int(subject["revision"]),
            }
        receipts = await self._current_receipts_on(conn, operation)
        limit = boundary.repair.primary_attempts
        return {
            "operation_id": operation["id"],
            "target_kind": operation["target_kind"],
            "starting_sha": starting_sha,
            "trigger_id": trigger_id,
            "manifest": manifest,
            "branch_sha": starting_sha,
            "required_checks": boundary.required_checks.model_dump(mode="json"),
            "artifact": boundary.route.artifact.model_dump(mode="json"),
            "receipts": receipts,
            "repair_commits": [],
            "failed_checks": [],
            "logs": [],
            "hypotheses": [],
            "commands_attempted": [],
            "budget": {
                "ordinal": 0,
                "started_at": started_at,
                "deadline_at": deadline_at,
                "attempt_limit": limit,
                "attempts": 0,
            },
        }

    @staticmethod
    def _dossier_with_evidence(
        dossier: dict[str, Any] | None,
        evidence,
        *,
        attempts: int,
    ) -> dict[str, Any]:
        updated = dict(dossier or {})
        failed_checks = list(updated.get("failed_checks", []))
        if evidence["conclusion"] == "failure":
            failed_checks.append(
                {"evidence_id": evidence["id"], "checks": evidence["checks"]}
            )
        updated["failed_checks"] = failed_checks
        updated["logs"] = list(updated.get("logs", [])) + [
            {
                "evidence_id": evidence["id"],
                "producer_id": evidence["producer_id"],
                "workflow_id": evidence["workflow_id"],
                "run_id": evidence["run_id"],
                "attempt": int(evidence["attempt"]),
            }
        ]
        updated["hypotheses"] = list(updated.get("hypotheses", [])) + [
            {
                "evidence_id": evidence["id"],
                "classification": evidence["classification"],
                "conclusion": evidence["conclusion"],
            }
        ]
        updated["commands_attempted"] = list(updated.get("commands_attempted", [])) + [
            {
                "workflow_id": evidence["workflow_id"],
                "run_id": evidence["run_id"],
                "attempt": int(evidence["attempt"]),
                "required_check_version": evidence["required_check_version"],
            }
        ]
        budget = dict(updated.get("budget", {}))
        budget["attempts"] = attempts
        updated["budget"] = budget
        return updated

    async def _current_receipts_on(self, conn, operation) -> list[dict[str, Any]]:
        if operation["target_kind"] == "parent":
            statement = select(task_delivery_receipts).where(
                task_delivery_receipts.c.parent_operation_id == operation["id"],
                task_delivery_receipts.c.parent_episode_id == operation["episode_id"],
            )
        else:
            statement = select(task_delivery_receipts).where(
                task_delivery_receipts.c.batch_id == operation["batch_id"]
            )
        rows = (await conn.execute(statement)).mappings().all()
        return [
            {
                "id": row["id"],
                "source_task_id": row["source_task_id"],
                "disposition": row["disposition"],
                "after_sha": row["after_sha"],
            }
            for row in sorted(rows, key=lambda item: item["id"])
        ]

    @staticmethod
    def _dossier_with_repair_commits(
        dossier: dict[str, Any] | None,
        previous_sha: str,
        head_sha: str,
        proof: dict[str, Any] | None,
    ) -> dict[str, Any]:
        updated = dict(dossier or {})
        commits = list(updated.get("repair_commits", []))
        if proof is None:
            # Some authoritative subject changes (for example Task9 candidate
            # rebinding) have no writer checkout to inspect.  Never relabel a
            # bare tip as the exact repair lineage; writer paths supply proof.
            additions = []
        else:
            additions = list(proof.get("commits") or [])
            if (
                proof.get("base_sha") != previous_sha
                or proof.get("head_sha") != head_sha
                or len(additions) != len(set(additions))
                or any(not is_valid_git_oid(value) for value in additions)
                or (head_sha != previous_sha and (not additions or additions[-1] != head_sha))
                or (head_sha == previous_sha and additions)
            ):
                raise ValueError("repair commit proof does not match the current subject")
        for commit_sha in additions:
            if commit_sha not in commits:
                commits.append(commit_sha)
        updated["repair_commits"] = commits
        updated["branch_sha"] = head_sha
        return updated

    async def _activate_debug_on(
        self,
        conn,
        *,
        operation: dict[str, Any],
        primary: dict[str, Any],
        attempts: int,
        now: float,
        terminal_state: str = "failed",
    ) -> bool:
        policy = HierarchicalIntegrationPolicy.model_validate(operation["policy_snapshot"])
        boundary = policy.parent if operation["target_kind"] == "parent" else policy.root
        previous_ordinal = int(primary["ordinal"])
        next_ordinal = previous_ordinal + 1
        primary_dossier = dict(primary["dossier"] or {})
        primary_dossier["receipts"] = await self._current_receipts_on(conn, operation)
        predecessor = None
        if previous_ordinal > 0:
            predecessor = (await conn.execute(select(integration_repair_stages).where(
                integration_repair_stages.c.operation_id == operation["id"],
                integration_repair_stages.c.ordinal == previous_ordinal - 1,
            ))).mappings().one_or_none()
            current_sha = self._subject_sha(primary["current_subject"])
            if (
                predecessor is None
                or not is_valid_git_oid(current_sha)
                or current_sha == self._subject_sha(predecessor["current_subject"])
            ):
                await self._supervisor_recovery_on(
                    conn, operation=operation, stage=primary | {"dossier": primary_dossier},
                    attempts=attempts, now=now, terminal_state=terminal_state,
                )
                return False
        successor_sha = self._subject_sha(primary["current_subject"])
        if is_valid_git_oid(successor_sha) and self._allocations_on_head(
            await self._stage_rows_on(conn, operation["id"]), successor_sha
        ) >= MAX_WRITER_ALLOCATIONS_PER_SUBJECT:
            # Refiles and successors share one budget per unchanged head.
            await self._supervisor_recovery_on(
                conn, operation=operation, stage=primary | {"dossier": primary_dossier},
                attempts=attempts, now=now, terminal_state=terminal_state,
                reason=(
                    f"{MAX_WRITER_ALLOCATIONS_PER_SUBJECT} writers were already allocated "
                    "on this unchanged subject head"
                ),
            )
            return False
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == operation["id"],
                integration_repair_stages.c.ordinal == previous_ordinal,
                integration_repair_stages.c.state.in_(("active", "awaiting_completion")),
            )
            .values(
                state=terminal_state,
                attempts=attempts,
                dossier=primary_dossier,
                completed_at=now,
            )
        )
        primary_budget = dict(primary_dossier.get("budget", {}))
        debug_dossier = dict(primary_dossier)
        # Refiles and deadline deferrals belong to the stage that made them.
        for key in ("writer_refiles", "deadline_deferrals", "deadline_deferral_count"):
            debug_dossier.pop(key, None)
        debug_dossier["allocation"] = {
            "subject_sha": successor_sha, "ordinal": next_ordinal, "allocated_at": now,
        }
        progress = debug_dossier.get("preserved_progress")
        if progress and progress["subject"] != primary["current_subject"]:
            history = list(debug_dossier.get("preserved_progress_history", []))
            history.append(debug_dossier.pop("preserved_progress"))
            debug_dossier["preserved_progress_history"] = history
        debug_dossier["starting_sha"] = self._subject_sha(primary["current_subject"])
        debug_dossier["branch_sha"] = self._subject_sha(primary["current_subject"])
        debug_dossier["budget"] = {
            "ordinal": next_ordinal,
            "started_at": now,
            "deadline_at": now + boundary.repair.debug_seconds,
            "attempt_limit": boundary.repair.debug_attempts,
            "attempts": 0,
        }
        debug_dossier["previous_stage"] = {
            "ordinal": previous_ordinal,
            "attempts": attempts,
            "budget": primary_budget,
            "dossier": primary_dossier if previous_ordinal == 0 else {
                "starting_sha": primary["starting_sha"],
                "current_subject": primary["current_subject"],
            },
        }
        trigger_id = f"stage-exhausted:{operation['id']}:{previous_ordinal}"
        conflict = await self._exhausted_subject_conflict_on(
            conn, operation, primary["current_subject"]
        )
        if conflict is not None:
            # The debug writer starts at the conflict's old tip, so the conflict is
            # still its work: fenced resolve and push require the trigger to name it.
            trigger_id = conflict["id"]
            debug_dossier["trigger_id"] = trigger_id
            debug_dossier["current_conflict"] = {
                "intent_id": trigger_id,
                "source_task_id": conflict["source_task_id"],
                "source_head": conflict["source_head"],
                "source_base": conflict["source_base"],
                "expected_target": conflict["expected_target"],
                "diagnostics": conflict["conflict_diagnostics"] or {},
            }
        debug = {
            "operation_id": operation["id"],
            "ordinal": next_ordinal,
            "policy": boundary.repair.model_dump(mode="json"),
            "intelligence_class": boundary.repair.debug_intelligence_class,
            # Deprecated column; ``debug_profile_id`` is ignored (routing spec §5.3).
            "profile_id": None,
            "repair_task_id": None,
            "writer_kind": None,
            "starting_sha": self._subject_sha(primary["current_subject"]),
            "trigger_id": trigger_id,
            "current_subject": primary["current_subject"],
            "deadline_event_id": f"repair-deadline-{operation['id']}-{next_ordinal}",
            "success_subject": None,
            "success_evidence_id": None,
            "started_at": now,
            "deadline_at": now + boundary.repair.debug_seconds,
            "attempts": 0,
            "dossier": debug_dossier,
            "state": "active",
        }
        await conn.execute(insert(integration_repair_stages).values(**debug))
        escalated = await conn.execute(
            update(integration_repair_operations)
            .where(
                integration_repair_operations.c.id == operation["id"],
                integration_repair_operations.c.active_stage == previous_ordinal,
                integration_repair_operations.c.state.in_(("active", "escalated")),
            )
            .values(active_stage=next_ordinal, state="escalated", updated_at=now)
        )
        if escalated.rowcount != 1:
            raise _RepairInvariant("repair operation changed before debug escalation")
        project_id = await self._operation_project_id_on(conn, operation)
        await enqueue_integration_event(
            conn,
            event_id=f"repair-exhausted-{operation['id']}-{previous_ordinal}",
            dedup_key=f"repair-exhausted:{operation['id']}:{previous_ordinal}",
            project_id=project_id,
            event_type="integration.repair_exhausted",
            payload={"operation_id": operation["id"]},
            available_at=now,
        )
        return True

    async def _exhausted_subject_conflict_on(
        self, conn, operation: dict[str, Any], subject: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Return the sole unresolved parent conflict whose old tip is ``subject``.

        A debug stage starting there inherits that conflict. Any other subject,
        a reserved or superseded resolution, or an ambiguous intent set leaves
        the ``stage-exhausted`` trigger (and detached-stage recovery) unchanged.
        """
        head = self._subject_sha(subject)
        if (
            operation["target_kind"] != "parent"
            or (subject or {}).get("kind") != "parent"
            or not is_valid_git_oid(head)
        ):
            return None
        parent = (
            await conn.execute(select(tasks).where(tasks.c.id == operation["parent_task_id"]))
        ).mappings().one_or_none()
        if parent is None:
            return None
        conflicts = (
            await conn.execute(
                select(integration_promotion_intents)
                .where(
                    integration_promotion_intents.c.operation_key == operation["id"],
                    integration_promotion_intents.c.target_task_id == parent["id"],
                    integration_promotion_intents.c.repository_id == parent["repo_id"],
                    integration_promotion_intents.c.target_branch == parent["branch_name"],
                    integration_promotion_intents.c.state.in_(("conflict", "resolution_reserved")),
                )
                .order_by(integration_promotion_intents.c.id)
                .limit(2)
                .with_for_update()
            )
        ).mappings().all()
        if len(conflicts) != 1:
            return None
        conflict = dict(conflicts[0])
        if (
            conflict["state"] != "conflict"
            or conflict["expected_target"] != head
            or conflict["resolution_head_sha"] is not None
            or conflict["superseded_by_intent_id"] is not None
        ):
            return None
        return conflict

    async def _supervisor_recovery_on(
        self, conn, *, operation, stage, attempts: int, now: float, terminal_state: str,
        reason: str = "repair subject has not advanced since the preceding stage",
    ) -> None:
        """End an unchanged-head budget once; preserve its writer and incident."""
        incident_id = f"repair-no-progress:{operation['id']}:{stage['ordinal']}"
        dossier = dict(stage["dossier"] or {})
        dossier["supervisor_recovery"] = {
            "incident_id": incident_id,
            "reason": reason,
            "subject": stage["current_subject"],
            "stage": int(stage["ordinal"]),
            "attempts": attempts,
            "deadline_at": stage["deadline_at"],
            "repair_task_id": stage["repair_task_id"],
            "recorded_at": now,
        }
        await conn.execute(update(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == operation["id"],
            integration_repair_stages.c.ordinal == stage["ordinal"],
        ).values(state=terminal_state, attempts=attempts, dossier=dossier, completed_at=now))
        await conn.execute(update(integration_repair_operations).where(
            integration_repair_operations.c.id == operation["id"],
        ).values(state="escalated", updated_at=now))
        project_id = await self._operation_project_id_on(conn, operation)
        owners = [dict(row) for row in (await conn.execute(
            select(integration_branch_owners).where(
                integration_branch_owners.c.owner_id == stage["repair_task_id"],
                integration_branch_owners.c.handoff_state != "released",
            )
        )).mappings()]
        await conn.execute(pg_insert(messages).values(
            id=f"msg-{incident_id}", project_id=project_id,
            from_kind="system", from_id="integration-repair", to_kind="session",
            to_id=f"supervisor-{project_id}",
            subject=f"Repair {operation['id']} stopped without progress",
            body=(f"Incident {incident_id}: no further automatic repair stage was allocated. "
                  f"Operation {operation['id']} remains escalated; target "
                  f"{operation['parent_task_id'] or operation['batch_id']}, "
                  f"subject {stage['current_subject']}, delegate {stage['repair_task_id']}, "
                  f"attempts {attempts}, deadline {stage['deadline_at']}. "
                  f"Stage history: {operation['id']} ordinals 0..{stage['ordinal']}. "
                  f"Dossier and retained owners: {dossier}; {owners}. "
                  "Reconcile the conflict/fence and preserved writer through supported "
                  "integration controls before authorizing further work. Human gates, "
                  "branches and workspace authority remain preserved. Archive only obsolete "
                  f"delegates with aq integration release-delegates {operation['id']} "
                  "--archive-obsolete."),
            created_at=now, priority=50, archive_after_inject=1,
            body_kind="integration_repair_no_progress",
        ).on_conflict_do_nothing(index_elements=[messages.c.id]))

    async def _human_block_on(
        self,
        conn,
        *,
        operation: dict[str, Any],
        stage: dict[str, Any],
        attempts: int,
        now: float,
        terminal_state: str,
    ):
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == operation["id"],
                integration_repair_stages.c.ordinal == stage["ordinal"],
                integration_repair_stages.c.state.in_(
                    ("active", "awaiting_completion")
                ),
            )
            .values(
                state=terminal_state,
                attempts=attempts,
                dossier=stage["dossier"],
                completed_at=now,
            )
        )
        human_blocked = await conn.execute(
            update(integration_repair_operations)
            .where(
                integration_repair_operations.c.id == operation["id"],
                integration_repair_operations.c.active_stage == stage["ordinal"],
                integration_repair_operations.c.state.in_(("active", "escalated")),
            )
            .values(state="human_required", updated_at=now)
        )
        if human_blocked.rowcount != 1:
            raise _RepairInvariant("repair operation changed before human escalation")
        transition = None
        if operation["target_kind"] == "parent":
            transition = await self.db._apply_transition(
                conn,
                operation["parent_task_id"],
                TaskStatus.BLOCKED,
                context="integration_repair_exhausted",
                force=True,
                _manual_pause_control=True,
            )
        else:
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == operation["batch_id"])
                .values(lifecycle="human_blocked", updated_at=now)
            )
        project_id = await self._operation_project_id_on(conn, operation)
        await enqueue_integration_event(
            conn,
            event_id=f"repair-human-{operation['id']}",
            dedup_key=f"repair-human:{operation['id']}",
            project_id=project_id,
            event_type="integration.human_blocked",
            payload={"operation_id": operation["id"]},
            available_at=now,
        )
        return transition

    @staticmethod
    async def _operation_project_id_on(conn, operation: dict[str, Any]) -> str:
        if operation["target_kind"] == "parent":
            from src.database.queries.task_identity import resolve_task_identity_on

            identity = await resolve_task_identity_on(conn, operation["parent_task_id"])
            project_id = identity.project_id if identity is not None else None
        else:
            project_id = (
                await conn.execute(
                    select(integration_batches.c.project_id).where(
                        integration_batches.c.id == operation["batch_id"]
                    )
                )
            ).scalar_one_or_none()
        if project_id is None:
            raise ValueError("repair operation project identity is missing")
        return str(project_id)

    _subject_sha = staticmethod(repair_subject_sha)

    async def _start_context_on(
        self,
        conn,
        operation: dict[str, Any],
        *,
        starting_sha: str,
        trigger_id: str,
    ):
        try:
            policy = HierarchicalIntegrationPolicy.model_validate(
                operation["policy_snapshot"]
            )
        except Exception as exc:
            raise _RepairInvariant("repair policy snapshot is corrupt") from exc
        if operation["target_kind"] not in {"parent", "batch"}:
            raise _RepairInvariant("repair target kind is corrupt")
        boundary = policy.parent if operation["target_kind"] == "parent" else policy.root
        if (
            operation["artifact_snapshot"]
            != boundary.route.artifact.model_dump(mode="json")
            or operation["required_check_version"] != boundary.required_checks.version
            or operation["route_playbook_id"] != boundary.route.playbook_id
            or operation["route_scope"] != boundary.route.scope
            or operation["route_scope_identifier"] != boundary.route.scope_identifier
            or operation["route_activation_id"] != boundary.route.activation_id
        ):
            raise _RepairInvariant("repair frozen route identity is corrupt")
        if operation["target_kind"] == "batch":
            try:
                batch, revision = await self._current_batch_subject_rows_on(conn, operation)
            except ValueError as exc:
                raise _RepairInvariant(str(exc)) from exc
            project = (
                await conn.execute(
                    select(projects).where(projects.c.id == batch["project_id"])
                )
            ).mappings().one_or_none()
            subject = self._batch_subject(revision)
            if project is None or project["integration_repository_id"] != batch["repository_id"]:
                raise _RepairInvariant("batch repair project identity is corrupt")
            if (
                batch["policy_snapshot"] != operation["policy_snapshot"]
                or batch["artifact_snapshot"] != operation["artifact_snapshot"]
                or batch["repository_id"] is None
            ):
                raise _RepairInvariant("batch repair identity is corrupt")
            if (
                project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
                or trigger_id != batch["id"]
                or subject["candidate_sha"] != starting_sha
            ):
                return None
            return policy, boundary, subject
        parent = (
            await conn.execute(
                select(tasks).where(tasks.c.id == operation["parent_task_id"])
            )
        ).mappings().one_or_none()
        if parent is None:
            raise _RepairInvariant("parent repair target is missing")
        project = (
            await conn.execute(select(projects).where(projects.c.id == parent["project_id"]))
        ).mappings().one_or_none()
        if project is None or project["integration_repository_id"] != parent["repo_id"]:
            raise _RepairInvariant("parent repair project identity is corrupt")
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == parent["id"]
                )
            )
        ).mappings().one_or_none()
        if checkpoint is None or checkpoint["episode_id"] != operation["episode_id"]:
            raise _RepairInvariant("parent repair checkpoint identity is corrupt")
        evidence = (
            await conn.execute(
                select(integration_check_evidence).where(
                    integration_check_evidence.c.id == trigger_id
                )
            )
        ).mappings().one_or_none()
        conflict = (
            await conn.execute(
                select(integration_promotion_intents).where(
                    integration_promotion_intents.c.id == trigger_id
                )
            )
        ).mappings().one_or_none()
        evidence_matches = bool(
            evidence is not None
            and evidence["operation_id"] == operation["id"]
            and evidence["parent_task_id"] == parent["id"]
            and int(evidence["parent_generation"]) == int(checkpoint["generation"])
            and evidence["parent_head_sha"] == starting_sha
            and evidence["conclusion"] == "failure"
            and evidence["required_check_version"]
            == operation["required_check_version"]
        )
        conflict_matches = bool(
            conflict is not None
            and conflict["state"] == "conflict"
            and conflict["operation_key"] == operation["id"]
            and conflict["target_task_id"] == parent["id"]
            and conflict["repository_id"] == parent["repo_id"]
            and conflict["target_branch"] == parent["branch_name"]
            and conflict["expected_target"] == starting_sha
        )
        if (
            project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
            or not (evidence_matches or conflict_matches)
        ):
            return None
        subject = {
            "kind": "parent",
            "generation": int(checkpoint["generation"]),
            "head_sha": starting_sha,
        }
        return policy, boundary, subject

    @staticmethod
    def _evidence_matches(operation, stage, evidence) -> bool:
        if (
            evidence is None
            or evidence["operation_id"] != operation["id"]
            or evidence["required_check_version"]
            != operation["required_check_version"]
        ):
            return False
        subject = stage["current_subject"] or {}
        if operation["target_kind"] == "parent":
            return bool(
                subject.get("kind") == "parent"
                and evidence["parent_task_id"] == operation["parent_task_id"]
                and int(evidence["parent_generation"]) == int(subject["generation"])
                and evidence["parent_head_sha"] == subject["head_sha"]
            )
        if operation["target_kind"] == "batch":
            return bool(
                subject.get("kind") == "batch"
                and evidence["batch_id"] == operation["batch_id"]
                and int(evidence["candidate_revision"]) == int(subject["revision"])
            )
        return False

    @staticmethod
    def _result_value(outcome: str, action: str, attempts: int) -> dict[str, Any]:
        return {"outcome": outcome, "action": action, "attempts": attempts}

    @staticmethod
    def _timeout_value(
        outcome: str, action: str, operation_id: str, stage: int
    ) -> dict[str, Any]:
        return {
            "outcome": outcome,
            "action": action,
            "operation_id": operation_id,
            "stage": stage,
        }

    @staticmethod
    def _dispatch_value(
        outcome: str,
        operation_id: str,
        stage: int,
        *,
        repair_task_id: str | None = None,
        writer_kind: str | None = None,
        fence: Fence | None = None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "outcome": outcome,
            "operation_id": operation_id,
            "stage": stage,
        }
        if repair_task_id is not None:
            value["repair_task_id"] = repair_task_id
        if writer_kind is not None:
            value["writer_kind"] = writer_kind
        if fence is not None:
            value["fence"] = fence.model_dump(mode="json")
        return value

    async def _dispatch_unknown(
        self,
        operation_id: str,
        stage: int,
        reason_code: str,
        reason: str,
        *,
        conn=None,
        repair_task_id: str | None = None,
        writer_kind: str | None = None,
    ) -> dict[str, Any]:
        """A state dispatch did not expect: retryable ``unknown``, never a human gate.

        Nothing is consumed, so the continuation and reservation passes retry
        the stage as it is.  The supervisor hears once per (operation, reason);
        the exact reason travels with every result.
        """
        if conn is None:
            async with self.db.immediate() as own:
                await self._dispatch_unknown_notice_on(
                    own, operation_id, stage, reason_code, reason
                )
        else:
            await self._dispatch_unknown_notice_on(
                conn, operation_id, stage, reason_code, reason
            )
        return self._dispatch_value(
            "unknown", operation_id, stage,
            repair_task_id=repair_task_id, writer_kind=writer_kind,
        ) | {"reason": reason, "reason_code": reason_code}

    async def _dispatch_unknown_notice_on(
        self, conn, operation_id: str, stage: int, reason_code: str, reason: str
    ) -> None:
        operation = (await conn.execute(
            select(integration_repair_operations)
            .where(integration_repair_operations.c.id == operation_id)
        )).mappings().one_or_none()
        if operation is None:
            return
        await self._stage_notice_on(
            conn, dict(operation),
            key=f"repair-dispatch-unknown:{operation_id}:{reason_code}",
            subject=f"Repair dispatch for {operation_id} is retrying: {reason_code}",
            body=(
                f"Dispatching repair stage {stage} of operation {operation_id} met a state "
                f"it did not expect ({reason_code}): {reason}. This is not a human "
                "decision: nothing was consumed and the stage stays retryable by the "
                "repair continuation and reservation passes. This notice is sent once per "
                "operation and reason."
            ),
            body_kind="integration_repair_dispatch_unknown",
            now=self.clock(),
        )

    @staticmethod
    def _start_value(stage: Any, *, outcome: str) -> dict[str, Any]:
        return {
            "outcome": outcome,
            "operation_id": stage["operation_id"],
            "stage": int(stage["ordinal"]),
            "starting_sha": stage["starting_sha"],
            "started_at": float(stage["started_at"]),
            "deadline_at": float(stage["deadline_at"]),
        }
