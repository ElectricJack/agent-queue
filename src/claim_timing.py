"""Request-local claim diagnostics, inherited by asynchronous preparation.

No command text or repository credentials are recorded. Durations use a
monotonic clock; nested phase totals are inclusive and must not be summed.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps

logger = logging.getLogger(__name__)


@dataclass
class ClaimTiming:
    session_id: str
    started: float = field(default_factory=time.monotonic)
    task_id: str | None = None
    deadline: float | None = None
    claim_epoch: int | None = None
    phases: dict[str, float] = field(default_factory=lambda: dict.fromkeys(
        ("frontier_select", "owner_lease", "fetch", "checkout", "reset", "setup"), 0.0,
    ))
    running: list[tuple[str, float]] = field(default_factory=list)
    result: str | None = None
    finished: float | None = None

    def snapshot(self) -> dict:
        now = self.finished if self.finished is not None else time.monotonic()
        phases = dict(self.phases)
        for phase, started in self.running:
            phases[phase] = phases.get(phase, 0.0) + now - started
        return {
            "session_id": self.session_id,
            "claim_epoch": self.claim_epoch,
            "total_seconds": round(now - self.started, 4),
            "phases": {name: round(seconds, 4) for name, seconds in phases.items()},
            "current_phase": self.running[-1][0] if self.running else None,
            "result": self.result,
        }

    def finish(self, result: str) -> None:
        self.result = result
        self.finished = time.monotonic()
        logger.info("pool.claim_timing session=%s task=%s epoch=%s timing=%s",
                    self.session_id, self.task_id, self.claim_epoch, self.snapshot())


current_claim_timing: ContextVar[ClaimTiming | None] = ContextVar("claim_timing", default=None)


@contextmanager
def claim_phase(name: str):
    timing = current_claim_timing.get()
    if timing is None:
        yield
        return
    started = time.monotonic()
    entry = (name, started)
    timing.running.append(entry)
    logger.info("pool.claim_phase session=%s task=%s epoch=%s phase=%s started",
                timing.session_id, timing.task_id, timing.claim_epoch, name)
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        timing.running.remove(entry)
        timing.phases[name] = timing.phases.get(name, 0.0) + elapsed
        logger.info("pool.claim_phase session=%s task=%s epoch=%s phase=%s seconds=%.4f",
                    timing.session_id, timing.task_id, timing.claim_epoch, name, elapsed)


def timed_claim_phase(name):
    def decorate(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            with claim_phase(name):
                return await fn(*args, **kwargs)
        return wrapped
    return decorate


def timed_claim_git(fn):
    @wraps(fn)
    async def wrapped(self, args, *rest, **kwargs):
        phase = {
            "fetch": "fetch", "switch": "checkout", "checkout": "checkout",
            "reset": "reset", "clean": "reset",
        }.get(args[0] if args else "", "git_checks")
        with claim_phase(phase):
            return await fn(self, args, *rest, **kwargs)
    return wrapped


@asynccontextmanager
async def claim_selection(db, phase="frontier_select"):
    with claim_phase(phase):
        async with db.immediate() as conn:
            yield conn
