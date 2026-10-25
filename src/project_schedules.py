"""Project-owned notifications that outlive any worker or supervisor instance."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ScheduleError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utc_instant(value: str) -> float:
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.utcoffset() is None:
            raise ValueError("missing UTC offset")
        return instant.astimezone(timezone.utc).timestamp()
    except (ValueError, OverflowError) as exc:
        raise ScheduleError("schedule.invalid", "due_at must be an offset-aware ISO UTC instant") from exc


def visible_schedule(row) -> dict:
    return {
        **dict(row),
        "due_at_utc": datetime.fromtimestamp(row["due_at"], timezone.utc).isoformat(),
    }


class ProjectScheduleService:
    def __init__(self, handler, *, clock=time.time):
        self.handler = handler
        self.clock = clock

    async def tick(self):
        from src.commands.principal import ExecutionPrincipal, principal_context

        with principal_context(ExecutionPrincipal.service("project-schedules")):
            return await self.handler.execute("reconcile_project_schedules", {"now": self.clock()})

    async def run(self):
        while True:
            try:
                result = await self.tick()
                if not result.get("success"):
                    logger.error("Project schedule reconciliation refused: %s", result)
            except Exception:
                logger.exception("Project schedule reconciliation failed")
            await asyncio.sleep(30)
