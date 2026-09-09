"""Agent liveness is session activity, never the task-scoped heartbeat.

Regression cover for the report that `aq agent list` showed hour-old
"heartbeats" for a fleet of healthy, idle pool workers: `last_heartbeat`
only advances while an agent holds a task, so a worker parked in
`aq task claim --next --wait 60` never touches it.
"""
import time
from types import SimpleNamespace

import pytest

from src.agents.liveness import is_live, liveness_by_agent, session_last_activity
from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.event_bus import EventBus
from src.mcp_interfaces import agent_to_dict
from src.models import Agent, AgentProfile, SessionRecord
from src.sessions.harness_parser import Harness
from src.sessions.harness_registry import HarnessRegistry
from src.sessions.spec import SessionSpecBuilder
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def handler(tmp_path):
    db = Database(lease_dsn("liveness.db"))
    await db.initialize()
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        workspace_dir=str(tmp_path / "work"),
        data_dir=str(tmp_path / "data"),
    )
    registry = HarnessRegistry()
    registry.upsert(Harness(id="claude", name="Claude", command="claude"))
    orchestrator = SimpleNamespace(
        db=db,
        bus=EventBus(validate_events=False),
        harness_registry=registry,
        session_spec_builder=SessionSpecBuilder(config, intelligence_classes={}),
        config=config,
    )
    await db.create_profile(AgentProfile(
        id="pool", name="Pool", harness="claude", lifecycle="pool",
    ))
    result = CommandHandler(orchestrator, config)
    result.set_active_project(None)
    yield result
    result.set_active_project(None)
    result._current_scope = None
    await db.close()


def _session(**kwargs):
    fields = {
        "id": "s1",
        "project_id": None,
        "profile_id": "pool",
        "harness": "claude",
        "provider": "tmux",
        "name": "n-pool-1",
        "lifecycle": "pool",
        "work_dir": "/tmp/pool",
        "epoch": "e",
        "instance_token": "tok",
        "started_at": 1.0,
        "state": "running",
        "agent_id": "worker-a",
    }
    fields.update(kwargs)
    return SessionRecord(**fields)


# --- the definition itself ------------------------------------------------


def test_last_activity_falls_back_to_launch_time():
    assert session_last_activity(None) is None
    assert session_last_activity(_session(last_activity=None, started_at=7.0)) == 7.0
    assert session_last_activity(_session(last_activity=9.0, started_at=7.0)) == 9.0


def test_liveness_map_takes_the_newest_live_session_per_agent():
    rows = [
        _session(id="s-old", last_activity=10.0),
        _session(id="s-new", last_activity=40.0),
        _session(id="s-dead", state="stopped", last_activity=99.0),
        _session(id="s-other", agent_id="worker-b", last_activity=5.0),
        _session(id="s-orphan", agent_id=None, last_activity=99.0),
    ]
    assert liveness_by_agent(rows) == {"worker-a": 40.0, "worker-b": 5.0}


def test_is_live_uses_the_lease_ttl():
    now = 1000.0
    assert is_live(None, now=now, ttl=60) is False
    assert is_live(now - 5, now=now, ttl=60) is True
    assert is_live(now - 600, now=now, ttl=60) is False
    # A disabled TTL keeps the "has a live session" half of the answer.
    assert is_live(now - 600, now=now, ttl=0) is True


# --- the consumers agree --------------------------------------------------


async def test_idle_pool_worker_with_an_active_session_is_reported_live(handler):
    """No task held, stale task heartbeat, session busy claiming -> live."""
    now = time.time()
    await handler.db.create_agent(
        Agent(id="worker-a", name="Ada", profile_id="pool", last_heartbeat=now - 3600)
    )
    await handler.db.create_session(_session(last_activity=now - 5, claim_phase="claiming"))

    result = await handler._cmd_list_agents({})
    row = result["agents"][0]

    assert row["current_task_id"] is None
    assert row["live"] is True
    assert row["last_activity"] == pytest.approx(now - 5, abs=1)
    # The task heartbeat is still an hour stale -- it is simply not liveness.
    assert now - row["last_heartbeat"] > 3000


async def test_agent_without_a_live_session_is_not_live(handler):
    now = time.time()
    await handler.db.create_agent(
        Agent(id="worker-a", name="Ada", profile_id="pool", last_heartbeat=now)
    )
    await handler.db.create_session(_session(state="stopped", last_activity=now))

    row = (await handler._cmd_list_agents({}))["agents"][0]
    assert row["live"] is False
    assert row["last_activity"] is None


async def test_stale_session_activity_is_not_live(handler):
    now = time.time()
    ttl = handler.config.sessions.lease_ttl_seconds
    if ttl <= 0:
        pytest.skip("lease TTL disabled in the default config")
    await handler.db.create_agent(Agent(id="worker-a", name="Ada", profile_id="pool"))
    await handler.db.create_session(_session(last_activity=now - ttl - 60))

    row = (await handler._cmd_list_agents({}))["agents"][0]
    assert row["live"] is False


def test_mcp_agent_dict_labels_the_heartbeat_as_task_scoped():
    agent = Agent(id="worker-a", name="Ada", profile_id="pool", last_heartbeat=1.0)
    payload = agent_to_dict(agent, 42.0)
    assert payload["last_activity"] == 42.0
    assert payload["last_task_heartbeat"] == 1.0
    assert payload["last_heartbeat"] == 1.0


def test_cli_agent_table_renders_session_activity():
    from src.cli.adapters import agent_proxy
    from src.cli.formatters import format_agent_table

    table = format_agent_table([
        agent_proxy({
            "id": "worker-a", "name": "Ada", "profile_id": "pool", "state": "idle",
            "current_task_id": None, "last_heartbeat": 1.0,
            "last_activity": time.time() - 30, "live": True,
        })
    ])
    assert [column.header for column in table.columns].count("Heartbeat") == 0
    assert "Activity" in [column.header for column in table.columns]
