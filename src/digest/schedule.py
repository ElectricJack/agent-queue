"""Destination identity, configuration generations and window boundaries.

The durable digest window is keyed by ``(destination, config_generation,
window_start, window_end)`` (:mod:`src.database.tables`), so §9's "a
configuration change starts a new schedule generation" is a statement about
those two leading columns.  This module is where a
:class:`~src.config.DiscordConfig` becomes them:

* the **destination** is the configured channel, addressed by ID -- a renamed
  channel keeps its pending deliveries, and pointing the installation at a
  different channel is a different destination rather than a silent rebind;
* the **generation** is a stable digest of the settings that change *what a
  window means* (interval, selected projects, categories, catch-up horizon).
  Changing one starts a new generation, so old windows are never re-evaluated
  under new settings and the new generation never replays historical windows.
  Settings that do not change a window's meaning -- mention lists, the
  escalation timeout -- deliberately do not roll it.

Boundaries are computed from an injected ``now``; nothing here reads a clock,
so digest scheduling stays testable and Discord-free.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from src.config import (
    MAX_DIGEST_CATCHUP_HOURS,
    MAX_DIGEST_INTERVAL_MINUTES,
    MIN_DIGEST_CATCHUP_HOURS,
    MIN_DIGEST_INTERVAL_MINUTES,
    DiscordConfig,
)
from src.digest.facts import CATEGORIES, DigestWindow

#: ``config_generation`` is a non-negative integer column; a 31-bit fold of the
#: settings hash keeps it stable across processes and inside that range.
_GENERATION_MASK = 0x7FFFFFFF


def destination_id(config: DiscordConfig) -> str:
    """Stable transport-qualified identity of the one configured channel."""
    return f"discord:{config.channel_id}" if config.channel_id else "discord:unconfigured"


def config_generation(config: DiscordConfig) -> int:
    """Generation number for the settings that define a digest window."""
    digest = config.digest
    material = json.dumps(
        {
            "channel_id": config.channel_id,
            "interval_minutes": digest.interval_minutes,
            "project_ids": sorted(digest.project_ids),
            "categories": sorted(digest.categories),
            "catchup_hours": digest.catchup_hours,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    fold = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(fold[:4], "big") & _GENERATION_MASK


@dataclass(frozen=True, slots=True)
class DigestSchedule:
    """Everything the evaluator and the dashboard need about the schedule."""

    destination: str
    generation: int
    enabled: bool
    interval_seconds: float
    catchup_seconds: float
    project_ids: tuple[str, ...]
    categories: frozenset[str]

    def window_for(self, now: float, *, last_window_end: float | None = None) -> DigestWindow:
        """The window ending at ``now``'s boundary.

        With no previous window the schedule starts one interval back.  A gap
        longer than one interval coalesces into a single ``catchup`` window,
        bounded by the configured horizon -- §8 forbids one message per missed
        hour, and forbids dumping unbounded history into the channel.
        """
        if last_window_end is None:
            return DigestWindow(since=now - self.interval_seconds, until=now)
        gap = now - last_window_end
        if gap <= self.interval_seconds:
            return DigestWindow(since=last_window_end, until=now)
        since = max(last_window_end, now - self.catchup_seconds)
        return DigestWindow(since=since, until=now, catchup=True)

    def next_evaluation_at(self, now: float, *, last_window_end: float | None = None) -> float:
        """When the next evaluation is due, in epoch seconds."""
        if last_window_end is None:
            return now + self.interval_seconds
        due = last_window_end + self.interval_seconds
        return max(now, due)


def schedule_for(config: DiscordConfig) -> DigestSchedule:
    """Project the configured settings onto the durable schedule identity."""
    digest = config.digest
    return DigestSchedule(
        destination=destination_id(config),
        generation=config_generation(config),
        enabled=digest.enabled,
        interval_seconds=digest.interval_minutes * 60.0,
        catchup_seconds=digest.catchup_hours * 3600.0,
        project_ids=tuple(sorted(digest.project_ids)),
        categories=frozenset(digest.categories),
    )


def validate_settings(config: DiscordConfig, known_project_ids: frozenset[str]) -> list[str]:
    """Actionable settings errors, including project membership.

    :func:`src.config.DiscordConfig.validate` cannot see the project table, so
    the membership half of §9's "validate IDs and project membership" lives
    here and is called by the command surface, where the projects are known.
    Returned strings are operator-facing: each names the field and the value.
    """
    errors = [f"{e.section}.{e.field}: {e.message}" for e in config.validate()]
    for project_id in config.digest.project_ids:
        if project_id not in known_project_ids:
            errors.append(
                f"discord.digest.project_ids: unknown project {project_id!r}; "
                "remove it or create the project first"
            )
    return errors


__all__ = [
    "CATEGORIES",
    "MAX_DIGEST_CATCHUP_HOURS",
    "MAX_DIGEST_INTERVAL_MINUTES",
    "MIN_DIGEST_CATCHUP_HOURS",
    "MIN_DIGEST_INTERVAL_MINUTES",
    "DigestSchedule",
    "config_generation",
    "destination_id",
    "schedule_for",
    "validate_settings",
]
