"""Workspace CRUD and locking operations."""

from __future__ import annotations

import logging
import time

from sqlalchemy import case, delete, func, insert, select, update

from src.database.tables import workspaces
from src.models import RepoSourceType, Workspace, WorkspaceMode

logger = logging.getLogger(__name__)

# Ordering expression: clones first, then links
_source_type_order = case(
    (workspaces.c.source_type == "clone", 0),
    else_=1,
)


class WorkspaceQueryMixin:
    """Query mixin for workspace operations.  Expects ``self._engine``."""

    async def create_workspace(self, workspace: Workspace) -> None:
        """Insert a new workspace record."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(workspaces).values(
                    id=workspace.id,
                    project_id=workspace.project_id,
                    workspace_path=workspace.workspace_path,
                    source_type=workspace.source_type.value,
                    name=workspace.name,
                    locked_by_agent_id=workspace.locked_by_agent_id,
                    locked_by_task_id=workspace.locked_by_task_id,
                    locked_at=workspace.locked_at,
                    created_at=time.time(),
                )
            )

    async def get_workspace(self, workspace_id: str) -> Workspace | None:
        """Fetch a single workspace by ID."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(workspaces).where(workspaces.c.id == workspace_id))
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_workspace(row)

    async def get_workspace_by_name(
        self,
        project_id: str,
        name: str,
    ) -> Workspace | None:
        """Find a workspace by name within a project."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(workspaces).where(
                    (workspaces.c.project_id == project_id) & (workspaces.c.name == name)
                )
            )
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_workspace(row)

    async def list_workspaces(
        self,
        project_id: str | None = None,
    ) -> list[Workspace]:
        """List workspaces, clone before link, optionally filtered by project."""
        stmt = select(workspaces)
        if project_id:
            stmt = stmt.where(workspaces.c.project_id == project_id)
        stmt = stmt.order_by(_source_type_order, workspaces.c.id)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_workspace(r) for r in result.mappings().fetchall()]

    async def delete_workspace(self, workspace_id: str) -> None:
        """Delete a workspace record."""
        async with self._engine.begin() as conn:
            await conn.execute(delete(workspaces).where(workspaces.c.id == workspace_id))

    async def acquire_workspace(
        self,
        project_id: str,
        agent_id: str,
        task_id: str,
        preferred_workspace_id: str | None = None,
        lock_mode: WorkspaceMode = WorkspaceMode.EXCLUSIVE,
    ) -> Workspace | None:
        """Atomically find an unlocked workspace for a project and lock it.

        If *preferred_workspace_id* is given, attempt to lock that specific
        workspace first.  Falls back to any unlocked workspace if the
        preferred one is unavailable.

        *lock_mode* records the locking strategy used for this acquisition.
        Default is ``WorkspaceMode.EXCLUSIVE`` — one agent, one workspace.
        See ``docs/specs/design/agent-coordination.md §7`` for lock mode
        semantics.

        Returns the locked workspace, or None if all are locked.
        """
        async with self._engine.begin() as conn:
            candidate_ids: list[str] = []

            if preferred_workspace_id:
                result = await conn.execute(
                    select(workspaces.c.id).where(
                        (workspaces.c.id == preferred_workspace_id)
                        & (workspaces.c.project_id == project_id)
                        & (workspaces.c.locked_by_agent_id.is_(None))
                    )
                )
                row = result.fetchone()
                if row:
                    candidate_ids.append(row[0])

            result = await conn.execute(
                select(workspaces.c.id)
                .where(
                    (workspaces.c.project_id == project_id)
                    & (workspaces.c.locked_by_agent_id.is_(None))
                )
                .order_by(workspaces.c.id)
            )
            for row in result.fetchall():
                if row[0] not in candidate_ids:
                    candidate_ids.append(row[0])

            if not candidate_ids:
                return None

            now = time.time()
            for ws_id in candidate_ids:
                result = await conn.execute(
                    select(workspaces).where(
                        (workspaces.c.id == ws_id) & (workspaces.c.locked_by_agent_id.is_(None))
                    )
                )
                row = result.mappings().fetchone()
                if not row:
                    continue

                # Path-level lock check — mode-aware.
                #
                # EXCLUSIVE (default): reject if ANY other workspace on the
                #   same filesystem path is locked, regardless of mode.
                # BRANCH_ISOLATED: reject only if a non-BRANCH_ISOLATED lock
                #   exists on the same path.  Two BRANCH_ISOLATED locks on
                #   the same path are compatible (agents work on separate
                #   branches via git worktrees).
                if lock_mode == WorkspaceMode.BRANCH_ISOLATED:
                    conflict_result = await conn.execute(
                        select(workspaces.c.id).where(
                            (workspaces.c.workspace_path == row["workspace_path"])
                            & (workspaces.c.locked_by_agent_id.isnot(None))
                            & (workspaces.c.id != row["id"])
                            & (workspaces.c.lock_mode != WorkspaceMode.BRANCH_ISOLATED.value)
                        )
                    )
                else:
                    conflict_result = await conn.execute(
                        select(workspaces.c.id).where(
                            (workspaces.c.workspace_path == row["workspace_path"])
                            & (workspaces.c.locked_by_agent_id.isnot(None))
                            & (workspaces.c.id != row["id"])
                        )
                    )
                conflict = conflict_result.fetchone()
                if conflict:
                    logger.warning(
                        "Workspace path %s already locked by workspace %s — skipping %s",
                        row["workspace_path"],
                        conflict[0],
                        row["id"],
                    )
                    continue

                # Optimistic lock
                lock_result = await conn.execute(
                    update(workspaces)
                    .where(
                        (workspaces.c.id == row["id"]) & (workspaces.c.locked_by_agent_id.is_(None))
                    )
                    .values(
                        locked_by_agent_id=agent_id,
                        locked_by_task_id=task_id,
                        locked_at=now,
                        lock_mode=lock_mode.value,
                    )
                )

                if lock_result.rowcount != 1:
                    continue

                ws = self._row_to_workspace(row)
                ws.locked_by_agent_id = agent_id
                ws.locked_by_task_id = task_id
                ws.locked_at = now
                ws.lock_mode = lock_mode
                return ws

            return None

    async def find_branch_isolated_base(
        self,
        project_id: str,
    ) -> Workspace | None:
        """Find a workspace locked with BRANCH_ISOLATED that can host worktrees.

        Used by the orchestrator when a BRANCH_ISOLATED task has no unlocked
        workspace available.  Returns the first workspace for the project
        that is currently locked with ``WorkspaceMode.BRANCH_ISOLATED``.
        The orchestrator will create a git worktree from this base workspace.

        Prefers clone workspaces over link workspaces (clones are managed by
        the orchestrator and more likely to have proper remote configuration).
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(workspaces)
                .where(
                    (workspaces.c.project_id == project_id)
                    & (workspaces.c.locked_by_agent_id.isnot(None))
                    & (workspaces.c.lock_mode == WorkspaceMode.BRANCH_ISOLATED.value)
                )
                .order_by(_source_type_order, workspaces.c.id)
                .limit(1)
            )
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_workspace(row)

    async def release_workspace(self, workspace_id: str) -> None:
        """Clear lock columns on a workspace."""
        async with self._engine.begin() as conn:
            await conn.execute(
                update(workspaces)
                .where(workspaces.c.id == workspace_id)
                .values(
                    locked_by_agent_id=None,
                    locked_by_task_id=None,
                    locked_at=None,
                    lock_mode=None,
                )
            )

    async def release_workspaces_for_agent(self, agent_id: str) -> int:
        """Release all workspace locks held by an agent. Returns count released."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(workspaces)
                .where(workspaces.c.locked_by_agent_id == agent_id)
                .values(
                    locked_by_agent_id=None,
                    locked_by_task_id=None,
                    locked_at=None,
                    lock_mode=None,
                )
            )
            return result.rowcount

    async def release_workspaces_for_task(self, task_id: str) -> int:
        """Release all workspace locks held by a task. Returns count released."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(workspaces)
                .where(workspaces.c.locked_by_task_id == task_id)
                .values(
                    locked_by_agent_id=None,
                    locked_by_task_id=None,
                    locked_at=None,
                    lock_mode=None,
                )
            )
            return result.rowcount

    async def get_workspace_for_task(self, task_id: str) -> Workspace | None:
        """Find the workspace currently locked by a task."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(workspaces).where(workspaces.c.locked_by_task_id == task_id)
            )
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_workspace(row)

    async def get_project_workspace_path(self, project_id: str) -> str | None:
        """Return the workspace_path of the first workspace for a project.

        Non-locking read. Prefers clone workspaces over link workspaces.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(workspaces.c.workspace_path)
                .where(workspaces.c.project_id == project_id)
                .order_by(_source_type_order, workspaces.c.id)
                .limit(1)
            )
            row = result.fetchone()
            return row[0] if row else None

    async def count_available_workspaces(self, project_id: str) -> int:
        """Count unlocked workspaces for a project."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(workspaces)
                .where(
                    (workspaces.c.project_id == project_id)
                    & (workspaces.c.locked_by_agent_id.is_(None))
                )
            )
            row = result.fetchone()
            return row[0]

    @staticmethod
    def _row_to_workspace(row) -> Workspace:
        """Convert a database row to a Workspace model."""
        raw_mode = row["lock_mode"]
        return Workspace(
            id=row["id"],
            project_id=row["project_id"],
            workspace_path=row["workspace_path"],
            source_type=RepoSourceType(row["source_type"]),
            name=row["name"],
            locked_by_agent_id=row["locked_by_agent_id"],
            locked_by_task_id=row["locked_by_task_id"],
            locked_at=row["locked_at"],
            lock_mode=WorkspaceMode(raw_mode) if raw_mode else None,
        )
