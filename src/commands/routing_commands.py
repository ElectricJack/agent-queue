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
) -> list[dict[str, Any]]:
    """One row per (class, provider, profile) an ordinary worker can execute.

    A profile with a fixed ``default_class`` offers that class only; a generic
    task-lifecycle profile offers every class its provider maps.  A pool
    profile without a fixed class offers nothing — a pool worker only claims
    its own class, so there would be nothing for it to claim.
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
            rows.append({
                "intelligence_class": class_id,
                "provider": provider,
                "profile_id": profile.id,
                "lifecycle": profile.lifecycle,
                "configured_capacity": max(1, potential or len(compatible)),
                "idle_count": sum(a.state == AgentState.IDLE for a in compatible),
                "busy_count": sum(a.state == AgentState.BUSY for a in compatible),
            })
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

    The task's own pin wins when it serves the class; otherwise a pool profile
    fixed on that class, preferring the project default's provider, then the
    lowest id; otherwise any task-lifecycle profile that can run it.
    """

    serving = [o for o in options if o["intelligence_class"] == intelligence_class]
    if not serving:
        return None
    if pinned_profile_id and any(o["profile_id"] == pinned_profile_id for o in serving):
        return pinned_profile_id

    def rank(option):
        return (
            option["lifecycle"] != "pool",
            option["provider"] != (prefer_provider or ""),
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
        ``no_options`` (nothing configured can execute it).
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
        options = build_route_options(
            task.project_id, profiles, await self.db.list_agents(),
            getattr(orchestrator, "harness_registry", None), classes,
        )
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
        pinned = task.profile_id or None
        explicit_profile_id = None
        if explicit:
            explicit_profile_id = profile_for_class(
                options, explicit, pinned_profile_id=pinned, prefer_provider=default_provider,
            )
            if explicit_profile_id is None:
                outcome = "no_options"
            elif pinned == explicit_profile_id:
                outcome = "already_routed"
            else:
                outcome = "explicit"
        else:
            outcome = "undecided" if options else "no_options"

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
        }
