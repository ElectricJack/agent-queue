"""add_workspace kind_id parameter + list_workspace_kinds. Spec §10."""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import DatabaseConfig, AppConfig
from src.database.queries.claim_queries import CLAIMED_BY_SESSION_KEY
from src.models import (
    Agent,
    AgentProfile,
    KIND_MODE_WORKTREE,
    Project,
    Project as _Project,
    RepoSourceType,
    SessionRecord,
    SYSTEM_KIND_SCOPE,
    Task,
    Workspace as _Workspace,
    WorkspaceKind,
    WorkspaceKind as _WorkspaceKind,
)
from src.orchestrator import Orchestrator
from src.orchestrator.worktree_manager import EXCLUDE_BLOCK
from src.runtimes.base import Runtime
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def setup(tmp_path, reuse_database):
    db = await reuse_database("test.db")
    await db.create_project(Project(id="p1", name="test", repo_url=""))

    # Add a project-scoped kind for the kind_id-acceptance tests.
    await db.upsert_workspace_kind(
        WorkspaceKind(
            project_id="p1",
            id="game-repo",
            description="Game repo",
            writable=True,
            lockable=True,
            is_git_repo=True,
            default_lock_mode="exclusive",
            created_at=time.time(),
            updated_at=time.time(),
        )
    )

    orch = MagicMock()
    orch.db = db
    orch._emit_notify = AsyncMock()
    orch.git = MagicMock()
    orch.git.acreate_checkout = AsyncMock()
    orch.git.aget_remote_url = AsyncMock(return_value=None)

    config = MagicMock()
    config.workspace_dir = str(tmp_path / "workspaces")

    handler = CommandHandler(orch, config)
    handler._active_project_id = "p1"

    yield handler, db, tmp_path


class TestAddWorkspaceKindId:
    async def test_default_is_project_repo(self, setup):
        handler, db, tmp_path = setup
        link_path = tmp_path / "ws-default"
        link_path.mkdir()
        result = await handler._cmd_add_workspace({
            "project_id": "p1",
            "source": "link",
            "path": str(link_path),
        })
        assert "created" in result, result
        assert result["kind_id"] == "project-repo"
        ws = await db.get_workspace(result["created"])
        assert ws.kind_id == "project-repo"

    async def test_explicit_kind_id_is_persisted(self, setup):
        handler, db, tmp_path = setup
        link_path = tmp_path / "ws-game"
        link_path.mkdir()
        result = await handler._cmd_add_workspace({
            "project_id": "p1",
            "source": "link",
            "path": str(link_path),
            "kind_id": "game-repo",
        })
        assert "created" in result, result
        assert result["kind_id"] == "game-repo"
        ws = await db.get_workspace(result["created"])
        assert ws.kind_id == "game-repo"

    async def test_unknown_kind_fails(self, setup):
        handler, db, tmp_path = setup
        link_path = tmp_path / "ws-bogus"
        link_path.mkdir()
        result = await handler._cmd_add_workspace({
            "project_id": "p1",
            "source": "link",
            "path": str(link_path),
            "kind_id": "does-not-exist",
        })
        assert "error" in result
        assert "does-not-exist" in result["error"]


async def test_slow_workspace_annotations_do_not_block_independent_inventory_reads(
    setup, monkeypatch,
):
    handler, db, tmp_path = setup
    path = tmp_path / "owned-source"
    path.mkdir()
    protected = path / "checkpoint.txt"
    protected.write_text("preserved source\n")
    await db.create_workspace(_Workspace(
        id="owned-workspace", project_id="p1", workspace_path=str(path),
        source_type=RepoSourceType.LINK, kind_id="game-repo",
    ))
    git = handler.orchestrator.git
    git.aread_status_summary = AsyncMock(return_value=("sample", False))
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def stalled(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with monkeypatch.context() as fault:
        fault.setattr(git, "aread_status_summary", stalled)
        request = asyncio.create_task(handler._cmd_list_workspaces({"project_id": "p1"}))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            assert [ws.id for ws in await db.list_workspaces(project_id="p1")] == [
                "owned-workspace",
            ]
            kinds = await handler._cmd_list_workspace_kinds({"project_id": "p1"})
            assert any(kind["id"] == "game-repo" for kind in kinds["workspace_kinds"])
            assert not request.done()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert cancelled.is_set()
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    complete = await handler._cmd_list_workspaces({"project_id": "p1"})
    assert complete["workspaces"][0]["branch"] == "sample"
    assert protected.read_text() == "preserved source\n"


class TestListWorkspaceKinds:
    async def test_lists_system_plus_project(self, setup):
        handler, db, _ = setup
        result = await handler._cmd_list_workspace_kinds({"project_id": "p1"})
        assert result["project_id"] == "p1"
        ids = {k["id"] for k in result["workspace_kinds"]}
        # system kinds (from migration)
        assert {"project-repo", "vault", "readonly-dir"}.issubset(ids)
        # project-scoped kind (from fixture)
        assert "game-repo" in ids

    async def test_marks_scope_correctly(self, setup):
        handler, db, _ = setup
        result = await handler._cmd_list_workspace_kinds({"project_id": "p1"})
        by_id = {k["id"]: k for k in result["workspace_kinds"]}
        assert by_id["project-repo"]["scope"] == "system"
        assert by_id["game-repo"]["scope"] == "project"

    async def test_no_project_returns_system_only(self, setup):
        handler, db, _ = setup
        handler._active_project_id = None  # clear active project
        result = await handler._cmd_list_workspace_kinds({})
        # No project context: only system rows.
        ids = {k["id"] for k in result["workspace_kinds"]}
        assert ids == {"project-repo", "vault", "readonly-dir"}
        # game-repo (project-scoped) should not appear.
        assert "game-repo" not in ids

    async def test_active_project_inherited(self, setup):
        handler, db, _ = setup
        # _active_project_id is "p1" from the fixture; no explicit project_id.
        result = await handler._cmd_list_workspace_kinds({})
        assert result["project_id"] == "p1"
        assert any(k["id"] == "game-repo" for k in result["workspace_kinds"])


# ═══════════════════════════════════════════════════════════════════════════
# Task A4 — workspace_doctor, workspace_reap, list annotations (P5).
#
# These use real Orchestrator + real slot manager against a real bare-origin
# repo, since the commands touch git worktree state.
# ═══════════════════════════════════════════════════════════════════════════


def _git_cli(args: list[str], cwd) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t.com", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


class _NullRuntimeFactory:
    def create(self, agent_type, profile=None, llm_logger=None) -> Runtime:
        raise AssertionError("no runtime should be created")


@pytest.fixture
def base_repo_for_wt(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True, capture_output=True,
    )
    base = tmp_path / "base"
    subprocess.run(
        ["git", "clone", str(origin), str(base)], check=True, capture_output=True
    )
    (base / "README.md").write_text("init\n")
    _git_cli(["add", "-A"], cwd=base)
    _git_cli(["commit", "-m", "init"], cwd=base)
    _git_cli(["push", "origin", "main"], cwd=base)
    return base


@pytest.fixture
async def worktree_handler(tmp_path, base_repo_for_wt):
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        database=DatabaseConfig(url=lease_dsn("aq.db")),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    config.worktrees.enabled = True
    orch = Orchestrator(config, runtimes=_NullRuntimeFactory())
    await orch.initialize()

    await orch.db.create_project(
        _Project(id="p1", name="alpha", repo_url="",
                 repo_default_branch="main", max_concurrent_agents=2)
    )
    await orch.db.upsert_workspace_kind(
        _WorkspaceKind(
            project_id=SYSTEM_KIND_SCOPE, id="project-repo",
            is_git_repo=True, lockable=True, writable=True,
            mode=KIND_MODE_WORKTREE, default_lock_mode="exclusive",
        )
    )
    await orch.db.create_workspace(
        _Workspace(id="ws-base", project_id="p1",
                   workspace_path=str(base_repo_for_wt),
                   source_type=RepoSourceType.CLONE, kind_id="project-repo")
    )
    await orch.db.upsert_profile(
        AgentProfile(id="p", name="p", model="claude-haiku-4-5-20251001",
                     permission_mode="bypassPermissions")
    )

    handler = CommandHandler(orch, config)
    handler._active_project_id = "p1"
    try:
        yield handler, orch, base_repo_for_wt
    finally:
        await orch.shutdown()


async def _make_slot_for(orch, task_id="tsk-1"):
    await orch.db.create_task(Task(id=task_id, project_id="p1",
                                   title=task_id, description=""))
    await orch.db.create_agent(Agent(id=task_id + "-a", name=task_id + "-a",
                                     profile_id="p"))
    t = await orch.db.get_task(task_id)
    a = await orch.db.get_agent(task_id + "-a")
    p = await orch._prepare_workspace(t, a)
    return await orch.db.get_workspace_for_task(task_id), Path(p)


async def test_resumed_exclusive_clone_handoff_excludes_daemon_bookkeeping(
    base_repo_for_wt, tmp_path, monkeypatch
):
    """Checkpoint resume keeps daemon state out of an exclusive clone's diff."""
    from src.orchestrator import task_checkpoint

    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        database=DatabaseConfig(url=lease_dsn("aq.db")),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    config.worktrees.enabled = False
    orch = Orchestrator(config, runtimes=_NullRuntimeFactory())
    await orch.initialize()
    try:
        await orch.db.create_project(
            Project(id="p1", name="alpha", repo_default_branch="main")
        )
        await orch.db.create_workspace(
            _Workspace(
                id="ws-clone",
                project_id="p1",
                workspace_path=str(base_repo_for_wt),
                source_type=RepoSourceType.CLONE,
                kind_id="project-repo",
            )
        )
        await orch.db.create_agent(Agent(id="agent", name="agent", profile_id="p"))
        await orch.db.create_task(Task(id="task", project_id="p1", title="task", description=""))
        await orch.db.set_task_meta("task", "manual_pause_checkpoint", {"saved": True})
        monkeypatch.setattr(task_checkpoint, "restore_checkpoint", AsyncMock(return_value="aq/task"))

        task = await orch.db.get_task("task")
        agent = await orch.db.get_agent("agent")
        assert await orch._prepare_workspace(task, agent) == str(base_repo_for_wt)

        (base_repo_for_wt / ".aq").mkdir()
        (base_repo_for_wt / ".aq" / "claim.json").write_text("{}\n")
        (base_repo_for_wt / ".aq-worktree.json").write_text("{}\n")
        (base_repo_for_wt / ".codex").mkdir()
        (base_repo_for_wt / ".codex" / "hooks.json").write_text("{}\n")

        status = _git_cli(["status", "--porcelain", "--untracked-files=all"], base_repo_for_wt)
        assert ".aq/claim.json" not in status
        assert ".aq-worktree.json" not in status
        assert ".codex/hooks.json" not in status
    finally:
        await orch.shutdown()


class TestWorkspaceDoctor:
    async def test_dirty_unlocked_slot_finding(self, worktree_handler):
        handler, orch, base = worktree_handler
        slot_ws, slot_dir = await _make_slot_for(orch)
        # Release lock, leave the slot dirty
        (slot_dir / "extra.txt").write_text("scratch\n")
        await orch.db.release_workspace(slot_ws.id)

        result = await handler._cmd_workspace_doctor({"project_id": "p1"})
        assert result["success"] is True
        kinds = {(f["kind"], f["workspace_id"]) for f in result["findings"]}
        assert ("dirty_unlocked_slot", slot_ws.id) in kinds

    async def test_stale_registration_finding(self, worktree_handler):
        handler, orch, base = worktree_handler
        _, slot_dir = await _make_slot_for(orch)
        # Delete the slot directory without pruning — creates a stale entry.
        import shutil
        shutil.rmtree(slot_dir)

        result = await handler._cmd_workspace_doctor({"project_id": "p1"})
        assert result["success"] is True
        findings = result["findings"]
        # A stale worktree registration must produce a finding whose
        # detail mentions the missing slot dir.
        stale = [f for f in findings if f["kind"] == "stale_registration"]
        assert stale, f"expected a stale_registration finding, got {findings!r}"
        assert any(str(slot_dir) in f["detail"] for f in stale), (
            f"expected stale finding to mention {slot_dir}, got {stale!r}"
        )

    async def test_exclude_missing_finding(self, worktree_handler):
        handler, orch, base = worktree_handler
        # Remove the exclude file entirely
        exclude = base / ".git" / "info" / "exclude"
        if exclude.exists():
            exclude.unlink()

        result = await handler._cmd_workspace_doctor({"project_id": "p1"})
        kinds = {(f["kind"], f["workspace_id"]) for f in result["findings"]}
        assert ("exclude_missing", "ws-base") in kinds

    async def test_exclude_check_runs_when_worktrees_are_disabled(self, worktree_handler):
        handler, orch, base = worktree_handler
        orch.config.worktrees.enabled = False
        exclude = base / ".git" / "info" / "exclude"
        if exclude.exists():
            exclude.unlink()

        result = await handler._cmd_workspace_doctor({"project_id": "p1"})

        kinds = {(finding["kind"], finding["workspace_id"]) for finding in result["findings"]}
        assert ("exclude_missing", "ws-base") in kinds

    async def test_exclude_check_ignores_explicit_non_git_kind(
        self, worktree_handler, tmp_path
    ):
        handler, orch, base = worktree_handler
        await orch.db.upsert_workspace_kind(
            _WorkspaceKind(
                project_id="p1",
                id="notes",
                is_git_repo=False,
                lockable=True,
                writable=True,
                mode="exclusive-clone",
                default_lock_mode="exclusive",
            )
        )
        non_git = tmp_path / "notes"
        non_git.mkdir()
        await orch.db.create_workspace(
            _Workspace(
                id="ws-notes",
                project_id="p1",
                workspace_path=str(non_git),
                source_type=RepoSourceType.LINK,
                kind_id="notes",
            )
        )

        result = await handler._cmd_workspace_doctor({"project_id": "p1"})

        assert not [
            finding
            for finding in result["findings"]
            if finding["workspace_id"] == "ws-notes" and finding["kind"].startswith("exclude_")
        ]

    async def test_separate_git_dir_checks_the_exact_exclude_path(
        self, worktree_handler, tmp_path
    ):
        handler, orch, base = worktree_handler
        metadata = tmp_path / "separate-metadata"
        workspace = tmp_path / "separate-workspace"
        subprocess.run(
            [
                "git",
                "clone",
                f"--separate-git-dir={metadata}",
                str(base.parent / "origin.git"),
                str(workspace),
            ],
            check=True,
            capture_output=True,
        )
        (metadata / "info" / "exclude").write_text(EXCLUDE_BLOCK)
        await orch.db.create_workspace(
            _Workspace(
                id="ws-separate",
                project_id="p1",
                workspace_path=str(workspace),
                source_type=RepoSourceType.CLONE,
                kind_id="project-repo",
            )
        )

        result = await handler._cmd_workspace_doctor({"project_id": "p1"})

        findings = [
            finding
            for finding in result["findings"]
            if finding["workspace_id"] == "ws-separate"
        ]
        assert not [finding for finding in findings if finding["kind"] == "exclude_missing"]

    async def test_git_subdirectory_base_is_unverifiable(
        self, worktree_handler
    ):
        handler, orch, base = worktree_handler
        nested = base / "nested"
        nested.mkdir()
        await orch.db.create_workspace(
            _Workspace(
                id="ws-subdir",
                project_id="p1",
                workspace_path=str(nested),
                source_type=RepoSourceType.LINK,
                kind_id="project-repo",
            )
        )

        result = await handler._cmd_workspace_doctor({"project_id": "p1"})

        findings = [
            finding
            for finding in result["findings"]
            if finding["workspace_id"] == "ws-subdir"
            and finding["kind"].startswith("exclude_")
        ]
        assert [finding["kind"] for finding in findings] == ["exclude_unverifiable"]
        assert "repository root" in findings[0]["detail"]

    async def test_redundant_clone_finding(self, worktree_handler, tmp_path):
        handler, orch, base = worktree_handler
        # Add a second base clone under project-repo — should be flagged.
        extra_path = tmp_path / "extra_base"
        subprocess.run(
            ["git", "clone", str(base.parent / "origin.git"), str(extra_path)],
            check=True, capture_output=True,
        )
        await orch.db.create_workspace(
            _Workspace(id="ws-extra", project_id="p1",
                       workspace_path=str(extra_path),
                       source_type=RepoSourceType.CLONE,
                       kind_id="project-repo")
        )

        result = await handler._cmd_workspace_doctor({"project_id": "p1"})
        kinds = {(f["kind"], f["workspace_id"]) for f in result["findings"]}
        # One of the two is designated; the other is flagged as redundant.
        redundant_ids = {ws for (k, ws) in kinds if k == "redundant_clone"}
        assert len(redundant_ids) == 1


class TestWorkspaceReap:
    async def test_reap_refused_when_slot_is_live(
        self, worktree_handler, monkeypatch,
    ):
        handler, orch, base = worktree_handler
        slot_ws, slot_dir = await _make_slot_for(orch)
        await orch.db.release_workspace(slot_ws.id)

        from src.sessions.proctable import ProcEntry

        async def fake_scan(marker_key):
            return [ProcEntry(pid=99999, ppid=1, comm="agent",
                              start_ticks=1, marker="tsk-1")]

        monkeypatch.setattr(
            "src.sessions.proctable.scan_by_env_marker", fake_scan
        )

        result = await handler._cmd_workspace_reap({"workspace_id": slot_ws.id})
        assert result["success"] is True
        assert result["reaped"] == []
        assert result["skipped"] and result["skipped"][0]["id"] == slot_ws.id
        # The refused reap must NOT have deleted the slot dir or its row.
        assert slot_dir.is_dir(), f"slot dir {slot_dir} was removed despite refusal"
        assert await orch.db.get_workspace(slot_ws.id) is not None

    async def test_all_retired_requires_project_scope(
        self, worktree_handler,
    ):
        handler, orch, base = worktree_handler
        # Clear the active project so nothing is scoped.
        handler._active_project_id = None

        result = await handler._cmd_workspace_reap({"all_retired": True})
        assert result["success"] is False
        assert "project" in result["error"].lower()

    async def test_reap_succeeds_on_retired(
        self, worktree_handler, monkeypatch,
    ):
        handler, orch, base = worktree_handler
        slot_ws, _ = await _make_slot_for(orch)
        await orch.db.release_workspace(slot_ws.id)

        async def fake_scan(marker_key):
            return []

        monkeypatch.setattr(
            "src.sessions.proctable.scan_by_env_marker", fake_scan
        )

        result = await handler._cmd_workspace_reap({"workspace_id": slot_ws.id})
        assert result["success"] is True
        assert slot_ws.id in result["reaped"]
        assert await orch.db.get_workspace(slot_ws.id) is None


class TestListWorkspacesAnnotations:
    async def test_annotations_present(self, worktree_handler):
        handler, orch, base = worktree_handler
        _, _ = await _make_slot_for(orch)

        result = await handler._cmd_list_workspaces({"project_id": "p1"})
        rows = result["workspaces"]
        # Every row carries the new annotations
        for r in rows:
            for key in ("role", "slot_index", "mode", "branch", "dirty"):
                assert key in r

        base_row = next(r for r in rows if r["id"] == "ws-base")
        slot_row = next(r for r in rows if r["role"] == "slot")
        assert base_row["role"] == "base"
        assert base_row["slot_index"] is None
        assert base_row["mode"] == KIND_MODE_WORKTREE
        assert slot_row["slot_index"] == 0
        assert slot_row["mode"] == KIND_MODE_WORKTREE
        assert slot_row["branch"] == "aq/tsk-1"


async def _live_session(orch, sid, *, task_id, work_dir, phase="active", epoch=None):
    await orch.db.create_session(SessionRecord(
        id=sid, task_id=task_id, project_id="p1", profile_id="p", harness="fake",
        provider="fake", name=sid, lifecycle="pool", state="running",
        desired_state="running", work_dir=str(work_dir), epoch="epoch",
        instance_token=sid, started_at=100.0, last_activity=110.0,
    ))
    await orch.db.update_session(
        sid, agent_id=task_id + "-a", claim_phase=phase, last_claim_epoch=epoch
    )


class TestListWorkspacesBoundedDiagnostics:
    """Bounded owner/checkout readback (src/workspace_listing.py)."""

    async def test_complete_list_with_held_owner(self, worktree_handler):
        handler, orch, _ = worktree_handler
        slot_ws, slot_dir = await _make_slot_for(orch)
        task = await orch.db.get_task("tsk-1")
        await _live_session(orch, "sess-1", task_id="tsk-1", work_dir=slot_dir,
                            epoch=task.claim_epoch)
        await orch.db.set_task_meta("tsk-1", CLAIMED_BY_SESSION_KEY, "sess-1")

        result = await handler._cmd_list_workspaces({"project_id": "p1"})
        diag = result["diagnostics"]
        assert diag["complete"] is True
        assert diag["owner_facts"] == "complete"
        assert diag["incomplete_rows"] == 0
        assert diag["git_status_counts"] == {"ok": 2}

        row = next(r for r in result["workspaces"] if r["id"] == slot_ws.id)
        assert (row["presence"], row["git_status"], row["branch"]) == (
            "present", "ok", "aq/tsk-1")
        assert row["beyond_cap"] is False
        owner = row["owner"]
        assert owner["classification"] == "held"
        assert owner["session"]["id"] == "sess-1"
        assert owner["task"]["claimed_by_session"] == "sess-1"
        assert owner["session_matches_claim"] is True
        assert owner["fence"] in ("consistent", "unknown")

    async def test_stale_historical_slot_stays_distinct(self, worktree_handler, tmp_path):
        handler, orch, _ = worktree_handler
        slot_ws, _ = await _make_slot_for(orch)
        await orch.db.create_workspace(_Workspace(
            id="ws-historic", project_id="p1",
            workspace_path=str(tmp_path / "gone" / "slot-5"),
            source_type=RepoSourceType.CLONE, kind_id="project-repo",
            slot_index=5, base_workspace_id="ws-base",
        ))
        rows = {r["id"]: r for r in
                (await handler._cmd_list_workspaces({"project_id": "p1"}))["workspaces"]}

        historic, active = rows["ws-historic"], rows[slot_ws.id]
        assert (historic["presence"], historic["git_status"], historic["beyond_cap"]) == (
            "missing", "missing", True)
        assert historic["branch"] is None and historic["dirty"] is None
        assert historic["owner"]["classification"] == "unlocked"
        assert (active["presence"], active["git_status"], active["beyond_cap"]) == (
            "present", "ok", False)
        assert active["locked_by_task_id"] == "tsk-1"

    async def test_preparing_writer_is_not_probed(self, worktree_handler, monkeypatch):
        handler, orch, _ = worktree_handler
        slot_ws, slot_dir = await _make_slot_for(orch)
        await _live_session(orch, "sess-1", task_id="tsk-1", work_dir=slot_dir,
                            phase="preparing")
        probed: list[str] = []
        real = orch.git.aread_status_summary

        async def _spy(path, **kw):
            probed.append(path)
            return await real(path, **kw)

        monkeypatch.setattr(orch.git, "aread_status_summary", _spy)
        result = await handler._cmd_list_workspaces({"project_id": "p1"})
        row = next(r for r in result["workspaces"] if r["id"] == slot_ws.id)
        assert row["owner"]["classification"] == "preparing"
        assert row["git_status"] == "skipped_active_writer"
        assert row["branch"] is None
        assert row["locked_by_task_id"] == "tsk-1"
        assert str(slot_dir) not in probed

    async def test_stuck_git_returns_partial_within_deadline(
        self, worktree_handler, monkeypatch
    ):
        handler, orch, _ = worktree_handler
        slot_ws, slot_dir = await _make_slot_for(orch)
        real = orch.git.aread_status_summary

        async def _stuck(path, **kw):
            if path == str(slot_dir):
                await asyncio.sleep(3600)
            return await real(path, **kw)

        monkeypatch.setattr(orch.git, "aread_status_summary", _stuck)
        started = time.monotonic()
        result = await handler._cmd_list_workspaces(
            {"project_id": "p1", "deadline_seconds": 1})
        assert time.monotonic() - started < 1 + 5.0

        diag = result["diagnostics"]
        assert diag["complete"] is False
        assert diag["incomplete_rows"] == 1
        rows = {r["id"]: r for r in result["workspaces"]}
        slot = rows[slot_ws.id]
        assert slot["git_status"] == "timeout"
        assert slot["branch"] is None and slot["dirty"] is None
        # The lock and owner readback survive the stuck probe.
        assert slot["locked_by_task_id"] == "tsk-1"
        assert slot["owner"]["facts"] == "complete"
        assert rows["ws-base"]["git_status"] == "ok"

    async def test_owner_facts_failure_keeps_lock_fields(
        self, worktree_handler, monkeypatch
    ):
        handler, orch, _ = worktree_handler
        slot_ws, _ = await _make_slot_for(orch)

        async def _boom(rows):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(orch.db, "read_workspace_owner_facts", _boom)
        result = await handler._cmd_list_workspaces({"project_id": "p1"})
        assert result["diagnostics"]["complete"] is False
        assert result["diagnostics"]["owner_facts"] == "unavailable"
        row = next(r for r in result["workspaces"] if r["id"] == slot_ws.id)
        assert row["owner"]["classification"] == "unknown"
        assert row["locked_by_task_id"] == "tsk-1"
        assert row["locked_by_agent_id"] == "tsk-1-a"
        assert row["beyond_cap"] is None
        assert row["branch"] == "aq/tsk-1"

    async def test_no_git_skips_probes(self, worktree_handler):
        handler, orch, _ = worktree_handler
        await _make_slot_for(orch)
        result = await handler._cmd_list_workspaces({"project_id": "p1", "git": False})
        assert {r["git_status"] for r in result["workspaces"]} == {"skipped"}
        assert all(r["branch"] is None for r in result["workspaces"])
        assert result["diagnostics"]["git_requested"] is False
        assert result["diagnostics"]["complete"] is True

    @pytest.mark.parametrize(
        "args", [{"deadline_seconds": "soon"}, {"deadline_seconds": True}, {"git": "yes"}])
    async def test_invalid_arguments(self, worktree_handler, args):
        handler, _, _ = worktree_handler
        result = await handler._cmd_list_workspaces({"project_id": "p1", **args})
        assert "error" in result

    async def test_cancellation_cancels_probes(self, worktree_handler, monkeypatch):
        handler, orch, _ = worktree_handler
        await _make_slot_for(orch)
        started_probe = asyncio.Event()
        cancelled: list[str] = []

        async def _stuck(path, **kw):
            started_probe.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(path)
                raise

        monkeypatch.setattr(orch.git, "aread_status_summary", _stuck)
        task = asyncio.create_task(handler._cmd_list_workspaces({"project_id": "p1"}))
        await asyncio.wait_for(started_probe.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled

    async def test_owner_facts_query(self, worktree_handler):
        _, orch, _ = worktree_handler
        slot_ws, slot_dir = await _make_slot_for(orch)
        await _live_session(orch, "sess-1", task_id="tsk-1", work_dir=slot_dir)
        await orch.db.set_task_meta("tsk-1", CLAIMED_BY_SESSION_KEY, "sess-1")
        await orch.db.create_task(Task(id="tsk-zz", project_id="p1",
                                       title="tsk-zz", description=""))
        await _live_session(orch, "sess-other", task_id="tsk-zz",
                            work_dir="/elsewhere")

        facts = await orch.db.read_workspace_owner_facts([slot_ws])
        assert [t["id"] for t in facts["tasks"]] == ["tsk-1"]
        assert facts["tasks"][0]["claimed_by_session"] is not None
        assert [s["id"] for s in facts["sessions"]] == ["sess-1"]
        assert await orch.db.read_workspace_owner_facts([]) == {
            "tasks": [], "sessions": []}
