"""Request-local inclusive timings, independent of the resetting fleet sampler.

Only fixed operation labels and repository identities enter the profile. Durations
overlap: a shared-observation wait can include its producer's Git/SQL work.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

_PROFILE: ContextVar[OperationProfile | None] = ContextVar("operation_profile", default=None)


class OperationProfile:
    def __init__(self):
        self.operations = {}
        self.repositories = set()
        self.closed = False

    def record(self, label, seconds):
        if self.closed:
            return
        entry = self.operations.setdefault(label, {"calls": 0, "seconds": 0.0})
        entry["calls"] += 1
        entry["seconds"] += seconds

    def as_dict(self):
        return {
            "operations": {k: dict(v) for k, v in self.operations.items()},
            "repositories": [list(key) for key in sorted(self.repositories)],
        }


def current_profile():
    return _PROFILE.get()


def profile_duration(label, seconds):
    if (profile := _PROFILE.get()) is not None:
        profile.record(label, seconds)


def profile_repository(project_id, repository_id):
    if (profile := _PROFILE.get()) is not None and not profile.closed:
        profile.repositories.add((project_id, repository_id))


@contextmanager
def operation_profile():
    profile = OperationProfile()
    token = _PROFILE.set(profile)
    try:
        yield profile
    finally:
        # Detached producers inherit the context, but must not mutate a finished
        # phase or have their later work attributed to the next phase.
        profile.closed = True
        _PROFILE.reset(token)


@contextmanager
def profile_timing(label):
    profile = _PROFILE.get()
    if profile is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        profile.record(label, time.perf_counter() - started)


@asynccontextmanager
async def profiled_lock(lock, label):
    with profile_timing(label):
        await lock.acquire()
    try:
        yield
    finally:
        lock.release()
