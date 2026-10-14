"""Dormant composition root for the listener, timer and downstream lifecycle owners."""

from __future__ import annotations

import time

from src.waits.listener import WaitSourceListener
from src.waits.registry import build_registry
from src.waits.timers import WaitTimerDriver


class WaitService:
    def __init__(self, orch, *, clock=time.time, owner=None, connect=None):
        self.orch = orch
        self.registry = getattr(orch, "wait_registry", None) or build_registry(
            clock=clock, resource_reader=getattr(orch, "wait_resource_reader", None),
        )
        self.listener = WaitSourceListener(self, clock=clock, owner=owner, connect=connect)
        self.timers = WaitTimerDriver(self, clock=clock)
        handlers = getattr(orch, "wait_v2_handlers", None)
        if handlers is None:
            handlers = orch.wait_v2_handlers = {}
        for name, callback in (("wait_deadline_fire", self.timers.fire),
                               ("wait_source_commit", self.source_commit)):
            if name in handlers:
                raise ValueError(f"duplicate wait lifecycle owner: {name}")
            handlers[name] = callback

    @property
    def enabled(self):
        return getattr(getattr(self.orch.config, "wait_policy", None), "v2_enabled", False) is True

    @property
    def dispatcher(self):
        return getattr(self.orch, "wait_dispatcher", None)

    async def command(self, name, args):
        return await self.orch._command_handler.wait_service_command(name, args)

    async def lease(self, *, release=False):
        return await self.orch._command_handler.wait_service_lease(self.listener, release=release)

    async def drain(self, watermark):
        return await self.orch._command_handler.wait_service_drain(self.listener, watermark)

    async def source_commit(self, args, *, principal, scope, handler):
        seq = await self.orch.db.wait_source_commit(**args)
        return {"success": True, "source_seq": seq}

    async def tick(self):
        if self.enabled:
            return await self.listener.tick()
        return False

    async def run(self):
        if self.enabled:
            await self.listener.run()
