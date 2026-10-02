"""Bounded, privacy-preserving observations attached to the existing route snapshot.

Headroom is a conservative upper bound, shared across profiles. It neither
reserves a worker nor changes routing policy; claims and placement own admission.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Any, Mapping

from src.models import TASK_TYPE_VALUES
from src.routing.planner import Snapshot, worker_classes

MAX_PROFILES = 64
MAX_PROVIDERS = 16
MAX_QUOTA_WINDOWS = 8
MAX_SUMMARY_CHARS = 2400
_COUNTS = ("busy", "idle", "starting", "draining", "unresponsive")


def quota_observations(rows, *, now: float, stale_after: float) -> tuple[Mapping, ...]:
    """Keep each window's identity/age; never expose account labels or file paths."""
    result = []
    for row in sorted(rows, key=lambda r: (str(r.get("window")), str(r.get("scope")))):
        observed = float(row["observed_at"])
        seen_at = row.get("last_seen_at")
        seen = observed if seen_at is None else float(seen_at)
        reset = row.get("resets_at")
        age = max(0.0, now - seen)
        status = "stale" if age > stale_after else "fresh"
        if reset is not None and float(reset) <= now:
            status = "reset"
        result.append({
            "window": str(row.get("window") or "unknown")[:80],
            "scope": str(row.get("scope") or "account")[:80],
            "used_percent": float(row["used_percent"]),
            "observed_at": observed, "last_seen_at": seen,
            "age_seconds": round(age, 1), "resets_at": reset,
            "freshness": status, "source": "provider_usage_snapshots",
            "collector": row.get("source") if row.get("source") in {"probe", "transcript"}
            else "unknown",
        })
    return tuple(result)


def live_context(
    snapshot: Snapshot, *, project_id: str, now: float, started_at: float,
    supply: list[dict], active_kinds: Mapping[str, int], project_cap: int,
    project_active: bool, global_cap: int | None, workspace_capacity: int | None,
    quarantine: Mapping[str, float],
    headroom_out: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Summarize compatible workers and compute local idle + constrained launch headroom."""
    fleet: dict[str, Counter] = {}
    local: dict[str, Counter] = {}
    pool_total = project_total = 0
    for row in supply:
        count = int(row["count"])
        fleet.setdefault(row["profile_id"], Counter())[row["bucket"]] += count
        if row["lifecycle"] == "pool":
            pool_total += count
        if row["project_id"] == project_id:
            project_total += count
            local.setdefault(row["profile_id"], Counter())[row["bucket"]] += count

    project_free = max(0, project_cap - project_total) if project_active else 0
    global_free = max(0, global_cap - pool_total) if global_cap is not None else None
    profiles = []
    for profile in sorted(snapshot.profiles, key=lambda p: p.id):
        # Disabled/zero-limit profiles remain visible as compatible with no headroom.
        classes = worker_classes(replace(profile, enabled=True, slots=max(1, profile.slots)))
        if not classes:
            continue
        counts = {k: int(fleet.get(profile.id, {}).get(k, 0)) for k in _COUNTS}
        here = {k: int(local.get(profile.id, {}).get(k, 0)) for k in _COUNTS}
        provider = snapshot.provider(profile.provider)
        until = quarantine.get(profile.id)
        usable = (profile.enabled and profile.slots > 0 and project_active
                  and provider.launchable and provider.state in {"available", "degraded"})
        idle = here["idle"] if usable else 0
        free = max(0, profile.slots - sum(counts.values()))
        limits = [free, project_free]
        if profile.lifecycle == "pool" and global_free is not None:
            limits.append(global_free)
        if profile.lifecycle == "pool" or profile.needs_workspace:
            limits.append(workspace_capacity or 0)
        launch = min(limits) if usable and until is None else 0
        # Routed backlog also competes for existing idle and future launch slots.
        backlog = int(snapshot.backlog.get(profile.id, 0))
        headroom = max(0, idle + launch - backlog)
        if headroom_out is not None:
            headroom_out[profile.id] = headroom
        profiles.append({
            "profile_id": profile.id[:120], "harness": profile.harness[:80],
            "provider": profile.provider[:80], "lifecycle": profile.lifecycle,
            "classes": [c[:120] for c in sorted(classes)[:32]], "enabled": profile.enabled,
            "classes_truncated": len(classes) > 32,
            "limit": profile.slots, **counts, "project_supply": here,
            "routed_backlog": backlog, "idle_claim_capacity": idle,
            "launch_headroom": launch, "effective_headroom": headroom,
            "quarantine_until": until,
            # Free-form quarantine reasons can contain captured terminal output.
            "quarantine_reason": "launch_backoff" if until is not None else None,
        })
    keys = sorted({p["provider"] for p in profiles})
    providers = []
    for key in keys[:MAX_PROVIDERS]:
        facts = snapshot.provider(key)
        providers.append({
            "provider": key, "state": facts.state, "launchable": facts.launchable,
            "reason": facts.reason_code[:120], "updated_at": facts.updated_at,
            "age_seconds": round(max(0, now - facts.updated_at), 1)
            if facts.updated_at else None,
            "source": "provider_availability" if facts.updated_at else "availability_default",
            "quota_status": "observed" if facts.quota else "unknown",
            "quota_source": facts.quota_source,
            "quota": list(facts.quota[:MAX_QUOTA_WINDOWS]),
            "quota_truncated": len(facts.quota) > MAX_QUOTA_WINDOWS,
        })
    kinds = Counter()
    for kind, count in active_kinds.items():
        kinds[kind if kind in TASK_TYPE_VALUES else "unknown"] += count
    return {
        "version": 1, "as_of": now, "collected_from": started_at,
        "collection_seconds": round(max(0, now - started_at), 3),
        "source": "routing_snapshot", "observational": True,
        "sources": {"supply": "sessions+agent_reservations+pending_pool_launches",
                    "backlog": "count_routed_backlog_by_profile",
                    "limits": "profiles+project+swarm", "quarantine": "pool_launch_backoff"},
        "headroom_semantics": "shared upper bound; no reservation; backlog subtracted",
        "project": {"project_id": project_id, "active": project_active,
                    "limit": project_cap, "live_workers": project_total,
                    "launch_headroom": project_free, "workspace_capacity": workspace_capacity,
                    "workspace_source": "count_available_workspaces"},
        "fleet": {"pool_limit": global_cap, "live_pool_workers": pool_total,
                  "launch_headroom": global_free},
        "profiles": profiles[:MAX_PROFILES], "providers": providers,
        "active_work": dict(sorted(kinds.items())),
        "truncated": len(profiles) > MAX_PROFILES or len(keys) > MAX_PROVIDERS,
    }


def summarize_context(context: Mapping[str, Any]) -> str:
    """A short equivalent for humans; bounded independently of fleet size."""
    project = context["project"]
    lines = [
        f"As of {context['as_of']:.0f}; observations, no reservations; headroom shared.",
        f"Project workers {project['live_workers']}/{project['limit']}; "
        f"free workspaces {project['workspace_capacity']}; "
        f"launch headroom {project['launch_headroom']}.",
    ]
    for provider in context["providers"][:6]:
        windows = ", ".join(
            f"{q['window']}({q['scope']}) {q['used_percent']:g}% {q['freshness']} "
            f"age {q['age_seconds']:g}s reset {q['resets_at']}"
            for q in provider["quota"][:2]
        ) or "quota unknown"
        lines.append(f"{provider['provider']}: {provider['state']} "
                     f"{provider['reason']}; {windows}.")
    for profile in context["profiles"][:8]:
        lines.append(
            f"{profile['profile_id']}: enabled={profile['enabled']} limit={profile['limit']} "
            f"busy/idle/starting/draining={profile['busy']}/{profile['idle']}/"
            f"{profile['starting']}/{profile['draining']} backlog={profile['routed_backlog']} "
            f"headroom={profile['effective_headroom']}"
            + (" quarantine=launch_backoff" if profile["quarantine_until"] else "") + "."
        )
    lines.append(f"Active work by kind: {dict(context['active_work'])}.")
    if context["truncated"] or len(context["profiles"]) > 8 or len(context["providers"]) > 6:
        lines.append("Additional rows omitted; see structured live_context.")
    return "\n".join(lines)[:MAX_SUMMARY_CHARS]
