"""Bounded responses for commands whose work can outlast a client's request timeout.

The CLI waits 30 seconds for most commands. A control that may need minutes
(an origin fetch, a recovery replay) runs here as a detached task: the caller
waits up to ``response_seconds`` and then gets a poll handle instead of a read
timeout with an unknown outcome. The work keeps running; repeating the same
request joins the run in flight, or collects its result once it has finished.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

#: Leaves headroom under the CLI's 30s default request timeout.
RESPONSE_SECONDS = 20.0
#: How long an uncollected finished result stays available to a poll.
RETAIN_SECONDS = 900.0


@dataclass
class _Run:
    operation_id: str
    started_at: float
    task: asyncio.Task


@dataclass
class DetachedOperations:
    response_seconds: float = RESPONSE_SECONDS
    retain_seconds: float = RETAIN_SECONDS
    clock: Callable[[], float] = time.monotonic
    _runs: dict[str, _Run] = field(default_factory=dict)

    async def run(
        self, key: str, factory: Callable[[], Awaitable[dict[str, Any]]], *, poll: str,
    ) -> dict[str, Any]:
        """Return ``factory()``'s result, or a ``running`` handle past the budget."""
        self._prune()
        run = self._runs.get(key)
        if run is None:
            run = _Run(uuid.uuid4().hex[:12], self.clock(), asyncio.create_task(factory()))
            self._runs[key] = run
        try:
            return await asyncio.wait_for(asyncio.shield(run.task), self.response_seconds)
        except TimeoutError:
            elapsed = round(self.clock() - run.started_at, 1)
            return {
                "success": True,
                "outcome": "running",
                "operation_id": run.operation_id,
                "elapsed_seconds": elapsed,
                "poll": poll,
                "message": (
                    f"still running after {elapsed}s; it continues in the daemon. "
                    f"Repeat `{poll}` to wait for or collect its result."
                ),
            }
        finally:
            if run.task.done() and self._runs.get(key) is run:
                del self._runs[key]

    def _prune(self) -> None:
        cutoff = self.clock() - self.retain_seconds
        for key, run in list(self._runs.items()):
            if run.task.done() and run.started_at < cutoff:
                del self._runs[key]
                with contextlib.suppress(asyncio.CancelledError):
                    run.task.exception()  # retrieved: no "never retrieved" warning


def detached_operations(owner: Any) -> DetachedOperations:
    """The registry shared by every command handler of one daemon ``owner``."""
    registry = vars(owner).get("_detached_operations")
    if not isinstance(registry, DetachedOperations):
        registry = vars(owner)["_detached_operations"] = DetachedOperations()
    return registry
