"""WorkspaceKindStore — markdown ↔ DB reconciliation. See spec §4."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.database import Database
from src.models import SYSTEM_KIND_SCOPE
from src.profiles.workspace_kind_registry import WorkspaceKindStore


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def vault_root(tmp_path) -> Path:
    """Empty vault directory for the test."""
    root = tmp_path / "vault"
    root.mkdir()
    return root


async def test_scan_inserts_system_kinds_from_markdown(vault_root: Path, db):
    sys_dir = vault_root / "workspace-kinds"
    sys_dir.mkdir()
    (sys_dir / "custom-kind.md").write_text(
        textwrap.dedent(
            """
            ---
            id: custom-kind
            writable: true
            lockable: true
            is_git_repo: true
            default_lock_mode: exclusive
            ---
            Custom system kind.
            """
        ).strip()
    )
    store = WorkspaceKindStore(db, vault_root=vault_root)
    await store.scan()

    kind = await db.resolve_workspace_kind("any-project", "custom-kind")
    assert kind is not None
    assert kind.lockable is True
    assert kind.default_lock_mode == "exclusive"


async def test_scan_inserts_project_overrides(vault_root: Path, db):
    proj_dir = vault_root / "projects" / "p1" / "workspace-kinds"
    proj_dir.mkdir(parents=True)
    (proj_dir / "vault.md").write_text(
        textwrap.dedent(
            """
            ---
            id: vault
            description: project-specific vault
            writable: true
            lockable: false
            is_git_repo: false
            auto_attach: true
            ---
            """
        ).strip()
    )
    store = WorkspaceKindStore(db, vault_root=vault_root)
    await store.scan()

    kind = await db.resolve_workspace_kind("p1", "vault")
    assert kind.project_id == "p1"
    assert kind.description == "project-specific vault"


async def test_scan_removes_kinds_for_deleted_files(vault_root: Path, db):
    proj_dir = vault_root / "projects" / "p1" / "workspace-kinds"
    proj_dir.mkdir(parents=True)
    f = proj_dir / "extra.md"
    f.write_text("---\nid: extra\n---\n")

    store = WorkspaceKindStore(db, vault_root=vault_root)
    await store.scan()
    assert await db.get_workspace_kind("p1", "extra") is not None

    f.unlink()
    await store.scan()
    assert await db.get_workspace_kind("p1", "extra") is None


async def test_scan_does_not_prune_when_directory_missing(vault_root: Path, db):
    """If the vault has no system workspace-kinds dir, the seeded system rows
    survive — pruning is keyed off the directory existing on disk.

    Otherwise: a fresh checkout with no `vault/workspace-kinds/` would wipe
    the migration-seeded system kinds on first daemon boot.
    """
    # Migration already seeded 3 system kinds.
    store = WorkspaceKindStore(db, vault_root=vault_root)
    await store.scan()  # vault is empty — must not prune

    assert await db.get_workspace_kind(SYSTEM_KIND_SCOPE, "project-repo") is not None
    assert await db.get_workspace_kind(SYSTEM_KIND_SCOPE, "vault") is not None
    assert await db.get_workspace_kind(SYSTEM_KIND_SCOPE, "readonly-dir") is not None


async def test_bootstrap_creates_missing_system_markdown(vault_root: Path, db):
    """Migration seeds DB rows; bootstrap writes the markdown for them."""
    store = WorkspaceKindStore(db, vault_root=vault_root)
    await store.bootstrap()

    for kind_id in ("project-repo", "vault", "readonly-dir"):
        md = vault_root / "workspace-kinds" / f"{kind_id}.md"
        assert md.exists(), kind_id
        text = md.read_text()
        assert f"id: {kind_id}" in text


async def test_bootstrap_skips_existing_files(vault_root: Path, db):
    """Existing markdown is not overwritten — operator edits are preserved."""
    sys_dir = vault_root / "workspace-kinds"
    sys_dir.mkdir()
    md = sys_dir / "vault.md"
    md.write_text("---\nid: vault\ndescription: operator edit\n---\n")

    store = WorkspaceKindStore(db, vault_root=vault_root)
    await store.bootstrap()

    assert md.read_text() == "---\nid: vault\ndescription: operator edit\n---\n"


async def test_ensure_project_dir_creates_empty_dir(vault_root: Path, db):
    store = WorkspaceKindStore(db, vault_root=vault_root)
    store.ensure_project_dir("p-new")
    assert (vault_root / "projects" / "p-new" / "workspace-kinds").is_dir()


async def test_scan_then_bootstrap_round_trip(vault_root: Path, db):
    """After a full scan + bootstrap cycle, every system kind has both
    a DB row and a markdown file, and the parser reads back what we wrote."""
    store = WorkspaceKindStore(db, vault_root=vault_root)
    await store.bootstrap()
    await store.scan()  # parses what we just wrote — must not error

    kind = await db.get_workspace_kind(SYSTEM_KIND_SCOPE, "vault")
    assert kind is not None
    assert kind.auto_attach is True


async def test_scan_skips_invalid_files(vault_root: Path, db, caplog):
    """A file with bad frontmatter is logged and skipped — no exception."""
    sys_dir = vault_root / "workspace-kinds"
    sys_dir.mkdir()
    (sys_dir / "good.md").write_text("---\nid: good\n---\n")
    (sys_dir / "bad.md").write_text("---\nno id here\n---\n")

    store = WorkspaceKindStore(db, vault_root=vault_root)
    await store.scan()

    assert await db.get_workspace_kind(SYSTEM_KIND_SCOPE, "good") is not None
