"""Once-per-local-day ``report.morning_due`` for the reviewed morning-report playbook.

The playbook used to trigger on ``timer.1m``.  Every minute event carries a
fresh ``event_id``, so the engine's dispatch dedup never matched and each
minute started a new run: ~1,000-1,300 runs a day for one report.

This emitter is the mechanism half of the fix.  It fires once the zoned
planned instant of the local day has passed, and its ``event_id`` is keyed by
schedule and local date.  The engine derives the dispatch id from the event
id, so a re-emission after a restart deduplicates against the day's run
instead of starting another one.  The playbook (the policy half) loops on a
timer wait inside that one run until the day's report settles.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.reports.schedule import SCHEDULE_ID, planned_at

logger = logging.getLogger(__name__)

EVENT_TYPE = "report.morning_due"


def morning_due_event_id(local_date: str) -> str:
    """The once-per-local-day idempotency key."""
    return f"{EVENT_TYPE}:{SCHEDULE_ID}:{local_date}"


class MorningDueEmitter:
    """Emit ``report.morning_due`` at most once per process per local day.

    Emission does not depend on ``reports.morning.enabled``: the tick the
    playbook runs is what cancels pending rows after a disable.  Invalid
    ``reports`` config emits nothing; the tick would only refuse it.
    """

    def __init__(
        self,
        config: Callable[[], Any],
        event_bus: Any,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._bus = event_bus
        self._clock = clock
        self._emitted: str | None = None

    async def tick(self) -> str | None:
        """Return the local date emitted on this call, if any.  Never raises."""
        try:
            reports = self._config().reports
            if reports.validate():
                return None
            now = self._clock()
            day = datetime.fromtimestamp(now, ZoneInfo(reports.timezone)).date()
            local_date = day.isoformat()
            if local_date == self._emitted:
                return None
            if now < planned_at(day, reports.morning.time, reports.timezone):
                return None
            await self._bus.emit(
                EVENT_TYPE,
                {
                    "event_id": morning_due_event_id(local_date),
                    "local_date": local_date,
                    "report_id": f"morning-{local_date}",
                },
            )
        except Exception:
            logger.warning("report.morning_due emit failed; next cycle retries", exc_info=True)
            return None
        self._emitted = local_date
        return local_date
