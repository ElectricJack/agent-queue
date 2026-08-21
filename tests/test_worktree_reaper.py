"""Worktree slot reaper + boot adoption + branch pruning (P4).

Worktree-execution implementation spec §6.2 (cascade), §6.4 (adoption),
§6.5 (pruning).  Real bare-origin + real slot manager fixtures — see
``test_worktree_prepare.py`` for the pattern.

Liveness guard (design §5) is exercised by monkey-patching the
process-table scanner rather than spawning real long-lived processes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.config import AppConfig
from src.models import (
    Agent,
    AgentProfile,
    KIND_MODE_WORKTREE,
    Project,
    RepoSourceType,
    SYSTEM_KIND_SCOPE,
    Task,
    Workspace,
    WorkspaceKind,
)
from src.orchestrator import Orchestrator
from src.orchestrator.worktree_manager import EXCLUDE_BEGIN
from src.runtimes.base import Runtime


def _git(args: list[str], cwd) -> str:
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
        raise AssertionError("no runtime should be created in these tests")


@pytest.fixture
def base_repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    base = tmp_path / "base"
    subprocess.run(
        ["git", "clone", str(origin), str(base)], check=True, capture_output=True
    )
    (base / "README.md").write_text("init\n")
    _git(["add", "-A"], cwd=base)
    _git(["commit", "-m", "init"], cwd=base)
    _git(["push", "origin", "main"], cwd=base)
    return base


async def _orch(tmp_path, *, worktrees_enabled=True):
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "aq.db"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    config.worktrees.enabled = worktrees_enabled
    o = Orchestrator(config, runtimes=_NullRuntimeFactory())
    await o.initialize()
    return o


async def _seed(o, base_repo, *, cap=2):
    await o.db.create_project(
        Project(id="p1", name="alpha", repo_url="",
                repo_default_branch="main", max_concurrent_agents=cap)
    )
    await o.db.upsert_workspace_kind(
        WorkspaceKind(
            project_id=SYSTEM_KIND_SCOPE, id="project-repo",
            is_git_repo=True, lockable=True, writable=True,
            mode=KIND_MODE_WORKTREE, default_lock_mode="exclusive",
        )
    )
    await o.db.create_workspace(
        Workspace(
            id="ws-base", project_id="p1", workspace_path=str(base_repo),
            source_type=RepoSourceType.CLONE, kind_id="project-repo",
        )
    )
    await o.db.upsert_profile(
        AgentProfile(id="p", name="p", model="claude-haiku-4-5-20251001",
                     permission_mode="bypassPermissions")
    )


async def _make_slot(o, base_repo, task_id="tsk-1"):
    await o.db.create_task(Task(id=task_id, project_id="p1",
                                title=task_id, description=""))
    await o.db.create_agent(Agent(id=task_id + "-a", name=task_id + "-a",
                                  profile_id="p"))
    t = await o.db.get_task(task_id)
    a = await o.db.get_agent(task_id + "-a")
    slot_path = await o._prepare_workspace(t, a)
    return await o.db.get_workspace_for_task(task_id), Path(slot_path)


# ── reap_slot ────────────────────────────────────────────────────────────


class TestReapSlot:
    async def test_reap_removes_retired_slot_dir_and_row_emits_event(
        self, tmp_path, base_repo, monkeypatch,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)

            # No task holding the slot — release it and mark it retired.
            await o.db.release_workspace(slot_ws.id)

            # No live processes.
            async def fake_scan(marker_key):
                return []

            monkeypatch.setattr(
                "src.sessions.proctable.scan_by_env_marker", fake_scan
            )

            events: list[tuple[str, dict]] = []
            orig_emit = o.bus.emit

            async def rec(t, p):
                events.append((t, p))
                return await orig_emit(t, p)

            o.bus.emit = rec

            mgr = o._worktree_slots()
            ok = await mgr.reap_slot(slot_ws, reason="retired-test")
            assert ok is True

            assert not slot_dir.exists() or not any(slot_dir.iterdir())
            assert await o.db.get_workspace(slot_ws.id) is None

            types = [t for t, _ in events]
            assert "worktree.reaped" in types
        finally:
            await o.shutdown()

    async def test_reap_blocked_by_live_process(
        self, tmp_path, base_repo, monkeypatch,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)
            await o.db.release_workspace(slot_ws.id)

            # Simulate a live process carrying the task's AQ_TASK_ID.
            from src.sessions.proctable import ProcEntry

            async def fake_scan(marker_key):
                return [ProcEntry(pid=99999, ppid=1, comm="agent",
                                  start_ticks=1, marker="tsk-1")]

            monkeypatch.setattr(
                "src.sessions.proctable.scan_by_env_marker", fake_scan
            )

            mgr = o._worktree_slots()
            ok = await mgr.reap_slot(slot_ws, reason="retired-test")
            assert ok is False
            assert slot_dir.is_dir()
            assert await o.db.get_workspace(slot_ws.id) is not None
        finally:
            await o.shutdown()

    async def test_reap_skips_on_proctable_scan_failure(
        self, tmp_path, base_repo, monkeypatch,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)
            await o.db.release_workspace(slot_ws.id)

            async def broken_scan(marker_key):
                raise OSError("/proc unavailable")

            monkeypatch.setattr(
                "src.sessions.proctable.scan_by_env_marker", broken_scan
            )

            mgr = o._worktree_slots()
            ok = await mgr.reap_slot(slot_ws, reason="retired-test")
            assert ok is False, "unknown liveness → never reap"
            assert slot_dir.is_dir()
        finally:
            await o.shutdown()


# ── prune_branches ──────────────────────────────────────────────────────


class TestPruneBranchesRetainFailedDays:
    """Finding #4: prune unmerged aq/* branches whose task is terminal-FAILED
    and whose last commit is older than ``retain_failed_days``."""

    async def test_failed_old_pruned_recent_and_active_kept(
        self, tmp_path, base_repo,
    ):
        from src.models import TaskStatus

        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            # Shrink retention window to 1 day for the test.
            o._worktree_slots().config.retain_failed_days = 1

            # Failed + old: task exists and is FAILED; commit is dated
            # well before the retention cutoff.
            _git(["switch", "-c", "aq/failed-old"], cwd=base_repo)
            (base_repo / "fo.txt").write_text("fo\n")
            _git(["add", "-A"], cwd=base_repo)
            past = "2000-01-01T00:00:00"
            subprocess.run(
                ["git",
                 "-c", "user.name=T", "-c", "user.email=t@t.com",
                 "commit", "-m", "fo",
                 "--date", past],
                cwd=str(base_repo),
                check=True, capture_output=True,
                env={**__import__("os").environ,
                     "GIT_COMMITTER_DATE": past,
                     "GIT_AUTHOR_DATE": past},
            )
            _git(["switch", "main"], cwd=base_repo)
            await o.db.create_task(
                Task(id="failed-old", project_id="p1",
                     title="fo", description="",
                     status=TaskStatus.FAILED)
            )

            # Failed + recent: FAILED task, but committed just now — keep.
            _git(["switch", "-c", "aq/failed-recent"], cwd=base_repo)
            (base_repo / "fr.txt").write_text("fr\n")
            _git(["add", "-A"], cwd=base_repo)
            _git(["commit", "-m", "fr"], cwd=base_repo)
            _git(["switch", "main"], cwd=base_repo)
            await o.db.create_task(
                Task(id="failed-recent", project_id="p1",
                     title="fr", description="",
                     status=TaskStatus.FAILED)
            )

            # Unmerged + active: no FAILED status — keep unconditionally.
            _git(["switch", "-c", "aq/active"], cwd=base_repo)
            (base_repo / "ac.txt").write_text("ac\n")
            _git(["add", "-A"], cwd=base_repo)
            subprocess.run(
                ["git",
                 "-c", "user.name=T", "-c", "user.email=t@t.com",
                 "commit", "-m", "ac", "--date", past],
                cwd=str(base_repo),
                check=True, capture_output=True,
                env={**__import__("os").environ,
                     "GIT_COMMITTER_DATE": past,
                     "GIT_AUTHOR_DATE": past},
            )
            _git(["switch", "main"], cwd=base_repo)
            # 'active' task is IN_PROGRESS (default) — keep the branch.
            await o.db.create_task(
                Task(id="active", project_id="p1",
                     title="ac", description="")
            )

            base_ws = await o.db.get_workspace("ws-base")
            mgr = o._worktree_slots()
            pruned = await mgr.prune_branches(base_ws, default_branch="main")

            branches = _git(["branch", "--list"], cwd=base_repo)
            assert "aq/failed-old" not in branches
            assert "aq/failed-recent" in branches
            assert "aq/active" in branches
            assert "aq/failed-old" in pruned
        finally:
            await o.shutdown()


class TestPruneBranches:
    async def test_prunes_merged_aq_branches_keeps_unmerged(
        self, tmp_path, base_repo,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)

            # Create a merged aq/ branch and an unmerged one.
            _git(["switch", "-c", "aq/merged"], cwd=base_repo)
            (base_repo / "m.txt").write_text("m\n")
            _git(["add", "-A"], cwd=base_repo)
            _git(["commit", "-m", "m"], cwd=base_repo)
            _git(["switch", "main"], cwd=base_repo)
            _git(["merge", "--no-ff", "aq/merged"], cwd=base_repo)

            _git(["switch", "-c", "aq/unmerged"], cwd=base_repo)
            (base_repo / "u.txt").write_text("u\n")
            _git(["add", "-A"], cwd=base_repo)
            _git(["commit", "-m", "u"], cwd=base_repo)
            _git(["switch", "main"], cwd=base_repo)

            base_ws = await o.db.get_workspace("ws-base")
            mgr = o._worktree_slots()
            pruned = await mgr.prune_branches(base_ws, default_branch="main")

            branches = _git(["branch", "--list"], cwd=base_repo)
            assert "aq/merged" not in branches
            assert "aq/unmerged" in branches
            assert "aq/merged" in pruned
        finally:
            await o.shutdown()


# ── adopt_existing ──────────────────────────────────────────────────────


class TestAdoptExisting:
    async def test_repairs_missing_exclude_block(self, tmp_path, base_repo):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            # Nuke the exclude file to simulate a repo that never had our block.
            exclude = base_repo / ".git" / "info" / "exclude"
            if exclude.exists():
                exclude.unlink()

            project = (await o.db.list_projects())[0]
            mgr = o._worktree_slots()
            report = await mgr.adopt_existing(project)

            # The base workspace should be in the repaired list.
            assert "ws-base" in report.repaired
            # And the block should now be in the exclude file.
            assert exclude.exists()
            content = exclude.read_text()
            assert EXCLUDE_BEGIN.split(" ")[0] in content or "agent-queue" in content
        finally:
            await o.shutdown()

    async def test_adopts_registered_slot_with_matching_sentinel(
        self, tmp_path, base_repo,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)
            await o.db.release_workspace(slot_ws.id)

            project = (await o.db.list_projects())[0]
            mgr = o._worktree_slots()
            report = await mgr.adopt_existing(project)

            # An existing, well-formed slot should show up as adopted.
            assert slot_ws.id in report.adopted
        finally:
            await o.shutdown()

    async def test_prunes_stale_worktree_registration(
        self, tmp_path, base_repo,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)

            # Delete the directory without telling git — creates a stale
            # entry in .git/worktrees.
            import shutil
            shutil.rmtree(slot_dir)

            project = (await o.db.list_projects())[0]
            mgr = o._worktree_slots()
            report = await mgr.adopt_existing(project)

            # Stale registration is pruned by `git worktree prune`.
            lines = await o.git.aworktree_list(str(base_repo))
            paths = [line["path"] for line in lines]
            assert str(slot_dir) not in paths
            # report.pruned must name the path that was cleaned up.
            assert isinstance(report.pruned, list)
            assert any(str(slot_dir) in p for p in report.pruned), (
                f"Expected {slot_dir} in report.pruned, got: {report.pruned}"
            )
        finally:
            await o.shutdown()


# ── recovery no longer deletes in-progress slots ────────────────────────


class TestRecoveryPreservesInProgressSlots:
    async def test_in_progress_slot_survives_recover_stale_state(
        self, tmp_path, base_repo,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)
            # Mark task IN_PROGRESS to model a mid-run restart.
            from src.models import TaskStatus

            await o.db.update_task("tsk-1", status=TaskStatus.IN_PROGRESS.value)

            # Recovery should NOT delete the slot directory or its branch.
            await o._recover_stale_state()

            assert slot_dir.is_dir()
            assert "aq/tsk-1" in _git(["branch", "--list"], cwd=base_repo)
            # Row survives.
            assert await o.db.get_workspace(slot_ws.id) is not None
        finally:
            await o.shutdown()
