"""Manual roster deletion must not be undone by automatic worker supply."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Agent, AgentProfile, Project, RepoSourceType, Task, TaskStatus, Workspace
from src.orchestrator import Orchestrator
from src.orchestrator.agent_reconciler import AgentReconciler
from src.sessions.harness_parser import Harness


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "supply.db"))
    await db.initialize()
    for profile in (
        AgentProfile(id="worker", name="Worker", harness="claude"),
        AgentProfile(id="personal", name="Personal", harness="claude"),
        AgentProfile(id="pool-worker", name="Pool worker", harness="claude", lifecycle="pool"),
    ):
        await db.create_profile(profile)
    await db.create_project(Project(id="p", name="P", default_profile_id="worker"))
    await db.create_workspace(
        Workspace(
            id="w",
            project_id="p",
            workspace_path=str(tmp_path / "workspace"),
            source_type=RepoSourceType.LINK,
            kind_id="project-repo",
        )
    )
    yield db
    await db.close()


async def demand(db, profile="worker"):
    await db.create_task(
        Task(
            id="t",
            project_id="p",
            title="Work",
            description="Work",
            status=TaskStatus.READY,
            profile_id=profile,
        )
    )


async def deleted_worker(db):
    # It is a global worker: demand need not use its saved profile.
    await db.create_agent(Agent(id="deleted", name="Personal", profile_id="personal"))
    assert await db.soft_delete_agent("deleted")


@pytest.fixture
async def orch(db, tmp_path):
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        database_path=str(tmp_path / "supply.db"),
        data_dir=str(tmp_path / "data"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    orch = Orchestrator(cfg)
    orch.db = db
    orch.git = MagicMock()
    orch.bus.emit = AsyncMock()
    orch.harness_registry.upsert(Harness(id="claude", command="claude"))
    return orch


async def test_fresh_registry_keeps_task_bootstrap(db):
    await demand(db)
    report = await AgentReconciler(db).reconcile()
    assert report.created == [("p", "worker")]
    assert len(await db.list_agents()) == 1


async def test_deleted_worker_is_not_replaced_for_another_profile_after_restart(db, tmp_path):
    await demand(db)
    await deleted_worker(db)
    restarted = Database(str(tmp_path / "supply.db"))
    await restarted.initialize()
    try:
        for _ in range(2):
            assert not (await AgentReconciler(restarted).reconcile()).created
        assert await restarted.list_agents() == []
        assert len(await restarted.list_agents(include_deleted=True)) == 1
    finally:
        await restarted.close()


async def test_remaining_global_worker_is_still_reused_after_deletion(db):
    await demand(db)
    await deleted_worker(db)
    await db.create_agent(Agent(id="remaining", name="Remaining", profile_id="personal"))
    assert not (await AgentReconciler(db).reconcile()).created
    assert [a.id for a in await db.list_agents()] == ["remaining"]
    assert await db.assign_task_to_agent("t", "remaining")


async def test_fresh_registry_keeps_pool_bootstrap(orch, db):
    await demand(db, "pool-worker")
    await orch._reconcile_pools()
    sessions = await db.list_sessions(lifecycle="pool")
    assert len(sessions) == 1
    assert len(await db.list_agents()) == 1


async def test_pool_fallback_does_not_replace_a_deleted_global_worker(orch, db):
    await demand(db, "pool-worker")
    await deleted_worker(db)
    await orch._reconcile_pools()
    await orch._reconcile_pools()
    assert await db.list_agents() == []
    assert await db.list_sessions(lifecycle="pool") == []


async def test_pool_reuses_remaining_definition_after_deletion(orch, db):
    await demand(db, "pool-worker")
    await deleted_worker(db)
    await db.create_agent(Agent(id="remaining", name="Remaining", profile_id="personal"))
    await orch._reconcile_pools()
    sessions = await db.list_sessions(lifecycle="pool")
    assert len(sessions) == 1
    assert sessions[0].agent_id == "remaining"
    assert [a.id for a in await db.list_agents()] == ["remaining"]


async def test_automatic_supply_waits_for_in_flight_deletion(db, monkeypatch):
    await db.create_agent(Agent(id="original", name="Original", profile_id="personal"))
    locked = asyncio.Event()
    release = asyncio.Event()
    lock_roster = db._lock_agent_roster_on

    async def hold_deletion(conn):
        await lock_roster(conn)
        if asyncio.current_task().get_name() == "delete-worker":
            locked.set()
            await release.wait()

    monkeypatch.setattr(db, "_lock_agent_roster_on", hold_deletion)
    deletion = asyncio.create_task(db.soft_delete_agent("original"), name="delete-worker")
    await asyncio.wait_for(locked.wait(), 5)
    creation = asyncio.create_task(
        db.create_automatic_agent(
            Agent(id="replacement", name="Replacement", profile_id="worker"),
        )
    )
    try:
        await asyncio.sleep(0)
        assert not creation.done()
    finally:
        release.set()
    assert await deletion
    assert not await creation
    assert await db.list_agents() == []
