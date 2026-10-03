"""Audited repair edges extending immutable parent collection receipts."""

from __future__ import annotations

import json
import shlex

from sqlalchemy import select, update

from src.database.tables import (
    archived_tasks,
    gates,
    integration_branch_owners,
    integration_outbox,
    integration_promotion_intents,
    integration_repair_stages,
    sessions,
    task_completion_records,
    task_gates,
    task_integration_checkpoints,
    tasks,
    workspaces,
)
from src.git.manager import RemoteRefState, is_valid_git_oid
from src.integration.parent_completion import ParentCompletion

EXTENSIONS = "parent_head_extensions"


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
        async with self.db.immediate() as conn:
            proof = await self._proof_on(conn, request)
        repository = await self.promotion._resolve_repository(proof["checkpoint"]["repository_id"])
        await self.promotion._ensure_retained_repository(repository)
        async with self.promotion.git.arepository_transaction(str(repository.retained_git_dir)):
            await self.promotion._fetch_all_heads(
                repository.retained_git_dir, repository.origin_url
            )
            git_proof = await self._git_proof(proof, repository, request.head_sha)
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
            if current["existing"]:
                return result | {"outcome": "already_recovered"}
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
        return result | {"outcome": "recovered"}

    async def _proof_on(self, conn, request):
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
                        integration_repair_stages.c.ordinal == operation["active_stage"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        subject = {
            "kind": "parent",
            "generation": checkpoint["generation"],
            "head_sha": request.head_sha,
        }
        if (
            stage is None
            or stage["writer_kind"] != "repair_delegate"
            or stage["current_subject"] != subject
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
        if (
            completion is None
            or completion["outcome"] != "pass"
            or completion["branch"] != checkpoint["branch"]
            or json.loads(completion["commits"]) != [request.head_sha]
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
        if len(close_events) != 1 or not all(
            close_events[0].get(key)
            for key in (
                "session_id",
                "instance_token",
                "workspace_id",
                "fence_token",
            )
        ):
            raise ValueError("repair lacks its exact fenced delegate-close audit")
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
        readiness = await self.parent.readiness_on(
            conn, parent=parent, project=project, checkpoint=checkpoint, operation=operation
        )
        if readiness["outcome"] != "ready":
            raise ValueError("child collection is not ready: " + str(readiness["blockers"]))
        existing = [
            e
            for e in (stage["dossier"] or {}).get(EXTENSIONS, [])
            if e["after_sha"] == request.head_sha
        ]
        base_sha = existing[0]["before_sha"] if existing else readiness["head_sha"]
        if base_sha == request.head_sha or (existing and readiness["head_sha"] != request.head_sha):
            raise ValueError("head is not an unconsumed repair extension of the aggregate")
        return {
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
        }

    async def _git_proof(self, proof, repository, head_sha):
        store = repository.retained_git_dir
        checkpoint, stage = proof["checkpoint"], proof["stage"]
        if checkpoint["branch"].removeprefix("refs/heads/") == repository.repo.default_branch:
            raise ValueError("parent repair recovery cannot target the default branch")
        remote = await self.promotion.git.als_remote_ref(
            str(store), checkpoint["branch"], repository_url=repository.origin_url
        )
        if remote.state is not RemoteRefState.PRESENT or remote.oid != head_sha:
            raise ValueError("published parent head differs from the supplied exact head")
        if not await self.promotion._is_ancestor(store, stage["starting_sha"], head_sha):
            raise ValueError("head is unrelated to the authorized repair base")
        audited = await self.promotion._resolution_commit_range(
            store, stage["starting_sha"], head_sha
        )
        recorded = (stage["dossier"] or {}).get("repair_commits") or []
        if not audited or recorded[-len(audited) :] != audited:
            raise ValueError("head does not match the stage's complete audited repair commit range")
        if not await self.promotion._is_ancestor(store, proof["base_sha"], head_sha):
            raise ValueError("head does not descend from the collected aggregate")
        commits = await self.promotion._resolution_commit_range(store, proof["base_sha"], head_sha)
        if not commits or any(sha not in audited for sha in commits):
            raise ValueError("aggregate extension contains unaudited commits")
        for receipt in proof["receipts"]:
            if receipt["after_sha"] and not await self.promotion._is_ancestor(
                store, receipt["after_sha"], head_sha
            ):
                raise ValueError("head loses an original child receipt")
        return {"base_sha": proof["base_sha"], "head_sha": head_sha, "commits": commits}
