"""Durable global workers keep identity while their task execution scope changes."""

from __future__ import annotations

import asyncio

import pytest
from src.database import Database
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
    RepoSourceType,
)
from src.orchestrator.agent_reconciler import AgentReconciler
from src.scheduler import Scheduler, SchedulerState


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "flock.db"))
    await database.initialize()
    yield database
    await database.close()


async def seed_project(db, pid="p1"):
    await db.create_profile(AgentProfile(id=pid, name=pid, harness="claude"))
    await db.create_project(Project(id=pid, name=pid, default_profile_id=pid))
    await db.create_workspace(
        Workspace(
            id=f"ws-{pid}",
            project_id=pid,
            workspace_path=f"/tmp/{pid}",
            source_type=RepoSourceType.LINK,
        )
    )
    await db.create_task(
        Task(id=f"t-{pid}", project_id=pid, title=pid, description="work", status=TaskStatus.READY)
    )


def session(**kwargs):
    return SessionRecord(
        id="s1",
        project_id="p1",
        profile_id="p1",
        harness="claude",
        provider="fake",
        name="s-t-p1",
        lifecycle="task",
        work_dir="/tmp/p1",
        epoch="e",
        instance_token="tok",
        started_at=1,
        **kwargs,
    )


async def test_agent_settings_and_launch_snapshot_round_trip(db):
    await seed_project(db)
    await db.create_agent(
        Agent(
            id="a1",
            name="Alice",
            profile_id="p1",
            role="worker",
            enabled=False,
            harness="codex",
            model="fixed-model",
            intelligence_class="deep",
        )
    )
    agent = await db.get_agent("a1")
    assert (agent.role, agent.enabled, agent.harness, agent.model, agent.intelligence_class) == (
        "worker",
        False,
        "codex",
        "fixed-model",
        "deep",
    )
    await db.create_session(
        session(
            agent_id="a1", llm_provider="openai", model="actual-model", intelligence_class="deep"
        )
    )
    record = await db.get_session("s1")
    assert (record.agent_id, record.llm_provider, record.model, record.intelligence_class) == (
        "a1",
        "openai",
        "actual-model",
        "deep",
    )


async def test_reconciler_reuses_global_worker_without_reprofiling(db):
    await seed_project(db, "p1")
    await seed_project(db, "p2")
    await db.create_profile(AgentProfile(id="personal", name="Personal", harness="codex"))
    await db.create_agent(Agent(id="a1", name="Alice", profile_id="personal"))
    before = await db.get_agent("a1")
    report = await AgentReconciler(db).reconcile()
    assert await db.get_agent("a1") == before
    assert report.reassigned == []
    assert len(await db.list_agents()) == 1


async def test_reconciler_does_not_reuse_live_session_worker(db):
    await seed_project(db)
    await db.create_agent(Agent(id="a1", name="Alice", profile_id="p1"))
    await db.create_session(session(agent_id="a1", state="running"))
    await AgentReconciler(db).reconcile()
    assert len(await db.list_agents()) == 2
    assert (await db.get_agent("a1")).profile_id == "p1"


@pytest.mark.parametrize("settings", [{"enabled": False}, {"role": "supervisor"}])
def test_scheduler_excludes_disabled_and_supervisor_workers(settings):
    agent = Agent(id="a1", name="Alice", profile_id="p1", **settings)
    state = SchedulerState(
        projects=[Project(id="p1", name="P1")],
        tasks=[
            Task(
                id="t1", project_id="p1", title="task", description="work", status=TaskStatus.READY
            )
        ],
        agents=[agent],
        project_token_usage={},
        project_active_agent_counts={},
        tasks_completed_in_window={},
    )
    assert Scheduler.schedule(state) == []


async def test_cross_project_assignment_only_one_task_wins(db):
    await seed_project(db, "p1")
    await seed_project(db, "p2")
    await db.create_agent(Agent(id="a1", name="Alice", profile_id="p1"))
    results = await asyncio.gather(
        db.assign_task_to_agent("t-p1", "a1"), db.assign_task_to_agent("t-p2", "a1")
    )
    assert results.count(True) == 1
    assert results.count(False) == 1
    tasks = [await db.get_task("t-p1"), await db.get_task("t-p2")]
    assigned = [t for t in tasks if t.assigned_agent_id == "a1"]
    assert len(assigned) == 1
    assert (await db.get_agent("a1")).current_task_id == assigned[0].id


async def test_assignment_refuses_worker_with_live_session(db):
    await seed_project(db)
    await db.create_agent(Agent(id="a1", name="Alice", profile_id="p1"))
    await db.create_session(session(agent_id="a1", state="running"))
    assert await db.assign_task_to_agent("t-p1", "a1") is False
    assert (await db.get_task("t-p1")).status == TaskStatus.READY


async def test_restart_preserves_orphan_and_retired_definitions(db, tmp_path):
    from unittest.mock import AsyncMock
    from src.config import AppConfig, DiscordConfig
    from src.orchestrator import Orchestrator

    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        database_path=str(tmp_path / "other.db"),
        data_dir=str(tmp_path / "data"),
        workspace_dir=str(tmp_path / "ws"),
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.bus.emit = AsyncMock()
    await db.create_agent(Agent(id="orphan", name="Keep", profile_id="missing"))
    await db.create_agent(
        Agent(id="retired", name="Retired", profile_id="missing", state=AgentState.RETIRED)
    )
    await orch._recover_stale_state()
    assert (await db.get_agent("orphan")).name == "Keep"
    assert (await db.get_agent("retired")).state == AgentState.RETIRED


async def test_existing_worker_supplies_its_default_without_project_reprofile(db, tmp_path):
    from unittest.mock import AsyncMock
    from src.config import AppConfig, DiscordConfig
    from src.orchestrator import Orchestrator

    await seed_project(db)
    await db.update_project("p1", default_profile_id=None)
    await db.create_profile(
        AgentProfile(id="personal", name="Personal", harness="claude", allowed_tools=["Read"])
    )
    await db.create_agent(Agent(id="a1", name="Alice", profile_id="personal"))
    await AgentReconciler(db).reconcile()
    assert (await db.get_project("p1")).default_profile_id is None
    assert await db.assign_task_to_agent("t-p1", "a1")
    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        database_path=str(tmp_path / "unused.db"),
        workspace_dir=str(tmp_path / "ws"),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.bus.emit = AsyncMock()
    profile = await orch._resolve_profile(await db.get_task("t-p1"))
    assert profile.id == "personal" and profile.allowed_tools == ["Read"]
    await db.update_task("t-p1", profile_id="p1")
    assert (await orch._resolve_profile(await db.get_task("t-p1"))).id == "p1"


async def test_ready_task_with_stale_assignment_can_use_another_worker(db):
    await seed_project(db)
    await db.create_agent(Agent(id="old", name="Old", profile_id="p1"))
    await db.create_agent(Agent(id="new", name="New", profile_id="p1"))
    await db.update_task("t-p1", assigned_agent_id="old")
    assert await db.assign_task_to_agent("t-p1", "new") is True
    assert (await db.get_task("t-p1")).assigned_agent_id == "new"
    assert (await db.get_agent("old")).current_task_id is None


async def test_ready_retry_cannot_bypass_its_old_live_session_with_other_worker(db):
    await seed_project(db)
    await db.create_agent(Agent(id="old", name="Old", profile_id="p1"))
    await db.create_agent(Agent(id="new", name="New", profile_id="p1"))
    await db.create_session(session(agent_id="old", task_id="t-p1"))
    assert await db.assign_task_to_agent("t-p1", "new") is False
    assert (await db.get_agent("new")).state == AgentState.IDLE


async def test_cooled_worker_does_not_suppress_other_provider_supply(db):
    import time

    await seed_project(db)
    await db.create_profile(AgentProfile(id="cooled", name="Cooled", harness="codex"))
    await db.create_agent(Agent(id="a1", name="Cooled", profile_id="cooled"))
    report = await AgentReconciler(db).reconcile(provider_cooldowns={"cooled": time.time() + 60})
    assert report.created == [("p1", "p1")]
    assert (await db.get_agent("a1")).profile_id == "cooled"


async def test_cooldown_does_not_create_more_unusable_durable_workers(db):
    import time
    await seed_project(db)
    await db.create_agent(Agent(id="a1", name="Waiting", profile_id="p1"))
    reconciler = AgentReconciler(db)
    for _ in range(2):
        report = await reconciler.reconcile(provider_cooldowns={"p1": time.time() + 60})
        assert report.created == []
    assert len(await db.list_agents()) == 1
