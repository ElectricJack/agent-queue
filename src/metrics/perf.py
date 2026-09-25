"""In-process performance counters the sampler folds into each sample.

One registry per process.  The loop probe, the API middleware and the engine
observers write to it; ``MetricsSampler.collect`` snapshots and resets it once
a second (spec 2026-09-24 dashboard performance §4.1).  Nothing here may raise
into a request path, so every observation method is wrapped by
:func:`_never_raises`: a bad argument is dropped, never propagated.

``enabled`` is the one switch every writer shares.  The sampler sets it from
``metrics.perf_enabled`` on each tick; while it is false nothing is recorded,
so turning the probes back on does not replay what happened while they were
off.  Labels are bounded: past :data:`ROUTE_LIMIT` distinct labels a new one
is folded into :data:`OVERFLOW_LABEL`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from src.metrics.histogram import new_hist, new_sum, observe

logger = logging.getLogger(__name__)

ROUTE_LIMIT = 256
OVERFLOW_LABEL = "other"
STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx")


def _never_raises(method: Callable) -> Callable:
    """Serialize ``method`` on the registry lock and swallow whatever it raises.

    The lock is there for observers that fire off the loop thread (a sync
    engine used from a worker thread); on the loop it is uncontended.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            with self._lock:
                return method(self, *args, **kwargs)
        except Exception:  # instrumentation must never fail a request
            logger.debug(
                "perf registry: %s dropped a bad observation", method.__name__, exc_info=True
            )
            return None

    return wrapper


def _ms(value: Any) -> float:
    """``value`` as a duration; raises so the caller drops the observation."""
    if isinstance(value, bool):
        raise TypeError("a bool is not a duration")
    ms = float(value)
    if not math.isfinite(ms) or ms < 0:
        raise ValueError(f"not a duration: {value!r}")
    return ms


def _status_class(status: Any) -> str | None:
    if isinstance(status, bool):
        return None
    try:
        code = int(status)
    except (TypeError, ValueError):
        return None
    return f"{code // 100}xx" if 200 <= code < 600 else None


def _route_entry() -> dict[str, Any]:
    return {"latency": new_hist(), "status": new_sum()}


def _stream_entry(open_count: int = 0) -> dict[str, Any]:
    return {
        "handshake": new_hist(),
        "outcome": new_sum(accepted=0, refused=0),
        "open": open_count,
    }


class PerfRegistry:
    """Loop drift, route latency, stream handshakes, pool wait and query time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.enabled = True
        self.slow_query_threshold_ms = 100.0
        #: Set by the :class:`LoopLagProbe` that feeds this registry; ``0``
        #: while no probe is attached.
        self.probe_interval_ms = 0
        self._loop = new_hist()
        self._api_all = new_hist()
        self._routes: dict[str, dict[str, Any]] = {}
        self._streams: dict[str, dict[str, Any]] = {}
        self._exceptions = 0
        self._unmatched = 0
        self._pool_wait = new_hist()
        self._query = new_hist()
        self._slow_queries = 0
        self._pool_timeouts = 0

    # -- label bounding -------------------------------------------------------

    @staticmethod
    def _entry(
        table: dict[str, dict[str, Any]], label: Any, factory: Callable[[], dict]
    ) -> dict[str, Any]:
        if not isinstance(label, str):
            raise TypeError("label must be a str")
        entry = table.get(label)
        if entry is None:
            if len(table) >= ROUTE_LIMIT:
                label = OVERFLOW_LABEL
                entry = table.get(label)
            if entry is None:
                entry = table[label] = factory()
        return entry

    # -- observations -----------------------------------------------------------

    @_never_raises
    def observe_loop_drift(self, ms: float) -> None:
        if self.enabled:
            observe(self._loop, ms)

    @_never_raises
    def observe_route(self, label: str, ms: float, status: int | None) -> None:
        if not self.enabled:
            return
        value = _ms(ms)
        entry = self._entry(self._routes, label, _route_entry)
        observe(entry["latency"], value)
        observe(self._api_all, value)
        klass = _status_class(status)
        if klass is not None:
            entry["status"][klass] = entry["status"].get(klass, 0) + 1

    @_never_raises
    def observe_exception(self, label: str) -> None:
        if self.enabled:
            self._exceptions += 1

    @_never_raises
    def observe_unmatched(self) -> None:
        if self.enabled:
            self._unmatched += 1

    @_never_raises
    def observe_stream_handshake(self, label: str, ms: float, *, accepted: bool) -> None:
        if not self.enabled:
            return
        if not isinstance(accepted, bool):
            raise TypeError("accepted must be a bool")
        value = _ms(ms)
        entry = self._entry(self._streams, label, _stream_entry)
        observe(entry["handshake"], value)
        key = "accepted" if accepted else "refused"
        entry["outcome"][key] += 1

    @_never_raises
    def stream_opened(self, label: str) -> None:
        if self.enabled:
            self._entry(self._streams, label, _stream_entry)["open"] += 1

    @_never_raises
    def stream_closed(self, label: str) -> None:
        # Not gated on ``enabled``: a stream opened before the probes were
        # switched off must still leave the gauge when it closes.
        entry = self._streams.get(label)
        if entry is None and len(self._streams) >= ROUTE_LIMIT:
            entry = self._streams.get(OVERFLOW_LABEL)
        if entry is not None and entry["open"] > 0:
            entry["open"] -= 1

    @_never_raises
    def observe_pool_wait(self, ms: float) -> None:
        if self.enabled:
            observe(self._pool_wait, _ms(ms))

    @_never_raises
    def observe_pool_timeout(self) -> None:
        if self.enabled:
            self._pool_timeouts += 1

    @_never_raises
    def observe_query(self, ms: float) -> None:
        if not self.enabled:
            return
        value = _ms(ms)
        observe(self._query, value)
        if value > self.slow_query_threshold_ms:
            self._slow_queries += 1

    # -- snapshot -----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The ``loop``/``api``/``db`` fragments since the last snapshot.

        Histograms and counters restart from zero; the ``open`` stream gauges
        carry over, because a stream that is still open is still open.
        """
        with self._lock:
            out = {
                "loop": {"drift": self._loop, "probe_interval_ms": self.probe_interval_ms},
                "api": {
                    "all": self._api_all,
                    "routes": self._routes,
                    "streams": self._streams,
                    "errors": new_sum(exceptions=self._exceptions, unmatched=self._unmatched),
                },
                "db": {
                    "pool_wait": self._pool_wait,
                    "query": self._query,
                    "counters": new_sum(
                        slow_queries=self._slow_queries, pool_timeouts=self._pool_timeouts
                    ),
                },
            }
            self._loop = new_hist()
            self._api_all = new_hist()
            self._routes = {}
            self._streams = {
                label: _stream_entry(entry["open"]) for label, entry in self._streams.items()
            }
            self._exceptions = self._unmatched = 0
            self._pool_wait = new_hist()
            self._query = new_hist()
            self._slow_queries = self._pool_timeouts = 0
        return out


class LoopLagProbe:
    """Sleep ``interval_ms``, measure how late the wake-up was, record it.

    ``expected`` is re-anchored after every wake-up, so one stall is counted
    once rather than smeared over the samples that follow it.
    """

    def __init__(
        self,
        registry: PerfRegistry,
        *,
        interval_ms: int = 100,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry
        self.interval_ms = int(interval_ms)
        self._sleep = sleep
        self._clock = clock
        self._task: asyncio.Task | None = None
        registry.probe_interval_ms = self.interval_ms

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="aq-loop-lag-probe")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def run(self) -> None:
        interval = self.interval_ms / 1000.0
        while True:
            expected = self._clock() + interval
            await self._sleep(interval)
            drift_ms = max(0.0, (self._clock() - expected) * 1000.0)
            self.registry.observe_loop_drift(drift_ms)


_REGISTRY: PerfRegistry | None = None


def perf_registry() -> PerfRegistry:
    """The process-wide registry, created on first use."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = PerfRegistry()
    return _REGISTRY


def install_registry(registry: PerfRegistry | None) -> None:
    """Replace the process-wide registry; ``None`` makes the next call build a fresh one."""
    global _REGISTRY
    _REGISTRY = registry
