"""Tests for the async API of GitManager.

Mirrors key tests from test_git_manager.py but exercises the async methods
(_arun, _arun_subprocess, and all a-prefixed public methods).
"""

import asyncio
import json
import pathlib
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.git.manager import GitManager, GitError


def _git(args: list[str], cwd: str) -> str:
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit_file(clone: str, filename: str, content: str, message: str) -> str:
    pathlib.Path(clone, filename).write_text(content)
    _git(["add", filename], cwd=clone)
    _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", message], cwd=clone)
    return _git(["rev-parse", "HEAD"], cwd=clone)


@pytest.fixture
def bare_repo(tmp_path):
    """Create a bare repo to act as 'origin'."""
    bare = str(tmp_path / "origin.git")
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", bare], check=True, capture_output=True
    )
    return bare


@pytest.fixture
def clone(tmp_path, bare_repo):
    """Clone the bare repo and add an initial commit."""
    clone_path = str(tmp_path / "clone")
    subprocess.run(["git", "clone", bare_repo, clone_path], check=True, capture_output=True)
    _git(["config", "user.name", "Test"], cwd=clone_path)
    _git(["config", "user.email", "t@t.com"], cwd=clone_path)
    pathlib.Path(clone_path, "README.md").write_text("init")
    _git(["add", "."], cwd=clone_path)
    _git(["commit", "-m", "init"], cwd=clone_path)
    _git(["push", "origin", "main"], cwd=clone_path)
    return clone_path


@pytest.fixture
def mgr():
    return GitManager()


# ------------------------------------------------------------------
# _arun basic tests
# ------------------------------------------------------------------


class TestArun:
    @pytest.mark.asyncio
    async def test_arun_returns_stdout(self, clone, mgr):
        result = await mgr._arun(["rev-parse", "--git-dir"], cwd=clone)
        assert result == ".git"

    @pytest.mark.asyncio
    async def test_arun_raises_on_failure(self, clone, mgr):
        with pytest.raises(GitError, match="failed"):
            await mgr._arun(["checkout", "nonexistent-branch"], cwd=clone)

    @pytest.mark.asyncio
    async def test_arun_timeout(self, clone, mgr, monkeypatch):
        """Timeout should raise GitError, not asyncio.TimeoutError."""
        import unittest.mock as mock

        async def _slow_communicate():
            await asyncio.sleep(10)
            return b"", b""

        original_create = asyncio.create_subprocess_exec

        async def mock_create(*args, **kwargs):
            proc = await original_create(*args, **kwargs)
            proc.communicate = _slow_communicate
            # Wrap kill() to tolerate the process already being gone
            original_kill = proc.kill

            def safe_kill():
                try:
                    original_kill()
                except ProcessLookupError:
                    pass

            proc.kill = safe_kill
            return proc

        with mock.patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            with pytest.raises(GitError, match="timed out"):
                await mgr._arun(["status"], cwd=clone, timeout=1)


# ------------------------------------------------------------------
# _arun_subprocess tests
# ------------------------------------------------------------------


class TestArunSubprocess:
    @pytest.mark.asyncio
    async def test_returns_completed_process(self, mgr):
        result = await mgr._arun_subprocess(["git", "--version"])
        assert result.returncode == 0
        assert "git version" in result.stdout

    @pytest.mark.asyncio
    async def test_nonzero_returncode(self, tmp_path, mgr):
        # Use a valid dir but invalid git command to get non-zero exit
        result = await mgr._arun_subprocess(
            ["git", "log", "--oneline", "-1"],
            cwd=str(tmp_path),
        )
        assert result.returncode != 0


# ------------------------------------------------------------------
# Async public methods
# ------------------------------------------------------------------


class TestAsyncValidateCheckout:
    @pytest.mark.asyncio
    async def test_valid(self, clone, mgr):
        assert await mgr.avalidate_checkout(clone) is True

    @pytest.mark.asyncio
    async def test_invalid(self, tmp_path, mgr):
        assert await mgr.avalidate_checkout(str(tmp_path / "nope")) is False


class TestAsyncGetCurrentBranch:
    @pytest.mark.asyncio
    async def test_returns_branch(self, clone, mgr):
        branch = await mgr.aget_current_branch(clone)
        assert branch == "main"


class TestAsyncFindOpenPr:
    @pytest.mark.asyncio
    async def test_returns_only_the_first_matching_pr_url(self, mgr, monkeypatch):
        """A duplicated head branch must not leak a newline-delimited URL list."""
        monkeypatch.setattr(
            mgr,
            "_arun_subprocess",
            AsyncMock(
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=(
                        "https://github.com/o/r/pull/41\n"
                        "https://github.com/o/r/pull/42\n"
                    ),
                )
            ),
        )

        assert await mgr.afind_open_pr("/repo", "feature-x") == "https://github.com/o/r/pull/41"
        jq_filter = mgr._arun_subprocess.await_args.args[0][
            mgr._arun_subprocess.await_args.args[0].index("--jq") + 1
        ]
        assert jq_filter.startswith("first(")

    @pytest.mark.asyncio
    async def test_accepts_merged_pr_and_excludes_closed_pr(self, mgr, monkeypatch):
        """Verification can use merged PRs but must reject closed-unmerged ones."""
        monkeypatch.setattr(
            mgr,
            "_arun_subprocess",
            AsyncMock(
                return_value=SimpleNamespace(
                    returncode=0, stdout="https://github.com/o/r/pull/42\n"
                )
            ),
        )

        assert await mgr.afind_open_pr("/repo", "feature-x") == "https://github.com/o/r/pull/42"
        args = mgr._arun_subprocess.await_args.args[0]
        assert args[args.index("--state") + 1] == "all"
        assert "MERGED" in args[args.index("--jq") + 1]
        assert "CLOSED" not in args[args.index("--jq") + 1]

    @pytest.mark.asyncio
    async def test_ancestor_recognizes_branch_at_default_tip(self, mgr, clone):
        await mgr.acreate_branch(clone, "feature-x")
        assert await mgr.ais_ancestor(clone, "feature-x", "main") is True

    @pytest.mark.asyncio
    async def test_count_ahead_is_zero_for_a_fresh_branch(self, mgr, clone):
        await mgr.acreate_branch(clone, "feature-x")
        assert await mgr.acount_commits_ahead(clone, "feature-x", "main") == 0

    @pytest.mark.asyncio
    async def test_count_ahead_counts_commits(self, mgr, clone):
        await mgr.acreate_branch(clone, "feature-x")
        for i in range(2):
            (pathlib.Path(clone) / f"f{i}.txt").write_text("x")
            await mgr._arun(["add", "-A"], cwd=clone)
            await mgr._arun(["commit", "-m", f"c{i}"], cwd=clone)
        assert await mgr.acount_commits_ahead(clone, "feature-x", "main") == 2

    @pytest.mark.asyncio
    async def test_count_ahead_returns_none_for_a_missing_ref(self, mgr, clone):
        """An unknown base is "unknown", never a silent zero."""
        assert await mgr.acount_commits_ahead(clone, "main", "origin/nope") is None

    @pytest.mark.asyncio
    async def test_branch_exists_distinguishes_absence_from_command_failure(
        self, mgr, clone, monkeypatch
    ):
        assert await mgr.abranch_exists(clone, "missing") is False
        monkeypatch.setattr(
            mgr,
            "_arun_subprocess",
            AsyncMock(return_value=SimpleNamespace(returncode=128, stdout="", stderr="broken")),
        )
        assert await mgr.abranch_exists(clone, "missing") is None


class TestAsyncGetStatus:
    @pytest.mark.asyncio
    async def test_returns_status(self, clone, mgr):
        status = await mgr.aget_status(clone)
        assert "nothing to commit" in status or "working tree clean" in status


class TestAsyncCreateBranch:
    @pytest.mark.asyncio
    async def test_creates_and_switches(self, clone, mgr):
        await mgr.acreate_branch(clone, "feature-x")
        branch = await mgr.aget_current_branch(clone)
        assert branch == "feature-x"

    @pytest.mark.asyncio
    async def test_existing_branch_switches(self, clone, mgr):
        await mgr.acreate_branch(clone, "feature-x")
        await mgr._arun(["checkout", "main"], cwd=clone)
        await mgr.acreate_branch(clone, "feature-x")
        branch = await mgr.aget_current_branch(clone)
        assert branch == "feature-x"


class TestAsyncCommitAll:
    @pytest.mark.asyncio
    async def test_clean_tree_does_not_invoke_hooks(self, clone, mgr):
        repo = pathlib.Path(clone)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho invoked > hook-ran\n")
        hook.chmod(0o755)

        assert await mgr.acommit_all(clone, "nothing") is False
        assert not (repo / "hook-ran").exists()

    @pytest.mark.asyncio
    async def test_native_hooks_run_once_and_cannot_commit_reserved_staging(self, clone, mgr):
        repo = pathlib.Path(clone)
        (repo / "work.txt").write_text("real work\n")
        reserved = repo / ".aq" / "claim.json"
        reserved.parent.mkdir()
        reserved.write_text("daemon state\n")
        pre_commit = repo / ".git" / "hooks" / "pre-commit"
        pre_commit.write_text(
            "#!/bin/sh\necho pre-commit >> hook.log\ngit add -f .aq/claim.json\n"
        )
        pre_commit.chmod(0o755)
        commit_msg = repo / ".git" / "hooks" / "commit-msg"
        commit_msg.write_text("#!/bin/sh\necho commit-msg >> hook.log\n")
        commit_msg.chmod(0o755)

        assert await mgr.acommit_all(clone, "task work", exclude_plans=False)

        assert (repo / "hook.log").read_text().splitlines() == ["pre-commit", "commit-msg"]
        assert _git(["show", "--pretty=", "--name-only", "HEAD"], cwd=clone) == "work.txt"
        assert _git(["diff", "--cached", "--name-only"], cwd=clone) == ""

    @pytest.mark.asyncio
    async def test_commit_msg_rejection_aborts_commit_after_invocation(self, clone, mgr):
        repo = pathlib.Path(clone)
        before = _git(["rev-parse", "HEAD"], cwd=clone)
        (repo / "work.txt").write_text("real work\n")
        commit_msg = repo / ".git" / "hooks" / "commit-msg"
        commit_msg.write_text("#!/bin/sh\necho rejected >> hook.log\nexit 19\n")
        commit_msg.chmod(0o755)

        with pytest.raises(GitError):
            await mgr.acommit_all(clone, "task work", exclude_plans=False)

        assert (repo / "hook.log").read_text().splitlines() == ["rejected"]
        assert _git(["rev-parse", "HEAD"], cwd=clone) == before

    @pytest.mark.asyncio
    async def test_failing_pre_commit_cleans_reserved_staging_only(self, clone, mgr):
        repo = pathlib.Path(clone)
        (repo / "work.txt").write_text("real work\n")
        reserved = repo / ".aq" / "claim.json"
        reserved.parent.mkdir()
        reserved.write_text("daemon state\n")
        pre_commit = repo / ".git" / "hooks" / "pre-commit"
        pre_commit.write_text("#!/bin/sh\ngit add -f .aq/claim.json\nexit 23\n")
        pre_commit.chmod(0o755)

        with pytest.raises(GitError):
            await mgr.acommit_all(clone, "task work", exclude_plans=False)

        assert _git(["diff", "--cached", "--name-only"], cwd=clone) == "work.txt"
        assert reserved.read_text() == "daemon state\n"

    @pytest.mark.asyncio
    async def test_no_verify_disables_every_commit_hook(self, clone, mgr):
        repo = pathlib.Path(clone)
        (repo / "work.txt").write_text("real work\n")
        for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit"):
            hook = repo / ".git" / "hooks" / name
            hook.write_text(f"#!/bin/sh\necho {name} >> hook.log\n")
            hook.chmod(0o755)

        assert await mgr.acommit_all(
            clone, "auto remediation", exclude_plans=False, no_verify=True
        )
        assert not (repo / "hook.log").exists()

    @pytest.mark.asyncio
    async def test_pre_commit_hook_cannot_stage_daemon_state(self, clone, mgr):
        """The async final commit cannot include state staged by a hook."""
        reserved = pathlib.Path(clone, ".aq/claim.json")
        reserved.parent.mkdir(parents=True)
        reserved.write_text("daemon state\n")
        pathlib.Path(clone, "work.txt").write_text("real work\n")
        hook = pathlib.Path(clone, ".git", "hooks", "pre-commit")
        hook.write_text("#!/bin/sh\ngit add -f .aq/claim.json\n")
        hook.chmod(0o755)

        assert await mgr.acommit_all(clone, "task work", exclude_plans=False)
        assert _git(["show", "--pretty=", "--name-only", "HEAD"], cwd=clone) == "work.txt"
        assert _git(["ls-files", "--others", "--exclude-standard"], cwd=clone) == ".aq/claim.json"

    @pytest.mark.asyncio
    async def test_commit_with_changes(self, clone, mgr):
        pathlib.Path(clone, "newfile.txt").write_text("hello")
        committed = await mgr.acommit_all(clone, "add newfile")
        assert committed is True

    @pytest.mark.asyncio
    async def test_no_changes(self, clone, mgr):
        committed = await mgr.acommit_all(clone, "nothing")
        assert committed is False

    @pytest.mark.asyncio
    async def test_auto_remediation_never_commits_reserved_daemon_paths(self, clone, mgr):
        """Reserved bookkeeping stays unstaged even when it is already tracked."""
        reserved_paths = (".aq/claim.json", ".aq-worktree.json", ".codex/hooks.json")
        for path in reserved_paths:
            target = pathlib.Path(clone, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("before\n")
        _git(["add", *reserved_paths], cwd=clone)
        _git(["commit", "-m", "tracked daemon paths"], cwd=clone)

        for path in reserved_paths:
            pathlib.Path(clone, path).write_text("after\n")
        _git(["add", *reserved_paths], cwd=clone)
        pathlib.Path(clone, "work.txt").write_text("real work\n")

        assert await mgr.acommit_all(clone, "auto remediation", exclude_plans=False)
        committed_paths = _git(["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"], cwd=clone)
        assert committed_paths.splitlines() == ["work.txt"]
        assert set(_git(["diff", "--name-only"], cwd=clone).splitlines()) == set(reserved_paths)

    @pytest.mark.asyncio
    async def test_omits_pre_staged_daemon_paths_in_unborn_repo(self, tmp_path, mgr):
        """Async commit-all cannot include bookkeeping in an initial commit."""
        repo = tmp_path / "unborn"
        _git(["init", "--initial-branch=main", str(repo)], cwd=str(tmp_path))
        _git(["config", "user.name", "Test"], cwd=str(repo))
        _git(["config", "user.email", "t@t.com"], cwd=str(repo))

        reserved_paths = (".aq/claim.json", ".aq-worktree.json", ".codex/hooks.json")
        for path in reserved_paths:
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("daemon state\n")
        _git(["add", *reserved_paths], cwd=str(repo))
        (repo / "work.txt").write_text("real work\n")

        assert await mgr.acommit_all(str(repo), "initial task work", exclude_plans=False)
        committed_paths = _git(["show", "--pretty=", "--name-only", "HEAD"], cwd=str(repo))
        assert committed_paths.splitlines() == ["work.txt"]
        assert set(_git(["ls-files", "--others", "--exclude-standard"], cwd=str(repo)).splitlines()) == set(
            reserved_paths
        )

    @pytest.mark.asyncio
    async def test_refuses_commit_when_reserved_path_remains_cached(self, clone, mgr, monkeypatch):
        """Async commit-all aborts if daemon state remains staged."""
        reserved_path = ".aq-worktree.json"
        pathlib.Path(clone, reserved_path).write_text("daemon state\n")
        _git(["add", reserved_path], cwd=clone)
        pathlib.Path(clone, "work.txt").write_text("real work\n")

        original_arun = mgr._arun

        async def leave_reserved_path_cached(args, **kwargs):
            if args[:3] == ["reset", "HEAD", "--"]:
                return ""
            return await original_arun(args, **kwargs)

        monkeypatch.setattr(mgr, "_arun", leave_reserved_path_cached)

        with pytest.raises(GitError, match="reserved daemon bookkeeping"):
            await mgr.acommit_all(clone, "task work", exclude_plans=False)

    @pytest.mark.asyncio
    async def test_propagates_reserved_path_unstage_failure(self, clone, mgr, monkeypatch):
        """Async commit-all does not suppress a real unstage failure."""
        pathlib.Path(clone, ".aq-worktree.json").write_text("daemon state\n")
        pathlib.Path(clone, "work.txt").write_text("real work\n")

        original_arun = mgr._arun

        async def fail_reserved_path_unstage(args, **kwargs):
            if args[:3] == ["reset", "HEAD", "--"]:
                raise GitError("synthetic reset failure")
            return await original_arun(args, **kwargs)

        monkeypatch.setattr(mgr, "_arun", fail_reserved_path_unstage)

        with pytest.raises(GitError, match="synthetic reset failure"):
            await mgr.acommit_all(clone, "task work", exclude_plans=False)

    @pytest.mark.asyncio
    async def test_emits_git_commit_event(self, clone, mgr):
        """Successful commit emits git.commit on the EventBus."""
        from src.event_bus import EventBus

        bus = EventBus()
        received: list[dict] = []
        bus.subscribe("git.commit", lambda data: received.append(data))

        pathlib.Path(clone, "event_test.txt").write_text("event content")
        committed = await mgr.acommit_all(
            clone,
            "feat: event test",
            event_bus=bus,
            project_id="proj-1",
            agent_id="agent-1",
        )
        assert committed is True
        assert len(received) == 1
        evt = received[0]
        assert evt["branch"] == "main"
        assert evt["message"] == "feat: event test"
        assert evt["project_id"] == "proj-1"
        assert evt["agent_id"] == "agent-1"
        assert "event_test.txt" in evt["changed_files"]
        # commit_hash should be a 40-char hex SHA
        assert len(evt["commit_hash"]) == 40

    @pytest.mark.asyncio
    async def test_no_event_when_nothing_to_commit(self, clone, mgr):
        """No event should be emitted when there are no changes."""
        from src.event_bus import EventBus

        bus = EventBus()
        received: list[dict] = []
        bus.subscribe("git.commit", lambda data: received.append(data))

        committed = await mgr.acommit_all(
            clone,
            "nothing here",
            event_bus=bus,
            project_id="proj-1",
        )
        assert committed is False
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_no_event_without_bus(self, clone, mgr):
        """Without an EventBus, commit succeeds silently (backward compat)."""
        pathlib.Path(clone, "no_bus.txt").write_text("no bus")
        committed = await mgr.acommit_all(clone, "no bus commit")
        assert committed is True
        # No exception means success — no bus to emit to

    @pytest.mark.asyncio
    async def test_event_emission_failure_does_not_break_commit(self, clone, mgr):
        """If event emission raises, the commit should still succeed."""
        from src.event_bus import EventBus

        bus = EventBus()

        async def bad_handler(data):
            raise RuntimeError("boom")

        bus.subscribe("git.commit", bad_handler)

        pathlib.Path(clone, "resilient.txt").write_text("resilient")
        committed = await mgr.acommit_all(
            clone,
            "resilient commit",
            event_bus=bus,
            project_id="proj-1",
        )
        assert committed is True

    @pytest.mark.asyncio
    async def test_event_multiple_files(self, clone, mgr):
        """Event changed_files should list all committed files."""
        from src.event_bus import EventBus

        bus = EventBus()
        received: list[dict] = []
        bus.subscribe("git.commit", lambda data: received.append(data))

        pathlib.Path(clone, "a.txt").write_text("a")
        pathlib.Path(clone, "b.txt").write_text("b")
        committed = await mgr.acommit_all(
            clone,
            "add two files",
            event_bus=bus,
        )
        assert committed is True
        assert len(received) == 1
        assert set(received[0]["changed_files"]) == {"a.txt", "b.txt"}


class TestAsyncReservedDeliveryDiff:
    @pytest.mark.asyncio
    async def test_reports_forced_add_and_changed_previously_tracked_paths(self, clone, mgr):
        repo = pathlib.Path(clone)
        tracked = repo / ".codex" / "settings.json"
        tracked.parent.mkdir()
        tracked.write_text('{"base": true}\n')
        _git(["add", ".codex/settings.json"], cwd=clone)
        _git(["commit", "-m", "track project codex settings"], cwd=clone)
        _git(["switch", "-c", "task/reserved"], cwd=clone)
        tracked.write_text('{"task": true}\n')
        forced = repo / ".aq" / "claim.json"
        forced.parent.mkdir()
        forced.write_text("daemon state\n")
        _git(["add", "-f", ".aq/claim.json", ".codex/settings.json"], cwd=clone)
        _git(["commit", "-m", "bad delivery"], cwd=clone)

        assert await mgr.areserved_paths_in_diff(clone, "main", "task/reserved") == [
            ".aq/claim.json",
            ".codex/settings.json",
        ]

    @pytest.mark.asyncio
    async def test_ignores_reserved_path_tracked_but_unchanged_from_base(self, clone, mgr):
        repo = pathlib.Path(clone)
        tracked = repo / ".codex" / "settings.json"
        tracked.parent.mkdir()
        tracked.write_text('{"base": true}\n')
        _git(["add", ".codex/settings.json"], cwd=clone)
        _git(["commit", "-m", "track project codex settings"], cwd=clone)
        _git(["switch", "-c", "task/legitimate"], cwd=clone)
        (repo / "work.txt").write_text("real work\n")
        _git(["add", "work.txt"], cwd=clone)
        _git(["commit", "-m", "task work"], cwd=clone)

        assert await mgr.areserved_paths_in_diff(clone, "main", "task/legitimate") == []


@pytest.mark.asyncio
async def test_validated_push_uses_the_resolved_oid_not_a_mutable_local_ref(mgr, monkeypatch):
    """A post-merge hook cannot substitute content between validation and push."""
    pushed: list[list[str]] = []
    tip = "d" * 40

    async def fake_arun(args, cwd=None, **_kwargs):
        if args[:2] == ["rev-parse", "--verify"]:
            return tip
        pushed.append(args)
        return ""

    monkeypatch.setattr(mgr, "_arun", fake_arun)

    await mgr.apush_validated_ref("/repo", "HEAD", "main")

    assert pushed == [["push", "origin", f"{tip}:refs/heads/main"]]


@pytest.mark.asyncio
async def test_delivery_push_checks_and_pushes_one_immutable_tip_despite_head_mutation(
    mgr, monkeypatch
):
    """A hook moving HEAD after diff inspection cannot replace the delivered commit."""
    clean_tip = "d" * 40
    unsafe_tip = "e" * 40
    pushed: list[list[str]] = []
    head_reads = 0

    async def fake_arun(args, cwd=None, **_kwargs):
        nonlocal head_reads
        if args[:2] == ["rev-parse", "--verify"]:
            if args[-1] == "HEAD":
                head_reads += 1
                return clean_tip if head_reads == 1 else unsafe_tip
            return clean_tip
        pushed.append(args)
        return ""

    monkeypatch.setattr(mgr, "_arun", fake_arun)
    inspect = AsyncMock(return_value=[])
    monkeypatch.setattr(mgr, "areserved_paths_in_diff", inspect)

    await mgr.apush_validated_delivery("/repo", "origin/main", "HEAD", "main")

    inspect.assert_awaited_once_with("/repo", "origin/main", clean_tip)
    assert pushed == [["push", "origin", f"{clean_tip}:refs/heads/main"]]


_RENAMEABLE_CONTENT = "".join(f"line {i}\n" for i in range(30))


@pytest.mark.parametrize(
    ("reserved_path", "change"),
    [
        (".aq/claim.json", "add"),
        (".aq-worktree.json", "modify"),
        (".codex/settings.json", "delete"),
        (".aq/claim.json", "rename-out"),
        (".codex/settings.json", "rename-in"),
    ],
)
@pytest.mark.asyncio
async def test_delivery_push_rejects_every_reserved_path_change_kind(
    clone, mgr, reserved_path, change
):
    """Task delivery rejects added, modified, deleted, and renamed daemon-owned paths.

    ``rename-out`` moves a tracked reserved file to an ordinary path (a
    deletion of daemon state that git's rename detection would otherwise
    collapse into the destination); ``rename-in`` moves an ordinary tracked
    file onto a reserved path.
    """
    repo = pathlib.Path(clone)
    path = repo / reserved_path
    plain = repo / "plain.json"
    if change in {"modify", "delete", "rename-out"}:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_RENAMEABLE_CONTENT)
        _git(["add", "-f", reserved_path], cwd=clone)
        _git(["commit", "-m", f"track {reserved_path}"], cwd=clone)
    elif change == "rename-in":
        plain.write_text(_RENAMEABLE_CONTENT)
        _git(["add", "plain.json"], cwd=clone)
        _git(["commit", "-m", "track plain.json"], cwd=clone)

    _git(["switch", "-c", f"task/reserved-{change}"], cwd=clone)
    if change == "delete":
        path.unlink()
        _git(["add", "-u", reserved_path], cwd=clone)
    elif change == "rename-out":
        _git(["mv", reserved_path, "moved.json"], cwd=clone)
    elif change == "rename-in":
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(["mv", "plain.json", reserved_path], cwd=clone)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("task\n")
        _git(["add", "-f", reserved_path], cwd=clone)
    _git(["commit", "-m", f"{change} reserved path"], cwd=clone)

    with pytest.raises(GitError, match=f"reserved delivery paths: {reserved_path}"):
        await mgr.apush_validated_delivery(
            clone,
            "main",
            f"task/reserved-{change}",
            f"delivery/reserved-{change}",
        )

    remote = _git(
        ["ls-remote", "--heads", "origin", f"refs/heads/delivery/reserved-{change}"],
        cwd=clone,
    )
    assert remote == ""


@pytest.mark.asyncio
async def test_reserved_path_guard_reports_both_sides_of_a_rename(clone, mgr):
    """The delivery guard sees rename sources, not just destinations.

    Git's default rename detection reports ``git mv .aq/claim.json moved.json``
    as a single changed path, ``moved.json``, hiding that a daemon-owned file
    left the tree. The guard must diff without rename detection, even when
    the repository configures the most aggressive detection.
    """
    repo = pathlib.Path(clone)
    _git(["config", "diff.renames", "copies"], cwd=clone)
    reserved = repo / ".aq" / "claim.json"
    reserved.parent.mkdir()
    reserved.write_text(_RENAMEABLE_CONTENT)
    (repo / "plain.json").write_text(_RENAMEABLE_CONTENT * 2)
    _git(["add", "-f", ".aq/claim.json", "plain.json"], cwd=clone)
    _git(["commit", "-m", "track reserved and plain files"], cwd=clone)

    _git(["switch", "-c", "task/renames"], cwd=clone)
    _git(["mv", ".aq/claim.json", "moved.json"], cwd=clone)
    (repo / ".codex").mkdir()
    _git(["mv", "plain.json", ".codex/settings.json"], cwd=clone)
    _git(["commit", "-m", "rename across the reserved boundary"], cwd=clone)

    # Premise: git's own rename detection collapses the reserved source path.
    detected = _git(["diff", "--name-only", "main", "task/renames", "--"], cwd=clone)
    assert ".aq/claim.json" not in detected.splitlines()

    expected = [".aq/claim.json", ".codex/settings.json"]
    assert await mgr.areserved_paths_in_diff(clone, "main", "task/renames") == expected


class TestBranchSourceIsNotShadowedByASameNamedTag:
    """A delivery source given as a *branch name* must resolve ``refs/heads/<name>``.

    Bare ``git rev-parse <name>`` tries ``refs/<name>`` and ``refs/tags/<name>``
    before ``refs/heads/<name>``, so a tag planted with the branch's name is what
    gets validated and pushed.  Only ``HEAD``, object ids and explicit revision
    expressions keep bare resolution.
    """

    @staticmethod
    def _plant_shadowing_tag(clone: str, branch: str) -> tuple[str, str]:
        """Return ``(branch_tip, decoy_tip)`` after tagging ``<branch>`` elsewhere."""
        branch_tip = _git(["rev-parse", f"refs/heads/{branch}"], cwd=clone)
        _git(["switch", "--detach", branch_tip], cwd=clone)
        decoy_tip = _commit_file(clone, "decoy.txt", "not the branch", "decoy")
        _git(["tag", branch, decoy_tip], cwd=clone)
        _git(["switch", branch], cwd=clone)
        # Sanity: the shadowing is real — bare resolution finds the tag.
        assert _git(["rev-parse", "--verify", branch], cwd=clone) == decoy_tip
        return branch_tip, decoy_tip

    @pytest.mark.asyncio
    async def test_apush_validated_delivery_pushes_the_branch_not_the_tag(self, clone, mgr):
        branch_tip = _commit_file(clone, "work.txt", "real work", "branch work")
        _, decoy_tip = self._plant_shadowing_tag(clone, "main")

        pushed = await mgr.apush_validated_delivery(clone, "origin/main", "main", "main")

        assert pushed == branch_tip
        assert _git(["rev-parse", "origin/main"], cwd=clone) == branch_tip
        assert _git(["rev-parse", "origin/main"], cwd=clone) != decoy_tip

    @pytest.mark.asyncio
    async def test_apush_validated_ref_pushes_the_branch_not_the_tag(self, clone, mgr):
        _git(["switch", "-c", "task/shadowed"], cwd=clone)
        branch_tip = _commit_file(clone, "work.txt", "real work", "branch work")
        self._plant_shadowing_tag(clone, "task/shadowed")

        pushed = await mgr.apush_validated_ref(clone, "task/shadowed", "task/shadowed")

        assert pushed == branch_tip
        assert _git(["rev-parse", "origin/task/shadowed"], cwd=clone) == branch_tip

    @pytest.mark.asyncio
    async def test_apush_branch_pushes_the_branch_not_the_tag(self, clone, mgr):
        _git(["switch", "-c", "task/shadowed"], cwd=clone)
        branch_tip = _commit_file(clone, "work.txt", "real work", "branch work")
        self._plant_shadowing_tag(clone, "task/shadowed")

        await mgr.apush_branch(clone, "task/shadowed")

        assert _git(["rev-parse", "origin/task/shadowed"], cwd=clone) == branch_tip

    @pytest.mark.asyncio
    async def test_branch_name_without_a_local_branch_fails_closed(self, clone, mgr):
        """A tag is never a substitute for a missing branch of the same name."""
        decoy_tip = _commit_file(clone, "decoy.txt", "decoy", "decoy")
        _git(["tag", "release/only-a-tag", decoy_tip], cwd=clone)

        with pytest.raises(GitError, match="refs/heads/release/only-a-tag"):
            await mgr.apush_validated_ref(clone, "release/only-a-tag", "release/only-a-tag")
        assert "release/only-a-tag" not in _git(["branch", "-r"], cwd=clone)

    @pytest.mark.asyncio
    async def test_head_oid_and_revision_expressions_keep_bare_resolution(self, clone, mgr):
        first = _git(["rev-parse", "HEAD"], cwd=clone)
        second = _commit_file(clone, "work.txt", "more", "second")

        assert await mgr.apush_validated_ref(clone, "HEAD", "by-head") == second
        assert await mgr.apush_validated_ref(clone, first, "by-oid") == first
        assert await mgr.apush_validated_ref(clone, "HEAD~1", "by-expr") == first
        assert await mgr.apush_validated_ref(clone, "refs/heads/main", "by-full") == second


class TestAsyncPrepareForTask:
    @pytest.mark.asyncio
    async def test_creates_branch(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/test-branch")
        branch = await mgr.aget_current_branch(clone)
        assert branch == "task/test-branch"

    @pytest.mark.asyncio
    async def test_existing_branch_reuse(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/reuse")
        pathlib.Path(clone, "work.txt").write_text("work")
        _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "work"], cwd=clone
        )
        # Go back to main and prepare again — should reuse branch
        _git(["checkout", "main"], cwd=clone)
        await mgr.aprepare_for_task(clone, "task/reuse")
        branch = await mgr.aget_current_branch(clone)
        assert branch == "task/reuse"


class TestAsyncPushBranch:
    @pytest.mark.asyncio
    async def test_push(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/push-test")
        pathlib.Path(clone, "pushed.txt").write_text("data")
        _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "push test"],
            cwd=clone,
        )
        await mgr.apush_branch(clone, "task/push-test")

    @pytest.mark.asyncio
    async def test_force_with_lease(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/fwl")
        pathlib.Path(clone, "f.txt").write_text("1")
        _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "first"], cwd=clone
        )
        await mgr.apush_branch(clone, "task/fwl")
        # Amend and force push
        pathlib.Path(clone, "f.txt").write_text("2")
        _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=t@t.com",
                "commit",
                "--amend",
                "-m",
                "amended",
            ],
            cwd=clone,
        )
        await mgr.apush_branch(clone, "task/fwl", force_with_lease=True)

    @pytest.mark.asyncio
    async def test_emits_git_push_event(self, clone, mgr):
        """Successful push emits git.push on the EventBus."""
        from src.event_bus import EventBus

        bus = EventBus()
        received: list[dict] = []
        bus.subscribe("git.push", lambda data: received.append(data))

        await mgr.aprepare_for_task(clone, "task/push-event")
        _commit_file(clone, "push_evt.txt", "data", "push event test")
        local_ref = _git(["rev-parse", "HEAD"], cwd=clone)

        await mgr.apush_branch(
            clone,
            "task/push-event",
            event_bus=bus,
            project_id="proj-push",
        )

        assert len(received) == 1
        evt = received[0]
        assert evt["branch"] == "task/push-event"
        assert evt["remote"] == "origin"
        assert evt["project_id"] == "proj-push"
        # First push — no remote ref before, so commit_range is the local ref
        assert evt["commit_range"] == local_ref

    @pytest.mark.asyncio
    async def test_git_push_event_commit_range(self, clone, mgr):
        """Second push should have old..new commit range."""
        from src.event_bus import EventBus

        bus = EventBus()
        received: list[dict] = []
        bus.subscribe("git.push", lambda data: received.append(data))

        await mgr.aprepare_for_task(clone, "task/push-range")
        _commit_file(clone, "first.txt", "1", "first commit")
        await mgr.apush_branch(clone, "task/push-range")
        remote_ref = _git(["rev-parse", "origin/task/push-range"], cwd=clone)

        _commit_file(clone, "second.txt", "2", "second commit")
        local_ref = _git(["rev-parse", "HEAD"], cwd=clone)

        await mgr.apush_branch(
            clone,
            "task/push-range",
            event_bus=bus,
            project_id="proj-range",
        )

        assert len(received) == 1
        evt = received[0]
        assert evt["commit_range"] == f"{remote_ref}..{local_ref}"

    @pytest.mark.asyncio
    async def test_no_git_push_event_without_bus(self, clone, mgr):
        """Without an EventBus, push succeeds silently (backward compat)."""
        await mgr.aprepare_for_task(clone, "task/push-nobus")
        _commit_file(clone, "nobus.txt", "data", "no bus push")
        await mgr.apush_branch(clone, "task/push-nobus")
        # No exception means success — no bus to emit to

    @pytest.mark.asyncio
    async def test_no_git_push_event_on_failure(self, clone, mgr):
        """Failed push should NOT emit a git.push event."""
        from src.event_bus import EventBus

        bus = EventBus()
        received: list[dict] = []
        bus.subscribe("git.push", lambda data: received.append(data))

        with pytest.raises(GitError):
            await mgr.apush_branch(
                clone,
                "nonexistent-branch-xyz",
                event_bus=bus,
                project_id="proj-fail",
            )
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_git_push_event_emission_failure_does_not_break_push(self, clone, mgr):
        """If event emission raises, the push should still succeed."""
        from src.event_bus import EventBus

        bus = EventBus()

        async def bad_handler(data):
            raise RuntimeError("boom")

        bus.subscribe("git.push", bad_handler)

        await mgr.aprepare_for_task(clone, "task/push-resilient")
        _commit_file(clone, "resilient.txt", "data", "resilient push")
        # Should not raise despite bad handler
        await mgr.apush_branch(
            clone,
            "task/push-resilient",
            event_bus=bus,
            project_id="proj-resilient",
        )


class TestAsyncMergeBranch:
    @pytest.mark.asyncio
    async def test_clean_merge(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/merge-test")
        pathlib.Path(clone, "feature.txt").write_text("feature")
        _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "feature"],
            cwd=clone,
        )
        result = await mgr.amerge_branch(clone, "task/merge-test")
        assert result is True


class TestAsyncGetDefaultBranch:
    @pytest.mark.asyncio
    async def test_returns_main(self, clone, mgr):
        branch = await mgr.aget_default_branch(clone)
        assert branch == "main"


class TestAsyncGetDiff:
    @pytest.mark.asyncio
    async def test_returns_diff(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/diff-test")
        pathlib.Path(clone, "changed.txt").write_text("changed")
        _git(["-c", "user.name=Test", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            ["-c", "user.name=Test", "-c", "user.email=t@t.com", "commit", "-m", "change"],
            cwd=clone,
        )
        diff = await mgr.aget_diff(clone, "main")
        assert "changed.txt" in diff


class TestAsyncListBranches:
    @pytest.mark.asyncio
    async def test_lists_branches(self, clone, mgr):
        await mgr.acreate_branch(clone, "branch-a")
        await mgr._arun(["checkout", "main"], cwd=clone)
        await mgr.acreate_branch(clone, "branch-b")
        branches = await mgr.alist_branches(clone)
        names = [b.lstrip("* ") for b in branches]
        assert "branch-a" in names
        assert "branch-b" in names


class TestAsyncRecoverWorkspace:
    @pytest.mark.asyncio
    async def test_recovers(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/recover")
        await mgr.arecover_workspace(clone)
        branch = await mgr.aget_current_branch(clone)
        assert branch == "main"


class TestAsyncDeleteBranch:
    @pytest.mark.asyncio
    async def test_deletes_local(self, clone, mgr):
        await mgr.aprepare_for_task(clone, "task/delete-me")
        await mgr._arun(["checkout", "main"], cwd=clone)
        await mgr.adelete_branch(clone, "task/delete-me", delete_remote=False)
        branches = await mgr.alist_branches(clone)
        names = [b.lstrip("* ") for b in branches]
        assert "task/delete-me" not in names


class TestAsyncHasRemote:
    @pytest.mark.asyncio
    async def test_has_origin(self, clone, mgr):
        assert await mgr.ahas_remote(clone) is True

    @pytest.mark.asyncio
    async def test_no_remote(self, clone, mgr):
        assert await mgr.ahas_remote(clone, "nonexistent") is False


class TestAsyncGetRecentCommits:
    @pytest.mark.asyncio
    async def test_returns_commits(self, clone, mgr):
        commits = await mgr.aget_recent_commits(clone)
        assert "init" in commits


# ---------------------------------------------------------------------------
# Refname validation — trust rule R4 (docs/specs/design/trust-and-ops.md §2.4)
# ---------------------------------------------------------------------------


class TestValidateRef:
    """The guard itself: a conservative ``git check-ref-format`` subset."""

    @pytest.mark.parametrize(
        "name",
        [
            "main",
            "aq/task-1",
            "feature/x.y",
            "release/2026.08",
            "a",
            "task_123",
            "AQ/Task-9",
        ],
    )
    def test_accepts_plausible_refnames(self, name):
        from src.git.manager import _validate_ref

        assert _validate_ref(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "-oops",  # would be parsed as an option
            "--upload-pack=evil",
            "a b",  # whitespace
            "",  # empty
            "a..b",  # git reserves '..'
            "trailing/",
            "branch.lock",
            "has;semicolon",
            "has$dollar",
            "has`backtick`",
            "has|pipe",
            "quote'name",
            "new\nline",
            ".hidden",  # must start alphanumeric
            "/leading-slash",
        ],
    )
    def test_rejects_dangerous_or_malformed_names(self, name):
        from src.git.manager import _validate_ref

        with pytest.raises(GitError):
            _validate_ref(name)

    def test_rejects_non_strings(self):
        from src.git.manager import _validate_ref

        with pytest.raises(GitError):
            _validate_ref(None)

    def test_error_names_the_offending_value(self):
        from src.git.manager import _validate_ref

        with pytest.raises(GitError, match="-oops"):
            _validate_ref("-oops")

    def test_error_names_the_field(self):
        from src.git.manager import _validate_ref

        with pytest.raises(GitError, match="base branch"):
            _validate_ref("-oops", field="base branch")


class TestValidateRev:
    """Read-only diff APIs accept git's revision syntax; write APIs do not.

    ``_validate_ref`` rejects ``HEAD~1`` / ``HEAD^`` / ``HEAD@{1}``, which
    turned an advertised, harmless call (``vibecop``'s ``diff_ref`` schema
    names ``HEAD~3``) into an error dict.  ``_validate_rev`` widens the
    character class by exactly git's revision suffixes and nothing else.
    """

    @pytest.mark.parametrize(
        "rev",
        [
            "main",
            "HEAD",
            "HEAD~1",
            "HEAD~10",
            "HEAD^",
            "HEAD^^",
            "HEAD@{1}",
            "main@{upstream}",
            "aq/task-1~2",
            "0a1b2c3",
        ],
    )
    def test_accepts_revision_expressions(self, rev):
        from src.git.manager import _validate_rev

        assert _validate_rev(rev) == rev

    @pytest.mark.parametrize(
        "rev",
        [
            "-oops",  # the whole point: still cannot become an option
            "--output=/etc/passwd",
            "",
            "a b",
            "has;semicolon",
            "has$dollar",
            "has`backtick`",
            "has|pipe",
            "quote'name",
            "new\nline",
            ".hidden",
            "/leading-slash",
            "rev.lock",
        ],
    )
    def test_still_rejects_dangerous_values(self, rev):
        from src.git.manager import _validate_rev

        with pytest.raises(GitError):
            _validate_rev(rev)

    def test_rejects_non_strings(self):
        from src.git.manager import _validate_rev

        with pytest.raises(GitError):
            _validate_rev(None)

    def test_write_apis_keep_the_stricter_guard(self):
        """A branch must never be moved to a revision expression."""
        from src.git.manager import _validate_ref

        for rev in ("HEAD~1", "HEAD^", "HEAD@{1}"):
            with pytest.raises(GitError):
                _validate_ref(rev)

    @pytest.mark.asyncio
    async def test_get_diff_accepts_a_revision_expression(self, clone, mgr):
        pathlib.Path(clone, "rev.txt").write_text("one")
        _git(["-c", "user.name=T", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            ["-c", "user.name=T", "-c", "user.email=t@t.com", "commit", "-m", "one"],
            cwd=clone,
        )
        diff = await mgr.aget_diff(clone, "HEAD~1")
        assert "rev.txt" in diff

    @pytest.mark.asyncio
    async def test_changed_files_accepts_a_revision_expression(self, clone, mgr):
        pathlib.Path(clone, "rev2.txt").write_text("one")
        _git(["-c", "user.name=T", "-c", "user.email=t@t.com", "add", "-A"], cwd=clone)
        _git(
            ["-c", "user.name=T", "-c", "user.email=t@t.com", "commit", "-m", "two"],
            cwd=clone,
        )
        files = await mgr.aget_changed_files(clone, "HEAD~1")
        assert "rev2.txt" in files

    @pytest.mark.asyncio
    async def test_diff_still_refuses_an_option_shaped_value(self, clone, mgr):
        with pytest.raises(GitError):
            await mgr.aget_diff(clone, "--output=/tmp/evil")


class TestBranchApisValidateBeforeSpawningGit:
    """Every ref-accepting API must raise before a subprocess is created.

    ``_arun`` is replaced with a tripwire: if git is ever spawned with an
    injected name, the test fails loudly rather than depending on git's own
    argument parsing to save us.
    """

    @pytest.fixture
    def tripwire(self, mgr):
        spawned = []

        async def _boom(*args, **kwargs):
            spawned.append(args)
            raise AssertionError(f"git was spawned with {args!r} despite validation")

        mgr._arun = _boom
        mgr._arun_subprocess = _boom
        return mgr

    BAD = "-oops"

    @pytest.mark.asyncio
    async def test_acreate_branch(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.acreate_branch("/tmp/x", self.BAD)

    @pytest.mark.asyncio
    async def test_acheckout_branch(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.acheckout_branch("/tmp/x", self.BAD)

    @pytest.mark.asyncio
    async def test_aswitch_to_branch(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.aswitch_to_branch("/tmp/x", self.BAD)

    @pytest.mark.asyncio
    async def test_aswitch_to_branch_default(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.aswitch_to_branch("/tmp/x", "ok", default_branch=self.BAD)

    @pytest.mark.asyncio
    async def test_apull_branch(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.apull_branch("/tmp/x", self.BAD)

    @pytest.mark.asyncio
    async def test_apush_branch(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.apush_branch("/tmp/x", self.BAD)

    @pytest.mark.asyncio
    async def test_arebase_onto(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.arebase_onto("/tmp/x", self.BAD)

    @pytest.mark.asyncio
    async def test_arebase_onto_target(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.arebase_onto("/tmp/x", "ok", target_branch=self.BAD)

    @pytest.mark.asyncio
    async def test_amerge_branch(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.amerge_branch("/tmp/x", self.BAD)

    @pytest.mark.asyncio
    async def test_adelete_branch(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.adelete_branch("/tmp/x", self.BAD)

    @pytest.mark.asyncio
    async def test_aprepare_for_task(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.aprepare_for_task("/tmp/x", self.BAD)

    @pytest.mark.asyncio
    async def test_amid_chain_sync(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.amid_chain_sync("/tmp/x", self.BAD)

    @pytest.mark.asyncio
    async def test_async_and_merge(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.async_and_merge("/tmp/x", self.BAD)

    @pytest.mark.asyncio
    async def test_arecover_workspace(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.arecover_workspace("/tmp/x", default_branch=self.BAD)

    @pytest.mark.asyncio
    async def test_apull_latest_main(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.apull_latest_main("/tmp/x", default_branch=self.BAD)

    @pytest.mark.asyncio
    async def test_aget_diff(self, tripwire):
        """``aget_diff`` swallows GitError by design — the guard must not be swallowed."""
        with pytest.raises(GitError):
            await tripwire.aget_diff("/tmp/x", base_branch=self.BAD)

    @pytest.mark.asyncio
    async def test_aget_changed_files(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.aget_changed_files("/tmp/x", base_branch=self.BAD)

    @pytest.mark.asyncio
    async def test_acreate_pr_branch(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.acreate_pr("/tmp/x", self.BAD, "t", "b")

    @pytest.mark.asyncio
    async def test_acreate_pr_base(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.acreate_pr("/tmp/x", "ok", "t", "b", base=self.BAD)

    @pytest.mark.asyncio
    async def test_acreate_worktree(self, tripwire):
        with pytest.raises(GitError):
            await tripwire.acreate_worktree("/tmp/x", "/tmp/wt", self.BAD)


class TestUntrustedTextStillFlowsAsFlagValues:
    """R4's accepted case: untrusted text as a flag value is fine.

    A commit message starting with ``-`` must still commit successfully — the
    guard covers refnames, not message bodies.
    """

    @pytest.mark.asyncio
    async def test_commit_message_starting_with_dash(self, clone, mgr):
        pathlib.Path(clone, "hostile.txt").write_text("x")
        committed = await mgr.acommit_all(clone, "--not-a-flag: hostile message")
        assert committed is True
        log = _git(["log", "-1", "--pretty=%s"], cwd=clone)
        assert log == "--not-a-flag: hostile message"


class TestDiffAndMergeBase:
    """Public async wrappers for merge-base + diff (used by task_files.py)."""

    @pytest.mark.asyncio
    async def test_amerge_base_and_aget_diff(self, clone, mgr):
        # Baseline commit is main. Add a branch with two more commits.
        _git(["checkout", "-b", "feature"], cwd=clone)
        pathlib.Path(clone, "a.txt").write_text("a")
        _git(["add", "."], cwd=clone)
        _git(["commit", "-m", "add a"], cwd=clone)
        pathlib.Path(clone, "b.txt").write_text("b")
        _git(["add", "."], cwd=clone)
        _git(["commit", "-m", "add b"], cwd=clone)

        mb = await mgr.amerge_base(clone, "main", "feature")
        assert mb, "merge-base should be non-empty"
        # Merge-base should match main's HEAD.
        main_head = _git(["rev-parse", "main"], cwd=clone)
        assert mb == main_head

        name_status = await mgr.aget_diff(
            clone, "main", to_ref="feature", name_status=True
        )
        assert "a.txt" in name_status and "b.txt" in name_status

        numstat = await mgr.aget_diff(
            clone, "main", to_ref="feature", numstat=True
        )
        # numstat lines look like "1\t0\ta.txt"
        assert "a.txt" in numstat and "b.txt" in numstat

    @pytest.mark.asyncio
    async def test_amerge_base_returns_empty_on_unknown_ref(self, clone, mgr):
        assert await mgr.amerge_base(clone, "main", "no-such-branch") == ""

    @pytest.mark.asyncio
    async def test_ahas_remote(self, clone, mgr):
        assert await mgr.ahas_remote(clone) is True
        assert await mgr.ahas_remote(clone, "no-such-remote") is False


# ------------------------------------------------------------------
# Platform plan 16-20: recovery semantics against real repository
# state (16-18) and external-tool parsing/error paths (19-20)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_staged_patch_preserves_trailing_newline_and_binary_flag(
    tmp_path, bare_repo, clone, mgr
):
    """The salvage patch keeps its terminating newline and binary hunks so
    ``git apply`` accepts it on a fresh checkout."""
    pathlib.Path(clone, "README.md").write_text("edited for salvage\n")
    pathlib.Path(clone, "blob.bin").write_bytes(b"\x00\x01\x02\xfe\xff")
    _git(["add", "README.md", "blob.bin"], cwd=clone)

    patch = await mgr.astaged_patch(clone)

    assert patch.endswith("\n"), "stripped trailing newline corrupts the patch"
    assert "GIT binary patch" in patch, "--binary hunk missing; bytes unrecoverable"

    # The archived text must actually apply on a clean clone of the same base.
    fresh = str(tmp_path / "fresh-checkout")
    subprocess.run(["git", "clone", bare_repo, fresh], check=True, capture_output=True)
    patch_file = tmp_path / "salvage.patch"
    patch_file.write_text(patch)
    subprocess.run(["git", "apply", str(patch_file)], cwd=fresh, check=True, capture_output=True)
    assert pathlib.Path(fresh, "README.md").read_text() == "edited for salvage\n"
    assert pathlib.Path(fresh, "blob.bin").read_bytes() == b"\x00\x01\x02\xfe\xff"


@pytest.mark.asyncio
async def test_async_abort_operations_removes_linked_worktree_lock(tmp_path, clone, mgr, monkeypatch):
    """For a linked worktree the stale ``index.lock`` lives under the base
    repo's ``.git/worktrees/<name>/`` — the resolved location must be
    cleaned, not a fictitious ``<worktree>/.git/index.lock``."""
    wt_path = str(tmp_path / "linked-wt")
    _git(["worktree", "add", "--detach", wt_path, "HEAD"], cwd=clone)
    assert pathlib.Path(wt_path, ".git").is_file()  # gitdir pointer, not a dir

    resolved_git_dir = pathlib.Path(clone, ".git", "worktrees", "linked-wt")
    stale_lock = resolved_git_dir / "index.lock"
    stale_lock.write_text("stale")

    async def harmless_abort(args, cwd=None, **kwargs):
        raise GitError(f"no {args[0]} in progress")

    monkeypatch.setattr(mgr, "_arun", harmless_abort)

    await mgr.aabort_in_progress_operations(wt_path)

    assert not stale_lock.exists()
    # The worktree's gitdir pointer file itself is untouched.
    assert pathlib.Path(wt_path, ".git").is_file()


@pytest.mark.asyncio
async def test_async_force_clean_workspace_removes_ignored_and_reports_clean(clone, mgr):
    """Nuclear cleanup drops staged, untracked, and gitignored artifacts and
    reports the workspace clean."""
    _commit_file(clone, ".gitignore", "ignored.txt\n", "add gitignore")

    pathlib.Path(clone, "README.md").write_text("staged edit")
    _git(["add", "README.md"], cwd=clone)
    pathlib.Path(clone, "untracked.txt").write_text("scratch")
    pathlib.Path(clone, "ignored.txt").write_text("cache artifact")

    assert await mgr.aforce_clean_workspace(clone) is True

    assert pathlib.Path(clone, "README.md").read_text() == "init"
    assert not pathlib.Path(clone, "untracked.txt").exists()
    assert not pathlib.Path(clone, "ignored.txt").exists()
    assert await mgr.ahas_uncommitted_changes(clone) is False


@pytest.mark.asyncio
async def test_async_worktree_list_parses_branch_detached_and_locked_entries(mgr, monkeypatch):
    """Porcelain parsing strips the refs/heads/ prefix, keeps flag values,
    and does not drop the final entry when no trailing blank line follows."""
    porcelain = (
        "worktree /repo/main\n"
        "HEAD " + "1" * 40 + "\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /repo/detached\n"
        "HEAD " + "2" * 40 + "\n"
        "detached\n"
        "\n"
        "worktree /repo/locked\n"
        "HEAD " + "3" * 40 + "\n"
        "branch refs/heads/aq/task-1\n"
        "locked reason: agent crashed\n"
        "\n"
        "worktree /repo/last\n"
        "HEAD " + "4" * 40 + "\n"
        "prunable gitdir file points to non-existent location"
    )

    async def fake_arun(args, cwd=None, **kwargs):
        assert args == ["worktree", "list", "--porcelain"]
        return porcelain

    monkeypatch.setattr(mgr, "_arun", fake_arun)

    entries = await mgr.aworktree_list("/repo/main")

    assert entries == [
        {"path": "/repo/main", "head": "1" * 40, "branch": "main"},
        {"path": "/repo/detached", "head": "2" * 40, "detached": True},
        {
            "path": "/repo/locked",
            "head": "3" * 40,
            "branch": "aq/task-1",
            "locked": "reason: agent crashed",
        },
        {
            "path": "/repo/last",
            "head": "4" * 40,
            "prunable": "gitdir file points to non-existent location",
        },
    ]


@pytest.mark.asyncio
async def test_async_merge_pr_handles_invalid_method_timeout_and_sha(mgr, monkeypatch):
    """Each amerge_pr failure mode returns its specified payload; an invalid
    method never reaches gh; a successful merge parses the printed SHA."""
    from types import SimpleNamespace

    calls: list[list[str]] = []
    behaviors: list = []

    async def fake_subprocess(args, cwd=None, timeout=None, **kwargs):
        calls.append(args)
        behavior = behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    monkeypatch.setattr(mgr, "_arun_subprocess", fake_subprocess)
    pr_url = "https://github.com/org/repo/pull/42"
    from src.git.manager import PullRequestIdentity

    mgr.avalidate_pr_for_merge = AsyncMock(
        return_value=PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40, 1)
    )

    # Invalid method: rejected before any gh invocation.
    result = await mgr.amerge_pr("/repo", pr_url, method="octopus")
    assert result == {"success": False, "sha": None, "error": "invalid method: octopus"}
    assert calls == []

    # Timeout: TimeoutExpired becomes the specified error payload.
    behaviors.append(subprocess.TimeoutExpired(cmd="gh", timeout=300))
    result = await mgr.amerge_pr("/repo", pr_url)
    assert result == {"success": False, "sha": None, "error": "gh pr merge timed out"}

    # Nonzero exit: stderr surfaces as the error.
    behaviors.append(SimpleNamespace(returncode=1, stdout="", stderr="merge conflict\n"))
    result = await mgr.amerge_pr("/repo", pr_url)
    assert result == {"success": False, "sha": None, "error": "merge conflict"}

    # Success: SHA is parsed out of gh's punctuation-wrapped output.
    sha = "0123456789abcdef0123456789abcdef01234567"
    behaviors.append(
        SimpleNamespace(returncode=0, stdout=f"Merged pull request #42 ({sha}).\n", stderr="")
    )
    result = await mgr.amerge_pr("/repo", pr_url, method="rebase")
    assert result == {"success": True, "sha": sha, "error": None}
    assert calls[-1] == [
        "gh",
        "pr",
        "merge",
        pr_url,
        "--rebase",
        "--match-head-commit",
        "b" * 40,
        "--delete-branch",
    ]


# ------------------------------------------------------------------
# afind_open_pr — name match, then commit match
# ------------------------------------------------------------------


class TestAfindOpenPr:
    """A PR delivers commits, not a branch name.

    The task branch is the daemon's handle on the work, but a task
    description that names a different delivery branch (or an agent that
    opens the PR from a second ref pointed at the same tip) publishes the
    very same commits under another head name.  Failing verification for
    that sends a correct, pushed task into a pointless retry, so the tip
    counts as well as the name.
    """

    @staticmethod
    def _fake_gh(mgr, monkeypatch, *, by_name: str = "", prs: list[dict] | None = None):
        from types import SimpleNamespace

        calls: list[list[str]] = []
        real = mgr._arun_subprocess

        async def fake_subprocess(args, cwd=None, timeout=None, **kwargs):
            if args[:3] != ["gh", "pr", "list"]:
                return await real(args, cwd=cwd, timeout=timeout, **kwargs)
            calls.append(args)
            if "--head" in args:
                return SimpleNamespace(returncode=0, stdout=by_name, stderr="")
            open_prs = [{"state": "OPEN", **pr} for pr in (prs or [])]
            return SimpleNamespace(returncode=0, stdout=json.dumps(open_prs), stderr="")

        monkeypatch.setattr(mgr, "_arun_subprocess", fake_subprocess)
        return calls

    @pytest.mark.asyncio
    async def test_head_name_match_wins_without_a_second_query(
        self, clone, mgr, monkeypatch
    ):
        calls = self._fake_gh(mgr, monkeypatch, by_name="https://gh/org/repo/pull/1\n")
        url = await mgr.afind_open_pr(clone, "main")
        assert url == "https://gh/org/repo/pull/1"
        assert len(calls) == 1, "a name match must not cost a second gh call"

    @pytest.mark.asyncio
    async def test_open_pr_from_another_branch_at_the_same_tip_counts(
        self, clone, mgr, monkeypatch
    ):
        _git(["checkout", "-b", "aq/t-1"], cwd=clone)
        _commit_file(clone, "work.txt", "done", "work")
        tip = _git(["rev-parse", "aq/t-1"], cwd=clone)
        _git(["branch", "feature/delivery", "aq/t-1"], cwd=clone)
        _git(["checkout", "main"], cwd=clone)

        self._fake_gh(
            mgr,
            monkeypatch,
            by_name="",
            prs=[
                {
                    "url": "https://gh/org/repo/pull/43",
                    "headRefName": "feature/delivery",
                    "headRefOid": tip,
                }
            ],
        )
        assert await mgr.afind_open_pr(clone, "aq/t-1") == "https://gh/org/repo/pull/43"

    @pytest.mark.asyncio
    async def test_open_prs_at_other_commits_are_not_accepted(
        self, clone, mgr, monkeypatch
    ):
        _git(["checkout", "-b", "aq/t-1"], cwd=clone)
        _commit_file(clone, "work.txt", "done", "work")
        _git(["checkout", "main"], cwd=clone)

        self._fake_gh(
            mgr,
            monkeypatch,
            by_name="",
            prs=[
                {
                    "url": "https://gh/org/repo/pull/44",
                    "headRefName": "someone-else",
                    "headRefOid": "0" * 40,
                }
            ],
        )
        assert await mgr.afind_open_pr(clone, "aq/t-1") is None

    @pytest.mark.asyncio
    async def test_strict_lookup_rejects_a_stale_pr_with_the_same_branch_name(
        self, clone, mgr, monkeypatch
    ):
        """A PR name alone does not prove it delivers the branch's current tip."""
        _git(["checkout", "-b", "aq/t-1"], cwd=clone)
        _commit_file(clone, "work.txt", "done", "work")
        _git(["checkout", "main"], cwd=clone)
        main_tip = _git(["rev-parse", "main"], cwd=clone)

        self._fake_gh(
            mgr,
            monkeypatch,
            by_name="https://gh/org/repo/pull/46\n",
            prs=[
                {
                    "url": "https://gh/org/repo/pull/46",
                    "headRefName": "aq/t-1",
                    "headRefOid": main_tip,
                }
            ],
        )

        assert (
            await mgr.afind_open_pr(clone, "aq/t-1", include_workspace_head=False) is None
        )

    @pytest.mark.asyncio
    async def test_head_commit_counts_when_the_task_branch_never_moved(
        self, clone, mgr, monkeypatch
    ):
        """The agent committed on its own delivery branch and left the
        workspace there; ``aq/<task>`` still points at the start point."""
        _git(["branch", "aq/t-1"], cwd=clone)
        _git(["checkout", "-b", "feature/delivery"], cwd=clone)
        tip = _commit_file(clone, "work.txt", "done", "work")

        self._fake_gh(
            mgr,
            monkeypatch,
            by_name="",
            prs=[
                {
                    "url": "https://gh/org/repo/pull/45",
                    "headRefName": "feature/delivery",
                    "headRefOid": tip,
                }
            ],
        )
        assert await mgr.afind_open_pr(clone, "aq/t-1") == "https://gh/org/repo/pull/45"

    @pytest.mark.asyncio
    async def test_unparseable_gh_output_is_not_an_error(self, clone, mgr, monkeypatch):
        from types import SimpleNamespace

        async def fake_subprocess(args, cwd=None, timeout=None, **kwargs):
            return SimpleNamespace(returncode=1, stdout="not json", stderr="boom")

        monkeypatch.setattr(mgr, "_arun_subprocess", fake_subprocess)
        assert await mgr.afind_open_pr(clone, "aq/t-1") is None
