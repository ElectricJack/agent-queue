"""Routing commands: the router's plan and apply, the re-run and the override.

Policy lives in the project's bound routing playbook
(``default-assignment-routing`` by default).  ``task_route_plan`` applies its
policy to one capacity snapshot and ``task_route_apply`` writes the route; only
the bound router may call the latter.  ``task_route`` sends a task back to the
router, and ``task_route_override`` is the audited emergency override
(``projects/agent-queue/specs/2026-09-28-mandatory-task-routing.md`` §6-§7).
``task_route_options`` is the superseded router's read, deleted with it.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Any

from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.database.queries.routing_queries import ROUTABLE_STATUSES, RoutingBusyError
from src.models import TASK_TYPE_VALUES, AgentState, TaskStatus
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
    worker_classes,
)
from src.routing.policy import Balance, PolicyError, parse_policy
from src.routing.sources import LEGACY, OVERRIDE, ROLE, ROLE_PROFILE_IDS, ROUTER, UNROUTED
from src.sessions.spec import _infer_provider_from_harness

logger = logging.getLogger(__name__)

#: ``task_route_apply``'s refusal code for any caller but the bound router.
NOT_ROUTER = "routing.not_router"
#: ``task_route`` / ``task_route_override`` refusal for a caller they do not admit.
NOT_PERMITTED = "routing.not_permitted"
#: ... for a task they cannot act on: claimed, running, finished or a role task.
NOT_ROUTABLE = "routing.not_routable"
#: ... for an override profile or class the task cannot run on.
INVALID_OVERRIDE = "routing.invalid_override"
#: ``task_route_override --reason`` bounds, in characters (spec §7).
OVERRIDE_REASON_MIN = 10
OVERRIDE_REASON_MAX = 400
#: The statuses a re-run or an override acts on: queued work, and a paused
#: task, which the router routes (or which runs on the override) once resumed.
REROUTABLE_STATUSES = frozenset(
    {TaskStatus.DEFINED, TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.PAUSED}
)
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
    """The router's ``task_route_plan`` / ``task_route_apply``, the operator's
    ``task_route`` (a router re-run) and ``task_route_override`` (mandatory
    routing), and the superseded ``task_route_options``."""

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
        # Superseded diagnostics (mandatory routing Task 8 deletes this
        # command): the raw column, no longer resolved by the orchestrator.
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
        class_hint = (getattr(task, "class_hint", None) or "").strip() or None
        if class_hint is None and (getattr(task, "route_source", None) or UNROUTED) == UNROUTED:
            # Rows filed before creation recorded the filer's class as
            # ``class_hint`` (mandatory routing Task 4) are unrouted with the
            # class in ``intelligence_class``.  The superseded router honoured
            # it; so does this one, as the migration's backfill did for every
            # row that existed before the column.
            class_hint = (task.intelligence_class or "").strip() or None
        return TaskFacts(
            task_id=task.id,
            title=task.title or "",
            description=task.description or "",
            task_type=str(task_type) if task_type else None,
            class_hint=class_hint,
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
                if getattr(fresh, "route_source", None) == LEGACY and fresh.profile_id:
                    # Cutover (spec §11): a queued legacy route -- a hand pin
                    # included -- is replaced, and kept here for audit.
                    route["legacy"] = {
                        "profile_id": fresh.profile_id,
                        "intelligence_class": fresh.intelligence_class,
                        "provider_intent": fresh.provider_intent,
                    }
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

    # -- aq task route and the emergency override (spec §7) --------------------

    async def _route_actor(
        self, task, *, command: str, workers: bool
    ) -> tuple[dict[str, str] | None, dict | None]:
        """Who runs *command* on *task*: ``(actor, None)`` or ``(None, refusal)``.

        The local operator and a live supervisor session of the task's project
        are admitted (``operator_or_supervisor``).  A playbook step never is:
        the router routes, and nothing else a playbook runs may pick a route.
        With *workers*, a worker session is admitted for a task it filed
        itself; any other session token, an API or MCP token included, is
        refused.  The actor names the comment author and the audit label.
        """
        from src.commands.supervisor_authority import operator_or_supervisor

        def refuse(why: str) -> tuple[None, dict]:
            return None, {
                "success": False,
                "code": NOT_PERMITTED,
                "error": f"{command} refused: {why}",
            }

        if current_invocation() is not None:
            return refuse("a playbook never picks or clears a route; the project's router does")
        principal = current_principal() or TRUSTED_LOCAL
        if workers and principal.kind is PrincipalKind.SESSION and not principal.elevated:
            session_id = principal.session_id or ""
            filed_here = (
                bool(session_id)
                and getattr(task, "created_by_kind", None) == "session"
                and getattr(task, "created_by_id", None) == session_id
                and principal.project_id in (None, task.project_id)
            )
            if not filed_here:
                return refuse(
                    "a worker may re-route only a task it filed itself; ask the supervisor"
                )
            return {
                "author_kind": "agent",
                "author_id": session_id,
                "by": f"worker session:{session_id}",
            }, None
        label, why = await operator_or_supervisor(self.db, task.project_id, subject="the task")
        if why is not None:
            return refuse(why)
        if principal.kind is PrincipalKind.SESSION:
            return {
                "author_kind": "supervisor",
                "author_id": principal.session_id or "supervisor",
                "by": label,
            }, None
        return {"author_kind": "user", "author_id": "local", "by": label}, None

    @staticmethod
    def _route_state_refusal(task, *, command: str) -> dict | None:
        """Refuse *command* on a task no route may change now (spec §7)."""
        status = getattr(task.status, "value", task.status)
        if task.assigned_agent_id or task.status in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
            why = (
                f"task '{task.id}' is claimed or running; stop it first "
                f"(aq task stop --task-id {task.id}), then run {command} again"
            )
        elif task.status not in REROUTABLE_STATUSES:
            why = (
                f"task '{task.id}' is {status}: only queued work "
                "(DEFINED, READY, BLOCKED or PAUSED) is routed"
            )
        elif (getattr(task, "route_source", None) or UNROUTED) == ROLE:
            why = (
                f"task '{task.id}' is a role task: it keeps its stage profile "
                f"'{task.profile_id}' and is never routed (mandatory routing D3)"
            )
        else:
            return None
        return {"success": False, "code": NOT_ROUTABLE, "error": why}

    async def _post_route_comment(self, task, actor: dict[str, str], body: str) -> None:
        """Leave *body* on *task* so the holder of its next claim reads it."""
        try:
            await self.db.add_task_comment(
                task.id, body[:4000],
                author_kind=actor["author_kind"], author_id=actor["author_id"],
            )
        except Exception:  # the route is written; a lost comment is not a failure
            logger.warning("could not comment the route change on %s", task.id, exc_info=True)

    async def _announce_route_change(self, task_id: str) -> None:
        """Publish the task's new route to graph subscribers (the dashboard)."""
        task = await self.db.get_task(task_id)
        if task is None:
            return
        try:
            await self._emit_task_graph_change("task.updated", task)
        except Exception:  # subscribers cannot undo a committed route change
            logger.debug("task.updated after a route change failed for %s", task_id,
                         exc_info=True)

    async def _cmd_task_route(self, args: dict) -> dict:
        """Send a task back to its router: ``aq task route`` (spec §7).

        It re-runs the router and never picks a profile.  On an unclaimed,
        unassigned, queued task it stores the new hints it is given -- an
        intelligence class and a kind; an empty value clears one -- clears
        ``profile_id``, ``intelligence_class`` and the ``route`` record (its
        ``constraints`` and ``legacy`` audit stay), sets
        ``route_source='unrouted'`` and clears the route-needed throttle, so
        the next cascade emits ``task.route_needed`` and the project's router
        plans the task again.  It clears an override the same way.  A
        profile, provider or pin is refused in the dispatch path
        (``routing.choice_forbidden``); the emergency lever is
        ``task_route_override``.

        Callers: the local operator, a live supervisor session, and a worker
        session for a task it filed.  A playbook never re-routes.  A claimed,
        running or finished task and a role task are refused.
        """
        task_id = args.get("task_id")
        if not task_id:
            return {"success": False, "error": "task_id is required"}
        task = await self.db.get_task(str(task_id))
        if task is None:
            return {"success": False, "error": f"task '{task_id}' not found"}
        actor, refusal = await self._route_actor(task, command="task_route", workers=True)
        if refusal is not None:
            return refusal
        refusal = self._route_state_refusal(task, command="aq task route")
        if refusal is not None:
            return refusal

        hints: dict[str, Any] = {}
        if (raw_class := args.get("intelligence_class")) is not None:
            class_hint = str(raw_class).strip() or None
            if class_hint is not None and (error := self._validate_routing_class(class_hint)):
                return {"success": False, "error": error}
            hints["class_hint"] = class_hint
        if (raw_type := args.get("task_type")) is not None:
            kind = str(raw_type).strip() or None
            if kind is not None and kind not in TASK_TYPE_VALUES:
                return {
                    "success": False,
                    "error": (
                        f"Invalid task_type '{kind}'. "
                        f"Allowed: {', '.join(sorted(TASK_TYPE_VALUES))}"
                    ),
                }
            hints["task_type"] = kind
        if not await self.db.reset_task_route(task.id, **hints):
            return {
                "success": False,
                "code": NOT_ROUTABLE,
                "error": (
                    f"task '{task.id}' is running or claimed; stop it first "
                    f"(aq task stop --task-id {task.id}), then run aq task route again"
                ),
            }
        clear_throttle = getattr(self.orchestrator, "_clear_route_needed_throttle", None)
        if callable(clear_throttle):
            clear_throttle(task.id)

        previous_source = getattr(task, "route_source", None) or UNROUTED
        cleared = None
        if task.profile_id:
            cleared = {
                "route_source": previous_source,
                "profile_id": task.profile_id,
                "intelligence_class": task.intelligence_class,
                "provider_intent": getattr(task, "provider_intent", None),
            }
        reason = str(args.get("reason") or "").strip()
        if reason or previous_source == OVERRIDE:
            what = (
                f"the override to {task.profile_id}" if previous_source == OVERRIDE
                else (f"the {previous_source} route to {task.profile_id}" if task.profile_id
                      else "the task's routing")
            )
            await self._post_route_comment(
                task, actor,
                f"Route cleared by {actor['by']}: {what} is gone and the project's router "
                "routes the task again"
                + (f". Reason: {reason[:OVERRIDE_REASON_MAX]}" if reason else "."),
            )
        await self._announce_route_change(task.id)
        fresh = await self.db.get_task(task.id) or task
        task_type = getattr(fresh.task_type, "value", fresh.task_type)
        return {
            "success": True,
            "task_id": task.id,
            "route_source": UNROUTED,
            "class_hint": getattr(fresh, "class_hint", None),
            "task_type": str(task_type) if task_type else None,
            "cleared": cleared,
        }

    async def _cmd_task_route_override(self, args: dict) -> dict:
        """Pin one task to a worker profile in an emergency (spec §7, D2).

        Allowed only to the local operator and a live supervisor session --
        never to a worker, a playbook, or any other API or MCP token -- and
        only with a ``reason`` of 10 to 400 characters.  It refuses control
        and role profiles, a profile that is not a worker candidate (the
        planner's rule: an enabled worker route with a slot), a class the
        profile cannot run, and a pool profile for a task whose workspace no
        pool can prepare.  It writes ``route_source='override'``,
        ``provider_intent='pinned'``, the one candidate and
        ``route.override = {by, at, reason}``, resolves the task's routing
        gates, emits ``task.route_overridden`` and comments on the task.
        ``aq task route`` clears it and hands the task back to the router.
        """
        task_id = args.get("task_id")
        profile_id = args.get("profile_id")
        if not task_id or not profile_id:
            return {"success": False, "error": "task_id and profile_id are required"}
        task = await self.db.get_task(str(task_id))
        if task is None:
            return {"success": False, "error": f"task '{task_id}' not found"}
        actor, refusal = await self._route_actor(
            task, command="task_route_override", workers=False
        )
        if refusal is not None:
            return refusal
        reason = " ".join(str(args.get("reason") or "").split())
        if not OVERRIDE_REASON_MIN <= len(reason) <= OVERRIDE_REASON_MAX:
            return {
                "success": False,
                "code": INVALID_OVERRIDE,
                "error": (
                    f"reason is required: {OVERRIDE_REASON_MIN} to {OVERRIDE_REASON_MAX} "
                    "characters saying why the router's choice is overridden "
                    f"(got {len(reason)})"
                ),
            }
        refusal = self._route_state_refusal(task, command="aq task route-override")
        if refusal is not None:
            return refusal

        def invalid(why: str) -> dict:
            return {"success": False, "code": INVALID_OVERRIDE, "error": why}

        profile = await self.db.get_profile(str(profile_id))
        if profile is None:
            return invalid(f"profile '{profile_id}' not found")
        if error := self._task_execution_profile_error(profile):
            return invalid(error)
        if profile.id in ROLE_PROFILE_IDS:
            return invalid(f"'{profile.id}' is a role profile, not a worker route")
        class_id = next(
            (
                value for value in (
                    args.get("intelligence_class"),
                    getattr(profile, "default_class", None),
                    getattr(task, "class_hint", None),
                    task.intelligence_class,
                )
                if str(value or "").strip()
            ),
            None,
        )
        if class_id is None:
            return invalid(
                f"intelligence_class is required: profile '{profile.id}' has no fixed "
                "class and the task names none"
            )
        class_id = str(class_id).strip()
        if error := self._validate_routing_class(class_id):
            return invalid(error)
        profiles, _providers = await self._routing_static_facts(task.project_id, [class_id])
        facts = next((p for p in profiles if p.id == profile.id), None)
        if facts is None or class_id not in worker_classes(facts):
            fixed = (facts.default_class.strip() if facts is not None else "")
            if facts is not None and class_id not in facts.classes:
                why = f"its provider '{facts.provider}' maps no model for '{class_id}'"
            elif facts is not None and fixed and fixed != class_id:
                why = f"it runs only its fixed class '{fixed}'"
            else:
                why = (
                    "it is not a worker candidate (a template, named, read-only, stage "
                    "or disabled profile, or one with no slots)"
                )
            return invalid(f"profile '{profile.id}' cannot run class '{class_id}': {why}")
        project = await self.db.get_project(task.project_id)
        if facts.lifecycle == "pool" and (
            await self._routing_task_facts(task, project)
        ).needs_task_lifecycle:
            return invalid(
                f"task '{task.id}' needs a workspace kind no pool prepares; override it to "
                "a task-lifecycle profile"
            )

        now = time.time()
        candidate = Candidate(
            profile_id=facts.id,
            intelligence_class=class_id,
            harness=facts.harness,
            provider=facts.provider,
            lifecycle=facts.lifecycle,
            hold=True,
        )
        previous_source = getattr(task, "route_source", None) or UNROUTED
        task_type = getattr(task.task_type, "value", task.task_type)
        route: dict[str, Any] = {
            "version": 1,
            "hints": {
                "class_hint": getattr(task, "class_hint", None),
                "task_type": str(task_type) if task_type else None,
            },
            "intelligence_class": class_id,
            "profile_id": facts.id,
            "provider": facts.provider,
            "provider_intent": "pinned",
            "candidates": [candidate.as_dict()],
            "reason": f"override by {actor['by']}: {reason}",
            "override": {"by": actor["by"], "at": now, "reason": reason},
            "routed_at": now,
        }
        prior = getattr(task, "route", None)
        if constraints := route_constraints(task):
            route["constraints"] = constraints
        if isinstance(prior, dict) and prior.get("legacy") is not None:
            route["legacy"] = prior["legacy"]
        if task.profile_id and previous_source == LEGACY:
            route["legacy"] = {
                "profile_id": task.profile_id,
                "intelligence_class": task.intelligence_class,
                "provider_intent": getattr(task, "provider_intent", None),
            }
        if task.profile_id and previous_source in (ROUTER, OVERRIDE):
            route["replaced"] = {
                "route_source": previous_source,
                "profile_id": task.profile_id,
                "intelligence_class": task.intelligence_class,
                "provider_intent": getattr(task, "provider_intent", None),
            }
        if not await self.db.write_override_route(
            task.id, profile_id=facts.id, intelligence_class=class_id, route=route,
        ):
            return {
                "success": False,
                "code": NOT_ROUTABLE,
                "error": (
                    f"task '{task.id}' changed before the override was written (claimed, "
                    "started or finished); stop it first and override it again"
                ),
            }

        if getattr(task, "provider_intent", None) != "pinned":
            await self._record_provider_intent_audit(
                task.id, "pinned", previous=getattr(task, "provider_intent", None)
            )
        resolved: list[str] = []
        for gate in await self.db.get_gates_for_task(task.id):
            if gate["gate_type"] == "routing" and gate["status"] == "open":
                await self.orchestrator._resolve_gate_and_emit(
                    gate["id"],
                    resolved_by="task_route_override",
                    resolution=f"overridden to {facts.id}",
                )
                resolved.append(gate["id"])
        routed = await self.db.get_task(task.id) or task
        try:
            await self.orchestrator._emit_task_event(
                "task.route_overridden",
                routed,
                profile_id=facts.id,
                intelligence_class=class_id,
                provider=facts.provider,
                by=actor["by"],
                reason=reason,
                previous_profile_id=task.profile_id or "",
                previous_route_source=previous_source,
            )
        except Exception:  # the override is committed; the audit row below still lands
            logger.warning("task.route_overridden emission failed for %s", task.id,
                           exc_info=True)
        await self._post_route_comment(
            task, actor,
            f"Route overridden to {facts.id} ({class_id}) by {actor['by']}: {reason}\n\n"
            "This is an emergency override, pinned to its provider: failover holds the "
            f"task rather than moving it. `aq task route --task-id {task.id}` hands it "
            "back to the project's router.",
        )
        await self._announce_route_change(task.id)
        return {
            "success": True,
            "task_id": task.id,
            "profile_id": facts.id,
            "intelligence_class": class_id,
            "provider": facts.provider,
            "provider_intent": "pinned",
            "route_source": OVERRIDE,
            "by": actor["by"],
            "resolved_gate_ids": resolved,
        }
