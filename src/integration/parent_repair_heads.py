"""Audited repair edges extending immutable parent collection receipts."""

from __future__ import annotations

import json
import shlex

from sqlalchemy import select, update

from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
from src.database.tables import (
    archived_tasks,
    gates,
    integration_branch_owners,
    integration_outbox,
    integration_promotion_intents,
    integration_repair_stages,
    sessions,
    task_completion_records,
    task_delivery_receipts,
    task_gates,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)
from src.git.manager import RemoteRefState, is_valid_git_oid
from src.integration.parent_completion import ParentCompletion
from src.integration.parent_engine import parent_engine_guard

EXTENSIONS = "parent_head_extensions"
COLLECTION_RECOVERY = "collection_head_recovery"


def extension(operation, checkpoint, stage, proof, authoring):
    """Snapshot only a proved, publication-backed change of subject."""
    return {
        "operation_id": operation["id"],
        "episode_id": operation["episode_id"],
        "generation": checkpoint["generation"],
        "repository_id": checkpoint["repository_id"],
        "branch": checkpoint["branch"],
        "stage": stage["ordinal"],
        "before_sha": proof["base_sha"],
        "after_sha": proof["head_sha"],
        "commits": list(proof["commits"]),
        "authoring": authoring,
    }


async def advance_checkpoint_on(conn, checkpoint, head_sha, now):
    await conn.execute(
        update(task_integration_checkpoints)
        .where(task_integration_checkpoints.c.task_id == checkpoint["task_id"])
        .values(
            checkpoint_sha=head_sha,
            verified_sha=None,
            verified_generation=None,
            current_verification_id=None,
            version=task_integration_checkpoints.c.version + 1,
            updated_at=now,
        )
    )


async def extensions_on(conn, operation, checkpoint):
    """Read durable edges; reject cross-episode or malformed proof instead of trusting a tip."""
    rows = (
        (
            await conn.execute(
                select(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation["id"],
                )
                .order_by(integration_repair_stages.c.ordinal)
            )
        )
        .mappings()
        .all()
    )
    edges = []
    for stage in rows:
        for edge in (stage["dossier"] or {}).get(EXTENSIONS, []):
            # Successor dossiers carry prior diagnostics. Read an edge only
            # from the stage that originally recorded it.
            if edge.get("stage") != stage["ordinal"]:
                continue
            commits = edge.get("commits")
            author = edge.get("authoring") or {}
            if (
                edge.get("operation_id") != operation["id"]
                or edge.get("episode_id") != checkpoint["episode_id"]
                or not isinstance(edge.get("generation"), int)
                or not 0 <= edge["generation"] <= checkpoint["generation"]
                or edge.get("repository_id") != checkpoint["repository_id"]
                or edge.get("branch") != checkpoint["branch"]
                or not is_valid_git_oid(edge.get("before_sha", ""))
                or not is_valid_git_oid(edge.get("after_sha", ""))
                or not isinstance(commits, list)
                or not commits
                or len(set(commits)) != len(commits)
                or any(not is_valid_git_oid(sha) for sha in commits)
                or commits[-1] != edge["after_sha"]
                or edge["before_sha"] == edge["after_sha"]
                or not author.get("task_id")
                or author["task_id"] != stage["repair_task_id"]
                or not author.get("fence_token")
            ):
                raise ValueError("parent repair extension identity or commit proof is invalid")
            # Later stages inherit dossiers for diagnostics. An edge is owned
            # by its original stage and must never be counted twice.
            if edge not in edges:
                edges.append(edge)
    return edges


class ParentHeadRecovery:
    """Operator reconciliation of a closed repair; never authorizes another repair."""

    def __init__(self, promotion):
        self.promotion = promotion
        self.db = promotion.db
        self.parent = ParentCompletion(self.db)

    async def run(self, request, *, principal):
        return await self._run(request.operation_id, request, principal=principal)

    @parent_engine_guard("operation", outcome="blocked")
    async def _run(self, operation_id, request, *, principal):
        async with self.db.immediate() as conn:
            proof = await self._proof_on(conn, request)
        repository = await self.promotion._resolve_repository(proof["checkpoint"]["repository_id"])
        await self.promotion._ensure_retained_repository(repository)
        async with self.promotion.git.arepository_transaction(str(repository.retained_git_dir)):
            await self.promotion._fetch_all_heads(
                repository.retained_git_dir, repository.origin_url
            )
            git_proof = await self._git_proof(proof, repository, request.head_sha)
            gap_git_proofs = [
                await self._git_proof(
                    gap,
                    repository,
                    gap["stage"]["current_subject"]["head_sha"],
                    published_head_sha=request.head_sha,
                )
                for gap in proof["gap_proofs"]
            ]
        result = {
            "operation_id": request.operation_id,
            "head_sha": request.head_sha,
            "episode_id": proof["checkpoint"]["episode_id"],
            "generation": proof["checkpoint"]["generation"],
            "stage": proof["stage"]["ordinal"],
            "fence_token": proof["owner"]["fence_token"],
            "receipt_head_sha": proof["base_sha"],
            "repair_task_id": proof["delegate"]["id"],
            "completion_id": proof["completion"]["id"],
            "attempts": proof["stage"]["attempts"],
            "deadline_at": proof["stage"]["deadline_at"],
            "stage_state": proof["stage"]["state"],
            "operation_state": proof["operation"]["state"],
        }
        if proof["collection_recovery"]:
            result["reason"] = "reconcile completed resolution; receipt gaps: " + ", ".join(
                f"stage {gap['stage']['ordinal']} {edge['base_sha']} -> {edge['head_sha']}"
                for gap, edge in zip(proof["gap_proofs"], gap_git_proofs, strict=True)
            )
        if request.dry_run:
            command = [
                "aq",
                "integration",
                "recover-parent-head",
                request.operation_id,
                "--head",
                request.head_sha,
                "--apply",
                "--episode",
                result["episode_id"],
                "--generation",
                str(result["generation"]),
                "--stage",
                str(result["stage"]),
                "--fence",
                str(result["fence_token"]),
                "--reason",
                "Reconcile the published authorized parent repair",
            ]
            return result | {"outcome": "would_recover", "apply_command": shlex.join(command)}
        expected = (
            request.expected_episode_id,
            request.expected_generation,
            request.expected_stage,
            request.expected_fence_token,
        )
        actual = (
            result["episode_id"],
            result["generation"],
            result["stage"],
            result["fence_token"],
        )
        if expected != actual:
            return result | {
                "outcome": "changed",
                "reason": "previewed episode, generation, stage or fence changed",
            }
        async with self.db.immediate() as conn:
            current = await self._proof_on(conn, request)
            if current != proof:
                return result | {"outcome": "changed", "reason": "recovery proof changed"}
            async with self.promotion.git.arepository_transaction(str(repository.retained_git_dir)):
                if await self._git_proof(current, repository, request.head_sha) != git_proof:
                    raise ValueError("published repair proof changed")
                for gap, expected_gap in zip(current["gap_proofs"], gap_git_proofs, strict=True):
                    if (
                        await self._git_proof(
                            gap,
                            repository,
                            gap["stage"]["current_subject"]["head_sha"],
                            published_head_sha=request.head_sha,
                        )
                        != expected_gap
                    ):
                        raise ValueError("historical repair proof changed")
            if current["existing"] or current["recovered_collection"]:
                return result | {"outcome": "already_recovered"}
            if current["collection_recovery"]:
                return await self._recover_collection_on(
                    conn,
                    current,
                    gap_git_proofs,
                    request=request,
                    principal=principal,
                    result=result,
                )
            stage = current["stage"]
            dossier = dict(stage["dossier"] or {})
            edge = extension(
                current["operation"],
                current["checkpoint"],
                stage,
                git_proof,
                current["authoring"]
                | {
                    "completion_id": current["completion"]["id"],
                    "recovery_fence_token": current["owner"]["fence_token"],
                    "principal": principal,
                    "reason": request.reason,
                },
            )
            dossier[EXTENSIONS] = [*(dossier.get(EXTENSIONS) or []), edge]
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == request.operation_id,
                    integration_repair_stages.c.ordinal == stage["ordinal"],
                )
                .values(dossier=dossier)
            )
            await advance_checkpoint_on(
                conn, current["checkpoint"], request.head_sha, self.parent.clock()
            )
            if current["operation"]["verifier_task_id"] is None:
                # A legacy close can strand the aggregate before its first
                # readiness event. Use the ordinary projection to file the
                # verifier and queue its fenced handoff in this transaction.
                ready = await self.parent.mark_ready_on(
                    conn, current["operation"]["parent_task_id"]
                )
                if ready.get("state") != "integration_ready":
                    raise ValueError(
                        "recovered parent cannot project verifier readiness: " + str(ready)
                    )
        return result | {"outcome": "recovered"}

    async def _recover_collection_on(self, conn, proof, git_proofs, *, request, principal, result):
        now = self.parent.clock()
        for gap, git_proof in zip(proof["gap_proofs"], git_proofs, strict=True):
            stage = gap["stage"]
            dossier = dict(stage["dossier"] or {})
            edge = extension(
                proof["operation"],
                proof["checkpoint"],
                stage,
                git_proof,
                gap["authoring"]
                | {
                    "completion_id": gap["completion"]["id"],
                    "principal": principal,
                    "reason": request.reason,
                    "recovery_fence_token": proof["owner"]["fence_token"],
                },
            )
            dossier[EXTENSIONS] = [*(dossier.get(EXTENSIONS) or []), edge]
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == request.operation_id,
                    integration_repair_stages.c.ordinal == stage["ordinal"],
                )
                .values(dossier=dossier)
            )
        stage = proof["stage"]
        dossier = dict(stage["dossier"] or {})
        dossier[COLLECTION_RECOVERY] = {
            "head_sha": request.head_sha,
            "episode_id": proof["checkpoint"]["episode_id"],
            "generation": proof["checkpoint"]["generation"],
            "receipt_id": proof["resolution"]["receipt"]["id"],
        }
        dossier["resolution_verification"] = {
            "intent_id": proof["resolution"]["intent"]["id"],
            "resolution_head_sha": request.head_sha,
            "stage": stage["ordinal"],
            "recorded_at": now,
        }
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == request.operation_id,
                integration_repair_stages.c.ordinal == stage["ordinal"],
            )
            .values(state="passed", completed_at=stage["completed_at"] or now, dossier=dossier)
        )
        await advance_checkpoint_on(conn, proof["checkpoint"], request.head_sha, now)
        ready = await self.parent.mark_ready_on(conn, proof["operation"]["parent_task_id"])
        if ready.get("state") != "integration_ready" or ready.get("head_sha") != request.head_sha:
            raise ValueError(
                "recovered collection cannot project verifier readiness: " + str(ready)
            )
        return result | {"outcome": "recovered"}

    async def _proof_on(self, conn, request, *, stage_ordinal=None, gap_base=None):
        from src.database.tables import integration_repair_operations

        hint = (
            (
                await conn.execute(
                    select(integration_repair_operations).where(
                        integration_repair_operations.c.id == request.operation_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if hint is None or hint["target_kind"] != "parent":
            raise ValueError("operation is not a parent repair")
        parent, project, checkpoint, operation = await self.parent._locked_context_on(
            conn, hint["parent_task_id"]
        )
        if operation["id"] != request.operation_id or operation["state"] not in {
            "active",
            "escalated",
        }:
            raise ValueError("operation identity changed or a human decision is required")
        if project["status"] != "ACTIVE" or project["hierarchical_integration_draining"]:
            raise ValueError("project is paused or draining")
        if parent["status"] != "PAUSED" or parent["assigned_agent_id"] is not None:
            raise ValueError("parent must be an unassigned paused aggregate")
        stage = (
            (
                await conn.execute(
                    select(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation["id"],
                        integration_repair_stages.c.ordinal
                        == (operation["active_stage"] if stage_ordinal is None else stage_ordinal),
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        subject = {
            "kind": "parent",
            "generation": (
                checkpoint["generation"]
                if stage_ordinal is None
                else (stage["current_subject"] or {}).get("generation")
                if stage
                else None
            ),
            "head_sha": request.head_sha,
        }
        if (
            stage is None
            or stage["writer_kind"] != "repair_delegate"
            or stage["current_subject"] != subject
            or type(subject["generation"]) is not int
            or not 0 <= subject["generation"] <= checkpoint["generation"]
        ):
            raise ValueError(
                "current stage is not bound to this exact repaired head and generation"
            )
        stage = dict(stage)
        delegate = None
        for table in (tasks, archived_tasks):
            delegate = (
                (
                    await conn.execute(
                        select(table)
                        .where(
                            table.c.id == stage["repair_task_id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if delegate is not None:
                break
        if (
            delegate is None
            or delegate["status"] != "COMPLETED"
            or delegate["created_by_kind"] != "integration_repair"
            or delegate["created_by_id"] != operation["id"]
            or delegate["project_id"] != parent["project_id"]
            or delegate["repo_id"] != checkpoint["repository_id"]
            or delegate["branch_name"] != checkpoint["branch"]
            or delegate["assigned_agent_id"] is not None
        ):
            raise ValueError("repair delegate has no matching completed authoring identity")
        completion = (
            (
                await conn.execute(
                    select(task_completion_records)
                    .where(
                        task_completion_records.c.task_id == delegate["id"],
                    )
                    .order_by(
                        task_completion_records.c.completed_at.desc(),
                        task_completion_records.c.id.desc(),
                    )
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        try:
            commits = json.loads(completion["commits"]) if completion is not None else None
        except (TypeError, ValueError):
            commits = None
        if (
            completion is None
            or completion["outcome"] != "pass"
            or completion["branch"] != checkpoint["branch"]
            or commits not in ([request.head_sha], [])
            or (stage["dossier"] or {}).get("branch_sha") != request.head_sha
        ):
            raise ValueError("latest repair completion does not prove this exact head")
        close_events = (
            (
                await conn.execute(
                    select(integration_outbox.c.payload).where(
                        integration_outbox.c.project_id == parent["project_id"],
                        integration_outbox.c.event_type == "integration.repair_delegate_closed",
                        integration_outbox.c.payload["operation_id"].as_string() == operation["id"],
                        integration_outbox.c.payload["task_id"].as_string() == delegate["id"],
                        integration_outbox.c.payload["stage"].as_integer() == stage["ordinal"],
                    )
                )
            )
            .scalars()
            .all()
        )
        resolution = await self._resolution_receipt_on(
            conn, operation, checkpoint, stage, project_id=parent["project_id"]
        )
        if (
            resolution is not None
            and completion["completed_at"] < resolution["intent"]["committed_at"]
        ):
            raise ValueError("repair completion predates its resolution push")
        if len(close_events) == 1 and all(
            close_events[0].get(key)
            for key in ("session_id", "instance_token", "workspace_id", "fence_token")
        ):
            authoring = {
                key: close_events[0][key]
                for key in (
                    "task_id",
                    "session_id",
                    "instance_token",
                    "workspace_id",
                    "fence_token",
                )
            }
            if resolution is not None and authoring != resolution["authoring"]:
                raise ValueError("delegate-close audit contradicts resolution authoring")
        elif not close_events and resolution is not None:
            authoring = dict(resolution["authoring"])
            authoring["resolution_receipt_id"] = resolution["receipt"]["id"]
            authoring["resolution_intent_id"] = resolution["intent"]["id"]
        else:
            raise ValueError(
                "repair lacks its exact fenced delegate-close audit or resolution receipt"
            )
        # Older closes proved the attached repair head, then lost it while
        # building the completion from a base checkout without the parent ref.
        # An empty record is usable only when this exact completion belongs to
        # the audited close. The stage lineage and remote Git proof below still
        # establish the head; summary text and an unrelated passing close do not.
        accepted_close = None
        if commits == []:
            accepted_value = await conn.scalar(
                select(task_metadata.c.value)
                .where(
                    task_metadata.c.task_id == delegate["id"],
                    task_metadata.c.key == ACCEPTED_CLOSE_KEY,
                )
                .with_for_update()
            )
            try:
                accepted_close = json.loads(accepted_value) if accepted_value else None
            except (TypeError, ValueError):
                accepted_close = None
            if (
                not isinstance(accepted_close, dict)
                or accepted_close.get("completion_id") != completion["id"]
                or accepted_close.get("session_id") != authoring["session_id"]
                or type(accepted_close.get("claim_epoch")) is not int
                or accepted_close["claim_epoch"] != delegate.get("claim_epoch")
            ):
                raise ValueError("empty repair completion lacks its exact accepted-close identity")
            authoring["accepted_close"] = accepted_close
        owner = (
            (
                await conn.execute(
                    select(integration_branch_owners)
                    .where(
                        integration_branch_owners.c.repository_id == checkpoint["repository_id"],
                        integration_branch_owners.c.ref == checkpoint["branch"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            owner is None
            or owner["handoff_state"] != "reserved"
            or owner["session_id"] is not None
            or owner["workspace_id"] is not None
            or owner["owner_id"]
            not in {operation["id"], operation["verifier_task_id"], parent["id"]}
            or owner["owner_role"] not in {"collector", "verifier"}
            or owner["fence_token"] <= authoring["fence_token"]
        ):
            raise ValueError("branch has an active writer or unrelated reservation")
        ids = [parent["id"], delegate["id"], operation["verifier_task_id"]]
        for task_id in filter(None, ids):
            if await self.db._read_manual_pause(conn, task_id) is not None:
                raise ValueError(f"manual pause on {task_id}")
        if await conn.scalar(
            select(gates.c.id)
            .join(task_gates, task_gates.c.gate_id == gates.c.id)
            .where(task_gates.c.task_id.in_(ids), gates.c.status == "open")
            .limit(1)
        ):
            raise ValueError("open human or task gate must be resolved first")
        if await conn.scalar(
            select(sessions.c.id)
            .where(
                sessions.c.task_id.in_(ids),
                (sessions.c.state != "stopped") | sessions.c.claim_phase.is_not(None),
            )
            .limit(1)
        ) or await conn.scalar(
            select(workspaces.c.id)
            .where(
                workspaces.c.locked_by_task_id.in_(ids),
            )
            .limit(1)
        ):
            raise ValueError("a session or workspace still holds the parent, repair or verifier")
        if await conn.scalar(
            select(integration_promotion_intents.c.id)
            .where(
                integration_promotion_intents.c.operation_key == operation["id"],
                integration_promotion_intents.c.state.in_(
                    ["prepared", "conflict", "resolution_reserved"]
                ),
            )
            .limit(1)
        ):
            raise ValueError("unresolved promotion or external write")
        from src.integration.recovery_controls import IntegrationRecoveryControls

        if await IntegrationRecoveryControls._ambiguous_writes_on(conn, operation):
            raise ValueError("unresolved promotion or external write")
        readiness = await self.parent.readiness_on(
            conn, parent=parent, project=project, checkpoint=checkpoint, operation=operation
        )
        if any(
            item["reason"] not in {"receipt_chain", "repair_head_chain"}
            for item in readiness["blockers"]
        ):
            raise ValueError("child collection is not ready: " + str(readiness["blockers"]))
        existing = [
            e
            for e in (stage["dossier"] or {}).get(EXTENSIONS, [])
            if e["after_sha"] == request.head_sha
        ]
        collection_recovery = resolution is not None and gap_base is None
        if collection_recovery:
            from src.database.tables import integration_check_evidence

            if stage["state"] not in {"active", "awaiting_completion", "passed"}:
                raise ValueError("completed resolution stage is no longer recoverable")
            if await conn.scalar(
                select(integration_check_evidence.c.id)
                .where(
                    integration_check_evidence.c.operation_id == operation["id"],
                    integration_check_evidence.c.parent_generation == checkpoint["generation"],
                    integration_check_evidence.c.parent_head_sha == request.head_sha,
                    integration_check_evidence.c.conclusion == "failure",
                    integration_check_evidence.c.classification != "infrastructure",
                    integration_check_evidence.c.observed_at
                    >= resolution["intent"]["committed_at"],
                )
                .limit(1)
            ):
                raise ValueError("completed resolution has a recorded exact-head check failure")
        marker = (stage["dossier"] or {}).get(COLLECTION_RECOVERY)
        recovered_collection = (
            marker
            == {
                "head_sha": request.head_sha,
                "episode_id": checkpoint["episode_id"],
                "generation": checkpoint["generation"],
                "receipt_id": resolution["receipt"]["id"],
            }
            if collection_recovery
            else False
        )
        base_sha = (
            gap_base
            if gap_base is not None
            else existing[0]["before_sha"]
            if existing
            else readiness["head_sha"]
        )
        if not collection_recovery and (
            base_sha == request.head_sha or (existing and readiness["head_sha"] != request.head_sha)
        ):
            raise ValueError("head is not an unconsumed repair extension of the aggregate")
        proof = {
            "operation": operation,
            "checkpoint": checkpoint,
            "stage": stage,
            "delegate": dict(delegate),
            "completion": dict(completion),
            "owner": dict(owner),
            "base_sha": base_sha,
            "receipts": readiness["receipts"],
            "existing": existing,
            "authoring": authoring,
            "accepted_close": accepted_close,
            "resolution": resolution,
            "collection_recovery": collection_recovery,
            "recovered_collection": recovered_collection,
            "gap_proofs": [],
        }
        if collection_recovery:
            from types import SimpleNamespace

            for before, after in await self._receipt_gaps_on(
                conn, operation, checkpoint, readiness
            ):
                candidates = (
                    (
                        await conn.execute(
                            select(integration_repair_stages)
                            .where(
                                integration_repair_stages.c.operation_id == operation["id"],
                                integration_repair_stages.c.ordinal <= operation["active_stage"],
                                integration_repair_stages.c.current_subject["head_sha"].as_string()
                                == after,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .all()
                )
                if len(candidates) != 1:
                    raise ValueError(
                        f"receipt gap {before} -> {after} has no unique completed stage"
                    )
                gap = await self._proof_on(
                    conn,
                    SimpleNamespace(operation_id=request.operation_id, head_sha=after),
                    stage_ordinal=candidates[0]["ordinal"],
                    gap_base=before,
                )
                proof["gap_proofs"].append(gap)
        elif gap_base is None and readiness["outcome"] != "ready":
            raise ValueError("child collection is not ready: " + str(readiness["blockers"]))
        return proof

    async def _resolution_receipt_on(self, conn, operation, checkpoint, stage, *, project_id):
        """A committed resolution preserves the original writer's exact push fence."""
        head = (stage["current_subject"] or {}).get("head_sha")
        intents = (
            (
                await conn.execute(
                    select(integration_promotion_intents)
                    .where(
                        integration_promotion_intents.c.operation_key == operation["id"],
                        integration_promotion_intents.c.resolution_operation_id == operation["id"],
                        integration_promotion_intents.c.resolution_stage_ordinal
                        == stage["ordinal"],
                        integration_promotion_intents.c.resolution_task_id
                        == stage["repair_task_id"],
                        integration_promotion_intents.c.resolution_head_sha == head,
                        integration_promotion_intents.c.state == "committed",
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if len(intents) != 1:
            return None
        intent = dict(intents[0])
        receipt = (
            (
                await conn.execute(
                    select(task_delivery_receipts)
                    .where(
                        task_delivery_receipts.c.id == intent["receipt_id"],
                        task_delivery_receipts.c.parent_operation_id == operation["id"],
                        task_delivery_receipts.c.parent_episode_id == checkpoint["episode_id"],
                        task_delivery_receipts.c.target_task_id == operation["parent_task_id"],
                        task_delivery_receipts.c.repository_id == checkpoint["repository_id"],
                        task_delivery_receipts.c.target_branch == checkpoint["branch"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if receipt is None or not self.parent._trusted_code_receipt(receipt):
            return None
        evidence = receipt["resolution_evidence"] or {}
        author = evidence.get("authoring") or {}
        fence = author.get("fence") or {}
        push = {"kind": "exact_resolution_push_observed", "remote_sha": head}
        if (
            evidence.get("kind") != "conflict_resolution"
            or intent["project_id"] != project_id
            or not isinstance(intent["committed_at"], (int, float))
            or intent["resolution_push_started_at"] is None
            or intent["target_task_id"] != operation["parent_task_id"]
            or intent["repository_id"] != checkpoint["repository_id"]
            or intent["target_branch"] != checkpoint["branch"]
            or intent["source_task_id"] != receipt["source_task_id"]
            or intent["source_head"] != receipt["reviewed_head_sha"]
            or intent["source_base"] != evidence.get("original_source_base")
            or intent["expected_target"] != receipt["before_sha"]
            or receipt["after_sha"] != head
            or intent["resolution_tree_sha"] != evidence.get("resolved_tree_sha")
            or intent["resolution_commit_shas"] != evidence.get("repair_commit_shas")
            or intent["resolution_session_id"] != author.get("repair_session_id")
            or intent["resolution_session_instance_token"]
            != author.get("repair_session_instance_token")
            or intent["resolution_workspace_id"] != author.get("repair_workspace_id")
            or intent["resolution_fence_owner_id"] != stage["repair_task_id"]
            or intent["resolution_fence_token"] != fence.get("token")
            or intent["resolution_task_id"] != author.get("repair_task_id")
            or author.get("stage_ordinal") != stage["ordinal"]
            or intent["resolution_push_evidence"] != push
            or evidence.get("push_authority") != push
            or intent["remote_evidence"] != evidence.get("remote_proof")
        ):
            return None
        return {
            "intent": intent,
            "receipt": dict(receipt),
            "authoring": {
                "task_id": stage["repair_task_id"],
                "session_id": author["repair_session_id"],
                "instance_token": author["repair_session_instance_token"],
                "workspace_id": author["repair_workspace_id"],
                "fence_token": fence["token"],
            },
        }

    async def _receipt_gaps_on(self, conn, operation, checkpoint, readiness):
        edges = list(await extensions_on(conn, operation, checkpoint))
        head, gaps = readiness["checkpoint_sha"], []

        def extend(value):
            while matching := [edge for edge in edges if edge["before_sha"] == value]:
                if len(matching) != 1:
                    raise ValueError("ambiguous existing parent repair extension")
                edges.remove(matching[0])
                value = matching[0]["after_sha"]
            return value

        for receipt in sorted(
            readiness["receipts"], key=lambda row: (row["created_at"], row["id"])
        ):
            if (
                receipt["disposition"] != "code"
                or receipt["parent_operation_id"] != operation["id"]
            ):
                continue
            if receipt["parent_episode_id"] != checkpoint["episode_id"]:
                raise ValueError("child receipt belongs to another collection episode")
            if not self.parent._trusted_code_receipt(receipt):
                raise ValueError("untrusted child receipt cannot bind collection recovery")
            if receipt["before_sha"] != head:
                head = extend(head)
            if receipt["before_sha"] != head:
                gaps.append((head, receipt["before_sha"]))
            head = receipt["after_sha"]
        head = extend(head)
        if edges or head != (await self._locked_stage_head_on(conn, operation)):
            raise ValueError("receipt chain does not end at the completed resolution head")
        return gaps

    @staticmethod
    async def _locked_stage_head_on(conn, operation):
        return await conn.scalar(
            select(
                integration_repair_stages.c.current_subject["head_sha"].as_string(),
            ).where(
                integration_repair_stages.c.operation_id == operation["id"],
                integration_repair_stages.c.ordinal == operation["active_stage"],
            )
        )

    async def _git_proof(self, proof, repository, head_sha, *, published_head_sha=None):
        store = repository.retained_git_dir
        checkpoint, stage = proof["checkpoint"], proof["stage"]
        published_head_sha = published_head_sha or head_sha
        if checkpoint["branch"].removeprefix("refs/heads/") == repository.repo.default_branch:
            raise ValueError("parent repair recovery cannot target the default branch")
        remote = await self.promotion.git.als_remote_ref(
            str(store), checkpoint["branch"], repository_url=repository.origin_url
        )
        if remote.state is not RemoteRefState.PRESENT or remote.oid != published_head_sha:
            raise ValueError("published parent head differs from the supplied exact head")
        if not await self.promotion._is_ancestor(store, head_sha, published_head_sha):
            raise ValueError("historical repair is absent from the published parent head")
        if proof["collection_recovery"]:
            receipt = proof["resolution"]["receipt"]
            tree = await self.promotion._tree_oid(store, head_sha)
            if tree != proof["resolution"]["intent"]["resolution_tree_sha"]:
                raise ValueError("resolution receipt tree differs from the published head")
            commits = await self.promotion._resolution_commit_range(
                store,
                receipt["before_sha"],
                head_sha,
            )
            if commits != proof["resolution"]["intent"]["resolution_commit_shas"]:
                raise ValueError(
                    "resolution receipt does not prove the complete published commit range"
                )
            base_sha = receipt["before_sha"]
        else:
            base_sha = proof["base_sha"]
        if not await self.promotion._is_ancestor(store, stage["starting_sha"], head_sha):
            raise ValueError("head is unrelated to the authorized repair base")
        audited = await self.promotion._resolution_commit_range(
            store, stage["starting_sha"], head_sha
        )
        recorded = (stage["dossier"] or {}).get("repair_commits") or []
        if not proof["collection_recovery"] and (
            not audited or recorded[-len(audited) :] != audited
        ):
            raise ValueError("head does not match the stage's complete audited repair commit range")
        if not await self.promotion._is_ancestor(store, base_sha, head_sha):
            raise ValueError("head does not descend from the collected aggregate")
        commits = await self.promotion._resolution_commit_range(store, base_sha, head_sha)
        if not commits or (
            not proof["collection_recovery"] and any(sha not in audited for sha in commits)
        ):
            raise ValueError("aggregate extension contains unaudited commits")
        for receipt in proof["receipts"]:
            if receipt["after_sha"] and not await self.promotion._is_ancestor(
                store, receipt["after_sha"], published_head_sha
            ):
                raise ValueError("head loses an original child receipt")
        return {"base_sha": base_sha, "head_sha": head_sha, "commits": commits}
