"""Fast, bounded LLM routing at the scheduler boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import time
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import and_, literal, or_, select

from src.agents.routing import resolve_profile
from src.assignment_routing import (
    AssignmentPlaybookError,
    EffectiveAssignmentRoute,
    assignment_input,
    assignment_input_hash,
    assignment_option_payload,
    options_hash,
    resolve_effective_route,
)
from src.database.tables import (
    gates as gates_table,
    projects as projects_table,
    task_gates as task_gates_table,
    tasks as tasks_table,
)
from src.database.queries.blocked_state import apply_label_filters
from src.models import (
    AgentState,
    AssignmentOption,
    Task,
    TaskAssignmentRoute,
    TaskStatus,
)
from src.playbooks.expressions import parse_json_from_text
from src.sessions.spec import _infer_provider_from_harness

logger = logging.getLogger(__name__)

_CONTROL_PROFILES = frozenset(
    {"supervisor", "triage", "reviewer", "final-reviewer", "playbook-compiler", "spec-ingest"}
)
#: Statuses a route may be decided for and stay valid across.  DEFINED is
#: included because that is the status every worker filing starts in, and a
#: root filing is *born* holding an open routing gate (swarm work model §12,
#: ``_create_worker_filed_task``).  Without DEFINED here the coordinator
#: never saw those tasks, so it never chose a class and never resolved the
#: gate that was waiting on it — filed work sat unrouted until a supervisor
#: hand-routed it.  ``_eligible_candidates`` keeps the population tight: a
#: DEFINED task qualifies only when it is already unblocked or its sole
#: blocker is that routing gate, so a dependency backlog is never router
#: traffic.
_ACTIVE_ROUTE_STATUSES = frozenset(
    {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.DEFINED}
)


class AssignmentRoutingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class AssignmentDecision:
    task_id: str
    input_hash: str
    intelligence_class: str
    provider: str | None
    reason: str


def validate_assignment_response(
    response: str,
    candidates: Sequence[Task],
    options: Sequence[AssignmentOption],
) -> list[AssignmentDecision]:
    """Validate the exact model contract before any decision is persisted."""

    parsed = parse_json_from_text(response)
    if not isinstance(parsed, dict) or set(parsed) != {"decisions"}:
        raise AssignmentRoutingValidationError("response must contain only a decisions array")
    raw = parsed.get("decisions")
    if not isinstance(raw, list):
        raise AssignmentRoutingValidationError("decisions must be an array")

    expected = {task.id: assignment_input_hash(task) for task in candidates}
    supported = {(o.intelligence_class, o.provider) for o in options}
    providers_by_class: dict[str, set[str]] = defaultdict(set)
    for class_id, provider in supported:
        providers_by_class[class_id].add(provider)
    seen: set[str] = set()
    decisions: list[AssignmentDecision] = []
    required = {"task_id", "intelligence_class", "reason"}
    allowed = required | {"provider", "input_hash"}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AssignmentRoutingValidationError(f"decision {index} must be an object")
        if not required.issubset(item) or not set(item).issubset(allowed):
            raise AssignmentRoutingValidationError(
                f"decision {index} has missing or unknown fields"
            )
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or task_id not in expected:
            raise AssignmentRoutingValidationError(f"unknown task_id {task_id!r}")
        if task_id in seen:
            raise AssignmentRoutingValidationError(f"duplicate decision for task {task_id}")
        seen.add(task_id)
        if "input_hash" in item and item["input_hash"] != expected[task_id]:
            raise AssignmentRoutingValidationError(f"input_hash mismatch for task {task_id}")
        class_id = item.get("intelligence_class")
        provider = item.get("provider")
        if not isinstance(class_id, str) or class_id not in providers_by_class:
            raise AssignmentRoutingValidationError(f"unsupported intelligence_class {class_id!r}")
        if provider is not None and (
            not isinstance(provider, str) or (class_id, provider) not in supported
        ):
            raise AssignmentRoutingValidationError(
                f"unsupported provider {provider!r} for class {class_id!r}"
            )
        reason = item.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 400:
            raise AssignmentRoutingValidationError(
                f"reason for task {task_id} must contain 1 to 400 characters"
            )
        decisions.append(
            AssignmentDecision(task_id, expected[task_id], class_id, provider, reason.strip())
        )
    if seen != set(expected):
        missing = sorted(set(expected) - seen)
        raise AssignmentRoutingValidationError(f"missing decisions for tasks {missing}")
    return decisions


def _effective_profiles(project_id: str, profiles):
    """Profiles a project can route to.

    Profiles are global: a durable worker is shared between projects, so the
    whole registry is in play.  Rows still carrying the retired
    ``project:<pid>:<type>`` id are skipped until the startup migration drops
    them — they resolve nowhere.
    """
    return [profile for profile in profiles if ":" not in profile.id]


def profile_provider(profile, harness_registry=None, project_id: str | None = None) -> str:
    """Return the provider selected by a profile's harness."""

    harness_id = getattr(profile, "harness", "") or ""
    if not harness_id:
        return ""
    harness = harness_registry.get(harness_id, project_id) if harness_registry else None
    if harness is None:
        harness = type(
            "HarnessRef",
            (),
            {"id": harness_id, "command": harness_id, "provider": ""},
        )()
    return str(getattr(harness, "provider", "") or _infer_provider_from_harness(harness))


def task_assignment_options(
    task: Task,
    options: Sequence[AssignmentOption],
    profiles,
    harness_registry=None,
) -> tuple[AssignmentOption, ...]:
    """Narrow a pinned task's catalog to its profile's fixed class and provider.

    A task without a pinned profile, or a profile without ``default_class``,
    keeps the ordinary project-wide catalog.  Profile resolution mirrors the
    scheduler.
    """

    if not task.profile_id:
        return tuple(options)
    profile = resolve_profile({profile.id: profile for profile in profiles}, task.profile_id)
    fixed_class = (getattr(profile, "default_class", "") or "").strip()
    if not fixed_class:
        return tuple(options)
    provider = profile_provider(profile, harness_registry, task.project_id)
    return tuple(
        option
        for option in options
        if option.intelligence_class == fixed_class
        and (not provider or option.provider == provider)
    )


def pool_profile_for_route(
    project_id: str,
    profiles,
    intelligence_class: str,
    provider: str | None,
    harness_registry=None,
    *,
    prefer_provider: str | None = None,
) -> str | None:
    """Pick the ``lifecycle: pool`` profile whose fixed class serves a route.

    Pool workers claim work by ``profile_id`` and only take tasks whose
    effective class equals their own, so a routed task with no profile can
    only ever be claimed when the project default pool happens to run the
    routed class.  This is the deterministic class → profile step the
    routing spec leaves to "existing scheduling rules": among pool profiles
    whose ``default_class`` is the routed class, honour a provider pin,
    then prefer the default pool's provider, then the lowest id.
    """

    candidates = []
    for profile in _effective_profiles(project_id, profiles):
        if getattr(profile, "lifecycle", "task") != "pool" or not getattr(profile, "harness", ""):
            continue
        if (getattr(profile, "default_class", "") or "").strip() != intelligence_class:
            continue
        profile_prov = profile_provider(profile, harness_registry, project_id)
        if provider and profile_prov != provider:
            continue
        candidates.append((profile_prov != (prefer_provider or ""), profile.id))
    if not candidates:
        return None
    return min(candidates)[1]


def _catalog_hash(
    project_id: str,
    options: Sequence[AssignmentOption],
    profiles,
) -> str:
    """Hash options plus every effective profile's fixed-class constraint."""

    return options_hash(
        options,
        profile_defaults=(
            (profile.id, (profile.default_class or "").strip())
            for profile in _effective_profiles(project_id, profiles)
        ),
    )


def build_assignment_options(
    project_id: str,
    profiles,
    agents,
    harness_registry,
    intelligence_classes,
) -> tuple[AssignmentOption, ...]:
    """Build the normalized ordinary-worker class/provider catalog."""

    effective_profiles = _effective_profiles(project_id, profiles)

    enabled_agents = [
        agent for agent in agents
        if agent.enabled and agent.role == "worker" and agent.deleted_at is None
    ]
    aggregate: dict[tuple[str, str], dict[str, int]] = {}
    for profile in effective_profiles:
        slug = profile.id
        if (
            slug in _CONTROL_PROFILES
            or profile.runtime == "supervisor"
            or profile.lifecycle not in {"task", "pool"}
            or not profile.harness
        ):
            continue
        provider = profile_provider(profile, harness_registry, project_id)
        if not provider:
            continue
        fixed_class = (profile.default_class or "").strip()
        class_ids = [fixed_class] if fixed_class else sorted(intelligence_classes)
        if profile.lifecycle == "pool" and not fixed_class:
            continue
        matching_agents = [
            agent for agent in enabled_agents
            if agent.profile_id in {profile.id, slug}
        ]
        for class_id in class_ids:
            cls = intelligence_classes.get(class_id)
            if cls is None:
                continue
            mapping = cls.mapping.get("codex") if profile.harness == "codex" else None
            mapping = mapping or cls.mapping.get(provider)
            if not isinstance(mapping, dict) or not mapping.get("model"):
                continue
            compatible_agents = [
                agent for agent in matching_agents
                if not agent.intelligence_class or agent.intelligence_class == class_id
            ]
            key = (class_id, provider)
            counts = aggregate.setdefault(
                key, {"configured": 0, "idle": 0, "busy": 0}
            )
            potential = profile.max_active if profile.lifecycle == "pool" else None
            counts["configured"] += max(1, potential or len(compatible_agents))
            counts["idle"] += sum(a.state == AgentState.IDLE for a in compatible_agents)
            counts["busy"] += sum(a.state == AgentState.BUSY for a in compatible_agents)

    return tuple(
        AssignmentOption(
            intelligence_class=class_id,
            provider=provider,
            configured_capacity=counts["configured"],
            idle_count=counts["idle"],
            busy_count=counts["busy"],
            availability="unknown",
        )
        for (class_id, provider), counts in sorted(aggregate.items())
    )


class AssignmentRoutingCoordinator:
    """Reconcile missing routes in small project batches before scheduling."""

    def __init__(self, orchestrator, *, batch_size: int = 25):
        self.owner = orchestrator
        self.batch_size = batch_size
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._retry: dict[str, tuple[int, float, str]] = {}
        self._catalog_hashes: dict[str, str] = {}
        self._running_task_ids: set[str] = set()
        self._task_retry: dict[str, tuple[float, str]] = {}
        self._v2_cache: dict[str, tuple[str, str]] = {}

    @property
    def db(self):
        # Tests and embedded callers may replace ``orchestrator.db`` after
        # construction; always use the live adapter.
        return self.owner.db

    @property
    def diagnostics(self) -> dict[str, tuple[int, float, str]]:
        return dict(self._retry)

    async def _catalog(self, project_id: str):
        classes = getattr(
            getattr(self.owner, "session_spec_builder", None),
            "_intelligence_classes",
            {},
        ) or {}
        profiles = await self.db.list_profiles()
        options = build_assignment_options(
            project_id,
            profiles,
            await self.db.list_agents(),
            getattr(self.owner, "harness_registry", None),
            classes,
        )
        self._catalog_hashes[project_id] = _catalog_hash(project_id, options, profiles)
        return options, profiles

    async def _options(self, project_id: str) -> tuple[AssignmentOption, ...]:
        """Return the project-wide catalog for callers that do not route a task."""

        options, _profiles = await self._catalog(project_id)
        return options

    def cached_options_hash(self, project_id: str) -> str | None:
        """Return the last catalog observed by reconciliation without I/O."""

        return self._catalog_hashes.get(project_id)

    async def _eligible_candidates(self) -> list[Task]:
        open_routing_gate = (
            select(literal(1))
            .select_from(
                task_gates_table.join(
                    gates_table, gates_table.c.id == task_gates_table.c.gate_id
                )
            )
            .where(
                task_gates_table.c.task_id == tasks_table.c.id,
                gates_table.c.status == "open",
                gates_table.c.gate_type == "routing",
            )
            .exists()
        )
        statement = select(tasks_table).where(
            or_(
                tasks_table.c.status.in_(
                    [TaskStatus.READY.value, TaskStatus.BLOCKED.value]
                ),
                and_(
                    tasks_table.c.status == TaskStatus.DEFINED.value,
                    or_(tasks_table.c.is_blocked == 0, open_routing_gate),
                ),
            ),
            tasks_table.c.assigned_agent_id.is_(None),
            tasks_table.c.is_plan_subtask == 0,
        )
        statement = apply_label_filters(statement, exclude_hold=True)
        async with self.db._engine.begin() as connection:
            rows = (await connection.execute(statement)).mappings().fetchall()
        tasks = [self.db._row_to_task(row) for row in rows]
        candidates: list[Task] = []
        for task in tasks:
            if task.is_blocked:
                if await self.db.get_blocking_dependencies(task.id):
                    continue
                gates = [
                    gate for gate in await self.db.get_gates_for_task(task.id)
                    if gate["status"] != "resolved"
                ]
                if not gates or any(
                    gate["status"] != "open" or gate["gate_type"] != "routing"
                    for gate in gates
                ):
                    continue
            candidates.append(task)
        return candidates

    async def _resolve_routing_gates(self, task_id: str, *, run_id: str | None) -> None:
        for gate in await self.db.get_gates_for_task(task_id):
            if gate["status"] == "open" and gate["gate_type"] == "routing":
                await self.owner._resolve_gate_and_emit(
                    gate["id"],
                    resolved_by="assignment-routing",
                    resolution=(
                        f"intelligence route selected by playbook run {run_id}"
                        if run_id else "explicit intelligence_class already set"
                    ),
                )

    @staticmethod
    def _batch_key(
        project,
        playbook,
        tasks,
        catalog_hash: str,
        *,
        artifact_sha256: str | None = None,
    ) -> str:
        value = {
            "project_id": project.id,
            "playbook_id": getattr(playbook, "playbook_id", None) or getattr(playbook, "id", ""),
            "playbook_version": getattr(playbook, "version", None),
            "artifact_sha256": artifact_sha256,
            "tasks": sorted((task.id, assignment_input_hash(task)) for task in tasks),
            "options_hash": catalog_hash,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()[:24]

    def _note_failure(self, batch_key: str, error: str, tasks: Sequence[Task]) -> None:
        count = self._retry.get(batch_key, (0, 0.0, ""))[0] + 1
        delay = min(300.0, float(2 ** min(count - 1, 8)))
        retry_at = time.time() + delay
        self._retry[batch_key] = (count, retry_at, error)
        for task in tasks:
            self._task_retry[task.id] = (retry_at, error)
        logger.warning("assignment routing batch %s failed: %s", batch_key, error)

    async def _assignment_artifact(self, project):
        from src.assignment_routing import DEFAULT_ASSIGNMENT_PLAYBOOK_ID
        from src.playbooks.services import DatabaseActivationSource

        playbook_id = project.assignment_playbook_id or DEFAULT_ASSIGNMENT_PLAYBOOK_ID
        ref = await DatabaseActivationSource(self.db).artifact_for(
            playbook_id, scope_identifier=project.id
        )
        if ref is None:
            raise AssignmentPlaybookError(
                f"assignment playbook '{playbook_id}' has no ready V2 activation"
            )
        return ref

    async def _route_batch(self, project, tasks, options, catalog_hash):
        try:
            artifact_ref = await self._assignment_artifact(project)
        except AssignmentPlaybookError as exc:
            logger.warning("assignment routing unavailable for project %s: %s", project.id, exc)
            return {}
        batch_key = self._batch_key(
            project, artifact_ref, tasks, catalog_hash,
            artifact_sha256=artifact_ref.artifact_sha256,
        )
        retry = self._retry.get(batch_key)
        if retry and retry[1] > time.time():
            return {}
        cached = self._v2_cache.get(batch_key)
        run_id = cached[0] if cached else None
        response = cached[1] if cached else None
        if cached is None:
            event = {
                "type": "assignment.route.requested",
                "_event_type": "assignment.route.requested",
                "event_id": f"assignment:{batch_key}",
                "project_id": project.id,
                "tasks": [
                    {**assignment_input(task), "input_hash": assignment_input_hash(task)}
                    for task in tasks
                ],
                "options": [assignment_option_payload(option) for option in options],
                "options_hash": options_hash(options),
                "catalog_hash": catalog_hash,
            }
            self._running_task_ids.update(task.id for task in tasks)
            try:
                from src.commands.principal import ExecutionPrincipal
                from src.playbooks.services import build_v2_engine

                handler = getattr(self.owner, "_command_handler", None)
                if handler is None:
                    self._note_failure(batch_key, "command handler unavailable", tasks)
                    return {}
                engine = build_v2_engine(
                    config=self.owner.config, db=self.db, handler=handler,
                    llm=getattr(self.owner, "llm", None),
                    bus=getattr(self.owner, "bus", None),
                )
                artifact = engine.services.artifact_store.load(artifact_ref.artifact_sha256)
                if artifact.purpose != "assignment_routing":
                    self._note_failure(batch_key, "artifact is not assignment routing", tasks)
                    return {}
                rule = next((
                    candidate for candidate in artifact.rules
                    if engine._trigger_matches(candidate, "assignment.route.requested", event)
                ), None)
                if rule is None:
                    self._note_failure(batch_key, "assignment rule unavailable", tasks)
                    return {}
                result = await engine.run_rule(
                    artifact_ref, rule.id, event,
                    ExecutionPrincipal.service("assignment-routing"),
                    project_task=False,
                )
                run_id = result.run_id
                if result.lifecycle.value != "completed" or result.result_value is None:
                    self._note_failure(
                        batch_key,
                        result.snapshot.error if result.snapshot else "assignment playbook failed",
                        tasks,
                    )
                    return {}
                response = result.result_value if isinstance(result.result_value, str) else json.dumps(
                    result.result_value, sort_keys=True
                )
                self._v2_cache[batch_key] = (run_id, response)
            finally:
                self._running_task_ids.difference_update(task.id for task in tasks)
        try:
            decisions = validate_assignment_response(response or "", tasks, options)
        except AssignmentRoutingValidationError as exc:
            self._note_failure(batch_key, str(exc), tasks)
            return {}
        committed = await self._commit(
            project, artifact_ref, tasks, options, catalog_hash, decisions, run_id
        )
        if committed:
            self._retry.pop(batch_key, None)
            for task_id in committed:
                self._task_retry.pop(task_id, None)
        return committed

    async def _commit(
        self, project, artifact_ref, original_tasks, options, catalog_hash, decisions, run_id
    ):
        current_project = await self.db.get_project(project.id)
        if current_project is None:
            return {}
        try:
            current_ref = await self._assignment_artifact(current_project)
        except AssignmentPlaybookError:
            return {}
        if current_ref.artifact_sha256 != artifact_ref.artifact_sha256:
            return {}
        current_options, current_profiles = await self._catalog(project.id)
        current_hash = _catalog_hash(project.id, current_options, current_profiles)
        if current_hash != catalog_hash:
            return {}
        batch_options_hash = options_hash(options)
        saved: list[TaskAssignmentRoute] = []
        async with self.db.immediate() as conn:
            rows = (
                await conn.execute(
                    select(tasks_table)
                    .where(tasks_table.c.id.in_([decision.task_id for decision in decisions]))
                    .with_for_update()
                )
            ).mappings().fetchall()
            current = {row["id"]: self.db._row_to_task(row) for row in rows}
            project_row = (
                await conn.execute(
                    select(projects_table.c.assignment_playbook_id)
                    .where(projects_table.c.id == project.id)
                )
            ).fetchone()
            selected_id = project_row[0] if project_row else None
            if selected_id != project.assignment_playbook_id:
                return {}
            for decision in decisions:
                task = current.get(decision.task_id)
                if (
                    task is None or task.project_id != project.id
                    or task.assigned_agent_id is not None
                    or task.status not in _ACTIVE_ROUTE_STATUSES
                    or (task.intelligence_class or "").strip()
                    or assignment_input_hash(task) != decision.input_hash
                    or options_hash(task_assignment_options(
                        task,
                        current_options,
                        current_profiles,
                        getattr(self.owner, "harness_registry", None),
                    ))
                    != batch_options_hash
                ):
                    continue
                saved.append(TaskAssignmentRoute(
                    task_id=task.id, project_id=task.project_id,
                    input_hash=decision.input_hash, task_updated_at=task.updated_at,
                    options_hash=current_hash,
                    intelligence_class=decision.intelligence_class,
                    provider=decision.provider,
                    playbook_id=artifact_ref.playbook_id,
                    playbook_version=artifact_ref.version,
                    playbook_run_id=run_id,
                    reason=decision.reason, decided_at=time.time(),
                ))
            await self.db.upsert_task_assignment_routes(saved, conn=conn)
        result: dict[str, EffectiveAssignmentRoute] = {}
        for route in saved:
            task = current[route.task_id]
            effective = resolve_effective_route(task, route, current_hash)
            if effective is not None:
                result[task.id] = effective
                await self._resolve_routing_gates(task.id, run_id=run_id)
        return result

    async def _restamp_route_revisions(self, drifted: dict[str, TaskAssignmentRoute]) -> None:
        """Realign a content-fresh route row with the task's current revision.

        ``resolve_effective_route`` treats ``input_hash`` as the authority,
        but the pool claim query joins on ``task_updated_at`` because SQL
        cannot hash a task row.  Any write that moves ``updated_at`` without
        touching the routed inputs — a status flip, a note, a released
        assignment — would otherwise hide the task from every claim until an
        edit forced a fresh decision.  Re-stamping is free: no LLM call, and
        the decision itself is unchanged.
        """
        if not drifted:
            return
        async with self.db.immediate() as conn:
            rows = (
                await conn.execute(
                    select(tasks_table)
                    .where(tasks_table.c.id.in_(sorted(drifted)))
                    .with_for_update()
                )
            ).mappings().fetchall()
            saved: list[TaskAssignmentRoute] = []
            for row in rows:
                task = self.db._row_to_task(row)
                route = drifted[task.id]
                if (
                    route.task_updated_at == task.updated_at
                    or route.input_hash != assignment_input_hash(task)
                ):
                    continue
                saved.append(replace(route, task_updated_at=task.updated_at))
            await self.db.upsert_task_assignment_routes(saved, conn=conn)

    async def reconcile(self) -> dict[str, EffectiveAssignmentRoute]:
        candidates = await self._eligible_candidates()
        by_project: dict[str, list[Task]] = defaultdict(list)
        for task in candidates:
            by_project[task.project_id].append(task)
        resolved: dict[str, EffectiveAssignmentRoute] = {}
        for project_id in sorted(by_project):
            lock = self._project_locks.setdefault(project_id, asyncio.Lock())
            if lock.locked():
                continue
            async with lock:
                project = await self.db.get_project(project_id)
                if project is None:
                    continue
                try:
                    await self._assignment_artifact(project)
                except AssignmentPlaybookError as exc:
                    logger.warning(
                        "assignment routing unavailable for project %s: %s",
                        project_id,
                        exc,
                    )
                    continue
                options, profiles = await self._catalog(project_id)
                tasks = sorted(by_project[project_id], key=lambda task: (task.priority, task.id))
                saved_rows = {
                    row.task_id: row
                    for row in await self.db.list_task_assignment_routes([task.id for task in tasks])
                }
                catalog_hash = _catalog_hash(project_id, options, profiles)
                drifted: dict[str, TaskAssignmentRoute] = {}
                pending_by_options: dict[str, tuple[tuple[AssignmentOption, ...], list[Task]]] = {}
                for task in tasks:
                    row = saved_rows.get(task.id)
                    effective = resolve_effective_route(task, row, catalog_hash)
                    if effective is not None:
                        resolved[task.id] = effective
                        self._task_retry.pop(task.id, None)
                        if task.is_blocked:
                            await self._resolve_routing_gates(
                                task.id,
                                run_id=(
                                    row.playbook_run_id
                                    if effective.source == "playbook" and row is not None
                                    else None
                                ),
                            )
                        if (
                            effective.source == "playbook"
                            and row is not None
                            and row.task_updated_at != task.updated_at
                        ):
                            drifted[task.id] = row
                    else:
                        task_options = task_assignment_options(
                            task,
                            options,
                            profiles,
                            getattr(self.owner, "harness_registry", None),
                        )
                        if task_options:
                            key = options_hash(task_options)
                            _batch_options, batch_tasks = pending_by_options.setdefault(
                                key, (task_options, [])
                            )
                            batch_tasks.append(task)
                await self._restamp_route_revisions(drifted)
                for batch_options, pending in pending_by_options.values():
                    for start in range(0, len(pending), self.batch_size):
                        resolved.update(
                            await self._route_batch(
                                project,
                                pending[start:start + self.batch_size],
                                batch_options,
                                catalog_hash,
                            )
                        )
                await self._backfill_pool_profiles(
                    project, tasks, resolved, profiles, catalog_hash
                )
        return resolved

    async def _backfill_pool_profiles(
        self, project, tasks, resolved, profiles, catalog_hash: str
    ) -> None:
        """Give every routed, unpinned task the pool profile that runs its class.

        Routing only decides the intelligence class.  Under pools nothing
        else turned that class into a claimable task: an unpinned task counts
        as demand for the project's *default* pool and is then refused by
        every worker in it, because a pool worker only claims its own fixed
        class.  So once a route is settled — explicit or playbook — and the
        project default is a pool that does not serve it, pin the task to
        the pool that does.  The task's ``updated_at`` and input hash move
        with the pin, so a playbook route is re-stamped in the same pass
        rather than being thrown away and re-asked for.
        """
        pending = [
            task for task in tasks
            if task.id in resolved
            and not task.profile_id
            and task.assigned_agent_id is None
            and task.status in _ACTIVE_ROUTE_STATUSES
        ]
        if not pending:
            return
        pool_profiles = {
            profile.id: profile
            for profile in _effective_profiles(project.id, profiles)
            if getattr(profile, "lifecycle", "task") == "pool"
        }
        if not pool_profiles:
            return
        resolver = getattr(self.owner, "_effective_default_profile_id", None)
        default_id = (
            await resolver(project) if resolver is not None else project.default_profile_id
        )
        default_pool = pool_profiles.get(default_id or "")
        if default_pool is None:
            return  # push scheduling already picks a worker by class
        registry = getattr(self.owner, "harness_registry", None)
        default_class = (getattr(default_pool, "default_class", "") or "").strip()
        default_provider = profile_provider(default_pool, registry, project.id)
        repinned: dict[str, str] = {}
        for task in pending:
            route = resolved[task.id]
            if default_class == route.intelligence_class and (
                not route.provider or route.provider == default_provider
            ):
                continue
            chosen = pool_profile_for_route(
                project.id, profiles, route.intelligence_class, route.provider,
                registry, prefer_provider=default_provider,
            )
            if chosen is None:
                logger.warning(
                    "task %s routes to class %s but no pool profile serves it",
                    task.id, route.intelligence_class,
                )
                continue
            if await self.db.update_task_routing(
                task.id, profile_id=chosen, intelligence_class=None,
                preferred_workspace_id=None,
            ):
                repinned[task.id] = chosen
                logger.info(
                    "task %s pinned to pool profile %s for class %s",
                    task.id, chosen, route.intelligence_class,
                )
        if not repinned:
            return
        saved_rows = {
            row.task_id: row
            for row in await self.db.list_task_assignment_routes(sorted(repinned))
        }
        if not saved_rows:
            return
        async with self.db.immediate() as conn:
            rows = (
                await conn.execute(
                    select(tasks_table)
                    .where(tasks_table.c.id.in_(sorted(saved_rows)))
                    .with_for_update()
                )
            ).mappings().fetchall()
            restamped: list[TaskAssignmentRoute] = []
            for row in rows:
                task = self.db._row_to_task(row)
                saved = saved_rows[task.id]
                if task.profile_id != repinned[task.id]:
                    continue
                restamped.append(replace(
                    saved,
                    input_hash=assignment_input_hash(task),
                    task_updated_at=task.updated_at,
                    options_hash=catalog_hash,
                ))
            await self.db.upsert_task_assignment_routes(restamped, conn=conn)

    async def routes_for(self, tasks: Sequence[Task]) -> dict[str, EffectiveAssignmentRoute]:
        by_project: dict[str, list[Task]] = defaultdict(list)
        for task in tasks:
            by_project[task.project_id].append(task)
        resolved: dict[str, EffectiveAssignmentRoute] = {}
        saved = {
            row.task_id: row
            for row in await self.db.list_task_assignment_routes([task.id for task in tasks])
        }
        for project_id, project_tasks in by_project.items():
            options, profiles = await self._catalog(project_id)
            catalog_hash = _catalog_hash(project_id, options, profiles)
            for task in project_tasks:
                route = resolve_effective_route(task, saved.get(task.id), catalog_hash)
                if route is not None:
                    resolved[task.id] = route
        return resolved

    async def explain(self, task: Task) -> tuple[dict | None, dict | None]:
        """Return route audit detail and one actionable routing reason."""

        options, profiles = await self._catalog(task.project_id)
        catalog_hash = _catalog_hash(task.project_id, options, profiles)
        saved = await self.db.get_task_assignment_route(task.id)
        effective = resolve_effective_route(task, saved, catalog_hash)
        detail = None
        if effective is not None:
            detail = {
                "source": effective.source,
                "intelligence_class": effective.intelligence_class,
                "provider": effective.provider,
                "reason": saved.reason if effective.source == "playbook" and saved else None,
                "playbook_id": saved.playbook_id if effective.source == "playbook" and saved else None,
                "playbook_version": (
                    saved.playbook_version if effective.source == "playbook" and saved else None
                ),
                "playbook_run_id": (
                    saved.playbook_run_id if effective.source == "playbook" and saved else None
                ),
                "freshness": "fresh",
            }
            if task.assigned_agent_id is None and task.status in _ACTIVE_ROUTE_STATUSES:
                return detail, {
                    "code": "route_waiting_for_compatible_agent",
                    "detail": (
                        f"route selects intelligence class '{effective.intelligence_class}'"
                        + (f" on provider '{effective.provider}'" if effective.provider else "")
                        + "; waiting for existing scheduling constraints"
                    ),
                    "ref": effective.decision_id,
                }
            return detail, None

        if saved is not None:
            detail = {
                "source": "playbook",
                "intelligence_class": saved.intelligence_class,
                "provider": saved.provider,
                "reason": saved.reason,
                "playbook_id": saved.playbook_id,
                "playbook_version": saved.playbook_version,
                "playbook_run_id": saved.playbook_run_id,
                "freshness": "stale",
            }
        if task.id in self._running_task_ids:
            return detail, {
                "code": "assignment_playbook_running",
                "detail": "assignment playbook is selecting an intelligence route",
                "ref": task.id,
            }
        retry = self._task_retry.get(task.id)
        if retry and retry[0] > time.time():
            return detail, {
                "code": "assignment_route_retry",
                "detail": f"assignment route failed and will retry: {retry[1]}",
                "ref": task.id,
            }
        project = await self.db.get_project(task.project_id)
        try:
            if project is None:
                raise AssignmentPlaybookError(f"project '{task.project_id}' is missing")
            await self._assignment_artifact(project)
            if not task_assignment_options(
                task,
                options,
                profiles,
                getattr(self.owner, "harness_registry", None),
            ):
                raise AssignmentPlaybookError(
                    "no compatible intelligence class/provider options are configured"
                )
        except AssignmentPlaybookError as exc:
            return detail, {
                "code": "assignment_playbook_unavailable",
                "detail": str(exc),
                "ref": project.assignment_playbook_id if project else task.project_id,
            }
        if saved is not None:
            return detail, {
                "code": "assignment_route_stale",
                "detail": "saved assignment route no longer matches the task or option catalog",
                "ref": saved.playbook_run_id,
            }
        return None, {
            "code": "awaiting_intelligence_route",
            "detail": "task is awaiting an assignment playbook intelligence route",
            "ref": task.id,
        }
