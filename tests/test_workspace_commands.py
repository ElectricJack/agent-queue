"""add_workspace kind_id parameter + list_workspace_kinds. Spec §10."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import DatabaseConfig, AppConfig
from src.models import (
    Agent,
    AgentProfile,
    KIND_MODE_WORKTREE,
    Project,
    Project as _Project,
    RepoSourceType,
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
        await orch.db.update_project("p1", max_concurrent_agents=0)

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


@pytest.mark.parametrize("drift", [None, "generation", "head", "claim"])
async def test_cutover_preserves_locked_source_and_rejects_stale_proof(setup, drift):
    handler, db, tmp_path = setup
    await db.create_profile(AgentProfile(id="worker", name="worker"))
    await db.create_agent(Agent(id="writer", name="writer", profile_id="worker"))
    await db.create_task(Task(id="working", project_id="p1", title="working", description=""))
    for wid, base, slot, enabled in (("old", None, None, True), ("new", None, None, False),
                                     ("old-slot", "old", 0, True)):
        path = tmp_path / wid
        path.mkdir()
        await db.create_workspace(_Workspace(
            id=wid, project_id="p1", workspace_path=str(path),
            source_type=RepoSourceType.WORKTREE if base else RepoSourceType.LINK,
            kind_id="project-repo", base_workspace_id=base, slot_index=slot, enabled=enabled,
            locked_by_agent_id="writer" if base else None,
            locked_by_task_id="working" if base else None,
        ))
    claim = tmp_path / "old-slot" / ".aq" / "claim.json"
    claim.parent.mkdir()
    claim.write_text('{"task_id":"working","claim_epoch":1}')
    git = handler.orchestrator.git
    git._arun = AsyncMock(side_effect=lambda args, cwd: cwd)
    git.aget_remote_url = AsyncMock(return_value="/origin.git")
    git.arev_parse = AsyncMock(return_value="a" * 40)
    git.aget_current_branch = AsyncMock(return_value="main")
    git.aworktree_base_path = AsyncMock(side_effect=lambda path: path)
    git.aget_dirty_paths = AsyncMock(return_value=[])
    args = {"project_id": "p1", "source_id": "old", "target_id": "new"}
    preview = await handler._cmd_workspace_cutover(args)
    assert preview["success"], preview
    assert not preview["applied"]
    if drift == "generation":
        await db.update_workspace("old-slot", generation=1)
    elif drift == "head":
        git.arev_parse.return_value = "b" * 40
    elif drift == "claim":
        claim.write_text('{"task_id":"working","claim_epoch":2}')
    result = await handler._cmd_workspace_cutover({**args, "apply": True,
                                                   "expected_proof": preview["proof"]})
    assert result["success"] is (drift is None), result
    assert (await db.get_workspace("old")).enabled is (drift is not None)
    assert (await db.get_workspace("new")).enabled is (drift is None)
    slot = await db.get_workspace("old-slot")
    assert (slot.locked_by_agent_id, slot.locked_by_task_id) == ("writer", "working")
    assert slot.generation == int(drift == "generation")
    assert claim.exists()
    assert slot.workspace_path == str(tmp_path / "old-slot")


async def test_cutover_requires_disabled_independent_target(setup):
    handler, db, tmp_path = setup
    for wid in ("old", "new"):
        await db.create_workspace(_Workspace(id=wid, project_id="p1", workspace_path=str(tmp_path / wid),
                                            source_type=RepoSourceType.LINK, kind_id="project-repo"))
    result = await handler._cmd_workspace_cutover({"project_id": "p1", "source_id": "old", "target_id": "new"})
    assert not result["success"]
    assert "disabled target" in result["error"]


async def test_add_disabled_target_and_refuse_linked_base_relocation(setup):
    handler, db, tmp_path = setup
    new = tmp_path / "new"
    new.mkdir()
    created = await handler._cmd_add_workspace({"project_id": "p1", "source": "link",
                                               "path": str(new), "enabled": False})
    base = await db.get_workspace(created["created"])
    assert not base.enabled
    await db.create_workspace(_Workspace(id="slot", project_id="p1", workspace_path=str(new / "slot"),
                                        source_type=RepoSourceType.WORKTREE, base_workspace_id=base.id,
                                        slot_index=0, kind_id="project-repo"))
    result = await handler._cmd_edit_workspace({"workspace_id": base.id, "workspace_path": str(tmp_path)})
    assert "cutover" in result["error"]
    assert (await db.get_workspace(base.id)).workspace_path == str(new)


@pytest.mark.parametrize("obstruction", ["origin", "subdirectory", "dirty", "confirmed_writer"])
async def test_cutover_refuses_unqualified_real_target(worktree_handler, tmp_path, obstruction):
    handler, orch, base = worktree_handler
    target = tmp_path / "target"
    subprocess.run(["git", "clone", str(base.parent / "origin.git"), str(target)],
                   check=True, capture_output=True)
    registered = target
    if obstruction == "origin":
        _git_cli(["remote", "set-url", "origin", str(tmp_path / "unrelated.git")], target)
    elif obstruction == "subdirectory":
        registered = target / "nested"
        registered.mkdir()
    elif obstruction == "dirty":
        (target / "untracked.txt").write_text("retained\n")
    await orch.db.create_workspace(_Workspace(
        id="ws-target", project_id="p1", workspace_path=str(registered),
        source_type=RepoSourceType.CLONE, kind_id="project-repo", enabled=False,
    ))
    if obstruction == "confirmed_writer":
        from sqlalchemy import insert
        from src.database.tables import integration_branch_owners

        async with orch.db.immediate() as conn:
            await conn.execute(insert(integration_branch_owners).values(
                id="retained-owner", repository_id="p1", ref="aq/integration",
                owner_id="owner", owner_role="integration", fence_token=1,
                handoff_state="handoff_pending", workspace_id="ws-base",
                confirmed_workspace_id="ws-target", created_at=1, updated_at=1,
            ))
    result = await handler._cmd_workspace_cutover({
        "project_id": "p1", "source_id": "ws-base", "target_id": "ws-target",
    })
    assert not result["success"], result
    assert (await orch.db.get_workspace("ws-base")).enabled
    assert not (await orch.db.get_workspace("ws-target")).enabled
    assert registered.exists()


async def test_real_cutover_preserves_writer_and_provisions_each_base_with_its_kind_limit(
    worktree_handler, tmp_path,
):
    handler, orch, base = worktree_handler
    await orch.db.update_project("p1", max_concurrent_agents=4)
    old_slot, old_path = await _make_slot_for(orch)
    (old_path / "unpublished.txt").write_text("writer still working\n")
    old_head = _git_cli(["rev-parse", "HEAD"], old_path)
    await orch.db.set_task_meta("tsk-1", "manual_pause_checkpoint", {"saved": True})

    args = {"project_id": "p1", "source_id": "ws-base", "target_id": "ws-fast"}
    for wid, name, kind, enabled in (
        ("ws-fast", "fast", "project-repo", False),
        ("ws-native", "native", "windows-native", True),
    ):
        path = tmp_path / name
        subprocess.run(["git", "clone", str(base.parent / "origin.git"), str(path)],
                       check=True, capture_output=True)
        await orch.db.create_workspace(_Workspace(
            id=wid, project_id="p1", workspace_path=str(path), kind_id=kind,
            source_type=RepoSourceType.CLONE, enabled=enabled,
        ))
    await orch.db.upsert_workspace_kind(_WorkspaceKind(
        project_id="p1", id="windows-native", is_git_repo=True, writable=True,
        lockable=True, mode=KIND_MODE_WORKTREE, max_slots=2,
    ))
    preview = await handler._cmd_workspace_cutover(args)
    assert preview["success"], preview
    result = await handler._cmd_workspace_cutover({**args, "apply": True,
                                                  "expected_proof": preview["proof"]})
    assert result["success"], result
    assert await orch.db.get_project_workspace_path("p1") == str(tmp_path / "fast")

    project = await orch.db.get_project("p1")
    for kind, limit in (("project-repo", 4), ("windows-native", 2)):
        # Growth is lazy, one slot per dispatch, and stops once its kind is full.
        for index in range(limit + 1):
            await orch._ensure_worktree_slots(project, kind)
            acquired = await orch.db.acquire_one_unlocked(
                "p1", kind, mode="exclusive", locked_by_task_id="tsk-1",
                locked_by_agent_id=old_slot.locked_by_agent_id,
                kind_mode=KIND_MODE_WORKTREE, worktree_slot_cap=4,
            )
            assert (acquired is not None) is (index < limit)
    fast_slots = await orch.db.list_slots_for_base("ws-fast")
    native_slots = await orch.db.list_slots_for_base("ws-native")
    assert len(fast_slots) == 4
    assert len(native_slots) == 2
    assert all(Path(s.workspace_path).is_relative_to(tmp_path / "fast") for s in fast_slots)
    assert all(Path(s.workspace_path).is_relative_to(tmp_path / "native") for s in native_slots)
    retained = await orch.db.get_workspace(old_slot.id)
    assert not retained.enabled
    assert retained.locked_by_task_id == "tsk-1"
    assert retained.locked_by_agent_id == old_slot.locked_by_agent_id
    assert retained.generation == old_slot.generation
    assert _git_cli(["rev-parse", "HEAD"], old_path) == old_head
    assert (old_path / "unpublished.txt").read_text() == "writer still working\n"
    assert await orch.db.get_task_meta("tsk-1", "manual_pause_checkpoint") == {"saved": True}
