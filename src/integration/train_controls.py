"""Preview/apply controls for Git-first batches and delivered branch origins."""

from __future__ import annotations

import json
import time
from dataclasses import replace

from sqlalchemy import select, update

from src.database.tables import (
    integration_batch_members,
    integration_batches,
    projects,
    repos,
    task_branch_origins,
    tasks,
)
from src.git.manager import RemoteRefState
from src.integration.batches import BatchStore, candidate_ref
from src.integration.delivery_observer import delivery_targets
from src.integration.delivery_truth import load_delivery_requests
from src.integration.train import TrainTarget
from src.integration.train_sources import project_delivered, project_snapshot


class TrainControls:
    def __init__(self, db, *, snapshot=project_snapshot, clock=time.time):
        self.db, self.snapshot, self.clock = db, snapshot, clock

    async def abort_batch(self, batch_id, *, dry_run, operator_id, reason):
        if not dry_run and not reason.strip():
            raise ValueError("abort requires a nonblank reason")
        store = BatchStore(self.db, clock=self.clock)
        batch = await store.get(batch_id)
        if batch is None:
            raise ValueError("Git-first batch is missing")
        target = TrainTarget(batch.project_id, batch.repository_id, batch.target_ref)
        snapshot = await self.snapshot(self.db, target)
        if snapshot is None or snapshot.error or not snapshot.target_oid:
            raise ValueError("batch target cannot be observed")
        ref = candidate_ref(batch.id)
        candidate = snapshot.observation.source_heads.get(
            "refs/remotes/origin/" + ref.removeprefix("refs/heads/")
        )
        if candidate:
            contained = await snapshot.observation.git.ais_ancestor(
                snapshot.observation.store,
                candidate,
                snapshot.target_oid,
                strict=True,
            )
            if contained is None:
                raise ValueError("candidate promotion cannot be observed")
            if contained:
                raise ValueError("promoted candidate cannot be aborted")

        async def unchanged():
            if not await snapshot.is_fresh():
                return False
            if candidate:
                return await snapshot.for_target(ref).is_fresh()
            observed = snapshot.observation
            remote = await observed.git.als_remote_ref(
                observed.store,
                ref.removeprefix("refs/heads/"),
                repository_url=observed.repository_url,
            )
            return remote.state is RemoteRefState.ABSENT

        await store.set_intent(
            batch_id,
            "aborted",
            dry_run=dry_run,
            authorize=unchanged,
            operator_id=operator_id,
            reason=reason,
        )
        return {
            "outcome": "preview" if dry_run else "aborted",
            "batch_id": batch_id,
            "project_id": batch.project_id,
            "target_ref": batch.target_ref,
            "candidate_sha": candidate,
            "target_sha": snapshot.target_oid,
            "intent": batch.intent if dry_run else "aborted",
            "dry_run": dry_run,
        }

    async def retire_origin(self, task_id, *, dry_run, origin_id, operator_id, reason):
        if not dry_run and (not origin_id or not reason.strip()):
            raise ValueError("retire requires the previewed origin id and a nonblank reason")
        async with self.db._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(
                            task_branch_origins,
                            tasks.c.project_id,
                            repos.c.default_branch,
                        )
                        .select_from(
                            task_branch_origins.join(
                                tasks, tasks.c.id == task_branch_origins.c.task_id
                            )
                            .join(projects, projects.c.id == tasks.c.project_id)
                            .join(repos, repos.c.id == task_branch_origins.c.repository_id)
                        )
                        .where(
                            tasks.c.id == task_id,
                            tasks.c.status == "COMPLETED",
                            projects.c.integration_repository_id == repos.c.id,
                            task_branch_origins.c.retired_at.is_(None),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            routed = await delivery_targets(conn, [task_id], reduced=True)
        if row is None:
            raise ValueError("completed task has no live integration origin")
        if origin_id is not None and origin_id != row["id"]:
            raise ValueError("origin changed; preview again")
        root = TrainTarget(
            row["project_id"],
            row["repository_id"],
            "refs/heads/" + row["default_branch"].removeprefix("refs/heads/"),
        )
        snapshot = await self.snapshot(self.db, root)
        requests = await load_delivery_requests(
            self.db,
            [task_id],
            repository_id=root.repository_id,
            target_ref=root.target_ref,
            reduced=True,
        )
        request = requests.get(task_id)
        if request is None:
            raise ValueError("current completion cannot be observed")
        delivered = task_id in await project_delivered(
            self.db,
            [task_id],
            project_id=root.project_id,
            repository_id=root.repository_id,
            target_ref=root.target_ref,
            snapshot=snapshot,
        )
        proof_snapshot = snapshot
        if not delivered and snapshot is not None and task_id in routed:
            proof_snapshot = snapshot.for_target(routed[task_id].target_ref)
            delivered = (
                await proof_snapshot.is_delivered(
                    replace(request, target_ref=routed[task_id].target_ref),
                    source_base=row["base_sha"],
                )
            ).satisfied
        if not delivered:
            raise ValueError("current completion is not proven delivered")
        async with self.db.immediate() as conn:
            # Share the publisher's batch lock before locking the task/origin.
            active = (
                (
                    await conn.execute(
                        select(integration_batches.c.id)
                        .where(
                            integration_batches.c.id.in_(
                                select(integration_batch_members.c.batch_id).where(
                                    integration_batch_members.c.task_id == task_id
                                )
                            ),
                            integration_batches.c.intent != "aborted",
                            integration_batches.c.lifecycle != "promoted",
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if active:
                raise ValueError("abort the task's open batches before retiring its origin")
            await conn.execute(select(tasks.c.id).where(tasks.c.id == task_id).with_for_update())
            current = (
                (
                    await conn.execute(
                        select(task_branch_origins)
                        .where(
                            task_branch_origins.c.id == row["id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            current_requests = await load_delivery_requests(
                self.db,
                [task_id],
                repository_id=root.repository_id,
                target_ref=root.target_ref,
                reduced=True,
                conn=conn,
            )
            default = await conn.scalar(
                select(repos.c.default_branch).where(repos.c.id == root.repository_id)
            )
            designated = await conn.scalar(
                select(projects.c.integration_repository_id).where(projects.c.id == root.project_id)
            )
            if (
                current["retired_at"] is not None
                or current["base_sha"] != row["base_sha"]
                or current_requests.get(task_id) != request
                or default != row["default_branch"]
                or designated != root.repository_id
                or proof_snapshot is not None
                and not await proof_snapshot.is_fresh()
            ):
                raise ValueError("delivery or origin changed; preview again")
            if not dry_run:
                await conn.execute(
                    update(task_branch_origins)
                    .where(
                        task_branch_origins.c.id == row["id"],
                    )
                    .values(retired_at=self.clock())
                )
                await self.db.log_event(
                    "integration.origin_retired",
                    project_id=root.project_id,
                    task_id=task_id,
                    payload=json.dumps(
                        {
                            "origin_id": row["id"],
                            "operator_id": operator_id,
                            "reason": reason,
                            "completion_id": request.completion_id,
                        }
                    ),
                    conn=conn,
                )
        return {
            "outcome": "preview" if dry_run else "retired",
            "task_id": task_id,
            "project_id": root.project_id,
            "origin_id": row["id"],
            "dry_run": dry_run,
            "target_ref": proof_snapshot.observation.target_ref
            if proof_snapshot
            else root.target_ref,
        }
