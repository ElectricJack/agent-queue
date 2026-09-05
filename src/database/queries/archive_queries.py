"""Archived task operations."""

from __future__ import annotations

import json
import logging
import time

from sqlalchemy import and_, delete, exists, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import (
    agents,
    archived_tasks,
    sessions,
    task_comments,
    task_completion_records,
    tasks,
)
from src.models import TaskStatus

logger = logging.getLogger(__name__)


class ArchiveQueryMixin:
    """Query mixin for archived task operations.  Expects ``self._engine``."""

    async def archive_task(self, task_id: str) -> bool:
        """Archive *task_id* and its whole subtree atomically (spec §7).

        Refuses live sessions or non-terminal tasks anywhere in the subtree.
        Deepest first, root last, so the subtree moves together.
        """
        from src.database.queries.hierarchy_queries import LIVE_SESSION_STATES, HierarchyError

        terminal = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.BLOCKED.value)
        async with self.immediate() as conn:
            await self.guard_integration_mutation(
                task_id, "archive", conn=conn, retire_pending=True
            )
            ids = await self.subtree_ids(task_id, conn=conn)
            if not ids:
                return False
            # Follow the existing sessions-before-tasks lock order. A task
            # can be terminal while its worker is still draining.
            live = await self.live_descendant_sessions(task_id, conn=conn)
            if live:
                raise HierarchyError("live_descendants", ", ".join(sorted(t for _, t in live)))
            task_rows = select(tasks.c.id, tasks.c.status).where(tasks.c.id.in_(ids))
            if conn.dialect.name == "postgresql":
                # A key lock also fences new session FK references. Recheck
                # after acquiring it to catch a launch that won that race.
                task_rows = task_rows.order_by(tasks.c.id).with_for_update()
            rows = (await conn.execute(task_rows)).fetchall()
            live_ids = (
                await conn.execute(
                    select(sessions.c.task_id).where(
                        sessions.c.task_id.in_(ids),
                        sessions.c.state.in_(LIVE_SESSION_STATES),
                    )
                )
            ).scalars().all()
            if live_ids:
                raise HierarchyError("live_descendants", ", ".join(sorted(set(live_ids))))
            if any(r[0] == task_id and r[1] not in terminal for r in rows):
                raise HierarchyError("non_terminal_root", task_id)
            open_ids = [r[0] for r in rows if r[1] not in terminal and r[0] != task_id]
            if open_ids:
                raise HierarchyError("open_descendants", ", ".join(sorted(open_ids)))
            parent = (
                await conn.execute(select(tasks.c.parent_task_id).where(tasks.c.id == task_id))
            ).scalar()
            affected = await self._collect_affected(set(ids), conn)
            affected -= set(ids)
            if parent:
                affected.add(parent)
            # Archiving moves the rows out of ``tasks``, so mark while the
            # project id is still readable there; ``_archive_one`` reuses
            # ``_delete_one``, which drops the layout rows (an FK holder on
            # ``tasks``) inside this same transaction.  The surviving PARENTS
            # are marked too: with the archived rows gone the driver cannot
            # find the former container from a stored row, so without this
            # the container never re-flows and its ancestors' aggregates go
            # stale.
            project_id = (
                await conn.execute(select(tasks.c.project_id).where(tasks.c.id == task_id))
            ).scalar_one_or_none()
            if project_id is not None:
                await self.mark_layout_dirty(
                    project_id, [*ids, *await self._layout_parent_ids(ids, conn=conn)],
                    "task.archived", conn=conn,
                )
            for tid in reversed(ids):
                task = await self._get_task_conn(tid, conn=conn)
                if task is not None:
                    await self._archive_one(task, conn=conn)
            flipped = await self.recompute_blocked(affected, conn=conn) if affected else set()
            # Archiving a blocker unblocks its dependents exactly as
            # completing it would, so the same ``task.ready`` audit row and
            # listener wake-up are owed (I2) — without them a waiting
            # ``task_claim`` long-poll sleeps through claimable work.
            ready = [
                (tid, "unblocked")
                for tid in await self._note_frontier_entry(conn, flipped, reason="unblocked")
            ]
            settle_result = await self.settle_containers({parent} if parent else set(), conn=conn)
        await self.log_blocked_flips(flipped | settle_result.flipped)
        await self._notify_settled(settle_result.settled)
        await self._notify_ready(ready + list(settle_result.ready))
        return True

    async def _archive_one(self, task, *, conn) -> None:
        """Move a single task row from ``tasks`` into ``archived_tasks``."""
        task_id = task.id
        # Serialize archive with accepted description/comment writes, then
        # snapshot the current task rather than the earlier unlocked read.
        locked = await conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(
                updated_at=tasks.c.updated_at,
            )
        )
        if locked.rowcount != 1:
            return
        task = await self._get_task_conn(task_id, conn=conn)
        now = time.time()
        # Insert into archive (skip if already archived).
        # on_conflict_do_nothing requires dialect-specific insert.
        _insert = pg_insert if self._engine.dialect.name == "postgresql" else sqlite_insert
        await conn.execute(
            _insert(archived_tasks)
            .on_conflict_do_nothing()
            .values(
                id=task.id,
                project_id=task.project_id,
                parent_task_id=task.parent_task_id,
                repo_id=task.repo_id,
                title=task.title,
                description=task.description,
                priority=task.priority,
                status=task.status.value,
                verification_type=task.verification_type.value,
                retry_count=task.retry_count,
                max_retries=task.max_retries,
                assigned_agent_id=task.assigned_agent_id,
                branch_name=task.branch_name,
                resume_after=task.resume_after,
                integration_mode=task.integration_mode,
                pr_url=task.pr_url,
                plan_source=task.plan_source,
                is_plan_subtask=int(task.is_plan_subtask),
                task_type=task.task_type.value if task.task_type else None,
                profile_id=task.profile_id,
                preferred_workspace_id=task.preferred_workspace_id,
                attachments=json.dumps(task.attachments) if task.attachments else "[]",
                skip_verification=int(task.skip_verification),
                workflow_id=task.workflow_id,
                affinity_agent_id=task.affinity_agent_id,
                affinity_reason=task.affinity_reason,
                workspace_mode=(task.workspace_mode.value if task.workspace_mode else None),
                # Carry the blocked-state projection across so archiving
                # really is lossless (work-graph §2.2).
                is_blocked=int(task.is_blocked),
                created_by_kind=task.created_by_kind,
                created_by_id=task.created_by_id,
                created_at=0.0,
                updated_at=0.0,
                archived_at=now,
            )
        )

        # Legacy allocation could reuse an archived ID in another project.
        # Never drop the active identity or overwrite that archive's timestamps.
        archived_project_id = (await conn.execute(
            select(archived_tasks.c.project_id).where(archived_tasks.c.id == task_id)
        )).scalar_one()
        if archived_project_id != task.project_id:
            from src.database.queries.hierarchy_queries import HierarchyError

            raise HierarchyError("archive_identity_conflict", task_id)

        # Copy original timestamps
        result = await conn.execute(
            select(tasks.c.created_at, tasks.c.updated_at).where(tasks.c.id == task_id)
        )
        row = result.fetchone()
        if row:
            await conn.execute(
                update(archived_tasks)
                .where(archived_tasks.c.id == task_id)
                .values(created_at=row[0], updated_at=row[1])
            )

        # Use the same FK cleanup as permanent deletion, but keep comments
        # attached to the archived task identity and preserve session history.
        await conn.execute(
            update(agents).where(agents.c.current_task_id == task_id).values(current_task_id=None)
        )
        await self._delete_one(
            task_id,
            conn=conn,
            preserve_comments=True,
            preserve_completion=True,
            gate_resolution="last waiter task archived",
        )

    async def archive_completed_tasks(
        self,
        project_id: str | None = None,
    ) -> list[str]:
        """Archive all COMPLETED tasks. Returns list of archived task IDs.

        A COMPLETED task can still have a non-terminal *descendant* (a
        grandchild left open under a completed child).  ``archive_task``
        refuses those with ``hierarchy.open_descendants``; like
        ``archive_old_terminal_tasks``, the bulk path skips them rather than
        aborting the whole sweep — and reports only what it actually archived.
        """
        from src.database.queries.hierarchy_queries import HierarchyError

        stmt = select(tasks.c.id).where(tasks.c.status == TaskStatus.COMPLETED.value)
        if project_id:
            stmt = stmt.where(tasks.c.project_id == project_id)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            task_ids = [r[0] for r in result.fetchall()]

        archived: list[str] = []
        for tid in task_ids:
            try:
                await self.archive_task(tid)
                archived.append(tid)
            except HierarchyError as exc:
                logger.debug("archive_completed_tasks: skipping %s, %s", tid, exc.code)

        return archived

    async def archive_old_terminal_tasks(
        self,
        statuses: list[str],
        older_than_seconds: float,
    ) -> list[str]:
        """Archive terminal tasks older than the threshold. Returns archived IDs.

        Selects only subtree roots of terminal subtrees — terminal, older
        than cutoff, ``parent_task_id IS NULL``, and with no non-terminal
        direct child. Children are archived by the root's own subtree
        archive, not selected individually. Open grandchildren are caught
        by ``archive_task``'s own subtree check, which raises; those roots
        are logged and skipped.
        """
        from src.database.queries.hierarchy_queries import HierarchyError

        if not statuses:
            return []

        cutoff = time.time() - older_than_seconds
        child = tasks.alias("child")
        stmt = select(tasks.c.id).where(
            and_(
                tasks.c.status.in_(statuses),
                # Reusable project triage keeps one identity and its run history.
                ~and_(
                    func.coalesce(tasks.c.profile_id, "") == "triage",
                    func.coalesce(tasks.c.dedup_key, "") == "triage-open",
                ),
                tasks.c.updated_at <= cutoff,
                tasks.c.parent_task_id.is_(None),
                ~exists(
                    select(literal(1)).where(
                        and_(
                            child.c.parent_task_id == tasks.c.id,
                            child.c.status.notin_(
                                (
                                    TaskStatus.COMPLETED.value,
                                    TaskStatus.FAILED.value,
                                    TaskStatus.BLOCKED.value,
                                )
                            ),
                        )
                    )
                ),
            )
        )
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            task_ids = [r[0] for r in result.fetchall()]

        archived: list[str] = []
        for tid in task_ids:
            try:
                await self.archive_task(tid)
                archived.append(tid)
            except HierarchyError as exc:
                logger.debug("archive_old_terminal_tasks: skipping %s, %s", tid, exc.code)

        return archived

    async def list_archived_tasks(
        self,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return archived tasks as dicts, newest archived first."""
        stmt = select(archived_tasks)
        if project_id:
            stmt = stmt.where(archived_tasks.c.project_id == project_id)
        stmt = stmt.order_by(archived_tasks.c.archived_at.desc()).limit(limit)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_archived_task(r) for r in result.mappings().fetchall()]

    async def get_archived_task(self, task_id: str) -> dict | None:
        """Return a single archived task as a dict, or *None* if not found."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(archived_tasks).where(archived_tasks.c.id == task_id)
            )
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_archived_task(row)

    async def delete_archived_task(self, task_id: str) -> bool:
        """Permanently delete an archived task. Returns *True* if deleted."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(archived_tasks.c.project_id).where(archived_tasks.c.id == task_id)
            )
            archived_project_id = result.scalar_one_or_none()
            if archived_project_id is None:
                return False
            await conn.execute(
                delete(task_completion_records).where(
                    task_completion_records.c.task_id == task_id,
                    ~exists(select(tasks.c.id).where(tasks.c.id == task_id)),
                )
            )
            await conn.execute(delete(archived_tasks).where(archived_tasks.c.id == task_id))
            # Restoration may recreate the active identity before removing
            # its archive snapshot. Only permanent deletion removes history.
            await conn.execute(
                delete(task_comments).where(
                    task_comments.c.task_id == task_id,
                    task_comments.c.project_id == archived_project_id,
                    ~exists(select(tasks.c.id).where(
                        tasks.c.id == task_id, tasks.c.project_id == archived_project_id,
                    )),
                )
            )
        return True

    async def count_archived_tasks(
        self,
        project_id: str | None = None,
    ) -> int:
        """Return the total count of archived tasks."""
        stmt = select(func.count()).select_from(archived_tasks)
        if project_id:
            stmt = stmt.where(archived_tasks.c.project_id == project_id)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            row = result.fetchone()
            return row[0] if row else 0

    @staticmethod
    def _row_to_archived_task(row) -> dict:
        """Convert a database row from ``archived_tasks`` to a plain dict."""
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "parent_task_id": row["parent_task_id"],
            "repo_id": row["repo_id"],
            "title": row["title"],
            "description": row["description"],
            "priority": row["priority"],
            "status": row["status"],
            "verification_type": row["verification_type"],
            "retry_count": row["retry_count"],
            "max_retries": row["max_retries"],
            "assigned_agent_id": row["assigned_agent_id"],
            "branch_name": row["branch_name"],
            "resume_after": row["resume_after"],
            "integration_mode": row["integration_mode"],
            "pr_url": row["pr_url"],
            "plan_source": row.get("plan_source"),
            "is_plan_subtask": bool(row.get("is_plan_subtask", 0)),
            "task_type": row.get("task_type"),
            "workflow_id": row.get("workflow_id"),
            "affinity_agent_id": row.get("affinity_agent_id"),
            "affinity_reason": row.get("affinity_reason"),
            "workspace_mode": row.get("workspace_mode"),
            "is_blocked": bool(row.get("is_blocked", 0)),
            "created_by_kind": row.get("created_by_kind"),
            "created_by_id": row.get("created_by_id"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
        }
