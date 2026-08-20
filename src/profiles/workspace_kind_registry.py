"""WorkspaceKindStore — markdown ↔ DB reconciliation. See spec §4.

Unlike :class:`McpRegistry` (which is a pure in-memory projection of the
vault), workspace kinds live in the ``workspace_kinds`` DB table.  This
store keeps the table in sync with the markdown files under
``vault/[projects/<pid>/]workspace-kinds/``.

Reconciliation strategy:

- :meth:`scan` walks the vault, upserts every parsed file into the DB,
  and deletes DB rows whose markdown is no longer present.
- :meth:`bootstrap` writes markdown files for any DB row that has no file
  yet — used after the workspaces-v2 migration which seeds system kinds
  directly into the DB.
- :meth:`ensure_project_dir` creates an empty
  ``vault/projects/<pid>/workspace-kinds/`` directory for new projects,
  matching the ``mcp-servers`` convention.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from src.models import SYSTEM_KIND_SCOPE, WorkspaceKind
from src.profiles.workspace_kind_parser import parse_workspace_kind_file

logger = logging.getLogger(__name__)


class WorkspaceKindStore:
    """Reconciles vault ``workspace-kinds`` markdown with the DB."""

    def __init__(self, db, vault_root: Path | str):
        self.db = db
        self.vault_root = Path(vault_root)

    # ---------------------------------------------------------------- scan

    async def scan(self) -> None:
        """Full reconciliation: upsert every file, delete every orphan row."""
        seen: set[tuple[str, str]] = set()

        # System-wide kinds.
        sys_dir = self.vault_root / "workspace-kinds"
        if sys_dir.is_dir():
            for f in sorted(sys_dir.glob("*.md")):
                try:
                    kind = parse_workspace_kind_file(f, project_id=SYSTEM_KIND_SCOPE)
                    await self.db.upsert_workspace_kind(kind)
                    seen.add((SYSTEM_KIND_SCOPE, kind.id))
                except Exception as e:
                    logger.warning("Failed to parse %s: %s", f, e)

        # Project-scoped kinds.
        projects_root = self.vault_root / "projects"
        if projects_root.is_dir():
            for proj_dir in sorted(projects_root.iterdir()):
                if not proj_dir.is_dir():
                    continue
                pid = proj_dir.name
                kinds_dir = proj_dir / "workspace-kinds"
                if not kinds_dir.is_dir():
                    continue
                for f in sorted(kinds_dir.glob("*.md")):
                    try:
                        kind = parse_workspace_kind_file(f, project_id=pid)
                        await self.db.upsert_workspace_kind(kind)
                        seen.add((pid, kind.id))
                    except Exception as e:
                        logger.warning("Failed to parse %s: %s", f, e)

        # Prune: any DB row not seen on disk gets deleted (spec §3.5 — file
        # delete reconciles to row delete).  Only prune rows from scopes
        # whose directory exists; otherwise we'd nuke seeded system rows
        # before bootstrap had a chance to write the markdown.
        sys_dir_exists = sys_dir.is_dir()
        scoped_dirs_with_files = {SYSTEM_KIND_SCOPE} if sys_dir_exists else set()
        if projects_root.is_dir():
            for proj_dir in projects_root.iterdir():
                if proj_dir.is_dir() and (proj_dir / "workspace-kinds").is_dir():
                    scoped_dirs_with_files.add(proj_dir.name)

        for k in await self.db.list_all_workspace_kinds():
            if k.project_id in scoped_dirs_with_files and (k.project_id, k.id) not in seen:
                logger.info(
                    "Removing workspace_kind (%s, %s) — no markdown found",
                    k.project_id,
                    k.id,
                )
                await self.db.delete_workspace_kind(k.project_id, k.id)

    # ----------------------------------------------------------- bootstrap

    async def bootstrap(self) -> None:
        """Ensure markdown exists for every kind currently in the DB.

        Called on daemon start so the operator can edit the files even when
        the migration seeded rows directly.  Spec §9.3.
        """
        for k in await self.db.list_all_workspace_kinds():
            md_path = self._path_for_kind(k.project_id, k.id)
            if md_path.exists():
                continue
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(self._render_kind_markdown(k))
            logger.info(
                "Bootstrapped workspace_kind markdown: %s (kind=%s, scope=%s)",
                md_path,
                k.id,
                k.project_id,
            )

    # ----------------------------------------------------- project_dir init

    def ensure_project_dir(self, project_id: str) -> None:
        """Create ``vault/projects/<pid>/workspace-kinds/`` if missing.

        Mirrors the ``mcp-servers`` convention.  Called from the project-create
        flow so operators can drop overrides into the directory immediately.
        """
        d = self.vault_root / "projects" / project_id / "workspace-kinds"
        d.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- helpers

    def _path_for_kind(self, project_id: str, kind_id: str) -> Path:
        if project_id == SYSTEM_KIND_SCOPE:
            return self.vault_root / "workspace-kinds" / f"{kind_id}.md"
        return (
            self.vault_root
            / "projects"
            / project_id
            / "workspace-kinds"
            / f"{kind_id}.md"
        )

    @staticmethod
    def _render_kind_markdown(k: WorkspaceKind) -> str:
        fm = {
            "id": k.id,
            "description": k.description,
            "writable": k.writable,
            "lockable": k.lockable,
            "is_git_repo": k.is_git_repo,
            "auto_attach": k.auto_attach,
        }
        if k.repo_url:
            fm["repo_url"] = k.repo_url
        if k.default_lock_mode:
            fm["default_lock_mode"] = k.default_lock_mode
        # Git provisioning mode + slot setup (worktree-execution §2.1/§3.6).
        # Rendered unconditionally for git kinds so the operator can see and
        # flip the knob; an upgrading install renders whatever the substrate
        # migration backfilled ('exclusive-clone'), so bootstrap never
        # changes an existing install's behavior.
        if k.is_git_repo:
            fm["mode"] = k.mode
            fm["worktree_setup"] = list(k.worktree_setup or [])
        body = (
            f"\n\n# {k.id}\n\n{k.description}\n"
            if k.description
            else f"\n\n# {k.id}\n"
        )
        return f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---{body}"
