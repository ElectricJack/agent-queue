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
        self._cursors: dict[str, tuple[Any, ...] | None] = {
            "schedules": None,
            "repair_stages": None,
            "candidate_ci": None,
            "intents": None,
        }

    async def tick(self, now: float) -> None:
        if self._tick_lock.locked():
            return
        await self._tick_lock.acquire()
        try:
            await self._source("schedule", self._tick_schedules, now)
            await self._source("repair deadline", self._tick_repair_stages, now)
            await self._source("candidate CI", self._tick_candidate_ci, now)
            await self._source("integration intent", self._tick_intents, now)

            # Task 10c adds the cleanup selector. The handler slot is intentionally
            # dormant here rather than manufacturing successful cleanup work.
            await self._source("integration outbox", self._outbox.dispatch_due, now)
        finally:
            self._tick_lock.release()

    async def _tick_schedules(self, now: float) -> None:
        rows = await self._page(
            "schedules",
            self._db.due_integration_schedule_page,
            lambda row: (row["next_due_at"], row["project_id"]),
            now=now,
        )
        for row in rows:
            await self._isolated(
                "schedule",
                row,
                self._scheduler.mark_due,
                row["project_id"],
                now,
                "periodic",
            )

    async def _tick_repair_stages(self, now: float) -> None:
        rows = await self._page(
            "repair_stages",
            self._db.due_integration_repair_stage_page,
            lambda row: (row["deadline_at"], row["operation_id"], row["stage"]),
            now=now,
        )
        for row in rows:
            await self._isolated(
                "repair deadline",
                row,
                self._repair.expire,
                row["operation_id"],
                int(row["stage"]),
                now=now,
            )

    async def _tick_candidate_ci(self, now: float) -> None:
        rows = await self._page(
            "candidate_ci",
            self._db.pending_candidate_ci_page,
            lambda row: (row["updated_at"], row["batch_id"], row["revision"]),
        )
        await self._run_optional("candidate CI", rows, self._candidate_ci_handler, now)

    async def _tick_intents(self, now: float) -> None:
        rows = await self._page(
            "intents",
            self._db.unresolved_integration_intent_page,
            lambda row: (row["updated_at"], row["id"]),
        )
        await self._run_optional(
            "integration intent", rows, self._unresolved_intent_handler, now
        )

    async def _page(
        self,
        source: str,
        selector: Callable[..., Awaitable[list[dict[str, Any]]]],
        cursor_for: Callable[[dict[str, Any]], tuple[Any, ...]],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        after = self._cursors[source]
        rows = await selector(after=after, limit=self._page_size, **kwargs)
        if not rows and after is not None:
            self._cursors[source] = None
            rows = await selector(after=None, limit=self._page_size, **kwargs)
        if rows:
            # Fairness state advances on what was scanned, before any handler can
            # decline, fail, or leave the durable row intentionally untouched.
            self._cursors[source] = cursor_for(rows[-1])
        return rows

    @staticmethod
    async def _source(name: str, callback: Callable, *args, **kwargs) -> Any:
        try:
            return await callback(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("integration %s source failed and remains retryable", name)
            return None

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
            try:
                await self.tick(self._clock())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("unexpected integration reconciliation tick failure")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                pass
