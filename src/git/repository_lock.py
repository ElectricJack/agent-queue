"""Repository exclusion with a caller-scoped acquisition bound."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar


_WAIT_LIMIT: ContextVar[float | None] = ContextVar("repository_lock_wait_limit", default=None)


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

    async def acquire(self) -> bool:
        seconds = _WAIT_LIMIT.get()
        if seconds is None:
            return await super().acquire()
        scope = asyncio.timeout(seconds)
        try:
            async with scope:
                return await super().acquire()
        except TimeoutError as exc:
            if scope.expired():
                raise RepositoryLockTimeout(seconds) from exc
            raise
