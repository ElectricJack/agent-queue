"""Context-local train diagnostics; metrics never participate in admission."""

from __future__ import annotations

import time
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar


class SelectionMetrics:
    """Inclusive operation times and counts, also readable during a slow visit."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.stages: dict[str, dict] = {}
        self.active: dict[object, tuple[str, float]] = {}

    def as_dict(self) -> dict:
        stages = {name: dict(stage) for name, stage in self.stages.items()}
        now = time.monotonic()
        for name, started in self.active.values():
            stages[name]["seconds"] += now - started
        return {"counts": dict(self.counts), "stages": stages}


_METRICS: ContextVar[SelectionMetrics | None] = ContextVar("train_selection_metrics", default=None)


@contextmanager
def selection_metrics_scope(metrics: SelectionMetrics):
    token = _METRICS.set(metrics)
    try:
        yield metrics
    finally:
        _METRICS.reset(token)


def selection_count(name: str, count: int = 1) -> None:
    if (metrics := _METRICS.get()) is not None:
        metrics.counts[name] += count


@contextmanager
def selection_stage(name: str, *, items: int = 0):
    metrics = _METRICS.get()
    if metrics is None:
        yield
        return
    stage = metrics.stages.setdefault(name, {"calls": 0, "items": 0, "seconds": 0.0})
    stage["calls"] += 1
    stage["items"] += items
    key, started = object(), time.monotonic()
    metrics.active[key] = name, started
    try:
        yield
    finally:
        stage["seconds"] += time.monotonic() - started
        del metrics.active[key]
