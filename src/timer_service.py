"""Lightweight timer service that emits synthetic ``timer.*`` events.

Scans compiled playbooks for triggers matching the ``timer.{interval}`` pattern,
tracks only intervals that have at least one active subscriber, and emits the
corresponding event when the interval elapses.

**Spec reference:** ``docs/specs/design/playbooks.md`` Section 7 — Timer Service.

Timer events carry ``project_id: null`` — they are inherently system-scoped.
A project-scoped playbook can trigger on a timer, but it fires once globally,
not once per project.

Payload format::

    {
        "tick_time": "2026-04-09T12:00:00+00:00",
        "interval": "30m"
    }

Integration:

- Created and started in ``Orchestrator.initialize()`` after the
  :class:`~src.playbooks.manager.PlaybookManager` loads compiled playbooks.
- ``tick()`` is called every orchestrator cycle (~5 seconds) in the
  housekeeping phase.
- Automatically rebuilds when playbooks are compiled or removed (subscribed
  to ``notify.playbook_compilation_succeeded`` and listens for trigger map
  changes via the :class:`PlaybookManager`).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.event_bus import EventBus
    from src.playbooks.manager import PlaybookManager

logger = logging.getLogger(__name__)

# Pattern: ``timer.{number}{unit}`` where unit is m (minutes) or h (hours).
# Captures the numeric part and the unit separately.
_TIMER_TRIGGER_RE = re.compile(r"^timer\.(\d+)(m|h)$")

# Minimum interval in seconds (1 minute per spec).
_MIN_INTERVAL_SECONDS = 60


def parse_interval(trigger: str) -> float | None:
    """Parse a ``timer.{interval}`` trigger string into seconds.

    Supported formats:
    - ``timer.{N}m`` — N minutes (e.g. ``timer.30m`` → 1800s)
    - ``timer.{N}h`` — N hours (e.g. ``timer.4h`` → 14400s)

    Returns ``None`` if the trigger doesn't match the timer pattern or if
    the resulting interval is below the minimum (1 minute).

    Parameters
    ----------
    trigger:
        The trigger event type string (e.g. ``"timer.30m"``).

    Returns
    -------
    float | None
        Interval in seconds, or ``None`` if not a valid timer trigger.
    """
    match = _TIMER_TRIGGER_RE.match(trigger)
    if match is None:
        return None

    amount = int(match.group(1))
    unit = match.group(2)

    if unit == "m":
        seconds = amount * 60.0
    elif unit == "h":
        seconds = amount * 3600.0
    else:
        return None

    if seconds < _MIN_INTERVAL_SECONDS:
        return None

    return seconds


def extract_timer_intervals(triggers: list[str]) -> dict[str, float]:
    """Extract timer intervals from a list of trigger event types.

    Parameters
    ----------
    triggers:
        List of trigger strings (e.g. ``["git.commit", "timer.30m"]``).

    Returns
    -------
    dict[str, float]
        Mapping from timer trigger string to interval in seconds.
        Only valid timer triggers are included.
    """
    result: dict[str, float] = {}
    for trigger in triggers:
        seconds = parse_interval(trigger)
        if seconds is not None:
            result[trigger] = seconds
    return result


class TimerService:
    """Emits synthetic ``timer.*`` events on the EventBus.

    The service scans the :class:`~src.playbooks.manager.PlaybookManager` for
    compiled playbooks with ``timer.{interval}`` triggers, tracks only
    intervals that have at least one active subscriber, and emits events
    when intervals elapse.

    Parameters
    ----------
    event_bus:
        The :class:`~src.event_bus.EventBus` to emit timer events on.
    playbook_manager:
        The :class:`~src.playbooks.manager.PlaybookManager` to scan for
        timer triggers.
    """

    def __init__(
        self,
        *,
        event_bus: EventBus,
        playbook_manager: PlaybookManager,
    ) -> None:
        self._bus = event_bus
        self._playbook_manager = playbook_manager

        # Active timer intervals: trigger string → interval in seconds.
        # Only intervals with at least one active playbook subscriber.
        self._intervals: dict[str, float] = {}

        # Last fire time for each trigger: trigger string → monotonic timestamp.
        # Starts empty — first fire happens after the interval elapses from
        # when the service starts (or the interval is first registered).
        self._last_fire: dict[str, float] = {}

        # Timestamp when the service was started (or intervals rebuilt).
        # Used as the initial "last fire" for newly added intervals so they
        # don't fire immediately on registration.
        self._start_time: float = 0.0

        # Whether the service is running.
        self._running = False

    @property
    def active_intervals(self) -> dict[str, float]:
        """Return a read-only copy of active intervals (trigger → seconds)."""
        return dict(self._intervals)

    @property
    def interval_count(self) -> int:
        """Return the number of active timer intervals being tracked."""
        return len(self._intervals)

    def start(self) -> None:
        """Start the timer service.

        Scans the playbook manager for timer triggers and begins tracking
        intervals.  Call this after the playbook manager has loaded its
        compiled playbooks.

        On startup all timers are considered overdue and fire on the first
        ``tick()`` call.  Since fire times are not persisted across restarts,
        there is no way to know how much time has passed — so we fire
        immediately rather than making everything wait a full interval.

        .. note::
            ``rebuild()`` called *during* runtime (when playbooks are
            compiled/removed) uses a different strategy: new intervals wait
            one full cycle before first firing, to avoid event storms when
            adding playbooks.
        """
        self._start_time = time.monotonic()
        self._running = True
        self.rebuild()
        # Override _last_fire for all intervals so they fire immediately
        # on the first tick.  rebuild() sets _last_fire = now (wait one
        # cycle), but on startup we treat all timers as overdue.
        for trigger in self._intervals:
            self._last_fire[trigger] = 0.0
        logger.info(
            "Timer service started — tracking %d interval(s): %s",
            len(self._intervals),
            ", ".join(sorted(self._intervals.keys())) or "(none)",
        )

    def stop(self) -> None:
        """Stop the timer service.  No more events will be emitted."""
        self._running = False
        self._intervals.clear()
        self._last_fire.clear()
        logger.info("Timer service stopped")

    def rebuild(self) -> None:
        """Rebuild the set of tracked intervals from the playbook manager.

        Called on startup and whenever playbooks are compiled/removed.
        Preserves existing ``_last_fire`` timestamps for intervals that
        remain active (so they don't re-fire immediately after a rebuild).
        New intervals get the current monotonic time as their initial
        "last fire" so they wait one full interval before first firing.
        """
        # Collect all timer triggers from the playbook manager's trigger map.
        all_triggers = self._playbook_manager.get_all_triggers()
        new_intervals = extract_timer_intervals(all_triggers)

        # Preserve last-fire times for intervals that still exist.
        # New intervals start from "now" so they wait one full cycle.
        now = time.monotonic()
        old_fire = self._last_fire

        self._intervals = new_intervals
        self._last_fire = {}
        for trigger in new_intervals:
            if trigger in old_fire:
                self._last_fire[trigger] = old_fire[trigger]
            else:
                self._last_fire[trigger] = now

        # Log changes
        added = set(new_intervals) - set(old_fire)
        removed = set(old_fire) - set(new_intervals)
        if added:
            logger.info("Timer service: added intervals %s", sorted(added))
        if removed:
            logger.info("Timer service: removed intervals %s", sorted(removed))

    def _check_for_trigger_changes(self) -> None:
        """Compare current intervals against the playbook manager's triggers.

        If the set of timer triggers has changed (playbook compiled, removed,
        or reloaded), triggers a full rebuild.  This is cheap — O(n) where
        n is the number of triggers, typically very small.
        """
        all_triggers = self._playbook_manager.get_all_triggers()
        current_timer_triggers = {t for t in all_triggers if t.startswith("timer.")}
        tracked_triggers = set(self._intervals.keys())

        if current_timer_triggers != tracked_triggers:
            self.rebuild()

    async def tick(self) -> int:
        """Check all tracked intervals and emit events for any that have elapsed.

        Called each orchestrator cycle (~5 seconds).  Automatically rebuilds
        the interval set if the playbook manager's timer triggers have
        changed (e.g. playbook compiled, removed, or reloaded).

        Returns
        -------
        int
            Number of timer events emitted this tick.
        """
        if not self._running:
            return 0

        # Detect trigger changes from the playbook manager (covers compile,
        # remove, store reload — any mutation that changes the trigger map).
        self._check_for_trigger_changes()

        if not self._intervals:
            return 0

        now = time.monotonic()
        tick_time = datetime.now(timezone.utc).isoformat()
        emitted = 0

        for trigger, interval_seconds in self._intervals.items():
            last = self._last_fire.get(trigger, self._start_time)
            elapsed = now - last

            if elapsed >= interval_seconds:
                # Emit the timer event
                await self._bus.emit(
                    trigger,
                    {
                        "tick_time": tick_time,
                        "interval": trigger.removeprefix("timer."),
                    },
                )
                self._last_fire[trigger] = now
                emitted += 1
                logger.debug(
                    "Emitted %s (elapsed=%.1fs, interval=%.0fs)",
                    trigger,
                    elapsed,
                    interval_seconds,
                )

        return emitted

    def time_until_next(self, trigger: str) -> float | None:
        """Return seconds until the next firing of *trigger*, or ``None``.

        Returns ``None`` if the trigger is not being tracked.

        Parameters
        ----------
        trigger:
            The timer trigger string (e.g. ``"timer.30m"``).

        Returns
        -------
        float | None
            Seconds remaining until the next fire (≥ 0.0), or ``None``.
        """
        if trigger not in self._intervals:
            return None

        interval = self._intervals[trigger]
        last = self._last_fire.get(trigger, self._start_time)
        elapsed = time.monotonic() - last
        remaining = interval - elapsed
        return max(0.0, remaining)
