"""Deletion hides idle workers without deleting any execution history."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import update

from src.database import Database
from src.database.tables import agents as agents_table
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
from src.orchestrator.agent_reconciler import AgentReconciler


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "delete.db"))
    await db.initialize()
    await db.create_profile(AgentProfile(id="worker", name="Worker", harness="claude"))
    await db.create_project(Project(id="p", name="P", default_profile_id="worker"))
    await db.create_agent(Agent(id="a", name="Keep history", profile_id="worker", model="saved"))
    yield db
    await db.close()


def record(*, state="stopped", lifecycle="task"):
    return SessionRecord(
        id="s",
        agent_id="a",
        task_id="t",
        project_id="p",
        profile_id="worker",
        harness="claude",
        provider="fake",
        name="s-t",
        lifecycle=lifecycle,
        state=state,
        work_dir="/work",
        epoch="e",
        instance_token="token",
        started_at=1,
    )


async def test_idle_deletion_hides_worker_preserving_history_and_repeat_is_safe(db):
    await db.create_task(
        Task(
            id="t",
            project_id="p",
            title="Done",
            description="history",
            status=TaskStatus.COMPLETED,
            assigned_agent_id="a",
        )
    )
    await db.create_session(record())
    before_task = await db.get_task("t")
    before_session = await db.get_session("s")
    assert await db.soft_delete_agent("a") is True
    deleted = await db.get_agent("a")
    assert deleted.deleted_at is not None and not deleted.enabled
    assert deleted.model == "saved" and deleted.name == "Keep history"
    assert await db.list_agents() == []
    assert [a.id for a in await db.list_agents(include_deleted=True)] == ["a"]
    assert await db.get_task("t") == before_task
    assert await db.get_session("s") == before_session
    assert await db.soft_delete_agent("a") is False
    assert (await db.get_agent("a")).deleted_at == deleted.deleted_at
    assert await db.soft_delete_agent("missing") is False


@pytest.mark.parametrize(
    "kind",
    [
        "busy",
        "current_task",
        "assigned",
        "waiting_input",
        "workspace",
        "supervisor",
        "reserved_supervisor",
    ],
)
async def test_deletion_refuses_workers_that_are_protected_or_own_work(db, kind):
    aid = "a"
    if kind == "busy":
        await db.update_agent(aid, state=AgentState.BUSY)
    elif kind in ("current_task", "assigned", "waiting_input"):
        status = (
            TaskStatus.COMPLETED
            if kind == "current_task"
            else (TaskStatus.WAITING_INPUT if kind == "waiting_input" else TaskStatus.ASSIGNED)
        )
        await db.create_task(
            Task(
                id="t",
                project_id="p",
                title="Owned",
                description="work",
                status=status,
                assigned_agent_id=aid,
            )
        )
        if kind == "current_task":
            await db.update_agent(aid, current_task_id="t")
    elif kind == "workspace":
        await db.create_workspace(
            Workspace(
                id="w",
                project_id="p",
                workspace_path="/work",
                source_type=RepoSourceType.LINK,
                locked_by_agent_id=aid,
            )
        )
    elif kind == "supervisor":
        await db.update_agent(aid, role="supervisor")
    else:
        aid = "supervisor-global"
        await db.create_agent(Agent(id=aid, name="Supervisor", profile_id="worker"))
    before = await db.get_agent(aid)
    assert await db.soft_delete_agent(aid) is False
    assert await db.get_agent(aid) == before


@pytest.mark.parametrize(
    "lifecycle,state",
    [("task", "starting"), ("task", "running"), ("task", "draining"), ("pool", "running")],
)
async def test_deletion_refuses_live_sessions_even_with_idle_agent(db, lifecycle, state):
    await db.create_task(
        Task(
            id="t", project_id="p", title="History", description="work", status=TaskStatus.COMPLETED
        )
    )
    await db.create_session(record(state=state, lifecycle=lifecycle))
    assert await db.soft_delete_agent("a") is False
    assert (await db.get_session("s")).state == state


async def test_tombstone_cannot_be_resurrected_or_claim_new_work(db):
    assert await db.soft_delete_agent("a")
    await db.update_agent("a", enabled=True, state=AgentState.IDLE, model="late-write")
    deleted = await db.get_agent("a")
    assert not deleted.enabled and deleted.model == "saved"
    # Even an old importer accidentally restoring enabled cannot bypass the tombstone.
    async with db._engine.begin() as conn:
        await conn.execute(
            update(agents_table).where(agents_table.c.id == "a").values(enabled=True)
        )
    await db.create_task(
        Task(id="ready", project_id="p", title="Next", description="work", status=TaskStatus.READY)
    )
    assert await db.reserve_idle_agent("a") is False
    assert await db.assign_task_to_agent("ready", "a") is False
    async with db.immediate() as conn:
        assert await db.take_task(conn, "ready", agent_id="a", now=time.time()) is None
    assert (await db.get_task("ready")).status == TaskStatus.READY
    await db.create_workspace(
        Workspace(id="w", project_id="p", workspace_path="/work", source_type=RepoSourceType.LINK)
    )
    await AgentReconciler(db).reconcile()
    assert all(a.id != "a" for a in await db.list_agents())
    assert (await db.get_agent("a")).deleted_at == deleted.deleted_at


async def test_delete_and_reserve_have_exactly_one_winner(db):
    deleted, reserved = await asyncio.gather(db.soft_delete_agent("a"), db.reserve_idle_agent("a"))
    assert deleted != reserved
    agent = await db.get_agent("a")
    assert (agent.deleted_at is not None) == deleted
    assert (agent.state == AgentState.BUSY) == reserved
