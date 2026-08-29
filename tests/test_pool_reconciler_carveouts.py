"""Session reconciler — pool lifecycle carve-outs (spec §10.4, §11.2, §11.4)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator
from src.sessions import SessionProviderRegistry
from src.sessions.fake import FakeProvider
from src.sessions.reconciler import SessionReconciler

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(AgentProfile(id="worker", name="w", harness="claude"))
    yield database
    await database.close()


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def registry(provider):
    class _Reg(SessionProviderRegistry):
        def create(self, name, config=None):
            return provider

    return _Reg({"fake": FakeProvider})


@pytest.fixture
async def orch(db, tmp_path, registry):
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "ws"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    o = Orchestrator(cfg)
    o.db = db
    # ``AgentReconciler`` was built in ``__init__`` against the (real,
    # uninitialized) db the constructor saw -- point it at the test db too.
    o._agent_reconciler._db = db
    o.git = MagicMock()
    o.bus.emit = AsyncMock()
    o.session_providers = registry
    return o


@pytest.fixture
def reconciler(db, orch, registry):
    return SessionReconciler(
        db, orch.config, registry, bus=orch.bus, orchestrator=orch, epoch="epoch-new"
    )


async def held_pool_session(db, sid="s1", agent_id="agent-1", phase="active", phase_at=None):
    # ``tasks.assigned_agent_id`` and ``agents.current_task_id`` form a
    # cycle -- insert both with the cross-reference unset, then backfill.
    await db.create_task(Task(id="t1", project_id=PROJECT_ID, title="t1", description="t1",
                              status=TaskStatus.IN_PROGRESS, claim_epoch=1, profile_id="worker"))
    await db.create_agent(Agent(id=agent_id, name=agent_id, profile_id="worker",
                                state=AgentState.BUSY))
    await db.update_task("t1", assigned_agent_id=agent_id)
    await db.update_agent(agent_id, current_task_id="t1")
    await db.create_workspace(Workspace(id=f"ws-{agent_id}", project_id=PROJECT_ID,
                                        workspace_path=f"/wd/{agent_id}",
                                        source_type=RepoSourceType.LINK, kind_id="project-repo",
                                        locked_by_agent_id=agent_id, locked_by_task_id="t1"))
    await db.create_session(SessionRecord(
        id=sid, project_id=PROJECT_ID, profile_id="worker", harness="claude", provider="fake",
        name=sid, lifecycle="pool", work_dir=f"/wd/{agent_id}", epoch="e", instance_token="t",
        started_at=time.time() - 600, state="running", agent_id=agent_id, task_id="t1",
        claim_phase=phase, claim_phase_at=phase_at if phase_at is not None else time.time()))
    return sid


class TestPrepareTimeout:
    async def test_stuck_preparing_is_released(self, db, reconciler):
        sid = await held_pool_session(db, phase="preparing", phase_at=time.time() - 1000)
        await reconciler._step_prepare_timeout()
        s = await db.get_session(sid)
        assert (s.task_id, s.claim_phase, s.last_claim_result) == (None, None, "prepare_failed")
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        assert await db.get_task_meta("t1", "needs_attention") == "prepare_timeout"

    async def test_fresh_preparing_is_left_alone(self, db, reconciler):
        sid = await held_pool_session(db, phase="preparing")
        await reconciler._step_prepare_timeout()
        assert (await db.get_session(sid)).claim_phase == "preparing"


class TestExits:
    async def test_pool_exit_holding_task_returns_task_and_retires_agent(self, db, reconciler,
                                                                         provider):
        sid = await held_pool_session(db)
        provider.peek = AsyncMock(return_value=MagicMock(alive=False, exit_code=0))
        await reconciler._step_exits()
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        assert await db.get_task_meta("t1", "needs_attention") == "exited_holding_task"
        assert (await db.get_agent("agent-1")).state == AgentState.RETIRED
        assert await db.get_workspace_for_agent("agent-1") is None
        assert (await db.get_session(sid)).state == "stopped"

    async def test_rapid_crash_quarantines_pool_key(self, db, reconciler, provider, orch):
        sid = await held_pool_session(db)
        await db.update_session(sid, started_at=time.time() - 1)
        provider.peek = AsyncMock(return_value=MagicMock(alive=False, exit_code=1))
        await reconciler._step_exits()
        assert orch._pool_quarantine[(PROJECT_ID, "worker")] > time.time()
        assert await db.get_task_meta("t1", "needs_attention") == "rapid_crash"

    async def test_idle_pool_drain_ack_stops_session(self, db, reconciler):
        sid = await held_pool_session(db)
        await db.release_claim(sid, task_status=TaskStatus.READY, context="x", now=time.time())
        await db.update_session(sid, desired_state="stopped")
        await reconciler._step_drain_ack()
        assert (await db.get_session(sid)).state == "stopped"
        assert (await db.get_agent("agent-1")).state == AgentState.RETIRED

    async def test_idle_pool_session_is_not_stale_for_backstop(self, db, reconciler):
        sid = await held_pool_session(db)
        await db.release_claim(sid, task_status=TaskStatus.READY, context="x", now=time.time())
        await db.update_session(sid, last_activity=time.time() - 10_000)
        await reconciler._step_backstop()
        assert (await db.get_session(sid)).state == "running"
