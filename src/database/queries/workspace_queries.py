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
        """Insert a new workspace record.

        Back-compat: when ``workspace.kind_id`` is None, defaults to
        ``"project-repo"`` so existing callers (orchestrator, CLI ``add_workspace``,
        tests written before workspaces-v2) keep working.  Explicit kind_id is
        respected.  Spec §9.5: ``kind_id`` becomes NOT NULL in a follow-up
        migration once every install is on workspaces-v2.
        """
        kind_id = workspace.kind_id if workspace.kind_id is not None else "project-repo"
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(workspaces).values(
                    id=workspace.id,
                    project_id=workspace.project_id,
                    workspace_path=workspace.workspace_path,
                    source_type=workspace.source_type.value,
                    name=workspace.name,
                    kind_id=kind_id,
                    locked_by_agent_id=workspace.locked_by_agent_id,
                    locked_by_task_id=workspace.locked_by_task_id,
                    locked_at=workspace.locked_at,
                    enabled=workspace.enabled,
                    slot_index=workspace.slot_index,
                    base_workspace_id=workspace.base_workspace_id,
                    created_at=time.time(),
                )
            )

    async def update_workspace(self, workspace_id: str, **fields) -> int:
        """Update arbitrary columns on a workspace. Returns rows affected.

        Used by ``_cmd_edit_workspace`` to atomically change path, source_type,
        name, or enabled flag. Lock columns are not updated through this path
        — that's what release_workspace and acquire_workspace are for.
        """
        if not fields:
            return 0
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(workspaces).where(workspaces.c.id == workspace_id).values(**fields)
            )
            return result.rowcount

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
                        & (workspaces.c.enabled.is_(True))
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
                    & (workspaces.c.enabled.is_(True))
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
                # DIRECTORY_ISOLATED (future): will allow multiple agents on
                #   the same branch in different directories.  Needs directory-
                #   scoped locking and careful handling of shared files
                #   (configs, lock files, root-level changes).  Currently
                #   falls through to the EXCLUSIVE path below — the
                #   orchestrator rejects this mode before it reaches here.
                #   See docs/specs/design/agent-coordination.md §7.
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

    async def first_workspace_of_kind(
        self,
        project_id: str,
        kind_id: str,
    ) -> Workspace | None:
        """Return the first enabled workspace of a given kind for a project.

        Non-locking read.  Used for non-lockable kinds (e.g. vault, readonly-dir)
        where multiple tasks may attach simultaneously.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(workspaces)
                .where(
                    (workspaces.c.project_id == project_id)
                    & (workspaces.c.kind_id == kind_id)
                    & (workspaces.c.enabled.is_(True))
                )
                .order_by(workspaces.c.id)
                .limit(1)
            )
            row = result.mappings().fetchone()
            return self._row_to_workspace(row) if row else None

    async def acquire_one_unlocked(
        self,
        project_id: str,
        kind_id: str,
        mode: str | None,
        locked_by_task_id: str,
        locked_by_agent_id: str,
        prefer_workspace_id: str | None = None,
    ) -> Workspace | None:
        """Atomically acquire one unlocked workspace of a given kind.

        Spec §6.4 per-dialect strategy.  Preserves the path-level conflict
        check from the legacy :meth:`acquire_workspace`: BRANCH_ISOLATED only
        conflicts with non-BRANCH_ISOLATED on the same path; EXCLUSIVE
        conflicts with any other lock on the same path.

        Args:
            project_id: Project scope.
            kind_id: Required kind id.  Workspaces are filtered by
                ``workspaces.kind_id == kind_id`` (no kind resolution here —
                callers resolve via ``resolve_workspace_kind`` and pass
                ``kind.id``).
            mode: Lock mode string (``"exclusive"`` / ``"branch_isolated"`` /
                ``"directory_isolated"``).  Comes from
                ``WorkspaceKind.default_lock_mode``.
            locked_by_task_id: Task acquiring the lock.
            locked_by_agent_id: Agent acquiring the lock.
            prefer_workspace_id: Try this workspace id first; falls back to
                first-unlocked if it's busy or doesn't match.

        Returns the locked workspace, or ``None`` if no instance was free.
        """
        now = time.time()
        lock_mode_value = mode  # already lowercase, e.g. "exclusive"

        async with self._engine.begin() as conn:
            candidate_ids: list[str] = []

            if prefer_workspace_id:
                row = (
                    await conn.execute(
                        select(workspaces.c.id).where(
                            (workspaces.c.id == prefer_workspace_id)
                            & (workspaces.c.project_id == project_id)
                            & (workspaces.c.kind_id == kind_id)
                            & (workspaces.c.locked_by_agent_id.is_(None))
                            & (workspaces.c.enabled.is_(True))
                        )
                    )
                ).fetchone()
                if row:
                    candidate_ids.append(row[0])

            rows = (
                await conn.execute(
                    select(workspaces.c.id)
                    .where(
                        (workspaces.c.project_id == project_id)
                        & (workspaces.c.kind_id == kind_id)
                        & (workspaces.c.locked_by_agent_id.is_(None))
                        & (workspaces.c.enabled.is_(True))
                    )
                    .order_by(workspaces.c.id)
                )
            ).fetchall()
            for row in rows:
                if row[0] not in candidate_ids:
                    candidate_ids.append(row[0])

            for ws_id in candidate_ids:
                ws_row = (
                    await conn.execute(
                        select(workspaces).where(
                            (workspaces.c.id == ws_id)
                            & (workspaces.c.locked_by_agent_id.is_(None))
                        )
                    )
                ).mappings().fetchone()
                if not ws_row:
                    continue

                # Path-level conflict check — preserved from acquire_workspace.
                if mode == WorkspaceMode.BRANCH_ISOLATED.value:
                    conflict = (
                        await conn.execute(
                            select(workspaces.c.id).where(
                                (workspaces.c.workspace_path == ws_row["workspace_path"])
                                & (workspaces.c.locked_by_agent_id.isnot(None))
                                & (workspaces.c.id != ws_row["id"])
                                & (workspaces.c.lock_mode != WorkspaceMode.BRANCH_ISOLATED.value)
                            )
                        )
                    ).fetchone()
                else:
                    conflict = (
                        await conn.execute(
                            select(workspaces.c.id).where(
                                (workspaces.c.workspace_path == ws_row["workspace_path"])
                                & (workspaces.c.locked_by_agent_id.isnot(None))
                                & (workspaces.c.id != ws_row["id"])
                            )
                        )
                    ).fetchone()
                if conflict:
                    continue

                # Atomic lock attempt — UPDATE with WHERE locked IS NULL.
                result = await conn.execute(
                    update(workspaces)
                    .where(
                        (workspaces.c.id == ws_row["id"])
                        & (workspaces.c.locked_by_agent_id.is_(None))
                    )
                    .values(
                        locked_by_agent_id=locked_by_agent_id,
                        locked_by_task_id=locked_by_task_id,
                        locked_at=now,
                        lock_mode=lock_mode_value,
                    )
                )
                if result.rowcount != 1:
                    continue

                ws = self._row_to_workspace(ws_row)
                ws.locked_by_agent_id = locked_by_agent_id
                ws.locked_by_task_id = locked_by_task_id
                ws.locked_at = now
                ws.lock_mode = (
                    WorkspaceMode(lock_mode_value) if lock_mode_value else None
                )
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
                    & (workspaces.c.enabled.is_(True))
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
        """Count unlocked, enabled workspaces for a project."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(workspaces)
                .where(
                    (workspaces.c.project_id == project_id)
                    & (workspaces.c.locked_by_agent_id.is_(None))
                    & (workspaces.c.enabled.is_(True))
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
            kind_id=row["kind_id"],
            locked_by_agent_id=row["locked_by_agent_id"],
            locked_by_task_id=row["locked_by_task_id"],
            locked_at=row["locked_at"],
            lock_mode=WorkspaceMode(raw_mode) if raw_mode else None,
            enabled=bool(row["enabled"]),
            slot_index=row["slot_index"],
            base_workspace_id=row["base_workspace_id"],
        )
