"""Return a settled red aggregate to its existing collection episode."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.queries.task_queries import TERMINAL_BLOCKED_META_KEY
from src.database.tables import (
    archived_tasks,
    events,
    gates,
    integration_branch_owners,
    integration_check_evidence,
    integration_candidate_ref_mutations,
    integration_parent_episodes,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    projects,
    repos,
    sessions,
    task_completion_records,
    task_delivery_receipts,
    task_gates,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)
from src.integration.cancelled_collection_recovery import (
    CancelledCollectionRecovery,
    Dispatch,
    _Changed,
    _ProofFailed,
)
from src.integration.models import BranchKey, Fence
from src.integration.verifier_subject import latest_red_parent_evidence, verifier_subject_on
from src.models import TaskStatus

RECOVERY_EVENT = "integration.failed_verification_collection_reopened"
FAILED_AGGREGATE_META_KEY = "integration_failed_aggregate"


class FailedVerificationRecovery(CancelledCollectionRecovery):
    """Reuse the collection remote proof and transactional dry-run/apply protocol."""

    def __init__(
        self, db: Any, promotion_service: Any, *, dispatch: Dispatch | None = None,
        clock: Callable[[], float] = time.time,
        confirm_handoff: Callable[[dict], Awaitable[bool]] | None = None,
    ) -> None:
        super().__init__(db, promotion_service, dispatch=dispatch, clock=clock)
        self.confirm_handoff = confirm_handoff
        self._previous_attachment = None

    async def _prepare_apply(self, task_id, facts):
        self._previous_attachment = None
        if (facts["ci_failure"] is None
                or facts["owner"]["handoff_state"] not in {"attached", "handoff_pending"}):
            return None, facts
        if self.confirm_handoff is None:
            return {"outcome": "blocked",
                    "reason": "server-side verifier stop/detach proof is unavailable"}, None
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, facts["project_id"])
            refusal, current = await self._facts_on(conn, task_id, lock=True)
            if refusal is not None or current["fingerprint"] != facts["fingerprint"]:
                return {"outcome": "changed",
                        "reason": "collection state changed; repeat the dry run"}, None
        from src.integration.ownership import BranchBusy, BranchOwnership, StaleFence

        attachment = dict(facts["owner"])
        try:
            await BranchOwnership(self.db, confirm_handoff=self.confirm_handoff).confirm_transfer(
                Fence(target=facts["target"], owner_id=attachment["owner_id"],
                      token=attachment["fence_token"])
            )
        except (BranchBusy, StaleFence) as exc:
            return {"outcome": "blocked", "reason": str(exc)}, None
        # Handoff can change the task/claim/fence. Diagnose again rather than
        # accepting any of the earlier facts as settlement or remote proof.
        diagnosis, refreshed = await self._diagnose(task_id)
        if diagnosis["outcome"] != "would_reopen":
            return diagnosis, None
        self._previous_attachment = attachment
        return None, refreshed

    async def _diagnose(self, task_id):
        async with self.db._engine.connect() as conn:
            refusal, facts = await self._facts_on(conn, task_id)
        if refusal is not None:
            return refusal, facts
        try:
            head = await self._prove(facts)
        except _ProofFailed as exc:
            return {
                **facts["report"],
                "outcome": "blocked",
                "reason": exc.reason,
                "remote_head_sha": exc.remote_head,
            }, facts
        return {
            **facts["report"],
            "outcome": "would_reopen",
            "head_sha": head,
            "remote_head_sha": head,
            "reason": "collect additional fixes in the same episode after failed verification",
        }, facts

    async def _facts_on(self, conn, task_id, *, lock=False):
        def guarded(statement):
            return statement.with_for_update() if lock else statement

        async def row(table, *conditions):
            return (
                (await conn.execute(guarded(select(table).where(*conditions))))
                .mappings()
                .one_or_none()
            )

        parent = await row(tasks, tasks.c.id == task_id)
        report = {"task_id": task_id, "kind": "failed_verification_collection", "stage": None}

        def refuse(reason, outcome="blocked"):
            return {**report, "outcome": outcome, "reason": reason}, None

        if parent is None:
            return refuse("parent not found", "not_found")
        report.update(project_id=parent["project_id"], branch=parent["branch_name"])
        project = await row(projects, projects.c.id == parent["project_id"])
        checkpoint = await row(
            task_integration_checkpoints, task_integration_checkpoints.c.task_id == task_id
        )
        if (
            checkpoint is None
            or checkpoint["state"] not in {"verifying", "integration_ready"}
            or not checkpoint["episode_id"]
        ):
            return refuse("the parent has no failed aggregate verification", "not_eligible")
        report["checkpoint"] = dict(checkpoint)
        if (
            project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
            or project["hierarchical_integration_draining"]
            or project["status"] != "ACTIVE"
        ):
            return refuse("the project is not actively collecting")
        if parent["status"] != "PAUSED" or parent["assigned_agent_id"] is not None:
            return refuse("the parent is not an unassigned PAUSED collector")
        repo = await row(repos, repos.c.id == parent["repo_id"])
        branch = parent["branch_name"]
        if (
            repo is None
            or repo["project_id"] != parent["project_id"]
            or not branch
            or repo["id"] != project["integration_repository_id"]
            or checkpoint["repository_id"] != repo["id"]
            or checkpoint["branch"] != branch
            or branch.removeprefix("refs/heads/") == repo["default_branch"]
        ):
            return refuse("the parent's canonical branch identity is inconsistent")
        episode = await row(
            integration_parent_episodes,
            integration_parent_episodes.c.id == checkpoint["episode_id"],
            integration_parent_episodes.c.parent_task_id == task_id,
            integration_parent_episodes.c.repository_id == repo["id"],
        )
        operation = await row(
            integration_repair_operations,
            integration_repair_operations.c.parent_task_id == task_id,
            integration_repair_operations.c.episode_id == checkpoint["episode_id"],
        )
        if (
            episode is None
            or operation is None
            or operation["target_kind"] != "parent"
            or operation["state"] not in {"active", "escalated"}
            or checkpoint["generation"] < episode["generation"]
        ):
            return refuse("the current episode has no live collection operation")
        verifier_id = operation["verifier_task_id"]
        report.update(operation_id=operation["id"], episode_id=episode["id"])
        if not verifier_id:
            return refuse("the collection has no bound aggregate verifier")
        verifier = await row(tasks, tasks.c.id == verifier_id)
        if verifier is None:
            verifier = await row(archived_tasks, archived_tasks.c.id == verifier_id)
        failure = (
            (
                await conn.execute(
                    guarded(
                        select(task_completion_records)
                        .where(task_completion_records.c.task_id == verifier_id)
                        .order_by(
                            task_completion_records.c.completed_at.desc(),
                            task_completion_records.c.id,
                        )
                        .limit(1)
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        commits_valid = True
        try:
            commits = json.loads(failure["commits"]) if failure else []
        except (TypeError, ValueError):
            commits_valid = False
            commits = []
        if not isinstance(commits, list):
            commits_valid = False
            commits = []
        settled_failure = not (
            verifier is None
            or verifier["status"] not in {"FAILED", "BLOCKED"}
            or verifier["project_id"] != parent["project_id"]
            or verifier["branch_name"] != branch
            or verifier["repo_id"] != repo["id"]
            or verifier.get("assigned_agent_id") is not None
            or failure is None
            or failure["outcome"] != "fail"
            or failure["completed_at"] < episode["created_at"]
            or failure["branch"] != branch
            or not commits_valid
        )
        failure_subject = None
        ci_failure = None
        if not settled_failure:
            if (verifier is None or verifier["status"] not in {"IN_PROGRESS", "PAUSED"}
                    or verifier["project_id"] != parent["project_id"]
                    or verifier["branch_name"] != branch or verifier["repo_id"] != repo["id"]):
                return refuse("the verifier has no settled failed completion for this exact head")
            try:
                failure_subject = await verifier_subject_on(
                    conn, verifier_id=verifier_id, project_id=parent["project_id"],
                    operation=operation, checkpoint=checkpoint, lock=lock,
                )
            except ValueError as exc:
                return refuse(str(exc), "ambiguous")
            if (failure_subject is None
                    or failure_subject["subject"]["generation"] != checkpoint["generation"]):
                return refuse("the held verifier has no immutable subject for this generation")
            evidence = (
                await conn.execute(guarded(select(integration_check_evidence).where(
                    integration_check_evidence.c.operation_id == operation["id"],
                    integration_check_evidence.c.parent_task_id == task_id,
                    integration_check_evidence.c.parent_generation == checkpoint["generation"],
                    integration_check_evidence.c.parent_head_sha == checkpoint["checkpoint_sha"],
                ).order_by(integration_check_evidence.c.observed_at.desc(),
                           integration_check_evidence.c.id)))
            ).mappings().all()
            # A later green/inconclusive observation cannot be ignored in favor
            # of an old failure. All selected evidence remains immutable.
            latest = latest_red_parent_evidence(
                evidence, operation=operation, checkpoint=checkpoint
            )
            if latest is None or latest["observed_at"] < failure_subject["created_at"]:
                return refuse("the held verifier has no trusted red CI for this exact subject")
            ci_failure = dict(latest)
            failure = None
        elif checkpoint["checkpoint_sha"] not in commits:
            if commits:
                return refuse("the failed completion names a different head")
            try:
                failure_subject = await verifier_subject_on(
                    conn, verifier_id=verifier_id, project_id=parent["project_id"],
                    operation=operation, checkpoint=checkpoint, lock=lock,
                )
            except ValueError as exc:
                return refuse(str(exc), "ambiguous")
            if (
                failure_subject is None
                or failure_subject["created_at"] > failure["completed_at"]
            ):
                return refuse("the empty failed completion has no immutable subject for this head")
        report["delegates"] = [
            {
                "task_id": verifier_id,
                "status": verifier["status"],
                "failure_completion_id": failure["id"] if failure else None,
                "failure_subject": failure_subject,
                "ci_failure_evidence_id": ci_failure["id"] if ci_failure else None,
            }
        ]
        subject_holds = {}
        for subject in (task_id, verifier_id):
            holds = dict(
                (
                    await conn.execute(
                        guarded(
                            select(task_metadata.c.key, task_metadata.c.value).where(
                                task_metadata.c.task_id == subject,
                                task_metadata.c.key.in_(
                                    ("manual_pause", TERMINAL_BLOCKED_META_KEY)
                                ),
                            )
                        )
                    )
                ).all()
            )
            subject_holds[subject] = holds
            terminal = holds.get(TERMINAL_BLOCKED_META_KEY)
            try:
                terminal = json.loads(terminal) if terminal else None
            except (TypeError, ValueError):
                pass
            # A worker hard-failure close is BLOCKED in the current state machine.
            # Keep that failure marker/budget intact; it is not a manual rollout hold.
            failed_close = (
                subject == verifier_id
                and verifier["status"] == "BLOCKED"
                and terminal in ("session_close_hard_failure", "max_retries")
            )
            if (
                "manual_pause" in holds
                or (terminal is not None and not failed_close)
                or subject == verifier_id
                and verifier["status"] == "BLOCKED"
                and not failed_close
            ):
                return refuse(f"{subject} has an operator or terminal hold")
            if await self._live_holder_on(conn, subject) and not (
                subject == verifier_id and ci_failure is not None
            ):
                return refuse(f"a session or workspace still holds {subject}")
        stages = [
            dict(s)
            for s in (
                await conn.execute(
                    guarded(
                        select(integration_repair_stages)
                        .where(integration_repair_stages.c.operation_id == operation["id"])
                        .order_by(integration_repair_stages.c.ordinal)
                    )
                )
            ).mappings()
        ]
        settled_repair_ids = set()
        for stage in stages:
            if stage["state"] not in {"passed", "failed", "expired", "cancelled"}:
                return refuse(f"repair stage {stage['ordinal']} is still {stage['state']}")
            delegate_id = stage["repair_task_id"]
            if delegate_id:
                delegate = await row(tasks, tasks.c.id == delegate_id)
                if (
                    delegate is not None
                    and (
                        delegate["status"] not in {"COMPLETED", "FAILED"}
                        or delegate["assigned_agent_id"] is not None
                    )
                    or await self._live_holder_on(conn, delegate_id)
                ):
                    return refuse(f"repair delegate {delegate_id} is not settled")
                if delegate is not None:
                    settled_repair_ids.add(delegate_id)
        owner = await row(
            integration_branch_owners,
            integration_branch_owners.c.repository_id == repo["id"],
            integration_branch_owners.c.ref == branch,
        )
        attached_verifier = bool(
            ci_failure is not None and owner is not None
            and owner["owner_role"] == "verifier" and owner["owner_id"] == verifier_id
            and owner["handoff_state"] in {"attached", "handoff_pending"}
            and owner["session_id"] is not None and owner["workspace_id"] is not None
        )
        if not attached_verifier and (
            owner is None
            or owner["session_id"] is not None
            or owner["workspace_id"] is not None
            or owner["handoff_state"] not in {"released", "reserved"}
            or (owner["owner_role"], owner["owner_id"])
            not in {("verifier", verifier_id), ("collector", operation["id"])}
            | {("repair", delegate_id) for delegate_id in settled_repair_ids}
        ):
            return refuse("the collection or failed verifier does not hold a detached fence")
        if ci_failure is not None:
            if (owner["owner_id"] != verifier_id or owner["owner_role"] != "verifier"
                    or owner["fence_token"] != failure_subject["subject"]["expected_token"] + 1):
                return refuse("the held verifier fence differs from its immutable handoff")
            if not attached_verifier and await self._live_holder_on(conn, verifier_id):
                return refuse(f"a session or workspace still holds {verifier_id}")
            if attached_verifier:
                extra_session = await conn.scalar(guarded(select(sessions.c.id).where(
                    sessions.c.task_id == verifier_id,
                    (sessions.c.state != "stopped") | sessions.c.claim_phase.is_not(None),
                    sessions.c.id != owner["session_id"],
                ).limit(1)))
                extra_workspace = await conn.scalar(guarded(select(workspaces.c.id).where(
                    workspaces.c.locked_by_task_id == verifier_id,
                    workspaces.c.id != owner["workspace_id"],
                ).limit(1)))
                if extra_session or extra_workspace:
                    return refuse("the held verifier has another session or workspace holder")
        report["owner"] = dict(owner)
        from src.integration.recovery_controls import IntegrationRecoveryControls

        ambiguous = await IntegrationRecoveryControls._ambiguous_writes_on(
            conn, operation, allowed_writer_id=owner["id"]
        )
        unsettled = await conn.scalar(
            select(integration_promotion_intents.c.id)
            .where(
                integration_promotion_intents.c.repository_id == repo["id"],
                integration_promotion_intents.c.target_branch == branch,
                integration_promotion_intents.c.state.not_in(("committed", "superseded")),
            )
            .limit(1)
        )
        mutation = await conn.scalar(
            select(integration_candidate_ref_mutations.c.id)
            .where(
                integration_candidate_ref_mutations.c.repository_id == repo["id"],
                integration_candidate_ref_mutations.c.branch == branch,
                integration_candidate_ref_mutations.c.state == "reserved",
            )
            .limit(1)
        )
        if ambiguous or unsettled or mutation:
            return refuse("an external mutation or conflict is unresolved", "ambiguous")
        receipts = [
            dict(r)
            for r in (
                await conn.execute(
                    select(task_delivery_receipts)
                    .where(
                        task_delivery_receipts.c.parent_operation_id == operation["id"],
                        task_delivery_receipts.c.parent_episode_id == episode["id"],
                    )
                    .order_by(task_delivery_receipts.c.created_at, task_delivery_receipts.c.id)
                )
            ).mappings()
        ]
        # Require new work; never dispatch another verifier on the known red tree alone.
        children = (
            await conn.execute(
                select(tasks.c.id, task_integration_checkpoints.c.checkpoint_sha)
                .join(
                    task_integration_checkpoints,
                    task_integration_checkpoints.c.task_id == tasks.c.id,
                )
                .where(
                    tasks.c.parent_task_id == task_id,
                    tasks.c.status == "COMPLETED",
                    tasks.c.repo_id == repo["id"],
                )
            )
        ).all()
        pending = [
            (child, head)
            for child, head in children
            if head
            and not any(
                r["source_task_id"] == child and r["reviewed_head_sha"] == head for r in receipts
            )
        ]
        if not pending:
            return refuse("there is no completed additional child fix awaiting a receipt")
        report["receipts"] = [
            {key: r[key] for key in ("id", "source_task_id", "reviewed_head_sha", "after_sha")}
            for r in receipts
        ]
        report["human_gates"] = list(
            (
                await conn.execute(
                    select(gates.c.id)
                    .join(task_gates, task_gates.c.gate_id == gates.c.id)
                    .where(task_gates.c.task_id == task_id, gates.c.status == "open")
                    .order_by(gates.c.id)
                )
            ).scalars()
        )
        report["recorded_head_sha"] = checkpoint["checkpoint_sha"]
        # Preserve full budgets and failure evidence in the CAS and audit, without resetting any.
        facts: dict[str, Any] = {
            "report": report,
            "project_id": parent["project_id"],
            "parent": dict(parent),
            "operation": dict(operation),
            "episode": dict(episode),
            "checkpoint": dict(checkpoint),
            "owner": dict(owner),
            "stages": stages,
            "failure": dict(failure) if failure else None,
            "ci_failure": ci_failure,
            "failure_subject": failure_subject,
            "verifier": dict(verifier),
            "holds": subject_holds,
            "receipts": receipts,
            "pending": sorted(pending),
            "expected_tip": checkpoint["checkpoint_sha"],
            "target": BranchKey(repository_id=repo["id"], branch=branch),
            "policy_generation": project["hierarchical_integration_generation"],
        }
        facts["fingerprint"] = json.dumps(
            {k: v for k, v in facts.items() if k != "target"}, sort_keys=True, default=str
        )
        return None, facts

    async def _apply_on(self, conn, facts, *, now, head_sha, reason, operator_id):
        from src.integration.ownership import BranchBusy, BranchOwnership, StaleFence

        # Re-read Git under the locked durable facts before transferring authority.
        try:
            if await self._prove(facts) != head_sha:
                raise _Changed()
        except _ProofFailed as exc:
            raise _Changed() from exc
        owner, operation = facts["owner"], facts["operation"]
        transition = None
        if facts["ci_failure"] is not None:
            # Never settle from a database unlock alone. The handoff must have
            # proved the old process/claim/checkout detached before this CAS.
            if (owner["session_id"] is not None or owner["workspace_id"] is not None
                    or owner["handoff_state"] not in {"reserved", "released"}
                    or await self._live_holder_on(conn, facts["verifier"]["id"])):
                raise _Changed()
            evidence = facts["ci_failure"]
            completion = {
                "id": "red-verifier-" + uuid.uuid5(
                    uuid.NAMESPACE_URL, facts["verifier"]["id"] + ":" + evidence["id"]
                ).hex,
                "task_id": facts["verifier"]["id"], "outcome": "fail",
                "branch": facts["parent"]["branch_name"], "commits": json.dumps([head_sha]),
                "summary": f"Trusted aggregate CI failed: {evidence['id']} (run {evidence['run_id']})",
                "verification": json.dumps(evidence), "completed_at": now,
            }
            await conn.execute(insert(task_completion_records).values(**completion))
            transition = await self.db._apply_transition(
                conn, facts["verifier"]["id"], TaskStatus.FAILED, force=True,
                context="integration_verifier_ci_failed",
                extra_where=tasks.c.claim_epoch == facts["verifier"]["claim_epoch"],
                # The confirmed pool handoff pauses its claim. Metadata holds
                # were separately checked under the same locked task row.
                _manual_pause_control=True,
                returning=True,
                assigned_agent_id=None, resume_after=None,
            )
            if transition.row is None:
                raise _Changed()
            facts["failure"] = completion
        ownership = BranchOwnership(self.db, clock=self.clock)
        try:
            if owner["handoff_state"] == "released":
                fence = await ownership.acquire(
                    facts["target"], operation["id"], "collector", conn=conn
                )
            else:
                fence = await ownership.transfer_detached_on(
                    conn,
                    Fence(
                        target=facts["target"],
                        owner_id=owner["owner_id"],
                        token=int(owner["fence_token"]),
                    ),
                    operation["id"],
                    "collector",
                )
        except (BranchBusy, StaleFence) as exc:
            raise _Changed() from exc
        checkpoint = facts["checkpoint"]
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == facts["parent"]["id"])
            .values(
                state="awaiting_children",
                generation=checkpoint["generation"] + 1,
                version=checkpoint["version"] + 1,
                branch_owner_id=operation["id"],
                verified_sha=None,
                verified_generation=None,
                current_verification_id=None,
                updated_at=now,
            )
        )
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == operation["id"])
            .values(verifier_task_id=None, updated_at=now)
        )
        await conn.execute(
            pg_insert(task_metadata)
            .values(
                task_id=facts["parent"]["id"],
                key=FAILED_AGGREGATE_META_KEY,
                value=json.dumps(
                    {
                        "operation_id": operation["id"],
                        "episode_id": facts["episode"]["id"],
                        "head_sha": head_sha,
                    }
                ),
            )
            .on_conflict_do_update(
                index_elements=[task_metadata.c.task_id, task_metadata.c.key],
                set_={
                    "value": json.dumps(
                        {
                            "operation_id": operation["id"],
                            "episode_id": facts["episode"]["id"],
                            "head_sha": head_sha,
                        }
                    )
                },
            )
        )
        await conn.execute(
            insert(events).values(
                event_type=RECOVERY_EVENT,
                project_id=facts["project_id"],
                task_id=facts["parent"]["id"],
                timestamp=now,
                payload=json.dumps(
                    {
                        "operator_id": operator_id,
                        "reason": reason,
                        "head_sha": head_sha,
                        "operation_id": operation["id"],
                        "episode_id": facts["episode"]["id"],
                        "previous_verifier_task_id": operation["verifier_task_id"],
                        "failure_completion_id": facts["failure"]["id"],
                        "failure_subject": facts["failure_subject"],
                        "ci_failure_evidence_id": (
                            facts["ci_failure"]["id"] if facts["ci_failure"] else None
                        ),
                        "previous_attachment": getattr(self, "_previous_attachment", None),
                        "previous_generation": checkpoint["generation"],
                        "generation": checkpoint["generation"] + 1,
                        "previous_owner": owner,
                        "collector_fence_token": fence.token,
                        "stages": facts["stages"],
                        "receipts": [r["id"] for r in facts["receipts"]],
                        "pending_children": facts["pending"],
                        "human_gates": facts["report"]["human_gates"],
                    }
                ),
            )
        )
        return transition, {"fence_token": fence.token, "stage": None}
