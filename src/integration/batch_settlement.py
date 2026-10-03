"""Retire an externally delivered train without manufacturing promotion or CI."""

from __future__ import annotations

import hashlib
import json
import shlex

from sqlalchemy import or_, select, update

from src.database import tables as t
from src.git.manager import RemoteRefState, is_valid_git_oid
from src.integration.engine import root_engine_guard
from src.integration.recovery_controls import IntegrationRecoveryControls
from src.integration.scheduler import TrainService
from src.integration.stale_schedule import (
    _write_blockers_on,
    classify_outstanding_request_on,
    release_outstanding_request_on,
)

AUDIT_KEY = "external_delivery_settlement"


class DeliveredBatchSettlement:
    def __init__(self, promotion):
        self.promotion = promotion
        self.db = promotion.db

    async def run(self, request, *, principal):
        try:
            return await self._run(request.batch_id, request, principal=principal)
        except ValueError as exc:
            return {"outcome": "blocked", "batch_id": request.batch_id, "reason": str(exc)}

    @root_engine_guard("batch", outcome="blocked", publisher=True)
    async def _run(self, batch_id, request, *, principal):
        async with self.db.immediate() as conn:
            proof = await self._proof_on(conn, batch_id)
        if proof.get("audit"):
            return self._replay(request, proof["audit"])
        repository = await self.promotion._resolve_repository(proof["batch"]["repository_id"])
        if repository.repo.project_id != proof["batch"]["project_id"]:
            raise ValueError("repository belongs to another project")
        await self.promotion._ensure_retained_repository(repository)
        store = repository.retained_git_dir
        async with self.promotion.git.arepository_transaction(str(store)):
            await self.promotion._fetch_all_heads(store, repository.origin_url)
            target_ref = "refs/heads/" + repository.repo.default_branch.removeprefix("refs/heads/")
            target = await self._target(store, target_ref, repository.origin_url)
            if not request.dry_run and (
                request.expected_candidate_sha != proof["revision"]["head_sha"]
                or request.expected_target_sha != target
                or request.expected_snapshot_digest != proof["digest"]
            ):
                return {
                    "outcome": "changed",
                    "batch_id": batch_id,
                    "reason": "previewed candidate, target or durable snapshot changed",
                }
            await self._ancestry(store, proof, target)
            result = {
                "batch_id": batch_id,
                "project_id": proof["batch"]["project_id"],
                "operation_id": proof["operation"]["id"],
                "revision": proof["revision"]["revision"],
                "candidate_sha": proof["revision"]["head_sha"],
                "target_ref": target_ref,
                "target_sha": target,
                "snapshot_digest": proof["digest"],
                "member_count": len(proof["members"]),
                "validation": "external delivery; not CI attested",
            }
            if request.dry_run:
                command = [
                    "aq",
                    "integration",
                    "settle-delivered-batch",
                    batch_id,
                    "--apply",
                    "--candidate",
                    result["candidate_sha"],
                    "--target-head",
                    target,
                    "--snapshot",
                    proof["digest"],
                    "--reason",
                    "Retire the externally delivered train",
                ]
                return result | {"outcome": "would_settle", "apply_command": shlex.join(command)}
            async with self.db.immediate() as conn:
                current = await self._proof_on(conn, batch_id)
                if current.get("audit"):
                    return self._replay(request, current["audit"])
                if current["digest"] != proof["digest"]:
                    return result | {"outcome": "changed", "reason": "durable snapshot changed"}
                # Observe the authenticated remote again after acquiring all
                # domain locks. No Git write takes place in this recovery.
                if await self._target(store, target_ref, repository.origin_url) != target:
                    return result | {"outcome": "changed", "reason": "remote target moved"}
                now = self.promotion.clock()
                audit = result | {
                    "operator_id": principal,
                    "reason": request.reason.strip(),
                    "settled_at": now,
                    "members": [
                        {
                            "task_id": m["task_id"],
                            "head_sha": m["reviewed_head_sha"],
                            "generated_sha": r["generated_squash_sha"],
                        }
                        for m, r in zip(proof["members"], proof["results"], strict=True)
                    ],
                    "repair_completion_ids": [c["id"] for c in proof["completions"]],
                    "repair_stage_state": proof["stage"]["state"],
                }
                await conn.execute(
                    update(t.integration_repair_operations)
                    .where(t.integration_repair_operations.c.id == proof["operation"]["id"])
                    .values(state="cancelled", updated_at=now)
                )
                await conn.execute(
                    update(t.integration_repair_stages)
                    .where(
                        t.integration_repair_stages.c.operation_id == proof["operation"]["id"],
                        t.integration_repair_stages.c.state.in_(
                            ("pending", "active", "awaiting_completion")
                        ),
                    )
                    .values(state="cancelled", completed_at=now)
                )
                stage = proof["stage"]
                await conn.execute(
                    update(t.integration_repair_stages)
                    .where(
                        t.integration_repair_stages.c.operation_id == stage["operation_id"],
                        t.integration_repair_stages.c.ordinal == stage["ordinal"],
                    )
                    .values(dossier=dict(stage["dossier"] or {}) | {AUDIT_KEY: audit})
                )
                await conn.execute(
                    update(t.integration_batches)
                    .where(
                        t.integration_batches.c.id == batch_id,
                    )
                    .values(
                        lifecycle="aborted",
                        final_main_sha=target,
                        human_abort_reason=request.reason.strip(),
                        updated_at=now,
                    )
                )
                for owner in proof["owners"]:
                    if owner["handoff_state"] == "reserved":
                        released = await conn.execute(
                            update(t.integration_branch_owners)
                            .where(
                                t.integration_branch_owners.c.id == owner["id"],
                                t.integration_branch_owners.c.fence_token == owner["fence_token"],
                                t.integration_branch_owners.c.handoff_state == "reserved",
                            )
                            .values(handoff_state="released", updated_at=now)
                        )
                        if released.rowcount != 1:
                            raise ValueError("branch ownership changed during settlement")
                state = await classify_outstanding_request_on(
                    conn,
                    result["project_id"],
                    proof["schedule"],
                    now=now,
                    lock=True,
                )
                if state.verdict != "stale" or state.batch_id != batch_id:
                    # Roll back the retirement too; never leave partial release.
                    raise ValueError("retired request cannot be safely released")
                release = await release_outstanding_request_on(
                    self.db,
                    conn,
                    state,
                    schedule=proof["schedule"],
                    now=now,
                    released_by="integration_settle_delivered_batch",
                    reason=request.reason.strip(),
                )
                await self.db.log_event(
                    project_id=result["project_id"],
                    event_type="integration.batch_externally_delivered",
                    payload=json.dumps(audit, sort_keys=True),
                    conn=conn,
                )
            return result | {"outcome": "settled", "release": release.as_dict()}

    @staticmethod
    def _replay(request, audit):
        if not request.dry_run and (
            request.expected_candidate_sha != audit["candidate_sha"]
            or request.expected_target_sha != audit["target_sha"]
            or request.expected_snapshot_digest != audit["snapshot_digest"]
        ):
            return {
                "outcome": "changed",
                "batch_id": request.batch_id,
                "reason": "request does not match the durable settlement",
            }
        return {"outcome": "already_settled", **audit}

    async def _target(self, store, target_ref, origin_url):
        remote = await self.promotion.git.als_remote_ref(
            str(store), target_ref.removeprefix("refs/heads/"), repository_url=origin_url
        )
        if remote.state is not RemoteRefState.PRESENT or not is_valid_git_oid(remote.oid or ""):
            raise ValueError("authenticated default-branch head is unavailable")
        return remote.oid

    async def _ancestry(self, store, proof, target):
        candidate = proof["revision"]["head_sha"]
        edges = [
            (candidate, target),
            (proof["batch"]["base_sha"], candidate),
            (proof["revision"]["construction_base_sha"], candidate),
        ]
        for member, result in zip(proof["members"], proof["results"], strict=True):
            head, base = member["reviewed_head_sha"], member["source_base_sha"]
            if await self.promotion._tree_oid(store, head) != member["reviewed_tree_sha"]:
                raise ValueError("frozen member tree does not match its commit")
            edges.extend(
                [(base, head), (head, candidate), (result["generated_squash_sha"], candidate)]
            )
        for completion in proof["completions"]:
            commits = json.loads(completion["commits"])
            if not isinstance(commits, list) or any(not is_valid_git_oid(c) for c in commits):
                raise ValueError("repair completion commits are not exact Git OIDs")
            edges.extend((commit, target) for commit in commits)
        for before, after in edges:
            if not is_valid_git_oid(before or "") or not await self.promotion._is_ancestor(
                store, before, after
            ):
                raise ValueError(f"required ancestry is missing: {before} -> {after}")

    async def _proof_on(self, conn, batch_id):
        async def rows(table, *conditions, order=None):
            query = select(table).where(*conditions)
            if order is not None:
                query = query.order_by(order)
            return [dict(row) for row in (await conn.execute(query.with_for_update())).mappings()]

        hint = await conn.scalar(
            select(t.integration_batches.c.project_id).where(
                t.integration_batches.c.id == batch_id,
            )
        )
        if hint is None:
            raise ValueError("batch does not exist")
        await self.db.lock_hierarchy_project(conn, hint)
        batch = (await rows(t.integration_batches, t.integration_batches.c.id == batch_id))[0]
        operations = await rows(
            t.integration_repair_operations, t.integration_repair_operations.c.batch_id == batch_id
        )
        if len(operations) != 1:
            raise ValueError("batch must have one unambiguous repair operation")
        operation = operations[0]
        stages = await rows(
            t.integration_repair_stages,
            t.integration_repair_stages.c.operation_id == operation["id"],
            order=t.integration_repair_stages.c.ordinal,
        )
        stage = next((s for s in stages if s["ordinal"] == operation["active_stage"]), None)
        if stage is None:
            raise ValueError("current repair stage is missing")
        audit = (stage["dossier"] or {}).get(AUDIT_KEY)
        if audit:
            if (
                batch["lifecycle"] != "aborted"
                or operation["state"] != "cancelled"
                or stage["state"]
                != (
                    "cancelled"
                    if audit["repair_stage_state"] in {"pending", "active", "awaiting_completion"}
                    else audit["repair_stage_state"]
                )
                or batch["final_main_sha"] != audit["target_sha"]
            ):
                raise ValueError("durable settlement no longer matches the batch")
            return {"audit": audit}
        if (
            batch["lifecycle"] not in {"testing", "repairing"}
            or operation["state"] not in {"active", "escalated"}
            or operation["target_kind"] != "batch"
            or operation["episode_id"] != batch_id
        ):
            raise ValueError(
                "batch is not an active legacy repair; human holds require normal recovery"
            )
        repository = await rows(t.repos, t.repos.c.id == batch["repository_id"])
        project = await rows(t.projects, t.projects.c.id == hint)
        if len(repository) != 1 or repository[0]["project_id"] != hint or not project:
            raise ValueError("canonical repository identity is missing")
        if project[0]["integration_repository_id"] != batch["repository_id"]:
            raise ValueError("project integration repository changed")
        revisions = await rows(
            t.integration_candidate_revisions,
            t.integration_candidate_revisions.c.batch_id == batch_id,
            t.integration_candidate_revisions.c.revision == batch["current_revision"],
        )
        if len(revisions) != 1 or revisions[0]["state"] not in {"built", "testing", "red", "green"}:
            raise ValueError("current candidate has not completed construction")
        revision = revisions[0]
        members = await rows(
            t.integration_batch_members,
            t.integration_batch_members.c.batch_id == batch_id,
            order=t.integration_batch_members.c.ordinal,
        )
        results = await rows(
            t.integration_candidate_member_results,
            t.integration_candidate_member_results.c.batch_id == batch_id,
            t.integration_candidate_member_results.c.revision == revision["revision"],
            order=t.integration_candidate_member_results.c.member_ordinal,
        )
        ordinals = list(range(len(members)))
        if (
            not members
            or [m["ordinal"] for m in members] != ordinals
            or [r["member_ordinal"] for r in results] != ordinals
            or revision["next_member_ordinal"] != len(members)
        ):
            raise ValueError("complete frozen membership and results are required")
        if revision["source_manifest"] is not None and revision["source_manifest"] != members:
            raise ValueError("candidate manifest differs from frozen batch membership")
        digest = TrainService._manifest_digest(
            [
                {
                    "task_id": member["task_id"],
                    "repository_id": member["repository_id"],
                    "source_base": member["source_base_sha"],
                    "source_head": member["reviewed_head_sha"],
                    "review": {
                        "id": member["review_evidence_id"],
                        "reviewed_tree_sha": member["reviewed_tree_sha"],
                    },
                    "source_ref": member["source_ref"],
                    "source_ref_retention": member["source_ref_retention"],
                }
                for member in members
            ]
        )
        if digest != batch["source_manifest_digest"]:
            raise ValueError("frozen membership digest does not match")
        for member, result in zip(members, results, strict=True):
            if (
                member["repository_id"] != batch["repository_id"]
                or result["result"] != "applied"
                or result["input_head_sha"] != member["reviewed_head_sha"]
                or result["input_tree_sha"] != member["reviewed_tree_sha"]
            ):
                raise ValueError("member result does not match its frozen input")
        delegates = {s["repair_task_id"] for s in stages if s["repair_task_id"]}
        if not stage["repair_task_id"] or stage["writer_kind"] != "repair_delegate":
            raise ValueError("current stage has no repair delegate")
        task_ids = delegates | {m["task_id"] for m in members}
        task_rows = await rows(t.tasks, t.tasks.c.id.in_(task_ids), order=t.tasks.c.id)
        # Historical delegates may have been archived by supported cleanup.
        # Source members and the current delegate still require active rows.
        missing = task_ids - {task["id"] for task in task_rows}
        historical = delegates - {stage["repair_task_id"]}
        if missing - historical:
            raise ValueError("source or current repair task is missing")
        archived_delegates = await rows(
            t.archived_tasks, t.archived_tasks.c.id.in_(missing), order=t.archived_tasks.c.id
        ) if missing else []
        task_rows = sorted(task_rows + archived_delegates, key=lambda task: task["id"])
        if len(task_rows) != len(task_ids):
            raise ValueError("historical repair task is missing from active and archived records")
        if any(
            task["project_id"] != hint or task["repo_id"] != batch["repository_id"]
            for task in task_rows
        ):
            raise ValueError("source or repair repository binding changed")
        completions = []
        for task_id in sorted(delegates):
            task = next((task for task in task_rows if task["id"] == task_id), None)
            if (
                task is None
                or task["status"] not in {"COMPLETED", "FAILED", "BLOCKED"}
                or task["assigned_agent_id"]
                or task["created_by_kind"] != "integration_repair"
                or task["created_by_id"] != operation["id"]
                or task["project_id"] != hint
                or task["repo_id"] != batch["repository_id"]
                or task["parent_task_id"] is not None
                or str(task["branch_name"] or "").removeprefix("refs/heads/")
                != batch["integration_branch"].removeprefix("refs/heads/")
            ):
                raise ValueError("every repair delegate must be terminal and detached")
            completed = await rows(
                t.task_completion_records,
                t.task_completion_records.c.task_id == task_id,
                order=t.task_completion_records.c.completed_at.desc(),
            )
            if task_id == stage["repair_task_id"] and (
                task["status"] != "COMPLETED" or not completed or completed[0]["outcome"] != "pass"
            ):
                raise ValueError("passing durable current repair completion evidence is missing")
            if completed:
                completions.append(completed[0])
        live_sessions = await rows(
            t.sessions,
            t.sessions.c.task_id.in_(task_ids),
            or_(t.sessions.c.state != "stopped", t.sessions.c.claim_phase.is_not(None)),
        )
        locked = await rows(t.workspaces, t.workspaces.c.locked_by_task_id.in_(task_ids))
        if live_sessions or locked or any(task["assigned_agent_id"] for task in task_rows):
            raise ValueError("a source or repair still has a live writer or locked checkout")
        holds = await rows(
            t.task_metadata,
            t.task_metadata.c.task_id.in_(task_ids),
            t.task_metadata.c.key.in_(("manual_pause", "integration_operator_hold")),
        )
        applicable = select(t.task_gates.c.gate_id).where(t.task_gates.c.task_id.in_(task_ids))
        human_gates = await rows(
            t.gates,
            t.gates.c.project_id == hint,
            t.gates.c.gate_type == "human",
            t.gates.c.status == "open",
            or_(
                t.gates.c.id.in_(applicable),
                t.gates.c.await_id.in_(task_ids | {batch_id, operation["id"]}),
            ),
        )
        subjects = await rows(t.integration_subjects, t.integration_subjects.c.batch_id == batch_id)
        if (
            holds
            or human_gates
            or any(s["gate_id"] or s["engine"] == "reconciler" for s in subjects)
        ):
            raise ValueError("human gate, operator hold or reconciler ownership blocks settlement")
        owners = await rows(
            t.integration_branch_owners,
            t.integration_branch_owners.c.repository_id == batch["repository_id"],
            or_(
                t.integration_branch_owners.c.ref == batch["integration_branch"],
                t.integration_branch_owners.c.owner_id.in_(delegates | {operation["id"]}),
            ),
            order=t.integration_branch_owners.c.id,
        )
        for owner in owners:
            if owner["handoff_state"] == "released":
                continue
            if (
                owner["ref"] != batch["integration_branch"]
                or owner["session_id"]
                or owner["workspace_id"]
                or owner["handoff_state"] != "reserved"
                or (owner["owner_id"], owner["owner_role"])
                not in {(operation["id"], "collector")} | {(d, "repair") for d in delegates}
            ):
                raise ValueError("branch has competing or attached ownership")
        blockers = await IntegrationRecoveryControls._ambiguous_writes_on(
            conn,
            operation,
            allow_reserved_delegate=True,
        )
        if blockers:
            raise ValueError("unresolved external writes: " + ", ".join(blockers))
        batch_blockers = await _write_blockers_on(conn, batch_id)
        if any(blocker["code"] != "live_operation" for blocker in batch_blockers):
            raise ValueError("batch has unresolved external writes")
        cleanup = await rows(
            t.integration_cleanup_items,
            t.integration_cleanup_items.c.batch_id == batch_id,
            t.integration_cleanup_items.c.execution_nonce.is_not(None),
        )
        if cleanup:
            raise ValueError("batch has an active cleanup writer")
        schedule_rows = await rows(
            t.project_integration_schedules, t.project_integration_schedules.c.project_id == hint
        )
        if (
            len(schedule_rows) != 1
            or schedule_rows[0]["outstanding_request_id"] != batch["request_id"]
        ):
            raise ValueError("batch no longer owns the outstanding sweep")
        leases = await rows(
            t.project_integration_leases, t.project_integration_leases.c.project_id == hint
        )
        if any(
            lease["batch_id"] != batch_id or lease["repository_id"] != batch["repository_id"]
            for lease in leases
        ):
            raise ValueError("project lease belongs to another batch")
        proof = dict(
            batch=batch,
            operation=operation,
            stage=stage,
            stages=stages,
            revision=revision,
            members=members,
            results=results,
            owners=owners,
            schedule=schedule_rows[0],
            leases=leases,
            repository=repository,
            project={
                key: value
                for key, value in project[0].items()
                if key == "id"
                or key == "integration_repository_id"
                or key.startswith("hierarchical_integration_")
            },
            tasks=task_rows,
            completions=completions,
            subjects=subjects,
        )
        proof["digest"] = hashlib.sha256(
            json.dumps(
                proof,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return proof
