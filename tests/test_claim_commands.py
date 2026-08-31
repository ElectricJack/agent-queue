"""aq task claim / close --claim-next / epoch fence — spec §10, §14."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
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

PROJECT_ID = "proj"
NOW = time.time()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
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
        database_path=str(tmp_path / "test.db"),
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
            await mktask(db, "new-fast", status=TaskStatus.DEFINED, profile_id="worker")
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

    async def test_session_exhausted_after_cap_via_close_claim_next(self, handler, db, tmp_path):
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
