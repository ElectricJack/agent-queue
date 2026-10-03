"""P0 acceptance: durable ticks recover stalls on disposable PostgreSQL and Git.

No daemon, operator database, hosted forge, LLM or live rollout is involved.
The existing owner-recovery fixture supplies real origins and worker worktrees.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import insert, select, update

from src.database.tables import (
    integration_batches,
    integration_branch_owners,
    integration_outbox,
    integration_repair_operations,
    integration_repair_stages,
    messages,
    playbook_artifacts,
    project_integration_leases,
)
from src.integration.outbox import IntegrationOutbox, UNSUBSCRIBED_EVENT_TYPES
from src.integration.ownership import BranchOwnership
from src.integration.repair import RepairService
from src.integration.scheduler import IntegrationScheduler, TrainService
from src.integration.service import IntegrationService
from src.models import Task, TaskStatus
from tests import test_integration_owner_recovery as owner_tests
from tests.test_integration_outbox import (
    NOW,
    _activate,
    _enqueue,
    _outbox_rows,
    _pending_rows,
    _proving,
    _runtime,
    _wait_for_pending_resolution,
)
from tests.test_integration_owner_recovery import git, remote_sha
from tests.test_integration_repair import _artifact, _policy
from tests.test_integration_repair_rollover import _batch_writer, _ordinals, _stage

env = owner_tests.env


async def test_hung_review_and_lost_event_do_not_stop_auditable_outbox_drain(env, tmp_path):
    db = env.db
    compiled = tmp_path / "compiled"
    await _activate(db, compiled, "stall-acceptance", "integration.sealed")
    await _enqueue(db, event_id="lost-event", dedup_key="lost-event")
    for ordinal, event_type in enumerate(sorted(UNSUBSCRIBED_EVENT_TYPES)):
        await _enqueue(
            db, event_id=f"noise-{ordinal}", dedup_key=f"noise-{ordinal}", event_type=event_type,
        )
    runtime = _runtime(db, compiled)
    now = [NOW]
    attempts = []
    cancelled = asyncio.Event()
    main_before = remote_sha(env.origin, "main")

    async def lost_acceptance(event_type, payload, event_id):
        if event_id == "lost-event":
            attempts.append(event_id)
            if len(attempts) == 1:
                raise ConnectionError("response lost before durable event acceptance")
        return await _proving(runtime)(event_type, payload, event_id)

    async def hung_review(_now):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    drain = AsyncMock()
    try:
        await runtime.refresh()
        outbox = IntegrationOutbox(db, lost_acceptance, page_size=3, clock=lambda: now[0])
        service = IntegrationService(
            db, SimpleNamespace(mark_due=AsyncMock()), SimpleNamespace(), outbox,
            review_handler=hung_review, drain_handler=drain, clock=lambda: now[0],
            source_timeouts={"GitHub PR reviews": 0.05},
        )
        await asyncio.wait_for(service.tick(now[0]), timeout=5)
        assert cancelled.is_set()
        assert drain.await_count == 1
        [lost] = [row for row in await _outbox_rows(db) if row["id"] == "lost-event"]
        assert lost["delivered_at"] is None and lost["attempts"] == 1
        assert "ConnectionError" in lost["last_error"]

        # New service and outbox instances have no in-memory cursor or event.
        restarted = IntegrationService(
            db, SimpleNamespace(mark_due=AsyncMock()), SimpleNamespace(),
            IntegrationOutbox(db, lost_acceptance, page_size=3, clock=lambda: now[0]),
            clock=lambda: now[0],
        )
        for _ in range(4):
            now[0] += 2
            await asyncio.wait_for(restarted.tick(now[0]), timeout=5)
        await _wait_for_pending_resolution(db, "lost-event")

        rows = await _outbox_rows(db)
        assert len(rows) == 6 and all(row["delivered_at"] is not None for row in rows)
        assert all(row["last_error"].startswith("unsubscribed:") for row in rows
                   if row["id"].startswith("noise-"))
        [accepted] = [row for row in rows if row["id"] == "lost-event"]
        assert accepted["last_error"] is None and accepted["acceptance_cursor"] == 1
        assert len(await _pending_rows(db)) == 1
        assert len(attempts) == 2
        assert remote_sha(env.origin, "main") == main_before
    finally:
        await runtime.shutdown()


async def test_unknown_dispatch_restarts_then_preserves_stopped_work_and_waits_for_capacity(env):
    case = await _batch_writer(env)
    db = case.db
    now = [130.0]
    main_before = remote_sha(env.origin, "main")
    collision = f"repair-{case.operation}-1"
    await db.create_task(Task(
        id=collision, project_id="p", title="Unrelated task", description="",
        status=TaskStatus.DEFINED,
    ))
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == case.operation,
            integration_repair_stages.c.ordinal == 0,
        ).values(attempts=1))

    outcomes = []
    repair = case.repair

    async def dispatch(row):
        result = await repair.dispatch(row["operation_id"], row["ordinal"])
        outcomes.append(result["outcome"])
        return result

    def service():
        # The durable dispatch selection, rather than an event or reservation
        # side path, must drive this repair after a refused/lost run.
        return IntegrationService(
            db, SimpleNamespace(mark_due=AsyncMock()),
            SimpleNamespace(expire=repair.expire, pending_dispatches=repair.pending_dispatches),
            SimpleNamespace(dispatch_due=AsyncMock()), repair_dispatcher=dispatch,
            clock=lambda: now[0],
        )

    await asyncio.wait_for(service().tick(now[0]), timeout=10)
    assert outcomes == ["unknown"]
    assert (await _stage(case, 1))["repair_task_id"] is None

    # A restart retains the active stage and its once-per-reason notice.
    repair = RepairService(db, owner_recovery=case.recovery, clock=lambda: now[0])
    restarted = service()
    now[0] = 131.0
    await asyncio.wait_for(restarted.tick(now[0]), timeout=10)
    assert outcomes == ["unknown", "unknown"]
    async with db._engine.connect() as conn:
        notices = (await conn.execute(select(messages).where(
            messages.c.body_kind == "integration_repair_dispatch_unknown",
        ))).mappings().all()
    assert len(notices) == 1 and "delegate_id_collision" in notices[0]["body"]

    await db.delete_task(collision)
    await db.update_session("old-session", state="stopped", desired_state="stopped")
    await db.update_task(case.primary, status=TaskStatus.BLOCKED)
    now[0] = 160.0
    await restarted.tick(now[0])
    assert outcomes == ["unknown", "unknown"]  # retry is due at 161, not every tick
    now[0] = 161.0
    await asyncio.wait_for(restarted.tick(now[0]), timeout=10)
    assert outcomes[-1] == "dispatched"
    stage = await _stage(case, 1)
    delegate = stage["repair_task_id"]
    assert (await db.get_task(delegate)).status is TaskStatus.READY
    owner = await BranchOwnership(db).get_owner(case.target)
    assert owner["owner_id"] == delegate and owner["handoff_state"] == "reserved"
    preserved = remote_sha(env.origin, f"aq/preserved/{case.owner['id']}")
    assert preserved == case.head
    for source in case.sources:
        git(env.origin, "merge-base", "--is-ancestor", source, preserved)
    assert len([row for row in await env.audits(case.owner["id"])
                if row["outcome"] == "preserved_and_released"]) == 1

    # The new writer has never been claimed. Deadline ticks extend that same
    # stage, with one capacity notice, rather than consuming new ordinals.
    for at in (190.0, 220.0, 250.0):
        now[0] = at
        await asyncio.wait_for(restarted.tick(at), timeout=10)
    deferred = await _stage(case, 1)
    assert deferred["deadline_at"] == 280.0
    assert deferred["repair_task_id"] == delegate and deferred["attempts"] == 0
    assert await _ordinals(case) == [0, 1]
    async with db._engine.connect() as conn:
        assert len((await conn.execute(select(messages).where(
            messages.c.body_kind == "integration_repair_deferred",
        ))).all()) == 1
    assert remote_sha(env.origin, "main") == main_before
    assert remote_sha(env.origin, case.target.branch) == case.partial


async def test_repeated_empty_frontier_ticks_create_no_batches_owners_or_remote_writes(env):
    db = env.db
    artifact = _artifact()
    await db.update_project(
        "p", hierarchical_integration_mode="train", integration_repository_id="r",
        hierarchical_integration_policy=_policy(), integration_mode="pull_request",
    )
    async with db.immediate() as conn:
        await conn.execute(insert(playbook_artifacts).values(
            **artifact.model_dump(mode="json"), scope="project", scope_identifier="p",
            profile_fingerprint="", path="/tmp/artifact", size_bytes=1,
            validation="{}", created_at=1.0,
        ))
    refs_before = git(env.origin, "show-ref")
    scheduler = IntegrationScheduler(db)
    train = TrainService(db)
    seals = []
    now = [20.0]

    async def seal_event(event_type, payload, event_id):
        assert event_type == "integration.sweep_due"
        seals.append(await train.seal("p", payload["operation_id"], now[0]))
        return True

    service = IntegrationService(
        db, scheduler, SimpleNamespace(),
        IntegrationOutbox(db, seal_event, clock=lambda: now[0]), clock=lambda: now[0],
    )
    for _ in range(3):
        request = await scheduler.mark_due("p", now[0], "manual")
        await asyncio.wait_for(service.tick(now[0]), timeout=5)
        # Lost result replay is still empty after the durable request was released.
        replay = await train.seal("p", request["request_id"], now[0] + 1)
        assert replay == seals[-1] and replay["outcome"] == "empty"
        now[0] += 10
    assert len(seals) == 3
    async with db._engine.connect() as conn:
        for table in (integration_batches, integration_branch_owners, integration_repair_operations,
                      project_integration_leases):
            assert (await conn.execute(select(table))).all() == []
        assert (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.delivered_at.is_(None),
        ))).all() == []
    assert git(env.origin, "show-ref") == refs_before
