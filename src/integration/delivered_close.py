"""Local operator completion of exact source already delivered to the default branch."""

from __future__ import annotations

import hashlib
import json
import time

from sqlalchemy import delete, insert, select, update

from src.database.queries.integration_state_queries import (
    session_attached_clause,
    unresolved_claim_clause,
)
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_ref_mutations,
    projects,
    repos,
    sessions,
    task_branch_origins,
    task_completion_records,
    task_metadata,
    task_subtasks,
    tasks,
    workspaces,
)
from src.git.manager import RemoteRefState
from src.integration.lock import BranchLock
from src.integration.models import BranchKey
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.integration.train import TrainTarget
from src.integration.train_sources import project_snapshot
from src.models import TaskStatus


class DeliveredClose:
    def __init__(self, db, *, snapshot=project_snapshot):
        self.db, self.snapshot = db, snapshot

    async def _identity_on(self, conn, request):
        task = (
            (
                await conn.execute(
                    select(tasks).where(
                        tasks.c.id == request.task_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if task is None or task["project_id"] != request.project_id:
            raise ValueError("task does not belong to the explicitly selected project")
        project = (
            (
                await conn.execute(
                    select(projects).where(
                        projects.c.id == request.project_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        repository_id = project["integration_repository_id"]
        repo = (
            (
                await conn.execute(
                    select(repos).where(
                        repos.c.id == repository_id,
                        repos.c.project_id == request.project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if repo is None or task["repo_id"] not in (None, repository_id):
            raise ValueError("task has no unambiguous designated project repository")
        origins = (
            (
                await conn.execute(
                    select(task_branch_origins).where(
                        task_branch_origins.c.task_id == task["id"],
                        task_branch_origins.c.retired_at.is_(None),
                    )
                )
            )
            .mappings()
            .all()
        )
        branch = task["branch_name"]
        if not branch:
            raise ValueError("task must record its canonical branch before recording delivery")
        if (
            len(origins) > 1
            or origins
            and (
                origins[0]["repository_id"] != repository_id
                or origins[0]["branch_name"] != branch
                or origins[0]["base_sha"] != request.base_sha
                or not origins[0]["materialized"]
            )
        ):
            raise ValueError(
                "recorded origin does not match the explicit repository, branch and base"
            )
        return dict(task), dict(repo), dict(origins[0]) if origins else None

    async def _unheld_on(self, conn, task):
        from src.database.queries.task_subtask_queries import OPEN_SUBTASK_STATUSES

        if task["assigned_agent_id"] or await conn.scalar(
            select(sessions.c.id)
            .where(
                sessions.c.task_id == task["id"],
                session_attached_clause(),
            )
            .limit(1)
        ):
            raise ValueError(
                "task still has an assigned agent or live writer; use supported release"
            )
        if await conn.scalar(
            select(workspaces.c.id)
            .where(
                workspaces.c.locked_by_task_id == task["id"],
            )
            .limit(1)
        ) or await conn.scalar(
            select(task_metadata.c.value).where(
                task_metadata.c.task_id == task["id"],
                task_metadata.c.key == "claimed_by_session",
                unresolved_claim_clause(),
            )
        ):
            raise ValueError("task still has a workspace or claim; use supported slot reset")
        owner = (
            await BranchLock(self.db).lock_on(
                conn,
                BranchKey(
                    repository_id=task["repo_id"],
                    branch=task["branch_name"],
                ),
            )
            if task["repo_id"]
            else None
        )
        if owner and owner["handoff_state"] != "released":
            if (
                owner["holder"]
                or owner["handoff_state"] != "reserved"
                or owner["session_id"]
                or owner["workspace_id"]
                or owner["owner_id"] != task["id"]
                or owner["owner_role"] not in ("worker", "repair")
            ):
                raise ValueError("task still has branch ownership; use supported owner release")
            from src.integration.owner_recovery import _Refusal
            from src.integration.stale_owners import _fence_blocker

            try:
                await _fence_blocker(conn, owner, include_aborted=False)
            except _Refusal as exc:
                raise ValueError(exc.detail) from exc
            if await conn.scalar(
                select(integration_candidate_ref_mutations.c.id)
                .where(
                    integration_candidate_ref_mutations.c.repository_id == task["repo_id"],
                    integration_candidate_ref_mutations.c.branch == task["branch_name"],
                    integration_candidate_ref_mutations.c.state == "reserved",
                )
                .limit(1)
            ):
                raise ValueError("branch still has an external mutation claim")
        if await conn.scalar(
            select(tasks.c.id)
            .where(
                tasks.c.parent_task_id == task["id"],
                tasks.c.status.not_in(("COMPLETED", "FAILED")),
            )
            .limit(1)
        ):
            raise ValueError("task has open children; record delivered children first")
        if await conn.scalar(
            select(task_subtasks.c.id)
            .where(
                task_subtasks.c.task_id == task["id"],
                task_subtasks.c.status.in_(OPEN_SUBTASK_STATUSES),
            )
            .limit(1)
        ):
            raise ValueError("task has open checklist subtasks; resolve them first")
        if any(item.get("required", True) for item in json.loads(task["deliverables"])):
            raise ValueError(
                "task has required deliverables; use the normal verified completion path"
            )
        if await conn.scalar(
            select(integration_batches.c.id)
            .where(
                integration_batches.c.id.in_(
                    select(integration_batch_members.c.batch_id).where(
                        integration_batch_members.c.task_id == task["id"],
                    )
                ),
                integration_batches.c.intent != "aborted",
                integration_batches.c.lifecycle != "promoted",
            )
            .limit(1)
        ):
            raise ValueError("task belongs to an open integration batch; resolve that batch first")
        if await conn.scalar(
            select(task_metadata.c.value).where(
                task_metadata.c.task_id == task["id"],
                task_metadata.c.key == "manual_pause",
            )
        ):
            raise ValueError(
                "task has an operator pause; use resume_task before recording delivery"
            )
        return owner

    async def record(self, request):
        async with self.db._engine.connect() as conn:
            identity = await self._identity_on(conn, request)
        task, repo, origin = identity
        target = TrainTarget(
            task["project_id"],
            repo["id"],
            "refs/heads/" + repo["default_branch"].removeprefix("refs/heads/"),
        )
        snapshot = await self.snapshot(self.db, target)
        if snapshot is None or snapshot.error or not snapshot.target_oid:
            raise ValueError("designated default branch cannot be observed")
        observed = snapshot.observation
        proof = GitProvenance(observed.git, observed.store, repository_url=observed.repository_url)
        if request.source_sha == request.base_sha:
            raise ValueError("source equals base; use the verified no-code completion command")
        await proof.exact(request.source_sha)
        await proof.exact(request.base_sha)
        if not await proof.ancestor(request.base_sha, request.source_sha):
            raise ValueError("exact source does not descend from the explicit base")
        if not await proof.ancestor(request.source_sha, snapshot.target_oid):
            raise ValueError("exact source is not contained in the designated default branch")
        generation = (
            "external-delivered-"
            + hashlib.sha256(
                repr(
                    (
                        task["project_id"],
                        repo["id"],
                        task["id"],
                        task["legacy_completion_id"],
                        request.source_sha,
                        request.base_sha,
                    )
                ).encode()
            ).hexdigest()
        )
        if task["status"] == "COMPLETED":
            existing = await self.db.get_task_completion(task["id"])
            notes = json.loads(existing.notes or "{}") if existing else {}
            if (
                existing
                and existing.commits == [request.source_sha]
                and (
                    notes.get("authority") == "local-operator"
                    and notes.get("base_sha") == request.base_sha
                )
            ):
                generation = existing.id
        result = {
            "outcome": "preview" if request.dry_run else "recorded",
            "task_id": task["id"],
            "project_id": task["project_id"],
            "completion_id": generation,
            "source_sha": request.source_sha,
            "base_sha": request.base_sha,
            "target_sha": snapshot.target_oid,
            "target_ref": target.target_ref,
            "dry_run": request.dry_run,
        }
        transition = None
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, task["project_id"])
            await self.db._slot_reset_claim_sessions(conn, task["id"], lock=True)
            await conn.execute(select(tasks.c.id).where(tasks.c.id == task["id"]).with_for_update())
            if await self._identity_on(conn, request) != identity:
                raise ValueError("task, repository or origin changed while observing delivery")
            owner = await self._unheld_on(conn, task | {"repo_id": repo["id"]})
            remote = await observed.git.als_remote_ref(
                observed.store,
                target.target_ref.removeprefix("refs/heads/"),
                repository_url=observed.repository_url,
            )
            if remote.state is not RemoteRefState.PRESENT or remote.oid != snapshot.target_oid:
                raise ValueError("designated default branch changed; preview again")
            if request.dry_run:
                return result
            existing = (
                (
                    await conn.execute(
                        select(task_completion_records)
                        .where(
                            task_completion_records.c.task_id == task["id"],
                        )
                        .order_by(task_completion_records.c.completed_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if task["status"] == "COMPLETED" and existing and existing["id"] != generation:
                raise ValueError(
                    "completed task already has another generation; preserve its completion"
                )
            await proof.write_completion(
                CompletedSource(
                    CompletionIdentity(task["project_id"], repo["id"], task["id"], generation),
                    request.source_sha,
                ),
                claim_epoch=task["claim_epoch"],
                artifact=True,
            )
            remote = await observed.git.als_remote_ref(
                observed.store,
                target.target_ref.removeprefix("refs/heads/"),
                repository_url=observed.repository_url,
            )
            if remote.state is not RemoteRefState.PRESENT or remote.oid != snapshot.target_oid:
                raise ValueError("default branch changed during completion publication")
            if owner and owner["handoff_state"] == "reserved":
                await conn.execute(
                    update(integration_branch_owners)
                    .where(
                        integration_branch_owners.c.id == owner["id"],
                    )
                    .values(
                        handoff_state="released",
                        fence_token=owner["fence_token"] + 1,
                        updated_at=time.time(),
                    )
                )
                await self.db.log_event(
                    "integration.branch_owner_released",
                    project_id=task["project_id"],
                    task_id=task["id"],
                    conn=conn,
                    payload=json.dumps(
                        {
                            "control": "record_delivered",
                            "owner_row_id": owner["id"],
                            "principal": "human:local-operator",
                            "completion_id": generation,
                            "source_sha": request.source_sha,
                            "target_sha": snapshot.target_oid,
                            "old_fence_token": owner["fence_token"],
                            "released_fence_token": owner["fence_token"] + 1,
                        }
                    ),
                )
            if origin is None:
                await conn.execute(
                    insert(task_branch_origins).values(
                        id=generation + "-origin",
                        task_id=task["id"],
                        repository_id=repo["id"],
                        branch_name=task["branch_name"],
                        base_sha=request.base_sha,
                        creation_generation=task["claim_epoch"],
                        reserved=True,
                        materialized=True,
                        created_at=time.time(),
                    )
                )
            if existing is None or existing["id"] != generation:
                await conn.execute(
                    insert(task_completion_records).values(
                        id=generation,
                        task_id=task["id"],
                        outcome="pass",
                        work_outcome="shipped",
                        branch=task["branch_name"],
                        commits=json.dumps([request.source_sha]),
                        summary=request.reason,
                        changes=request.reason,
                        verification=f"Exact source {request.source_sha} contained in {target.target_ref}@{snapshot.target_oid}",
                        tests=json.dumps(request.tests),
                        commands=json.dumps(request.commands),
                        notes=json.dumps(
                            {"authority": "local-operator", "base_sha": request.base_sha}
                        ),
                        completed_at=time.time(),
                    )
                )
            from src.database.queries.task_queries import DEVELOPMENT_COMPLETION_ID_KEY

            await self.db._upsert_meta(task["id"], "work_outcome", "shipped", conn=conn)
            await conn.execute(
                delete(task_metadata).where(
                    task_metadata.c.task_id == task["id"],
                    task_metadata.c.key == "needs_attention",
                )
            )
            if task["status"] != "COMPLETED":
                transition = await self.db._apply_transition(
                    conn,
                    task["id"],
                    TaskStatus.COMPLETED,
                    context="operator_record_delivered",
                    force=True,
                    repo_id=repo["id"],
                )
            await self.db._upsert_meta(
                task["id"], DEVELOPMENT_COMPLETION_ID_KEY, generation, conn=conn
            )
            flipped = await self.db.recompute_blocked({task["id"]}, conn=conn)
        await self.db.log_blocked_flips(flipped | (transition.flipped if transition else set()))
        if transition:
            await self.db._notify_settled(transition.settled)
            await self.db._notify_ready(transition.ready)
        return result
