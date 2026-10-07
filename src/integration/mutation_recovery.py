"""Local read-back recovery of an expired mutation whose operation has ended."""

from __future__ import annotations

import json
import time

from sqlalchemy import select, update

from src.database import tables as t
from src.database.queries.integration_state_queries import (
    session_attached_clause,
    unresolved_claim_clause,
)
from src.git.manager import RemoteRefState, is_valid_git_oid
from src.integration.engine import RootEngineOwnership
from src.integration.lock import BranchLock
from src.integration.models import BranchKey
from src.integration.train import TrainTarget
from src.integration.train_sources import project_snapshot


class ExpiredMutationRecovery:
    def __init__(self, db, *, snapshot=project_snapshot, clock=time.time):
        self.db, self.snapshot, self.clock = db, snapshot, clock

    async def _identity_on(self, conn, request):
        row = (
            (
                await conn.execute(
                    select(t.integration_candidate_ref_mutations)
                    .where(
                        t.integration_candidate_ref_mutations.c.id == request.mutation_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ValueError("mutation does not exist")
        batch = (
            (
                await conn.execute(
                    select(t.integration_batches)
                    .where(
                        t.integration_batches.c.id == row["batch_id"],
                        t.integration_batches.c.project_id == request.project_id,
                        t.integration_batches.c.repository_id == row["repository_id"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        project = (
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
            .one_or_none()
        )
        if (
            batch is None
            or project is None
            or project["integration_repository_id"] != row["repository_id"]
        ):
            raise ValueError("mutation does not belong to the designated project repository")
        if (
            row["nonce"] != request.expected_nonce
            or row["branch_fence_token"] != request.expected_branch_fence
            or row["lease_fence_token"] != request.expected_lease_fence
        ):
            raise ValueError("mutation nonce or fence changed")
        if row["state"] != "reserved" or row["expires_at"] > self.clock():
            raise ValueError("only an expired reserved mutation may be reconciled")
        if row["purpose"] not in (
            "candidate_partial",
            "candidate_final",
            "repair_resolution",
            "repair_handoff",
        ):
            raise ValueError("mutation purpose requires its original publication recovery control")
        operation = (
            (
                await conn.execute(
                    select(t.integration_repair_operations)
                    .where(
                        t.integration_repair_operations.c.id == row["operation_id"],
                        t.integration_repair_operations.c.batch_id == batch["id"],
                        t.integration_repair_operations.c.episode_id == row["operation_episode_id"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if operation is None or operation["state"] not in ("completed", "cancelled"):
            raise ValueError("the owning operation has not ended")
        owner = await BranchLock(self.db).lock_on(
            conn, BranchKey(repository_id=row["repository_id"], branch=row["branch"])
        )
        if (
            owner is None
            or owner["holder"]
            or owner["session_id"]
            or owner["workspace_id"]
            or not (
                owner["handoff_state"] == "released"
                or (
                    owner["handoff_state"] == "reserved"
                    and owner["owner_role"] == "collector"
                    and owner["owner_id"] == row["operation_id"]
                )
            )
        ):
            raise ValueError("branch authority still names a writer or another reservation")
        task_ids = select(t.tasks.c.id).where(t.tasks.c.project_id == request.project_id)
        guards = (
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
            select(t.project_integration_leases.c.batch_id).where(
                t.project_integration_leases.c.project_id == request.project_id,
                t.project_integration_leases.c.expires_at > self.clock(),
            ),
        )
        for guard in guards:
            if await conn.scalar(guard.limit(1)):
                raise ValueError(
                    "project still has a live writer, workspace, claim or integration lease"
                )
        repo = (
            (
                await conn.execute(
                    select(t.repos).where(
                        t.repos.c.id == row["repository_id"],
                        t.repos.c.project_id == request.project_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        return dict(row), dict(batch), dict(operation), owner, dict(repo)

    async def run(self, request):
        project = await self.db.get_project(request.project_id)
        if project is None or not project.integration_repository_id:
            raise ValueError("project has no designated integration repository")
        repository_id = project.integration_repository_id
        # Observe Git outside the transaction; all durable bindings are re-proved
        # after the remote read under the same project and repository fences.
        async with RootEngineOwnership(self.db).exclusion(repository_id) as conn:
            await self.db.lock_hierarchy_project(conn, request.project_id)
            identity = await self._identity_on(conn, request)
        row, _, _, _, repo = identity
        snapshot = await self.snapshot(
            self.db,
            TrainTarget(
                request.project_id,
                repository_id,
                "refs/heads/" + repo["default_branch"].removeprefix("refs/heads/"),
            ),
        )
        if snapshot is None or snapshot.error:
            raise ValueError("repository cannot be observed")
        observation = snapshot.observation

        async def remote():
            result = await observation.git.als_remote_ref(
                observation.store,
                row["target_branch"].removeprefix("refs/heads/"),
                repository_url=observation.repository_url,
            )
            if result.state not in (RemoteRefState.PRESENT, RemoteRefState.ABSENT):
                raise ValueError("mutation remote target observation failed")
            if (
                result.state is RemoteRefState.PRESENT and not is_valid_git_oid(result.oid or "")
            ) or (result.state is RemoteRefState.ABSENT and result.oid is not None):
                raise ValueError("mutation remote target observation is invalid")
            return result.state, result.oid

        observed = await remote()
        outcome = "applied" if observed[1] == row["desired_sha"] else "superseded"
        result = {
            "outcome": "preview" if request.dry_run else outcome,
            "project_id": request.project_id,
            "mutation_id": row["id"],
            "disposition": outcome,
            "remote_sha": observed[1],
            "dry_run": request.dry_run,
        }
        async with RootEngineOwnership(self.db).exclusion(repository_id) as conn:
            await self.db.lock_hierarchy_project(conn, request.project_id)
            if await self._identity_on(conn, request) != identity:
                raise ValueError("mutation, operation or authority changed during observation")
            if await remote() != observed:
                raise ValueError("remote target changed during recovery; preview again")
            if request.dry_run:
                return result
            changed = await conn.execute(
                update(t.integration_candidate_ref_mutations)
                .where(
                    t.integration_candidate_ref_mutations.c.id == row["id"],
                    t.integration_candidate_ref_mutations.c.state == "reserved",
                    t.integration_candidate_ref_mutations.c.nonce == request.expected_nonce,
                    t.integration_candidate_ref_mutations.c.branch_fence_token
                    == request.expected_branch_fence,
                    t.integration_candidate_ref_mutations.c.lease_fence_token
                    == request.expected_lease_fence,
                )
                .values(
                    state=outcome,
                    remote_sha=observed[1] if outcome == "applied" else None,
                    updated_at=self.clock(),
                )
            )
            if changed.rowcount != 1:
                raise ValueError("mutation recovery lost its nonce fence")
            await self.db.log_event(
                "integration.expired_mutation_reconciled",
                project_id=request.project_id,
                conn=conn,
                payload=json.dumps(
                    result
                    | {
                        "reason": request.reason,
                        "principal": "human:local-operator",
                        "nonce": row["nonce"],
                        "branch_fence_token": row["branch_fence_token"],
                        "lease_fence_token": row["lease_fence_token"],
                        "operation_id": row["operation_id"],
                        "target_ref": row["target_branch"],
                    }
                ),
            )
        return result
