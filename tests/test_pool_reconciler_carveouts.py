"""Session reconciler — pool lifecycle carve-outs (spec §10.4, §11.2, §11.4)."""

from __future__ import annotations

import json
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import insert

from src.commands.claim_commands import write_claim_file
from src.config import DatabaseConfig, AppConfig, DiscordConfig
from src.database import Database
from src.database.queries.claim_queries import ACCEPTED_CLOSE_KEY
from src.database.tables import (
    integration_branch_owners,
    integration_parent_episodes,
    integration_repair_operations,
    integration_repair_stages,
)
from src.doctor.models import Severity
from src.doctor.pool_checks import run_check
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
from src.orchestrator import Orchestrator
from src.sessions import SessionProviderRegistry
from src.sessions.exit_classifier import ExitVerdict, Verdict
from src.sessions.fake import FakeProvider
from src.sessions.provider import SessionHandle, SessionSpec
from src.sessions.reconciler import (
    DRAIN_ACK_KEY,
    META_STALL_LAST_ACTION,
    META_STALL_NUDGES,
    SessionReconciler,
)
from tests.db_fixtures import lease_dsn

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("test.db"))
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
        database=DatabaseConfig(url=lease_dsn("test.db")),
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
    o.agent_questions.db = db
    o.git = MagicMock()
    o.bus.emit = AsyncMock()
    o.session_providers = registry
    return o


@pytest.fixture
def reconciler(db, orch, registry):
    return SessionReconciler(
        db, orch.config, registry, bus=orch.bus, orchestrator=orch, epoch="epoch-new"
    )


async def held_pool_session(
    db, sid="s1", agent_id="agent-1", phase="active", phase_at=None, task_id="t1"
):
    # ``tasks.assigned_agent_id`` and ``agents.current_task_id`` form a
    # cycle -- insert both with the cross-reference unset, then backfill.
    await db.create_task(Task(id=task_id, project_id=PROJECT_ID, title=task_id, description=task_id,
                              status=TaskStatus.IN_PROGRESS, claim_epoch=1, profile_id="worker",
                              route_source="legacy"))
    await db.create_agent(Agent(id=agent_id, name=agent_id, profile_id="worker",
                                state=AgentState.BUSY))
    await db.update_task(task_id, assigned_agent_id=agent_id)
    await db.update_agent(agent_id, current_task_id=task_id)
    await db.create_workspace(Workspace(id=f"ws-{agent_id}", project_id=PROJECT_ID,
                                        workspace_path=f"/wd/{agent_id}",
                                        source_type=RepoSourceType.LINK, kind_id="project-repo",
                                        locked_by_agent_id=agent_id, locked_by_task_id=task_id))
    await db.create_session(SessionRecord(
        id=sid, project_id=PROJECT_ID, profile_id="worker", harness="claude", provider="fake",
        name=sid, lifecycle="pool", work_dir=f"/wd/{agent_id}", epoch="e", instance_token="t",
        started_at=time.time() - 600, state="running", agent_id=agent_id, task_id=task_id,
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
    async def test_pool_exit_holding_task_returns_task_and_releases_worker(self, db, reconciler,
                                                                         provider):
        sid = await held_pool_session(db)
        provider.peek = AsyncMock(return_value="")
        live, now = await observe(reconciler)
        await reconciler._step_exits(live, now)
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        assert await db.get_task_meta("t1", "needs_attention") == "exited_holding_task"
        assert (await db.get_agent("agent-1")).state == AgentState.IDLE
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

    async def test_rate_limit_pauses_the_task_briefly_and_quarantines_nothing(
        self, db, reconciler, provider, orch
    ):
        """provider-failover D13 (``mode: enforce``): a rate-limit exit is the
        provider's, so its state -- not a 900 s key quarantine -- decides what
        launches next; one uncorroborated exit pauses the task
        ``launch.suspect_backoff_seconds`` with its ``provider_pause``."""
        sid = await held_pool_session(db)
        provider.peek = AsyncMock(return_value="rate limit exceeded, please retry later")
        live, now = await observe(reconciler)
        await reconciler._step_exits(live, now)
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id, t.retry_count) == (TaskStatus.PAUSED, None, 0)
        assert t.resume_after is not None and t.resume_after <= time.time() + 31
        assert (await db.get_task_meta("t1", "provider_pause"))["context"] == "provider_suspect"
        assert orch._pool_quarantine == {}
        assert (await db.get_session(sid)).state == "stopped"

    async def test_observe_mode_rate_limit_quarantines_pool_key_and_keeps_task_ready(
        self, db, reconciler, provider, orch
    ):
        orch.config.provider_failover.mode = "observe"
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
        assert (await db.get_session(sid)).task_id is None
        await db.update_session(sid, desired_state="stopped")
        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)
        assert (await db.get_session(sid)).state == "stopped"
        assert (await db.get_agent("agent-1")).state == AgentState.IDLE

    @pytest.mark.parametrize("reclaimed", [False, True])
    async def test_draining_pool_stops_after_task_is_reclaimed_elsewhere(
        self, db, reconciler, provider, reclaimed
    ):
        sid = await held_pool_session(db)
        await db.create_agent(Agent(id="agent-2", name="agent-2", profile_id="worker",
                                    state=AgentState.BUSY))
        status = TaskStatus.IN_PROGRESS if reclaimed else TaskStatus.READY
        new_agent = "agent-2" if reclaimed else None
        await db.update_task("t1", status=status, assigned_agent_id=new_agent, claim_epoch=2)
        await db.update_agent("agent-2", current_task_id="t1" if reclaimed else None)
        await db.update_session(sid, state="draining", desired_state="stopped",
                                last_claim_epoch=1)
        provider.stop = AsyncMock(wraps=provider.stop)

        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)

        session = await db.get_session(sid)
        task = await db.get_task("t1")
        assert (session.state, session.task_id, session.claim_phase) == (
            "stopped", None, None
        )
        assert (task.status, task.assigned_agent_id, task.claim_epoch) == (
            status, new_agent, 2
        )
        assert (await db.get_agent("agent-2")).current_task_id == ("t1" if reclaimed else None)
        assert await db.get_workspace_for_agent("agent-1") is None
        provider.stop.assert_awaited_once()

    async def test_draining_pool_keeps_its_active_claim(self, db, reconciler):
        sid = await held_pool_session(db)
        await db.update_task("t1", claim_epoch=1)
        await db.update_session(sid, state="draining", desired_state="stopped",
                                last_claim_epoch=1)
        session = await db.get_session(sid)
        task = await db.get_task("t1")
        assert (session.last_claim_epoch, task.claim_epoch, task.assigned_agent_id) == (
            1, 1, "agent-1"
        )
        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)
        session = await db.get_session(sid)
        assert (session.state, session.task_id, session.claim_phase) == (
            "draining", "t1", "active"
        )


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
    @pytest.mark.parametrize("status", [TaskStatus.PAUSED, TaskStatus.BLOCKED, TaskStatus.FAILED])
    @pytest.mark.parametrize("handoff_state", ["attached", "handoff_pending"])
    async def test_integration_owned_non_live_claim_is_retained(
        self, db, reconciler, status, handoff_state
    ):
        """An attached branch owner is durable evidence that cleanup must wait."""
        sid = await held_pool_session(db)
        await db.transition_task("t1", status, context="test", force=True)
        await db.update_session(sid, state="draining", desired_state="stopped")
        async with db.immediate() as conn:
            await conn.execute(
                insert(integration_branch_owners).values(
                    id="owner-1",
                    repository_id="repo-1",
                    ref="aq/t1",
                    owner_id="t1",
                    owner_role="worker",
                    fence_token=1,
                    handoff_state=handoff_state,
                    session_id=sid,
                    workspace_id="ws-agent-1",
                    created_at=time.time(),
                    updated_at=time.time(),
                )
            )

        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)
        await reconciler._step_orphans(live, now)

        session = await db.get_session(sid)
        workspace = await db.get_workspace("ws-agent-1")
        assert (session.task_id, session.claim_phase) == ("t1", "active")
        assert workspace.locked_by_agent_id == "agent-1"
        assert await db.get_pending_messages("session", sid) == []

    async def test_owned_orphan_does_not_block_reclaiming_an_unrelated_orphan(self, db, reconciler):
        sid = await held_pool_session(db)
        other_sid = await held_pool_session(db, "s2", "agent-2", task_id="t2")
        await db.transition_task("t1", TaskStatus.PAUSED, context="test", force=True)
        await db.transition_task("t2", TaskStatus.BLOCKED, context="test", force=True)
        async with db.immediate() as conn:
            await conn.execute(
                insert(integration_branch_owners).values(
                    id="owner-1", repository_id="repo-1", ref="aq/t1", owner_id="t1",
                    owner_role="worker", fence_token=1, handoff_state="attached", session_id=sid,
                    workspace_id="ws-agent-1", created_at=time.time(), updated_at=time.time(),
                )
            )

        live, now = await observe(reconciler)
        await reconciler._step_orphans(live, now)

        assert (await db.get_session(sid)).task_id == "t1"
        assert (await db.get_workspace("ws-agent-1")).locked_by_agent_id == "agent-1"
        assert (await db.get_session(other_sid)).task_id is None
        assert (await db.get_workspace("ws-agent-2")).locked_by_agent_id == "agent-2"

    async def test_released_integration_owner_allows_non_live_claim_reclaim(self, db, reconciler):
        sid = await held_pool_session(db)
        await db.transition_task("t1", TaskStatus.PAUSED, context="test", force=True)
        async with db.immediate() as conn:
            await conn.execute(
                insert(integration_branch_owners).values(
                    id="owner-1", repository_id="repo-1", ref="aq/t1", owner_id="t1",
                    owner_role="worker", fence_token=1, handoff_state="released", session_id=None,
                    workspace_id=None, confirmed_workspace_id="ws-agent-1", created_at=time.time(),
                    updated_at=time.time(),
                )
            )

        live, now = await observe(reconciler)
        await reconciler._step_orphans(live, now)

        assert (await db.get_session(sid)).task_id is None
        assert (await db.get_workspace("ws-agent-1")).locked_by_agent_id == "agent-1"

    async def test_termination_retains_attached_integration_owner_bindings(self, db):
        sid = await held_pool_session(db)
        async with db.immediate() as conn:
            await conn.execute(
                insert(integration_branch_owners).values(
                    id="owner-1", repository_id="repo-1", ref="aq/t1", owner_id="t1",
                    owner_role="worker", fence_token=1, handoff_state="attached", session_id=sid,
                    workspace_id="ws-agent-1", created_at=time.time(), updated_at=time.time(),
                )
            )

        result = await db.terminate_pool_session(sid, reason="test")

        assert not result.released
        assert (await db.get_session(sid)).task_id == "t1"
        assert (await db.get_workspace("ws-agent-1")).locked_by_agent_id == "agent-1"

    @pytest.mark.parametrize("status", [TaskStatus.PAUSED, TaskStatus.BLOCKED, TaskStatus.FAILED])
    async def test_non_live_active_pool_claim_is_released_and_worker_is_notified(
        self, db, reconciler, status
    ):
        sid = await held_pool_session(db)
        await db.transition_task("t1", status, context="test", force=True)

        live, now = await observe(reconciler)
        await reconciler._step_orphans(live, now)

        session = await db.get_session(sid)
        task = await db.get_task("t1")
        workspace = await db.get_workspace("ws-agent-1")
        messages = await db.get_pending_messages("session", sid)
        assert (session.task_id, session.claim_phase) == (None, None)
        assert task.status is status
        assert workspace.locked_by_agent_id == "agent-1"
        assert len(messages) == 1
        assert messages[0].from_kind == "system"
        assert messages[0].to_id == sid
        assert status.value in messages[0].body
        finding = await run_check(db, "pools.stuck", config=None)
        assert finding.severity is Severity.OK

    async def test_live_active_pool_claim_is_not_reclaimed(self, db, reconciler):
        sid = await held_pool_session(db)

        live, now = await observe(reconciler)
        await reconciler._step_orphans(live, now)

        session = await db.get_session(sid)
        workspace = await db.get_workspace("ws-agent-1")
        assert (session.task_id, session.claim_phase) == ("t1", "active")
        assert workspace.locked_by_agent_id == "agent-1"
        assert await db.get_pending_messages("session", sid) == []

    @pytest.mark.parametrize("status", [TaskStatus.PAUSED, TaskStatus.BLOCKED])
    async def test_reclaim_does_not_overwrite_a_concurrent_resume(
        self, db, reconciler, monkeypatch, status
    ):
        """The release transaction, not the earlier orphan read, owns the race."""
        sid = await held_pool_session(db)
        transition_kwargs = {"resume_after": time.time() + 60} if status is TaskStatus.PAUSED else {}
        await db.transition_task("t1", status, context="test", force=True, **transition_kwargs)

        real_release_claim = db.release_claim

        async def resume_before_release(*args, **kwargs):
            await db.transition_task("t1", TaskStatus.IN_PROGRESS, context="concurrent_resume", force=True)
            return await real_release_claim(*args, **kwargs)

        monkeypatch.setattr(db, "release_claim", resume_before_release)
        live, now = await observe(reconciler)
        await reconciler._step_orphans(live, now)

        task = await db.get_task("t1")
        session = await db.get_session(sid)
        workspace = await db.get_workspace("ws-agent-1")
        assert task.status is TaskStatus.IN_PROGRESS
        assert (session.task_id, session.claim_phase) == ("t1", "active")
        assert workspace.locked_by_agent_id == "agent-1"
        assert await db.get_pending_messages("session", sid) == []

    async def test_terminal_pool_task_release_removes_claim_file(
        self, db, reconciler, tmp_path
    ):
        sid = await held_pool_session(db, phase="preparing")
        work_dir = tmp_path / "pool-slot"
        await db.update_session(sid, work_dir=str(work_dir))
        claim_file = work_dir / ".aq" / "claim.json"
        write_claim_file(
            str(work_dir),
            {"task_id": "t1", "claim_epoch": 1, "session_id": sid},
        )
        await db.transition_task("t1", TaskStatus.COMPLETED, context="test", force=True)

        live, now = await observe(reconciler)
        await reconciler._step_orphans(live, now)

        assert not claim_file.exists()

    async def test_lost_terminal_release_preserves_successor_claim_file(
        self, db, reconciler, tmp_path, monkeypatch
    ):
        sid = await held_pool_session(db)
        work_dir = tmp_path / "pool-slot"
        await db.update_session(sid, work_dir=str(work_dir), last_claim_epoch=1)
        successor = {"task_id": "t2", "claim_epoch": 2, "session_id": sid}
        write_claim_file(
            str(work_dir),
            {"task_id": "t1", "claim_epoch": 1, "session_id": sid},
        )
        await db.transition_task("t1", TaskStatus.COMPLETED, context="test", force=True)

        async def lose_release(*args, **kwargs):
            write_claim_file(str(work_dir), successor)
            return MagicMock(released=False)

        monkeypatch.setattr(db, "release_claim", lose_release)
        live, now = await observe(reconciler)
        await reconciler._step_orphans(live, now)

        claim_file = work_dir / ".aq" / "claim.json"
        assert json.loads(claim_file.read_text()) == successor

    async def test_terminal_pool_task_releases_hold_without_terminating_worker(self, db, reconciler):
        sid = await held_pool_session(db)
        await db.transition_task("t1", TaskStatus.COMPLETED, context="test", force=True)
        live, now = await observe(reconciler)
        await reconciler._step_orphans(live, now)
        session = await db.get_session(sid)
        assert (session.state, session.desired_state, session.task_id) == (
            "running",
            "stopped",
            None,
        )
        assert (await db.get_agent("agent-1")).state == AgentState.IDLE
        assert (await db.get_workspace("ws-agent-1")).locked_by_agent_id == "agent-1"


# ---------------------------------------------------------------------------
# A drain-acked worker still holding a retired integration delegate
# (bold-impact-53).  The close refuses forever ("this delegate is retired"),
# the worker acks, and the pool branch must not wait for a close that cannot
# come -- but only for a durably proven retirement, and only on the agent's
# own ack.
# ---------------------------------------------------------------------------

SHA = "a" * 40


async def _integration_delegate(
    db, *, operation_state="completed", stage_state="passed", active_stage=0, task_id="t1"
):
    """Make *task_id* the stage-0 repair delegate of a parent repair operation."""
    if await db.get_repo("repo") is None:
        await db.create_repo(
            RepoConfig(id="repo", project_id=PROJECT_ID, source_type=RepoSourceType.LINK)
        )
    await db.create_task(Task(id="parent", project_id=PROJECT_ID, title="parent",
                              description="", status=TaskStatus.IN_PROGRESS))
    async with db.immediate() as conn:
        await conn.execute(insert(integration_parent_episodes).values(
            id="episode", parent_task_id="parent", repository_id="repo", generation=1,
            pre_collection_checkpoint_sha=SHA, created_at=1.0,
        ))
        await conn.execute(insert(integration_repair_operations).values(
            id="operation", target_kind="parent", parent_task_id="parent",
            episode_id="episode", active_stage=active_stage, state=operation_state,
            policy_snapshot={}, artifact_snapshot={}, required_check_version="checks-v1",
            created_at=1.0, updated_at=1.0,
        ))
        for ordinal in range(active_stage + 1):
            await conn.execute(insert(integration_repair_stages).values(
                operation_id="operation", ordinal=ordinal, policy={}, starting_sha=SHA,
                repair_task_id=task_id if ordinal == 0 else None,
                writer_kind="repair_delegate" if ordinal == 0 else None,
                attempts=0, state=stage_state if ordinal == 0 else "active",
            ))


class TestRetiredDelegateDrain:
    @pytest.fixture
    async def acked(self, db, provider):
        """A pool worker that ran ``aq session drain-ack`` over its still-open claim."""
        sid = await held_pool_session(db)
        await db.update_task("t1", claim_epoch=1)
        await provider.start(SessionSpec(
            session_name=sid, work_dir="/wd/agent-1", command=("claude",), instance_token="t",
        ))
        await db.update_session(sid, state="draining", desired_state="stopped",
                                last_claim_epoch=1)
        await provider.set_meta(SessionHandle(sid, "fake", "t"), DRAIN_ACK_KEY, "1")
        return sid

    async def _assert_still_waiting(self, db, provider, sid):
        assert sid in provider.sessions
        session = await db.get_session(sid)
        assert (session.state, session.task_id, session.claim_phase) == (
            "draining", "t1", "active"
        )
        task = await db.get_task("t1")
        assert (task.status, task.assigned_agent_id, task.claim_epoch) == (
            TaskStatus.IN_PROGRESS, "agent-1", 1
        )
        assert (await db.get_workspace("ws-agent-1")).locked_by_task_id == "t1"

    @pytest.mark.parametrize(("operation_state", "stage_state", "active_stage", "disposition"), [
        ("completed", "passed", 0, "superseded"),
        ("cancelled", "active", 0, "cancelled"),
        ("escalated", "expired", 1, "superseded"),
    ])
    async def test_acked_worker_of_a_retired_delegate_is_stopped(
        self, db, reconciler, provider, orch, acked, operation_state, stage_state,
        active_stage, disposition,
    ):
        await _integration_delegate(db, operation_state=operation_state,
                                    stage_state=stage_state, active_stage=active_stage)

        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)

        # Stopped through the same teardown a supervisor kill reaches.
        assert acked not in provider.sessions
        session = await db.get_session(acked)
        assert (session.state, session.desired_state, session.end_reason) == (
            "stopped", "stopped", "retired_delegate"
        )
        # Back on the frontier like any pool teardown -- never a manufactured pass.
        task = await db.get_task("t1")
        assert (task.status, task.assigned_agent_id) == (TaskStatus.READY, None)
        assert await db.get_task_meta("t1", "outcome") is None
        bodies = [c["body"] for c in (await db.list_task_comments("t1"))["comments"]]
        assert any(
            "integration operation operation" in body and "drain-ack" in body
            for body in bodies
        )
        stopped = [c for c in orch.bus.emit.await_args_list
                   if c.args[0] == "session.retired_delegate_stopped"]
        assert len(stopped) == 1
        assert stopped[0].args[1]["task_id"] == "t1"
        assert stopped[0].args[1]["retirement"]["disposition"] == disposition

        if operation_state == "escalated":
            # The live operation keeps the ticket off the frontier for its successor.
            return
        from src.integration.delegate_release import release_delegates

        released = await release_delegates(db, now=now, released_by="integration_service")
        assert [row["task_id"] for row in released] == ["t1"]
        assert (await db.get_task("t1")).status == TaskStatus.FAILED
        record = await db.get_task_meta("t1", "integration_retirement")
        assert record["disposition"] == disposition

    async def test_an_attached_owner_keeps_the_stopped_writers_evidence(
        self, db, reconciler, provider, acked
    ):
        """Stop proof comes first; owner recovery preserves the work before any release."""
        await _integration_delegate(db)
        async with db.immediate() as conn:
            await conn.execute(insert(integration_branch_owners).values(
                id="owner-1", repository_id="repo", ref="aq/parent", owner_id="t1",
                owner_role="repair", fence_token=4, handoff_state="attached",
                session_id=acked, workspace_id="ws-agent-1", created_at=1.0, updated_at=1.0,
            ))

        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)

        assert acked not in provider.sessions
        session = await db.get_session(acked)
        assert (session.state, session.desired_state) == ("stopped", "stopped")
        assert (session.task_id, session.claim_phase) == ("t1", "active")
        task = await db.get_task("t1")
        assert (task.status, task.assigned_agent_id) == (TaskStatus.IN_PROGRESS, "agent-1")
        workspace = await db.get_workspace("ws-agent-1")
        assert (workspace.locked_by_agent_id, workspace.locked_by_task_id) == ("agent-1", "t1")
        owner = await db.get_integration_delegate_cleanup("t1")
        assert [(b["code"], b.get("handoff_state")) for b in owner] == [
            ("branch_owner_retained", "attached"), ("workspace_locked", None),
        ]

    @pytest.mark.parametrize("seat", [
        "active", "awaiting_completion", "human_required", "current_terminal", "unrelated",
    ])
    async def test_an_unproven_retirement_keeps_waiting_for_the_close(
        self, db, reconciler, provider, acked, seat
    ):
        shapes = {
            "active": {"operation_state": "active", "stage_state": "active"},
            "awaiting_completion": {
                "operation_state": "active", "stage_state": "awaiting_completion",
            },
            # Human gate: a resume may revive this stage with this very writer.
            "human_required": {"operation_state": "human_required", "stage_state": "expired"},
            "current_terminal": {"operation_state": "escalated", "stage_state": "expired"},
        }
        if seat in shapes:
            await _integration_delegate(db, **shapes[seat])

        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)

        await self._assert_still_waiting(db, provider, acked)

    async def test_without_the_agents_own_ack_a_retired_delegate_is_not_stopped(
        self, db, reconciler, provider, acked
    ):
        """An operator drain is not the worker saying its work is saved."""
        await _integration_delegate(db)
        await provider.set_meta(SessionHandle(acked, "fake", "t"), DRAIN_ACK_KEY, "0")

        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)

        await self._assert_still_waiting(db, provider, acked)

    async def test_an_unconfirmed_stop_releases_nothing_and_retries(
        self, db, reconciler, provider, acked
    ):
        await _integration_delegate(db)
        real_stop = provider.stop
        provider.stop = AsyncMock(side_effect=RuntimeError("tmux server unreachable"))

        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)

        await self._assert_still_waiting(db, provider, acked)
        assert provider.stop.await_count == 1

        provider.stop = AsyncMock(wraps=real_stop)
        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)
        assert acked not in provider.sessions
        assert (await db.get_session(acked)).state == "stopped"


# ---------------------------------------------------------------------------
# A drain-acked worker still bound to a task whose close already committed
# (wise-willow).  A daemon restart lost the close after its terminal
# transition: the task is COMPLETED, but the claim and the attached branch
# owner still name the session, so ``release_displaced_pool_claim`` keeps
# refusing and nothing stopped the idle worker.
# ---------------------------------------------------------------------------


async def _attached_owner(db, sid, *, task_id="t1"):
    if await db.get_repo("repo") is None:
        await db.create_repo(
            RepoConfig(id="repo", project_id=PROJECT_ID, source_type=RepoSourceType.LINK)
        )
    async with db.immediate() as conn:
        await conn.execute(insert(integration_branch_owners).values(
            id="owner-1", repository_id="repo", ref=f"aq/{task_id}", owner_id=task_id,
            owner_role="worker", fence_token=2, handoff_state="attached",
            session_id=sid, workspace_id="ws-agent-1", created_at=1.0, updated_at=1.0,
        ))


async def _completion(db, *, completed_at=None, task_id="t1", outcome="pass"):
    from src.models import TaskCompletion

    await db.save_task_completion(TaskCompletion(
        id=f"completion-{task_id}-{completed_at}", task_id=task_id, outcome=outcome,
        summary="Pushed the fix.",
        completed_at=time.time() if completed_at is None else completed_at,
    ))


async def _code_receipt(db, *, task_id="t1"):
    from src.database.tables import task_delivery_receipts

    async with db.immediate() as conn:
        await conn.execute(insert(task_delivery_receipts).values(
            id=f"receipt-{task_id}", domain_key=f"delivery:{task_id}", source_task_id=task_id,
            repository_id="repo", target_branch="main", disposition="code",
            created_at=time.time(),
        ))


async def _accepted_close(db, session_id, *, claim_epoch=1, task_id="t1"):
    """The identity the transition accepting a close records in its transaction."""
    await db.set_task_meta(task_id, ACCEPTED_CLOSE_KEY, {
        "completion_id": f"completion-{task_id}", "session_id": session_id,
        "claim_epoch": claim_epoch,
    })


class TestSettledClaimDrain:
    @pytest.fixture
    async def bound(self, db, provider):
        """The ambiguous close: the terminal transition committed and nothing after it.

        The claim, the workspace hold and the attached branch owner still name
        the session; the worker then ran ``aq session drain-ack``.
        """
        sid = await held_pool_session(db)
        await db.update_task("t1", claim_epoch=1)
        await _attached_owner(db, sid)
        await provider.start(SessionSpec(
            session_name=sid, work_dir="/wd/agent-1", command=("claude",), instance_token="t",
        ))
        await db.update_session(sid, state="draining", desired_state="stopped",
                                last_claim_epoch=1)
        await provider.set_meta(SessionHandle(sid, "fake", "t"), DRAIN_ACK_KEY, "1")
        return sid

    async def _close_committed(self, db, status=TaskStatus.COMPLETED):
        await db.update_task("t1", status=status, assigned_agent_id=None)

    async def _owner_state(self, db):
        async with db._engine.connect() as conn:
            return (await conn.execute(
                integration_branch_owners.select().where(integration_branch_owners.c.id == "owner-1")
            )).mappings().one()["handoff_state"]

    async def _assert_still_bound(self, db, provider, sid, status):
        assert sid in provider.sessions
        session = await db.get_session(sid)
        assert (session.state, session.task_id, session.claim_phase) == (
            "draining", "t1", "active"
        )
        assert (await db.get_task("t1")).status == status
        assert await self._owner_state(db) == "attached"
        assert (await db.get_workspace("ws-agent-1")).locked_by_task_id == "t1"

    @pytest.mark.parametrize(("status", "evidence"), [
        (TaskStatus.COMPLETED, "completion_record"),
        # sound-orbit: the restart lost the completion record; delivery wrote a receipt.
        (TaskStatus.COMPLETED, "delivery_receipt"),
        (TaskStatus.FAILED, "completion_record"),
        # bold-impact-53's wait: no record and no receipt yet, but the transition
        # that accepted this session's close recorded its identity.
        (TaskStatus.COMPLETED, "accepted_close"),
        (TaskStatus.FAILED, "accepted_close"),
    ])
    async def test_acked_worker_of_a_settled_task_is_stopped(
        self, db, reconciler, provider, orch, bound, status, evidence
    ):
        await self._close_committed(db, status)
        if evidence == "completion_record":
            await _completion(db, outcome="pass" if status == TaskStatus.COMPLETED else "fail")
        elif evidence == "delivery_receipt":
            await _code_receipt(db)
        else:
            await _accepted_close(db, bound)
        completions = await db.get_task_completions("t1")

        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)

        # Stopped through the teardown a supervisor kill reaches, stop confirmed.
        assert bound not in provider.sessions
        session = await db.get_session(bound)
        assert (session.state, session.desired_state, session.end_reason) == (
            "stopped", "stopped", "settled_claim"
        )
        # The owner keeps the claim, hold and binding for owner recovery.
        assert (session.task_id, session.claim_phase) == ("t1", "active")
        workspace = await db.get_workspace("ws-agent-1")
        assert (workspace.locked_by_agent_id, workspace.locked_by_task_id) == ("agent-1", "t1")
        assert await self._owner_state(db) == "attached"
        assert (await db.get_agent("agent-1")).state == AgentState.RETIRED
        # Completion truth is untouched: same status, no new close record.
        task = await db.get_task("t1")
        assert (task.status, task.assigned_agent_id, task.claim_epoch) == (status, None, 1)
        assert await db.get_task_completions("t1") == completions
        bodies = [c["body"] for c in (await db.list_task_comments("t1"))["comments"]]
        assert any(
            f"is {status.value} with" in body and "drain-ack" in body for body in bodies
        )
        stopped = [c for c in orch.bus.emit.await_args_list
                   if c.args[0] == "session.settled_claim_stopped"]
        assert len(stopped) == 1
        assert stopped[0].args[1]["task_id"] == "t1"
        assert stopped[0].args[1]["settlement"]["evidence"]["kind"] == evidence

    @pytest.mark.parametrize("shape", [
        "active", "blocked", "no_evidence", "earlier_close", "requeued_epoch", "live_seat",
        "closed_elsewhere", "earlier_claim_accepted", "unaccepted_close_metadata",
    ])
    async def test_an_open_or_unsettled_task_keeps_waiting(
        self, db, reconciler, provider, bound, shape
    ):
        status = TaskStatus.COMPLETED
        if shape == "active":
            # The worker acked over its own open claim: a premature drain.
            status = TaskStatus.IN_PROGRESS
            await _completion(db)
        elif shape == "blocked":
            status = TaskStatus.BLOCKED
            await self._close_committed(db, status)
            await _completion(db, outcome="fail")
        elif shape == "no_evidence":
            await self._close_committed(db)
        elif shape == "earlier_close":
            # A reopened task's previous close says nothing about this claim.
            await self._close_committed(db)
            attempt_start = (await db.get_session(bound)).started_at
            await _completion(db, completed_at=attempt_start - 100)
        elif shape == "closed_elsewhere":
            # An accepted close speaks only for the session it names.
            await self._close_committed(db)
            await _accepted_close(db, "another-session")
        elif shape == "earlier_claim_accepted":
            # This session's close of an earlier claim on the task says
            # nothing about the claim it holds now.
            await self._close_committed(db)
            await _accepted_close(db, bound, claim_epoch=0)
        elif shape == "unaccepted_close_metadata":
            # ``task close`` writes its metadata before the close is accepted:
            # a refused close leaves it, and an unrelated failure then ended
            # the task.  Only the accepted-close marker proves this session's
            # close committed.
            await db.set_task_meta("t1", "close_session_id", bound)
            await db.set_task_meta("t1", "outcome", "pass")
            status = TaskStatus.FAILED
            await self._close_committed(db, status)
        elif shape == "requeued_epoch":
            await self._close_committed(db)
            await db.update_task("t1", claim_epoch=2)
            await _completion(db)
        else:
            # A running operation still owns the task as its repair writer.
            await self._close_committed(db)
            await _completion(db)
            await _integration_delegate(
                db, operation_state="active", stage_state="awaiting_completion"
            )

        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)

        await self._assert_still_bound(db, provider, bound, status)

    async def test_without_the_agents_own_ack_a_settled_task_is_not_stopped(
        self, db, reconciler, provider, bound
    ):
        """An operator drain is not the worker saying its work is saved."""
        await self._close_committed(db)
        await _completion(db)
        await provider.set_meta(SessionHandle(bound, "fake", "t"), DRAIN_ACK_KEY, "0")

        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)

        await self._assert_still_bound(db, provider, bound, TaskStatus.COMPLETED)

    async def test_a_close_still_running_here_is_not_stopped_mid_handoff(
        self, db, reconciler, provider, orch, bound
    ):
        """The worker acked after its close timed out client-side; the pipeline runs on.

        The terminal transition and its accepted-close marker are committed,
        so only the task's control lock says the handoff has not finished.
        """
        await self._close_committed(db)
        await _accepted_close(db, bound)

        async with orch._task_control_lock("t1"):
            live, now = await observe(reconciler)
            await reconciler._step_drain_ack(live, now)
            await self._assert_still_bound(db, provider, bound, TaskStatus.COMPLETED)

        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)
        assert bound not in provider.sessions
        assert (await db.get_session(bound)).end_reason == "settled_claim"

    async def test_an_unconfirmed_stop_releases_nothing_and_retries(
        self, db, reconciler, provider, bound
    ):
        await self._close_committed(db)
        await _code_receipt(db)
        real_stop = provider.stop
        provider.stop = AsyncMock(side_effect=RuntimeError("tmux server unreachable"))

        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)

        await self._assert_still_bound(db, provider, bound, TaskStatus.COMPLETED)
        assert provider.stop.await_count == 1

        provider.stop = AsyncMock(wraps=real_stop)
        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)
        assert bound not in provider.sessions
        session = await db.get_session(bound)
        assert (session.state, session.task_id) == ("stopped", "t1")

    async def test_a_retired_delegate_is_stopped_under_its_own_proof_only(
        self, db, reconciler, provider, orch, bound
    ):
        """A task proving both takes the retirement; an unconfirmed stop is not redone."""
        await self._close_committed(db)
        await _completion(db)
        await _integration_delegate(db)
        real_stop = provider.stop
        provider.stop = AsyncMock(side_effect=RuntimeError("tmux server unreachable"))

        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)

        assert provider.stop.await_count == 1
        await self._assert_still_bound(db, provider, bound, TaskStatus.COMPLETED)

        provider.stop = AsyncMock(wraps=real_stop)
        live, now = await observe(reconciler)
        await reconciler._step_drain_ack(live, now)
        assert (await db.get_session(bound)).end_reason == "retired_delegate"
        emitted = [c.args[0] for c in orch.bus.emit.await_args_list]
        assert "session.retired_delegate_stopped" in emitted
        assert "session.settled_claim_stopped" not in emitted
