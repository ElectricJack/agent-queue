"""Lightweight timer service that emits synthetic ``timer.*`` and ``cron.*`` events.

Two trigger families are supported:

- ``timer.{N}m`` / ``timer.{N}h`` — periodic, elapsed-time based. First fire on
  startup (all timers considered overdue), then every N minutes/hours. Not
  persisted — daemon restart resets elapsed time.
- ``cron.HH:MM`` — once per local day at a wall-clock time (e.g. ``cron.07:00``,
  ``cron.17:30``). Fires at-or-after the target within a single ~5s tick.
  Per-trigger "last fired date" is persisted to disk so daemon restarts do
  not re-fire the same day.

**Spec reference:** ``docs/specs/design/playbooks.md`` Section 7 — Timer Service.

Timer/cron events carry ``project_id: null`` — they are inherently
system-scoped.  A project-scoped playbook can trigger on them, but each
trigger fires once globally, not once per project.

Payload format (same for both families)::

    {
        "tick_time": "2026-04-09T12:00:00+00:00",
        "interval": "30m"      # or "07:00" for cron triggers
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

import datetime as _dt
import json
import logging
import os
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

# Pattern: ``cron.HH:MM`` where HH is 00-23 and MM is 00-59. Two-digit required.
_CRON_TRIGGER_RE = re.compile(r"^cron\.(\d{2}):(\d{2})$")

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


def parse_cron(trigger: str) -> tuple[int, int] | None:
    """Parse a ``cron.HH:MM`` trigger string into a ``(hour, minute)`` tuple.

    The time is interpreted in the system's local timezone — the trigger
    fires once per local day at that wall-clock time.

    Supported format:
    - ``cron.HH:MM`` — 24-hour local time (e.g. ``cron.07:00``, ``cron.17:30``).
      Both fields are zero-padded two digits.

    Returns ``None`` if the trigger doesn't match the cron pattern or if the
    hour/minute are out of range.

    Parameters
    ----------
    trigger:
        The trigger event type string (e.g. ``"cron.07:00"``).

    Returns
    -------
    tuple[int, int] | None
        ``(hour, minute)`` in local time, or ``None`` if invalid.
    """
    match = _CRON_TRIGGER_RE.match(trigger)
    if match is None:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    return hour, minute


def extract_cron_targets(triggers: list[str]) -> dict[str, tuple[int, int]]:
    """Extract cron targets from a list of trigger event types.

    Parameters
    ----------
    triggers:
        List of trigger strings (e.g. ``["git.commit", "cron.07:00"]``).

    Returns
    -------
    dict[str, tuple[int, int]]
        Mapping from cron trigger string to ``(hour, minute)`` in local time.
        Only valid cron triggers are included.
    """
    result: dict[str, tuple[int, int]] = {}
    for trigger in triggers:
        parsed = parse_cron(trigger)
        if parsed is not None:
            result[trigger] = parsed
    return result


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
        state_path: str | None = None,
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

        # Active cron targets: trigger string → (hour, minute) in local time.
        self._cron_targets: dict[str, tuple[int, int]] = {}

        # Last local date a cron trigger fired on. Dedup key — we fire at most
        # once per local day per trigger. Persisted across restarts so a
        # daemon bounce doesn't cause a same-day re-fire.
        self._cron_last_fired_date: dict[str, _dt.date] = {}

        # On-disk persistence for ``_cron_last_fired_date``. ``None`` disables
        # persistence (used in tests).
        self._state_path: str | None = state_path

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

    @property
    def active_cron_targets(self) -> dict[str, tuple[int, int]]:
        """Return a read-only copy of cron targets (trigger → (hour, minute))."""
        return dict(self._cron_targets)

    @property
    def cron_count(self) -> int:
        """Return the number of active cron triggers being tracked."""
        return len(self._cron_targets)

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
        self._load_cron_state()
        self.rebuild()
        # Override _last_fire for all intervals so they fire immediately
        # on the first tick.  rebuild() sets _last_fire = now (wait one
        # cycle), but on startup we treat all timers as overdue.
        for trigger in self._intervals:
            self._last_fire[trigger] = 0.0
        logger.info(
            "Timer service started — tracking %d interval(s): %s; %d cron(s): %s",
            len(self._intervals),
            ", ".join(sorted(self._intervals.keys())) or "(none)",
            len(self._cron_targets),
            ", ".join(sorted(self._cron_targets.keys())) or "(none)",
        )

    def stop(self) -> None:
        """Stop the timer service.  No more events will be emitted."""
        self._running = False
        self._intervals.clear()
        self._last_fire.clear()
        self._cron_targets.clear()
        # Intentionally keep ``_cron_last_fired_date`` so a stop/start cycle
        # during the same local day does not re-fire. ``start()`` reloads
        # from disk anyway, which is the authoritative source.
        logger.info("Timer service stopped")

    def rebuild(self) -> None:
        """Rebuild the set of tracked intervals from the playbook manager.

        Called on startup and whenever playbooks are compiled/removed.
        Preserves existing ``_last_fire`` timestamps for intervals that
        remain active (so they don't re-fire immediately after a rebuild).
        New intervals get the current monotonic time as their initial
        "last fire" so they wait one full interval before first firing.
        """
        # Collect all timer/cron triggers from the playbook manager's trigger map.
        all_triggers = self._playbook_manager.get_all_triggers()
        new_intervals = extract_timer_intervals(all_triggers)
        new_cron = extract_cron_targets(all_triggers)

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

        # Cron targets — replace the set; keep per-trigger last-fired-date for
        # triggers that still exist, drop entries for triggers that went away.
        old_cron = self._cron_targets
        self._cron_targets = new_cron
        self._cron_last_fired_date = {
            t: d for t, d in self._cron_last_fired_date.items() if t in new_cron
        }

        # Log changes
        added = set(new_intervals) - set(old_fire)
        removed = set(old_fire) - set(new_intervals)
        if added:
            logger.info("Timer service: added intervals %s", sorted(added))
        if removed:
            logger.info("Timer service: removed intervals %s", sorted(removed))

        cron_added = set(new_cron) - set(old_cron)
        cron_removed = set(old_cron) - set(new_cron)
        if cron_added:
            logger.info("Timer service: added cron triggers %s", sorted(cron_added))
        if cron_removed:
            logger.info("Timer service: removed cron triggers %s", sorted(cron_removed))

    def _check_for_trigger_changes(self) -> None:
        """Compare current intervals against the playbook manager's triggers.

        If the set of timer triggers has changed (playbook compiled, removed,
        or reloaded), triggers a full rebuild.  This is cheap — O(n) where
        n is the number of triggers, typically very small.
        """
        all_triggers = self._playbook_manager.get_all_triggers()
        current_timer_triggers = {t for t in all_triggers if t.startswith("timer.")}
        current_cron_triggers = {t for t in all_triggers if t.startswith("cron.")}
        tracked_timer = set(self._intervals.keys())
        tracked_cron = set(self._cron_targets.keys())

        if current_timer_triggers != tracked_timer or current_cron_triggers != tracked_cron:
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

        if not self._intervals and not self._cron_targets:
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

        # Cron targets — fire once per local day when the wall-clock time is
        # at or past the target. Date-based dedup is the authoritative signal.
        if self._cron_targets:
            now_local = self._now_local()
            today = now_local.date()
            now_hhmm = (now_local.hour, now_local.minute)

            for trigger, target in self._cron_targets.items():
                if self._cron_last_fired_date.get(trigger) == today:
                    continue
                if now_hhmm >= target:
                    hh, mm = target
                    await self._bus.emit(
                        trigger,
                        {
                            "tick_time": tick_time,
                            "interval": f"{hh:02d}:{mm:02d}",
                        },
                    )
                    self._cron_last_fired_date[trigger] = today
                    self._save_cron_state()
                    emitted += 1
                    logger.debug(
                        "Emitted %s (local=%02d:%02d, target=%02d:%02d)",
                        trigger,
                        now_local.hour,
                        now_local.minute,
                        hh,
                        mm,
                    )

        return emitted

    def _now_local(self) -> datetime:
        """Return current wall-clock time in the system local timezone.

        Broken out so tests can patch a single method instead of
        ``datetime.now`` across the module.
        """
        return datetime.now().astimezone()

    def time_until_next(self, trigger: str) -> float | None:
        """Return seconds until the next firing of *trigger*, or ``None``.

        Returns ``None`` if the trigger is not being tracked. Works for both
        ``timer.*`` (periodic) and ``cron.*`` (daily wall-clock) triggers.

        Parameters
        ----------
        trigger:
            The timer or cron trigger string (e.g. ``"timer.30m"``, ``"cron.07:00"``).

        Returns
        -------
        float | None
            Seconds remaining until the next fire (≥ 0.0), or ``None``.
        """
        if trigger in self._intervals:
            interval = self._intervals[trigger]
            last = self._last_fire.get(trigger, self._start_time)
            elapsed = time.monotonic() - last
            remaining = interval - elapsed
            return max(0.0, remaining)

        if trigger in self._cron_targets:
            hh, mm = self._cron_targets[trigger]
            now_local = self._now_local()
            target_today = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            # If already fired today, aim for tomorrow regardless of clock.
            fired_today = self._cron_last_fired_date.get(trigger) == now_local.date()
            if fired_today or target_today <= now_local:
                target = target_today + _dt.timedelta(days=1)
            else:
                target = target_today
            return max(0.0, (target - now_local).total_seconds())

        return None

    # ------------------------------------------------------------------
    # Cron state persistence
    # ------------------------------------------------------------------

    def _load_cron_state(self) -> None:
        """Load ``_cron_last_fired_date`` from disk, if configured.

        Silently starts empty if the file is missing or malformed — a lost
        state file causes at worst one extra same-day fire, which is a fine
        failure mode for a daily notification trigger.
        """
        if not self._state_path:
            return
        if not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Timer service: could not load cron state from %s (%s); starting fresh",
                self._state_path,
                exc,
            )
            return

        loaded: dict[str, _dt.date] = {}
        for trigger, iso_date in (raw.get("cron_last_fired_date") or {}).items():
            try:
                loaded[trigger] = _dt.date.fromisoformat(iso_date)
            except (TypeError, ValueError):
                continue
        self._cron_last_fired_date = loaded
        if loaded:
            logger.info(
                "Timer service: loaded %d cron last-fired date(s) from %s",
                len(loaded),
                self._state_path,
            )

    def _save_cron_state(self) -> None:
        """Persist ``_cron_last_fired_date`` to disk, if configured."""
        if not self._state_path:
            return
        payload = {
            "cron_last_fired_date": {
                t: d.isoformat() for t, d in self._cron_last_fired_date.items()
            }
        }
        try:
            os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
            tmp_path = self._state_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp_path, self._state_path)
        except OSError as exc:
            logger.warning(
                "Timer service: could not save cron state to %s (%s)",
                self._state_path,
                exc,
            )
