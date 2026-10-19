"""Dormant composition root for the listener, timer and downstream lifecycle owners."""

from __future__ import annotations

import time

from src.waits.listener import WaitSourceListener
from src.waits.registry import build_registry
from src.waits.timers import WaitTimerDriver
from src.waits.dispatcher import WakeDispatcher
from src.waits.graph import recompute_from
from src.waits.source_locking import lock_database_source
from src.waits.decisions import WaitDecisions


class WaitService:
    def __init__(self, orch, *, clock=time.time, owner=None, connect=None):
        self.orch = orch
        self.registry = getattr(orch, "wait_registry", None) or build_registry(
            clock=clock, resource_reader=getattr(orch, "wait_resource_reader", None),
        )
        self.listener = WaitSourceListener(self, clock=clock, owner=owner, connect=connect)
        self.timers = WaitTimerDriver(self, clock=clock)
        self.decisions = WaitDecisions(self, clock=clock)
        self.registry.loop_handler = self.loop_escalation
        self._dispatcher = WakeDispatcher(
            self.registry, clock=clock,
            recompute_priority=getattr(orch, "wait_recompute_priority", recompute_from),
            resolve_escalation=getattr(orch, "wait_resolve_escalation",
                                       self.decisions.resolve_escalation),
            lock_source=getattr(orch, "wait_lock_source", lock_database_source),
        )
        handlers = getattr(orch, "wait_v2_handlers", None)
        if handlers is None:
            handlers = orch.wait_v2_handlers = {}
        for name, callback in (("wait_deadline_fire", self.timers.fire),
                               ("wait_source_commit", self.source_commit),
                               ("wait_decide", self.decisions.decide)):
            if name in handlers:
                raise ValueError(f"duplicate wait lifecycle owner: {name}")
            handlers[name] = callback
        deadlines = getattr(orch, "wait_deadline_handlers", None)
        if deadlines is None:
            deadlines = orch.wait_deadline_handlers = {}
        if "escalation" in deadlines:
            raise ValueError("duplicate wait lifecycle owner: escalation deadline")
        deadlines["escalation"] = self.decisions.raise_escalation

    @property
    def enabled(self):
        return getattr(getattr(self.orch.config, "wait_policy", None), "v2_enabled", False) is True

    @property
    def dispatcher(self):
        return getattr(self.orch, "wait_dispatcher", self._dispatcher)

    async def command(self, name, args):
        return await self.orch._command_handler.wait_service_command(name, args)

    async def lease(self, *, release=False):
        return await self.orch._command_handler.wait_service_lease(self.listener, release=release)

    async def drain(self, watermark):
        return await self.orch._command_handler.wait_service_drain(self.listener, watermark)

    async def source_commit(self, args, *, principal, scope, handler):
        seq = await self.orch.db.wait_source_commit(**args)
        return {"success": True, "source_seq": seq}

    async def loop_escalation(self, conn, wait, *, now):
        from sqlalchemy import update
        from src.database.tables import wait_deadlines

        await self.decisions.raise_escalation(
            conn, {"generation": 1}, wait, now=now, handler=self.orch._command_handler,
        )
        await conn.execute(update(wait_deadlines).where(
            wait_deadlines.c.wait_id == wait["id"],
            wait_deadlines.c.deadline_type == "escalation", wait_deadlines.c.generation == 1,
        ).values(fired_at=now))

    async def tick(self):
        if self.enabled:
            return await self.listener.tick()
        return False

    async def run(self):
        if self.enabled:
            await self.listener.run()
