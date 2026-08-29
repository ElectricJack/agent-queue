"""_reconcile_pools / _launch_pool_session — spec §11 (fake provider)."""

from __future__ import annotations

import time

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import (
    AgentProfile,
    AgentState,
    Project,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator
from src.sessions.harness_parser import Harness

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(
        AgentProfile(
            id="worker", name="w", lifecycle="pool", min_active=0, max_active=2, harness="claude"
        )
    )
    for i in range(2):
        await database.create_workspace(
            Workspace(
                id=f"ws{i}",
                project_id=PROJECT_ID,
                workspace_path=str(tmp_path / f"ws{i}"),
                source_type=RepoSourceType.LINK,
                kind_id="project-repo",
            )
        )
    yield database
    await database.close()


@pytest.fixture
async def orch(db, tmp_path):
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "ws"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    cfg.swarm.max_starts_per_tick = 5
    o = Orchestrator(cfg)
    o.db = db
    o.git = MagicMock()
    o.bus.emit = AsyncMock()
    o.harness_registry.upsert(
        Harness(
            id="claude",
            name="claude",
            command="claude",
            prompt_mode="arg",
            session_id_flag="--session-id",
            process_names=("claude",),
        )
    )
    return o


async def ready(db, tid):
    await db.create_task(
        Task(
            id=tid,
            project_id=PROJECT_ID,
            title=tid,
            description=tid,
            status=TaskStatus.READY,
            profile_id="worker",
        )
    )


class TestReconcilePools:
    async def test_starts_sessions_for_ready_work(self, orch, db):
        for t in ("t1", "t2", "t3"):
            await ready(db, t)
        await orch._reconcile_pools()
        pool = await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID)
        assert len(pool) == 2  # max_active
        assert all(s.id.startswith("p-worker--proj--") and s.agent_id for s in pool)
        agents = await db.list_agents()
        assert sorted(a.state.value for a in agents) == [
            AgentState.IDLE.value,
            AgentState.IDLE.value,
        ]
        for s in pool:
            assert (await db.get_workspace_for_agent(s.agent_id)) is not None
        kinds = [c.args[0] for c in orch.bus.emit.await_args_list]
        assert kinds.count("pool.scaled") == 1

    async def test_no_starts_when_disabled(self, orch, db):
        orch.config.swarm.enabled = False
        await ready(db, "t1")
        await orch._reconcile_pools()
        assert await db.list_sessions(lifecycle="pool") == []

    async def test_starved_pool_starts_nothing_when_no_workspace(self, orch, db):
        for ws in await db.list_workspaces(PROJECT_ID):
            await db.delete_workspace(ws.id)
        await ready(db, "t1")
        await orch._reconcile_pools()
        assert await db.list_sessions(lifecycle="pool") == []
        assert await db.list_agents() == []

    async def test_quarantined_key_starts_nothing(self, orch, db):
        orch._pool_quarantine[(PROJECT_ID, "worker")] = time.time() + 60
        await ready(db, "t1")
        await orch._reconcile_pools()
        assert await db.list_sessions(lifecycle="pool") == []

    async def test_drain_marks_idle_sessions_after_grace(self, orch, db):
        await ready(db, "t1")
        await orch._reconcile_pools()
        for s in await db.list_sessions(lifecycle="pool"):
            await db.update_session(s.id, state="running")
        await db.delete_task("t1")
        orch.config.swarm.scale_down_grace = 0
        await orch._reconcile_pools()
        await orch._reconcile_pools()
        pool = await db.list_sessions(lifecycle="pool")
        assert [s.desired_state for s in pool] == ["stopped"]

    async def test_push_scheduler_ignores_pool_profile_tasks(self, orch, db):
        await ready(db, "t1")
        assert await orch._pool_profile_ids(PROJECT_ID) == {"worker"}
        task = await db.get_task("t1")
        profile = await orch._resolve_profile(task)
        assert orch._is_session_routed(profile) is False
