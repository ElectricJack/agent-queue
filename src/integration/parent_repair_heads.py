"""Audited repair edges extending immutable parent collection receipts."""

from __future__ import annotations

import json
import os
import shlex

from sqlalchemy import or_, select, update

from src.database.queries.integration_state_queries import session_attached_clause
from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
from src.database.tables import (
    archived_tasks,
    gates,
    integration_branch_owners,
    integration_outbox,
    integration_promotion_intents,
    integration_repair_stages,
    integration_repair_operations,
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
from src.integration.recovery_controls import IntegrationRecoveryControls

EXTENSIONS = "parent_head_extensions"
COLLECTION_RECOVERY = "collection_head_recovery"
EMPTY_VERIFICATION_RECOVERY = "empty_verification_recovery"
RECEIPT_COVERED_EXTENSIONS = "receipt_covered_extensions"


def receipt_covers_extension(receipt, edge):
    """Two proofs may describe the same edge, only with the same complete range."""
    commits = (
        [receipt["squash_sha"]]
        if receipt["squash_sha"]
        else (receipt["resolution_evidence"] or {}).get("repair_commit_shas")
    )
    return (
        ParentCompletion._trusted_code_receipt(receipt)
        and receipt["before_sha"] == edge["before_sha"]
        and receipt["after_sha"] == edge["after_sha"]
        and commits == edge["commits"]
    )


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
            covered = [
                item
                for item in (stage["dossier"] or {}).get(RECEIPT_COVERED_EXTENSIONS, [])
                if item.get("extension") == edge
            ]
            if covered:
                receipt = (
                    (
                        await conn.execute(
                            select(task_delivery_receipts).where(
                                task_delivery_receipts.c.id == covered[0].get("receipt_id"),
                                task_delivery_receipts.c.parent_operation_id == operation["id"],
                                task_delivery_receipts.c.parent_episode_id
                                == checkpoint["episode_id"],
                                task_delivery_receipts.c.target_task_id
                                == operation["parent_task_id"],
                                task_delivery_receipts.c.repository_id
                                == checkpoint["repository_id"],
                                task_delivery_receipts.c.target_branch == checkpoint["branch"],
                                task_delivery_receipts.c.disposition == "code",
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    len(covered) != 1
                    or receipt is None
                    or not receipt_covers_extension(receipt, edge)
                ):
                    raise ValueError("parent repair extension lacks its reconciled receipt proof")
                continue
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
                    gap["gap_head_sha"],
                    published_head_sha=request.head_sha,
                )
                for gap in proof["gap_proofs"]
            ]
        async with self.db._engine.connect() as conn:
            workspace_proof = await self._workspace_proof_on(
                conn, proof, repository, request.head_sha
            )
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
            "repair_head_sha": proof["repair_head_sha"],
            "collection_receipt_ids": [row["id"] for row in proof["following_receipts"]],
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
        if proof["empty_verification"]:
            result["reason"] = "settle audited empty verification stage at the receipt-proven head"
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
                            gap["gap_head_sha"],
                            published_head_sha=request.head_sha,
                        )
                        != expected_gap
                    ):
                        raise ValueError("historical repair proof changed")
            if (
                await self._workspace_proof_on(conn, current, repository, request.head_sha)
                != workspace_proof
            ):
                raise ValueError("confirmed parent workspace changed during recovery")
            if (
                current["recovered_collection"]
                or (current["empty_verification"] and current["settled"])
                or (current["existing"] and (not current["advanced"] or current["settled"]))
            ):
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
            reverify = current["advanced"] or current["empty_verification"]
            if current["empty_verification"]:
                await self._cover_extensions_on(conn, current["covered_extensions"])
                dossier[EMPTY_VERIFICATION_RECOVERY] = {
                    "head_sha": request.head_sha,
                    "episode_id": current["checkpoint"]["episode_id"],
                    "generation": current["checkpoint"]["generation"],
                    "receipt_ids": result["collection_receipt_ids"],
                    "completion_id": current["completion"]["id"],
                    "authoring": current["authoring"],
                    "principal": principal,
                    "reason": request.reason,
                }
            elif not current["existing"]:
                edge = extension(
                    current["operation"],
                    current["checkpoint"] | {"generation": stage["current_subject"]["generation"]},
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
            if current["advanced"]:
                dossier["collected_head_recovery"] = {
                    "head_sha": request.head_sha,
                    "generation": current["checkpoint"]["generation"],
                    "receipt_ids": result["collection_receipt_ids"],
                    "repair_head_sha": current["repair_head_sha"],
                    "fence_token": current["owner"]["fence_token"],
                    "principal": principal,
                    "reason": request.reason,
                }
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == request.operation_id,
                    integration_repair_stages.c.ordinal == stage["ordinal"],
                )
                .values(
                    **(
                        {"state": "passed", "completed_at": self.parent.clock()} if reverify else {}
                    ),
                    dossier=dossier,
                )
            )
            await advance_checkpoint_on(
                conn, current["checkpoint"], request.head_sha, self.parent.clock()
            )
            if reverify:
                await conn.execute(
                    update(integration_repair_operations)
                    .where(
                        integration_repair_operations.c.id == request.operation_id,
                    )
                    .values(state="active", updated_at=self.parent.clock())
                )
                await conn.execute(
                    update(task_integration_checkpoints)
                    .where(
                        task_integration_checkpoints.c.task_id == current["checkpoint"]["task_id"],
                    )
                    .values(state="awaiting_children")
                )
            if current["operation"]["verifier_task_id"] is None or reverify:
                # A legacy close can strand the aggregate before its first
                # readiness event. Use the ordinary projection to file the
                # verifier and queue its fenced handoff in this transaction.
                ready = await self.parent.mark_ready_on(
                    conn,
                    current["operation"]["parent_task_id"],
                    require_verifier=reverify,
                    event_suffix=f":recovery:{request.head_sha}" if reverify else "",
                )
                if (
                    ready.get("state") != "integration_ready"
                    or ready.get("head_sha") != request.head_sha
                ):
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
                proof["checkpoint"] | {"generation": stage["current_subject"]["generation"]},
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
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == proof["checkpoint"]["task_id"])
            .values(state="awaiting_children")
        )
        ready = await self.parent.mark_ready_on(
            conn,
            proof["operation"]["parent_task_id"],
            require_verifier=True,
            event_suffix=f":recovery:{request.head_sha}",
        )
        if ready.get("state") != "integration_ready" or ready.get("head_sha") != request.head_sha:
            raise ValueError(
                "recovered collection cannot project verifier readiness: " + str(ready)
            )
        return result | {"outcome": "recovered"}

    async def _proof_on(self, conn, request, *, stage_ordinal=None, gap_base=None):
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
        subject = stage["current_subject"] if stage else None
        if (
            stage is None
            or stage["writer_kind"] != "repair_delegate"
            or not isinstance(subject, dict)
            or subject.get("kind") != "parent"
            or type(subject.get("generation")) is not int
            or not 0 <= subject["generation"] <= checkpoint["generation"]
            or not is_valid_git_oid(subject.get("head_sha", ""))
            or (stage_ordinal is not None and subject["head_sha"] != request.head_sha)
        ):
            raise ValueError(
                "current stage is not bound to this exact repaired head and generation"
            )
        stage = dict(stage)
        repair_head = subject["head_sha"]
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
            or commits not in ([repair_head], [])
            or (stage["dossier"] or {}).get("branch_sha") != repair_head
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
        author_keys = ("task_id", "session_id", "instance_token", "workspace_id", "fence_token")
        audited_authors = [
            {key: event[key] for key in author_keys}
            for event in close_events
            if all(event.get(key) for key in author_keys)
        ]
        if resolution is not None and close_events:
            if len(audited_authors) != len(close_events):
                raise ValueError(f"stage {stage['ordinal']} has an incomplete delegate-close audit")
            matching = [author for author in audited_authors if author == resolution["authoring"]]
            if len(matching) != 1:
                raise ValueError(
                    f"stage {stage['ordinal']} has no unique delegate-close audit "
                    "matching its resolution authoring"
                )
            authoring = matching[0]
        elif len(close_events) == 1 and len(audited_authors) == 1:
            authoring = audited_authors[0]
        elif (
            not close_events
            and resolution is not None
            and gap_base is None
            and repair_head == request.head_sha
        ):
            authoring = dict(resolution["authoring"])
            authoring["resolution_receipt_id"] = resolution["receipt"]["id"]
            authoring["resolution_intent_id"] = resolution["intent"]["id"]
        else:
            raise ValueError(
                f"stage {stage['ordinal']} repair lacks its exact fenced "
                "delegate-close audit or resolution receipt"
            )
        # Older closes proved the attached repair head, then lost it while
        # building the completion from a base checkout without the parent ref.
        # An empty record is usable only when this exact completion belongs to
        # the audited close. The stage lineage and remote Git proof below still
        # establish the head; summary text and an unrelated passing close do not.
        accepted_close = None
        archived_history = None
        if commits == [] and table is archived_tasks and gap_base is not None:
            archived_history = await self._close_history_on(
                conn,
                stage,
                operation,
                checkpoint,
                parent["project_id"],
            )
            if (
                len(archived_history["completions"]) != 1
                or archived_history["completions"][0]["id"] != completion["id"]
                or resolution is not None
                or stage["starting_sha"] != gap_base
                or {key: archived_history["audits"][0]["payload"][key] for key in author_keys}
                != authoring
            ):
                raise ValueError("archived empty gap lacks its one exact close and completion")
            authoring["archived_completion_id"] = completion["id"]
        elif commits == []:
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
        workspace = None
        if owner["confirmed_workspace_id"]:
            workspace = (
                (
                    await conn.execute(
                        select(workspaces)
                        .where(
                            workspaces.c.id == owner["confirmed_workspace_id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if workspace is None or workspace["project_id"] != parent["project_id"]:
                raise ValueError("confirmed parent workspace is unavailable")
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
        if await IntegrationRecoveryControls._ambiguous_writes_on(
            conn,
            operation,
            allowed_writer_id=owner["id"],
        ):
            raise ValueError("unresolved promotion or external write")
        existing = [
            e
            for e in (stage["dossier"] or {}).get(EXTENSIONS, [])
            if e.get("stage") == stage["ordinal"] and e.get("after_sha") == repair_head
        ]
        if len(existing) > 1:
            raise ValueError("repair extension is ambiguous")
        original = await self.parent.readiness_on(
            conn, parent=parent, project=project, checkpoint=checkpoint, operation=operation
        )
        collection_recovery = (
            resolution is not None and gap_base is None and repair_head == request.head_sha
        )
        empty_verification = (
            not collection_recovery
            and gap_base is None
            and (stage["dossier"] or {}).get("repair_commits") == []
        )
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
        following, advanced, settled = [], False, False
        readiness = original
        base_sha = (
            gap_base
            if gap_base is not None
            else existing[0]["before_sha"]
            if existing
            else original["head_sha"]
            if original["outcome"] == "ready"
            else stage["starting_sha"]
        )
        covered_extensions = []
        if empty_verification:
            following, covered_extensions = await self._empty_verification_proof_on(
                conn, operation, checkpoint, stage, completion, original, request.head_sha
            )
            base_sha = original["checkpoint_sha"]
            recovered = (stage["dossier"] or {}).get(EMPTY_VERIFICATION_RECOVERY) or {}
            settled = (
                recovered.get("head_sha") == request.head_sha
                and recovered.get("episode_id") == checkpoint["episode_id"]
                and recovered.get("generation") == checkpoint["generation"]
                and recovered.get("receipt_ids") == [row["id"] for row in following]
                and recovered.get("completion_id") == completion["id"]
                and stage["state"] == "passed"
            )
        elif collection_recovery or gap_base is not None:
            if any(
                item["reason"] not in {"receipt_chain", "repair_head_chain"}
                for item in original["blockers"]
            ):
                raise ValueError("child collection is not ready: " + str(original["blockers"]))
            if gap_base is not None and base_sha == repair_head:
                raise ValueError("historical gap has no repair extension")
        else:
            recorded = (stage["dossier"] or {}).get("repair_commits") or []
            if not existing and (not recorded or base_sha == repair_head):
                raise ValueError("head is not an unconsumed repair extension of the aggregate")
            proposed = extension(
                operation,
                checkpoint | {"generation": subject["generation"]},
                stage,
                {"base_sha": base_sha, "head_sha": repair_head, "commits": recorded},
                authoring,
            )
            readiness = await self.parent.readiness_on(
                conn,
                parent=parent,
                project=project,
                checkpoint=checkpoint,
                operation=operation,
                additional_extension=None if existing else proposed,
            )
            if readiness["outcome"] != "ready":
                raise ValueError("child collection is not ready: " + str(readiness["blockers"]))
            if readiness["head_sha"] != request.head_sha:
                raise ValueError("supplied head is not the receipt-proven current aggregate")
            following, cursor = [], repair_head
            for receipt in sorted(
                readiness["receipts"], key=lambda row: (row["created_at"], row["id"])
            ):
                if receipt["disposition"] == "code" and receipt["before_sha"] == cursor:
                    following.append(receipt)
                    cursor = receipt["after_sha"]
            if cursor != request.head_sha:
                raise ValueError(
                    "head advancement is not completely covered by collection receipts"
                )
            advanced = repair_head != request.head_sha
            recovered = (stage["dossier"] or {}).get("collected_head_recovery") or {}
            settled = (
                recovered.get("head_sha") == request.head_sha
                and recovered.get("generation") == checkpoint["generation"]
                and recovered.get("repair_head_sha") == repair_head
                and recovered.get("receipt_ids") == [row["id"] for row in following]
                and stage["state"] == "passed"
            )
        if (advanced or collection_recovery or empty_verification) and operation[
            "verifier_task_id"
        ]:
            verifier = (
                (
                    await conn.execute(
                        select(tasks)
                        .where(
                            tasks.c.id == operation["verifier_task_id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                verifier is None
                or verifier["project_id"] != parent["project_id"]
                or verifier["repo_id"] != checkpoint["repository_id"]
                or verifier["branch_name"] != checkpoint["branch"]
                or verifier["status"] not in {"PAUSED", "READY"}
                or verifier["assigned_agent_id"] is not None
            ):
                raise ValueError("current verifier is not an idle matching aggregate verifier")
        proof = {
            "project_id": parent["project_id"],
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
            "archived_close_history": archived_history,
            "repair_head_sha": repair_head,
            "following_receipts": following,
            "advanced": advanced,
            "settled": settled,
            "workspace": dict(workspace) if workspace else None,
            "resolution": resolution,
            "collection_recovery": collection_recovery,
            "recovered_collection": recovered_collection,
            "gap_proofs": [],
            "empty_verification": empty_verification,
            "covered_extensions": covered_extensions,
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
                            )
                            .order_by(integration_repair_stages.c.ordinal)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .all()
                )
                proof_stages = candidates
                origins, inherited = [], set()
                for candidate in candidates:
                    recorded = (candidate["dossier"] or {}).get("repair_commits") or []
                    if not isinstance(recorded, list) or any(
                        not isinstance(sha, str) or not is_valid_git_oid(sha) for sha in recorded
                    ):
                        raise ValueError("stage has malformed recorded repair commits")
                    if len(set(recorded)) != len(recorded):
                        raise ValueError("stage has duplicate recorded repair commits")
                    introduced = [sha for sha in recorded if sha not in inherited]
                    inherited.update(recorded)
                    if after in introduced:
                        origins.append((candidate, introduced))
                candidates = origins
                if len(candidates) != 1:
                    raise ValueError(
                        f"receipt gap {before} -> {after} has no unique completed stage"
                    )
                origin_subject = candidates[0][0]["current_subject"]
                if not isinstance(origin_subject, dict):
                    raise ValueError("receipt gap origin has no exact completed subject")
                gap = await self._proof_on(
                    conn,
                    SimpleNamespace(
                        operation_id=request.operation_id,
                        head_sha=origin_subject.get("head_sha", ""),
                    ),
                    stage_ordinal=candidates[0][0]["ordinal"],
                    gap_base=before,
                )
                gap["gap_head_sha"] = after
                gap["gap_recorded_commits"] = candidates[0][1]
                if gap["archived_close_history"]:
                    # Origin discovery discarded inherited stages; inspect the
                    # immediately preceding durable stage's complete dossier.
                    prior = next(
                        (
                            row
                            for row in proof_stages
                            if row["ordinal"] == gap["stage"]["ordinal"] - 1
                        ),
                        None,
                    )
                    if prior is None or ((prior["dossier"] or {}).get("repair_commits") or [])[
                        -1:
                    ] != [before]:
                        raise ValueError(
                            "archived gap does not follow the previous recorded stage head"
                        )
                if after != gap["repair_head_sha"]:
                    gap["interior"] = await self._interior_gap_on(conn, gap, after)
                proof["gap_proofs"].append(gap)
        return proof

    async def _empty_verification_proof_on(
        self, conn, operation, checkpoint, stage, completion, readiness, head_sha
    ):
        """An empty stage supplies no edge: receipts must prove the entire aggregate."""
        dossier = stage["dossier"] or {}
        incident = dossier.get("supervisor_recovery")
        if (
            not isinstance(incident, dict)
            or incident.get("incident_id")
            != f"repair-no-progress:{operation['id']}:{stage['ordinal']}"
            or incident.get("stage") != stage["ordinal"]
            or incident.get("subject") != stage["current_subject"]
            or any(
                incident.get(key) != stage[key]
                for key in ("attempts", "deadline_at", "repair_task_id")
            )
            or stage["state"] not in {"failed", "expired", "passed"}
            or (stage["state"] == "passed" and not dossier.get(EMPTY_VERIFICATION_RECOVERY))
            or stage["starting_sha"] != stage["current_subject"]["head_sha"]
            or json.loads(completion["commits"]) != []
        ):
            raise ValueError("empty verification stage lacks its exact no-progress close proof")
        if any(item["reason"] != "repair_head_chain" for item in readiness["blockers"]):
            raise ValueError("child collection is not ready: " + str(readiness["blockers"]))
        chain = sorted(
            (row for row in readiness["receipts"] if row["disposition"] == "code"),
            key=lambda row: (row["created_at"], row["id"]),
        )
        cursor = readiness["checkpoint_sha"]
        tips = []
        for receipt in chain:
            if (
                receipt["parent_operation_id"] != operation["id"]
                or receipt["parent_episode_id"] != checkpoint["episode_id"]
                or receipt["target_task_id"] != operation["parent_task_id"]
                or receipt["repository_id"] != checkpoint["repository_id"]
                or receipt["target_branch"] != checkpoint["branch"]
                or receipt["before_sha"] != cursor
                or not self.parent._trusted_code_receipt(receipt)
            ):
                raise ValueError("empty verification requires a contiguous trusted receipt chain")
            cursor = receipt["after_sha"]
            tips.append(cursor)
        if (
            cursor != head_sha
            or stage["current_subject"]["head_sha"] not in tips
            or checkpoint["checkpoint_sha"] not in [readiness["checkpoint_sha"], *tips]
        ):
            raise ValueError("empty verification head is not covered by the complete receipt chain")
        stages = (
            (
                await conn.execute(
                    select(integration_repair_stages)
                    .where(integration_repair_stages.c.operation_id == operation["id"])
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        delegate_ids = [row["repair_task_id"] for row in stages if row["repair_task_id"]]
        if any(row["state"] not in {"passed", "failed", "expired"} for row in stages):
            raise ValueError("another repair stage remains live")
        for task_id in delegate_ids:
            if await self.db._read_manual_pause(conn, task_id) is not None:
                raise ValueError(f"manual pause on {task_id}")
        if await conn.scalar(
            select(gates.c.id)
            .join(task_gates, task_gates.c.gate_id == gates.c.id)
            .where(task_gates.c.task_id.in_(delegate_ids), gates.c.status == "open")
            .limit(1)
        ):
            raise ValueError("open human or task gate must be resolved first")
        if await conn.scalar(
            select(sessions.c.id)
            .where(
                sessions.c.task_id.in_(delegate_ids),
                (sessions.c.state != "stopped") | sessions.c.claim_phase.is_not(None),
            )
            .limit(1)
        ) or await conn.scalar(
            select(workspaces.c.id)
            .where(
                workspaces.c.locked_by_task_id.in_(delegate_ids),
            )
            .limit(1)
        ):
            raise ValueError("an earlier repair delegate retains a session or workspace")
        covered = []
        for edge in await extensions_on(conn, operation, checkpoint):
            matches = [receipt for receipt in chain if receipt_covers_extension(receipt, edge)]
            if len(matches) != 1 or edge["stage"] == stage["ordinal"]:
                raise ValueError("empty verification cannot consume a repair outside the receipts")
            origin = next(row for row in stages if row["ordinal"] == edge["stage"])
            covered.append(
                {"stage": dict(origin), "extension": edge, "receipt_id": matches[0]["id"]}
            )
        return chain, covered

    @staticmethod
    async def _cover_extensions_on(conn, covered):
        """Keep original edges byte-identical, recording which immutable receipt covers them."""
        dossiers = {}
        for item in covered:
            stage = item["stage"]
            dossier = dossiers.setdefault(stage["ordinal"], dict(stage["dossier"] or {}))
            dossier[RECEIPT_COVERED_EXTENSIONS] = [
                *(dossier.get(RECEIPT_COVERED_EXTENSIONS) or []),
                {"extension": item["extension"], "receipt_id": item["receipt_id"]},
            ]
        for ordinal, dossier in dossiers.items():
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == covered[0]["stage"]["operation_id"],
                    integration_repair_stages.c.ordinal == ordinal,
                )
                .values(dossier=dossier)
            )

    async def _close_history_on(self, conn, stage, operation, checkpoint, project_id):
        """Pair every fenced close with its one subsequent passing completion."""
        completions = (
            (
                await conn.execute(
                    select(task_completion_records)
                    .where(
                        task_completion_records.c.task_id == stage["repair_task_id"],
                    )
                    .order_by(
                        task_completion_records.c.completed_at,
                        task_completion_records.c.id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        events = (
            (
                await conn.execute(
                    select(
                        integration_outbox.c.payload,
                        integration_outbox.c.created_at,
                    )
                    .where(
                        integration_outbox.c.project_id == project_id,
                        integration_outbox.c.event_type == "integration.repair_delegate_closed",
                        integration_outbox.c.payload["operation_id"].as_string() == operation["id"],
                        integration_outbox.c.payload["stage"].as_integer() <= stage["ordinal"],
                    )
                    .order_by(
                        integration_outbox.c.created_at,
                        integration_outbox.c.id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        previous = [
            row["payload"].get("fence_token")
            for row in events
            if row["payload"]["stage"] < stage["ordinal"]
        ]
        if any(type(token) is not int or token <= 0 for token in previous):
            raise ValueError("historical close has an invalid previous-stage fence")
        audits = [dict(row) for row in events if row["payload"]["stage"] == stage["ordinal"]]
        fence = max(previous, default=0)
        if not audits or len(audits) != len(completions):
            raise ValueError("historical closes and passing completions do not pair exactly")
        keys = ("task_id", "session_id", "instance_token", "workspace_id")
        for index, (audit, completion) in enumerate(zip(audits, completions, strict=True)):
            payload = audit["payload"]
            next_close = audits[index + 1]["created_at"] if index + 1 < len(audits) else None
            if (
                payload.get("project_id") != project_id
                or payload.get("task_id") != stage["repair_task_id"]
                or any(not isinstance(payload.get(key), str) or not payload[key] for key in keys)
                or type(payload.get("fence_token")) is not int
                or payload["fence_token"] <= fence
                or completion["outcome"] != "pass"
                or completion["branch"] != checkpoint["branch"]
                or completion["completed_at"] < audit["created_at"]
                or (next_close is not None and completion["completed_at"] >= next_close)
            ):
                raise ValueError("historical close lacks its exact subsequent passing completion")
            fence = payload["fence_token"]
        return {
            "audits": audits,
            "completions": [dict(row) for row in completions],
            "previous_fence": max(previous, default=0),
        }

    async def _interior_gap_on(self, conn, proof, gap_head):
        """Bind a historical interior edge to its stage's first fenced resolution."""
        stage, operation, checkpoint = proof["stage"], proof["operation"], proof["checkpoint"]
        intents = (
            (
                await conn.execute(
                    select(integration_promotion_intents)
                    .where(
                        integration_promotion_intents.c.resolution_operation_id == operation["id"],
                        integration_promotion_intents.c.resolution_stage_ordinal
                        == stage["ordinal"],
                        integration_promotion_intents.c.state == "committed",
                    )
                    .order_by(
                        integration_promotion_intents.c.committed_at,
                        integration_promotion_intents.c.id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if not intents or intents[0]["expected_target"] != gap_head:
            raise ValueError("interior gap is not the first committed resolution's exact base")
        history = await self._close_history_on(
            conn,
            stage,
            operation,
            checkpoint,
            proof["project_id"],
        )
        keys = ("task_id", "session_id", "instance_token", "workspace_id", "fence_token")
        resolutions, paired = [], set()
        for intent in intents:
            resolution = await self._resolution_receipt_on(
                conn,
                operation,
                checkpoint,
                stage
                | {
                    "current_subject": stage["current_subject"]
                    | {
                        "head_sha": intent["resolution_head_sha"],
                    }
                },
                project_id=proof["project_id"],
            )
            matching = [
                index
                for index, row in enumerate(history["audits"])
                if {key: row["payload"][key] for key in keys}
                == (resolution["authoring"] if resolution else None)
            ]
            if len(matching) != 1 or matching[0] in paired:
                raise ValueError("interior gap resolution lacks its exact historical close")
            completion = history["completions"][matching[0]]
            try:
                commits = json.loads(completion["commits"])
            except (TypeError, ValueError):
                commits = None
            if (
                commits not in ([], [intent["resolution_head_sha"]])
                or completion["completed_at"] < intent["committed_at"]
            ):
                raise ValueError("historical completion contradicts its committed resolution")
            paired.add(matching[0])
            resolutions.append(resolution)
        for index, completion in enumerate(history["completions"]):
            if index not in paired:
                try:
                    commits = json.loads(completion["commits"])
                except (TypeError, ValueError):
                    commits = None
                if commits not in ([], [gap_head]) or index >= min(paired):
                    raise ValueError("unpaired historical completion contradicts the interior gap")
        return {"resolutions": resolutions, "close_history": history}

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
        push = {
            "kind": "exact_resolution_push_observed",
            "remote_sha": head,
            "operation_id": operation["id"],
            "stage_ordinal": stage["ordinal"],
            "repair_task_id": stage["repair_task_id"],
            "repair_session_id": intent["resolution_session_id"],
            "repair_session_instance_token": intent["resolution_session_instance_token"],
            "repair_workspace_id": intent["resolution_workspace_id"],
            "fence_owner_id": stage["repair_task_id"],
            "fence_token": intent["resolution_fence_token"],
        }
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

    async def _workspace_proof_on(self, conn, proof, repository, head_sha):
        """A detached reservation still preserves any unpublished work in its former slot."""
        workspace = proof["workspace"]
        if workspace is None:
            if proof["empty_verification"]:
                raise ValueError("empty verification requires a confirmed parent workspace")
            return
        git = self.promotion.git
        path, branch = workspace["workspace_path"], proof["checkpoint"]["branch"]
        branch = branch.removeprefix("refs/heads/")
        remote = await git.arun_git_result(["remote", "get-url", "origin"], cwd=path)
        if remote.returncode != 0 or remote.stdout.strip() != repository.origin_url:
            raise ValueError("confirmed parent workspace repository cannot be proved")
        local_ref = "refs/heads/" + branch
        exists = await git.aref_exists(path, local_ref)
        local_sha = await git.arev_parse(path, local_ref) if exists else None
        if exists is None or exists and not local_sha:
            raise ValueError("confirmed parent ref cannot be observed")
        if local_sha:
            ancestry = await git.arun_git_result(
                ["merge-base", "--is-ancestor", local_sha, head_sha],
                cwd=str(repository.retained_git_dir),
            )
            if ancestry.returncode != 0:
                raise ValueError("confirmed parent ref has unpublished or unprovable work")
        writers = {
            os.path.realpath(p)
            for p in (
                await conn.execute(
                    select(workspaces.c.workspace_path).where(
                        or_(
                            workspaces.c.locked_by_task_id.is_not(None),
                            workspaces.c.locked_by_agent_id.is_not(None),
                        ),
                    )
                )
            ).scalars()
        }
        writers.update(
            os.path.realpath(p)
            for p in (
                await conn.execute(
                    select(sessions.c.work_dir).where(
                        session_attached_clause(),
                    )
                )
            ).scalars()
            if p
        )
        checkouts = []
        for checkout in await git.aworktree_list(path):
            if checkout.get("branch") != branch:
                continue
            if checkout.get("head") != local_sha or os.path.realpath(checkout["path"]) in writers:
                raise ValueError("confirmed parent checkout moved or has a retained writer")
            dirty = await git.aget_dirty_paths(checkout["path"])
            if dirty is None or any(p != ".agent-queue-lock" for p in dirty):
                raise ValueError("confirmed parent checkout has unpublished changes")
            checkouts.append((os.path.realpath(checkout["path"]), checkout["head"]))
        return {"local_sha": local_sha, "checkouts": sorted(checkouts)}

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
        if proof["empty_verification"]:
            return await self._empty_verification_git_proof(proof, store, head_sha)
        repair_head = proof["repair_head_sha"]
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
        if not await self.promotion._is_ancestor(store, stage["starting_sha"], repair_head):
            raise ValueError("head is unrelated to the authorized repair base")
        audited = await self.promotion._resolution_commit_range(
            store, stage["starting_sha"], repair_head
        )
        recorded = (stage["dossier"] or {}).get("repair_commits") or []
        gap_resolution = proof["resolution"] if "gap_head_sha" in proof else None
        if (
            not proof["collection_recovery"]
            and not gap_resolution
            and (not audited or recorded[-len(audited) :] != audited)
        ):
            raise ValueError("head does not match the stage's complete audited repair commit range")
        if "gap_head_sha" in proof:
            # A continued collection stage can record a fix before later child
            # resolutions advance its subject. Prove only the missing edge,
            # while its latest completion and close still prove the full subject.
            if not await self.promotion._is_ancestor(store, base_sha, head_sha):
                raise ValueError("receipt gap does not descend from the collected aggregate")
            if not await self.promotion._is_ancestor(store, head_sha, repair_head):
                raise ValueError("receipt gap is absent from its completed repair subject")
            if not await self.promotion._is_ancestor(store, repair_head, published_head_sha):
                raise ValueError("completed gap repair is absent from the published parent head")
            if gap_resolution:
                # A continuation may have moved starting_sha to the subject.
                # Its immutable committed resolution still proves that subject.
                intent, receipt = gap_resolution["intent"], gap_resolution["receipt"]
                if (
                    await self.promotion._tree_oid(store, repair_head)
                    != intent["resolution_tree_sha"]
                    or not await self.promotion._is_ancestor(
                        store, receipt["before_sha"], repair_head
                    )
                    or await self.promotion._resolution_commit_range(
                        store, receipt["before_sha"], repair_head
                    )
                    != intent["resolution_commit_shas"]
                ):
                    raise ValueError("completed gap repair differs from its resolution receipt")
            commits = await self.promotion._resolution_commit_range(store, base_sha, head_sha)
            recorded = proof["gap_recorded_commits"]
            if proof["archived_close_history"] and (head_sha != repair_head or commits != recorded):
                raise ValueError("archived empty gap differs from its complete introduced range")
            if not commits or not any(
                recorded[index : index + len(commits)] == commits for index in range(len(recorded))
            ):
                raise ValueError("receipt gap lacks its complete recorded repair commit range")
            if "interior" in proof:
                for resolution in proof["interior"]["resolutions"]:
                    intent, receipt = resolution["intent"], resolution["receipt"]
                    if (
                        not await self.promotion._is_ancestor(
                            store,
                            intent["resolution_head_sha"],
                            repair_head,
                        )
                        or await self.promotion._tree_oid(store, intent["resolution_head_sha"])
                        != intent["resolution_tree_sha"]
                        or await self.promotion._resolution_commit_range(
                            store,
                            receipt["before_sha"],
                            receipt["after_sha"],
                        )
                        != intent["resolution_commit_shas"]
                    ):
                        raise ValueError("historical resolution differs from its published range")
            if "interior" in proof or proof["archived_close_history"]:
                lineage = await self.promotion.git.arun_git_result(
                    ["rev-list", "--reverse", "--first-parent", f"{base_sha}..{head_sha}"],
                    cwd=str(store),
                    env={"LC_ALL": "C"},
                    lock_held=True,
                )
                if lineage.returncode != 0 or lineage.stdout.splitlines() != commits:
                    raise ValueError("historical gap is not a contiguous parent first-parent range")
            for receipt in proof["receipts"]:
                if receipt["after_sha"] and not await self.promotion._is_ancestor(
                    store, receipt["after_sha"], published_head_sha
                ):
                    raise ValueError("head loses an original child receipt")
            return {"base_sha": base_sha, "head_sha": head_sha, "commits": commits}
        if not await self.promotion._is_ancestor(store, base_sha, repair_head):
            raise ValueError("head does not descend from the collected aggregate")
        commits = await self.promotion._resolution_commit_range(store, base_sha, repair_head)
        if not commits or (
            not proof["collection_recovery"] and any(sha not in audited for sha in commits)
        ):
            raise ValueError("aggregate extension contains unaudited commits")
        if not await self.promotion._is_ancestor(store, repair_head, head_sha):
            raise ValueError("published aggregate does not descend from the proved repair head")
        covered = []
        for receipt in proof["following_receipts"]:
            if not await self.promotion._is_ancestor(
                store, receipt["before_sha"], receipt["after_sha"]
            ):
                raise ValueError("collection receipt has a non-ancestor head")
            receipt_commits = await self.promotion._resolution_commit_range(
                store, receipt["before_sha"], receipt["after_sha"]
            )
            expected = (
                [receipt["squash_sha"]]
                if receipt["squash_sha"]
                else receipt["resolution_evidence"]["repair_commit_shas"]
            )
            if receipt_commits != expected:
                raise ValueError("collection receipt does not cover its complete Git commit range")
            covered.extend(receipt_commits)
        if await self.promotion._resolution_commit_range(store, repair_head, head_sha) != covered:
            raise ValueError("head advancement contains commits without collection receipts")
        for receipt in proof["receipts"]:
            if receipt["after_sha"] and not await self.promotion._is_ancestor(
                store, receipt["after_sha"], published_head_sha
            ):
                raise ValueError("head loses an original child receipt")
        return {"base_sha": base_sha, "head_sha": repair_head, "commits": commits}

    async def _empty_verification_git_proof(self, proof, store, head_sha):
        covered = []
        for receipt in proof["following_receipts"]:
            if not await self.promotion._is_ancestor(
                store, receipt["before_sha"], receipt["after_sha"]
            ):
                raise ValueError("collection receipt has a non-ancestor head")
            commits = await self.promotion._resolution_commit_range(
                store, receipt["before_sha"], receipt["after_sha"]
            )
            expected = (
                [receipt["squash_sha"]]
                if receipt["squash_sha"]
                else receipt["resolution_evidence"]["repair_commit_shas"]
            )
            if commits != expected:
                raise ValueError("collection receipt does not cover its complete Git commit range")
            covered.extend(commits)
        if (
            not await self.promotion._is_ancestor(store, proof["base_sha"], head_sha)
            or await self.promotion._resolution_commit_range(store, proof["base_sha"], head_sha)
            != covered
        ):
            raise ValueError("published head contains commits outside the complete receipt chain")
        return {"base_sha": proof["base_sha"], "head_sha": head_sha, "commits": covered}
