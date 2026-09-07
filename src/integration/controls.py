"""Authenticated operational controls for hierarchical integration rollout.

The service owns the hierarchy-locked state transition.  Provider and retained-
repository checks run before that lock; all durable mode, schedule, suppression,
waiver, and audit writes then commit together behind the project's generation CAS.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import insert, select, update

from src.database.tables import (
    gates,
    integration_batches,
    integration_branch_owners,
    integration_cleanup_items,
    integration_history_waiver_consumptions,
    integration_history_waivers,
    integration_legacy_gate_applicability,
    integration_legacy_suppression,
    integration_promotion_intents,
    integration_repair_operations,
    playbook_activations,
    playbook_artifacts,
    project_integration_leases,
    project_integration_schedules,
    projects,
    repos,
    tasks,
)
from src.integration.models import HierarchicalIntegrationPolicy
from src.integration.preflight import daemon_functional_preflight
from src.integration.scheduler import IntegrationScheduler


ExternalPreflight = Callable[[str, str], Awaitable[tuple[str, ...]] | tuple[str, ...]]

_ACTIVE_BATCH_STATES = (
    "sealing",
    "sealed",
    "building",
    "testing",
    "repairing",
    "human_blocked",
    "promoting",
    "cleanup_pending",
)
_ACTIVE_OPERATION_STATES = ("active", "escalated", "human_required")
_DEFERRED_CERTIFICATION = ("protection", "scratch_probe", "transport_isolation")
_LEGACY_POLICY_OFF = {
    "merge_sweep_suppressed": False,
    "final_review_route_suppressed": False,
    "legacy_gate_creation_suppressed": False,
}
_LEGACY_POLICY_ON = {key: True for key in _LEGACY_POLICY_OFF}


def _blocker(code: str, detail: str, ref: str) -> dict[str, str]:
    return {"code": code, "detail": detail, "ref": ref}


def _sorted_blockers(blockers: list[dict[str, str]]) -> list[dict[str, str]]:
    unique = {(item["code"], item["ref"], item["detail"]): item for item in blockers}
    return [unique[key] for key in sorted(unique)]


def _blocker_digest(blockers: list[dict[str, str]]) -> str:
    encoded = json.dumps(blockers, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class IntegrationControlService:
    """Functional preflight plus atomic rollout and recovery controls."""

    def __init__(
        self,
        db: Any,
        *,
        scheduler: IntegrationScheduler | None = None,
        external_preflight: ExternalPreflight | None = None,
        cleanup_service: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.scheduler = scheduler or IntegrationScheduler(db)
        self.external_preflight = external_preflight
        self.cleanup_service = cleanup_service
        self.clock = clock

    def _recovery(self):
        from src.integration.recovery_controls import IntegrationRecoveryControls

        return IntegrationRecoveryControls(self.db, clock=self.clock)

    async def resume(self, operation_id: str) -> dict[str, Any]:
        return await self._recovery().resume(operation_id)

    async def abort(self, operation_id: str, *, reason: str) -> dict[str, Any]:
        return await self._recovery().abort(operation_id, reason=reason)

    async def retry_cleanup(self, batch_id: str) -> dict[str, Any]:
        return await self._recovery().retry_cleanup(batch_id)

    async def has_active_work(self, project_id: str) -> bool:
        async with self.db._engine.connect() as conn:
            return await self._has_active_work_on(conn, project_id)

    async def preflight(self, project_id: str) -> dict[str, Any]:
        """Read functional readiness without persisting observations."""
        async with self.db._engine.connect() as conn:
            projection = await self._functional_preflight_on(conn, project_id)
        projection["database_blocker_digest"] = projection["blocker_digest"]
        projection["database_blockers"] = list(projection["blockers"])
        if projection["project_id"] is None:
            return projection
        repository_id = projection["repository_id"]
        if repository_id and self.external_preflight is not None:
            external = self.external_preflight(project_id, repository_id)
            if inspect.isawaitable(external):
                external = await external
            for code in external:
                projection["blockers"].append(
                    _blocker(
                        str(code),
                        "functional integration dependency is unavailable",
                        repository_id,
                    )
                )
        projection["blockers"] = _sorted_blockers(projection["blockers"])
        projection["blocker_digest"] = _blocker_digest(projection["blockers"])
        projection["ready"] = not projection["blockers"]
        return projection

    async def status(self, project_id: str) -> dict[str, Any]:
        """Return status plus live functional wiring blockers."""
        from src.integration.status import IntegrationStatusService

        status = await IntegrationStatusService(self.db, clock=self.clock).status(project_id)
        if status is None:
            return {"outcome": "not_found", "project_id": project_id}
        observed = await self.preflight(project_id)
        database_keys = {
            (item["code"], item["ref"], item["detail"])
            for item in observed["database_blockers"]
        }
        external = [
            item
            for item in observed["blockers"]
            if (item["code"], item["ref"], item["detail"]) not in database_keys
        ]
        blockers = _sorted_blockers(list(status["blockers"]) + external)
        return {
            "outcome": "status",
            **status,
            "blockers": blockers,
            "blocker_digest": _blocker_digest(blockers),
            "ready": not blockers,
            "rollout_ready": not blockers,
        }

    async def _functional_preflight_on(self, conn: Any, project_id: str) -> dict[str, Any]:
        project = (
            await conn.execute(select(projects).where(projects.c.id == project_id))
        ).mappings().one_or_none()
        certification = {
            "status": "not_performed",
            "deferred": _DEFERRED_CERTIFICATION,
        }
        if project is None:
            blockers = [_blocker("project_not_found", "project does not exist", project_id)]
            return {
                "project_id": None,
                "repository_id": None,
                "generation": None,
                "effective_mode": None,
                "desired_mode": None,
                "draining": False,
                "ready": False,
                "blockers": blockers,
                "blocker_digest": _blocker_digest(blockers),
                "configuration_fingerprint": None,
                "certification": certification,
            }

        blockers: list[dict[str, str]] = []
        repository_id = project["integration_repository_id"]
        repository = None
        if not repository_id:
            blockers.append(
                _blocker(
                    "repository_not_designated",
                    "project has no designated integration repository",
                    project_id,
                )
            )
        else:
            repository = (
                await conn.execute(
                    select(repos).where(
                        repos.c.id == repository_id,
                        repos.c.project_id == project_id,
                    )
                )
            ).mappings().one_or_none()
            if repository is None or not self._github_origin(repository["url"]):
                blockers.append(
                    _blocker(
                        "repository_origin_invalid",
                        "designated repository must be an exact GitHub HTTPS clone origin",
                        repository_id,
                    )
                )
            elif not repository["default_branch"]:
                blockers.append(
                    _blocker(
                        "repository_default_branch_missing",
                        "designated repository has no default branch",
                        repository_id,
                    )
                )

        policy = None
        try:
            policy = HierarchicalIntegrationPolicy.model_validate(
                project["hierarchical_integration_policy"]
            )
        except (ValidationError, TypeError):
            blockers.append(
                _blocker(
                    "policy_invalid",
                    "hierarchical integration policy is absent or invalid",
                    project_id,
                )
            )
        if project["integration_mode"] != "pull_request":
            blockers.append(
                _blocker(
                    "review_policy_invalid",
                    "hierarchical integration requires pull_request delivery review",
                    project_id,
                )
            )
        if policy is not None:
            for boundary_name, boundary in (("parent", policy.parent), ("root", policy.root)):
                route = boundary.route
                ref = f"{boundary_name}:{route.playbook_id}"
                if route.scope_identifier != project_id or route.scope != "project":
                    blockers.append(
                        _blocker(
                            "route_scope_mismatch",
                            "integration route must be project-scoped to the target project",
                            ref,
                        )
                    )
                    continue
                artifact = (
                    await conn.execute(
                        select(playbook_artifacts).where(
                            playbook_artifacts.c.artifact_sha256
                            == route.artifact.artifact_sha256
                        )
                    )
                ).mappings().one_or_none()
                expected = route.artifact.model_dump(mode="json")
                if artifact is None or any(artifact[key] != value for key, value in expected.items()):
                    blockers.append(
                        _blocker(
                            "route_artifact_missing",
                            "integration route artifact identity is not stored and exact",
                            ref,
                        )
                    )
                    continue
                activation_stmt = select(playbook_activations).where(
                    playbook_activations.c.playbook_id == route.playbook_id,
                    playbook_activations.c.scope == route.scope,
                    playbook_activations.c.scope_identifier == route.scope_identifier,
                    playbook_activations.c.active_artifact_sha256
                    == route.artifact.artifact_sha256,
                    playbook_activations.c.enabled.is_(True),
                    playbook_activations.c.health == "ready",
                )
                if route.activation_id:
                    activation_stmt = activation_stmt.where(
                        playbook_activations.c.activation_id == route.activation_id
                    )
                activation = (await conn.execute(activation_stmt)).mappings().one_or_none()
                if activation is None:
                    blockers.append(
                        _blocker(
                            "route_not_ready",
                            "integration route has no matching ready activation",
                            ref,
                        )
                    )
                required_profiles = [
                    boundary.primary_profile_id,
                    boundary.repair.debug_profile_id,
                ]
                required_classes = [
                    boundary.primary_intelligence_class,
                    boundary.repair.debug_intelligence_class,
                ]
                if policy.branchless_parent == "verifier":
                    required_profiles.append(boundary.verifier_profile_id)
                    required_classes.append(boundary.verifier_intelligence_class)
                if any(value is None for value in required_profiles):
                    blockers.append(
                        _blocker(
                            "profile_route_missing",
                            "primary, debug, and required verifier profiles must be explicit",
                            boundary_name,
                        )
                    )
                if any(value is None for value in required_classes):
                    blockers.append(
                        _blocker(
                            "intelligence_route_missing",
                            "primary, debug, and required verifier classes must be explicit",
                            boundary_name,
                        )
                    )

        gate_rows = (
            await conn.execute(
                select(gates.c.id).where(
                    gates.c.project_id == project_id,
                    gates.c.gate_type == "pr-merged",
                    gates.c.status == "open",
                    ~select(integration_legacy_gate_applicability.c.gate_id)
                    .where(
                        integration_legacy_gate_applicability.c.project_id
                        == project_id,
                        integration_legacy_gate_applicability.c.gate_id == gates.c.id,
                        integration_legacy_gate_applicability.c.applicable.is_(False),
                    )
                    .exists(),
                )
            )
        ).scalars().all()
        for gate_id in gate_rows:
            blockers.append(
                _blocker(
                    "legacy_pr_merge_gate",
                    "open legacy merge gate requires an exact history waiver",
                    str(gate_id),
                )
            )
        blockers = _sorted_blockers(blockers)
        identity = {
            "project_id": project_id,
            "generation": int(project["hierarchical_integration_generation"]),
            "repository_id": repository_id,
            "repository": dict(repository) if repository is not None else None,
            "policy": policy.model_dump(mode="json") if policy is not None else None,
            "activations": [
                (item["code"], item["ref"])
                for item in blockers
                if item["code"].startswith("route_")
            ],
        }
        fingerprint = "sha256:" + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return {
            "project_id": project_id,
            "repository_id": repository_id,
            "generation": int(project["hierarchical_integration_generation"]),
            "effective_mode": project["hierarchical_integration_mode"],
            "desired_mode": project["hierarchical_integration_desired_mode"],
            "draining": bool(project["hierarchical_integration_draining"]),
            "ready": not blockers,
            "blockers": blockers,
            "blocker_digest": _blocker_digest(blockers),
            "configuration_fingerprint": fingerprint,
            "certification": certification,
        }

    async def enable(
        self,
        project_id: str,
        *,
        mode: str,
        expected_generation: int,
        reason: str,
        operator_id: str,
        waiver_id: str | None = None,
        interval_seconds: int | None = None,
    ) -> dict[str, Any]:
        if mode not in {"disabled", "observe", "hierarchy", "train"}:
            raise ValueError("integration mode must be disabled, observe, hierarchy, or train")
        if expected_generation < 0 or not reason.strip() or not operator_id.strip():
            raise ValueError("expected generation, reason, and operator are required")
        if interval_seconds is not None:
            if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int):
                raise ValueError("integration schedule interval must be a positive integer")
            if interval_seconds <= 0:
                raise ValueError("integration schedule interval must be positive")
            if mode != "train":
                raise ValueError("integration schedule interval is only valid with train mode")

        observed = await self.preflight(project_id)
        if observed["project_id"] is None:
            return {"outcome": "not_found", **observed}
        historical_only = all(
            blocker["code"] == "legacy_pr_merge_gate" for blocker in observed["blockers"]
        )
        if mode in {"hierarchy", "train"} and observed["blockers"] and not (
            waiver_id and historical_only
        ):
            return {"outcome": "blocked", **observed}

        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            locked = await self._functional_preflight_on(conn, project_id)
            if locked["project_id"] is None:
                return {"outcome": "not_found", **locked}
            if (
                locked["generation"] != expected_generation
                or locked["configuration_fingerprint"]
                != observed["configuration_fingerprint"]
                or locked["blocker_digest"] != observed["database_blocker_digest"]
            ):
                return {
                    "outcome": "stale",
                    "project_id": project_id,
                    "generation": locked["generation"],
                    "blockers": locked["blockers"],
                    "blocker_digest": locked["blocker_digest"],
                }
            current = (
                await conn.execute(
                    select(projects).where(projects.c.id == project_id).with_for_update()
                )
            ).mappings().one()
            if interval_seconds is not None and current[
                "hierarchical_integration_draining"
            ]:
                blockers = _sorted_blockers(
                    list(locked["blockers"])
                    + [
                        _blocker(
                            "integration_drain_active",
                            "integration cadence cannot change while the project is draining",
                            project_id,
                        )
                    ]
                )
                return {
                    "outcome": "blocked",
                    "project_id": project_id,
                    "effective_mode": current["hierarchical_integration_mode"],
                    "desired_mode": current["hierarchical_integration_desired_mode"],
                    "draining": True,
                    "generation": int(current["hierarchical_integration_generation"]),
                    "blockers": blockers,
                    "blocker_digest": _blocker_digest(blockers),
                    "certification": observed["certification"],
                }
            active = await self._has_active_work_on(conn, project_id)
            if (
                mode == "observe"
                and current["hierarchical_integration_mode"] in {"hierarchy", "train"}
                and active
            ):
                blockers = _sorted_blockers(
                    list(observed["blockers"])
                    + [
                        _blocker(
                            "active_integration_work",
                            "managed integration work must drain before observe mode",
                            project_id,
                        )
                    ]
                )
                return {
                    "outcome": "blocked",
                    "project_id": project_id,
                    "effective_mode": current["hierarchical_integration_mode"],
                    "desired_mode": current["hierarchical_integration_desired_mode"],
                    "draining": bool(current["hierarchical_integration_draining"]),
                    "generation": int(current["hierarchical_integration_generation"]),
                    "blockers": blockers,
                    "blocker_digest": _blocker_digest(blockers),
                    "certification": observed["certification"],
                }
            if waiver_id is not None:
                waiver = (
                    await conn.execute(
                        select(integration_history_waivers.c.id)
                        .outerjoin(
                            integration_history_waiver_consumptions,
                            integration_history_waiver_consumptions.c.waiver_id
                            == integration_history_waivers.c.id,
                        )
                        .where(
                            integration_history_waivers.c.id == waiver_id,
                            integration_history_waivers.c.project_id == project_id,
                            integration_history_waivers.c.blocker_digest
                            == observed["blocker_digest"],
                            integration_history_waiver_consumptions.c.waiver_id.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if waiver is None:
                    raise ValueError("history waiver is stale or already consumed")
            new_effective = mode
            new_desired = mode
            draining = False
            outcome = "enabled"
            if mode == "disabled" and active:
                new_effective = current["hierarchical_integration_mode"]
                new_desired = "disabled"
                draining = True
                outcome = "draining"
            elif mode == "disabled":
                outcome = "disabled"

            old_policy = await self._legacy_policy_on(conn, project_id)
            new_policy = (
                dict(_LEGACY_POLICY_ON)
                if new_effective in {"hierarchy", "train"}
                else dict(_LEGACY_POLICY_OFF)
            )
            changed = await self.db.cas_project_integration_control_on(
                conn,
                project_id=project_id,
                expected_generation=expected_generation,
                effective_mode=new_effective,
                desired_mode=new_desired,
                draining=draining,
            )
            if not changed:
                return {
                    "outcome": "stale",
                    "project_id": project_id,
                    "generation": int(current["hierarchical_integration_generation"]),
                }
            generation = expected_generation + 1
            transition_id = f"integration-transition-{uuid4().hex}"
            await self.db.append_integration_rollout_transition_on(
                conn,
                transition_id=transition_id,
                project_id=project_id,
                generation=generation,
                old_effective_mode=current["hierarchical_integration_mode"],
                new_effective_mode=new_effective,
                old_desired_mode=current["hierarchical_integration_desired_mode"],
                new_desired_mode=new_desired,
                draining=draining,
                operator_id=operator_id,
                reason=reason,
                blocker_digest=observed["blocker_digest"],
                old_legacy_policy=old_policy,
                new_legacy_policy=new_policy,
                waiver_id=waiver_id,
                now=now,
            )
            if waiver_id is not None:
                consumed = await self.db.consume_integration_history_waiver_on(
                    conn,
                    waiver_id=waiver_id,
                    transition_id=transition_id,
                    project_id=project_id,
                    blocker_digest=observed["blocker_digest"],
                    consumed_by=operator_id,
                    now=now,
                )
                if not consumed:
                    raise ValueError("history waiver is stale or already consumed")
                for blocker in observed["blockers"]:
                    if blocker["code"] == "legacy_pr_merge_gate":
                        await self.db.append_integration_legacy_gate_applicability_on(
                            conn,
                            project_id=project_id,
                            gate_id=blocker["ref"],
                            waiver_id=waiver_id,
                            transition_id=transition_id,
                            blocker_digest=observed["blocker_digest"],
                            applicable=False,
                            now=now,
                        )
            await self.db.set_integration_legacy_suppression_on(
                conn,
                project_id=project_id,
                generation=generation,
                policy_snapshot=new_policy,
                now=now,
                **new_policy,
            )
            await self._configure_schedule_on(
                conn,
                project_id=project_id,
                enabled=mode == "train" and not draining,
                create=mode == "train" and not draining,
                now=now,
                interval_seconds=interval_seconds,
            )
        return {
            "outcome": outcome,
            "project_id": project_id,
            "effective_mode": new_effective,
            "desired_mode": new_desired,
            "draining": draining,
            "generation": generation,
            "blockers": observed["blockers"],
            "blocker_digest": observed["blocker_digest"],
            "certification": observed["certification"],
        }

    async def configure(
        self,
        project_id: str,
        *,
        updates: dict[str, Any],
        expected_generation: int,
        reason: str,
        operator_id: str,
    ) -> dict[str, Any]:
        """Configure rollout inputs only while fully disabled and drained."""
        allowed = {"integration_repository_id", "hierarchical_integration_policy"}
        if not updates or set(updates) - allowed:
            raise ValueError("only repository and hierarchical policy are configurable here")
        if expected_generation < 0:
            raise ValueError("expected integration generation must be non-negative")
        if "hierarchical_integration_policy" in updates and updates[
            "hierarchical_integration_policy"
        ] is not None:
            policy = HierarchicalIntegrationPolicy.model_validate(
                updates["hierarchical_integration_policy"]
            )
            for boundary in (policy.parent, policy.root):
                if boundary.route.scope != "project" or boundary.route.scope_identifier != project_id:
                    raise ValueError("integration routes must be scoped to the configured project")
            updates = dict(updates)
            updates["hierarchical_integration_policy"] = policy.model_dump(mode="json")

        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            project = (
                await conn.execute(
                    select(projects).where(projects.c.id == project_id).with_for_update()
                )
            ).mappings().one_or_none()
            if project is None:
                return {"outcome": "not_found", "project_id": project_id}
            generation = int(project["hierarchical_integration_generation"])
            if generation != expected_generation:
                return {"outcome": "stale", "project_id": project_id, "generation": generation}
            if (
                project["hierarchical_integration_mode"] != "disabled"
                or project["hierarchical_integration_desired_mode"] != "disabled"
                or project["hierarchical_integration_draining"]
                or await self._has_active_work_on(conn, project_id)
            ):
                return {
                    "outcome": "busy",
                    "project_id": project_id,
                    "generation": generation,
                    "error": "integration configuration requires disabled and drained state",
                }
            repository_id = updates.get(
                "integration_repository_id", project["integration_repository_id"]
            )
            if repository_id is not None:
                repository = (
                    await conn.execute(
                        select(repos.c.id).where(
                            repos.c.id == repository_id,
                            repos.c.project_id == project_id,
                        )
                    )
                ).scalar_one_or_none()
                if repository is None:
                    return {
                        "outcome": "blocked",
                        "project_id": project_id,
                        "generation": generation,
                        "error": "designated repository does not belong to the project",
                    }
            observed = await self._functional_preflight_on(conn, project_id)
            old_policy = await self._legacy_policy_on(conn, project_id)
            if not await self.db.cas_project_integration_control_on(
                conn,
                project_id=project_id,
                expected_generation=generation,
                effective_mode="disabled",
                desired_mode="disabled",
                draining=False,
            ):
                return {"outcome": "stale", "project_id": project_id, "generation": generation}
            next_generation = generation + 1
            changed = await conn.execute(
                update(projects)
                .where(
                    projects.c.id == project_id,
                    projects.c.hierarchical_integration_generation == next_generation,
                    projects.c.hierarchical_integration_mode == "disabled",
                    projects.c.hierarchical_integration_desired_mode == "disabled",
                    projects.c.hierarchical_integration_draining.is_(False),
                )
                .values(**updates)
            )
            if changed.rowcount != 1:
                raise RuntimeError("integration configuration lost its generation fence")
            transition_id = f"integration-transition-{uuid4().hex}"
            await self.db.append_integration_rollout_transition_on(
                conn,
                transition_id=transition_id,
                project_id=project_id,
                generation=next_generation,
                old_effective_mode="disabled",
                new_effective_mode="disabled",
                old_desired_mode="disabled",
                new_desired_mode="disabled",
                draining=False,
                operator_id=operator_id,
                reason=reason,
                blocker_digest=observed["blocker_digest"],
                old_legacy_policy=old_policy,
                new_legacy_policy=old_policy,
                waiver_id=None,
                now=now,
            )
            await self.db.set_integration_legacy_suppression_on(
                conn,
                project_id=project_id,
                generation=next_generation,
                policy_snapshot=old_policy,
                now=now,
                **old_policy,
            )
        return {
            "outcome": "configured",
            "project_id": project_id,
            "generation": next_generation,
            "fields": sorted(updates),
        }

    async def flush(self, project_id: str) -> dict[str, Any]:
        project = await self.db.get_project(project_id)
        if project is None:
            return {"outcome": "not_found", "project_id": project_id}
        if project.hierarchical_integration_draining:
            return {"outcome": "draining", "project_id": project_id}
        if project.hierarchical_integration_mode == "disabled":
            return {"outcome": "disabled", "project_id": project_id}
        if project.hierarchical_integration_mode in {"observe", "hierarchy"}:
            return {"outcome": "eligibility", **await self.preflight(project_id)}
        return await self.scheduler.mark_due(project_id, self.clock(), "manual")

    async def waive_history(
        self,
        project_id: str,
        *,
        reason: str,
        blocker_digest: str,
        operator_id: str,
    ) -> dict[str, Any]:
        observed = await self.preflight(project_id)
        if observed["project_id"] is None:
            return {"outcome": "not_found", **observed}
        if blocker_digest != observed["blocker_digest"]:
            return {"outcome": "stale", **observed}
        if not observed["blockers"] or any(
            blocker["code"] != "legacy_pr_merge_gate" for blocker in observed["blockers"]
        ):
            return {"outcome": "not_waivable", **observed}
        waiver_id = f"integration-waiver-{uuid4().hex}"
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            locked = await self._functional_preflight_on(conn, project_id)
            if locked["blocker_digest"] != blocker_digest:
                return {"outcome": "stale", **locked}
            await self.db.append_integration_history_waiver_on(
                conn,
                waiver_id=waiver_id,
                project_id=project_id,
                operator_id=operator_id,
                reason=reason,
                blocker_digest=blocker_digest,
                now=self.clock(),
            )
        return {
            "outcome": "waived",
            "project_id": project_id,
            "waiver_id": waiver_id,
            "blocker_digest": blocker_digest,
        }

    async def reconcile_drains(self, now: float | None = None) -> tuple[str, ...]:
        """Finish requested drains after all frozen work reaches a safe terminal state."""
        observed_at = self.clock() if now is None else now
        async with self.db._engine.connect() as conn:
            project_ids = (
                await conn.execute(
                    select(projects.c.id)
                    .where(projects.c.hierarchical_integration_draining.is_(True))
                    .order_by(projects.c.id)
                    .limit(100)
                )
            ).scalars().all()
        completed = []
        for project_id in project_ids:
            if await self._complete_drain(str(project_id), observed_at):
                completed.append(str(project_id))
        return tuple(completed)

    async def _complete_drain(self, project_id: str, now: float) -> bool:
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            project = (
                await conn.execute(
                    select(projects).where(projects.c.id == project_id).with_for_update()
                )
            ).mappings().one_or_none()
            if (
                project is None
                or not project["hierarchical_integration_draining"]
                or project["hierarchical_integration_desired_mode"] != "disabled"
                or await self._has_active_work_on(conn, project_id)
            ):
                return False
            generation = int(project["hierarchical_integration_generation"])
            if not await self.db.cas_project_integration_control_on(
                conn,
                project_id=project_id,
                expected_generation=generation,
                effective_mode="disabled",
                desired_mode="disabled",
                draining=False,
            ):
                return False
            next_generation = generation + 1
            old_policy = await self._legacy_policy_on(conn, project_id)
            await self.db.append_integration_rollout_transition_on(
                conn,
                transition_id=f"integration-transition-{uuid4().hex}",
                project_id=project_id,
                generation=next_generation,
                old_effective_mode=project["hierarchical_integration_mode"],
                new_effective_mode="disabled",
                old_desired_mode="disabled",
                new_desired_mode="disabled",
                draining=False,
                operator_id="service:integration-drain",
                reason="all frozen integration work reached terminal state",
                blocker_digest=_blocker_digest([]),
                old_legacy_policy=old_policy,
                new_legacy_policy=dict(_LEGACY_POLICY_OFF),
                waiver_id=None,
                now=now,
            )
            await self.db.set_integration_legacy_suppression_on(
                conn,
                project_id=project_id,
                generation=next_generation,
                policy_snapshot=dict(_LEGACY_POLICY_OFF),
                now=now,
                **_LEGACY_POLICY_OFF,
            )
            await self._configure_schedule_on(
                conn, project_id=project_id, enabled=False, create=False, now=now
            )
            return True

    async def _configure_schedule_on(
        self,
        conn: Any,
        *,
        project_id: str,
        enabled: bool,
        create: bool,
        now: float,
        interval_seconds: int | None = None,
    ) -> None:
        schedule = (
            await conn.execute(
                select(project_integration_schedules)
                .where(project_integration_schedules.c.project_id == project_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        if schedule is None and create:
            interval = interval_seconds or IntegrationScheduler.DEFAULT_INTERVAL_SECONDS
            await conn.execute(
                insert(project_integration_schedules).values(
                    project_id=project_id,
                    enabled=enabled,
                    interval_seconds=interval,
                    next_due_at=now + interval,
                    updated_at=now,
                )
            )
        elif schedule is not None:
            values: dict[str, Any] = {"enabled": enabled, "updated_at": now}
            if (
                interval_seconds is not None
                and interval_seconds != schedule["interval_seconds"]
            ):
                values.update(
                    interval_seconds=interval_seconds,
                    next_due_at=now + interval_seconds,
                )
            if not enabled:
                values.update(
                    catchup_trigger=None,
                    catchup_requested_at=None,
                    catchup_after_sequence=None,
                )
            await conn.execute(
                update(project_integration_schedules)
                .where(project_integration_schedules.c.project_id == project_id)
                .values(**values)
            )

    async def _legacy_policy_on(self, conn: Any, project_id: str) -> dict[str, bool]:
        row = (
            await conn.execute(
                select(integration_legacy_suppression).where(
                    integration_legacy_suppression.c.project_id == project_id
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return dict(_LEGACY_POLICY_OFF)
        return {key: bool(row[key]) for key in _LEGACY_POLICY_OFF}

    async def _has_active_work_on(self, conn: Any, project_id: str) -> bool:
        checks = (
            select(integration_batches.c.id).where(
                integration_batches.c.project_id == project_id,
                (
                    integration_batches.c.lifecycle.in_(_ACTIVE_BATCH_STATES)
                    | (
                        (integration_batches.c.lifecycle == "promoted")
                        & (integration_batches.c.cleanup_state != "complete")
                    )
                ),
            ),
            select(integration_repair_operations.c.id)
            .select_from(
                integration_repair_operations
                .outerjoin(
                    integration_batches,
                    integration_batches.c.id == integration_repair_operations.c.batch_id,
                )
                .outerjoin(
                    tasks,
                    tasks.c.id == integration_repair_operations.c.parent_task_id,
                )
            )
            .where(
                integration_repair_operations.c.state.in_(_ACTIVE_OPERATION_STATES),
                (integration_batches.c.project_id == project_id)
                | (tasks.c.project_id == project_id),
            ),
            select(integration_branch_owners.c.id)
            .select_from(
                integration_branch_owners.join(
                    repos, repos.c.id == integration_branch_owners.c.repository_id
                )
            )
            .where(
                repos.c.project_id == project_id,
                integration_branch_owners.c.handoff_state != "released",
            ),
            select(project_integration_leases.c.project_id).where(
                project_integration_leases.c.project_id == project_id
            ),
            select(integration_promotion_intents.c.id).where(
                integration_promotion_intents.c.project_id == project_id,
                integration_promotion_intents.c.state.not_in(
                    ("committed", "conflict", "superseded")
                ),
            ),
            select(integration_cleanup_items.c.batch_id).where(
                integration_cleanup_items.c.project_id == project_id,
                integration_cleanup_items.c.state.in_(("pending", "retryable")),
            ),
        )
        for statement in checks:
            if (await conn.execute(statement.limit(1))).scalar_one_or_none() is not None:
                return True
        return False

    @staticmethod
    def _github_origin(url: str) -> bool:
        parsed = urlparse(str(url or ""))
        return bool(
            parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
            and parsed.path.count("/") == 2
            and parsed.path.endswith(".git")
        )


__all__ = ["IntegrationControlService", "daemon_functional_preflight"]
