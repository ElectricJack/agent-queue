"""Wait maintenance off the scheduler cycle's critical path.

The legacy bridge and the scheduler wait audit can take minutes on a large
graph (one recursive stack-authorization query per admission check). The
cycle used to await them inline, ahead of session reconciliation, so a slow
pass stopped dead sessions being reaped and pools being spawned. Here the
work runs as one single-flight background task: the cycle that starts it
waits at most ``budget`` seconds, and later cycles skip while it runs. Every
step it drives resumes from durable receipts, so a pass that overruns simply
continues on the next start.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class CycleWaitMaintenance:
    budget = 5.0
    notice_seconds = 60.0

    def __init__(self, orch):
        self.orch = orch
        self.task: asyncio.Task | None = None
        self.started_at = 0.0
        self._noticed_at = 0.0

    async def tick(self) -> None:
        if self.task is not None and not self.task.done():
            now = time.monotonic()
            if now - self._noticed_at >= self.notice_seconds:
                self._noticed_at = now
                logger.warning("Wait maintenance still running after %.0fs; cycle continues",
                               now - self.started_at)
            return
        self.started_at = time.monotonic()
        self.task = asyncio.create_task(self._run(), name="aq-cycle-waits")
        await asyncio.wait({self.task}, timeout=self.budget)

    async def _run(self) -> None:
        handler = self.orch._command_handler
        try:
            from src.agent_waits import AgentWaitReconciler

            result = await AgentWaitReconciler(handler).tick()
            if not result.get("success"):
                logger.error("AgentWaitReconciler tick refused: %s", result)
        except Exception:
            logger.exception("AgentWaitReconciler tick failed")
        try:
            wait_service = getattr(self.orch, "wait_service", None)
            if wait_service is not None:
                await wait_service.tick()
        except Exception:
            logger.exception("WaitService recovery tick failed")

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
