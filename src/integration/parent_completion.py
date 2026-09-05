"""Receipt-owned parent integration episodes and completion guards."""

from __future__ import annotations

import time
import uuid
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy import insert, select, update

from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    integration_operation_artifact_pins,
    integration_episode_receipt_acceptances,
    integration_check_evidence,
    integration_branch_owners,
    integration_child_dispositions,
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verification_evidence,
    integration_parent_verifications,
    integration_repair_operations,
    integration_repair_stages,
    playbook_artifacts,
    projects,
    task_branch_origins,
    task_delivery_receipts,
    task_integration_checkpoints,
    task_session_attempts,
    tasks,
)
from src.integration.models import HierarchicalIntegrationPolicy
from src.integration.outbox import enqueue_integration_event
from src.playbooks.artifact_ref import ArtifactRef


_OID = re.compile(r"^[0-9a-f]{40}$")


class ParentCompletion:
    """Conn-owned primitives for one parent's durable collection episode."""

    def __init__(self, db, *, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.clock = clock

    async def reserve_episode_on(
        self,
        conn,
        *,
        parent: dict[str, Any],
        project: dict[str, Any],
        checkpoint: dict[str, Any],
        pre_collection_sha: str,
        carry_forward: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing_episode = checkpoint.get("episode_id")
        if existing_episode:
            existing = (
                await conn.execute(
                    select(integration_repair_operations).where(
                        integration_repair_operations.c.parent_task_id == parent["id"],
                        integration_repair_operations.c.episode_id == existing_episode,
                    )
                )
            ).mappings().one_or_none()
            if existing is None:
                raise HierarchyError("invariant_error", "checkpoint episode has no operation")
            return dict(existing)

        raw_policy = project.get("hierarchical_integration_policy")
        if raw_policy is None:
            raise HierarchyError("invalid", "hierarchical integration policy is missing")
        try:
            policy = HierarchicalIntegrationPolicy.model_validate(raw_policy)
        except Exception as exc:
            raise HierarchyError("invalid", f"hierarchical integration policy is invalid: {exc}") from exc
        boundary = policy.parent
        route = boundary.route
        artifact_row = (
            await conn.execute(
                select(playbook_artifacts).where(
                    playbook_artifacts.c.artifact_sha256 == route.artifact.artifact_sha256
                )
            )
        ).mappings().one_or_none()
        if artifact_row is None:
            raise HierarchyError("invalid", "parent route artifact is not stored")
        if ArtifactRef.from_row(artifact_row).as_dict() != route.artifact.model_dump(mode="json"):
            raise HierarchyError("invalid", "parent route artifact identity changed")
        if (
            artifact_row["playbook_id"] != route.playbook_id
            or artifact_row["scope"] != route.scope
            or artifact_row["scope_identifier"] != route.scope_identifier
        ):
            raise HierarchyError("invalid", "parent route does not match the stored artifact")

        now = self.clock()
        episode_id = str(uuid.uuid4())
        operation_id = str(uuid.uuid4())
        await conn.execute(
            insert(integration_parent_episodes).values(
                id=episode_id,
                parent_task_id=parent["id"],
                repository_id=checkpoint["repository_id"],
                generation=int(checkpoint["generation"]),
                pre_collection_checkpoint_sha=pre_collection_sha,
                created_at=now,
            )
        )
        operation = {
            "id": operation_id,
            "target_kind": "parent",
            "parent_task_id": parent["id"],
            "episode_id": episode_id,
            "active_stage": 0,
            "state": "active",
            "policy_snapshot": policy.model_dump(mode="json"),
            "artifact_snapshot": route.artifact.model_dump(mode="json"),
            "required_check_version": boundary.required_checks.version,
            "verifier_task_id": None,
            "route_playbook_id": route.playbook_id,
            "route_scope": route.scope,
            "route_scope_identifier": route.scope_identifier,
            "route_activation_id": route.activation_id,
            "created_at": now,
            "updated_at": now,
        }
        await conn.execute(insert(integration_repair_operations).values(**operation))
        await conn.execute(
            insert(integration_operation_artifact_pins).values(
                operation_id=operation_id,
                artifact_sha256=route.artifact.artifact_sha256,
            )
        )
        if carry_forward is not None:
            previous = (
                await conn.execute(
                    select(integration_parent_verifications).where(
                        integration_parent_verifications.c.id
                        == carry_forward["verification_id"],
                        integration_parent_verifications.c.operation_id
                        == carry_forward["operation_id"],
                        integration_parent_verifications.c.parent_task_id == parent["id"],
                        integration_parent_verifications.c.episode_id
                        == carry_forward["episode_id"],
                        integration_parent_verifications.c.head_sha
                        == carry_forward["head_sha"],
                    )
                )
            ).mappings().one_or_none()
            previous_operation = (
                await conn.execute(
                    select(integration_repair_operations.c.id).where(
                        integration_repair_operations.c.id
                        == carry_forward["operation_id"],
                        integration_repair_operations.c.parent_task_id == parent["id"],
                        integration_repair_operations.c.episode_id
                        == carry_forward["episode_id"],
                        integration_repair_operations.c.state == "completed",
                    )
                )
            ).one_or_none()
            previous_completion = (
                await conn.execute(
                    select(integration_parent_operation_completions.c.operation_id).where(
                        integration_parent_operation_completions.c.operation_id
                        == carry_forward["operation_id"],
                        integration_parent_operation_completions.c.verification_id
                        == carry_forward["verification_id"],
                        integration_parent_operation_completions.c.parent_task_id
                        == parent["id"],
                        integration_parent_operation_completions.c.episode_id
                        == carry_forward["episode_id"],
                    )
                )
            ).one_or_none()
            previous_episode = (
                await conn.execute(
                    select(integration_parent_episodes.c.id).where(
                        integration_parent_episodes.c.id == carry_forward["episode_id"],
                        integration_parent_episodes.c.parent_task_id == parent["id"],
                        integration_parent_episodes.c.repository_id
                        == checkpoint["repository_id"],
                    )
                )
            ).one_or_none()
            if (
                previous is None
                or previous_operation is None
                or previous_completion is None
                or previous_episode is None
            ):
                raise HierarchyError(
                    "stale_head", "previous verified aggregate changed before rollover"
                )
            receipt_ids = (
                await conn.execute(
                    select(task_delivery_receipts.c.id).where(
                        task_delivery_receipts.c.target_task_id == parent["id"],
                        task_delivery_receipts.c.repository_id
                        == checkpoint["repository_id"],
                        task_delivery_receipts.c.target_branch == checkpoint["branch"],
                        task_delivery_receipts.c.parent_operation_id
                        == carry_forward["operation_id"],
                        task_delivery_receipts.c.parent_episode_id
                        == carry_forward["episode_id"],
                    )
                )
            ).scalars().all()
            for receipt_id in receipt_ids:
                await conn.execute(
                    insert(integration_episode_receipt_acceptances).values(
                        episode_id=episode_id,
                        receipt_id=receipt_id,
                        operation_id=operation_id,
                        previous_episode_id=carry_forward["episode_id"],
                        previous_operation_id=carry_forward["operation_id"],
                        previous_verification_id=carry_forward["verification_id"],
                        ancestry_from_sha=carry_forward["head_sha"],
                        ancestry_to_sha=pre_collection_sha,
                        created_at=now,
                    )
                )
        return operation

    async def readiness(self, task_id: str) -> dict[str, Any]:
        async with self.db.immediate() as conn:
            parent, project, checkpoint, operation = await self._locked_context_on(
                conn, task_id
            )
            return await self.readiness_on(
                conn,
                parent=parent,
                project=project,
                checkpoint=checkpoint,
                operation=operation,
            )

    async def mark_ready_on(self, conn, task_id: str) -> dict[str, Any]:
        """Project readiness into checkpoint state and one durable event."""
        parent, project, checkpoint, operation = await self._locked_context_on(conn, task_id)
        if parent["status"] != "PAUSED":
            return {"outcome": "waiting", "task_id": task_id}
        readiness = await self.readiness_on(
            conn,
            parent=parent,
            project=project,
            checkpoint=checkpoint,
            operation=operation,
        )
        if readiness["outcome"] != "ready":
            return readiness
        policy = HierarchicalIntegrationPolicy.model_validate(operation["policy_snapshot"])
        attempts = (
            await conn.execute(
                select(task_session_attempts.c.id).where(
                    task_session_attempts.c.task_id == task_id
                ).limit(1)
            )
        ).first()
        if attempts is None and policy.branchless_parent == "verifier":
            route = policy.parent
            if not route.verifier_intelligence_class or not route.verifier_profile_id:
                await enqueue_integration_event(
                    conn,
                    event_id=f"parent-verifier-route-missing-{operation['id']}",
                    dedup_key=f"task.integration_configuration_blocked:{operation['id']}",
                    project_id=parent["project_id"],
                    event_type="task.integration_configuration_blocked",
                    payload={
                        "project_id": parent["project_id"],
                        "operation_id": operation["id"],
                        "task_id": task_id,
                        "title": parent["title"],
                        "reason": "verifier_routing_missing",
                    },
                    available_at=self.clock(),
                )
                return readiness | {
                    "outcome": "configuration_blocked",
                    "reason": "verifier_routing_missing",
                }
            if operation.get("verifier_task_id") is None:
                from src.models import Task, TaskStatus

                verifier_id = f"verify-{operation['id']}"
                existing = (
                    await conn.execute(select(tasks.c.id).where(tasks.c.id == verifier_id))
                ).first()
                if existing is None:
                    await self.db.create_task(
                        Task(
                            id=verifier_id,
                            project_id=parent["project_id"],
                            title=f"Verify aggregate for {task_id}",
                            description=(
                                "Verify the exact collected aggregate and record trusted "
                                "integration check evidence."
                            ),
                            status=TaskStatus.PAUSED,
                            repo_id=checkpoint["repository_id"],
                            branch_name=checkpoint["branch"],
                            profile_id=route.verifier_profile_id,
                            intelligence_class=route.verifier_intelligence_class,
                            dedup_key=f"integration-verifier:{operation['id']}",
                        ),
                        conn=conn,
                    )
                await conn.execute(
                    update(integration_repair_operations)
                    .where(integration_repair_operations.c.id == operation["id"])
                    .where(integration_repair_operations.c.verifier_task_id.is_(None))
                    .values(verifier_task_id=verifier_id, updated_at=self.clock())
                )
                operation = dict(operation) | {"verifier_task_id": verifier_id}
        owner = (
            await conn.execute(
                select(integration_branch_owners).where(
                    integration_branch_owners.c.repository_id
                    == checkpoint["repository_id"],
                    integration_branch_owners.c.ref == checkpoint["branch"],
                )
            )
        ).mappings().one_or_none()
        if owner is None:
            raise HierarchyError(
                "invariant_error", "integration-ready parent has no branch owner"
            )
        next_owner_id = operation.get("verifier_task_id") or task_id
        projected = await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == task_id)
            .where(task_integration_checkpoints.c.episode_id == checkpoint["episode_id"])
            .where(task_integration_checkpoints.c.state != "integration_ready")
            .values(state="integration_ready", updated_at=self.clock())
        )
        if projected.rowcount:
            await enqueue_integration_event(
                conn,
                event_id=f"parent-ready-{operation['id']}-{checkpoint['generation']}",
                dedup_key=(
                    f"task.integration_ready:{operation['id']}:{checkpoint['generation']}"
                ),
                project_id=parent["project_id"],
                event_type="task.integration_ready",
                payload={
                    "project_id": parent["project_id"],
                    "operation_id": operation["id"],
                    "task_id": task_id,
                    "title": parent["title"],
                    "episode_id": checkpoint["episode_id"],
                    "generation": int(checkpoint["generation"]),
                    "head_sha": readiness["head_sha"],
                    "verifier_task_id": operation.get("verifier_task_id"),
                    "target": {
                        "repository_id": checkpoint["repository_id"],
                        "branch": checkpoint["branch"],
                    },
                    "expected_token": int(owner["fence_token"]),
                    "next_owner_id": next_owner_id,
                    "next_role": "verifier",
                },
                available_at=self.clock(),
            )
        return readiness | {"state": "integration_ready"}

    async def readiness_on(
        self,
        conn,
        *,
        parent: dict[str, Any],
        project: dict[str, Any],
        checkpoint: dict[str, Any],
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        episode = (
            await conn.execute(
                select(integration_parent_episodes).where(
                    integration_parent_episodes.c.id == checkpoint["episode_id"]
                )
            )
        ).mappings().one_or_none()
        if episode is None or episode["parent_task_id"] != parent["id"]:
            raise HierarchyError("invariant_error", "parent episode is missing")

        child_rows = (
            await conn.execute(
                select(
                    tasks.c.id,
                    tasks.c.status,
                    tasks.c.pr_url,
                    task_integration_checkpoints.c.checkpoint_sha.label("source_head"),
                )
                .outerjoin(
                    task_integration_checkpoints,
                    task_integration_checkpoints.c.task_id == tasks.c.id,
                )
                .where(tasks.c.parent_task_id == parent["id"])
                .order_by(tasks.c.id)
            )
        ).mappings().all()
        origins = {
            row["task_id"]: dict(row)
            for row in (
                await conn.execute(
                    select(task_branch_origins).where(
                        task_branch_origins.c.task_id.in_([row["id"] for row in child_rows]),
                        task_branch_origins.c.retired_at.is_(None),
                    )
                )
            ).mappings().all()
        } if child_rows else {}
        receipts = [
            dict(row)
            for row in (
                await conn.execute(
                    select(task_delivery_receipts)
                    .where(
                        task_delivery_receipts.c.target_task_id == parent["id"],
                        task_delivery_receipts.c.repository_id == checkpoint["repository_id"],
                        task_delivery_receipts.c.target_branch == checkpoint["branch"],
                    )
                    .order_by(task_delivery_receipts.c.created_at, task_delivery_receipts.c.id)
                )
            ).mappings().all()
        ]
        dispositions = {
            row["child_task_id"]: dict(row)
            for row in (
                await conn.execute(
                    select(integration_child_dispositions).where(
                        integration_child_dispositions.c.parent_task_id == parent["id"]
                    )
                )
            ).mappings().all()
        }

        blockers: list[dict[str, str]] = []
        selected: list[dict[str, Any]] = []
        terminal = {"COMPLETED", "FAILED"}
        by_child: dict[str, list[dict[str, Any]]] = {}
        carried_receipt_ids = set(
            (
                await conn.execute(
                    select(integration_episode_receipt_acceptances.c.receipt_id)
                    .select_from(
                        integration_episode_receipt_acceptances.join(
                            integration_parent_verifications,
                            integration_parent_verifications.c.id
                            == integration_episode_receipt_acceptances.c.previous_verification_id,
                        ).join(
                            integration_repair_operations,
                            integration_repair_operations.c.id
                            == integration_episode_receipt_acceptances.c.previous_operation_id,
                        ).join(
                            integration_parent_operation_completions,
                            integration_parent_operation_completions.c.operation_id
                            == integration_episode_receipt_acceptances.c.previous_operation_id,
                        )
                    )
                    .where(
                        integration_episode_receipt_acceptances.c.episode_id
                        == checkpoint["episode_id"],
                        integration_episode_receipt_acceptances.c.operation_id
                        == operation["id"],
                        integration_episode_receipt_acceptances.c.ancestry_to_sha
                        == episode["pre_collection_checkpoint_sha"],
                        integration_parent_verifications.c.operation_id
                        == integration_episode_receipt_acceptances.c.previous_operation_id,
                        integration_parent_verifications.c.episode_id
                        == integration_episode_receipt_acceptances.c.previous_episode_id,
                        integration_parent_verifications.c.parent_task_id == parent["id"],
                        integration_parent_verifications.c.head_sha
                        == integration_episode_receipt_acceptances.c.ancestry_from_sha,
                        integration_parent_operation_completions.c.verification_id
                        == integration_episode_receipt_acceptances.c.previous_verification_id,
                        integration_parent_operation_completions.c.parent_task_id
                        == parent["id"],
                        integration_parent_operation_completions.c.episode_id
                        == integration_episode_receipt_acceptances.c.previous_episode_id,
                        integration_repair_operations.c.parent_task_id == parent["id"],
                        integration_repair_operations.c.episode_id
                        == integration_episode_receipt_acceptances.c.previous_episode_id,
                        integration_repair_operations.c.state == "completed",
                    )
                )
            ).scalars().all()
        )
        for receipt in receipts:
            directly_bound = (
                receipt["parent_operation_id"] == operation["id"]
                and receipt["parent_episode_id"] == checkpoint["episode_id"]
            )
            if receipt["source_task_id"] and (
                directly_bound or receipt["id"] in carried_receipt_ids
            ):
                by_child.setdefault(receipt["source_task_id"], []).append(receipt)
        for child in child_rows:
            child_id = child["id"]
            origin = origins.get(child_id)
            if (
                origin is None
                or origin["parent_task_id"] != parent["id"]
                or origin["parent_repository_id"] != checkpoint["repository_id"]
                or origin["parent_ref"] != checkpoint["branch"]
            ):
                blockers.append({"task_id": child_id, "reason": "origin_mismatch"})
                continue
            candidates = by_child.get(child_id, [])
            code = [
                row
                for row in candidates
                if row["disposition"] == "code"
                and row["reviewed_head_sha"] == child["source_head"]
            ]
            if len(code) == 1 and child["status"] == "COMPLETED":
                selected.append(code[0])
                continue
            current = dispositions.get(child_id)
            disposed = [
                row
                for row in candidates
                if row["disposition"] in {"noop", "ineligible", "skipped"}
                and current is not None
                and row["disposition"] == current["disposition"]
                and row["disposition_revision"] == current["revision"]
                and row["reviewed_head_sha"] == child["source_head"]
                and row["resolution_evidence"]
                and (row["disposition"] != "noop" or row["verification_evidence"])
            ]
            if len(disposed) == 1 and child["status"] in terminal:
                selected.append(disposed[0])
                continue
            blockers.append(
                {
                    "task_id": child_id,
                    "reason": "failed_child" if child["status"] == "FAILED" else "receipt_missing",
                }
            )

        code_chain = sorted(
            (
                row
                for row in selected
                if row["disposition"] == "code"
                and row["parent_operation_id"] == operation["id"]
                and row["parent_episode_id"] == checkpoint["episode_id"]
            ),
            key=lambda row: (row["created_at"], row["id"]),
        )
        head_sha = episode["pre_collection_checkpoint_sha"]
        for row in code_chain:
            if row["before_sha"] != head_sha or not self._trusted_code_receipt(row):
                blockers.append({"task_id": row["source_task_id"], "reason": "receipt_chain"})
                break
            head_sha = row["after_sha"]
        outcome = "ready" if not blockers else (
            "failed" if any(row["reason"] == "failed_child" for row in blockers) else "waiting"
        )
        policy = HierarchicalIntegrationPolicy.model_validate(operation["policy_snapshot"])
        return {
            "outcome": outcome,
            "task_id": parent["id"],
            "episode_id": checkpoint["episode_id"],
            "operation_id": operation["id"],
            "generation": int(checkpoint["generation"]),
            "checkpoint_sha": episode["pre_collection_checkpoint_sha"],
            "head_sha": head_sha,
            "receipts": sorted(selected, key=lambda row: row["source_task_id"]),
            "blockers": blockers,
            "required_checks": policy.parent.required_checks.model_dump(mode="json"),
            "on_failed_child": policy.on_failed_child,
        }

    @staticmethod
    def _trusted_code_receipt(receipt: dict[str, Any]) -> bool:
        """Accept clean squash edges or the exact conflict-resolution proof shape."""
        if receipt["squash_sha"] is not None:
            return bool(
                receipt["after_sha"] == receipt["squash_sha"]
                and receipt["resolution_evidence"] is None
            )
        evidence = receipt["resolution_evidence"]
        if not isinstance(evidence, dict) or evidence.get("kind") != "conflict_resolution":
            return False
        review_snapshot = receipt["review_evidence"]
        review = review_snapshot.get("review") if isinstance(review_snapshot, dict) else None
        authoring = evidence.get("authoring")
        fence = authoring.get("fence") if isinstance(authoring, dict) else None
        proof = evidence.get("remote_proof")
        commits = evidence.get("repair_commit_shas")
        if (
            not isinstance(review, dict)
            or not isinstance(authoring, dict)
            or not isinstance(fence, dict)
            or not isinstance(proof, dict)
            or not isinstance(commits, list)
            or not commits
            or len(set(commits)) != len(commits)
            or any(not isinstance(oid, str) or not _OID.fullmatch(oid) for oid in commits)
        ):
            return False
        required_strings = (
            authoring.get("repair_task_id"),
            authoring.get("repair_session_id"),
            authoring.get("repair_session_instance_token"),
            authoring.get("repair_workspace_id"),
        )
        return bool(
            evidence.get("original_source_base") == review.get("source_base")
            and evidence.get("original_source_head") == receipt["reviewed_head_sha"]
            and evidence.get("original_source_tree") == receipt["reviewed_tree_sha"]
            and evidence.get("original_expected_target") == receipt["before_sha"]
            and evidence.get("resolved_head_sha") == receipt["after_sha"]
            and isinstance(evidence.get("resolved_tree_sha"), str)
            and _OID.fullmatch(evidence["resolved_tree_sha"])
            and commits[-1] == receipt["after_sha"]
            and authoring.get("operation_id") == receipt["parent_operation_id"]
            and isinstance(authoring.get("stage_ordinal"), int)
            and not isinstance(authoring.get("stage_ordinal"), bool)
            and authoring["stage_ordinal"] >= 0
            and all(isinstance(value, str) and value for value in required_strings)
            and fence.get("repository_id") == receipt["repository_id"]
            and fence.get("branch") == receipt["target_branch"]
            and fence.get("owner_id") == authoring.get("repair_task_id")
            and isinstance(fence.get("token"), int)
            and not isinstance(fence.get("token"), bool)
            and fence["token"] >= 0
            and proof
            == {
                "kind": "exact_resolution_tip",
                "remote_sha": receipt["after_sha"],
                "resolved_tree_sha": evidence["resolved_tree_sha"],
                "repair_commit_shas": commits,
            }
        )

    async def record_disposition(
        self,
        child_task_id: str,
        *,
        disposition: str,
        reviewed_head_sha: str,
        reviewed_tree_sha: str,
        verification_evidence: dict[str, Any],
        resolution_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        if disposition not in {"noop", "ineligible", "skipped"}:
            raise HierarchyError("invalid", "unsupported delivery disposition")
        if not resolution_evidence or (disposition == "noop" and not verification_evidence):
            raise HierarchyError("invalid", "disposition evidence is required")
        async with self.db.immediate() as conn:
            child = (
                await conn.execute(select(tasks).where(tasks.c.id == child_task_id))
            ).mappings().one_or_none()
            if child is None or not child["parent_task_id"]:
                raise HierarchyError("invalid", "disposition child has no parent")
            parent, project, checkpoint, operation = await self._locked_context_on(
                conn, child["parent_task_id"]
            )
            child = (
                await conn.execute(select(tasks).where(tasks.c.id == child_task_id))
            ).mappings().one()
            child_checkpoint = (
                await conn.execute(
                    select(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id == child_task_id
                    )
                )
            ).mappings().one_or_none()
            if (
                child["status"] not in {"COMPLETED", "FAILED"}
                or child_checkpoint is None
                or child_checkpoint["checkpoint_sha"] != reviewed_head_sha
            ):
                raise HierarchyError("invalid", "disposition source is not terminal at that head")
            code_receipt = (
                await conn.execute(
                    select(task_delivery_receipts.c.id).where(
                        task_delivery_receipts.c.source_task_id == child_task_id,
                        task_delivery_receipts.c.disposition == "code",
                    ).limit(1)
                )
            ).first()
            if code_receipt:
                raise HierarchyError("delivery_target_fixed", "delivered code cannot be disposed")
            current = (
                await conn.execute(
                    select(integration_child_dispositions)
                    .where(
                        integration_child_dispositions.c.parent_task_id == parent["id"],
                        integration_child_dispositions.c.child_task_id == child_task_id,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            revision = 0 if current is None else int(current["revision"])
            if current is not None and current["disposition"] != disposition:
                revision += 1
                await conn.execute(
                    update(task_integration_checkpoints)
                    .where(task_integration_checkpoints.c.task_id == parent["id"])
                    .values(
                        generation=task_integration_checkpoints.c.generation + 1,
                        verified_sha=None,
                        verified_generation=None,
                        current_verification_id=None,
                        version=task_integration_checkpoints.c.version + 1,
                        updated_at=self.clock(),
                    )
                )
            if current is None:
                await conn.execute(
                    insert(integration_child_dispositions).values(
                        parent_task_id=parent["id"],
                        child_task_id=child_task_id,
                        revision=revision,
                        disposition=disposition,
                        parent_operation_id=operation["id"],
                        parent_episode_id=checkpoint["episode_id"],
                        updated_at=self.clock(),
                    )
                )
            elif (
                current["disposition"] != disposition
                or current["parent_operation_id"] != operation["id"]
                or current["parent_episode_id"] != checkpoint["episode_id"]
            ):
                await conn.execute(
                    update(integration_child_dispositions)
                    .where(
                        integration_child_dispositions.c.parent_task_id == parent["id"],
                        integration_child_dispositions.c.child_task_id == child_task_id,
                    )
                    .values(
                        revision=revision,
                        disposition=disposition,
                        parent_operation_id=operation["id"],
                        parent_episode_id=checkpoint["episode_id"],
                        updated_at=self.clock(),
                    )
                )
            domain_key = (
                f"disposition:{parent['id']}:{child_task_id}:"
                f"{operation['id']}:{revision}"
            )
            existing = (
                await conn.execute(
                    select(task_delivery_receipts).where(
                        task_delivery_receipts.c.domain_key == domain_key
                    )
                )
            ).mappings().one_or_none()
            if existing is not None:
                if existing["disposition"] != disposition:
                    raise HierarchyError("invariant_error", "disposition identity changed")
                return dict(existing) | {"revision": revision}
            origin = (
                await conn.execute(
                    select(task_branch_origins).where(
                        task_branch_origins.c.task_id == child_task_id,
                        task_branch_origins.c.repository_id == checkpoint["repository_id"],
                        task_branch_origins.c.retired_at.is_(None),
                    )
                )
            ).mappings().one()
            receipt = {
                "id": str(uuid.uuid4()),
                "domain_key": domain_key,
                "source_task_id": child_task_id,
                "target_task_id": parent["id"],
                "repository_id": checkpoint["repository_id"],
                "target_branch": checkpoint["branch"],
                "reviewed_head_sha": reviewed_head_sha,
                "reviewed_tree_sha": reviewed_tree_sha,
                "before_sha": None,
                "squash_sha": None,
                "after_sha": None,
                "verification_evidence": verification_evidence,
                "resolution_evidence": {
                    **resolution_evidence,
                    "origin_id": origin["id"],
                    "origin_base_sha": origin["base_sha"],
                    "episode_id": checkpoint["episode_id"],
                },
                "disposition": disposition,
                "disposition_revision": revision,
                "parent_operation_id": operation["id"],
                "parent_episode_id": checkpoint["episode_id"],
                "created_at": self.clock(),
            }
            await conn.execute(insert(task_delivery_receipts).values(**receipt))
            await self.mark_ready_on(conn, parent["id"])
            return receipt | {"revision": revision}

    async def verify_parent(
        self, task_id: str, generation: int, head_sha: str, evidence_ids: list[str]
    ) -> dict[str, Any]:
        if not evidence_ids or len(set(evidence_ids)) != len(evidence_ids):
            return {"outcome": "invalid_evidence", "task_id": task_id}
        async with self.db.immediate() as conn:
            parent, project, checkpoint, operation = await self._locked_context_on(
                conn, task_id
            )
            if int(checkpoint["generation"]) != generation:
                return {"outcome": "stale_generation", "task_id": task_id}
            readiness = await self.readiness_on(
                conn,
                parent=parent,
                project=project,
                checkpoint=checkpoint,
                operation=operation,
            )
            if readiness["outcome"] != "ready":
                return readiness
            if readiness["head_sha"] != head_sha:
                return {"outcome": "stale_head", "task_id": task_id}
            evidence = [
                dict(row)
                for row in (
                    await conn.execute(
                        select(integration_check_evidence).where(
                            integration_check_evidence.c.id.in_(evidence_ids)
                        )
                    )
                ).mappings().all()
            ]
            policy = HierarchicalIntegrationPolicy.model_validate(operation["policy_snapshot"])
            required = policy.parent.required_checks
            valid = len(evidence) == len(evidence_ids)
            covered: set[str] = set()
            for row in evidence:
                valid = valid and all(
                    (
                        row["operation_id"] == operation["id"],
                        row["parent_task_id"] == task_id,
                        row["parent_generation"] == generation,
                        row["parent_head_sha"] == head_sha,
                        row["producer_id"] == required.producer_id,
                        row["required_check_version"] == required.version,
                        row["conclusion"] == "success",
                        row["classification"] != "infrastructure",
                    )
                )
                covered.update(
                    name for name, result in (row["checks"] or {}).items() if result == "success"
                )
            valid = valid and covered == set(required.names)
            if not valid:
                return {"outcome": "invalid_evidence", "task_id": task_id}
            existing = (
                await conn.execute(
                    select(integration_parent_verifications).where(
                        integration_parent_verifications.c.operation_id == operation["id"],
                        integration_parent_verifications.c.generation == generation,
                        integration_parent_verifications.c.head_sha == head_sha,
                    )
                )
            ).mappings().one_or_none()
            verification_id = existing["id"] if existing else str(uuid.uuid4())
            if existing is None:
                await conn.execute(
                    insert(integration_parent_verifications).values(
                        id=verification_id,
                        operation_id=operation["id"],
                        parent_task_id=task_id,
                        episode_id=checkpoint["episode_id"],
                        generation=generation,
                        head_sha=head_sha,
                        required_check_version=required.version,
                        created_at=self.clock(),
                    )
                )
                for evidence_id in sorted(evidence_ids):
                    await conn.execute(
                        insert(integration_parent_verification_evidence).values(
                            verification_id=verification_id,
                            evidence_id=evidence_id,
                        )
                    )
            else:
                linked = set(
                    (
                        await conn.execute(
                            select(integration_parent_verification_evidence.c.evidence_id).where(
                                integration_parent_verification_evidence.c.verification_id
                                == verification_id
                            )
                        )
                    ).scalars().all()
                )
                if linked != set(evidence_ids):
                    return {"outcome": "invalid_evidence", "task_id": task_id}
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_id)
                .values(
                    checkpoint_sha=head_sha,
                    verified_sha=head_sha,
                    verified_generation=generation,
                    current_verification_id=verification_id,
                    state="verifying",
                    version=task_integration_checkpoints.c.version + 1,
                    updated_at=self.clock(),
                )
            )
            await enqueue_integration_event(
                conn,
                event_id=f"parent-verified-{verification_id}",
                dedup_key=f"task.integration_verified:{verification_id}",
                project_id=parent["project_id"],
                event_type="task.integration_verified",
                payload={
                    "project_id": parent["project_id"],
                    "operation_id": operation["id"],
                    "task_id": task_id,
                    "title": parent["title"],
                    "generation": generation,
                    "head_sha": head_sha,
                    "verification_id": verification_id,
                },
                available_at=self.clock(),
            )
            return {
                "outcome": "verified",
                "task_id": task_id,
                "generation": generation,
                "head_sha": head_sha,
                "verification_id": verification_id,
            }

    async def wake_verifier(self, task_id: str, fence) -> dict[str, Any]:
        """Wake only the exact transferred verifier on the collected head."""
        from src.database.queries.task_queries import _INTEGRATION_WAKE_TOKEN
        from src.models import TaskStatus

        async with self.db.immediate() as conn:
            parent, project, checkpoint, operation = await self._locked_context_on(
                conn, task_id
            )
            readiness = await self.readiness_on(
                conn,
                parent=parent,
                project=project,
                checkpoint=checkpoint,
                operation=operation,
            )
            if readiness["outcome"] != "ready":
                return readiness
            expected_owner = operation["verifier_task_id"] or task_id
            owner = (
                await conn.execute(
                    select(integration_branch_owners).where(
                        integration_branch_owners.c.repository_id
                        == checkpoint["repository_id"],
                        integration_branch_owners.c.ref == checkpoint["branch"],
                    )
                )
            ).mappings().one_or_none()
            if (
                fence.owner_id != expected_owner
                or fence.target.repository_id != checkpoint["repository_id"]
                or fence.target.branch != checkpoint["branch"]
                or owner is None
                or owner["owner_id"] != expected_owner
                or owner["owner_role"] != "verifier"
                or int(owner["fence_token"]) != fence.token
                or owner["handoff_state"] != "reserved"
            ):
                raise HierarchyError("invariant_error", "verifier handoff is not current")
            wake_task_id = expected_owner if operation["verifier_task_id"] else task_id
            if (
                await self.db._read_manual_pause(conn, task_id) is not None
                or await self.db._read_manual_pause(conn, wake_task_id) is not None
            ):
                raise HierarchyError("human_required", "operator manual pause is active")
            transition = await self.db._apply_transition(
                conn,
                wake_task_id,
                TaskStatus.READY,
                context="integration_verifier_handoff",
                assigned_agent_id=None,
                _manual_pause_control=True,
                _integration_wake_token=_INTEGRATION_WAKE_TOKEN,
            )
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_id)
                .where(task_integration_checkpoints.c.episode_id == checkpoint["episode_id"])
                .values(
                    checkpoint_sha=readiness["head_sha"],
                    branch_owner_id=expected_owner,
                    state="verifying",
                    version=task_integration_checkpoints.c.version + 1,
                    updated_at=self.clock(),
                )
            )
        await self.db.log_blocked_flips(transition.flipped)
        await self.db._notify_ready(transition.ready)
        return readiness | {"outcome": "woken", "owner_id": expected_owner}

    async def complete_parent(
        self, task_id: str, generation: int, head_sha: str
    ) -> dict[str, Any]:
        from src.database.queries.task_queries import _INTEGRATION_COMPLETION_TOKEN
        from src.models import TaskStatus

        async with self.db.immediate() as conn:
            parent, project, checkpoint, operation = await self._locked_context_on(
                conn, task_id
            )
            if int(checkpoint["generation"]) != generation:
                return {"outcome": "stale_verification", "task_id": task_id}
            readiness = await self.readiness_on(
                conn,
                parent=parent,
                project=project,
                checkpoint=checkpoint,
                operation=operation,
            )
            if readiness["outcome"] != "ready":
                return readiness
            if (
                checkpoint["verified_generation"] != generation
                or checkpoint["verified_sha"] != head_sha
                or checkpoint["current_verification_id"] is None
                or readiness["head_sha"] != head_sha
            ):
                return {"outcome": "stale_verification", "task_id": task_id}
            verification = (
                await conn.execute(
                    select(integration_parent_verifications).where(
                        integration_parent_verifications.c.id
                        == checkpoint["current_verification_id"],
                        integration_parent_verifications.c.operation_id == operation["id"],
                        integration_parent_verifications.c.generation == generation,
                        integration_parent_verifications.c.head_sha == head_sha,
                    )
                )
            ).first()
            if verification is None:
                return {"outcome": "stale_verification", "task_id": task_id}
            expected_owner = operation["verifier_task_id"] or task_id
            owner = (
                await conn.execute(
                    select(integration_branch_owners).where(
                        integration_branch_owners.c.repository_id
                        == checkpoint["repository_id"],
                        integration_branch_owners.c.ref == checkpoint["branch"],
                    )
                )
            ).mappings().one_or_none()
            if (
                operation["state"] not in {"active", "escalated"}
                or owner is None
                or owner["owner_id"] != expected_owner
                or owner["owner_role"] != "verifier"
                or owner["handoff_state"] not in {"reserved", "attached"}
                or checkpoint["branch_owner_id"] != expected_owner
            ):
                return {"outcome": "invariant_error", "task_id": task_id}
            transition = await self.db._apply_transition(
                conn,
                task_id,
                TaskStatus.COMPLETED,
                context="integration_parent_verified",
                force=True,
                _integration_completion_token=_INTEGRATION_COMPLETION_TOKEN,
            )
            completed_at = self.clock()
            await conn.execute(
                insert(integration_parent_operation_completions).values(
                    operation_id=operation["id"],
                    verification_id=checkpoint["current_verification_id"],
                    parent_task_id=task_id,
                    episode_id=checkpoint["episode_id"],
                    completed_at=completed_at,
                )
            )
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_id)
                .values(
                    last_completed_operation_id=operation["id"],
                    last_completed_verification_id=checkpoint["current_verification_id"],
                )
            )
            await conn.execute(
                update(integration_repair_operations)
                .where(
                    integration_repair_operations.c.id == operation["id"],
                    integration_repair_operations.c.state.in_(("active", "escalated")),
                )
                .values(state="completed", updated_at=completed_at)
            )
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation["id"],
                    integration_repair_stages.c.ordinal == operation["active_stage"],
                    integration_repair_stages.c.state.in_(
                        ("active", "awaiting_completion")
                    ),
                )
                .values(state="passed", completed_at=completed_at)
            )
        await self.db.log_blocked_flips(transition.flipped)
        await self.db._notify_settled(transition.settled)
        await self.db._notify_ready(transition.ready)
        return {
            "outcome": "completed",
            "task_id": task_id,
            "generation": generation,
            "head_sha": head_sha,
            "operation_id": operation["id"],
        }

    async def _locked_context_on(self, conn, task_id: str):
        parent = (
            await conn.execute(select(tasks).where(tasks.c.id == task_id))
        ).mappings().one_or_none()
        if parent is None:
            raise HierarchyError("invariant_error", "parent task does not exist")
        project = (
            await conn.execute(select(projects).where(projects.c.id == parent["project_id"]))
        ).mappings().one()
        if project["hierarchical_integration_mode"] not in {"hierarchy", "train"}:
            raise HierarchyError("invariant_error", "hierarchical integration is disabled")
        await self.db.lock_hierarchy_project(conn, project["id"])
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        if checkpoint is None or checkpoint["episode_id"] is None:
            raise HierarchyError("invariant_error", "parent has no active integration episode")
        operation = (
            await conn.execute(
                select(integration_repair_operations)
                .where(
                    integration_repair_operations.c.parent_task_id == task_id,
                    integration_repair_operations.c.episode_id == checkpoint["episode_id"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if operation is None:
            raise HierarchyError("invariant_error", "parent episode operation is missing")
        return dict(parent), dict(project), dict(checkpoint), dict(operation)
