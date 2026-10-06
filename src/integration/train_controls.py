"""Preview/apply controls for Git-first batches and delivered branch origins."""

from __future__ import annotations

import hashlib
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
    task_dependencies,
    tasks,
)
from src.git.manager import RemoteRefState
from src.integration.batches import Batch, BatchStore, candidate_ref
from src.integration.delivery_observer import delivery_targets
from src.integration.delivery_truth import load_delivery_requests
from src.integration.train import TrainTarget
from src.integration.train_sources import project_delivered, project_snapshot


class TrainControlError(ValueError):
    """A control refusal whose outcome is part of the command contract."""

    def __init__(self, outcome, message):
        super().__init__(message)
        self.outcome = outcome


class TrainControls:
    def __init__(self, db, *, snapshot=project_snapshot, clock=time.time, train=None):
        self.db, self.snapshot, self.clock, self.train = db, snapshot, clock, train

    async def refresh_epic(self, task_id, *, dry_run, operator_id):
        from src.integration.stacked_branches import EpicRefresh

        return await EpicRefresh(self.db, self.train, clock=self.clock).refresh(
            task_id, dry_run=dry_run, operator_id=operator_id)

    async def abort_batch(self, batch_id, *, dry_run, operator_id, reason):
        if not dry_run and not reason.strip():
            raise ValueError("abort requires a nonblank reason")
        batch, snapshot, candidate, unchanged = await self._abort_observation(batch_id)
        store = BatchStore(self.db, clock=self.clock)
        await store.set_intent(
            batch_id, "aborted", dry_run=dry_run, authorize=unchanged,
            operator_id=operator_id, reason=reason,
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

    async def _abort_observation(self, batch_id):
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
        # A failed first merge publishes the target as its repair start; a
        # later conflict can publish only some members. Candidate ancestry
        # cannot prove that the batch's complete frozen inputs were promoted.
        members = await store.members(batch_id)
        proofs = [
            await snapshot.contains_source(member.task_id, member.source_sha, member.base_sha)
            for member in members
        ]
        if any(proof is None for proof in proofs):
            raise ValueError("batch promotion cannot be observed")
        if members and all(proof is True for proof in proofs):
            raise ValueError("promoted batch cannot be aborted")

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

        return batch, snapshot, candidate, unchanged

    async def set_batch_intent(self, batch_id, intent, *, dry_run, operator_id, reason=""):
        store = BatchStore(self.db, clock=self.clock)
        batch = await store.get(batch_id)
        if batch is None:
            raise ValueError("Git-first batch is missing")
        await store.set_intent(batch_id, intent, dry_run=dry_run,
                               operator_id=operator_id, reason=reason)
        return {
            "outcome": "preview" if dry_run else "paused" if intent == "paused" else "resumed",
            "batch_id": batch_id,
            "project_id": batch.project_id,
            "target_ref": batch.target_ref,
            "intent": batch.intent if dry_run else intent,
            "dry_run": dry_run,
        }

    async def eject(self, batch_id, task_id, *, service, dry_run, operator_id, reason):
        if not dry_run and not reason.strip():
            raise ValueError("ejection requires a nonblank reason")
        batch = await service.store.get(batch_id)
        if batch is None:
            raise TrainControlError("unknown_batch", "Git-first batch is missing")
        if batch.epic_sync:
            raise ValueError("epic sync membership cannot be ejected")
        frozen = await service.store.members(batch_id)
        if task_id not in {m.task_id for m in frozen}:
            raise TrainControlError("not_a_member", "task is not a frozen batch member")
        batch, snapshot, candidate, unchanged = await self._abort_observation(batch_id)
        requests = await load_delivery_requests(self.db, [m.task_id for m in frozen],
            repository_id=batch.repository_id, target_ref=batch.target_ref, reduced=True)
        _, trees = await service.freeze_inputs(batch, frozen, requests=requests, snapshot=snapshot)
        members = tuple(replace(m, order=i) for i, m in enumerate(
            m for m in frozen if m.task_id != task_id))
        replacement = None
        if members:
            digest = hashlib.sha256(repr((batch_id, task_id, members)).encode()).hexdigest()
            replacement = Batch("train-eject-" + digest[:32], batch.project_id,
                batch.repository_id, batch.target_ref, intent=batch.intent, created_at=self.clock())
            async with self.db._engine.connect() as conn:
                dependent = await conn.scalar(select(task_dependencies.c.task_id).where(
                    task_dependencies.c.task_id.in_([m.task_id for m in members]),
                    task_dependencies.c.depends_on_task_id == task_id,
                    task_dependencies.c.dep_type == "blocks",
                ))
            ejected = next(m for m in frozen if m.task_id == task_id)
            if dependent and not await snapshot.contains_source(
                    task_id, ejected.source_sha, ejected.base_sha):
                raise ValueError("ejected member is an undelivered prerequisite of the remainder")

        async def authorize():
            current = await load_delivery_requests(self.db, list(requests),
                repository_id=batch.repository_id, target_ref=batch.target_ref, reduced=True)
            return (current == requests and await unchanged()
                    and await service.eligible(batch, frozen))

        await service.store.eject(batch, task_id, replacement, members, trees=trees,
            authorize=authorize, dry_run=dry_run, operator_id=operator_id, reason=reason)
        return {
            "outcome": "preview" if dry_run else "ejected", "project_id": batch.project_id,
            "batch_id": batch_id, "task_id": task_id, "dry_run": dry_run,
            "replacement_batch_id": replacement.id if replacement else None,
            "members": [m.task_id for m in members], "candidate_sha": candidate,
            "target_sha": snapshot.target_oid, "target_ref": batch.target_ref,
            "intent": batch.intent if dry_run else "aborted",
        }

    async def seal_now(self, project_id, *, train, dry_run):
        if train is None:
            raise ValueError("the integration train is not active")
        targets = await train.targets.targets(self.clock())
        roots = [t for t in targets if t.project_id == project_id and t.kind != "epic"]
        if len(roots) != 1:
            raise ValueError("project has no unique active root train target")
        target = roots[0]
        lane = await train.lane_for(target)
        snapshot = await lane.snapshot()
        if snapshot.error or not snapshot.target_oid:
            raise ValueError("train target cannot be observed")
        if dry_run:
            batch = await train.batches.current(target)
            blockers = []
            if batch is not None:
                members = await lane.service.store.members(batch.id)
            else:
                pending = await train.batches.pending(target, snapshot, blockers=blockers)
                members = pending[0] if pending else ()
            return {"outcome": "preview", "project_id": project_id, "dry_run": True,
                "batch_id": batch.id if batch else None, "target_ref": target.target_ref,
                "intent": batch.intent if batch else None,
                "members": [m.task_id for m in members], "blockers": blockers}
        selection = await train.batches.open_batch(target, snapshot, lane.service, seal_now=True)
        outcome = "no_ready_work"
        if selection.batch:
            outcome = "existing_batch" if selection.existing else "sealed"
        return {"outcome": outcome,
            "project_id": project_id, "dry_run": False,
            "batch_id": selection.batch.id if selection.batch else None,
            "intent": selection.batch.intent if selection.batch else None,
            "target_ref": target.target_ref, "members": [m.task_id for m in selection.members],
            "blockers": list(selection.blockers)}

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
