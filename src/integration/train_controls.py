"""Preview/apply controls for Git-first batches and delivered branch origins."""

from __future__ import annotations

import asyncio
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
from src.git.manager import GitError, RemoteRefState
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


async def _refresh_contains(snapshot, members, *, same_tree=False):
    """Strict ancestry of each frozen default head in the observed epic.

    ``same_tree`` adds the train's refresh delivery rule: the epic is the
    default head's exact tree. None is an unavailable observation.
    """
    observed = snapshot.observation
    store, target = str(observed.store), observed.target_oid
    proofs = []
    for member in members:
        proof = await observed.git.ais_ancestor(store, member.source_sha, target, strict=True)
        if proof and same_tree:
            try:
                proof = (await observed.git.atree_sha(store, member.source_sha)
                         == await observed.git.atree_sha(store, target))
            except GitError:
                proof = None
        proofs.append(proof)
    return proofs


class TrainControls:
    def __init__(self, db, *, snapshot=project_snapshot, clock=time.time, train=None):
        self.db, self.snapshot, self.clock, self.train = db, snapshot, clock, train

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

    async def _assert_root_unheld(self, conn, task, *, subject="root", action="recording a no-op",
                                  retire_epic_reservation=False):
        from src.database.queries.integration_state_queries import (
            session_attached_clause,
            unresolved_claim_clause,
        )
        from src.database.tables import sessions, task_metadata, workspaces
        from src.integration.lock import BranchLock
        from src.integration.models import BranchKey

        if await conn.scalar(select(sessions.c.id).where(
            sessions.c.task_id == task["id"],
            session_attached_clause() if retire_epic_reservation
            else sessions.c.state.in_(("starting", "running", "draining")),
        ).limit(1)) or await conn.scalar(select(workspaces.c.id).where(
            workspaces.c.locked_by_task_id == task["id"],
        ).limit(1)):
            raise ValueError(f"{subject} still has a live writer or claimed workspace")
        # A claim record survives pause and close as provenance; only a holder
        # that is not durably stopped still claims the root (azure-beacon-38).
        if await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == task["id"], task_metadata.c.key == "claimed_by_session",
            unresolved_claim_clause(),
        )):
            raise ValueError(f"{subject} still has a worker claim; reset it before {action}")
        owner = await BranchLock(self.db).lock_on(conn, BranchKey(
            repository_id=task["repo_id"], branch=task["branch_name"],
        ))
        retire = bool(retire_epic_reservation and owner
            and task["status"] == "COMPLETED"
            and owner["owner_id"] == task["id"] and owner["owner_role"] == "worker"
            and owner["handoff_state"] == "reserved"
            and owner["session_id"] is None and owner["workspace_id"] is None
            and owner["holder"] in (None, task["id"])
            and (owner["expires_at"] is None or owner["expires_at"] <= self.clock()))
        if owner and (owner["holder"] or owner["handoff_state"] != "released") and not retire:
            raise ValueError(f"{subject} still has branch ownership; release it before {action}")
        if await conn.scalar(select(integration_batches.c.id).where(
            integration_batches.c.id.in_(select(integration_batch_members.c.batch_id).where(
                integration_batch_members.c.task_id == task["id"],
            )), integration_batches.c.intent != "aborted",
            integration_batches.c.lifecycle != "promoted",
        ).limit(1)):
            raise ValueError(f"{subject} belongs to an open batch")
        if retire:
            from src.database.tables import integration_branch_owners as owners
            from src.database.tables import integration_candidate_ref_mutations as mutations
            from src.integration.delegate_release import live_integration_owner

            branch = owner["ref"].removeprefix("refs/heads/")
            if await live_integration_owner(conn, [task["id"]]) or await conn.scalar(
                select(mutations.c.id).where(
                    mutations.c.repository_id == owner["repository_id"],
                    mutations.c.branch.in_((branch, "refs/heads/" + branch)),
                    mutations.c.state == "reserved",
                ).limit(1)
            ):
                raise ValueError("epic still has an integration operation or ref mutation")

            # lock_on excludes managed ref acquisition (including ref aliases).
            # Bump both fences so no previous authority can survive retirement.
            token = max(owner["fence_token"], owner["fence"] or 0) + 1
            swapped = await conn.execute(update(owners).where(
                owners.c.id == owner["id"],
                owners.c.fence_token == owner["fence_token"],
                owners.c.fence.is_not_distinct_from(owner["fence"]),
                owners.c.handoff_state == "reserved",
            ).values(handoff_state="released", holder=None, expires_at=self.clock(),
                     fence_token=token, fence=token if owner["fence"] is not None else None,
                     updated_at=self.clock()))
            if swapped.rowcount != 1:
                raise ValueError("epic branch ownership changed; preview again")
            return {"owner_row_id": owner["id"], "previous_fence_token": owner["fence_token"],
                    "previous_fence": owner["fence"], "released_fence_token": token}
        return None

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

    async def _epic_reverification_identity_on(self, conn, task_id):
        from src.database.queries.hierarchy_queries import container_flag_exists
        from src.database.tables import archived_tasks

        task = (await conn.execute(select(tasks).where(
            tasks.c.id == task_id,
        ))).mappings().one_or_none()
        if task is None or task["status"] != "COMPLETED" or not task["branch_name"]:
            raise ValueError("aggregate reverification requires a completed, branched epic")
        child = tasks.alias("reverify_child")
        container = await conn.scalar(select(tasks.c.id).where(
            tasks.c.id == task_id,
            container_flag_exists() | select(child.c.id).where(
                child.c.parent_task_id == task_id).exists() | select(archived_tasks.c.id).where(
                    archived_tasks.c.parent_task_id == task_id).exists(),
        ))
        project = (await conn.execute(select(projects).where(
            projects.c.id == task["project_id"],
        ))).mappings().one()
        repo = (await conn.execute(select(repos).where(
            repos.c.id == project["integration_repository_id"],
            repos.c.project_id == project["id"],
        ))).mappings().one_or_none()
        if (not container or project["status"] != "ACTIVE"
                or project["hierarchical_integration_mode"] not in {"hierarchy", "train"}
                or project["hierarchical_integration_draining"]
                or repo is None or task["repo_id"] != repo["id"]):
            raise ValueError("epic is not in an active designated train repository")
        branch = task["branch_name"].removeprefix("refs/heads/")
        if branch == repo["default_branch"].removeprefix("refs/heads/"):
            raise ValueError("an epic cannot own the default branch")
        origins = (await conn.execute(select(task_branch_origins).where(
            task_branch_origins.c.task_id == task_id,
            task_branch_origins.c.repository_id == repo["id"],
            task_branch_origins.c.retired_at.is_(None),
        ))).mappings().all()
        if (len(origins) != 1 or not origins[0]["materialized"]
                or origins[0]["branch_name"].removeprefix("refs/heads/") != branch):
            raise ValueError("epic has no unique live materialized source origin")
        return dict(task), dict(project), dict(repo), dict(origins[0])

    async def _epic_current_completion_id(self, task_id, target):
        requests = await load_delivery_requests(self.db, [task_id],
            repository_id=target.repository_id, target_ref=target.target_ref, reduced=True)
        request = requests[task_id]
        return request.completion_id if request.completed_at is not None else None

    async def reverify_epic(self, task_id, *, dry_run, operator_id,
                            expected_head_sha=None, reason="", expected_project_id=None):
        """Observe/request the normal train; a visited run never implies PASS."""
        from src.git.manager import is_valid_git_oid
        from src.integration.lock import CRITICAL_SECTION_SECONDS

        if not dry_run and (not is_valid_git_oid(expected_head_sha or "") or not reason.strip()):
            raise ValueError("apply requires the preview head and a nonblank reason")
        async with self.db._engine.connect() as conn:
            identity = await self._epic_reverification_identity_on(conn, task_id)
        task, _project, repo, _origin = identity
        if expected_project_id is not None and task["project_id"] != expected_project_id:
            raise ValueError("epic project changed after authorization; preview again")
        target = TrainTarget(task["project_id"], repo["id"],
                            "refs/heads/" + task["branch_name"].removeprefix("refs/heads/"), "epic")
        observed = await self.snapshot(self.db, target)
        if observed is None or observed.error or not observed.target_oid:
            raise ValueError("epic branch cannot be observed")
        before = await self.db.get_task_completion(task_id)
        result = {"task_id": task_id, "project_id": task["project_id"],
                  "target_ref": target.target_ref, "head_sha": observed.target_oid,
                  "previous_completion_id": before.id if before else None,
                  "completion_id": await self._epic_current_completion_id(task_id, target),
                  "dry_run": dry_run}
        if dry_run:
            return {**result, "outcome": "preview"}
        if observed.target_oid != expected_head_sha:
            return {**result, "outcome": "changed"}
        if self.train is None:
            raise ValueError("the Git-first integration train is unavailable")
        async with asyncio.timeout(CRITICAL_SECTION_SECONDS), self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, task["project_id"])
            await self.db._slot_reset_claim_sessions(conn, task_id, lock=True)
            await conn.execute(select(tasks.c.id).where(tasks.c.id == task_id).with_for_update())
            if await self._epic_reverification_identity_on(conn, task_id) != identity:
                raise ValueError("epic identity changed; preview again")
            if task["assigned_agent_id"]:
                raise ValueError("epic still has an assigned agent")
            retired = await self._assert_root_unheld(conn, task, subject="epic",
                action="requesting aggregate reverification", retire_epic_reservation=True)
            if not await observed.observation.is_fresh():
                # Roll back any retirement along with the refused observation.
                await conn.rollback()
                return {**result, "outcome": "changed"}
            if retired:
                await self.db.log_event("integration.epic_reservation_released",
                    project_id=task["project_id"], task_id=task_id,
                    payload=json.dumps({**retired, "operator_id": operator_id, "reason": reason,
                                        "head_sha": expected_head_sha}), conn=conn)
        audit = {**result, "operator_id": operator_id, "reason": reason}
        await self.db.log_event("integration.epic_reverification_requested",
            project_id=task["project_id"], task_id=task_id, payload=json.dumps(audit))
        self.train.wake(*target.key)
        visit = await self.train.request_visit(target)
        result.update(completion_id=await self._epic_current_completion_id(task_id, target),
                      outcome="pending" if visit is None else "visited",
                      state="queued" if visit is None else visit.state,
                      detail=None if visit is None else visit.detail)
        if visit is not None:
            result.update(batch_id=visit.batch_id, candidate_sha=visit.candidate_sha,
                          target_sha=visit.target_sha,
                          blockers=(visit.detail or {}).get("blockers", []))
        await self.db.log_event("integration.epic_reverification_observed",
            project_id=task["project_id"], task_id=task_id,
            payload=json.dumps({**audit, **result}))
        return result

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

    async def settle_batch(self, batch_id, *, dry_run, expected_target_sha, operator_id, reason):
        """Record complete frozen-source inclusion without attempting publication."""
        from src.integration.batches import BatchObservation
        from src.integration.lock import CRITICAL_SECTION_SECONDS
        from src.integration.train_sources import DatabaseBatches
        from src.operator_decisions import history_on, related_on

        if not dry_run and (not expected_target_sha or not reason.strip()):
            raise ValueError("apply requires the previewed target SHA and a nonblank reason")
        store = BatchStore(self.db, clock=self.clock)
        batch = await store.get(batch_id)
        if batch is None:
            raise TrainControlError("unknown_batch", "Git-first batch is missing")
        if batch.epic_sync:
            raise ValueError("settlement requires an ordinary Git-first batch")
        target = TrainTarget(batch.project_id, batch.repository_id, batch.target_ref)
        snapshot = await self.snapshot(self.db, target)
        if snapshot is None or snapshot.error or not snapshot.target_oid:
            raise ValueError("batch target cannot be observed")
        if expected_target_sha and snapshot.target_oid != expected_target_sha:
            raise ValueError("batch target changed; preview again")
        members = await store.members(batch.id)
        # A refresh settles once the epic has its frozen default head as an
        # ancestor, however that merge reached the epic; it publishes nothing.
        proofs = (await _refresh_contains(snapshot, members) if batch.epic_refresh else [
            await snapshot.contains_source(m.task_id, m.source_sha, m.base_sha) for m in members
        ])
        if not members or not all(proof is True for proof in proofs):
            raise TrainControlError("target_not_settled",
                                    "target does not prove every frozen source was delivered")
        observed = snapshot.observation
        candidate = observed.source_heads.get(
            "refs/remotes/origin/" + candidate_ref(batch.id).removeprefix("refs/heads/"))
        result = {
            "outcome": "preview" if dry_run else "settled", "batch_id": batch.id,
            "project_id": batch.project_id, "target_ref": batch.target_ref,
            "target_sha": snapshot.target_oid, "candidate_sha": candidate,
            "members": tuple(m.task_id for m in members), "dry_run": dry_run,
        }
        batches = (self.train.batches if self.train is not None
                   else DatabaseBatches(self.db, clock=self.clock))
        async with asyncio.timeout(CRITICAL_SECTION_SECONDS), self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, batch.project_id)
            await conn.execute(select(projects.c.id).where(
                projects.c.id == batch.project_id).with_for_update())
            await store._target_lock(conn, batch)
            row = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch.id,
            ).with_for_update())).mappings().one()
            if row["trigger"] == "promotion":
                raise ValueError("promotion intents require their promotion settlement protocol")
            if batch.intent == "aborted" or row["lifecycle"] == "aborted":
                raise ValueError("aborted batch cannot be settled")
            if Batch.from_row(row) != batch or await store.members(batch.id) != members:
                raise ValueError("frozen batch changed; preview again")
            decisions = await history_on(conn, batch.project_id,
                                         await related_on(conn, "batch", batch.id))
            if any(decision["active"] for decision in decisions):
                raise ValueError("batch has an active operator hold")
            if not await snapshot.is_fresh():
                raise ValueError("batch target changed; preview again")
            if not dry_run:
                await batches.settle_on(conn, batch, BatchObservation(
                    "delivered", None, snapshot.target_oid))
                await self.db.log_event("integration.batch_settled", project_id=batch.project_id,
                    payload=json.dumps({**result, "operator_id": operator_id,
                                        "reason": reason.strip()}), conn=conn)
        if not dry_run:
            from src.integration.repair import OrdinaryRepairService

            repair = (self.train.repair if self.train is not None
                      else OrdinaryRepairService(self.db, clock=self.clock))
            await repair.settle_green(batch.id, candidate or snapshot.target_oid)
            await batches.cleanup_settled(batch)
        return result

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
        # A refresh member is the frozen default head, which a refresh repair
        # publishes into the epic itself. Containment is not this batch's
        # promotion: only the train's own refresh delivery rule refuses abort.
        proofs = (await _refresh_contains(snapshot, members, same_tree=True)
                  if batch.epic_refresh else [
            await snapshot.contains_source(member.task_id, member.source_sha, member.base_sha)
            for member in members
        ])
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
        roots = [t for t in targets
                 if t.project_id == project_id and t.kind in {"root", "development"}]
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
