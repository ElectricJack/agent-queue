"""Bounded in-memory counts of ignored inbound Discord messages, by reason code.

:class:`~src.discord.escalation_intake.DiscordEscalationIntake` logs one INFO
line per message it does not consume, which answers "why did *this* message
vanish?".  This counter answers the aggregate question — what is the gateway
ignoring, and how much of it — and ``digest_status`` reports its snapshot as
the ``intake`` block.

It is deliberately small and forgetful: counts cover a sliding window (an hour
by default), at most :data:`MAX_EVENTS` events are held however busy the
channel is (the oldest go first), and nothing survives a restart.  It keeps the
code and a timestamp only, never an id or any content.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Callable
from typing import Any

#: How far back a snapshot counts, in seconds.
WINDOW_SECONDS = 3600.0
#: Most events held at once; beyond it the oldest are forgotten.
MAX_EVENTS = 10_000

#: The ``intake`` block when no gateway is connected to count anything.  Same
#: keys as a live snapshot.  Hand out :func:`empty_snapshot`, never this dict.
EMPTY_SNAPSHOT: dict[str, Any] = {
    "available": False,
    "window_seconds": int(WINDOW_SECONDS),
    "total": 0,
    "ignored": {},
}


def empty_snapshot() -> dict[str, Any]:
    """A fresh copy of :data:`EMPTY_SNAPSHOT` a caller may mutate."""
    return {**EMPTY_SNAPSHOT, "ignored": {}}


class IgnoreCounter:
    """Sliding-window counts of ignore codes, bounded by event count."""

    def __init__(
        self,
        *,
        window_seconds: float = WINDOW_SECONDS,
        max_events: int = MAX_EVENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = float(window_seconds)
        self._clock = clock
        self._events: deque[tuple[float, str]] = deque(maxlen=max_events)

    def record(self, code: str) -> None:
        """Count one ignored message under *code*."""
        self._events.append((self._clock(), str(code)))

    def snapshot(self) -> dict[str, Any]:
        """Counts inside the window, by code, dropping anything older."""
        cutoff = self._clock() - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        counts = Counter(code for _, code in self._events)
        return {
            "available": True,
            "window_seconds": int(self._window),
            "total": sum(counts.values()),
            "ignored": dict(sorted(counts.items())),
        }


__all__ = ["EMPTY_SNAPSHOT", "MAX_EVENTS", "WINDOW_SECONDS", "IgnoreCounter", "empty_snapshot"]
