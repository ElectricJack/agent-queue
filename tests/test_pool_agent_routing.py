"""Pool startup uses the same execution constraints as push assignment."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.intelligence_classes import IntelligenceClass
from src.models import Agent, AgentProfile, Project, RepoSourceType, SessionRecord, Workspace
from src.orchestrator import Orchestrator
from src.sessions.harness_parser import Harness


@pytest.fixture
async def pool_routing(tmp_path):
    db = Database(str(tmp_path / "pool-routing.db"))
    await db.initialize()
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
    ):
        await db.create_profile(profile)
    await db.create_workspace(Workspace(
        id="ws", project_id="p", workspace_path=str(tmp_path / "workspace"),
        source_type=RepoSourceType.LINK, kind_id="project-repo",
    ))
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        workspace_dir=str(tmp_path / "work"), data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "unused.db"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    orch = Orchestrator(cfg)
    orch.db = db
    orch.git = MagicMock()
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


async def freeze_roster(db):
    await db.create_agent(Agent(id="deleted", name="Deleted", profile_id="triage"))
    assert await db.soft_delete_agent("deleted")


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


async def test_pool_start_waits_without_growing_manual_roster_or_using_triage(pool_routing):
    orch, db = pool_routing
    await db.create_agent(Agent(id="triage", name="Triage", profile_id="triage"))
    await freeze_roster(db)
    assert await launch(orch, db) is None
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
    await freeze_roster(db)
    await db.create_session(SessionRecord(
        id="interactive", project_id=None, profile_id="saved-codex", harness="codex",
        provider="fake", name="interactive-sol", lifecycle="named", state="running",
        work_dir="/tmp", epoch="e", instance_token="test", started_at=1, agent_id="sol",
    ))
    assert await launch(orch, db) is None
    assert await db.list_sessions(lifecycle="pool") == []
    assert (await db.get_session("interactive")).state == "running"
    assert (await db.get_workspace("ws")).locked_by_agent_id is None
