"""Independent, coalescing delivery of the durable messages backlog."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


class MessageDeliveryService:
    """One consumer, independent of scheduler/Git work and patrol playbooks.

    Events are hints, never the queue: every pass reads pending database rows,
    including after restart and when an emitter did not publish an event.
    A burst occupies one event bit, and passes run no faster than the configured
    delivery interval. No task or timer is allocated per message.
    """

    def __init__(self, engine, config: Callable, bus):
        self._engine = engine
        self._config = config
        self._bus = bus
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._unsubscribe = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._wake.clear()
        self._unsubscribe = self._bus.subscribe("message.sent", self._on_message)
        self._task = asyncio.create_task(self._run(), name="message-delivery")

    def _on_message(self, payload: dict) -> None:
        # User platform delivery itself emits message.sent. Do not feed those
        # completions back into this consumer, or wake for non-live mailboxes.
        if payload.get("to_kind") in {"session", "task"}:
            self._wake.set()

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            self._wake.clear()
            started = loop.time()
            if self._config().enabled:
                try:
                    await self._engine.run_delivery_pass()
                except Exception:
                    logger.exception("Message delivery pass failed")
                try:
                    async with asyncio.timeout(120):
                        await self._engine.check_reply_timeouts()
                except Exception:
                    logger.exception("Message reply timeout pass failed")
            interval = max(0.1, self._config().delivery_interval)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except TimeoutError:
                pass
            # Events received during a pass are coalesced. Even a continuous
            # sender or immediate failure cannot turn durable retry into a spin.
            await asyncio.sleep(max(0, interval - (loop.time() - started)))
