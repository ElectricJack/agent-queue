"""Fenced local shutdown of idle train admission and detached reservations."""

from __future__ import annotations

import json
import time

from sqlalchemy import select, union, update

from src.database import tables as t
from src.database.queries.integration_state_queries import (
    session_attached_clause,
    unresolved_claim_clause,
)
from src.integration.engine import RootEngineOwnership
from src.integration.lock import BranchLock
from src.integration.models import BranchKey
from src.integration.owner_recovery import _Refusal
from src.integration.stale_owners import _fence_blocker
from src.integration.runtime_contracts import Subject, SubjectSchedule, schedule_values


class TrainQuiesce:
    def __init__(self, db):
        self.db = db

    async def run(self, request):
        project = await self.db.get_project(request.project_id)
        if project is None or not project.integration_repository_id:
            raise ValueError("project has no designated integration repository")
        repository_id = project.integration_repository_id
        now = time.time()
        async with RootEngineOwnership(self.db).exclusion(repository_id) as conn:
            await self.db.lock_hierarchy_project(conn, request.project_id)
            current = (
                (
                    await conn.execute(
                        select(t.projects)
                        .where(
                            t.projects.c.id == request.project_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if (
                current["integration_repository_id"] != repository_id
                or current["hierarchical_integration_generation"] != request.expected_generation
            ):
                raise ValueError("project integration generation or repository changed")
            row = (
                (
                    await conn.execute(
                        select(t.integration_subjects)
                        .where(
                            t.integration_subjects.c.id == request.subject_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                row is None
                or row["project_id"] != request.project_id
                or row["repository_id"] != repository_id
                or row["version"] != request.expected_version
            ):
                raise ValueError("subject identity or version changed")
            if (
                row["kind"] != "root_batch"
                or row["engine"] != "reconciler"
                or row["phase"] != "admitting"
                or row["batch_id"]
                or row["head_sha"]
                or row["base_sha"]
                or row["writer_status"] != "none"
                or row["writer_task_id"]
                or row["writer_session_id"]
            ):
                raise ValueError("only an unfrozen admitting root without a writer may be quiesced")
            schedule = (
                (
                    await conn.execute(
                        select(t.project_integration_schedules)
                        .where(
                            t.project_integration_schedules.c.project_id == request.project_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            request_id = row["subject_key"].removeprefix(f"root_batch:{repository_id}:")
            prefix = f"integration-sweep:{request.project_id}:"
            if (
                schedule is None
                or not request_id.startswith(prefix)
                or not request_id.removeprefix(prefix).isdigit()
                or int(request_id.removeprefix(prefix)) > schedule["request_sequence"]
                or schedule["outstanding_request_id"] not in (None, request_id)
            ):
                raise ValueError("subject does not match the current or ended sweep request")
            task_ids = select(t.tasks.c.id).where(t.tasks.c.project_id == request.project_id)
            parent_ids = union(
                task_ids,
                select(t.archived_tasks.c.id).where(
                    t.archived_tasks.c.project_id == request.project_id
                ),
            )
            batch_ids = select(t.integration_batches.c.id).where(
                t.integration_batches.c.project_id == request.project_id
            )
            blockers = (
                select(t.integration_subjects.c.id).where(
                    t.integration_subjects.c.project_id == request.project_id,
                    t.integration_subjects.c.id != request.subject_id,
                    t.integration_subjects.c.phase != "done",
                ),
                select(t.integration_batches.c.id).where(
                    t.integration_batches.c.project_id == request.project_id,
                    t.integration_batches.c.intent != "aborted",
                    t.integration_batches.c.lifecycle.not_in(("promoted", "empty", "failed", "aborted")),
                ),
                select(t.integration_batches.c.id).where(
                    t.integration_batches.c.project_id == request.project_id,
                    t.integration_batches.c.request_id == request_id,
                ),
                select(t.integration_promotion_intents.c.id).where(
                    t.integration_promotion_intents.c.project_id == request.project_id,
                    t.integration_promotion_intents.c.state.not_in(
                        ("committed", "conflict", "superseded")
                    ),
                ),
                select(t.integration_candidate_ref_mutations.c.id).where(
                    t.integration_candidate_ref_mutations.c.repository_id == repository_id,
                    t.integration_candidate_ref_mutations.c.state == "reserved",
                ),
                select(t.integration_candidate_resolutions.c.id).where(
                    t.integration_candidate_resolutions.c.batch_id.in_(batch_ids),
                    t.integration_candidate_resolutions.c.state.in_(("reserved", "pushed")),
                ),
                select(t.integration_attestation_publications.c.id).where(
                    t.integration_attestation_publications.c.batch_id.in_(batch_ids),
                    t.integration_attestation_publications.c.state == "reserved",
                ),
                select(t.integration_repair_operations.c.id).where(
                    (
                        t.integration_repair_operations.c.parent_task_id.in_(parent_ids)
                        | t.integration_repair_operations.c.batch_id.in_(batch_ids)
                    ),
                    t.integration_repair_operations.c.state.not_in(("completed", "cancelled")),
                ),
                select(t.sessions.c.id).where(
                    t.sessions.c.project_id == request.project_id,
                    t.sessions.c.task_id.is_not(None),
                    session_attached_clause(),
                ),
                select(t.workspaces.c.id).where(t.workspaces.c.locked_by_task_id.in_(task_ids)),
                select(t.task_metadata.c.task_id).where(
                    t.task_metadata.c.task_id.in_(task_ids),
                    t.task_metadata.c.key == "claimed_by_session",
                    unresolved_claim_clause(),
                ),
            )
            for statement in blockers:
                blocker = await conn.scalar(statement.limit(1))
                if blocker:
                    raise ValueError(
                        f"project still has a writer, claim, frozen batch or unresolved integration operation ({blocker})"
                    )
            owners = []
            for expected in request.owners:
                owner = (
                    (
                        await conn.execute(
                            select(t.integration_branch_owners)
                            .where(
                                t.integration_branch_owners.c.id == expected.owner_row_id,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    owner is None
                    or owner["repository_id"] != repository_id
                    or owner["fence_token"] != expected.fence_token
                ):
                    raise ValueError("branch owner identity or fence changed")
                owner = await BranchLock(self.db).lock_on(
                    conn, BranchKey(repository_id=repository_id, branch=owner["ref"])
                )
                if (
                    owner["handoff_state"] != "reserved"
                    or owner["holder"]
                    or owner["session_id"]
                    or owner["workspace_id"]
                ):
                    raise ValueError(
                        "quiesce can release only detached reservations without writers"
                    )
                if owner["owner_role"] in ("worker", "repair", "verifier"):
                    task = (
                        (
                            await conn.execute(
                                select(t.tasks)
                                .where(
                                    t.tasks.c.id == owner["owner_id"],
                                    t.tasks.c.project_id == request.project_id,
                                )
                                .with_for_update()
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if (
                        task is None
                        or task["status"] not in ("PAUSED", "COMPLETED", "FAILED")
                        or task["assigned_agent_id"]
                        or (task["branch_name"] or "").removeprefix("refs/heads/")
                        != owner["ref"].removeprefix("refs/heads/")
                    ):
                        raise ValueError("reserved task must be explicitly paused or finished")
                elif owner["owner_role"] == "collector":
                    operation = (
                        (
                            await conn.execute(
                                select(t.integration_repair_operations)
                                .where(
                                    t.integration_repair_operations.c.id == owner["owner_id"],
                                )
                                .with_for_update()
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if operation is None or operation["state"] not in ("completed", "cancelled"):
                        raise ValueError("collector operation must have ended")
                    if (
                        operation["parent_task_id"]
                        not in (await conn.execute(parent_ids)).scalars().all()
                        and operation["batch_id"]
                        not in (await conn.execute(batch_ids)).scalars().all()
                    ):
                        raise ValueError("collector operation belongs to another project")
                else:
                    raise ValueError("unsupported reservation role")
                try:
                    await _fence_blocker(conn, owner, include_aborted=False)
                except _Refusal as exc:
                    raise ValueError(exc.detail) from exc
                owners.append(owner)
            result = {
                "outcome": "preview" if request.dry_run else "quiesced",
                "project_id": request.project_id,
                "subject_id": request.subject_id,
                "subject_version": row["version"],
                "request_id": request_id,
                "owner_row_ids": [owner["id"] for owner in owners],
                "dry_run": request.dry_run,
            }
            if request.dry_run:
                return result
            await conn.execute(
                update(t.project_integration_schedules)
                .where(
                    t.project_integration_schedules.c.project_id == request.project_id,
                )
                .values(
                    enabled=False,
                    outstanding_request_id=None,
                    outstanding_trigger=None,
                    outstanding_requested_at=None,
                    catchup_trigger=None,
                    catchup_requested_at=None,
                    catchup_after_sequence=None,
                    settling_first_approval_at=None,
                    settling_fires_at=None,
                    updated_at=now,
                )
            )
            subject = Subject.from_row(row)
            closed = SubjectSchedule.close(
                now=now, reason=request.reason, max_wait_seconds=subject.schedule.max_wait_seconds
            )
            updated = await self.db.update_integration_subject_on(
                conn,
                subject_id=subject.id,
                expected_version=subject.version,
                values={"phase": "done", **schedule_values(closed)},
                now=now,
            )
            if updated is None:
                raise ValueError("subject changed during quiesce")
            for owner in owners:
                await conn.execute(
                    update(t.integration_branch_owners)
                    .where(
                        t.integration_branch_owners.c.id == owner["id"],
                    )
                    .values(
                        handoff_state="released",
                        fence_token=owner["fence_token"] + 1,
                        updated_at=now,
                    )
                )
            await self.db.append_integration_subject_journal_on(
                conn,
                {
                    "subject_id": subject.id,
                    "entry_kind": "action",
                    "idempotency_key": f"operator-quiesce:{subject.version}",
                    "visit_id": f"operator-quiesce:{subject.version}",
                    "mode": "active",
                    "policy_artifact_sha256": subject.policy.artifact_sha256,
                    "subject_version": subject.version,
                    "phase": subject.phase.value,
                    "head_sha": subject.head_sha,
                    "generation": subject.generation,
                    "primitive": "record_decision",
                    "outcome": "recorded",
                    "payload": {
                        "reason": request.reason,
                        "principal": "human:local-operator",
                        "owners": [
                            {
                                "id": owner["id"],
                                "old_fence": owner["fence_token"],
                                "new_fence": owner["fence_token"] + 1,
                            }
                            for owner in owners
                        ],
                    },
                    "recorded_at": now,
                },
            )
            await self.db.log_event(
                "integration.operator_quiesced",
                project_id=request.project_id,
                payload=json.dumps(
                    result | {"reason": request.reason, "principal": "human:local-operator"}
                ),
                conn=conn,
            )
            return result
