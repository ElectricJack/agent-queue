"""End-to-end pool worker loop — spec §9-§12 (fake provider, real cascade).

Drives the REAL cascade (``run_one_cycle``) and the REAL handler
(``_cmd_task_claim``/``_cmd_task_close``/``_cmd_create_task``) against a
fake session provider — no tmux, no real agent, no LLM.  Mirrors the ``orch``
fixture in ``tests/test_pool_reconciler.py`` and the handler fixture in
``tests/test_claim_commands.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
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
from src.orchestrator.core import _eval_pipeline_when
from src.sessions.harness_parser import Harness

PROJECT_ID = "proj"

#: Provenance predicate used to verify the worker-filed event payload. Custom
#: task.created subscribers can still use these fields even though the bundled
#: pipeline no longer performs worker-filed triage.
WORKER_FILED_EVENT_WHEN = {
    "all": [
        {"field": "event.created_by_kind", "equals": "session"},
        {"field": "event.parent_task_id", "is_null": True},
    ]
}


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_profile(
        AgentProfile(
            id="worker",
            name="w",
            lifecycle="pool",
            harness="claude",
            max_active=1,
            max_claims_per_session=2,
            default_class="fast-low",
            needs_workspace=False,
        )
    )
    await database.create_project(Project(id=PROJECT_ID, name="p", default_profile_id="worker"))
    await database.create_workspace(
        Workspace(
            id="ws0",
            project_id=PROJECT_ID,
            workspace_path=str(tmp_path / "ws0"),
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
    cfg.swarm.claim_wait_max = 5
    cfg.swarm.max_starts_per_tick = 5
    o = Orchestrator(cfg)
    o.session_spec_builder._intelligence_classes = {
        "fast-low": IntelligenceClass(
            "fast-low",
            "Fast",
            "",
            {"anthropic": {"model": "claude-haiku"}},
        )
    }
    o.db = db
    # AgentReconciler and SessionReconciler were both built in __init__
    # against the (real, uninitialized) db the constructor saw -- point
    # them at the test db too, same as tests/test_pool_reconciler.py's
    # ``orch`` fixture (which only needs the former; this test drives the
    # full ``run_one_cycle`` cascade, so ``_reconcile_sessions`` needs the
    # latter as well or every reconciler step raises against a bare engine).
    o._agent_reconciler._db = db
    o.session_reconciler.db = db
    o.session_lens.db = db
    o.agent_questions.db = db
    o.transcript_watcher.db = db
    o.git = MagicMock()
    # Pool launch installs the managed git excludes before it hands a checkout
    # to a session (src/orchestrator/pools.py::_launch_pool_session), which
    # reaches for the real GitManager; tmp_path holds no checkout.  Same stub
    # as tests/test_pool_reconciler.py's ``orch`` fixture.
    o._ensure_control_files_excluded = AsyncMock(return_value=True)
    # MessageDeliveryEngine was built in __init__ against the constructor's
    # (uninitialized) db, like the reconcilers above; the cascade's delivery
    # pass runs every cycle.
    o.message_delivery._db = db
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
    # No real checkout under tmp_path -- stub the worktree-slot reset the
    # claim path drives, and the completion pipeline task_close runs on a
    # "pass" outcome, same stubs as tests/test_claim_commands.py.
    o._worktree_slots = MagicMock(
        return_value=MagicMock(reset_slot_for_task=AsyncMock(return_value="aq/t"))
    )
    o._run_completion_pipeline = AsyncMock(return_value=(None, True))
    o.register_settlement_listener()
    return o


@pytest.fixture
def handler(orch):
    return CommandHandler(orch, orch.config)


def scoped(handler, sid):
    handler._current_scope = {
        "kind": "session",
        "session_id": sid,
        "task_id": None,
        "project_id": PROJECT_ID,
        "elevated": False,
    }
    return handler


def emitted_payloads(orch, event_type):
    return [c.args[1] for c in orch.bus.emit.await_args_list if c.args[0] == event_type]


async def mktask(db, tid, **kw):
    await db.create_task(
        Task(
            id=tid,
            project_id=PROJECT_ID,
            title=tid,
            description=tid,
            status=TaskStatus.READY,
            profile_id="worker",
            intelligence_class="fast-low",
            **kw,
        )
    )


class TestSwarmWorkerLoopEndToEnd:
    async def test_default_next_task_gets_fresh_session_on_same_worker(self, orch, handler, db):
        await mktask(db, "t1")
        await mktask(db, "t2")
        await orch.run_one_cycle()
        old = (await db.list_sessions(lifecycle="pool", live_only=True))[0]
        first = await scoped(handler, old.id)._cmd_task_claim({"next": True})
        await handler._cmd_task_close({
            "task_id": "t1", "outcome": "pass", "summary": "done",
            "claim_epoch": first["claim_epoch"], "claim_next": True,
        })
        assert (await db.get_task("t2")).status == TaskStatus.READY
        # No simulated process exit: the daemon itself retires the old context.
        await orch.run_one_cycle()
        await orch.run_one_cycle()
        live = await db.list_sessions(lifecycle="pool", live_only=True)
        assert len(live) == 1
        fresh = live[0]
        assert fresh.id != old.id and fresh.instance_token != old.instance_token
        assert fresh.agent_id == old.agent_id
        provider = orch.session_providers.create("fake")
        old_spec = next(spec for spec in provider.starts if spec.session_name == old.name)
        fresh_spec = next(spec for spec in provider.starts if spec.session_name == fresh.name)
        assert "--resume" not in fresh_spec.command
        assert old_spec.command[old_spec.command.index("--session-id") + 1] == old.id
        assert fresh_spec.command[fresh_spec.command.index("--session-id") + 1] == fresh.id
        assert (await db.get_session(old.id)).state == "stopped"
        next_task = await scoped(handler, fresh.id)._cmd_task_claim({"next": True})
        assert (next_task["result"], next_task["task"]["id"]) == ("claimed", "t2")

    async def test_pool_worker_loop_end_to_end(self, orch, handler, db):
        orch.config.swarm.fresh_context_per_task = False
        await mktask(db, "t1")
        await mktask(db, "t2")
        await mktask(db, "t3")

        # Phase 1: the real cascade starts one pool session (max_active=1).
        await orch.run_one_cycle()
        pool = await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID)
        assert len(pool) == 1
        sid = pool[0].id
        agent_id = pool[0].agent_id
        assert pool[0].name.startswith("p-")

        # Phase 2: the worker claims t1 via the handler (``aq task claim --next``).
        h = scoped(handler, sid)
        first = await h._cmd_task_claim({"next": True})
        assert (first["result"], first["task"]["id"]) == ("claimed", "t1")

        # Phase 3: close t1 with claim_next -- claims t2 (claim #2 of 2).
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
        assert (closed["next"]["result"], closed["next"]["task"]["id"]) == ("claimed", "t2")
        second_epoch = closed["next"]["claim_epoch"]

        # Phase 4: close t2 with claim_next -- the session hit its cap.
        closed2 = await h._cmd_task_close(
            {
                "task_id": "t2",
                "outcome": "pass",
                "summary": "done",
                "claim_epoch": second_epoch,
                "claim_next": True,
            }
        )
        assert closed2["success"] is True
        assert closed2["next"]["result"] == "session_exhausted"
        assert (await db.get_task("t1")).status == TaskStatus.COMPLETED
        assert (await db.get_task("t2")).status == TaskStatus.COMPLETED

        # Phase 5: the provider reports the (now idle, exhausted) session
        # exiting 0 -- the next cycle's reconciler must terminate it fully.
        provider = orch.session_providers.create("fake")
        session_row = await db.get_session(sid)
        provider.script_death(session_row.name, after_s=0)

        # ``_reconcile_pools`` (phase 2, step 6b) runs *before*
        # ``_reconcile_sessions`` (phase 4) in ``run_one_cycle`` -- so this
        # first cycle's pool sizing still sees the exhausted session as
        # occupying the one ``max_active`` slot, and only the reconciler
        # step (later in the same cycle) detects the exit and terminates.
        await orch.run_one_cycle()

        agent = await db.get_agent(agent_id)
        assert agent.state == AgentState.IDLE
        assert await db.get_workspace_for_agent(agent_id) is None

        # Phase 6: the *next* cycle's pool reconcile sees the gap the
        # termination left and starts a fresh session for t3, still READY
        # and unclaimed.
        await orch.run_one_cycle()
        pool_after = await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID)
        live = [s for s in pool_after if s.state == "running"]
        assert len(live) == 1
        new_sid = live[0].id
        assert new_sid != sid
        assert live[0].agent_id == agent_id
        assert (await db.get_agent(agent_id)).profile_id == "worker"
        h2 = scoped(handler, new_sid)
        third = await h2._cmd_task_claim({"next": True})
        assert (third["result"], third["task"]["id"]) == ("claimed", "t3")

        # Every event this path actually fires carries the base triple
        # (task_id, project_id, title) -- event schema (spec §14). Narrowed
        # to the two event types this pool/session-close path fires:
        # ``task.claimed`` (src/commands/claim_commands.py's
        # ``_prepare_and_activate``) and ``task.closed`` (the session-close
        # transition, src/orchestrator/execution.py's
        # ``complete_session_task`` -- NOT ``task.completed``, which is a
        # different, monitoring-loop-only event this path never emits).
        # ``task.ready`` doesn't fire either: t1/t2/t3 are seeded directly
        # as READY rather than promoted from DEFINED, so there's no
        # promotion transition to emit it from.
        for etype in ("task.claimed", "task.closed"):
            payloads = emitted_payloads(orch, etype)
            assert payloads, f"expected at least one {etype!r} event"
            for payload in payloads:
                assert {"task_id", "project_id", "title"} <= payload.keys()

    async def test_worker_filed_task_lands_defined_with_routing_gate(self, orch, handler, db):
        await mktask(db, "t1")
        await orch.run_one_cycle()
        pool = await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID)
        sid = pool[0].id
        h = scoped(handler, sid)
        claimed = await h._cmd_task_claim({"next": True})
        assert claimed["result"] == "claimed"

        res = await h._cmd_create_task({
            "title": "found a bug while working t1",
            "reason": "The active task uncovered a separate defect.",
        })
        assert res["success"] is True
        filed_id = res["task_id"]
        assert res["status"] == TaskStatus.DEFINED.value
        assert res["gate_id"] is not None

        filed = await db.get_task(filed_id)
        assert filed.status == TaskStatus.DEFINED
        assert filed.created_by_kind == "session"
        assert filed.parent_task_id is None
        gates = await db.get_gates_for_task(filed_id)
        assert any(g["gate_type"] == "routing" for g in gates)

        created_payloads = emitted_payloads(orch, "task.created")
        payload = next(p for p in created_payloads if p["task_id"] == filed_id)
        assert _eval_pipeline_when(WORKER_FILED_EVENT_WHEN, payload) is True
