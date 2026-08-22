"""Tests for /api/tasks/{task_id}/files and /api/tasks/{task_id}/file."""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import dependencies as deps
from src.api.task_files import build_task_files_router
from src.database import Database
from src.git.manager import GitManager
from src.models import Project, RepoSourceType, Task, TaskStatus, Workspace


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A fresh git repo with a main branch + a task branch that adds a file."""
    root = tmp_path / "repo"
    root.mkdir()
    _run(["git", "init", "-q", "-b", "main"], root)
    _run(["git", "config", "user.email", "t@e"], root)
    _run(["git", "config", "user.name", "t"], root)
    (root / "README.md").write_text("# original\n")
    _run(["git", "add", "."], root)
    _run(["git", "commit", "-q", "-m", "init"], root)
    _run(["git", "checkout", "-q", "-b", "task-branch"], root)
    (root / "new_file.py").write_text("print('hello')\n")
    (root / "README.md").write_text("# original\n\nchanged\n")
    _run(["git", "add", "."], root)
    _run(["git", "commit", "-q", "-m", "work"], root)
    return root


@pytest.fixture
def wired(tmp_path, repo):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    db = Database(str(tmp_path / "aq.db"))
    loop.run_until_complete(db.initialize())
    loop.run_until_complete(
        db.create_project(Project(id="proj", name="P", repo_default_branch="main"))
    )
    loop.run_until_complete(db.create_task(Task(
        id="task1", project_id="proj", title="t", description="",
        status=TaskStatus.IN_PROGRESS, branch_name="task-branch",
    )))
    loop.run_until_complete(db.create_workspace(Workspace(
        id="ws1", project_id="proj", workspace_path=str(repo),
        source_type=RepoSourceType.CLONE, name="main",
        locked_by_task_id="task1",
    )))

    orch = MagicMock()
    orch.db = db
    orch.git = GitManager()
    orch.config = MagicMock()

    app = FastAPI()
    app.include_router(build_task_files_router())
    prev_orch = deps._orchestrator
    deps._orchestrator = orch
    try:
        with TestClient(app) as c:
            yield c, db, repo, loop
    finally:
        loop.run_until_complete(db.close())
        deps._orchestrator = prev_orch
        loop.close()


def test_files_returns_diff_stats(wired):
    client, _, repo, _ = wired
    r = client.get("/api/tasks/task1/files")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["workspace_path"] == str(repo)
    assert body["base"] in ("origin/main", "main")
    paths = {f["path"]: f for f in body["files"]}
    assert "new_file.py" in paths
    assert paths["new_file.py"]["status"] == "A"
    assert paths["new_file.py"]["additions"] == 1
    assert paths["new_file.py"]["deletions"] == 0
    assert paths["README.md"]["status"] == "M"
    assert paths["README.md"]["additions"] == 2


def test_files_no_workspace_returns_empty_with_reason(wired):
    client, db, _, loop = wired
    loop.run_until_complete(db.release_workspaces_for_task("task1"))
    r = client.get("/api/tasks/task1/files")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["files"] == []
    assert body["reason"] == "no_workspace"
    assert body["workspace_path"] is None


def test_files_unknown_task_is_404(wired):
    client, _, _, _ = wired
    r = client.get("/api/tasks/does-not-exist/files")
    assert r.status_code == 404


# ── Task 2: /file endpoint ────────────────────────────────────────────


def test_file_returns_content(wired):
    client, _, _, _ = wired
    r = client.get("/api/tasks/task1/file", params={"path": "new_file.py"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "print('hello')\n"


def test_file_rejects_parent_traversal(wired):
    client, _, _, _ = wired
    r = client.get("/api/tasks/task1/file", params={"path": "../secret"})
    assert r.status_code == 403


def test_file_rejects_absolute_path(wired):
    client, _, _, _ = wired
    r = client.get("/api/tasks/task1/file", params={"path": "/etc/passwd"})
    assert r.status_code == 403


def test_file_rejects_symlink_escape(wired, tmp_path):
    client, _, repo, _ = wired
    secret = tmp_path / "secret.txt"
    secret.write_text("shh")
    link = repo / "escape"
    os.symlink(secret, link)
    r = client.get("/api/tasks/task1/file", params={"path": "escape"})
    assert r.status_code == 403


def test_file_size_cap(wired):
    client, _, repo, _ = wired
    big = repo / "big.bin"
    big.write_bytes(b"x" * (512 * 1024 + 1))
    r = client.get("/api/tasks/task1/file", params={"path": "big.bin"})
    assert r.status_code == 413


def test_file_missing_is_404(wired):
    client, _, _, _ = wired
    r = client.get("/api/tasks/task1/file", params={"path": "nope.txt"})
    assert r.status_code == 404


def test_file_rejects_nested_parent_traversal(wired):
    """Even nested traversal like ``sub/../../etc`` must be caught."""
    client, _, repo, _ = wired
    (repo / "sub").mkdir(exist_ok=True)
    r = client.get(
        "/api/tasks/task1/file", params={"path": "sub/../../outside"}
    )
    # Either 404 (resolved path doesn't exist) or 403 (escapes) is safe;
    # what MUST NOT happen is a 200 leaking data outside the workspace.
    assert r.status_code in (403, 404)


def test_file_rejects_symlink_to_directory_escape(wired, tmp_path):
    """A symlinked subdir whose target lies outside must not leak files."""
    client, _, repo, _ = wired
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "leak.txt").write_text("leak")
    os.symlink(outside, repo / "linkdir")
    r = client.get(
        "/api/tasks/task1/file", params={"path": "linkdir/leak.txt"}
    )
    assert r.status_code == 403


def test_file_binary_returns_json_reason(wired):
    """A file with NUL bytes must return JSON {reason:'binary'}, not scrambled text."""
    client, _, repo, _ = wired
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRfoo")
    r = client.get("/api/tasks/task1/file", params={"path": "logo.png"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["success"] is True
    assert body["reason"] == "binary"
    assert body["size"] > 0
    assert body["path"] == "logo.png"
