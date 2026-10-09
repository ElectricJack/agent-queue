"""Preview/apply controls for Git-first batches and delivered branch origins."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
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
from src.integration.branch_abandon import ABORT, SUPERSEDE
from src.integration.delivery_observer import delivery_targets
from src.integration.delivery_truth import load_delivery_requests
from src.integration.train import TrainTarget
from src.integration.train_sources import project_delivered, project_snapshot

logger = logging.getLogger(__name__)


class TrainControlError(ValueError):
    """A control refusal whose outcome is part of the command contract."""

    def __init__(self, outcome, message):
        super().__init__(message)
        self.outcome = outcome


class TrainControls:
    def __init__(self, db, *, snapshot=project_snapshot, clock=time.time, train=None,
                 git=None, data_dir=None):
        self.db, self.snapshot, self.clock, self.train = db, snapshot, clock, train
        self.git = git
        self.data_dir = data_dir

    async def _root_noop_identity(self, conn, task_id):
        from src.database.queries.task_subtask_queries import OPEN_SUBTASK_STATUSES
        from src.database.tables import archived_tasks, task_completion_records, task_subtasks
        from src.integration.train_sources import TRAIN_MODES

        task = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().one_or_none()
        if task is None or task["parent_task_id"] is not None or not task["branch_name"]:
            raise ValueError("no-op control requires a branched Git root")
        if task["status"] not in {"DEFINED", "READY", "COMPLETED"}:
            raise ValueError("root must be unheld and READY, DEFINED or COMPLETED")
        if task["assigned_agent_id"] or any(
            item.get("required", True) for item in json.loads(task["deliverables"])
        ):
            raise ValueError("root has an assigned agent or required deliverables")
        for table in (tasks, archived_tasks):
            if await conn.scalar(select(table.c.id).where(table.c.parent_task_id == task_id).limit(1)):
                raise ValueError("a container cannot be completed as a no-artifact root")
        if await conn.scalar(select(task_subtasks.c.id).where(
            task_subtasks.c.task_id == task_id, task_subtasks.c.status.in_(OPEN_SUBTASK_STATUSES),
        ).limit(1)):
            raise ValueError("root has open checklist subtasks")
        project = (await conn.execute(select(projects).where(
            projects.c.id == task["project_id"],
        ))).mappings().one()
        if (project["hierarchical_integration_mode"] not in TRAIN_MODES
                or project["integration_repository_id"] != task["repo_id"]):
            raise ValueError("root has no designated train repository")
        origins = (await conn.execute(select(task_branch_origins).where(
            task_branch_origins.c.task_id == task_id,
            task_branch_origins.c.repository_id == task["repo_id"],
            task_branch_origins.c.retired_at.is_(None),
        ))).mappings().all()
        if (len(origins) != 1 or not origins[0]["materialized"]
                or origins[0]["branch_name"].removeprefix("refs/heads/") !=
                task["branch_name"].removeprefix("refs/heads/")):
            raise ValueError("root has no unique materialized source origin")
        repo = (await conn.execute(select(repos).where(repos.c.id == task["repo_id"]))).mappings().one()
        if repo["project_id"] != task["project_id"]:
            raise ValueError("root repository belongs to another project")
        completion = (await conn.execute(select(task_completion_records).where(
            task_completion_records.c.task_id == task_id,
        ).order_by(task_completion_records.c.completed_at.desc(),
                   task_completion_records.c.id.desc()).limit(1))).mappings().one_or_none()
        if task["status"] == "COMPLETED" and completion is not None and (
            completion["outcome"] != "pass" or completion["work_outcome"] != "no-op"
        ):
            raise ValueError("existing completion is not a passing no-op")
        requests = await load_delivery_requests(self.db, [task_id], repository_id=repo["id"],
            target_ref="refs/heads/" + repo["default_branch"], conn=conn)
        request = requests[task_id]
        if request.requires_parent_completion:
            raise ValueError("a former container requires its parent completion protocol")
        if task["status"] == "COMPLETED" and completion and request.completion_id != completion["id"]:
            raise ValueError("current completion generation is missing; use its completion protocol")
        return (dict(task), dict(origins[0]), dict(repo), dict(completion) if completion else None,
                request.completion_id)

    async def _assert_root_unheld(self, conn, task):
        from src.database.tables import sessions, task_metadata, workspaces
        from src.integration.lock import BranchLock
        from src.integration.models import BranchKey

        if await conn.scalar(select(sessions.c.id).where(
            sessions.c.task_id == task["id"], sessions.c.state.in_(("starting", "running", "draining")),
        ).limit(1)) or await conn.scalar(select(workspaces.c.id).where(
            workspaces.c.locked_by_task_id == task["id"],
        ).limit(1)):
            raise ValueError("root still has a live writer or claimed workspace")
        if await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == task["id"], task_metadata.c.key == "claimed_by_session",
        )):
            raise ValueError("root still has a worker claim; reset it before recording a no-op")
        owner = await BranchLock(self.db).lock_on(conn, BranchKey(
            repository_id=task["repo_id"], branch=task["branch_name"],
        ))
        if owner and (owner["holder"] or owner["handoff_state"] != "released"):
            raise ValueError("root still has branch ownership; release it before recording a no-op")
        if await conn.scalar(select(integration_batches.c.id).where(
            integration_batches.c.id.in_(select(integration_batch_members.c.batch_id).where(
                integration_batch_members.c.task_id == task["id"],
            )), integration_batches.c.intent != "aborted",
            integration_batches.c.lifecycle != "promoted",
        ).limit(1)):
            raise ValueError("root belongs to an open batch")

    async def record_root_noop(self, task_id, *, dry_run, expected_head_sha, operator_id, reason,
                               expected_project_id=None):
        """Retain a proven empty source and its completion without taking a worker claim."""
        from sqlalchemy.dialects.postgresql import insert

        from src.database.tables import task_completion_records
        from src.git.manager import is_valid_git_oid
        from src.integration.lock import CRITICAL_SECTION_SECONDS
        from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
        from src.models import TaskStatus

        if not dry_run and (not is_valid_git_oid(expected_head_sha) or not reason.strip()):
            raise ValueError("apply requires the previewed exact head and a nonblank reason")
        async with self.db._engine.connect() as conn:
            identity = await self._root_noop_identity(conn, task_id)
        task, origin, repo, completion, current_generation = identity
        if expected_project_id is not None and task["project_id"] != expected_project_id:
            raise ValueError("root project changed after authorization; preview again")
        target = TrainTarget(task["project_id"], repo["id"], "refs/heads/" + repo["default_branch"])
        snapshot = await self.snapshot(self.db, target)
        if snapshot is None or snapshot.error or not snapshot.target_oid:
            raise ValueError("root Git evidence is unavailable")
        branch = "refs/heads/" + task["branch_name"].removeprefix("refs/heads/")
        source = snapshot.for_target(branch).target_oid
        if not is_valid_git_oid(source) or expected_head_sha and source != expected_head_sha:
            raise ValueError("published root head is missing or changed from preview")
        base = origin["base_sha"]
        if source != base:
            raise ValueError(
                "root source contains changes in its commit history: published head differs "
                "from its recorded origin base; use the normal task close path"
            )
        observed = snapshot.observation
        provenance = GitProvenance(observed.git, observed.store, repository_url=observed.repository_url)
        await provenance.exact(source)
        await provenance.exact(base)
        tree = await provenance.run("rev-parse", source + "^{tree}")
        if (not await provenance.ancestor(base, source)
                or tree != await provenance.run("rev-parse", base + "^{tree}")):
            raise ValueError("root source contains changes relative to its recorded origin")
        reuse = task["status"] == "COMPLETED" and completion is not None
        if reuse and (completion["branch"] not in {None, "", task["branch_name"]}
                      or json.loads(completion["commits"]) not in ([], [source])):
            raise ValueError("existing no-op completion names another branch or source")
        generation = current_generation if task["status"] == "COMPLETED" else (
            "root-noop-" + hashlib.sha256(repr((task["project_id"], repo["id"], task_id,
                task["legacy_completion_id"], source)).encode()).hexdigest())
        completed = CompletedSource(CompletionIdentity(task["project_id"], repo["id"], task_id,
                                                       generation), source)
        retained = await provenance.read_completion(completed.identity)
        if retained and (retained["artifact"] or retained["source_oid"] != source):
            raise ValueError("retained completion is an artifact or names another source")
        result = {"outcome": "preview" if dry_run else "recorded", "task_id": task_id,
                  "project_id": task["project_id"], "head_sha": source, "base_sha": base,
                  "tree_sha": tree, "completion_id": generation, "dry_run": dry_run}
        transition = None
        # Claims and hierarchy edits share this project lock. The ref lock
        # excludes managed source writers while the exact remote is rechecked.
        async with asyncio.timeout(CRITICAL_SECTION_SECONDS), self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, task["project_id"])
            await self.db._slot_reset_claim_sessions(conn, task_id, lock=True)
            await conn.execute(select(tasks.c.id).where(tasks.c.id == task_id).with_for_update())
            if await self._root_noop_identity(conn, task_id) != identity:
                raise ValueError("root identity or completion changed; preview again")
            await self._assert_root_unheld(conn, task)
            remote = await observed.git.als_remote_ref(observed.store, branch.removeprefix("refs/heads/"),
                                                     repository_url=observed.repository_url)
            if remote.state is not RemoteRefState.PRESENT or remote.oid != source:
                raise ValueError("published root head changed; preview again")
            if dry_run:
                return result
            if task["status"] != "COMPLETED":
                transition = await self.db._apply_transition(
                    conn, task_id, TaskStatus.COMPLETED, context="root_noop_completion",
                )
            await provenance.write_completion(completed, claim_epoch=task["claim_epoch"], artifact=False)
            remote = await observed.git.als_remote_ref(observed.store, branch.removeprefix("refs/heads/"),
                                                     repository_url=observed.repository_url)
            if remote.state is not RemoteRefState.PRESENT or remote.oid != source:
                raise ValueError("published root head changed during evidence publication")
            if not reuse:
                await conn.execute(insert(task_completion_records).values(
                    id=generation, task_id=task_id, outcome="pass", work_outcome="no-op",
                    branch=task["branch_name"], commits=json.dumps([]), summary=reason.strip(),
                    verification=f"Published source {source} has origin {base} and tree {tree}.",
                    notes=json.dumps({"authority": operator_id, "reason": reason.strip(),
                                      "origin_id": origin["id"]}),
                    completed_at=max(self.clock(), (completion or {}).get("completed_at", 0) + 0.000001),
                ))
            await self.db._upsert_meta(task_id, "work_outcome", "no-op", conn=conn)
            from src.database.queries.task_queries import DEVELOPMENT_COMPLETION_ID_KEY

            await self.db._upsert_meta(task_id, DEVELOPMENT_COMPLETION_ID_KEY, generation, conn=conn)
            await self.db._upsert_meta(task_id, "integration_root_noop", {
                "completion_id": generation, "head_sha": source, "base_sha": base,
                "operator": operator_id, "reason": reason.strip(),
            }, conn=conn)
            flipped = await self.db.recompute_blocked({task_id}, conn=conn)
        await self.db.log_blocked_flips(flipped | (transition.flipped if transition else set()))
        if transition:
            await self.db._notify_settled(transition.settled)
            await self.db._notify_ready(transition.ready)
        return result

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
        result = {
            "outcome": "preview" if dry_run else "aborted",
            "batch_id": batch_id,
            "project_id": batch.project_id,
            "target_ref": batch.target_ref,
            "candidate_sha": candidate,
            "target_sha": snapshot.target_oid,
            "intent": batch.intent if dry_run else "aborted",
            "dry_run": dry_run,
        }
        if not dry_run:
            result["branch_abandon"] = await self._abandon_batch_refs(
                batch_id, reason=ABORT, detail=reason, principal=operator_id
            )
        return result

    async def _abandon_batch_refs(self, batch_id, *, reason, detail, principal):
        """Delete the refs an aborted or superseded batch owned outright.

        A batch's *members* keep their branches: an unchanged member returns to
        pending and is collected again, so deleting one would throw away work
        that is about to be delivered.  Only the private candidate refs — the
        ones no task and no other batch can name — are abandoned here.  A
        failure to delete never fails the abort: the refs are already withheld
        from delivery, and the recorded audit row is retried.
        """
        from src.integration.branch_abandon import BranchAbandonService

        service = BranchAbandonService(
            self.db, data_dir=self.data_dir, git_manager=self.git
        )
        try:
            return await service.abandon_batch_candidates(
                batch_id, reason=reason, detail=detail, principal=principal
            )
        except Exception as exc:  # noqa: BLE001 - an abort must not be undone by cleanup
            logger.warning(
                "Could not abandon the candidate refs of batch %s", batch_id, exc_info=True
            )
            return {"batch_id": batch_id, "state": "pending", "branches": [],
                    "reason": str(exc) or type(exc).__name__}

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
