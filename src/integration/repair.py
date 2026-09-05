"""Durable bounded repair stages for parent and root integration operations."""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import insert, select, update

from src.database.tables import (
    agents,
    integration_batches,
    integration_branch_owners,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_operation_artifact_pins,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stage_evidence,
    integration_repair_stages,
    playbook_artifacts,
    projects,
    sessions,
    task_integration_checkpoints,
    tasks,
    workspaces,
)
from src.git.manager import is_valid_git_oid
from src.integration.models import HierarchicalIntegrationPolicy, RepairPolicy
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchBusy, BranchOwnership, StaleFence
from src.integration.outbox import enqueue_integration_event
from src.models import Task, TaskStatus
from src.playbooks.artifact_ref import ArtifactRef


class RepairService:
    """Own atomic stage clocks and evidence-accounting transitions."""

    def __init__(
        self,
        db,
        *,
        clock: Callable[[], float] = time.time,
        confirm_handoff=None,
        confirm_stopped=None,
        route_validator: Callable[[str, str | None], bool | Awaitable[bool]] | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self._ownership = BranchOwnership(db, confirm_handoff=confirm_handoff)
        self._confirm_stopped = confirm_stopped
        self._route_validator = route_validator

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

    async def start(
        self,
        operation_id: str,
        starting_sha: str,
        trigger_id: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Activate an already-reserved operation's primary stage exactly once."""
        if not is_valid_git_oid(starting_sha) or not str(trigger_id).strip():
            return {"outcome": "stale", "operation_id": operation_id}
        activated_at = self.clock() if now is None else now
        async with self.db.immediate() as conn:
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
            if existing is not None:
                if (
                    existing["starting_sha"] != starting_sha
                    or existing["trigger_id"] != trigger_id
                    or existing["deadline_event_id"]
                    != f"repair-deadline-{operation_id}-0"
                ):
                    return {"outcome": "invariant_error", "operation_id": operation_id}
                return self._start_value(existing, outcome="already_started")

            if operation["state"] != "active" or int(operation["active_stage"]) != 0:
                return {"outcome": "stale", "operation_id": operation_id}

            context = await self._start_context_on(
                conn, dict(operation), starting_sha=starting_sha, trigger_id=trigger_id
            )
            if context is None:
                return {"outcome": "stale", "operation_id": operation_id}
            policy, boundary, subject = context
            deadline_at = activated_at + boundary.repair.primary_seconds
            row = {
                "operation_id": operation_id,
                "ordinal": 0,
                "policy": boundary.repair.model_dump(mode="json"),
                "intelligence_class": boundary.primary_intelligence_class,
                "profile_id": boundary.primary_profile_id,
                "repair_task_id": None,
                "writer_kind": None,
                "starting_sha": starting_sha,
                "trigger_id": trigger_id,
                "current_subject": subject,
                "deadline_event_id": f"repair-deadline-{operation_id}-0",
                "started_at": activated_at,
                "deadline_at": deadline_at,
                "attempts": 0,
                "dossier": {
                    "operation_id": operation_id,
                    "target_kind": operation["target_kind"],
                    "starting_sha": starting_sha,
                    "trigger_id": trigger_id,
                    "required_checks": boundary.required_checks.model_dump(mode="json"),
                    "artifact": policy.parent.route.artifact.model_dump(mode="json")
                    if operation["target_kind"] == "parent"
                    else policy.root.route.artifact.model_dump(mode="json"),
                    "repair_commits": [],
                },
                "state": "active",
            }
            await conn.execute(insert(integration_repair_stages).values(**row))
            return self._start_value(row, outcome="started")

    async def record_result(
        self, operation_id: str, evidence_id: str, *, now: float | None = None
    ) -> dict[str, Any]:
        """Record one exact check attempt under the current stage budget."""
        recorded_at = self.clock() if now is None else now
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
                    )
                )
            elif counted and evidence["conclusion"] == "failure" and attempts >= limit:
                if int(stage["ordinal"]) == 0:
                    outcome = "escalate"
                    action = "dispatch_debug"
                    await self._activate_debug_on(
                        conn,
                        operation=dict(operation),
                        primary=dict(stage),
                        attempts=attempts,
                        now=recorded_at,
                    )
                    result_extra["stage"] = 1
                else:
                    outcome = "human_required"
                    action = "block_for_human"
                    post_transition = await self._human_block_on(
                        conn,
                        operation=dict(operation),
                        stage=dict(stage),
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
                    .values(attempts=attempts)
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
            result = self._result_value(outcome, action, attempts) | result_extra
        if post_transition is not None:
            await self.db.log_blocked_flips(post_transition.flipped)
            await self.db._notify_settled(post_transition.settled)
            await self.db._notify_ready(post_transition.ready)
        return result

    async def dispatch(self, operation_id: str, stage: int) -> dict[str, Any]:
        """Create and safely hand off to the exact current repair writer."""
        if stage not in {0, 1}:
            return self._dispatch_value("stale", operation_id, stage)

        # The durable relationship and paused task are committed before the
        # ownership callback is allowed to stop/detach the predecessor.
        async with self.db.immediate() as conn:
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
                if task is None or repair_stage["writer_kind"] != "repair_delegate":
                    return self._dispatch_value(
                        "human_required",
                        operation_id,
                        stage,
                        repair_task_id=repair_task_id,
                        writer_kind=repair_stage["writer_kind"],
                    )
                if not self._delegate_task_matches(task, operation, target, project_id):
                    return self._dispatch_value(
                        "human_required",
                        operation_id,
                        stage,
                        repair_task_id=repair_task_id,
                        writer_kind="repair_delegate",
                    )
            else:
                intelligence_class = repair_stage["intelligence_class"]
                profile_id = repair_stage["profile_id"]
                if not intelligence_class or not await self._route_is_valid(
                    intelligence_class, profile_id
                ):
                    return self._dispatch_value(
                        "configuration_blocked", operation_id, stage
                    )
                repair_task_id = f"repair-{operation_id}-{stage}"
                collision = (
                    await conn.execute(
                        select(tasks).where(tasks.c.id == repair_task_id).with_for_update()
                    )
                ).mappings().one_or_none()
                if collision is not None:
                    return self._dispatch_value(
                        "human_required", operation_id, stage
                    )
                await self.db.create_task(
                    Task(
                        id=repair_task_id,
                        project_id=project_id,
                        title=f"Repair integration stage {stage}",
                        description=self._delegate_description(operation, repair_stage),
                        status=TaskStatus.PAUSED,
                        parent_task_id=None,
                        repo_id=target.repository_id,
                        branch_name=target.branch,
                        profile_id=profile_id,
                        intelligence_class=intelligence_class,
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
            return self._dispatch_value(
                "human_required",
                operation_id,
                stage,
                repair_task_id=repair_task_id,
                writer_kind="repair_delegate",
            )
        if owner["owner_id"] == repair_task_id and owner["owner_role"] == "repair":
            fence = Fence(
                target=target,
                owner_id=repair_task_id,
                token=int(owner["fence_token"]),
            )
            replay = True
        else:
            retained_primary = stage == 1 and await self._is_primary_writer(
                owner, operation_id
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
                if not self._predecessor_matches(owner, operation):
                    return self._dispatch_value(
                        "human_required",
                        operation_id,
                        stage,
                        repair_task_id=repair_task_id,
                        writer_kind="repair_delegate",
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
            if (
                context is None
                or context[1]["repair_task_id"] != repair_task_id
                or context[1]["writer_kind"] != "repair_delegate"
                or owner is None
                or owner["owner_id"] != repair_task_id
                or owner["owner_role"] != "repair"
                or int(owner["fence_token"]) != fence.token
                or owner["handoff_state"] != "reserved"
                or task is None
                or task["status"] not in {
                    TaskStatus.PAUSED.value,
                    TaskStatus.READY.value,
                }
            ):
                return self._dispatch_value(
                    "human_required",
                    operation_id,
                    stage,
                    repair_task_id=repair_task_id,
                    writer_kind="repair_delegate",
                )
            ready = []
            if task["status"] == TaskStatus.PAUSED.value:
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

    async def expire(
        self, operation_id: str, stage: int, *, now: float | None = None
    ) -> dict[str, Any]:
        """Conditionally expire the exact current stage at its absolute deadline."""
        observed_at = self.clock() if now is None else now
        if stage not in {0, 1}:
            return self._timeout_value("stale", "ignore", operation_id, stage)
        async with self.db.immediate() as conn:
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
                return self._timeout_value(
                    "already_terminal",
                    "dispatch_debug" if stage == 0 else "block_for_human",
                    operation_id,
                    1 if stage == 0 else stage,
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
            if (
                operation["target_kind"] == "batch"
                and row["state"] == "awaiting_completion"
                and await self._root_success_is_current_on(conn, operation, row)
            ):
                return self._timeout_value(
                    "not_due", "awaiting_promotion", operation_id, stage
                )
            if stage == 0:
                await self._activate_debug_on(
                    conn,
                    operation=dict(operation),
                    primary=dict(row),
                    attempts=int(row["attempts"]),
                    now=observed_at,
                    terminal_state="expired",
                )
                return self._timeout_value(
                    "expired", "dispatch_debug", operation_id, 1
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

    async def due_stages(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Return every current durable stage whose absolute deadline is due."""
        observed_at = self.clock() if now is None else now
        statement = (
            select(
                integration_repair_stages.c.operation_id,
                integration_repair_stages.c.ordinal,
                integration_repair_stages.c.deadline_at,
                integration_repair_stages.c.deadline_event_id,
            )
            .join(
                integration_repair_operations,
                integration_repair_operations.c.id
                == integration_repair_stages.c.operation_id,
            )
            .where(
                integration_repair_operations.c.active_stage
                == integration_repair_stages.c.ordinal,
                integration_repair_operations.c.state.in_(("active", "escalated")),
                integration_repair_stages.c.state.in_(("active", "awaiting_completion")),
                integration_repair_stages.c.deadline_at.is_not(None),
                integration_repair_stages.c.deadline_at <= observed_at,
            )
            .order_by(
                integration_repair_stages.c.deadline_at,
                integration_repair_stages.c.operation_id,
            )
        )
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [
            {
                "operation_id": row["operation_id"],
                "stage": int(row["ordinal"]),
                "deadline_at": float(row["deadline_at"]),
                "deadline_event_id": row["deadline_event_id"],
            }
            for row in rows
        ]

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

    async def _route_is_valid(
        self, intelligence_class: str, profile_id: str | None
    ) -> bool:
        if self._route_validator is None:
            return False
        result = self._route_validator(intelligence_class, profile_id)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

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

    async def _is_primary_writer(self, owner, operation_id: str) -> bool:
        async with self.db._engine.connect() as conn:
            primary = (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.operation_id == operation_id,
                        integration_repair_stages.c.ordinal == 0,
                    )
                )
            ).mappings().one_or_none()
        return bool(
            primary is not None
            and primary["repair_task_id"] == owner["owner_id"]
            and primary["writer_kind"]
            in {"repair_delegate", "existing_verifier"}
            and owner["handoff_state"] == "attached"
        )

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
        if (
            workspace_id != owner.get("workspace_id")
            or session_id != owner.get("session_id")
            or not is_valid_git_oid(head_sha)
        ):
            return None
        old_task_id = owner["owner_id"]
        old_token = int(owner["fence_token"])
        async with self.db.immediate() as conn:
            context = await self._dispatch_context_on(
                conn, operation["id"], int(debug_stage["ordinal"])
            )
            primary = (
                await conn.execute(
                    select(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation["id"],
                        integration_repair_stages.c.ordinal == 0,
                    )
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
                or primary["writer_kind"]
                not in {"repair_delegate", "existing_verifier"}
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
            }
            stage_changed = await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation["id"],
                    integration_repair_stages.c.ordinal == 1,
                    integration_repair_stages.c.repair_task_id == debug_task_id,
                    integration_repair_stages.c.retained_workspace_id.is_(None),
                )
                .values(
                    retained_workspace_id=workspace_id,
                    retained_handoff=provenance,
                )
            )
            await conn.execute(
                update(tasks)
                .where(tasks.c.id == debug_task_id)
                .values(preferred_workspace_id=workspace_id)
            )
            if old_task["assigned_agent_id"]:
                await conn.execute(
                    update(agents)
                    .where(
                        agents.c.id == old_task["assigned_agent_id"],
                        agents.c.current_task_id == old_task_id,
                    )
                    .values(state="idle", current_task_id=None)
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
            if (
                changed.rowcount != 1
                or rebound.rowcount != 1
                or stage_changed.rowcount != 1
            ):
                raise RuntimeError("retained repair handoff lost its compare-and-swap")
        return Fence(target=target, owner_id=debug_task_id, token=new_token)

    @staticmethod
    def _delegate_task_matches(task, operation, target, project_id: str) -> bool:
        return bool(
            task["project_id"] == project_id
            and task["parent_task_id"] is None
            and task["repo_id"] == target.repository_id
            and task["branch_name"] == target.branch
            and task["created_by_kind"] == "integration_repair"
            and task["created_by_id"] == operation["id"]
            and task["status"] in {TaskStatus.PAUSED.value, TaskStatus.READY.value}
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

    @staticmethod
    def _delegate_description(operation, repair_stage) -> str:
        return (
            "Execute the frozen hierarchical-integration repair stage.\n\n"
            f"Operation: {operation['id']}\n"
            f"Stage: {repair_stage['ordinal']}\n"
            f"Starting SHA: {repair_stage['starting_sha']}\n"
            f"Dossier: {repair_stage['dossier']}"
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

    async def _activate_debug_on(
        self,
        conn,
        *,
        operation: dict[str, Any],
        primary: dict[str, Any],
        attempts: int,
        now: float,
        terminal_state: str = "failed",
    ) -> None:
        policy = HierarchicalIntegrationPolicy.model_validate(operation["policy_snapshot"])
        boundary = policy.parent if operation["target_kind"] == "parent" else policy.root
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == operation["id"],
                integration_repair_stages.c.ordinal == 0,
                integration_repair_stages.c.state.in_(("active", "awaiting_completion")),
            )
            .values(state=terminal_state, attempts=attempts, completed_at=now)
        )
        debug = {
            "operation_id": operation["id"],
            "ordinal": 1,
            "policy": boundary.repair.model_dump(mode="json"),
            "intelligence_class": boundary.repair.debug_intelligence_class,
            "profile_id": boundary.repair.debug_profile_id,
            "repair_task_id": None,
            "writer_kind": None,
            "starting_sha": self._subject_sha(primary["current_subject"]),
            "trigger_id": f"stage-exhausted:{operation['id']}:0",
            "current_subject": primary["current_subject"],
                "deadline_event_id": f"repair-deadline-{operation['id']}-1",
            "success_subject": None,
            "success_evidence_id": None,
            "started_at": now,
            "deadline_at": now + boundary.repair.debug_seconds,
            "attempts": 0,
            "dossier": {
                "operation_id": operation["id"],
                "target_kind": operation["target_kind"],
                "starting_sha": self._subject_sha(primary["current_subject"]),
                "required_checks": boundary.required_checks.model_dump(mode="json"),
                "artifact": boundary.route.artifact.model_dump(mode="json"),
                "repair_commits": list((primary["dossier"] or {}).get("repair_commits", [])),
                "previous_stage": {
                    "ordinal": 0,
                    "attempts": attempts,
                    "dossier": primary["dossier"],
                },
            },
            "state": "active",
        }
        await conn.execute(insert(integration_repair_stages).values(**debug))
        await conn.execute(
            update(integration_repair_operations)
            .where(
                integration_repair_operations.c.id == operation["id"],
                integration_repair_operations.c.active_stage == 0,
            )
            .values(active_stage=1, state="escalated", updated_at=now)
        )
        project_id = await self._operation_project_id_on(conn, operation)
        await enqueue_integration_event(
            conn,
            event_id=f"repair-exhausted-{operation['id']}-0",
            dedup_key=f"repair-exhausted:{operation['id']}:0",
            project_id=project_id,
            event_type="integration.repair_exhausted",
            payload={"operation_id": operation["id"]},
            available_at=now,
        )

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
                completed_at=now,
            )
        )
        await conn.execute(
            update(integration_repair_operations)
            .where(
                integration_repair_operations.c.id == operation["id"],
                integration_repair_operations.c.active_stage == stage["ordinal"],
            )
            .values(state="human_required", updated_at=now)
        )
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
            project_id = (
                await conn.execute(
                    select(tasks.c.project_id).where(
                        tasks.c.id == operation["parent_task_id"]
                    )
                )
            ).scalar_one_or_none()
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

    @staticmethod
    def _subject_sha(subject: dict[str, Any] | None) -> str:
        raw = subject or {}
        return str(raw.get("head_sha") or raw.get("candidate_sha") or "")

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
        except Exception:
            return None
        if operation["target_kind"] == "batch":
            batch, revision = await self._current_batch_subject_rows_on(conn, operation)
            project = (
                await conn.execute(
                    select(projects).where(projects.c.id == batch["project_id"])
                )
            ).mappings().one_or_none()
            subject = self._batch_subject(revision)
            if (
                project is None
                or project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
                or project["integration_repository_id"] != batch["repository_id"]
                or trigger_id != batch["id"]
                or subject["candidate_sha"] != starting_sha
            ):
                return None
            return policy, policy.root, subject
        if operation["target_kind"] != "parent":
            return None
        parent = (
            await conn.execute(
                select(tasks).where(tasks.c.id == operation["parent_task_id"])
            )
        ).mappings().one_or_none()
        if parent is None:
            return None
        project = (
            await conn.execute(select(projects).where(projects.c.id == parent["project_id"]))
        ).mappings().one_or_none()
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == parent["id"]
                )
            )
        ).mappings().one_or_none()
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
            project is None
            or project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
            or checkpoint is None
            or checkpoint["episode_id"] != operation["episode_id"]
            or not (evidence_matches or conflict_matches)
        ):
            return None
        subject = {
            "kind": "parent",
            "generation": int(checkpoint["generation"]),
            "head_sha": starting_sha,
        }
        return policy, policy.parent, subject

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
