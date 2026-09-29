"""Provider allocation snapshot: the read half of provider-level worker controls.

Spec: ``projects/agent-queue/specs/provider-worker-allocation-controls.md``
(vault), plan Task 6.  An operator changing providers needs one answer to
"what runs where" before touching anything: which ordinary worker profiles
each provider has, what their pools hold right now, which tasks are pinned to
them, which manual agent definitions draw on them, and what each project
prefers.  This module builds that answer; it decides nothing and writes
nothing.

Three pieces, the first shared by the other two:

* :func:`build_allocation_snapshot` reads everything once -- one
  ``_measure_pools()`` call for pool supply, the live task sessions, the
  pinned and preferred tasks, the agent definitions, the projects and the
  last allocation events -- into one unredacted, JSON-shaped snapshot.
* :func:`status_view` narrows that snapshot for a caller: a view filter or a
  project-scoped caller keeps every fleet-wide count (profiles and pools are
  global) but drops other projects' rows, sessions and task ids, counting
  what it hid.
* :func:`plan_allocation_preview` (plan Task 8) is a pure function of a
  snapshot and a request: which profiles one allocation selects, their
  before and after rows, what happens to each live session, the pins and
  manual agents it leaves alone, and a SHA-256 preview token over the
  request plus everything the decision observed.  Apply (plan Task 9)
  rebuilds it from the request :class:`PreviewRegistry` kept for the token
  and refuses a token that no longer matches.

The allocation key is the **harness** provider key (``harness.base or
harness.id``: ``claude``, ``codex``), the same key availability and re-routes
store; ``sessions.provider`` is transport (``tmux``) and is never read here.
Only ordinary worker profiles are selectable (:func:`classify_profile`);
every other profile is listed in ``diagnostics`` with the reason.
"""

from __future__ import annotations

import asyncio
import hashlib
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
            # The router binding (mandatory routing §8); there is no default profile.
            "assignment_playbook_id": project.assignment_playbook_id,
            "max_concurrent_agents": project.max_concurrent_agents,
        }
        for project in sorted(await db.list_projects(), key=lambda p: p.id)
    ]
    return {
        "now": now,
        "global_max_active": orchestrator._pool_global_cap(),
        # ``pool_set_lifecycle`` refuses ``pool`` while the swarm is off; so does the preview.
        "swarm_enabled": bool(getattr(orchestrator.config.swarm, "enabled", True)),
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


# -- the preview (plan Task 8) --------------------------------------------------------

#: How a request treats the sessions it displaces (spec §Drain semantics), gentlest first.
DRAIN_MODES = ("graceful", "idle-now", "interrupt-busy")
#: The lifecycle a request gives every selected profile (spec ``participation``).
PARTICIPATIONS = ("pool", "task")
#: What ``receive_new_work`` does to one project's preferred provider.
PREFERENCE_MODES = ("prefer", "clear")
#: The least scope that may preview or apply a request (spec §Authorization).
OPERATOR_SCOPE = "operator"
PROJECT_ADMIN_SCOPE = "project_admin"
#: The profile fields an allocation compares before and after.
PROFILE_FIELDS = ("lifecycle", "enabled", "min_active", "max_active", "min_per_project")
#: Bumped whenever the token's canonical input changes shape, so a token minted
#: under one layout can never match a preview built under another.
PREVIEW_TOKEN_VERSION = 1


class AllocationRequestError(ValueError):
    """A provider allocation request the preview refuses; the message is the operator's."""


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AllocationRequestError(f"{name} must be an integer")
    return value


def _normalize_bounds(bounds: Any) -> dict[str, int | None]:
    """``bounds`` with only the keys the caller gave; ``max: None`` is an unbounded pool.

    The per-value checks are ``aq pool scale``'s; whether an omitted bound
    passes against a profile's current one is checked per profile.
    """
    if not isinstance(bounds, Mapping):
        raise AllocationRequestError("bounds must be an object with min and/or max")
    unknown = sorted(set(bounds) - {"min", "max"})
    if unknown:
        raise AllocationRequestError(f"bounds accepts only min and max, not {', '.join(unknown)}")
    if not bounds:
        raise AllocationRequestError("nothing to change: pass bounds.min and/or bounds.max")
    clean: dict[str, int | None] = {}
    if "min" in bounds:
        if bounds["min"] is None:
            raise AllocationRequestError("min must be >= 0")
        low = _integer(bounds["min"], "bounds.min")
        if low < 0:
            raise AllocationRequestError("min must be >= 0")
        clean["min"] = low
    if "max" in bounds:
        high = bounds["max"]
        if isinstance(high, str) and high.strip().lower() == "unbounded":
            high = None
        if high is not None:
            high = _integer(high, "bounds.max")
            if high < 1:
                raise AllocationRequestError("max must be >= 1")
        clean["max"] = high
    if clean.get("max") is not None and "min" in clean and clean["max"] < clean["min"]:
        raise AllocationRequestError("max must be >= min")
    return clean


def normalize_allocation_request(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The canonical form of an allocation request, or :class:`AllocationRequestError`.

    The request is the spec's (§Backend commands): ``provider``;
    ``profile_ids`` (``None`` selects every eligible profile of the provider);
    ``participation`` ``pool`` / ``task``; ``bounds`` ``{min, max}``;
    ``receive_new_work`` ``{project_id, mode: prefer | clear}``; ``drain``
    (default ``graceful``); and ``allow_pinned_wait``.  Normalizing is
    structural and idempotent -- whether the provider, profiles and project
    exist is the snapshot's question -- and unknown keys (a scope-injected
    ``project_id``) are ignored.  Two spellings of one request normalize to one
    canonical dict, which is what the preview token hashes.
    """
    if not isinstance(raw, Mapping):
        raise AllocationRequestError("request must be an object")
    provider = str(raw.get("provider") or "").strip().lower()
    if not provider:
        raise AllocationRequestError("provider is required")

    profile_ids = raw.get("profile_ids")
    if profile_ids is not None:
        if isinstance(profile_ids, str):
            profile_ids = profile_ids.split(",")
        if not isinstance(profile_ids, list | tuple):
            raise AllocationRequestError("profile_ids must be a list of profile ids or null")
        profile_ids = sorted({str(pid).strip() for pid in profile_ids} - {""})
        if not profile_ids:
            raise AllocationRequestError(
                "empty selection: profile_ids names no profile "
                "(omit it to select every eligible profile of the provider)"
            )

    participation = raw.get("participation")
    if participation is not None:
        participation = str(participation).strip().lower()
        if participation not in PARTICIPATIONS:
            raise AllocationRequestError("participation must be pool or task")

    bounds = raw.get("bounds")
    if bounds is not None:
        bounds = _normalize_bounds(bounds)
        if participation == "task":
            raise AllocationRequestError(
                "bounds apply only to pool profiles; participation task clears them"
            )

    receive = raw.get("receive_new_work")
    if receive is not None:
        if not isinstance(receive, Mapping):
            raise AllocationRequestError("receive_new_work must be an object")
        mode = str(receive.get("mode") or "").strip().lower()
        if mode not in PREFERENCE_MODES:
            raise AllocationRequestError("receive_new_work.mode must be prefer or clear")
        project_id = str(receive.get("project_id") or "").strip()
        if not project_id:
            raise AllocationRequestError("receive_new_work.project_id is required")
        receive = {"project_id": project_id, "mode": mode}

    drain = raw.get("drain")
    drain = "graceful" if drain in (None, "") else str(drain).strip().lower().replace("_", "-")
    if drain not in DRAIN_MODES:
        raise AllocationRequestError(f"drain must be one of {', '.join(DRAIN_MODES)}")

    allow = raw.get("allow_pinned_wait")
    if allow is not None and not isinstance(allow, bool):
        raise AllocationRequestError("allow_pinned_wait must be a boolean")

    if participation is None and bounds is None and receive is None:
        raise AllocationRequestError(
            "nothing to change: pass participation, bounds or receive_new_work"
        )
    return {
        "provider": provider,
        "profile_ids": profile_ids,
        "participation": participation,
        "bounds": bounds,
        "receive_new_work": receive,
        "drain": drain,
        "allow_pinned_wait": bool(allow),
    }


def allocation_scope(request: Mapping[str, Any]) -> str:
    """The least scope that may preview or apply a normalized *request* (spec §Authorization).

    A lifecycle or bounds edit rewrites global profiles, and interrupting busy
    work is never delegated: both need :data:`OPERATOR_SCOPE` (the local
    operator or the global admin).  A change to one project's preference alone
    needs :data:`PROJECT_ADMIN_SCOPE` for that project.
    """
    if request.get("participation") is not None or request.get("bounds") is not None:
        return OPERATOR_SCOPE
    if request.get("drain") == "interrupt-busy":
        return OPERATOR_SCOPE
    return PROJECT_ADMIN_SCOPE


def preview_token(request: Mapping[str, Any], observed: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical JSON of *request* plus what the preview *observed*."""
    payload = {"version": PREVIEW_TOKEN_VERSION, "request": request, "observed": observed}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _select(
    snapshot: Mapping[str, Any],
    provider: str,
    eligible: Mapping[str, Any],
    profile_ids: list[str] | None,
) -> list[str]:
    """The selected profile ids, or the refusal the spec requires (§Apply algorithm 2)."""
    if profile_ids is None:
        if not eligible:
            raise AllocationRequestError(
                f"empty selection: provider {provider} has no ordinary worker profiles"
            )
        return sorted(eligible)
    owners = {
        row["profile_id"]: group["provider"]
        for group in snapshot["providers"]
        for row in group["profiles"]
    }
    skipped = {
        row["id"]: row["reason"] for row in snapshot["diagnostics"] if row["kind"] == "profile"
    }
    for profile_id in profile_ids:
        if profile_id in eligible:
            continue
        if profile_id in owners:
            raise AllocationRequestError(
                f"profile {profile_id} belongs to provider {owners[profile_id]}, not {provider}"
            )
        if profile_id in skipped:
            raise AllocationRequestError(
                f"profile {profile_id} is not an ordinary worker profile "
                f"({skipped[profile_id]}); provider allocation never selects it"
            )
        raise AllocationRequestError(f"unknown profile {profile_id!r}")
    return list(profile_ids)


def _bounded(profile_id: str, state: Mapping[str, Any], bounds: Mapping[str, Any]) -> dict:
    """The bound updates ``aq pool scale`` would write for *bounds*, validated as it validates."""
    low = bounds["min"] if "min" in bounds else state["min_active"]
    high = bounds["max"] if "max" in bounds else state["max_active"]
    if low is None or low < 0:
        hint = " (its min_active is unset; pass bounds.min)" if low is None else ""
        raise AllocationRequestError(f"{profile_id}: min must be >= 0{hint}")
    if high is not None and high < 1:
        raise AllocationRequestError(f"{profile_id}: max must be >= 1")
    if high is not None and high < low:
        raise AllocationRequestError(f"{profile_id}: max must be >= min")
    updates: dict[str, Any] = {}
    if "min" in bounds:
        updates["min_active"] = bounds["min"]
    if "max" in bounds:
        updates["max_active"] = bounds["max"]
    return updates


def _effective_max(state: Mapping[str, Any], project_cap: Any, global_cap: Any) -> int | None:
    """``aq pool scale``'s ``effective_max_active``: the least of the three ceilings.

    ``None`` for a task-lifecycle profile (no pool) and for a pool nothing bounds.
    """
    if state["lifecycle"] != "pool":
        return None
    ceilings = [cap for cap in (state["max_active"], project_cap, global_cap) if cap is not None]
    return min(ceilings) if ceilings else None


def _scale_now_victims(
    sessions: Iterable[Mapping[str, Any]],
    after: Mapping[str, Any],
    projects: Mapping[str, Mapping[str, Any]],
    global_cap: Any,
) -> set[str]:
    """The sessions ``aq pool scale --now`` terminates under the *after* bounds.

    The same arithmetic as ``_cmd_pool_scale``: per project, running pool
    sessions beyond the project-effective max, taken from the idle ones
    (no task), oldest first.  Busy work is never among them.
    """
    running: dict[Any, list[Mapping[str, Any]]] = {}
    for session in sessions:
        if session["lifecycle"] == "pool" and session["state"] == "running":
            running.setdefault(session["project_id"], []).append(session)
    victims: set[str] = set()
    for project_id, live in running.items():
        project = projects.get(project_id)
        effective = (
            _effective_max(after, project.get("max_concurrent_agents"), global_cap)
            if project is not None
            else after["max_active"]
        )
        if effective is None:
            continue
        idle = sorted(
            (session for session in live if not session["task_id"]),
            key=lambda session: session["started_at"] or 0,
        )
        victims.update(session["session_id"] for session in idle[: max(0, len(live) - effective)])
    return victims


def _session_action(
    session: Mapping[str, Any], *, leaving: bool, victim: bool, drain: str
) -> str:
    """What the request does to one live session of a changed profile (spec §Drain semantics).

    Leaving the pool marks every live pool session stopped -- the claim race
    closes first -- and the drain mode decides only when each one goes:
    ``stop`` (reconciler teardown), ``terminate`` (idle, now),
    ``stop_after_task`` (busy, finishes first) or ``interrupt`` (busy, the
    exact authorized set).  A lowered bound stops only what ``aq pool scale
    --now`` would (``terminate``), and only for an immediate drain; the
    graceful drain leaves the excess to the sizer's grace window.
    """
    if session["lifecycle"] != "pool":
        return "none"
    if not leaving:
        return "terminate" if victim else "none"
    activity = session["activity"]
    if activity == "draining":
        return "none"
    if activity == "starting":
        return "stop"
    if activity == "busy":
        return "interrupt" if drain == "interrupt-busy" else "stop_after_task"
    return "stop" if drain == "graceful" else "terminate"


def _warning(
    code: str, message: str, subjects: Iterable[str], *, blocking: bool = False,
    acknowledged: bool = False,
) -> dict[str, Any]:
    return {
        "code": code,
        "blocking": blocking,
        "acknowledged": acknowledged,
        "message": message,
        "subjects": sorted(subjects),
    }


def _fingerprint(
    snapshot: Mapping[str, Any],
    eligible: Mapping[str, Mapping[str, Any]],
    selected: list[str],
    *,
    structural: bool,
) -> dict[str, Any]:
    """Everything the preview's decision read, minus the clock and pool demand.

    Always: every eligible profile's lifecycle, bounds and enabled flag (the
    selection, the rows and the ceiling), every project's preference and cap,
    the global cap and the swarm switch.  For a lifecycle or bounds change
    also the selected profiles' live sessions (id, project, state, whether
    busy, task, start), their explicit pins and the manual agents drawing on
    them.  A preference-only change touches no session, so worker churn does
    not stale it.  An idle worker that merely looks stalled is still idle.
    """
    observed: dict[str, Any] = {
        "global_max_active": snapshot.get("global_max_active"),
        "swarm_enabled": bool(snapshot.get("swarm_enabled", True)),
        "profiles": [
            {"profile_id": profile_id, **{field: eligible[profile_id][field]
                                          for field in PROFILE_FIELDS}}
            for profile_id in sorted(eligible)
        ],
        "projects": [
            {
                "project_id": project["project_id"],
                "preferred_provider": project.get("preferred_provider"),
                "max_concurrent_agents": project.get("max_concurrent_agents"),
            }
            for project in sorted(snapshot["projects"], key=lambda row: row["project_id"])
        ],
    }
    if not structural:
        return observed
    chosen = set(selected)
    observed["sessions"] = sorted(
        (
            {
                "session_id": session["session_id"],
                "profile_id": profile_id,
                "project_id": session["project_id"],
                "lifecycle": session["lifecycle"],
                "state": session["state"],
                "activity": "idle" if session["activity"] == "unresponsive"
                else session["activity"],
                "task_id": session["task_id"],
                "started_at": session["started_at"],
            }
            for profile_id in selected
            for session in eligible[profile_id]["sessions"]
        ),
        key=lambda row: row["session_id"],
    )
    observed["pinned"] = sorted(
        (
            {"task_id": task["task_id"], "profile_id": profile_id,
             "project_id": task["project_id"], "status": task["status"]}
            for profile_id in selected
            for task in eligible[profile_id]["pinned"]
        ),
        key=lambda row: (row["task_id"], row["profile_id"]),
    )
    observed["manual_agents"] = sorted(
        (
            {"agent_id": agent["agent_id"], "profile_id": agent["profile_id"]}
            for group in snapshot["providers"]
            for agent in group["manual_agents"]
            if agent["profile_id"] in chosen
        ),
        key=lambda row: row["agent_id"],
    )
    return observed


def plan_allocation_preview(snapshot: Mapping[str, Any], request: Mapping[str, Any]) -> dict:
    """What one provider allocation *request* would do against *snapshot*.  Pure.

    Returns ``{"success": False, "error": ...}`` for a request the spec
    refuses (unknown provider, a profile of another provider, a control or
    otherwise ineligible profile, an empty selection, bounds that fail
    ``aq pool scale``'s validation, an unknown project, ``pool`` while the
    swarm is off).  Otherwise the preview:

    * ``request`` -- canonical, provider resolved to its key -- and
      ``required_scope`` (:func:`allocation_scope`);
    * ``profiles`` -- every eligible profile of the provider with
      ``selected``, ``before``, ``after`` and ``changed_fields``;
    * ``ceiling`` -- the provider-wide configured ceiling before and after,
      so per-profile bounds are never mistaken for a total;
    * ``project_limits`` -- each project's effective max for every changed
      pool profile, before and after (``aq pool scale``'s arithmetic);
    * ``sessions`` -- every live session of a changed profile and its
      ``action``; ``busy`` -- the busy sessions and tasks the request stops,
      which an interrupt must authorize exactly;
    * ``pinned`` -- explicit pins on changed profiles (never rewritten), each
      ``waits`` when it is READY on a profile leaving the pool;
    * ``manual_agents`` -- definitions whose push eligibility changes;
    * ``preference`` -- the project's preferred provider before and after;
    * ``warnings`` and ``blocked`` (a blocking warning not acknowledged);
    * ``preview_token`` -- :func:`preview_token` over the request and
      :func:`_fingerprint`.
    """
    try:
        return _plan(snapshot, normalize_allocation_request(request))
    except AllocationRequestError as exc:
        return {"success": False, "error": str(exc)}


def _plan(snapshot: Mapping[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    provider = resolve_provider(snapshot, request["provider"])
    if provider is None:
        known = ", ".join(row["provider"] for row in snapshot["providers"]) or "none"
        raise AllocationRequestError(
            f"unknown provider {request['provider']!r}; known providers: {known}"
        )
    request = {**request, "provider": provider}
    group = next(row for row in snapshot["providers"] if row["provider"] == provider)
    eligible = {row["profile_id"]: row for row in group["profiles"]}
    selected = _select(snapshot, provider, eligible, request["profile_ids"])
    chosen = set(selected)
    projects = {row["project_id"]: row for row in snapshot["projects"]}
    global_cap = snapshot.get("global_max_active")
    participation, bounds, drain = (
        request["participation"], request["bounds"], request["drain"]
    )
    structural = participation is not None or bounds is not None

    preference = None
    receive = request["receive_new_work"]
    if receive is not None:
        project = projects.get(receive["project_id"])
        if project is None:
            raise AllocationRequestError(f"Project '{receive['project_id']}' not found")
        before_provider = project.get("preferred_provider")
        after_provider = provider if receive["mode"] == "prefer" else None
        preference = {
            "project_id": receive["project_id"],
            "mode": receive["mode"],
            "before": before_provider,
            "after": after_provider,
            "changed": before_provider != after_provider,
        }
    if participation == "pool" and not snapshot.get("swarm_enabled", True):
        raise AllocationRequestError("cannot set lifecycle to pool while swarm.enabled is false")

    rows: list[dict[str, Any]] = []
    bounds_skipped: list[str] = []
    for profile_id in sorted(eligible):
        source = eligible[profile_id]
        before = {field: source[field] for field in PROFILE_FIELDS}
        after = dict(before)
        if profile_id in chosen:
            if participation == "task":
                # ``pool set-lifecycle task`` clears the pool-only sizing keys.
                after.update(lifecycle="task", min_active=None, max_active=None,
                             min_per_project=None)
            elif participation == "pool":
                after["lifecycle"] = "pool"
            if bounds is not None:
                if after["lifecycle"] == "pool":
                    after.update(_bounded(profile_id, after, bounds))
                elif request["profile_ids"] is not None:
                    raise AllocationRequestError(
                        f"no pool profile '{profile_id}': bounds apply only to pool profiles "
                        "(pass participation pool)"
                    )
                else:
                    bounds_skipped.append(profile_id)
        changed = [field for field in PROFILE_FIELDS if before[field] != after[field]]
        rows.append(
            {
                "profile_id": profile_id,
                "name": source.get("name") or profile_id,
                "harness": source.get("harness"),
                "intelligence_class": source.get("intelligence_class"),
                "selected": profile_id in chosen,
                "changed": bool(changed),
                "changed_fields": changed,
                "before": before,
                "after": after,
            }
        )

    limits: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    busy_sessions: list[str] = []
    busy_tasks: list[str] = []
    pinned: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    changed_rows = [row for row in rows if row["changed"]] if structural else []
    for row in changed_rows:
        before, after = row["before"], row["after"]
        if "pool" not in (before["lifecycle"], after["lifecycle"]):
            continue
        for project_id in sorted(projects):
            cap = projects[project_id].get("max_concurrent_agents")
            limits.append(
                {
                    "project_id": project_id,
                    "profile_id": row["profile_id"],
                    "max_concurrent_agents": cap,
                    "lifecycle_before": before["lifecycle"],
                    "lifecycle_after": after["lifecycle"],
                    "effective_max_before": _effective_max(before, cap, global_cap),
                    "effective_max_after": _effective_max(after, cap, global_cap),
                }
            )
    limits.sort(key=lambda item: (item["project_id"], item["profile_id"]))
    for row in changed_rows:
        profile_id, before, after = row["profile_id"], row["before"], row["after"]
        source = eligible[profile_id]
        leaving = before["lifecycle"] == "pool" and after["lifecycle"] == "task"
        victims: set[str] = set()
        if not leaving and after["lifecycle"] == "pool" and bounds is not None and (
            drain != "graceful"
        ):
            victims = _scale_now_victims(source["sessions"], after, projects, global_cap)
        for session in source["sessions"]:
            action = _session_action(
                session, leaving=leaving, victim=session["session_id"] in victims, drain=drain
            )
            sessions.append(
                {
                    "session_id": session["session_id"],
                    "project_id": session["project_id"],
                    "profile_id": profile_id,
                    "lifecycle": session["lifecycle"],
                    "state": session["state"],
                    "activity": session["activity"],
                    "task_id": session["task_id"],
                    "task_title": session.get("task_title"),
                    "action": action,
                }
            )
            if leaving and session["lifecycle"] == "pool" and session["activity"] == "busy":
                busy_sessions.append(session["session_id"])
                if session["task_id"]:
                    busy_tasks.append(session["task_id"])
        for task in source["pinned"]:
            pinned.append(
                {
                    "task_id": task["task_id"],
                    "project_id": task["project_id"],
                    "profile_id": profile_id,
                    "status": task["status"],
                    "waits": leaving and task["status"] == "READY",
                }
            )
    sessions.sort(key=lambda item: item["session_id"])
    pinned.sort(key=lambda item: (item["task_id"], item["profile_id"]))

    # Push work reaches a manual agent only while its profile is task-lifecycle.
    relifecycled = {
        row["profile_id"]: row for row in changed_rows if "lifecycle" in row["changed_fields"]
    }
    for entry in snapshot["providers"]:
        for agent in entry["manual_agents"]:
            row = relifecycled.get(agent["profile_id"])
            if row is None:
                continue
            manual.append(
                {
                    "agent_id": agent["agent_id"],
                    "name": agent.get("name"),
                    "profile_id": agent["profile_id"],
                    "provider": entry["provider"],
                    "effective_harness": agent.get("effective_harness"),
                    "state": agent.get("state"),
                    "current_task_id": agent.get("current_task_id"),
                    "push_before": row["before"]["lifecycle"] == "task",
                    "push_after": row["after"]["lifecycle"] == "task",
                }
            )
    manual.sort(key=lambda item: item["agent_id"])

    warnings: list[dict[str, Any]] = []
    waiting = [task["task_id"] for task in pinned if task["waits"]]
    if waiting:
        warnings.append(
            _warning(
                "pinned_ready_wait",
                f"{len(waiting)} pinned READY task(s) stay on {provider} profiles this request "
                "takes out of the pool; allocation never rewrites a pin, so they will not move "
                "to another provider (acknowledge with allow_pinned_wait)",
                waiting,
                blocking=True,
                acknowledged=request["allow_pinned_wait"],
            )
        )
    if manual:
        warnings.append(
            _warning(
                "manual_agent_push_changes",
                f"{len(manual)} manual agent definition(s) draw on a profile whose lifecycle "
                "changes, so whether they receive push work changes; their saved overrides are "
                "left alone",
                (agent["agent_id"] for agent in manual),
            )
        )
    if bounds_skipped:
        warnings.append(
            _warning(
                "bounds_skipped",
                "bounds apply per pool profile; these task-lifecycle profiles keep theirs",
                bounds_skipped,
            )
        )
    if not any(row["changed"] for row in rows) and not (preference and preference["changed"]):
        warnings.append(_warning("no_change", "the request changes nothing", ()))

    return {
        "success": True,
        "now": snapshot.get("now"),
        "provider": provider,
        "vendor": group.get("vendor") or "",
        "state": group.get("state"),
        "request": request,
        "required_scope": allocation_scope(request),
        "global_max_active": global_cap,
        "selected": selected,
        "profiles": rows,
        "ceiling": {
            "before": aggregate_ceiling(row["before"] for row in rows),
            "after": aggregate_ceiling(row["after"] for row in rows),
        },
        "project_limits": limits,
        "sessions": sessions,
        "busy": {"session_ids": sorted(busy_sessions), "task_ids": sorted(busy_tasks)},
        "pinned": pinned,
        "manual_agents": manual,
        "preference": preference,
        "warnings": warnings,
        "blocked": any(item["blocking"] and not item["acknowledged"] for item in warnings),
        "preview_token": preview_token(
            request, _fingerprint(snapshot, eligible, selected, structural=structural)
        ),
    }


# -- apply (plan Task 9) --------------------------------------------------------------

#: How long an issued preview stays appliable.  A structural token stales on
#: any session churn within minutes anyway; the bound only keeps the registry
#: from holding requests nobody will apply.
PREVIEW_TTL_SECONDS = 900.0
#: The most issued previews one daemon remembers (oldest dropped first).
PREVIEW_CAPACITY = 256


class PreviewRegistry:
    """The requests this daemon issued preview tokens for, keyed by token.

    Apply takes only a token (spec §Backend commands: no mutable selector),
    so it needs the request the token was issued for to rebuild the preview.
    The registry is in memory, per daemon (:func:`preview_registry`), and that
    is durable enough: a token is a promise about the fleet *now* -- any
    session churn stales a structural one within minutes -- so a daemon
    restart costs at most one unapplied preview, and apply then fails closed
    (``preview_unknown``) instead of acting on a request it cannot verify.
    Keeping it out of the database also keeps preview free of writes.

    ``lock`` serializes applies, so two operators cannot interleave the
    profile edits and compensations of two allocations.
    """

    def __init__(
        self,
        *,
        ttl: float = PREVIEW_TTL_SECONDS,
        capacity: int = PREVIEW_CAPACITY,
        clock=time.time,
    ) -> None:
        self._ttl = ttl
        self._capacity = capacity
        self._clock = clock
        self._issued: dict[str, tuple[float, dict[str, Any]]] = {}
        self.lock = asyncio.Lock()

    def issue(self, token: str, request: Mapping[str, Any]) -> None:
        """Remember that *token* was issued for the canonical *request*."""
        self._expire()
        self._issued.pop(token, None)
        self._issued[token] = (self._clock(), json.loads(json.dumps(request)))
        while len(self._issued) > self._capacity:
            self._issued.pop(next(iter(self._issued)))

    def lookup(self, token: str) -> dict[str, Any] | None:
        """The request *token* was issued for, or ``None`` (never issued, expired, used)."""
        self._expire()
        entry = self._issued.get(token)
        return json.loads(json.dumps(entry[1])) if entry is not None else None

    def consume(self, token: str) -> None:
        """Forget *token*: an applied preview is not applied twice."""
        self._issued.pop(token, None)

    def _expire(self) -> None:
        cutoff = self._clock() - self._ttl
        for token in [token for token, (at, _) in self._issued.items() if at < cutoff]:
            del self._issued[token]


def preview_registry(orchestrator: Any) -> PreviewRegistry:
    """The daemon's one :class:`PreviewRegistry`.

    It lives on the orchestrator, not a ``CommandHandler``: the daemon builds
    several handlers (CLI API, MCP, the typed routes) and a preview issued
    through one must be appliable through another.
    """
    registry = getattr(orchestrator, "_allocation_previews", None)
    if registry is None:
        registry = PreviewRegistry()
        orchestrator._allocation_previews = registry
    return registry


def busy_authorization_error(
    preview: Mapping[str, Any], authorized: Iterable[str] | None
) -> tuple[str, str] | None:
    """``(error_code, message)`` unless *authorized* names exactly the preview's busy set.

    The busy set is the sessions the preview would ``interrupt`` (spec
    invariant 5).  Each authorized id may be a session id or the id of the
    task that session runs; every busy session must be named and nothing
    else may be.  ``None`` when the authorization is exact -- or when there
    is nothing to authorize and nothing was given.
    """
    names = sorted({str(item).strip() for item in (authorized or ())} - {""})
    busy = [row for row in preview.get("sessions", []) if row.get("action") == "interrupt"]
    if preview["request"].get("drain") != "interrupt-busy":
        if names:
            return (
                "busy_authorization_unexpected",
                "authorize_busy_interrupt applies only to drain interrupt-busy",
            )
        return None
    if not busy:
        if names:
            return (
                "busy_authorization_mismatch",
                "the preview interrupts no busy session; authorize nothing",
            )
        return None
    matched: set[str] = set()
    unknown: list[str] = []
    for name in names:
        hits = {row["session_id"] for row in busy if name in (row["session_id"], row.get("task_id"))}
        if not hits:
            unknown.append(name)
        matched |= hits
    missing = sorted(row["session_id"] for row in busy if row["session_id"] not in matched)
    expected = ", ".join(sorted(row["session_id"] for row in busy))
    if not names:
        return (
            "busy_authorization_required",
            (
                f"drain interrupt-busy interrupts {len(busy)} busy session(s); authorize "
                f"exactly that set with authorize_busy_interrupt: {expected}"
            ),
        )
    if unknown or missing:
        parts = []
        if unknown:
            parts.append(f"not in the busy set: {', '.join(unknown)}")
        if missing:
            parts.append(f"busy but not authorized: {', '.join(missing)}")
        return (
            "busy_authorization_mismatch",
            f"authorize_busy_interrupt must name exactly the preview's busy set ({expected}); "
            + "; ".join(parts),
        )
    return None
