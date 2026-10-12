"""Reconcile one completed, preserved parent resolution without reopening its budget."""

from __future__ import annotations

import shlex

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import JSONB

from src.database.tables import (
    gates,
    integration_branch_owners,
    integration_owner_recoveries,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    projects,
    sessions,
    task_branch_origins,
    task_gates,
    task_integration_checkpoints,
    tasks,
    workspaces,
)
from src.git.manager import RemoteRefState
from src.integration.models import BranchKey, RepairPolicy
from src.integration.owner_recovery import consume_recovery_progress, recovery_ref_allowed
from src.integration.promotion_contracts import PromotionInvariantError
from src.integration.writers import OperationSafety
from src.integration.repair import _RepairInvariant

_RECORD = "preserved_resolution_recovery"


class RecoveryRefused(ValueError):
    pass


class PreservedRepairRecovery:
    def __init__(self, promotion, repair):
        self.promotion = promotion
        self.repair = repair
        self.db = promotion.db

    async def run(self, request, *, principal: str) -> dict:
        try:
            return await self._run(request, principal)
        except RecoveryRefused as exc:
            return {
                "outcome": "blocked",
                "operation_id": request.operation_id,
                "reason": str(exc),
                "next_step": "Resolve the named blocker through supported controls; rerun preview.",
            }

    async def _run(self, request, principal):
        async with self.db.immediate() as conn:
            proof = await self._proof_on(conn, request)
        if proof.get("completed"):
            if not request.dry_run:
                await self._cleanup_completed(proof)
            return self._public(proof) | {"outcome": "already_recovered"}
        repository = await self.promotion._resolve_repository(proof["intent"]["repository_id"])
        self.promotion._assert_resolution_repository(proof["intent"], repository)
        await self.promotion._ensure_retained_repository(repository)
        async with self.promotion.git.arepository_transaction(str(repository.retained_git_dir)):
            await self.promotion._fetch_all_heads(
                repository.retained_git_dir, repository.origin_url
            )
            git_proof = await self._git_proof(proof, repository)
        result = self._public(proof) | git_proof
        if request.dry_run:
            command = shlex.join(
                [
                    "aq",
                    "integration",
                    "recover-preserved-repair",
                    request.operation_id,
                    "--intent",
                    request.intent_id,
                    "--candidate",
                    request.candidate_sha,
                    "--apply",
                    "--stage",
                    str(proof["stage"]["ordinal"]),
                    "--released-fence",
                    str(proof["release"]["released_fence_token"]),
                    "--reason",
                    "Consume the verified preserved resolution",
                ]
            )
            return result | {"outcome": "would_recover", "apply_command": command}
        if (
            request.expected_stage != proof["stage"]["ordinal"]
            or request.expected_released_fence != proof["release"]["released_fence_token"]
        ):
            return result | {"outcome": "changed", "reason": "previewed stage or fence changed"}

        # Reserve under a fresh collector fence, recording original authoring identity.
        async with self.db.immediate() as conn:
            current = await self._proof_on(conn, request)
            if current != proof:
                return result | {"outcome": "changed", "reason": "recovery proof changed"}
            async with self.promotion.git.arepository_transaction(str(repository.retained_git_dir)):
                await self._git_proof(current, repository)
            if not current["record"]:
                fence = await self.promotion.ownership.acquire(
                    BranchKey(
                        repository_id=repository.repo.id, branch=current["intent"]["target_branch"]
                    ),
                    request.operation_id,
                    "collector",
                    conn=conn,
                )
                release = current["release"]
                await self.db.reserve_integration_conflict_resolution(
                    conn,
                    request.intent_id,
                    {
                        "resolved_head_sha": request.candidate_sha,
                        "resolved_tree_sha": git_proof["tree_sha"],
                        "repair_commit_shas": git_proof["repair_commit_shas"],
                        "operation_id": request.operation_id,
                        "stage_ordinal": current["stage"]["ordinal"],
                        "repair_task_id": current["stage"]["repair_task_id"],
                        "repair_session_id": release["session_id"],
                        "repair_session_instance_token": release["stop_proof"]["instance_token"],
                        "repair_workspace_id": release["workspace_id"],
                        "fence_owner_id": current["stage"]["repair_task_id"],
                        "fence_token": release["fence_token"],
                    },
                )
                record = {
                    "intent_id": request.intent_id,
                    "candidate_sha": request.candidate_sha,
                    "tree_sha": git_proof["tree_sha"],
                    "release_id": current["audit"]["id"],
                    "released_fence_token": release["released_fence_token"],
                    "collector_fence_token": fence.token,
                    "principal": principal,
                    "reason": request.reason,
                    "recorded_at": self.repair.clock(),
                    "operation_snapshot": current["operation"],
                    "budget": {
                        key: current["stage"][key]
                        for key in (
                            "attempts",
                            "started_at",
                            "deadline_at",
                            "deadline_event_id",
                            "state",
                        )
                    },
                }
                await self._record_on(conn, current, record)
        await self.promotion._crash("after_preserved_reservation")

        # Commit a marker before the only permitted external write. A retry may
        # observe a completed write, but an old target after a marker is ambiguous.
        started_here = False
        async with self.db.immediate() as conn:
            current = await self._proof_on(conn, request)
            async with self.promotion.git.arepository_transaction(str(repository.retained_git_dir)):
                observed = await self._git_proof(current, repository)
            if observed["remote_head_sha"] == current["intent"]["expected_target"]:
                if current["intent"]["resolution_push_started_at"] is not None:
                    raise RecoveryRefused(
                        "resolution push may be in flight; inspect the exact target and writer "
                        "before reconciliation; the command will not repeat this push"
                    )
                await self.db.mark_integration_resolution_push_started_on(
                    conn, request.intent_id, started_at=self.repair.clock()
                )
                started_here = True
        await self.promotion._crash("after_preserved_push_marker")

        async with self.db.immediate() as conn:
            current = await self._proof_on(conn, request)
            async with self.promotion.git.arepository_transaction(str(repository.retained_git_dir)):
                observed = await self._git_proof(current, repository)
                if observed["remote_head_sha"] == current["intent"]["expected_target"]:
                    if not started_here:
                        raise RecoveryRefused("unproven prior push; publication is ambiguous")
                    await self.promotion.git.apush_expected_delivery(
                        str(repository.retained_git_dir),
                        current["intent"]["expected_target"],
                        request.candidate_sha,
                        current["intent"]["target_branch"],
                        current["intent"]["expected_target"],
                        lock_held=True,
                        remote=repository.origin_url,
                    )
                    await self.promotion._crash("after_preserved_push")
                evidence = {
                    "kind": "preserved_resolution_push_observed",
                    "remote_sha": request.candidate_sha,
                    "recovery": current["record"],
                    "preserved_ref": current["release"]["preserved_ref"],
                }
                await self.db.record_integration_resolution_push_on(
                    conn, request.intent_id, evidence
                )
                # Keep the subject exact without waking the expired stage. The
                # normal receipt path owns readiness, gates and verification.
                record = current["record"] | {"completed_at": self.repair.clock()}
                await self._record_on(conn, current, record, head_sha=request.candidate_sha)
                await self.db._finalize_integration_promotion_on(
                    conn,
                    request.intent_id,
                    {
                        "kind": "exact_resolution_tip",
                        "remote_sha": request.candidate_sha,
                        "resolved_tree_sha": current["intent"]["resolution_tree_sha"],
                        "repair_commit_shas": current["intent"]["resolution_commit_shas"],
                    },
                )
        await consume_recovery_progress(
            self.promotion.git, str(repository.retained_git_dir),
            current["intent"]["target_branch"],
            {"ref": current["release"]["preserved_ref"], "sha": request.candidate_sha,
             "owner_row_id": current["audit"]["owner_row_id"]},
            repository_url=repository.origin_url,
        )
        return result | {
            "outcome": "recovered",
            "next_step": "Normal parent collection and verification can continue; no repair budget was renewed.",
        }

    async def _cleanup_completed(self, proof):
        """Replay cleanup after a crash between the receipt and snapshot deletion."""
        async with self.db._engine.connect() as conn:
            audit = (await conn.execute(select(integration_owner_recoveries).where(
                integration_owner_recoveries.c.id == proof["record"]["release_id"],
            ))).mappings().one()
        evidence = audit["evidence"]
        branch = proof["intent"]["target_branch"]
        if evidence["preserved_ref"] == branch.removeprefix("refs/heads/"):
            return
        repository = await self.promotion._resolve_repository(audit["repository_id"])
        await self.promotion._ensure_retained_repository(repository)
        store = str(repository.retained_git_dir)
        await self.promotion.git.afetch_origin(store, repository_url=repository.origin_url)
        target = await self.promotion.git.als_remote_ref(store, branch)
        if (target.state is not RemoteRefState.PRESENT
            or not await self.promotion.git.ais_ancestor(
                store, evidence["preserved_sha"], target.oid, strict=True,
            )):
            raise RecoveryRefused("completed resolution is no longer on the task branch")
        await consume_recovery_progress(
            self.promotion.git, store, branch,
            {"ref": evidence["preserved_ref"], "sha": evidence["preserved_sha"],
             "owner_row_id": audit["owner_row_id"]}, repository_url=repository.origin_url,
        )

    async def _proof_on(self, conn, request):
        async def row(table, *conditions):
            value = (
                (await conn.execute(select(table).where(*conditions).with_for_update()))
                .mappings()
                .one_or_none()
            )
            return dict(value) if value is not None else None

        hint = (
            (
                await conn.execute(
                    select(integration_repair_operations).where(
                        integration_repair_operations.c.id == request.operation_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if hint is None:
            raise RecoveryRefused("operation is missing")
        project_id = await OperationSafety._project_id_on(conn, hint)
        await self.db.lock_hierarchy_project(conn, project_id)
        operation = await row(
            integration_repair_operations,
            integration_repair_operations.c.id == request.operation_id,
        )
        if operation is None or operation["target_kind"] != "parent":
            raise RecoveryRefused("operation is not a parent repair")
        if await OperationSafety._project_id_on(conn, operation) != project_id:
            raise RecoveryRefused("operation project changed")
        project = await row(projects, projects.c.id == project_id)
        if (
            project is None
            or project["status"] != "ACTIVE"
            or project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
        ):
            raise RecoveryRefused("project is paused or hierarchical integration is disabled")
        stage = await row(
            integration_repair_stages,
            integration_repair_stages.c.operation_id == request.operation_id,
            integration_repair_stages.c.ordinal == operation["active_stage"],
        )
        intent = await row(
            integration_promotion_intents, integration_promotion_intents.c.id == request.intent_id
        )
        if stage is None or intent is None:
            raise RecoveryRefused("current stage or requested intent is missing")
        record = (stage["dossier"] or {}).get(_RECORD)
        if record and (
            record["intent_id"] != request.intent_id
            or record["candidate_sha"] != request.candidate_sha
        ):
            raise RecoveryRefused("stage already records another preserved candidate")
        if (
            record
            and record.get("completed_at")
            and intent["state"] == "committed"
            and intent["resolution_head_sha"] == request.candidate_sha
        ):
            return {"completed": True, "stage": stage, "intent": intent, "record": record}
        if record and (
            record["operation_snapshot"] != operation
            or any(stage[key] != value for key, value in record["budget"].items())
        ):
            raise RecoveryRefused("frozen operation or consumed budget changed after reservation")
        if operation["state"] != "escalated":
            raise RecoveryRefused(
                f"operation is {operation['state']}; human decisions require integration resume"
            )
        incident = (stage["dossier"] or {}).get("supervisor_recovery") or {}
        if (
            stage["ordinal"] < 1
            or stage["state"] != "expired"
            or stage["writer_kind"] != "repair_delegate"
            or stage["trigger_id"] != f"stage-exhausted:{operation['id']}:{stage['ordinal'] - 1}"
            or incident.get("incident_id")
            != f"repair-no-progress:{operation['id']}:{stage['ordinal']}"
            or incident.get("subject") != stage["current_subject"]
            or incident.get("repair_task_id") != stage["repair_task_id"]
        ):
            raise RecoveryRefused("stage is not the current expired no-progress incident")
        parent = await row(tasks, tasks.c.id == operation["parent_task_id"])
        checkpoint = await row(
            task_integration_checkpoints,
            task_integration_checkpoints.c.task_id == operation["parent_task_id"],
        )
        if (
            parent is None
            or checkpoint is None
            or parent["project_id"] != project_id
            or parent["status"] != "PAUSED"
            or checkpoint["episode_id"] != operation["episode_id"]
            or checkpoint["state"] != "awaiting_children"
            or checkpoint["repository_id"] != parent["repo_id"]
            or project["integration_repository_id"] != parent["repo_id"]
            or checkpoint["branch"] != parent["branch_name"]
        ):
            raise RecoveryRefused("parent is not detached in the current collection episode")
        subject = {
            "kind": "parent",
            "generation": checkpoint["generation"],
            "head_sha": intent["expected_target"],
        }
        if (
            stage["current_subject"] != subject
            or stage["starting_sha"] != intent["expected_target"]
        ):
            raise RecoveryRefused("stage subject moved from the conflict target")
        conflicts = (
            (
                await conn.execute(
                    select(integration_promotion_intents.c.id)
                    .where(
                        integration_promotion_intents.c.repository_id == parent["repo_id"],
                        integration_promotion_intents.c.target_branch == parent["branch_name"],
                        integration_promotion_intents.c.state.not_in(("committed", "superseded")),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if conflicts != [request.intent_id]:
            raise RecoveryRefused("target has no unique current conflict intent")
        if (
            intent["operation_key"] != operation["id"]
            or intent["fence_owner_id"] != operation["id"]
            or intent["project_id"] != project_id
            or intent["target_task_id"] != parent["id"]
            or intent["superseded_by_intent_id"] is not None
            or intent["state"] != ("resolution_reserved" if record else "conflict")
            or (not record and intent["resolution_head_sha"] is not None)
        ):
            raise RecoveryRefused("intent is not this operation's unresolved conflict")
        # _start_context_on requires conflict state, so for a reservation replay
        # validate the frozen route and subject using the original conflict view.
        if not record:
            try:
                context = await self.repair._start_context_on(
                    conn,
                    operation,
                    starting_sha=intent["expected_target"],
                    trigger_id=request.intent_id,
                )
            except _RepairInvariant as exc:
                raise RecoveryRefused(str(exc)) from exc
            if context is None:
                raise RecoveryRefused("frozen policy, route or conflict subject changed")
        from src.integration.models import HierarchicalIntegrationPolicy

        policy = HierarchicalIntegrationPolicy.model_validate(operation["policy_snapshot"])
        budget = RepairPolicy.model_validate(stage["policy"])
        limit = min(budget.debug_attempts, policy.parent.repair.debug_attempts)
        if int(stage["attempts"]) >= limit:
            raise RecoveryRefused(
                "attempt budget exhausted; a human policy decision is required, no budget reset is permitted"
            )
        source = await row(tasks, tasks.c.id == intent["source_task_id"])
        source_checkpoint = await row(
            task_integration_checkpoints,
            task_integration_checkpoints.c.task_id == intent["source_task_id"],
        )
        if (
            source is None
            or source_checkpoint is None
            or source["status"] != "COMPLETED"
            or source["parent_task_id"] != parent["id"]
            or source["repo_id"] != parent["repo_id"]
            or source_checkpoint["checkpoint_sha"] != intent["source_head"]
            or source_checkpoint["repository_id"] != parent["repo_id"]
            or source_checkpoint["branch"] != source["branch_name"]
        ):
            raise RecoveryRefused("completed source or its current head changed")
        review = (
            (
                await conn.execute(
                    select(integration_review_evidence)
                    .where(
                        integration_review_evidence.c.source_task_id == source["id"],
                        integration_review_evidence.c.repository_id == parent["repo_id"],
                        integration_review_evidence.c.source_base == intent["source_base"],
                        integration_review_evidence.c.reviewed_head_sha == intent["source_head"],
                        integration_review_evidence.c.generation == source_checkpoint["generation"],
                    )
                    .order_by(
                        integration_review_evidence.c.created_at.desc(),
                        integration_review_evidence.c.id.desc(),
                    )
                    .limit(1)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            review is None
            or dict(review) != intent["review_evidence"]
            or review["verdict"] != "approved"
        ):
            raise RecoveryRefused("source review changed or is no longer approved")
        origin = await row(
            task_branch_origins,
            task_branch_origins.c.task_id == source["id"],
            task_branch_origins.c.repository_id == parent["repo_id"],
            task_branch_origins.c.retired_at.is_(None),
        )
        if (
            origin is None
            or not origin["materialized"]
            or not origin["reserved"]
            or origin["base_sha"] != intent["source_base"]
            or origin["parent_task_id"] != parent["id"]
            or origin["parent_repository_id"] != parent["repo_id"]
            or origin["parent_ref"] != parent["branch_name"]
        ):
            raise RecoveryRefused("source branch origin changed")
        delegate = await row(tasks, tasks.c.id == stage["repair_task_id"])
        if (
            delegate is None
            or delegate["status"] != "BLOCKED"
            or delegate["created_by_kind"] != "integration_repair"
            or delegate["created_by_id"] != operation["id"]
            or delegate["parent_task_id"] is not None
            or delegate["project_id"] != project_id
            or delegate["repo_id"] != parent["repo_id"]
            or delegate["branch_name"] != parent["branch_name"]
        ):
            raise RecoveryRefused("former repair delegate is not blocked and detached")
        # Owner recovery preserves terminal task attribution. assigned_agent_id
        # may therefore name the stopped writer after its claim was released;
        # live session/claim rows and workspace locks below are the authority.
        task_ids = (parent["id"], delegate["id"], source["id"])
        for task_id in task_ids:
            if await self.db._read_manual_pause(conn, task_id) is not None:
                raise RecoveryRefused(f"manual pause on {task_id}; operator must resolve it first")
        gate = (
            await conn.execute(
                select(gates.c.id)
                .join(task_gates, task_gates.c.gate_id == gates.c.id)
                .where(
                    task_gates.c.task_id.in_(task_ids),
                    gates.c.status == "open",
                )
                .with_for_update()
            )
        ).first()
        if gate:
            raise RecoveryRefused(f"open gate {gate[0]}; resolve the human gate first")
        holders = (
            (
                await conn.execute(
                    select(sessions).where(sessions.c.task_id.in_(task_ids)).with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if any(s["state"] != "stopped" or s["claim_phase"] is not None for s in holders):
            raise RecoveryRefused("a session still holds the parent, source or repair delegate")
        if (
            await conn.execute(
                select(workspaces.c.id)
                .where(workspaces.c.locked_by_task_id.in_(task_ids))
                .with_for_update()
            )
        ).first():
            raise RecoveryRefused("a workspace still holds the parent, source or repair delegate")
        owner = await row(
            integration_branch_owners,
            integration_branch_owners.c.repository_id == parent["repo_id"],
            integration_branch_owners.c.ref == parent["branch_name"],
        )
        if owner is None or owner["session_id"] or owner["workspace_id"]:
            raise RecoveryRefused("branch owner is not detached")
        audit = (
            (
                await conn.execute(
                    select(integration_owner_recoveries)
                    .where(
                        integration_owner_recoveries.c.owner_row_id == owner["id"],
                        integration_owner_recoveries.c.outcome == "preserved_and_released",
                    )
                    .order_by(
                        integration_owner_recoveries.c.created_at.desc(),
                        integration_owner_recoveries.c.id.desc(),
                    )
                    .limit(1)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        release = dict(audit["evidence"]) if audit else {}
        stop = release.get("stop_proof") or {}
        if (
            audit is None
            or audit["task_id"] != delegate["id"]
            or audit["repository_id"] != parent["repo_id"]
            or audit["ref"] != parent["branch_name"]
            or release.get("preserved_sha") != request.candidate_sha
            or not recovery_ref_allowed(
                release.get("preserved_ref"), parent["branch_name"], owner["id"], request.candidate_sha,
            )
            or (not record and owner["confirmed_workspace_id"] != release.get("workspace_id"))
            or not release.get("workspace_id")
            or not release.get("session_id")
            or stop.get("session_id") != release["session_id"]
            or not stop.get("instance_token")
            or stop.get("state") != "stopped"
            or stop.get("desired_state") != "stopped"
            or release.get("released_fence_token") != release.get("fence_token", -2) + 1
        ):
            raise RecoveryRefused(
                "supported owner recovery has no exact preserved candidate and stop proof"
            )
        stopped = await row(sessions, sessions.c.id == release["session_id"])
        if (
            stopped is None
            or stopped["instance_token"] != stop["instance_token"]
            or stopped["state"] != "stopped"
            or stopped["desired_state"] != "stopped"
            or stopped["claim_phase"] is not None
            or stopped["project_id"] != project_id
            or stopped["task_id"] not in {None, delegate["id"]}
        ):
            raise RecoveryRefused("preserved writer was reused or is not stopped")
        if not record:
            if (
                owner["owner_id"] != delegate["id"]
                or owner["owner_role"] != "repair"
                or owner["handoff_state"] != "released"
                or owner["fence_token"] != release["released_fence_token"]
            ):
                raise RecoveryRefused("released repair fence changed")
        elif (
            record["release_id"] != audit["id"]
            or owner["owner_id"] != operation["id"]
            or owner["owner_role"] != "collector"
            or owner["handoff_state"] != "reserved"
            or owner["fence_token"] != record["collector_fence_token"]
            or intent["resolution_head_sha"] != request.candidate_sha
            or intent["resolution_tree_sha"] != record["tree_sha"]
        ):
            raise RecoveryRefused("reserved recovery identity or collector fence changed")
        blockers = await OperationSafety._ambiguous_writes_on(
            conn,
            operation,
            allowed_writer_id=owner["id"] if record else None,
            allowed_promotion_intent_id=request.intent_id if record else None,
        )
        if blockers:
            raise RecoveryRefused("ambiguous external writes: " + ", ".join(blockers))
        return {
            "operation": operation,
            "stage": stage,
            "intent": intent,
            "record": record,
            "owner": owner,
            "audit": dict(audit),
            "release": release,
            "source": source,
            "remaining_attempts": limit - int(stage["attempts"]),
            "receipts": await self.repair._current_receipts_on(conn, operation),
        }

    async def _git_proof(self, proof, repository):
        intent, release = proof["intent"], proof["release"]
        store = repository.retained_git_dir
        if intent["target_branch"].removeprefix("refs/heads/") == repository.repo.default_branch:
            raise RecoveryRefused("parent recovery cannot publish to the default branch")
        refs = {}
        for label, branch in (
            ("target", intent["target_branch"]),
            ("source", proof["source"]["branch_name"]),
            ("preserved", release["preserved_ref"]),
        ):
            observed = await self.promotion.git.als_remote_ref(
                str(store), branch, repository_url=repository.origin_url
            )
            if observed.state is RemoteRefState.ABSENT and label == "preserved":
                refs[label] = None  # Cleanup may have completed before a crash.
            elif observed.state is not RemoteRefState.PRESENT or not observed.oid:
                raise RecoveryRefused(f"origin {label} ref is absent or unreadable")
            else:
                refs[label] = observed.oid
        candidate = release["preserved_sha"]
        allowed = {intent["expected_target"], candidate}
        if refs["target"] not in allowed:
            raise RecoveryRefused("parent target moved from the expected old tip")
        if (refs["source"] != intent["source_head"]
            or (refs["preserved"] != candidate
                and not (refs["preserved"] is None and refs["target"] == candidate))):
            raise RecoveryRefused("source or preserved origin ref moved")
        parents = await self.promotion.git.arun_git_result(
            ["show", "-s", "--format=%P", candidate],
            cwd=str(store),
            lock_held=True,
        )
        expected_parents = [intent["expected_target"], intent["source_head"]]
        if parents.returncode != 0 or parents.stdout.split() != expected_parents:
            raise RecoveryRefused(
                "candidate must have exactly the expected target and frozen source parents"
            )
        tree = await self.promotion._tree_oid(store, candidate)
        commits = await self.promotion._resolution_commit_range(
            store, intent["expected_target"], candidate
        )
        await self.promotion._assert_exact_resolution(
            store,
            intent
            | {
                "resolution_head_sha": candidate,
                "resolution_tree_sha": tree,
                "resolution_commit_shas": commits,
            },
        )
        if proof["record"] and (
            tree != intent["resolution_tree_sha"] or commits != intent["resolution_commit_shas"]
        ):
            raise PromotionInvariantError("reserved candidate tree or commit range changed")
        for receipt in proof["receipts"]:
            if receipt["after_sha"] and not await self.promotion._is_ancestor(
                store, receipt["after_sha"], candidate
            ):
                raise RecoveryRefused(f"candidate loses delivery receipt {receipt['id']}")
        return {
            "tree_sha": tree,
            "parents": expected_parents,
            "repair_commit_shas": commits,
            "remote_head_sha": refs["target"],
            "preserved_ref": release["preserved_ref"],
        }

    async def _record_on(self, conn, proof, record, *, head_sha=None):
        stage = proof["stage"]
        values = {"dossier": dict(stage["dossier"] or {}) | {_RECORD: record}}
        if head_sha:
            values.update(
                current_subject=stage["current_subject"] | {"head_sha": head_sha},
                success_subject=None,
                success_evidence_id=None,
            )
        changed = await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == stage["operation_id"],
                integration_repair_stages.c.ordinal == stage["ordinal"],
                integration_repair_stages.c.state == stage["state"],
                integration_repair_stages.c.current_subject.cast(JSONB) == stage["current_subject"],
                integration_repair_stages.c.dossier.cast(JSONB) == stage["dossier"],
            )
            .values(**values)
        )
        if changed.rowcount != 1:
            raise RuntimeError("preserved repair lost its stage compare-and-swap")

    @staticmethod
    def _public(proof):
        stage, intent = proof["stage"], proof["intent"]
        return {
            "operation_id": stage["operation_id"],
            "stage": stage["ordinal"],
            "intent_id": intent["id"],
            "candidate_sha": (proof.get("release") or {}).get(
                "preserved_sha", intent["resolution_head_sha"]
            ),
            "expected_target": intent["expected_target"],
            "source_head": intent["source_head"],
            "released_fence": (proof.get("release") or proof["record"])["released_fence_token"],
            "deadline_at": stage["deadline_at"],
            "release_id": (proof.get("audit") or {}).get(
                "id", (proof["record"] or {}).get("release_id")
            ),
            "attempts": stage["attempts"],
            "remaining_attempts": proof.get("remaining_attempts"),
        }
