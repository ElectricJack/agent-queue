"""Database-backed deadline heap, elected by the source listener's lease."""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import time

from sqlalchemy import insert, or_, select, update

from src.database.tables import (
    admission_reservations,
    continuations,
    events,
    messages,
    wait_deadlines,
    wait_records,
)
from src.waits.listener import LeaseLost, ListenerUnavailable
from src.waits.conditions import WaitError
from src.waits.graph import background_graph_work, lock_graph
from src.waits.sources import insert_message_with_source_event
from src.waits.admission import admission_pass

logger = logging.getLogger(__name__)

# These identify an invalid/stale owner or request, rather than missing runtime
# infrastructure. Unknown errors and lease/service failures remain retryable.
PERMANENT_REFUSALS = frozenset({
    "wait.out_of_scope", "wait.invalid", "wait.condition_required",
    "wait.not_escalated", "wait.source_unresolved", "continuation.stale_generation",
    "stale_claim",
})


class WaitTimerDriver:
    retry_seconds = 1
    max_retry_seconds = 30

    def __init__(self, service, *, clock=time.time):
        self.service = service
        self.clock = clock
        self.heap = []
        self._heap_lock = asyncio.Lock()
        # Durable deadlines retain retry ownership; this local cadence only
        # prevents contention from hammering the graph fence between rebuilds.
        self._retry_deadlines = {}

    async def rearm(self):
        async with self._heap_lock:
            await self._rearm()

    async def _rearm(self):
        async with self.service.orch.db._engine.connect() as conn:
            rows = (await conn.execute(select(wait_deadlines).where(
                wait_deadlines.c.fired_at.is_(None), wait_deadlines.c.cancelled_at.is_(None),
            ))).mappings().all()
        active = {row["id"] for row in rows}
        self._retry_deadlines = {key: value for key, value in self._retry_deadlines.items()
                                 if key in active}
        self.heap = [(max(row["due_at"], self._retry_deadlines.get(row["id"], (0, 0))[0]),
                      row["id"], dict(row)) for row in rows]
        heapq.heapify(self.heap)

    async def tick(self):
        async with self._heap_lock:
            return await self._tick()

    async def _tick(self):
        fired = 0
        while self.heap and self.heap[0][0] <= self.clock():
            _, deadline_id, row = self.heap[0]
            try:
                async with asyncio.timeout(self.service.listener.lease_seconds):
                    result = await self.service.command("wait_deadline_fire", {
                        "wait_id": row["wait_id"], "deadline_type": row["deadline_type"],
                        "generation": row["generation"],
                    })
                if not result.get("success") and result.get("error_code") != "wait.deadline_stale":
                    if result.get("error_code") == "wait.graph_busy":
                        raise WaitError("wait.graph_busy", result.get("error", "graph busy"))
                    if result.get("error_code") not in PERMANENT_REFUSALS:
                        raise ListenerUnavailable(f"wait deadline fire refused: {result}")
                    await self.service.quarantine_deadline(row, result)
            except WaitError as exc:
                if exc.code != "wait.graph_busy":
                    raise
                previous = self._retry_deadlines.get(deadline_id)
                delay = min(previous[1] * 2, self.max_retry_seconds) if previous else self.retry_seconds
                due = self.clock() + delay
                self._retry_deadlines[deadline_id] = (due, delay)
                heapq.heapreplace(self.heap, (due, deadline_id, row))
                logger.debug("wait.deadline_graph_busy id=%s retry_after=%s", deadline_id, delay)
                continue
            heapq.heappop(self.heap)
            self._retry_deadlines.pop(deadline_id, None)
            fired += bool(result.get("success"))
        return fired

    @background_graph_work()
    async def quarantine(self, deadline, refusal):
        """Retire one poisoned deadline, retaining its refusal and notifying once.

        The service command boundary authenticates this internal mutation. The
        lease and row CAS make restart/replay harmless; the wait stays inspectable
        and can be cancelled by its project supervisor without inventing a wake.
        """
        now = self.clock()
        async with self.service.orch.db._engine.begin() as conn:
            await lock_graph(conn)
            await self.service.listener.locked_lease(conn)
            row = (await conn.execute(update(wait_deadlines).where(
                wait_deadlines.c.id == deadline["id"],
                wait_deadlines.c.fired_at.is_(None), wait_deadlines.c.cancelled_at.is_(None),
            ).values(cancelled_at=now).returning(wait_deadlines.c.id))).scalar_one_or_none()
            if row is None:
                return
            wait = (await conn.execute(select(wait_records).where(
                wait_records.c.id == deadline["wait_id"],
            ))).mappings().one()
            payload = {"wait_id": wait["id"], "deadline_id": row,
                       "project_id": wait["project_id"],
                       "deadline_type": deadline["deadline_type"],
                       "generation": deadline["generation"], "refusal": refusal}
            await conn.execute(insert(events).values(
                event_type="wait.deadline_quarantined", project_id=wait["project_id"],
                task_id=wait["owner_id"] if wait["owner_kind"] == "task" else None,
                timestamp=now, payload=json.dumps(payload),
            ))
            await insert_message_with_source_event(conn, insert(messages).values(
                id=f"wait-deadline:{row}:quarantined", project_id=wait["project_id"],
                from_kind="system", from_id="waits-driver", to_kind="session",
                to_id=f"supervisor-{wait['project_id']}", body_kind="wait_deadline_quarantined",
                body=json.dumps({**payload, "next_step":
                    "Inspect the refusal; use aq wait cancel to retire an orphaned task wait."}),
                created_at=now,
            ))
            await self.service.listener.locked_lease(conn)
        logger.warning("wait.deadline_quarantined: %s", payload)

    async def run(self):
        changed = self.service.listener.deadlines_changed
        while True:
            changed.clear()
            await self.rearm()
            await self.tick()
            delay = max(0, self.heap[0][0] - self.clock()) if self.heap else 60
            # The periodic rebuild also recovers a lost deadline notification.
            try:
                await asyncio.wait_for(changed.wait(), min(delay, 60))
            except TimeoutError:
                pass

    @admission_pass("service.scheduler.reader")

    @background_graph_work()
    async def fire(self, args, *, principal, scope, handler):
        """Transactional command callback; all five deadline types share this CAS.

        Non-due lifecycle owners install ``orch.wait_deadline_handlers[type]``.
        A callback receives (conn, deadline, wait, *, now, handler) and commits
        the effect in THIS transaction. Missing or failed effects roll back firing.
        Ceiling callbacks invoke the sleep decision with reason='ceiling';
        escalation callbacks create the wait escalation without waking its owner;
        transition callbacks hold an overdue stop/launch; reservation callbacks
        mark the corresponding reservation overdue. The owning leaves provide
        those policies; this driver never substitutes a wake for an absent owner.
        """
        db = self.service.orch.db
        now = args.get("now", self.clock())
        async with db._engine.begin() as conn:
            # Match source dispatch: graph before lease, so a busy graph never
            # fences out lease renewal and timer/source writers cannot deadlock.
            await lock_graph(conn)
            try:
                cursor = await self.service.listener.locked_lease(conn)
            except LeaseLost:
                return {"success": False, "error_code": "wait.unavailable",
                        "error": "timer driver no longer owns the source lease"}
            # Task before wait before deadline, like registration and dispatch.
            subject = (await conn.execute(select(wait_records).where(
                wait_records.c.id == args["wait_id"],
            ))).mappings().first()
            if subject and subject["owner_kind"] in {"task", "scheduler"}:
                from src.waits.recovery import retire_terminal_task_waits

                if await retire_terminal_task_waits(conn, subject["owner_id"], now=now):
                    await self.service.listener.locked_lease(conn)
                    return {"success": False, "error_code": "wait.deadline_stale",
                            "error": "terminal task waits retired"}
            wait = (await conn.execute(select(wait_records).where(
                wait_records.c.id == args["wait_id"],
            ).with_for_update())).mappings().first()
            continuation_ids = list((await conn.execute(select(continuations.c.id).where(
                continuations.c.wait_id == args["wait_id"],
            ).order_by(continuations.c.id).with_for_update())).scalars())
            if wait:
                await conn.execute(select(admission_reservations.c.id).where(or_(
                    admission_reservations.c.continuation_id.in_(continuation_ids),
                    admission_reservations.c.task_id == wait["owner_id"],
                )).order_by(admission_reservations.c.id).with_for_update())
            newer = wait_deadlines.alias("newer")
            row = (await conn.execute(select(wait_deadlines).where(
                wait_deadlines.c.wait_id == args["wait_id"],
                wait_deadlines.c.deadline_type == args["deadline_type"],
                wait_deadlines.c.generation == args["generation"],
                wait_deadlines.c.fired_at.is_(None), wait_deadlines.c.cancelled_at.is_(None),
                wait_deadlines.c.due_at <= now,
                ~select(newer.c.id).where(
                    newer.c.wait_id == args["wait_id"],
                    newer.c.deadline_type == args["deadline_type"],
                    newer.c.generation > args["generation"],
                ).exists(),
            ).with_for_update())).mappings().first()
            if not wait or wait["outcome"] != "pending" or not row:
                return {"success": False, "error_code": "wait.deadline_stale",
                        "error": "deadline resolved, cancelled, not due or superseded"}
            if row["deadline_type"] == "due":
                if wait["condition_kind"] != "timer":
                    return {"success": False, "error_code": "wait.deadline_stale",
                            "error": "due deadline requires a timer condition"}
                seq = await db.fire_wait_due_deadline(wait["id"], row["generation"],
                                                      now=now, conn=conn)
                result = {"success": True, "source_seq": seq}
            else:
                callback = getattr(self.service.orch, "wait_deadline_handlers", {}).get(
                    row["deadline_type"],
                )
                if callback is None:
                    return {"success": False, "error_code": "wait.unavailable",
                            "error": f"{row['deadline_type']} deadline effect is unavailable"}
                result = await callback(conn, dict(row), dict(wait), now=now, handler=handler)
                if not isinstance(result, dict) or not result.get("success"):
                    if isinstance(result, dict) and result.get("error_code") == "wait.graph_busy":
                        raise WaitError("wait.graph_busy", result.get("error", "graph busy"))
                    raise ListenerUnavailable(f"deadline effect failed: {result}")
                await conn.execute(update(wait_deadlines).where(
                    wait_deadlines.c.id == row["id"],
                ).values(fired_at=now))
            if cursor["lease_expires_at"] <= self.clock():
                raise LeaseLost("timer lease expired during deadline effect")
        if payload := result.pop('escalation_event', None):
            await self.service.orch.bus.emit('wait.escalated', payload)
        return result
