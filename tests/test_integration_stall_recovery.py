"""P0 acceptance: durable ticks recover stalls on disposable PostgreSQL and Git.

No daemon, operator database, hosted forge, LLM or live rollout is involved.
The existing owner-recovery fixture supplies real origins and worker worktrees.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock


from src.integration.outbox import IntegrationOutbox, UNSUBSCRIBED_EVENT_TYPES
from src.integration.service import IntegrationService
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
from tests.test_integration_owner_recovery import remote_sha

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
            db, outbox,
            maintenance={"GitHub PR reviews": hung_review, "maintenance": drain}, clock=lambda: now[0],
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
            db, IntegrationOutbox(db, lost_acceptance, page_size=3, clock=lambda: now[0]),
            clock=lambda: now[0],
        )
        for _ in range(4):
            now[0] += 2
            await asyncio.wait_for(restarted.tick(now[0]), timeout=5)
        await _wait_for_pending_resolution(db, "lost-event")

        rows = await _outbox_rows(db)
        expected_ids = {"lost-event"} | {
            f"noise-{ordinal}" for ordinal in range(len(UNSUBSCRIBED_EVENT_TYPES))
        }
        assert {row["id"] for row in rows} == expected_ids
        assert all(row["delivered_at"] is not None for row in rows)
        assert all(row["last_error"].startswith("unsubscribed:") for row in rows
                   if row["id"].startswith("noise-"))
        [accepted] = [row for row in rows if row["id"] == "lost-event"]
        assert accepted["last_error"] is None and accepted["acceptance_cursor"] == 1
        assert len(await _pending_rows(db)) == 1
        assert len(attempts) == 2
        assert remote_sha(env.origin, "main") == main_before
    finally:
        await runtime.shutdown()
