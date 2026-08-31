"""Authenticated, atomic completion of mandatory routing triage."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
import logging

from sqlalchemy import select

from src.agents.execution_types import (
    ExecutionCatalog,
    execution_catalog_config_lock,
    resolve_execution_catalog,
)
from src.database.tables import sessions
from src.models import TaskStatus
from src.triage.models import RoutingChoice, TriagePrincipal


_ACTIVE_TASK_STATES = {
    TaskStatus.ASSIGNED.value,
    TaskStatus.IN_PROGRESS.value,
    TaskStatus.WAITING_INPUT.value,
    TaskStatus.AWAITING_PLAN_APPROVAL.value,
}
_LIVE_SESSION_STATES = {"starting", "running", "draining"}
logger = logging.getLogger(__name__)


def _error(code: str, message: str) -> dict:
    return {"success": False, "code": code, "error": message}


def _hash_json(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _catalog_generation(catalog: ExecutionCatalog) -> str:
    return _hash_json(
        {
            "types": {key: asdict(value) for key, value in catalog.types.items()},
            "members": catalog.members,
            "diagnostics": catalog.diagnostics,
        }
    )


class TriageService:
    """The sole service allowed to turn an open routing gate into a decision."""

    def __init__(
        self,
        db,
        *,
        builder,
        harness_registry,
        config_lock: asyncio.Lock | None = None,
        event_callback=None,
    ):
        self.db = db
        self.builder = builder
        self.harness_registry = harness_registry
        # Lock order is always config snapshot -> database transaction. Editors
        # that mutate in-memory class configuration share this same lock.
        self.config_lock = config_lock or execution_catalog_config_lock()
        self.event_callback = event_callback

    async def authenticate(self, scope):
        """Resolve a live principal only from the server-derived request scope."""
        kind = scope.get("kind") if isinstance(scope, dict) else getattr(scope, "kind", None)
        if kind != "session":
            return _error("unauthorized", "A live triage playbook session is required")
        session_id = (
            scope.get("session_id") if isinstance(scope, dict) else getattr(scope, "session_id", None)
        )
        project_id = (
            scope.get("project_id") if isinstance(scope, dict) else getattr(scope, "project_id", None)
        )
        if not isinstance(session_id, str) or not session_id or not isinstance(project_id, str):
            return _error("unauthorized", "The request has no triage session ownership")
        session = await self.db.get_session(session_id)
        if session is None or not session.playbook_run_id:
            return _error("unauthorized", "The session is not owned by a triage run")
        run = await self.db.get_playbook_run(session.playbook_run_id)
        principal = TriagePrincipal(
            project_id=project_id,
            run_id=session.playbook_run_id,
            session_id=session_id,
            instance_token=session.instance_token,
        )
        if not self._authorized(principal, run, session):
            return _error("unauthorized", "Triage run/session ownership is not live")
        return principal

    @staticmethod
    def _authorized(principal: TriagePrincipal, run, session) -> bool:
        if run is None or session is None:
            return False
        def field(value, key):
            getter = getattr(value, "get", None)
            return getter(key) if getter is not None else getattr(value, key, None)

        return bool(
            field(run, "role") == "triage"
            and field(run, "status") in {"running", "paused"}
            and field(run, "project_id") == principal.project_id
            and field(run, "run_id") == principal.run_id
            and field(run, "owner_session_id") == principal.session_id
            and field(session, "id") == principal.session_id
            and field(session, "project_id") == principal.project_id
            and field(session, "playbook_run_id") == principal.run_id
            and field(session, "playbook_node_id")
            and field(session, "state") in {"starting", "running"}
            and field(session, "desired_state") == "running"
            and field(session, "instance_token") == principal.instance_token
        )

    async def options(self, principal: TriagePrincipal) -> dict:
        async with self.config_lock:
            async with self.db.immediate() as conn:
                context = await self._lock_context(conn, principal)
                if isinstance(context, dict) and context.get("success") is False:
                    return context
                catalog = await resolve_execution_catalog(
                    self.db,
                    principal.project_id,
                    builder=self.builder,
                    harness_registry=self.harness_registry,
                    conn=conn,
                )
        return {
            "success": True,
            "project_id": principal.project_id,
            "catalog_generation": _catalog_generation(catalog),
            "types": [
                {
                    "execution_type_key": key,
                    **asdict(identity),
                    "member_ids": list(catalog.members[key]),
                    "idle_count": catalog.idle_counts[key],
                }
                for key, identity in catalog.types.items()
            ],
            "diagnostics": list(catalog.diagnostics),
        }

    async def complete(self, principal: TriagePrincipal, choice: RoutingChoice) -> dict:
        invalid = self._validate_choice(choice)
        if invalid:
            return invalid
        if not isinstance(principal, TriagePrincipal):
            return _error("unauthorized", "A verified triage principal is required")

        gate_ids: list[str] = []
        flipped: set[str] = set()
        ready_ids: list[str] = []
        async with self.config_lock:
            async with self.db.immediate() as conn:
                context = await self._lock_context(conn, principal)
                if isinstance(context, dict) and context.get("success") is False:
                    return context
                run, _session = context
                task = await self.db.lock_routing_task_on(conn, choice.task_id)
                if task is None:
                    return _error("task_not_found", f"Task '{choice.task_id}' was not found")
                if task["project_id"] != principal.project_id:
                    return _error("wrong_project", "The task belongs to another project")

                # Retry lookup deliberately precedes the revision/gate checks.
                existing = await self.db.get_routing_decision_on(
                    conn, choice.task_id, choice.expected_revision
                )
                if existing is not None:
                    if existing["execution_type_key"] == choice.execution_type_key:
                        return self._result(existing, [])
                    return _error(
                        "decision_conflict",
                        "A different immutable routing decision already exists for this revision",
                    )

                if task["routing_revision"] != choice.expected_revision:
                    return _error("stale_revision", "The task routing revision has changed")
                if await self._has_active_execution(conn, task):
                    return _error(
                        "active_execution", "Stop active task execution before routing it"
                    )
                if not await self.db.has_open_routing_gate_on(conn, choice.task_id):
                    return _error("routing_gate_closed", "The task has no open routing gate")

                catalog = await resolve_execution_catalog(
                    self.db,
                    principal.project_id,
                    builder=self.builder,
                    harness_registry=self.harness_registry,
                    conn=conn,
                )
                identity = catalog.types.get(choice.execution_type_key)
                if identity is None or not catalog.members.get(choice.execution_type_key):
                    return _error(
                        "execution_type_unavailable",
                        "The selected execution type has no eligible configured worker",
                    )
                snapshot = asdict(identity)
                request = json.loads(task["routing_request"] or "{}")
                mismatch = self._constraint_mismatch(request, snapshot, choice.execution_type_key)
                if mismatch:
                    return _error("constraint_mismatch", mismatch)

                decision_id = await self.db.insert_routing_decision_on(
                    conn,
                    project_id=principal.project_id,
                    task_id=choice.task_id,
                    routing_revision=choice.expected_revision,
                    run=run,
                    execution_type_key=choice.execution_type_key,
                    execution_snapshot=snapshot,
                    reason=choice.reason.strip(),
                )
                await self.db.apply_routing_decision_on(
                    conn,
                    task_id=choice.task_id,
                    decision_id=decision_id,
                    execution_snapshot=snapshot,
                )
                gate_ids = await self.db.resolve_routing_gates_on(
                    conn,
                    task_id=choice.task_id,
                    resolution=f"routed to {choice.execution_type_key}",
                )
                flipped = await self.db.recompute_blocked({choice.task_id}, conn=conn)
                ready_ids = await self.db._note_frontier_entry(
                    conn, set(flipped), reason="unblocked"
                )
                result = {
                    "success": True,
                    "decision_id": decision_id,
                    "task_id": choice.task_id,
                    "routing_revision": choice.expected_revision,
                    "execution_type_key": choice.execution_type_key,
                    "profile_id": snapshot["profile_id"],
                    "intelligence_class": snapshot["intelligence_class"],
                    "resolved_gate_ids": gate_ids,
                }
        await self._after_commit(gate_ids, flipped, ready_ids, principal.project_id)
        return result

    async def defer(
        self,
        principal: TriagePrincipal,
        task_id: str,
        expected_revision: int,
        reason: str,
    ) -> dict:
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            return _error("invalid_request", "task_id, revision, and nonempty reason are required")
        async with self.config_lock:
            async with self.db.immediate() as conn:
                context = await self._lock_context(conn, principal)
                if isinstance(context, dict) and context.get("success") is False:
                    return context
                task = await self.db.lock_routing_task_on(conn, task_id)
                if task is None:
                    return _error("task_not_found", f"Task '{task_id}' was not found")
                if task["project_id"] != principal.project_id:
                    return _error("wrong_project", "The task belongs to another project")
                if task["routing_revision"] != expected_revision:
                    return _error("stale_revision", "The task routing revision has changed")
                if not await self.db.has_open_routing_gate_on(conn, task_id):
                    return _error("routing_gate_closed", "The task has no open routing gate")
                catalog = await resolve_execution_catalog(
                    self.db,
                    principal.project_id,
                    builder=self.builder,
                    harness_registry=self.harness_registry,
                    conn=conn,
                )
                row = await self.db.insert_or_get_routing_deferral_on(
                    conn,
                    project_id=principal.project_id,
                    task_id=task_id,
                    routing_revision=expected_revision,
                    playbook_run_id=principal.run_id,
                    catalog_generation=_catalog_generation(catalog),
                    policy_generation=_hash_json(json.loads(task["routing_request"] or "{}")),
                    reason=reason.strip(),
                )
        return {
            "success": True,
            "deferral_id": row["id"],
            "task_id": task_id,
            "routing_revision": expected_revision,
            "catalog_generation": row["catalog_generation"],
            "policy_generation": row["policy_generation"],
        }

    async def _lock_context(self, conn, principal: TriagePrincipal):
        if not isinstance(principal, TriagePrincipal):
            return _error("unauthorized", "A verified triage principal is required")
        run = await self.db.lock_triage_run_on(conn, principal.run_id)
        session = await self.db.lock_triage_session_on(conn, principal.session_id)
        if not self._authorized(principal, run, session):
            return _error("unauthorized", "Triage run/session ownership is not live")
        return run, session

    @staticmethod
    async def _has_active_execution(conn, task) -> bool:
        if task["status"] in _ACTIVE_TASK_STATES or task["assigned_agent_id"] is not None:
            return True
        return bool(
            await conn.scalar(
                select(sessions.c.id)
                .where(
                    sessions.c.task_id == task["id"],
                    sessions.c.state.in_(_LIVE_SESSION_STATES),
                )
                .limit(1)
            )
        )

    @staticmethod
    def _validate_choice(choice) -> dict | None:
        if not isinstance(choice, RoutingChoice):
            return _error("invalid_request", "A structured routing choice is required")
        if not isinstance(choice.task_id, str) or not choice.task_id.strip():
            return _error("invalid_request", "task_id must be a nonempty string")
        if not isinstance(choice.execution_type_key, str) or not choice.execution_type_key.strip():
            return _error("invalid_request", "execution_type_key must be a nonempty string")
        if (
            not isinstance(choice.expected_revision, int)
            or isinstance(choice.expected_revision, bool)
            or choice.expected_revision < 1
        ):
            return _error("invalid_request", "expected_revision must be a positive integer")
        if not isinstance(choice.reason, str) or not choice.reason.strip():
            return _error("invalid_request", "reason must be a nonempty string")
        return None

    @staticmethod
    def _constraint_mismatch(request: dict, snapshot: dict, type_key: str) -> str | None:
        constraints = request.get("execution", request) if isinstance(request, dict) else {}
        if not isinstance(constraints, dict):
            return "routing_request execution constraints are malformed"
        values = {**snapshot, "execution_type_key": type_key}
        for key in (
            "execution_type_key",
            "profile_id",
            "harness",
            "provider",
            "model",
            "intelligence_class",
            "reasoning_effort",
        ):
            required = constraints.get(key)
            if required is not None and required != values[key]:
                return f"Selected execution type does not satisfy requested {key}"
        return None

    @staticmethod
    def _result(existing: dict, gate_ids: list[str]) -> dict:
        return {
            "success": True,
            "decision_id": existing["id"],
            "task_id": existing["task_id"],
            "routing_revision": existing["routing_revision"],
            "execution_type_key": existing["execution_type_key"],
            "profile_id": existing["profile_id"],
            "intelligence_class": existing["intelligence_class"],
            "resolved_gate_ids": gate_ids,
        }

    async def _after_commit(self, gate_ids, flipped, ready_ids, project_id):
        try:
            await self.db.log_blocked_flips(flipped)
            await self.db._notify_ready([(task_id, "unblocked") for task_id in ready_ids])
        except Exception:
            logger.debug("triage post-commit task notification failed", exc_info=True)
        for gate_id in gate_ids:
            try:
                await self.db.log_event("gate.resolved", project_id=project_id, payload=gate_id)
                if self.event_callback is not None:
                    await self.event_callback(
                        "gate.resolved",
                        {
                            "gate_id": gate_id,
                            "project_id": project_id,
                            "gate_type": "routing",
                            "resolved_by": "task_route",
                            "resolution": "mandatory triage decision",
                            "unblocked_task_ids": sorted(flipped),
                        },
                    )
            except Exception:
                logger.debug("triage post-commit gate event failed", exc_info=True)
