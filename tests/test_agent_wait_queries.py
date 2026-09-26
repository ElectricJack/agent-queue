"""Durable wait and result-message boundaries on disposable PostgreSQL."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, insert, select, update

from src.agent_waits import WaitError
from src.database import Database
from src.database.tables import archived_tasks, messages, sessions, tasks
from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskCompletion, TaskStatus
from tests.db_fixtures import lease_dsn

NOW = 1_800_000_000.0


@pytest.fixture
async def env():
    db = Database(lease_dsn("agent-waits"))
    await db.initialize()
    await db.create_project(Project(id="p", name="P"))
    await db.create_project(Project(id="q", name="Q"))
    await db.create_agent(Agent(id="a", name="A", profile_id="worker-codex", state=AgentState.BUSY))
    await db.create_task(
        Task(
            id="owner",
            project_id="p",
            title="Owner",
            description="",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="a",
            claim_epoch=1,
        )
    )
    await db.update_agent("a", current_task_id="owner")
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=1))
    await db.create_task(Task(id="source", project_id="p", title="Source", description=""))
    await db.create_task(Task(id="foreign", project_id="q", title="Foreign", description=""))
    session = SessionRecord(
        id="s",
        project_id="p",
        profile_id="worker-codex",
        harness="codex",
        provider="fake",
        name="p-wait",
        lifecycle="pool",
        work_dir="/tmp/wait",
        epoch="boot",
        instance_token="token",
        started_at=NOW,
        task_id="owner",
        state="running",
        agent_id="a",
        claim_phase="active",
        last_claim_epoch=1,
    )
    await db.create_session(session)
    identity = dict(
        session_id="s", instance_token="token", project_id="p", claim_epoch=1, elevated=False
    )
    yield SimpleNamespace(db=db, session=session, identity=identity)
    await db.close()


async def register(env, *, kind="task", match=None, key="key", now=NOW, timeout=100):
    return await env.db.register_agent_wait(
        identity=env.identity,
        kind=kind,
        match=match or {"task_id": "source"},
        deadline_at=now + timeout,
        idempotency_key=key,
        now=now,
    )


async def complete(db, at=NOW + 50, status="COMPLETED", outcome="pass"):
    async with db._engine.begin() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "source").values(status=status, updated_at=at)
        )
    await db.save_task_completion(
        TaskCompletion(
            id=f"close-{at}",
            task_id="source",
            outcome=outcome,
            completed_at=at,
            summary="actual outcome",
        )
    )


async def result_count(db, wait_id):
    async with db._engine.connect() as conn:
        return await conn.scalar(
            select(func.count())
            .select_from(messages)
            .where(messages.c.id == f"wait:{wait_id}:result")
        )


async def test_completion_before_registration_preserves_failure(env):
    await complete(env.db, at=NOW - 1, status="FAILED", outcome="fail")
    row = await register(env)
    assert row["state"] == "satisfied"
    assert row["digest"] == {"task_id": "source", "status": "FAILED", "outcome": "fail"}
    assert row["version"] == 2
    assert await result_count(env.db, row["id"]) == 1
    result = await env.db.get_message(row["result_message_id"])
    assert (
        result.body_kind == "wait_result" and result.to_kind == "task" and result.to_id == "owner"
    )


async def test_duplicate_keys_and_concurrent_registration(env):
    first, second = await asyncio.gather(register(env), register(env))
    assert first["id"] == second["id"]
    with pytest.raises(WaitError) as exc:
        await register(env, match={"task_id": "owner"})
    assert exc.value.code == "wait.idempotency_conflict"
    with pytest.raises(WaitError) as exc:
        await register(env, key="other")
    assert exc.value.code == "wait.already_active"


async def test_completion_concurrent_with_registration_and_restart(env):
    row, _ = await asyncio.gather(register(env), complete(env.db))
    # Reconstruct the adapter to prove no observer/in-memory subscription is needed.
    fresh = Database(env.db._dsn)
    fresh._engine = env.db._engine
    await fresh.reconcile_agent_waits(now=NOW + 60)
    await fresh.reconcile_agent_waits(now=NOW + 60)
    assert (await fresh.get_agent_wait(row["id"]))["state"] == "satisfied"
    assert await result_count(fresh, row["id"]) == 1


@pytest.mark.parametrize("delta,state", [(99, "satisfied"), (100, "satisfied"), (101, "expired")])
async def test_late_scan_uses_producer_completion_time(env, delta, state):
    row = await register(env)
    await complete(env.db, at=NOW + delta)
    await env.db.reconcile_agent_waits(now=NOW + 200)
    result = await env.db.get_agent_wait(row["id"])
    assert result["state"] == state
    assert result["wait_resumed_at"] == NOW + 200


async def test_archive_and_missing_producer(env):
    row = await register(env)
    await complete(env.db, at=NOW + 1)
    async with env.db._engine.begin() as conn:
        source = (await conn.execute(select(tasks).where(tasks.c.id == "source"))).mappings().one()
        fields = {key: value for key, value in source.items() if key in archived_tasks.c}
        fields["archived_at"] = NOW + 2
        await conn.execute(insert(archived_tasks).values(**fields))
        await conn.execute(delete(tasks).where(tasks.c.id == "source"))
    await env.db.reconcile_agent_waits(now=NOW + 3)
    assert (await env.db.get_agent_wait(row["id"]))["digest"]["status"] == "COMPLETED"
    async with env.db._engine.begin() as conn:
        await conn.execute(delete(archived_tasks).where(archived_tasks.c.id == "source"))
    missing = await register(env, key="missing", now=NOW + 4)
    assert missing["digest"] == {"reason": "source_unavailable"}


async def test_deleted_producer_resolves_and_cross_project_is_denied(env):
    with pytest.raises(WaitError) as exc:
        await register(env, match={"task_id": "foreign"})
    assert exc.value.code == "out_of_scope"
    row = await register(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(delete(tasks).where(tasks.c.id == "source"))
    assert await env.db.blocking_wait_for("s", 1, NOW + 1) is None
    await env.db.reconcile_agent_waits(now=NOW + 1)
    assert (await env.db.get_agent_wait(row["id"]))["digest"]["reason"] == "source_unavailable"


async def test_timer_expiry_and_cancel_completion_race(env):
    row = await register(env, kind="timer", match={"due_at": NOW + 50})
    assert row["state"] == "active"
    assert await env.db.blocking_wait_for(env.session, 1, NOW + 1)
    await asyncio.gather(
        env.db.cancel_agent_wait(row["id"], identity=env.identity, now=NOW + 60),
        env.db.reconcile_agent_waits(now=NOW + 60),
    )
    assert (await env.db.get_agent_wait(row["id"]))["state"] in ("satisfied", "cancelled")
    assert await result_count(env.db, row["id"]) == 1
    expired = await register(env, key="expire", now=NOW + 70)
    await env.db.reconcile_agent_waits(now=NOW + 200)
    assert (await env.db.get_agent_wait(expired["id"]))["state"] == "expired"
    assert (await env.db.get_task("owner")).status == TaskStatus.IN_PROGRESS


async def test_outbox_failure_and_retry_are_durable(env, monkeypatch):
    row = await register(env, kind="timer", match={"due_at": NOW + 1})
    original = env.db._enqueue_wait_result
    monkeypatch.setattr(env.db, "_enqueue_wait_result", AsyncMock())
    await env.db.reconcile_agent_waits(now=NOW + 2)
    resolved = await env.db.get_agent_wait(row["id"])
    assert resolved["state"] == "satisfied" and resolved["result_message_id"] is None
    monkeypatch.setattr(env.db, "_enqueue_wait_result", original)
    await env.db.reconcile_agent_waits(now=NOW + 3)
    await env.db.reconcile_agent_waits(now=NOW + 4)
    assert await result_count(env.db, row["id"]) == 1
    assert (await env.db.get_agent_wait(row["id"]))["result_message_id"]


@pytest.mark.parametrize("boundary", ["message", "pointer"])
async def test_failed_message_insert_retains_resolution_intent(env, boundary):
    from sqlalchemy import event

    row = await register(env, kind="timer", match={"due_at": NOW + 1})

    def reject_message(conn, cursor, statement, parameters, context, executemany):
        if (boundary == "message" and statement.startswith("INSERT INTO messages")) or (
            boundary == "pointer" and statement.startswith("UPDATE agent_waits SET result_message_id")
        ):
            raise RuntimeError("injected outbox failure")

    event.listen(env.db._engine.sync_engine, "before_cursor_execute", reject_message)
    try:
        await env.db.reconcile_agent_waits(now=NOW + 2)
        assert (await env.db.get_agent_wait(row["id"]))["state"] == "satisfied"
        assert await result_count(env.db, row["id"]) == 0
    finally:
        event.remove(env.db._engine.sync_engine, "before_cursor_execute", reject_message)
    await env.db.reconcile_agent_waits(now=NOW + 3)
    assert await result_count(env.db, row["id"]) == 1


async def test_transaction_rollback_does_not_leave_half_result(env):
    with pytest.raises(RuntimeError):
        async with env.db._engine.begin() as conn:
            await env.db.register_agent_wait_in_transaction(
                conn,
                identity=env.identity,
                kind="timer",
                match={"due_at": NOW - 1},
                deadline_at=NOW + 100,
                idempotency_key="rollback",
                now=NOW,
            )
            raise RuntimeError("crash before commit")
    assert await env.db.list_agent_waits(project_id="p") == []
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(messages)) == 0


async def test_epoch_turnover_cancels_exemption_preserves_history(env):
    row = await register(env)
    assert await env.db.blocking_wait_for("s", 1, NOW + 1)
    assert await env.db.blocking_wait_for("s", 2, NOW + 1) is None
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
        await conn.execute(update(sessions).where(sessions.c.id == "s").values(last_claim_epoch=2))
    assert await env.db.blocking_wait_for("s", 1, NOW + 1) is None
    for mutation in (
        register(env, key="stale"),
        env.db.cancel_agent_wait(row["id"], identity=env.identity, now=NOW + 2),
    ):
        with pytest.raises(WaitError) as exc:
            await mutation
        assert exc.value.code == "stale_claim"
    await env.db.reconcile_agent_waits(now=NOW + 2)
    assert (await env.db.get_agent_wait(row["id"]))["state"] == "cancelled"
    env.identity["claim_epoch"] = 2
    new = await register(env, key="new", now=NOW + 3)
    assert await env.db.blocking_wait_for("s", 2, NOW + 4)
    assert (
        len(await env.db.list_agent_waits(project_id="p", owner_kind="task", owner_id="owner")) == 2
    )
    assert new["id"] != row["id"]


async def test_instance_mismatch_and_manual_pause(env):
    from dataclasses import replace

    stale = dict(env.identity, instance_token="old")
    with pytest.raises(WaitError):
        await env.db.register_agent_wait(
            identity=stale,
            kind="timer",
            match={"due_at": NOW + 1},
            deadline_at=NOW + 100,
            idempotency_key="old",
            now=NOW,
        )
    row = await register(env)
    assert (
        await env.db.blocking_wait_for(replace(env.session, instance_token="old"), 1, NOW + 1)
        is None
    )
    async with env.db._engine.begin() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "owner").values(status="PAUSED", resume_after=None)
        )
    await env.db.reconcile_agent_waits(now=NOW + 1)
    assert (await env.db.get_agent_wait(row["id"]))["state"] == "cancelled"
    owner = await env.db.get_task("owner")
    assert owner.status == TaskStatus.PAUSED and owner.resume_after is None


async def test_message_sequence_ignores_delivery_and_read_markers(env):
    first = await env.db.create_message(
        project_id="p",
        from_kind="session",
        from_id="s",
        to_kind="task",
        to_id="source",
        thread_id="thread",
        body="request",
    )
    assert first.created_seq is not None
    row = await register(
        env, kind="message", match={"thread_id": "thread", "after_seq": first.created_seq}
    )
    reply = await env.db.create_message(
        project_id="p",
        from_kind="system",
        from_id="producer",
        to_kind="task",
        to_id="owner",
        thread_id="thread",
        body="response",
    )
    async with env.db._engine.begin() as conn:
        await conn.execute(
            update(messages)
            .where(messages.c.id == reply.id)
            .values(created_at=NOW + 1, delivered_at=NOW + 2, read_at=NOW + 3, archived_at=NOW + 4)
        )
    await env.db.reconcile_agent_waits(now=NOW + 5)
    result = await env.db.get_agent_wait(row["id"])
    assert result["state"] == "satisfied"
    assert result["digest"]["created_seq"] > first.created_seq
    assert result["result_ref"] == f"message:{reply.id}"


async def test_message_thread_authorization_and_strict_sequence(env):
    foreign = await env.db.create_message(
        project_id="q",
        from_kind="system",
        from_id="producer",
        to_kind="task",
        to_id="owner",
        thread_id="foreign-thread",
        body="private",
    )
    with pytest.raises(WaitError):
        await register(env, kind="message", match={"thread_id": foreign.thread_id, "after_seq": 0})
    own = await env.db.create_message(
        project_id="p",
        from_kind="system",
        from_id="producer",
        to_kind="task",
        to_id="owner",
        thread_id="thread",
        body="previous",
    )
    row = await register(
        env, kind="message", match={"thread_id": "thread", "after_seq": own.created_seq}
    )
    await env.db.reconcile_agent_waits(now=NOW + 1)
    assert (await env.db.get_agent_wait(row["id"]))["state"] == "active"


async def test_bounded_batches_rotate_past_active_producers(env):
    # Supervisor subscriptions can fill a scan batch without blocking a task.
    supervisor = SessionRecord(
        id="super",
        project_id="p",
        profile_id="supervisor",
        harness="codex",
        provider="fake",
        name="n-super",
        lifecycle="named",
        work_dir="/tmp/super",
        epoch="boot",
        instance_token="super-token",
        started_at=NOW,
        state="running",
    )
    await env.db.create_session(supervisor)
    identity = dict(
        session_id="super",
        instance_token="super-token",
        project_id="p",
        claim_epoch=0,
        elevated=True,
    )
    for i in range(100):
        await env.db.register_agent_wait(
            identity=identity,
            kind="task",
            match={"task_id": "source"},
            deadline_at=NOW + 100,
            idempotency_key=f"sub-{i}",
            now=NOW,
        )
    with pytest.raises(WaitError) as exc:
        await env.db.register_agent_wait(
            identity=identity,
            kind="timer",
            match={"due_at": NOW + 200},
            deadline_at=NOW + 300,
            idempotency_key="over-cap",
            now=NOW,
        )
    assert exc.value.code == "wait.subscription_limit"
    row = await register(env, kind="timer", match={"due_at": NOW + 2}, timeout=200)
    first = await env.db.reconcile_agent_waits(now=NOW + 3, limit=1000)
    assert first["scanned"] == 100
    await env.db.reconcile_agent_waits(now=NOW + 4)
    assert (await env.db.get_agent_wait(row["id"]))["state"] == "satisfied"
    assert await env.db.blocking_wait_for("super", 0, NOW + 4) is None


async def test_migration_adds_sequence_to_existing_messages_and_is_idempotent(env):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    await env.db.create_message(
        project_id="p",
        from_kind="system",
        from_id="producer",
        to_kind="task",
        to_id="owner",
        body="pre-upgrade",
    )
    path = Path("migrations/versions/a00000000029_agent_waits.py")
    spec = importlib.util.spec_from_file_location("wait_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with env.db._engine.begin() as conn:
        await conn.execute(text("DROP TABLE agent_waits"))
        await conn.execute(text("ALTER TABLE messages DROP COLUMN created_seq CASCADE"))

        def upgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()
                migration.upgrade()
            assert inspect(sync).has_table("agent_waits")
            column = next(
                c for c in inspect(sync).get_columns("messages") if c["name"] == "created_seq"
            )
            assert column["nullable"] is False and column["identity"]

        await conn.run_sync(upgrade)
    old = (await env.db.get_pending_messages("task", "owner"))[0]
    new = await env.db.create_message(
        project_id="p",
        from_kind="system",
        from_id="producer",
        to_kind="task",
        to_id="owner",
        body="post-upgrade",
    )
    assert 0 < old.created_seq < new.created_seq
