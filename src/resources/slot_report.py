"""How long a test run spent *queued* for a slot, as reported by ``aq test``.

A caller that bounds a test command's duration — the development publisher
runs its selected validation under ``timeout_seconds`` — cannot see from the
outside how much of that time the command spent waiting in
:class:`~src.resources.semaphore.SlotSemaphore` rather than running tests.
On a busy box that wait is minutes, and charging it to the run turned a
passing batch into a "validation failure" (2026-09-24: 160 s of slot wait
plus a 209 s run against a 300 s budget).

So the caller sets :data:`REPORT_ENV` to a file path and ``aq test`` appends
one JSON line per slot event to it:

``waiting``       ``at`` — the moment the wait began
``acquired``      ``at``, ``waited`` (seconds queued), ``slot``
``slot_timeout``  ``at``, ``waited`` — no slot came free; ``aq test`` exits 75
``released``      ``at``

Several ``aq test`` invocations inside one command append to the same file,
and their waits add up.  :data:`WAIT_TIMEOUT_ENV` lets the same caller bound
the wait itself.  Nothing here is required: a command that never runs
``aq test`` writes no report, and :func:`read_slot_wait` then says no time
was spent queued.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "REPORT_ENV",
    "WAIT_TIMEOUT_ENV",
    "SlotWait",
    "append_event",
    "read_slot_wait",
]

#: Path ``aq test`` appends its slot events to, when set.
REPORT_ENV = "AQ_TEST_SLOT_REPORT"
#: Seconds ``aq test`` may wait for a slot, when set (``--aq-timeout`` wins).
WAIT_TIMEOUT_ENV = "AQ_TEST_WAIT_TIMEOUT"


@dataclass(frozen=True)
class SlotWait:
    """Queueing observed so far in one report."""

    #: Every completed wait plus the one still open.
    total_seconds: float = 0.0
    #: The wait still open at *now*; ``0`` when nothing is queued.
    waiting_seconds: float = 0.0
    #: How many slots were taken.
    acquired: int = 0
    #: Whether an ``aq test`` gave up waiting.
    timed_out: bool = False


def append_event(path: str | os.PathLike[str], event: str, **fields: object) -> None:
    """Append one event line.  Never raises: the report is advisory."""
    record = {"event": event, "at": time.time(), **fields}
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        logger.debug("could not append test-slot event to %s", path)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def read_slot_wait(path: str | os.PathLike[str], *, now: float | None = None) -> SlotWait:
    """Sum the queueing a report records up to *now*.

    Unparseable lines — a write still in flight, or anything that is not one
    of our events — are skipped rather than trusted.
    """
    now = time.time() if now is None else now
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return SlotWait()
    total, acquired, timed_out = 0.0, 0, False
    waiting_since: float | None = None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        event = record.get("event")
        if event == "waiting":
            waiting_since = _number(record.get("at"))
        elif event in ("acquired", "slot_timeout"):
            waited = _number(record.get("waited"))
            if waited is None:
                continue
            total += max(0.0, waited)
            waiting_since = None
            if event == "acquired":
                acquired += 1
            else:
                timed_out = True
    open_wait = max(0.0, now - waiting_since) if waiting_since is not None else 0.0
    return SlotWait(
        total_seconds=total + open_wait,
        waiting_seconds=open_wait,
        acquired=acquired,
        timed_out=timed_out,
    )
