"""Cumulative in-memory relay counters for independent HTTP readers.

Only durations and bounded failure names enter these counters; request URLs,
headers and bodies never do. Instrumentation must never fail a relay.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from typing import Any

from src.metrics.histogram import new_hist, observe

FAILURES = (
    "daemon_unreachable", "daemon_timeout", "daemon_bad_response", "dashboard_server_stopping",
)


class RelayStats:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self.epoch = clock()
        self.http = new_hist()
        self.ws_handshake = new_hist()
        self.upstream_failures = dict.fromkeys(FAILURES, 0)
        self.open_relays: Callable[[], int] = lambda: 0

    def observe_http(self, ms: float) -> None:
        """Time to upstream HTTP headers, including SSE headers."""
        with contextlib.suppress(Exception):
            observe(self.http, ms)

    def observe_ws_handshake(self, ms: float) -> None:
        with contextlib.suppress(Exception):
            observe(self.ws_handshake, ms)

    def failure(self, error: str) -> None:
        with contextlib.suppress(Exception):
            if error in self.upstream_failures:
                self.upstream_failures[error] += 1

    def snapshot(self) -> dict[str, Any]:
        """Copy counters without draining them or sharing mutable buckets."""
        return {
            "epoch": self.epoch,
            "now": self._clock(),
            "http": {**self.http, "counts": list(self.http["counts"])},
            "ws_handshake": {**self.ws_handshake, "counts": list(self.ws_handshake["counts"])},
            "upstream_failures": dict(self.upstream_failures),
            "relays_open": int(self.open_relays()),
        }
