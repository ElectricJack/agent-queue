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
from unittest.mock import ANY, AsyncMock, create_autospec

import asyncio

import pytest

from src.api.auth import RequestScope
from src.api.scope import (
    _WORKER_GIT_COMMANDS,
    check_request_scope,
    worker_branches_for_session,
)
from src.config import AppConfig, DatabaseConfig, DiscordConfig
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
async def env(tmp_path, request, reuse_database):
    db = await reuse_database("worker-git.db")
    await db.create_project(Project(id="p", name="p", repo_default_branch="main"))
    await db.create_project(Project(id="other", name="other"))
    await db.upsert_profile(AgentProfile(
        id="coder", name="coder", harness="claude", needs_workspace=False,
        default_class="standard-low",
    ))
    await db.create_agent(Agent(id="a1", name="a1", profile_id="coder"))
    await db.create_task(Task(
        id="t1", project_id="p", title="work", description="work",
        status=TaskStatus.IN_PROGRESS, profile_id="coder",
        route_source="legacy", assigned_agent_id="a1",
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
        event_bus=w.handler._bus, project_id="p", identity_policy=ANY,
        inherited_oids=[],  # no source-CI repair head on this branch
    )
    # The worker's publication is held to the project's Git identity.
    policy = w.git.apush_validated_delivery.await_args.kwargs["identity_policy"]
    assert policy.enforce and policy.source == "fallback"
    w.git.apush_branch.assert_not_awaited()
    w.git.aget_current_branch.assert_not_awaited()


@pytest.mark.parametrize("command", ["git_push", "push_branch"])
async def test_worker_lease_reaches_the_validated_delivery_push(worker_git, command):
    w = worker_git
    oid = "a" * 40
    result = await w.handler.execute(command, w.scoped(expected_remote_oid=oid))

    assert result["oid"] == _TIP
    assert w.git.apush_validated_delivery.await_args.kwargs["expected_remote_oid"] == oid


@pytest.mark.parametrize("command", ["git_push", "push_branch"])
async def test_worker_publication_advances_checkpoint_once_per_published_tip(worker_git, command):
    from sqlalchemy import insert

    from src.database.tables import task_branch_origins, task_integration_checkpoints
    from src.git.manager import GitError

    w = worker_git
    branch = "aq/calm-ember-48"
    base = "a" * 40
    await w.db.create_repo(RepoConfig(
        id="repo", project_id="p", source_type=RepoSourceType.CLONE, url=_REPOSITORY,
    ))
    await w.db.update_task("t1", repo_id="repo")
    async with w.db.immediate() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id="origin", task_id="t1", repository_id="repo", parent_ref="main",
            base_sha=base, creation_generation=0, reserved=True, materialized=True,
            created_at=1.0, materialized_at=1.0,
        ))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="t1", repository_id="repo", branch=branch, generation=0,
            checkpoint_sha=base, verified_sha=base, verified_generation=0,
            updated_at=1.0,
        ))
    for _ in range(2):
        result = await w.handler.execute(command, w.scoped())
        assert result["oid"] == _TIP
        checkpoint = await w.db.get_integration_checkpoint("t1")
        assert checkpoint["checkpoint_sha"] == _TIP
        assert checkpoint["generation"] == checkpoint["version"] == 1
        assert checkpoint["verified_sha"] is checkpoint["verified_generation"] is None

    # A deliberate leased rewrite is another publication, not stale generation 0.
    rewritten = "d" * 40
    w.git.apush_validated_delivery.return_value = rewritten
    await w.handler.execute(command, w.scoped(expected_remote_oid=_TIP))
    checkpoint = await w.db.get_integration_checkpoint("t1")
    assert checkpoint["checkpoint_sha"] == rewritten
    assert checkpoint["generation"] == 2
    origin = await w.db.get_task_branch_origin_for_promotion("t1", "repo")
    assert origin["base_sha"] == base and origin["creation_generation"] == 0

    w.git.apush_validated_delivery.side_effect = GitError("remote moved")
    result = await w.handler.execute(command, w.scoped())
    assert "remote moved" in result["error"]
    assert await w.db.get_integration_checkpoint("t1") == checkpoint


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


async def test_audited_first_binding_enables_existing_worker_publication(worker_git, monkeypatch):
    w = worker_git
    await w.db.update_project("p", repo_url="")
    w.git.aget_remote_url.return_value = "https://github.com/evil/other.git"
    github = AsyncMock()
    monkeypatch.setattr(w.handler, "_github_client", lambda: github)
    bound = await w.handler.execute("bind_project_repository", {
        "project_id": "p", "repo_url": _REPOSITORY, "expected_repo_url": "",
        "reason": "authorize the repository created after init",
    })
    assert bound["success"]
    result = await w.handler.execute("git_push", w.scoped())
    assert "error" not in result
    assert w.git.apush_validated_delivery.await_args.kwargs["repository_url"] == f"{_REPOSITORY}.git"


async def test_binding_refuses_an_inflight_worker_push(worker_git, tmp_path, monkeypatch):
    w = worker_git
    await w.db.update_project("p", repo_url="")
    origin = tmp_path / "origin.git"
    origin.mkdir()
    w.git.aget_remote_url.return_value = str(origin)
    github = AsyncMock()
    monkeypatch.setattr(w.handler, "_github_client", lambda: github)
    started, finish = asyncio.Event(), asyncio.Event()

    async def push(*args, **kwargs):
        started.set()
        await finish.wait()
        return _TIP

    w.git.apush_validated_delivery.side_effect = push
    running = asyncio.create_task(w.handler.execute("git_push", w.scoped()))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        bound = await w.handler.execute("bind_project_repository", {
            "project_id": "p", "repo_url": _REPOSITORY, "expected_repo_url": "",
            "reason": "authorize repository",
        })
        assert bound["error_code"] == "repository_publication_busy"
        assert (await w.db.get_project("p")).repo_url == ""
    finally:
        finish.set()
        await running


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
    import os
    import subprocess

    from src.git.identity import FALLBACK_IDENTITY

    # A worker commits as its project's resolved identity (here the unset
    # install's fallback): the session env carries it, and publishing checks it.
    git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    git_env.update(FALLBACK_IDENTITY.env())

    def git(*args: str, cwd: Path = tmp_path) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, env=git_env, check=True, capture_output=True, text=True,
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


@pytest.mark.parametrize("command", [
    "integration_resolve_conflict", "integration_push_conflict_resolution",
    "integration_resolve_candidate_member",
])
@pytest.mark.parametrize('assignment', ['parent', 'batch', 'inactive', 'ordinary'])
async def test_retired_conflict_commands_are_denied_for_every_assignment(env, monkeypatch, command, assignment):
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
    assert result == f"out of scope: {command}"
    assert args == {'intent_id': 'intent', 'fence': {}}
    lookup.assert_not_awaited()
    await db.update_task('t1', status=TaskStatus.COMPLETED)
    assert await check_request_scope(command, args, scope, db=db) is not None


async def test_worker_pull_updates_only_its_checkout(worker_git):
    w = worker_git
    w.git.aget_current_branch.return_value = "aq/calm-ember-48"
    result = await w.handler.execute("git_pull", w.scoped(branch="main"))
    assert "error" not in result
    w.git.apull_branch.assert_awaited_once_with(
        w.work_dir, "main", repository_url=_REPOSITORY,
    )


async def test_worker_pull_cannot_change_main_checkout(worker_git):
    w = worker_git
    w.git.aget_current_branch.return_value = "main"
    result = await w.handler.execute("git_pull", w.scoped(branch="main"))
    assert "not this session's task branch" in result["error"]
    w.git.apull_branch.assert_not_awaited()


async def test_worker_pull_scope_is_own_workspace(env):
    db, scope = env
    assert await check_request_scope("git_pull", {"branch": "main"}, scope, db=db) is None
    assert await check_request_scope(
        "git_pull", {"workspace": "base"}, scope, db=db,
    ) == "out of scope: workspace mismatch"


@pytest.mark.parametrize("event,invoke_ai,allowed", [
    ({}, False, True),
    ({"project_id": "other"}, False, False),
    ({"task_id": "other"}, False, False),
    ({}, True, False),
])
async def test_worker_dry_run_fences_synthetic_context(worker_git, event, invoke_ai, allowed):
    from unittest.mock import AsyncMock, patch

    w = worker_git
    w.handler.config.playbooks.enabled = True
    with patch.object(w.handler, "_run_v2_artifact", new=AsyncMock(return_value={"dry_run": True})) as run:
        result = await w.handler.execute("dry_run_playbook", w.scoped(
            playbook_id="routing", event=event, invoke_ai=invoke_ai,
        ))
    if allowed:
        assert result == {"dry_run": True}
        assert run.await_args.args[1]["project_id"] == "p"
        assert run.await_args.args[1]["task_id"] == "t1"
        assert run.await_args.kwargs == {"dry_run": True, "invoke_ai": False}
    else:
        assert "error" in result
        run.assert_not_awaited()


@pytest.fixture
async def delegated_git(worker_git):
    from dataclasses import replace

    from src.integration.publication_delegations import PublicationDelegations

    w = worker_git
    await w.db.create_repo(RepoConfig(
        id="delegated-repo", project_id="p", source_type=RepoSourceType.CLONE,
        url=_REPOSITORY,
    ))
    await w.db.update_project("p", hierarchical_integration_mode="train",
                              integration_repository_id="delegated-repo")
    await w.db.update_task("t1", repo_id="delegated-repo")
    w.handler.config.integration.git_first = "active"
    w.git.arev_parse.return_value = _TIP
    w.git.ais_ancestor.return_value = True
    original = w.scoped
    w.scoped = lambda **args: {
        **original(**args),
        "_scope": {**original()["_scope"], "session_instance_token": "instance-a1"},
    }
    w.scope = replace(RequestScope(kind="session", session_id="s1", project_id="p",
                                  task_id=original()["_scope"]["task_id"]),
                      session_instance_token="instance-a1")
    w.request = dict(
        id="delegate-1", project_id="p", repository_id="delegated-repo", task_id="t1",
        session_id="s1", claim_epoch=0, target_branch="aq/epic/aggregate",
        expected_target_oid="a" * 40, expected_owner_fence=0, reason="aggregate siblings",
    )
    w.service = PublicationDelegations(w.db, active=lambda: True)
    return w


async def _issue(w, **changes):
    return await w.handler.execute("integration_delegate_publication", {**w.request, **changes})


@pytest.fixture
async def assigned_batch_git(delegated_git):
    from src.git.manager import RemoteRefResult, RemoteRefState
    from src.integration.batches import Batch, BatchMember, BatchStore, candidate_ref

    w = delegated_git
    w.batch = await BatchStore(w.db).freeze(
        Batch("manual-repair", "p", "delegated-repo", "refs/heads/aq/epic/aggregate"),
        (BatchMember(task_id="source", source_sha="a" * 40, source_base_sha="b" * 40),),
        trees={"source": "c" * 40},
    )
    w.batch_branch = candidate_ref(w.batch.id).removeprefix("refs/heads/")
    assigned = await w.handler.execute("task_set", {"task_id": "t1", "repair_batch": w.batch.id})
    assert assigned["success"], assigned
    async def remote_ref(_checkout, ref, **_kwargs):
        oid = "b" * 40 if ref == w.batch.target_ref.removeprefix("refs/heads/") else "a" * 40
        return RemoteRefResult(RemoteRefState.PRESENT, oid=oid)

    w.git.als_remote_ref.side_effect = remote_ref
    return w


@pytest.mark.parametrize("command", ["git_push", "push_branch"])
@pytest.mark.parametrize("explicit", [False, True])
async def test_assigned_batch_fixer_publishes_candidate_without_renaming_task(
    assigned_batch_git, command, explicit,
):
    w = assigned_batch_git
    args = {_BRANCH_KEY[command]: w.batch_branch} if explicit else {}
    assert await check_request_scope(command, dict(args), w.scope, db=w.db) is None
    result = await w.handler.execute(command, w.scoped(**args))
    assert result.get("oid") == _TIP, result
    w.git.apush_validated_delivery.assert_awaited_once_with(
        w.work_dir, "a" * 40, _TIP, w.batch_branch,
        expected_remote_oid="a" * 40, repository_url=_REPOSITORY,
        event_bus=w.handler._bus, project_id="p", identity_policy=ANY,
        inherited_oids=("a" * 40, "b" * 40),
    )
    w.git.als_remote_ref.assert_any_await(w.work_dir, w.batch.target_ref.removeprefix("refs/heads/"),
                                        repository_url=_REPOSITORY)
    w.git._arun.assert_any_await(["cat-file", "-e", "b" * 40 + "^{commit}"], cwd=w.work_dir)
    assert (await w.db.get_task("t1")).branch_name == "aq/calm-ember-48"
    assert (await w.db.get_task_meta("t1", "integration_batch_repair"))["batch_id"] == w.batch.id


async def test_batch_assignment_cannot_be_self_granted_or_spoofed(delegated_git):
    w = delegated_git
    for args in ({"repair_batch": "manual-repair"},
                 {"meta": {"integration_batch_repair": {"batch_id": "manual-repair"}}}):
        result = await w.handler.execute("task_set", w.scoped(task_id="t1", **args))
        assert result.get("success") is False, result
    assert await w.db.get_task_meta("t1", "integration_batch_repair") is None
    await w.db.update_task("t1", description="Repair aq/batches/arbitrary directly and push there")
    assert await check_request_scope(
        "git_push", {"branch": "aq/batches/arbitrary"}, w.scope, db=w.db,
    ) == "out of scope: branch mismatch"


async def test_batch_assignment_rejects_foreign_repository_and_reassignment_revokes_old_ref(assigned_batch_git):
    from src.integration.batches import Batch, BatchMember, BatchStore, candidate_ref

    w = assigned_batch_git
    store = BatchStore(w.db)
    await store.freeze(Batch("next-repair", "p", "delegated-repo", "refs/heads/aq/epic/next"),
                       (BatchMember("source-next", "a" * 40, "b" * 40),),
                       trees={"source-next": "c" * 40})
    for args in ({"repair_batch": "missing"}, {"repair_batch": ""}):
        result = await w.handler.execute("task_set", {"task_id": "t1", **args})
        assert result.get("success") is False, result
    await w.db.update_task("t1", repo_id=None)
    result = await w.handler.execute("task_set", {"task_id": "t1", "repair_batch": w.batch.id})
    assert result.get("success") is False
    await w.db.update_task("t1", repo_id="delegated-repo")
    result = await w.handler.execute("task_set", {"task_id": "t1", "repair_batch": "next-repair"})
    assert result["success"], result
    assert await check_request_scope(
        "git_push", {"branch": w.batch_branch}, w.scope, db=w.db,
    ) == "out of scope: branch mismatch"
    assert await check_request_scope(
        "git_push", {"branch": candidate_ref("next-repair").removeprefix("refs/heads/")},
        w.scope, db=w.db,
    ) is None


@pytest.mark.parametrize("command,args", [
    ("git_push", {"branch": "main"}),
    ("git_push", {"branch": "aq/batches/other"}),
    ("git_create_pr", {"title": "fix"}),
])
async def test_batch_assignment_grants_only_candidate_push(assigned_batch_git, command, args):
    w = assigned_batch_git
    if command == "git_create_pr":
        args = {**args, "branch": w.batch_branch}
    assert await check_request_scope(command, dict(args), w.scope, db=w.db) == "out of scope: branch mismatch"
    result = await w.handler.execute(command, w.scoped(**args))
    assert "not this session's task branch" in result["error"]
    _assert_no_network(w.git)


@pytest.mark.parametrize("failure", [
    "diverged", "remote_moved", "missing", "unavailable", "writer", "aborted", "claim", "inactive",
    "target_missing", "target_unavailable", "target_object_missing",
])
async def test_assigned_batch_push_refuses_stale_or_unsafe_publication(assigned_batch_git, failure):
    from sqlalchemy import update

    from src.database.tables import integration_batches
    from src.git.manager import GitError, RemoteRefResult, RemoteRefState
    from src.integration.lock import BranchLock
    from src.integration.models import BranchKey

    w = assigned_batch_git
    args = {"branch": w.batch_branch}
    if failure == "diverged":
        w.git.ais_ancestor.return_value = False
    elif failure == "remote_moved":
        args["expected_remote_oid"] = "d" * 40
    elif failure in {"missing", "unavailable"}:
        w.git.als_remote_ref.side_effect = None
        w.git.als_remote_ref.return_value = RemoteRefResult(
            RemoteRefState.ABSENT if failure == "missing" else RemoteRefState.ERROR,
        )
    elif failure in {"target_missing", "target_unavailable"}:
        observe = w.git.als_remote_ref.side_effect

        async def target_unavailable(checkout, ref, **kwargs):
            if ref == w.batch.target_ref.removeprefix("refs/heads/"):
                state = RemoteRefState.ABSENT if failure == "target_missing" else RemoteRefState.ERROR
                return RemoteRefResult(state)
            return await observe(checkout, ref, **kwargs)

        w.git.als_remote_ref.side_effect = target_unavailable
    elif failure == "target_object_missing":
        w.git._arun.side_effect = GitError("target commit unavailable locally")
    elif failure == "writer":
        await BranchLock(w.db).acquire(
            BranchKey(repository_id="delegated-repo", branch=w.batch_branch), "another-task",
        )
    elif failure == "aborted":
        async with w.db.immediate() as conn:
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == w.batch.id,
            ).values(intent="aborted"))
    elif failure == "claim":
        await w.db.update_task("t1", claim_epoch=1)
    elif failure == "inactive":
        w.handler.config.integration.git_first = "shadow"
    result = await w.handler.execute("git_push", w.scoped(**args))
    assert "error" in result, result
    _assert_no_network(w.git)


async def test_assigned_batch_push_rechecks_claim_after_policy_resolution(assigned_batch_git, monkeypatch):
    from src.plugins.internal.git import GitPlugin

    w = assigned_batch_git
    original = GitPlugin._publish_policy

    async def lose_claim(*args, **kwargs):
        policy = await original(*args, **kwargs)
        await w.db.update_task("t1", claim_epoch=1)
        return policy

    monkeypatch.setattr(GitPlugin, "_publish_policy", lose_claim)
    result = await w.handler.execute("git_push", w.scoped(branch=w.batch_branch))
    assert "current held claim" in result["error"], result
    _assert_no_network(w.git)


@pytest.mark.parametrize("command", ["git_push", "push_branch"])
async def test_delegated_publication_uses_pinned_source_and_consumes_audited_grant(
    delegated_git, command,
):
    w = delegated_git
    issued = await _issue(w)
    assert issued["success"], issued
    assert (await _issue(w))["owner_fence"] == issued["owner_fence"]
    args = {_BRANCH_KEY[command]: w.request["target_branch"],
            "publication_delegation_id": "delegate-1"}
    assert await check_request_scope(command, args, w.scope, db=w.db) is None
    result = await w.handler.execute(command, w.scoped(**args))
    assert "error" not in result, result
    assert result["oid"] == _TIP
    w.git.apush_validated_delivery.assert_awaited_once_with(
        w.work_dir, "a" * 40, _TIP, "aq/epic/aggregate",
        expected_remote_oid="a" * 40, repository_url=_REPOSITORY,
        event_bus=w.handler._bus, project_id="p", identity_policy=ANY, inherited_oids=[],
    )
    assert w.git.apush_validated_delivery.await_args.kwargs["identity_policy"].enforce
    async with w.db._engine.connect() as conn:
        row = await w.service._grant(conn, "delegate-1")
    assert row["published_oid"] == _TIP and row["published_at"] is not None
    assert row["issued_by"] == "human:local-operator"
    assert (await w.db.get_task("t1")).branch_name == "aq/calm-ember-48"
    assert (await w.service.locks.get(w.service._target(row))).holder is None
    w.git.apush_validated_delivery.reset_mock()
    reused = await w.handler.execute(command, w.scoped(**args))
    assert "delegation" in reused["error"]
    _assert_no_network(w.git)


@pytest.mark.parametrize("change", [
    "revoked", "expired", "claim_epoch", "session_instance", "source_branch", "repository",
    "repository_url",
    "owner", "target", "expected_oid", "worker_instance", "inactive", "nonancestor",
])
async def test_delegated_publication_rechecks_every_binding_before_network(delegated_git, change):
    from sqlalchemy import update

    from src.database import tables as t

    w = delegated_git
    assert (await _issue(w))["success"]
    args = dict(branch=w.request["target_branch"], publication_delegation_id="delegate-1")
    # Dispatch has already accepted this grant. Native execution must freshly refuse it.
    assert await check_request_scope("git_push", dict(args), w.scope, db=w.db) is None
    if change == "revoked":
        result = await w.handler.execute("integration_revoke_publication", dict(
            id="delegate-1", project_id="p", reason="cancel delegation",
        ))
        assert result["success"], result
    elif change == "claim_epoch":
        await w.db.update_task("t1", claim_epoch=1)
    elif change == "session_instance":
        await w.db.update_session("s1", instance_token="replacement")
    elif change == "source_branch":
        await w.db.update_task("t1", branch_name="aq/rebound")
    elif change == "repository":
        await w.db.create_repo(RepoConfig(
            id="replacement-repo", project_id="p", source_type=RepoSourceType.CLONE,
            url=_REPOSITORY,
        ))
        await w.db.update_project("p", integration_repository_id="replacement-repo")
    elif change == "repository_url":
        await w.db.update_repo("delegated-repo", url="https://github.com/other/repository")
    elif change in {"expired", "owner"}:
        async with w.db.immediate() as conn:
            await conn.execute(update(t.integration_branch_owners).where(
                t.integration_branch_owners.c.ref == w.request["target_branch"],
            ).values(**({"expires_at": time.time() - 1} if change == "expired"
                        else {"fence": 99, "holder": "another-writer"})))
    elif change == "target":
        args["branch"] = "aq/epic/another"
    elif change == "expected_oid":
        args["expected_remote_oid"] = "b" * 40
    elif change == "worker_instance":
        args["_scope"] = {**w.scoped()["_scope"], "session_instance_token": "old-instance"}
    elif change == "inactive":
        w.handler.config.integration.git_first = "shadow"
    elif change == "nonancestor":
        w.git.ais_ancestor.return_value = False
    result = await w.handler.execute(
        "git_push", args if change == "worker_instance" else w.scoped(**args),
    )
    assert "error" in result, result
    _assert_no_network(w.git)


@pytest.mark.parametrize("change", ["worker", "protected", "busy", "fence", "retry", "shadow"])
async def test_delegation_cannot_expand_authority_or_take_a_live_writer(delegated_git, change):
    from src.integration.models import BranchKey

    w = delegated_git
    args = dict(w.request)
    if change == "worker":
        args = w.scoped(**args)
    elif change == "protected":
        await w.db.update_project("p", promotion_flow=[dict(
            id="release", source="dev", target="production",
        )])
        args["target_branch"] = "refs/heads/production"
    elif change == "busy":
        await w.service.locks.acquire(
            BranchKey(repository_id="delegated-repo", branch=args["target_branch"]),
            "integration-train", role="integration",
        )
        args["expected_owner_fence"] = 1
    elif change == "fence":
        args["expected_owner_fence"] = 99
    elif change == "retry":
        assert (await _issue(w))["success"]
        args["ttl_seconds"] = 60
    elif change == "shadow":
        w.handler.config.integration.git_first = "shadow"
    result = await w.handler.execute("integration_delegate_publication", args)
    assert not result.get("success", False), result
    _assert_no_network(w.git)


@pytest.mark.parametrize("scenario", ["success", "remote_moved", "reserved", "identity", "diverged"])
async def test_delegated_publication_real_git_preserves_delivery_gates(
    delegated_git, tmp_path, monkeypatch, scenario,
):
    from src.git.identity import GitIdentity, PublishPolicy
    from src.git.manager import GitError
    from tests.test_delivery_consumers import Origin, git

    w = delegated_git
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "Tester")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "tester@example.test")
    origin = Origin(tmp_path)
    base = git(origin.clone, "rev-parse", "HEAD")
    target = w.request["target_branch"]
    git(origin.clone, "push", "origin", f"main:{target}")
    await w.db.update_repo("delegated-repo", url=origin.url)
    if scenario == "diverged":
        git(origin.clone, "checkout", "--orphan", "unrelated")
    else:
        git(origin.clone, "checkout", "-b", "aq/calm-ember-48")
    path = origin.clone / (".aq/claim.json" if scenario == "reserved" else "work.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("work\n")
    git(origin.clone, "add", "-f", str(path))
    git(origin.clone, "commit", "-m", "work")
    tip = git(origin.clone, "rev-parse", "HEAD")
    if scenario == "diverged":
        git(origin.clone, "branch", "aq/calm-ember-48")
    issued = await _issue(w, expected_target_oid=base)
    assert issued["success"], issued
    remote_before = base
    if scenario == "remote_moved":
        git(origin.clone, "checkout", "main")
        (origin.clone / "external.txt").write_text("external\n")
        git(origin.clone, "add", ".")
        git(origin.clone, "commit", "-m", "external writer")
        git(origin.clone, "push", "origin", f"HEAD:{target}")
        remote_before = git(origin.clone, "rev-parse", "HEAD")
    identity = GitIdentity("Other" if scenario == "identity" else "Tester",
                           "other@example.test" if scenario == "identity" else "tester@example.test")
    policy = PublishPolicy(identity=identity, source="project", allowed=frozenset({identity.digest}))
    async def publish():
        return await w.service.publish(
            "delegate-1", w.scope, target, git=GitManager(), checkout_path=str(origin.clone),
            policy=policy,
        )
    if scenario == "success":
        assert await publish() == tip
    else:
        with pytest.raises(GitError):
            await publish()
    assert git(origin.url, "rev-parse", target) == (tip if scenario == "success" else remote_before)


async def test_delegation_revocation_serializes_with_inflight_publication(delegated_git):
    from src.commands.contracts.integration import IntegrationRevokePublicationArgs

    w = delegated_git
    assert (await _issue(w))["success"]
    entered, release = asyncio.Event(), asyncio.Event()
    async def transport(*args, **kwargs):
        entered.set()
        await release.wait()
        return _TIP
    w.git.apush_validated_delivery.side_effect = transport
    publication = asyncio.create_task(w.service.publish(
        "delegate-1", w.scope, w.request["target_branch"], git=w.git,
        checkout_path=w.work_dir, policy=None,
    ))
    await asyncio.wait_for(entered.wait(), timeout=10)
    revocation = asyncio.create_task(w.service.revoke(IntegrationRevokePublicationArgs(
        id="delegate-1", project_id="p", reason="cancel",
    )))
    try:
        await asyncio.sleep(0.05)
        assert not revocation.done()
    finally:
        release.set()
    assert await publication == _TIP
    row = await revocation
    assert row["published_oid"] == _TIP and row["revoked_at"] is None


@pytest.mark.parametrize("stale", [False, True])
async def test_only_current_named_supervisor_can_issue_delegation(delegated_git, stale):
    from dataclasses import asdict

    w = delegated_git
    await w.db.upsert_profile(AgentProfile(
        id="supervisor", name="supervisor", harness="claude", needs_workspace=False,
        default_class="standard-low",
    ))
    await w.db.create_session(SessionRecord(
        id="supervisor-p", project_id="p", profile_id="supervisor", harness="claude",
        provider="fake", name="supervisor-p", lifecycle="named", state="running",
        work_dir=w.work_dir, epoch="test", instance_token="supervisor-current", started_at=time.time(),
    ))
    scope = RequestScope(
        kind="session", project_id="p", session_id="supervisor-p", elevated=True,
        session_instance_token="stale" if stale else "supervisor-current",
    )
    args = dict(w.request)
    assert await check_request_scope("integration_delegate_publication", args, scope, db=w.db) is None
    result = await w.handler.execute("integration_delegate_publication", {**args, "_scope": asdict(scope)})
    assert result.get("success", False) is (not stale), result
