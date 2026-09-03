"""Tests for pr_merge command (dv2 Phase 2, Task 1).

Tests GitManager.amerge_pr and CommandHandler._cmd_pr_merge.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.git.manager import GitError
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, RepoSourceType, Workspace
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# GitManager.amerge_pr unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_merge_command_shells_gh_and_returns_success(monkeypatch):
    from src.git.manager import GitManager, PullRequestIdentity

    gm = GitManager()
    gm.avalidate_pr_for_merge = AsyncMock(
        return_value=PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40)
    )

    async def fake_arun_subprocess(cmd, cwd, timeout):
        assert cmd[:3] == ["gh", "pr", "merge"]
        assert "--squash" in cmd
        assert "https://github.com/org/repo/pull/42" in cmd
        r = MagicMock()
        r.returncode = 0
        r.stdout = "Merged\n"
        r.stderr = ""
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr("/some/checkout", "https://github.com/org/repo/pull/42")
    assert result["success"] is True
    assert result["error"] is None


@pytest.mark.asyncio
async def test_pr_merge_command_reports_gh_failure(monkeypatch):
    from src.git.manager import GitManager, PullRequestIdentity

    gm = GitManager()
    gm.avalidate_pr_for_merge = AsyncMock(
        return_value=PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40)
    )

    async def fake_arun_subprocess(cmd, cwd, timeout):
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "not mergeable: conflicts"
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr("/some/checkout", "https://github.com/org/repo/pull/42")
    assert result["success"] is False
    assert "conflicts" in result["error"]


@pytest.mark.asyncio
async def test_pr_merge_invalid_method(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    result = await gm.amerge_pr(
        "/some/checkout", "https://github.com/org/repo/pull/42", method="fast-forward"
    )
    assert result["success"] is False
    assert "invalid method" in result["error"]


@pytest.mark.asyncio
async def test_pr_merge_parses_sha_from_output(monkeypatch):
    from src.git.manager import GitManager, PullRequestIdentity

    gm = GitManager()
    gm.avalidate_pr_for_merge = AsyncMock(
        return_value=PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40)
    )
    sha = "a" * 40

    async def fake_arun_subprocess(cmd, cwd, timeout):
        r = MagicMock()
        r.returncode = 0
        r.stdout = f"Merged pull request #42 ({sha})\n"
        r.stderr = ""
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr("/some/checkout", "https://github.com/org/repo/pull/42")
    assert result["success"] is True
    assert result["sha"] == sha


@pytest.mark.asyncio
async def test_pr_merge_pins_the_validated_head_oid(monkeypatch):
    """The reviewed head is passed to gh, not re-resolved by a mutable PR URL."""
    from src.git.manager import GitManager, PullRequestIdentity

    gm = GitManager()
    head_oid = "b" * 40
    gm.avalidate_pr_for_merge = AsyncMock(
        return_value=PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", head_oid)
    )

    async def fake_arun_subprocess(cmd, cwd, timeout):
        assert cmd == [
            "gh",
            "pr",
            "merge",
            "https://github.com/org/repo/pull/42",
            "--squash",
            "--match-head-commit",
            head_oid,
            "--delete-branch",
        ]
        return MagicMock(returncode=0, stdout="Merged\n", stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr(
        "/some/checkout",
        "https://github.com/org/repo/pull/42",
        expected_head_oid=head_oid,
    )
    assert result["success"] is True


def _pr_identity_payload(*, base_oid: str = "a" * 40, head_oid: str = "b" * 40) -> str:
    import json

    return json.dumps(
        {
            "baseRefName": "main",
            "baseRefOid": base_oid,
            "headRefName": "feature/guard",
            "headRefOid": head_oid,
            "baseRepository": {"nameWithOwner": "org/repo"},
        }
    )


# The exact jq program handed to ``gh api``: one line per PR-file path, with a
# rename or copy source (``previous_filename``) emitted after its destination.
_PR_FILES_JQ = ".[] | .filename, (.previous_filename // empty)"


def _pr_files_stdout(files: list[dict]) -> str:
    """Emulate what ``gh api --jq _PR_FILES_JQ`` prints for a PR-files payload."""
    lines: list[str] = []
    for entry in files:
        lines.append(entry["filename"])
        if entry.get("previous_filename"):
            lines.append(entry["previous_filename"])
    return "".join(f"{line}\n" for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "entry", "reserved"),
    [
        ("added", {"filename": ".aq/claim.json", "status": "added"}, ".aq/claim.json"),
        ("modified", {"filename": ".aq-worktree.json", "status": "modified"}, ".aq-worktree.json"),
        ("deleted", {"filename": ".codex/config.json", "status": "deleted"}, ".codex/config.json"),
        (
            "renamed out of a reserved path",
            {"filename": "moved.json", "status": "renamed", "previous_filename": ".aq/claim.json"},
            ".aq/claim.json",
        ),
        (
            "renamed onto a reserved path",
            {"filename": ".codex/config.json", "status": "renamed", "previous_filename": "c.json"},
            ".codex/config.json",
        ),
        (
            "copied out of a reserved path",
            {"filename": "copy.json", "status": "copied", "previous_filename": ".aq/claim.json"},
            ".aq/claim.json",
        ),
    ],
)
async def test_pr_validation_rejects_added_modified_and_deleted_reserved_paths(
    monkeypatch, operation, entry, reserved
):
    from src.git.manager import GitManager

    gm = GitManager()
    files = [{"filename": "work.py", "status": "modified"}, entry]

    async def fake_arun_subprocess(cmd, cwd, timeout):
        if cmd[:3] == ["gh", "pr", "view"]:
            return MagicMock(returncode=0, stdout=_pr_identity_payload(), stderr="")
        assert cmd[:4] == ["gh", "api", "--paginate", "repos/org/repo/pulls/42/files"]
        # GitHub reports a rename or copy under its new ``filename`` and keeps
        # the old name in ``previous_filename``; the guard must ask for both.
        assert cmd[cmd.index("--jq") + 1] == _PR_FILES_JQ
        return MagicMock(returncode=0, stdout=_pr_files_stdout(files), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)

    # Safety must not vary with whether the reserved path was added, modified,
    # deleted, or moved across the reserved boundary in either direction.
    with pytest.raises(GitError, match=f"reserved daemon bookkeeping.*{reserved}") as excinfo:
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")
    assert operation
    assert "work.py" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_pr_changed_paths_include_rename_sources(monkeypatch):
    """``_apr_changed_paths`` returns every filename and every previous_filename."""
    from src.git.manager import GitManager, PullRequestIdentity

    gm = GitManager()
    identity = PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40)
    files = [
        {"filename": "work.py", "status": "modified"},
        {"filename": "moved.json", "status": "renamed", "previous_filename": ".aq/claim.json"},
        {"filename": "new.py", "status": "added"},
    ]

    async def fake_arun_subprocess(cmd, cwd, timeout):
        assert cmd[cmd.index("--jq") + 1] == _PR_FILES_JQ
        return MagicMock(returncode=0, stdout=_pr_files_stdout(files), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    paths = await gm._apr_changed_paths("/some/checkout", identity)
    assert paths == ["work.py", "moved.json", ".aq/claim.json", "new.py"]


@pytest.mark.asyncio
async def test_pr_validation_fails_closed_for_malformed_or_unavailable_identity(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()

    async def malformed(cmd, cwd, timeout):
        return MagicMock(returncode=0, stdout='{"headRefOid": "not-an-oid"}', stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", malformed)
    with pytest.raises(GitError, match="complete PR identity"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")

    async def unavailable(cmd, cwd, timeout):
        return MagicMock(returncode=1, stdout="", stderr="not authenticated")

    monkeypatch.setattr(gm, "_arun_subprocess", unavailable)
    with pytest.raises(GitError, match="could not resolve PR identity"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")

    calls = 0

    async def unavailable_diff(cmd, cwd, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return MagicMock(returncode=0, stdout=_pr_identity_payload(), stderr="")
        return MagicMock(returncode=1, stdout="", stderr="API unavailable")

    monkeypatch.setattr(gm, "_arun_subprocess", unavailable_diff)
    with pytest.raises(GitError, match="could not inspect PR delivery diff"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")


@pytest.mark.asyncio
async def test_pr_validation_accepts_clean_paths_and_detects_a_changed_head(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    views = iter([_pr_identity_payload(), _pr_identity_payload()])

    async def fake_arun_subprocess(cmd, cwd, timeout):
        if cmd[:3] == ["gh", "pr", "view"]:
            return MagicMock(returncode=0, stdout=next(views), stderr="")
        return MagicMock(returncode=0, stdout="work.py\n", stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)

    identity = await gm.avalidate_pr_for_merge(
        "/some/checkout", "https://github.com/org/repo/pull/42"
    )
    assert identity.head_oid == "b" * 40

    views = iter([_pr_identity_payload(), _pr_identity_payload(head_oid="c" * 40)])

    with pytest.raises(GitError, match="identity changed"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")

    views = iter([_pr_identity_payload(), _pr_identity_payload(base_oid="e" * 40)])

    with pytest.raises(GitError, match="identity changed"):
        await gm.avalidate_pr_for_merge("/some/checkout", "https://github.com/org/repo/pull/42")


@pytest.mark.asyncio
async def test_pr_merge_refuses_when_head_changes_after_ci_validation(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    merge_called = False

    async def fake_arun_subprocess(cmd, cwd, timeout):
        nonlocal merge_called
        if cmd[:3] == ["gh", "pr", "view"]:
            return MagicMock(returncode=0, stdout=_pr_identity_payload(head_oid="c" * 40), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            merge_called = True
        return MagicMock(returncode=0, stdout="Merged\n", stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr(
        "/some/checkout",
        "https://github.com/org/repo/pull/42",
        expected_head_oid="b" * 40,
        expected_base_oid="a" * 40,
    )

    assert result["success"] is False
    assert "identity changed" in result["error"]
    assert merge_called is False


@pytest.mark.asyncio
async def test_direct_manager_merge_validates_and_pins_identity(monkeypatch):
    from src.git.manager import GitManager, PullRequestIdentity

    gm = GitManager()
    identity = PullRequestIdentity("org/repo", 42, "main", "a" * 40, "feature", "b" * 40)
    gm.avalidate_pr_for_merge = AsyncMock(return_value=identity)

    async def fake_arun_subprocess(cmd, cwd, timeout):
        assert cmd[-3:] == ["--match-head-commit", "b" * 40, "--delete-branch"]
        return MagicMock(returncode=0, stdout="Merged\n", stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    result = await gm.amerge_pr("/some/checkout", "https://github.com/org/repo/pull/42")

    assert result["success"] is True
    gm.avalidate_pr_for_merge.assert_awaited_once_with(
        "/some/checkout", "https://github.com/org/repo/pull/42"
    )


# ---------------------------------------------------------------------------
# CommandHandler._cmd_pr_merge integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "pm.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1"))
    await d.create_workspace(
        Workspace(
            id="w1",
            project_id="p1",
            workspace_path="/tmp/p1",
            source_type=RepoSourceType.CLONE,
        )
    )
    yield d
    await d.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "pm.db"),
        data_dir=str(tmp_path / "d"),
    )


@pytest.fixture
async def handler(db, config):
    o = Orchestrator(config)
    o.db = db
    o.git = MagicMock()
    return CommandHandler(o, config)


@pytest.mark.asyncio
async def test_cmd_pr_merge_routes_through_git_manager(monkeypatch, handler):
    calls = {}

    head_oid = "c" * 40

    async def fake_amerge(
        checkout_path, pr_url, method="squash", *, expected_head_oid=None, expected_base_oid=None
    ):
        calls["args"] = (checkout_path, pr_url, method)
        calls["head_oid"] = expected_head_oid
        calls["base_oid"] = expected_base_oid
        return {"success": True, "sha": "abc123", "error": None}

    handler.orchestrator.git.avalidate_pr_for_merge = AsyncMock(
        return_value=MagicMock(head_oid=head_oid)
    )
    monkeypatch.setattr(handler.orchestrator.git, "amerge_pr", fake_amerge)
    result = await handler.execute(
        "pr_merge",
        {
            "project_id": "p1",
            "pr_url": "https://github.com/o/r/pull/1",
            "method": "squash",
        },
    )
    assert result["success"] is True
    assert result["sha"] == "abc123"
    # gh runs outside any checkout: given a full PR URL it resolves the repo
    # from the URL, so pr_merge no longer borrows the project's base clone
    # (which is routinely the operator's own working tree).
    checkout_path, url, method = calls["args"]
    assert (url, method) == ("https://github.com/o/r/pull/1", "squash")
    assert checkout_path == handler.config.data_dir
    assert checkout_path != "/tmp/p1"
    assert calls["head_oid"] == head_oid


@pytest.mark.asyncio
async def test_cmd_pr_merge_rejects_unknown_project(handler):
    result = await handler.execute(
        "pr_merge",
        {
            "project_id": "nope",
            "pr_url": "https://github.com/o/r/pull/1",
        },
    )
    assert result["success"] is False
    assert "project" in result["error"].lower()


@pytest.mark.asyncio
async def test_cmd_pr_merge_requires_pr_url(handler):
    result = await handler.execute(
        "pr_merge",
        {"project_id": "p1"},
    )
    assert result["success"] is False
    assert "pr_url" in result["error"].lower()


@pytest.mark.asyncio
async def test_cmd_pr_merge_requires_project_id(handler):
    result = await handler.execute(
        "pr_merge",
        {"pr_url": "https://github.com/o/r/pull/1"},
    )
    assert result["success"] is False
    assert "project_id" in result["error"].lower()
