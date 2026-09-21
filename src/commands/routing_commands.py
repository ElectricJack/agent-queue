"""Routing read command: what a task needs and what can serve it.

Policy lives in the ``default-assignment-routing`` playbook.  This module only
reports facts a playbook step can act on — the task's current routing fields,
whether its class is explicit, and the class/provider/profile catalog of
ordinary workers that could execute it — and the one deterministic tie-break
the playbook cannot express (which profile serves an explicit class).  Spec:
``docs/superpowers/specs/2026-09-06-assignment-routing-as-playbook.md``.
"""

from __future__ import annotations

from typing import Any

from src.models import AgentState
from src.sessions.spec import _infer_provider_from_harness

#: Stage profiles never offered as an ordinary worker route.
_CONTROL_PROFILES = frozenset(
    {"supervisor", "triage", "reviewer", "final-reviewer", "playbook-compiler", "spec-ingest"}
)


def profile_provider(profile, harness_registry=None, project_id: str | None = None) -> str:
    """Return the provider selected by a profile's harness."""

    harness_id = getattr(profile, "harness", "") or ""
    if not harness_id:
        return ""
    harness = harness_registry.get(harness_id, project_id) if harness_registry else None
    if harness is None:
        harness = type(
            "HarnessRef", (), {"id": harness_id, "command": harness_id, "provider": ""}
        )()
    return str(getattr(harness, "provider", "") or _infer_provider_from_harness(harness))


def _effective_profiles(profiles):
    """Profiles are global; rows still carrying a retired ``project:`` id resolve nowhere."""
    return [profile for profile in profiles if ":" not in profile.id]


def _worker_profile(profile) -> bool:
    return not (
        profile.id in _CONTROL_PROFILES
        or getattr(profile, "runtime", "") == "supervisor"
        or getattr(profile, "lifecycle", "task") not in {"task", "pool"}
        or not getattr(profile, "harness", "")
    )


def _class_mapping(cls, profile, provider: str) -> dict | None:
    mapping = cls.mapping.get("codex") if profile.harness == "codex" else None
    mapping = mapping or cls.mapping.get(provider)
    return mapping if isinstance(mapping, dict) and mapping.get("model") else None


def build_route_options(
    project_id: str,
    profiles,
    agents,
    harness_registry,
    intelligence_classes,
    availability=None,
) -> list[dict[str, Any]]:
    """One row per (class, provider, profile) an ordinary worker can execute.

    A profile with a fixed ``default_class`` offers that class only; a generic
    task-lifecycle profile offers every class its provider maps.  A pool
    profile without a fixed class offers nothing — a pool worker only claims
    its own class, so there would be nothing for it to claim.  Disabled pools
    remain in this diagnostic catalog, marked ``enabled=False``: callers must
    preserve an existing explicit pin, while automatic selection filters them
    out.

    With *availability* (``Orchestrator.provider_availability``) every row is
    annotated with ``provider_key`` (the harness login), ``provider_state`` and
    ``launchable`` (provider-failover D11 mechanism 3).  Like ``enabled``, a
    row on an unavailable provider stays in the catalog and automatic
    selection filters it out.
    """

    enabled_agents = [
        agent for agent in agents
        if agent.enabled and agent.role == "worker" and agent.deleted_at is None
    ]
    rows: list[dict[str, Any]] = []
    for profile in _effective_profiles(profiles):
        if not _worker_profile(profile):
            continue
        provider = profile_provider(profile, harness_registry, project_id)
        if not provider:
            continue
        fixed_class = (getattr(profile, "default_class", "") or "").strip()
        if profile.lifecycle == "pool" and not fixed_class:
            continue
        class_ids = [fixed_class] if fixed_class else sorted(intelligence_classes)
        matching_agents = [a for a in enabled_agents if a.profile_id == profile.id]
        for class_id in class_ids:
            cls = intelligence_classes.get(class_id)
            if cls is None or _class_mapping(cls, profile, provider) is None:
                continue
            compatible = [
                a for a in matching_agents
                if not a.intelligence_class or a.intelligence_class == class_id
            ]
            potential = profile.max_active if profile.lifecycle == "pool" else None
            row = {
                "intelligence_class": class_id,
                "provider": provider,
                "profile_id": profile.id,
                "lifecycle": profile.lifecycle,
                "enabled": getattr(profile, "enabled", True),
                "configured_capacity": max(1, potential or len(compatible)),
                "idle_count": sum(a.state == AgentState.IDLE for a in compatible),
                "busy_count": sum(a.state == AgentState.BUSY for a in compatible),
            }
            if availability is not None:
                key = availability.provider_for_profile(profile, project_id=project_id)
                state = availability.effective_state(key)
                row["provider_key"] = key
                row["provider_state"] = state
                row["launchable"] = not availability.suppresses(key)
            rows.append(row)
    rows.sort(key=lambda r: (r["intelligence_class"], r["provider"], r["profile_id"]))
    return rows


def profile_for_class(
    options: list[dict[str, Any]],
    intelligence_class: str,
    *,
    pinned_profile_id: str | None = None,
    prefer_provider: str | None = None,
) -> str | None:
    """The deterministic profile choice for a class the operator already fixed.

    The task's own compatible pin wins even when its pool is disabled; an
    explicit route must never silently change providers.  Otherwise disabled
    pools are excluded, then a pool profile fixed on that class is preferred,
    followed by the project default's provider and the lowest id; finally any
    task-lifecycle profile that can run it is considered.
    """

    serving = [o for o in options if o["intelligence_class"] == intelligence_class]
    if not serving:
        return None
    if pinned_profile_id and any(o["profile_id"] == pinned_profile_id for o in serving):
        return pinned_profile_id

    # Disabled pools and rows on an unavailable provider are never chosen
    # automatically (provider-failover D11 mechanism 3).
    serving = [o for o in serving if o.get("enabled", True) and o.get("launchable", True)]
    if not serving:
        return None

    def rank(option):
        return (
            option["lifecycle"] != "pool",
            option["provider"] != (prefer_provider or ""),
            # A degraded provider sorts after an available one (D1).
            option.get("provider_state", "available") != "available",
            option["profile_id"],
        )

    return min(serving, key=rank)["profile_id"]


class RoutingCommandsMixin:
    """``task_route_options`` — the read half of assignment routing."""

    async def _cmd_task_route_options(self, args: dict) -> dict:
        """Report a task's routing state and the catalog that could serve it.

        Outcomes (``outcome`` in the result): ``already_routed`` (class and
        profile set and compatible), ``explicit`` (class set —
        ``explicit_profile_id`` names the profile that serves it),
        ``undecided`` (no class; the playbook must choose from ``options``),
        ``no_options`` (nothing configured can execute it), ``held`` (options
        exist in principle but every one is on an unavailable provider --
        provider-failover D13a; the playbook ends quietly on it).

        ``options`` holds only launchable, enabled rows; ``unavailable_options``
        and ``disabled_options`` report the rest.  The task's own profile
        narrows the catalog only when its ``provider_intent`` is ``preferred``
        or ``pinned`` (D8): a ``class_only`` placement constrains nothing.
        """

        task_id = args.get("task_id")
        if not task_id:
            return {"success": False, "error": "task_id is required"}
        task = await self.db.get_task(str(task_id))
        if task is None:
            return {"success": False, "error": f"task '{task_id}' not found"}
        project = await self.db.get_project(task.project_id)
        if project is None:
            return {"success": False, "error": f"project '{task.project_id}' not found"}

        orchestrator = self.orchestrator
        classes = getattr(
            getattr(orchestrator, "session_spec_builder", None), "_intelligence_classes", None
        ) or {}
        profiles = await self.db.list_profiles()
        catalog = build_route_options(
            task.project_id, profiles, await self.db.list_agents(),
            getattr(orchestrator, "harness_registry", None), classes,
            availability=getattr(orchestrator, "provider_availability", None),
        )
        # ``catalog`` retains disabled pools for an existing profile pin and
        # for diagnostics.  New automatic decisions may only see routes that
        # can actually start a pool worker.  A ``preferred`` or ``pinned``
        # profile is itself an explicit constraint even before the task has
        # an intelligence class: choosing another profile would silently
        # substitute its provider.  A ``class_only`` placement is routing's
        # own output and constrains nothing (provider-failover D8).
        from src.providers.intent import narrows_catalog

        pinned = task.profile_id or None
        narrowing = pinned if narrows_catalog(task) else None
        automatic_catalog = (
            [option for option in catalog if option["profile_id"] == narrowing]
            if narrowing else catalog
        )
        options = [
            option for option in automatic_catalog
            if option.get("enabled", True) and option.get("launchable", True)
        ]
        disabled_options = [option for option in automatic_catalog if not option.get("enabled", True)]
        unavailable_options = [
            option for option in automatic_catalog
            if option.get("enabled", True) and not option.get("launchable", True)
        ]
        default_profile_id = project.default_profile_id
        resolver = getattr(orchestrator, "_effective_default_profile_id", None)
        if resolver is not None:
            try:
                default_profile_id = await resolver(project)
            except Exception:  # pragma: no cover - diagnostics only
                default_profile_id = project.default_profile_id
        by_id = {p.id: p for p in profiles}
        default_provider = (
            profile_provider(by_id[default_profile_id], getattr(orchestrator, "harness_registry", None), task.project_id)
            if default_profile_id in by_id else ""
        )

        explicit = (task.intelligence_class or "").strip() or None
        explicit_profile_id = None
        if explicit:
            # A class_only placement keeps its profile while that profile can
            # run (so a healthy box answers exactly as before); a preferred or
            # pinned one keeps it even when disabled or unavailable -- moving
            # it is the re-route sweep's decision, not routing's.
            keep = narrowing or (
                pinned
                if any(
                    o["profile_id"] == pinned
                    and o["intelligence_class"] == explicit
                    and o.get("enabled", True)
                    and o.get("launchable", True)
                    for o in catalog
                )
                else None
            )
            explicit_profile_id = profile_for_class(
                catalog, explicit, pinned_profile_id=keep, prefer_provider=default_provider,
            )
            if explicit_profile_id is None:
                held = any(o["intelligence_class"] == explicit for o in unavailable_options)
                outcome = "held" if held else "no_options"
            elif pinned == explicit_profile_id:
                outcome = "already_routed"
            else:
                outcome = "explicit"
        elif options:
            outcome = "undecided"
        else:
            outcome = "held" if unavailable_options else "no_options"

        return {
            "success": True,
            "outcome": outcome,
            "task_id": task.id,
            "project_id": task.project_id,
            "title": task.title,
            "description": task.description or "",
            "priority": task.priority,
            "task_type": str(getattr(task.task_type, "value", task.task_type) or ""),
            "intelligence_class": explicit,
            "profile_id": pinned,
            "default_profile_id": default_profile_id,
            "explicit_profile_id": explicit_profile_id,
            "options": options,
            "disabled_options": disabled_options,
            "unavailable_options": unavailable_options,
            "provider_intent": getattr(task, "provider_intent", None) or "class_only",
        }
