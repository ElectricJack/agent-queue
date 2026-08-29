"""_reconcile_pools / _launch_pool_session — spec §11 (fake provider)."""

from __future__ import annotations

import dataclasses
import logging
import os
import time

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.claim_commands import CLAIM_FILE, write_claim_file
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
    # ``AgentReconciler`` was built in ``__init__`` against the (real,
    # uninitialized) db the constructor saw -- point it at the test db too
    # so ``_schedule()`` (which reconciles agents first) works end to end.
    o._agent_reconciler._db = db
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


async def ready(db, tid, *, profile_id="worker"):
    await db.create_task(
        Task(
            id=tid,
            project_id=PROJECT_ID,
            title=tid,
            description=tid,
            status=TaskStatus.READY,
            profile_id=profile_id,
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
        assert all(s.state == "running" for s in pool)
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
        # A starved pool is expected, not exceptional -- it must not
        # quarantine the key (unlike a genuine launch failure, R3).
        assert orch._pool_quarantine == {}

    async def test_quarantined_key_starts_nothing(self, orch, db):
        orch._pool_quarantine[(PROJECT_ID, "worker")] = time.time() + 60
        await ready(db, "t1")
        await orch._reconcile_pools()
        assert await db.list_sessions(lifecycle="pool") == []

    async def test_drain_marks_idle_sessions_after_grace(self, orch, db):
        # Sessions are created running (R1) -- nothing promotes
        # starting -> running for a pool row, so no hand-edit is needed
        # (or possible) to make the session count as idle supply.
        await ready(db, "t1")
        await orch._reconcile_pools()
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

    async def test_launch_failure_rolls_back_fully_and_quarantines(self, orch, db, monkeypatch):
        async def _boom(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(db, "acquire_one_unlocked", _boom)
        await ready(db, "t1")
        await orch._reconcile_pools()

        assert await db.list_agents() == []
        assert await db.list_sessions(lifecycle="pool") == []
        for ws in await db.list_workspaces(PROJECT_ID):
            assert ws.locked_by_agent_id is None
        until = orch._pool_quarantine.get((PROJECT_ID, "worker"))
        assert until is not None and until > time.time()

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

        pool = await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID)
        assert len(pool) == 1
        slots = await db.list_slots_for_base("base0")
        assert len(slots) == 1 and slots[0].locked_by_agent_id == pool[0].agent_id

    async def test_terminate_pool_session_full_teardown(self, orch, db):
        await ready(db, "t1")
        await orch._reconcile_pools()
        session = (await db.list_sessions(lifecycle="pool"))[0]
        claim_path = os.path.join(session.work_dir, CLAIM_FILE)
        write_claim_file(session.work_dir, {"task_id": "t1"})
        assert os.path.exists(claim_path)

        await orch._terminate_pool_session(session, reason="test_teardown")

        agent = await db.get_agent(session.agent_id)
        assert agent.state == AgentState.RETIRED
        assert await db.get_workspace_for_agent(session.agent_id) is None
        updated = await db.get_session(session.id)
        assert updated.state == "stopped"
        assert not os.path.exists(claim_path)

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
        pool_sessions = await db.list_sessions(lifecycle="pool")
        assert len(pool_sessions) == 1
        pool_agent_id = pool_sessions[0].agent_id

        await db.create_profile(AgentProfile(id="reviewer", name="r", harness="claude"))
        await ready(db, "t2", profile_id="reviewer")

        actions = await orch._schedule()

        assigned_agent_ids = {a.agent_id for a in actions}
        assigned_task_ids = {a.task_id for a in actions}
        assert pool_agent_id not in assigned_agent_ids
        assert "t1" not in assigned_task_ids
        assert "t2" in assigned_task_ids
