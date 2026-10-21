"""Database-backed deadline heap, elected by the source listener's lease."""

from __future__ import annotations

import asyncio
import heapq
import time

from sqlalchemy import func, or_, select, update

from src.database.queries.wait_queries import REGISTRATION_LOCK
from src.database.tables import (
    admission_reservations,
    continuations,
    tasks,
    wait_deadlines,
    wait_records,
)
from src.waits.listener import LeaseLost, ListenerUnavailable


class WaitTimerDriver:
    def __init__(self, service, *, clock=time.time):
        self.service = service
        self.clock = clock
        self.heap = []
        self._heap_lock = asyncio.Lock()

    async def rearm(self):
        async with self._heap_lock:
            await self._rearm()

    async def _rearm(self):
        async with self.service.orch.db._engine.connect() as conn:
            rows = (await conn.execute(select(wait_deadlines).where(
                wait_deadlines.c.fired_at.is_(None), wait_deadlines.c.cancelled_at.is_(None),
            ))).mappings().all()
        self.heap = [(row["due_at"], row["id"], dict(row)) for row in rows]
        heapq.heapify(self.heap)

    async def tick(self):
        async with self._heap_lock:
            return await self._tick()

    async def _tick(self):
        fired = 0
        while self.heap and self.heap[0][0] <= self.clock():
            _, _, row = self.heap[0]
            async with asyncio.timeout(self.service.listener.renewal_seconds):
                result = await self.service.command("wait_deadline_fire", {
                    "wait_id": row["wait_id"], "deadline_type": row["deadline_type"],
                    "generation": row["generation"],
                })
            if not result.get("success") and result.get("error_code") != "wait.deadline_stale":
                raise ListenerUnavailable(f"wait deadline fire refused: {result}")
            heapq.heappop(self.heap)
            fired += bool(result.get("success"))
        return fired

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
            try:
                cursor = await self.service.listener.locked_lease(conn)
            except LeaseLost:
                return {"success": False, "error_code": "wait.unavailable",
                        "error": "timer driver no longer owns the source lease"}
            await conn.execute(select(func.pg_advisory_xact_lock(REGISTRATION_LOCK)))
            # Task before wait before deadline, like registration and dispatch.
            subject = (await conn.execute(select(wait_records).where(
                wait_records.c.id == args["wait_id"],
            ))).mappings().first()
            if subject and subject["owner_kind"] in {"task", "scheduler"}:
                await conn.execute(select(tasks.c.id).where(
                    tasks.c.id == subject["owner_id"],
                ).with_for_update())
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
                    raise ListenerUnavailable(f"deadline effect failed: {result}")
                await conn.execute(update(wait_deadlines).where(
                    wait_deadlines.c.id == row["id"],
                ).values(fired_at=now))
            if cursor["lease_expires_at"] <= self.clock():
                raise LeaseLost("timer lease expired during deadline effect")
        if payload := result.pop('escalation_event', None):
            await self.service.orch.bus.emit('wait.escalated', payload)
        return result
