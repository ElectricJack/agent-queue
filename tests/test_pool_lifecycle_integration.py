"""End-to-end worker-pool lifecycle on a real database (swarm-work-model §10-§11).

The unit suites (``test_pool_reconciler.py``, ``test_claim_commands.py``,
``test_claim_queries.py``) each prove one hop of the pull loop in isolation.
This module drives **one pool session through the whole loop** against a live
adapter — launch → ``aq task claim --next --wait`` → ``aq prime`` (pool
variant) → work → ``aq task close --claim-next`` → recycle under
``fresh_context_per_task`` → ``no_ready_work`` → scale-down after
``scale_down_grace`` → drain → session row ``stopped`` and agent row
``RETIRED``.

Every test is parametrised over SQLite **and** PostgreSQL (the production
backend, per ``docs/specs/design/``): SQLite's single-writer ``immediate()``
lock serialises the claim CAS, so it proves the *result* is right without
ever exercising the row-level locking the pull loop actually depends on in
production.  Postgres arms run whenever ``POSTGRES_TEST_DSN`` is set and skip
otherwise, matching ``tests/test_claim_queries.py``'s ``any_db`` shape.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from dataclasses import replace

from src.commands.handler import CommandHandler
from src.config import DatabaseConfig, AppConfig, DiscordConfig
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
from src.sessions.harness_parser import Harness
from src.sessions.reconciler import SessionReconciler
from tests.pg_dsn import ensure_worker_postgres_dsn
from tests.db_fixtures import lease_dsn

PROJECT_ID = "proj"

#: Per-xdist-worker database (tests/pg_dsn.py) so a parallel worker's
#: ``reset_for_tests()`` truncate cannot race this suite's seed.
POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()

_CLASSES = {
    "standard-medium": IntelligenceClass(
        "standard-medium", "Standard", "", {"anthropic": {"model": "claude-sonnet-5"}}
    ),
    "deep-high": IntelligenceClass(
        "deep-high", "Deep", "", {"anthropic": {"model": "claude-opus-5"}}
    ),
}


@pytest.fixture
async def db(request, tmp_path):
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    # These fixtures model independent clones; slot provisioning has its own tests.
    kind = await database.resolve_workspace_kind("__system__", "project-repo")
    await database.upsert_workspace_kind(replace(kind, mode="exclusive-clone"))
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(
        AgentProfile(
            id="worker",
            name="w",
            lifecycle="pool",
            min_active=0,
            max_active=2,
            harness="claude",
            default_class="standard-medium",
        )
    )
    for i in range(2):
        path = tmp_path / f"ws{i}"
        path.mkdir()
        await database.create_workspace(
            Workspace(
                id=f"ws{i}",
                project_id=PROJECT_ID,
                workspace_path=str(path),
                source_type=RepoSourceType.LINK,
                kind_id="project-repo",
            )
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
    cfg.swarm.max_starts_per_tick = 5
    return cfg


@pytest.fixture
async def orch(db, config):
    o = Orchestrator(config)
    o.session_spec_builder._intelligence_classes = dict(_CLASSES)
    o.db = db
    o._agent_reconciler._db = db
    # The reconciler is also built in ``Orchestrator.__init__``.  Keep its
    # observation/recovery pass on this fixture's initialized adapter rather
    # than the constructor's uninitialized production adapter.
    o.session_reconciler.db = db
    o.git = MagicMock()
    # The claim now persists the branch its slot reset created, so
    # ``task_close`` reaches the "capture the final branch tip" read that a
    # branchless pool task used to skip.  There is no checkout behind this
    # fixture's git mock; answer with no SHA.
    o.git.arev_parse = AsyncMock(return_value=None)
    # Pool launch installs the managed git excludes before it hands a
    # checkout to a session (src/orchestrator/pools.py), which reaches for
    # the real GitManager; these fixtures have no checkout.  Same stub as
    # tests/test_pool_reconciler.py's ``orch`` fixture.
    o._ensure_control_files_excluded = AsyncMock(return_value=True)
    o._last_scheduler_state = None  # no snapshot yet → admissible
    o._run_completion_pipeline = AsyncMock(return_value=(None, True))
    o._worktree_slots = MagicMock(
        return_value=MagicMock(reset_slot_for_task=AsyncMock(return_value="aq/t"))
    )
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
    o.register_settlement_listener()
    return o


@pytest.fixture
def handler(orch, config):
    return CommandHandler(orch, config)


async def ready(db, tid, *, profile_id="worker", intelligence_class="standard-medium", **kw):
    await db.create_task(
        Task(
            id=tid,
            project_id=PROJECT_ID,
            title=tid,
            description=tid,
            status=TaskStatus.READY,
            profile_id=profile_id,
            intelligence_class=intelligence_class,
            **kw,
        )
    )


def scoped(handler, sid):
    handler._current_scope = {
        "kind": "session",
        "session_id": sid,
        "task_id": None,
        "project_id": PROJECT_ID,
        "elevated": False,
    }
    return handler


async def only_pool_session(db):
    """The single live pool session, asserting the pool really is size one.

    Tests that want one worker cap the profile at ``max_active=1`` via
    :func:`single_worker_pool` first; without that a two-task frontier
    legitimately launches two sessions.
    """
    rows = await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID)
    live = [r for r in rows if r.state != "stopped"]
    assert len(live) == 1, [(r.id, r.state) for r in rows]
    return live[0]


async def single_worker_pool(db):
    await db.update_profile("worker", max_active=1)


class TestFullPullLoop:
    """launch → claim → prime → close/claim-next → recycle → drain."""

    async def test_pull_loop_end_to_end(self, orch, db, handler):
        await ready(db, "t1")

        # -- launch ---------------------------------------------------------
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session = await only_pool_session(db)
        assert (session.lifecycle, session.state, session.desired_state) == (
            "pool",
            "running",
            "running",
        )
        assert session.agent_id and session.task_id is None
        slot = await db.get_workspace_for_agent(session.agent_id)
        assert slot is not None and slot.locked_by_task_id is None

        # -- claim ----------------------------------------------------------
        res = await scoped(handler, session.id)._cmd_task_claim({"next": True, "wait": 2})
        assert (res["result"], res["task"]["id"]) == ("claimed", "t1")
        epoch = res["claim_epoch"]
        held = await db.get_session(session.id)
        assert (held.task_id, held.claim_phase, held.claims) == ("t1", "active", 1)
        assert (await db.get_task("t1")).status == TaskStatus.IN_PROGRESS
        assert (await db.get_agent(session.agent_id)).state == AgentState.BUSY
        claim_file = Path(session.work_dir) / ".aq" / "claim.json"
        assert json.loads(claim_file.read_text())["claim_epoch"] == epoch
        assert (await db.get_workspace_for_agent(session.agent_id)).locked_by_task_id == "t1"

        # -- prime (pool variant) -------------------------------------------
        primed = await handler._cmd_prime({})
        assert primed["success"], primed
        protocol = next(s for s in primed["sections"] if s["key"] == "completion_protocol")
        assert "--claim-next" in protocol["body"]

        # -- close with --claim-next, no further work -----------------------
        closed = await handler._cmd_task_close(
            {
                "outcome": "pass",
                "work_outcome": "shipped",
                "summary": "done",
                "claim_next": True,
                # The real worker reads this back out of ``.aq/claim.json``;
                # a pool close without it is refused as ``stale_claim``.
                "claim_epoch": epoch,
            }
        )
        assert closed["success"], closed
        assert (await db.get_task("t1")).status == TaskStatus.COMPLETED
        # ``fresh_context_per_task`` (the default) recycles the worker rather
        # than letting it carry the finished task's conversation forward.
        assert orch.config.swarm.fresh_context_per_task is True
        after = await db.get_session(session.id)
        assert (after.task_id, after.claim_phase) == (None, None)
        assert after.desired_state == "stopped"
        assert closed["next"]["result"] == "drain_requested"
        assert not claim_file.exists()

        # -- drain ----------------------------------------------------------
        await orch.session_reconciler._step_drain_ack([after], time.time())
        drained = await db.get_session(session.id)
        assert (drained.state, drained.desired_state) == ("stopped", "stopped")
        # Confirmed-stop teardown returns the worker definition to the reuse
        # pool (agent-flock design: "pools ... may reuse idle definitions
        # after safe termination"); ``RETIRED`` is reserved for a teardown
        # whose process could not be confirmed stopped.
        agent = await db.get_agent(session.agent_id)
        assert (agent.state, agent.current_task_id) == (AgentState.IDLE, None)
        assert (await db.get_workspace_for_agent(session.agent_id)) is None

    async def test_recycled_pool_replaces_itself_for_the_next_task(self, orch, db, handler):
        """A drained worker is replaced, and the replacement claims the next task."""
        await single_worker_pool(db)
        await ready(db, "t1")
        await ready(db, "t2")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        first = await only_pool_session(db)

        res = await scoped(handler, first.id)._cmd_task_claim({"next": True})
        assert res["result"] == "claimed"
        claimed_first = res["task"]["id"]
        await handler._cmd_task_close(
            {"outcome": "pass", "summary": "s", "claim_epoch": res["claim_epoch"]}
        )
        await orch.session_reconciler._step_drain_ack([await db.get_session(first.id)], time.time())
        assert (await db.get_session(first.id)).state == "stopped"

        # Next tick: the stopped row is no longer supply, so the pool
        # re-launches and the fresh worker takes the remaining task.
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        second = await only_pool_session(db)
        assert second.id != first.id
        # The roster stays bounded: the replacement reuses the definition the
        # drained session released rather than minting a second one.
        assert second.agent_id == first.agent_id
        assert len(await db.list_agents()) == 1
        res2 = await scoped(handler, second.id)._cmd_task_claim({"next": True})
        assert res2["result"] == "claimed"
        assert res2["task"]["id"] != claimed_first

    async def test_prepare_failure_abandoned_loop_is_recycled_for_next_ready_task(
        self, orch, db, handler
    ):
        """A failed prepare cannot consume the sole idle worker indefinitely."""
        await single_worker_pool(db)
        await ready(db, "broken", priority=1)
        await ready(db, "next", priority=100)
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        first = await only_pool_session(db)
        reset = orch._worktree_slots.return_value.reset_slot_for_task
        reset.side_effect = [RuntimeError("repair checkout cannot be prepared"), "aq/next"]

        failed = await scoped(handler, first.id)._cmd_task_claim({"next": True})
        assert failed["result"] == "prepare_failed"
        assert await db.get_task_meta("broken", "needs_attention") == "slot_reset_failed"
        assert await db.get_task_meta("broken", "claim_prepare_backoff_until") > time.time()

        # No subsequent claim reached the daemon.  Simulate the stale pane
        # observation that distinguishes this from a worker in its normal
        # 60-second long poll.
        stale_at = time.time() - 1_000
        provider = orch.session_providers.create("fake", orch.config)
        provider.sessions[first.name].activity = stale_at
        await db.update_session(first.id, last_activity=stale_at)
        await orch.session_reconciler.tick(now=time.time())
        assert (await db.get_session(first.id)).state == "stopped"

        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        second = await only_pool_session(db)
        assert second.id != first.id
        claimed = await scoped(handler, second.id)._cmd_task_claim({"next": True})
        assert (claimed["result"], claimed["task"]["id"]) == ("claimed", "next")

    async def test_no_ready_work_then_scale_down_after_grace(self, orch, db, handler):
        # ``fresh_context_per_task`` caps the session at one claim and drains
        # it on close; turning it off is what leaves an *idle* session behind
        # for the sizing path (surplus grace) to scale down, which is what
        # this test is about.
        orch.config.swarm.fresh_context_per_task = False
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session = await only_pool_session(db)
        res = await scoped(handler, session.id)._cmd_task_claim({"next": True})
        assert res["result"] == "claimed"
        await handler._cmd_task_close(
            {"outcome": "pass", "summary": "s", "claim_epoch": res["claim_epoch"]}
        )
        assert (await db.get_session(session.id)).desired_state == "running"

        empty = await scoped(handler, session.id)._cmd_task_claim({"next": True})
        assert empty["result"] == "no_ready_work"

        orch.config.swarm.scale_down_grace = 3600
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert (await db.get_session(session.id)).desired_state == "running"

        orch.config.swarm.scale_down_grace = 0
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert (await db.get_session(session.id)).desired_state == "stopped"


class TestPublicPoolSleep:
    async def test_sleep_terminates_idle_pool_and_reconciliation_replaces_it(
        self, orch, db, handler
    ):
        """Public sleep retires the exact idle worker instance, not its successor."""
        await single_worker_pool(db)
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        first = await only_pool_session(db)
        provider = orch.session_providers.create(first.provider, orch.config)
        reconciler = SessionReconciler(
            db, orch.config, orch.session_providers, bus=orch.bus, orchestrator=orch
        )

        slept = await handler.execute("session_sleep", {"session_id": first.id})
        assert slept["success"] is True
        assert (await db.get_session(first.id)).desired_state == "sleeping"
        assert [h.name for h in await provider.list_running("")] == [first.name]

        # This is the production reconciliation path, rather than directly
        # calling the drain helper: it observes the live provider instance,
        # stops that exact instance, then releases the pool capacity.
        await reconciler.tick()
        stopped = await db.get_session(first.id)
        assert (stopped.state, stopped.desired_state, stopped.end_reason) == (
            "stopped",
            "stopped",
            "sleeping",
        )
        assert await provider.list_running("") == []
        assert (await db.get_agent(first.agent_id)).state is AgentState.IDLE
        assert await db.get_workspace_for_agent(first.agent_id) is None

        # Sleeping a pool instance does not leave a stale desired state that
        # can stop the worker launched to meet still-ready demand.
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        successor = await only_pool_session(db)
        assert successor.id != first.id
        assert successor.agent_id == first.agent_id
        assert successor.instance_token != first.instance_token
        assert (successor.state, successor.desired_state) == ("running", "running")
        assert [h.name for h in await provider.list_running("")] == [successor.name]

    async def test_sleep_defers_while_pool_holds_a_task(self, orch, db, handler):
        """Sleeping a busy pool worker never interrupts its active claim."""
        orch.config.swarm.fresh_context_per_task = False
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session = await only_pool_session(db)
        provider = orch.session_providers.create(session.provider, orch.config)
        reconciler = SessionReconciler(
            db, orch.config, orch.session_providers, bus=orch.bus, orchestrator=orch
        )
        claim = await scoped(handler, session.id)._cmd_task_claim({"next": True})
        assert claim["result"] == "claimed"

        slept = await handler.execute("session_sleep", {"session_id": session.id})
        assert slept["success"] is True
        await reconciler.tick()
        busy = await db.get_session(session.id)
        assert (busy.task_id, busy.state, busy.desired_state) == ("t1", "running", "sleeping")
        assert [h.name for h in await provider.list_running("")] == [session.name]

        # The normal completion path releases the claim; the next tick then
        # consumes the already-recorded sleep intent and performs teardown.
        closed = await handler._cmd_task_close(
            {"outcome": "pass", "summary": "done", "claim_epoch": claim["claim_epoch"]}
        )
        assert closed["success"], closed
        assert (await db.get_session(session.id)).task_id is None
        await reconciler.tick()
        assert (await db.get_session(session.id)).state == "stopped"
        assert await provider.list_running("") == []


class TestClaimAdmissibility:
    """A pool session may only claim work its live class/harness can run."""

    async def test_pool_session_cannot_claim_a_higher_class_task(self, orch, db, handler):
        await ready(db, "deep", intelligence_class="deep-high")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session = await only_pool_session(db)
        assert session.intelligence_class == "standard-medium"

        res = await scoped(handler, session.id)._cmd_task_claim({"next": True})
        assert res["result"] == "no_ready_work"
        assert (await db.get_task("deep")).status == TaskStatus.READY

    async def test_pool_session_claims_its_own_class(self, orch, db, handler):
        await single_worker_pool(db)
        await ready(db, "deep", intelligence_class="deep-high", priority=1)
        await ready(db, "std", intelligence_class="standard-medium", priority=50)
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session = await only_pool_session(db)
        res = await scoped(handler, session.id)._cmd_task_claim({"next": True})
        # ``deep`` sorts first by priority but is inadmissible for this worker.
        assert (res["result"], res["task"]["id"]) == ("claimed", "std")

    async def test_targeted_claim_of_an_inadmissible_task_is_refused(self, orch, db, handler):
        await ready(db, "deep", intelligence_class="deep-high")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        session = await only_pool_session(db)
        res = await scoped(handler, session.id)._cmd_task_claim({"task_id": "deep"})
        assert res["result"] != "claimed"
        assert (await db.get_task("deep")).status == TaskStatus.READY


def _pool_warnings(caplog):
    """Warnings from ``pools`` only — another module's noise is not the subject."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "src.orchestrator.pools" and r.levelname == "WARNING"
    ]


class TestQuarantine:
    """A startup death stops the pool and says why — once, not per tick."""

    async def test_startup_death_quarantines_with_a_readable_reason(
        self, orch, db, handler, tmp_path, caplog
    ):
        from src.sessions.provider import SessionDiedDuringStartup

        stderr = tmp_path / "start.log"
        stderr.write_text("Traceback ...\nclaude: error: unknown flag --nope\n")
        provider = orch.session_providers.create("fake", orch.config)

        async def _die(spec):
            raise SessionDiedDuringStartup(spec.session_name, str(stderr), "exit 1")

        provider.start = _die
        await ready(db, "t1")

        with caplog.at_level("WARNING", logger="src.orchestrator.pools"):
            await orch._reconcile_pools()
            await orch.wait_for_pool_launches()
        assert await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID) == []

        status = await handler._cmd_pool_status({"project_id": PROJECT_ID})
        row = next(r for r in status["pools"] if r["profile_id"] == "worker")
        # The pool is global; the quarantine belongs to the project whose
        # checkout the harness died in.
        entry = next(p for p in row["projects"] if p["project_id"] == PROJECT_ID)
        assert entry["quarantined_until"] > time.time()
        # The captured startup output is what makes the row actionable; a
        # bare timestamp left an operator with nowhere to look.
        assert "unknown flag --nope" in entry["quarantined_reason"]

        assert len(_pool_warnings(caplog)) == 1

        # Second tick: the key is still quarantined, so the launch is never
        # attempted and the excerpt is not re-read or re-logged. This is the
        # difference between one warning per backoff window and one per 5s
        # tick for as long as the harness stays broken.
        caplog.clear()
        with caplog.at_level("WARNING", logger="src.orchestrator.pools"):
            await orch._reconcile_pools()
            await orch.wait_for_pool_launches()
        assert _pool_warnings(caplog) == []
        assert await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID) == []

    async def test_expired_quarantine_relaunches(self, orch, db):
        orch._pool_quarantine[(PROJECT_ID, "worker")] = time.time() - 1
        orch._pool_quarantine_reason[(PROJECT_ID, "worker")] = "stale"
        await ready(db, "t1")
        await orch._reconcile_pools()
        await orch.wait_for_pool_launches()
        assert await only_pool_session(db)


class TestReconcilerInterplay:
    """Pool work never reaches the push path, and never disappears silently."""

    async def test_push_scheduler_and_agent_reconciler_both_exclude_pool_work(self, orch, db):
        await ready(db, "t1")
        task = await db.get_task("t1")
        profile = await orch._resolve_profile(task)

        assert await orch._pool_profile_ids(PROJECT_ID) == {"worker"}
        assert orch._is_session_routed(profile) is False

        report = await orch._agent_reconciler.reconcile(
            harness_registry=orch.harness_registry,
            intelligence_classes=orch.session_spec_builder._intelligence_classes,
        )
        # No push agent row is manufactured for a pool profile's demand.
        assert report.created == []
        assert await db.list_agents() == []

        # ...and the push scheduler never dispatches it either.
        await orch._schedule()
        assert (await db.get_task("t1")).status == TaskStatus.READY
        assert (await db.get_task("t1")).assigned_agent_id is None

    async def test_explain_says_awaiting_pool_session(self, orch, db, handler):
        await ready(db, "t1")
        res = await handler._cmd_explain_task({"task_id": "t1"})
        assert res["success"], res
        assert "awaiting_pool_session" in res["reason_codes"]
        # The push-path capacity codes would send an operator looking for an
        # idle worker that is never going to be created for this task.
        assert "no_idle_agent" not in res["reason_codes"]
        detail = next(r["detail"] for r in res["reasons"] if r["code"] == "awaiting_pool_session")
        assert "worker" in detail

    @pytest.mark.parametrize("status", [
        TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PAUSED,
    ])
    async def test_explain_does_not_report_worker_wait_outside_claim_queue(
        self, orch, db, handler, status,
    ):
        await db.create_task(Task(
            id="not-waiting", project_id=PROJECT_ID, title="not waiting", description="",
            status=status, profile_id="worker", intelligence_class="standard-medium",
        ))
        # Populate the scheduler's capacity snapshot as well as the pool path.
        await orch._schedule()
        result = await handler._cmd_explain_task({"task_id": "not-waiting"})
        assert "awaiting_pool_session" not in result["reason_codes"]
        assert "no_idle_agent" not in result["reason_codes"]
        assert "workspace_locked" not in result["reason_codes"]
        if status == TaskStatus.PAUSED:
            assert "paused_manually" in result["reason_codes"]

    async def test_explain_uses_fleet_occupancy_for_global_pool_cap(self, orch, db, handler):
        from src.models import SessionRecord

        await ready(db, "waiting")
        await db.create_project(Project(id="other-project", name="Other"))
        for project_id, task_id in ((PROJECT_ID, "busy-local"), ("other-project", "busy-other")):
            await db.create_task(Task(
                id=task_id, project_id=project_id, title=task_id, description="busy worker",
                profile_id="worker", status=TaskStatus.IN_PROGRESS,
            ))
            await db.create_session(SessionRecord(
                id=task_id, project_id=project_id, profile_id="worker",
                harness="claude", provider="fake", name=task_id, lifecycle="pool",
                work_dir="/tmp/unused-pool-explain", epoch="test", instance_token=task_id,
                started_at=time.time(), task_id=task_id, state="running",
            ))

        result = await handler._cmd_explain_task({"task_id": "waiting"})
        detail = next(r["detail"] for r in result["reasons"]
                      if r["code"] == "awaiting_pool_session")
        assert "project: 1 busy, 0 idle, 0 starting" in detail
        assert "fleet: 2 busy, 0 idle, 0 starting" in detail
        assert "max_active=2" in detail
        assert "at max_active with no idle worker" in detail

    async def test_explain_names_the_quarantine_as_the_blocker(self, orch, db, handler):
        await ready(db, "t1")
        orch._quarantine_pool(PROJECT_ID, "worker", "unknown harness 'nope'")
        res = await handler._cmd_explain_task({"task_id": "t1"})
        detail = next(r["detail"] for r in res["reasons"] if r["code"] == "awaiting_pool_session")
        assert "quarantined" in detail and "unknown harness" in detail

    async def test_explain_names_the_swarm_flag_when_it_is_off(self, orch, db, handler):
        orch.config.swarm.enabled = False
        await ready(db, "t1")
        res = await handler._cmd_explain_task({"task_id": "t1"})
        detail = next(r["detail"] for r in res["reasons"] if r["code"] == "awaiting_pool_session")
        assert "swarm.enabled is false" in detail

    async def test_explain_names_a_disabled_profile_instead_of_a_capacity_wait(
        self, orch, db, handler,
    ):
        await db.update_profile("worker", enabled=False)
        await ready(db, "t1")

        res = await handler._cmd_explain_task({"task_id": "t1"})
        assert "pool_disabled" in res["reason_codes"]
        assert "awaiting_pool_session" not in res["reason_codes"]
        detail = next(r["detail"] for r in res["reasons"] if r["code"] == "pool_disabled")
        assert "aq pool set-enabled --profile-id worker --enabled" in detail

    async def test_explain_names_a_disabled_route_for_an_integration_repair_delegate(
        self, orch, db, handler,
    ):
        """Repair delegates retain their frozen route and use the same diagnosis."""
        await db.update_profile("worker", enabled=False)
        await ready(
            db,
            "repair-operation-0",
            created_by_kind="integration_repair",
            created_by_id="operation",
        )

        res = await handler._cmd_explain_task({"task_id": "repair-operation-0"})
        assert "pool_disabled" in res["reason_codes"]
        detail = next(r["detail"] for r in res["reasons"] if r["code"] == "pool_disabled")
        assert "disabled pool profile 'worker'" in detail

    async def test_explain_keeps_an_enabled_zero_cap_pool_as_a_capacity_wait(
        self, orch, db, handler,
    ):
        await db.update_profile("worker", enabled=True, max_active=0)
        await ready(db, "t1")

        res = await handler._cmd_explain_task({"task_id": "t1"})
        assert "pool_disabled" not in res["reason_codes"]
        detail = next(r["detail"] for r in res["reasons"] if r["code"] == "awaiting_pool_session")
        assert "max_active=0" in detail and "at max_active" in detail

    async def test_pool_task_keeps_the_capacity_reasons_that_still_bite(self, orch, db, handler):
        """Only the *push-supply* codes are filtered, not every capacity code.

        A paused project fails ``_admission_reason`` on the claim itself, so
        it is the real answer for a pool task too — dropping the whole
        capacity block would have hidden it behind "awaiting a pool session".
        """
        from src.models import ProjectStatus

        await ready(db, "t1")
        await db.update_project(PROJECT_ID, status=ProjectStatus.PAUSED)
        # One tick to populate the cached snapshot ``explain`` reads.
        await orch._schedule()

        res = await handler._cmd_explain_task({"task_id": "t1"})
        assert "awaiting_pool_session" in res["reason_codes"]
        assert "project_paused" in res["reason_codes"]
        assert "no_idle_agent" not in res["reason_codes"]

    async def test_push_routed_task_still_gets_capacity_reasons(self, orch, db, handler):
        """The pool branch must not swallow the ordinary push-path answer."""
        await db.create_profile(AgentProfile(id="pusher", name="p", harness="claude"))
        await ready(db, "t1", profile_id="pusher")
        res = await handler._cmd_explain_task({"task_id": "t1"})
        assert "awaiting_pool_session" not in res["reason_codes"]


class TestPoolStatusRendering:
    """``aq pool status`` has to show the reason, not just the deadline."""

    def _render(self, row):
        from io import StringIO

        from rich.console import Console

        from src.cli.formatters import format_pool_table

        buf = StringIO()
        Console(file=buf, width=200).print(format_pool_table([row]))
        return buf.getvalue()

    def _row(self, **project):
        """One pool row, with ``project`` folded into its single project entry."""
        entry = {"project_id": PROJECT_ID, "ready": 1, "workspace_capacity": 1}
        entry.update(project)
        return {
            "profile_id": "worker",
            "min_active": 0,
            "max_active": 2,
            "min_per_project": 0,
            "desired": 0,
            "running_idle": 0,
            "running_busy": 0,
            "starting": 0,
            "draining": 0,
            "ready": 1,
            "projects": [entry],
        }

    def test_quarantine_reason_is_rendered(self):
        out = self._render(
            self._row(
                quarantined_until=time.time() + 60,
                quarantined_reason="unknown harness 'claud'",
            )
        )
        assert "unknown harness" in out
        # And the project it applies to, now that the pool spans projects.
        assert PROJECT_ID in out

    def test_healthy_pool_renders_placement_and_no_quarantine_note(self):
        out = self._render(self._row())
        assert f"{PROJECT_ID}:0" in out
        assert "quarantined" not in out
