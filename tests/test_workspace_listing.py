"""Bounded list_workspaces diagnostics (src/workspace_listing.py).

Spec: docs/specs/command-handler.md (list_workspaces). Pure units and real
Git; the handler-level regressions live in tests/test_workspace_commands.py.
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from src.git.manager import GitError, GitManager, _parse_status_branch_header
from src.models import RepoSourceType, Workspace
from src.workspace_listing import (
    DEFAULT_DEADLINE_SECONDS,
    MAX_DEADLINE_SECONDS,
    MIN_DEADLINE_SECONDS,
    beyond_cap,
    classify_owner,
    diagnose_workspaces,
    probe_checkouts,
    read_owner_facts,
    resolve_deadline,
)


def _ws(id="ws-1", *, path="/nonexistent/ws", project="p1", agent=None, task=None,
        slot_index=None, base=None) -> Workspace:
    return Workspace(
        id=id, project_id=project, workspace_path=path,
        source_type=RepoSourceType.CLONE, kind_id="project-repo",
        locked_by_agent_id=agent, locked_by_task_id=task,
        locked_at=1.0 if (agent or task) else None,
        slot_index=slot_index, base_workspace_id=base,
    )


def _facts(*, tasks=None, sessions=None, caps=None, status="complete") -> dict:
    return {"status": status, "error": None, "tasks": tasks or {},
            "sessions": sessions or [], "caps": caps or {}}


def _session(id="s-1", *, task="t-1", agent="a-1", work_dir="/nonexistent/ws",
             epoch=3, phase="active", project="p1") -> dict:
    return {"id": id, "project_id": project, "agent_id": agent, "task_id": task,
            "lifecycle": "pool", "state": "running", "claim_phase": phase,
            "last_claim_epoch": epoch, "last_activity": 10.0, "work_dir": work_dir}


def _task(id="t-1", *, status="IN_PROGRESS", epoch=3, claimed_by='"s-1"') -> dict:
    return {"id": id, "status": status, "claim_epoch": epoch,
            "assigned_agent_id": "a-1", "claimed_by_session": claimed_by}


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(["init", "--initial-branch=main"], path)
    _git(["config", "user.email", "t@example.com"], path)
    _git(["config", "user.name", "t"], path)
    (path / "README.md").write_text("x\n")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "init"], path)
    return path


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------


class TestResolveDeadline:
    def test_default_and_clamp(self):
        assert resolve_deadline(None) == DEFAULT_DEADLINE_SECONDS
        assert resolve_deadline(0) == MIN_DEADLINE_SECONDS
        assert resolve_deadline(1000) == MAX_DEADLINE_SECONDS
        assert resolve_deadline("3") == 3.0

    @pytest.mark.parametrize("bad", [True, "soon", float("nan"), float("inf"), [1]])
    def test_rejects_non_numbers(self, bad):
        with pytest.raises((TypeError, ValueError)):
            resolve_deadline(bad)


# ---------------------------------------------------------------------------
# Lock-free status probe
# ---------------------------------------------------------------------------


class TestStatusSummary:
    @pytest.mark.parametrize(
        ("header", "branch"),
        [
            ("main...origin/main [ahead 1]", "main"),
            ("aq/tsk-1", "aq/tsk-1"),
            ("No commits yet on main", "main"),
            ("Initial commit on main", "main"),
            ("HEAD (no branch)", "HEAD"),
        ],
    )
    def test_branch_header(self, header, branch):
        assert _parse_status_branch_header(header) == branch

    async def test_clean_dirty_detached_and_not_a_repo(self, repo, tmp_path):
        git = GitManager()
        assert await git.aread_status_summary(str(repo)) == ("main", False)

        (repo / "new.txt").write_text("y\n")
        assert await git.aread_status_summary(str(repo)) == ("main", True)

        (repo / "new.txt").unlink()
        _git(["checkout", "--detach"], repo)
        assert await git.aread_status_summary(str(repo)) == ("HEAD", False)

        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(GitError, match="not a git repository"):
            await git.aread_status_summary(str(plain))

    async def test_probe_takes_no_index_lock(self, repo):
        """A writer holding ``index.lock`` neither blocks nor loses its lock."""
        (repo / ".git" / "index.lock").write_text("")
        (repo / "README.md").write_text("changed\n")
        assert await GitManager().aread_status_summary(str(repo)) == ("main", True)
        assert (repo / ".git" / "index.lock").exists()


# ---------------------------------------------------------------------------
# Owner classification
# ---------------------------------------------------------------------------


class TestClassifyOwner:
    def test_held_with_consistent_fence(self):
        owner = classify_owner(
            _ws(agent="a-1", task="t-1"),
            _facts(tasks={"t-1": _task()}, sessions=[_session()]),
        )
        assert owner["classification"] == "held"
        assert owner["fence"] == "consistent"
        assert owner["session_matches_claim"] is True
        assert owner["task"] == {"id": "t-1", "found": True, "status": "IN_PROGRESS",
                                 "claim_epoch": 3, "claimed_by_session": "s-1"}
        assert owner["session"]["id"] == "s-1"

    def test_preparing_writer(self):
        owner = classify_owner(
            _ws(agent="a-1", task="t-1"),
            _facts(tasks={"t-1": _task()}, sessions=[_session(phase="preparing")]),
        )
        assert owner["classification"] == "preparing"

    @pytest.mark.parametrize(
        ("tasks", "sessions", "fence"),
        [
            ({}, [_session()], None),  # lock names a task that is gone
            ({"t-1": _task()}, [], None),  # no live session
            ({"t-1": _task(epoch=4)}, [_session()], "mismatch"),  # superseded claim
            ({"t-1": _task()}, [_session(task="t-9")], "unknown"),  # session moved on
        ],
    )
    def test_unverified_never_claims_stale(self, tasks, sessions, fence):
        owner = classify_owner(
            _ws(agent="a-1", task="t-1"), _facts(tasks=tasks, sessions=sessions)
        )
        assert owner["classification"] == "unverified"
        assert owner["fence"] == fence
        # The lock itself is reported verbatim.
        assert (owner["lock_state"], owner["agent_id"], owner["task_id"]) == (
            "task", "a-1", "t-1")

    @pytest.mark.parametrize("status", ["timeout", "unavailable"])
    def test_unknown_when_facts_missing(self, status):
        owner = classify_owner(_ws(agent="a-1", task="t-1"), _facts(status=status))
        assert owner["classification"] == "unknown"
        assert owner["facts"] == status
        assert owner["task_id"] == "t-1"
        assert owner["task"] is None and owner["session"] is None

    def test_agent_only_lock(self):
        ws = _ws(agent="a-1")
        assert classify_owner(ws, _facts())["classification"] == "unverified"
        held = classify_owner(ws, _facts(sessions=[_session(task=None)]))
        assert held["classification"] == "agent_held"
        assert held["lock_state"] == "agent_only"

    def test_unlocked(self):
        assert classify_owner(_ws(), _facts())["classification"] == "unlocked"
        owner = classify_owner(_ws(), _facts(sessions=[_session(task=None, agent=None)]))
        assert owner["classification"] == "unlocked_session_present"

    def test_best_match_and_foreign_project(self):
        ws = _ws(agent="a-1", task="t-1")
        sessions = [
            _session("s-dir", task="t-x", agent="a-x"),  # directory only
            _session("s-foreign", work_dir="/elsewhere", project="p2"),  # other project
            _session("s-both"),  # directory and lock
        ]
        owner = classify_owner(ws, _facts(tasks={"t-1": _task(claimed_by=None)},
                                          sessions=sessions))
        assert owner["session"]["id"] == "s-both"
        assert owner["sessions_matched"] == 2
        assert owner["session_matches_claim"] is None

    def test_beyond_cap(self):
        facts = _facts(caps={"p1": 2})
        assert beyond_cap(_ws(slot_index=1, base="ws-base"), facts) is False
        assert beyond_cap(_ws(slot_index=2, base="ws-base"), facts) is True
        assert beyond_cap(_ws(), facts) is None
        assert beyond_cap(_ws(slot_index=9, base="ws-base"), _facts()) is None


# ---------------------------------------------------------------------------
# Owner facts bound
# ---------------------------------------------------------------------------


class _FactsDB:
    def __init__(self, *, delay=0.0, fail=None, cap=2):
        self.delay, self.fail, self.cap = delay, fail, cap

    async def read_workspace_owner_facts(self, rows):
        await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return {"tasks": [_task()], "sessions": [_session()]}

    async def get_project(self, project_id):
        return type("P", (), {"max_concurrent_agents": self.cap})()


class TestReadOwnerFacts:
    async def test_complete(self):
        facts = await read_owner_facts(
            _FactsDB(), [_ws(slot_index=0, base="b")], timeout=1.0)
        assert facts["status"] == "complete"
        assert set(facts["tasks"]) == {"t-1"}
        assert facts["caps"] == {"p1": 2}

    async def test_timeout_is_bounded(self):
        started = time.monotonic()
        facts = await read_owner_facts(_FactsDB(delay=30), [_ws()], timeout=0.2)
        assert time.monotonic() - started < 5
        assert facts["status"] == "timeout"
        assert facts["tasks"] == {} and facts["sessions"] == []

    async def test_failure_is_unavailable(self):
        facts = await read_owner_facts(
            _FactsDB(fail=RuntimeError("pool exhausted")), [_ws()], timeout=1.0)
        assert facts["status"] == "unavailable"
        assert facts["error"] == "pool exhausted"


# ---------------------------------------------------------------------------
# Checkout probes
# ---------------------------------------------------------------------------


class _FakeGit:
    """Per-path scripted ``aread_status_summary``."""

    def __init__(self, script: dict):
        self.script = script
        self.calls: list[str] = []
        self.cancelled: list[str] = []

    async def aread_status_summary(self, path, *, timeout=None):
        self.calls.append(path)
        action = self.script[path]
        if action == "hang":
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled.append(path)
                raise
        if isinstance(action, BaseException):
            raise action
        return action


class TestProbeCheckouts:
    async def test_mixed_rows(self, tmp_path):
        dirs = {name: tmp_path / name for name in ("ok", "dirty", "plain", "err", "gone")}
        for name, path in dirs.items():
            if name != "gone":
                path.mkdir()
        git = _FakeGit({
            str(dirs["ok"]): ("aq/t-1", False),
            str(dirs["dirty"]): ("main", True),
            str(dirs["plain"]): GitError("fatal: not a git repository"),
            str(dirs["err"]): GitError("git status failed: corrupt index"),
        })
        targets = [(name, str(path), None) for name, path in dirs.items()]
        targets.append(("skip", str(dirs["ok"]), "skipped"))
        results = await probe_checkouts(git, targets, deadline=5.0)

        assert results["ok"] == {"git_status": "ok", "presence": "present",
                                 "branch": "aq/t-1", "dirty": False, "error": None}
        assert results["dirty"]["dirty"] is True
        assert results["plain"]["git_status"] == "not_a_checkout"
        assert results["err"]["git_status"] == "error"
        assert "corrupt index" in results["err"]["error"]
        assert results["err"]["branch"] is None and results["err"]["dirty"] is None
        assert results["gone"] == {"git_status": "missing", "presence": "missing",
                                   "branch": None, "dirty": None, "error": None}
        assert results["skip"]["git_status"] == "skipped"
        assert results["skip"]["presence"] == "present"
        assert str(dirs["gone"]) not in git.calls

    async def test_stuck_probe_times_out_and_queue_reports_not_started(self, tmp_path):
        hung, waiting = tmp_path / "hung", tmp_path / "waiting"
        hung.mkdir()
        waiting.mkdir()
        git = _FakeGit({str(hung): "hang", str(waiting): ("main", False)})
        started = time.monotonic()
        results = await probe_checkouts(
            git, [("hung", str(hung), None), ("waiting", str(waiting), None)],
            deadline=0.3, concurrency=1,
        )
        assert time.monotonic() - started < 0.3 + 5.0
        assert results["hung"]["git_status"] == "timeout"
        assert results["hung"]["branch"] is None and results["hung"]["dirty"] is None
        assert results["waiting"]["git_status"] == "not_started"
        assert results["waiting"]["presence"] == "unknown"
        assert git.cancelled == [str(hung)]

    async def test_caller_cancellation_cancels_probes(self, tmp_path):
        hung = tmp_path / "hung"
        hung.mkdir()
        git = _FakeGit({str(hung): "hang"})
        task = asyncio.create_task(
            probe_checkouts(git, [("hung", str(hung), None)], deadline=60.0))
        while not git.calls:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert git.cancelled == [str(hung)]


def _fake_git_bin(tmp_path: Path) -> tuple[Path, Path]:
    """A ``git`` that records its pid and blocks, standing in for a wedged probe."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pidfile = tmp_path / "git.pid"
    script = bindir / "git"
    script.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nexec sleep 30\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bindir, pidfile


def _assert_reaped(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


class TestRealGitChildIsKilled:
    @pytest.fixture
    def stuck_git(self, tmp_path):
        bindir, pidfile = _fake_git_bin(tmp_path)
        git = GitManager()
        git._SUBPROCESS_ENV = {
            **GitManager._SUBPROCESS_ENV,
            "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        return git, pidfile, checkout

    async def test_deadline_kills_child(self, stuck_git):
        git, pidfile, checkout = stuck_git
        started = time.monotonic()
        results = await probe_checkouts(git, [("w", str(checkout), None)], deadline=0.5)
        assert time.monotonic() - started < 0.5 + 5.0
        assert results["w"]["git_status"] == "timeout"
        _assert_reaped(int(pidfile.read_text()))

    async def test_cancellation_kills_child(self, stuck_git):
        git, pidfile, checkout = stuck_git
        task = asyncio.create_task(
            probe_checkouts(git, [("w", str(checkout), None)], deadline=60.0))
        for _ in range(500):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        _assert_reaped(int(pidfile.read_text()))


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


class TestDiagnoseWorkspaces:
    async def test_partial_response_is_structured(self, tmp_path):
        ok_dir, hung_dir = tmp_path / "ok", tmp_path / "hung"
        ok_dir.mkdir()
        hung_dir.mkdir()
        rows = [
            _ws("ok", path=str(ok_dir), slot_index=0, base="b"),
            _ws("hung", path=str(hung_dir), slot_index=1, base="b",
                agent="a-1", task="t-1"),
            _ws("old", path=str(tmp_path / "old"), slot_index=7, base="b"),
        ]
        git = _FakeGit({str(ok_dir): ("aq/t-0", False), str(hung_dir): "hang"})
        started = time.monotonic()
        per_row, diag = await diagnose_workspaces(
            _FactsDB(), git, rows, probe_git=True, deadline=0.6)
        assert time.monotonic() - started < 0.6 + 5.0

        assert diag["complete"] is False
        assert diag["owner_facts"] == "complete"
        assert diag["incomplete_rows"] == 1
        assert diag["git_status_counts"] == {"missing": 1, "ok": 1, "timeout": 1}
        assert "not evidence" in diag["authority"]
        assert per_row["ok"]["branch"] == "aq/t-0"
        assert per_row["hung"]["git_status"] == "timeout"
        assert per_row["hung"]["owner"]["task_id"] == "t-1"
        assert per_row["old"] | {"owner": None} == {
            "owner": None, "presence": "missing", "git_status": "missing",
            "branch": None, "dirty": None, "git_error": None, "beyond_cap": True}
        assert per_row["ok"]["beyond_cap"] is False

    async def test_preparing_writer_is_not_probed(self, tmp_path):
        slot = tmp_path / "slot"
        slot.mkdir()
        db = _FactsDB()

        async def _facts(rows):
            return {"tasks": [_task()],
                    "sessions": [_session(work_dir=str(slot), phase="preparing")]}

        db.read_workspace_owner_facts = _facts
        git = _FakeGit({})
        per_row, diag = await diagnose_workspaces(
            db, git, [_ws("w", path=str(slot), agent="a-1", task="t-1")],
            probe_git=True, deadline=2.0)
        assert per_row["w"]["owner"]["classification"] == "preparing"
        assert per_row["w"]["git_status"] == "skipped_active_writer"
        assert git.calls == []
        assert diag["complete"] is True

    async def test_owner_facts_failure_marks_incomplete(self, tmp_path):
        per_row, diag = await diagnose_workspaces(
            _FactsDB(fail=RuntimeError("boom")), None,
            [_ws("w", agent="a-1", task="t-1", slot_index=0, base="b")],
            probe_git=True, deadline=1.0)
        assert diag["complete"] is False
        assert diag["owner_facts"] == "unavailable"
        assert diag["git_requested"] is False
        owner = per_row["w"]["owner"]
        assert owner["classification"] == "unknown"
        assert (owner["agent_id"], owner["task_id"]) == ("a-1", "t-1")
        assert per_row["w"]["beyond_cap"] is None
