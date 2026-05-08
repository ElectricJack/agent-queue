"""WorkspaceKindQueryMixin — CRUD + resolution. See spec §3.1, §3.5."""

from __future__ import annotations

import time

import pytest

from src.database import Database
from src.models import SYSTEM_KIND_SCOPE, WorkspaceKind


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


def _kind(**overrides) -> WorkspaceKind:
    """Build a WorkspaceKind with sensible defaults plus overrides."""
    base = dict(
        project_id=SYSTEM_KIND_SCOPE,
        id="project-repo",
        description="",
        writable=True,
        lockable=True,
        is_git_repo=True,
        repo_url=None,
        default_lock_mode=None,
        auto_attach=False,
        created_at=time.time(),
        updated_at=time.time(),
    )
    base.update(overrides)
    return WorkspaceKind(**base)


class TestUpsertAndGet:
    async def test_upsert_then_get_returns_same_row(self, db):
        kind = _kind(
            project_id=SYSTEM_KIND_SCOPE,
            id="project-repo",
            description="Default project repo",
            default_lock_mode="exclusive",
        )
        await db.upsert_workspace_kind(kind)
        fetched = await db.get_workspace_kind(SYSTEM_KIND_SCOPE, "project-repo")
        assert fetched is not None
        assert fetched.description == "Default project repo"
        assert fetched.lockable is True
        assert fetched.default_lock_mode == "exclusive"

    async def test_upsert_updates_existing_row(self, db):
        await db.upsert_workspace_kind(_kind(description="v1"))
        await db.upsert_workspace_kind(_kind(description="v2"))
        fetched = await db.get_workspace_kind(SYSTEM_KIND_SCOPE, "project-repo")
        assert fetched.description == "v2"

    async def test_get_returns_none_for_missing(self, db):
        assert await db.get_workspace_kind("p1", "nope") is None


class TestResolution:
    async def test_resolve_uses_project_override(self, db):
        await db.upsert_workspace_kind(
            _kind(project_id=SYSTEM_KIND_SCOPE, id="vault",
                  description="system vault",
                  writable=True, lockable=False, is_git_repo=False, auto_attach=True)
        )
        await db.upsert_workspace_kind(
            _kind(project_id="p1", id="vault",
                  description="project vault override",
                  writable=True, lockable=False, is_git_repo=False, auto_attach=True)
        )

        resolved = await db.resolve_workspace_kind("p1", "vault")
        assert resolved.project_id == "p1"
        assert resolved.description == "project vault override"

    async def test_resolve_falls_back_to_system(self, db):
        await db.upsert_workspace_kind(
            _kind(project_id=SYSTEM_KIND_SCOPE, id="vault",
                  description="system vault")
        )
        resolved = await db.resolve_workspace_kind("p2", "vault")
        assert resolved is not None
        assert resolved.project_id == SYSTEM_KIND_SCOPE
        assert resolved.description == "system vault"

    async def test_resolve_returns_none_for_unknown(self, db):
        assert await db.resolve_workspace_kind("p1", "does-not-exist") is None


class TestListing:
    async def test_list_for_project_includes_overrides_and_unshadowed_system(self, db):
        # Migration seeds project-repo, vault, readonly-dir at the system scope.
        # Assert behavior on top of that baseline rather than asserting the
        # kinds list is exactly our inserts.
        await db.upsert_workspace_kind(
            _kind(project_id="p1", id="vault", description="custom")  # shadows system
        )
        await db.upsert_workspace_kind(
            _kind(project_id="p1", id="package-foo")  # project-only
        )

        kinds = await db.list_workspace_kinds_for_project("p1")
        by_id = {k.id: k for k in kinds}
        assert by_id["vault"].project_id == "p1"  # override wins
        assert by_id["vault"].description == "custom"
        assert by_id["readonly-dir"].project_id == SYSTEM_KIND_SCOPE
        assert by_id["project-repo"].project_id == SYSTEM_KIND_SCOPE
        assert by_id["package-foo"].project_id == "p1"

    async def test_list_auto_attach_only_returns_auto(self, db):
        await db.upsert_workspace_kind(
            _kind(project_id=SYSTEM_KIND_SCOPE, id="vault", auto_attach=True)
        )
        await db.upsert_workspace_kind(
            _kind(project_id=SYSTEM_KIND_SCOPE, id="readonly-dir", auto_attach=False)
        )
        autos = await db.list_auto_attach_kinds_for_project("p1")
        assert {k.id for k in autos} == {"vault"}

    async def test_list_all_returns_every_row(self, db):
        await db.upsert_workspace_kind(
            _kind(project_id="p1", id="vault")
        )
        await db.upsert_workspace_kind(
            _kind(project_id="p2", id="game-repo")
        )
        all_kinds = await db.list_all_workspace_kinds()
        keys = {(k.project_id, k.id) for k in all_kinds}
        # Migration seeds 3 system rows; we added 2 project rows on top.
        assert ("p1", "vault") in keys
        assert ("p2", "game-repo") in keys
        assert (SYSTEM_KIND_SCOPE, "project-repo") in keys
        assert (SYSTEM_KIND_SCOPE, "vault") in keys
        assert (SYSTEM_KIND_SCOPE, "readonly-dir") in keys


class TestDelete:
    async def test_delete_removes_kind(self, db):
        await db.upsert_workspace_kind(_kind(project_id="p1", id="game-repo"))
        assert await db.get_workspace_kind("p1", "game-repo") is not None
        await db.delete_workspace_kind("p1", "game-repo")
        assert await db.get_workspace_kind("p1", "game-repo") is None

    async def test_delete_resolves_to_system_after(self, db):
        """Deleting a project override falls back to the system row at resolve time."""
        await db.upsert_workspace_kind(
            _kind(project_id=SYSTEM_KIND_SCOPE, id="vault", description="sys")
        )
        await db.upsert_workspace_kind(
            _kind(project_id="p1", id="vault", description="override")
        )
        assert (await db.resolve_workspace_kind("p1", "vault")).description == "override"
        await db.delete_workspace_kind("p1", "vault")
        assert (await db.resolve_workspace_kind("p1", "vault")).description == "sys"
