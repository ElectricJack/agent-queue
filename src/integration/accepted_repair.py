"""Terminal bookkeeping for an accepted root repair whose writer already handed off."""

from __future__ import annotations

from sqlalchemy import select, update

from src.claim_file import remove_claim_file_if_matches
from src.commands.principal import PrincipalKind, current_principal, matches_session_instance
from src.database.tables import (
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_resolutions,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    sessions,
    tasks,
    workspaces,
)
from src.models import TaskCompletion, TaskStatus


async def accepted_candidate_on(conn, operation, stage, *, completing=False):
    """Prove the *current* accepted candidate and its detached collector fence.

    Callers hold the project hierarchy lock. This deliberately grants no writer
    authority and does not treat acceptance as CI evidence.
    """
    if (
        operation["target_kind"] != "batch"
        or operation["state"]
        not in ({"active", "escalated", "completed"} if completing else {"active", "escalated"})
        or (
            operation["active_stage"] < stage["ordinal"]
            if completing
            else operation["active_stage"] != stage["ordinal"]
        )
        or stage["state"]
        not in (
            {"active", "awaiting_completion", "expired", "passed"}
            if completing
            else {"active", "awaiting_completion"}
        )
        or stage["writer_kind"] != "repair_delegate"
    ):
        return None
    batch = (
        (
            await conn.execute(
                select(integration_batches)
                .where(
                    integration_batches.c.id == operation["batch_id"],
                )
                .with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    if batch is None or batch["lifecycle"] not in (
        {"building", "testing", "promoting", "promoted"} if completing else {"building", "testing"}
    ):
        return None
    revision = (
        (
            await conn.execute(
                select(integration_candidate_revisions)
                .where(
                    integration_candidate_revisions.c.batch_id == batch["id"],
                    integration_candidate_revisions.c.revision == batch["current_revision"],
                )
                .with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        revision is None
        or revision["state"]
        not in (
            {"built", "testing", "green", "promoted"}
            if completing
            else {"built", "testing", "green"}
        )
        or stage["current_subject"]
        != {
            "kind": "batch",
            "revision": revision["revision"],
            "candidate_sha": revision["head_sha"],
        }
    ):
        return None
    reservations = (
        (
            await conn.execute(
                select(integration_candidate_resolutions)
                .where(
                    integration_candidate_resolutions.c.operation_id == operation["id"],
                    integration_candidate_resolutions.c.stage_ordinal == stage["ordinal"],
                    integration_candidate_resolutions.c.repair_task_id == stage["repair_task_id"],
                    integration_candidate_resolutions.c.batch_id == batch["id"],
                    integration_candidate_resolutions.c.revision == revision["revision"],
                    integration_candidate_resolutions.c.resolved_head_sha == revision["head_sha"],
                    integration_candidate_resolutions.c.state == "accepted",
                )
                .with_for_update()
            )
        )
        .mappings()
        .all()
    )
    if len(reservations) != 1:
        return None
    proof = dict(reservations[0])
    if (
        proof["operation_episode_id"] != operation["episode_id"]
        or proof["stage_deadline_at"] != stage["deadline_at"]
        or proof["project_id"] != batch["project_id"]
        or proof["repository_id"] != batch["repository_id"]
        or proof["branch"] != batch["integration_branch"]
        or proof["fence_owner_id"] != stage["repair_task_id"]
        or proof["handoff_owner_id"] != operation["id"]
        or proof["handoff_fence_token"] != proof["fence_token"] + 1
        or (proof["push_evidence"] or {}).get("remote_sha") != proof["resolved_head_sha"]
        or (proof["push_evidence"] or {}).get("target_branch") != proof["target_branch"]
    ):
        return None
    member = (
        (
            await conn.execute(
                select(integration_candidate_member_results).where(
                    integration_candidate_member_results.c.batch_id == proof["batch_id"],
                    integration_candidate_member_results.c.revision == proof["revision"],
                    integration_candidate_member_results.c.member_ordinal
                    == proof["member_ordinal"],
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    owner = (
        (
            await conn.execute(
                select(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == proof["repository_id"],
                    integration_branch_owners.c.ref == proof["branch"],
                )
                .with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    collector = bool(
        owner is not None
        and owner["owner_id"] == proof["handoff_owner_id"]
        and owner["fence_token"] == proof["handoff_fence_token"]
        and owner["owner_role"] == "collector"
        and owner["handoff_state"] == "reserved"
        and owner["session_id"] is None
        and owner["workspace_id"] is None
    )
    successor = None
    if completing and operation["active_stage"] > stage["ordinal"] and owner is not None:
        successor = (
            (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.operation_id == operation["id"],
                        integration_repair_stages.c.ordinal == operation["active_stage"],
                        integration_repair_stages.c.writer_kind == "repair_delegate",
                        integration_repair_stages.c.state.in_(
                            ("active", "awaiting_completion", "passed")
                        ),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    successor_matches = bool(
        successor is not None
        and owner["owner_id"] == successor["repair_task_id"]
        and owner["owner_role"] == "repair"
        and owner["handoff_state"] in {"attached", "reserved"}
        and owner["fence_token"] > proof["handoff_fence_token"]
        and successor["current_subject"] == stage["current_subject"]
    )
    # An older accepted delegate can outlive the debug successor. Follow the
    # successor's recorded exact-green handoff, never just any newer fence.
    returned = False
    if (
        successor is not None
        and owner["owner_id"] == operation["id"]
        and owner["owner_role"] == "collector"
        and owner["handoff_state"] == "reserved"
        and owner["session_id"] is None
        and owner["workspace_id"] is None
        and successor["current_subject"] == stage["current_subject"]
        and successor["success_subject"] == stage["current_subject"]
        and successor["success_evidence_id"] == revision["ci_evidence_id"]
        and revision["ci_evidence_id"] == batch["ci_evidence_id"]
        and revision["head_sha"] == batch["tested_candidate_sha"]
        and revision["state"] in {"green", "promoted"}
    ):
        successor_task = (
            (
                await conn.execute(
                    select(tasks).where(
                        tasks.c.id == successor["repair_task_id"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        returned = bool(
            successor_task is not None
            and successor_task["status"] == TaskStatus.COMPLETED.value
            and successor_task["assigned_agent_id"] is None
            and any(
                h.get("task_id") == successor["repair_task_id"]
                and h.get("to_owner_id") == owner["owner_id"]
                and h.get("to_fence") == owner["fence_token"]
                and h.get("from_fence", -1) > proof["handoff_fence_token"]
                and h.get("to_fence") == h.get("from_fence", -1) + 1
                and h.get("candidate_sha") == proof["resolved_head_sha"]
                and h.get("revision") == proof["revision"]
                and h.get("evidence_id") == revision["ci_evidence_id"]
                for h in (successor["dossier"] or {}).get("green_handoffs", [])
            )
        )
    if returned:
        green = (
            await conn.execute(
                select(integration_check_evidence.c.id).where(
                    integration_check_evidence.c.id == revision["ci_evidence_id"],
                    integration_check_evidence.c.operation_id == operation["id"],
                    integration_check_evidence.c.batch_id == proof["batch_id"],
                    integration_check_evidence.c.candidate_revision == proof["revision"],
                    integration_check_evidence.c.conclusion == "success",
                    integration_check_evidence.c.classification == "conclusive",
                )
            )
        ).first()
        returned = green is not None
    if operation["state"] == "completed":
        promoted = (
            await conn.execute(
                select(integration_promotion_intents.c.id).where(
                    integration_promotion_intents.c.intent_kind == "root",
                    integration_promotion_intents.c.operation_key == operation["id"],
                    integration_promotion_intents.c.root_batch_id == proof["batch_id"],
                    integration_promotion_intents.c.root_candidate_revision == proof["revision"],
                    integration_promotion_intents.c.prepared_sha == proof["resolved_head_sha"],
                    integration_promotion_intents.c.ci_evidence_id == revision["ci_evidence_id"],
                    integration_promotion_intents.c.state == "committed",
                )
            )
        ).first()
        if (
            promoted is None
            or batch["lifecycle"] != "promoted"
            or revision["state"] != "promoted"
            or batch["final_main_sha"] != proof["resolved_head_sha"]
        ):
            return None
    if (
        member is None
        or member["result"] != "applied"
        or member["generated_squash_sha"] != proof["resolved_head_sha"]
        or (member["conflict_evidence"] or {}).get("accepted_reservation_id") != proof["id"]
        or not (collector or successor_matches or returned)
    ):
        return None
    mutations = (
        (
            await conn.execute(
                select(integration_candidate_ref_mutations).where(
                    integration_candidate_ref_mutations.c.resolution_id == proof["id"],
                    integration_candidate_ref_mutations.c.purpose.in_(
                        ("repair_resolution", "repair_handoff")
                    ),
                )
            )
        )
        .mappings()
        .all()
    )
    for purpose, fence, branch in (
        ("repair_resolution", proof["fence_token"], proof["target_branch"]),
        ("repair_handoff", proof["handoff_fence_token"], proof["branch"]),
    ):
        matching = [row for row in mutations if row["purpose"] == purpose]
        if len(matching) != 1:
            return None
        row = matching[0]
        if (
            row["state"] != "applied"
            or row["operation_id"] != operation["id"]
            or row["operation_stage"] != stage["ordinal"]
            or row["operation_episode_id"] != proof["operation_episode_id"]
            or row["repository_id"] != proof["repository_id"]
            or row["branch"] != proof["branch"]
            or row["branch_owner_id"]
            != (
                proof["fence_owner_id"]
                if purpose == "repair_resolution"
                else proof["handoff_owner_id"]
            )
            or row["branch_owner_role"]
            != ("repair" if purpose == "repair_resolution" else "collector")
            or row["batch_id"] != proof["batch_id"]
            or row["revision"] != proof["revision"]
            or row["branch_fence_token"] != fence
            or row["target_branch"] != branch
            or row["desired_sha"] != proof["resolved_head_sha"]
            or row["remote_sha"] != proof["resolved_head_sha"]
        ):
            return None
    # Failed/inconclusive observations do not become an unlimited CI retry.
    # A later exact green is handled by the ordinary success-evidence guard.
    failed = (
        await conn.execute(
            select(integration_check_evidence.c.id)
            .where(
                integration_check_evidence.c.operation_id == operation["id"],
                integration_check_evidence.c.candidate_revision == proof["revision"],
                integration_check_evidence.c.batch_id == proof["batch_id"],
                integration_check_evidence.c.conclusion != "success",
            )
            .limit(1)
        )
    ).first()
    if failed is not None and revision["state"] not in {"green", "promoted"}:
        return None
    if not completing:
        # Pending CI belongs to an actually published exact candidate, not an
        # abandoned reservation or a construction that never reached CI.
        published = (
            await conn.execute(
                select(integration_candidate_publications).where(
                    integration_candidate_publications.c.batch_id == proof["batch_id"],
                    integration_candidate_publications.c.revision == proof["revision"],
                    integration_candidate_publications.c.head_sha == proof["resolved_head_sha"],
                    integration_candidate_publications.c.state == "pr_published",
                )
            )
        ).first()
        if published is None:
            return None
    return proof


async def complete_accepted_delegate(
    db,
    task_id,
    *,
    session_id,
    claim_epoch,
    commit="",
    skip_open_subtasks=False,
    recover_stopped=False,
):
    """Close only the original uninterrupted claim; leave CI and ownership alone."""
    principal = current_principal()
    if not session_id or (
        not recover_stopped
        and (
            principal is None
            or principal.kind != PrincipalKind.SESSION
            or principal.session_id != session_id
        )
    ):
        return None
    transition = None
    async with db.immediate() as conn:
        project_id = (
            await conn.execute(
                select(tasks.c.project_id).where(
                    tasks.c.id == task_id,
                )
            )
        ).scalar_one_or_none()
        if project_id is None:
            return None
        await db.lock_hierarchy_project(conn, project_id)
        stages = (
            (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.repair_task_id == task_id,
                        integration_repair_stages.c.writer_kind == "repair_delegate",
                    )
                )
            )
            .mappings()
            .all()
        )
        if len(stages) != 1:
            return None
        operation = (
            (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(
                        integration_repair_operations.c.id == stages[0]["operation_id"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        stage = (
            (
                await conn.execute(
                    select(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.operation_id == operation["id"],
                        integration_repair_stages.c.ordinal == stages[0]["ordinal"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        session = (
            (
                await conn.execute(
                    select(sessions)
                    .where(
                        sessions.c.id == session_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        proof = await accepted_candidate_on(conn, operation, stage, completing=True)
        if proof is None:
            return None
        task = (
            (
                await conn.execute(
                    select(tasks)
                    .where(
                        tasks.c.id == task_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        workspace = (
            (
                await conn.execute(
                    select(workspaces)
                    .where(
                        workspaces.c.id == proof["repair_workspace_id"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            session is None
            or session["task_id"] != task_id
            or session["id"] != proof["repair_session_id"]
            or session["instance_token"] != proof["repair_session_instance_token"]
            or (
                not recover_stopped
                and not matches_session_instance(principal, session["instance_token"])
            )
            or (
                recover_stopped
                and (session["state"] != "stopped" or session["desired_state"] != "stopped")
            )
            or session["project_id"] != project_id
            or proof["project_id"] != project_id
            or session["state"] not in {"starting", "running", "draining", "stopped"}
            or session["last_claim_epoch"] != task["claim_epoch"]
            or claim_epoch != task["claim_epoch"]
            or session["claim_phase"] != "active"
            or session["claim_phase_at"] is None
            or session["claim_phase_at"] > proof["created_at"]
            or workspace is None
            or workspace["locked_by_task_id"] != task_id
            or workspace["locked_by_agent_id"] != session["agent_id"]
            or task["assigned_agent_id"] not in {None, session["agent_id"]}
            or workspace["project_id"] != project_id
            or not workspace["enabled"]
            or workspace["workspace_path"] != proof["repair_workspace_path"]
            or session["work_dir"] != proof["repair_workspace_path"]
            or task["repo_id"] != proof["repository_id"]
            or str(task["branch_name"] or "").removeprefix("refs/heads/")
            != proof["branch"].removeprefix("refs/heads/")
            or commit
            and commit != proof["resolved_head_sha"]
        ):
            return None
        receipt = {
            "reservation_id": proof["id"],
            "completion_id": f"accepted-repair:{proof['id']}:{claim_epoch}",
            "task_id": task_id,
            "session_id": session_id,
            "instance_token": session["instance_token"],
            "claim_epoch": claim_epoch,
            "claim_phase_at": session["claim_phase_at"],
            "workspace_id": workspace["id"],
            "operation_id": operation["id"],
            "stage": stage["ordinal"],
            "revision": proof["revision"],
            "head_sha": proof["resolved_head_sha"],
            "branch": proof["branch"],
            "successor_owner_id": proof["handoff_owner_id"],
            "successor_fence_token": proof["handoff_fence_token"],
        }
        dossier = dict(stage["dossier"] or {})
        if task["status"] == TaskStatus.COMPLETED.value:
            if dossier.get("accepted_delegate_completion") != receipt:
                return None
        elif task["status"] in {TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value}:
            transition = await db._apply_transition(
                conn,
                task_id,
                TaskStatus.COMPLETED,
                context="integration_accepted_repair_delegate_closed",
                assigned_agent_id=None,
                expect_claim_epoch=claim_epoch,
                skip_open_subtasks=skip_open_subtasks,
            )
            dossier["accepted_delegate_completion"] = receipt
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation["id"],
                    integration_repair_stages.c.ordinal == stage["ordinal"],
                )
                .values(dossier=dossier)
            )
        else:
            return None
    if transition is not None:
        await db.log_blocked_flips(transition.flipped)
        await db._notify_settled(transition.settled)
        await db._notify_ready(transition.ready)
    return receipt


async def reconcile_stopped_accepted_delegates(db, now, *, limit=100):
    """Recover accepted writers stopped by the old timeout path, never a live writer."""
    async with db._engine.connect() as conn:
        held = (
            (
                await conn.execute(
                    select(
                        sessions.c.id,
                        sessions.c.task_id,
                        sessions.c.last_claim_epoch,
                        sessions.c.work_dir,
                    )
                    .join(
                        integration_candidate_resolutions,
                        integration_candidate_resolutions.c.repair_session_id == sessions.c.id,
                    )
                    .where(
                        integration_candidate_resolutions.c.state == "accepted",
                        integration_candidate_resolutions.c.repair_task_id == sessions.c.task_id,
                        sessions.c.state == "stopped",
                        sessions.c.desired_state == "stopped",
                        sessions.c.claim_phase == "active",
                    )
                    .distinct()
                    .order_by(sessions.c.id)
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
    recovered = []
    for session in held:
        receipt = await complete_accepted_delegate(
            db,
            session["task_id"],
            session_id=session["id"],
            claim_epoch=session["last_claim_epoch"],
            recover_stopped=True,
        )
        if receipt is None:
            continue
        await db.save_task_completion(
            TaskCompletion(
                id=receipt["completion_id"],
                task_id=session["task_id"],
                outcome="pass",
                branch=receipt["branch"],
                commits=[receipt["head_sha"]],
                completed_at=now,
                summary="Recovered stopped delegate after its exact accepted candidate handoff; "
                "candidate CI and promotion remain independently guarded.",
            ),
            idempotent=True,
        )
        result = await db.release_claim(
            session["id"],
            task_status=TaskStatus.COMPLETED,
            context="integration_accepted_repair_recovery",
            now=now,
            expected_task_id=session["task_id"],
            expected_claim_epoch=session["last_claim_epoch"],
            preserve_terminal_task=True,
            release_workspace_lock=True,
        )
        if result.released:
            remove_claim_file_if_matches(
                session["work_dir"], session["task_id"], session["last_claim_epoch"]
            )
            recovered.append(session["task_id"])
    return recovered
