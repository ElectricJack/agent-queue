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


class TestAsyncBranchCommitCount:
    """``abranch_commit_count`` — how much work a task branch actually carries.

    The require-a-PR verification gate uses it to tell "the agent forgot to
    open a PR" apart from "there is nothing to open a PR for".
    """

    @pytest.mark.asyncio
    async def test_empty_branch_counts_zero(self, mgr, clone):
        await mgr.acreate_branch(clone, "aq/review-1")
        assert await mgr.abranch_commit_count(clone, "aq/review-1", "main") == 0

    @pytest.mark.asyncio
    async def test_counts_commits_ahead_of_base(self, mgr, clone):
        await mgr.acreate_branch(clone, "aq/work-1")
        _commit_file(clone, "a.txt", "a", "one")
        _commit_file(clone, "b.txt", "b", "two")
        assert await mgr.abranch_commit_count(clone, "aq/work-1", "main") == 2

    @pytest.mark.asyncio
    async def test_prefers_the_remote_base_ref(self, mgr, clone):
        """Local ``main`` may lag origin; the pushed base is the real one."""
        await mgr.acreate_branch(clone, "aq/work-2")
        _commit_file(clone, "c.txt", "c", "three")
        _git(["push", "origin", "aq/work-2"], cwd=clone)
        _git(["push", "origin", "aq/work-2:main"], cwd=clone)
        _git(["fetch", "origin"], cwd=clone)
        # origin/main now contains the branch tip; local main does not.
        assert await mgr.abranch_commit_count(clone, "aq/work-2", "main") == 0

    @pytest.mark.asyncio
    async def test_falls_back_to_the_local_base_ref(self, mgr, clone):
        """A checkout with no ``origin/<base>`` still gets an answer."""
        await mgr.acreate_branch(clone, "aq/work-3")
        _commit_file(clone, "d.txt", "d", "four")
        _git(["update-ref", "-d", "refs/remotes/origin/main"], cwd=clone)
        assert await mgr.abranch_commit_count(clone, "aq/work-3", "main") == 1

    @pytest.mark.asyncio
    async def test_unresolvable_base_returns_none(self, mgr, clone):
        await mgr.acreate_branch(clone, "aq/work-4")
        assert await mgr.abranch_commit_count(clone, "aq/work-4", "no-such-base") is None

    @pytest.mark.asyncio
    async def test_unresolvable_branch_returns_none(self, mgr, clone):
        assert await mgr.abranch_commit_count(clone, "aq/never-created", "main") is None


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
    async def test_commit_with_changes(self, clone, mgr):
        pathlib.Path(clone, "newfile.txt").write_text("hello")
        committed = await mgr.acommit_all(clone, "add newfile")
        assert committed is True

    @pytest.mark.asyncio
    async def test_no_changes(self, clone, mgr):
        committed = await mgr.acommit_all(clone, "nothing")
        assert committed is False

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
    assert calls[-1] == ["gh", "pr", "merge", pr_url, "--rebase", "--delete-branch"]


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
