"""Stage work a timed-out train visit hands to its lane's next visit.

Every train visit is bounded so one target cannot hold a repository slot. Some
stages are deterministic work whose result does not depend on the visit that
began it: a retained-store fetch, or a candidate assembled from frozen inputs.
Cancelling them at the visit deadline discards the work, so a target whose
stages together exceed the budget times out on every visit and never advances.

A carried stage keeps running past its visit's deadline, up to its own absolute
deadline. The lane's next visit with the same stage identity adopts the running
task or its result. A result is consumed once, and only while it is fresh; a
different identity, the deadline, lane retirement or train stop cancels it. A
lane whose carry is still running holds its repository slot, as its visit did.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from src.integration.selection_metrics import selection_count

#: A carry may outlive this many visit budgets before it is cancelled.
CARRIED_STAGE_VISITS = 3

_CARRIES: ContextVar[StageCarries | None] = ContextVar("train_stage_carries", default=None)


class CarriedStageExpired(Exception):
    """A carried stage reached its own deadline before any visit adopted its result."""


@dataclass
class _Carry:
    identity: Hashable
    task: asyncio.Task
    finished_at: float | None = None


class StageCarries:
    """One lane's carried stages, by stage name."""

    def __init__(self, *, limit_seconds: float, fresh_seconds: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit_seconds, self.fresh_seconds, self.clock = limit_seconds, fresh_seconds, clock
        self._stages: dict[str, _Carry] = {}

    def live(self) -> tuple[str, ...]:
        """The stages still running, which hold the lane's repository slot."""
        return tuple(sorted(stage for stage, item in self._stages.items()
                            if not item.task.done()))

    def cancel(self) -> list[asyncio.Task]:
        tasks = [item.task for item in self._stages.values() if not item.task.done()]
        for task in tasks:
            task.cancel()
        self._stages.clear()
        return tasks

    async def run[T](self, stage: str, identity: Hashable,
                     factory: Callable[[], Awaitable[T]]) -> T:
        item = self._stages.get(stage)
        if item is not None and (item.identity != identity or item.task.done() and (
                item.task.cancelled() or item.finished_at is None
                or self.clock() - item.finished_at > self.fresh_seconds)):
            # The inputs changed, or the result is too old to stand in for
            # this visit's own observation: start over, but only once the old
            # run has settled, so two runs of one stage never overlap.
            item.task.cancel()
            await asyncio.wait({item.task})
            self._drop(stage, item)
            item = None
        if item is None:
            item = _Carry(identity, asyncio.create_task(
                self._bounded(stage, factory), name=f"integration-train-carry:{stage}"))
            item.task.add_done_callback(lambda _done, item=item: self._finished(item))
            self._stages[stage] = item
        else:
            selection_count(f"carried_{stage}_adopted")
        try:
            result = await asyncio.shield(item.task)
        except asyncio.CancelledError:
            # The visit's deadline: the carry continues for the next visit.
            if item.task.cancelled():
                self._drop(stage, item)
            raise
        except BaseException:
            self._drop(stage, item)
            raise
        self._drop(stage, item)
        return result

    async def _bounded[T](self, stage: str, factory: Callable[[], Awaitable[T]]) -> T:
        # The task's copied context keeps the starting visit's metrics, never
        # an adopting visit's; the carry runs its own stages directly.
        _CARRIES.set(None)
        try:
            async with asyncio.timeout(self.limit_seconds):
                return await factory()
        except TimeoutError as exc:
            raise CarriedStageExpired(
                f"carried {stage} exceeded {self.limit_seconds:g}s") from exc

    def _finished(self, item: _Carry) -> None:
        item.finished_at = self.clock()
        if not item.task.cancelled():
            item.task.exception()  # retrieved here, re-raised to the adopting visit

    def _drop(self, stage: str, item: _Carry) -> None:
        if self._stages.get(stage) is item:
            del self._stages[stage]


@contextmanager
def stage_carries(carries: StageCarries | None):
    token = _CARRIES.set(carries)
    try:
        yield carries
    finally:
        _CARRIES.reset(token)


async def carried[T](stage: str, identity: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
    """Run *factory* as a carried stage of the current train visit, if there is one."""
    carries = _CARRIES.get()
    if carries is None:
        return await factory()
    return await carries.run(stage, identity, factory)


def carried_stages() -> tuple[str, ...]:
    """The current visit's lane stages still running in the background."""
    carries = _CARRIES.get()
    return carries.live() if carries is not None else ()
