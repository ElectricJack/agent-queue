"""Provider allocation snapshot: the read half of provider-level worker controls.

Spec: ``projects/agent-queue/specs/provider-worker-allocation-controls.md``
(vault), plan Task 6.  An operator changing providers needs one answer to
"what runs where" before touching anything: which ordinary worker profiles
each provider has, what their pools hold right now, which tasks are pinned to
them, which manual agent definitions draw on them, and what each project
prefers.  This module builds that answer; it decides nothing and writes
nothing.

Two halves, so the preview (plan Task 8) can reuse the first:

* :func:`build_allocation_snapshot` reads everything once -- one
  ``_measure_pools()`` call for pool supply, the live task sessions, the
  pinned and preferred tasks, the agent definitions, the projects and the
  last allocation events -- into one unredacted, JSON-shaped snapshot.
* :func:`status_view` narrows that snapshot for a caller: a view filter or a
  project-scoped caller keeps every fleet-wide count (profiles and pools are
  global) but drops other projects' rows, sessions and task ids, counting
  what it hid.

The allocation key is the **harness** provider key (``harness.base or
harness.id``: ``claude``, ``codex``), the same key availability and re-routes
store; ``sessions.provider`` is transport (``tmux``) and is never read here.
Only ordinary worker profiles are selectable (:func:`classify_profile`);
every other profile is listed in ``diagnostics`` with the reason.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from src.providers.availability import provider_key, vendor_of

logger = logging.getLogger(__name__)

#: The audit event an allocation apply writes (spec §Audit, plan Task 9).
ALLOCATION_EVENT = "provider.allocation_changed"
#: How many recent allocation events are scanned for each provider's latest.
ALLOCATION_EVENT_SCAN = 200
#: Session states that hold capacity (``src.orchestrator.pools._LIVE_STATES``).
LIVE_SESSION_STATES = ("starting", "running", "draining")
#: The supply counters every profile, project row and provider reports.
SUPPLY_FIELDS = ("ready", "idle", "busy", "starting", "draining", "unresponsive")
#: Task statuses in which a pin keeps work on a provider (spec §Backend commands).
INTENT_TASK_STATUSES = ("READY", "ASSIGNED", "IN_PROGRESS")
#: A manual agent's current task counts only while it is one of these.
_ACTIVE_TASK_STATUSES = frozenset({"ASSIGNED", "IN_PROGRESS", "WAITING_INPUT"})
#: Stage profiles no generic worker may stand in for (``routing_commands``).
_CONTROL_PROFILE_IDS = frozenset(
    {"supervisor", "triage", "reviewer", "final-reviewer", "playbook-compiler", "spec-ingest"}
)


# -- classification -----------------------------------------------------------------


def _role_profile_ids() -> frozenset[str]:
    from src.profiles.catalog import _stage_profile_ids

    return _stage_profile_ids() | _CONTROL_PROFILE_IDS


def classify_profile(profile: Any, *, provider_known: bool) -> str | None:
    """``None`` for an ordinary worker profile, else why bulk control skips it.

    Reasons, first match wins: ``retired_project_scoped`` (a ``project:`` id
    that resolves nowhere), ``template``, ``named`` (a resident session),
    ``role`` (a stage or control profile, or a read-only one), ``malformed``
    (no harness, no class, or an unknown lifecycle -- no worker route at
    all), ``unknown_provider`` (its harness is not registered).
    """
    from src.profiles.catalog import worker_route

    profile_id = str(getattr(profile, "id", "") or "")
    if ":" in profile_id:
        return "retired_project_scoped"
    if getattr(profile, "template", False):
        return "template"
    lifecycle = str(getattr(profile, "lifecycle", "task") or "task").strip()
    if lifecycle == "named":
        return "named"
    if (
        profile_id in _role_profile_ids()
        or getattr(profile, "read_only", False)
        or getattr(profile, "runtime", "") == "supervisor"
    ):
        return "role"
    route = worker_route(
        profile_id,
        harness=getattr(profile, "harness", ""),
        default_class=getattr(profile, "default_class", ""),
        lifecycle=lifecycle,
    )
    if route is None:
        return "malformed"
    if not provider_known:
        return "unknown_provider"
    return None


def _harness_provider(registry: Any, harness_id: str) -> tuple[str, str, bool]:
    """``(provider key, vendor, known)`` for *harness_id* through the live registry."""
    name = str(harness_id or "").strip()
    harness = registry.get(name) if registry is not None and name else None
    if harness is None:
        return name, "", False
    return provider_key(harness), vendor_of(harness), True


# -- the pure pieces ----------------------------------------------------------------


def aggregate_ceiling(profiles: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The provider-wide configured pool ceiling over *profiles* (spec §Backend commands).

    Only enabled ``pool`` profiles count: a disabled pool is sized to zero and
    a task-lifecycle profile has no pool.  One unbounded pool makes the whole
    ceiling unbounded (``max_active`` ``None``), because nothing then caps the
    provider short of the global and project limits.  Bounds stay per
    profile; this total exists so ``max: 2`` across three profiles is never
    mistaken for two workers.
    """
    pools = [
        row for row in profiles
        if row.get("lifecycle") == "pool" and row.get("enabled", True)
    ]
    unbounded = any(row.get("max_active") is None for row in pools)
    return {
        "min_active": sum(int(row.get("min_active") or 0) for row in pools),
        "max_active": None if unbounded else sum(int(row["max_active"]) for row in pools),
        "unbounded": unbounded,
        "pool_profiles": len(pools),
    }


def _empty_supply(*, ready: int | None = 0) -> dict[str, int | None]:
    return {"ready": ready, "idle": 0, "busy": 0, "starting": 0, "draining": 0,
            "unresponsive": 0}


def _add_supply(total: dict[str, Any], part: Mapping[str, Any]) -> None:
    for field in SUPPLY_FIELDS:
        value = part.get(field)
        if value is not None:
            total[field] = (total.get(field) or 0) + value


def session_activity(session: Any, *, now: float, stall_seconds: float) -> str:
    """The supply bucket a live session counts in, as ``_measure_pools`` counts it."""
    from src.pool_claims import idle_pool_claim_loop_stalled

    if session.state == "starting":
        return "starting"
    if session.state == "draining" or session.desired_state == "stopped":
        return "draining"
    if session.task_id or session.claim_phase:
        return "busy"
    if session.lifecycle == "pool" and idle_pool_claim_loop_stalled(
        session, now=now, stall_seconds=stall_seconds
    ):
        return "unresponsive"
    return "idle"


def resolve_provider(snapshot: Mapping[str, Any], name: Any) -> str | None:
    """The snapshot's provider key for *name*: a key, a vendor, or a vendor alias."""
    from src.providers.availability_service import _VENDOR_ALIASES

    key = str(name or "").strip().lower()
    if not key:
        return None
    groups = {row["provider"]: row for row in snapshot.get("providers", [])}
    if key in groups:
        return key
    alias = _VENDOR_ALIASES.get(key)
    if alias in groups:
        return alias
    for provider, row in groups.items():
        if row.get("vendor") == key:
            return provider
    return None


# -- the snapshot ---------------------------------------------------------------


async def build_allocation_snapshot(orchestrator: Any, *, now: float | None = None) -> dict:
    """Everything a provider allocation decision reads, unredacted, in stable order."""
    from src.pool_claims import pool_claim_loop_stall_seconds
    from src.scheduler import PoolKey

    db = orchestrator.db
    registry = getattr(orchestrator, "harness_registry", None)
    availability = getattr(orchestrator, "provider_availability", None)
    measurement = await orchestrator._measure_pools()
    now = time.time() if now is None else now
    stall_seconds = pool_claim_loop_stall_seconds(orchestrator.config.swarm)

    profiles: dict[str, dict[str, Any]] = {}
    groups: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []

    def group(provider: str, vendor: str) -> dict[str, Any]:
        row = groups.get(provider)
        if row is None:
            state = "available"
            if availability is not None:
                try:
                    state = availability.effective_state(provider, now)
                except Exception:  # a state is decoration, never a failure
                    logger.debug("allocation: availability unreadable", exc_info=True)
            row = groups[provider] = {
                "provider": provider,
                "vendor": vendor,
                "state": state,
                "harnesses": set(),
                "profiles": [],
                "manual_agents": [],
                "last_allocation": None,
            }
        return row

    for profile in sorted(measurement.all_profiles, key=lambda p: p.id):
        harness = str(getattr(profile, "harness", "") or "")
        provider, vendor, known = _harness_provider(registry, harness)
        lifecycle = str(getattr(profile, "lifecycle", "task") or "task")
        reason = classify_profile(profile, provider_known=known)
        if reason is not None:
            diagnostics.append(
                {
                    "kind": "profile",
                    "id": profile.id,
                    "harness": harness or None,
                    "lifecycle": lifecycle,
                    "provider": provider if known else None,
                    "reason": reason,
                }
            )
            continue
        row = {
            "profile_id": profile.id,
            "name": getattr(profile, "name", "") or profile.id,
            "harness": harness,
            "lifecycle": lifecycle,
            "enabled": bool(getattr(profile, "enabled", True)),
            "intelligence_class": str(getattr(profile, "default_class", "") or ""),
            "min_active": getattr(profile, "min_active", None),
            "max_active": getattr(profile, "max_active", None),
            "min_per_project": getattr(profile, "min_per_project", None),
            "supply": _empty_supply(ready=0 if lifecycle == "pool" else None),
            "projects": [],
            "sessions": [],
            "pinned": [],
            "preferred": [],
        }
        profiles[profile.id] = row
        entry = group(provider, vendor)
        entry["harnesses"].add(harness)
        entry["profiles"].append(row)

    # Pool supply: exactly the numbers the sizer reads this tick.
    for profile_id, row in profiles.items():
        if row["lifecycle"] != "pool":
            continue
        key = PoolKey(profile_id)
        supply = measurement.supply.get(key)
        if supply is None:
            continue
        row["supply"] = {
            "ready": measurement.demand.get(key, 0),
            "idle": supply.running_idle,
            "busy": supply.running_busy,
            "starting": supply.starting,
            "draining": supply.draining,
            "unresponsive": supply.unresponsive,
        }
        for candidate in sorted(measurement.candidates.get(key, []), key=lambda c: c.project_id):
            local = supply.by_project.get(candidate.project_id)
            row["projects"].append(
                {
                    "project_id": candidate.project_id,
                    "ready": candidate.ready,
                    "idle": getattr(local, "running_idle", 0),
                    "busy": getattr(local, "running_busy", 0),
                    "starting": getattr(local, "starting", 0),
                    "draining": getattr(local, "draining", 0),
                    "unresponsive": getattr(local, "unresponsive", 0),
                }
            )

    # Live sessions: pool sessions from the measurement, task sessions read once.
    live = [s for s in measurement.pool_sessions if s.state in LIVE_SESSION_STATES]
    live_pool_agents = {s.agent_id for s in live if s.agent_id}
    live += await db.list_sessions(lifecycle="task", live_only=True)
    live = [s for s in live if s.profile_id in profiles]
    titles = await db.get_task_titles([s.task_id for s in live if s.task_id])
    task_rows: dict[str, dict[str, dict[str, Any]]] = {}
    for session in sorted(live, key=lambda s: (s.project_id or "", s.id)):
        row = profiles[session.profile_id]
        activity = session_activity(session, now=now, stall_seconds=stall_seconds)
        idle = session.task_id is None and session.claim_phase is None
        idle_since = session.last_activity or session.started_at
        row["sessions"].append(
            {
                "session_id": session.id,
                "project_id": session.project_id,
                "lifecycle": session.lifecycle,
                "state": session.state,
                "activity": activity,
                "agent_id": session.agent_id,
                "task_id": session.task_id,
                "task_title": titles.get(session.task_id) if session.task_id else None,
                "idle_seconds": max(0.0, now - idle_since) if idle and idle_since else None,
                "started_at": session.started_at,
            }
        )
        if row["lifecycle"] != "pool":
            # A task-lifecycle profile has no pool demand: its supply is what runs.
            row["supply"][activity] += 1
            local = task_rows.setdefault(session.profile_id, {}).setdefault(
                session.project_id or "",
                {"project_id": session.project_id, **_empty_supply(ready=None)},
            )
            local[activity] += 1
    for profile_id, rows in task_rows.items():
        profiles[profile_id]["projects"] = [rows[key] for key in sorted(rows)]

    # Explicit intent: what a bulk change must leave where it is.
    for task in await db.list_provider_intent_tasks(statuses=INTENT_TASK_STATUSES):
        row = profiles.get(task["profile_id"])
        if row is None:
            continue
        intent = task["provider_intent"]
        if intent in ("pinned", "preferred"):
            row[intent].append(
                {"task_id": task["id"], "project_id": task["project_id"],
                 "status": task["status"]}
            )

    # Manual agents: definitions no live pool session owns.
    by_id = {profile.id: profile for profile in measurement.all_profiles}
    for agent in sorted(await db.list_agents(), key=lambda a: a.id):
        if (
            agent.role != "worker"
            or getattr(agent, "deleted_at", None) is not None
            or not agent.enabled
            or agent.id in live_pool_agents
        ):
            continue
        profile = by_id.get(agent.profile_id)
        effective_harness = agent.harness or str(getattr(profile, "harness", "") or "")
        effective_class = agent.intelligence_class or str(
            getattr(profile, "default_class", "") or ""
        )
        provider, vendor, known = _harness_provider(registry, effective_harness)
        if not known:
            diagnostics.append(
                {
                    "kind": "agent",
                    "id": agent.id,
                    "harness": effective_harness or None,
                    "lifecycle": None,
                    "provider": None,
                    "reason": "unknown_provider",
                }
            )
            continue
        current = None
        if agent.current_task_id:
            task = await db.get_task(agent.current_task_id)
            status = getattr(getattr(task, "status", None), "value", None)
            if task is not None and status in _ACTIVE_TASK_STATUSES and (
                task.assigned_agent_id == agent.id
            ):
                current = task
        group(provider, vendor)["manual_agents"].append(
            {
                "agent_id": agent.id,
                "name": agent.name,
                "profile_id": agent.profile_id,
                "enabled": agent.enabled,
                "state": agent.state.value.lower(),
                "harness": agent.harness,
                "intelligence_class": agent.intelligence_class,
                "model": agent.model,
                "has_overrides": bool(agent.harness or agent.intelligence_class or agent.model),
                "effective_harness": effective_harness,
                "effective_class": effective_class or None,
                "current_task_id": current.id if current else None,
                "current_task_title": current.title if current else None,
                "current_project_id": current.project_id if current else None,
            }
        )

    try:
        events = await db.get_recent_events(
            limit=ALLOCATION_EVENT_SCAN, event_type=ALLOCATION_EVENT
        )
    except Exception:  # history is decoration, never a failure
        logger.debug("allocation: events unreadable", exc_info=True)
        events = []
    for event in events:
        try:
            payload = json.loads(event.get("payload") or "{}")
        except (TypeError, ValueError):
            continue
        entry = groups.get(str(payload.get("provider") or ""))
        if entry is None or entry["last_allocation"] is not None:
            continue
        entry["last_allocation"] = {
            "event_id": event.get("id"),
            "at": event.get("timestamp"),
            "request_id": payload.get("request_id"),
            "status": payload.get("status") or payload.get("outcome"),
            "actor": payload.get("actor"),
        }

    providers = []
    for provider in sorted(groups):
        entry = groups[provider]
        supply = _empty_supply()
        for row in entry["profiles"]:
            _add_supply(supply, row["supply"])
        entry["harnesses"] = sorted(entry["harnesses"])
        entry["supply"] = supply
        entry["ceiling"] = aggregate_ceiling(entry["profiles"])
        providers.append(entry)

    projects = [
        {
            "project_id": project.id,
            "name": project.name,
            "status": getattr(project.status, "value", project.status),
            # ``projects.preferred_provider`` lands with plan Task 1.
            "preferred_provider": getattr(project, "preferred_provider", None),
            "default_profile_id": project.default_profile_id,
            "max_concurrent_agents": project.max_concurrent_agents,
        }
        for project in sorted(await db.list_projects(), key=lambda p: p.id)
    ]
    return {
        "now": now,
        "global_max_active": orchestrator._pool_global_cap(),
        "providers": providers,
        "projects": projects,
        "diagnostics": diagnostics,
    }


# -- the caller's view ----------------------------------------------------------------


def _intent_view(tasks: list[dict[str, Any]], visible) -> tuple[dict[str, Any], int]:
    shown = [task["task_id"] for task in tasks if visible(task["project_id"])]
    counts = Counter(task["status"] for task in tasks)
    return (
        {
            "count": len(tasks),
            "by_status": {status: counts[status] for status in sorted(counts)},
            "task_ids": shown,
        },
        len(tasks) - len(shown),
    )


def status_view(
    snapshot: Mapping[str, Any],
    *,
    visible_projects: frozenset[str] | None = None,
    provider: str | None = None,
    project_id: str | None = None,
    redacted: bool = False,
) -> dict[str, Any]:
    """``provider_allocation_status``'s result: *snapshot* narrowed for one caller.

    *visible_projects* ``None`` shows every project.  Counts, bounds and the
    ceiling stay fleet-wide either way -- a profile and its pool are global,
    and narrowing them would misreport what the sizer acts on.  What falls
    outside the view is the per-project detail: project rows, sessions, task
    ids and titles, and a manual agent's current task.  Each profile says
    how much of that it ``hidden``.
    """

    def visible(project: str | None) -> bool:
        return visible_projects is None or (project is not None and project in visible_projects)

    providers = []
    for entry in snapshot["providers"]:
        if provider is not None and entry["provider"] != provider:
            continue
        rows = []
        pinned_total = preferred_total = 0
        for row in entry["profiles"]:
            projects = [item for item in row["projects"] if visible(item["project_id"])]
            sessions = [item for item in row["sessions"] if visible(item["project_id"])]
            pinned, hidden_pinned = _intent_view(row["pinned"], visible)
            preferred, hidden_preferred = _intent_view(row["preferred"], visible)
            pinned_total += pinned["count"]
            preferred_total += preferred["count"]
            rows.append(
                {
                    **row,
                    "projects": projects,
                    "sessions": sessions,
                    "pinned": pinned,
                    "preferred": preferred,
                    "hidden": {
                        "projects": len(row["projects"]) - len(projects),
                        "sessions": len(row["sessions"]) - len(sessions),
                        "tasks": hidden_pinned + hidden_preferred,
                    },
                }
            )
        agents = []
        for agent in entry["manual_agents"]:
            hide = agent["current_project_id"] is not None and not visible(
                agent["current_project_id"]
            )
            agents.append(
                {
                    **agent,
                    "current_task_id": None if hide else agent["current_task_id"],
                    "current_task_title": None if hide else agent["current_task_title"],
                    "current_project_id": None if hide else agent["current_project_id"],
                    "redacted": hide,
                }
            )
        providers.append(
            {
                **entry,
                "profiles": rows,
                "manual_agents": agents,
                "pinned_tasks": pinned_total,
                "preferred_tasks": preferred_total,
            }
        )
    return {
        "success": True,
        "now": snapshot["now"],
        "project_id": project_id,
        "redacted": redacted,
        "global_max_active": snapshot["global_max_active"],
        "providers": providers,
        "projects": [row for row in snapshot["projects"] if visible(row["project_id"])],
        "diagnostics": list(snapshot["diagnostics"]),
    }
