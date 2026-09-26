"""Restore a displaced collector without restarting a parent worker."""

from __future__ import annotations

import json
import time

from sqlalchemy import delete, insert, select

from src.database.queries.task_queries import TERMINAL_BLOCKED_META_KEY
from src.database.tables import (
    events,
    integration_branch_owners,
    integration_parent_episodes,
    integration_repair_operations,
    projects,
    repos,
    sessions,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)
from src.models import TaskStatus


class CollectingParentRecovery:
    """Dry-run-first recovery of a BLOCKED parent with a reserved collector."""

    def __init__(self, db, *, clock=time.time):
        self.db = db
        self.clock = clock

    async def diagnose(self, task_id):
        async with self.db._engine.connect() as conn:
            return await self._diagnose_on(conn, task_id)

    async def _diagnose_on(self, conn, task_id, *, lock=False):
        def guarded(statement):
            return statement.with_for_update() if lock else statement

        parent = (
            (await conn.execute(select(tasks).where(tasks.c.id == task_id)))
            .mappings()
            .one_or_none()
        )
        if parent is None:
            return {"outcome": "not_found", "task_id": task_id}
        base = {
            "task_id": task_id,
            "project_id": parent["project_id"],
            "branch": parent["branch_name"],
            "kind": "collection",
        }

        def refused(reason):
            return {**base, "outcome": "blocked", "reason": reason}

        project = (
            (await conn.execute(select(projects).where(projects.c.id == parent["project_id"])))
            .mappings()
            .one()
        )
        if (
            project["hierarchical_integration_mode"] != "train"
            or project["hierarchical_integration_draining"]
        ):
            return refused("the project is not actively collecting in train mode")
        checkpoint = (
            (
                await conn.execute(
                    guarded(
                        select(task_integration_checkpoints).where(
                            task_integration_checkpoints.c.task_id == task_id
                        )
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if checkpoint is None or checkpoint["state"] != "awaiting_children":
            return refused("the parent checkpoint is not awaiting children")
        base["head_sha"] = checkpoint["checkpoint_sha"]
        base["checkpoint"] = {
            key: checkpoint[key] for key in ("state", "generation", "episode_id", "checkpoint_sha")
        }
        if parent["status"] != "BLOCKED" or parent["assigned_agent_id"] is not None:
            return refused("the parent is not an unassigned BLOCKED collector")
        repo = (
            (
                await conn.execute(
                    select(repos).where(
                        repos.c.id == parent["repo_id"], repos.c.project_id == parent["project_id"]
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            repo is None
            or repo["id"] != project["integration_repository_id"]
            or checkpoint["repository_id"] != repo["id"]
            or checkpoint["branch"] != parent["branch_name"]
            or not parent["branch_name"]
            or parent["branch_name"].removeprefix("refs/heads/") == repo["default_branch"]
        ):
            return refused("the parent's canonical integration branch identity is inconsistent")
        episode = (
            (
                await conn.execute(
                    select(integration_parent_episodes).where(
                        integration_parent_episodes.c.id == checkpoint["episode_id"],
                        integration_parent_episodes.c.parent_task_id == task_id,
                        integration_parent_episodes.c.repository_id == repo["id"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        operation = (
            (
                await conn.execute(
                    guarded(
                        select(integration_repair_operations).where(
                            integration_repair_operations.c.parent_task_id == task_id,
                            integration_repair_operations.c.episode_id == checkpoint["episode_id"],
                            integration_repair_operations.c.target_kind == "parent",
                        )
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            episode is None
            or operation is None
            or int(checkpoint["generation"]) < int(episode["generation"])
            or episode["created_at"] < parent["created_at"]
        ):
            return refused("the parent has no matching collection episode and operation")
        if operation["state"] not in {"active", "escalated"}:
            return refused(
                "the operation is not collecting; human_required repairs need integration resume"
            )
        metadata = dict(
            (
                await conn.execute(
                    select(task_metadata.c.key, task_metadata.c.value).where(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key.in_(
                            ("manual_pause", TERMINAL_BLOCKED_META_KEY, "needs_attention")
                        ),
                    )
                )
            ).all()
        )
        if "manual_pause" in metadata or TERMINAL_BLOCKED_META_KEY in metadata:
            return refused(
                "an operator hold or terminal failure requires its own recovery decision"
            )
        attention = metadata.get("needs_attention")
        if attention is not None:
            try:
                attention = json.loads(attention)
            except (ValueError, TypeError):
                pass
            if attention != "session_not_live":
                return refused("the parent's operational failure is not a stale stopped session")
        live = await conn.scalar(
            select(sessions.c.id)
            .where(
                sessions.c.task_id == task_id,
                sessions.c.state.in_(("starting", "running", "draining")),
            )
            .limit(1)
        )
        attached = await conn.scalar(
            select(workspaces.c.id)
            .where(
                workspaces.c.locked_by_task_id == task_id,
            )
            .limit(1)
        )
        if live is not None or attached is not None:
            return refused("a session or workspace still holds the parent")
        owner = (
            (
                await conn.execute(
                    guarded(
                        select(integration_branch_owners).where(
                            integration_branch_owners.c.repository_id == repo["id"],
                            integration_branch_owners.c.ref == checkpoint["branch"],
                        )
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            owner is None
            or owner["owner_role"] != "collector"
            or owner["owner_id"] != operation["id"]
            or owner["handoff_state"] != "reserved"
            or owner["session_id"] is not None
            or owner["workspace_id"] is not None
        ):
            return refused(
                "the exact collection operation does not hold a detached collector fence"
            )
        base["checkpoint"].update(
            operation_id=operation["id"],
            collector_fence_token=owner["fence_token"],
        )
        return {
            **base,
            "outcome": "would_collect",
            "reason": "restore PAUSED collection under the existing episode and collector fence",
        }

    async def run(
        self, task_id, *, dry_run=True, expected_head_sha=None, reason=None, operator_id=None
    ):
        diagnosis = await self.diagnose(task_id)
        if dry_run or diagnosis["outcome"] != "would_collect":
            return diagnosis
        if not reason or expected_head_sha != diagnosis["head_sha"]:
            return {
                **diagnosis,
                "outcome": "changed",
                "reason": "supply the reported head and an audit reason",
            }
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, diagnosis["project_id"])
            await conn.execute(
                select(projects.c.id)
                .where(
                    projects.c.id == diagnosis["project_id"],
                )
                .with_for_update()
            )
            await self.db._lock_task_row(conn, task_id)
            current = await self._diagnose_on(conn, task_id, lock=True)
            if current != diagnosis:
                return {
                    **current,
                    "outcome": "changed",
                    "reason": "collection state changed; repeat the dry run",
                }
            transition = await self.db._apply_transition(
                conn,
                task_id,
                TaskStatus.PAUSED,
                context="integration_collection_redrive",
                force=True,
                assigned_agent_id=None,
                resume_after=None,
            )
            await conn.execute(
                delete(task_metadata).where(
                    task_metadata.c.task_id == task_id,
                    task_metadata.c.key == "needs_attention",
                )
            )
            await conn.execute(
                insert(events).values(
                    event_type="integration.collection_redriven",
                    project_id=diagnosis["project_id"],
                    task_id=task_id,
                    timestamp=self.clock(),
                    payload=json.dumps(
                        {
                            "operator_id": operator_id,
                            "reason": reason,
                            "checkpoint": diagnosis["checkpoint"],
                            "head_sha": expected_head_sha,
                        }
                    ),
                )
            )
        await self.db.log_blocked_flips(transition.flipped)
        return {**diagnosis, "outcome": "collecting", "reason": "restored PAUSED collection"}
