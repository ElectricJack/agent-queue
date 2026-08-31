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
from dataclasses import replace
from pathlib import Path

import yaml

from src.models import (
    KIND_MODE_EXCLUSIVE_CLONE,
    KIND_MODE_WORKTREE,
    SYSTEM_KIND_SCOPE,
    WorkspaceKind,
)
from src.profiles.workspace_kind_parser import parse_workspace_kind_file
from src.vault_index import is_generated_hub_file

logger = logging.getLogger(__name__)


def _inject_mode_frontmatter(text: str, mode: str | None) -> str | None:
    """Add ``mode: <mode>`` to a kind file's frontmatter.  ``None`` = no change.

    Returns ``None`` when the file already declares ``mode``, has no
    frontmatter block to edit, or *mode* is itself unknown — the caller then
    leaves the file untouched.  The insertion is a single line appended to
    the end of the existing frontmatter, so no other key is reformatted and
    the body is copied verbatim.
    """
    if not mode:
        return None
    if not text.startswith("---"):
        return None
    lines = text.split("\n")
    end_idx = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == "---"), None
    )
    if end_idx is None:
        return None
    fm = yaml.safe_load("\n".join(lines[1:end_idx])) or {}
    if not isinstance(fm, dict) or "mode" in fm:
        return None
    lines.insert(end_idx, f"mode: {mode}")
    return "\n".join(lines)


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
                if is_generated_hub_file(f):
                    logger.debug("Skipping generated vault hub file %s", f)
                    continue
                try:
                    kind = parse_workspace_kind_file(f, project_id=SYSTEM_KIND_SCOPE)
                    await self.db.upsert_workspace_kind(kind)
                    seen.add((SYSTEM_KIND_SCOPE, kind.id))
                except Exception as e:
                    logger.warning("Failed to parse %s: %s", f, e)
                    # The file is still on disk, so its row is not an orphan.
                    # Bootstrap names each file after the kind id, so the stem
                    # is the right fallback key: a mid-edit parse failure must
                    # not be read as "the markdown was deleted" and prune the
                    # last good row.
                    seen.add((SYSTEM_KIND_SCOPE, f.stem))

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
                    if is_generated_hub_file(f):
                        logger.debug("Skipping generated vault hub file %s", f)
                        continue
                    try:
                        kind = parse_workspace_kind_file(f, project_id=pid)
                        await self.db.upsert_workspace_kind(kind)
                        seen.add((pid, kind.id))
                    except Exception as e:
                        logger.warning("Failed to parse %s: %s", f, e)
                        seen.add((pid, f.stem))

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

        Two ``mode``-specific concerns are settled here (worktree-execution
        §7.1/§7.2), because both directions of the P6 flag flip depend on the
        markdown and the DB agreeing:

        * **Upgrade.** An install whose ``project-repo.md`` predates ``mode``
          is not rewritten (this method only writes files that do not exist),
          so :meth:`backfill_mode_frontmatter` injects the DB's stored value
          into it once.  Until it does — and for hand-authored files — the
          parser reports an absent key as ``None`` and the upsert leaves the
          column alone.
        * **Fresh install.** The substrate migration's blanket
          ``UPDATE workspace_kinds SET mode = 'exclusive-clone'`` cannot tell
          rows it is *upgrading* from rows the preceding revision just seeded,
          so a brand-new install came out on the legacy mode instead of the
          shipped default.  :meth:`_normalize_fresh_install_modes` corrects
          that, under conditions strict enough that no install with any
          history can match.
        """
        await self._normalize_fresh_install_modes()
        for k in await self.db.list_all_workspace_kinds():
            md_path = self._path_for_kind(k.project_id, k.id)
            if md_path.exists():
                continue
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(self._render_kind_markdown(k), encoding="utf-8")
            logger.info(
                "Bootstrapped workspace_kind markdown: %s (kind=%s, scope=%s)",
                md_path,
                k.id,
                k.project_id,
            )
        await self.backfill_mode_frontmatter()

    async def _normalize_fresh_install_modes(self) -> None:
        """Give a genuinely fresh install the shipped ``mode`` default.

        Runs only when the install has **no projects, no workspaces, and no
        workspace-kind markdown at all** — i.e. the daemon has never done
        anything.  Under those conditions the ``exclusive-clone`` on a git
        kind can only have come from the migration's blanket backfill, and
        §7.2 says a fresh install gets the shipped default.  Anything with
        history fails at least one of the three tests and is left alone, so
        §7.1 still holds.
        """
        if (self.vault_root / "workspace-kinds").exists():
            return
        if await self.db.list_projects():
            return
        if await self.db.list_workspaces():
            return

        for k in await self.db.list_all_workspace_kinds():
            if not k.is_git_repo or k.mode != KIND_MODE_EXCLUSIVE_CLONE:
                continue
            await self.db.upsert_workspace_kind(replace(k, mode=KIND_MODE_WORKTREE))
            logger.info(
                "Fresh install: workspace_kind (%s, %s) set to mode=%s "
                "(the shipped default; the migration's upgrade backfill does "
                "not apply to a new install)",
                k.project_id,
                k.id,
                KIND_MODE_WORKTREE,
            )

    async def backfill_mode_frontmatter(self) -> None:
        """One-shot: write the DB's ``mode`` into markdown files lacking it.

        An install that upgrades with a pre-``mode`` kind file would
        otherwise sit forever in the "frontmatter is silent" state, where the
        operator cannot see which provisioning strategy is in force and the
        file is not the source of truth it claims to be (principle #1).
        Injecting the stored value makes the file explicit and idempotent:
        the next ``scan()`` upserts the same value it read.

        Only files that parse and whose kind exists in the DB are touched,
        and only the ``mode:`` key is added — everything else in the file,
        including comments and body prose, is preserved byte-for-byte.
        """
        for k in await self.db.list_all_workspace_kinds():
            if not k.is_git_repo:
                continue
            md_path = self._path_for_kind(k.project_id, k.id)
            if not md_path.exists():
                continue
            try:
                text = md_path.read_text()
                injected = _inject_mode_frontmatter(text, k.mode)
            except Exception as e:
                logger.warning("Could not backfill mode into %s: %s", md_path, e)
                continue
            if injected is None:
                continue
            md_path.write_text(injected)
            logger.info(
                "Backfilled 'mode: %s' into %s from the database "
                "(the file predates worktree-execution)",
                k.mode,
                md_path,
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
