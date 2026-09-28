"""Routing read command: what a task needs and what can serve it.

Policy lives in the ``default-assignment-routing`` playbook.  This module only
reports facts a playbook step can act on — the task's current routing fields,
whether its class is explicit, and the class/provider/profile catalog of
ordinary workers that could execute it — and the one deterministic tie-break
the playbook cannot express (which profile serves an explicit class).  Spec:
``docs/superpowers/specs/2026-09-06-assignment-routing-as-playbook.md``.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Any

from src.database.queries.routing_queries import ROUTABLE_STATUSES, RoutingBusyError
from src.models import AgentState
from src.playbooks.invocation import current_invocation
from src.routing.planner import (
    ROUTABLE_SOURCES,
    ROUTED_SOURCES,
    Candidate,
    ProfileFacts,
    ProviderFacts,
    Snapshot,
    TaskFacts,
    is_candidate,
    plan_route,
    reselect,
)
from src.routing.policy import Balance, PolicyError, parse_policy
from src.routing.sources import UNROUTED
from src.sessions.spec import _infer_provider_from_harness

logger = logging.getLogger(__name__)

#: ``task_route_apply``'s refusal code for any caller but the bound router.
NOT_ROUTER = "routing.not_router"
#: Workspace kinds a pool may prepare (``_claim_preparation_predicates``); a
#: task needing any other kind is offered task-lifecycle profiles only.
POOL_WORKSPACE_KINDS = frozenset({"project-repo", "vault"})

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


def profile_provider_key(profile, harness_registry=None, project_id: str | None = None) -> str:
    """The harness login key, rather than the vendor used by class mappings."""
    from src.providers.availability import provider_key

    harness_id = getattr(profile, "harness", "") or ""
    harness = harness_registry.get(harness_id, project_id) if harness_registry else None
    return provider_key(harness if harness is not None else harness_id)


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
            prefer_provider not in {option["provider"], option.get("provider_key")},
            # A degraded provider sorts after an available one (D1).
            option.get("provider_state", "available") != "available",
            option["profile_id"],
        )

    return min(serving, key=rank)["profile_id"]


def route_constraints(task) -> dict[str, Any]:
    """``tasks.route.constraints`` (§4): system limits on routing, e.g. ``exclude_providers``."""
    route = getattr(task, "route", None)
    constraints = route.get("constraints") if isinstance(route, dict) else None
    return dict(constraints) if isinstance(constraints, dict) else {}


class RoutingCommandsMixin:
    """``task_route_options`` — the read half of assignment routing — and the
    router's ``task_route_plan`` / ``task_route_apply`` (mandatory routing)."""

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
        preferred_provider = getattr(project, "preferred_provider", None) if not narrowing else None
        if preferred_provider:
            by_id = {profile.id: profile for profile in profiles}
            automatic_catalog = [
                option for option in automatic_catalog
                if profile_provider_key(
                    by_id[option["profile_id"]],
                    getattr(orchestrator, "harness_registry", None), task.project_id,
                ) == preferred_provider
            ]
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
                    for o in automatic_catalog
                )
                else None
            )
            explicit_profile_id = profile_for_class(
                automatic_catalog if preferred_provider else catalog,
                explicit, pinned_profile_id=keep,
                prefer_provider=preferred_provider or default_provider,
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

    # -- the router (mandatory-routing spec §6.2-§6.6) -------------------------

    async def _routing_static_facts(
        self, project_id: str, class_ids
    ) -> tuple[tuple[ProfileFacts, ...], dict[str, ProviderFacts]]:
        """The half of a snapshot routing does not move: profiles and providers.

        ``slots`` is ``max_active`` for a pool (the fleet-wide cap when the
        pool sets none, nothing while ``swarm.enabled`` is off) and the
        enabled worker-agent count for a task-lifecycle profile (§6.4 step
        5).  ``classes`` is every class on the policy's ``class_order`` that
        has a model for the profile's provider.
        """
        from src.providers.availability import usage_view
        from src.providers.availability_service import stale_after_seconds

        orchestrator = self.orchestrator
        availability = getattr(orchestrator, "provider_availability", None)
        registry = getattr(orchestrator, "harness_registry", None)
        agents = await self.db.list_agents()
        agent_slots = Counter(
            agent.profile_id for agent in agents
            if agent.enabled and agent.role == "worker" and agent.deleted_at is None
        )
        swarm_enabled = bool(getattr(getattr(self.config, "swarm", None), "enabled", True))
        global_cap = None
        cap = getattr(orchestrator, "_pool_global_cap", None)
        if callable(cap):
            try:
                global_cap = cap()
            except Exception:  # noqa: BLE001 - a missing cap is "one slot"
                global_cap = None

        facts: list[ProfileFacts] = []
        keys: set[str] = set()
        for profile in await self.db.list_profiles():
            harness = str(getattr(profile, "harness", "") or "")
            if not harness:
                continue
            if availability is not None:
                key = availability.provider_for_profile(profile, project_id=project_id)
            else:
                key = profile_provider_key(profile, registry, project_id)
            lifecycle = str(getattr(profile, "lifecycle", "task") or "task")
            if lifecycle == "pool":
                max_active = getattr(profile, "max_active", None)
                slots = 0 if not swarm_enabled else (
                    max_active if max_active is not None else (global_cap or 1)
                )
            else:
                slots = agent_slots.get(profile.id, 0)
            facts.append(ProfileFacts(
                id=profile.id,
                harness=harness,
                provider=key,
                lifecycle=lifecycle,
                default_class=str(getattr(profile, "default_class", "") or ""),
                classes=frozenset(
                    class_id for class_id in class_ids
                    if self._validate_routing_class(class_id, profile) is None
                ),
                slots=int(slots or 0),
                enabled=bool(getattr(profile, "enabled", True)),
                template=bool(getattr(profile, "template", False)),
                read_only=bool(getattr(profile, "read_only", False)),
                runtime=str(getattr(profile, "runtime", "") or ""),
            ))
            keys.add(key)

        rows_by_provider: dict[str, list] = {}
        try:
            for row in await self.db.latest_provider_usage():
                rows_by_provider.setdefault(str(row.get("provider") or ""), []).append(row)
        except Exception:  # no reading is "usage unknown"
            logger.debug("routing: usage read failed", exc_info=True)
        now = time.time()
        providers: dict[str, ProviderFacts] = {}
        for key in keys:
            usage = None
            if rows_by_provider.get(key):
                readings = [
                    reading.used_percent
                    for reading in usage_view(
                        rows_by_provider[key], now=now,
                        stale_after=stale_after_seconds(key, self.config),
                    )
                    if reading is not None
                ]
                usage = max(readings) if readings else None
            providers[key] = ProviderFacts(
                state=availability.effective_state(key) if availability is not None else "available",
                launchable=not availability.suppresses(key) if availability is not None else True,
                usage_percent=usage,
            )
        return tuple(facts), providers

    async def _routing_task_facts(self, task, project) -> TaskFacts:
        requirements = await self.db.fetch_task_workspace_requirements(task.id)
        exclude = route_constraints(task).get("exclude_providers") or []
        task_type = getattr(task.task_type, "value", task.task_type)
        return TaskFacts(
            task_id=task.id,
            title=task.title or "",
            description=task.description or "",
            task_type=str(task_type) if task_type else None,
            class_hint=(getattr(task, "class_hint", None) or "").strip() or None,
            created_by_kind=getattr(task, "created_by_kind", None),
            exclude_providers=frozenset(str(p) for p in exclude if p),
            preferred_provider=(getattr(project, "preferred_provider", None) or None)
            if project is not None else None,
            needs_task_lifecycle=any(
                row.kind_id not in POOL_WORKSPACE_KINDS for row in requirements
            ),
        )

    async def _cmd_task_route_plan(self, args: dict) -> dict:
        """Plan a route for a task the router still owes one (§6.2).

        Pure selection over one snapshot (:func:`src.routing.planner.plan_route`);
        it writes nothing.  Outcomes: ``planned``, ``needs_classification``,
        ``held``, ``no_candidates``, ``already_routed``; an unknown task or an
        invalid policy is ``rejected``.
        """
        task_id = args.get("task_id")
        if not task_id:
            return {"success": False, "error": "task_id is required"}
        task = await self.db.get_task(str(task_id))
        if task is None:
            return {"success": False, "error": f"task '{task_id}' not found"}
        source = getattr(task, "route_source", None) or UNROUTED
        if source in ROUTED_SOURCES:
            return {
                "success": True,
                "outcome": "already_routed",
                "task_id": task.id,
                "profile_id": task.profile_id,
                "intelligence_class": task.intelligence_class,
                "provider_intent": getattr(task, "provider_intent", None),
                "route_source": source,
                "route": getattr(task, "route", None),
            }
        try:
            policy, digest = parse_policy(args.get("policy"))
        except PolicyError as exc:
            return {"success": False, "code": "routing.invalid_policy", "error": str(exc)}
        project = await self.db.get_project(task.project_id)
        facts = await self._routing_task_facts(task, project)
        profiles, providers = await self._routing_static_facts(task.project_id, policy.class_order)
        snapshot = Snapshot(
            profiles=profiles,
            providers=providers,
            busy=await self.db.count_busy_sessions_by_profile(),
            backlog=await self.db.count_routed_backlog_by_profile(),
        )
        result = plan_route(
            facts, policy, snapshot,
            policy_sha256=digest, classification=args.get("classification"),
        )
        return {"success": True, "outcome": result.outcome, **result.value}

    def _not_router_refusal(self, project) -> dict | None:
        """``routing.not_router`` unless the caller is *project*'s bound router (§6.6).

        A playbook command step runs as the ``playbook-dispatch`` service, so
        the principal proves nothing: the live invocation must come from the
        playbook the project is bound to, and that artifact must grant
        ``task_route_apply``.  The operator CLI, the supervisor, workers and
        every other playbook have no such invocation.
        """
        invocation = current_invocation()
        bound = (getattr(project, "assignment_playbook_id", None) or "").strip()
        if invocation is None:
            why = "it runs only as a step of the project's bound routing playbook"
        elif not bound:
            why = "the project is bound to no routing playbook"
        elif invocation.artifact_ref.playbook_id != bound:
            why = (
                f"playbook '{invocation.artifact_ref.playbook_id}' is not the project's "
                f"bound router '{bound}'"
            )
        elif "task_route_apply" not in invocation.aq_commands:
            why = "the invoking artifact does not grant task_route_apply"
        else:
            return None
        return {
            "success": False,
            "code": NOT_ROUTER,
            "error": f"task_route_apply refused: {why}",
        }

    async def _cmd_task_route_apply(self, args: dict) -> dict:
        """Write a planned route, re-selected on a fresh snapshot (§6.6).

        Only the project's bound routing playbook may call it
        (:meth:`_not_router_refusal`).  Under the fleet-wide routing lock it
        re-reads the task (``stale`` when it was claimed, assigned, finished
        or routed meanwhile), re-validates the plan's candidates and re-runs
        availability, load and choice, so a burst of plans made against one
        snapshot spreads out.  The route, ``route_source='router'`` and the
        route record are written in one guarded update; the routing gates are
        resolved and ``task.routed`` is emitted after it commits.
        """
        task_id = args.get("task_id")
        plan = args.get("plan")
        if not task_id:
            return {"success": False, "error": "task_id is required"}
        task = await self.db.get_task(str(task_id))
        if task is None:
            return {"success": False, "error": f"task '{task_id}' not found"}
        project = await self.db.get_project(task.project_id)
        refusal = self._not_router_refusal(project)
        if refusal is not None:
            return refusal
        invocation = current_invocation()
        try:
            if not isinstance(plan, dict) or not plan.get("candidates"):
                raise ValueError("plan must be a planned task_route_plan value")
            if plan.get("task_id") not in (None, "", task.id):
                raise ValueError(f"plan is for task '{plan.get('task_id')}'")
            candidates = [Candidate.from_dict(item) for item in plan["candidates"]]
            balance = Balance.model_validate(plan.get("balance") or {})
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "code": "routing.invalid_plan", "error": str(exc)}

        profiles, providers = await self._routing_static_facts(
            task.project_id, sorted({c.intelligence_class for c in candidates})
        )
        planned_profile_id = plan.get("profile_id")
        try:
            async with self.db.routing_apply_lock() as conn:
                fresh = await self.db.get_task_on(conn, task.id)
                stale = await self._route_stale_reason(conn, fresh)
                if stale is not None:
                    return {"success": True, "outcome": "stale", "task_id": task.id,
                            "reason": stale}
                snapshot = Snapshot(
                    profiles=profiles,
                    providers=providers,
                    busy=await self.db.count_busy_sessions_by_profile(conn=conn),
                    backlog=await self.db.count_routed_backlog_by_profile(conn=conn),
                )
                valid = [c for c in candidates if is_candidate(c, snapshot)]
                selection = reselect(valid, snapshot, balance)
                if selection is None:
                    return {"success": True, "outcome": "stale", "task_id": task.id,
                            "reason": "no candidate of the plan is launchable now"}
                chosen = selection.chosen
                adjusted = chosen.profile_id != planned_profile_id
                reason = str(plan.get("reason") or "")
                if adjusted:
                    reason = (
                        f"{reason}; adjusted at apply: {chosen.profile_id} "
                        f"{selection.score.pressure:.2f} (planned {planned_profile_id})"
                    ).lstrip("; ")
                classification = plan.get("classification")
                write_type = None
                if (
                    fresh.task_type is None
                    and isinstance(classification, dict)
                    and not classification.get("failed")
                    and classification.get("task_type")
                ):
                    write_type = str(plan.get("task_type") or classification["task_type"])
                route = {
                    "version": 1,
                    "hints": {
                        "class_hint": getattr(fresh, "class_hint", None),
                        "task_type": getattr(fresh.task_type, "value", fresh.task_type),
                    },
                    "classification": classification,
                    "task_type": plan.get("task_type"),
                    "rule": plan.get("rule"),
                    "lane": chosen.lane,
                    "class_clamped_from": plan.get("class_clamped_from"),
                    "intelligence_class": chosen.intelligence_class,
                    "profile_id": chosen.profile_id,
                    "provider": chosen.provider,
                    "provider_intent": selection.provider_intent,
                    "candidates": [c.as_dict() for c in valid],
                    "scores": [s.as_dict() for s in selection.scores],
                    "reason": reason,
                    "policy_sha256": plan.get("policy_sha256"),
                    "playbook_id": invocation.artifact_ref.playbook_id,
                    "artifact_sha256": invocation.artifact_ref.artifact_sha256,
                    "run_id": invocation.run_id,
                    "step_id": invocation.step_id,
                    "planned_profile_id": planned_profile_id,
                    "adjusted_at_apply": adjusted,
                    "routed_at": time.time(),
                }
                prior = getattr(fresh, "route", None)
                if constraints := route_constraints(fresh):
                    route["constraints"] = constraints
                if isinstance(prior, dict) and prior.get("legacy") is not None:
                    route["legacy"] = prior["legacy"]
                written = await self.db.write_router_route(
                    conn, task.id,
                    profile_id=chosen.profile_id,
                    intelligence_class=chosen.intelligence_class,
                    provider_intent=selection.provider_intent,
                    route=route,
                    task_type=write_type,
                )
                if not written:
                    return {"success": True, "outcome": "stale", "task_id": task.id,
                            "reason": "the task changed before the route was written"}
        except RoutingBusyError as exc:
            return {"success": False, "code": "routing.busy", "error": str(exc)}

        if selection.provider_intent != fresh.provider_intent:
            await self._record_provider_intent_audit(
                task.id, selection.provider_intent, previous=fresh.provider_intent
            )
        resolved: list[str] = []
        for gate in await self.db.get_gates_for_task(task.id):
            if gate["gate_type"] == "routing" and gate["status"] == "open":
                await self.orchestrator._resolve_gate_and_emit(
                    gate["id"],
                    resolved_by="task_route_apply",
                    resolution=f"routed to {chosen.profile_id}",
                )
                resolved.append(gate["id"])
        routed = await self.db.get_task(task.id) or fresh
        await self.orchestrator._emit_task_event(
            "task.routed",
            routed,
            intelligence_class=chosen.intelligence_class,
            profile_id=chosen.profile_id,
            provider=chosen.provider,
            provider_intent=selection.provider_intent,
            lane=chosen.lane,
            rule=plan.get("rule"),
            reason=reason,
            candidates=[c.as_dict() for c in valid],
            policy_sha256=plan.get("policy_sha256"),
            run_id=invocation.run_id,
            adjusted_at_apply=adjusted,
        )
        return {
            "success": True,
            "outcome": "routed",
            "task_id": task.id,
            "profile_id": chosen.profile_id,
            "intelligence_class": chosen.intelligence_class,
            "provider": chosen.provider,
            "provider_intent": selection.provider_intent,
            "lane": chosen.lane,
            "reason": reason,
            "planned_profile_id": planned_profile_id,
            "adjusted_at_apply": adjusted,
            "resolved_gate_ids": resolved,
        }

    async def _route_stale_reason(self, conn, task) -> str | None:
        """Why a fresh read of *task* no longer takes a router route, or ``None``."""
        if task is None:
            return "the task no longer exists"
        source = getattr(task, "route_source", None) or UNROUTED
        if source not in ROUTABLE_SOURCES:
            return f"the task is already routed ({source})"
        status = getattr(task.status, "value", task.status)
        if status not in ROUTABLE_STATUSES:
            return f"the task is {status}"
        if task.assigned_agent_id:
            return "the task is assigned"
        if await self.db.task_has_live_session(conn, task.id):
            return "the task is claimed"
        return None
