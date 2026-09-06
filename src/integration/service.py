"""Single-loop bounded reconciliation for durable integration work."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

IntegrationHandler = Callable[[dict[str, Any], float], Awaitable[Any]]


class IntegrationService:
    """Poll durable integration work without becoming a second authority."""

    def __init__(
        self,
        db: Any,
        scheduler: Any,
        repair: Any,
        outbox: Any,
        *,
        candidate_ci_handler: IntegrationHandler | None = None,
        unresolved_intent_handler: IntegrationHandler | None = None,
        cleanup_handler: IntegrationHandler | None = None,
        page_size: int = 100,
        interval_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if page_size <= 0:
            raise ValueError("integration service page size must be positive")
        if interval_seconds <= 0:
            raise ValueError("integration service interval must be positive")
        self._db = db
        self._scheduler = scheduler
        self._repair = repair
        self._outbox = outbox
        self._candidate_ci_handler = candidate_ci_handler
        self._unresolved_intent_handler = unresolved_intent_handler
        self._cleanup_handler = cleanup_handler
        self._page_size = page_size
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._tick_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def tick(self, now: float) -> None:
        if self._tick_lock.locked():
            return
        await self._tick_lock.acquire()
        try:
            schedules = await self._db.due_integration_schedule_page(
                now=now, after=None, limit=self._page_size
            )
            for row in schedules:
                await self._isolated(
                    "schedule",
                    row,
                    self._scheduler.mark_due,
                    row["project_id"],
                    now,
                    "periodic",
                )

            stages = await self._db.due_integration_repair_stage_page(
                now=now, after=None, limit=self._page_size
            )
            for row in stages:
                await self._isolated(
                    "repair deadline",
                    row,
                    self._repair.expire,
                    row["operation_id"],
                    int(row["stage"]),
                    now=now,
                )

            candidates = await self._db.pending_candidate_ci_page(
                after=None, limit=self._page_size
            )
            await self._run_optional("candidate CI", candidates, self._candidate_ci_handler, now)

            intents = await self._db.unresolved_integration_intent_page(
                after=None, limit=self._page_size
            )
            await self._run_optional(
                "integration intent", intents, self._unresolved_intent_handler, now
            )

            # Task 10c adds the cleanup selector. The handler slot is intentionally
            # dormant here rather than manufacturing successful cleanup work.
            await self._isolated("integration outbox", {}, self._outbox.dispatch_due, now)
        finally:
            self._tick_lock.release()

    async def _run_optional(
        self,
        name: str,
        rows: list[dict[str, Any]],
        handler: IntegrationHandler | None,
        now: float,
    ) -> None:
        for row in rows:
            if handler is None:
                logger.warning("%s remains pending: later-phase handler is unavailable", name)
                continue
            await self._isolated(name, row, handler, row, now)

    @staticmethod
    async def _isolated(name: str, row: dict[str, Any], callback: Callable, *args, **kwargs):
        try:
            return await callback(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("integration %s item failed and remains retryable: %s", name, row)
            return None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(), name="integration-reconciliation-service"
        )

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        await task
        self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            await self.tick(self._clock())
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                pass
