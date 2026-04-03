"""Schedule matching for periodic hooks.

Provides cron-like scheduling capabilities that can be embedded in hook
trigger definitions.  The schedule is evaluated on each tick to determine
whether the hook should fire.

Schedule format (embedded in trigger JSON)::

    {
        "type": "periodic",
        "interval_seconds": 3600,
        "schedule": {
            "times": ["02:00", "14:30"],          # Fire at these times (HH:MM, 24h)
            "days_of_week": ["mon", "wed", "fri"], # Fire on these days
            "days_of_month": [1, 15],              # Fire on these calendar dates
            "cron": "0 2 * * 1-5"                  # Or use cron syntax
        }
    }

When a ``schedule`` block is present, the hook only fires when the current
time matches the schedule AND the interval has elapsed.  Without a schedule
block, the hook fires purely on interval (existing behavior).

Multiple fields combine with AND logic: ``times + days_of_week`` means
"at these times ON these days".  Each field independently can have
multiple values (OR within a field).

The ``cron`` field uses standard 5-field cron syntax::

    ┌───────────── minute (0-59)
    │ ┌───────────── hour (0-23)
    │ │ ┌───────────── day of month (1-31)
    │ │ │ ┌───────────── month (1-12)
    │ │ │ │ ┌───────────── day of week (0-6, 0=Monday)
    │ │ │ │ │
    * * * * *

Supported cron features:

- ``*`` (any value)
- ``*/N`` (every N)
- ``N-M`` (range)
- ``N,M,O`` (list)
- ``N`` (exact value)

See ``specs/hooks.md`` for the periodic hook trigger specification.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Day name -> weekday index (Monday=0, matching Python's weekday())
DAY_NAMES: dict[str, int] = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def matches_schedule(
    schedule: dict[str, Any],
    now: datetime | None = None,
    last_run: datetime | None = None,
    tolerance_seconds: int = 60,
) -> bool:
    """Check whether the current time matches a schedule definition.

    Args:
        schedule: Schedule dict with optional keys: times, days_of_week,
                  days_of_month, cron.
        now: Current datetime (UTC). Defaults to utcnow().
        last_run: When the hook last fired. Used to prevent duplicate
                  firings within the same schedule window.
        tolerance_seconds: How many seconds around the target time still
                          count as a match. Default 60s (one minute window).

    Returns:
        True if the current time matches the schedule.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Ensure timezone-aware
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # If cron field is present, use cron matching exclusively
    if "cron" in schedule:
        return _matches_cron(schedule["cron"], now, last_run, tolerance_seconds)

    # Structured schedule: all present fields must match (AND logic)
    checks: list[bool] = []

    if "times" in schedule:
        checks.append(_matches_times(schedule["times"], now, tolerance_seconds))

    if "days_of_week" in schedule:
        checks.append(_matches_days_of_week(schedule["days_of_week"], now))

    if "days_of_month" in schedule:
        checks.append(_matches_days_of_month(schedule["days_of_month"], now))

    if not checks:
        # Empty schedule = always matches (no constraints)
        return True

    if not all(checks):
        return False

    # Dedup: if last_run is within the same matching window, don't fire again
    if last_run is not None:
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
        # For time-based schedules, check if we already fired in this window
        if "times" in schedule:
            if _same_time_window(schedule["times"], now, last_run, tolerance_seconds):
                return False

    return True


def _matches_times(times: list[str], now: datetime, tolerance_seconds: int) -> bool:
    """Check if current time matches any of the specified times.

    Each time is HH:MM in 24-hour format.  A match occurs if the current
    time is within ``tolerance_seconds`` of the target.
    """
    now_minutes = now.hour * 60 + now.minute
    now_seconds_in_day = now_minutes * 60 + now.second

    for t in times:
        try:
            parts = t.strip().split(":")
            target_h, target_m = int(parts[0]), int(parts[1])
            if not (0 <= target_h <= 23 and 0 <= target_m <= 59):
                logger.warning("Invalid time value in schedule: %s", t)
                continue
            target_seconds = (target_h * 60 + target_m) * 60
            diff = abs(now_seconds_in_day - target_seconds)
            # Handle midnight wrap
            if diff > 43200:  # 12 hours
                diff = 86400 - diff
            if diff <= tolerance_seconds:
                return True
        except (ValueError, IndexError):
            logger.warning("Invalid time format in schedule: %s", t)
            continue
    return False


def _same_time_window(
    times: list[str],
    now: datetime,
    last_run: datetime,
    tolerance_seconds: int,
) -> bool:
    """Check if last_run and now are in the same time-matching window."""
    # If they're on different dates, they can't be the same window
    if now.date() != last_run.date():
        return False

    now_seconds = now.hour * 3600 + now.minute * 60 + now.second
    last_seconds = last_run.hour * 3600 + last_run.minute * 60 + last_run.second

    for t in times:
        try:
            parts = t.strip().split(":")
            target_h, target_m = int(parts[0]), int(parts[1])
            target_seconds = target_h * 3600 + target_m * 60
            # Both now and last_run within tolerance of same target
            now_diff = abs(now_seconds - target_seconds)
            last_diff = abs(last_seconds - target_seconds)
            if now_diff <= tolerance_seconds and last_diff <= tolerance_seconds:
                return True
        except (ValueError, IndexError):
            continue
    return False


def _matches_days_of_week(days: list[str | int], now: datetime) -> bool:
    """Check if current day matches any of the specified days.

    Accepts day names (mon, tuesday, etc.) or integers (0=Monday).
    """
    current_day = now.weekday()  # Monday=0
    for d in days:
        if isinstance(d, int):
            if d == current_day:
                return True
        elif isinstance(d, str):
            day_lower = d.strip().lower()
            if day_lower in DAY_NAMES:
                if DAY_NAMES[day_lower] == current_day:
                    return True
            else:
                try:
                    if int(day_lower) == current_day:
                        return True
                except ValueError:
                    logger.warning("Unknown day name: %s", d)
    return False


def _matches_days_of_month(days: list[int], now: datetime) -> bool:
    """Check if current day-of-month matches any of the specified dates."""
    return now.day in days


# ---------------------------------------------------------------------------
# Cron parsing
# ---------------------------------------------------------------------------


def _matches_cron(
    expr: str,
    now: datetime,
    last_run: datetime | None,
    tolerance_seconds: int,
) -> bool:
    """Evaluate a 5-field cron expression against the current time.

    Fields: minute hour day-of-month month day-of-week
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        logger.warning("Invalid cron expression (expected 5 fields): %s", expr)
        return False

    minute_match = _cron_field_matches(fields[0], now.minute, 0, 59)
    hour_match = _cron_field_matches(fields[1], now.hour, 0, 23)
    dom_match = _cron_field_matches(fields[2], now.day, 1, 31)
    month_match = _cron_field_matches(fields[3], now.month, 1, 12)
    dow_match = _cron_field_matches(fields[4], now.weekday(), 0, 6)

    if not all([minute_match, hour_match, dom_match, month_match, dow_match]):
        return False

    # Dedup: don't fire twice in the same minute
    if last_run is not None:
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
        if (
            last_run.year == now.year
            and last_run.month == now.month
            and last_run.day == now.day
            and last_run.hour == now.hour
            and last_run.minute == now.minute
        ):
            return False

    return True


def _cron_field_matches(field: str, value: int, min_val: int, max_val: int) -> bool:
    """Check if a single cron field matches the given value.

    Supports: * (any), */N (step), N-M (range), N,M (list), N (exact).
    """
    # Handle comma-separated list
    if "," in field:
        return any(
            _cron_field_matches(part.strip(), value, min_val, max_val) for part in field.split(",")
        )

    # Wildcard
    if field == "*":
        return True

    # Step: */N or N-M/S
    if "/" in field:
        base, step_str = field.split("/", 1)
        try:
            step = int(step_str)
        except ValueError:
            return False
        if base == "*":
            return (value - min_val) % step == 0
        if "-" in base:
            range_start, range_end = base.split("-", 1)
            try:
                rs, re_ = int(range_start), int(range_end)
                return rs <= value <= re_ and (value - rs) % step == 0
            except ValueError:
                return False
        return False

    # Range: N-M
    if "-" in field:
        try:
            start, end = field.split("-", 1)
            return int(start) <= value <= int(end)
        except ValueError:
            return False

    # Exact value
    try:
        return int(field) == value
    except ValueError:
        return False


def parse_schedule(trigger: dict) -> dict | None:
    """Extract schedule from a trigger dict, returning None if not present."""
    return trigger.get("schedule")


def describe_schedule(schedule: dict) -> str:
    """Return a human-readable description of a schedule.

    Used for display in CLI/Discord commands.
    """
    if not schedule:
        return "No schedule (runs on interval)"

    parts: list[str] = []

    if "cron" in schedule:
        return f"Cron: {schedule['cron']}"

    if "times" in schedule:
        times_str = ", ".join(schedule["times"])
        parts.append(f"at {times_str}")

    if "days_of_week" in schedule:
        days_str = ", ".join(d if isinstance(d, str) else str(d) for d in schedule["days_of_week"])
        parts.append(f"on {days_str}")

    if "days_of_month" in schedule:
        dom_str = ", ".join(str(d) for d in schedule["days_of_month"])
        parts.append(f"on day(s) {dom_str}")

    return " ".join(parts) if parts else "No schedule constraints"


def next_run_time(
    schedule: dict[str, Any],
    now: datetime | None = None,
    last_run: datetime | None = None,
    tolerance_seconds: int = 60,
    max_lookahead_hours: int = 168,
) -> datetime | None:
    """Calculate when a schedule will next match.

    Walks forward from *now* in one-minute increments until the schedule
    matches, returning the first matching datetime.  Returns ``None`` if
    no match is found within *max_lookahead_hours* (default 7 days).

    Args:
        schedule: Schedule dict (same format as ``matches_schedule``).
        now: Current datetime (UTC). Defaults to utcnow().
        last_run: Last run time, used for dedup checks.
        tolerance_seconds: Passed through to ``matches_schedule``.
        max_lookahead_hours: How many hours ahead to search.

    Returns:
        The next matching UTC datetime, or None.
    """
    from datetime import timedelta

    if not schedule:
        return None  # Pure interval hooks — no deterministic next time

    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Snap to next whole minute to start scanning
    candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = now + timedelta(hours=max_lookahead_hours)

    while candidate <= end:
        if matches_schedule(
            schedule, now=candidate, last_run=last_run, tolerance_seconds=tolerance_seconds
        ):
            return candidate
        candidate += timedelta(minutes=1)

    return None


def format_next_run(dt: datetime | None) -> str:
    """Format a next-run datetime for human-readable display.

    Returns a string like "in 2h 15m (14:30 UTC)" or "no upcoming run".
    """
    if dt is None:
        return "no upcoming run"
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    delta = dt - now
    if delta.total_seconds() < 0:
        return f"overdue ({dt.strftime('%H:%M UTC')})"

    total_minutes = int(delta.total_seconds()) // 60
    hours, minutes = divmod(total_minutes, 60)
    days, hours = divmod(hours, 24)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")

    return f"in {' '.join(parts)} ({dt.strftime('%H:%M UTC %a')})"
