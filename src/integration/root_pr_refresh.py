"""Command-owned mechanical root refresh and original-author conflict feedback."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.queries.task_queries import DEVELOPMENT_COMPLETION_ID_KEY
from src.database.tables import (
    integration_batch_members, integration_batches, projects, sessions,
    task_branch_origins, task_completion_records, task_context,
    task_integration_checkpoints, task_metadata, tasks,
)
from src.git.manager import GitError, commit_identity, is_valid_git_oid
from src.integration.delivery_truth import DeliveryRequest, load_delivery_requests
from src.integration.lock import BranchLock
from src.integration.manual_control import enabled_train_write
from src.integration.models import BranchKey
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.integration.regeneration import GeneratedMergeConflict, generated_paths, merge_generated_tree
from src.models import TaskStatus
from src.task_templates import render_task_template

REFRESH_KEY = "root_pr_conflict_refresh"


@dataclass(frozen=True)
class RootPRConflict:
    """A server-observed refusal, bound to the fetched head and default tip."""

    request: DeliveryRequest
    source_sha: str
    source_base_sha: str
    default_sha: str
    pr_url: str
    policy_generation: int
    policy: dict

    @property
    def key(self):
        return hashlib.sha256(repr((self.source_sha, self.default_sha)).encode()).hexdigest()


async def admission_source_on(conn, request, source_sha, generation, policy):
    """A bot merge retains the original source's admission evidence, never its CI verdict."""
    raw = await conn.scalar(select(task_metadata.c.value).where(
        task_metadata.c.task_id == request.task_id, task_metadata.c.key == REFRESH_KEY,
    ))
    current = (json.loads(raw).get("current") or {}) if raw else {}
    if (current.get("refreshed_sha") == source_sha
            and current.get("completion_id") == request.completion_id
            and current.get("policy_generation") == generation and current.get("policy") == policy):
        return current["checked_sha"]
    return source_sha


async def refresh_root_pr(handler, observation, gitops, snapshot):
    """Merge before asking an agent; persist deduplication with the lifecycle change.

    Publication uses the ordinary branch fence and expected-old transport. A
    prepared merge is persisted before its push so a lost response/restart can
    finish its immutable completion without pushing or reopening another source.
    """
    if (not isinstance(observation, RootPRConflict)
            or any(not is_valid_git_oid(sha) for sha in (
                observation.source_sha, observation.source_base_sha, observation.default_sha))
            or snapshot.error):
        return {"success": False, "outcome": "invalid"}
    db, request = handler.db, observation.request
    if request.reported_source != observation.source_sha or not request.completion_id:
        return {"success": True, "outcome": "stale"}
    repo = await gitops.repository(request)
    locks = BranchLock(db)
    fence = await locks.acquire(
        BranchKey(repository_id=request.repository_id, branch=request.branch_name),
        "service:root-pr-refresh", role="integration",
    )

    async def current_on(conn):
        task = (await conn.execute(select(tasks).where(
            tasks.c.id == request.task_id).with_for_update())).mappings().one_or_none()
        project = (await conn.execute(select(projects).where(
            projects.c.id == request.project_id))).mappings().one_or_none()
        current = (await load_delivery_requests(db, [request.task_id],
            repository_id=request.repository_id, target_ref=request.target_ref,
            conn=conn, reduced=True)).get(request.task_id)
        origin = await conn.scalar(select(task_branch_origins.c.id).where(
            task_branch_origins.c.task_id == request.task_id,
            task_branch_origins.c.repository_id == request.repository_id,
            task_branch_origins.c.branch_name == request.branch_name,
            task_branch_origins.c.base_sha == observation.source_base_sha,
            task_branch_origins.c.retired_at.is_(None),
        ).limit(1))
        active = await conn.scalar(select(sessions.c.id).where(
            sessions.c.task_id == request.task_id,
            sessions.c.state.in_(("starting", "running", "draining")),
        ).limit(1))
        frozen = await conn.scalar(select(integration_batch_members.c.batch_id).join(
            integration_batches, integration_batches.c.id == integration_batch_members.c.batch_id,
        ).where(integration_batch_members.c.task_id == request.task_id,
                integration_batches.c.intent.in_(("open", "paused")),
                integration_batches.c.lifecycle.not_in(("promoted", "aborted", "superseded")),
        ).limit(1))
        if (task is None or project is None or current != request
                or task["status"] != "COMPLETED" or task["assigned_agent_id"] or active or frozen
                or task["parent_task_id"] is not None or task["pr_url"] != observation.pr_url
                or not origin or project["hierarchical_integration_mode"] not in (
                    "train", "hierarchy", "development")
                or project["hierarchical_integration_generation"] != observation.policy_generation
                or (project["hierarchical_integration_policy"] or {}) != observation.policy):
            return None
        return task

    async def authorize():
        async with db.immediate() as conn:
            return await current_on(conn) is not None

    try:
        async with db.immediate() as conn:
            task = await current_on(conn)
            if task is None:
                return {"success": True, "outcome": "stale"}
            raw = await conn.scalar(select(task_metadata.c.value).where(
                task_metadata.c.task_id == request.task_id, task_metadata.c.key == REFRESH_KEY,
            ))
            history = json.loads(raw) if raw else {}
            pair = (history.get("pairs") or {}).get(observation.key)
            if pair and pair["outcome"] != "prepared":
                return {**pair, "success": True, "outcome": "already_handled"}
            if pair and pair.get("source_completion_id") != request.completion_id:
                return {"success": True, "outcome": "stale"}
            checked = await admission_source_on(conn, request, observation.source_sha,
                observation.policy_generation, observation.policy)

        actual = await gitops.remote(repo, "refs/heads/" + request.branch_name.removeprefix("refs/heads/"))
        if actual != observation.source_sha and (not pair or actual != pair.get("refreshed_sha")):
            return {"success": True, "outcome": "stale"}
        if (not pair or actual != pair.get("refreshed_sha")) and (
                snapshot.target_oid != observation.default_sha
                or await gitops.remote(repo, request.target_ref) != observation.default_sha):
            return {"success": True, "outcome": "stale"}
        await gitops.run(repo, "fetch", "--no-tags", snapshot.observation.store,
                         observation.source_sha, observation.default_sha)
        if pair is None:
            try:
                tree = await merge_generated_tree(gitops.git, repo.store,
                    ["--no-replace-objects", "merge-tree", "--write-tree",
                     observation.source_sha, observation.default_sha],
                    command=repo.regenerate, timeout_seconds=repo.regenerate_timeout_seconds)
            except GeneratedMergeConflict as exc:
                # Regeneration/configuration failures must never dispatch an
                # author as though their source contained a real conflict.
                files = {line.split("\t", 1)[1] for line in exc.stdout.splitlines()[1:]
                         if "\t" in line and line.split("\t", 1)[0].endswith((" 1", " 2", " 3"))}
                merged_tree = (exc.stdout.splitlines() or [""])[0]
                generated = await generated_paths(gitops.git, repo.store, merged_tree, files)
                files -= generated
                if exc.reason or not files:
                    raise GitError(exc.reason or "merge conflict diagnostics unavailable") from exc
                feedback = render_task_template("root-pr-conflict-reopen",
                    default_ref=request.target_ref, default_sha=observation.default_sha,
                    source_branch=request.branch_name, source_sha=observation.source_sha,
                    conflicting_paths="\n".join(f"- {path}" for path in sorted(files)),
                    pr_url=observation.pr_url, pair_key=observation.key)
                delivery = await db.observe_removal_delivery([request.task_id], for_mutation=True)
                async with db.immediate() as conn:
                    await db.lock_hierarchy_project(conn, request.project_id)
                    task = await current_on(conn)
                    if task is None:
                        return {"success": True, "outcome": "stale"}
                    transition = await db._apply_transition(conn, request.task_id, TaskStatus.READY,
                        integration_delivery=delivery, context="reopen_with_feedback",
                        _preserve_collected_children=True,
                        assigned_agent_id=None, retry_count=0,
                        description=task["description"] + "\n\n---\n**Reopen Feedback:**\n" + feedback)
                    pair = {"outcome": "reopened", "source_sha": observation.source_sha,
                            "default_sha": observation.default_sha, "files": sorted(files)}
                    await db._upsert_meta(request.task_id, REFRESH_KEY,
                        {**history, "pairs": {**history.get("pairs", {}), observation.key: pair}},
                        conn=conn)
                    await conn.execute(insert(task_context).values(
                        id="root-conflict-" + request.task_id + "-" + observation.key,
                        task_id=request.task_id, type="reopen_feedback",
                        label="Root PR merge conflict", content=feedback, created_at=time.time()))
                    await db.add_task_comment(request.task_id, feedback, author_kind="agent",
                        author_id="service:integration-admission", conn=conn)
                    await db.log_event("integration.root_pr_conflict_reopened",
                        project_id=request.project_id, task_id=request.task_id,
                        payload=json.dumps(pair), conn=conn)
                await db.log_blocked_flips(transition.flipped)
                await db._notify_settled(transition.settled)
                await db._notify_ready(transition.ready)
                return {"success": True, **pair}
            with commit_identity(gitops.git.resolve_commit_identity()):
                merged = await gitops.run(repo, "commit-tree", tree,
                    "-p", observation.source_sha, "-p", observation.default_sha,
                    "-m", f"Merge {request.target_ref} into {request.branch_name} [aq refresh]")
            pair = {"outcome": "prepared", "source_sha": observation.source_sha,
                    "default_sha": observation.default_sha, "refreshed_sha": merged,
                    "source_completion_id": request.completion_id,
                    "checked_sha": checked, "completion_id": "root-refresh-" + hashlib.sha256(
                        repr((request.task_id, observation.key)).encode()).hexdigest(),
                    "policy_generation": observation.policy_generation, "policy": observation.policy}
            async with db.immediate() as conn:
                if await current_on(conn) is None:
                    return {"success": True, "outcome": "stale"}
                await db._upsert_meta(request.task_id, REFRESH_KEY,
                    {**history, "pairs": {**history.get("pairs", {}), observation.key: pair}}, conn=conn)

        if actual != pair["refreshed_sha"]:
            async with enabled_train_write(db, request.repository_id):
                await locks.fenced_push(fence, git=gitops.git, checkout_path=str(repo.store),
                    repository=repo.binding, tip_oid=pair["refreshed_sha"],
                    expected_old_oid=observation.source_sha, authorize=authorize)
        await GitProvenance(gitops.git, str(repo.store),
            repository_url=(await db.get_repo(request.repository_id)).url).write_completion(
                CompletedSource(CompletionIdentity(request.project_id, request.repository_id,
                    request.task_id, pair["completion_id"]), pair["refreshed_sha"]),
                claim_epoch=request.claim_epoch)
        async with db.immediate() as conn:
            if await current_on(conn) is None:
                return {"success": True, "outcome": "stale"}
            await conn.execute(pg_insert(task_completion_records).values(
                id=pair["completion_id"], task_id=request.task_id, outcome="pass",
                branch=request.branch_name, commits=json.dumps([pair["refreshed_sha"]]),
                completed_at=time.time(), summary="Mechanically merged the default branch; "
                "admission retains the original source checks; batch-tip CI gates delivery.",
                notes=json.dumps({"root_pr_refresh": {
                    "completion_id": request.completion_id, "source_sha": observation.source_sha,
                    "default_sha": observation.default_sha}}),
            ).on_conflict_do_nothing(index_elements=["id"]))
            await db._upsert_meta(request.task_id, DEVELOPMENT_COMPLETION_ID_KEY,
                                  pair["completion_id"], conn=conn)
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == request.task_id,
            ).values(checkpoint_sha=pair["refreshed_sha"],
                generation=task_integration_checkpoints.c.generation + 1,
                version=task_integration_checkpoints.c.version + 1, updated_at=time.time()))
            pair = {**pair, "outcome": "refreshed"}
            await db._upsert_meta(request.task_id, REFRESH_KEY,
                {**history, "pairs": {**history.get("pairs", {}), observation.key: pair},
                 "current": pair}, conn=conn)
            await db.log_event("integration.root_pr_refreshed", project_id=request.project_id,
                task_id=request.task_id, payload=json.dumps(pair), conn=conn)
        return {"success": True, **pair}
    finally:
        await locks.release(fence)
