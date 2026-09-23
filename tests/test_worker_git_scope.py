"""A worker token may push and open a PR for its own task branch, only.

Every worker profile's close protocol ends with "push the branch and open a
PR", but ``AGENT_COMMAND_SET`` carries no ``git_*`` command, so the aq path
answered ``out of scope: git_create_pr`` and workers fell back to the raw
``gh`` CLI.  The carve-out here is verified against persisted state (session →
task → agent) and reaches exactly the branch the task records.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from src.api.auth import RequestScope
from src.api.scope import (
    _WORKER_GIT_COMMANDS,
    check_request_scope,
    worker_branches_for_session,
)
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.manager import GitManager
from src.plugins.internal.git import _build_tool_definitions
from src.profiles.drift import diff_profile, merge_profile_grants, vault_profile_path
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
)
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=["task", "pool"])
async def env(tmp_path, request):
    db = Database(lease_dsn("worker-git.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="p", repo_default_branch="main"))
    await db.create_project(Project(id="other", name="other"))
    await db.upsert_profile(AgentProfile(
        id="coder", name="coder", harness="claude", needs_workspace=False,
        default_class="standard-low",
    ))
    await db.create_agent(Agent(id="a1", name="a1", profile_id="coder"))
    await db.create_task(Task(
        id="t1", project_id="p", title="work", description="work",
        status=TaskStatus.IN_PROGRESS, profile_id="coder", assigned_agent_id="a1",
        branch_name="aq/calm-ember-48",
    ))
    await db.update_agent("a1", state=AgentState.BUSY, current_task_id="t1")
    await db.create_session(SessionRecord(
        id="s1", task_id="t1", project_id="p", agent_id="a1", profile_id="coder",
        harness="claude", provider="fake", name="s1", lifecycle=request.param,
        state="running", work_dir=str(tmp_path), epoch="test",
        instance_token="instance-a1", started_at=time.time(), last_claim_epoch=0,
    ))
    scope = RequestScope(
        kind="session", session_id="s1",
        task_id="t1" if request.param == "task" else None, project_id="p",
    )
    yield db, scope
    await db.close()


# --- the grant ------------------------------------------------------------


async def test_worker_creates_a_pr_for_its_own_branch(env):
    db, scope = env
    args = {"title": "Fix it", "branch": "aq/calm-ember-48"}
    assert await check_request_scope("git_create_pr", args, scope, db=db) is None
    assert args["project_id"] == "p"
    assert args["session_id"] == "s1"


async def test_worker_creates_a_pr_with_defaults(env):
    """The common case: no explicit branch (the worktree is already on it)."""
    db, scope = env
    args = {"title": "Fix it"}
    assert await check_request_scope("git_create_pr", args, scope, db=db) is None


async def test_conventional_branch_name_is_also_accepted(env):
    db, scope = env
    args = {"title": "Fix it", "branch": "aq/t1"}
    assert await check_request_scope("git_create_pr", args, scope, db=db) is None


async def test_worker_pushes_its_own_branch(env):
    db, scope = env
    args = {"branch": "aq/calm-ember-48"}
    assert await check_request_scope("git_push", args, scope, db=db) is None


# --- the command: publication derived from the held task ------------------

_TIP = "c" * 40
_REPOSITORY = "https://github.com/acme/widgets"
_BRANCH_KEY = {"git_push": "branch", "push_branch": "branch_name", "git_create_pr": "branch"}


@pytest.fixture
async def worker_git(env, tmp_path, internal_plugins_handler):
    """A worker session's command path with a mocked GitManager.

    The HTTP scope is trusted and its ``_scope`` is forwarded as-is; tests
    that pass arguments the HTTP layer would refuse are exercising the
    command's own, second check.
    """
    db, scope = env
    await db.update_project("p", repo_url=_REPOSITORY)
    base = tmp_path / "base-checkout"
    base.mkdir()
    await db.create_workspace(Workspace(
        id="base", project_id="p", workspace_path=str(base),
        source_type=RepoSourceType.LINK,
    ))
    git = create_autospec(GitManager, instance=True)
    git.avalidate_checkout.return_value = True
    git.aget_current_branch.return_value = "main"
    git.aref_exists.return_value = True
    git.apush_validated_delivery.return_value = _TIP
    git.bind_github_repository.return_value = GitHubRepositoryBinding(303, "acme/widgets")
    git.acreate_pr.return_value = f"{_REPOSITORY}/pull/7"
    config = AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path),
        database=DatabaseConfig(url=lease_dsn("worker-git.db")),
        data_dir=str(tmp_path / "data"),
    )
    handler = await internal_plugins_handler(db=db, config=config, git=git)

    def scoped(**args):
        return {"_scope": {
            "kind": "session", "session_id": scope.session_id,
            "task_id": scope.task_id, "project_id": scope.project_id,
        }, **args}

    return SimpleNamespace(
        db=db, git=git, handler=handler, scoped=scoped, work_dir=str(tmp_path),
    )


def _assert_no_network(git) -> None:
    """Nothing that binds a repository, selects a credential or transfers ran."""
    for name in ("apush_validated_delivery", "apush_branch", "bind_github_repository",
                 "acreate_pr"):
        getattr(git, name).assert_not_awaited()


@pytest.mark.parametrize("command", ["git_push", "push_branch"])
@pytest.mark.parametrize("explicit_branch", [False, True])
async def test_worker_push_uses_claimed_worktree_and_task_branch(
    worker_git, command, explicit_branch,
):
    w = worker_git
    # The missing session_id argument reproduces the path that previously fell
    # through to the first project workspace; HEAD on ``main`` is ignored.
    args = w.scoped()
    if explicit_branch:
        args[_BRANCH_KEY[command]] = "aq/calm-ember-48"
    result = await w.handler.execute(command, args)

    assert "error" not in result, result
    assert result.get("pushed", result.get("branch")) == "aq/calm-ember-48"
    assert result["oid"] == _TIP
    w.git.apush_validated_delivery.assert_awaited_once_with(
        w.work_dir, "refs/remotes/origin/main", "aq/calm-ember-48", "aq/calm-ember-48",
        expected_remote_oid=None, repository_url=_REPOSITORY,
        event_bus=w.handler._bus, project_id="p",
    )
    w.git.apush_branch.assert_not_awaited()
    w.git.aget_current_branch.assert_not_awaited()


@pytest.mark.parametrize("command", ["git_push", "push_branch"])
async def test_worker_lease_reaches_the_validated_delivery_push(worker_git, command):
    w = worker_git
    oid = "a" * 40
    result = await w.handler.execute(command, w.scoped(expected_remote_oid=oid))

    assert result["oid"] == _TIP
    assert w.git.apush_validated_delivery.await_args.kwargs["expected_remote_oid"] == oid


async def test_worker_push_without_a_fetched_default_is_a_root_delivery(worker_git):
    w = worker_git
    w.git.aref_exists.return_value = False
    await w.handler.execute("git_push", w.scoped())
    assert w.git.apush_validated_delivery.await_args.args[1] is None


@pytest.mark.parametrize("command,args,error", [
    ("git_push", {"branch": "main"}, "is not this session's task branch"),
    ("push_branch", {"branch_name": "aq/someone-else-99"}, "is not this session's task branch"),
    ("git_create_pr", {"title": "t", "branch": "aq/someone-else-99"},
     "is not this session's task branch"),
    ("git_create_pr", {"title": "t", "base": "release/1.0"},
     "is not the project's default branch"),
    ("git_push", {"workspace": "base"}, "workspace is not this session's worktree"),
    ("git_create_pr", {"title": "t", "workspace": "base"},
     "workspace is not this session's worktree"),
])
async def test_foreign_publication_fails_before_credential_use(worker_git, command, args, error):
    w = worker_git
    result = await w.handler.execute(command, w.scoped(**args))
    assert error in result["error"]
    _assert_no_network(w.git)


async def test_task_repository_from_another_project_fails_before_credential_use(worker_git):
    w = worker_git
    await w.db.create_repo(RepoConfig(
        id="foreign", project_id="other", source_type=RepoSourceType.CLONE,
        url="https://github.com/evil/other",
    ))
    await w.db.update_task("t1", repo_id="foreign")
    for command, args in (("git_push", {}), ("git_create_pr", {"title": "t"})):
        result = await w.handler.execute(command, w.scoped(**args))
        assert result["error"] == "task repository is not authorized for this project"
    _assert_no_network(w.git)


async def test_task_repository_row_is_the_publication_target(worker_git):
    w = worker_git
    await w.db.create_repo(RepoConfig(
        id="service", project_id="p", source_type=RepoSourceType.CLONE,
        url="https://github.com/acme/service",
    ))
    await w.db.update_task("t1", repo_id="service")

    await w.handler.execute("git_push", w.scoped())
    await w.handler.execute("git_create_pr", w.scoped(title="t"))

    assert (w.git.apush_validated_delivery.await_args.kwargs["repository_url"]
            == "https://github.com/acme/service")
    w.git.bind_github_repository.assert_awaited_once_with("https://github.com/acme/service")


async def test_a_network_origin_is_never_its_own_authority(worker_git):
    """No recorded repository: the checkout's remote cannot stand in for one."""
    w = worker_git
    await w.db.update_project("p", repo_url="")
    w.git.aget_remote_url.return_value = "https://github.com/evil/other.git"
    result = await w.handler.execute("git_push", w.scoped())
    assert result["error"] == "task has no authorized repository"
    _assert_no_network(w.git)


async def test_a_local_origin_may_stand_in_for_a_missing_record(worker_git, tmp_path):
    w = worker_git
    origin = tmp_path / "origin.git"
    origin.mkdir()
    await w.db.update_project("p", repo_url="")
    w.git.aget_remote_url.return_value = str(origin)

    await w.handler.execute("git_push", w.scoped())
    assert w.git.apush_validated_delivery.await_args.kwargs["repository_url"] == str(origin)

    result = await w.handler.execute("git_create_pr", w.scoped(title="t"))
    assert result["error"] == "Project has no authorized GitHub repository"
    w.git.bind_github_repository.assert_not_awaited()


async def test_worker_pr_uses_the_task_branch_and_repository(worker_git):
    w = worker_git
    result = await w.handler.execute("git_create_pr", w.scoped(title="Fix it", body="why"))

    assert result == {
        "project_id": "p", "pr_url": f"{_REPOSITORY}/pull/7",
        "branch": "aq/calm-ember-48", "base": "main",
    }
    w.git.bind_github_repository.assert_awaited_once_with(_REPOSITORY)
    assert w.git.acreate_pr.await_args.args == (
        w.work_dir, "aq/calm-ember-48", "Fix it", "why", "main",
    )
    assert w.git.acreate_pr.await_args.kwargs["repository"] == GitHubRepositoryBinding(
        303, "acme/widgets"
    )
    w.git.aget_current_branch.assert_not_awaited()


async def test_a_session_that_no_longer_holds_its_task_publishes_nothing(worker_git):
    w = worker_git
    await w.db.transition_task("t1", TaskStatus.COMPLETED, context="test")
    for command, args in (("git_push", {}), ("git_create_pr", {"title": "t"})):
        result = await w.handler.execute(command, w.scoped(**args))
        assert result["error"] == "No active task branch for this session"
    _assert_no_network(w.git)


async def test_ungranted_publication_is_denied_before_the_command_runs(worker_git):
    w = worker_git
    await w.db.upsert_profile(AgentProfile(
        id="coder", name="coder", harness="claude", needs_workspace=False,
        default_class="standard-low", harness_tools=[], aq_commands=[],
        plugin_tools=["git_log"],
    ))
    for command, args in (
        ("git_push", {}), ("push_branch", {}), ("git_create_pr", {"title": "t"}),
    ):
        result = await w.handler.execute(command, w.scoped(**args))
        assert result["error_code"] == "capability_denied", result
    _assert_no_network(w.git)


async def test_worker_squash_lease_workflow_through_the_command(
    env, tmp_path, internal_plugins_handler,
):
    """The documented push → squash → ``--expected-remote-oid`` flow, end to end."""
    import subprocess

    def git(*args: str, cwd: Path = tmp_path) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=t@t.test", *args],
            cwd=cwd, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit(content: str) -> str:
        (tmp_path / "work.txt").write_text(content)
        git("add", "work.txt")
        git("commit", "-m", content)
        return git("rev-parse", "HEAD")

    db, scope = env
    origin = tmp_path.parent / f"{tmp_path.name}-origin.git"
    git("init", "--bare", "--initial-branch=main", str(origin))
    # The session's worktree (env's work_dir) is a clone of the project origin.
    git("init", "--initial-branch=main")
    git("remote", "add", "origin", str(origin))
    commit("base")
    git("push", "origin", "main")
    await db.update_project("p", repo_url=str(origin))
    config = AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path),
        database=DatabaseConfig(url=lease_dsn("worker-git.db")),
        data_dir=str(tmp_path.parent / f"{tmp_path.name}-data"),
    )
    handler = await internal_plugins_handler(db=db, config=config, git=GitManager())

    async def push(**args):
        return await handler.execute("git_push", {"_scope": {
            "kind": "session", "session_id": scope.session_id,
            "task_id": scope.task_id, "project_id": scope.project_id,
        }, **args})

    def remote_tip() -> str:
        return git("rev-parse", "refs/heads/aq/calm-ember-48", cwd=origin)

    git("switch", "-c", "aq/calm-ember-48")
    commit("first")
    first = (await push())["oid"]
    assert first == git("rev-parse", "HEAD") == remote_tip()

    commit("second")
    git("reset", "--soft", "main")
    squashed = commit("squashed")
    assert "fast-forward" in (await push())["error"]
    assert (await push(expected_remote_oid=first))["oid"] == squashed == remote_tip()
    # A stale lease is refused; the remote keeps the squashed commit.
    assert "expected target" in (await push(expected_remote_oid=first))["error"]
    assert remote_tip() == squashed


async def test_push_branch_alias_is_held_to_the_task_branch(env):
    db, scope = env
    args = {"branch_name": "aq/calm-ember-48", "expected_remote_oid": "a" * 40}
    assert await check_request_scope("push_branch", args, scope, db=db) is None
    assert args["project_id"] == "p"
    assert (
        await check_request_scope("push_branch", {"branch_name": "main"}, scope, db=db)
        == "out of scope: branch mismatch"
    )


async def test_explicit_lease_stays_on_the_held_task_branch(env):
    db, scope = env
    args = {"branch": "aq/calm-ember-48", "expected_remote_oid": "a" * 40}
    assert await check_request_scope("git_push", args, scope, db=db) is None
    assert args["project_id"] == "p"
    assert args["session_id"] == "s1"
    assert (
        await check_request_scope(
            "git_push",
            {"branch": "main", "expected_remote_oid": "a" * 40},
            scope,
            db=db,
        )
        == "out of scope: branch mismatch"
    )


async def test_worker_cannot_select_a_different_workspace(env):
    db, scope = env
    assert (
        await check_request_scope("git_push", {"workspace": "other"}, scope, db=db)
        == "out of scope: workspace mismatch"
    )


@pytest.mark.parametrize(
    "command,args",
    [
        ("get_git_status", {}),
        ("git_diff", {}),
        ("git_log", {"count": 5}),
        ("git_branch", {}),
        ("git_changed_files", {}),
    ],
)
async def test_read_only_git_commands_are_reachable(env, command, args):
    db, scope = env
    assert await check_request_scope(command, args, scope, db=db) is None


async def test_pr_base_may_be_the_project_default_branch(env):
    db, scope = env
    args = {"title": "Fix it", "base": "main"}
    assert await check_request_scope("git_create_pr", args, scope, db=db) is None


# --- the limits -----------------------------------------------------------


async def test_worker_cannot_create_a_pr_for_another_branch(env):
    db, scope = env
    args = {"title": "Sneaky", "branch": "aq/someone-else-99"}
    assert (
        await check_request_scope("git_create_pr", args, scope, db=db)
        == "out of scope: branch mismatch"
    )


async def test_worker_cannot_push_another_branch(env):
    db, scope = env
    assert (
        await check_request_scope("git_push", {"branch": "main"}, scope, db=db)
        == "out of scope: branch mismatch"
    )


async def test_worker_cannot_retarget_a_pr_base(env):
    db, scope = env
    args = {"title": "Sneaky", "base": "release/1.0"}
    assert (
        await check_request_scope("git_create_pr", args, scope, db=db)
        == "out of scope: branch mismatch"
    )


async def test_worker_cannot_create_an_unrelated_branch(env):
    db, scope = env
    assert (
        await check_request_scope("git_branch", {"name": "wip/whatever"}, scope, db=db)
        == "out of scope: branch mismatch"
    )


@pytest.mark.parametrize("command", ["git_merge", "pr_merge", "git_checkout", "git_commit"])
async def test_merge_and_checkout_stay_out_of_scope(env, command):
    db, scope = env
    assert command not in _WORKER_GIT_COMMANDS
    assert (
        await check_request_scope(command, {"branch_name": "aq/calm-ember-48"}, scope, db=db)
        == f"out of scope: {command}"
    )


async def test_another_projects_id_is_rejected(env):
    db, scope = env
    args = {"title": "Fix it", "project_id": "other"}
    assert (
        await check_request_scope("git_create_pr", args, scope, db=db)
        == "out of scope: project_id mismatch"
    )


async def test_a_stale_session_grants_nothing(env):
    """The task moved on; the still-running session must lose the grant."""
    db, scope = env
    await db.transition_task("t1", TaskStatus.COMPLETED, context="test")
    assert await worker_branches_for_session(db, scope) is None
    assert (
        await check_request_scope("git_push", {}, scope, db=db)
        == "out of scope: git_push"
    )


async def test_a_token_for_an_unknown_session_grants_nothing(env):
    db, _ = env
    scope = RequestScope(kind="session", session_id="ghost", task_id="t1", project_id="p")
    assert await worker_branches_for_session(db, scope) is None


async def test_shipped_worker_grants_match_the_push_and_pr_contract():
    from src.profiles.parser import parse_profile
    from src.profiles.capabilities import classify_capability

    root = Path(__file__).resolve().parents[1]
    names = {item["name"]: item for item in _build_tool_definitions()}
    for command in ("git_push", "push_branch"):
        assert "expected_remote_oid" in names[command]["input_schema"]["properties"]
    assert {"title", "body"} <= set(names["git_create_pr"]["input_schema"]["properties"])
    for command in ("git_push", "git_create_pr"):
        assert classify_capability(
            command, plugin_command_names=frozenset(names)
        ) == "plugin_tools"
    for harness in ("claude", "codex"):
        profile = parse_profile(
            (root / "src" / "profiles" / "defaults" / f"worker-{harness}" / "profile.md")
            .read_text(encoding="utf-8")
        )
        commands = set(profile.capabilities["plugin_tools"])
        assert {"git_push", "git_create_pr"} <= commands
        assert "integration_push_conflict_resolution" not in set(profile.capabilities["aq_commands"])
        assert "integration_delivery_promote" not in set(profile.capabilities["aq_commands"])


async def test_cli_push_lease_option_reaches_the_daemon_contract():
    from unittest.mock import AsyncMock, patch

    from click.testing import CliRunner
    from src.cli.app import cli

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.execute = AsyncMock(return_value={"project_id": "p", "pushed": "aq/t1"})
    oid = "a" * 40
    with patch("src.cli.app._get_client", return_value=client):
        result = CliRunner().invoke(
            cli, ["git", "push", "--branch", "aq/t1", "--expected-remote-oid", oid]
        )
    assert result.exit_code == 0, result.output
    assert client.execute.await_args.args == (
        "git_push", {"branch": "aq/t1", "expected_remote_oid": oid}
    )


async def test_old_custom_worker_profile_reports_and_merges_missing_grants(tmp_path):
    root = Path(__file__).resolve().parents[1]
    profile_id = "worker-codex"
    shipped = (
        root / "src" / "profiles" / "defaults" / profile_id / "profile.md"
    ).read_text(encoding="utf-8")
    old = shipped.replace('    "git_create_pr",\n', '').replace('    "git_push",\n', '')
    old += "\n## Operator notes\nKeep this customization.\n"
    data_dir = str(tmp_path / "data")
    vault_path = Path(vault_profile_path(data_dir, profile_id))
    vault_path.parent.mkdir(parents=True)
    vault_path.write_text(old, encoding="utf-8")
    drift = diff_profile(profile_id, data_dir)
    assert drift.missing_grants["plugin_tools"] == ["git_create_pr", "git_push"]
    result = merge_profile_grants(data_dir, profile_id)
    assert result["added"]["plugin_tools"] == ["git_create_pr", "git_push"]
    merged = vault_path.read_text(encoding="utf-8")
    assert "## Operator notes\nKeep this customization." in merged
    assert diff_profile(profile_id, data_dir).missing_grants == {}


@pytest.mark.parametrize('command', [
    'integration_resolve_conflict', 'integration_push_conflict_resolution',
])
@pytest.mark.parametrize('assignment', ['parent', 'batch', 'inactive', 'ordinary'])
async def test_conflict_commands_require_live_parent_repair(env, monkeypatch, command, assignment):
    from unittest.mock import AsyncMock

    db, scope = env
    repair = None if assignment == 'ordinary' else {
        'active': assignment != 'inactive', 'writer_kind': 'repair_delegate',
        'target_kind': assignment,
    }
    lookup = AsyncMock(return_value=repair)
    monkeypatch.setattr(db, 'get_repair_filing_scope', lookup)
    args = {'intent_id': 'intent', 'fence': {}}
    result = await check_request_scope(command, args, scope, db=db)
    assert (result is None) == (assignment == 'parent')
    assert args == {'intent_id': 'intent', 'fence': {}}
    lookup.assert_awaited_once_with('t1', session_id='s1')
    await db.update_task('t1', status=TaskStatus.COMPLETED)
    assert await check_request_scope(command, args, scope, db=db) is not None
