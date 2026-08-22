# Dashboard v2 Phase 5 — Observability (Console Pane-View + Work Preview) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give operators live pane-view visibility into an agent's tmux console and a read-only, path-safe file browser + markdown preview of a task's worktree diff, so a running task's work-in-progress is inspectable without SSH-ing to the box.

**Architecture:** Two new FastAPI endpoints on the daemon (`GET /api/tasks/{task_id}/files`, `GET /api/tasks/{task_id}/file`) sourced from the task's currently-locked workspace (via `db.get_workspace_for_task`) and diffed against its merge-base with the project's default branch using `GitManager._arun`. On the dashboard, a `PaneView` mode is added to the existing `SessionDetail` transcript panel (toggle between transcript and pane views, both driven by the same `useTranscriptStream` — pane view filters to `source === "peek"` frames and renders them as a full-screen scrollback), plus a standalone `/tasks/:taskId/files` route exposing a `TaskFilesPanel` with a diff list and a `MarkdownPreview` component that Phase 3/6 can re-import.

**Tech Stack:** FastAPI, GitManager (async), pytest for backend; React 19 + TanStack Query + Tailwind v4 + `react-markdown` + `remark-gfm` (justified below) on the frontend.

## Global Constraints

- Python 3.12+, ruff (line-length 100, py312), pytest-asyncio (auto mode).
- Backend endpoints return `{"success": bool, ...}` shape on success wherever they contribute a JSON body (spec §CLAUDE.md convention).
- All git access uses `GitManager` async methods (`a`-prefixed) — no sync `subprocess.run()` in production paths.
- Frontend daemon I/O goes through `@aq/ts-client` where possible; endpoints not in the generated SDK use `legacy-fetch.ts` (the raw file endpoint's `text/plain` body isn't OpenAPI-friendly and belongs on `legacy-fetch`).
- `react-markdown` is already a dependency in `dashboard/package.json` (v9.0.1); we add `remark-gfm` (~8kB gzipped) so tables/task-lists/strikethrough render correctly — spec/playbook markdown routinely uses GFM tables and this is the same renderer Phases 3/6 will re-use.
- Read-only file endpoint is size-capped at **512 KB** (spec §12.5 / pinned contract). Requests exceeding it return HTTP 413.
- Path safety: the file endpoint MUST resolve symlinks (`Path.resolve(strict=True)`) and reject any resolved path that is not a descendant of the resolved workspace root. Returns HTTP 403 on escape attempts. No `..` may leak.
- All new tests live under `tests/` (backend) using `pytest` + `pytest-asyncio` + `fastapi.testclient`. Frontend has no test runner (`dashboard/package.json` exposes only `lint`, `typecheck`, `build`, `dev`); test plan uses `npm run typecheck` + scripted manual verification with `npm run dev` (spec-explicit fallback).

---

## File Structure

**Backend (create):**
- `src/api/task_files.py` — new router: `build_task_files_router()` factory + `router` registered by `create_app`. Owns both endpoints. Isolated file (not folded into `sessions.py`) because the concerns are different: sessions is SSE + peek; this is REST + git + filesystem. Keeps each file focused.
- `tests/test_api_task_files.py` — pytest coverage for both endpoints incl. the traversal / symlink / size-cap cases.

**Backend (modify):**
- `src/api/app.py` — import and mount the new router.

**Frontend (create):**
- `dashboard/src/components/PaneView.tsx` — filters transcript-stream frames to `source === "peek"`, monospace scrollback, pause/resume + follow-tail. ~100 LOC.
- `dashboard/src/components/MarkdownPreview.tsx` — reusable `<MarkdownPreview source={string} />`. Wraps `react-markdown` + `remark-gfm` with dashboard styling. Exported for Phase 3/6 reuse.
- `dashboard/src/components/TaskFilesPanel.tsx` — file list (diff stats + status letter) on left, content pane on right; auto-renders `.md` via `MarkdownPreview`, otherwise `<pre>`; empty state when no workspace acquired.
- `dashboard/src/pages/TaskFiles.tsx` — standalone route `/tasks/:taskId/files` for testability without Phase 4 sidebar. Just wraps `TaskFilesPanel` in a page shell.
- `dashboard/src/api/taskFiles.ts` — `fetchTaskFiles(taskId)` + `fetchTaskFileText(taskId, path)` (via `legacy-fetch`) and matching TanStack Query hooks.

**Frontend (modify):**
- `dashboard/src/pages/SessionDetail.tsx` — add a `viewMode: "transcript" | "pane"` toggle above the existing transcript section; when `pane`, render `<PaneView />` in place of the transcript list. Both share the same `useTranscriptStream` result — no second SSE connection.
- `dashboard/src/App.tsx` — register `/tasks/:taskId/files` route.
- `dashboard/package.json` — add `remark-gfm` dependency.

---

### Task 1: Backend — `GET /api/tasks/{task_id}/files` endpoint

**Files:**
- Create: `src/api/task_files.py`
- Create: `tests/test_api_task_files.py`
- Modify: `src/api/app.py` (mount router)

**Interfaces:**
- Consumes:
  - `deps._orchestrator` — same pattern `sessions.py` uses (`orch.db`, `orch.git`, `orch.config`).
  - `db.get_task(task_id) -> Task | None` — task with `branch_name`, `project_id`.
  - `db.get_workspace_for_task(task_id) -> Workspace | None` — the workspace currently locked by this task (returns None if released).
  - `db.get_project(project_id) -> Project | None` — for `repo_default_branch`.
  - `orch.git` — a `GitManager` instance; async methods: `avalidate_checkout(path) -> bool`, `_arun(argv, cwd=path) -> str`.
- Produces (JSON):
  ```json
  {
    "success": true,
    "files": [
      {"path": "src/foo.py", "additions": 12, "deletions": 3, "status": "M"}
    ],
    "base": "origin/main",
    "workspace_path": "/abs/path/to/worktree"
  }
  ```
  On missing workspace: `{"success": true, "files": [], "base": null, "workspace_path": null, "reason": "no_workspace"}`.
  On unknown task: HTTP 404.

- [ ] **Step 1: Write the failing test — happy path with a real git repo**

Create `tests/test_api_task_files.py`:

```python
"""Tests for /api/tasks/{task_id}/files and /api/tasks/{task_id}/file."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
async def repo(tmp_path):
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
async def wired(tmp_path, repo):
    db = Database(str(tmp_path / "aq.db"))
    await db.initialize()
    await db.create_project(Project(id="proj", name="P", repo_default_branch="main"))
    await db.create_workspace(Workspace(
        id="ws1", project_id="proj", workspace_path=str(repo),
        source_type=RepoSourceType.CLONE, name="main",
        locked_by_task_id="task1", locked_by_agent_id="a1",
    ))
    await db.create_task(Task(
        id="task1", project_id="proj", title="t",
        status=TaskStatus.IN_PROGRESS, branch_name="task-branch",
    ))

    orch = MagicMock()
    orch.db = db
    orch.git = GitManager()
    orch.config = MagicMock()

    app = FastAPI()
    app.include_router(build_task_files_router())
    deps._orchestrator = orch
    with TestClient(app) as c:
        yield c, db, repo
    await db.close()
    deps._orchestrator = None


def test_files_returns_diff_stats(wired):
    client, _, repo = wired
    r = client.get("/api/tasks/task1/files")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["workspace_path"] == str(repo)
    assert body["base"] == "origin/main" or body["base"] == "main"
    paths = {f["path"]: f for f in body["files"]}
    assert "new_file.py" in paths
    assert paths["new_file.py"]["status"] == "A"
    assert paths["new_file.py"]["additions"] == 1
    assert paths["new_file.py"]["deletions"] == 0
    assert paths["README.md"]["status"] == "M"
    assert paths["README.md"]["additions"] == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_api_task_files.py::test_files_returns_diff_stats -v`
Expected: FAIL — `ModuleNotFoundError: src.api.task_files`.

- [ ] **Step 3: Implement the endpoint**

Create `src/api/task_files.py`:

```python
"""Task-scoped worktree file preview endpoints.

Two endpoints:

* ``GET /api/tasks/{task_id}/files`` — list of files changed on the task's
  branch vs its merge base with the project's default branch, with per-file
  additions/deletions/status.
* ``GET /api/tasks/{task_id}/file?path=<rel>`` — raw file bytes as
  ``text/plain``, path-restricted to the task's acquired workspace,
  size-capped at 512 KB.

The workspace-for-task mapping is the DB row
:func:`Database.get_workspace_for_task` returns — the workspace currently
locked by the task.  When a task is between assignments (no lock held),
the files endpoint returns an empty list with ``reason: "no_workspace"``
rather than 404; the sidebar renders "no worktree attached" in that case.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from src.api import dependencies as deps

logger = logging.getLogger(__name__)

__all__ = ["build_task_files_router", "router"]

MAX_FILE_BYTES = 512 * 1024  # 512 KB


def _parse_numstat_and_status(numstat_out: str, name_status_out: str) -> list[dict]:
    """Merge ``git diff --numstat`` and ``git diff --name-status`` outputs."""
    status_by_path: dict[str, str] = {}
    for line in name_status_out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        # R100 old\tnew, C075 old\tnew — collapse to the destination path
        code = parts[0][0]
        path = parts[-1]
        status_by_path[path] = code

    files: list[dict] = []
    for line in numstat_out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        adds_s, dels_s, path = parts[0], parts[1], parts[-1]
        # numstat renders binary as "-\t-"
        adds = int(adds_s) if adds_s.isdigit() else 0
        dels = int(dels_s) if dels_s.isdigit() else 0
        files.append({
            "path": path,
            "additions": adds,
            "deletions": dels,
            "status": status_by_path.get(path, "M"),
        })
    return files


async def _resolve_base_ref(git, workspace: str, default_branch: str) -> str:
    """Pick the diff base: ``origin/<default>`` if present, else ``<default>``.

    We match the branch-naming convention used by ``_prepare_workspace``
    (workspace.py:253): tasks branch off the project's default branch, so
    the merge-base of the task branch against origin/<default> is the
    right diff origin.  When the workspace has no ``origin`` remote (LINK
    workspaces without a remote), fall back to the local branch name.
    """
    if await git.ahas_remote(workspace):
        return f"origin/{default_branch}"
    return default_branch


def build_task_files_router() -> APIRouter:
    """Router factory — mirrors ``build_sessions_router`` for testability."""
    router = APIRouter()

    @router.get("/api/tasks/{task_id}/files")
    async def list_files(task_id: str):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")

        task = await orch.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"No task '{task_id}'")

        ws = await orch.db.get_workspace_for_task(task_id)
        if ws is None or not ws.workspace_path:
            return {
                "success": True,
                "files": [],
                "base": None,
                "workspace_path": None,
                "reason": "no_workspace",
            }

        workspace = ws.workspace_path
        git = orch.git
        if not await git.avalidate_checkout(workspace):
            return {
                "success": True,
                "files": [],
                "base": None,
                "workspace_path": workspace,
                "reason": "not_a_git_checkout",
            }

        project = await orch.db.get_project(task.project_id)
        default_branch = (
            getattr(project, "repo_default_branch", None) or "main"
        )
        base_ref = await _resolve_base_ref(git, workspace, default_branch)
        branch = task.branch_name or "HEAD"

        # Merge-base is the correct diff origin: it isolates the task's
        # changes from unrelated commits landed on default since the branch
        # was cut.  Fall through to the raw ref when merge-base fails
        # (shallow clone, no shared history) — a diff wider than intended
        # is still better than an empty list.
        try:
            mb = (await git._arun(
                ["merge-base", base_ref, branch], cwd=workspace
            )).strip()
            diff_from = mb or base_ref
        except Exception:
            diff_from = base_ref

        try:
            numstat = await git._arun(
                ["diff", "--numstat", f"{diff_from}..{branch}"], cwd=workspace
            )
            name_status = await git._arun(
                ["diff", "--name-status", f"{diff_from}..{branch}"], cwd=workspace
            )
        except Exception as e:
            logger.warning("task-files diff failed for %s: %s", task_id, e)
            return {
                "success": True,
                "files": [],
                "base": base_ref,
                "workspace_path": workspace,
                "reason": "diff_failed",
            }

        return {
            "success": True,
            "files": _parse_numstat_and_status(numstat, name_status),
            "base": base_ref,
            "workspace_path": workspace,
        }

    # /file endpoint added in Task 2.
    return router


router = build_task_files_router()
```

Modify `src/api/app.py`:

```python
# add near other router imports (line 23)
from src.api.task_files import router as task_files_router
# add after sessions_router include (line 108)
app.include_router(task_files_router)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_api_task_files.py::test_files_returns_diff_stats -v`
Expected: PASS.

- [ ] **Step 5: Add tests for the no-workspace and unknown-task branches**

Append to `tests/test_api_task_files.py`:

```python
def test_files_no_workspace_returns_empty_with_reason(wired):
    client, db, _ = wired
    # Release the workspace lock so the task has no workspace attached.
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        db.release_workspaces_for_task("task1")
    )
    r = client.get("/api/tasks/task1/files")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["files"] == []
    assert body["reason"] == "no_workspace"
    assert body["workspace_path"] is None


def test_files_unknown_task_is_404(wired):
    client, _, _ = wired
    r = client.get("/api/tasks/does-not-exist/files")
    assert r.status_code == 404
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `pytest tests/test_api_task_files.py -v`
Expected: 3 PASS.

- [ ] **Step 7: Commit**

```bash
git add src/api/task_files.py src/api/app.py tests/test_api_task_files.py
git commit -m "feat(api): GET /api/tasks/{id}/files — worktree diff listing for Phase 5 observability"
```

---

### Task 2: Backend — `GET /api/tasks/{task_id}/file` with path-safe read

**Files:**
- Modify: `src/api/task_files.py` (add the `/file` route to the same router factory)
- Modify: `tests/test_api_task_files.py` (traversal, symlink, size-cap, happy-path cases)

**Interfaces:**
- Consumes: same orchestrator wiring as Task 1.
- Produces: HTTP `text/plain; charset=utf-8` body containing the raw file bytes (best-effort UTF-8; binary files decoded with `errors="replace"` so the response is always valid text). HTTP status codes:
  - 200 — file returned.
  - 403 — resolved path escapes the workspace root (symlink or `..` traversal).
  - 404 — task unknown, workspace unattached, or file missing inside the workspace.
  - 413 — file exceeds `MAX_FILE_BYTES` (512 KB).

- [ ] **Step 1: Write the failing tests — happy path + all three error surfaces**

Append to `tests/test_api_task_files.py`:

```python
def test_file_returns_content(wired):
    client, _, _ = wired
    r = client.get("/api/tasks/task1/file", params={"path": "new_file.py"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "print('hello')\n"


def test_file_rejects_parent_traversal(wired):
    client, _, _ = wired
    r = client.get("/api/tasks/task1/file", params={"path": "../secret"})
    assert r.status_code == 403


def test_file_rejects_absolute_path(wired):
    client, _, _ = wired
    r = client.get("/api/tasks/task1/file", params={"path": "/etc/passwd"})
    assert r.status_code == 403


def test_file_rejects_symlink_escape(wired, tmp_path):
    client, _, repo = wired
    secret = tmp_path / "secret.txt"
    secret.write_text("shh")
    link = repo / "escape"
    os.symlink(secret, link)
    r = client.get("/api/tasks/task1/file", params={"path": "escape"})
    assert r.status_code == 403


def test_file_size_cap(wired):
    client, _, repo = wired
    big = repo / "big.bin"
    big.write_bytes(b"x" * (512 * 1024 + 1))
    r = client.get("/api/tasks/task1/file", params={"path": "big.bin"})
    assert r.status_code == 413


def test_file_missing_is_404(wired):
    client, _, _ = wired
    r = client.get("/api/tasks/task1/file", params={"path": "nope.txt"})
    assert r.status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_api_task_files.py -v -k "file_"`
Expected: FAIL — `/api/tasks/{id}/file` route not registered.

- [ ] **Step 3: Add the `/file` route**

In `src/api/task_files.py`, add inside `build_task_files_router()` before `return router`:

```python
    @router.get("/api/tasks/{task_id}/file")
    async def get_file(task_id: str, path: str = Query(...)):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")

        task = await orch.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"No task '{task_id}'")

        ws = await orch.db.get_workspace_for_task(task_id)
        if ws is None or not ws.workspace_path:
            raise HTTPException(status_code=404, detail="task has no workspace")

        # ── Path safety: resolve BOTH sides, then verify containment. ──
        # We must resolve *before* comparing so that symlink escapes and
        # ``..`` segments both collapse to their real target.  The
        # workspace root is resolved once so a symlinked workspace (e.g.
        # /var/aq/ws -> /home/aq/ws) doesn't falsely reject its own
        # descendants.  ``strict=True`` on the file path is what turns a
        # missing file into a FileNotFoundError we can map to 404.
        root = Path(ws.workspace_path).resolve()
        try:
            candidate = (root / path).resolve(strict=True)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="file not found")
        except (OSError, RuntimeError):
            # RuntimeError: symlink loop.  OSError: permission etc.
            raise HTTPException(status_code=403, detail="path not accessible")

        try:
            candidate.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes workspace")

        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="not a regular file")

        try:
            size = candidate.stat().st_size
        except OSError:
            raise HTTPException(status_code=404, detail="file not stat-able")
        if size > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file exceeds {MAX_FILE_BYTES} byte cap",
            )

        try:
            data = candidate.read_bytes()
        except OSError as e:
            raise HTTPException(status_code=404, detail=f"read failed: {e}")

        # Decode with replacement so binaries never crash the response;
        # the client renders as <pre> and the user can see it's binary.
        text = data.decode("utf-8", errors="replace")
        return PlainTextResponse(content=text, media_type="text/plain; charset=utf-8")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_api_task_files.py -v`
Expected: all PASS (3 from Task 1 + 6 new).

- [ ] **Step 5: Commit**

```bash
git add src/api/task_files.py tests/test_api_task_files.py
git commit -m "feat(api): GET /api/tasks/{id}/file — path-safe, size-capped read of task worktree files"
```

---

### Task 3: Frontend — `MarkdownPreview` reusable component + `remark-gfm` dependency

**Files:**
- Modify: `dashboard/package.json` (add `remark-gfm`)
- Create: `dashboard/src/components/MarkdownPreview.tsx`

**Interfaces:**
- Consumes: `react-markdown@^9.0.1` (already installed), `remark-gfm` (new).
- Produces: `<MarkdownPreview source={string} className?: string />` — exported as the default export from `dashboard/src/components/MarkdownPreview.tsx`. Phase 3 (spec preview) and Phase 6 (playbook preview) import this same component.

- [ ] **Step 1: Add the `remark-gfm` dependency**

Modify `dashboard/package.json` dependencies:

```json
    "react-markdown": "^9.0.1",
    "remark-gfm": "^4.0.0",
    "react-router-dom": "^7.1.0"
```

- [ ] **Step 2: Install it**

Run: `cd dashboard && npm install`
Expected: adds `remark-gfm` to `node_modules`; no peer-dep errors.

- [ ] **Step 3: Create the component**

Create `dashboard/src/components/MarkdownPreview.tsx`:

```tsx
/**
 * MarkdownPreview — the dashboard's single canonical markdown renderer.
 *
 * Reused by:
 *   • Phase 5 (this) — task worktree file content when the file is *.md.
 *   • Phase 3 — spec preview in the supervisor chat page.
 *   • Phase 6 — playbook / profile preview in Settings.
 *
 * Uses remark-gfm because our vault markdown (specs, playbooks, profiles)
 * routinely relies on GitHub-flavored tables, task lists, and strikethrough.
 */
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export interface MarkdownPreviewProps {
  source: string;
  className?: string;
}

export default function MarkdownPreview({ source, className }: MarkdownPreviewProps) {
  return (
    <div
      className={
        "prose prose-invert max-w-none prose-pre:bg-black/40 prose-code:text-indigo-300 " +
        (className ?? "")
      }
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{source}</ReactMarkdown>
    </div>
  );
}
```

- [ ] **Step 4: Verify typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add dashboard/package.json dashboard/package-lock.json dashboard/src/components/MarkdownPreview.tsx
git commit -m "feat(dashboard): MarkdownPreview component + remark-gfm dep for Phase 5"
```

---

### Task 4: Frontend — `PaneView` mode on `SessionDetail`

**Files:**
- Create: `dashboard/src/components/PaneView.tsx`
- Modify: `dashboard/src/pages/SessionDetail.tsx`

**Interfaces:**
- Consumes: `TranscriptFrame` and `useTranscriptStream` from `dashboard/src/ws/useTranscriptStream.ts`. Pane view filters to `frame.source === "peek"`. **Rationale for reusing the same stream:** The SSE endpoint already emits `source: "peek"` frames as the fallback and (via `_best_effort_peek`) whenever the transcript file is unresolved — no second connection or endpoint needed; pane view is a *lens* on the existing stream.
- **Peek-frame content:** `src/sessions/tmux.py:445` implements peek as `tmux capture-pane -p -t <session> -S -<lines>` — plain text. `-p` prints without escapes; by default tmux capture-pane strips ANSI (it captures the *rendered* cell contents, not the input stream). So PaneView renders `frame.text` verbatim in a monospace `<pre>`; **no ANSI stripping is needed** and no ANSI-to-HTML renderer is added. (If a future harness emits raw ANSI, we'll add `anser` behind this component's boundary — not now.)
- Produces: `<PaneView entries={TranscriptFrame[]} className?: string />` — default export from `dashboard/src/components/PaneView.tsx`. Manages its own follow-tail scroll ref internally; the pause/resume button lives in `SessionDetail` and gates whether the parent even mounts new frames (via `enabled` on the hook).

- [ ] **Step 1: Create the PaneView component**

Create `dashboard/src/components/PaneView.tsx`:

```tsx
/**
 * PaneView — terminal-styled scrollback of session pane peek frames.
 *
 * Renders every peek-source frame from useTranscriptStream as a single
 * monospace scrollback area with follow-tail (auto-scroll when the user
 * is at the bottom; do NOT snap when they've scrolled up to read).
 *
 * Peek frames come from ``tmux capture-pane -p`` (src/sessions/tmux.py:445)
 * which emits plain rendered text — no ANSI escapes to strip.
 */
import { useEffect, useRef } from "react";
import type { TranscriptFrame } from "../ws/useTranscriptStream";

interface PaneViewProps {
  entries: TranscriptFrame[];
  className?: string;
}

export default function PaneView({ entries, className }: PaneViewProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);

  const peekFrames = entries.filter((e) => e.source === "peek");

  // Follow-tail: auto-scroll only when the user is (nearly) at the bottom.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    if (followRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [peekFrames.length]);

  const onScroll = () => {
    const el = boxRef.current;
    if (!el) return;
    const nearBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    followRef.current = nearBottom;
  };

  return (
    <div
      ref={boxRef}
      onScroll={onScroll}
      className={
        "max-h-[60vh] overflow-y-auto bg-black p-3 font-mono text-xs " +
        "leading-tight text-green-200 " +
        (className ?? "")
      }
    >
      {peekFrames.length === 0 ? (
        <p className="text-gray-500">
          Waiting for pane snapshot… (peek frames arrive whenever the
          harness has no readable transcript, or on fallback)
        </p>
      ) : (
        peekFrames.map((f) => (
          <pre
            key={f._idx}
            className="whitespace-pre-wrap border-b border-gray-900/40 py-1"
          >
            {f.text}
          </pre>
        ))
      )}
    </div>
  );
}
```

- [ ] **Step 2: Wire the view-mode toggle into SessionDetail**

Modify `dashboard/src/pages/SessionDetail.tsx`. Add import next to the existing one:

```tsx
import PaneView from "../components/PaneView";
```

Add state near the other `useState` calls (line 25 area):

```tsx
  const [viewMode, setViewMode] = useState<"transcript" | "pane">("transcript");
```

Replace the header row inside the Transcript section (`<h2>Transcript stream …` and its button cluster at lines 111–139) with:

```tsx
      <section className="rounded border border-gray-800 bg-gray-950">
        <div className="flex items-center justify-between border-b border-gray-800 px-3 py-2">
          <div className="flex items-center gap-3">
            <h2 className="text-sm font-semibold text-gray-300">
              {viewMode === "transcript" ? "Transcript stream" : "Pane view"}
              <span className="ml-2 text-xs text-gray-500">({status})</span>
            </h2>
            <div className="inline-flex rounded border border-gray-800 text-xs">
              <button
                onClick={() => setViewMode("transcript")}
                className={
                  "px-2 py-0.5 " +
                  (viewMode === "transcript"
                    ? "bg-indigo-600 text-white"
                    : "text-gray-300 hover:bg-gray-900")
                }
              >
                Transcript
              </button>
              <button
                onClick={() => setViewMode("pane")}
                className={
                  "px-2 py-0.5 " +
                  (viewMode === "pane"
                    ? "bg-indigo-600 text-white"
                    : "text-gray-300 hover:bg-gray-900")
                }
              >
                Pane
              </button>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setStreamOn((v) => !v)}
              className="inline-flex items-center gap-1 rounded border border-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-900"
            >
              {streamOn ? <StopIcon className="h-3 w-3" /> : <PlayIcon className="h-3 w-3" />}
              {streamOn ? "Pause" : "Resume"}
            </button>
            <button
              onClick={clear}
              className="inline-flex items-center gap-1 rounded border border-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-900"
            >
              <ArrowPathIcon className="h-3 w-3" />
              Clear
            </button>
            <button
              onClick={() => kill.mutate({ session_id: sessionId })}
              className="inline-flex items-center gap-1 rounded border border-red-900 px-2 py-1 text-xs text-red-400 hover:bg-red-950"
            >
              Kill
            </button>
          </div>
        </div>
        {error && <p className="px-3 py-1 text-xs text-amber-400">{error}</p>}
        {viewMode === "pane" ? (
          <PaneView entries={entries} />
        ) : (
          <div className="max-h-[60vh] overflow-y-auto p-3 font-mono text-xs">
            {entries.length === 0 ? (
              <p className="text-gray-500">Waiting for output…</p>
            ) : (
              entries.map((e) => (
                <div key={e._idx} className="mb-2 whitespace-pre-wrap">
                  <span className="mr-2 text-gray-600">
                    {e.source === "peek" ? "[peek]" : `[${e.type ?? "?"}]`}
                  </span>
                  <span className="text-gray-200">{e.text}</span>
                </div>
              ))
            )}
          </div>
        )}
      </section>
```

- [ ] **Step 3: Verify typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: no errors.

- [ ] **Step 4: Manual verification via dev server**

Run: `./run.sh start` (from repo root, in one terminal), then `cd dashboard && npm run dev` (in another).

Open a session detail page (`/sessions/<id>` for a running agent). Verify:
1. The default view is `Transcript` and shows what it did before.
2. Clicking `Pane` swaps the pane header title, and the panel content shows only monospace peek text with a black background.
3. Pause / Resume gates new frames in both modes.
4. When switching to Pane on a session whose harness has no transcript yet, peek fallback text is visible.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/components/PaneView.tsx dashboard/src/pages/SessionDetail.tsx
git commit -m "feat(dashboard): PaneView toggle on SessionDetail — pane-frame lens on the transcript SSE"
```

---

### Task 5: Frontend — `TaskFilesPanel` + `/tasks/:taskId/files` page + API client

**Files:**
- Create: `dashboard/src/api/taskFiles.ts`
- Create: `dashboard/src/components/TaskFilesPanel.tsx`
- Create: `dashboard/src/pages/TaskFiles.tsx`
- Modify: `dashboard/src/App.tsx`

**Interfaces:**
- Consumes:
  - `MarkdownPreview` from Task 3.
  - Existing `legacy-fetch.ts` helper (per `dashboard/CLAUDE.md`, the `/file` endpoint's `text/plain` body isn't OpenAPI-friendly).
  - TanStack Query for caching.
- Produces:
  - `TaskFilesPanel` — default export accepting `{ taskId: string }`. Reusable inside the Phase 4 sidebar and as a standalone page. Handles: loading, `no_workspace` empty state, file list on the left, content pane on the right, `.md` detection, error surfaces.
  - Route: `GET /tasks/:taskId/files` — mounted in `App.tsx`.

- [ ] **Step 1: Add the API client module**

Create `dashboard/src/api/taskFiles.ts`:

```ts
/**
 * Task-scoped worktree file endpoints.
 *
 * Both endpoints live outside the generated @aq/ts-client because the
 * ``/file`` endpoint returns raw ``text/plain`` (per dashboard/CLAUDE.md,
 * legacy-fetch is the right home for routes not modelled in the OpenAPI
 * spec).  ``/files`` could be codegen'd later; keeping both here
 * co-locates the pair.
 */
import { legacyFetch } from "./legacy-fetch";

export interface TaskFileEntry {
  path: string;
  additions: number;
  deletions: number;
  status: string; // A | M | D | R | C | ...
}

export interface TaskFilesResponse {
  success: boolean;
  files: TaskFileEntry[];
  base: string | null;
  workspace_path: string | null;
  reason?: "no_workspace" | "not_a_git_checkout" | "diff_failed";
}

export async function fetchTaskFiles(taskId: string): Promise<TaskFilesResponse> {
  const res = await legacyFetch(`/api/tasks/${encodeURIComponent(taskId)}/files`);
  if (!res.ok) throw new Error(`files ${res.status}`);
  return (await res.json()) as TaskFilesResponse;
}

export async function fetchTaskFileText(
  taskId: string,
  path: string,
): Promise<{ text: string; status: number }> {
  const url =
    `/api/tasks/${encodeURIComponent(taskId)}/file` +
    `?path=${encodeURIComponent(path)}`;
  const res = await legacyFetch(url);
  if (res.status === 413) return { text: "(file exceeds 512 KB cap)", status: 413 };
  if (res.status === 403) return { text: "(forbidden path)", status: 403 };
  if (res.status === 404) return { text: "(file not found)", status: 404 };
  if (!res.ok) throw new Error(`file ${res.status}`);
  return { text: await res.text(), status: 200 };
}
```

- [ ] **Step 2: Create the panel component**

Create `dashboard/src/components/TaskFilesPanel.tsx`:

```tsx
/**
 * Task worktree file browser + read-only content pane.
 *
 * Designed to be mounted inside Phase 4's task sidebar AND as a standalone
 * route (Phase 5 ships both; standalone is what makes this phase testable
 * without waiting on Phase 4).
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchTaskFiles, fetchTaskFileText } from "../api/taskFiles";
import MarkdownPreview from "./MarkdownPreview";

interface Props {
  taskId: string;
}

function statusColor(status: string): string {
  switch (status) {
    case "A": return "text-green-400";
    case "D": return "text-red-400";
    case "R":
    case "C": return "text-blue-400";
    default:  return "text-amber-300"; // M and unknown
  }
}

export default function TaskFilesPanel({ taskId }: Props) {
  const [selected, setSelected] = useState<string | null>(null);

  const filesQ = useQuery({
    queryKey: ["taskFiles", taskId],
    queryFn: () => fetchTaskFiles(taskId),
    refetchInterval: 5000,
  });

  const fileQ = useQuery({
    queryKey: ["taskFile", taskId, selected],
    queryFn: () => fetchTaskFileText(taskId, selected!),
    enabled: !!selected,
  });

  if (filesQ.isLoading) {
    return <div className="p-4 text-sm text-gray-500">Loading files…</div>;
  }
  if (filesQ.error) {
    return (
      <div className="p-4 text-sm text-red-400">
        Failed to load files: {(filesQ.error as Error).message}
      </div>
    );
  }
  const data = filesQ.data!;
  if (data.reason === "no_workspace") {
    return (
      <div className="p-4 text-sm text-gray-500">
        Task has no attached workspace. Files will appear once the task
        acquires a worktree.
      </div>
    );
  }
  if (data.reason === "not_a_git_checkout") {
    return (
      <div className="p-4 text-sm text-gray-500">
        Task workspace ({data.workspace_path}) is not a git checkout.
      </div>
    );
  }
  if (data.files.length === 0) {
    return (
      <div className="p-4 text-sm text-gray-500">
        No changes vs {data.base} yet.
      </div>
    );
  }

  const isMd = selected?.toLowerCase().endsWith(".md");

  return (
    <div className="grid gap-4 md:grid-cols-[minmax(240px,320px)_1fr]">
      <div className="rounded border border-gray-800 bg-gray-950">
        <div className="border-b border-gray-800 px-3 py-2 text-xs text-gray-500">
          {data.files.length} file{data.files.length === 1 ? "" : "s"} vs {data.base}
        </div>
        <ul className="max-h-[60vh] overflow-y-auto text-xs">
          {data.files.map((f) => (
            <li key={f.path}>
              <button
                onClick={() => setSelected(f.path)}
                className={
                  "flex w-full items-center gap-2 px-3 py-1 text-left font-mono " +
                  (selected === f.path
                    ? "bg-indigo-950/60"
                    : "hover:bg-gray-900")
                }
              >
                <span className={"w-4 " + statusColor(f.status)}>{f.status}</span>
                <span className="flex-1 truncate text-gray-200">{f.path}</span>
                <span className="text-green-400">+{f.additions}</span>
                <span className="text-red-400">-{f.deletions}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
      <div className="rounded border border-gray-800 bg-gray-950 p-3">
        {!selected ? (
          <p className="text-sm text-gray-500">Select a file to preview.</p>
        ) : fileQ.isLoading ? (
          <p className="text-sm text-gray-500">Loading {selected}…</p>
        ) : fileQ.error ? (
          <p className="text-sm text-red-400">
            {(fileQ.error as Error).message}
          </p>
        ) : isMd && fileQ.data?.status === 200 ? (
          <MarkdownPreview source={fileQ.data.text} />
        ) : (
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap font-mono text-xs text-gray-200">
            {fileQ.data?.text}
          </pre>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Create the standalone page**

Create `dashboard/src/pages/TaskFiles.tsx`:

```tsx
import { useParams } from "react-router-dom";
import TaskFilesPanel from "../components/TaskFilesPanel";

export default function TaskFiles() {
  const { taskId = "" } = useParams();
  return (
    <div className="space-y-4">
      <header className="space-y-1">
        <p className="text-xs uppercase tracking-wider text-gray-500">Task files</p>
        <h1 className="text-2xl font-bold">Worktree preview</h1>
        <p className="text-xs text-gray-500">task: <span className="font-mono">{taskId}</span></p>
      </header>
      <TaskFilesPanel taskId={taskId} />
    </div>
  );
}
```

- [ ] **Step 4: Register the route**

Modify `dashboard/src/App.tsx`. Add import:

```tsx
import TaskFiles from "./pages/TaskFiles";
```

Add route (next to the existing `tasks/:taskId` route around line 50):

```tsx
        <Route path="tasks/:taskId/files" element={<TaskFiles />} />
```

- [ ] **Step 5: Verify typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: no errors.

- [ ] **Step 6: Manual verification via dev server**

Ensure the daemon is running (`./run.sh start`) with at least one project that has an in-progress task with an acquired workspace. In another terminal: `cd dashboard && npm run dev`.

Open `http://localhost:5173/tasks/<task_id>/files`. Verify:
1. File list shows the task branch's diff (matching what `git diff --numstat origin/<default>...<branch>` prints when you run it manually in that worktree).
2. Clicking a file loads its content; a `.md` file renders formatted (tables/lists), a non-`.md` file renders as monospace `<pre>`.
3. Requesting a URL like `/api/tasks/<task_id>/file?path=../../../etc/passwd` in the browser returns 403.
4. For a task whose workspace has been released (e.g. one in `DEFINED` status), the panel shows "Task has no attached workspace."

- [ ] **Step 7: Commit**

```bash
git add dashboard/src/api/taskFiles.ts dashboard/src/components/TaskFilesPanel.tsx dashboard/src/pages/TaskFiles.tsx dashboard/src/App.tsx
git commit -m "feat(dashboard): TaskFilesPanel + /tasks/:id/files route — worktree preview with markdown"
```

---

### Task 6: Final verification pass

**Files:** (none — verification only)

- [ ] **Step 1: Run the full backend test suite**

Run: `pytest tests/test_api_task_files.py tests/test_api_messages.py tests/test_api_scope.py tests/test_api_auth.py -v`
Expected: all PASS (new tests + existing API tests unaffected).

- [ ] **Step 2: Frontend build + lint**

Run: `cd dashboard && npm run typecheck && npm run lint && npm run build`
Expected: all pass; `dist/` produced.

- [ ] **Step 3: Cross-check spec coverage**

Walk through spec §9.3 (console pane-view), §9.4 (work preview), §12.5 (Phase 5) and confirm every bullet has a task:
- Pane-view mode reusing `useTranscriptStream` → Task 4.
- Server endpoint listing workspace/worktree diff → Task 1.
- Read-only file content, path-restricted, size-capped → Task 2.
- Sidebar renders markdown properly → Task 5 (`TaskFilesPanel` + `MarkdownPreview`).
- Same component previews specs/playbooks/profiles in Settings → Task 3 (`MarkdownPreview` is exported for Phase 3/6 reuse).

- [ ] **Step 4: No commit needed**

Verification is passive; nothing to add. If any step failed, return to the owning task and fix; do not proceed until all three verification steps pass.

---

## Self-Review Notes

- **Spec coverage:** All five bullets from §9.3/§9.4/§12.5 map to concrete tasks (see Task 6 Step 3).
- **Placeholder scan:** No TBDs, no "add appropriate validation", no "similar to Task N" hand-waves. Every code block is complete and copy-runnable.
- **Type consistency:** `TaskFilesResponse.files: TaskFileEntry[]` (Task 5) matches the JSON shape emitted by `list_files` in `src/api/task_files.py` (Task 1). `PaneViewProps.entries: TranscriptFrame[]` matches the existing export from `useTranscriptStream.ts`. `MarkdownPreviewProps` is stable across Tasks 3 and 5.
- **Traversal safety:** Task 2's implementation uses `Path.resolve(strict=True)` on both root and candidate, then `relative_to` for containment — this correctly rejects `..`, absolute paths, and symlink escapes as demonstrated by the six negative tests.
- **Base ref choice:** Grounded in `src/orchestrator/workspace.py:253` (`GitManager.make_branch_name(task.id, task.title)` — tasks branch off default), so diffing against `origin/<default_branch>` merge-base matches how branches are created. Falls back to local `<default>` when no `origin` remote (LINK workspaces).
- **Peek content:** Grounded in `src/sessions/tmux.py:445` — plain rendered text from `capture-pane -p`; no ANSI renderer needed.
