"""Durable per-project integration sweep scheduling."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from sqlalchemy import delete, insert, select, update

from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_repair_operations,
    playbook_artifacts,
    project_integration_leases,
    project_integration_schedules,
    projects,
    repos,
)
from src.integration.models import HierarchicalIntegrationPolicy
from src.integration.outbox import enqueue_integration_event
from src.integration.repair import RepairService
from src.git.manager import GitError, _validate_ref
from src.models import resolve_integration_mode_with_source
from src.playbooks.artifact_ref import ArtifactRef


ScheduleTrigger = Literal["periodic", "manual"]


class IntegrationScheduler:
    """Coalesce periodic and manual triggers into one durable sweep request."""

    DEFAULT_INTERVAL_SECONDS = 300

    def __init__(self, db: Any):
        self.db = db

    async def configure(
        self,
        *,
        project_id: str,
        now: float,
        enabled: bool,
        interval_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Persist scheduling controls while retaining any outstanding request."""
        if interval_seconds is not None and interval_seconds <= 0:
            raise ValueError("integration schedule interval must be positive")
        async with self.db.immediate() as conn:
            schedule = await self.db.lock_integration_schedule_on(
                conn,
                project_id=project_id,
                now=now,
                default_interval_seconds=self.DEFAULT_INTERVAL_SECONDS,
            )
            values: dict[str, Any] = {"enabled": enabled, "updated_at": now}
            if (
                interval_seconds is not None
                and interval_seconds != schedule["interval_seconds"]
            ):
                values["interval_seconds"] = interval_seconds
                values["next_due_at"] = now + interval_seconds
            return await self.db.update_integration_schedule_on(
                conn, project_id=project_id, values=values
            )

    async def mark_due(
        self, project_id: str, now: float, trigger: str
    ) -> dict[str, Any]:
        """Mark one sweep due, or return the durable request already in flight."""
        if trigger not in {"periodic", "manual"}:
            raise ValueError("integration schedule trigger must be periodic or manual")

        async with self.db.immediate() as conn:
            schedule = await self.db.lock_integration_schedule_on(
                conn,
                project_id=project_id,
                now=now,
                default_interval_seconds=self.DEFAULT_INTERVAL_SECONDS,
            )
            if trigger == "periodic" and not schedule["enabled"]:
                return self._result("disabled", project_id, schedule)

            periodic_due = trigger == "periodic" and now >= schedule["next_due_at"]
            if periodic_due:
                interval = int(schedule["interval_seconds"])
                elapsed_boundaries = int((now - schedule["next_due_at"]) // interval) + 1
                next_due_at = schedule["next_due_at"] + elapsed_boundaries * interval
                schedule = await self.db.update_integration_schedule_on(
                    conn,
                    project_id=project_id,
                    values={
                        "next_due_at": next_due_at,
                        "last_observed_window": next_due_at - interval,
                        "updated_at": now,
                    },
                )

            if schedule["outstanding_request_id"] is not None:
                active_batch = (
                    await conn.execute(
                        select(integration_batches.c.id).where(
                            integration_batches.c.project_id == project_id,
                            integration_batches.c.request_id
                            == schedule["outstanding_request_id"],
                            integration_batches.c.lifecycle.in_(
                                (
                                    "sealing",
                                    "sealed",
                                    "building",
                                    "testing",
                                    "repairing",
                                    "human_blocked",
                                    "promoting",
                                    "cleanup_pending",
                                    "promoted",
                                )
                            ),
                        )
                    )
                ).scalar_one_or_none()
                if (
                    active_batch is not None
                    and schedule["catchup_trigger"] is None
                    and (trigger == "manual" or periodic_due)
                ):
                    schedule = await self.db.update_integration_schedule_on(
                        conn,
                        project_id=project_id,
                        values={
                            "catchup_trigger": trigger,
                            "catchup_requested_at": now,
                            "catchup_after_sequence": int(schedule["request_sequence"]),
                            "updated_at": now,
                        },
                    )
                return self._result("coalesced", project_id, schedule)
            if trigger == "periodic" and not periodic_due:
                return self._result("not_due", project_id, schedule)

            sequence = int(schedule["request_sequence"]) + 1
            request_id = f"integration-sweep:{project_id}:{sequence}"
            schedule = await self.db.update_integration_schedule_on(
                conn,
                project_id=project_id,
                values={
                    "request_sequence": sequence,
                    "outstanding_request_id": request_id,
                    "outstanding_trigger": trigger,
                    "outstanding_requested_at": now,
                    "updated_at": now,
                },
            )
            await enqueue_integration_event(
                conn,
                event_id=request_id,
                dedup_key=request_id,
                project_id=project_id,
                event_type="integration.sweep_due",
                payload={"project_id": project_id, "operation_id": request_id},
                available_at=now,
            )
            return self._result("due", project_id, schedule)

    @staticmethod
    def _result(outcome: str, project_id: str, schedule: dict[str, Any]) -> dict[str, Any]:
        return {
            "outcome": outcome,
            "project_id": project_id,
            "request_id": schedule["outstanding_request_id"],
            "trigger": schedule["outstanding_trigger"],
            "requested_at": schedule["outstanding_requested_at"],
            "request_sequence": int(schedule["request_sequence"]),
            "next_due_at": float(schedule["next_due_at"]),
        }


class TrainService:
    """Seal one request's complete reviewed root frontier without Git I/O."""

    DEFAULT_PAGE_SIZE = 64
    LEASE_SECONDS = 300

    def __init__(
        self,
        db: Any,
        *,
        default_mode: str = "pull_request",
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        if page_size <= 0:
            raise ValueError("integration train page size must be positive")
        self.db = db
        self.default_mode = default_mode
        self.page_size = page_size

    async def seal(self, project_id: str, request_id: str, now: float) -> dict[str, Any]:
        if not project_id.strip() or not request_id.strip():
            raise ValueError("integration seal project and request are required")
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)

            request_batch = await self._batch_for_request(conn, project_id, request_id)
            if request_batch is not None and request_batch["lifecycle"] != "sealing":
                return await self._replay_result(conn, request_batch)

            lease = (
                await conn.execute(
                    select(project_integration_leases)
                    .where(project_integration_leases.c.project_id == project_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if lease is not None and float(lease["expires_at"]) > now:
                return self._result(
                    "busy", project_id, request_id, lease["batch_id"], None
                )

            schedule = (
                await conn.execute(
                    select(project_integration_schedules)
                    .where(project_integration_schedules.c.project_id == project_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if schedule is None or schedule["outstanding_request_id"] != request_id:
                raise ValueError("integration seal request is not outstanding")

            project = (
                await conn.execute(
                    select(projects).where(projects.c.id == project_id).with_for_update()
                )
            ).mappings().one_or_none()
            if (
                project is None
                or project["hierarchical_integration_mode"] != "train"
                or not project["integration_repository_id"]
            ):
                raise ValueError("project is not configured for integration trains")
            repository_id = project["integration_repository_id"]
            repository = (
                await conn.execute(
                    select(repos).where(
                        repos.c.id == repository_id,
                        repos.c.project_id == project_id,
                    )
                )
            ).mappings().one_or_none()
            if repository is None:
                raise ValueError("designated integration repository does not exist")

            policy = HierarchicalIntegrationPolicy.model_validate(
                project["hierarchical_integration_policy"]
            )
            boundary = policy.root
            artifact = (
                await conn.execute(
                    select(playbook_artifacts).where(
                        playbook_artifacts.c.artifact_sha256
                        == boundary.route.artifact.artifact_sha256
                    )
                )
            ).mappings().one_or_none()
            artifact_snapshot = boundary.route.artifact.model_dump(mode="json")
            if (
                artifact is None
                or ArtifactRef.from_row(artifact).as_dict() != artifact_snapshot
                or artifact["playbook_id"] != boundary.route.playbook_id
                or artifact["scope"] != boundary.route.scope
                or artifact["scope_identifier"] != boundary.route.scope_identifier
            ):
                raise ValueError("root route artifact identity is not stored and exact")

            if lease is not None:
                batch = (
                    await conn.execute(
                        select(integration_batches)
                        .where(integration_batches.c.id == lease["batch_id"])
                        .with_for_update()
                    )
                ).mappings().one_or_none()
                if (
                    batch is None
                    or batch["lifecycle"] != "sealing"
                    or batch["request_id"] != request_id
                ):
                    raise ValueError("expired integration lease has no resumable batch")
                request_batch = dict(batch)
            elif request_batch is not None:
                batch = request_batch
            else:
                active = (
                    await conn.execute(
                        select(integration_batches).where(
                            integration_batches.c.project_id == project_id,
                            integration_batches.c.lifecycle.in_(
                                (
                                    "sealing",
                                    "sealed",
                                    "building",
                                    "testing",
                                    "repairing",
                                    "human_blocked",
                                    "promoting",
                                    "cleanup_pending",
                                )
                            ),
                        )
                    )
                ).mappings().one_or_none()
                if active is not None:
                    return self._result(
                        "busy", project_id, request_id, active["id"], None
                    )

            members = await self._eligible_members(
                conn,
                project_id=project_id,
                repository_id=repository_id,
                project_mode=project["integration_mode"],
            )
            policy_snapshot = policy.model_dump(mode="json")
            for member in members:
                member["source_ref"] = self._source_ref(member["source_branch"])
                member["source_ref_retention"] = (
                    "retain"
                    if member["source_branch"] == member["default_branch"]
                    else policy.cleanup.successful_source_refs
                )
            if len({member["source_ref"] for member in members}) != len(members):
                raise ValueError("integration source refs must be unique within a batch")
            manifest_digest = self._manifest_digest(members)
            batch_id = (
                request_batch["id"]
                if request_batch is not None
                else self._batch_id(project_id, request_id)
            )

            if not members:
                if request_batch is not None:
                    raise ValueError("expired non-empty sealing batch lost its frontier")
                await conn.execute(
                    insert(integration_batches).values(
                        id=batch_id,
                        project_id=project_id,
                        repository_id=repository_id,
                        request_id=request_id,
                        trigger=schedule["outstanding_trigger"],
                        source_manifest_digest=manifest_digest,
                        base_sha=None,
                        lifecycle="empty",
                        current_revision=0,
                        integration_branch=None,
                        policy_snapshot=policy_snapshot,
                        artifact_snapshot=artifact_snapshot,
                        cleanup_state="complete",
                        created_at=now,
                        updated_at=now,
                    )
                )
                await self._consume_request(conn, project_id, request_id, now)
                return self._result("empty", project_id, request_id, batch_id, None)

            integration_branch = self._integration_branch(project_id, request_id)
            if request_batch is None:
                await conn.execute(
                    insert(integration_batches).values(
                        id=batch_id,
                        project_id=project_id,
                        repository_id=repository_id,
                        request_id=request_id,
                        trigger=schedule["outstanding_trigger"],
                        source_manifest_digest=manifest_digest,
                        base_sha=members[0]["source_base"],
                        lifecycle="sealing",
                        current_revision=0,
                        integration_branch=integration_branch,
                        policy_snapshot=policy_snapshot,
                        artifact_snapshot=artifact_snapshot,
                        cleanup_state="pending",
                        created_at=now,
                        updated_at=now,
                    )
                )
                await conn.execute(
                    insert(project_integration_leases).values(
                        project_id=project_id,
                        repository_id=repository_id,
                        batch_id=batch_id,
                        owner_id=f"sealer-{batch_id}",
                        fence_token=1,
                        heartbeat_at=now,
                        expires_at=now + self.LEASE_SECONDS,
                    )
                )
            else:
                await conn.execute(
                    delete(integration_batch_members).where(
                        integration_batch_members.c.batch_id == batch_id
                    )
                )
                await conn.execute(
                    update(integration_batches)
                    .where(integration_batches.c.id == batch_id)
                    .values(
                        repository_id=repository_id,
                        trigger=schedule["outstanding_trigger"],
                        source_manifest_digest=manifest_digest,
                        base_sha=members[0]["source_base"],
                        integration_branch=integration_branch,
                        policy_snapshot=policy_snapshot,
                        artifact_snapshot=artifact_snapshot,
                        updated_at=now,
                    )
                )
                if lease is None:
                    await conn.execute(
                        insert(project_integration_leases).values(
                            project_id=project_id,
                            repository_id=repository_id,
                            batch_id=batch_id,
                            owner_id=f"sealer-{batch_id}",
                            fence_token=1,
                            heartbeat_at=now,
                            expires_at=now + self.LEASE_SECONDS,
                        )
                    )
                else:
                    await conn.execute(
                        update(project_integration_leases)
                        .where(project_integration_leases.c.project_id == project_id)
                        .values(
                            repository_id=repository_id,
                            owner_id=f"sealer-{batch_id}",
                            fence_token=int(lease["fence_token"]) + 1,
                            heartbeat_at=now,
                            expires_at=now + self.LEASE_SECONDS,
                        )
                    )

            for ordinal, member in enumerate(members):
                review = member["review"]
                await conn.execute(
                    insert(integration_batch_members).values(
                        batch_id=batch_id,
                        ordinal=ordinal,
                        task_id=member["task_id"],
                        pr_url=member["pr_url"],
                        repository_id=repository_id,
                        source_base_sha=member["source_base"],
                        reviewed_head_sha=member["source_head"],
                        reviewed_tree_sha=review["reviewed_tree_sha"],
                        source_ref=member["source_ref"],
                        source_ref_retention=member["source_ref_retention"],
                        review_evidence_id=review["id"],
                        review_evidence=review,
                    )
                )

            operation = await RepairService(self.db).reserve_batch_operation_on(
                conn, batch_id, now=now
            )
            await enqueue_integration_event(
                conn,
                event_id=f"integration-sealed:{batch_id}",
                dedup_key=f"integration-sealed:{batch_id}",
                project_id=project_id,
                event_type="integration.sealed",
                payload={
                    "project_id": project_id,
                    "batch_id": batch_id,
                    "operation_id": operation["id"],
                },
                available_at=now,
            )
            await conn.execute(
                update(integration_batches)
                .where(
                    integration_batches.c.id == batch_id,
                    integration_batches.c.lifecycle == "sealing",
                )
                .values(lifecycle="sealed", updated_at=now)
            )
            return self._result(
                "sealed", project_id, request_id, batch_id, operation["id"]
            )

    async def _eligible_members(
        self,
        conn,
        *,
        project_id: str,
        repository_id: str,
        project_mode: str | None,
    ) -> list[dict[str, Any]]:
        members: list[dict[str, Any]] = []
        after: tuple[str, str] | None = None
        while True:
            page = await self.db.eligible_root_page_on(
                conn,
                project_id=project_id,
                repository_id=repository_id,
                after=after,
                limit=self.page_size,
            )
            if not page:
                break
            reviews = await self.db.latest_exact_reviews_on(conn, page)
            for candidate in page:
                mode, _source = resolve_integration_mode_with_source(
                    candidate["task_integration_mode"],
                    parent_task_mode=None,
                    project_mode=project_mode,
                    default_mode=self.default_mode,
                )
                key = (
                    candidate["task_id"],
                    candidate["repository_id"],
                    candidate["source_base"],
                    candidate["source_head"],
                    int(candidate["generation"]),
                )
                review = reviews.get(key)
                if (
                    mode != "pull_request"
                    or review is None
                    or review["verdict"] != "approved"
                    or not review["evidence"]
                    or review["review_kind"] != candidate["source_kind"]
                    or (
                        candidate["source_kind"] == "parent"
                        and review["evidence"].get("verification_id")
                        != candidate["current_verification_id"]
                    )
                ):
                    continue
                members.append({**candidate, "review": review})
            after = (page[-1]["task_id"], page[-1]["source_head"])
        return sorted(members, key=lambda row: (row["task_id"], row["source_head"]))

    async def _batch_for_request(self, conn, project_id: str, request_id: str):
        row = (
            await conn.execute(
                select(integration_batches)
                .where(
                    integration_batches.c.project_id == project_id,
                    integration_batches.c.request_id == request_id,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def _replay_result(self, conn, batch: dict[str, Any]) -> dict[str, Any]:
        if batch["lifecycle"] == "empty":
            return self._result(
                "empty", batch["project_id"], batch["request_id"], batch["id"], None
            )
        operation_id = (
            await conn.execute(
                select(integration_repair_operations.c.id).where(
                    integration_repair_operations.c.batch_id == batch["id"]
                )
            )
        ).scalar_one()
        return self._result(
            "sealed",
            batch["project_id"],
            batch["request_id"],
            batch["id"],
            operation_id,
        )

    @staticmethod
    async def _consume_request(conn, project_id: str, request_id: str, now: float) -> None:
        result = await conn.execute(
            update(project_integration_schedules)
            .where(
                project_integration_schedules.c.project_id == project_id,
                project_integration_schedules.c.outstanding_request_id == request_id,
            )
            .values(
                outstanding_request_id=None,
                outstanding_trigger=None,
                outstanding_requested_at=None,
                catchup_trigger=None,
                catchup_requested_at=None,
                catchup_after_sequence=None,
                last_completed_sweep_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise ValueError("integration seal request changed during sealing")

    @staticmethod
    def _batch_id(project_id: str, request_id: str) -> str:
        digest = hashlib.sha256(f"{project_id}\0{request_id}".encode()).hexdigest()
        return f"integration-batch-{digest[:32]}"

    @staticmethod
    def _integration_branch(project_id: str, request_id: str) -> str:
        project_digest = hashlib.sha256(project_id.encode()).hexdigest()[:32]
        request_digest = hashlib.sha256(request_id.encode()).hexdigest()[:32]
        return (
            "refs/heads/aq/integration/"
            f"p-{project_digest}/r-{request_digest}"
        )

    @staticmethod
    def _source_ref(branch: str) -> str:
        try:
            _validate_ref(branch, field="integration source branch")
        except GitError as exc:
            raise ValueError("integration source branch identity is invalid") from exc
        return f"refs/heads/{branch}"

    @staticmethod
    def _manifest_digest(members: list[dict[str, Any]]) -> str:
        manifest = [
            {
                "task_id": member["task_id"],
                "repository_id": member["repository_id"],
                "source_base_sha": member["source_base"],
                "reviewed_head_sha": member["source_head"],
                "reviewed_tree_sha": member["review"]["reviewed_tree_sha"],
                "review_evidence_id": member["review"]["id"],
                "source_ref": member["source_ref"],
                "source_ref_retention": member["source_ref_retention"],
            }
            for member in members
        ]
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _result(
        outcome: str,
        project_id: str,
        request_id: str,
        batch_id: str,
        operation_id: str | None,
    ) -> dict[str, Any]:
        return {
            "outcome": outcome,
            "project_id": project_id,
            "request_id": request_id,
            "batch_id": batch_id,
            "operation_id": operation_id,
        }
