"""Routing commands: the router's plan and apply, the re-run and the override.

Policy lives in the project's bound routing playbook
(``default-assignment-routing`` by default).  ``task_route_plan`` applies that
policy, deterministically, to the task's hints and a snapshot of the fleet;
``task_route_apply`` writes the route and refuses every caller but the bound
router.  ``task_route`` sends a task back to the router, and
``task_route_override`` is the audited emergency override.  Spec:
``projects/agent-queue/specs/2026-09-28-mandatory-task-routing.md`` §6-§7.  The
superseded read command and its catalog were deleted with
``projects.default_profile_id`` (Task 8).
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import replace
from typing import Any

from src.commands.integration_commands import SOURCE_CI_REPAIR_ORIGIN
from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.database.queries.routing_queries import ROUTABLE_STATUSES, RoutingBusyError
from src.models import TASK_TYPE_VALUES, TaskStatus
from src.playbooks.invocation import current_invocation
from src.routing.context import live_context, quota_observations, summarize_context
from src.routing.planner import (
    LOCAL_MODEL_PROVIDERS,
    PREFER_MODES,
    PREFER_SOFT,
    PREFER_STRICT,
    ROUTABLE_SOURCES,
    ROUTED_SOURCES,
    Candidate,
    ProfileFacts,
    ProviderFacts,
    Snapshot,
    TaskFacts,
    is_candidate,
    plan_route,
    prefer_target,
    preference_record,
    reselect,
    selection_evidence,
    selection_reason,
    serves_preference,
    worker_classes,
)
from src.routing.policy import Balance, PolicyError, parse_policy
from src.routing.readiness import orchestrator_ready_projects
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
#: ``projects.hierarchical_integration_mode`` values whose tasks deliver
#: through an integration train (``development`` batches them; the other two
#: collect them under their parents).
TRAIN_INTEGRATION_MODES = frozenset({"hierarchy", "train", "development"})

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


def route_constraints(task) -> dict[str, Any]:
    """``tasks.route.constraints`` (§4): system limits on routing, e.g. ``exclude_providers``."""
    route = getattr(task, "route", None)
    constraints = route.get("constraints") if isinstance(route, dict) else None
    return dict(constraints) if isinstance(constraints, dict) else {}


async def validate_route_preference(
    handler,
    project_id: str | None,
    target: Any,
    mode: Any,
    *,
    keep_target: str | None = None,
    keep_mode: str | None = None,
) -> tuple[tuple[str | None, str | None], str | None]:
    """Resolve ``--prefer``/``--prefer-mode`` into the pair the task stores.

    ``(target, mode)`` is the value to store — ``(None, None)`` clears the
    preference — and the string is the refusal, naming what exists instead.
    A preference is an input to the router, not a route: the name must be an
    installed harness or an enabled worker profile, and the mode must be one
    of :data:`~src.routing.planner.PREFER_MODES`.  A profile id wins over a
    harness name, which is the resolution the planner applies (§4).

    *keep_target* and *keep_mode* are the task's current pair, carried forward
    when the caller supplies only half of it, so ``aq task route --prefer``
    never silently relaxes a strict preference and ``--prefer-mode`` never
    silently drops the target.  An explicitly empty ``--prefer`` clears both.
    Filing has no prior pair and so takes ``soft``.
    """
    if target is None and mode is None:
        return (None, None), None
    chosen_mode = str(mode or keep_mode or PREFER_SOFT).strip().lower()
    if chosen_mode not in PREFER_MODES:
        return (None, None), (
            f"Invalid prefer_mode '{mode}'. Allowed: {', '.join(sorted(PREFER_MODES))}"
        )
    name = str(target).strip() if target is not None else ""
    if not name:
        # ``--prefer ""`` clears; a mode on its own keeps the stored target.
        name = "" if target is not None else str(keep_target or "").strip()
    if not name:
        return (None, None), None
    profiles = await handler.db.list_profiles()
    profile = next((p for p in profiles if p.id == name), None)
    if profile is not None:
        if not bool(getattr(profile, "enabled", True)):
            return (None, None), (
                f"profile '{name}' is disabled; enable it with "
                f"`aq agent profile set {name} enabled true` or prefer its harness"
            )
        if not _worker_profile(profile) or getattr(profile, "template", False) or getattr(
            profile, "read_only", False
        ):
            return (None, None), (
                f"profile '{name}' is not a worker route (a template, stage, named, "
                "read-only or non-worker profile); prefer its harness instead"
            )
        return (name, chosen_mode), None
    harnesses = {
        str(getattr(p, "harness", "") or "") for p in profiles
        if bool(getattr(p, "enabled", True)) and getattr(p, "harness", "")
    }
    registry = getattr(handler.orchestrator, "harness_registry", None)
    if registry is not None:
        harnesses |= {
            harness.id for harness in registry.list_for_scope(project_id) if harness.id
        }
    if name not in harnesses:
        known = ", ".join(sorted(harnesses)) or "none"
        return (None, None), (
            f"'{name}' is neither an installed harness nor an enabled worker profile "
            f"(known: {known})"
        )
    return (name, chosen_mode), None


class RoutingCommandsMixin:
    """The router's ``task_route_plan`` / ``task_route_apply``, and the
    operator's ``task_route`` (a router re-run) and ``task_route_override``
    (mandatory routing §6-§7)."""

    # -- the router (mandatory-routing spec §6.2-§6.6) -------------------------

    def _benchmark_mapped_model(self, class_id: str, harness_id: str,
                                project_id: str) -> str | None:
        """Resolve the model a benchmark route would launch under the live class map."""
        from src.intelligence_classes import load_intelligence_classes, resolve_class
        from src.profiles.intelligence import provider_for_harness

        classes = self._live_intelligence_classes()
        if classes is None:
            classes = load_intelligence_classes(self.config.data_dir)
        cls = classes.get(class_id)
        if cls is None:
            return None
        registry = getattr(self.orchestrator, "harness_registry", None)
        harness = registry.get(harness_id, project_id) if registry else None
        provider = str(getattr(harness, "provider", "") or provider_for_harness(harness_id))
        mapping = resolve_class(cls, "codex") if harness_id == "codex" else {}
        mapping = mapping or (resolve_class(cls, provider) if provider else {})
        return str(mapping.get("model") or "").strip() or None

    async def _routing_static_facts(
        self, project_id: str, class_ids, *, conn=None
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
        agents = await self.db.list_agents(conn=conn)
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

        from src.routing.policy import is_opencode_family

        facts: list[ProfileFacts] = []
        keys: set[str] = set()
        for profile in await self.db.list_profiles(conn=conn):
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
                harness_family=(
                    "opencode" if registry is not None and (
                        is_opencode_family(
                            harness, profile_provider(profile, registry, project_id),
                            getattr(registry.get(harness, project_id), "command", ""),
                        )
                    ) else ""
                ),
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
                needs_workspace=bool(getattr(profile, "needs_workspace", True)),
                local=profile_provider(profile, registry, project_id) in LOCAL_MODEL_PROVIDERS,
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
            row = availability.row(key) if availability is not None and callable(
                getattr(availability, "row", None)
            ) else None
            providers[key] = ProviderFacts(
                state=availability.effective_state(key) if availability is not None else "available",
                launchable=not availability.suppresses(key) if availability is not None else True,
                usage_percent=usage,
                reason_code=row.effective_reason_code(now) if row else "",
                updated_at=row.updated_at if row else None,
                quota=quota_observations(
                    rows_by_provider.get(key, []), now=now,
                    stale_after=stale_after_seconds(key, self.config),
                ),
                quota_source="provider_usage_snapshots" if rows_by_provider else "unknown",
            )
        return tuple(facts), providers

    async def _routing_snapshot(self, task, project, class_ids, *, conn=None) -> Snapshot:
        """Reuse planner facts, adding bounded observations of the existing admission limits.

        Pending launches consume headroom before their durable session rows exist.
        Workspace counts and provider usage are observations, not locked reservations;
        eligibility/load at apply are refreshed inside the fleet-wide route transaction.
        """
        from src.models import ProjectStatus
        from src.pool_claims import pool_claim_loop_stall_seconds

        started = time.time()
        pending = tuple(getattr(self.orchestrator, "_pool_launches", {}).values())
        unacquired = {p.session_id for p in pending if not p.workspace_acquired}
        profiles, providers = await self._routing_static_facts(
            task.project_id, class_ids, conn=conn
        )
        ready_project_ids = await orchestrator_ready_projects(self.orchestrator)
        backlog = await self.db.count_routed_backlog_by_profile(
            ready_project_ids=ready_project_ids, conn=conn
        )
        snapshot = Snapshot(
            profiles=profiles, providers=providers,
            busy=await self.db.count_busy_sessions_by_profile(conn=conn),
            backlog={k: v["eligible"] for k, v in backlog.items()},
            blocked={k: v["blocked"] for k, v in backlog.items()},
        )
        supply = await self.db.routing_supply(
            now=time.time(), stall_seconds=pool_claim_loop_stall_seconds(self.config.swarm),
            conn=conn,
        )
        for launch in pending:
            if launch.session_id not in supply["session_ids"]:
                supply["supply"].append({
                    "project_id": launch.project_id, "profile_id": launch.profile_id,
                    "lifecycle": "pool", "bucket": "starting", "count": 1,
                })
        worktrees = self.orchestrator._worktrees_enabled()
        # Pool workers always need a project-repo seat; task workers may require
        # additional kinds. The minimum reports a conservative shared bound.
        requirements = await self.db.fetch_task_workspace_requirements(task.id)
        kinds = {r.kind_id for r in requirements} or {"project-repo"}
        if any(p.lifecycle == "pool" and worker_classes(p) for p in profiles):
            kinds.add("project-repo")
        capacities = [await self.db.count_available_workspaces(
            task.project_id, kind_id=kind,
            worktree_slot_cap=self.orchestrator._project_slot_cap(project) if worktrees else None,
        ) for kind in sorted(kinds)]
        workspace_capacity = max(0, min(capacities) - sum(
            launch.project_id == task.project_id and launch.session_id in unacquired
            for launch in pending
        ))
        now = time.time()
        quarantine = {
            profile_id: until
            for (project_id, profile_id), until
            in getattr(self.orchestrator, "_pool_quarantine", {}).items()
            if project_id == task.project_id and until > now
        }
        headroom = {}
        context = live_context(
            snapshot, project_id=task.project_id, now=now, started_at=started,
            supply=supply["supply"], active_kinds=supply["active_kinds"],
            project_cap=project.max_concurrent_agents if project else 0,
            project_active=project is not None and project.status == ProjectStatus.ACTIVE,
            global_cap=self.orchestrator._pool_global_cap(),
            workspace_capacity=workspace_capacity, quarantine=quarantine, headroom_out=headroom,
        )
        registry = getattr(self.orchestrator, "harness_registry", None)
        installed = {profile.harness for profile in profiles if profile.harness}
        if registry is not None:
            installed |= {h.id for h in registry.list_for_scope(task.project_id) if h.id}
        return replace(
            snapshot, context=context, headroom=headroom, harnesses=frozenset(installed)
        )

    async def _preferred_provider_serves(
        self, project_id: str, provider: str, class_id: str | None
    ) -> bool:
        """Whether *provider* has a launchable worker candidate for *class_id*.

        Any class when *class_id* is ``None``.  The explain side of the
        planner's ``preferred_provider_unavailable`` (§6.4 step 2): a project
        preferring a provider with no such worker routes nothing.
        """
        orchestrator = self.orchestrator
        classes = getattr(
            getattr(orchestrator, "session_spec_builder", None), "_intelligence_classes", None
        ) or getattr(orchestrator, "intelligence_classes", None) or {}
        class_ids = [class_id] if class_id else sorted(classes)
        profiles, providers = await self._routing_static_facts(project_id, class_ids)
        return any(
            profile.provider == provider
            and worker_classes(profile)
            and providers.get(profile.provider, ProviderFacts()).launchable
            for profile in profiles
        )

    async def _routing_task_facts(self, task, project) -> TaskFacts:
        requirements = await self.db.fetch_task_workspace_requirements(task.id)
        labels = await self.db.get_task_labels(task.id)
        exclude = route_constraints(task).get("exclude_providers") or []
        task_type = getattr(task.task_type, "value", task.task_type)
        origin = getattr(task, "created_by_kind", None)
        if origin == "system":
            from src.integration.repair import OrdinaryRepairService

            # Ordinary train repairs are system filings, not legacy stage
            # delegates. Their immutable, identity-checked input supplies the
            # routing origin without changing their claim/restart lifecycle.
            if await OrdinaryRepairService(self.db).input(task.id) is not None:
                origin = "integration_repair"
        elif origin in {SOURCE_CI_REPAIR_ORIGIN, "integration_writer"} or (
            origin is None and await self.db.list_source_ci_inherited_oids(task.id)
        ):
            # Source-CI repairs and integration writers (including verifier
            # test tasks) use the repair policy, keeping every OpenCode lane
            # excluded without changing their stored identity or lifecycle.
            # The record lookup covers repairs filed before origin stamping.
            origin = "integration_repair"
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
            created_by_kind=origin,
            exclude_providers=frozenset(str(p) for p in exclude if p),
            prefer_target=getattr(task, "prefer_target", None) or None,
            prefer_mode=getattr(task, "prefer_mode", None) or PREFER_SOFT,
            benchmark_arms=tuple(sorted(
                label.removeprefix("benchmark:") for label in labels
                if label.startswith("benchmark:")
            )),
            preferred_provider=(getattr(project, "preferred_provider", None) or None)
            if project is not None else None,
            needs_task_lifecycle=any(
                row.kind_id not in POOL_WORKSPACE_KINDS for row in requirements
            ),
            on_train=project is not None and getattr(
                project, "hierarchical_integration_mode", None
            ) in TRAIN_INTEGRATION_MODES,
            blocks_work=await self.db.has_waiting_dependents(task.id),
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
        snapshot = await self._routing_snapshot(task, project, policy.class_order)
        if len(facts.benchmark_arms) == 1:
            arm_name = facts.benchmark_arms[0]
            arm = policy.benchmark_arms.get(arm_name)
            if arm is not None:
                mapped = self._benchmark_mapped_model(arm.class_, arm.harness, task.project_id)
                if mapped != arm.requested_model:
                    return {
                        "success": True, "outcome": "held", "task_id": task.id,
                        "benchmark_arm": arm_name, "reason": "requested_model_unavailable",
                        "requested_model": arm.requested_model, "mapped_model": mapped,
                        "live_context": dict(snapshot.context),
                        "live_summary": summarize_context(snapshot.context),
                    }
        result = plan_route(
            facts, policy, snapshot,
            policy_sha256=digest, classification=args.get("classification"),
        )
        return {"success": True, "outcome": result.outcome, **result.value,
                "live_context": dict(snapshot.context),
                "live_summary": summarize_context(snapshot.context)}

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
            selectors = [
                label.removeprefix("benchmark:")
                for label in await self.db.get_task_labels(task.id)
                if label.startswith("benchmark:")
            ]
            if selectors and not plan.get("benchmark_arm"):
                raise ValueError("benchmark task requires an allowlisted arm plan")
            if arm := plan.get("benchmark_arm"):
                if selectors != [arm]:
                    raise ValueError("benchmark selector changed since route planning")
                if not plan.get("requested_model") or not plan.get("observed_models"):
                    raise ValueError("benchmark plan has no model provenance")
                if any(
                    not candidate.hold
                    or candidate.intelligence_class != plan.get("benchmark_class")
                    or candidate.harness != plan.get("benchmark_harness")
                    for candidate in candidates
                ):
                    raise ValueError("benchmark candidates differ from allowlisted arm")
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "code": "routing.invalid_plan", "error": str(exc)}

        planned_profile_id = plan.get("profile_id")
        try:
            async with self.db.routing_apply_lock() as conn:
                fresh = await self.db.get_task_on(conn, task.id)
                stale = await self._route_stale_reason(conn, fresh)
                if stale is not None:
                    return {"success": True, "outcome": "stale", "task_id": task.id,
                            "reason": stale}
                project = await self.db.get_project(fresh.project_id)
                refusal = self._not_router_refusal(project)
                if refusal is not None:
                    return refusal
                fresh_facts = await self._routing_task_facts(fresh, project)
                if plan.get("benchmark_arm") and self._benchmark_mapped_model(
                    plan["benchmark_class"], plan["benchmark_harness"], task.project_id
                ) != plan["requested_model"]:
                    return {"success": True, "outcome": "stale", "task_id": task.id,
                            "reason": "benchmark requested model mapping changed"}
                snapshot = await self._routing_snapshot(
                    fresh, project, sorted({c.intelligence_class for c in candidates}), conn=conn,
                )
                valid = [c for c in candidates if is_candidate(c, snapshot)
                         and c.provider not in fresh_facts.exclude_providers
                         and (not fresh_facts.preferred_provider
                              or c.provider == fresh_facts.preferred_provider)
                         and (not fresh_facts.needs_task_lifecycle or c.lifecycle == "task")]
                # The preference is read from the fresh task, never from the
                # plan: a strict one narrows the plan's own candidates to what
                # serves it (so apply cannot fall back out of one), and a soft
                # one flags them.  A preference set since the plan is applied.
                target = prefer_target(fresh_facts)
                if target:
                    if fresh_facts.prefer_mode == PREFER_STRICT:
                        valid = [c for c in valid if serves_preference(c, target)]
                        if not valid:
                            return {"success": True, "outcome": "stale", "task_id": task.id,
                                    "reason": (
                                        f"no candidate of the plan serves the strict "
                                        f"preference '{target}'"
                                    )}
                    valid = [
                        replace(c, prefer_target=serves_preference(c, target))
                        for c in valid
                    ]
                selection = reselect(valid, snapshot, balance)
                if selection is None:
                    return {"success": True, "outcome": "stale", "task_id": task.id,
                            "reason": "no candidate of the plan is launchable now"}
                chosen = selection.chosen
                adjusted = chosen.profile_id != planned_profile_id
                preference = preference_record(
                    fresh_facts, valid, snapshot, selection,
                )
                decision = selection_evidence(
                    valid, selection, snapshot, prefer_harnesses=plan.get("prefer_harnesses") or (),
                    preference=preference,
                )
                reason = f"kind {plan.get('task_type')}, rule {plan.get('rule')}; " + selection_reason(
                    decision
                )
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
                    "decision": decision,
                    "preference": preference,
                    "policy_sha256": plan.get("policy_sha256"),
                    "benchmark_arm": plan.get("benchmark_arm"),
                    "benchmark_class": plan.get("benchmark_class"),
                    "benchmark_harness": plan.get("benchmark_harness"),
                    "requested_model": plan.get("requested_model"),
                    "observed_models": plan.get("observed_models"),
                    "playbook_id": invocation.artifact_ref.playbook_id,
                    "artifact_sha256": invocation.artifact_ref.artifact_sha256,
                    "run_id": invocation.run_id,
                    "step_id": invocation.step_id,
                    "planned_profile_id": planned_profile_id,
                    "adjusted_at_apply": adjusted,
                    "routed_at": time.time(),
                    "live_context": dict(snapshot.context),
                }
                if raised := plan.get("class_raised_for_risk"):
                    # Only a plan whose risk floor raised the class carries it,
                    # so every other route record is unchanged.
                    route["class_raised_for_risk"] = raised
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
            preference=preference,
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
            "preference": preference,
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
        plans the task again.  It clears an override the same way.  It also
        stores the routing preference it is given (§4): ``--prefer`` names a
        harness or profile the router weighs before scoring and
        ``--prefer-mode`` chooses ``soft`` (the default) or ``strict``; an
        empty ``--prefer`` clears it, and an unknown name or mode is refused
        here, before anything is written.  A profile, provider or pin is
        refused in the dispatch path (``routing.choice_forbidden``); the
        emergency lever is ``task_route_override``.

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
        preference: dict[str, Any] = {}
        if "prefer" in args or "prefer_mode" in args:
            (target, mode), error = await validate_route_preference(
                self, task.project_id, args.get("prefer"), args.get("prefer_mode"),
                # Either half of the pair carries the other forward.
                keep_target=getattr(task, "prefer_target", None),
                keep_mode=getattr(task, "prefer_mode", None),
            )
            if error is not None:
                return {"success": False, "error": error}
            preference = {"prefer_target": target, "prefer_mode": mode}
        if not await self.db.reset_task_route(task.id, **hints, **preference):
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
            "prefer_target": getattr(fresh, "prefer_target", None),
            "prefer_mode": getattr(fresh, "prefer_mode", None),
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
