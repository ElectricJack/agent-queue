"""Recover checkpoint provenance from an accepted, published container completion.

This never records a completion or delivery, changes a ref, or approves review/CI.
The owner lock, project hierarchy lock and checkpoint CAS are the ordinary writers'
serialization boundaries; Git reads use the canonical retained repository transaction.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from sqlalchemy import insert, or_, select, text, update

from src.database.tables import (
    agents,
    events,
    integration_branch_owners,
    integration_parent_episodes,
    projects,
    repos,
    sessions,
    task_branch_origins,
    task_completion_records,
    task_integration_checkpoints,
    tasks,
    workspaces,
)
from src.git.manager import GitError, RemoteRefState, is_valid_git_oid
from src.integration.models import BranchKey
from src.integration.ownership import BranchOwnership
from src.integration.promotion_contracts import PromotionError

RECOVERY_EVENT = "integration.completed_container_checkpoint_recovered"
_LIVE = ("starting", "running", "draining", "sleeping")


class RecoveryRefused(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _require(condition, reason):
    if not condition:
        raise RecoveryRefused(reason)


async def _one(conn, table, predicate, *, lock=False):
    statement = select(table).where(predicate)
    if lock:
        statement = statement.with_for_update()
    row = (await conn.execute(statement)).mappings().one_or_none()
    return dict(row) if row is not None else None


async def _completion(conn, task_id, *, lock=False):
    statement = (
        select(task_completion_records)
        .where(
            task_completion_records.c.task_id == task_id,
        )
        .order_by(
            task_completion_records.c.completed_at.desc(), task_completion_records.c.id.desc()
        )
    )
    if lock:
        statement = statement.with_for_update()
    row = (await conn.execute(statement.limit(1))).mappings().first()
    return dict(row) if row is not None else None


def _source(completion, branch):
    _require(
        completion is not None
        and completion["outcome"] == "pass"
        and completion["work_outcome"] != "abandoned",
        "latest completion must be an accepted pass",
    )
    _require(
        isinstance(branch, str) and branch and completion["branch"] == branch,
        "completion branch identity missing or differs",
    )
    try:
        commits = json.loads(completion["commits"])
    except (ValueError, TypeError):
        commits = None
    _require(
        isinstance(commits, list)
        and commits
        and all(isinstance(oid, str) and is_valid_git_oid(oid) for oid in commits),
        "completion has no exact final commit provenance",
    )
    return commits[-1]


class CompletedContainerRecovery:
    def __init__(self, db, repository_git, *, git_first, authorize=None, clock=time.time):
        self.db, self.git, self.git_first, self.clock = db, repository_git, git_first, clock
        self.authorize = authorize
        self.ownership = BranchOwnership(db)

    async def _snapshot(self, conn, request, *, lock=False):
        task = await _one(conn, tasks, tasks.c.id == request.task_id, lock=lock)
        _require(task is not None, "task not found")
        project = await _one(conn, projects, projects.c.id == request.project_id, lock=lock)
        repo = await _one(conn, repos, repos.c.id == request.repository_id, lock=lock)
        _require(
            project is not None
            and repo is not None
            and task["project_id"] == repo["project_id"] == project["id"],
            "project/repository identity differs",
        )
        _require(
            project["hierarchical_integration_mode"] in {"hierarchy", "train"}
            and project["integration_repository_id"] == task["repo_id"] == repo["id"],
            "task is not bound to the designated managed repository",
        )
        _require(task["status"] == "COMPLETED", "container must be COMPLETED")
        _require(task["branch_name"] == request.branch, "task branch identity differs")
        _require(task["assigned_agent_id"] is None, "container still has an assigned claimant")
        checkpoint = await _one(
            conn,
            task_integration_checkpoints,
            task_integration_checkpoints.c.task_id == task["id"],
            lock=lock,
        )
        _require(
            checkpoint is not None
            and checkpoint["repository_id"] == repo["id"]
            and checkpoint["branch"] == request.branch,
            "checkpoint identity differs",
        )
        _require(
            checkpoint["episode_id"] is None
            and checkpoint["current_verification_id"] is None
            and checkpoint["state"] == "working",
            "checkpoint has a parent episode or active state",
        )
        _require(
            is_valid_git_oid(checkpoint["checkpoint_sha"] or ""),
            "checkpoint has no exact old source",
        )
        origin = await _one(
            conn,
            task_branch_origins,
            (task_branch_origins.c.task_id == task["id"])
            & task_branch_origins.c.retired_at.is_(None),
            lock=lock,
        )
        _require(
            origin is not None
            and origin["repository_id"] == repo["id"]
            and origin["branch_name"] == request.branch
            and origin["materialized"]
            and origin["reserved"]
            and is_valid_git_oid(origin["base_sha"]),
            "container origin identity/provenance differs",
        )
        owner = await _one(
            conn,
            integration_branch_owners,
            (integration_branch_owners.c.repository_id == repo["id"])
            & (integration_branch_owners.c.ref == request.branch),
            lock=lock,
        )
        _require(
            owner is not None
            and owner["holder"] is None
            and (
                owner["handoff_state"] == "released"
                or owner["handoff_state"] == "reserved"
                and owner["owner_id"] == task["id"]
                and owner["owner_role"] in {"worker", "verifier"}
            ),
            "container branch has a missing/foreign/live owner or pending handoff",
        )
        live = (
            await conn.execute(
                select(sessions.c.id).where(
                    or_(sessions.c.task_id == task["id"], sessions.c.id == owner["session_id"]),
                    sessions.c.state.in_(_LIVE),
                )
            )
        ).all()
        claimant = await conn.scalar(
            select(agents.c.id).where(agents.c.current_task_id == task["id"])
        )
        checkout = await conn.scalar(
            select(workspaces.c.id).where(
                workspaces.c.locked_by_task_id == task["id"],
            )
        )
        _require(
            not live and claimant is None and checkout is None,
            "container still has a live claim/writer",
        )
        episodes = (
            (
                await conn.execute(
                    select(integration_parent_episodes).where(
                        integration_parent_episodes.c.parent_task_id == task["id"],
                    )
                )
            )
            .mappings()
            .all()
        )
        _require(not episodes, "container has a parent episode")
        completion = await _completion(conn, task["id"], lock=lock)
        _source(completion, request.branch)
        statement = select(tasks).where(tasks.c.parent_task_id == task["id"]).order_by(tasks.c.id)
        if lock:
            statement = statement.with_for_update()
        rows = (await conn.execute(statement)).mappings().all()
        _require(rows, "task is not a container")
        children = []
        for row in rows:
            _require(
                row["status"] == "COMPLETED"
                and row["project_id"] == project["id"]
                and row["repo_id"] == repo["id"],
                "child unresolved or wrong repository/project",
            )
            child_completion = await _completion(conn, row["id"], lock=lock)
            _source(child_completion, row["branch_name"])
            children.append({"task": dict(row), "completion": child_completion})
        return dict(
            task=task,
            project=project,
            repository=repo,
            checkpoint=checkpoint,
            origin=origin,
            owner=owner,
            completion=completion,
            children=children,
        )

    async def run(self, request, *, operator_id):
        try:
            self._active()
            _require(operator_id, "integration recovery authority is required")
            async with self.db.immediate() as conn:
                observed = await self._snapshot(conn, request)
            head = _source(observed["completion"], request.branch)
            resolved = await self.git._resolve_repository(request.repository_id)
            _require(
                resolved.repo.project_id == request.project_id,
                "resolved repository project differs",
            )
            origin_digest = hashlib.sha256(resolved.origin_url.encode()).hexdigest()
            if request.expected_head_sha is not None:
                _require(head == request.expected_head_sha, "completion head changed")
            if request.expected_completion_id is not None:
                _require(
                    observed["completion"]["id"] == request.expected_completion_id,
                    "completion identity changed",
                )
            if request.expected_origin_sha256 is not None:
                _require(
                    origin_digest == request.expected_origin_sha256, "canonical origin changed"
                )
            await self.git._ensure_retained_repository(resolved)
            store = resolved.retained_git_dir
            async with self.git.git.arepository_transaction(str(store)):
                await self.git._fetch_all_heads(store, resolved.origin_url)
                await self._remote(store, request.branch, head, resolved.origin_url)
                for source in [
                    observed["origin"]["base_sha"],
                    observed["checkpoint"]["checkpoint_sha"],
                    *[
                        _source(c["completion"], c["task"]["branch_name"])
                        for c in observed["children"]
                    ],
                ]:
                    _require(
                        await self.git._is_ancestor(store, source, head),
                        "published completion does not contain old checkpoint/origin/child source",
                    )
                tree = await self.git._tree_oid(store, head)
                # The owner lock precedes the hierarchy lock, like normal fenced writers.
                async with self.db.immediate() as conn:
                    # Completion rows have soft task references: a task row lock cannot
                    # fence insertion of a newer completion. Freeze this append-only
                    # table before other DB locks for the short final proof/CAS.
                    await conn.execute(text("LOCK TABLE task_completion_records IN SHARE MODE"))
                    await self.ownership._locked_row(
                        conn,
                        BranchKey(
                            repository_id=request.repository_id,
                            branch=request.branch,
                        ),
                    )
                    await self.db.lock_hierarchy_project(conn, request.project_id)
                    current = await self._snapshot(conn, request, lock=True)
                    _require(
                        current == observed,
                        "task/completion/children/owner/checkpoint changed during Git proof",
                    )
                    latest_repo = await self.git._resolve_repository(request.repository_id)
                    _require(
                        latest_repo == resolved,
                        "canonical repository/origin changed during Git proof",
                    )
                    # Authenticated fresh remote observation after every database recheck,
                    # still under the repository and owner locks. No remote writer is invoked.
                    await self._remote(store, request.branch, head, resolved.origin_url)
                    self._active()
                    if self.authorize is not None:
                        principal, refusal = await self.authorize()
                        _require(
                            refusal is None and principal == operator_id,
                            "integration recovery authority changed during Git proof",
                        )
                    checkpoint = current["checkpoint"]
                    payload = {
                        "task_id": request.task_id,
                        "project_id": request.project_id,
                        "repository_id": request.repository_id,
                        "branch": request.branch,
                        "head_sha": head,
                        "completion_id": current["completion"]["id"],
                        "origin_sha256": origin_digest,
                        "snapshot_sha256": _digest(current),
                        "generation": checkpoint["generation"],
                        "version": checkpoint["version"],
                        "old_head_sha": checkpoint["checkpoint_sha"],
                        "tree_sha": tree,
                        "remote_head_sha": head,
                    }
                    if checkpoint["checkpoint_sha"] == head:
                        audit = (
                            await conn.execute(
                                select(events.c.payload)
                                .where(
                                    events.c.event_type == RECOVERY_EVENT,
                                    events.c.task_id == request.task_id,
                                )
                                .order_by(events.c.id.desc())
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                        receipt = json.loads(audit) if audit else None
                        _require(
                            receipt is not None
                            and receipt["after_snapshot_sha256"] == _digest(current)
                            and (
                                request.expected_version is None
                                or request.expected_version == receipt["before"]["version"]
                            )
                            and (
                                request.expected_generation is None
                                or request.expected_generation == receipt["before"]["generation"]
                            )
                            and (
                                request.expected_snapshot_sha256 is None
                                or request.expected_snapshot_sha256
                                == receipt["before_snapshot_sha256"]
                            ),
                            "checkpoint already current without matching recovery receipt",
                        )
                        return {"outcome": "already_recovered", **payload, "receipt": receipt}
                    for name, actual in (
                        ("expected_generation", checkpoint["generation"]),
                        ("expected_version", checkpoint["version"]),
                        ("expected_snapshot_sha256", _digest(current)),
                    ):
                        expected = getattr(request, name)
                        _require(expected is None or expected == actual, f"{name} changed")
                    if request.dry_run:
                        return {"outcome": "preview", **payload}
                    now = self.clock()
                    receipt_id = str(uuid.uuid4())
                    updated = {
                        **checkpoint,
                        "checkpoint_sha": head,
                        "version": checkpoint["version"] + 1,
                        "updated_at": now,
                    }
                    after = {**current, "checkpoint": updated}
                    receipt = {
                        "id": receipt_id,
                        "operator_id": operator_id,
                        "reason": request.reason,
                        **payload,
                        "before": checkpoint,
                        "after": updated,
                        "before_snapshot_sha256": _digest(current),
                        "after_snapshot_sha256": _digest(after),
                        "owner": current["owner"],
                        "origin_id": current["origin"]["id"],
                        "children": [
                            {
                                "task_id": c["task"]["id"],
                                "completion_id": c["completion"]["id"],
                                "head_sha": _source(c["completion"], c["task"]["branch_name"]),
                            }
                            for c in current["children"]
                        ],
                        "observed_at": now,
                        "delivery": False,
                    }
                    result = await conn.execute(
                        update(task_integration_checkpoints)
                        .where(
                            task_integration_checkpoints.c.task_id == request.task_id,
                            task_integration_checkpoints.c.version == checkpoint["version"],
                            task_integration_checkpoints.c.generation == checkpoint["generation"],
                            task_integration_checkpoints.c.checkpoint_sha
                            == checkpoint["checkpoint_sha"],
                        )
                        .values(checkpoint_sha=head, version=updated["version"], updated_at=now)
                    )
                    _require(result.rowcount == 1, "checkpoint CAS changed")
                    await conn.execute(
                        insert(events).values(
                            event_type=RECOVERY_EVENT,
                            project_id=request.project_id,
                            task_id=request.task_id,
                            payload=json.dumps(receipt, sort_keys=True),
                            timestamp=now,
                        )
                    )
                    return {"outcome": "recovered", **payload, "receipt": receipt}
        except (ValueError, PromotionError, GitError, OSError) as exc:
            return {"outcome": "refused", "task_id": request.task_id, "reason": str(exc)}

    def _active(self):
        mode = self.git_first() if callable(self.git_first) else self.git_first
        _require(mode == "active", "git_first must be active")

    async def _remote(self, store, branch, head, origin_url):
        remote = await self.git.git.als_remote_ref(str(store), branch, repository_url=origin_url)
        _require(
            remote.state is RemoteRefState.PRESENT and remote.oid == head,
            "authenticated current remote differs from exact completion head",
        )
