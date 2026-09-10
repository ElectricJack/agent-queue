"""_reconcile_pools / _launch_pool_session — spec §11 (fake provider)."""

from __future__ import annotations

import dataclasses
import logging
import os
import time
import uuid

from unittest.mock import AsyncMock, MagicMock

import pytest
from dataclasses import replace
from sqlalchemy import insert

from src.commands.claim_commands import CLAIM_FILE, write_claim_file
from src.config import DatabaseConfig, AppConfig, DiscordConfig
from src.database import Database
from src.database.tables import integration_branch_owners
from src.intelligence_classes import IntelligenceClass
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
from tests.db_fixtures import lease_dsn

PROJECT_ID = "proj"


class _FakeSlotManager:
    """Stubs the git-level slot creation ``WorktreeSlotManager`` normally does.

    ``ensure_slots`` just writes the DB rows a real slot would end up with —
    no git, no filesystem — so worktree-mode tests exercise
    ``_ensure_worktree_slots`` / ``_launch_pool_session`` without a real repo.
    """

    def __init__(self, db):
        self.db = db

    async def ensure_slots(self, project, base_ws, kind, count):
        slots = await self.db.list_slots_for_base(base_ws.id)
        for idx in range(len(slots), count):
            ws = Workspace(
                id=f"{base_ws.id}-slot{idx}",
                project_id=base_ws.project_id,
                workspace_path=f"{base_ws.workspace_path}-slot{idx}",
                source_type=RepoSourceType.WORKTREE,
                kind_id=base_ws.kind_id,
                slot_index=idx,
                base_workspace_id=base_ws.id,
            )
            await self.db.create_workspace(ws)
            slots.append(ws)
        return slots


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    # These fixtures model independent clones; slot provisioning has its own tests.
    kind = await database.resolve_workspace_kind("__system__", "project-repo")
    await database.upsert_workspace_kind(replace(kind, mode="exclusive-clone"))
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
        database=DatabaseConfig(url=lease_dsn("test.db")),
        data_dir=str(tmp_path / "data"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    cfg.swarm.max_starts_per_tick = 5
    o = Orchestrator(cfg)
    o.session_spec_builder._intelligence_classes = {
        "standard-medium": IntelligenceClass(
            "standard-medium",
            "Standard",
            "",
            {"anthropic": {"model": "claude-sonnet-5"}},
        ),
    }
    o.db = db
    # ``AgentReconciler`` was built in ``__init__`` against the (real,
    # uninitialized) db the constructor saw -- point it at the test db too
    # so ``_schedule()`` (which reconciles agents first) works end to end.
    o._agent_reconciler._db = db
    o.git = MagicMock()
    o._ensure_control_files_excluded = AsyncMock(return_value=True)
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
    yield o
    await o.wait_for_pool_launches(cancel=True)


async def ready(db, tid, *, profile_id="worker", intelligence_class=None):
    await db.create_task(
        Task(
            id=tid,
            project_id=PROJECT_ID,
            title=tid,
            description=tid,
            status=TaskStatus.READY,
            profile_id=profile_id,
            intelligence_class=intelligence_class,
        )
    )


class TestReconcilePools:
    async def test_pool_handoff_detects_git_without_literal_dot_git(
        self, orch, db, tmp_path
    ):
        """Pool launch protects its checkout before the session receives it."""
        from src.api.auth import SessionTokenStore

        orch.config.worktrees.enabled = False
        orch.token_store = SessionTokenStore(db)
        workspace = tmp_path / "ws0"
        workspace.mkdir(parents=True)

        session_id = await orch._launch_pool_session(
            await db.get_project(PROJECT_ID), await db.get_profile("worker")
        )

        assert session_id is not None
        orch._ensure_control_files_excluded.assert_awaited_once_with(str(workspace))
        row = await db.get_session(session_id)
        spec = orch.session_providers.create("fake").starts[0]
        scope = await orch.token_store.validate(spec.env["AQ_API_TOKEN"])
        assert scope is not None
        assert scope.session_instance_token == row.instance_token

    async def test_pool_handoff_refuses_unverifiable_daemon_excludes(
        self, orch, db, tmp_path
    ):
        """A pool session must not receive a checkout without the managed block."""
        orch.config.worktrees.enabled = False
        workspace = tmp_path / "ws0"
        (workspace / ".git").mkdir(parents=True)
        orch._ensure_control_files_excluded = AsyncMock(
            side_effect=OSError("exclude is read-only")
        )

        session_id = await orch._launch_pool_session(
            await db.get_project(PROJECT_ID), await db.get_profile("worker")
        )

        assert session_id is None
        assert await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID) == []
        assert all(ws.locked_by_agent_id is None for ws in await db.list_workspaces(PROJECT_ID))

    async def test_starts_sessions_for_ready_work(self, orch, db):
        for t in ("t1", "t2", "t3"):
            await ready(db, t)
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        pool = await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID)
        assert len(pool) == 2  # max_active
        assert all(s.agent_id for s in pool)
        assert all(str(uuid.UUID(s.id)) == s.id and s.name.startswith("p-worker--proj--") for s in pool)
        assert all(s.state == "running" for s in pool)
        provider = orch.session_providers.create("fake", orch.config)
        assert {spec.session_name for spec in provider.starts} == {s.name for s in pool}
        sessions_by_name = {session.name: session for session in pool}
        for spec in provider.starts:
            argv = list(spec.command)
            assert argv[argv.index("--session-id") + 1] == sessions_by_name[spec.session_name].id
        agents = await db.list_agents()
        assert sorted(a.state.value for a in agents) == [
            AgentState.IDLE.value,
            AgentState.IDLE.value,
        ]
        for s in pool:
            assert (await db.get_workspace_for_agent(s.agent_id)) is not None
        kinds = [c.args[0] for c in orch.bus.emit.await_args_list]
        assert kinds.count("pool.scaled") == 1
        # The bus is in-process and its WebSocket forward is live-only, so
        # the audit row is the only trace an operator surface
        # (`aq system get-recent-events --event-type pool.scaled`) can read
        # back after the fact.
        rows = await db.get_recent_events(event_type="pool.scaled")
        assert len(rows) == 1
        assert rows[0]["project_id"] == PROJECT_ID
        assert rows[0]["payload"] == "start 2 worker"

    async def test_no_audit_row_when_nothing_scales(self, orch, db):
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert await db.get_recent_events(event_type="pool.scaled") == []

    async def test_no_starts_when_disabled(self, orch, db):
        orch.config.swarm.enabled = False
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert await db.list_sessions(lifecycle="pool") == []

    async def test_starved_pool_starts_nothing_when_no_workspace(self, orch, db):
        for ws in await db.list_workspaces(PROJECT_ID):
            await db.delete_workspace(ws.id)
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert await db.list_sessions(lifecycle="pool") == []
        assert await db.list_agents() == []
        # A starved pool is expected, not exceptional -- it must not
        # quarantine the key (unlike a genuine launch failure, R3).
        assert orch._pool_quarantine == {}

    async def test_quarantined_key_starts_nothing(self, orch, db):
        orch._pool_quarantine[(PROJECT_ID, "worker")] = time.time() + 60
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert await db.list_sessions(lifecycle="pool") == []

    async def test_drain_marks_idle_sessions_after_grace(self, orch, db):
        # Sessions are created running (R1) -- nothing promotes
        # starting -> running for a pool row, so no hand-edit is needed
        # (or possible) to make the session count as idle supply.
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session_id = (await db.list_sessions(lifecycle="pool"))[0].id
        await db.delete_task("t1")
        orch.config.swarm.scale_down_grace = 0
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert (await db.get_session(session_id)).desired_state == "stopped"

    async def test_disabled_profile_starts_nothing_and_drains_idle_workers(self, orch, db):
        """The operator switch sizes the pool to zero without deleting it."""
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session_id = (await db.list_sessions(lifecycle="pool"))[0].id

        await db.update_profile("worker", enabled=False)
        await ready(db, "t2")
        orch.config.swarm.scale_down_grace = 0
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()

        # No new worker for the newly ready task, and the idle one is drained.
        assert len(await db.list_sessions(lifecycle="pool")) == 1
        assert (await db.get_session(session_id)).desired_state == "stopped"

    async def test_disabled_profile_leaves_a_worker_on_a_task_alone(self, orch, db):
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session = (await db.list_sessions(lifecycle="pool"))[0]
        await db.update_session(session.id, task_id="t1")

        await db.update_profile("worker", enabled=False)
        orch.config.swarm.scale_down_grace = 0
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()

        # Busy supply floors ``desired``: the task it holds runs to completion.
        assert (await db.get_session(session.id)).desired_state == "running"

    async def test_codex_pool_launch_keeps_uuid_id_without_session_id_flag(self, orch, db):
        await db.update_profile("worker", harness="codex")
        orch.harness_registry.upsert(Harness(id="codex", name="codex", command="codex"))

        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()

        session = (await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID))[0]
        assert str(uuid.UUID(session.id)) == session.id
        assert session.name.startswith("p-worker--proj--")
        provider = orch.session_providers.create("fake", orch.config)
        assert provider.starts[0].session_name == session.name
        assert "--session-id" not in provider.starts[0].command

    async def test_push_scheduler_ignores_pool_profile_tasks(self, orch, db):
        await ready(db, "t1")
        assert await orch._pool_profile_ids(PROJECT_ID) == {"worker"}
        task = await db.get_task("t1")
        profile = await orch._resolve_profile(task)
        assert orch._is_session_routed(profile) is False

    async def test_launch_failure_releases_resources_and_preserves_definition(self, orch, db, monkeypatch):
        async def _boom(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(db, "acquire_one_unlocked", _boom)
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()

        workers = await db.list_agents()
        assert len(workers) == 1 and workers[0].state == AgentState.IDLE
        assert workers[0].profile_id == "worker"
        assert await db.list_sessions(lifecycle="pool") == []
        for ws in await db.list_workspaces(PROJECT_ID):
            assert ws.locked_by_agent_id is None
        until = orch._pool_quarantine.get((PROJECT_ID, "worker"))
        assert until is not None and until > time.time()

    async def test_failed_acquisition_yields_next_tick_to_other_project(self, orch, db, tmp_path):
        orch.config.swarm.max_starts_per_tick = 1
        await db.create_project(Project(id="other", name="other"))
        await db.create_workspace(Workspace(
            id="other-ws", project_id="other", workspace_path=str(tmp_path / "other"),
            source_type=RepoSourceType.LINK, kind_id="project-repo",
        ))
        for i in range(3):
            await ready(db, f"backlog-{i}")
        await db.create_task(Task(
            id="other-task", project_id="other", title="other", description="",
            status=TaskStatus.READY, profile_id="worker",
        ))
        acquire = db.acquire_one_unlocked

        async def fail_busy_project(**kwargs):
            if kwargs["project_id"] == PROJECT_ID:
                return None
            return await acquire(**kwargs)

        db.acquire_one_unlocked = fail_busy_project
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert await db.list_sessions(lifecycle="pool") == []
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        sessions = await db.list_sessions(lifecycle="pool")
        assert len(sessions) == 1
        assert sessions[0].project_id == "other"

    async def test_worktree_mode_grows_a_slot_and_starts(self, orch, db, tmp_path):
        orch.config.worktrees.enabled = True
        orch._worktree_slot_manager = _FakeSlotManager(db)

        # Swap the two exclusive-clone workspaces for a worktree-mode base
        # with no pre-existing slots.
        for ws in await db.list_workspaces(PROJECT_ID):
            await db.delete_workspace(ws.id)
        system_kind = await db.resolve_workspace_kind(PROJECT_ID, "project-repo")
        await db.upsert_workspace_kind(
            dataclasses.replace(system_kind, project_id=PROJECT_ID, mode="worktree")
        )
        base = Workspace(
            id="base0",
            project_id=PROJECT_ID,
            workspace_path=str(tmp_path / "base0"),
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
        )
        await db.create_workspace(base)

        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()

        pool = await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID)
        assert len(pool) == 1
        slots = await db.list_slots_for_base("base0")
        assert len(slots) == 1 and slots[0].locked_by_agent_id == pool[0].agent_id

    async def test_terminate_pool_session_full_teardown(self, orch, db):
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session = (await db.list_sessions(lifecycle="pool"))[0]
        claim_path = os.path.join(session.work_dir, CLAIM_FILE)
        write_claim_file(session.work_dir, {"task_id": "t1"})
        assert os.path.exists(claim_path)

        await orch._terminate_pool_session(session, reason="test_teardown")

        agent = await db.get_agent(session.agent_id)
        assert agent.state == AgentState.IDLE
        assert await db.get_workspace_for_agent(session.agent_id) is None
        updated = await db.get_session(session.id)
        assert updated.state == "stopped"
        assert not os.path.exists(claim_path)

    async def test_terminate_pool_session_retains_attached_integration_owner(self, orch, db):
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session = (await db.list_sessions(lifecycle="pool"))[0]
        claim_path = os.path.join(session.work_dir, CLAIM_FILE)
        await db.update_session(session.id, task_id="t1", claim_phase="active")
        await db.update_task("t1", status=TaskStatus.IN_PROGRESS, assigned_agent_id=session.agent_id)
        await db.update_agent(session.agent_id, current_task_id="t1")
        workspace = await db.get_workspace_for_agent(session.agent_id)
        await db.update_workspace(workspace.id, locked_by_task_id="t1")
        write_claim_file(session.work_dir, {"task_id": "t1", "claim_epoch": 0})
        async with db.immediate() as conn:
            await conn.execute(
                insert(integration_branch_owners).values(
                    id="owner-1", repository_id="repo-1", ref="aq/t1", owner_id="t1",
                    owner_role="worker", fence_token=1, handoff_state="attached",
                    session_id=session.id, workspace_id=workspace.id,
                    created_at=time.time(), updated_at=time.time(),
                )
            )

        await orch._terminate_pool_session(session, reason="integration_owner")

        updated = await db.get_session(session.id)
        agent = await db.get_agent(session.agent_id)
        assert updated.state != "stopped"
        assert updated.task_id == "t1"
        assert agent.state is not AgentState.IDLE
        assert (await db.get_workspace(workspace.id)).locked_by_agent_id == session.agent_id
        assert os.path.exists(claim_path)

    async def test_startup_warns_when_pool_profiles_but_swarm_disabled(self, orch, caplog):
        """I5 / ruling P2-17: say out loud that the flag strands pool work."""
        orch.config.swarm.enabled = False
        with caplog.at_level(logging.WARNING, logger="src.orchestrator.core"):
            await orch._warn_if_pools_disabled()
        assert any("swarm.enabled is false" in r.message for r in caplog.records)

    async def test_startup_silent_when_swarm_enabled(self, orch, caplog):
        orch.config.swarm.enabled = True
        with caplog.at_level(logging.WARNING, logger="src.orchestrator.core"):
            await orch._warn_if_pools_disabled()
        assert not any("swarm.enabled is false" in r.message for r in caplog.records)

    async def test_schedule_skips_snapshot_emit_with_no_subscribers(self, orch, db):
        """I7: the only listener is a ``task_claim`` long-poll (none here)."""
        await ready(db, "t1")
        assert orch.bus.subscriber_count("snapshot.refreshed") == 0
        await orch._schedule()
        emitted = [c.args[0] for c in orch.bus.emit.await_args_list if c.args]
        assert "snapshot.refreshed" not in emitted

    async def test_schedule_emits_snapshot_when_someone_listens(self, orch, db):
        await ready(db, "t1")

        async def _noop(_event):
            return None

        orch.bus.subscribe("snapshot.refreshed", _noop)
        await orch._schedule()
        emitted = [c.args[0] for c in orch.bus.emit.await_args_list if c.args]
        assert "snapshot.refreshed" in emitted

    async def test_schedule_excludes_pool_profile_task_and_pool_agent(self, orch, db):
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        pool_sessions = await db.list_sessions(lifecycle="pool")
        assert len(pool_sessions) == 1
        pool_agent_id = pool_sessions[0].agent_id

        await db.create_profile(AgentProfile(id="reviewer", name="r", harness="claude"))
        await ready(
            db, "t2", profile_id="reviewer", intelligence_class="standard-medium"
        )

        actions = await orch._schedule()

        assigned_agent_ids = {a.agent_id for a in actions}
        assigned_task_ids = {a.task_id for a in actions}
        assert pool_agent_id not in assigned_agent_ids
        assert "t1" not in assigned_task_ids
        assert "t2" in assigned_task_ids


async def test_durable_pool_reuses_definition_after_teardown(orch, db):
    from src.models import Agent
    await db.create_agent(Agent(id="configured-worker", name="Keeper", profile_id="worker", model="fixed-model"))
    await ready(db, "task-a")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    first = (await db.list_sessions(lifecycle="pool"))[0]
    assert first.agent_id == "configured-worker"
    assert first.model == "fixed-model" and first.llm_provider == "anthropic"
    await orch._terminate_pool_session(first, reason="rotation")
    assert (await db.get_agent("configured-worker")).state == AgentState.IDLE
    assert (await db.get_agent("configured-worker")).model == "fixed-model"
    await db.create_project(Project(id="second", name="Second"))
    await db.create_workspace(Workspace(id="second-ws", project_id="second", workspace_path="/tmp/second-ws", source_type=RepoSourceType.LINK, kind_id="project-repo"))
    new_id = await orch._launch_pool_session(await db.get_project("second"), await db.get_profile("worker"))
    second = await db.get_session(new_id)
    assert second.agent_id == "configured-worker" and second.project_id == "second"
    assert first.id != second.id
    assert len(await db.list_agents()) == 1


async def test_pool_stop_failure_keeps_worker_and_workspace_unavailable(orch, db, monkeypatch):
    await ready(db, "task-a")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    record = (await db.list_sessions(lifecycle="pool"))[0]
    provider = orch.session_providers.create(record.provider, orch.config)
    monkeypatch.setattr(provider, "stop", AsyncMock(side_effect=RuntimeError("cannot confirm exit")))
    await orch._terminate_pool_session(record, reason="test")
    assert (await db.get_session(record.id)).state != "stopped"
    assert (await db.get_agent(record.agent_id)).state != AgentState.IDLE
    assert (await db.get_workspace_for_agent(record.agent_id)) is not None


async def test_stopped_pool_worker_can_take_push_task_without_reprofile(orch, db):
    await ready(db, "pooled")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    row = (await db.list_sessions(lifecycle="pool"))[0]
    await orch._terminate_pool_session(row, reason="rotate")
    await db.create_profile(AgentProfile(id="reviewer", name="Review", harness="claude"))
    await ready(
        db, "push", profile_id="reviewer", intelligence_class="standard-medium"
    )
    actions = await orch._schedule()
    assert [(a.task_id, a.agent_id) for a in actions] == [("push", row.agent_id)]
    assert (await db.get_agent(row.agent_id)).profile_id == "worker"


async def test_repeated_pool_teardown_does_not_steal_new_launch_reservation(orch, db):
    await ready(db, "pooled")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    row = (await db.list_sessions(lifecycle="pool"))[0]
    await orch._terminate_pool_session(row, reason="rotate")
    assert await db.reserve_idle_agent(row.agent_id)
    await orch._terminate_pool_session(row, reason="old-history")
    assert (await db.get_agent(row.agent_id)).state == AgentState.BUSY


async def test_first_pool_claim_survives_launch_completion(orch, db, monkeypatch):
    await ready(db, "pooled")
    original = db.create_session
    visible_states = []

    async def claim_immediately_after_insert(row, **kwargs):
        await original(row, **kwargs)
        visible_states.append((await db.get_agent(row.agent_id)).state)
        async with db.immediate() as conn:
            await db.record_holder(
                conn,
                session_id=row.id,
                task_id="pooled",
                claim_epoch=0,
                agent_id=row.agent_id,
                work_dir=row.work_dir,
                now=time.time(),
            )

    monkeypatch.setattr(db, "create_session", claim_immediately_after_insert)
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    row = (await db.list_sessions(lifecycle="pool"))[0]
    agent = await db.get_agent(row.agent_id)
    assert agent.state == AgentState.BUSY and agent.current_task_id == "pooled"
    assert visible_states == [AgentState.IDLE]


async def test_concurrent_pool_teardown_stops_and_releases_only_once(orch, db, monkeypatch):
    import asyncio
    await ready(db, "pooled")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    row = (await db.list_sessions(lifecycle="pool"))[0]
    provider = orch.session_providers.create(row.provider, orch.config)
    original_stop = provider.stop

    async def slow_stop(*args, **kwargs):
        await asyncio.sleep(0.1)
        await original_stop(*args, **kwargs)

    stop = AsyncMock(side_effect=slow_stop)
    monkeypatch.setattr(provider, "stop", stop)
    await asyncio.gather(
        orch._terminate_pool_session(row, reason="reconciler"),
        orch._terminate_pool_session(row, reason="operator"),
    )
    assert stop.await_count == 1
    assert (await db.get_agent(row.agent_id)).state == AgentState.IDLE


# ---------------------------------------------------------------------------
# Global keying: one pool per profile, placement decides the project.
# ---------------------------------------------------------------------------


async def second_project(db, *, path="/tmp/second-ws"):
    """A second ACTIVE project with one free ``project-repo`` workspace."""
    await db.create_project(Project(id="second", name="Second"))
    await db.create_workspace(
        Workspace(
            id="second-ws",
            project_id="second",
            workspace_path=path,
            source_type=RepoSourceType.LINK,
            kind_id="project-repo",
        )
    )


async def test_measure_pools_aggregates_projects_under_one_profile_key(orch, db, tmp_path):
    """Two projects, one profile, one pool — with the per-project breakdown and
    a placement candidate each kept alongside the aggregate."""
    from src.scheduler import PoolKey

    await second_project(db, path=str(tmp_path / "second-ws"))
    await ready(db, "t1")
    await ready(db, "t2")
    await db.update_task("t2", project_id="second")

    measurement = await orch._measure_pools()

    key = PoolKey("worker")
    assert set(measurement.supply) == {key}
    assert set(measurement.bounds) == {key}
    assert measurement.bounds[key] == (0, 2)  # the profile's own bounds, once
    assert measurement.demand[key] == 2  # summed across both projects
    assert set(measurement.supply[key].by_project) == {PROJECT_ID, "second"}
    assert {c.project_id for c in measurement.candidates[key]} == {PROJECT_ID, "second"}
    assert set(measurement.projects) == {PROJECT_ID, "second"}


async def test_measure_pools_records_placement_inputs_per_project(orch, db, tmp_path):
    from src.scheduler import PoolKey

    await second_project(db, path=str(tmp_path / "second-ws"))
    orch._pool_quarantine[("second", "worker")] = time.time() + 60
    await ready(db, "t1")

    measurement = await orch._measure_pools()
    by_project = {c.project_id: c for c in measurement.candidates[PoolKey("worker")]}

    assert by_project["second"].quarantined is True
    assert by_project[PROJECT_ID].quarantined is False
    assert by_project[PROJECT_ID].workspace_capacity == 2  # the fixture's two links
    assert by_project[PROJECT_ID].ready == 1
    assert by_project["second"].ready == 0
    # ``max_concurrent_agents`` is a placement input now, not a pool bound.
    project = await db.get_project(PROJECT_ID)
    assert by_project[PROJECT_ID].project_cap == project.max_concurrent_agents


async def test_quarantined_project_does_not_burn_the_fleets_start_budget(orch, db, tmp_path):
    """Quarantine is an eligibility predicate, checked before a start is spent.

    It used to be checked *after* the sizer had already picked a key, so a
    single broken project could consume the tick's whole start budget and
    leave a healthy one with nothing.
    """
    await second_project(db, path=str(tmp_path / "second-ws"))
    orch._pool_quarantine[(PROJECT_ID, "worker")] = time.time() + 60
    await ready(db, "t1")
    await ready(db, "t2")
    await db.update_task("t2", project_id="second")

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    sessions = await db.list_sessions(lifecycle="pool")
    assert [s.project_id for s in sessions] == ["second"]


async def test_every_project_quarantined_starves_rather_than_starting(orch, db, tmp_path):
    await second_project(db, path=str(tmp_path / "second-ws"))
    now = time.time()
    orch._pool_quarantine[(PROJECT_ID, "worker")] = now + 60
    orch._pool_quarantine[("second", "worker")] = now + 60
    await ready(db, "t1")

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    assert await db.list_sessions(lifecycle="pool") == []
    assert await db.get_recent_events(event_type="pool.scaled") == []


async def test_pool_scaled_payload_carries_the_placement_reason(orch, db):
    await ready(db, "t1")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    scaled = [
        call.args[1] for call in orch.bus.emit.await_args_list if call.args[0] == "pool.scaled"
    ]
    assert len(scaled) == 1
    assert scaled[0]["project_id"] == PROJECT_ID
    assert scaled[0]["profile_id"] == "worker"
    assert scaled[0]["kind"] == "start"
    assert scaled[0]["placement_reason"] == "deficit"


async def test_drain_event_reports_the_project_the_worker_actually_left(orch, db):
    await ready(db, "t1")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    await db.delete_task("t1")
    orch.config.swarm.scale_down_grace = 0
    orch.bus.emit.reset_mock()
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    drains = [
        call.args[1]
        for call in orch.bus.emit.await_args_list
        if call.args[0] == "pool.scaled" and call.args[1]["kind"] == "drain"
    ]
    assert [(d["project_id"], d["count"]) for d in drains] == [(PROJECT_ID, 1)]
    # A drain has no placement reason: nothing was placed.
    assert "placement_reason" not in drains[0]


async def test_global_cap_bounds_the_whole_fleet(orch, db, tmp_path):
    """``global_cap`` was hardcoded ``None`` at the call site, so the fleet had no
    box-wide bound at all.  It now resolves to ``resources.max_concurrent_agents``
    unless ``swarm.global_max_active`` overrides it."""
    await second_project(db, path=str(tmp_path / "second-ws"))
    orch.config.resources.max_concurrent_agents = 1
    await ready(db, "t1")
    await ready(db, "t2")

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    assert len(await db.list_sessions(lifecycle="pool")) == 1


async def test_swarm_global_max_active_overrides_the_resource_cap(orch, db):
    orch.config.resources.max_concurrent_agents = 8
    orch.config.swarm.global_max_active = 1
    await ready(db, "t1")
    await ready(db, "t2")

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    assert len(await db.list_sessions(lifecycle="pool")) == 1


async def test_starvation_is_reported_once_per_condition_not_once_per_tick(orch, db, caplog):
    """The pool step runs every five seconds, and a fleet with nowhere to put a
    worker stays that way until an operator acts.  One warning per condition,
    not one per tick — the same restraint the quarantine window buys."""
    for ws in await db.list_workspaces(PROJECT_ID):
        await db.delete_workspace(ws.id)
    await ready(db, "t1")

    with caplog.at_level(logging.WARNING, logger="src.orchestrator.pools"):
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        first = [r.getMessage() for r in caplog.records if r.name == "src.orchestrator.pools"]
        caplog.clear()
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        second = [r.getMessage() for r in caplog.records if r.name == "src.orchestrator.pools"]

    assert len(first) == 1
    assert "no eligible project" in first[0] and "no workspace capacity" in first[0]
    assert second == []


async def test_all_quarantined_starvation_does_not_re_warn_over_the_quarantine(orch, db, caplog):
    """``_quarantine_pool`` already said this, once, with the startup output
    attached; a second warning per tick is the wall of noise it exists to stop."""
    orch._pool_quarantine[(PROJECT_ID, "worker")] = time.time() + 60
    await ready(db, "t1")

    with caplog.at_level(logging.WARNING, logger="src.orchestrator.pools"):
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()

    assert [r for r in caplog.records if r.name == "src.orchestrator.pools"] == []
    assert await db.list_sessions(lifecycle="pool") == []


async def test_min_per_project_parks_a_warm_worker_in_every_eligible_project(
    orch, db, tmp_path
):
    """The knob that buys back what global sizing costs: a stated per-project
    reservation raises the fleet floor *and* is placed where it was promised,
    with no ready work anywhere."""
    await second_project(db, path=str(tmp_path / "second-ws"))
    await db.update_profile("worker", min_active=0, max_active=4, min_per_project=1)

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    sessions = await db.list_sessions(lifecycle="pool")
    assert sorted(s.project_id for s in sessions) == ["proj", "second"]
    reasons = {
        call.args[1]["project_id"]: call.args[1].get("placement_reason")
        for call in orch.bus.emit.await_args_list
        if call.args[0] == "pool.scaled"
    }
    assert reasons == {"proj": "warm_floor", "second": "warm_floor"}


async def test_a_quarantined_project_holds_no_warm_reservation_open(orch, db, tmp_path):
    """``min_per_project`` raises the effective floor only for projects that could
    actually host the worker — otherwise a broken project would park a slot of
    the fleet's floor against a project that cannot use it."""
    from src.scheduler import PoolKey

    await second_project(db, path=str(tmp_path / "second-ws"))
    await db.update_profile("worker", min_active=0, max_active=4, min_per_project=1)
    orch._pool_quarantine[("second", "worker")] = time.time() + 60

    measurement = await orch._measure_pools()

    assert measurement.bounds[PoolKey("worker")] == (1, 4)


async def test_reconcile_relocates_idle_capacity_without_raising_global_cap(orch, db, tmp_path):
    """A full pool can serve a new project's queue after its old queue empties."""
    await second_project(db, path=str(tmp_path / "second-ws"))
    await db.update_profile("worker", max_active=1)
    orch.config.swarm.global_max_active = 1
    orch.config.swarm.scale_down_grace = 0
    await ready(db, "old-demand")
    await db.update_task("old-demand", project_id="second")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    sessions = await db.list_sessions(lifecycle="pool")
    assert len(sessions) == 1
    old = sessions[0]
    assert old.project_id == "second"
    await db.update_session(old.id, state="running")
    await db.delete_task("old-demand")
    await ready(db, "new-demand")

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()

    assert (await db.get_session(old.id)).desired_state == "stopped"
    assert len(await db.list_sessions(lifecycle="pool")) == 1
    # A later sizing pass, after retirement, may launch in the demanding project.
    await db.update_session(old.id, state="stopped")
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    successors = [s for s in await db.list_sessions(lifecycle="pool") if s.id != old.id]
    assert len(successors) == 1
    assert successors[0].project_id == PROJECT_ID
    assert (await db.get_task("new-demand")).status is TaskStatus.READY


async def test_slow_launch_does_not_block_other_project_or_oversubscribe(orch, db, tmp_path, monkeypatch):
    import asyncio
    from src.scheduler import PoolKey

    await ready(db, "slow-task")
    await db.create_project(Project(id="second", name="Second"))
    await db.create_workspace(Workspace(id="second-ws", project_id="second",
        workspace_path=str(tmp_path / "second"), source_type=RepoSourceType.LINK,
        kind_id="project-repo"))
    await db.create_task(Task(id="fast-task", project_id="second", title="Fast",
        description="", status=TaskStatus.READY, profile_id="worker"))
    provider = orch.session_providers.create("fake", orch.config)
    original = provider.start
    entered, release, fast_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def start(spec):
        if "--proj--" in spec.session_name:
            entered.set()
            await release.wait()
        result = await original(spec)
        if "--second--" in spec.session_name:
            fast_started.set()
        return result

    monkeypatch.setattr(provider, "start", start)
    await asyncio.wait_for(orch._reconcile_pools(), timeout=5)
    await asyncio.wait_for(entered.wait(), timeout=5)
    await asyncio.wait_for(fast_started.wait(), timeout=5)
    # The slow launch holds no session row yet, but its capacity is reserved.
    measurement = await orch._measure_pools()
    supply = measurement.supply[PoolKey("worker")]
    assert supply.starting + supply.running_idle + supply.running_busy == 2
    for _ in range(3):
        await orch._reconcile_pools()
    assert len(provider.starts) == 1
    assert len(orch._pool_launches) == 1
    release.set()
    await orch.wait_for_pool_launches()
    rows = await db.list_sessions(lifecycle="pool")
    assert {row.project_id for row in rows} == {PROJECT_ID, "second"}
    assert len({row.agent_id for row in rows}) == 2
    assert not orch._pool_launches


async def test_cancelled_background_start_stops_process_and_releases_resources(orch, db, monkeypatch):
    import asyncio

    await ready(db, "task")
    provider = orch.session_providers.create("fake", orch.config)
    original = provider.start
    entered = asyncio.Event()

    async def start(spec):
        result = await original(spec)
        entered.set()
        await asyncio.Event().wait()
        return result

    monkeypatch.setattr(provider, "start", start)
    await orch._reconcile_pools()
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert any(ws.locked_by_agent_id for ws in await db.list_workspaces(PROJECT_ID))
    await orch.wait_for_pool_launches(cancel=True)
    assert not orch._pool_launches
    assert await db.list_sessions(lifecycle="pool") == []
    assert all(ws.locked_by_agent_id is None for ws in await db.list_workspaces(PROJECT_ID))
    assert all(agent.state == AgentState.IDLE for agent in await db.list_agents())
    assert provider.sessions == {}


async def test_cancelled_reservation_waits_for_commit_before_releasing(orch, db, monkeypatch):
    import asyncio
    from src.models import Agent

    await db.create_agent(Agent(id="existing", name="Existing", profile_id="worker"))
    original = db.reserve_idle_agent
    entered, release = asyncio.Event(), asyncio.Event()

    async def reserve(agent_id):
        result = await original(agent_id)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(db, "reserve_idle_agent", reserve)
    task = asyncio.create_task(orch._launch_pool_session(
        await db.get_project(PROJECT_ID), await db.get_profile("worker")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await db.get_agent("existing")).state == AgentState.IDLE
    assert await db.list_sessions(lifecycle="pool") == []


async def test_live_slow_launch_identity_survives_reconciler_reservation_timeout(orch, db, monkeypatch):
    import asyncio

    await ready(db, "task")
    entered, release = asyncio.Event(), asyncio.Event()
    provider = orch.session_providers.create("fake", orch.config)
    original = provider.start

    async def start(spec):
        entered.set()
        await release.wait()
        return await original(spec)

    monkeypatch.setattr(provider, "start", start)
    await orch._reconcile_pools()
    await asyncio.wait_for(entered.wait(), timeout=5)
    agent_id = next(iter(orch._launching_pool_agent_ids()))
    await db.update_agent(agent_id, last_heartbeat=time.time() - 300)
    await orch._schedule()
    assert (await db.get_agent(agent_id)).state == AgentState.BUSY
    release.set()
    await orch.wait_for_pool_launches()


@pytest.mark.parametrize("constraint", ["global", "project", "workspace"])
async def test_pending_launch_reserves_shared_capacity_across_profiles(orch, db, monkeypatch, constraint):
    import asyncio

    await db.create_profile(AgentProfile(id="other", name="Other", lifecycle="pool",
        min_active=0, max_active=2, harness="claude"))
    await ready(db, "task-a")
    await ready(db, "task-b", profile_id="other")
    if constraint == "global":
        orch.config.resources.max_concurrent_agents = 1
    elif constraint == "project":
        await db.update_project(PROJECT_ID, max_concurrent_agents=1)
    else:
        await db.delete_workspace("ws1")
    provider = orch.session_providers.create("fake", orch.config)
    original = provider.start
    entered, release = asyncio.Event(), asyncio.Event()
    attempted = []

    async def start(spec):
        attempted.append(spec)
        entered.set()
        await release.wait()
        return await original(spec)

    monkeypatch.setattr(provider, "start", start)
    await orch._reconcile_pools()
    await asyncio.wait_for(entered.wait(), timeout=5)
    for _ in range(3):
        await orch._reconcile_pools()
    assert len(attempted) == 1
    assert len(orch._pool_launches) == 1
    release.set()
    await orch.wait_for_pool_launches()
    assert len(await db.list_sessions(lifecycle="pool")) == 1


async def test_slow_provider_does_not_hide_another_free_workspace(orch, db, monkeypatch):
    import asyncio

    await ready(db, "first")
    provider = orch.session_providers.create("fake", orch.config)
    original = provider.start
    first, second, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempts = []

    async def start(spec):
        attempts.append(spec)
        (first if len(attempts) == 1 else second).set()
        await release.wait()
        return await original(spec)

    monkeypatch.setattr(provider, "start", start)
    await orch._reconcile_pools()
    await asyncio.wait_for(first.wait(), timeout=5)
    await ready(db, "second")
    await orch._reconcile_pools()
    await asyncio.wait_for(second.wait(), timeout=5)
    assert len(orch._pool_launches) == 2
    release.set()
    await orch.wait_for_pool_launches()
    assert len(await db.list_sessions(lifecycle="pool")) == 2


async def test_cancelled_start_keeps_resources_when_process_stop_is_unconfirmed(orch, db, monkeypatch):
    import asyncio

    await ready(db, "task")
    provider = orch.session_providers.create("fake", orch.config)
    original = provider.start
    entered = asyncio.Event()

    async def start(spec):
        await original(spec)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, "start", start)
    monkeypatch.setattr(provider, "stop", AsyncMock(side_effect=RuntimeError("unknown process state")))
    await orch._reconcile_pools()
    await asyncio.wait_for(entered.wait(), timeout=5)
    await orch.wait_for_pool_launches(cancel=True)
    assert provider.sessions
    assert any(ws.locked_by_agent_id for ws in await db.list_workspaces(PROJECT_ID))
    assert all(agent.state == AgentState.ERROR for agent in await db.list_agents())
    assert not orch._pool_launches


async def test_cancellation_after_session_insert_preserves_durable_owner(orch, db, monkeypatch):
    import asyncio

    await ready(db, "task")
    entered = asyncio.Event()

    async def emit(event, *args, **kwargs):
        if event == "pool.session_started":
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(orch.bus, "emit", emit)
    await orch._reconcile_pools()
    await asyncio.wait_for(entered.wait(), timeout=5)
    before = (await db.list_sessions(lifecycle="pool"))[0]
    await orch.wait_for_pool_launches(cancel=True)
    after = await db.get_session(before.id)
    assert after.state == "running"
    assert (await db.get_workspace_for_agent(after.agent_id)) is not None
    provider = orch.session_providers.create("fake", orch.config)
    assert len(provider.sessions) == 1
    assert not orch._pool_launches
