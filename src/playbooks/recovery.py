"""Bounded recovery of V2 runs whose driver died with the previous daemon."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.git.manager import detached_commit_context
from src.playbooks.engine import InterruptedByRestart, PlaybookEngine

logger = logging.getLogger(__name__)
DEFAULT_RECOVERY_CONCURRENCY = 10


class RestartReconciler:
    """Scan one keyset page per cycle; never await an executor in the cycle.

    This is a single-daemon service. A fixed process boundary identifies old
    drivers, while the shared engine registry protects a run taken over by a
    current event/operator resume. The engine rechecks eligibility at resume.
    Paused runs already have durable owners and are not restart candidates.
    """

    def __init__(
        self,
        engine: PlaybookEngine,
        runs: Any,
        principal: Any,
        *,
        process_started_at: float,
        concurrency: int = DEFAULT_RECOVERY_CONCURRENCY,
    ) -> None:
        self.engine = engine
        self.process_started_at = process_started_at
        self._runs = runs
        self._principal = principal
        self._concurrency = max(1, min(concurrency, 100))
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._cursor: tuple[float, str] | None = None
        self._scan_lock = asyncio.Lock()
        self._closed = False

    @property
    def active_run_ids(self) -> frozenset[str]:
        return self.engine.active_run_ids.union(self._tasks)

    async def tick(self) -> tuple[str, ...]:
        scan = getattr(self._runs, "list_interrupted_runs", None)
        if not callable(scan) or self._closed:
            return ()
        async with self._scan_lock:
            capacity = self._concurrency - len(self._tasks)
            if capacity <= 0 or self._closed:
                return ()
            rows = await scan(
                updated_before=self.process_started_at,
                after=self._cursor,
                exclude_ids=self.active_run_ids,
                limit=capacity,
            )
            # Advance even when a resume fails. After the last page, start a
            # new pass so failed reads can recover when their dependency does.
            self._cursor = rows[-1].cursor if len(rows) == capacity else None
            if self._closed:
                return ()
            scheduled: list[str] = []
            for row in rows:
                if row.run_id in self.active_run_ids:
                    continue
                task = asyncio.create_task(
                    self._resume(row.run_id),
                    name=f"playbook-v2-restart:{row.run_id}",
                    context=detached_commit_context(),
                )
                self._tasks[row.run_id] = task
                task.add_done_callback(
                    lambda done, run_id=row.run_id: self._done(run_id, done)
                )
                scheduled.append(row.run_id)
            return tuple(scheduled)

    def _done(self, run_id: str, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)

    async def _resume(self, run_id: str) -> None:
        try:
            outcome = await self.engine.resume(
                run_id, InterruptedByRestart(self.process_started_at), self._principal
            )
            logger.info(
                "V2 restart recovery run=%s lifecycle=%s outcome=%s",
                run_id, outcome.lifecycle.value, outcome.outcome,
            )
        except Exception:
            # A bad artifact or repository read never stalls sibling runs.
            logger.exception("V2 restart recovery could not resume run %s", run_id)

    async def shutdown(self) -> None:
        self._closed = True
        async with self._scan_lock:
            tasks = tuple(self._tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.clear()
