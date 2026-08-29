"""Session reconciler — pool lifecycle carve-outs (spec §10.4, §11.2, §11.4)."""

from __future__ import annotations

import logging
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
from src.sessions.exit_classifier import ExitVerdict, Verdict
from src.sessions.fake import FakeProvider
from src.sessions.reconciler import META_STALL_LAST_ACTION, META_STALL_NUDGES, SessionReconciler

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
    # Small and explicit so the backstop tests can control it precisely
    # rather than relying on the (1800s) production default.
    cfg.agents_config.stuck_timeout_seconds = 300
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


async def observe(reconciler, now=None):
    """Snapshot ``live`` the way ``tick()`` does, for a step called directly."""
    now = now if now is not None else time.time()
    live = await reconciler._step_observe(now)
    return live, now


class TestPrepareTimeout:
    async def test_stuck_preparing_is_released(self, db, reconciler):
        sid = await held_pool_session(db, phase="preparing", phase_at=time.time() - 1000)
        live, now = await observe(reconciler)
        await reconciler._step_prepare_timeout(live, now)
        s = await db.get_session(sid)
        assert (s.task_id, s.claim_phase, s.last_claim_result) == (None, None, "prepare_failed")
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        assert await db.get_task_meta("t1", "needs_attention") == "prepare_timeout"

    async def test_fresh_preparing_is_left_alone(self, db, reconciler):
        sid = await held_pool_session(db, phase="preparing")
        live, now = await observe(reconciler)
        await reconciler._step_prepare_timeout(live, now)
        assert (await db.get_session(sid)).claim_phase == "preparing"

    async def test_no_phase_at_stamp_is_treated_as_already_stuck(self, db, reconciler):
        """A missing ``claim_phase_at`` must not read as "just started"."""
        sid = await held_pool_session(db, phase="preparing", phase_at=None)
        await db.update_session(sid, claim_phase_at=None)
        live, now = await observe(reconciler)
        await reconciler._step_prepare_timeout(live, now)
        assert (await db.get_session(sid)).claim_phase is None

    async def test_claim_timeout_event_is_emitted(self, db, reconciler, orch):
        sid = await held_pool_session(db, phase="preparing", phase_at=time.time() - 1000)
        live, now = await observe(reconciler)
        await reconciler._step_prepare_timeout(live, now)
        emitted = [c.args[0] for c in orch.bus.emit.await_args_list]
        assert "session.claim_timeout" in emitted
        payload = next(
            c.args[1]
            for c in orch.bus.emit.await_args_list
            if c.args[0] == "session.claim_timeout"
        )
        assert payload["session_id"] == sid

    async def test_full_tick_releases_stuck_preparing_without_step_errors(
        self, db, reconciler, caplog
    ):
        """The dispatch bug: ``tick()`` must actually run this step, not
        raise ``TypeError`` into the per-step catch-all every 5s."""
        sid = "s-tick"
        await db.create_session(SessionRecord(
            id=sid, project_id=PROJECT_ID, profile_id="worker", harness="claude", provider="fake",
            name=sid, lifecycle="pool", work_dir="/wd/x", epoch="e", instance_token="t",
            started_at=time.time() - 5, state="running",
            claim_phase="claiming", claim_phase_at=time.time() - 1000,
        ))
        with caplog.at_level(logging.ERROR, logger="src.sessions.reconciler"):
            await reconciler.tick(now=time.time())
        failures = [r for r in caplog.records if "step" in r.message and "failed" in r.message]
        assert failures == [], [r.message for r in failures]
        s = await db.get_session(sid)
        assert s.claim_phase is None


class TestExits:
    async def test_pool_exit_holding_task_returns_task_and_retires_agent(self, db, reconciler,
                                                                         provider):
        sid = await held_pool_session(db)
        provider.peek = AsyncMock(return_value="")
        live, now = await observe(reconciler)
        await reconciler._step_exits(live, now)
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        assert await db.get_task_meta("t1", "needs_attention") == "exited_holding_task"
        assert (await db.get_agent("agent-1")).state == AgentState.RETIRED
        assert await db.get_workspace_for_agent("agent-1") is None
        assert (await db.get_session(sid)).state == "stopped"

    async def test_rapid_crash_quarantines_pool_key(self, db, reconciler, provider, orch):
        sid = await held_pool_session(db)
        await db.update_session(sid, started_at=time.time() - 1)
        provider.peek = AsyncMock(return_value="")
        live, now = await observe(reconciler)
        await reconciler._step_exits(live, now)
        assert orch._pool_quarantine[(PROJECT_ID, "worker")] > time.time()
        assert await db.get_task_meta("t1", "needs_attention") == "rapid_crash"

    async def test_rate_limit_quarantines_pool_key_and_keeps_task_ready(
        self, db, reconciler, provider, orch
    ):
        sid = await held_pool_session(db)
        provider.peek = AsyncMock(return_value="rate limit exceeded, please retry later")
        live, now = await observe(reconciler)
        await reconciler._step_exits(live, now)
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        assert orch._pool_quarantine[(PROJECT_ID, "worker")] > time.time()
        assert (await db.get_session(sid)).state == "stopped"

    async def test_apply_pool_verdict_with_no_task_terminates_cleanly(self, db, reconciler):
        """``task is None`` (e.g. the task row is gone by the time the
        verdict is applied) must not raise -- it just terminates."""
        sid = await held_pool_session(db)
        verdict = ExitVerdict(Verdict.PRODUCTIVE_DEATH, "test")
        row = await db.get_session(sid)
        await reconciler._apply_pool_verdict(row, verdict, None, time.time())
        assert (await db.get_session(sid)).state == "stopped"

    async def test_idle_pool_drain_ack_stops_session(self, db, reconciler):
        sid = await held_pool_session(db)
        await db.release_claim(sid, task_status=TaskStatus.READY, context="x", now=time.time())
        await db.update_session(sid, desired_state="stopped")
        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)
        assert (await db.get_session(sid)).state == "stopped"
        assert (await db.get_agent("agent-1")).state == AgentState.RETIRED


class TestBackstop:
    async def test_idle_pool_session_is_not_stale_for_backstop(self, db, reconciler):
        sid = await held_pool_session(db)
        await db.release_claim(sid, task_status=TaskStatus.READY, context="x", now=time.time())
        await db.update_session(sid, last_activity=time.time() - 10_000)
        live, now = await observe(reconciler)
        await reconciler._step_backstop(live, now)
        assert (await db.get_session(sid)).state == "running"

    async def test_pool_session_active_recently_survives_backstop_despite_its_age(
        self, db, reconciler
    ):
        """Keyed on inactivity, not on how long the session has existed."""
        sid = await held_pool_session(db)
        await db.update_session(sid, started_at=time.time() - 100_000,
                                 last_activity=time.time() - 5)
        live, now = await observe(reconciler)
        await reconciler._step_backstop(live, now)
        assert (await db.get_session(sid)).state == "running"

    async def test_pool_session_inactive_past_the_limit_is_terminated(self, db, reconciler):
        sid = await held_pool_session(db)
        await db.update_session(sid, started_at=time.time() - 100,
                                 last_activity=time.time() - 1000)
        live, now = await observe(reconciler)
        await reconciler._step_backstop(live, now)
        assert (await db.get_session(sid)).state == "stopped"
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        assert await db.get_task_meta("t1", "needs_attention") == "exited_holding_task"


class TestStallLadder:
    async def test_pool_restart_rung_terminates_with_reason_stalled(self, db, reconciler):
        sid = await held_pool_session(db, phase="active")
        now = time.time()
        await db.update_session(sid, last_activity=now - 1000)
        await db.set_task_meta("t1", META_STALL_NUDGES, "5")
        await db.set_task_meta("t1", META_STALL_LAST_ACTION, str(now - 1000))
        live, now = await observe(reconciler, now)
        await reconciler._step_stall_ladder(live, now)
        assert (await db.get_session(sid)).state == "stopped"
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)


class TestPrepareTimeoutFlagGate:
    """I7: pool sessions can only exist when ``swarm.enabled`` is true."""

    async def test_skips_queries_when_swarm_disabled(self, db, reconciler):
        reconciler.config.swarm.enabled = False
        db.list_sessions = AsyncMock(side_effect=AssertionError("must not query"))
        await reconciler._step_prepare_timeout([], time.time())

    async def test_queries_when_swarm_enabled(self, db, reconciler):
        reconciler.config.swarm.enabled = True
        spy = AsyncMock(return_value=[])
        db.list_sessions = spy
        await reconciler._step_prepare_timeout([], time.time())
        assert spy.await_count == 2  # claiming + preparing


class TestOrphans:
    async def test_pool_orphan_terminates_with_reason_orphaned(self, db, reconciler):
        sid = await held_pool_session(db)
        await db.transition_task("t1", TaskStatus.COMPLETED, context="test", force=True)
        live, now = await observe(reconciler)
        await reconciler._step_orphans(live, now)
        assert (await db.get_session(sid)).state == "stopped"
