"""Pure zoned daily boundaries, independent of the daemon host's clock zone."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SCHEDULE_ID = "morning-daily"
ZONE_CHANGE_GUARD_SECONDS = 20 * 3600


def planned_at(day: date, clock: str, timezone: str) -> float:
    """First fold occurrence; gaps move to the first real local minute."""
    zone = ZoneInfo(timezone)
    hour, minute = map(int, clock.split(":"))
    wall = datetime(day.year, day.month, day.day, hour, minute)
    for _ in range(48 * 60):
        candidates = [
            instant
            for fold in (0, 1)
            if datetime.fromtimestamp(
                instant := wall.replace(tzinfo=zone, fold=fold).timestamp(), zone
            ).replace(tzinfo=None)
            == wall
        ]
        if candidates:
            return min(candidates)
        wall += timedelta(minutes=1)
    raise ValueError("could not resolve report time")


def next_due(now: float, clock: str, timezone: str, *, after: float | None = None) -> float:
    day = datetime.fromtimestamp(now, ZoneInfo(timezone)).date()
    for _ in range(4):
        instant = planned_at(day, clock, timezone)
        if instant > now and (after is None or instant >= after):
            return instant
        day += timedelta(days=1)
    raise ValueError("could not resolve next report time")
