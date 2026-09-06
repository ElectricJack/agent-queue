"""Exact candidate-tree trust and GitHub App attestation publication."""

from __future__ import annotations

import hashlib
import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import insert, select, update

from src.database.tables import (
    integration_batches,
    integration_attestation_publications,
    integration_candidate_publications,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_repair_operations,
    integration_repair_stages,
    projects,
)
from src.git.github_app import GitHubAppError, GitHubRepositoryBinding
from src.git.manager import GitError
from src.integration.ci import (
    TRUST_MANIFEST_PATH,
    AttestationError,
    AuthenticatedGitHubObserver,
    CIService,
    CandidateCISubject,
    IntegrationTrustManifest,
    TrustedCIObservation,
    select_trusted_attestation,
)
from src.integration.main_promotion import RootAttestationProof, RootAttestationSubject


_MAX_TRUST_BYTES = 64 * 1024
_PUBLICATION_LEASE_SECONDS = 300.0
AppClientFactory = Callable[[GitHubRepositoryBinding], Awaitable[Any] | Any]
EnablementReader = Callable[[str], Awaitable[bool | tuple[str, ...]] | bool | tuple[str, ...]]


class AttestationPublicationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal[
        "published", "already_published", "not_green", "stale", "configuration_blocked"
    ]
    subject: RootAttestationSubject
    proof: RootAttestationProof | None = None


class IntegrationEnablementProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    ready: bool
    blockers: tuple[str, ...]


class IntegrationAttestationService:
    """Resolve and publish one exact, App-authenticated root attestation."""

    def __init__(
        self,
        db: Any,
        *,
        data_dir: str | Path,
        git_manager: Any,
        app_client_factory: AppClientFactory | None,
        protection_reader: EnablementReader | None = None,
        probe_reader: EnablementReader | None = None,
        debug_class_reader: EnablementReader | None = None,
        crash_hook: Callable[[str], Awaitable[None] | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.data_dir = Path(data_dir)
        self.git = git_manager
        self.app_client_factory = app_client_factory
        self.protection_reader = protection_reader
        self.probe_reader = probe_reader
        self.debug_class_reader = debug_class_reader
        self.crash_hook = crash_hook
        self.clock = clock
        self._clients: dict[GitHubRepositoryBinding, Any] = {}
        self._owned_publications: dict[str, str] = {}

    async def publish(self, subject: RootAttestationSubject) -> AttestationPublicationResult:
        if not isinstance(subject, RootAttestationSubject):
            raise TypeError("publish requires RootAttestationSubject")
        initial = await self._green_state(subject, allow_promoting=False)
        if initial is None:
            return AttestationPublicationResult(outcome="stale", subject=subject)
        try:
            trust, client = await self._load_trust(initial)
            observation = await AuthenticatedGitHubObserver(client).observe(
                trust, subject.candidate_sha
            )
            if not isinstance(observation, TrustedCIObservation):
                return AttestationPublicationResult(outcome="not_green", subject=subject)
            payload = observation.payload
            if payload.external_id != initial["aggregate_external_id"]:
                return AttestationPublicationResult(outcome="not_green", subject=subject)

            claim_status, claim = await self._reserve_publication(initial)
            if claim_status == "stale":
                return AttestationPublicationResult(outcome="stale", subject=subject)
            if claim_status == "wait":
                return AttestationPublicationResult(
                    outcome="configuration_blocked", subject=subject
                )
            if claim_status == "owner":
                await self._crash("after_publication_reservation")

            records = await self._attestation_records(client, trust, subject.candidate_sha)
            trusted_records = self._trusted_records(records, trust)
            if trusted_records:
                proof = self._proof_from_records(records, trust, subject, initial)
                if proof is None:
                    return AttestationPublicationResult(
                        outcome="configuration_blocked", subject=subject
                    )
                if not await self._finish_publication(initial, claim, proof):
                    return AttestationPublicationResult(outcome="stale", subject=subject)
                return AttestationPublicationResult(
                    outcome="already_published", subject=subject, proof=proof
                )

            if claim_status in {"reconcile", "published"}:
                return AttestationPublicationResult(
                    outcome="configuration_blocked", subject=subject
                )
            if not await self._mark_publication_prewrite(initial, claim):
                return AttestationPublicationResult(
                    outcome="configuration_blocked", subject=subject
                )

            await AuthenticatedGitHubObserver(client).publish(trust, payload)
        except (AttestationError, GitHubAppError, GitError, OSError, ValueError, ValidationError):
            return AttestationPublicationResult(
                outcome="configuration_blocked", subject=subject
            )

        await self._crash("after_attestation_publication")
        try:
            records = await self._attestation_records(client, trust, subject.candidate_sha)
            proof = self._proof_from_records(records, trust, subject, initial)
        except (AttestationError, GitHubAppError, GitError, OSError, ValueError, ValidationError):
            return AttestationPublicationResult(
                outcome="configuration_blocked", subject=subject
            )
        if proof is None:
            return AttestationPublicationResult(
                outcome="configuration_blocked", subject=subject
            )
        if not await self._finish_publication(initial, claim, proof):
            return AttestationPublicationResult(outcome="stale", subject=subject)
        return AttestationPublicationResult(outcome="published", subject=subject, proof=proof)

    async def resolve(self, subject: RootAttestationSubject) -> RootAttestationProof | None:
        if not isinstance(subject, RootAttestationSubject):
            raise TypeError("resolve requires RootAttestationSubject")
        initial = await self._green_state(subject, allow_promoting=True)
        if initial is None:
            return None
        claim = await self._published_publication(initial)
        if claim is None:
            return None
        try:
            trust, client = await self._load_trust(initial)
            records = await self._attestation_records(client, trust, subject.candidate_sha)
            proof = self._proof_from_records(records, trust, subject, initial)
        except (AttestationError, GitHubAppError, GitError, OSError, ValueError, ValidationError):
            return None
        if (
            proof is None
            or proof.subject() != subject
            or proof.check_run_id != claim["check_run_id"]
        ):
            return None
        rechecked = await self._published_publication(initial)
        if rechecked is None or rechecked["check_run_id"] != claim["check_run_id"]:
            return None
        return proof

    async def handle_candidate_ci(self, row: dict[str, Any], _now: float) -> dict[str, Any]:
        """Observe a pending candidate, then publish its exact attestation."""
        try:
            candidate = CandidateCISubject.model_validate(
                {
                    "operation_id": row["operation_id"],
                    "batch_id": row["batch_id"],
                    "revision": row["revision"],
                    "candidate_sha": row["candidate_sha"],
                }
            )
            pending = await self._pending_state(candidate)
            if pending is None:
                return {"outcome": "stale_subject"}
            trust, client = await self._load_trust(pending)
            if pending["candidate_state"] != "green":
                observed = await CIService(
                    self.db,
                    trust,
                    AuthenticatedGitHubObserver(client),
                    clock=self.clock,
                ).observe_candidate(candidate)
                if observed["outcome"] != "green":
                    return observed
            root_subject = RootAttestationSubject(
                repository_numeric_id=pending["repository_numeric_id"],
                repository_full_name=pending["repository_full_name"],
                operation_id=candidate.operation_id,
                batch_id=candidate.batch_id,
                revision=candidate.revision,
                candidate_sha=candidate.candidate_sha,
                required_check_version=pending["required_check_version"],
            )
            published = await self.publish(root_subject)
            return {
                "outcome": published.outcome,
                **(
                    {"proof": published.proof.model_dump(mode="json")}
                    if published.proof is not None
                    else {}
                ),
            }
        except (AttestationError, GitHubAppError, GitError, OSError, ValueError, ValidationError):
            return {"outcome": "configuration_blocked"}

    async def enablement_blockers(
        self, canonical_repository_id: str
    ) -> IntegrationEnablementProbeResult:
        blockers: list[str] = []
        if self.app_client_factory is None:
            blockers.append("missing_trusted_integration_app")
        repository = await self.db.get_repo(canonical_repository_id)
        if (
            repository is None
            or not repository.url.startswith("https://github.com/")
            or not repository.url.endswith(".git")
        ):
            blockers.append("repository_mismatch")
        await self._project_reader(
            self.debug_class_reader,
            canonical_repository_id,
            "debug_intelligence_class_unresolved",
            blockers,
        )
        await self._project_reader(
            self.protection_reader,
            canonical_repository_id,
            "branch_protection_incompatible",
            blockers,
        )
        await self._project_reader(
            self.probe_reader,
            canonical_repository_id,
            "scratch_probe_missing_or_failed",
            blockers,
        )
        unique = tuple(dict.fromkeys(blockers))
        return IntegrationEnablementProbeResult(ready=not unique, blockers=unique)

    async def _load_trust(self, state: dict[str, Any]) -> tuple[IntegrationTrustManifest, Any]:
        binding = GitHubRepositoryBinding(
            state["repository_numeric_id"], state["repository_full_name"]
        )
        client = await self._client(binding)
        token = await client.installation_token()
        store = self._store(state["canonical_repository_id"])
        destination_ref = "refs/aq/attestation-trust/" + hashlib.sha256(
            f"{state['batch_id']}:{state['revision']}".encode()
        ).hexdigest()
        imported = await self.git.afetch_exact_oid_with_app_auth(
            str(store),
            repository=binding,
            token=token,
            oid=state["candidate_sha"],
            destination_ref=destination_ref,
        )
        if imported != state["candidate_sha"]:
            raise AttestationError("candidate trust import changed identity")
        result = await self.git.arun_git_result(
            ["show", f"{state['candidate_sha']}:{TRUST_MANIFEST_PATH}"],
            cwd=str(store),
            env={"LC_ALL": "C"},
        )
        if result.returncode != 0:
            raise AttestationError("candidate trust manifest is missing")
        raw = result.stdout.encode("utf-8")
        if len(raw) > _MAX_TRUST_BYTES:
            raise AttestationError("candidate trust manifest is too large")
        trust = _parse_trust_manifest(raw)
        if (
            trust.canonical_repository_id != state["canonical_repository_id"]
            or trust.repository_id != binding.repository_id
            or trust.full_name != binding.full_name
            or trust.attestation_app_id != client.config.app_id
            or trust.required_checks.version != state["required_check_version"]
            or trust.required_checks.names != state["required_check_names"]
            or str(trust.ci_producer_app_id) != state["ci_producer_id"]
        ):
            raise AttestationError("candidate trust manifest does not match durable authority")
        return trust, client

    async def _client(self, binding: GitHubRepositoryBinding) -> Any:
        if self.app_client_factory is None:
            raise AttestationError("trusted integration App is unavailable")
        if binding in self._clients:
            return self._clients[binding]
        value = self.app_client_factory(binding)
        if inspect.isawaitable(value):
            value = await value
        if (
            value is None
            or value.repository != binding
            or value.repository.forge_host != "github.com"
            or isinstance(value.config.app_id, bool)
            or not isinstance(value.config.app_id, int)
            or value.config.app_id <= 0
        ):
            raise AttestationError("trusted integration App binding is invalid")
        self._clients[binding] = value
        return value

    async def _green_state(
        self, subject: RootAttestationSubject, *, allow_promoting: bool
    ) -> dict[str, Any] | None:
        state = await self._locked_state(subject.batch_id, subject.revision)
        if state is None or not self._subject_matches(subject, state):
            return None
        if state["candidate_state"] != "green" or state["batch_lifecycle"] not in (
            {"testing", "promoting"} if allow_promoting else {"testing"}
        ):
            return None
        return state

    async def _pending_state(self, subject: CandidateCISubject) -> dict[str, Any] | None:
        state = await self._locked_state(subject.batch_id, subject.revision)
        if state is None:
            return None
        if (
            state["operation_id"] != subject.operation_id
            or state["candidate_sha"] != subject.candidate_sha
            or state["candidate_state"] not in {"built", "testing", "green"}
            or state["batch_lifecycle"] not in {"testing", "repairing"}
        ):
            return None
        return state

    @staticmethod
    def _publication_id(state: dict[str, Any]) -> str:
        digest = hashlib.sha256(
            f"{state['batch_id']}\0{state['revision']}".encode()
        ).hexdigest()
        return f"integration-attestation-{digest}"

    @staticmethod
    def _publication_identity(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": IntegrationAttestationService._publication_id(state),
            "project_id": state["project_id"],
            "batch_id": state["batch_id"],
            "revision": state["revision"],
            "operation_id": state["operation_id"],
            "head_sha": state["candidate_sha"],
            "ci_evidence_id": state["ci_evidence_id"],
            "external_id": state["aggregate_external_id"],
        }

    async def _reserve_publication(
        self, state: dict[str, Any]
    ) -> tuple[Literal["owner", "wait", "reconcile", "published", "stale"], dict[str, Any]]:
        identity = self._publication_identity(state)
        now = self.clock()
        nonce = uuid.uuid4().hex
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project_id"])
            if not await self._publication_subject_current_on(conn, state):
                return "stale", {}
            row = (
                await conn.execute(
                    select(integration_attestation_publications)
                    .where(integration_attestation_publications.c.id == identity["id"])
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if row is None:
                await conn.execute(
                    insert(integration_attestation_publications).values(
                        **identity,
                        execution_nonce=nonce,
                        state="reserved",
                        prewrite_at=None,
                        check_run_id=None,
                        expires_at=now + _PUBLICATION_LEASE_SECONDS,
                        created_at=now,
                        updated_at=now,
                    )
                )
                self._owned_publications[identity["id"]] = nonce
                return "owner", {
                    **identity,
                    "execution_nonce": nonce,
                    "prewrite_at": None,
                }
            row = dict(row)
            if any(row[key] != value for key, value in identity.items()):
                raise AttestationError("attestation reservation identity changed")
            if row["state"] == "published":
                return "published", row
            if row["prewrite_at"] is not None:
                if self._owned_publications.get(identity["id"]) == row["execution_nonce"]:
                    return "reconcile", row
                return ("wait" if float(row["expires_at"]) > now else "reconcile"), row
            if self._owned_publications.get(identity["id"]) == row["execution_nonce"]:
                return "owner", row
            if float(row["expires_at"]) > now:
                return "wait", row
            takeover = await conn.execute(
                update(integration_attestation_publications)
                .where(
                    integration_attestation_publications.c.id == identity["id"],
                    integration_attestation_publications.c.state == "reserved",
                    integration_attestation_publications.c.prewrite_at.is_(None),
                    integration_attestation_publications.c.execution_nonce
                    == row["execution_nonce"],
                    integration_attestation_publications.c.expires_at <= now,
                )
                .values(
                    execution_nonce=nonce,
                    expires_at=now + _PUBLICATION_LEASE_SECONDS,
                    updated_at=now,
                )
            )
            if takeover.rowcount != 1:
                return "wait", row
            self._owned_publications[identity["id"]] = nonce
            return "owner", {
                **row,
                "execution_nonce": nonce,
                "expires_at": now + _PUBLICATION_LEASE_SECONDS,
            }

    async def _mark_publication_prewrite(
        self, state: dict[str, Any], claim: dict[str, Any]
    ) -> bool:
        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project_id"])
            if not await self._publication_subject_current_on(conn, state):
                return False
            result = await conn.execute(
                update(integration_attestation_publications)
                .where(
                    integration_attestation_publications.c.id == claim["id"],
                    integration_attestation_publications.c.state == "reserved",
                    integration_attestation_publications.c.execution_nonce
                    == claim["execution_nonce"],
                    integration_attestation_publications.c.prewrite_at.is_(None),
                    integration_attestation_publications.c.expires_at > now,
                )
                .values(prewrite_at=now, updated_at=now)
            )
            if result.rowcount != 1:
                return False
        claim["prewrite_at"] = now
        return True

    async def _finish_publication(
        self,
        state: dict[str, Any],
        claim: dict[str, Any],
        proof: RootAttestationProof,
    ) -> bool:
        now = self.clock()
        identity = self._publication_identity(state)
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project_id"])
            if not await self._publication_subject_current_on(conn, state):
                return False
            row = (
                await conn.execute(
                    select(integration_attestation_publications)
                    .where(integration_attestation_publications.c.id == claim["id"])
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if row is None or any(row[key] != value for key, value in identity.items()):
                return False
            if row["state"] == "published":
                return (
                    row["check_run_id"] == proof.check_run_id
                    and row["external_id"] == proof.external_id
                )
            if row["execution_nonce"] != claim["execution_nonce"]:
                return False
            prewrite_at = row["prewrite_at"] if row["prewrite_at"] is not None else now
            result = await conn.execute(
                update(integration_attestation_publications)
                .where(
                    integration_attestation_publications.c.id == row["id"],
                    integration_attestation_publications.c.state == "reserved",
                    integration_attestation_publications.c.execution_nonce
                    == claim["execution_nonce"],
                )
                .values(
                    state="published",
                    prewrite_at=prewrite_at,
                    check_run_id=proof.check_run_id,
                    updated_at=now,
                )
            )
            return result.rowcount == 1

    async def _published_publication(
        self, state: dict[str, Any]
    ) -> dict[str, Any] | None:
        identity = self._publication_identity(state)
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project_id"])
            if not await self._publication_subject_current_on(conn, state):
                return None
            row = (
                await conn.execute(
                    select(integration_attestation_publications)
                    .where(
                        integration_attestation_publications.c.id == identity["id"],
                        integration_attestation_publications.c.state == "published",
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if row is None or any(row[key] != value for key, value in identity.items()):
                return None
            return dict(row)

    async def _publication_subject_current_on(
        self, conn: Any, state: dict[str, Any]
    ) -> bool:
        batch = (
            await conn.execute(
                select(integration_batches)
                .where(integration_batches.c.id == state["batch_id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        candidate = (
            await conn.execute(
                select(integration_candidate_revisions)
                .where(
                    integration_candidate_revisions.c.batch_id == state["batch_id"],
                    integration_candidate_revisions.c.revision == state["revision"],
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        operation = (
            await conn.execute(
                select(integration_repair_operations)
                .where(integration_repair_operations.c.id == state["operation_id"])
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
        return bool(
            batch is not None
            and candidate is not None
            and operation is not None
            and stage is not None
            and batch["project_id"] == state["project_id"]
            and batch["repository_id"] == state["canonical_repository_id"]
            and int(batch["current_revision"]) == state["revision"]
            and batch["lifecycle"] == state["batch_lifecycle"]
            and batch["tested_candidate_sha"] == state["candidate_sha"]
            and batch["ci_evidence_id"] == state["ci_evidence_id"]
            and candidate["state"] == "green"
            and candidate["head_sha"] == state["candidate_sha"]
            and candidate["ci_evidence_id"] == state["ci_evidence_id"]
            and operation["state"] in {"active", "escalated"}
            and int(operation["active_stage"]) == int(stage["ordinal"])
            and stage["state"] == "awaiting_completion"
        )

    async def _locked_state(self, batch_id: str, revision: int) -> dict[str, Any] | None:
        async with self.db.immediate() as conn:
            initial = (
                await conn.execute(
                    select(integration_batches.c.project_id).where(
                        integration_batches.c.id == batch_id
                    )
                )
            ).scalar_one_or_none()
            if initial is None:
                return None
            await self.db.lock_hierarchy_project(conn, initial)

            async def one(statement):
                return (await conn.execute(statement.with_for_update())).mappings().one_or_none()

            project = await one(select(projects).where(projects.c.id == initial))
            batch = await one(select(integration_batches).where(integration_batches.c.id == batch_id))
            candidate = await one(
                select(integration_candidate_revisions).where(
                    integration_candidate_revisions.c.batch_id == batch_id,
                    integration_candidate_revisions.c.revision == revision,
                )
            )
            operation = await one(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.batch_id == batch_id,
                    integration_repair_operations.c.episode_id == batch_id,
                )
            )
            stage = None
            if operation is not None:
                stage = await one(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.operation_id == operation["id"],
                        integration_repair_stages.c.ordinal == operation["active_stage"],
                    )
                )
            publication = await one(
                select(integration_candidate_publications).where(
                    integration_candidate_publications.c.batch_id == batch_id,
                    integration_candidate_publications.c.revision == revision,
                )
            )
            evidence = None
            if candidate is not None and candidate["ci_evidence_id"] is not None:
                evidence = await one(
                    select(integration_check_evidence).where(
                        integration_check_evidence.c.id == candidate["ci_evidence_id"]
                    )
                )
            if any(value is None for value in (project, batch, candidate, operation, stage, publication)):
                return None
            required = operation["policy_snapshot"].get("root", {}).get("required_checks", {})
            names = required.get("names") if isinstance(required, dict) else None
            if (
                project["status"] != "ACTIVE"
                or project["hierarchical_integration_mode"] != "train"
                or project["integration_repository_id"] != batch["repository_id"]
                or batch["project_id"] != project["id"]
                or int(batch["current_revision"]) != revision
                or operation["target_kind"] != "batch"
                or operation["state"] not in {"active", "escalated"}
                or operation["id"] != stage["operation_id"]
                or stage["state"] != "awaiting_completion"
                or publication["state"] != "pr_published"
                or publication["repository_id"] != batch["repository_id"]
                or publication["head_sha"] != candidate["head_sha"]
                or not isinstance(names, list)
                or not names
                or any(not isinstance(name, str) or not name for name in names)
                or required.get("version") != operation["required_check_version"]
                or not isinstance(required.get("producer_id"), str)
            ):
                return None
            if candidate["state"] == "green":
                if (
                    evidence is None
                    or batch["tested_candidate_sha"] != candidate["head_sha"]
                    or batch["ci_evidence_id"] != candidate["ci_evidence_id"]
                    or evidence["operation_id"] != operation["id"]
                    or evidence["batch_id"] != batch_id
                    or int(evidence["candidate_revision"]) != revision
                    or evidence["producer_id"] != required["producer_id"]
                    or evidence["required_check_version"] != required["version"]
                    or list(evidence["checks"].keys()) != names
                    or any(value != "success" for value in evidence["checks"].values())
                    or evidence["conclusion"] != "success"
                    or evidence["classification"] != "conclusive"
                ):
                    return None
            return {
                "project_id": project["id"],
                "canonical_repository_id": batch["repository_id"],
                "repository_numeric_id": publication["repository_numeric_id"],
                "repository_full_name": publication["repository_full_name"],
                "batch_id": batch_id,
                "revision": revision,
                "batch_lifecycle": batch["lifecycle"],
                "candidate_state": candidate["state"],
                "candidate_sha": candidate["head_sha"],
                "operation_id": operation["id"],
                "required_check_version": required["version"],
                "required_check_names": tuple(names),
                "ci_producer_id": required["producer_id"],
                "ci_evidence_id": candidate["ci_evidence_id"],
                "aggregate_external_id": evidence["run_id"] if evidence is not None else None,
                "publication_id": publication["idempotency_key"],
            }

    @staticmethod
    def _subject_matches(subject: RootAttestationSubject, state: dict[str, Any]) -> bool:
        return bool(
            subject.repository_numeric_id == state["repository_numeric_id"]
            and subject.repository_full_name == state["repository_full_name"]
            and subject.operation_id == state["operation_id"]
            and subject.batch_id == state["batch_id"]
            and subject.revision == state["revision"]
            and subject.candidate_sha == state["candidate_sha"]
            and subject.required_check_version == state["required_check_version"]
        )

    @staticmethod
    async def _attestation_records(
        client: Any, trust: IntegrationTrustManifest, head_sha: str
    ) -> list[dict[str, Any]]:
        owner, repository = trust.full_name.split("/", 1)
        return await client.paged_items(
            f"/repos/{owner}/{repository}/commits/{head_sha}/check-runs"
            f"?check_name={quote(trust.attestation_name, safe='')}&filter=all&per_page=100",
            key="check_runs",
        )

    @staticmethod
    def _trusted_records(
        records: list[dict[str, Any]], trust: IntegrationTrustManifest
    ) -> list[dict[str, Any]]:
        trusted: list[dict[str, Any]] = []
        for record in records:
            if record.get("name") != trust.attestation_name:
                continue
            app = record.get("app")
            app_id = app.get("id") if isinstance(app, dict) else None
            if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
                raise AttestationError("exact-name attestation App identity is malformed")
            if app_id == trust.attestation_app_id:
                trusted.append(record)
        return trusted

    @classmethod
    def _proof_from_records(
        cls,
        records: list[dict[str, Any]],
        trust: IntegrationTrustManifest,
        subject: RootAttestationSubject,
        state: dict[str, Any],
    ) -> RootAttestationProof | None:
        selected = select_trusted_attestation(
            records, trust, expected_head_sha=subject.candidate_sha
        )
        payload = selected.payload
        if payload.external_id != state["aggregate_external_id"]:
            return None
        return RootAttestationProof(
            **subject.model_dump(),
            check_run_id=selected.record_id,
            external_id=payload.external_id,
        )

    def _store(self, canonical_repository_id: str) -> Path:
        return self.data_dir / "integration-repositories" / (
            hashlib.sha256(canonical_repository_id.encode()).hexdigest() + ".git"
        )

    async def _crash(self, phase: str) -> None:
        if self.crash_hook is None:
            return
        value = self.crash_hook(phase)
        if inspect.isawaitable(value):
            await value

    @staticmethod
    async def _project_reader(
        reader: EnablementReader | None,
        repository_id: str,
        absent: str,
        blockers: list[str],
    ) -> None:
        if reader is None:
            blockers.append(absent)
            return
        try:
            value = reader(repository_id)
            if inspect.isawaitable(value):
                value = await value
        except Exception:
            blockers.append(absent)
            return
        if value is True:
            return
        if isinstance(value, tuple):
            blockers.extend(value or (absent,))
        else:
            blockers.append(absent)


def _parse_trust_manifest(raw: bytes) -> IntegrationTrustManifest:
    try:
        decoded = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        return IntegrationTrustManifest.model_validate(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, AttestationError, ValidationError) as exc:
        raise AttestationError("candidate trust manifest is invalid") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AttestationError("candidate trust manifest contains duplicate fields")
        value[key] = item
    return value


__all__ = [
    "AttestationPublicationResult",
    "IntegrationAttestationService",
    "IntegrationEnablementProbeResult",
]
