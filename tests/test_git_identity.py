"""Deterministic Git commit identity (docs/specs/git-identity.md).

One resolver -- project override > installation default > documented
fallback -- feeds every commit AQ makes: worker session env, the daemon's
GitManager, checkpoints and integration commits.  These tests pin the
resolution, its validation, the surfaces that edit it, the pool's refresh
rule and the publishing check.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from sqlalchemy import insert, text

from src.commands.handler import CommandHandler
from src.commands.principal import (
    DENY_ALL,
    ExecutionPrincipal,
    PrincipalKind,
    principal_context,
)
from src.config import AppConfig, DatabaseConfig, DiscordConfig, GitIdentityConfig, load_config
from src.database import Database
from src.database.tables import integration_source_ci
from src.git.identity import (
    FALLBACK_IDENTITY,
    LEDGER_IDENTITY,
    GitIdentity,
    GitIdentityError,
    identity_digest,
    legacy_pool_identity,
    resolve_git_identity,
    validate_git_email,
    validate_git_name,
)
from src.git.manager import GitManager, commit_identity
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

JACK = GitIdentity("Jack Example", "jack@example.com")
ACME = GitIdentity("Acme Bot", "bot@acme.test")


def _config(identity: GitIdentity | None = None, **kw) -> AppConfig:
    config = AppConfig(**kw)
    if identity is not None:
        config.git_identity = GitIdentityConfig(identity.name, identity.email, "manual")
    return config


def _project(identity: GitIdentity | None = None, pid: str = "p"):
    return SimpleNamespace(
        id=pid,
        git_identity_name=identity.name if identity else None,
        git_identity_email=identity.email if identity else None,
    )


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize("name", ["Jack Kern", "José Núñez", "O'Brien Bot", "aq worker 2"])
def test_valid_names(name):
    assert validate_git_name(f"  {name} ") == name


@pytest.mark.parametrize(
    "name",
    ["", "   ", "Jack\nKern", "Jack\rKern", "Jack\tKern", "a<b", "a>b", "Jack.", ".Jack",
     "Jack,", "x" * 201, "zero\u200bwidth", 7],
)
def test_unsafe_names_are_refused(name):
    with pytest.raises(GitIdentityError) as exc:
        validate_git_name(name)
    assert exc.value.field == "name"


@pytest.mark.parametrize(
    "email",
    ["jack@example.com", "123+jack@users.noreply.github.com", "agent-queue@localhost"],
)
def test_valid_emails(email):
    assert validate_git_email(email) == email


@pytest.mark.parametrize(
    "email",
    ["", "jack", "jack@", "@example.com", "a b@example.com", "a@b@c", "<a@b>", "a@b\nc",
     "jack@example.com.", "x" * 250 + "@a.bc"],
)
def test_unsafe_emails_are_refused(email):
    with pytest.raises(GitIdentityError) as exc:
        validate_git_email(email)
    assert exc.value.field == "email"


def test_digest_ignores_email_case_and_matches_identity():
    assert identity_digest("Jack Example", "JACK@example.com") == JACK.digest
    assert identity_digest("Jack", "jack@example.com") != JACK.digest


def test_identity_env_and_config_args():
    assert JACK.env() == {
        "GIT_AUTHOR_NAME": "Jack Example", "GIT_AUTHOR_EMAIL": "jack@example.com",
        "GIT_COMMITTER_NAME": "Jack Example", "GIT_COMMITTER_EMAIL": "jack@example.com",
    }
    assert JACK.config_args() == [
        "-c", "user.name=Jack Example", "-c", "user.email=jack@example.com",
    ]


# --- resolution --------------------------------------------------------------


def test_unset_install_resolves_to_the_documented_fallback():
    resolved = resolve_git_identity(_config(), _project())
    assert resolved.identity == FALLBACK_IDENTITY == GitIdentity(
        "Agent Queue", "agent-queue@localhost"
    )
    assert resolved.source == "fallback"
    assert not resolved.configured
    assert resolved.as_dict()["installation"] is None


def test_projects_inherit_the_installation_default():
    resolved = resolve_git_identity(_config(JACK), _project())
    assert (resolved.identity, resolved.source, resolved.configured) == (
        JACK, "installation", True
    )


def test_project_override_wins_and_never_mixes_fields():
    resolved = resolve_git_identity(_config(JACK), _project(ACME))
    assert resolved.identity == ACME
    assert resolved.source == "project"
    assert resolved.as_dict()["installation"] == JACK.as_dict()
    half = SimpleNamespace(git_identity_name="Half", git_identity_email=None)
    assert resolve_git_identity(_config(JACK), half).identity == JACK


def test_isolated_projects_resolve_independently():
    config = _config(JACK)
    a, b = _project(ACME, "a"), _project(None, "b")
    assert resolve_git_identity(config, a).identity == ACME
    assert resolve_git_identity(config, b).identity == JACK


def test_an_invalid_stored_identity_never_reaches_git():
    config = AppConfig()
    config.git_identity = GitIdentityConfig("bad<name", "x@y")
    assert resolve_git_identity(config).identity == FALLBACK_IDENTITY


def test_mocks_resolve_to_the_fallback():
    assert resolve_git_identity(MagicMock(), MagicMock()).identity == FALLBACK_IDENTITY


def test_ledger_and_legacy_identities_are_the_historical_values():
    assert LEDGER_IDENTITY == GitIdentity("Agent Queue", "aq@localhost")
    assert legacy_pool_identity("worker") == ("aq worker", "worker@agent-queue.local")


# --- config -------------------------------------------------------------------


_BASE_CONFIG = {
    "messaging_platform": "none",
    "database": {"url": "postgresql://aq:aq@localhost:1/aq"},
}


def _write_config(tmp_path: Path, body: dict) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(body))
    return str(path)


def test_config_section_loads_and_hot_reloads(tmp_path):
    from src.config import HOT_RELOADABLE_SECTIONS

    config = load_config(_write_config(tmp_path, {
        **_BASE_CONFIG,
        "data_dir": str(tmp_path / "d"),
        "git_identity": {"name": "Jack Example", "email": "jack@example.com",
                         "source": "gh:jack"},
    }))
    assert config.git_identity == GitIdentityConfig(
        "Jack Example", "jack@example.com", "gh:jack"
    )
    assert "git_identity" in HOT_RELOADABLE_SECTIONS


@pytest.mark.parametrize("section,field", [
    ({"name": "Jack"}, "email"),
    ({"email": "jack@example.com"}, "name"),
    ({"name": "Jack\nX", "email": "jack@example.com"}, "name"),
    ({"name": "Jack", "email": "not-an-email"}, "email"),
])
def test_config_validation_names_the_bad_field(section, field):
    errors = GitIdentityConfig(**section).validate()
    assert [(e.section, e.field) for e in errors] == [("git_identity", field)]


def test_unset_section_is_valid():
    assert GitIdentityConfig().validate() == []


# --- GitManager ------------------------------------------------------------------


def test_only_commit_writing_subcommands_receive_an_identity():
    git = GitManager()
    git.set_identity_resolver(lambda project: JACK)
    for args in (["commit", "-m", "x"], ["-c", "a=b", "merge", "x"], ["rebase", "main"],
                 ["cherry-pick", "x"], ["commit-tree", "t"], ["-C", "/x", "tag", "-a", "v"]):
        assert git.commit_identity_env(args) == JACK.env(), args
    for args in (["status"], ["fetch", "origin"], ["push"], ["-c", "user.name=x", "log"]):
        assert git.commit_identity_env(args) == {}, args


def test_explicit_identity_and_scope_precedence():
    git = GitManager()
    assert git.commit_identity_env(["commit"]) == {}  # standalone: Git's own rules
    git.set_identity_resolver(lambda project: ACME if project else JACK)
    assert git.commit_identity_env(["commit"]) == JACK.env()
    with commit_identity(ACME):
        assert git.commit_identity_env(["commit"]) == ACME.env()
        assert git.resolve_commit_identity() == ACME
    assert git.resolve_commit_identity(_project(ACME)) == ACME
    assert GitManager().resolve_commit_identity() == FALLBACK_IDENTITY


def _git(cwd, *args, env=None):
    full = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    full.update(env or {})
    return subprocess.run(
        ["git", *args], cwd=cwd, env=full, check=True, capture_output=True, text=True
    ).stdout.strip()


def _ident(cwd, rev="HEAD"):
    return _git(cwd, "log", "-1", "--format=%an <%ae>|%cn <%ce>", rev)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository whose daemon env carries an unrelated identity."""
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    third = {"GIT_AUTHOR_NAME": "Third Party", "GIT_AUTHOR_EMAIL": "third@up.test",
             "GIT_COMMITTER_NAME": "Third Party", "GIT_COMMITTER_EMAIL": "third@up.test"}
    _git(path, "commit", "-q", "--allow-empty", "-m", "base", env=third)
    _git(path, "checkout", "-q", "-b", "feature")
    (path / "f").write_text("f")
    _git(path, "add", "f")
    _git(path, "commit", "-q", "-m", "upstream work", env=third)
    _git(path, "checkout", "-q", "main")
    (path / "g").write_text("g")
    _git(path, "add", "g")
    _git(path, "commit", "-q", "-m", "main moves", env=third)
    return path


async def test_daemon_commits_use_the_resolved_identity_and_keep_authors(repo):
    git = GitManager()
    git._SUBPROCESS_ENV = {
        **git._SUBPROCESS_ENV,
        "GIT_AUTHOR_NAME": "Operator", "GIT_AUTHOR_EMAIL": "operator@home.test",
        "GIT_COMMITTER_NAME": "Operator", "GIT_COMMITTER_EMAIL": "operator@home.test",
    }
    git.set_identity_resolver(lambda project: JACK)
    (repo / "h").write_text("h")
    assert await git.acommit_all(str(repo), "daemon commit")
    assert _ident(repo) == "Jack Example <jack@example.com>|Jack Example <jack@example.com>"

    with commit_identity(ACME):
        await git._arun(["cherry-pick", "feature"], cwd=str(repo))
    # Upstream authorship survives; AQ is only the committer.
    assert _ident(repo) == "Third Party <third@up.test>|Acme Bot <bot@acme.test>"
    assert _ident(repo, "feature") == "Third Party <third@up.test>|Third Party <third@up.test>"


# --- session launch ---------------------------------------------------------------


def _harness():
    from src.sessions.harness_parser import Harness

    return Harness(id="claude", command="claude", provider="anthropic")


def test_pool_spec_injects_the_project_identity_over_any_supplied_git_env():
    from src.sessions.spec import SessionSpecBuilder

    builder = SessionSpecBuilder(_config(JACK))
    profile = SimpleNamespace(id="standard-high-claude", harness="claude")
    spec = builder.build_pool_spec(
        profile=profile, project=_project(ACME), agent_id="a", harness=_harness(),
        work_dir="/tmp/w", session_id="s", session_name="p-s", instance_token="t",
    )
    assert {k: spec.env[k] for k in ACME.env()} == ACME.env()
    assert spec.git_identity_digest == ACME.digest
    assert "agent-queue.local" not in " ".join(spec.env.values())

    inherited = builder.build_pool_spec(
        profile=profile, project=_project(None), agent_id="a", harness=_harness(),
        work_dir="/tmp/w", session_id="s", session_name="p-s", instance_token="t",
    )
    assert inherited.env["GIT_COMMITTER_EMAIL"] == JACK.email
    assert inherited.git_identity_digest == JACK.digest


def test_task_spec_identity_beats_extra_env():
    from src.sessions.spec import SessionSpecBuilder

    builder = SessionSpecBuilder(_config())
    task = SimpleNamespace(id="t1", project_id="p", intelligence_class=None)
    spec = builder.build_task_spec(
        task=task, profile=SimpleNamespace(id="w"), harness=_harness(), work_dir="/tmp/w",
        session_id="s", instance_token="t",
        extra_env={"GIT_AUTHOR_NAME": "Claude", "GIT_COMMITTER_EMAIL": "noreply@x.test"},
        git_identity=FALLBACK_IDENTITY,
    )
    assert {k: spec.env[k] for k in FALLBACK_IDENTITY.env()} == FALLBACK_IDENTITY.env()
    assert spec.git_identity_digest == FALLBACK_IDENTITY.digest


# --- database-backed surfaces --------------------------------------------------

_AGENT = ExecutionPrincipal(
    kind=PrincipalKind.SESSION, policy=DENY_ALL, session_id="s-agent", project_id="p"
)


@pytest.fixture
async def db():
    database = Database(lease_dsn("git-identity.db"))
    await database.initialize()
    yield database
    await database.close()


def _plain_handler(db, config):
    orch = MagicMock()
    orch.db = db
    orch.config = config
    orch._config_watcher = None
    return CommandHandler(orch, config)


async def test_project_override_inherit_and_reset_round_trip(db):
    await db.create_project(Project(id="p", name="p"))
    handler = _plain_handler(db, _config(JACK))

    shown = await handler._cmd_get_project({"project_id": "p"})
    assert (shown["git_identity_name"], shown["git_identity_email"]) == (None, None)
    assert shown["git_identity"]["source"] == "installation"
    assert shown["git_identity"]["email"] == JACK.email

    edited = await handler._cmd_edit_project({
        "project_id": "p", "git_identity_name": " Acme Bot ",
        "git_identity_email": "bot@acme.test",
    })
    assert set(edited["fields"]) == {"git_identity_name", "git_identity_email"}
    shown = await handler._cmd_get_project({"project_id": "p"})
    assert shown["git_identity"]["source"] == "project"
    assert shown["git_identity_name"] == "Acme Bot"

    # One field of an existing override may change alone.
    await handler._cmd_edit_project({"project_id": "p", "git_identity_email": "ci@acme.test"})
    assert (await db.get_project("p")).git_identity_email == "ci@acme.test"

    # Clearing one half is refused: overrides are pairs.
    half = await handler._cmd_edit_project({"project_id": "p", "git_identity_name": ""})
    assert half["error_code"] == "invalid_git_identity"
    bad = await handler._cmd_edit_project({"project_id": "p", "git_identity_name": "a<b"})
    assert bad["field"] == "git_identity_name"
    assert (await db.get_project("p")).git_identity_name == "Acme Bot"

    reset = await handler._cmd_edit_project(
        {"project_id": "p", "git_identity_name": "", "git_identity_email": ""}
    )
    assert "error" not in reset
    shown = await handler._cmd_get_project({"project_id": "p"})
    assert shown["git_identity_name"] is None
    assert shown["git_identity"]["source"] == "installation"


async def test_the_database_refuses_half_an_override(db):
    from sqlalchemy.exc import IntegrityError

    await db.create_project(Project(id="p", name="p"))
    with pytest.raises(IntegrityError, match="ck_projects_git_identity_pair"):
        await db.update_project("p", git_identity_name="Only Name")


async def test_agent_sessions_cannot_change_identity(db):
    await db.create_project(Project(id="p", name="p"))
    handler = _plain_handler(db, _config(JACK))
    with principal_context(_AGENT):
        project = await handler._cmd_edit_project({
            "project_id": "p", "git_identity_name": "Claude",
            "git_identity_email": "noreply@anthropic.com",
        })
        installation = await handler._cmd_set_git_identity(
            {"name": "Claude", "email": "noreply@anthropic.com"}
        )
    assert project["error_code"] == installation["error_code"] == "operator_only"
    assert (await db.get_project("p")).git_identity_name is None


async def test_installation_default_set_show_and_clear(db, tmp_path):
    path = _write_config(tmp_path, {**_BASE_CONFIG, "data_dir": str(tmp_path / "d")})
    config = load_config(path)
    await db.create_project(Project(id="p", name="p"))
    handler = _plain_handler(db, config)

    shown = await handler._cmd_get_git_identity({"project_id": "p"})
    assert shown["configured"] is False
    assert shown["effective"]["source"] == "fallback"
    assert shown["fallback"] == FALLBACK_IDENTITY.as_dict()

    refused = await handler._cmd_set_git_identity({"name": "Jack", "email": "jack"})
    assert refused["field"] == "email"
    assert "git_identity" not in yaml.safe_load(Path(path).read_text())

    done = await handler._cmd_set_git_identity(
        {"name": "Jack Example", "email": "jack@example.com", "source": "gh:jack"}
    )
    assert done["changed"] and done["installation"] == JACK.as_dict()
    assert yaml.safe_load(Path(path).read_text())["git_identity"] == {
        "name": "Jack Example", "email": "jack@example.com", "source": "gh:jack",
    }
    assert resolve_git_identity(config, await db.get_project("p")).identity == JACK

    cleared = await handler._cmd_set_git_identity({"clear": True})
    assert cleared["configured"] is False
    assert "git_identity" not in yaml.safe_load(Path(path).read_text())
    assert resolve_git_identity(config).identity == FALLBACK_IDENTITY


# --- pool refresh: a new claim never inherits a stale identity -----------------


@pytest.fixture
async def claim_env(db, tmp_path):
    from src.intelligence_classes import IntelligenceClass
    from src.orchestrator import Orchestrator
    from tests.assignment_routing_helpers import route_source_for

    await db.create_project(Project(id="p", name="p"))
    await db.create_profile(
        AgentProfile(id="worker", name="w", lifecycle="pool", needs_workspace=False)
    )
    config = _config(
        JACK,
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "ws"),
        database=DatabaseConfig(url=lease_dsn("git-identity.db")),
        data_dir=str(tmp_path / "data"),
    )
    config.sessions.enabled = True
    config.sessions.provider = "fake"
    config.swarm.enabled = True
    config.swarm.claim_wait_max = 5
    orch = Orchestrator(config)
    orch.session_spec_builder._intelligence_classes = {
        "standard-medium": IntelligenceClass(
            "standard-medium", "Standard", "", {"anthropic": {"model": "claude-sonnet-5"}}
        ),
    }
    orch.db = db
    orch.git = MagicMock()
    orch._worktree_slots = MagicMock(
        return_value=MagicMock(reset_slot_for_task=AsyncMock(return_value="aq/t"))
    )
    orch._last_scheduler_state = None
    orch._run_completion_pipeline = AsyncMock(return_value=(None, True))
    orch.register_settlement_listener()
    handler = CommandHandler(orch, config)

    async def task(tid):
        await db.create_task(Task(
            id=tid, project_id="p", title=tid, description=tid, status=TaskStatus.READY,
            profile_id="worker", intelligence_class="standard-medium",
            route_source=route_source_for("worker"),
        ))

    async def session(sid, digest):
        work_dir = tmp_path / sid
        work_dir.mkdir()
        agent_id = f"agent-{sid}"
        await db.create_agent(
            Agent(id=agent_id, name=agent_id, profile_id="worker", state=AgentState.IDLE)
        )
        await db.create_workspace(Workspace(
            id=f"ws-{sid}", project_id="p", workspace_path=str(work_dir),
            kind_id="project-repo", source_type=RepoSourceType.LINK,
            locked_by_agent_id=agent_id,
        ))
        await db.create_session(SessionRecord(
            id=sid, project_id="p", profile_id="worker", harness="claude", provider="fake",
            name=f"p-worker--p--{sid}", lifecycle="pool", work_dir=str(work_dir), epoch="e",
            instance_token="t", started_at=time.time(), state="running", agent_id=agent_id,
            llm_provider="anthropic", model="claude-sonnet-5",
            intelligence_class="standard-medium", git_identity_digest=digest,
        ))

    async def claim(sid):
        handler._current_scope = {
            "kind": "session", "session_id": sid, "task_id": None, "project_id": "p",
            "elevated": False,
        }
        return await handler._cmd_task_claim({"next": True})

    return SimpleNamespace(config=config, db=db, task=task, session=session, claim=claim)


async def test_a_session_launched_under_the_current_identity_claims(claim_env):
    env = claim_env
    await env.task("t1")
    await env.session("s1", JACK.digest)
    result = await env.claim("s1")
    assert result["result"] == "claimed", result


@pytest.mark.parametrize("launched", ["legacy", ACME.digest])
async def test_a_stale_session_is_retired_instead_of_claiming(claim_env, launched):
    env = claim_env
    await env.task("t1")
    await env.session("s1", launched)
    result = await env.claim("s1")
    assert result["result"] == "session_exhausted"
    assert "git identity changed" in result["reason"]
    assert JACK.formatted() in result["reason"]
    session = await env.db.get_session("s1")
    assert session.desired_state == "stopped"
    assert session.claim_phase is None
    assert (await env.db.get_task("t1")).status == TaskStatus.READY


async def test_an_operator_edit_retires_running_sessions_at_their_next_claim(claim_env):
    env = claim_env
    await env.task("t1")
    await env.task("t2")
    await env.session("s1", JACK.digest)
    await env.session("s2", JACK.digest)
    assert (await env.claim("s1"))["result"] == "claimed"
    # The project now overrides the installation default.
    await env.db.update_project(
        "p", git_identity_name=ACME.name, git_identity_email=ACME.email
    )
    assert (await env.claim("s2"))["result"] == "session_exhausted"
    await env.session("s3", ACME.digest)
    assert (await env.claim("s3"))["result"] == "claimed"


async def test_a_session_without_a_recorded_identity_is_not_judged(claim_env):
    env = claim_env
    await env.task("t1")
    await env.session("s1", None)
    assert (await env.claim("s1"))["result"] == "claimed"


# --- the publishing boundary ------------------------------------------------------


CLAUDE = {"GIT_AUTHOR_NAME": "Claude", "GIT_AUTHOR_EMAIL": "noreply@anthropic.com",
          "GIT_COMMITTER_NAME": "Claude", "GIT_COMMITTER_EMAIL": "noreply@anthropic.com"}


@pytest.fixture
async def push_env(db, tmp_path, internal_plugins_handler, monkeypatch):
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    upstream = {"GIT_AUTHOR_NAME": "Up Stream", "GIT_AUTHOR_EMAIL": "up@stream.test",
                "GIT_COMMITTER_NAME": "Up Stream", "GIT_COMMITTER_EMAIL": "up@stream.test"}
    _git(work, "commit", "-q", "--allow-empty", "-m", "base", env=upstream)
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "-b", "aq/t1")

    await db.create_project(Project(id="p", name="p", repo_url=str(origin)))
    await db.upsert_profile(AgentProfile(id="coder", name="coder", harness="claude",
                                         needs_workspace=False))
    await db.create_agent(Agent(id="a1", name="a1", profile_id="coder"))
    await db.create_task(Task(
        id="t1", project_id="p", title="t", description="t", status=TaskStatus.IN_PROGRESS,
        profile_id="coder", route_source="legacy", assigned_agent_id="a1",
        branch_name="aq/t1",
    ))
    await db.update_agent("a1", state=AgentState.BUSY, current_task_id="t1")

    async def session(digest, lifecycle="pool"):
        await db.create_session(SessionRecord(
            id="s1", task_id="t1", project_id="p", agent_id="a1", profile_id="coder",
            harness="claude", provider="fake", name="s1", lifecycle=lifecycle,
            state="running", work_dir=str(work), epoch="e", instance_token="i",
            started_at=time.time(), last_claim_epoch=0, git_identity_digest=digest,
        ))

    git = GitManager()
    # Everything but the network transfer is real: the identity check runs
    # inside ``apush_validated_delivery`` on the exact tip it would publish.
    git._apush_oid = AsyncMock(return_value=None)
    config = _config(
        JACK,
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path),
        database=DatabaseConfig(url=lease_dsn("git-identity.db")),
        data_dir=str(tmp_path / "data"),
    )
    handler = await internal_plugins_handler(db=db, config=config, git=git)

    def commit(message, env):
        (work / message).write_text(message)
        _git(work, "add", message)
        _git(work, "commit", "-q", "-m", message, env=env)

    async def push():
        return await handler.execute("git_push", {"_scope": {
            "kind": "session", "session_id": "s1", "task_id": "t1", "project_id": "p",
        }})

    def pushed() -> bool:
        return git._apush_oid.await_count > 0

    return SimpleNamespace(work=work, git=git, session=session, commit=commit, push=push,
                           upstream=upstream, db=db, config=config, pushed=pushed)


async def test_commits_as_the_project_identity_publish(push_env):
    env = push_env
    await env.session(JACK.digest)
    env.commit("one", JACK.env())
    result = await env.push()
    assert result.get("oid") == _git(env.work, "rev-parse", "HEAD"), result
    assert "identity_warning" not in result
    assert env.pushed()


async def test_a_self_supplied_committer_is_refused_with_the_fix(push_env):
    env = push_env
    await env.session(JACK.digest)
    env.commit("one", JACK.env())
    env.commit("two", CLAUDE)
    result = await env.push()
    error = result["error"]
    assert "refusing to publish: 1 new commit(s)" in error
    assert "Claude <noreply@anthropic.com>" in error
    assert JACK.formatted() in error
    assert "GIT_COMMITTER_NAME='Jack Example'" in error
    assert "git rebase --force-rebase --rebase-merges" in error
    assert not env.pushed()


async def test_kept_authors_are_reported_not_refused(push_env):
    env = push_env
    await env.session(JACK.digest)
    # A third-party author kept by am/cherry-pick: AQ is only the committer.
    env.commit("patch", {**JACK.env(), "GIT_AUTHOR_NAME": "Patch Author",
                         "GIT_AUTHOR_EMAIL": "patch@third.test"})
    result = await env.push()
    assert result.get("oid"), result
    assert result["identity_notes"][0]["author"] == "Patch Author <patch@third.test>"
    assert "committer" not in result["identity_notes"][0]
    assert "committers were verified" in result["identity_warning"]


async def test_a_mid_task_edit_does_not_strand_the_launch_identity(push_env):
    env = push_env
    await env.session(ACME.digest)  # launched before the default became JACK
    env.commit("one", ACME.env())
    assert (await env.push()).get("oid")


async def test_sessions_from_an_earlier_release_publish_their_old_identity(push_env):
    env = push_env
    await env.session("legacy")
    name, email = legacy_pool_identity("coder")
    env.commit("one", {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                       "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
    assert (await env.push()).get("oid")


async def test_a_same_named_tag_cannot_hide_the_branch(push_env):
    env = push_env
    await env.session(JACK.digest)
    env.commit("forged", CLAUDE)
    _git(env.work, "tag", "aq/t1", "origin/main")  # git log aq/t1 would read the tag
    result = await env.push()
    assert "refusing to publish" in result["error"]
    assert not env.pushed()


async def test_a_planted_remote_tracking_ref_cannot_hide_commits(push_env):
    env = push_env
    await env.session(JACK.digest)
    env.commit("forged", CLAUDE)
    _git(env.work, "update-ref", "refs/remotes/zz/hide", "HEAD")
    assert "refusing to publish" in (await env.push())["error"]


async def test_commits_from_an_earlier_session_of_the_task_publish(push_env):
    env = push_env
    await env.session(JACK.digest)
    # A previous attempt on the task ran under the old project override.
    await env.db.create_session(SessionRecord(
        id="s0", task_id="t1", project_id="p", agent_id="a1", profile_id="coder",
        harness="claude", provider="fake", name="s0", lifecycle="task", state="stopped",
        work_dir=str(env.work), epoch="e", instance_token="i0", started_at=time.time() - 60,
        git_identity_digest=ACME.digest,
    ))
    env.commit("earlier", ACME.env())
    env.commit("now", JACK.env())
    assert (await env.push()).get("oid")


async def test_already_published_commits_are_not_judged(push_env):
    env = push_env
    await env.session(JACK.digest)
    env.commit("old", CLAUDE)
    _git(env.work, "push", "-q", "origin", "aq/t1")
    env.commit("new", JACK.env())
    assert (await env.push()).get("oid")


async def _source_head(env, branch="aq/source"):
    """A source branch committed by another session's identity, published on origin."""
    _git(env.work, "checkout", "-q", "-b", branch, "origin/main")
    env.commit("source-" + branch.rsplit("/", 1)[-1], CLAUDE)
    _git(env.work, "push", "-q", "origin", branch)
    head = _git(env.work, "rev-parse", "HEAD")
    _git(env.work, "checkout", "-q", "aq/t1")
    return head


async def _authorize_source_repair(env, head):
    from src.database.tables import integration_source_ci

    await env.db.create_repo(RepoConfig(
        id="r", project_id="p", source_type=RepoSourceType.LINK, source_path=str(env.work),
    ))
    async with env.db._engine.begin() as conn:
        await conn.execute(integration_source_ci.insert().values(
            task_id="src-task", repository_id="r", source_base="b" * 40, source_head=head,
            generation=1, policy_generation=1, state="red", evidence={},
            repair_task_id="t1", repair_attempt=1, observed_at=time.time(),
        ))


async def test_an_authorized_source_ci_repair_head_is_not_judged(push_env):
    env = push_env
    await env.session(JACK.digest)
    head = await _source_head(env)
    await _authorize_source_repair(env, head)
    _git(env.work, "merge", "-q", "--no-ff", "-m", "merge source", head, env=JACK.env())
    env.commit("fix", JACK.env())
    result = await env.push()
    assert result.get("oid") == _git(env.work, "rev-parse", "HEAD"), result
    assert env.pushed()


async def test_a_foreign_committer_beside_the_authorized_head_is_refused(push_env):
    env = push_env
    await env.session(JACK.digest)
    head = await _source_head(env)
    await _authorize_source_repair(env, head)
    _git(env.work, "merge", "-q", "--no-ff", "-m", "merge source", head, env=JACK.env())
    env.commit("forged", CLAUDE)
    result = await env.push()
    assert "refusing to publish: 1 new commit(s)" in result["error"], result
    assert not env.pushed()


async def test_an_unauthorized_published_head_is_still_judged(push_env):
    env = push_env
    await env.session(JACK.digest)
    head = await _source_head(env)  # published, but no daemon record names it
    _git(env.work, "merge", "-q", "--no-ff", "-m", "merge source", head, env=JACK.env())
    result = await env.push()
    assert "refusing to publish: 1 new commit(s)" in result["error"], result
    assert not env.pushed()


async def _declare_prerequisite(env, branch="aq/source", *, project_id="p", repo_id=None,
                                dep_type="blocks"):
    await env.db.create_task(Task(
        id="prerequisite", project_id=project_id, title="pilot", description="pilot",
        status=TaskStatus.COMPLETED, branch_name=branch, repo_id=repo_id,
    ))
    await env.db.add_dependency("t1", "prerequisite", dep_type)


@pytest.mark.parametrize("identity", [JACK, FALLBACK_IDENTITY])
async def test_a_published_stacked_prerequisite_is_not_judged(push_env, identity):
    env = push_env
    if identity == FALLBACK_IDENTITY:
        env.config.git_identity = GitIdentityConfig()
    await env.session(identity.digest)
    head = await _source_head(env)
    await _declare_prerequisite(env)
    _git(env.work, "merge", "-q", "--no-ff", "-m", "merge pilot", head, env=identity.env())
    env.commit("redesign", identity.env())
    result = await env.push()
    assert result.get("oid") == _git(env.work, "rev-parse", "HEAD"), result
    assert env.pushed()
    assert _ident(env.work, head) == "Claude <noreply@anthropic.com>|Claude <noreply@anthropic.com>"


async def test_a_prerequisite_tracking_ref_cannot_hide_a_new_foreign_commit(push_env):
    env = push_env
    await env.session(JACK.digest)
    head = await _source_head(env)
    await _declare_prerequisite(env)
    _git(env.work, "merge", "-q", "--no-ff", "-m", "merge pilot", head, env=JACK.env())
    env.commit("forged", CLAUDE)
    _git(env.work, "update-ref", "refs/remotes/origin/aq/source", "HEAD")
    result = await env.push()
    assert "refusing to publish: 1 new commit(s)" in result["error"], result
    assert not env.pushed()


async def test_an_unavailable_prerequisite_observation_refuses_publication(push_env):
    from src.git.manager import RemoteRefResult, RemoteRefState

    env = push_env
    await env.session(JACK.digest)
    head = await _source_head(env)
    await _declare_prerequisite(env)
    _git(env.work, "merge", "-q", "--no-ff", "-m", "merge pilot", head, env=JACK.env())
    env.git.als_remote_refs = AsyncMock(return_value={
        "aq/source": RemoteRefResult(RemoteRefState.ERROR, error="offline"),
    })
    result = await env.push()
    assert "could not verify published prerequisite branch 'aq/source'" in result["error"]
    env.git.als_remote_refs.assert_awaited_once_with(
        str(env.work), ["aq/source"], repository_url=_git(env.work, "remote", "get-url", "origin")
    )
    assert not env.pushed()


async def test_a_substituted_origin_cannot_authorize_prerequisite_history(push_env, tmp_path):
    env = push_env
    await env.session(JACK.digest)
    head = await _source_head(env)
    await _declare_prerequisite(env)
    _git(env.work, "merge", "-q", "--no-ff", "-m", "merge pilot", head, env=JACK.env())
    replacement = tmp_path / "replacement.git"
    _git(tmp_path, "init", "-q", "--bare", str(replacement))
    _git(env.work, "remote", "set-url", "origin", str(replacement))
    result = await env.push()
    assert "could not verify published prerequisite" in result["error"], result
    assert not env.pushed()


async def test_completion_publication_uses_the_same_prerequisite_proof(push_env):
    from src.git.manager import GitError
    from src.orchestrator.git_ops import GitOpsMixin

    env = push_env
    await env.session(JACK.digest)
    head = await _source_head(env)
    await _declare_prerequisite(env)
    _git(env.work, "merge", "-q", "--no-ff", "-m", "merge pilot", head, env=JACK.env())
    env.commit("redesign", JACK.env())
    task = await env.db.get_task("t1")
    orchestrator = SimpleNamespace(db=env.db, git=env.git, config=env.config)
    policy = await GitOpsMixin._publish_policy(orchestrator, task, str(env.work))
    assert policy.enforce and policy.authorized_heads == {head}
    await env.git.apush_validated_delivery(
        str(env.work), "origin/main", "HEAD", "aq/t1", identity_policy=policy
    )
    env.git._apush_oid.reset_mock()
    env.commit("forged", CLAUDE)
    with pytest.raises(GitError, match="refusing to publish: 1 new commit"):
        await env.git.apush_validated_delivery(
            str(env.work), "origin/main", "HEAD", "aq/t1", identity_policy=policy
        )
    assert not env.pushed()


async def test_an_unpublished_prerequisite_is_still_judged(push_env):
    env = push_env
    await env.session(JACK.digest)
    head = await _source_head(env)
    await _declare_prerequisite(env)
    origin = _git(env.work, "remote", "get-url", "origin")
    _git(origin, "update-ref", "-d", "refs/heads/aq/source")
    _git(env.work, "merge", "-q", "--no-ff", "-m", "merge pilot", head, env=JACK.env())
    result = await env.push()
    assert "refusing to publish: 1 new commit(s)" in result["error"], result
    assert not env.pushed()


@pytest.mark.parametrize("dep_type", ["related", "discovered-from", "waits-for",
                                     "conditional-blocks"])
async def test_other_dependency_kinds_do_not_authorize_stacked_history(push_env, dep_type):
    env = push_env
    await env.session(JACK.digest)
    head = await _source_head(env)
    await _declare_prerequisite(env, dep_type=dep_type)
    _git(env.work, "merge", "-q", "--no-ff", "-m", "merge pilot", head, env=JACK.env())
    result = await env.push()
    assert "refusing to publish: 1 new commit(s)" in result["error"], result
    assert not env.pushed()


@pytest.mark.parametrize("different", ["project", "repository"])
async def test_prerequisites_outside_the_task_repository_are_still_judged(push_env, different):
    env = push_env
    await env.session(JACK.digest)
    head = await _source_head(env)
    if different == "project":
        await env.db.create_project(Project(id="other", name="other"))
        await _declare_prerequisite(env, project_id="other")
    else:
        await env.db.create_repo(RepoConfig(id="other", project_id="p",
                                            source_type=RepoSourceType.LINK))
        await _declare_prerequisite(env, repo_id="other")
    _git(env.work, "merge", "-q", "--no-ff", "-m", "merge pilot", head, env=JACK.env())
    result = await env.push()
    assert "refusing to publish: 1 new commit(s)" in result["error"], result
    assert not env.pushed()


# --- migration -----------------------------------------------------------------------


def _migration():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "migrations/versions/a00000000053_git_identity.py"
    spec = importlib.util.spec_from_file_location("a00000000053_git_identity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply(migration, *steps):
    def run(sync_conn):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        with Operations.context(MigrationContext.configure(sync_conn)):
            for step in steps:
                getattr(migration, step)()

    return run


async def test_migration_adds_columns_stamps_live_sessions_and_is_idempotent(db):
    from sqlalchemy import text

    await db.create_project(Project(id="p", name="p"))
    migration = _migration()
    async with db._engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.run_sync(_apply(migration, "downgrade"))
            await conn.execute(text(
                "INSERT INTO sessions (id, project_id, profile_id, harness, provider, name, "
                "lifecycle, work_dir, epoch, instance_token, started_at) VALUES "
                "('old', 'p', 'worker', 'claude', 'fake', 'n', 'pool', '/w', 'e', 't', 0)"
            ))
            await conn.run_sync(_apply(migration, "upgrade", "upgrade"))
            digest = (await conn.execute(
                text("SELECT git_identity_digest FROM sessions WHERE id = 'old'")
            )).scalar_one()
            assert digest == "legacy"
            checks = (await conn.execute(text(
                "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_projects_git_identity_pair'"
            ))).scalar_one()
            assert checks == 1
        finally:
            await trans.rollback()


# --- checkpoints ----------------------------------------------------------------------


async def test_pause_checkpoints_commit_as_the_scoped_project_identity(repo):
    from src.orchestrator.task_checkpoint import CHECKPOINT_META, capture_checkpoint

    class _Db:
        def __init__(self):
            self.meta = {}

        async def set_task_meta(self, task_id, key, value):
            self.meta[key] = value

        async def add_task_context(self, task_id, **kw):
            pass

    (repo / "wip").write_text("unsaved")
    git = GitManager()
    git.set_identity_resolver(lambda project: JACK)
    db = _Db()
    with commit_identity(ACME):  # what the daemon opens for the task's project
        await capture_checkpoint(db, git, "t1", str(repo))
    checkpoint = db.meta[CHECKPOINT_META]["commit"]
    assert _ident(repo, checkpoint) == "Acme Bot <bot@acme.test>|Acme Bot <bot@acme.test>"
    assert _ident(repo, f"{checkpoint}^") == "Acme Bot <bot@acme.test>|Acme Bot <bot@acme.test>"


async def test_root_deliveries_report_committers_instead_of_refusing(repo):
    from src.git.identity import publish_policy

    git = GitManager()
    policy = publish_policy(resolve_git_identity(_config(JACK)))
    tip = _git(repo, "rev-parse", "main")
    notes = await git.acheck_publish_identity(
        str(repo), tip, base_ref=None, branch="main", policy=policy
    )
    assert {n["committer"] for n in notes} == {"Third Party <third@up.test>"}
    with pytest.raises(Exception, match="refusing to publish"):
        await git.acheck_publish_identity(
            str(repo), tip, base_ref="feature~1", branch="main", policy=policy
        )


def test_legacy_task_launches_are_reported_not_refused():
    from src.git.identity import publish_policy

    resolved = resolve_git_identity(_config(JACK))
    pool = publish_policy(resolved, [("legacy", "pool", "coder"), (ACME.digest, "task", "w")])
    assert pool.enforce
    assert identity_digest(*legacy_pool_identity("coder")) in pool.allowed
    assert ACME.digest in pool.allowed
    assert not publish_policy(resolved, [("legacy", "task", "w")]).enforce


def ACME_env():
    return {"GIT_AUTHOR_NAME": "Acme Bot", "GIT_AUTHOR_EMAIL": "bot@acme.test",
            "GIT_COMMITTER_NAME": "Acme Bot", "GIT_COMMITTER_EMAIL": "bot@acme.test"}


async def _record_source_ci(db, task_id, repository_id, source_head):
    """Insert a minimal projects/repos/integration_source_ci row for a repair task."""
    await db.create_project(Project(id="p", name="p"))
    async with db._engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO repos (id, project_id, url, checkout_base_path) "
                "VALUES (:rid, 'p', 'git@o/r', '/tmp/repo') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"rid": repository_id},
        )
        await conn.execute(
            insert(integration_source_ci).values(
                task_id="source", repository_id=repository_id, source_base="b",
                source_head=source_head, generation=0, policy_generation=0,
                state="red", evidence={}, repair_task_id=task_id,
                repair_attempt=1, observed_at=1.0,
            )
        )


async def test_source_ci_repair_branch_inherits_foreign_source_head(db, repo):
    """A source-CI repair branch that contains a foreign-identity source head
    (committed by another worker) publishes normally when that source head is
    listed in ``inherited_oids``.

    Regression: without the exclusion the ACME source head counts as a "new"
    commit outside the repair task's publish policy and the gate refuses.
    """
    from src.git.identity import publish_policy

    base = _git(repo, "rev-parse", "main")  # original main, before source-CI work

    # Source head -- committed by ACME (not in the repair task's policy).
    (repo / "src-head").write_text("upstream source")
    _git(repo, "add", "src-head")
    _git(repo, "commit", "-q", "-m", "source head", env=ACME_env())
    source_head = _git(repo, "rev-parse", "HEAD")

    # Repair commit -- committed by JACK (the project identity).
    (repo / "fix").write_text("repair")
    _git(repo, "add", "fix")
    _git(repo, "commit", "-q", "-m", "repair", env=JACK.env())
    tip = _git(repo, "rev-parse", "HEAD")

    policy = publish_policy(resolve_git_identity(_config(JACK)))
    await _record_source_ci(db, "repair-task", "repo", source_head)

    # Bug scenario: without inherited_oids the ACME source head is refused.
    with pytest.raises(Exception, match="refusing to publish"):
        await GitManager().acheck_publish_identity(
            str(repo), tip, base_ref=base, branch="main", policy=policy
        )

    # Fixed path: query the inherited heads from the DB and pass them to the gate.
    inherited = await db.list_source_ci_inherited_oids("repair-task")
    assert inherited == [source_head]
    notes = await GitManager().acheck_publish_identity(
        str(repo), tip, base_ref=base, branch="main", policy=policy, inherited_oids=inherited
    )
    # Only the repair commit (JACK) is "new"; no committer violation.
    assert not any(n.get("committer") for n in notes)


async def test_source_ci_inherited_oid_does_not_shield_a_new_foreign_commit(db, repo):
    """Even with the source head excluded, a *new* commit with a foreign
    committer above it is still refused."""
    from src.git.identity import publish_policy

    base = _git(repo, "rev-parse", "main")

    (repo / "src-head").write_text("upstream source")
    _git(repo, "add", "src-head")
    _git(repo, "commit", "-q", "-m", "source head", env=ACME_env())
    source_head = _git(repo, "rev-parse", "HEAD")

    # New repair commit by a foreign committer (CLAUDE, not in policy).
    (repo / "bad").write_text("bad")
    _git(repo, "add", "bad")
    _git(repo, "commit", "-q", "-m", "bad repair", env=CLAUDE)
    tip = _git(repo, "rev-parse", "HEAD")

    policy = publish_policy(resolve_git_identity(_config(JACK)))
    await _record_source_ci(db, "repair-task", "repo", source_head)
    inherited = await db.list_source_ci_inherited_oids("repair-task")

    with pytest.raises(Exception, match="refusing to publish"):
        await GitManager().acheck_publish_identity(
            str(repo), tip, base_ref=base, branch="main", policy=policy, inherited_oids=inherited
        )


async def test_spawned_event_work_does_not_inherit_a_project_scope():
    import asyncio

    from src.git.manager import detached_commit_context

    git = GitManager()
    git.set_identity_resolver(lambda project: JACK)
    seen = {}

    async def run(key):
        seen[key] = git.resolve_commit_identity()

    with commit_identity(ACME):
        await asyncio.create_task(run("inherited"))
        await asyncio.create_task(run("detached"), context=detached_commit_context())
        assert git.resolve_commit_identity(scoped=False) == JACK
    assert seen == {"inherited": ACME, "detached": JACK}
