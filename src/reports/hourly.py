"""Pure hourly report policy and bounded brief construction."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from src.config import ReportsConfig
from src.digest.aggregate import DigestResult
from src.digest.facts import DigestWindow
from src.digest.schedule import DigestSchedule

MAX_BRIEF_BYTES = 24 * 1024


def local_day_bounds(now: float, zone_name: str) -> tuple[float, float]:
    zone = ZoneInfo(zone_name)
    day = datetime.fromtimestamp(now, zone).date()
    return (
        datetime.combine(day, time.min, zone).timestamp(),
        datetime.combine(day + timedelta(days=1), time.min, zone).timestamp(),
    )


def quiet_at(now: float, config: ReportsConfig) -> bool:
    quiet = config.hourly.quiet_hours
    if quiet is None:
        return False
    local = datetime.fromtimestamp(now, ZoneInfo(config.timezone))
    clock = local.hour * 60 + local.minute
    start = int(quiet.start[:2]) * 60 + int(quiet.start[3:])
    end = int(quiet.end[:2]) * 60 + int(quiet.end[3:])
    if start < end:
        return start <= clock < end
    return clock >= start or clock < end


def author_skip_reason(
    config: ReportsConfig,
    schedule: DigestSchedule,
    window: DigestWindow,
    result: DigestResult,
    *,
    now: float,
    playbook_active: bool,
) -> str | None:
    if not config.hourly.enabled:
        return "feature_off"
    if not playbook_active:
        return "playbook_inactive"
    if not config.hourly.full_fleet_visibility or schedule.project_ids:
        return "restricted_destination"
    if window.catchup:
        return "catchup_window"
    if result.eligibility is None or not result.eligibility.facts:
        return "active_only"
    if quiet_at(now, config):
        return "quiet_hours"
    return None


def _encoded(brief: dict) -> bytes:
    return json.dumps(brief, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def hash_brief(brief: dict) -> str:
    """Canonical evidence hash shared by read-only and durable report builders."""
    return hashlib.sha256(_encoded(brief)).hexdigest()


def build_hourly_brief(
    result: DigestResult,
    window: DigestWindow,
    *,
    destination: str,
    dashboard_url: str = "",
    dashboard_notice: str = "",
    previous_narrative: str = "",
) -> tuple[dict, str]:
    """Keep structural evidence and omission counts inside a 24 KiB JSON cap."""
    eligibility = result.eligibility
    if eligibility is None or not result.send:
        raise ValueError("an ineligible digest has no report brief")
    brief = {
        "window": {"since": window.since, "until": window.until},
        "destination": destination,
        "dashboard_url": dashboard_url,
        "dashboard_notice": dashboard_notice,
        "facts": [
            {
                "key": fact.key,
                "kind": fact.kind,
                "category": fact.category,
                "project_id": fact.project_id,
                "task_id": fact.task_id,
                "title": fact.title[:160],
                "detail": fact.detail[:500],
                "at": fact.at,
                "source_url": (
                    f"{dashboard_url.rstrip('/')}/tasks/{quote(fact.task_id, safe='')}"
                    if dashboard_url and fact.task_id
                    else dashboard_url
                ),
                # A completion is not proof that a branch reached main.
                "delivery": "unknown",
            }
            for fact in eligibility.facts
        ],
        "active": [
            {"task_id": task.task_id, "project_id": task.project_id, "title": task.title[:160]}
            for task in eligibility.active
        ],
        "previous_narrative_context": previous_narrative[:1200],
        "omitted": {"facts": 0, "active": 0},
    }
    while len(_encoded(brief)) > MAX_BRIEF_BYTES:
        if brief["previous_narrative_context"]:
            brief["previous_narrative_context"] = ""
        elif len(brief["facts"]) > len(brief["active"]) and len(brief["facts"]) > 1:
            brief["facts"].pop()
            brief["omitted"]["facts"] += 1
        elif brief["active"]:
            brief["active"].pop()
            brief["omitted"]["active"] += 1
        elif brief["facts"]:
            brief["facts"].pop()
            brief["omitted"]["facts"] += 1
        else:
            raise ValueError("report brief metadata exceeds 24 KiB")
    return brief, hash_brief(brief)
