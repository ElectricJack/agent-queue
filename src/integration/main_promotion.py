"""Durable root-to-main intent preparation and exact tested-SHA promotion."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    integration_root_intent_members,
    project_integration_leases,
    projects,
    task_delivery_receipts,
)
from src.integration.outbox import enqueue_integration_event
from src.git.manager import (
    APP_AUTH_PUSH_CLEANUP_MARGIN_SECONDS,
    APP_AUTH_PUSH_TIMEOUT_SECONDS,
    GitManager,
    is_valid_git_oid,
)


_IDENTITY_NAMESPACE = uuid.UUID("2cfd2eea-e0e5-4397-b1c4-2dd6c40d64dd")
_CLAIM_SECONDS = 135.0


class RootPromotionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal[
        "prepared",
        "promoted",
        "already_promoted",
        "base_moved",
        "ci_missing",
        "non_fast_forward",
        "wait",
        "reconciliation_blocked",
        "stale",
        "configuration_blocked",
    ]
    batch_id: str
    revision: int
    intent_id: str | None = None
    receipt_ids: tuple[str, ...] = ()
    head_sha: str | None = None


class RootPromotionInvariantError(RuntimeError):
    """Durable root promotion state is internally inconsistent."""


class RootAttestationSubject(BaseModel):
    """Exact server-derived root candidate identity requiring attestation."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    repository_numeric_id: StrictInt = Field(gt=0)
    repository_full_name: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    revision: StrictInt = Field(ge=0)
    candidate_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    required_check_version: str = Field(min_length=1)


class RootAttestationProof(RootAttestationSubject):
    """Authenticated publication/readback identity supplied by Task10."""

    check_run_id: StrictInt = Field(gt=0)
    external_id: str = Field(pattern=r"^aq-attestation-v1:[0-9a-f]{64}$")

    def subject(self) -> RootAttestationSubject:
        fields = RootAttestationSubject.model_fields
        return RootAttestationSubject.model_validate(
            {name: getattr(self, name) for name in fields}
        )


RepositoryResolver = Callable[[str], Awaitable[Any] | Any]
CrashHook = Callable[[str], Awaitable[None] | None]
AttestationResolver = Callable[
    [RootAttestationSubject], Awaitable[RootAttestationProof | None] | RootAttestationProof | None
]


class RootPromotionService:
    """Reserve all root receipts and the one fenced exact-main mutation."""

    def __init__(
        self,
        db: Any,
        *,
        data_dir: str | Path,
        git_manager: GitManager | None = None,
        repository_resolver: RepositoryResolver | None = None,
        app_client: Any | None = None,
        attestation_resolver: AttestationResolver | None = None,
        crash_hook: CrashHook | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.data_dir = Path(data_dir)
        self.git = git_manager or GitManager()
        self.repository_resolver = repository_resolver
        self.app_client = app_client
        self.attestation_resolver = attestation_resolver
        self.crash_hook = crash_hook
        self.clock = clock
        self._owned_nonces: dict[str, str] = {}

    async def prepare(self, batch_id: str, revision: int) -> RootPromotionResult:
        identity = self._identity(batch_id, revision)
        intent_id = identity["intent_id"]
        existing = await self._intent(intent_id)
        if existing is not None:
            return await self._existing_result(existing, batch_id, revision)

        project_id = await self._project_id(batch_id)
        if project_id is None:
            return RootPromotionResult(outcome="stale", batch_id=batch_id, revision=revision)
        snapshot = await self._snapshot(project_id, batch_id, revision)
        if snapshot["batch"]["lifecycle"] == "empty":
            return RootPromotionResult(
                outcome="already_promoted", batch_id=batch_id, revision=revision
            )
        failure = self._validate_snapshot(snapshot, revision)
        if failure is not None:
            return RootPromotionResult(outcome=failure, batch_id=batch_id, revision=revision)
        attestation_subject = self._attestation_subject(snapshot)
        attestation = await self._resolve_attestation(attestation_subject)
        if attestation is None:
            return RootPromotionResult(
                outcome="configuration_blocked", batch_id=batch_id, revision=revision
            )

        repository = await self._repository(snapshot["batch"]["repository_id"])
        recovery_ref = f"refs/aq/root-promotions/{intent_id}"
        await self._pin_recovery(repository, recovery_ref, snapshot["revision"]["head_sha"])
        await self._crash("after_recovery_pin")

        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            locked = await self._snapshot_on(conn, project_id, batch_id, revision)
            failure = self._validate_snapshot(locked, revision)
            if failure is not None or not self._proof_matches_state(attestation, locked):
                return RootPromotionResult(
                    outcome=failure or "configuration_blocked",
                    batch_id=batch_id,
                    revision=revision,
                )
            existing = await self._intent_on(conn, intent_id)
            if existing is not None:
                return await self._existing_result_on(conn, existing, batch_id, revision)
            unresolved = (
                await conn.execute(
                    select(integration_promotion_intents.c.id).where(
                        integration_promotion_intents.c.repository_id
                        == locked["batch"]["repository_id"],
                        integration_promotion_intents.c.target_branch
                        == f"refs/heads/{repository.default_branch}",
                        integration_promotion_intents.c.state.not_in(
                            ("committed", "conflict", "superseded")
                        ),
                    )
                )
            ).scalar_one_or_none()
            if unresolved is not None:
                return RootPromotionResult(
                    outcome="reconciliation_blocked",
                    batch_id=batch_id,
                    revision=revision,
                )
            receipt_ids = tuple(
                self._receipt_id(batch_id, revision, int(member["ordinal"]))
                for member in locked["members"]
            )
            if not receipt_ids:
                raise RootPromotionInvariantError("nonempty root batch has no members")
            intent_values = {
                "id": intent_id,
                "domain_key": identity["domain_key"],
                "operation_key": locked["operation"]["id"],
                "project_id": project_id,
                "receipt_id": receipt_ids[0],
                "source_task_id": None,
                "target_task_id": None,
                "source_head": locked["revision"]["head_sha"],
                "source_base": locked["revision"]["construction_base_sha"],
                "repository_id": locked["batch"]["repository_id"],
                "origin_url": repository.url,
                "target_branch": f"refs/heads/{repository.default_branch}",
                "expected_target": locked["revision"]["construction_base_sha"],
                "prepared_sha": locked["revision"]["head_sha"],
                "recovery_ref": recovery_ref,
                # Legacy child fence columns remain required; root code never
                # treats them as a project-lease namespace.
                "fence_owner_id": locked["owner"]["owner_id"],
                "fence_token": locked["owner"]["fence_token"],
                "state": "prepared",
                "review_evidence": {
                    "aggregate_ci_evidence_id": locked["evidence"]["id"],
                    "member_review_evidence_ids": [
                        member["review_evidence_id"] for member in locked["members"]
                    ],
                },
                "authors": [],
                "provenance": {
                    "publication_idempotency_key": locked["publication"]["idempotency_key"],
                    "publication_pr_number": locked["publication"]["pr_number"],
                    "attestation": attestation.model_dump(mode="json"),
                },
                "commit_metadata": {},
                "intent_kind": "root",
                "root_batch_id": batch_id,
                "root_candidate_revision": revision,
                "project_lease_owner_id": locked["lease"]["owner_id"],
                "project_lease_fence_token": locked["lease"]["fence_token"],
                "branch_fence_owner_id": locked["owner"]["owner_id"],
                "branch_fence_token": locked["owner"]["fence_token"],
                "ci_evidence_id": locked["evidence"]["id"],
                "created_at": now,
                "updated_at": now,
            }
            try:
                async with conn.begin_nested():
                    await conn.execute(insert(integration_promotion_intents).values(**intent_values))
                    for member, result, receipt_id in zip(
                        locked["members"], locked["results"], receipt_ids, strict=True
                    ):
                        await conn.execute(
                            insert(integration_root_intent_members).values(
                                intent_id=intent_id,
                                member_ordinal=member["ordinal"],
                                receipt_id=receipt_id,
                                batch_id=batch_id,
                                candidate_revision=revision,
                                source_task_id=member["task_id"],
                                repository_id=member["repository_id"],
                                reviewed_head_sha=member["reviewed_head_sha"],
                                reviewed_tree_sha=member["reviewed_tree_sha"],
                                generated_squash_sha=result["generated_squash_sha"],
                                result_evidence=result["conflict_evidence"] or {},
                                review_evidence_id=member["review_evidence_id"],
                                created_at=now,
                            )
                        )
                    mutation_nonce = str(uuid.uuid4())
                    await conn.execute(
                        insert(integration_candidate_ref_mutations).values(
                            id=self._mutation_id(intent_id),
                            batch_id=batch_id,
                            revision=revision,
                            purpose="root_main",
                            repository_id=locked["batch"]["repository_id"],
                            branch=locked["batch"]["integration_branch"],
                            target_branch=f"refs/heads/{repository.default_branch}",
                            expected_old_sha=locked["revision"]["construction_base_sha"],
                            desired_sha=locked["revision"]["head_sha"],
                            operation_id=locked["operation"]["id"],
                            operation_episode_id=locked["operation"]["episode_id"],
                            operation_stage=locked["operation"]["active_stage"],
                            lease_owner_id=locked["lease"]["owner_id"],
                            lease_fence_token=locked["lease"]["fence_token"],
                            branch_owner_id=locked["owner"]["owner_id"],
                            branch_owner_role=locked["owner"]["owner_role"],
                            branch_fence_token=locked["owner"]["fence_token"],
                            nonce=mutation_nonce,
                            state="reserved",
                            expires_at=now + _CLAIM_SECONDS,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    promoted = await conn.execute(
                        update(integration_batches)
                        .where(
                            integration_batches.c.id == batch_id,
                            integration_batches.c.current_revision == revision,
                            integration_batches.c.lifecycle == "testing",
                        )
                        .values(lifecycle="promoting", updated_at=now)
                    )
                    if promoted.rowcount != 1:
                        raise RootPromotionInvariantError(
                            "root batch did not enter promotion atomically"
                        )
            except IntegrityError:
                canonical = await self._intent_on(conn, intent_id)
                if canonical is None:
                    raise RootPromotionInvariantError(
                        "root promotion reservation raced without canonical state"
                    ) from None
                return await self._existing_result_on(
                    conn, canonical, batch_id, revision
                )
        self._owned_nonces[intent_id] = mutation_nonce
        await self._crash("after_reservation")
        return RootPromotionResult(
            outcome="prepared",
            batch_id=batch_id,
            revision=revision,
            intent_id=intent_id,
            receipt_ids=receipt_ids,
            head_sha=snapshot["revision"]["head_sha"],
        )

    async def promote(self, batch_id: str, revision: int) -> RootPromotionResult:
        prepared = await self.prepare(batch_id, revision)
        if prepared.outcome != "prepared" or prepared.intent_id is None:
            return prepared
        return await self.reconcile(prepared.intent_id)

    async def reconcile(self, intent_id: str) -> RootPromotionResult:
        intent = await self._intent(intent_id)
        if intent is None or intent.get("intent_kind") != "root":
            raise RootPromotionInvariantError("root promotion intent does not exist")
        batch_id = intent["root_batch_id"]
        revision = int(intent["root_candidate_revision"])
        if intent["state"] == "committed":
            return await self._existing_result(intent, batch_id, revision)
        if intent["state"] == "superseded":
            return RootPromotionResult(
                outcome="base_moved", batch_id=batch_id, revision=revision,
                intent_id=intent_id, receipt_ids=await self._receipt_ids(intent_id),
                head_sha=intent["prepared_sha"],
            )
        if self.app_client is None:
            return RootPromotionResult(
                outcome="configuration_blocked", batch_id=batch_id, revision=revision,
                intent_id=intent_id, receipt_ids=await self._receipt_ids(intent_id),
                head_sha=intent["prepared_sha"],
            )
        attestation = self._frozen_attestation(intent)
        if attestation is None:
            return RootPromotionResult(
                outcome="configuration_blocked", batch_id=batch_id, revision=revision,
                intent_id=intent_id, receipt_ids=await self._receipt_ids(intent_id),
                head_sha=intent["prepared_sha"],
            )
        repository = await self._repository(intent["repository_id"])
        binding = self.app_client.repository
        expected_origin = f"https://github.com/{binding.full_name}.git"
        if (
            binding.forge_host != "github.com"
            or repository.url != expected_origin
            or attestation.repository_numeric_id != binding.repository_id
            or attestation.repository_full_name != binding.full_name
        ):
            raise RootPromotionInvariantError("root promotion App repository is not canonical")
        remote = await self.app_client.exact_head_ref(
            intent["target_branch"].removeprefix("refs/heads/")
        )
        if remote is None:
            return await self._blocked(intent)
        store = self._store(repository.id)
        reachable = remote == intent["prepared_sha"]
        if not reachable and remote != intent["expected_target"]:
            try:
                await self._import_observed_main(store, intent, remote)
            except Exception:
                return await self._blocked(intent)
            ancestry = await self._is_ancestor(store, intent["prepared_sha"], remote)
            if ancestry is None:
                return await self._blocked(intent)
            reachable = ancestry
        if reachable:
            await self._mark_applied(intent, remote)
            return await self._finalize_root(intent_id, remote)

        current = await self._is_current_revision(batch_id, revision)
        mutation = await self._mutation(intent_id)
        if mutation is None:
            raise RootPromotionInvariantError("root main mutation claim is missing")
        if mutation["prewrite_at"] is not None:
            return await self._blocked(intent)
        if not current:
            if float(mutation["expires_at"]) > self.clock():
                return RootPromotionResult(
                    outcome="wait", batch_id=batch_id, revision=revision,
                    intent_id=intent_id, receipt_ids=await self._receipt_ids(intent_id),
                    head_sha=intent["prepared_sha"],
                )
            if mutation["prewrite_at"] is not None:
                return await self._blocked(intent)
            await self._supersede_unattempted(intent, mutation)
            return RootPromotionResult(
                outcome="base_moved", batch_id=batch_id, revision=revision,
                intent_id=intent_id, receipt_ids=await self._receipt_ids(intent_id),
                head_sha=intent["prepared_sha"],
            )
        if remote != intent["expected_target"]:
            if float(mutation["expires_at"]) > self.clock():
                return RootPromotionResult(
                    outcome="wait", batch_id=batch_id, revision=revision,
                    intent_id=intent_id, receipt_ids=await self._receipt_ids(intent_id),
                    head_sha=intent["prepared_sha"],
                )
            await self._supersede_unattempted(intent, mutation, current_moved=True)
            return RootPromotionResult(
                outcome="base_moved", batch_id=batch_id, revision=revision,
                intent_id=intent_id, receipt_ids=await self._receipt_ids(intent_id),
                head_sha=intent["prepared_sha"],
            )
        current_attestation = await self._resolve_attestation(attestation.subject())
        if current_attestation != attestation:
            return RootPromotionResult(
                outcome="configuration_blocked", batch_id=batch_id, revision=revision,
                intent_id=intent_id, receipt_ids=await self._receipt_ids(intent_id),
                head_sha=intent["prepared_sha"],
            )
        nonce = await self._claim_execution(intent, mutation)
        if nonce is None:
            return RootPromotionResult(
                outcome="wait", batch_id=batch_id, revision=revision,
                intent_id=intent_id, receipt_ids=await self._receipt_ids(intent_id),
                head_sha=intent["prepared_sha"],
            )
        if not await self._local_fast_forward(store, intent):
            return RootPromotionResult(
                outcome="non_fast_forward", batch_id=batch_id, revision=revision,
                intent_id=intent_id, receipt_ids=await self._receipt_ids(intent_id),
                head_sha=intent["prepared_sha"],
            )
        token = await self.app_client.installation_token()
        authority_deadline = await self._mark_prewrite(
            intent, nonce, current_attestation
        )
        if authority_deadline is None:
            return RootPromotionResult(
                outcome="wait", batch_id=batch_id, revision=revision,
                intent_id=intent_id, receipt_ids=await self._receipt_ids(intent_id),
                head_sha=intent["prepared_sha"],
            )
        await self._crash("after_prewrite_marker")
        try:
            await self.git.apush_oid_with_app_auth(
                str(store),
                repository=self.app_client.repository,
                token=token,
                tip_oid=intent["prepared_sha"],
                branch=intent["target_branch"].removeprefix("refs/heads/"),
                expected_old_oid=intent["expected_target"],
                authority_deadline=authority_deadline,
            )
        except Exception:
            observed = await self.app_client.exact_head_ref(
                intent["target_branch"].removeprefix("refs/heads/")
            )
            if observed != intent["prepared_sha"]:
                raise
            remote = observed
        else:
            remote = intent["prepared_sha"]
        await self._crash("after_external_push")
        await self._mark_applied(intent, remote)
        await self._crash("after_push_proof")
        return await self._finalize_root(intent_id, remote)

    async def _claim_execution(self, intent: dict[str, Any], mutation: dict[str, Any]) -> str | None:
        project_id = intent["project_id"]
        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            locked = await self._snapshot_on(
                conn, project_id, intent["root_batch_id"], int(intent["root_candidate_revision"])
            )
            if (
                self._validate_snapshot(locked, int(intent["root_candidate_revision"]))
                is not None
                or not self._intent_matches_authority(intent, locked)
            ):
                return None
            row = (
                await conn.execute(
                    select(integration_candidate_ref_mutations)
                    .where(integration_candidate_ref_mutations.c.id == mutation["id"])
                    .with_for_update()
                )
            ).mappings().one()
            if row["prewrite_at"] is not None:
                return None
            owned = self._owned_nonces.get(intent["id"])
            if owned == row["nonce"] and float(row["expires_at"]) > now:
                minimum = now + _CLAIM_SECONDS
                if float(row["expires_at"]) < minimum:
                    renewed = await conn.execute(
                        update(integration_candidate_ref_mutations)
                        .where(
                            integration_candidate_ref_mutations.c.id == row["id"],
                            integration_candidate_ref_mutations.c.state == "reserved",
                            integration_candidate_ref_mutations.c.nonce == owned,
                            integration_candidate_ref_mutations.c.expires_at
                            == row["expires_at"],
                            integration_candidate_ref_mutations.c.prewrite_at.is_(None),
                        )
                        .values(expires_at=minimum, updated_at=now)
                    )
                    if renewed.rowcount != 1:
                        return None
                return owned
            if float(row["expires_at"]) > now:
                return None
            nonce = str(uuid.uuid4())
            changed = await conn.execute(
                update(integration_candidate_ref_mutations)
                .where(
                    integration_candidate_ref_mutations.c.id == row["id"],
                    integration_candidate_ref_mutations.c.state == "reserved",
                    integration_candidate_ref_mutations.c.nonce == row["nonce"],
                    integration_candidate_ref_mutations.c.expires_at == row["expires_at"],
                    integration_candidate_ref_mutations.c.expires_at <= now,
                    integration_candidate_ref_mutations.c.prewrite_at.is_(None),
                )
                .values(nonce=nonce, expires_at=now + _CLAIM_SECONDS, updated_at=now)
            )
            if changed.rowcount != 1:
                return None
            self._owned_nonces[intent["id"]] = nonce
            return nonce

    async def _mark_prewrite(
        self, intent: dict[str, Any], nonce: str, attestation: RootAttestationProof
    ) -> float | None:
        authority_deadline: float | None = None
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, intent["project_id"])
            locked = await self._snapshot_on(
                conn,
                intent["project_id"],
                intent["root_batch_id"],
                int(intent["root_candidate_revision"]),
            )
            monotonic_started_at = asyncio.get_running_loop().time()
            now = self.clock()
            minimum = (
                now
                + APP_AUTH_PUSH_TIMEOUT_SECONDS
                + APP_AUTH_PUSH_CLEANUP_MARGIN_SECONDS
            )
            if (
                self._validate_snapshot(
                    locked,
                    int(intent["root_candidate_revision"]),
                    minimum_lease_expires_at=minimum,
                )
                is not None
                or not self._intent_matches_authority(intent, locked)
                or not self._proof_matches_state(attestation, locked)
                or float(locked["lease"]["expires_at"]) < minimum
            ):
                return None
            changed = await conn.execute(
                update(integration_candidate_ref_mutations)
                .where(
                    integration_candidate_ref_mutations.c.id == self._mutation_id(intent["id"]),
                    integration_candidate_ref_mutations.c.state == "reserved",
                    integration_candidate_ref_mutations.c.nonce == nonce,
                    integration_candidate_ref_mutations.c.expires_at >= minimum,
                    integration_candidate_ref_mutations.c.prewrite_at.is_(None),
                )
                .values(
                    prewrite_at=now,
                    updated_at=now,
                )
            )
            if changed.rowcount == 1:
                authority_deadline = (
                    monotonic_started_at + APP_AUTH_PUSH_TIMEOUT_SECONDS
                )
        return authority_deadline

    async def _mark_applied(self, intent: dict[str, Any], remote: str) -> None:
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, intent["project_id"])
            locked_intent = await self._intent_on(conn, intent["id"])
            row = (
                await conn.execute(
                    select(integration_candidate_ref_mutations)
                    .where(integration_candidate_ref_mutations.c.id == self._mutation_id(intent["id"]))
                    .with_for_update()
                )
            ).mappings().one()
            if (
                locked_intent is None
                or locked_intent["intent_kind"] != "root"
                or locked_intent["state"] not in {"prepared", "pushed"}
                or locked_intent["prepared_sha"] != intent["prepared_sha"]
                or row["purpose"] != "root_main"
                or row["batch_id"] != intent["root_batch_id"]
                or int(row["revision"]) != int(intent["root_candidate_revision"])
                or row["desired_sha"] != intent["prepared_sha"]
            ):
                raise RootPromotionInvariantError("root main proof identity changed")
            if row["state"] == "applied":
                if row["remote_sha"] != intent["prepared_sha"]:
                    raise RootPromotionInvariantError("root main proof identity changed")
            elif row["state"] != "reserved":
                raise RootPromotionInvariantError("root main write was not proven")
            else:
                await conn.execute(
                    update(integration_candidate_ref_mutations)
                    .where(
                        integration_candidate_ref_mutations.c.id == row["id"],
                        integration_candidate_ref_mutations.c.state == "reserved",
                    )
                    .values(
                        state="applied",
                        remote_sha=intent["prepared_sha"],
                        updated_at=self.clock(),
                    )
                )
            await conn.execute(
                update(integration_promotion_intents)
                .where(
                    integration_promotion_intents.c.id == intent["id"],
                    integration_promotion_intents.c.state.in_(("prepared", "pushed")),
                )
                .values(
                    state="pushed",
                    remote_evidence={"kind": "authenticated_main", "remote_sha": remote},
                    updated_at=self.clock(),
                )
            )

    async def _supersede_unattempted(
        self,
        intent: dict[str, Any],
        mutation: dict[str, Any],
        *,
        current_moved: bool = False,
    ) -> None:
        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, intent["project_id"])
            if current_moved:
                locked = await self._snapshot_on(
                    conn,
                    intent["project_id"],
                    intent["root_batch_id"],
                    int(intent["root_candidate_revision"]),
                )
                batch = locked["batch"]
                authority_changed = (
                    self._validate_snapshot(
                        locked, int(intent["root_candidate_revision"])
                    )
                    is not None
                    or not self._intent_matches_authority(intent, locked)
                )
            else:
                batch = (
                    await conn.execute(
                        select(integration_batches)
                        .where(integration_batches.c.id == intent["root_batch_id"])
                        .with_for_update()
                    )
                ).mappings().one()
                authority_changed = False
            locked_intent = await self._intent_on(conn, intent["id"])
            row = (
                await conn.execute(
                    select(integration_candidate_ref_mutations)
                    .where(integration_candidate_ref_mutations.c.id == mutation["id"])
                    .with_for_update()
                )
            ).mappings().one()
            if (
                (
                    int(batch["current_revision"])
                    == int(intent["root_candidate_revision"])
                )
                != current_moved
                or authority_changed
                or locked_intent["state"] != "prepared"
                or row["state"] != "reserved"
                or row["prewrite_at"] is not None
                or row["nonce"] != mutation["nonce"]
                or row["expires_at"] != mutation["expires_at"]
                or float(row["expires_at"]) > now
            ):
                raise RootPromotionInvariantError("attempted root promotion cannot be superseded")
            changed = await conn.execute(
                update(integration_candidate_ref_mutations)
                .where(
                    integration_candidate_ref_mutations.c.id == row["id"],
                    integration_candidate_ref_mutations.c.state == "reserved",
                    integration_candidate_ref_mutations.c.nonce == row["nonce"],
                    integration_candidate_ref_mutations.c.expires_at == row["expires_at"],
                    integration_candidate_ref_mutations.c.expires_at <= now,
                    integration_candidate_ref_mutations.c.prewrite_at.is_(None),
                )
                .values(state="superseded", updated_at=now)
            )
            superseded = await conn.execute(
                update(integration_promotion_intents)
                .where(
                    integration_promotion_intents.c.id == intent["id"],
                    integration_promotion_intents.c.state == "prepared",
                )
                .values(state="superseded", updated_at=now)
            )
            if changed.rowcount != 1 or superseded.rowcount != 1:
                raise RootPromotionInvariantError(
                    "attempted root promotion cannot be superseded"
                )
            if current_moved:
                lifecycle = await conn.execute(
                    update(integration_batches)
                    .where(
                        integration_batches.c.id == intent["root_batch_id"],
                        integration_batches.c.current_revision
                        == intent["root_candidate_revision"],
                        integration_batches.c.lifecycle == "promoting",
                    )
                    .values(lifecycle="building", updated_at=now)
                )
                if lifecycle.rowcount != 1:
                    raise RootPromotionInvariantError(
                        "moved-main batch could not re-enter candidate building"
                    )

    @staticmethod
    def _intent_matches_authority(intent: dict[str, Any], state: dict[str, Any]) -> bool:
        lease = state["lease"]
        owner = state["owner"]
        operation = state["operation"]
        return bool(
            lease is not None
            and owner is not None
            and operation is not None
            and intent["operation_key"] == operation["id"]
            and intent["project_lease_owner_id"] == lease["owner_id"]
            and int(intent["project_lease_fence_token"]) == int(lease["fence_token"])
            and intent["branch_fence_owner_id"] == owner["owner_id"]
            and int(intent["branch_fence_token"]) == int(owner["fence_token"])
        )

    async def _finalize_root(self, intent_id: str, remote: str) -> RootPromotionResult:
        intent = await self._intent(intent_id)
        if intent is None:
            raise RootPromotionInvariantError("root promotion intent disappeared")
        if intent["state"] == "committed":
            return await self._existing_result(intent, intent["root_batch_id"], int(intent["root_candidate_revision"]))
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, intent["project_id"])
            project = (
                await conn.execute(
                    select(projects)
                    .where(projects.c.id == intent["project_id"])
                    .with_for_update()
                )
            ).mappings().one_or_none()
            batch = (
                await conn.execute(
                    select(integration_batches)
                    .where(integration_batches.c.id == intent["root_batch_id"])
                    .with_for_update()
                )
            ).mappings().one_or_none()
            candidate = (
                await conn.execute(
                    select(integration_candidate_revisions)
                    .where(
                        integration_candidate_revisions.c.batch_id
                        == intent["root_batch_id"],
                        integration_candidate_revisions.c.revision
                        == intent["root_candidate_revision"],
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            lease = (
                await conn.execute(
                    select(project_integration_leases)
                    .where(project_integration_leases.c.project_id == intent["project_id"])
                    .with_for_update()
                )
            ).mappings().one_or_none()
            operation = (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(integration_repair_operations.c.id == intent["operation_key"])
                    .with_for_update()
                )
            ).mappings().one_or_none()
            stage = None
            if operation is not None:
                stage = (
                    await conn.execute(
                        select(integration_repair_stages)
                        .where(
                            integration_repair_stages.c.operation_id == operation["id"],
                            integration_repair_stages.c.ordinal == operation["active_stage"],
                        )
                        .with_for_update()
                    )
                ).mappings().one_or_none()
            owner = None
            if batch is not None and batch["integration_branch"] is not None:
                owner = (
                    await conn.execute(
                        select(integration_branch_owners)
                        .where(
                            integration_branch_owners.c.repository_id
                            == batch["repository_id"],
                            integration_branch_owners.c.ref == batch["integration_branch"],
                        )
                        .with_for_update()
                    )
                ).mappings().one_or_none()
            locked_intent = (
                await conn.execute(
                    select(integration_promotion_intents)
                    .where(integration_promotion_intents.c.id == intent_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if locked_intent is not None and locked_intent["state"] == "committed":
                return await self._existing_result_on(
                    conn,
                    locked_intent,
                    locked_intent["root_batch_id"],
                    int(locked_intent["root_candidate_revision"]),
                )
            claim = (
                await conn.execute(
                    select(integration_candidate_ref_mutations)
                    .where(integration_candidate_ref_mutations.c.id == self._mutation_id(intent_id))
                    .with_for_update()
                )
            ).mappings().one_or_none()
            members = (
                await conn.execute(
                    select(integration_batch_members)
                    .where(integration_batch_members.c.batch_id == intent["root_batch_id"])
                    .order_by(integration_batch_members.c.ordinal)
                    .with_for_update()
                )
            ).mappings().all()
            results = (
                await conn.execute(
                    select(integration_candidate_member_results)
                    .where(
                        integration_candidate_member_results.c.batch_id
                        == intent["root_batch_id"],
                        integration_candidate_member_results.c.revision
                        == intent["root_candidate_revision"],
                    )
                    .order_by(integration_candidate_member_results.c.member_ordinal)
                    .with_for_update()
                )
            ).mappings().all()
            reservations = (
                await conn.execute(
                    select(integration_root_intent_members)
                    .where(integration_root_intent_members.c.intent_id == intent_id)
                    .order_by(integration_root_intent_members.c.member_ordinal)
                    .with_for_update()
                )
            ).mappings().all()
            review_ids = [member["review_evidence_id"] for member in members]
            reviews = (
                await conn.execute(
                    select(integration_review_evidence)
                    .where(integration_review_evidence.c.id.in_(review_ids))
                    .order_by(integration_review_evidence.c.id)
                    .with_for_update()
                )
            ).mappings().all()
            receipt_ids = [reservation["receipt_id"] for reservation in reservations]
            existing_receipt_rows = (
                await conn.execute(
                    select(task_delivery_receipts)
                    .where(
                        or_(
                            task_delivery_receipts.c.id.in_(receipt_ids),
                            (
                                (task_delivery_receipts.c.batch_id == intent["root_batch_id"])
                                & (
                                    task_delivery_receipts.c.candidate_revision
                                    == intent["root_candidate_revision"]
                                )
                            ),
                        )
                    )
                    .order_by(task_delivery_receipts.c.member_ordinal)
                    .with_for_update()
                )
            ).mappings().all()
            evidence = None
            if candidate is not None and candidate["ci_evidence_id"] is not None:
                evidence = (
                    await conn.execute(
                        select(integration_check_evidence).where(
                            integration_check_evidence.c.id == candidate["ci_evidence_id"]
                        )
                    )
                ).mappings().one_or_none()

            ordinal_count = len(members)
            ordinals = list(range(ordinal_count))
            reservation_ordinals = [int(row["member_ordinal"]) for row in reservations]
            result_ordinals = [int(row["member_ordinal"]) for row in results]
            review_by_id = {row["id"]: row for row in reviews}
            if (
                project is None
                or batch is None
                or candidate is None
                or lease is None
                or operation is None
                or stage is None
                or owner is None
                or locked_intent is None
                or claim is None
                or evidence is None
                or locked_intent["intent_kind"] != "root"
                or locked_intent["state"] != "pushed"
                or locked_intent["project_id"] != project["id"]
                or locked_intent["root_batch_id"] != batch["id"]
                or int(locked_intent["root_candidate_revision"])
                != int(candidate["revision"])
                or locked_intent["operation_key"] != operation["id"]
                or batch["project_id"] != project["id"]
                or batch["repository_id"] != locked_intent["repository_id"]
                or batch["lifecycle"] != "promoting"
                or int(batch["current_revision"]) != int(candidate["revision"])
                or batch["tested_candidate_sha"] != candidate["head_sha"]
                or batch["ci_evidence_id"] != candidate["ci_evidence_id"]
                or candidate["state"] != "green"
                or candidate["head_sha"] != locked_intent["prepared_sha"]
                or evidence["id"] != locked_intent["ci_evidence_id"]
                or evidence["operation_id"] != operation["id"]
                or evidence["batch_id"] != batch["id"]
                or int(evidence["candidate_revision"]) != int(candidate["revision"])
                or evidence["conclusion"] != "success"
                or evidence["classification"] != "conclusive"
                or operation["target_kind"] != "batch"
                or operation["batch_id"] != batch["id"]
                or operation["episode_id"] != batch["id"]
                or operation["state"] not in {"active", "escalated"}
                or stage["operation_id"] != operation["id"]
                or int(stage["ordinal"]) != int(operation["active_stage"])
                or stage["state"] != "awaiting_completion"
                or lease["batch_id"] != batch["id"]
                or lease["repository_id"] != batch["repository_id"]
                or lease["owner_id"] != locked_intent["project_lease_owner_id"]
                or int(lease["fence_token"])
                != int(locked_intent["project_lease_fence_token"])
                or owner["owner_id"] != operation["id"]
                or owner["owner_id"] != locked_intent["branch_fence_owner_id"]
                or owner["owner_role"] != "collector"
                or owner["handoff_state"] != "reserved"
                or int(owner["fence_token"]) != int(locked_intent["branch_fence_token"])
                or claim["purpose"] != "root_main"
                or claim["batch_id"] != batch["id"]
                or int(claim["revision"]) != int(candidate["revision"])
                or claim["operation_id"] != operation["id"]
                or claim["state"] != "applied"
                or claim["desired_sha"] != candidate["head_sha"]
                or claim["remote_sha"] != candidate["head_sha"]
                or locked_intent["remote_evidence"]
                != {"kind": "authenticated_main", "remote_sha": remote}
                or not ordinal_count
                or [int(row["ordinal"]) for row in members] != ordinals
                or result_ordinals != ordinals
                or reservation_ordinals != ordinals
                or int(candidate["next_member_ordinal"]) != ordinal_count
                or len(reviews) != ordinal_count
                or locked_intent["receipt_id"] != reservations[0]["receipt_id"]
            ):
                raise RootPromotionInvariantError("root finalization proof is incomplete")

            for ordinal, (member, result, reservation) in enumerate(
                zip(members, results, reservations, strict=True)
            ):
                review = review_by_id.get(member["review_evidence_id"])
                if (
                    int(member["ordinal"]) != ordinal
                    or int(result["member_ordinal"]) != ordinal
                    or int(reservation["member_ordinal"]) != ordinal
                    or reservation["intent_id"] != locked_intent["id"]
                    or reservation["receipt_id"]
                    != self._receipt_id(batch["id"], int(candidate["revision"]), ordinal)
                    or reservation["batch_id"] != batch["id"]
                    or int(reservation["candidate_revision"]) != int(candidate["revision"])
                    or reservation["source_task_id"] != member["task_id"]
                    or reservation["repository_id"] != member["repository_id"]
                    or reservation["reviewed_head_sha"] != member["reviewed_head_sha"]
                    or reservation["reviewed_tree_sha"] != member["reviewed_tree_sha"]
                    or reservation["review_evidence_id"] != member["review_evidence_id"]
                    or result["result"] != "applied"
                    or result["input_head_sha"] != member["reviewed_head_sha"]
                    or result["input_tree_sha"] != member["reviewed_tree_sha"]
                    or reservation["generated_squash_sha"] != result["generated_squash_sha"]
                    or reservation["result_evidence"] != (result["conflict_evidence"] or {})
                    or review is None
                    or review["source_task_id"] != member["task_id"]
                    or review["repository_id"] != member["repository_id"]
                    or review["reviewed_head_sha"] != member["reviewed_head_sha"]
                    or review["reviewed_tree_sha"] != member["reviewed_tree_sha"]
                    or review["verdict"] != "approved"
                    or member["review_evidence"] != review["evidence"]
                ):
                    raise RootPromotionInvariantError(
                        "root finalization requires the complete frozen member set"
                    )

            now = self.clock()
            expected_receipts: list[dict[str, Any]] = []
            for reservation in reservations:
                receipt = {
                    "id": reservation["receipt_id"],
                    "domain_key": f"root:{reservation['batch_id']}:{reservation['candidate_revision']}:{reservation['member_ordinal']}",
                    "source_task_id": reservation["source_task_id"],
                    "target_task_id": None,
                    "repository_id": reservation["repository_id"],
                    "target_branch": locked_intent["target_branch"],
                    "reviewed_head_sha": reservation["reviewed_head_sha"],
                    "reviewed_tree_sha": reservation["reviewed_tree_sha"],
                    "before_sha": locked_intent["expected_target"],
                    "squash_sha": reservation["generated_squash_sha"],
                    "after_sha": locked_intent["prepared_sha"],
                    "review_evidence": {
                        "review_evidence_id": reservation["review_evidence_id"],
                        "candidate_result_evidence": reservation["result_evidence"],
                    },
                    "verification_evidence": {
                        "ci_evidence_id": locked_intent["ci_evidence_id"]
                    },
                    "resolution_evidence": None,
                    "batch_id": reservation["batch_id"],
                    "member_ordinal": reservation["member_ordinal"],
                    "candidate_revision": reservation["candidate_revision"],
                    "disposition": "code",
                    "disposition_revision": None,
                    "parent_operation_id": None,
                    "parent_episode_id": None,
                    "workspace_kind": None,
                    "source_pr": None,
                    "created_at": reservation["created_at"],
                }
                expected_receipts.append(receipt)
            existing_receipts = {row["id"]: dict(row) for row in existing_receipt_rows}
            if set(existing_receipts) - set(receipt_ids):
                raise RootPromotionInvariantError(
                    "root finalization requires the complete frozen member set"
                )
            for receipt in expected_receipts:
                existing = existing_receipts.get(receipt["id"])
                if existing is None:
                    await conn.execute(insert(task_delivery_receipts).values(**receipt))
                elif existing != receipt:
                    raise RootPromotionInvariantError("root receipt identity changed")
                await self._crash(f"after_root_receipt:{receipt['member_ordinal']}")
                event_id = f"root-delivered-{receipt['id']}"
                await enqueue_integration_event(
                    conn,
                    event_id=event_id,
                    dedup_key=event_id,
                    event_type="integration.root_delivered",
                    project_id=locked_intent["project_id"],
                    payload={
                        "operation_id": operation["id"],
                        "batch_id": receipt["batch_id"],
                        "revision": receipt["candidate_revision"],
                        "member_ordinal": receipt["member_ordinal"],
                        "receipt_id": receipt["id"],
                    },
                    available_at=now,
                )
            promoted_candidate = await conn.execute(
                update(integration_candidate_revisions)
                .where(
                    integration_candidate_revisions.c.batch_id == locked_intent["root_batch_id"],
                    integration_candidate_revisions.c.revision == locked_intent["root_candidate_revision"],
                    integration_candidate_revisions.c.state == "green",
                    integration_candidate_revisions.c.head_sha == locked_intent["prepared_sha"],
                    integration_candidate_revisions.c.ci_evidence_id
                    == locked_intent["ci_evidence_id"],
                )
                .values(state="promoted", updated_at=now)
            )
            promoted_batch = await conn.execute(
                update(integration_batches)
                .where(
                    integration_batches.c.id == locked_intent["root_batch_id"],
                    integration_batches.c.lifecycle == "promoting",
                    integration_batches.c.current_revision
                    == locked_intent["root_candidate_revision"],
                    integration_batches.c.tested_candidate_sha
                    == locked_intent["prepared_sha"],
                    integration_batches.c.ci_evidence_id == locked_intent["ci_evidence_id"],
                    integration_batches.c.final_main_sha.is_(None),
                )
                .values(
                    lifecycle="promoted",
                    final_main_sha=remote,
                    cleanup_state="pending",
                    updated_at=now,
                )
            )
            passed_stage = await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == locked_intent["operation_key"],
                    integration_repair_stages.c.ordinal == operation["active_stage"],
                    integration_repair_stages.c.state == "awaiting_completion",
                )
                .values(state="passed", completed_at=now)
            )
            completed_operation = await conn.execute(
                update(integration_repair_operations)
                .where(
                    integration_repair_operations.c.id == locked_intent["operation_key"],
                    integration_repair_operations.c.batch_id == batch["id"],
                    integration_repair_operations.c.episode_id == batch["id"],
                    integration_repair_operations.c.state.in_(("active", "escalated")),
                )
                .values(state="completed", updated_at=now)
            )
            if any(
                result.rowcount != 1
                for result in (
                    promoted_candidate,
                    promoted_batch,
                    passed_stage,
                    completed_operation,
                )
            ):
                raise RootPromotionInvariantError("root finalization terminal CAS failed")
            for event_type in ("integration.batch_promoted", "integration.cleanup_requested"):
                event_id = f"{event_type}:{intent_id}"
                await enqueue_integration_event(
                    conn,
                    event_id=event_id,
                    dedup_key=event_id,
                    event_type=event_type,
                    project_id=locked_intent["project_id"],
                    payload={
                        "operation_id": operation["id"],
                        "batch_id": locked_intent["root_batch_id"],
                        "revision": locked_intent["root_candidate_revision"],
                        "intent_id": intent_id,
                        "head_sha": remote,
                    },
                    available_at=now,
                )
            committed_intent = await conn.execute(
                update(integration_promotion_intents)
                .where(
                    integration_promotion_intents.c.id == intent_id,
                    integration_promotion_intents.c.state == "pushed",
                )
                .values(state="committed", committed_at=now, updated_at=now)
            )
            if committed_intent.rowcount != 1:
                raise RootPromotionInvariantError("root finalization terminal CAS failed")
        committed = await self._intent(intent_id)
        return await self._existing_result(
            committed, committed["root_batch_id"], int(committed["root_candidate_revision"])
        )

    async def _mutation(self, intent_id: str) -> dict[str, Any] | None:
        async with self.db._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(integration_candidate_ref_mutations).where(
                        integration_candidate_ref_mutations.c.id == self._mutation_id(intent_id)
                    )
                )
            ).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def _receipt_ids(self, intent_id: str) -> tuple[str, ...]:
        async with self.db._engine.connect() as conn:
            return tuple(
                (
                    await conn.execute(
                        select(integration_root_intent_members.c.receipt_id)
                        .where(integration_root_intent_members.c.intent_id == intent_id)
                        .order_by(integration_root_intent_members.c.member_ordinal)
                    )
                ).scalars()
            )

    async def _blocked(self, intent: dict[str, Any]) -> RootPromotionResult:
        return RootPromotionResult(
            outcome="reconciliation_blocked", batch_id=intent["root_batch_id"],
            revision=int(intent["root_candidate_revision"]), intent_id=intent["id"],
            receipt_ids=await self._receipt_ids(intent["id"]), head_sha=intent["prepared_sha"],
        )

    async def _is_current_revision(self, batch_id: str, revision: int) -> bool:
        async with self.db._engine.connect() as conn:
            current = (
                await conn.execute(
                    select(integration_batches.c.current_revision).where(
                        integration_batches.c.id == batch_id
                    )
                )
            ).scalar_one_or_none()
        return current == revision

    async def _is_ancestor(self, store: Path, older: str, newer: str) -> bool | None:
        result = await self.git.arun_git_result(
            ["merge-base", "--is-ancestor", older, newer],
            cwd=str(store), env={"LC_ALL": "C"},
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        return None

    async def _local_fast_forward(self, store: Path, intent: dict[str, Any]) -> bool:
        return bool(
            await self._is_ancestor(
                store, intent["expected_target"], intent["prepared_sha"]
            )
        )

    async def _import_observed_main(
        self, store: Path, intent: dict[str, Any], remote: str
    ) -> None:
        token = await self.app_client.installation_token()
        imported = await self.git.afetch_exact_oid_with_app_auth(
            str(store),
            repository=self.app_client.repository,
            token=token,
            oid=remote,
            destination_ref=f"refs/aq/root-main-observed/{intent['id']}",
        )
        if imported != remote:
            raise RootPromotionInvariantError("authenticated main import changed identity")

    def _store(self, repository_id: str) -> Path:
        return self.data_dir / "integration-repositories" / (
            hashlib.sha256(repository_id.encode()).hexdigest() + ".git"
        )

    async def _snapshot(self, project_id: str, batch_id: str, revision: int) -> dict[str, Any]:
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            return await self._snapshot_on(conn, project_id, batch_id, revision)

    async def _snapshot_on(
        self, conn: Any, project_id: str, batch_id: str, revision: int
    ) -> dict[str, Any]:
        async def one(statement):
            return (await conn.execute(statement)).mappings().one_or_none()

        project = await one(select(projects).where(projects.c.id == project_id).with_for_update())
        batch = await one(
            select(integration_batches)
            .where(integration_batches.c.id == batch_id)
            .with_for_update()
        )
        candidate = await one(
            select(integration_candidate_revisions)
            .where(
                integration_candidate_revisions.c.batch_id == batch_id,
                integration_candidate_revisions.c.revision == revision,
            )
            .with_for_update()
        )
        operation = await one(
            select(integration_repair_operations)
            .where(
                integration_repair_operations.c.batch_id == batch_id,
                integration_repair_operations.c.episode_id == batch_id,
            )
            .with_for_update()
        )
        stage = None
        if operation is not None:
            stage = await one(
                select(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == operation["id"],
                    integration_repair_stages.c.ordinal == operation["active_stage"],
                )
                .with_for_update()
            )
        lease = await one(
            select(project_integration_leases)
            .where(project_integration_leases.c.project_id == project_id)
            .with_for_update()
        )
        owner = None
        if batch is not None and batch["integration_branch"] is not None:
            owner = await one(
                select(integration_branch_owners)
                .where(
                    integration_branch_owners.c.repository_id == batch["repository_id"],
                    integration_branch_owners.c.ref == batch["integration_branch"],
                )
                .with_for_update()
            )
        members = (
            await conn.execute(
                select(integration_batch_members)
                .where(integration_batch_members.c.batch_id == batch_id)
                .order_by(integration_batch_members.c.ordinal)
            )
        ).mappings().all()
        results = (
            await conn.execute(
                select(integration_candidate_member_results)
                .where(
                    integration_candidate_member_results.c.batch_id == batch_id,
                    integration_candidate_member_results.c.revision == revision,
                )
                .order_by(integration_candidate_member_results.c.member_ordinal)
            )
        ).mappings().all()
        evidence = None
        if candidate is not None and candidate["ci_evidence_id"]:
            evidence = await one(
                select(integration_check_evidence).where(
                    integration_check_evidence.c.id == candidate["ci_evidence_id"]
                )
            )
        publication = await one(
            select(integration_candidate_publications).where(
                integration_candidate_publications.c.batch_id == batch_id,
                integration_candidate_publications.c.revision == revision,
            )
        )
        return {
            "project": project,
            "batch": batch,
            "revision": candidate,
            "operation": operation,
            "stage": stage,
            "lease": lease,
            "owner": owner,
            "members": list(members),
            "results": list(results),
            "evidence": evidence,
            "publication": publication,
        }

    def _validate_snapshot(
        self,
        state: dict[str, Any],
        revision: int,
        *,
        minimum_lease_expires_at: float | None = None,
    ) -> str | None:
        project = state["project"]
        batch = state["batch"]
        candidate = state["revision"]
        if project is None or batch is None:
            return "stale"
        if batch["lifecycle"] == "empty":
            return None
        evidence = state["evidence"]
        required = batch["policy_snapshot"].get("root", {}).get("required_checks", {})
        exact_checks = required.get("names") if isinstance(required, dict) else None
        if (
            project["hierarchical_integration_mode"] != "train"
            or project["integration_repository_id"] != batch["repository_id"]
            or batch["project_id"] != project["id"]
            or int(batch["current_revision"]) != revision
            or candidate is None
            or candidate["state"] != "green"
            or candidate["head_sha"] != batch["tested_candidate_sha"]
            or candidate["ci_evidence_id"] != batch["ci_evidence_id"]
            or evidence is None
            or evidence["id"] != candidate["ci_evidence_id"]
            or evidence["operation_id"]
            != (state["operation"]["id"] if state["operation"] else None)
            or evidence["batch_id"] != batch["id"]
            or int(evidence["candidate_revision"]) != revision
            or evidence["conclusion"] != "success"
            or evidence["classification"] != "conclusive"
            or evidence["required_check_version"] != required.get("version")
            or evidence["producer_id"] != required.get("producer_id")
            or not isinstance(exact_checks, list)
            or list(evidence["checks"].keys()) != exact_checks
            or any(value != "success" for value in evidence["checks"].values())
        ):
            return "ci_missing"
        operation = state["operation"]
        stage = state["stage"]
        lease = state["lease"]
        owner = state["owner"]
        publication = state["publication"]
        if minimum_lease_expires_at is None:
            minimum_lease_expires_at = self.clock() + _CLAIM_SECONDS
        if (
            batch["lifecycle"] not in {"testing", "promoting"}
            or operation is None
            or operation["target_kind"] != "batch"
            or operation["episode_id"] != batch["id"]
            or operation["state"] not in {"active", "escalated"}
            or stage is None
            or stage["state"] != "awaiting_completion"
            or lease is None
            or lease["batch_id"] != batch["id"]
            or lease["repository_id"] != batch["repository_id"]
            or float(lease["expires_at"]) < minimum_lease_expires_at
            or owner is None
            or owner["owner_id"] != operation["id"]
            or owner["owner_role"] != "collector"
            or owner["handoff_state"] != "reserved"
            or publication is None
            or publication["state"] != "pr_published"
            or publication["head_sha"] != candidate["head_sha"]
            or publication["repository_id"] != batch["repository_id"]
            or (
                self.app_client is not None
                and (
                    publication["repository_numeric_id"]
                    != self.app_client.repository.repository_id
                    or publication["repository_full_name"]
                    != self.app_client.repository.full_name
                )
            )
        ):
            return "wait"
        ordinals = [int(member["ordinal"]) for member in state["members"]]
        result_ordinals = [int(result["member_ordinal"]) for result in state["results"]]
        if (
            not ordinals
            or ordinals != list(range(len(ordinals)))
            or result_ordinals != ordinals
            or any(result["result"] != "applied" for result in state["results"])
            or any(not is_valid_git_oid(result["generated_squash_sha"]) for result in state["results"])
            or not is_valid_git_oid(candidate["head_sha"])
            or not is_valid_git_oid(candidate["construction_base_sha"])
        ):
            return "stale"
        return None

    @staticmethod
    def _attestation_subject(state: dict[str, Any]) -> RootAttestationSubject:
        publication = state["publication"]
        operation = state["operation"]
        candidate = state["revision"]
        evidence = state["evidence"]
        return RootAttestationSubject(
            repository_numeric_id=publication["repository_numeric_id"],
            repository_full_name=publication["repository_full_name"],
            operation_id=operation["id"],
            batch_id=state["batch"]["id"],
            revision=int(candidate["revision"]),
            candidate_sha=candidate["head_sha"],
            required_check_version=evidence["required_check_version"],
        )

    def _proof_matches_state(
        self, proof: RootAttestationProof, state: dict[str, Any]
    ) -> bool:
        try:
            return proof.subject() == self._attestation_subject(state)
        except (TypeError, ValidationError):
            return False

    async def _resolve_attestation(
        self, subject: RootAttestationSubject
    ) -> RootAttestationProof | None:
        if self.attestation_resolver is None:
            return None
        try:
            proof = self.attestation_resolver(subject)
            if inspect.isawaitable(proof):
                proof = await proof
        except Exception:
            return None
        if not isinstance(proof, RootAttestationProof) or proof.subject() != subject:
            return None
        return proof

    @staticmethod
    def _frozen_attestation(intent: dict[str, Any]) -> RootAttestationProof | None:
        try:
            return RootAttestationProof.model_validate(
                intent["provenance"]["attestation"]
            )
        except (KeyError, TypeError, ValidationError):
            return None

    async def _pin_recovery(self, repository: Any, ref: str, head_sha: str) -> None:
        digest = hashlib.sha256(repository.id.encode()).hexdigest()
        store = self.data_dir / "integration-repositories" / f"{digest}.git"
        if not store.is_dir():
            raise RootPromotionInvariantError("candidate recovery repository is unavailable")
        result = await self.git.arun_git_result(
            ["update-ref", ref, head_sha], cwd=str(store), env={"LC_ALL": "C"}
        )
        if result.returncode != 0:
            raise RootPromotionInvariantError("candidate recovery ref could not be pinned")

    async def _repository(self, repository_id: str) -> Any:
        value = (
            self.repository_resolver(repository_id)
            if self.repository_resolver is not None
            else self.db.get_repo(repository_id)
        )
        if inspect.isawaitable(value):
            value = await value
        if value is None or value.id != repository_id or not value.url:
            raise RootPromotionInvariantError("promotion repository is unavailable")
        return value

    async def _project_id(self, batch_id: str) -> str | None:
        async with self.db._engine.connect() as conn:
            return (
                await conn.execute(
                    select(integration_batches.c.project_id).where(
                        integration_batches.c.id == batch_id
                    )
                )
            ).scalar_one_or_none()

    async def _intent(self, intent_id: str) -> dict[str, Any] | None:
        async with self.db._engine.connect() as conn:
            row = await self._intent_on(conn, intent_id)
        return dict(row) if row is not None else None

    @staticmethod
    async def _intent_on(conn: Any, intent_id: str) -> Any | None:
        return (
            await conn.execute(
                select(integration_promotion_intents).where(
                    integration_promotion_intents.c.id == intent_id
                )
            )
        ).mappings().one_or_none()

    async def _existing_result(
        self, intent: dict[str, Any], batch_id: str, revision: int
    ) -> RootPromotionResult:
        async with self.db._engine.connect() as conn:
            return await self._existing_result_on(conn, intent, batch_id, revision)

    async def _existing_result_on(
        self, conn: Any, intent: Any, batch_id: str, revision: int
    ) -> RootPromotionResult:
        if (
            intent["intent_kind"] != "root"
            or intent["root_batch_id"] != batch_id
            or int(intent["root_candidate_revision"]) != revision
        ):
            raise RootPromotionInvariantError("root promotion identity changed")
        receipt_ids = tuple(
            (
                await conn.execute(
                    select(integration_root_intent_members.c.receipt_id)
                    .where(integration_root_intent_members.c.intent_id == intent["id"])
                    .order_by(integration_root_intent_members.c.member_ordinal)
                )
            ).scalars()
        )
        return RootPromotionResult(
            outcome="promoted" if intent["state"] == "committed" else "prepared",
            batch_id=batch_id,
            revision=revision,
            intent_id=intent["id"],
            receipt_ids=receipt_ids,
            head_sha=intent["prepared_sha"],
        )

    @staticmethod
    def _identity(batch_id: str, revision: int) -> dict[str, str]:
        domain_key = f"root:{batch_id}:{revision}"
        return {
            "domain_key": domain_key,
            "intent_id": "root-intent-" + str(uuid.uuid5(_IDENTITY_NAMESPACE, domain_key)),
        }

    @staticmethod
    def _receipt_id(batch_id: str, revision: int, ordinal: int) -> str:
        return "receipt-" + str(
            uuid.uuid5(_IDENTITY_NAMESPACE, f"root-receipt:{batch_id}:{revision}:{ordinal}")
        )

    @staticmethod
    def _mutation_id(intent_id: str) -> str:
        return str(uuid.uuid5(_IDENTITY_NAMESPACE, f"root-main:{intent_id}"))

    async def _crash(self, phase: str) -> None:
        if self.crash_hook is None:
            return
        value = self.crash_hook(phase)
        if inspect.isawaitable(value):
            await value


__all__ = [
    "RootAttestationProof",
    "RootAttestationSubject",
    "RootPromotionInvariantError",
    "RootPromotionResult",
    "RootPromotionService",
]
