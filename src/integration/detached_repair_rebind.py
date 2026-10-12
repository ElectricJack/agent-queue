"""Operator rebind of a detached parent repair stage frozen on an unpublished head.

docs/superpowers/specs/2026-10-01-detached-repair-rebind-design.md: a debug
stage whose frozen starting commit never reached the parent branch can never
admit its writer, and its ``stage-exhausted`` trigger cannot resolve the open
conflict. This control proves that exact state and points the stage back at the
published head and its conflict intent. It never writes Git, renews a budget,
creates a stage, records a delivery or changes ownership.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select, update

from src.database.tables import (
    gates,
    integration_branch_owners,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    sessions,
    task_gates,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)
from src.git.manager import GitError, RemoteRefState, is_valid_git_oid
from src.integration.promotion_contracts import PromotionError
from src.integration.writers import OperationSafety
from src.integration.repair import RepairService, _RepairInvariant
from src.models import TaskStatus

logger = logging.getLogger(__name__)

_DELEGATE_STATUSES = frozenset(
    {TaskStatus.PAUSED.value, TaskStatus.READY.value, TaskStatus.BLOCKED.value}
)
#: The only BLOCKED delegate this control revives: the failed admission itself.
_ADMISSION_FAILURE = "slot_reset_failed"


class DetachedRepairRebind:
    def __init__(self, promotion, repair: RepairService):
        self.promotion = promotion
        self.repair = repair
        self.db = promotion.db

    async def run(
        self,
        operation_id: str,
        *,
        dry_run: bool,
        expected_stage: int | None = None,
        expected_remote_head_sha: str | None = None,
        reason: str | None = None,
        principal: str | None = None,
    ) -> dict[str, Any]:
        """Prove a detached stage frozen on an unpublished head, then rebind it.

        The durable proof runs twice: once to choose what Git must prove, and
        again under the same locks as the write. Git facts are about immutable
        commit IDs except the remote head, which apply re-reads in that write
        transaction.
        """
        async with self.db.immediate() as conn:
            proof = await self._proof_on(conn, operation_id)
        if proof["outcome"] != "ready":
            return self._public(proof)
        observed = await self._git_proof(proof)
        result = self._public(proof) | {
            key: observed[key]
            for key in ("remote_head_sha", "frozen_head_refs", "reason")
            if key in observed
        }
        if observed["outcome"] != "ready":
            return result | {"outcome": "blocked"}
        if dry_run:
            return result | {
                "outcome": "would_rebind",
                "next_step": (
                    "apply with --stage, --remote-head and --reason to rebind this stage; "
                    "its writer then resolves the conflict from the published head"
                ),
            }
        if (
            expected_stage != proof["stage"]
            or expected_remote_head_sha != observed["remote_head_sha"]
        ):
            return result | {"outcome": "changed"}

        transition = None
        async with self.db.immediate() as conn:
            current = await self._proof_on(conn, operation_id)
            if current["outcome"] != "ready":
                return self._public(current)
            if self._identity(current) != self._identity(proof):
                return self._public(current) | {"outcome": "changed"}
            remote = await self.promotion.git.als_remote_ref(
                str(observed["store"]), current["branch"],
                repository_url=observed["origin_url"],
            )
            if (
                remote.state is not RemoteRefState.PRESENT
                or remote.oid != observed["remote_head_sha"]
            ):
                return result | {"outcome": "changed", "reason": "published parent head moved"}
            transition = await self._rebind_on(
                conn, current, observed, reason=reason, principal=principal
            )
        await self.db.log_blocked_flips(transition.flipped)
        await self.db._notify_settled(transition.settled)
        await self.db._notify_ready(transition.ready)
        try:
            dispatched = await self.repair.dispatch(operation_id, proof["stage"])
            dispatch_outcome = dispatched["outcome"]
        except (GitError, RuntimeError, ValueError) as exc:
            # The rebind is committed; continuation replay retries a PAUSED delegate.
            logger.warning("detached repair rebind dispatch failed for %s: %s", operation_id, exc)
            dispatch_outcome = f"error: {exc}"
        delegate = await self.db.get_task(proof["repair_task_id"])
        return result | {
            "outcome": "rebound",
            "delegate_status": delegate.status.value if delegate is not None else None,
            "dispatch_outcome": dispatch_outcome,
            "next_step": (
                "the delegate claims at the published head and resolves the conflict "
                "through integration-resolve-conflict and push-conflict-resolution"
            ),
        }

    async def _proof_on(self, conn, operation_id: str) -> dict[str, Any]:
        hint = (
            await conn.execute(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.id == operation_id
                )
            )
        ).mappings().one_or_none()
        if hint is None:
            return {"outcome": "not_found", "operation_id": operation_id}
        base: dict[str, Any] = {"operation_id": operation_id}
        if hint["target_kind"] != "parent":
            return self._blocked(base, "only a parent repair operation can be rebound")
        try:
            project_id = await OperationSafety._project_id_on(conn, dict(hint))
        except ValueError as exc:
            return self._blocked(base, str(exc))
        # Match collection and repair-start: project before operation.
        await self.db.lock_hierarchy_project(conn, project_id)
        operation = await OperationSafety._locked_operation_on(conn, operation_id)
        if operation is None:
            return {"outcome": "not_found", "operation_id": operation_id}
        base["project_id"] = project_id
        if operation["state"] not in {"active", "escalated"}:
            return self._blocked(
                base,
                f"operation is {operation['state']}; human decisions stay with "
                "integration resume or abort",
            )
        stage = await OperationSafety._locked_stage_on(conn, operation)
        if stage is None:
            return self._blocked(base, "operation has no active stage row")
        ordinal = int(stage["ordinal"])
        dossier = dict(stage["dossier"] or {})
        frozen = str(stage["starting_sha"] or "")
        base |= {
            "stage": ordinal,
            "repair_task_id": stage["repair_task_id"],
            "frozen_head_sha": frozen,
            "deadline_at": stage["deadline_at"],
        }
        if stage["state"] != "active" or dossier.get("supervisor_recovery"):
            return self._blocked(base, f"stage {ordinal} is {stage['state']}, not active")
        if stage["writer_kind"] != "repair_delegate" or not stage["repair_task_id"]:
            return self._blocked(base, "stage has no repair delegate")

        parent = (
            await conn.execute(
                select(tasks).where(tasks.c.id == operation["parent_task_id"]).with_for_update()
            )
        ).mappings().one_or_none()
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == operation["parent_task_id"]
                )
            )
        ).mappings().one_or_none()
        if parent is None or checkpoint is None or parent["project_id"] != project_id:
            return self._blocked(base, "parent target or checkpoint is missing")
        branch = str(parent["branch_name"] or "")
        base["branch"] = branch
        if (
            checkpoint["episode_id"] != operation["episode_id"]
            or checkpoint["state"] != "awaiting_children"
            or checkpoint["repository_id"] != parent["repo_id"]
            or checkpoint["branch"] != branch
        ):
            return self._blocked(base, "parent checkpoint is not this operation's collection")
        if parent["status"] != TaskStatus.PAUSED.value:
            return self._blocked(base, f"parent is {parent['status']}, not PAUSED for repair")
        if not is_valid_git_oid(frozen) or stage["current_subject"] != {
            "kind": "parent",
            "generation": int(checkpoint["generation"]),
            "head_sha": frozen,
        }:
            return self._blocked(base, "stage subject has moved from its frozen starting commit")

        conflicts = (
            await conn.execute(
                select(integration_promotion_intents)
                .where(
                    integration_promotion_intents.c.operation_key == operation_id,
                    integration_promotion_intents.c.target_task_id == parent["id"],
                    integration_promotion_intents.c.repository_id == parent["repo_id"],
                    integration_promotion_intents.c.target_branch == branch,
                    integration_promotion_intents.c.state.in_(
                        ("conflict", "resolution_reserved")
                    ),
                )
                .order_by(integration_promotion_intents.c.id)
                .limit(2)
                .with_for_update()
            )
        ).mappings().all()
        if len(conflicts) != 1 or conflicts[0]["state"] != "conflict":
            return self._blocked(base, "operation has no single open conflict intent")
        intent = dict(conflicts[0])
        base |= {"intent_id": intent["id"], "expected_target": intent["expected_target"]}
        history = list(dossier.get("detached_rebinds") or [])
        if (
            stage["trigger_id"] == intent["id"]
            and frozen == intent["expected_target"]
            and history
            and history[-1].get("intent_id") == intent["id"]
            and history[-1].get("remote_head_sha") == frozen
        ):
            return base | {"outcome": "already_rebound", "remote_head_sha": frozen}
        if ordinal == 0 or stage["trigger_id"] != f"stage-exhausted:{operation_id}:{ordinal - 1}":
            return self._blocked(base, "stage is not a debug continuation of an exhausted stage")
        if frozen == intent["expected_target"]:
            return self._blocked(base, "stage already starts at the conflict's target")
        source = (
            await conn.execute(select(tasks).where(tasks.c.id == intent["source_task_id"]))
        ).mappings().one_or_none()
        if (
            intent["project_id"] != project_id
            or intent["fence_owner_id"] != operation_id
            or intent["resolution_head_sha"] is not None
            or intent["superseded_by_intent_id"] is not None
            or source is None
            or source["parent_task_id"] != parent["id"]
            or source["repo_id"] != parent["repo_id"]
            or source["status"] != TaskStatus.COMPLETED.value
        ):
            return self._blocked(base, "conflict intent is not the parent's open child conflict")
        try:
            context = await self.repair._start_context_on(
                conn,
                operation,
                starting_sha=intent["expected_target"],
                trigger_id=intent["id"],
            )
        except _RepairInvariant as exc:
            return self._blocked(base, str(exc))
        if context is None:
            return self._blocked(base, "conflict intent does not match the frozen operation")
        _policy, boundary, subject = context
        limit = (
            boundary.repair.primary_attempts if ordinal == 0 else boundary.repair.debug_attempts
        )
        if (
            stage["deadline_at"] is None
            or self.repair.clock() >= float(stage["deadline_at"])
            or int(stage["attempts"]) >= limit
        ):
            return self._blocked(
                base, "stage budget is exhausted; this control never renews a budget"
            )
        if (
            stage["retained_workspace_id"] is not None
            or stage["retained_handoff"]
            or dossier.get("preserved_progress")
        ):
            return self._blocked(base, "stage retains writer progress; use owner recovery")

        delegate_id = stage["repair_task_id"]
        owner = (
            await conn.execute(
                select(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == parent["repo_id"],
                    integration_branch_owners.c.ref == branch,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            owner is None
            or owner["owner_id"] != delegate_id
            or owner["owner_role"] != "repair"
            or owner["handoff_state"] != "reserved"
            or owner["session_id"] is not None
            or owner["workspace_id"] is not None
        ):
            return self._blocked(base, "repair writer is not detached and reserved")
        delegate = (
            await conn.execute(select(tasks).where(tasks.c.id == delegate_id).with_for_update())
        ).mappings().one_or_none()
        if (
            delegate is None
            or delegate["project_id"] != project_id
            or delegate["parent_task_id"] is not None
            or delegate["repo_id"] != parent["repo_id"]
            or str(delegate["branch_name"] or "").removeprefix("refs/heads/")
            != branch.removeprefix("refs/heads/")
            or delegate["created_by_kind"] != "integration_repair"
            or delegate["created_by_id"] != operation_id
        ):
            return self._blocked(base, "task is not this stage's repair delegate")
        base["delegate_status"] = delegate["status"]
        if delegate["status"] not in _DELEGATE_STATUSES or delegate["assigned_agent_id"]:
            return self._blocked(base, f"delegate is {delegate['status']} with an agent or claim")
        if delegate["status"] == TaskStatus.BLOCKED.value:
            attention = (
                await conn.execute(
                    select(task_metadata.c.value).where(
                        task_metadata.c.task_id == delegate_id,
                        task_metadata.c.key == "needs_attention",
                    )
                )
            ).scalar_one_or_none()
            if attention is None or json.loads(attention) != _ADMISSION_FAILURE:
                return self._blocked(base, "delegate is blocked for another reason")
        holders = (
            await conn.execute(select(sessions).where(sessions.c.task_id == delegate_id))
        ).mappings().all()
        if any(row["state"] != "stopped" or row["claim_phase"] is not None for row in holders):
            return self._blocked(base, "a session still holds the delegate")
        locked = (
            await conn.execute(
                select(workspaces.c.id).where(workspaces.c.locked_by_task_id == delegate_id)
            )
        ).first()
        if locked is not None:
            return self._blocked(base, "a workspace is still locked by the delegate")
        open_gate = (
            await conn.execute(
                select(gates.c.id)
                .join(task_gates, task_gates.c.gate_id == gates.c.id)
                .where(
                    task_gates.c.task_id.in_((delegate_id, parent["id"])),
                    gates.c.status == "open",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if open_gate is not None:
            return self._blocked(base, f"open gate {open_gate} holds the repair")
        blockers = await OperationSafety._ambiguous_writes_on(
            conn, operation, allow_reserved_delegate=True
        )
        if blockers:
            return self._blocked(base, "unresolved integration writes: " + ", ".join(blockers))
        receipts = await self.repair._current_receipts_on(conn, operation)
        return base | {
            "outcome": "ready",
            "operation": operation,
            "stage_row": dict(stage),
            "intent": intent,
            "subject": subject,
            "repository_id": parent["repo_id"],
            "owner_row_id": owner["id"],
            "fence_token": int(owner["fence_token"]),
            "receipts": receipts,
            "receipt_ids": tuple(row["id"] for row in receipts),
        }

    async def _git_proof(self, proof: dict[str, Any]) -> dict[str, Any]:
        """Prove the frozen head is an unpublished descendant of the published head."""
        promotion = self.promotion
        intent = proof["intent"]
        frozen = proof["frozen_head_sha"]
        try:
            repository = await promotion._resolve_repository(proof["repository_id"])
            promotion._assert_resolution_repository(intent, repository)
            await promotion._ensure_retained_repository(repository)
            store = repository.retained_git_dir
            async with promotion.git.arepository_transaction(str(store)):
                await promotion._fetch_all_heads(store, repository.origin_url)
                remote = await promotion.git.als_remote_ref(
                    str(store), proof["branch"], repository_url=repository.origin_url
                )
                if remote.state is not RemoteRefState.PRESENT or not remote.oid:
                    return {"outcome": "blocked", "reason": "published parent head is unreadable"}
                head = remote.oid
                observed = {"remote_head_sha": head}
                if head != intent["expected_target"]:
                    return observed | {
                        "outcome": "blocked",
                        "reason": "published parent head is not the conflict's expected target",
                    }
                kind = await promotion.git.arun_git_result(
                    ["cat-file", "-t", frozen], cwd=str(store), env={"LC_ALL": "C"},
                    lock_held=True,
                )
                if kind.returncode != 0 or kind.stdout.strip() != "commit":
                    return observed | {
                        "outcome": "blocked",
                        "reason": "frozen head is not reachable from any published ref",
                    }
                if not await promotion._is_ancestor(store, head, frozen):
                    return observed | {
                        "outcome": "blocked",
                        "reason": "frozen head does not descend from the published head",
                    }
                if await promotion._is_ancestor(store, frozen, head):
                    return observed | {
                        "outcome": "blocked",
                        "reason": "frozen head is already published on the parent branch",
                    }
                for receipt in proof["receipts"]:
                    if receipt["after_sha"] and not await promotion._is_ancestor(
                        store, receipt["after_sha"], head
                    ):
                        return observed | {
                            "outcome": "blocked",
                            "reason": f"receipt {receipt['id']} is not in the published history",
                        }
                containing = await promotion.git.arun_git_result(
                    ["for-each-ref", "--contains", frozen, "--format=%(refname)",
                     "refs/remotes/origin"],
                    cwd=str(store), env={"LC_ALL": "C"}, lock_held=True,
                )
                refs = tuple(containing.stdout.split()) if containing.returncode == 0 else ()
        except PromotionError as exc:
            return {"outcome": "blocked", "reason": str(exc)}
        return observed | {
            "outcome": "ready",
            "frozen_head_refs": refs,
            "store": store,
            "origin_url": repository.origin_url,
        }

    async def _rebind_on(self, conn, proof, observed, *, reason, principal):
        operation, stage, intent = proof["operation"], proof["stage_row"], proof["intent"]
        head = observed["remote_head_sha"]
        now = self.repair.clock()
        dossier = dict(stage["dossier"] or {})
        history = list(dossier.get("detached_rebinds") or [])
        history.append(
            {
                "intent_id": intent["id"],
                "remote_head_sha": head,
                "previous": {
                    "starting_sha": stage["starting_sha"],
                    "trigger_id": stage["trigger_id"],
                    "current_subject": stage["current_subject"],
                    "repair_commits": list(dossier.get("repair_commits") or []),
                },
                "frozen_head_refs": list(observed["frozen_head_refs"]),
                "receipt_ids": list(proof["receipt_ids"]),
                "owner_row_id": proof["owner_row_id"],
                "fence_token": proof["fence_token"],
                "delegate_status": proof["delegate_status"],
                "budget": {
                    "started_at": stage["started_at"],
                    "deadline_at": stage["deadline_at"],
                    "attempts": stage["attempts"],
                },
                "principal": principal,
                "reason": reason,
                "recorded_at": now,
            }
        )
        # The same marker a conflict continuation writes, so the parent
        # playbook's stage-zero alias and resume recognize this stage.
        continuations = list(dossier.get("continuations") or [])
        continuations.append({"intent_id": intent["id"], "starting_sha": head, "recorded_at": now})
        dossier.update(
            {
                "starting_sha": head,
                "branch_sha": head,
                "trigger_id": intent["id"],
                "continuations": continuations,
                "repair_commits": [],
                "receipts": proof["receipts"],
                "current_conflict": {
                    "intent_id": intent["id"],
                    "source_task_id": intent["source_task_id"],
                    "source_head": intent["source_head"],
                    "source_base": intent["source_base"],
                    "expected_target": head,
                    "diagnostics": intent["conflict_diagnostics"] or {},
                },
                "detached_rebinds": history,
            }
        )
        changed = await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == operation["id"],
                integration_repair_stages.c.ordinal == stage["ordinal"],
                integration_repair_stages.c.state == "active",
                integration_repair_stages.c.repair_task_id == stage["repair_task_id"],
                integration_repair_stages.c.starting_sha == stage["starting_sha"],
                integration_repair_stages.c.trigger_id == stage["trigger_id"],
                integration_repair_stages.c.attempts == stage["attempts"],
                integration_repair_stages.c.deadline_at == stage["deadline_at"],
            )
            .values(
                starting_sha=head,
                trigger_id=intent["id"],
                current_subject=proof["subject"],
                success_subject=None,
                success_evidence_id=None,
                dossier=dossier,
            )
        )
        if changed.rowcount != 1:
            raise RuntimeError("detached repair rebind lost its stage compare-and-swap")
        updated = stage | {
            "starting_sha": head,
            "trigger_id": intent["id"],
            "current_subject": proof["subject"],
            "dossier": dossier,
        }
        return await self.db._apply_transition(
            conn,
            stage["repair_task_id"],
            TaskStatus.PAUSED,
            context="integration_repair_detached_rebind",
            force=True,
            _manual_pause_control=True,
            assigned_agent_id=None,
            description=RepairService._delegate_description(operation, updated),
        )

    @staticmethod
    def _identity(proof: dict[str, Any]) -> tuple:
        stage = proof["stage_row"]
        return (
            proof["stage"],
            proof["repair_task_id"],
            stage["starting_sha"],
            stage["trigger_id"],
            stage["deadline_at"],
            stage["attempts"],
            proof["intent_id"],
            proof["expected_target"],
            proof["owner_row_id"],
            proof["fence_token"],
            proof["receipt_ids"],
        )

    @staticmethod
    def _blocked(base: dict[str, Any], reason: str) -> dict[str, Any]:
        return base | {"outcome": "blocked", "reason": reason}

    @staticmethod
    def _public(proof: dict[str, Any]) -> dict[str, Any]:
        hidden = {"operation", "stage_row", "intent", "subject", "receipts", "owner_row_id",
                  "repository_id", "expected_target", "project_id", "branch"}
        return {key: value for key, value in proof.items() if key not in hidden}
