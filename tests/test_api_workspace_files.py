"""Tests for /api/workspaces/{workspace_id}/browse and .../file."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import dependencies as deps
from src.api.auth import RequestScope
from src.api.workspace_files import build_workspace_files_router
from src.database import Database
from src.models import Project, RepoSourceType, Workspace


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# hello\n")
    (root / "sub").mkdir()
    (root / "sub" / "nested.py").write_text("print(1)\n")
    return root


@pytest.fixture
async def wired(tmp_path, repo):
    db = Database(str(tmp_path / "aq.db"))
    await db.initialize()
    await db.create_project(Project(id="proj", name="P", repo_default_branch="main"))
    await db.create_project(Project(id="other", name="O", repo_default_branch="main"))
    await db.create_workspace(Workspace(
        id="ws1", project_id="proj", workspace_path=str(repo),
        source_type=RepoSourceType.CLONE, name="main",
    ))
    await db.create_workspace(Workspace(
        id="ws-no-path", project_id="proj", workspace_path="",
        source_type=RepoSourceType.CLONE, name="empty",
    ))

    orch = MagicMock()
    orch.db = db
    orch.config = MagicMock()

    app = FastAPI()

    # No TokenAuthMiddleware is mounted on this bare test app (httpx's
    # ASGITransport calls the app directly with no middleware stack), so
    # the router's ``getattr(request.state, "scope", LOCAL_SCOPE)`` inline
    # read (matching every other router's pattern — see src/api/messages.py)
    # never finds ``request.state.scope`` unless something sets it. This
    # tiny middleware stands in for TokenAuthMiddleware for scope-sensitive
    # tests, pulling the desired scope out of a mutable box the test can
    # swap between requests.
    scope_box: dict[str, RequestScope | None] = {"scope": None}

    @app.middleware("http")
    async def _inject_scope(request, call_next):
        if scope_box["scope"] is not None:
            request.state.scope = scope_box["scope"]
        return await call_next(request)

    app.include_router(build_workspace_files_router(db=db))
    prev_orch = deps._orchestrator
    deps._orchestrator = orch

    def _client(scope: RequestScope | None = None) -> AsyncClient:
        scope_box["scope"] = scope
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://t")

    try:
        yield _client, db, repo
    finally:
        deps._orchestrator = prev_orch
        await db.close()


async def test_browse_root_lists_entries(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/browse")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["path"] == ""
    names = [e["name"] for e in body["entries"]]
    assert names == ["sub", "README.md"]  # dirs first, alphabetical within group
    readme = next(e for e in body["entries"] if e["name"] == "README.md")
    assert readme["type"] == "file"
    assert readme["size"] == 8
    sub = next(e for e in body["entries"] if e["name"] == "sub")
    assert sub["type"] == "dir"
    assert "size" not in sub


async def test_browse_subdir(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/browse", params={"path": "sub"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "sub"
    assert [e["name"] for e in body["entries"]] == ["nested.py"]


async def test_browse_rejects_traversal(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/browse", params={"path": "../../etc"})
    assert r.status_code == 403


async def test_browse_rejects_symlink_escape(wired, tmp_path):
    client_factory, _, repo = wired
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, repo / "escape")
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/browse", params={"path": "escape"})
    assert r.status_code == 403


async def test_browse_path_is_a_file_is_404(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/browse", params={"path": "README.md"})
    assert r.status_code == 404


async def test_browse_unknown_workspace_is_404(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/does-not-exist/browse")
    assert r.status_code == 404


async def test_browse_no_workspace_path(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws-no-path/browse")
    assert r.status_code == 200
    body = r.json()
    assert body["entries"] == []
    assert body["reason"] == "no_workspace_path"


async def test_browse_out_of_scope_project_is_404(wired):
    client_factory, _, _ = wired
    scope = RequestScope(kind="session", project_id="other")
    async with client_factory(scope) as ac:
        r = await ac.get("/api/workspaces/ws1/browse")
    assert r.status_code == 404


async def test_browse_global_admin_scope_succeeds(wired):
    client_factory, _, _ = wired
    scope = RequestScope(kind="session", project_id=None, elevated=True)
    async with client_factory(scope) as ac:
        r = await ac.get("/api/workspaces/ws1/browse")
    assert r.status_code == 200


async def test_file_returns_content(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/file", params={"path": "README.md"})
    assert r.status_code == 200
    assert r.text == "# hello\n"


async def test_file_binary_returns_json_reason(wired, tmp_path):
    client_factory, _, repo = wired
    (repo / "logo.png").write_bytes(b"\x89PNG\x00\x00\x00")
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/file", params={"path": "logo.png"})
    assert r.status_code == 200
    assert r.json()["reason"] == "binary"


async def test_file_size_cap(wired, tmp_path):
    client_factory, _, repo = wired
    (repo / "big.bin").write_bytes(b"x" * (512 * 1024 + 1))
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/file", params={"path": "big.bin"})
    assert r.status_code == 413


async def test_file_rejects_absolute_path(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/file", params={"path": "/etc/passwd"})
    assert r.status_code == 403


async def test_file_out_of_scope_project_is_404(wired):
    client_factory, _, _ = wired
    scope = RequestScope(kind="session", project_id="other")
    async with client_factory(scope) as ac:
        r = await ac.get("/api/workspaces/ws1/file", params={"path": "README.md"})
    assert r.status_code == 404
