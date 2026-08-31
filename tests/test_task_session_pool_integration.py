"""Pool launch/exit metadata using fake providers and disposable workspaces."""

import time

from tests.test_pool_reconciler import db, orch, ready  # noqa: F401


async def test_pool_launch_timestamp_precedes_provider_start(orch, db, monkeypatch):  # noqa: F811
    await ready(db, "task")
    provider = orch.session_providers.create("fake", orch.config)
    original_start = provider.start
    observed = []

    async def start(spec):
        observed.append(time.time())
        return await original_start(spec)

    monkeypatch.setattr(provider, "start", start)
    await orch._reconcile_pools()
    (row,) = await db.list_sessions(lifecycle="pool")
    assert row.started_at <= observed[0]
    assert row.session_key == row.id  # Claude's --session-id is already exact.


async def test_pool_termination_retains_specific_reason(orch, db):  # noqa: F811
    await ready(db, "task")
    await orch._reconcile_pools()
    (row,) = await db.list_sessions(lifecycle="pool")
    async with db.immediate() as conn:
        await db.record_holder(
            conn,
            session_id=row.id,
            task_id="task",
            agent_id=row.agent_id,
            work_dir=row.work_dir,
            now=time.time(),
        )
    await orch._terminate_pool_session(row, reason="rapid_crash")
    session = await db.get_session(row.id)
    (attempt,) = await db.list_task_session_attempts("task")
    assert session.end_reason == attempt["end_reason"] == "rapid_crash"
    assert session.ended_at is not None and attempt["ended_at"] is not None
