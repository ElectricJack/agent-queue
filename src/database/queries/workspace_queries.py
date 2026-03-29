"""Workspace CRUD and locking operations."""

from __future__ import annotations

import logging
import time

from src.models import RepoSourceType, Workspace

logger = logging.getLogger(__name__)


class WorkspaceQueryMixin:
    """Query mixin for workspace operations.  Expects ``self._db``."""

    async def create_workspace(self, workspace: Workspace) -> None:
        """Insert a new workspace record."""
        await self._db.execute(
            "INSERT INTO workspaces (id, project_id, workspace_path, source_type, "
            "name, locked_by_agent_id, locked_by_task_id, locked_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (workspace.id, workspace.project_id, workspace.workspace_path,
             workspace.source_type.value, workspace.name,
             workspace.locked_by_agent_id, workspace.locked_by_task_id,
             workspace.locked_at, time.time()),
        )
        await self._db.commit()

    async def get_workspace(self, workspace_id: str) -> Workspace | None:
        """Fetch a single workspace by ID."""
        cursor = await self._db.execute(
            "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_workspace(row)

    async def get_workspace_by_name(
        self, project_id: str, name: str,
    ) -> Workspace | None:
        """Find a workspace by name within a project."""
        cursor = await self._db.execute(
            "SELECT * FROM workspaces WHERE project_id = ? AND name = ?",
            (project_id, name),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_workspace(row)

    async def list_workspaces(
        self, project_id: str | None = None,
    ) -> list[Workspace]:
        """List workspaces, clone before link, optionally filtered by project."""
        if project_id:
            cursor = await self._db.execute(
                "SELECT * FROM workspaces WHERE project_id = ? "
                "ORDER BY CASE source_type WHEN 'clone' THEN 0 ELSE 1 END, rowid",
                (project_id,),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM workspaces ORDER BY "
                "CASE source_type WHEN 'clone' THEN 0 ELSE 1 END, rowid"
            )
        rows = await cursor.fetchall()
        return [self._row_to_workspace(r) for r in rows]

    async def delete_workspace(self, workspace_id: str) -> None:
        """Delete a workspace record."""
        await self._db.execute(
            "DELETE FROM workspaces WHERE id = ?", (workspace_id,)
        )
        await self._db.commit()

    async def acquire_workspace(
        self, project_id: str, agent_id: str, task_id: str,
        preferred_workspace_id: str | None = None,
    ) -> Workspace | None:
        """Atomically find an unlocked workspace for a project and lock it.

        If *preferred_workspace_id* is given, attempt to lock that specific
        workspace first.  Falls back to any unlocked workspace if the
        preferred one is unavailable.

        Returns the locked workspace, or None if all are locked.
        """
        candidate_ids: list[str] = []

        if preferred_workspace_id:
            cursor = await self._db.execute(
                "SELECT id FROM workspaces "
                "WHERE id = ? AND project_id = ? AND locked_by_agent_id IS NULL",
                (preferred_workspace_id, project_id),
            )
            row = await cursor.fetchone()
            if row:
                candidate_ids.append(row["id"])

        cursor = await self._db.execute(
            "SELECT id FROM workspaces "
            "WHERE project_id = ? AND locked_by_agent_id IS NULL "
            "ORDER BY id",
            (project_id,),
        )
        for row in await cursor.fetchall():
            if row["id"] not in candidate_ids:
                candidate_ids.append(row["id"])

        if not candidate_ids:
            return None

        now = time.time()
        for ws_id in candidate_ids:
            cursor = await self._db.execute(
                "SELECT * FROM workspaces WHERE id = ? AND locked_by_agent_id IS NULL",
                (ws_id,),
            )
            row = await cursor.fetchone()
            if not row:
                continue

            # Path-level lock check
            cursor = await self._db.execute(
                "SELECT id FROM workspaces "
                "WHERE workspace_path = ? AND locked_by_agent_id IS NOT NULL "
                "AND id != ?",
                (row["workspace_path"], row["id"]),
            )
            conflict = await cursor.fetchone()
            if conflict:
                logger.warning(
                    "Workspace path %s already locked by workspace %s — skipping %s",
                    row["workspace_path"], conflict["id"], row["id"],
                )
                continue

            # Optimistic lock
            cursor = await self._db.execute(
                "UPDATE workspaces SET locked_by_agent_id = ?, "
                "locked_by_task_id = ?, locked_at = ? "
                "WHERE id = ? AND locked_by_agent_id IS NULL",
                (agent_id, task_id, now, row["id"]),
            )
            await self._db.commit()

            if cursor.rowcount != 1:
                continue

            ws = self._row_to_workspace(row)
            ws.locked_by_agent_id = agent_id
            ws.locked_by_task_id = task_id
            ws.locked_at = now
            return ws

        return None

    async def release_workspace(self, workspace_id: str) -> None:
        """Clear lock columns on a workspace."""
        await self._db.execute(
            "UPDATE workspaces SET locked_by_agent_id = NULL, "
            "locked_by_task_id = NULL, locked_at = NULL "
            "WHERE id = ?",
            (workspace_id,),
        )
        await self._db.commit()

    async def release_workspaces_for_agent(self, agent_id: str) -> int:
        """Release all workspace locks held by an agent. Returns count released."""
        cursor = await self._db.execute(
            "UPDATE workspaces SET locked_by_agent_id = NULL, "
            "locked_by_task_id = NULL, locked_at = NULL "
            "WHERE locked_by_agent_id = ?",
            (agent_id,),
        )
        await self._db.commit()
        return cursor.rowcount

    async def release_workspaces_for_task(self, task_id: str) -> int:
        """Release all workspace locks held by a task. Returns count released."""
        cursor = await self._db.execute(
            "UPDATE workspaces SET locked_by_agent_id = NULL, "
            "locked_by_task_id = NULL, locked_at = NULL "
            "WHERE locked_by_task_id = ?",
            (task_id,),
        )
        await self._db.commit()
        return cursor.rowcount

    async def get_workspace_for_task(self, task_id: str) -> Workspace | None:
        """Find the workspace currently locked by a task."""
        cursor = await self._db.execute(
            "SELECT * FROM workspaces WHERE locked_by_task_id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_workspace(row)

    async def get_project_workspace_path(self, project_id: str) -> str | None:
        """Return the workspace_path of the first workspace for a project.

        Non-locking read. Prefers clone workspaces over link workspaces.
        """
        cursor = await self._db.execute(
            "SELECT workspace_path FROM workspaces WHERE project_id = ? "
            "ORDER BY CASE source_type WHEN 'clone' THEN 0 ELSE 1 END, rowid "
            "LIMIT 1",
            (project_id,),
        )
        row = await cursor.fetchone()
        return row["workspace_path"] if row else None

    async def count_available_workspaces(self, project_id: str) -> int:
        """Count unlocked workspaces for a project."""
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS cnt FROM workspaces "
            "WHERE project_id = ? AND locked_by_agent_id IS NULL",
            (project_id,),
        )
        row = await cursor.fetchone()
        return row["cnt"]

    @staticmethod
    def _row_to_workspace(row) -> Workspace:
        """Convert a database row to a Workspace model."""
        return Workspace(
            id=row["id"],
            project_id=row["project_id"],
            workspace_path=row["workspace_path"],
            source_type=RepoSourceType(row["source_type"]),
            name=row["name"],
            locked_by_agent_id=row["locked_by_agent_id"],
            locked_by_task_id=row["locked_by_task_id"],
            locked_at=row["locked_at"],
        )
