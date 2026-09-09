"""aq task claim / close --claim-next / epoch fence — spec §10, §14."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import DatabaseConfig, AppConfig, DiscordConfig
from src.database import Database
from src.database.tables import integration_repair_stages, task_branch_origins, task_metadata
from src.intelligence_classes import IntelligenceClass
from src.integration.models import BranchKey
from src.integration.ownership import BranchOwnership
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
from src.sessions.reconciler import SessionReconciler
from tests.db_fixtures import lease_dsn

PROJECT_ID = "proj"
NOW = time.time()


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(
        AgentProfile(id="worker", name="w", lifecycle="pool", needs_workspace=False)
    )
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "ws"),
        database=DatabaseConfig(url=lease_dsn("test.db")),
        data_dir=str(tmp_path / "data"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    cfg.swarm.claim_wait_max = 5
    return cfg


@pytest.fixture
async def handler(db, config):
    orch = Orchestrator(config)
    orch.session_spec_builder._intelligence_classes = {
        "standard-medium": IntelligenceClass(
            "standard-medium",
            "Standard",
            "",
            {"anthropic": {"model": "claude-sonnet-5"}},
        ),
    }
    orch.db = db
    orch.git = MagicMock()
    orch._worktree_slots = MagicMock(
        return_value=MagicMock(reset_slot_for_task=AsyncMock(return_value="aq/t"))
    )
    orch._last_scheduler_state = None  # no snapshot yet → admissible
    # ``orch.git`` is a bare MagicMock (no real checkout exists under
    # ``tmp_path``) — stub the completion pipeline so ``task_close(outcome=
    # "pass")`` doesn't try to ``await`` a non-async git call.  Mirrors the
    # stub in ``tests/test_session_commands.py``.
    orch._run_completion_pipeline = AsyncMock(return_value=(None, True))
    # The ready listener is what lets a blocked ``task_claim`` long-poll
    # wake on ``task.ready`` — normally wired by ``Orchestrator.initialize()``
    # via ``monitoring.register_settlement_listener``, which this fixture
    # skips (no daemon loop in these tests).
    orch.register_settlement_listener()
    return CommandHandler(orch, config)


async def mktask(db, tid, status=TaskStatus.READY, **kw):
    kw.setdefault("intelligence_class", "standard-medium")
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )


async def pool_session(db, tmp_path, sid="s1", agent_id="agent-1"):
    work_dir = tmp_path / agent_id
    work_dir.mkdir()
    await db.create_agent(
        Agent(id=agent_id, name=agent_id, profile_id="worker", state=AgentState.IDLE)
    )
    await db.create_workspace(
        Workspace(
            id=f"ws-{agent_id}",
            project_id=PROJECT_ID,
            workspace_path=str(work_dir),
            kind_id="project-repo",
            source_type=RepoSourceType.LINK,
            locked_by_agent_id=agent_id,
        )
    )
    await db.create_session(
        SessionRecord(
            id=sid,
            project_id=PROJECT_ID,
            profile_id="worker",
            harness="claude",
            provider="fake",
            name=f"p-worker--proj--{sid}",
            lifecycle="pool",
            work_dir=str(work_dir),
            epoch="e",
            instance_token="t",
            started_at=NOW,
            state="running",
            agent_id=agent_id,
            llm_provider="anthropic",
            model="claude-sonnet-5",
            intelligence_class="standard-medium",
        )
    )
    return sid, work_dir


def scoped(handler, sid):
    handler._current_scope = {
        "kind": "session",
        "session_id": sid,
        "task_id": None,
        "project_id": PROJECT_ID,
        "elevated": False,
    }
    return handler


def emitted(handler):
    return [c.args[0] for c in handler.orchestrator.bus.emit.await_args_list]


class TestClaim:
    async def _hierarchy_task(self, db, tmp_path, task_id="child"):
        await db.create_repo(
            RepoConfig(
                id="repo",
                project_id=PROJECT_ID,
                source_type=RepoSourceType.LINK,
                source_path=str(tmp_path),
            )
        )
        await db.update_project(
            PROJECT_ID,
            hierarchical_integration_mode="hierarchy",
            integration_repository_id="repo",
        )
        await mktask(
            db,
            task_id,
            profile_id="worker",
            repo_id="repo",
            branch_name=f"aq/{task_id}",
        )
        async with db.immediate() as conn:
            await conn.execute(
                task_branch_origins.insert().values(
                    id=f"origin-{task_id}",
                    task_id=task_id,
                    repository_id="repo",
                    parent_task_id="parent",
                    parent_repository_id="repo",
                    parent_ref="aq/parent",
                    base_sha="a" * 40,
                    creation_generation=1,
                    reserved=True,
                    materialized=True,
                    created_at=time.time(),
                    materialized_at=time.time(),
                )
            )
        ownership = BranchOwnership(db)
        fence = await ownership.acquire(
            BranchKey(repository_id="repo", branch=f"aq/{task_id}"),
            task_id,
            "worker",
        )
        return ownership, fence

    async def test_hierarchy_pool_claim_resets_from_exact_origin_and_attaches(
        self, handler, db, tmp_path
    ):
        ownership, _fence = await self._hierarchy_task(db, tmp_path)
        sid, _wd = await pool_session(db, tmp_path)

        result = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert result["result"] == "claimed"
        reset = handler.orchestrator._worktree_slots.return_value.reset_slot_for_task
        assert reset.await_args.kwargs["base_branch"] == "a" * 40
        assert reset.await_args.kwargs["target_branch"] == "aq/child"
        owner = await ownership.get_owner(
            BranchKey(repository_id="repo", branch="aq/child")
        )
        assert owner["handoff_state"] == "attached"
        assert owner["session_id"] == sid
        assert owner["workspace_id"] == "ws-agent-1"

    async def test_collector_owned_hierarchy_branch_cannot_reset_or_activate_pool_claim(
        self, handler, db, tmp_path
    ):
        ownership, fence = await self._hierarchy_task(db, tmp_path)
        sid, _wd = await pool_session(db, tmp_path)
        await ownership.transfer(fence, "collector", "collector")

        result = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert result["result"] == "prepare_failed"
        reset = handler.orchestrator._worktree_slots.return_value.reset_slot_for_task
        reset.assert_not_awaited()
        assert (await db.get_session(sid)).claim_phase is None

    async def test_failed_attached_prepare_retries_same_claim_and_fence(self, handler, db, tmp_path):
        ownership, fence = await self._hierarchy_task(db, tmp_path)
        sid, _wd = await pool_session(db, tmp_path)
        reset = handler.orchestrator._worktree_slots.return_value.reset_slot_for_task
        reset.side_effect = [RuntimeError("stale predecessor checkout"), "aq/child"]
        h = scoped(handler, sid)
        failed = await h._cmd_task_claim({"next": True})
        assert failed["result"] == "prepare_failed"
        held = await db.get_session(sid)
        assert held.claim_phase == "preparing"
        epoch = held.last_claim_epoch
        retried = await h._cmd_task_claim({"next": True})
        assert retried["result"] == "claimed"
        assert (await db.get_session(sid)).last_claim_epoch == epoch
        owner = await ownership.get_owner(fence.target)
        assert owner["fence_token"] == fence.token
        assert owner["session_id"] == sid
        assert reset.await_count == 2

    async def test_reconciler_release_honors_fresh_context_drain(self, handler, db, config, tmp_path, monkeypatch):
        """A reconciler-won close release keeps fresh-context drain semantics."""
        await mktask(db, "t1", profile_id="worker")
        sid, work_dir = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claim = await h._cmd_task_claim({"next": True})
        assert (work_dir / ".aq" / "claim.json").exists()

        release_started = asyncio.Event()
        allow_close_release = asyncio.Event()
        real_release_claim = db.release_claim
        release_calls = 0

        async def gate_close_release(*args, **kwargs):
            nonlocal release_calls
            release_calls += 1
            if release_calls == 1:
                release_started.set()
                await allow_close_release.wait()
            return await real_release_claim(*args, **kwargs)

        monkeypatch.setattr(db, "release_claim", gate_close_release)
        close = asyncio.create_task(
            h._cmd_task_close(
                {
                    "task_id": "t1",
                    "outcome": "pass",
                    "summary": "done",
                    "claim_epoch": claim["claim_epoch"],
                }
            )
        )
        try:
            await asyncio.wait_for(release_started.wait(), timeout=5)
            reconciler = SessionReconciler(
                db,
                config,
                SessionProviderRegistry({}),
                bus=handler.orchestrator.bus,
                orchestrator=handler.orchestrator,
                epoch="test",
            )
            await reconciler._step_orphans(await db.list_sessions(live_only=True), time.time())
            session = await db.get_session(sid)
            assert (session.desired_state, session.task_id) == ("stopped", None)
        finally:
            allow_close_release.set()
            await close

        assert not (work_dir / ".aq" / "claim.json").exists()

    async def test_close_release_race_keeps_live_pool_worker_available(
        self, handler, db, config, tmp_path, monkeypatch
    ):
        """A terminal task is a normal intermediate state before pool release.

        Hold the close path immediately before its ``release_claim`` and run
        the orphan sweep in that window.  The sweep must release only the
        task hold; terminating the worker would bypass normal pool
        scale-down grace and an explicit drain acknowledgement.
        """
        handler.config.swarm.fresh_context_per_task = False
        await db.update_profile("worker", max_claims_per_session=None)
        await mktask(db, "t1", profile_id="worker")
        await mktask(db, "t2", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claim = await h._cmd_task_claim({"next": True})

        release_started = asyncio.Event()
        allow_close_release = asyncio.Event()
        real_release_claim = db.release_claim
        release_calls = 0

        async def gate_close_release(*args, **kwargs):
            nonlocal release_calls
            release_calls += 1
            if release_calls == 1:
                release_started.set()
                await allow_close_release.wait()
            return await real_release_claim(*args, **kwargs)

        monkeypatch.setattr(db, "release_claim", gate_close_release)
        close = asyncio.create_task(
            h._cmd_task_close(
                {
                    "task_id": "t1",
                    "outcome": "pass",
                    "summary": "done",
                    "claim_epoch": claim["claim_epoch"],
                }
            )
        )
        try:
            await asyncio.wait_for(release_started.wait(), timeout=5)
            assert (await db.get_task("t1")).status == TaskStatus.COMPLETED

            reconciler = SessionReconciler(
                db,
                config,
                SessionProviderRegistry({}),
                bus=handler.orchestrator.bus,
                orchestrator=handler.orchestrator,
                epoch="test",
            )
            await reconciler._step_orphans(await db.list_sessions(live_only=True), time.time())

            session = await db.get_session(sid)
            assert (session.state, session.desired_state, session.task_id) == (
                "running",
                "running",
                None,
            )
            assert (await db.get_agent("agent-1")).state == AgentState.IDLE

            reclaimed = await h._cmd_task_claim({"task_id": "t2"})
            assert reclaimed["result"] == "claimed"
        finally:
            allow_close_release.set()
            await close

        session = await db.get_session(sid)
        task = await db.get_task("t2")
        assert (session.desired_state, session.task_id) == ("running", "t2")
        assert (task.status, task.assigned_agent_id) == (TaskStatus.IN_PROGRESS, "agent-1")

    @pytest.mark.parametrize("status", [TaskStatus.PAUSED, TaskStatus.BLOCKED, TaskStatus.FAILED])
    async def test_reclaimed_pool_claim_retains_its_slot_for_the_next_claim(
        self, handler, db, tmp_path, status
    ):
        """A live worker can claim again after the reconciler releases its slot."""
        handler.config.swarm.fresh_context_per_task = False
        await db.update_profile("worker", max_claims_per_session=None)
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        await db.create_project(Project(id="other-project", name="other"))
        await db.create_workspace(
            Workspace(
                id="ws-other-project",
                project_id="other-project",
                workspace_path=str(tmp_path / "agent-1"),
                kind_id="project-repo",
                source_type=RepoSourceType.LINK,
            )
        )
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})

        await db.transition_task("t1", status, context="test", force=True)
        reconciler = SessionReconciler(
            db,
            handler.config,
            SessionProviderRegistry({}),
            bus=handler.orchestrator.bus,
            orchestrator=handler.orchestrator,
            epoch="test",
        )
        await reconciler._step_orphans(await db.list_sessions(live_only=True), time.time())
        assert (await db.get_session(sid)).task_id is None
        assert (await db.get_workspace_for_agent("agent-1")).locked_by_task_id is None

        await mktask(db, "t2", profile_id="worker")
        next_claim = await h._cmd_task_claim({"next": True})

        assert next_claim["result"] == "claimed", next_claim
        assert next_claim["task"]["id"] == "t2"
        workspace = await db.get_workspace("ws-agent-1")
        assert (workspace.locked_by_agent_id, workspace.locked_by_task_id) == ("agent-1", "t2")
        assert (await db.get_workspace("ws-other-project")).locked_by_agent_id is None
        assert (await db.get_session(sid)).claim_phase == "active"

    async def test_claim_next_returns_task_epoch_and_writes_file(self, handler, db, tmp_path):
        handler.orchestrator.bus.emit = AsyncMock()
        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert (res["result"], res["task"]["id"], res["claim_epoch"]) == ("claimed", "t1", 1)
        data = json.loads((wd / ".aq" / "claim.json").read_text())
        assert (data["task_id"], data["claim_epoch"], data["session_id"]) == ("t1", 1, sid)
        assert (await db.get_session(sid)).claim_phase == "active"
        assert "task.claimed" in emitted(handler) and "task.started" in emitted(handler)

    @pytest.mark.parametrize("bad_value", ['"0"', '"1788823522.8"', '""', "null", "not-a-number"])
    async def test_malformed_backoff_metadata_does_not_break_the_work_query(
        self, handler, db, tmp_path, bad_value
    ):
        """One unparseable backoff value must not take the whole project down.

        ``task_metadata.value`` is free-form JSON text, so a caller that
        stores the *string* ``"0"`` where the number ``0`` was meant leaves a
        row that ``cast(value, Float)`` cannot read.  The cast sits in a
        correlated EXISTS over every candidate task, so before
        :func:`numeric_meta_value` that single row raised
        ``invalid input syntax for type double precision`` for the entire
        statement -- every claim in the project failed for ~18 hours on
        2026-09-07 while the queue reported no ready work.

        A malformed deadline reads as expired: failing open to claimable is
        right for a value whose only job is to *withhold* a task briefly.
        """
        await mktask(db, "t1", profile_id="worker")
        async with db.immediate() as conn:
            await conn.execute(
                task_metadata.insert().values(
                    task_id="t1", key="claim_prepare_backoff_until", value=bad_value
                )
            )
        sid, _wd = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "claimed"
        assert res["task"]["id"] == "t1"

    async def test_a_well_formed_backoff_deadline_still_withholds_the_task(
        self, handler, db, tmp_path
    ):
        """The guard must not defeat the backoff it is guarding.

        Companion to the test above: reading a malformed value as expired is
        only safe if a *valid* future deadline is still honoured, otherwise
        the fix would have quietly removed the hot-loop protection that
        ``prepare_failed`` relies on.
        """
        await mktask(db, "t1", profile_id="worker")
        await db.set_task_meta("t1", "claim_prepare_backoff_until", time.time() + 300)
        sid, _wd = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "no_ready_work"

    @pytest.mark.parametrize("live_class,live_model", [
        ("fast-low", "gpt-5.6-luna"),
        ("deep-high", "gpt-5.6-luna"),
        (None, None),
    ])
    async def test_pool_claim_never_downgrades_explicit_task_class(
        self, handler, db, tmp_path, live_class, live_model
    ):
        from src.intelligence_classes import IntelligenceClass

        handler.orchestrator.session_spec_builder._intelligence_classes = {
            "deep-high": IntelligenceClass("deep-high", "Deep", "", {"codex": {"model": "gpt-5.6-sol"}}),
        }
        await mktask(db, "deep", profile_id="worker", intelligence_class="deep-high")
        sid, wd = await pool_session(db, tmp_path)
        await db.update_session(sid, harness="codex", intelligence_class=live_class, model=live_model)
        # Edits affect the next launch, not the already-running pool's model.
        await db.update_agent("agent-1", harness="codex", intelligence_class="deep-high", model="gpt-5.6-sol")
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "no_ready_work"
        task = await db.get_task("deep")
        assert task.status == TaskStatus.READY and task.claim_epoch == 0
        assert task.assigned_agent_id is None
        assert (await db.get_session(sid)).claim_phase is None
        assert not (wd / ".aq" / "claim.json").exists()

    async def test_pool_claim_uses_live_sol_even_after_next_launch_settings_change(self, handler, db, tmp_path):
        from src.intelligence_classes import IntelligenceClass

        handler.orchestrator.session_spec_builder._intelligence_classes = {
            "deep-high": IntelligenceClass("deep-high", "Deep", "", {"codex": {"model": "gpt-5.6-sol"}}),
        }
        await mktask(db, "deep", profile_id="worker", intelligence_class="deep-high")
        sid, _ = await pool_session(db, tmp_path)
        await db.update_session(sid, harness="codex", intelligence_class="deep-high", model="gpt-5.6-sol")
        await db.update_agent("agent-1", harness="codex", intelligence_class="fast-low", model="gpt-5.6-luna")
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "claimed" and res["task"]["id"] == "deep"
        assert res["task"]["intelligence_class"] == "deep-high"
        assert (await db.get_task("deep")).assigned_agent_id == "agent-1"

    async def test_waiting_pool_rechecks_updated_profile_before_claiming(self, handler, db, tmp_path, monkeypatch):
        from src.intelligence_classes import IntelligenceClass

        handler.orchestrator.session_spec_builder._intelligence_classes = {
            "deep-high": IntelligenceClass("deep-high", "Deep", "", {"codex": {"model": "gpt-5.6-sol"}}),
            "fast-low": IntelligenceClass("fast-low", "Fast", "", {"codex": {"model": "gpt-5.6-luna"}}),
        }
        sid, _ = await pool_session(db, tmp_path)
        await db.update_session(sid, harness="codex", intelligence_class="deep-high", model="gpt-5.6-sol")
        waiting = asyncio.Event()
        attempt = handler._attempt_claim

        async def observe_attempt(*args, **kwargs):
            result = await attempt(*args, **kwargs)
            if result["result"] == "no_ready_work":
                waiting.set()
            return result

        monkeypatch.setattr(handler, "_attempt_claim", observe_attempt)
        claim = asyncio.create_task(scoped(handler, sid)._cmd_task_claim({"next": True, "wait": 1}))
        try:
            await asyncio.wait_for(waiting.wait(), timeout=5)
            await db.update_profile("worker", default_class="fast-low")
            await mktask(
                db,
                "new-fast",
                status=TaskStatus.DEFINED,
                profile_id="worker",
                intelligence_class=None,
            )
            await db.transition_task("new-fast", TaskStatus.READY, context="profile_changed")
            result = await claim
            assert result["result"] == "no_ready_work"
            assert (await db.get_task("new-fast")).status == TaskStatus.READY
        finally:
            if not claim.done():
                claim.cancel()
                await asyncio.gather(claim, return_exceptions=True)

    async def test_pool_claim_skips_incompatible_higher_priority_task(self, handler, db, tmp_path):
        from src.intelligence_classes import IntelligenceClass

        handler.orchestrator.session_spec_builder._intelligence_classes = {
            "deep-high": IntelligenceClass("deep-high", "Deep", "", {"codex": {"model": "gpt-5.6-sol"}}),
            "fast-low": IntelligenceClass("fast-low", "Fast", "", {"codex": {"model": "gpt-5.6-luna"}}),
        }
        await mktask(db, "deep", profile_id="worker", intelligence_class="deep-high", priority=1)
        await mktask(db, "fast", profile_id="worker", intelligence_class="fast-low", priority=100)
        sid, _ = await pool_session(db, tmp_path)
        await db.update_session(sid, harness="codex", intelligence_class="fast-low", model="gpt-5.6-luna")
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "claimed" and res["task"]["id"] == "fast"
        assert (await db.get_task("deep")).status == TaskStatus.READY

    async def test_pool_claim_takes_an_integration_repair_delegate(
        self, handler, db, tmp_path
    ):
        """A repair delegate is ordinary claimable work for a pool session.

        Both shipped repair profiles are ``lifecycle: pool``, so excluding
        delegates from the frontier would strand every repair dispatch.  The
        ownership half of the ladder is what has to accommodate the pull
        model (``aconfirm_integration_pool_owner_handoff``), not the queue.
        """
        await mktask(
            db,
            "repair-operation-0",
            profile_id="worker",
            priority=1,
            created_by_kind="integration_repair",
            created_by_id="operation",
        )
        await mktask(db, "ordinary", profile_id="worker", priority=100)
        sid, _ = await pool_session(db, tmp_path)

        res = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert res["result"] == "claimed" and res["task"]["id"] == "repair-operation-0"

    async def test_pool_claim_skips_an_expired_repair_delegate(
        self, handler, db, tmp_path
    ):
        """A stage can expire while its never-started delegate is still READY."""
        await mktask(
            db,
            "repair-operation-0",
            profile_id="worker",
            priority=1,
            created_by_kind="integration_repair",
            created_by_id="operation",
        )
        await mktask(db, "ordinary", profile_id="worker", priority=100)
        async with db.immediate() as conn:
            await conn.execute(
                integration_repair_stages.insert().values(
                    operation_id="operation",
                    ordinal=0,
                    policy={},
                    repair_task_id="repair-operation-0",
                    writer_kind="repair_delegate",
                    starting_sha="a" * 40,
                    attempts=0,
                    state="expired",
                )
            )
        sid, _ = await pool_session(db, tmp_path)

        res = await scoped(handler, sid)._cmd_task_claim({"next": True})

        assert res["result"] == "claimed" and res["task"]["id"] == "ordinary"
        assert (await db.get_task("repair-operation-0")).status is TaskStatus.READY

    async def test_no_ready_work_without_wait(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "no_ready_work"
        assert (await db.get_session(sid)).claim_phase is None

    async def test_wait_wakes_on_task_ready(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)

        async def promote():
            await asyncio.sleep(0.05)
            await mktask(db, "late", status=TaskStatus.DEFINED, profile_id="worker")
            await db.transition_task("late", TaskStatus.READY, context="promotion")

        asyncio.create_task(promote())
        t0 = time.monotonic()
        res = await scoped(handler, sid)._cmd_task_claim({"next": True, "wait": 3})
        assert (res["result"], res["task"]["id"]) == ("claimed", "late")
        assert time.monotonic() - t0 < 2.0  # woke on the event, not the deadline

    async def test_wait_clamped_and_times_out(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)
        handler.config.swarm.claim_wait_max = 0
        res = await scoped(handler, sid)._cmd_task_claim({"next": True, "wait": 100})
        assert res["result"] == "no_ready_work"

    async def test_prepare_failed_releases_and_reports(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        handler.orchestrator._worktree_slots.return_value.reset_slot_for_task = AsyncMock(
            side_effect=RuntimeError("git exploded")
        )
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "prepare_failed"
        assert not (wd / ".aq" / "claim.json").exists()
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        s = await db.get_session(sid)
        assert (s.claims, s.last_claim_result, s.claim_phase) == (0, "prepare_failed", None)
        assert await db.get_task_meta("t1", "claim_prepare_backoff_attempts") == 1
        assert await db.get_task_meta("t1", "claim_prepare_backoff_until") > time.time()
        # READY is intentionally not enough to re-offer a task whose slot
        # preparation just failed; this prevents a claim --wait hot loop.
        again = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert again["result"] == "no_ready_work"
        assert "pool.prepare_failed" in handler.orchestrator.bus.seen_event_types

    async def test_claim_file_write_failure_releases_and_reports(
        self, handler, db, tmp_path, monkeypatch
    ):
        """Any failure after ``record_holder`` committed — including an OSError
        writing the claim file — must release the claim and leave no claim
        file behind, same as a slot-reset failure (review finding #1)."""
        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        import src.commands.claim_commands as claim_commands

        monkeypatch.setattr(
            claim_commands,
            "write_claim_file",
            MagicMock(side_effect=OSError("disk full")),
        )
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "prepare_failed"
        assert not (wd / ".aq" / "claim.json").exists()
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        s = await db.get_session(sid)
        assert (s.claims, s.last_claim_result, s.claim_phase) == (0, "prepare_failed", None)

    async def test_pool_close_restores_its_slot_before_releasing_claim(
        self, handler, db, tmp_path
    ):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        slot = MagicMock()
        handler.orchestrator._slot_workspace_at = AsyncMock(return_value=slot)
        restore = AsyncMock()
        handler.orchestrator._worktree_slots.return_value.restore_slot_after_task = restore

        closed = await h._cmd_task_close(
            {"outcome": "pass", "summary": "done", "claim_epoch": claimed["claim_epoch"]}
        )

        assert closed["success"] is True
        restore.assert_awaited_once_with(slot, task_id="t1")
        assert (await db.get_session(sid)).task_id is None

    async def test_pool_close_skips_the_slot_restore_the_release_already_ran(
        self, handler, db, tmp_path
    ):
        """A failing close is restored inside ``complete_session_task``.

        Its integration-writer release needs a clean tree to prove the
        handoff, so it cannot wait for this call.  Salvaging the same slot a
        second time re-walks the archive path for nothing.
        """
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        handler.orchestrator.complete_session_task = AsyncMock(
            return_value={"status": TaskStatus.READY.value, "slot_restored": True}
        )
        slot = MagicMock()
        handler.orchestrator._slot_workspace_at = AsyncMock(return_value=slot)
        restore = AsyncMock()
        handler.orchestrator._worktree_slots.return_value.restore_slot_after_task = restore

        closed = await h._cmd_task_close(
            {"outcome": "fail", "summary": "no", "claim_epoch": claimed["claim_epoch"]}
        )

        assert closed["success"] is True
        restore.assert_not_awaited()
        assert (await db.get_session(sid)).task_id is None

    async def test_pool_close_reports_a_claim_release_it_could_not_make(
        self, handler, db, tmp_path
    ):
        """``_release_claim_on`` declines silently; the close must not.

        An integration owner still attached to this session makes the release
        a no-op, which left ``sessions.task_id`` and ``claim_phase='active'``
        set on a task already back on the frontier — with the claim file
        deleted and every surface reporting ``success: true`` (brisk-delta).
        """
        from src.database.queries.task_queries import TransitionResult

        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        handler.orchestrator._slot_workspace_at = AsyncMock(return_value=None)
        db.release_claim = AsyncMock(return_value=TransitionResult())

        closed = await h._cmd_task_close(
            {"outcome": "fail", "summary": "no", "claim_epoch": claimed["claim_epoch"]}
        )

        assert closed["claim_released"] is False
        assert closed["needs_attention"] == "claim_release_declined"
        assert await db.get_task_meta("t1", "needs_attention") == "claim_release_declined"
        # The session really does still hold the task, so its proof of that
        # must survive: deleting the claim file here is what made the stall
        # invisible to the worker loop as well.
        assert (wd / ".aq" / "claim.json").exists()

    async def test_pool_close_stays_quiet_when_a_reconciler_already_released_it(
        self, handler, db, tmp_path
    ):
        """The other reason ``_release_claim_on`` declines is benign.

        A pool reconciler can release the hold and the session can claim again
        before an old close resumes; the guarded write skipping that close is
        exactly what it is for.  The session no longer holds this task, so
        this is not the stuck hold above and must not be reported as one.
        """
        from src.database.queries.task_queries import TransitionResult

        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        handler.orchestrator._slot_workspace_at = AsyncMock(return_value=None)

        async def release_but_report_nothing(*args, **kwargs):
            await db.update_session(sid, task_id=None)
            return TransitionResult()

        db.release_claim = release_but_report_nothing

        closed = await h._cmd_task_close(
            {"outcome": "fail", "summary": "no", "claim_epoch": claimed["claim_epoch"]}
        )

        assert "claim_released" not in closed
        assert closed.get("needs_attention") != "claim_release_declined"
        assert await db.get_task_meta("t1", "needs_attention") is None
        assert not (wd / ".aq" / "claim.json").exists()

    async def test_duplicate_claim_is_idempotent_once_active(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        first = await h._cmd_task_claim({"next": True})
        second = await h._cmd_task_claim({"next": True})
        assert (second["result"], second["claim_epoch"]) == ("claimed", first["claim_epoch"])

    async def test_specific_task_held_by_other_is_conflict(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        s1, _ = await pool_session(db, tmp_path, sid="s1", agent_id="agent-1")
        s2, _ = await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
        await scoped(handler, s1)._cmd_task_claim({"task_id": "t1"})
        res = await scoped(handler, s2)._cmd_task_claim({"task_id": "t1"})
        assert res["result"] == "claim_conflict"

    @pytest.mark.parametrize("cap", [None, 1, 8])
    @pytest.mark.parametrize("claim_next", [False, True])
    async def test_default_close_retires_context_without_claiming_next_task(
        self, handler, db, tmp_path, cap, claim_next
    ):
        await db.update_profile("worker", max_claims_per_session=cap)
        await mktask(db, "t1", profile_id="worker")
        await mktask(db, "t2", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        first = await h._cmd_task_claim({"next": True})
        # Reading/retrying the currently held task must not clear its context.
        again = await h._cmd_task_claim({"next": True})
        assert again["claim_epoch"] == first["claim_epoch"]
        assert (await db.get_session(sid)).desired_state == "running"
        closed = await h._cmd_task_close({
            "task_id": "t1", "outcome": "pass", "summary": "done",
            "claim_epoch": first["claim_epoch"], "claim_next": claim_next,
        })
        assert closed["success"] is True
        assert (await db.get_session(sid)).desired_state == "stopped"
        second = await db.get_task("t2")
        assert second.status == TaskStatus.READY and second.assigned_agent_id is None
        if claim_next:
            assert closed["next"]["result"] == "drain_requested"
        assert (await h._cmd_task_claim({"task_id": "t2"}))["result"] == "drain_requested"

    async def test_disabled_pool_profile_is_refused_new_work(self, handler, db, tmp_path):
        """The operator switch stops the *next* claim, not the current task."""
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        held = await h._cmd_task_claim({"next": True})
        assert held["result"] == "claimed"

        await db.update_profile("worker", enabled=False)

        # The task already held is untouched and still assigned to this session.
        assert (await db.get_task("t1")).status == TaskStatus.IN_PROGRESS
        refused = await h._cmd_task_claim({"next": True})
        assert (refused["result"], refused["reason"]) == ("drain_requested", "pool is disabled")

        await mktask(db, "t2", profile_id="worker")
        # A long poll ends on the switch rather than waiting out its deadline.
        waited = await h._cmd_task_claim({"next": True, "wait": 5})
        assert waited["result"] == "drain_requested"
        assert (await db.get_task("t2")).status == TaskStatus.READY

        # Enabling restores eligibility for the pool's next worker.
        await db.update_profile("worker", enabled=True)
        s2, _ = await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
        assert (await scoped(handler, s2)._cmd_task_claim({"next": True}))["result"] == "claimed"

    async def test_disabling_a_pool_wakes_a_pending_long_poll(
        self, handler, db, tmp_path, monkeypatch
    ):
        """The switch reaches a claim that is *already* parked in a long poll.

        Without ``pool.enabled_changed`` among the wake events the disable is
        only noticed when the wait expires, so a worker keeps asking for work
        for up to ``swarm.claim_wait_max`` seconds after its pool was turned
        off.  Work that became ready in the meantime stays unclaimed.
        """
        sid, _ = await pool_session(db, tmp_path)
        waiting = asyncio.Event()
        attempt = handler._attempt_claim

        async def observe_attempt(*args, **kwargs):
            result = await attempt(*args, **kwargs)
            if result["result"] == "no_ready_work":
                waiting.set()
            return result

        monkeypatch.setattr(handler, "_attempt_claim", observe_attempt)
        t0 = time.monotonic()
        claim = asyncio.create_task(
            scoped(handler, sid)._cmd_task_claim({"next": True, "wait": 5})
        )
        try:
            await asyncio.wait_for(waiting.wait(), timeout=5)
            # Ready work exists by the time the switch flips; creating the row
            # emits no ``task.ready``, so the poll is still parked on it.
            await mktask(db, "late", profile_id="worker")
            flip = await handler._cmd_pool_set_enabled(
                {"profile_id": "worker", "enabled": False}
            )
            assert flip["success"] is True
            result = await asyncio.wait_for(claim, timeout=5)
        finally:
            if not claim.done():
                claim.cancel()
                await asyncio.gather(claim, return_exceptions=True)
        assert (result["result"], result["reason"]) == ("drain_requested", "pool is disabled")
        assert time.monotonic() - t0 < 4.0  # woke on the event, not the deadline
        assert (await db.get_task("late")).status == TaskStatus.READY

    async def test_old_idle_pool_cannot_reuse_completed_context(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)
        await db.update_session(sid, claims=3)
        await mktask(db, "t2", profile_id="worker")
        result = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert result["result"] == "session_exhausted"
        assert (await db.get_session(sid)).desired_state == "stopped"
        assert (await db.get_task("t2")).status == TaskStatus.READY

    async def test_session_exhausted_after_cap_via_close_claim_next(self, handler, db, tmp_path):
        handler.config.swarm.fresh_context_per_task = False
        await db.update_profile("worker", max_claims_per_session=1)
        await mktask(db, "t1", profile_id="worker")
        await mktask(db, "t2", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        first = await h._cmd_task_claim({"next": True})
        closed = await h._cmd_task_close(
            {
                "task_id": "t1",
                "outcome": "pass",
                "summary": "done",
                "claim_epoch": first["claim_epoch"],
                "claim_next": True,
            }
        )
        assert closed["success"] is True
        assert closed["next"]["result"] == "session_exhausted"
        assert not (wd / ".aq" / "claim.json").exists()
        assert (await db.get_task("t1")).status == TaskStatus.COMPLETED
        assert (await db.get_workspace_for_agent("agent-1")).locked_by_agent_id == "agent-1"

    async def test_not_admissible_when_project_paused(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        await db.update_project(PROJECT_ID, status="PAUSED")
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert (res["result"], res["reason"]) == ("not_admissible", "project_inactive")

    async def test_not_admissible_when_budget_exhausted(self, handler, db, tmp_path):
        """review finding #2 — ``_admission_reason`` must read ``Project.budget_limit``,
        not the nonexistent ``token_budget``."""
        from src.scheduler import SchedulerState

        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        await db.update_project(PROJECT_ID, budget_limit=50)
        handler.orchestrator._last_scheduler_state = SchedulerState(
            projects=[],
            tasks=[],
            agents=[],
            project_token_usage={PROJECT_ID: 100},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert (res["result"], res["reason"]) == ("not_admissible", "budget_exhausted")

    async def test_task_lifecycle_session_reclaims_own_task_only(self, handler, db, tmp_path):
        await db.create_agent(
            Agent(id="agent-1", name="agent-1", profile_id="worker", state=AgentState.BUSY)
        )
        await mktask(
            db,
            "mine",
            status=TaskStatus.IN_PROGRESS,
            profile_id="worker",
            assigned_agent_id="agent-1",
            claim_epoch=1,
        )
        await mktask(db, "other", profile_id="worker")
        await db.create_session(
            SessionRecord(
                id="s-task",
                project_id=PROJECT_ID,
                profile_id="worker",
                harness="claude",
                provider="fake",
                name="s-task",
                lifecycle="task",
                task_id="mine",
                work_dir="/x",
                epoch="e",
                instance_token="t",
                started_at=NOW,
                state="running",
            )
        )
        h = scoped(handler, "s-task")
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        assert (await h._cmd_task_claim({"task_id": "other"}))["result"] == "out_of_scope"


class TestFence:
    async def test_stale_epoch_rejected_on_close(self, handler, db, tmp_path):
        handler.config.swarm.fresh_context_per_task = False
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})  # epoch 1
        await db.release_claim(sid, task_status=TaskStatus.READY, context="x", now=NOW)
        await h._cmd_task_claim({"next": True})  # epoch 2
        res = await h._cmd_task_close(
            {"task_id": "t1", "outcome": "pass", "summary": "x", "claim_epoch": 1}
        )
        assert res["result"] == "stale_claim"
        assert (await db.get_task("t1")).status == TaskStatus.IN_PROGRESS

    async def test_pool_session_must_send_epoch(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})
        res = await h._cmd_task_close({"task_id": "t1", "outcome": "pass", "summary": "x"})
        assert res["result"] == "stale_claim"

    async def test_heartbeat_set_handoff_require_matching_epoch(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})
        assert (await h._cmd_task_heartbeat({"task_id": "t1", "claim_epoch": 1}))["success"]
        assert (await h._cmd_task_heartbeat({"task_id": "t1", "claim_epoch": 7}))[
            "result"
        ] == "stale_claim"
        assert (await h._cmd_task_set({"task_id": "t1", "note": "x", "claim_epoch": 7}))[
            "result"
        ] == "stale_claim"
        assert (await h._cmd_task_handoff({"task_id": "t1", "reason": "x", "claim_epoch": 7}))[
            "result"
        ] == "stale_claim"

    async def test_other_session_is_out_of_scope(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        s1, _ = await pool_session(db, tmp_path, sid="s1", agent_id="agent-1")
        s2, _ = await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
        await scoped(handler, s1)._cmd_task_claim({"next": True})
        res = await scoped(handler, s2)._cmd_task_heartbeat({"task_id": "t1", "claim_epoch": 1})
        assert res["result"] == "out_of_scope"

    async def test_prime_resolves_task_from_session(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})
        res = await h._cmd_prime({})
        assert res["success"] is True
        body = res.get("body") or res.get("prompt") or json.dumps(res)
        assert "t1" in body and "Claim epoch: 1" in body


class TestReadScope:
    """I6: a pool token pins no ``task_id``, so ``project_id`` is the fence."""

    async def setup_other_project(self, db):
        await db.create_project(Project(id="other", name="o"))
        await db.create_task(
            Task(
                id="foreign",
                project_id="other",
                title="foreign",
                description="d",
                status=TaskStatus.READY,
            )
        )

    @pytest.mark.parametrize(
        "command",
        [
            "_cmd_get_task",
            "_cmd_task_show",
            "_cmd_task_children",
            "_cmd_task_progress",
            "_cmd_prime",
        ],
    )
    async def test_cross_project_read_refused(self, handler, db, tmp_path, command):
        await self.setup_other_project(db)
        sid, _ = await pool_session(db, tmp_path)
        res = await getattr(scoped(handler, sid), command)({"task_id": "foreign"})
        assert res["result"] == "out_of_scope"
        assert res["success"] is False

    @pytest.mark.parametrize(
        "command",
        [
            "_cmd_get_task",
            "_cmd_task_show",
            "_cmd_task_children",
            "_cmd_task_progress",
            "_cmd_prime",
        ],
    )
    async def test_same_project_read_allowed(self, handler, db, tmp_path, command):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        res = await getattr(scoped(handler, sid), command)({"task_id": "t1"})
        assert res.get("result") != "out_of_scope"
        assert "error" not in res

    async def test_local_scope_reads_any_project(self, handler, db, tmp_path):
        await self.setup_other_project(db)
        handler._current_scope = None
        res = await handler._cmd_get_task({"task_id": "foreign"})
        assert res["id"] == "foreign"

    async def test_elevated_session_reads_any_project(self, handler, db, tmp_path):
        await self.setup_other_project(db)
        sid, _ = await pool_session(db, tmp_path)
        scoped(handler, sid)
        handler._current_scope["elevated"] = True
        res = await handler._cmd_get_task({"task_id": "foreign"})
        assert res["id"] == "foreign"


class TestImplicitTaskId:
    """I3: the pool worker loop closes/heartbeats without naming a task."""

    async def test_close_resolves_held_task_from_scope(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        res = await h._cmd_task_close(
            {"outcome": "pass", "summary": "done", "claim_epoch": claimed["claim_epoch"]}
        )
        assert res["success"] is True
        assert (await db.get_task("t1")).status == TaskStatus.COMPLETED

    async def test_heartbeat_resolves_held_task_from_scope(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        res = await h._cmd_task_heartbeat({"claim_epoch": claimed["claim_epoch"]})
        assert res["success"] is True

    async def test_close_without_a_held_task_errors_clearly(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_close({"outcome": "pass"})
        assert res == {"success": False, "error": "no task_id and the session holds no task"}

    async def test_heartbeat_without_a_held_task_errors_clearly(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_heartbeat({})
        assert res == {"success": False, "error": "no task_id and the session holds no task"}


class TestSwarmDisabledGate:
    async def test_claim_refused_when_swarm_disabled(self, handler, db, tmp_path):
        """M8: the command stays callable but hands out no work."""
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        handler.config.swarm.enabled = False
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert (res["result"], res["reason"]) == ("not_admissible", "swarm_disabled")
        assert (await db.get_task("t1")).status == TaskStatus.READY


class TestActiveClaimWithDeletedTask:
    async def test_missing_held_task_returns_out_of_scope(self, handler, db, tmp_path):
        """M4: a held task row gone underneath the session must not AttributeError.

        ``sessions.task_id`` is a plain FK, so a straight ``delete_task``
        is refused while the session still points at it -- the row can only
        vanish through a path that clears the reference first (or on a
        backend/ordering where it does).  The re-claim's ``active`` branch
        has to survive the read coming back ``None`` either way, so the
        read is stubbed rather than the row contrived away.
        """
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        db._get_task_conn = AsyncMock(return_value=None)
        res = await h._cmd_task_claim({"next": True})
        assert res["result"] == "out_of_scope"
        assert "no longer exists" in res["reason"]


class TestStaleClaimBinding:
    """A binding whose task left IN_PROGRESS must never be re-served.

    ``take_claim_slot`` decides "already active" from ``sessions.task_id``
    alone.  A release that did not land (an attached integration owner
    vetoes ``_release_claim_on``) leaves the session naming a task that is
    back on the frontier, and the old ``active`` branch handed it straight
    back as ``claimed`` -- a task the worker could neither comment on nor
    close, re-offered on every claim until the lease reaper killed it.
    """

    @staticmethod
    async def _strand(db, sid, tid):
        """Move the held task off IN_PROGRESS without releasing the session."""
        await db.transition_task(
            tid, TaskStatus.READY, context="test", force=True, assigned_agent_id=None
        )
        assert (await db.get_session(sid)).task_id == tid

    async def test_stale_binding_is_released_and_the_task_reclaimed(
        self, handler, db, tmp_path
    ):
        handler.config.swarm.fresh_context_per_task = False  # let the same session re-claim
        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        first = await h._cmd_task_claim({"next": True})
        assert first["result"] == "claimed"
        await self._strand(db, sid, "t1")

        again = await h._cmd_task_claim({"next": True})

        # A real claim, not the old phantom: fresh epoch, IN_PROGRESS task,
        # and a claim file the fence commands can read.
        assert again["result"] == "claimed"
        assert again["claim_epoch"] > first["claim_epoch"]
        task = await db.get_task("t1")
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.assigned_agent_id == "agent-1"
        assert json.loads((wd / ".aq" / "claim.json").read_text())["claim_epoch"] == (
            again["claim_epoch"]
        )

    async def test_stale_binding_reports_no_ready_work_when_nothing_is_ready(
        self, handler, db, tmp_path
    ):
        handler.config.swarm.fresh_context_per_task = False
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        await db.transition_task(
            "t1", TaskStatus.BLOCKED, context="test", force=True, assigned_agent_id=None
        )

        res = await h._cmd_task_claim({"next": True})

        assert res["result"] == "no_ready_work"
        session = await db.get_session(sid)
        assert (session.task_id, session.claim_phase) == (None, None)

    async def test_stale_binding_retires_a_fresh_context_worker(
        self, handler, db, tmp_path
    ):
        """The default one-claim-per-session cap turns the repair into an exit.

        ``fresh_context_per_task`` caps the session at a single claim, which
        the stranded task already spent.  ``session_exhausted`` is what the
        pool loop is told to exit on, so the worker leaves cleanly instead of
        spinning on a task it cannot close.
        """
        assert handler.config.swarm.fresh_context_per_task is True
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        await self._strand(db, sid, "t1")

        res = await h._cmd_task_claim({"next": True})

        assert res["result"] == "session_exhausted"
        session = await db.get_session(sid)
        assert (session.task_id, session.desired_state) == (None, "stopped")

    async def test_vetoed_release_drains_instead_of_re_serving(
        self, handler, db, tmp_path
    ):
        """The integration-owner veto is evidence, not something to erase.

        ``release_claim`` refuses while an attached owner still names this
        session/workspace pair, so the binding cannot be unwound here.  The
        claim must stop the loop rather than hand back an unclosable task;
        the stall stays visible on the task and the reconciler owns cleanup.
        """
        from src.database.queries.task_queries import TransitionResult

        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})
        await self._strand(db, sid, "t1")
        db.release_claim = AsyncMock(return_value=TransitionResult())

        res = await h._cmd_task_claim({"next": True})

        assert res["result"] == "drain_requested"
        assert "t1" in res["reason"]
        assert (await db.get_session(sid)).desired_state == "stopped"
        assert await db.get_task_meta("t1", "needs_attention") == "claim_binding_stale"
        # The claim file is the handoff's evidence: it stays put.
        assert (wd / ".aq" / "claim.json").exists()

    async def test_a_reclaim_between_observation_and_release_is_left_alone(
        self, handler, db, tmp_path
    ):
        """The repair must not clobber the worker that legitimately took over.

        ``_attempt_claim`` reads the stranded task in one transaction and
        ``_recover_stale_binding`` releases it in a later one.  In between,
        the task is READY on the frontier and any pool worker may claim it.
        The release replays a status/epoch it no longer holds, so it must
        match nothing: the new owner keeps the task IN_PROGRESS, keeps its
        ``assigned_agent_id``, and this session drains instead.
        """
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
        h = scoped(handler, sid)
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        await self._strand(db, sid, "t1")

        real_release = db.release_claim

        async def claim_from_under_us(*a, **kw):
            # Exactly the interleaving: the other worker's claim commits
            # between our observation and our release transaction.
            async with db.immediate() as conn:
                assert await db.take_task(conn, "t1", agent_id="agent-2", now=time.time())
            db.release_claim = real_release
            return await real_release(*a, **kw)

        db.release_claim = claim_from_under_us

        res = await h._cmd_task_claim({"next": True})

        assert res["result"] == "drain_requested"
        task = await db.get_task("t1")
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.assigned_agent_id == "agent-2"
        # The other worker's task is not flagged for a stall that is ours.
        assert await db.get_task_meta("t1", "needs_attention") is None
        assert (await db.get_session(sid)).desired_state == "stopped"

    async def test_a_task_reclaimed_before_the_claim_is_never_re_served(
        self, handler, db, tmp_path
    ):
        """The same binding, one step on: IN_PROGRESS under someone else.

        ``take_claim_slot`` still answers ``active`` from ``sessions.task_id``,
        and the task is IN_PROGRESS again -- so an IN_PROGRESS check alone
        would hand a second session the task agent-2 is working.  Nothing
        about the task may be touched here.
        """
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
        h = scoped(handler, sid)
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        await self._strand(db, sid, "t1")
        async with db.immediate() as conn:
            assert await db.take_task(conn, "t1", agent_id="agent-2", now=time.time())
        before = await db.get_task("t1")

        res = await h._cmd_task_claim({"next": True})

        assert res["result"] == "drain_requested"
        assert "agent-2" in res["reason"]
        after = await db.get_task("t1")
        assert (after.status, after.assigned_agent_id, after.claim_epoch) == (
            TaskStatus.IN_PROGRESS,
            "agent-2",
            before.claim_epoch,
        )
        assert (await db.get_session(sid)).desired_state == "stopped"



class TestEventWaiter:
    async def test_waiter_subscribes_before_check(self):
        from src.event_bus import EventBus

        bus = EventBus()
        w = bus.waiter(["task.ready"], filter={"project_id": "p"})
        await bus.emit("task.ready", {"task_id": "t", "project_id": "p", "title": "t"})
        assert (await w.wait(0.5))["task_id"] == "t"
        w.close()
        assert bus.subscriber_count("task.ready") == 0


class TestTaskShowClaimedBy:
    """``task_show.claimed_by`` — spec §14.

    Assembled from ``task_metadata.claimed_by_session``,
    ``tasks.assigned_agent_id`` and ``tasks.claim_epoch``.
    """

    async def test_unclaimed_task_reports_none(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        res = await handler._cmd_task_show({"task_id": "t1"})
        assert res["claimed_by"] is None

    async def test_claimed_task_reports_holder(self, handler, db, tmp_path):
        handler.orchestrator.bus.emit = AsyncMock()
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        claim = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert claim["result"] == "claimed"

        res = await handler._cmd_task_show({"task_id": "t1"})
        assert res["claimed_by"] == {
            "session_id": sid,
            "agent_id": "agent-1",
            "claim_epoch": claim["claim_epoch"],
        }

    async def test_claim_epoch_matches_the_fence_handed_to_the_worker(self, handler, db, tmp_path):
        handler.orchestrator.bus.emit = AsyncMock()
        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        await scoped(handler, sid)._cmd_task_claim({"next": True})

        on_disk = json.loads((wd / ".aq" / "claim.json").read_text())
        res = await handler._cmd_task_show({"task_id": "t1"})
        assert res["claimed_by"]["claim_epoch"] == on_disk["claim_epoch"]
