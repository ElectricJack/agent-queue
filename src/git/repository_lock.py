"""Repository exclusion with a caller-scoped acquisition bound."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar


_WAIT_LIMIT: ContextVar[float | None] = ContextVar("repository_lock_wait_limit", default=None)
logger = logging.getLogger(__name__)


class RepositoryLockTimeout(TimeoutError):
    """Repository exclusion was unavailable; no protected operation started."""

    def __init__(self, seconds: float):
        super().__init__(f"repository lock busy after {seconds:g}s; retry after cooldown")


@contextmanager
def repository_lock_wait_limit(seconds: float):
    """Bound acquisition only, leaving the owner and protected work untouched."""
    previous = _WAIT_LIMIT.get()
    token = _WAIT_LIMIT.set(seconds if previous is None else min(previous, seconds))
    try:
        yield
    finally:
        _WAIT_LIMIT.reset(token)


class RepositoryLock(asyncio.Lock):
    """The usual exclusive mutex, with bounded waits for opted-in callers."""

    def __init__(self):
        super().__init__()
        self._owner: asyncio.Task | None = None
        self._acquired_at: float | None = None

    async def acquire(self) -> bool:
        seconds = _WAIT_LIMIT.get()
        if seconds is None:
            acquired = await super().acquire()
        else:
            scope = asyncio.timeout(seconds)
            try:
                async with scope:
                    acquired = await super().acquire()
            except TimeoutError as exc:
                if scope.expired():
                    self._report_owner(seconds)
                    raise RepositoryLockTimeout(seconds) from exc
                raise
        self._owner = asyncio.current_task()
        self._acquired_at = time.monotonic()
        return acquired

    def release(self) -> None:
        super().release()
        self._owner = None
        self._acquired_at = None

    def _report_owner(self, seconds: float) -> None:
        # Coroutine names identify the suspended holder without exposing
        # arguments, locals, task names or worker-controlled repository data.
        current = self._owner.get_coro() if self._owner is not None else None
        chain = []
        seen = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            code = getattr(current, "cr_code", None) or getattr(current, "gi_code", None)
            if code is not None:
                chain.append(code.co_qualname)
            current = getattr(current, "cr_await", None) or getattr(current, "gi_yieldfrom", None)
        held = time.monotonic() - self._acquired_at if self._acquired_at is not None else 0.0
        logger.warning(
            "repository lock acquisition timed out: wait_seconds=%.3f held_seconds=%.3f "
            "owner_chain=%s", seconds, held, " -> ".join(chain),
            extra={"wait_seconds": seconds, "held_seconds": held, "owner_chain": chain},
        )
