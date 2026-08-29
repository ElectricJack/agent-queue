"""ClaimQueryMixin — spec §10 claim transaction, §11.2 ownership lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from src.database import Database
from src.database.queries.task_queries import StaleClaim
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

PROJECT_ID = "proj"
NOW = 1_000_000.0


@pytest.fixture
async def db(tmp_path):
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


class TestClaimTransaction:
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
        assert await db.activate_claim(sid, "t1", epoch=1, now=NOW) is True
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
        assert await db.activate_claim(sid, "t1", epoch=1, now=NOW) is False
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
