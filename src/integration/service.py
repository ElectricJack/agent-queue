"""Bounded control and remote reconciliation for durable integration work.

Two properties keep one stuck subject from stopping the train:

* **Every source and item is bounded.**  Each source callback, and each item of
  a page the service iterates itself, runs in its own task under a wall-clock
  budget.  A callback past its budget is cancelled and the pass moves on; the
  durable row stays where it was and is retried on a later pass.  A callback
  that refuses cancellation is left to unwind on its own and its source is
  skipped until it has, so two copies of one source never run at once.  (Bare
  ``asyncio.wait_for`` is not enough: it waits for the cancelled coroutine to
  finish, so an uncancellable call would still hold the pass.)
* **Refusals are revisited from durable Subjects.** Each runtime resumes
  the persisted cursor and next visit time after a restart; the service owns
  neither writer policy nor an in-memory delivery queue.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

IntegrationHandler = Callable[[dict[str, Any], float], Awaitable[Any]]
DrainHandler = Callable[[float], Awaitable[Any]]

#: Budget for one source callback (one bounded page of one source).  The
#: slowest sources observed on the operator's install (2026-09-28..10-02) were
#: GitHub PR reviews at 131 s, the outbox at 128 s and branch materialization
#: at 120 s; a hang is anything well past that.
DEFAULT_SOURCE_TIMEOUT_SECONDS = 300.0
#: Budget for one item of a page the service iterates itself.
DEFAULT_ITEM_TIMEOUT_SECONDS = 60.0
#: How long a timed-out callback gets to unwind its cancellation before the
#: pass stops waiting for it.
CANCEL_GRACE_SECONDS = 5.0


class _CallTimedOut(Exception):
    """One bounded callback exceeded its budget and was cancelled."""

    def __init__(self, budget: float) -> None:
        super().__init__(f"exceeded {budget:.0f}s")
        self.budget = budget


class IntegrationService:
    """One bounded pass visits subjects; maintenance has no delivery authority."""

    def __init__(
        self,
        db,
        outbox,
        *,
        subject_runtime=None,
        parent_subject_runtime=None,
        development_subject_runtime=None,
        maintenance=None,
        interval_seconds=5.0,
        source_timeout_seconds=DEFAULT_SOURCE_TIMEOUT_SECONDS,
        item_timeout_seconds=DEFAULT_ITEM_TIMEOUT_SECONDS,
        source_timeouts=None,
        clock=time.time,
    ):
        if interval_seconds <= 0:
            raise ValueError("integration interval must be positive")
        for value in (
            source_timeout_seconds,
            item_timeout_seconds,
            *(source_timeouts or {}).values(),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or value <= 0
            ):
                raise ValueError("integration timeout must be positive")
        self._db, self._outbox = db, outbox
        self._subject_runtime = subject_runtime
        self._parent_subject_runtime = parent_subject_runtime
        self._development_subject_runtime = development_subject_runtime
        self._maintenance = dict(maintenance or {})
        self._source_timeout_seconds = float(source_timeout_seconds)
        self._item_timeout_seconds = float(item_timeout_seconds)
        self._source_timeouts = dict(source_timeouts or {})
        self._interval_seconds, self._clock = interval_seconds, clock
        self._tick_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._task = self._reconciliation_task = None
        self._unwinding = {}
        self._skip_reported = set()

    async def tick(self, now, *, background=False):
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            if not self._stop_event.is_set() and (
                self._reconciliation_task is None or self._reconciliation_task.done()
            ):
                self._reconciliation_task = asyncio.create_task(
                    self._reconcile(), name="integration-subject-pass"
                )
            if not background and self._reconciliation_task is not None:
                await self._reconciliation_task
            await self._source(
                "integration outbox", self._outbox.dispatch_due, self._clock()
            )

    async def _reconcile(self):
        for name, runtime in (
            ("root subjects", self._subject_runtime),
            ("parent subjects", self._parent_subject_runtime),
            ("development subjects", self._development_subject_runtime),
        ):
            if runtime is not None:
                await self._source(name, runtime.tick, self._clock())
        for name, callback in self._maintenance.items():
            await self._source(name, callback, self._clock())

    def _source_budget(self, name: str) -> float:
        return self._source_timeouts.get(name, self._source_timeout_seconds)

    def _still_unwinding(self, name: str) -> bool:
        tasks = self._unwinding.get(name)
        if tasks:
            tasks.difference_update({task for task in tasks if task.done()})
        if tasks:
            if name not in self._skip_reported:
                self._skip_reported.add(name)
                logger.warning(
                    "integration source=%s skipped until its timed-out call finishes unwinding",
                    name,
                )
            return True
        self._unwinding.pop(name, None)
        self._skip_reported.discard(name)
        return False

    async def _bounded(
        self, name: str, budget: float, callback: Callable, *args, **kwargs
    ) -> Any:
        """Await ``callback`` in its own task for at most ``budget`` seconds.

        The callback's own result, exception, or cancellation is returned or
        re-raised unchanged.  Past the budget it is cancelled and given
        :data:`CANCEL_GRACE_SECONDS` to unwind; one that is still running is
        tracked under ``name`` so the source is not started again until it
        has finished, and :class:`_CallTimedOut` is raised either way.
        """
        task = asyncio.ensure_future(callback(*args, **kwargs))
        try:
            done, _pending = await asyncio.wait((task,), timeout=budget)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if done:
            return task.result()
        task.cancel()
        try:
            await asyncio.wait((task,), timeout=CANCEL_GRACE_SECONDS)
        finally:
            if task.done():
                self._settle_abandoned(name, task)
            else:
                self._unwinding.setdefault(name, set()).add(task)
                task.add_done_callback(
                    lambda done_task: self._settle_abandoned(name, done_task)
                )
        raise _CallTimedOut(budget)

    def _settle_abandoned(self, name: str, task: asyncio.Task[Any]) -> None:
        """Retrieve a timed-out callback's end so it is reported, not leaked."""
        tasks = self._unwinding.get(name)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._unwinding.pop(name, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(
                "integration %s callback failed while unwinding its timeout: %r",
                name,
                exc,
            )

    async def _source(self, name: str, callback: Callable, *args, **kwargs) -> Any:
        if self._still_unwinding(name):
            return None
        started = self._clock()
        try:
            return await self._bounded(
                name, self._source_budget(name), callback, *args, **kwargs
            )
        except asyncio.CancelledError:
            raise
        except _CallTimedOut as exc:
            logger.warning(
                "integration source=%s exceeded its %.0fs budget; cancelled, later sources "
                "continue and its work remains retryable",
                name,
                exc.budget,
            )
            return None
        except Exception:
            logger.exception("integration %s source failed and remains retryable", name)
            return None
        finally:
            elapsed = self._clock() - started
            if elapsed > self._interval_seconds:
                logger.warning(
                    "integration slow source=%s elapsed=%.3fs", name, elapsed
                )

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(), name="integration-reconciliation-service"
        )

    async def stop(self) -> None:
        task = self._task
        self._stop_event.set()
        if self._subject_runtime is not None:
            await self._subject_runtime.stop()
        if self._parent_subject_runtime is not None:
            await self._parent_subject_runtime.stop()
        if self._development_subject_runtime is not None:
            await self._development_subject_runtime.stop()
        if self._reconciliation_task is not None:
            self._reconciliation_task.cancel()
            await asyncio.gather(self._reconciliation_task, return_exceptions=True)
            self._reconciliation_task = None
        # Timed-out calls that ignored cancellation once get one more request;
        # they are not awaited, so a stuck one cannot hold shutdown either.
        for abandoned in [late for tasks in self._unwinding.values() for late in tasks]:
            abandoned.cancel()
        if task is not None:
            await task
        self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick(self._clock(), background=True)
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
