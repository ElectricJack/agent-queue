"""Background health collection; probes never wait on the database or scheduler."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


class HealthMonitor:
    """One bounded collector and an inexpensive, fail-closed snapshot read."""

    def __init__(
        self, provider: Callable[[], Awaitable[dict[str, Any]]], *,
        interval: float = 5.0, timeout: float = 2.0, max_age: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.interval = interval
        self.timeout = timeout
        self.max_age = max_age
        self._clock = clock
        self._updated_at: float | None = None
        self._checks: dict[str, Any] = {}
        self._reason: str | None = "initializing"
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="aq-health-monitor")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def checks(self) -> dict[str, Any]:
        age = None if self._updated_at is None else max(0.0, self._clock() - self._updated_at)
        reason = "stale" if age is not None and age > self.max_age else self._reason
        metadata = {"ok": reason is None, "age_seconds": None if age is None else round(age, 2)}
        if reason is not None:
            metadata["reason"] = reason
        return {**self._checks, "health_snapshot": metadata}

    async def refresh(self) -> None:
        """Collect outside any HTTP request; failed collections fail closed."""
        try:
            checks = await asyncio.wait_for(self.provider(), timeout=self.timeout)
            if not isinstance(checks, dict):
                raise TypeError("health provider must return a dict")
        except asyncio.TimeoutError:
            self._checks = {"_provider_error": {"ok": False, "error": "provider timed out"}}
            self._reason = "timeout"
        except Exception:
            logger.exception("Health provider raised an exception")
            self._checks = {"_provider_error": {"ok": False, "error": "provider failed"}}
            self._reason = "provider_failed"
        else:
            self._checks = checks
            self._reason = None
        self._updated_at = self._clock()

    async def _run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.interval)
