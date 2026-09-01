"""Fast, bounded LLM routing at the scheduler boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import time
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.assignment_routing import (
    AssignmentPlaybookError,
    EffectiveAssignmentRoute,
    assignment_input,
    assignment_input_hash,
    assignment_option_payload,
    options_hash,
    resolve_effective_route,
    select_assignment_playbook,
)
from src.database.tables import projects as projects_table, tasks as tasks_table
from src.database.queries.blocked_state import apply_label_filters
from src.models import (
    AgentState,
    AssignmentOption,
    Task,
    TaskAssignmentRoute,
    TaskStatus,
)
from src.playbooks.runner import PlaybookRunner, _parse_json_from_text
from src.sessions.spec import _infer_provider_from_harness

logger = logging.getLogger(__name__)

_CONTROL_PROFILES = frozenset(
    {"supervisor", "triage", "reviewer", "final-reviewer", "playbook-compiler", "spec-ingest"}
)
_ACTIVE_ROUTE_STATUSES = frozenset({TaskStatus.READY, TaskStatus.BLOCKED})


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

    parsed = _parse_json_from_text(response)
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
    required = {"task_id", "input_hash", "intelligence_class", "reason"}
    allowed = required | {"provider"}
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
        if item.get("input_hash") != expected[task_id]:
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


def _profile_slug(profile_id: str) -> str:
    return profile_id.rsplit(":", 1)[-1]


def build_assignment_options(
    project_id: str,
    profiles,
    agents,
    harness_registry,
    intelligence_classes,
) -> tuple[AssignmentOption, ...]:
    """Build the normalized ordinary-worker class/provider catalog."""

    scoped = {
        _profile_slug(profile.id): profile
        for profile in profiles
        if profile.id.startswith(f"project:{project_id}:")
    }
    effective_profiles = []
    for profile in profiles:
        if profile.id.startswith("project:"):
            if profile.id.startswith(f"project:{project_id}:"):
                effective_profiles.append(profile)
            continue
        if profile.id in scoped:
            continue
        effective_profiles.append(profile)

    enabled_agents = [
        agent for agent in agents
        if agent.enabled and agent.role == "worker" and agent.deleted_at is None
    ]
    aggregate: dict[tuple[str, str], dict[str, int]] = {}
    for profile in effective_profiles:
        slug = _profile_slug(profile.id)
        if (
            slug in _CONTROL_PROFILES
            or profile.runtime == "supervisor"
            or profile.lifecycle not in {"task", "pool"}
            or not profile.harness
        ):
            continue
        harness = harness_registry.get(profile.harness, project_id) if harness_registry else None
        if harness is None:
            harness = type("HarnessRef", (), {
                "id": profile.harness, "command": profile.harness, "provider": ""
            })()
        provider = str(getattr(harness, "provider", "") or _infer_provider_from_harness(harness))
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

    @property
    def db(self):
        # Tests and embedded callers may replace ``orchestrator.db`` after
        # construction; always use the live adapter.
        return self.owner.db

    @property
    def diagnostics(self) -> dict[str, tuple[int, float, str]]:
        return dict(self._retry)

    async def _options(self, project_id: str) -> tuple[AssignmentOption, ...]:
        classes = getattr(
            getattr(self.owner, "session_spec_builder", None),
            "_intelligence_classes",
            {},
        ) or {}
        options = build_assignment_options(
            project_id,
            await self.db.list_profiles(),
            await self.db.list_agents(),
            getattr(self.owner, "harness_registry", None),
            classes,
        )
        self._catalog_hashes[project_id] = options_hash(options)
        return options

    def cached_options_hash(self, project_id: str) -> str | None:
        """Return the last catalog observed by reconciliation without I/O."""

        return self._catalog_hashes.get(project_id)

    async def _eligible_candidates(self) -> list[Task]:
        statement = select(tasks_table).where(
            tasks_table.c.status.in_([status.value for status in _ACTIVE_ROUTE_STATUSES]),
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
    def _batch_key(project, playbook, tasks, catalog_hash: str) -> str:
        value = {
            "project_id": project.id,
            "playbook_id": playbook.id,
            "playbook_version": playbook.version,
            "tasks": sorted((task.id, assignment_input_hash(task)) for task in tasks),
            "options_hash": catalog_hash,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()[:24]

    async def _attempt_event_id(self, playbook_id: str, batch_key: str):
        attempt = 0
        while True:
            event_id = f"assignment:{batch_key}:{attempt}"
            existing = await self.db.get_playbook_run_by_event(playbook_id, event_id)
            if existing is None:
                return event_id, None
            if existing.status in {"failed", "timed_out", "cancelled"}:
                attempt += 1
                continue
            return event_id, existing

    @staticmethod
    def _existing_response(run) -> str | None:
        if run.status != "completed":
            return None
        try:
            messages = json.loads(run.conversation_history or "[]")
        except (TypeError, json.JSONDecodeError):
            return None
        for message in reversed(messages):
            if message.get("role") == "assistant" and isinstance(message.get("content"), str):
                return message["content"]
        return None

    def _note_failure(self, batch_key: str, error: str, tasks: Sequence[Task]) -> None:
        count = self._retry.get(batch_key, (0, 0.0, ""))[0] + 1
        delay = min(300.0, float(2 ** min(count - 1, 8)))
        retry_at = time.time() + delay
        self._retry[batch_key] = (count, retry_at, error)
        for task in tasks:
            self._task_retry[task.id] = (retry_at, error)
        logger.warning("assignment routing batch %s failed: %s", batch_key, error)

    async def _route_batch(self, project, tasks, options):
        manager = self.owner.playbook_manager
        playbook = select_assignment_playbook(manager, project)
        catalog_hash = options_hash(options)
        batch_key = self._batch_key(project, playbook, tasks, catalog_hash)
        retry = self._retry.get(batch_key)
        if retry and retry[1] > time.time():
            return {}
        event_id, existing = await self._attempt_event_id(playbook.id, batch_key)
        response = self._existing_response(existing) if existing else None
        run_id = existing.run_id if existing else None
        if existing is not None and response is None:
            return {}
        if existing is None:
            event = {
                "type": "assignment.route.requested",
                "event_id": event_id,
                "project_id": project.id,
                "tasks": [
                    {**assignment_input(task), "input_hash": assignment_input_hash(task)}
                    for task in tasks
                ],
                "options": [assignment_option_payload(option) for option in options],
                "options_hash": catalog_hash,
            }
            runner = PlaybookRunner(
                graph=playbook.to_dict(),
                event=event,
                services=self.owner.playbook_services(),
                db=self.db,
                event_bus=getattr(self.owner, "bus", None),
                sync_task_projection=False,
                tool_overrides=[],
            )
            self._running_task_ids.update(task.id for task in tasks)
            try:
                try:
                    result = await runner.run()
                except IntegrityError:
                    logger.info("assignment batch %s lost the playbook-run insert race", batch_key)
                    return {}
            finally:
                self._running_task_ids.difference_update(task.id for task in tasks)
            run_id = runner.run_id
            if result.status != "completed" or not result.final_response:
                self._note_failure(
                    batch_key, result.error or "assignment playbook failed", tasks
                )
                return {}
            response = result.final_response
        try:
            decisions = validate_assignment_response(response or "", tasks, options)
        except AssignmentRoutingValidationError as exc:
            # The graph completed, but its application-level output is not a
            # valid assignment decision. Persist that terminal failure so the
            # next reconciliation advances the event attempt ordinal instead
            # of replaying this same completed response forever.
            if run_id is not None:
                await self.db.update_playbook_run(
                    run_id,
                    status="failed",
                    completed_at=time.time(),
                    error=f"invalid assignment response: {exc}",
                )
            self._note_failure(batch_key, str(exc), tasks)
            return {}
        committed = await self._commit(project, playbook, tasks, options, decisions, run_id)
        if committed:
            self._retry.pop(batch_key, None)
            for task_id in committed:
                self._task_retry.pop(task_id, None)
        return committed

    async def _commit(self, project, playbook, original_tasks, options, decisions, run_id):
        current_project = await self.db.get_project(project.id)
        if current_project is None:
            return {}
        current_playbook = select_assignment_playbook(self.owner.playbook_manager, current_project)
        if current_playbook.id != playbook.id or current_playbook.version != playbook.version:
            return {}
        current_options = await self._options(project.id)
        current_hash = options_hash(current_options)
        if current_hash != options_hash(options):
            return {}
        original = {task.id: task for task in original_tasks}
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
                before = original[decision.task_id]
                if (
                    task is None
                    or task.project_id != project.id
                    or task.assigned_agent_id is not None
                    or task.status not in _ACTIVE_ROUTE_STATUSES
                    or (task.intelligence_class or "").strip()
                    or task.updated_at != before.updated_at
                    or assignment_input_hash(task) != decision.input_hash
                ):
                    continue
                saved.append(TaskAssignmentRoute(
                    task_id=task.id,
                    project_id=task.project_id,
                    input_hash=decision.input_hash,
                    task_updated_at=task.updated_at,
                    options_hash=current_hash,
                    intelligence_class=decision.intelligence_class,
                    provider=decision.provider,
                    playbook_id=playbook.id,
                    playbook_version=playbook.version,
                    playbook_run_id=run_id,
                    reason=decision.reason,
                    decided_at=time.time(),
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
                    select_assignment_playbook(self.owner.playbook_manager, project)
                except AssignmentPlaybookError as exc:
                    logger.warning(
                        "assignment routing unavailable for project %s: %s",
                        project_id,
                        exc,
                    )
                    continue
                options = await self._options(project_id)
                tasks = sorted(by_project[project_id], key=lambda task: (task.priority, task.id))
                pending: list[Task] = []
                saved_rows = {
                    row.task_id: row
                    for row in await self.db.list_task_assignment_routes([task.id for task in tasks])
                }
                catalog_hash = options_hash(options)
                for task in tasks:
                    effective = resolve_effective_route(task, saved_rows.get(task.id), catalog_hash)
                    if effective is not None:
                        resolved[task.id] = effective
                        self._task_retry.pop(task.id, None)
                        if effective.source == "explicit":
                            await self._resolve_routing_gates(task.id, run_id=None)
                    else:
                        pending.append(task)
                if not pending or not options:
                    continue
                for start in range(0, len(pending), self.batch_size):
                    resolved.update(
                        await self._route_batch(
                            project, pending[start:start + self.batch_size], options
                        )
                    )
        return resolved

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
            catalog_hash = options_hash(await self._options(project_id))
            for task in project_tasks:
                route = resolve_effective_route(task, saved.get(task.id), catalog_hash)
                if route is not None:
                    resolved[task.id] = route
        return resolved

    async def explain(self, task: Task) -> tuple[dict | None, dict | None]:
        """Return route audit detail and one actionable routing reason."""

        options = await self._options(task.project_id)
        catalog_hash = options_hash(options)
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
            select_assignment_playbook(self.owner.playbook_manager, project)
            if not options:
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
