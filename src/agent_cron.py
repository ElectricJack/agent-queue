"""Pure recurrence arithmetic and daemon-only command adapters for agent cron."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_ATTEMPTS = 5
PENDING_TTL = 3600
MAX_ACTIVE = 10
MAX_COUNT_MINUTES = 7 * 24 * 60


class CronError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def recurrence(*, every=None, offset=0, cron=None, zone="UTC") -> dict:
    """Validate the deliberately small interval/cron language."""
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise CronError("cron.invalid", "timezone must name an IANA zone") from exc
    if (every is None) == (cron is None):
        raise CronError("cron.invalid", "choose exactly one of every or cron")
    if every is not None:
        if (
            isinstance(every, bool)
            or not math.isfinite(every)
            or not 60 <= every <= 604800
            or isinstance(offset, bool)
            or not math.isfinite(offset)
            or not 0 <= offset < every
        ):
            raise CronError("cron.invalid", "every must be 60–604800; offset must be in [0, every)")
        return {"kind": "interval", "every": every, "offset": offset, "timezone": zone}
    if offset != 0:
        raise CronError("cron.invalid", "offset is only supported with every")
    fields = cron.split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        raise CronError("cron.invalid", "cron requires MINUTE HOUR * * *")
    minute, hour = fields[:2]
    valid_minute = (
        minute == "*"
        or (re.fullmatch(r"[0-9]{1,2}", minute) and int(minute) <= 59)
        or (re.fullmatch(r"\*/[0-9]{1,2}", minute) and 1 <= int(minute[2:]) <= 59)
    )
    valid_hour = hour == "*" or (re.fullmatch(r"[0-9]{1,2}", hour) and int(hour) <= 23)
    if not valid_minute or not valid_hour:
        raise CronError("cron.invalid", "minute: 0–59, * or */N; hour: 0–23 or *")
    return {"kind": "cron", "cron": " ".join(fields), "timezone": zone}


def _matches_cron(local, minute, hour):
    minute_matches = minute == "*" or (
        local.minute % int(minute[2:]) == 0
        if minute.startswith("*/")
        else local.minute == int(minute)
    )
    return minute_matches and (hour == "*" or local.hour == int(hour))


def next_fire(spec: dict, after: float) -> float:
    """First tick strictly after an instant; intervals never drift on recovery."""
    if spec["kind"] == "interval":
        every, offset = spec["every"], spec["offset"]
        return (math.floor((after - offset) / every) + 1) * every + offset
    minute, hour = spec["cron"].split()[:2]
    zone = ZoneInfo(spec["timezone"])
    candidate = (math.floor(after / 60) + 1) * 60
    # Fixed minute/hour patterns match within two real days even across DST.
    for _ in range(60 * 48):
        local = datetime.fromtimestamp(candidate, zone)
        if _matches_cron(local, minute, hour):
            return candidate
        candidate += 60
    raise CronError("cron.invalid", "no tick found within 48 hours")


def due_count(spec: dict, first: float, now: float) -> int:
    if spec["kind"] == "interval":
        return max(0, math.floor((now - first) / spec["every"]) + 1)
    minute, hour = spec["cron"].split()[:2]
    zone = ZoneInfo(spec["timezone"])
    # Bound UTC minutes examined, including sparse daily schedules. For
    # outages over a week this diagnostic is a lower bound; delivery still
    # coalesces everything and advances directly to the next future tick.
    minutes = min(MAX_COUNT_MINUTES, max(0, math.floor((now - first) / 60) + 1))
    return sum(
        _matches_cron(datetime.fromtimestamp(first + index * 60, zone), minute, hour)
        for index in range(minutes)
    )


def visible_schedule(row: dict) -> dict:
    return {
        **row,
        "timezone": row["recurrence"]["timezone"],
        "next_fire_local": datetime.fromtimestamp(
            row["next_fire_at"], ZoneInfo(row["recurrence"]["timezone"])
        ).isoformat(),
        "next_fire_utc": datetime.fromtimestamp(row["next_fire_at"], timezone.utc).isoformat(),
    }


class AgentCronService:
    """All scheduled state changes pass through the daemon command boundary."""

    def __init__(self, handler):
        self.handler = handler

    async def _execute(self, name, args):
        from src.commands.principal import ExecutionPrincipal, principal_context

        with principal_context(ExecutionPrincipal.service("agent-cron")):
            return await self.handler.execute(name, args)

    async def tick(self, *, now=None):
        return await self._execute("reconcile_agent_cron", {"now": now})

    async def begin_delivery(self, message_id):
        result = await self._execute("cron_delivery_begin", {"message_id": message_id})
        return result.get("success") and result.get("allowed", False)

    async def finish_delivery(self, message_id, *, delivered, error=None):
        return await self._execute(
            "cron_delivery_finish",
            {
                "message_id": message_id,
                "delivered": delivered,
                "error": error,
            },
        )
