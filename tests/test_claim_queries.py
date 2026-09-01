"""ClaimQueryMixin — spec §10 claim transaction, §11.2 ownership lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from src.database import Database
from src.database.queries.task_queries import StaleClaim
from src.assignment_routing import assignment_input_hash
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    PlaybookRun,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskAssignmentRoute,
    TaskStatus,
    Workspace,
)
from tests.pg_dsn import ensure_worker_postgres_dsn

PROJECT_ID = "proj"
NOW = 1_000_000.0

#: Ruling P2-7: parametrise over the same ``any_db`` shape as the perf
#: tests (``tests/perf/conftest.py``) -- SQLite always, Postgres when
#: ``POSTGRES_TEST_DSN`` is set -- so ``test_exactly_once_under_concurrency``
#: and ``test_reserve_filing_is_atomic`` prove the CAS under a genuine race
#: on Postgres (SQLite's ``immediate()`` per-adapter lock serialises them
#: instead, so it only proves the *result* is correct, not that the CAS
#: itself is what enforced it). Per-xdist-worker DSN (tests/pg_dsn.py) --
#: this suite's own database, separate from tests/perf and
#: tests/test_database_postgresql.py's, so concurrent truncates don't race.
POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "test.db"))
        await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(AgentProfile(id="worker", name="Worker"))
    await database.create_profile(AgentProfile(id="reviewer", name="Reviewer"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.READY, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )


async def save_route(
    db, task_id, *, intelligence_class="fast-low", provider=None, options_hash="catalog-1"
):
    task = await db.get_task(task_id)
    run_id = f"route-{task_id}"
    await db.create_playbook_run(
        PlaybookRun(
            run_id=run_id,
            playbook_id="default-assignment-routing",
            playbook_version=1,
            started_at=NOW,
        )
    )
    route = TaskAssignmentRoute(
        task_id=task.id,
        project_id=task.project_id,
        input_hash=assignment_input_hash(task),
        task_updated_at=task.updated_at,
        options_hash=options_hash,
        intelligence_class=intelligence_class,
        provider=provider,
        playbook_id="default-assignment-routing",
        playbook_version=1,
        playbook_run_id=run_id,
        reason="test route",
        decided_at=NOW,
    )
    async with db.immediate() as conn:
        await db.upsert_task_assignment_routes([route], conn=conn)


async def pool_session(db, sid="s1", agent_id="agent-1", **over):
    await db.create_agent(
        Agent(id=agent_id, name=agent_id, profile_id="worker", state=AgentState.IDLE)
    )
    await db.create_workspace(
        Workspace(
            id=f"ws-{agent_id}",
            project_id=PROJECT_ID,
            workspace_path=f"/wd/{agent_id}",
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
            locked_by_agent_id=agent_id,
        )
    )
    base = dict(
        id=sid,
        project_id=PROJECT_ID,
        profile_id="worker",
        harness="claude",
        provider="fake",
        name=f"p-worker--proj--{sid}",
        lifecycle="pool",
        work_dir=f"/wd/{agent_id}",
        epoch="e",
        instance_token="t",
        started_at=NOW,
        state="running",
        agent_id=agent_id,
    )
    base.update(over)
    await db.create_session(SessionRecord(**base))
    return sid


async def claim_once(db, sid, *, cap=None, task_id=None):
    async with db.immediate() as conn:
        kind, row = await db.take_claim_slot(conn, sid, now=NOW, cap=cap)
        if kind != "slot":
            return kind, None
        tid = await db.select_ready_for_profile(
            conn,
            project_id=PROJECT_ID,
            profile_id="worker",
            default_profile_id=None,
            agent_id=row.agent_id,
            task_id=task_id,
        )
        if tid is None:
            await db.release_claim_slot(conn, sid)
            return "no_ready_work", None
        task = await db.take_task(conn, tid, agent_id=row.agent_id, now=NOW)
        if task is None:
            await db.release_claim_slot(conn, sid)
            return "claim_conflict", None
        await db.record_holder(
            conn, session_id=sid, task_id=tid, agent_id=row.agent_id, work_dir=row.work_dir, now=NOW
        )
        return "claimed", task


async def test_stale_epoch_cannot_activate_a_reclaimed_task(db):
    """A holder from a superseded claim cannot activate against the new epoch.

    Claim (epoch 1) -> release -> re-claim (epoch 2).  ``activate_claim``
    carrying the stale epoch must lose; the current epoch must win.  Runs on
    both backends; on Postgres the epoch check is a genuine second-connection
    row read rather than SQLite's serialized writer.
    """
    await mktask(db, "epoch", profile_id="worker")
    sid = await pool_session(db)
    kind, task = await claim_once(db, sid)
    assert kind == "claimed" and task.claim_epoch == 1
    await db.release_claim(sid, task_status=TaskStatus.READY, context="crash", now=NOW)
    kind, task = await claim_once(db, sid)
    assert kind == "claimed" and task.claim_epoch == 2
    assert await db.activate_claim(sid, "epoch", epoch=1, now=NOW) is None
    session = await db.get_session(sid)
    assert session.claim_phase == "preparing"  # the stale writer changed nothing
    activated = await db.activate_claim(sid, "epoch", epoch=2, now=NOW)
    assert activated is not None and activated.claim_phase == "active"


async def test_two_sessions_race_one_task_and_leave_one_complete_holder_graph(db):
    """Exactly one racer claims, and its holder graph is complete.

    The winner must hold every row the claim transaction stamps (task,
    session, agent, workspace, metadata); the loser must be left fully
    clean, not half-claimed.
    """
    await mktask(db, "race", profile_id="worker")
    first = await pool_session(db, sid="race-1", agent_id="race-agent-1")
    second = await pool_session(db, sid="race-2", agent_id="race-agent-2")
    results = await asyncio.gather(claim_once(db, first), claim_once(db, second))
    kinds = [kind for kind, _ in results]
    assert kinds.count("claimed") == 1
    assert all(kind in ("claimed", "no_ready_work", "claim_conflict") for kind in kinds)
    winner_sid = (first, second)[kinds.index("claimed")]
    loser_sid = second if winner_sid == first else first
    winner_agent = f"race-agent-{winner_sid[-1]}"
    loser_agent = f"race-agent-{loser_sid[-1]}"

    task = await db.get_task("race")
    assert (task.status, task.assigned_agent_id, task.claim_epoch) == (
        TaskStatus.IN_PROGRESS,
        winner_agent,
        1,
    )
    winner = await db.get_session(winner_sid)
    assert (winner.task_id, winner.claim_phase) == ("race", "preparing")
    agent = await db.get_agent(winner_agent)
    assert (agent.state, agent.current_task_id) == (AgentState.BUSY, "race")
    assert (await db.get_workspace_for_agent(winner_agent)).locked_by_task_id == "race"
    assert await db.get_task_meta("race", "claimed_by_session") == winner_sid

    loser = await db.get_session(loser_sid)
    assert (loser.task_id, loser.claim_phase) == (None, None)
    assert (await db.get_agent(loser_agent)).state == AgentState.IDLE
    assert (await db.get_workspace_for_agent(loser_agent)).locked_by_task_id is None


async def test_take_task_rejects_soft_deleted_or_nonworker_agent_on_both_backends(db):
    await mktask(db, "eligible", profile_id="worker")
    await db.create_agent(
        Agent(id="reviewer-agent", name="r", profile_id="reviewer", role="supervisor")
    )
    await db.create_agent(
        Agent(id="deleted-agent", name="d", profile_id="worker", deleted_at=NOW)
    )
    async with db.immediate() as conn:
        assert await db.take_task(conn, "eligible", agent_id="reviewer-agent", now=NOW) is None
        assert await db.take_task(conn, "eligible", agent_id="deleted-agent", now=NOW) is None
    task = await db.get_task("eligible")
    assert (task.status, task.assigned_agent_id) == (TaskStatus.READY, None)


async def test_record_holder_stamps_and_release_clears_every_agent_workspace_slot(db):
    """DB-4: an agent holding several workspace slots gets them all stamped.

    ``record_holder``'s workspace UPDATE matches on ``locked_by_agent_id``,
    so a multi-kind agent (e.g. project-repo + readonly-dir) must see
    ``locked_by_task_id`` land on every slot it holds — and a release must
    clear every slot while keeping the agent lock itself.
    """
    await mktask(db, "multi", profile_id="worker")
    sid = await pool_session(db)  # locks ws-agent-1 (project-repo) for agent-1
    await db.create_workspace(
        Workspace(
            id="ws-agent-1-extra",
            project_id=PROJECT_ID,
            workspace_path="/wd/agent-1-extra",
            source_type=RepoSourceType.LINK,
            kind_id="readonly-dir",
            locked_by_agent_id="agent-1",
        )
    )
    kind, task = await claim_once(db, sid)
    assert kind == "claimed" and task.id == "multi"
    for ws_id in ("ws-agent-1", "ws-agent-1-extra"):
        ws = await db.get_workspace(ws_id)
        assert (ws.locked_by_task_id, ws.locked_by_agent_id) == ("multi", "agent-1")

    await db.release_claim(sid, task_status=TaskStatus.READY, context="done", now=NOW)
    for ws_id in ("ws-agent-1", "ws-agent-1-extra"):
        ws = await db.get_workspace(ws_id)
        assert (ws.locked_by_task_id, ws.locked_by_agent_id) == (None, "agent-1")


class TestClaimTransaction:
    async def test_work_query_uses_fresh_route_class_and_provider_before_limit(self, db):
        await mktask(db, "untriaged", profile_id="worker", priority=1)
        await mktask(db, "wrong-class", profile_id="worker", priority=2)
        await save_route(db, "wrong-class", intelligence_class="deep-high")
        await mktask(db, "wrong-provider", profile_id="worker", priority=3)
        await save_route(db, "wrong-provider", provider="openai")
        await mktask(db, "compatible", profile_id="worker", priority=4)
        await save_route(db, "compatible", provider="anthropic")

        async with db.immediate() as conn:
            selected = await db.select_ready_for_profile(
                conn,
                project_id=PROJECT_ID,
                profile_id="worker",
                default_profile_id=None,
                agent_id="agent-1",
                enforce_routing=True,
                intelligence_class="fast-low",
                llm_provider="anthropic",
                options_hash="catalog-1",
            )

        assert selected == "compatible"

    async def test_work_query_rejects_stale_route_but_accepts_explicit_class(self, db):
        await mktask(db, "stale", profile_id="worker", priority=1)
        await save_route(db, "stale")
        await db.update_task("stale", title="changed after routing")
        await mktask(
            db,
            "explicit",
            profile_id="worker",
            priority=2,
            intelligence_class="fast-low",
        )

        async with db.immediate() as conn:
            selected = await db.select_ready_for_profile(
                conn,
                project_id=PROJECT_ID,
                profile_id="worker",
                default_profile_id=None,
                agent_id="agent-1",
                enforce_routing=True,
                intelligence_class="fast-low",
                llm_provider="anthropic",
                options_hash="catalog-1",
            )

        assert selected == "explicit"

    async def test_claim_records_holder_everywhere(self, db):
        await mktask(db, "t1", profile_id="worker")
        sid = await pool_session(db)
        kind, task = await claim_once(db, sid)
        assert kind == "claimed" and task.id == "t1"
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id, t.claim_epoch) == (
            TaskStatus.IN_PROGRESS,
            "agent-1",
            1,
        )
        s = await db.get_session(sid)
        assert (s.task_id, s.claim_phase, s.claims) == ("t1", "preparing", 0)
        a = await db.get_agent("agent-1")
        assert (a.state, a.current_task_id) == (AgentState.BUSY, "t1")
        assert (await db.get_workspace_for_agent("agent-1")).locked_by_task_id == "t1"
        assert await db.get_task_meta("t1", "claimed_by_session") == sid

    async def test_second_slot_take_classifies_phase(self, db):
        await mktask(db, "t1", profile_id="worker")
        sid = await pool_session(db)
        await claim_once(db, sid)
        async with db.immediate() as conn:
            kind, _ = await db.take_claim_slot(conn, sid, now=NOW, cap=None)
        assert kind == "preparing"
        # activate_claim returns the row it wrote (spec §15: the caller
        # builds its session block from it instead of re-reading).
        activated = await db.activate_claim(sid, "t1", epoch=1, now=NOW)
        assert activated is not None
        assert (activated.claim_phase, activated.claims) == ("active", 1)
        async with db.immediate() as conn:
            kind, _ = await db.take_claim_slot(conn, sid, now=NOW, cap=None)
        assert kind == "active"
        assert (await db.get_session(sid)).claims == 1

    async def test_cap_and_drain_classification(self, db):
        sid = await pool_session(db, claims=1)
        async with db.immediate() as conn:
            kind, _ = await db.take_claim_slot(conn, sid, now=NOW, cap=1)
        assert kind == "session_exhausted"
        await db.update_session(sid, desired_state="stopped")
        async with db.immediate() as conn:
            kind, _ = await db.take_claim_slot(conn, sid, now=NOW, cap=None)
        assert kind == "drain_requested"

    async def test_drain_takes_precedence_over_cap_exhaustion(self, db):
        sid = await pool_session(db, claims=1, desired_state="stopped")
        async with db.immediate() as conn:
            kind, _ = await db.take_claim_slot(conn, sid, now=NOW, cap=1)
        assert kind == "drain_requested"

    async def test_work_query_excludes_other_profiles_holds_and_plan_subtasks(self, db):
        await mktask(db, "other", profile_id="reviewer")
        await mktask(db, "held", profile_id="worker")
        await db.add_task_label("held", "hold:triage")
        await mktask(db, "plan", profile_id="worker", is_plan_subtask=True)
        await mktask(db, "mine", profile_id="worker", priority=50)
        sid = await pool_session(db)
        kind, task = await claim_once(db, sid)
        assert kind == "claimed" and task.id == "mine"
        sid2 = await pool_session(db, sid="s2", agent_id="agent-2")
        assert (await claim_once(db, sid2))[0] == "no_ready_work"

    async def test_default_profile_takes_unrouted_tasks(self, db):
        await mktask(db, "unrouted")  # profile_id None
        sid = await pool_session(db)
        async with db.immediate() as conn:
            kind, row = await db.take_claim_slot(conn, sid, now=NOW, cap=None)
            assert (
                await db.select_ready_for_profile(
                    conn,
                    project_id=PROJECT_ID,
                    profile_id="worker",
                    default_profile_id="worker",
                    agent_id=row.agent_id,
                )
                == "unrouted"
            )
            assert (
                await db.select_ready_for_profile(
                    conn,
                    project_id=PROJECT_ID,
                    profile_id="worker",
                    default_profile_id="other",
                    agent_id=row.agent_id,
                )
                is None
            )

    async def test_exactly_once_under_concurrency(self, db):
        for i in range(10):
            await mktask(db, f"t{i}", profile_id="worker")
        sids = [await pool_session(db, sid=f"s{i}", agent_id=f"agent-{i}") for i in range(20)]
        results = await asyncio.gather(*(claim_once(db, s) for s in sids))
        kinds = [k for k, _ in results]
        assert kinds.count("claimed") == 10 and kinds.count("no_ready_work") == 10
        assert sorted(t.id for k, t in results if k == "claimed") == sorted(
            f"t{i}" for i in range(10)
        )

    async def test_activate_loses_to_release(self, db):
        await mktask(db, "t1", profile_id="worker")
        sid = await pool_session(db)
        await claim_once(db, sid)
        await db.release_claim(
            sid,
            task_status=TaskStatus.READY,
            context="slot_reset_failed",
            now=NOW,
            result="prepare_failed",
            needs_attention="slot_reset_failed",
        )
        assert await db.activate_claim(sid, "t1", epoch=1, now=NOW) is None
        s = await db.get_session(sid)
        assert (s.task_id, s.claim_phase, s.last_claim_epoch, s.last_claim_result) == (
            None,
            None,
            1,
            "prepare_failed",
        )
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id, t.claim_epoch) == (TaskStatus.READY, None, 1)
        assert await db.get_task_meta("t1", "needs_attention") == "slot_reset_failed"
        ws = await db.get_workspace_for_agent("agent-1")
        assert (ws.locked_by_task_id, ws.locked_by_agent_id) == (None, "agent-1")
        assert (await db.get_agent("agent-1")).state == AgentState.IDLE

    async def test_epoch_fence_rejects_stale_writer(self, db):
        await mktask(db, "t1", profile_id="worker")
        sid = await pool_session(db)
        await claim_once(db, sid)  # epoch 1
        await db.release_claim(sid, task_status=TaskStatus.READY, context="x", now=NOW)
        await claim_once(db, sid)  # epoch 2
        async with db.immediate() as conn:
            with pytest.raises(StaleClaim):
                await db._apply_transition(
                    conn,
                    "t1",
                    TaskStatus.COMPLETED,
                    context="close",
                    force=True,
                    expect_claim_epoch=1,
                )
        assert (await db.get_task("t1")).status == TaskStatus.IN_PROGRESS

    async def test_terminate_releases_everything(self, db):
        await mktask(db, "t1", profile_id="worker")
        sid = await pool_session(db)
        await claim_once(db, sid)
        await db.terminate_pool_session(sid, reason="stopped")
        assert await db.get_workspace_for_agent("agent-1") is None
        assert (await db.get_agent("agent-1")).state == AgentState.RETIRED
        s = await db.get_session(sid)
        assert (s.task_id, s.claim_phase) == (None, None)
        assert (await db.get_task("t1")).status == TaskStatus.READY

    async def test_reserve_filing_is_atomic(self, db):
        await mktask(db, "t1", status=TaskStatus.IN_PROGRESS)

        async def one():
            async with db.immediate() as conn:
                return await db.reserve_filing(conn, "t1", max_filings=20)

        got = await asyncio.gather(*(one() for _ in range(25)))
        assert got.count(True) == 20 and got.count(False) == 5
        assert (await db.get_task("t1")).filed_count == 20

    async def test_count_ready_by_profile(self, db):
        await mktask(db, "a", profile_id="worker")
        await mktask(db, "b", profile_id="worker")
        await mktask(db, "c")
        await mktask(db, "d", status=TaskStatus.DEFINED, profile_id="worker")
        assert await db.count_ready_by_profile(PROJECT_ID) == {"worker": 2, None: 1}


class TestPushAssignmentRoutingGate:
    async def test_push_assignment_cannot_cross_existing_routing_gate(self, db):
        await db.create_agent(Agent(id="worker-1", name="Worker", profile_id="worker"))
        await mktask(db, "unrouted")
        await db.create_gate(PROJECT_ID, "routing", "Choose worker", waiter_task_ids=["unrouted"])
        assert (await db.get_task("unrouted")).is_blocked
        assert await db.assign_task_to_agent("unrouted", "worker-1") is False
        task = await db.get_task("unrouted")
        agent = await db.get_agent("worker-1")
        assert task.status == TaskStatus.READY and task.assigned_agent_id is None
        assert agent.state == AgentState.IDLE and agent.current_task_id is None
