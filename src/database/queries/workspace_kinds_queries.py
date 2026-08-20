"""WorkspaceKind CRUD and resolution. See spec §3.1, §3.5."""

from __future__ import annotations

import json
import logging
import time

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import workspace_kinds
from src.models import KIND_MODE_WORKTREE, SYSTEM_KIND_SCOPE, WorkspaceKind

logger = logging.getLogger(__name__)

# Columns updated on conflict — every column except the PK pair and created_at.
#
# ``mode`` and ``worktree_setup`` are here deliberately (worktree-execution
# §3.6 + principle #1: the markdown is the source of truth).  Behavior changes
# only when an operator edits the file, which is the intended knob.
#
# ``mode`` is *removed* from this set when ``kind.mode is None`` — the parser's
# encoding of "the frontmatter says nothing about mode" — so an absent key
# means "leave the stored value alone".  ``bootstrap`` only writes markdown
# that does not already exist, so an install upgrading with a pre-``mode``
# ``project-repo.md`` keeps a file with no ``mode:`` key; without the coalesce
# that file would upsert a default over the migration's ``exclusive-clone``
# backfill on the first daemon start and on every start after.
_UPSERT_UPDATE_COLS = (
    "description",
    "writable",
    "lockable",
    "is_git_repo",
    "repo_url",
    "default_lock_mode",
    "auto_attach",
    "mode",
    "worktree_setup",
    "updated_at",
)


class WorkspaceKindQueryMixin:
    """Query mixin for ``workspace_kinds``. Expects ``self._engine``."""

    async def upsert_workspace_kind(self, kind: WorkspaceKind) -> None:
        """Insert or update a workspace kind, keyed by ``(project_id, id)``.

        ``kind.mode is None`` means "the source said nothing about mode": the
        column is left at whatever the row already holds (and takes the
        shipped default on insert).  Every other field is written
        unconditionally — the markdown is the source of truth.
        """
        now = time.time()
        update_cols = _UPSERT_UPDATE_COLS
        if kind.mode is None:
            update_cols = tuple(c for c in update_cols if c != "mode")
        values = {
            "project_id": kind.project_id,
            "id": kind.id,
            "description": kind.description,
            "writable": kind.writable,
            "lockable": kind.lockable,
            "is_git_repo": kind.is_git_repo,
            "repo_url": kind.repo_url,
            "default_lock_mode": kind.default_lock_mode,
            "auto_attach": kind.auto_attach,
            "mode": kind.mode or KIND_MODE_WORKTREE,
            "worktree_setup": json.dumps(list(kind.worktree_setup or [])),
            "created_at": kind.created_at or now,
            "updated_at": now,
        }
        async with self._engine.begin() as conn:
            dialect = conn.dialect.name
            if dialect == "sqlite":
                stmt = sqlite_insert(workspace_kinds).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "id"],
                    set_={c: stmt.excluded[c] for c in update_cols},
                )
                await conn.execute(stmt)
            elif dialect == "postgresql":
                stmt = pg_insert(workspace_kinds).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "id"],
                    set_={c: stmt.excluded[c] for c in update_cols},
                )
                await conn.execute(stmt)
            else:
                # Generic fallback: try update, then insert if 0 rows changed.
                update_values = {
                    k: v
                    for k, v in values.items()
                    if k not in ("project_id", "id", "created_at")
                    and (k != "mode" or kind.mode is not None)
                }
                result = await conn.execute(
                    update(workspace_kinds)
                    .where(
                        (workspace_kinds.c.project_id == kind.project_id)
                        & (workspace_kinds.c.id == kind.id)
                    )
                    .values(**update_values)
                )
                if result.rowcount == 0:
                    await conn.execute(insert(workspace_kinds).values(**values))

    async def get_workspace_kind(
        self, project_id: str, kind_id: str
    ) -> WorkspaceKind | None:
        """Fetch one kind by exact ``(project_id, id)`` — no fallback."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(workspace_kinds).where(
                    (workspace_kinds.c.project_id == project_id)
                    & (workspace_kinds.c.id == kind_id)
                )
            )
            row = result.mappings().fetchone()
            return self._row_to_workspace_kind(row) if row else None

    async def resolve_workspace_kind(
        self, project_id: str, kind_id: str
    ) -> WorkspaceKind | None:
        """Look up a kind for a project, falling back to the system row.

        Spec §3.5: project-scoped row shadows the system row with the same id.
        """
        kind = await self.get_workspace_kind(project_id, kind_id)
        if kind is not None:
            return kind
        return await self.get_workspace_kind(SYSTEM_KIND_SCOPE, kind_id)

    async def list_workspace_kinds_for_project(
        self, project_id: str
    ) -> list[WorkspaceKind]:
        """All kinds visible to a project: project rows + system rows whose
        ids are not shadowed by project rows. Sorted by id."""
        async with self._engine.begin() as conn:
            project_rows = (
                await conn.execute(
                    select(workspace_kinds).where(
                        workspace_kinds.c.project_id == project_id
                    )
                )
            ).mappings().fetchall()
            project_ids = {r["id"] for r in project_rows}
            system_rows = (
                await conn.execute(
                    select(workspace_kinds).where(
                        workspace_kinds.c.project_id == SYSTEM_KIND_SCOPE
                    )
                )
            ).mappings().fetchall()

        out = [self._row_to_workspace_kind(r) for r in project_rows]
        for r in system_rows:
            if r["id"] not in project_ids:
                out.append(self._row_to_workspace_kind(r))
        out.sort(key=lambda k: k.id)
        return out

    async def list_auto_attach_kinds_for_project(
        self, project_id: str
    ) -> list[WorkspaceKind]:
        """Auto-attach kinds visible to a project (project rows shadow system)."""
        kinds = await self.list_workspace_kinds_for_project(project_id)
        return [k for k in kinds if k.auto_attach]

    async def list_all_workspace_kinds(self) -> list[WorkspaceKind]:
        """All rows in workspace_kinds (system + every project). Used by the
        vault watcher's bootstrap and prune passes."""
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(select(workspace_kinds))
            ).mappings().fetchall()
        return [self._row_to_workspace_kind(r) for r in rows]

    async def delete_workspace_kind(
        self, project_id: str, kind_id: str
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(workspace_kinds).where(
                    (workspace_kinds.c.project_id == project_id)
                    & (workspace_kinds.c.id == kind_id)
                )
            )

    @staticmethod
    def _decode_worktree_setup(raw) -> list[str]:
        """Decode the JSON-in-Text ``worktree_setup`` column defensively.

        A hand-edited row (or a pre-migration NULL) must not take the daemon
        down — an undecodable value degrades to "no setup commands".
        """
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(c) for c in raw]
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("workspace_kinds.worktree_setup is not valid JSON: %r", raw)
            return []
        if not isinstance(parsed, list):
            logger.warning("workspace_kinds.worktree_setup is not a list: %r", raw)
            return []
        return [str(c) for c in parsed]

    @classmethod
    def _row_to_workspace_kind(cls, row) -> WorkspaceKind:
        return WorkspaceKind(
            project_id=row["project_id"],
            id=row["id"],
            description=row["description"],
            writable=bool(row["writable"]),
            lockable=bool(row["lockable"]),
            is_git_repo=bool(row["is_git_repo"]),
            repo_url=row["repo_url"],
            default_lock_mode=row["default_lock_mode"],
            auto_attach=bool(row["auto_attach"]),
            mode=row["mode"] or KIND_MODE_WORKTREE,
            worktree_setup=cls._decode_worktree_setup(row["worktree_setup"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
