"""Tests for ``aq://`` URI resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from src.aq_uri import AQ_SCHEME, AqUriError, is_aq_uri, resolve


@dataclass
class FakeConfig:
    data_dir: str
    vault_root: str


@dataclass
class FakeWorkspace:
    workspace_path: str


class FakeDb:
    def __init__(
        self,
        *,
        project_paths: dict[str, str] | None = None,
        workspaces: dict[str, str] | None = None,
    ):
        self._project_paths = project_paths or {}
        self._workspaces = workspaces or {}

    async def get_project_workspace_path(self, project_id: str) -> str | None:
        return self._project_paths.get(project_id)

    async def get_workspace(self, workspace_id: str) -> FakeWorkspace | None:
        path = self._workspaces.get(workspace_id)
        return FakeWorkspace(workspace_path=path) if path else None


@pytest.fixture
def config(tmp_path: Path) -> FakeConfig:
    return FakeConfig(data_dir=str(tmp_path), vault_root=str(tmp_path / "vault"))


# ---------------------------------------------------------------------------
# is_aq_uri / scheme
# ---------------------------------------------------------------------------


def test_is_aq_uri_recognises_scheme():
    assert is_aq_uri("aq://prompts/foo.md")
    assert is_aq_uri("aq://vault/projects/x/memory.md")


def test_is_aq_uri_rejects_other_schemes():
    assert not is_aq_uri("/abs/path")
    assert not is_aq_uri("file:///tmp/x")
    assert not is_aq_uri("prompts/foo.md")
    assert not is_aq_uri("")
    assert not is_aq_uri(None)


# ---------------------------------------------------------------------------
# Singleton authorities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_prompts_uses_bundled_dir(config):
    # aq://prompts/ resolves relative to the installed src/prompts/, not
    # config.data_dir — confirms a fresh install on a different machine
    # still finds bundled templates.
    resolved = await resolve("aq://prompts/consolidation_task.md", config=config)
    # The tail is what we care about; the parent is machine-specific.
    assert resolved.name == "consolidation_task.md"
    assert resolved.parent.name == "prompts"


@pytest.mark.asyncio
async def test_resolve_prompts_nested_path(config):
    resolved = await resolve(
        "aq://prompts/default_playbooks/memory-consolidation.md", config=config
    )
    assert resolved.name == "memory-consolidation.md"
    assert resolved.parent.name == "default_playbooks"


@pytest.mark.asyncio
async def test_resolve_vault(config, tmp_path):
    resolved = await resolve("aq://vault/projects/foo/memory.md", config=config)
    assert resolved == tmp_path / "vault" / "projects" / "foo" / "memory.md"


@pytest.mark.asyncio
async def test_resolve_logs(config, tmp_path):
    resolved = await resolve("aq://logs/daemon/2026-04-23.log", config=config)
    assert resolved == tmp_path / "logs" / "daemon" / "2026-04-23.log"


@pytest.mark.asyncio
async def test_resolve_tasks(config, tmp_path):
    resolved = await resolve("aq://tasks/abc-123/supervisor.md", config=config)
    assert resolved == tmp_path / "tasks" / "abc-123" / "supervisor.md"


@pytest.mark.asyncio
async def test_resolve_attachments(config, tmp_path):
    resolved = await resolve("aq://attachments/msg-42/file.pdf", config=config)
    assert resolved == tmp_path / "attachments" / "msg-42" / "file.pdf"


# ---------------------------------------------------------------------------
# Workspace authorities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_workspace_primary(config):
    db = FakeDb(project_paths={"proj-1": "/work/proj-1"})
    resolved = await resolve(
        "aq://workspace/proj-1/specs/plan.md", config=config, db=db
    )
    assert resolved == Path("/work/proj-1/specs/plan.md")


@pytest.mark.asyncio
async def test_resolve_workspace_unknown_project(config):
    db = FakeDb()
    with pytest.raises(AqUriError, match="No workspace found for project"):
        await resolve("aq://workspace/missing/x.md", config=config, db=db)


@pytest.mark.asyncio
async def test_resolve_workspace_id_specific(config):
    db = FakeDb(workspaces={"ws-7": "/work/clone-7"})
    resolved = await resolve(
        "aq://workspace-id/ws-7/src/foo.py", config=config, db=db
    )
    assert resolved == Path("/work/clone-7/src/foo.py")


@pytest.mark.asyncio
async def test_resolve_workspace_id_unknown(config):
    db = FakeDb()
    with pytest.raises(AqUriError, match="not found"):
        await resolve("aq://workspace-id/nope/x.md", config=config, db=db)


@pytest.mark.asyncio
async def test_workspace_authority_requires_db(config):
    with pytest.raises(AqUriError, match="db handle"):
        await resolve("aq://workspace/p/x.md", config=config, db=None)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejects_parent_traversal(config):
    with pytest.raises(AqUriError, match=r"\.\."):
        await resolve("aq://vault/../etc/passwd", config=config)


@pytest.mark.asyncio
async def test_rejects_parent_traversal_in_workspace(config):
    db = FakeDb(project_paths={"p": "/w"})
    with pytest.raises(AqUriError, match=r"\.\."):
        await resolve("aq://workspace/p/../../etc/passwd", config=config, db=db)


@pytest.mark.asyncio
async def test_rejects_unknown_authority(config):
    with pytest.raises(AqUriError, match="Unknown aq:// authority"):
        await resolve("aq://bogus/anything.md", config=config)


@pytest.mark.asyncio
async def test_rejects_malformed_scheme(config):
    with pytest.raises(AqUriError, match="Not an aq"):
        await resolve("http://example.com/foo", config=config)


@pytest.mark.asyncio
async def test_rejects_missing_path(config):
    with pytest.raises(AqUriError, match="missing path"):
        await resolve("aq://prompts", config=config)


@pytest.mark.asyncio
async def test_rejects_empty_path_after_authority(config):
    with pytest.raises(AqUriError, match="path after authority"):
        await resolve("aq://prompts/", config=config)


@pytest.mark.asyncio
async def test_rejects_workspace_without_id(config):
    db = FakeDb()
    with pytest.raises(AqUriError, match="<id>"):
        await resolve("aq://workspace/onlyone", config=config, db=db)


def test_scheme_constant_matches():
    assert AQ_SCHEME == "aq://"
