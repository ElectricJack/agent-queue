"""Human recovery controls over already-frozen integration work."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, select, update

from src.database.queries.task_queries import TERMINAL_BLOCKED_META_KEY
from src.database.tables import (
    integration_attestation_publications,
    integration_batches,
    integration_branch_owners,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_resolutions,
    integration_cleanup_items,
    integration_parent_episodes,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    sessions,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)
from src.integration.models import RepairPolicy
from src.integration.outbox import enqueue_integration_event
from src.models import TaskStatus


class IntegrationRecoveryControls:
    """Resume, abort, and retry only when no external mutation is ambiguous."""

    def __init__(self, db: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.clock = clock

    async def resume(self, operation_id: str) -> dict[str, Any]:
        now = self.clock()
        transitions = []
        async with self.db.immediate() as conn:
            hint = (
                await conn.execute(
                    select(integration_repair_operations).where(
                        integration_repair_operations.c.id == operation_id
                    )
                )
            ).mappings().one_or_none()
            if hint is None:
                return {"outcome": "not_found", "operation_id": operation_id}
            project_id = await self._project_id_on(conn, hint)
            # Match collection and repair-start: project before operation.
            await self.db.lock_hierarchy_project(conn, project_id)
            operation = await self._locked_operation_on(conn, operation_id)
            if operation is None:
                return {"outcome": "not_found", "operation_id": operation_id}
            if await self._project_id_on(conn, operation) != project_id:
                return self._state_result("stale", operation, project_id)
            blockers = await self._ambiguous_writes_on(
                conn, operation, allow_reserved_delegate=True
            )
            if blockers:
                return self._ambiguous_result(operation, project_id, blockers)
            stage = await self._locked_stage_on(conn, operation)
            if stage is None:
                return self._state_result("invalid_state", operation, project_id)

            if operation["state"] in {"active", "escalated"}:
                # A retry after the first resume must not buy another timeout
                # window.  The persisted deadline identity is intentionally
                # the evidence: an active operation alone might be its
                # original, never-human-resumed attempt.
                if (
                    stage["state"] not in {"active", "awaiting_completion"}
                    or not self._has_operator_resume_evidence(operation, stage)
                ):
                    return self._state_result("invalid_state", operation, project_id)
                # A resumed parent can receive a newer conflict after its old
                # delegate closed. Resolve it before changing either task so
                # ambiguous/stale intent evidence leaves the durable resume
                # projection untouched. The actual continuation below uses
                # RepairService's same fence, detached-writer, dossier, and
                # budget checks as a public repair-start replay.
                from src.integration.repair import RepairService

                repair = RepairService(self.db)
                continuation = await repair.continue_current_parent_conflict_on(
                    conn,
                    operation,
                    stage,
                    project_id=project_id,
                    now=now,
                    validate_only=True,
                )
                if continuation["outcome"] == "stale":
                    return self._state_result("stale", operation, project_id)
                _, delegate_recovery = await self._restore_completed_delegate_on(
                    conn, operation, stage, validate_only=True
                )
                if delegate_recovery is not None:
                    return self._state_result(delegate_recovery, operation, project_id)
                transition, recovery = await self._restore_parent_collection_on(
                    conn, operation, allow_paused=True
                )
                if recovery is not None:
                    return self._state_result(recovery, operation, project_id)
                if transition is not None:
                    transitions.append(transition)
                if continuation["outcome"] == "ready":
                    continued = await repair.continue_current_parent_conflict_on(
                        conn,
                        operation,
                        stage,
                        project_id=project_id,
                        now=now,
                    )
                    if continued["outcome"] != "continued":
                        raise RuntimeError("repair continuation changed during resume")
                    transitions.append(continued["transition"])
                else:
                    delegate_transition, delegate_recovery = await self._restore_completed_delegate_on(
                        conn, operation, stage
                    )
                    if delegate_recovery is not None:
                        return self._state_result(delegate_recovery, operation, project_id)
                    if delegate_transition is not None:
                        transitions.append(delegate_transition)
                if transitions:
                    await self._event_on(
                        conn, operation, project_id, "integration.repair_exhausted", now
                    )
                result = {
                    "outcome": "resumed",
                    "operation_id": operation_id,
                    "project_id": project_id,
                    "state": operation["state"],
                    "stage": int(stage["ordinal"]),
                    "deadline_at": float(stage["deadline_at"]),
                }
            elif operation["state"] != "human_required" or stage["state"] not in {"failed", "expired", "cancelled"}:
                return self._state_result("invalid_state", operation, project_id)
            else:
                policy = RepairPolicy.model_validate(stage["policy"])
                timeout = (
                    policy.primary_seconds
                    if int(stage["ordinal"]) == 0
                    else policy.debug_seconds
                )
                deadline_event_id = f"repair-deadline-{operation_id}-resume-{uuid4().hex}"
                resumed_state = "active" if int(stage["ordinal"]) == 0 else "escalated"

                # Validate the current conflict against the state this human
                # resume will create, before restoring the parent, rearming
                # its clock, or reopening the completed delegate. A current
                # parent conflict requires an active stage, but this branch is
                # intentionally still human_required until every durable
                # ownership and episode proof has been checked.
                from src.integration.repair import RepairService

                prospective_operation = dict(operation) | {"state": resumed_state}
                prospective_stage = dict(stage) | {
                    "state": "active",
                    "started_at": now,
                    "deadline_at": now + timeout,
                    "deadline_event_id": deadline_event_id,
                    "completed_at": None,
                }
                repair = RepairService(self.db)
                continuation = await repair.continue_current_parent_conflict_on(
                    conn,
                    prospective_operation,
                    prospective_stage,
                    project_id=project_id,
                    now=now,
                    validate_only=True,
                )
                if continuation["outcome"] == "stale":
                    return self._state_result("stale", operation, project_id)
                _, delegate_recovery = await self._restore_completed_delegate_on(
                    conn, operation, stage, validate_only=True
                )
                if delegate_recovery is not None:
                    return self._state_result(delegate_recovery, operation, project_id)
                _, recovery = await self._restore_parent_collection_on(
                    conn, operation, allow_paused=False, validate_only=True
                )
                if recovery is not None:
                    return self._state_result(recovery, operation, project_id)
                transition, recovery = await self._restore_parent_collection_on(
                    conn, operation, allow_paused=False
                )
                if recovery is not None:
                    raise RuntimeError("parent recovery changed during resume")
                if transition is not None:
                    transitions.append(transition)
                await conn.execute(
                    update(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.ordinal == stage["ordinal"],
                        integration_repair_stages.c.state == stage["state"],
                    )
                    .values(
                        # Consumed attempts are durable (revision 3f30b34c7e7c keeps
                        # ``attempts`` monotone on both dialects): a human resume
                        # re-arms the stage's clock, never its attempt budget, so a
                        # resumed stage that fails again escalates or re-blocks
                        # instead of silently earning a fresh ladder.
                        state="active",
                        started_at=now,
                        deadline_at=now + timeout,
                        deadline_event_id=deadline_event_id,
                        completed_at=None,
                    )
                )
                await conn.execute(
                    update(integration_repair_operations)
                    .where(
                        integration_repair_operations.c.id == operation_id,
                        integration_repair_operations.c.state == "human_required",
                    )
                    .values(state=resumed_state, updated_at=now)
                )
                if continuation["outcome"] == "ready":
                    continued = await repair.continue_current_parent_conflict_on(
                        conn,
                        prospective_operation,
                        prospective_stage,
                        project_id=project_id,
                        now=now,
                    )
                    if continued["outcome"] != "continued":
                        raise RuntimeError("repair continuation changed during human resume")
                    transitions.append(continued["transition"])
                else:
                    delegate_transition, delegate_recovery = await self._restore_completed_delegate_on(
                        conn, operation, stage
                    )
                    if delegate_recovery is not None:
                        raise RuntimeError("delegate recovery changed during resume")
                    if delegate_transition is not None:
                        transitions.append(delegate_transition)
                if operation["target_kind"] == "batch":
                    await conn.execute(
                        update(integration_batches)
                        .where(
                            integration_batches.c.id == operation["batch_id"],
                            integration_batches.c.lifecycle == "human_blocked",
                        )
                        .values(lifecycle="repairing", human_abort_reason=None, updated_at=now)
                    )
                await self._event_on(
                    conn, operation, project_id, "integration.repair_exhausted", now
                )
                result = {
                    "outcome": "resumed",
                    "operation_id": operation_id,
                    "project_id": project_id,
                    "state": resumed_state,
                    "stage": int(stage["ordinal"]),
                    "deadline_at": now + timeout,
                }
        for transition in transitions:
            await self.db.log_blocked_flips(transition.flipped)
            await self.db._notify_settled(transition.settled)
            await self.db._notify_ready(transition.ready)
        return result

    @staticmethod
    def _has_operator_resume_evidence(
        operation: dict[str, Any], stage: dict[str, Any]
    ) -> bool:
        return bool(stage["deadline_event_id"] and str(stage["deadline_event_id"]).startswith(
            f"repair-deadline-{operation['id']}-resume-"
        ))

    async def _restore_parent_collection_on(
        self,
        conn: Any,
        operation: dict[str, Any],
        *,
        allow_paused: bool,
        validate_only: bool = False,
    ) -> tuple[Any | None, str | None]:
        """Restore only the terminally blocked parent for this exact episode.

        The transition owns terminal-block metadata and the blocked projection;
        the surrounding checks are repeated in its UPDATE guard so an old
        recovery request cannot revive a parent a newer writer has claimed.
        """
        if operation["target_kind"] != "parent":
            return None, None
        parent = (
            await conn.execute(
                select(tasks)
                .where(tasks.c.id == operation["parent_task_id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == operation["parent_task_id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        episode = (
            await conn.execute(
                select(integration_parent_episodes)
                .where(integration_parent_episodes.c.id == operation["episode_id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            parent is None
            or checkpoint is None
            or episode is None
            or checkpoint["episode_id"] != operation["episode_id"]
            or episode["parent_task_id"] != operation["parent_task_id"]
            or parent["repo_id"] != checkpoint["repository_id"]
            or parent["branch_name"] != checkpoint["branch"]
            or episode["repository_id"] != parent["repo_id"]
            or int(checkpoint["generation"]) < int(episode["generation"])
        ):
            return None, "stale"
        if await self.db._read_manual_pause(conn, operation["parent_task_id"]) is not None:
            return None, "invalid_state"
        if parent["assigned_agent_id"] is not None:
            return None, "invalid_state"
        if parent["status"] == TaskStatus.PAUSED.value:
            return (None, None) if allow_paused else (None, "invalid_state")
        terminal_context = (
            await conn.execute(
                select(task_metadata.c.value).where(
                    task_metadata.c.task_id == operation["parent_task_id"],
                    task_metadata.c.key == TERMINAL_BLOCKED_META_KEY,
                )
            )
        ).scalar_one_or_none()
        encoded_terminal_context = terminal_context
        try:
            terminal_context = json.loads(terminal_context)
        except (TypeError, ValueError):
            pass  # Older metadata may store the context without JSON encoding.
        if (
            parent["status"] != TaskStatus.BLOCKED.value
            or terminal_context != "integration_repair_exhausted"
        ):
            return None, "invalid_state"
        if validate_only:
            return None, None
        transition = await self.db._apply_transition(
            conn,
            operation["parent_task_id"],
            TaskStatus.PAUSED,
            context="integration_repair_resume",
            force=True,
            resume_after=None,
            assigned_agent_id=None,
            extra_where=and_(
                tasks.c.status == TaskStatus.BLOCKED.value,
                tasks.c.assigned_agent_id.is_(None),
                select(task_metadata.c.task_id)
                .where(
                    task_metadata.c.task_id == operation["parent_task_id"],
                    task_metadata.c.key == TERMINAL_BLOCKED_META_KEY,
                    task_metadata.c.value == encoded_terminal_context,
                )
                .exists(),
            ),
            returning=True,
        )
        if transition.row is None:
            return None, "stale"
        return transition, None

    async def _restore_completed_delegate_on(
        self, conn: Any, operation: dict[str, Any], stage: dict[str, Any],
        *, validate_only: bool = False,
    ) -> tuple[Any | None, str | None]:
        """Make an exact, safely released repair delegate dispatchable again.

        A delegate may have closed after producing the failed CI evidence that
        exhausted its stage.  Human resume keeps its attempt count, but must
        return that same released writer to PAUSED so the durable repair event
        can dispatch it; it never creates a second writer identity.
        """
        repair_task_id = stage["repair_task_id"]
        if repair_task_id is None or stage["writer_kind"] != "repair_delegate":
            return None, None
        delegate = (
            await conn.execute(
                select(tasks).where(tasks.c.id == repair_task_id).with_for_update()
            )
        ).mappings().one_or_none()
        if delegate is None:
            return None, "stale"
        if await self.db._read_manual_pause(conn, repair_task_id) is not None:
            return None, "invalid_state"
        if operation["target_kind"] == "parent":
            target = (await conn.execute(select(tasks).where(
                tasks.c.id == operation["parent_task_id"]
            ).with_for_update())).mappings().one()
            project_id, repo_id, branch = target["project_id"], target["repo_id"], target["branch_name"]
        else:
            target = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == operation["batch_id"]
            ).with_for_update())).mappings().one()
            project_id, repo_id, branch = target["project_id"], target["repository_id"], target["integration_branch"]
        if (
            delegate["project_id"] != project_id
            or delegate["repo_id"] != repo_id
            or delegate["branch_name"] != branch
            or delegate["created_by_kind"] != "integration_repair"
            or delegate["created_by_id"] != operation["id"]
            or delegate["assigned_agent_id"] is not None
            or delegate["parent_task_id"] is not None
        ):
            return None, "invalid_state"
        live_session = (await conn.execute(select(sessions.c.id).where(
            sessions.c.task_id == repair_task_id,
            (sessions.c.state != "stopped") | sessions.c.claim_phase.is_not(None),
        ).limit(1))).first()
        locked_workspace = (await conn.execute(select(workspaces.c.id).where(
            workspaces.c.locked_by_task_id == repair_task_id
        ).limit(1))).first()
        if live_session is not None or locked_workspace is not None:
            return None, "invalid_state"
        if delegate["status"] not in {
            TaskStatus.COMPLETED.value, TaskStatus.PAUSED.value, TaskStatus.READY.value,
        }:
            return None, "invalid_state"
        owner = (
            await conn.execute(
                select(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == delegate["repo_id"],
                    integration_branch_owners.c.ref == delegate["branch_name"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            owner is not None
            and owner["owner_id"] == operation["id"]
            and owner["owner_role"] == "collector"
            and owner["handoff_state"] == "reserved"
            and owner["session_id"] is None
            and owner["workspace_id"] is None
        ):
            return None, None
        if (
            owner is None
            or owner["owner_id"] != repair_task_id
            or owner["owner_role"] != "repair"
            or owner["handoff_state"] != "reserved"
            or owner["session_id"] is not None
            or owner["workspace_id"] is not None
        ):
            return None, "invalid_state"
        if validate_only or delegate["status"] != TaskStatus.COMPLETED.value:
            return None, None
        transition = await self.db._apply_transition(
            conn,
            repair_task_id,
            TaskStatus.PAUSED,
            context="integration_repair_resume_delegate",
            force=True,
            extra_where=tasks.c.status == TaskStatus.COMPLETED.value,
            returning=True,
        )
        if transition.row is None:
            return None, "stale"
        return transition, None

    async def abort(self, operation_id: str, *, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("abort reason is required")
        now = self.clock()
        async with self.db.immediate() as conn:
            operation = await self._locked_operation_on(conn, operation_id)
            if operation is None:
                return {"outcome": "not_found", "operation_id": operation_id}
            project_id = await self._project_id_on(conn, operation)
            if operation["state"] != "human_required":
                return self._state_result("invalid_state", operation, project_id)
            blockers = await self._ambiguous_writes_on(conn, operation)
            if blockers:
                return self._ambiguous_result(operation, project_id, blockers)
            stage = await self._locked_stage_on(conn, operation)
            await conn.execute(
                update(integration_repair_operations)
                .where(
                    integration_repair_operations.c.id == operation_id,
                    integration_repair_operations.c.state == "human_required",
                )
                .values(state="cancelled", updated_at=now)
            )
            if stage is not None and stage["state"] not in {"passed", "cancelled"}:
                await conn.execute(
                    update(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.ordinal == stage["ordinal"],
                    )
                    .values(state="cancelled", completed_at=now)
                )
            if operation["target_kind"] == "batch":
                await conn.execute(
                    update(integration_batches)
                    .where(
                        integration_batches.c.id == operation["batch_id"],
                        integration_batches.c.lifecycle == "human_blocked",
                    )
                    .values(lifecycle="aborted", human_abort_reason=reason, updated_at=now)
                )
        return {
            "outcome": "aborted",
            "operation_id": operation_id,
            "project_id": project_id,
            "reason": reason,
        }

    async def retry_cleanup(self, batch_id: str) -> dict[str, Any]:
        """Requeue existing safe identities without changing any irreversible marker."""
        now = self.clock()
        async with self.db.immediate() as conn:
            batch = (
                await conn.execute(
                    select(integration_batches)
                    .where(integration_batches.c.id == batch_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if batch is None:
                return {"outcome": "not_found", "batch_id": batch_id}
            rows = (
                await conn.execute(
                    select(integration_cleanup_items)
                    .where(
                        integration_cleanup_items.c.batch_id == batch_id,
                        integration_cleanup_items.c.state.in_(("retryable", "failed")),
                    )
                    .with_for_update()
                )
            ).mappings().all()
            ambiguous = [
                row["domain_key"]
                for row in rows
                if row["irreversible_prewrite_at"] is not None
                or row["execution_nonce"] is not None
            ]
            if ambiguous:
                return {
                    "outcome": "ambiguous",
                    "batch_id": batch_id,
                    "project_id": batch["project_id"],
                    "blockers": [
                        {
                            "code": "cleanup_irreversible",
                            "detail": (
                                "cleanup item has an unresolved irreversible write marker"
                            ),
                            "ref": identity,
                        }
                        for identity in sorted(ambiguous)
                    ],
                }
            if not rows:
                return {
                    "outcome": "nothing_to_retry",
                    "batch_id": batch_id,
                    "project_id": batch["project_id"],
                }
            identities = [row["domain_key"] for row in rows]
            await conn.execute(
                update(integration_cleanup_items)
                .where(integration_cleanup_items.c.domain_key.in_(identities))
                .values(
                    state="retryable",
                    attempts=0,
                    next_attempt_at=now,
                    terminal_at=None,
                    updated_at=now,
                )
            )
        return {
            "outcome": "requeued",
            "batch_id": batch_id,
            "project_id": batch["project_id"],
            "count": len(identities),
        }

    @staticmethod
    async def _locked_operation_on(conn: Any, operation_id: str) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                select(integration_repair_operations)
                .where(integration_repair_operations.c.id == operation_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        return dict(row) if row is not None else None

    @staticmethod
    async def _locked_stage_on(conn: Any, operation: dict[str, Any]) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                select(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation["id"],
                    integration_repair_stages.c.ordinal == operation["active_stage"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        return dict(row) if row is not None else None

    @staticmethod
    async def _project_id_on(conn: Any, operation: dict[str, Any]) -> str:
        if operation["target_kind"] == "batch":
            statement = select(integration_batches.c.project_id).where(
                integration_batches.c.id == operation["batch_id"]
            )
        else:
            statement = select(tasks.c.project_id).where(
                tasks.c.id == operation["parent_task_id"]
            )
        project_id = (await conn.execute(statement)).scalar_one_or_none()
        if project_id is None:
            raise ValueError("operation target has no owning project")
        return str(project_id)

    @staticmethod
    async def _ambiguous_writes_on(
        conn: Any,
        operation: dict[str, Any],
        *,
        allow_reserved_delegate: bool = False,
    ) -> list[str]:
        operation_id = operation["id"]
        writer = select(integration_branch_owners.c.id).where(
            (
                integration_branch_owners.c.owner_id.in_(
                    select(integration_repair_stages.c.repair_task_id).where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.repair_task_id.is_not(None),
                    )
                )
            )
            | (
                integration_branch_owners.c.owner_id.in_(
                    select(integration_candidate_ref_mutations.c.branch_owner_id).where(
                        integration_candidate_ref_mutations.c.operation_id
                        == operation_id
                    )
                )
            ),
            integration_branch_owners.c.handoff_state != "released",
        )
        if allow_reserved_delegate:
            exact_delegate = select(
                integration_repair_stages.c.repair_task_id
            ).where(
                integration_repair_stages.c.operation_id == operation_id,
                integration_repair_stages.c.ordinal == operation["active_stage"],
                integration_repair_stages.c.writer_kind == "repair_delegate",
            )
            writer = writer.where(
                ~and_(
                    integration_branch_owners.c.owner_id.in_(exact_delegate),
                    integration_branch_owners.c.owner_role == "repair",
                    integration_branch_owners.c.handoff_state == "reserved",
                    integration_branch_owners.c.session_id.is_(None),
                    integration_branch_owners.c.workspace_id.is_(None),
                )
            )
        if allow_reserved_delegate and operation["batch_id"] is not None:
            # A detached collector is the daemon's reservation, not a live
            # worker. Applied writes are checked separately below; retain all
            # ambiguity guards for attached collectors and other targets.
            exact_target = select(integration_batches.c.id).where(
                integration_batches.c.id == operation["batch_id"],
                integration_batches.c.repository_id == integration_branch_owners.c.repository_id,
                integration_batches.c.integration_branch == integration_branch_owners.c.ref,
            ).exists()
            writer = writer.where(
                ~and_(
                    integration_branch_owners.c.owner_id == operation_id,
                    integration_branch_owners.c.owner_role == "collector",
                    exact_target,
                    integration_branch_owners.c.handoff_state == "reserved",
                    integration_branch_owners.c.session_id.is_(None),
                    integration_branch_owners.c.workspace_id.is_(None),
                )
            )
        statements = {
            "ref_mutation": select(integration_candidate_ref_mutations.c.id).where(
                integration_candidate_ref_mutations.c.operation_id == operation_id,
                integration_candidate_ref_mutations.c.state == "reserved",
            ),
            "resolution": select(integration_candidate_resolutions.c.id).where(
                integration_candidate_resolutions.c.operation_id == operation_id,
                integration_candidate_resolutions.c.state.in_(("reserved", "pushed")),
            ),
            "attestation": select(integration_attestation_publications.c.id).where(
                integration_attestation_publications.c.operation_id == operation_id,
                integration_attestation_publications.c.state == "reserved",
            ),
            "promotion": select(integration_promotion_intents.c.id).where(
                (
                    integration_promotion_intents.c.operation_key == operation_id
                )
                | (
                    integration_promotion_intents.c.resolution_operation_id
                    == operation_id
                ),
                integration_promotion_intents.c.state.not_in(
                    ("committed", "conflict", "superseded")
                ),
            ),
            "writer": writer,
        }
        if operation["batch_id"] is not None:
            statements["candidate_publication"] = select(
                integration_candidate_publications.c.batch_id
            ).where(
                integration_candidate_publications.c.batch_id == operation["batch_id"],
                integration_candidate_publications.c.state != "pr_published",
            )
            if allow_reserved_delegate:
                # pr_reserved follows a confirmed ref write. Audit-PR metadata
                # may remain pending without making that branch write uncertain.
                pub = integration_candidate_publications.c
                mutation = integration_candidate_ref_mutations.c
                applied_ref = select(mutation.id).where(
                    mutation.purpose == "candidate_final",
                    mutation.state == "applied",
                    mutation.operation_id == operation_id,
                    mutation.operation_episode_id == operation["episode_id"],
                    mutation.batch_id == pub.batch_id,
                    mutation.revision == pub.revision,
                    mutation.repository_id == pub.repository_id,
                    mutation.target_branch == "refs/heads/" + pub.head_ref,
                    mutation.branch == mutation.target_branch,
                    mutation.target_branch == select(integration_batches.c.integration_branch)
                        .where(integration_batches.c.id == operation["batch_id"]).scalar_subquery(),
                    mutation.expected_old_sha == pub.expected_old_sha,
                    mutation.desired_sha == pub.head_sha,
                    mutation.remote_sha == pub.head_sha,
                ).exists()
                statements["candidate_publication"] = statements["candidate_publication"].where(
                    ~and_(pub.state == "pr_reserved", applied_ref)
                )
            statements["cleanup_prewrite"] = select(
                integration_cleanup_items.c.domain_key
            ).where(
                integration_cleanup_items.c.batch_id == operation["batch_id"],
                integration_cleanup_items.c.irreversible_prewrite_at.is_not(None),
                integration_cleanup_items.c.state.in_(("pending", "retryable")),
            )
        blockers = []
        for kind, statement in statements.items():
            if (await conn.execute(statement.limit(1))).scalar_one_or_none() is not None:
                blockers.append(kind)
        return sorted(blockers)

    @staticmethod
    async def _event_on(
        conn: Any,
        operation: dict[str, Any],
        project_id: str,
        event_type: str,
        now: float,
    ) -> None:
        action = event_type.rsplit(".", 1)[-1]
        await enqueue_integration_event(
            conn,
            event_id=f"repair-{action}-{operation['id']}-{uuid4().hex}",
            dedup_key=f"repair-{action}:{operation['id']}:{now}",
            project_id=project_id,
            event_type=event_type,
            payload={"operation_id": operation["id"]},
            available_at=now,
        )

    @staticmethod
    def _state_result(
        outcome: str, operation: dict[str, Any], project_id: str
    ) -> dict[str, Any]:
        return {
            "outcome": outcome,
            "operation_id": operation["id"],
            "project_id": project_id,
            "state": operation["state"],
        }

    @staticmethod
    def _ambiguous_result(
        operation: dict[str, Any], project_id: str, blockers: list[str]
    ) -> dict[str, Any]:
        return {
            "outcome": "ambiguous",
            "operation_id": operation["id"],
            "project_id": project_id,
            "state": operation["state"],
            "blockers": [
                {
                    "code": "ambiguous_external_write",
                    "detail": "operation has unresolved external mutation evidence",
                    "ref": blocker,
                }
                for blocker in blockers
            ],
        }
