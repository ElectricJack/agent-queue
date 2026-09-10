"""Pool startup uses the same execution constraints as push assignment."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from dataclasses import replace

from src.config import DatabaseConfig, AppConfig, DiscordConfig
from src.database import Database
from src.intelligence_classes import IntelligenceClass
from src.models import Agent, AgentProfile, Project, RepoSourceType, SessionRecord, Workspace
from src.orchestrator import Orchestrator
from src.sessions.harness_parser import Harness
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def pool_routing(tmp_path):
    db = Database(lease_dsn("pool-routing.db"))
    await db.initialize()
    # These fixtures model independent clones; slot provisioning has its own tests.
    kind = await db.resolve_workspace_kind("__system__", "project-repo")
    await db.upsert_workspace_kind(replace(kind, mode="exclusive-clone"))
    await db.create_project(Project(id="p", name="Project"))
    for profile in (
        AgentProfile(
            id="deep-codex-pool", name="Deep Codex pool", lifecycle="pool",
            harness="codex", default_class="deep-high",
        ),
        AgentProfile(
            id="worker-deep", name="Generic deep pool", lifecycle="pool",
            harness="claude", default_class="deep-high",
        ),
        AgentProfile(
            id="saved-codex", name="Saved Codex worker",
            harness="codex", default_class="deep-high",
        ),
        AgentProfile(
            id="triage", name="Triage worker", harness="codex", default_class="fast-low",
        ),
        AgentProfile(
            id="saved-claude", name="Saved Claude worker",
            harness="claude", default_class="deep-high",
        ),
        AgentProfile(
            id="deep-gemini-pool", name="Deep Gemini pool", lifecycle="pool",
            harness="gemini", default_class="deep-high",
        ),
    ):
        await db.create_profile(profile)
    await db.create_workspace(Workspace(
        id="ws", project_id="p", workspace_path=str(tmp_path / "workspace"),
        source_type=RepoSourceType.LINK, kind_id="project-repo",
    ))
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        workspace_dir=str(tmp_path / "work"), data_dir=str(tmp_path / "data"),
        database=DatabaseConfig(url=lease_dsn("unused.db")),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    orch = Orchestrator(cfg)
    orch.db = db
    orch.git = MagicMock()
    # Pool launch installs the managed git excludes before it hands a
    # checkout to a session (src/orchestrator/pools.py), which reaches for
    # the real GitManager; these fixtures have no checkout.  Same stub as
    # tests/test_pool_reconciler.py's ``orch`` fixture.
    orch._ensure_control_files_excluded = AsyncMock(return_value=True)
    orch.bus.emit = AsyncMock()
    for harness in ("claude", "codex"):
        orch.harness_registry.upsert(Harness(
            id=harness, name=harness, command=harness, model_flag="--model",
        ))
    orch.session_spec_builder._intelligence_classes = {
        "deep-high": IntelligenceClass("deep-high", "Deep", "", {
            "anthropic": {"model": "claude-fable-5", "thinking": "high"},
            "codex": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        }),
        "fast-low": IntelligenceClass("fast-low", "Fast", "", {
            "anthropic": {"model": "claude-sonnet-5", "thinking": "low"},
            "codex": {"model": "gpt-5.6-luna", "reasoning_effort": "low"},
        }),
    }
    yield orch, db
    await db.close()


async def launch(orch, db, profile_id="deep-codex-pool"):
    return await orch._launch_pool_session(
        await db.get_project("p"), await db.get_profile(profile_id),
    )


async def test_pool_start_selects_compatible_worker_before_reserving(pool_routing):
    orch, db = pool_routing
    await db.create_agent(Agent(id="triage", name="Triage first", profile_id="triage"))
    await db.create_agent(Agent(id="claude", name="Deep Claude", profile_id="saved-claude"))
    await db.create_agent(Agent(id="sol", name="Deep Codex", profile_id="saved-codex"))
    before = await db.get_agent("sol")
    sid = await launch(orch, db)
    assert sid is not None
    row = await db.get_session(sid)
    assert (row.agent_id, row.harness, row.model, row.intelligence_class) == (
        "sol", "codex", "gpt-5.6-sol", "deep-high",
    )
    assert (await db.get_agent("sol")).profile_id == before.profile_id
    assert await db.get_workspace_for_agent("triage") is None
    assert await db.get_workspace_for_agent("claude") is None


async def test_pool_start_grows_the_roster_rather_than_using_triage(pool_routing):
    orch, db = pool_routing
    await db.create_agent(Agent(id="triage", name="Triage", profile_id="triage"))
    sid = await launch(orch, db)
    assert sid is not None
    # Deleting a worker is not a scaling policy, so growth is allowed here --
    # but only into a *fresh* identity for the requested pool profile.  The
    # incompatible triage worker is neither reused nor reprofiled.
    row = await db.get_session(sid)
    assert row.agent_id != "triage"
    assert (row.harness, row.model, row.intelligence_class) == (
        "codex", "gpt-5.6-sol", "deep-high",
    )
    assert (await db.get_agent(row.agent_id)).profile_id == "deep-codex-pool"
    assert sorted(agent.id for agent in await db.list_agents()) == sorted(
        ["triage", row.agent_id]
    )
    assert (await db.get_agent("triage")).profile_id == "triage"
    assert await db.get_workspace_for_agent("triage") is None
    assert (await db.get_workspace("ws")).locked_by_agent_id == row.agent_id


async def test_pool_start_waits_rather_than_growing_into_an_unrunnable_worker(pool_routing):
    orch, db = pool_routing
    await db.create_agent(Agent(id="triage", name="Triage", profile_id="triage"))
    # ``deep-high`` maps a model for anthropic and codex only, so a fresh
    # gemini identity would fail the same execution check push assignment
    # applies.  Growth is permitted; growing into an unusable worker is not.
    assert await launch(orch, db, "deep-gemini-pool") is None
    assert await db.list_sessions(lifecycle="pool") == []
    assert [agent.id for agent in await db.list_agents()] == ["triage"]
    assert (await db.get_workspace("ws")).locked_by_agent_id is None


async def test_generic_pool_inherits_the_matched_workers_codex_identity(pool_routing):
    orch, db = pool_routing
    await db.create_agent(Agent(id="sol", name="Codex", profile_id="saved-codex"))
    sid = await launch(orch, db, "worker-deep")
    assert sid is not None
    row = await db.get_session(sid)
    assert (row.agent_id, row.harness, row.model, row.intelligence_class) == (
        "sol", "codex", "gpt-5.6-sol", "deep-high",
    )
    assert (await db.get_profile("worker-deep")).harness == "claude"
    assert (await db.get_agent("sol")).harness is None


async def test_pool_start_does_not_steal_an_interactive_sol_or_fall_back_to_triage(pool_routing):
    orch, db = pool_routing
    await db.create_agent(Agent(id="triage", name="Triage first", profile_id="triage"))
    await db.create_agent(Agent(id="sol", name="Codex", profile_id="saved-codex"))
    await db.create_session(SessionRecord(
        id="interactive", project_id=None, profile_id="saved-codex", harness="codex",
        provider="fake", name="interactive-sol", lifecycle="named", state="running",
        work_dir="/tmp", epoch="e", instance_token="test", started_at=1, agent_id="sol",
    ))
    sid = await launch(orch, db)
    assert sid is not None
    row = await db.get_session(sid)
    # The only compatible saved worker is held by a live interactive session,
    # so the pool grows instead of stealing it -- and still never falls back
    # to the incompatible triage worker.
    assert row.agent_id not in ("sol", "triage")
    assert (row.harness, row.model, row.intelligence_class) == (
        "codex", "gpt-5.6-sol", "deep-high",
    )
    assert (await db.get_session("interactive")).state == "running"
    assert (await db.get_agent("sol")).profile_id == "saved-codex"
    assert await db.get_workspace_for_agent("sol") is None
    assert await db.get_workspace_for_agent("triage") is None
    assert (await db.get_workspace("ws")).locked_by_agent_id == row.agent_id
